"""
Rafiq Bot API — PRODUCTION v5.1
================================
Changes vs v5.0:
- FIX: get_parenting_plans uses COALESCE(plan_duration, 15) to handle missing column
- NEW: Query expansion before embedding (expand_query_for_embedding)
- NEW: Expanded queries used in both retrieval and ingestion embedding
"""

from dotenv import load_dotenv
load_dotenv()

import os, json, uuid, re, io, logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import psycopg2


# ══════════════════════════════════════════════
# TRANSLATIONS
# ══════════════════════════════════════════════

Lang = Literal["ar", "en"]

_T: dict[str, dict[str, str]] = {
    "gemini_disabled": {
        "ar": "ميزة الشات غير مفعّلة. التقييم والـ Memory شغالين ✅",
        "en": "Chat feature is currently disabled. Assessment and Memory are working ✅",
    },
    "ok":                     {"ar": "تم بنجاح",       "en": "Success"},
    "out_of_scope_reply":     {"ar": "أنا بوت (رفيق) متخصص في دعم الأسرة. مش بقدر أساعد في برمجة/أدوية/تشخيص.",
                               "en": "I'm Rafiq, a family support assistant. I can't help with programming, medication, or diagnosis."},
    "out_of_scope_card":      {"ar": "اسأل عن: مراهقة، عصبية، موبايل، تنمر، مذاكرة، قصص أطفال، ألعاب، تقييم شخصية.",
                               "en": "Ask about: teen communication, anger, screen time, bullying, studying, kids stories, games, personality assessment."},
    "scope_refusal":          {"ar": "سؤالك خارج نطاق رفيق. اسأل عن مشكلة أسرية/تربوية وأنا أساعدك فورًا ✅",
                               "en": "Your question is outside Rafiq's scope. Ask about a parenting or family issue and I'll help right away ✅"},
    "risk_high":              {"ar": "أنا قلقان عليك جدًا. تواصل فورًا مع شخص كبير موثوق قريب منك أو خدمات الطوارئ.",
                               "en": "I'm very concerned about you. Please immediately reach out to a trusted adult or call emergency services."},
    "risk_high_card":         {"ar": "في الحالات العاجلة لازم تدخل مختص فورًا. رفيق للدعم العام فقط.",
                               "en": "In urgent cases a specialist must intervene immediately. Rafiq is for general support only."},
    "kids_safety":            {"ar": "خلّينا نخلي المحتوى مناسب للأطفال 🙏 قوليلي سن الطفل والموضوع.",
                               "en": "Let's keep content child-appropriate 🙏 Please share the child's age and topic."},
    "low_conf_prefix":        {"ar": "الموضوع محتاج تفاصيل أكتر. ",
                               "en": "I need a bit more context to help effectively. "},
    "low_conf_suffix":        {"ar": " ولو تقدر احكيلي موقف حصل قريب.",
                               "en": " If you can, share a recent situation that happened."},
    "confidence_score":       {"ar": "درجة الثقة",                     "en": "Confidence Score"},
    "follow_up":              {"ar": "سؤال متابعة",                    "en": "Follow-up"},
    "verify_fallback":        {"ar": "أنا معاك ✅ بس خلّيني أسألك: ",  "en": "I'm here for you ✅ Let me ask: "},
    "assessment_note":        {"ar": "النتيجة إرشادية وليست تشخيصًا طبيًا.",
                               "en": "This result is indicative, not a clinical diagnosis."},
    "assessment_result_title":{"ar": "نتيجة تقييم شخصية الطفل",       "en": "Child Personality Assessment Result"},
    "daily_tip_notif_title":  {"ar": "💡 نصيحة جديدة من رفيق",        "en": "💡 New Parenting Tip from Rafiq"},
    "daily_tip_notif_body_prefix": {"ar": "", "en": ""},
    "plan_notif_title":       {"ar": "📋 خطتك التربوية جاهزة 🎉",     "en": "Your Parenting Plan is Ready 🎉"},
    "plan_notif_body":        {"ar": "أعددنا خطة مخصصة لـ 15 يومًا لطفلك. افتحها الآن.",
                               "en": "We created a personalized 15-day plan for your child. Open it now."},
    "plan_created_title":     {"ar": "تم إنشاء الخطة بنجاح",          "en": "Parenting plan generated successfully"},
    "token_saved":            {"ar": "تم حفظ رمز الإشعار بنجاح",      "en": "FCM token saved successfully"},
    "no_fcm_token":           {"ar": "المستخدم لا يملك رمز إشعار. استدعِ POST /register-token أولًا.",
                               "en": "User has no registered FCM token. Call POST /register-token first."},
    "fcm_token_expired":      {"ar": "رمز FCM لم يعد صالحًا. يُرجى إعادة التسجيل عبر POST /register-token.",
                               "en": "FCM token is no longer valid (device unregistered). Please re-register via POST /register-token."},
    "firebase_not_configured":{"ar": "Firebase غير مُفعَّل — تم حفظ الخطة لكن لم يُرسَل إشعار.",
                               "en": "Firebase is not configured — plan saved but no push notification sent."},
    "no_assessment_found":    {"ar": "لا يوجد تقييم لهذا المستخدم. أكمل التقييم عبر POST /assessment/submit أولًا.",
                               "en": "No assessment found for this user. Please complete an assessment first via POST /assessment/submit."},
    "no_plan_found":          {"ar": "لا توجد خطة تربوية لهذا المستخدم.",
                               "en": "No parenting plan found for this user."},
    "user_not_found":         {"ar": "المستخدم غير موجود.",            "en": "User not found."},
    "pdf_unavailable":        {"ar": "تصدير PDF غير متاح — مكتبة reportlab غير مثبّتة.",
                               "en": "PDF export is unavailable — reportlab is not installed."},
    "pdf_main_title":         {"ar": "خطة تربوية مخصصة — رفيق AI",    "en": "Personalized Parenting Plan — Rafiq AI"},
    "pdf_subtitle":           {"ar": "خطة 15 يومًا",                   "en": "15-Day Plan"},
    "pdf_label_parent_name":  {"ar": "اسم الوالد/الوالدة",             "en": "Parent Name"},
    "pdf_label_child_name":   {"ar": "اسم الطفل",                      "en": "Child Name"},
    "pdf_label_user_id":      {"ar": "معرف المستخدم",                  "en": "User ID"},
    "pdf_label_child_age":    {"ar": "عمر الطفل",                      "en": "Child Age"},
    "pdf_label_archetype":    {"ar": "النمط الشخصي",                   "en": "Child Profile"},
    "pdf_label_generated":    {"ar": "تاريخ الإنشاء",                  "en": "Generated"},
    "pdf_label_age_unknown":  {"ar": "غير محدد",                       "en": "Not specified"},
    "pdf_section_plan":       {"ar": "الخطة التربوية",                 "en": "Parenting Plan"},
    "pdf_footer_line1":       {"ar": "أُنشئت بواسطة رفيق AI — هذه الخطة إرشادية وليست تشخيصًا طبيًا.",
                               "en": "Generated by Rafiq AI — This plan is for guidance only and is not a clinical diagnosis."},
    "card_out_of_scope":      {"ar": "خارج نطاق رفيق",                 "en": "Out of scope"},
    "card_important":         {"ar": "مهم جدًا",                       "en": "Important"},
    "card_tip":               {"ar": "نصيحة عملية",                    "en": "Practical Tip"},
    "card_story":             {"ar": "قصة للأطفال",                    "en": "Kids Story"},
    "card_game":              {"ar": "لعبة / نشاط",                    "en": "Activity / Game"},
    "card_books":             {"ar": "اقتراح قراءة",                   "en": "Book Suggestion"},
    "card_assessment":        {"ar": "تقييم شخصية الطفل",              "en": "Personality Assessment"},
    "card_refusal_reason_prefix": {"ar": "السبب: ",                    "en": "Reason: "},
    "child_appropriate_content":  {"ar": "محتوى مناسب للأطفال",       "en": "Child-appropriate content"},
    "choose_safe_topic":          {"ar": "اختر موضوعًا مناسبًا للأطفال.", "en": "Choose a safe, age-appropriate topic."},
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in ("ar", "en") else "ar"
    entry = _T.get(key, {})
    if isinstance(entry, dict):
        text = entry.get(lang) or entry.get("ar") or key
    else:
        text = entry or key
    return text.format(**kwargs) if kwargs else text


def detect_lang(text: str) -> Lang:
    ar = len(re.findall(r'[\u0600-\u06FF]', text))
    en = len(re.findall(r'[a-zA-Z]', text))
    return "ar" if ar >= en else "en"


def user_lang(preferred_language: Optional[str], fallback_text: str = "") -> Lang:
    if preferred_language in ("ar", "en"):
        return preferred_language  # type: ignore[return-value]
    return detect_lang(fallback_text)


# ══════════════════════════════════════════════
# MARKDOWN STRIPPING
# ══════════════════════════════════════════════

_MD_BOLD_ITALIC = re.compile(r'\*{1,3}(.+?)\*{1,3}', re.DOTALL)
_MD_BOLD_UNDER  = re.compile(r'_{2}(.+?)_{2}',        re.DOTALL)
_MD_ITALIC_UNDER= re.compile(r'_(.+?)_',              re.DOTALL)
_MD_HEADING     = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MD_HR          = re.compile(r'^[-*_]{3,}\s*$', re.MULTILINE)
_MD_BACKTICK    = re.compile(r'`{1,3}(.+?)`{1,3}', re.DOTALL)


def strip_markdown(text: str) -> str:
    if not text:
        return text
    text = _MD_BOLD_ITALIC.sub(r'\1', text)
    text = _MD_BOLD_UNDER.sub(r'\1',  text)
    text = _MD_ITALIC_UNDER.sub(r'\1', text)
    text = _MD_HEADING.sub('', text)
    text = _MD_HR.sub('', text)
    text = _MD_BACKTICK.sub(r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ══════════════════════════════════════════════
# OPTIONAL DEPENDENCIES
# ══════════════════════════════════════════════

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle,
        PageBreak, KeepTogether,
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    print("WARNING: reportlab not installed — PDF export disabled.")

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_display
    _ARABIC_SHAPING = True
except ImportError:
    _ARABIC_SHAPING = False
    print("WARNING: arabic-reshaper / python-bidi not installed.")

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials, messaging as fb_messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    firebase_admin = fb_credentials = fb_messaging = None
    _FIREBASE_AVAILABLE = False

try:
    from pgvector.psycopg2 import register_vector
    _PGVECTOR_AVAILABLE = True
except ImportError:
    _PGVECTOR_AVAILABLE = False
    print("INFO: pgvector python package not installed — vector storage disabled.")


# ══════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════

DEBUG          = os.getenv("RAFIQ_DEBUG", "0") == "1"
DATABASE_URL   = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)
ADMIN_KEY      = os.getenv("RAFIQ_ADMIN_KEY", "change-me")
ENABLE_VERIFY  = os.getenv("RAFIQ_VERIFY_OUTPUT", "0") == "1"

FONT_DIR         = os.getenv("RAFIQ_FONT_DIR", "/app/fonts")
FONT_NOTO_ARABIC = os.getenv("RAFIQ_FONT_ARABIC", os.path.join(FONT_DIR, "NotoSansArabic-Regular.ttf"))
FONT_NOTO_BOLD   = os.getenv("RAFIQ_FONT_BOLD",   os.path.join(FONT_DIR, "NotoSansArabic-Bold.ttf"))
FONT_NOTO_LATIN  = os.getenv("RAFIQ_FONT_LATIN",  os.path.join(FONT_DIR, "NotoSans-Regular.ttf"))

if ADMIN_KEY == "change-me":
    print("WARNING: RAFIQ_ADMIN_KEY is default.")

client = None
if GEMINI_ENABLED:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini initialized ✔")
    except Exception as exc:
        print("Gemini init failed:", exc)

FIREBASE_ENABLED = False
_FIREBASE_CREDS_JSON = os.getenv("FIREBASE_CREDENTIALS", "").strip()
if _FIREBASE_AVAILABLE and _FIREBASE_CREDS_JSON:
    try:
        _fb_cred_dict = json.loads(_FIREBASE_CREDS_JSON)
        _fb_cred      = fb_credentials.Certificate(_fb_cred_dict)
        firebase_admin.initialize_app(_fb_cred)
        FIREBASE_ENABLED = True
        print("Firebase initialized ✔")
    except Exception as _fb_exc:
        print(f"Firebase init failed: {_fb_exc}")

_FONT_ARABIC_REGISTERED = False
_FONT_LATIN_REGISTERED  = False


def _register_fonts() -> None:
    global _FONT_ARABIC_REGISTERED, _FONT_LATIN_REGISTERED
    if not _REPORTLAB_AVAILABLE:
        return
    try:
        if os.path.exists(FONT_NOTO_ARABIC):
            pdfmetrics.registerFont(TTFont("NotoArabic",     FONT_NOTO_ARABIC))
            _FONT_ARABIC_REGISTERED = True
        if os.path.exists(FONT_NOTO_BOLD):
            pdfmetrics.registerFont(TTFont("NotoArabicBold", FONT_NOTO_BOLD))
        if os.path.exists(FONT_NOTO_LATIN):
            pdfmetrics.registerFont(TTFont("NotoLatin",      FONT_NOTO_LATIN))
            _FONT_LATIN_REGISTERED = True
        if _FONT_ARABIC_REGISTERED:
            print("PDF Arabic fonts registered ✔")
        else:
            print("PDF fonts NOT found — falling back to Helvetica.")
    except Exception as exc:
        print(f"Font registration warning: {exc}")


# ══════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════

app = FastAPI(
    title="Rafiq Bot API",
    version="5.1.0",
    description="Family support & parenting assistant API — bilingual (ar/en) | FTS + pgvector RAG",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    _run_schema_migrations()
    _register_fonts()


# ══════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════

def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    if _PGVECTOR_AVAILABLE:
        try:
            register_vector(conn)
        except Exception:
            pass
    return conn


def _run_schema_migrations() -> None:
    """Apply all schema migrations idempotently."""
    if not DATABASE_URL:
        print("Skipping DB migrations — DATABASE_URL not set")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur  = conn.cursor()

        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language VARCHAR(5) DEFAULT 'ar';")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS parent_name VARCHAR(200);")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS child_name  VARCHAR(200);")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_tips (
                id         SERIAL PRIMARY KEY,
                user_id    VARCHAR(100),
                tip        TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS parenting_plans (
                id            SERIAL PRIMARY KEY,
                user_id       VARCHAR(100),
                plan_text     TEXT,
                plan_language VARCHAR(5)  DEFAULT 'en',
                plan_days     JSONB,
                parent_name   VARCHAR(200),
                child_name    VARCHAR(200),
                intro_letter  TEXT,
                plan_duration INTEGER     DEFAULT 15,
                created_at    TIMESTAMP   DEFAULT NOW()
            );
            """
        )
        for col_sql in [
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_language VARCHAR(5) DEFAULT 'en';",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_days     JSONB;",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS parent_name   VARCHAR(200);",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS child_name    VARCHAR(200);",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS intro_letter  TEXT;",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_duration INTEGER DEFAULT 15;",
        ]:
            cur.execute(col_sql)

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS faq_knowledge_base (
                id            SERIAL PRIMARY KEY,
                topic         VARCHAR(100)  NOT NULL,
                question      TEXT          NOT NULL,
                answer        TEXT          NOT NULL,
                tags          TEXT[]        DEFAULT '{}',
                age_min       INTEGER       DEFAULT 4,
                age_max       INTEGER       DEFAULT 18,
                lang          VARCHAR(5)    DEFAULT 'ar',
                search_vector TSVECTOR,
                source        VARCHAR(100)  DEFAULT 'manual',
                source_plan_id INTEGER,
                created_at    TIMESTAMP     DEFAULT NOW(),
                updated_at    TIMESTAMP     DEFAULT NOW()
            );
            """
        )
        for col_sql in [
            "ALTER TABLE faq_knowledge_base ADD COLUMN IF NOT EXISTS source         VARCHAR(100) DEFAULT 'manual';",
            "ALTER TABLE faq_knowledge_base ADD COLUMN IF NOT EXISTS source_plan_id INTEGER;",
        ]:
            cur.execute(col_sql)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_kb_fts   ON faq_knowledge_base USING GIN (search_vector);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_kb_topic ON faq_knowledge_base (topic);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_kb_lang  ON faq_knowledge_base (lang);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_kb_src   ON faq_knowledge_base (source);")

        cur.execute(
            """
            CREATE OR REPLACE FUNCTION faq_kb_search_vector_update()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('simple', COALESCE(NEW.question, '')), 'A') ||
                    setweight(to_tsvector('simple', COALESCE(NEW.answer,   '')), 'B') ||
                    setweight(to_tsvector('simple', COALESCE(array_to_string(NEW.tags, ' '), '')), 'C');
                NEW.updated_at := NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        cur.execute("DROP TRIGGER IF EXISTS trig_faq_kb_fts ON faq_knowledge_base;")
        cur.execute(
            """
            CREATE TRIGGER trig_faq_kb_fts
            BEFORE INSERT OR UPDATE ON faq_knowledge_base
            FOR EACH ROW EXECUTE FUNCTION faq_kb_search_vector_update();
            """
        )
        cur.execute(
            """
            UPDATE faq_knowledge_base
            SET search_vector =
                setweight(to_tsvector('simple', COALESCE(question, '')), 'A') ||
                setweight(to_tsvector('simple', COALESCE(answer,   '')), 'B') ||
                setweight(to_tsvector('simple', COALESCE(array_to_string(tags, ' '), '')), 'C')
            WHERE search_vector IS NULL;
            """
        )

        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_embeddings (
                    id            SERIAL PRIMARY KEY,
                    plan_id       INTEGER       NOT NULL REFERENCES parenting_plans(id) ON DELETE CASCADE,
                    user_id       VARCHAR(100)  NOT NULL,
                    chunk_index   INTEGER       DEFAULT 0,
                    chunk_text    TEXT          NOT NULL,
                    embedding     vector(768),
                    child_age     INTEGER,
                    child_profile VARCHAR(200),
                    lang          VARCHAR(5)    DEFAULT 'en',
                    created_at    TIMESTAMP     DEFAULT NOW()
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_emb_user ON plan_embeddings (user_id);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_emb_plan ON plan_embeddings (plan_id);"
            )
            print("DB migrations: pgvector plan_embeddings table ready ✔")
        except Exception as vec_exc:
            print(f"DB migrations: pgvector not available — vector table skipped ({vec_exc})")

        conn.commit()
        conn.close()
        print("DB migrations applied ✔ (v5.1)")
    except Exception as exc:
        print(f"DB migration warning: {exc}")


# ══════════════════════════════════════════════
# FULL-TEXT SEARCH
# ══════════════════════════════════════════════

def fts_knowledge_base(
    query: str,
    topic: Optional[str] = None,
    lang: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    if not query or not query.strip():
        return []

    results: List[Dict[str, Any]] = []

    try:
        conn = get_conn()
        cur  = conn.cursor()

        filter_clauses: List[str] = []
        params_fts:  List[Any]   = []
        params_ilike: List[Any]  = []

        if topic:
            filter_clauses.append("topic = %s")
            params_fts.append(topic)
            params_ilike.append(topic)
        if lang:
            filter_clauses.append("lang = %s")
            params_fts.append(lang)
            params_ilike.append(lang)

        where_extra = ("AND " + " AND ".join(filter_clauses)) if filter_clauses else ""

        raw_tokens = [
            re.sub(r"[^\w\u0600-\u06FF]", "", tok)
            for tok in query.strip().split()
            if len(tok) >= 2
        ]
        tokens = [tok for tok in raw_tokens if tok]

        if tokens:
            tsquery_str = " | ".join(tokens)

            fts_sql = f"""
                SELECT topic, question, answer, tags,
                       ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS rank,
                       source
                FROM   faq_knowledge_base
                WHERE  search_vector @@ to_tsquery('simple', %s)
                {where_extra}
                ORDER  BY rank DESC
                LIMIT  %s;
            """
            cur.execute(fts_sql, [tsquery_str, tsquery_str] + params_fts + [limit])
            rows = cur.fetchall()
            for row in rows:
                results.append({
                    "topic": row[0], "question": row[1], "answer": row[2],
                    "tags": row[3] or [], "rank": float(row[4]),
                    "method": "fts", "source": row[5] or "manual",
                })

        if not results:
            search_term  = tokens[0] if tokens else query.strip()
            like_pattern = f"%{search_term}%"
            ilike_sql = f"""
                SELECT topic, question, answer, tags, 1.0 AS rank, source
                FROM   faq_knowledge_base
                WHERE  (question ILIKE %s OR answer ILIKE %s
                        OR array_to_string(tags, ' ') ILIKE %s)
                {where_extra}
                ORDER  BY CASE WHEN question ILIKE %s THEN 0 ELSE 1 END,
                          updated_at DESC
                LIMIT  %s;
            """
            cur.execute(
                ilike_sql,
                [like_pattern, like_pattern, like_pattern, like_pattern]
                + params_ilike + [limit],
            )
            rows = cur.fetchall()
            for row in rows:
                results.append({
                    "topic": row[0], "question": row[1], "answer": row[2],
                    "tags": row[3] or [], "rank": float(row[4]),
                    "method": "ilike", "source": row[5] or "manual",
                })

        conn.close()
    except Exception as exc:
        print(f"[FTS] retrieval error: {exc}")
        results = []

    return results


def fts_or_kb_fallback(
    query: str,
    topic: str,
    age: Optional[int],
    lang: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 3,
) -> Tuple[List[Dict[str, Any]], bool]:
    db_results = fts_knowledge_base(query=query, topic=topic, lang=lang, user_id=user_id, limit=limit)
    if db_results:
        return db_results, True

    kb_res = kb_search_v2(topic=topic, query=query, age=age)
    tips = [
        {
            "topic":    item.get("topic", topic),
            "question": " ".join(item.get("tags", [])),
            "answer":   item.get("tip", ""),
            "tags":     item.get("tags", []),
            "rank":     0.5,
            "method":   "in_memory_kb",
        }
        for item in kb_res.tips
    ]
    return tips, False


# ══════════════════════════════════════════════
# IN-MEMORY KNOWLEDGE BASE
# ══════════════════════════════════════════════

KB: List[Dict[str, Any]] = [
    {"id": "kb_001", "topic": "teen_communication", "age_min": 12, "age_max": 18,
     "tags": ["مراهق", "مراهقة", "مش بيرد", "ساكت", "قافل"],
     "tip": "ابدئي في وقت هدوء بجملة: «أنا مهتمة أفهمك مش ألومك». اسألي سؤال واحد مفتوح وسيبي مساحة للرد."},
    {"id": "kb_002", "topic": "anger", "age_min": 6, "age_max": 18,
     "tags": ["عصبية", "غضب", "صراخ", "بيزعق"],
     "tip": "وقت الغضب قللي الكلام وثبتي حدود هادية. بعد ما يهدى: «إيه اللي ضايقك؟ وإيه الحل المرة الجاية؟»."},
    {"id": "kb_003", "topic": "screen_addiction", "age_min": 8, "age_max": 18,
     "tags": ["موبايل", "شاشات", "تيك توك", "إدمان"],
     "tip": "اعملي اتفاق مكتوب: وقت شاشة + وقت عيلة. قلّلي تدريجيًا (15 دقيقة) مع بديل ممتع مش عقاب."},
    {"id": "kb_004", "topic": "bullying", "age_min": 6, "age_max": 18,
     "tags": ["تنمر", "مدرسة", "سخرية", "بيضرب"],
     "tip": "صدّقي مشاعره، خدي تفاصيل بسيطة، تواصلي مع المدرسة، ودرّبيه على ردود قصيرة وطلب المساعدة."},
    {"id": "kb_005", "topic": "study_focus", "age_min": 8, "age_max": 18,
     "tags": ["مذاكرة", "تركيز", "تسويف", "واجب"],
     "tip": "قسّمي المذاكرة لبلوكات 25 دقيقة + 5 راحة. خلي البداية سهلة (أول 5 دقائق) لتكسير حاجز البدء."},
    {"id": "kb_100", "topic": "kids_stories", "age_min": 4, "age_max": 10,
     "tags": ["قصة", "قصص", "حكاية", "قبل النوم", "احكي"],
     "tip": (
         "قصة قصيرة (5 دقايق) — «نجمة والمشاركة»\n"
         "نجمة عندها لعبة جديدة، وكل ما أصحابها ييجوا تلعب لوحدها. "
         "في يوم، صحابها زعلوا ومشيوا. نجمة حسّت بالوحدة.\n"
         "ماما قالت: «المشاركة مش بتقلل لعبتك… بتكبر فرحتك».\n"
         "نجمة جرّبت تدي كل واحد دوره دقيقة، ولعبوا وضحكوا.\n"
         "الدرس: المشاركة + الدور.\nسؤال للطفل: إنت كنت هتعمل إيه لو كنت مكان نجمة؟"
     )},
    {"id": "kb_101", "topic": "activities_games", "age_min": 4, "age_max": 12,
     "tags": ["لعبة", "نشاط", "ملل", "بيت", "وقت فراغ"],
     "tip": (
         "لعبة 10 دقايق: «صيد المشاعر»\n"
         "الأدوات: ورق + قلم.\n"
         "اكتبوا 6 مشاعر، اسحبوا ورقة، الطفل يمثل موقف للمشاعر دي.\n"
         "وبعدها: «إيه اللي يساعدني لما أحس كده؟»\n"
         "الهدف: التعبير عن المشاعر + التهدئة."
     )},
    {"id": "kb_102", "topic": "book_recommendations", "age_min": 4, "age_max": 12,
     "tags": ["كتاب", "كتب", "قراءة", "اقترح كتب"],
     "tip": (
         "اقتراح كتب حسب السن:\n"
         "- سن 4–7: كتب مصوّرة عن الصداقة/المشاركة/الصدق.\n"
         "- سن 8–12: مغامرات + قيم (مسؤولية/شجاعة/تعاون).\n"
         "بعد القراءة اسألي: «إيه أكتر موقف عجبك؟ وإيه الدرس؟»"
     )},
    {"id": "kb_103", "topic": "assessment_personality", "age_min": 4, "age_max": 18,
     "tags": ["تقييم", "assessment", "شخصية", "قيادي", "اجتماعي"],
     "tip": (
         "We can run a personality assessment to help you understand your child better. "
         "Call GET /assessment/questions?age=X then POST /assessment/submit with the answers."
     )},
]


# ══════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    user_id: str
    messages: List[ChatMessage]
    child_age: Optional[int] = None
    preferred_language: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str


class UserUpsertReq(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    child_age: Optional[int] = None
    preferred_language: Optional[str] = "ar"
    parent_name: Optional[str] = None
    child_name: Optional[str] = None


class KbAddRequest(BaseModel):
    admin_key: str
    topic: str
    age_min: int = 6
    age_max: int = 18
    tags: List[str] = []
    tip: str


class AppEventRequest(BaseModel):
    user_id: str
    event_name: Literal[
        "open_app", "view_content", "save_tip", "start_chat", "complete_activity",
        "behavior_event", "view_assessment", "assessment_submit"
    ]
    meta: Dict[str, Any] = {}


class FeedbackReq(BaseModel):
    user_id: str
    message_id: str
    rating: Literal["up", "down"]
    comment: Optional[str] = None
    topic: Optional[str] = None


class AssessmentSubmitReq(BaseModel):
    user_id: str
    child_age: Optional[int] = None
    answers: List[Dict[str, Any]] = []
    behavior_signals: Optional[Dict[str, Any]] = None
    preferred_language: Optional[str] = None


class RegisterTokenReq(BaseModel):
    user_id: str
    fcm_token: str


class SendDailyTipReq(BaseModel):
    user_id: str
    tip: str


class FaqKbAddRequest(BaseModel):
    admin_key: str
    topic: str
    question: str
    answer: str
    tags: List[str] = []
    age_min: int = 4
    age_max: int = 18
    lang: str = "ar"


class GeneratePlanRequest(BaseModel):
    parent_name: Optional[str] = None
    child_name:  Optional[str] = None


AllowedTopic = Literal[
    "teen_communication", "anger", "screen_addiction", "bullying", "study_focus",
    "siblings_jealousy", "parents_conflict", "lying", "general_parenting",
    "kids_stories", "activities_games", "book_recommendations",
    "assessment_personality", "out_of_scope"
]
AllowedAction = Literal["answer_with_tips", "refuse_out_of_scope"]


class RouteDecision(BaseModel):
    in_scope: bool        = Field(description="Is question within Rafiq scope?")
    topic: AllowedTopic   = Field(description="Detected topic")
    action: AllowedAction = Field(description="Action to take")
    extracted_child_age: Optional[int] = Field(default=None)
    reason: str           = Field(description="Short reason")


# ══════════════════════════════════════════════
# CONSTANTS & GUARDS
# ══════════════════════════════════════════════

PARENTING_TOPICS    = {"teen_communication", "anger", "screen_addiction", "bullying",
                        "study_focus", "siblings_jealousy", "parents_conflict",
                        "lying", "general_parenting"}
KIDS_CONTENT_TOPICS = {"kids_stories", "activities_games", "book_recommendations"}
ASSESSMENT_TOPIC    = "assessment_personality"
ALL_TRAITS          = ["leadership", "sociability", "empathy", "self_control",
                       "focus", "curiosity", "adaptability", "sensitivity"]

OUT_OF_SCOPE_KW = ["برمجة", "كود", "flutter", "android", "python", "java", "c++",
                   "backend", "front", "database", "debug", "algorithm"]
MEDICAL_KW      = ["جرعة", "دواء", "حبوب", "مضاد", "تشخيص", "روشتة", "وصفة", "medication", "diagnosis"]
KIDS_UNSAFE_KW  = ["انتحار", "إباحية", "اباحية", "سلاح", "مخدرات"]
RISK_HIGH_KW    = ["عايز أموت", "مش عايز أعيش", "هأذي نفسي", "انتحار", "هنتحر", "هقتل", "هموت", "أذي نفسي"]
RISK_MEDIUM_KW  = ["خوف شديد", "هلع", "نوبات", "قلق جامد", "اكتئاب",
                   "حزين طول الوقت", "مش قادر", "مخنوق طول الوقت"]


def hard_out_of_scope(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in OUT_OF_SCOPE_KW)


def hard_medical(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in MEDICAL_KW)


def kids_safety_guard(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in KIDS_UNSAFE_KW)


def detect_risk_level(text: str) -> Literal["low", "medium", "high"]:
    tl = text.lower()
    if any(k.lower() in tl for k in RISK_HIGH_KW):   return "high"
    if any(k.lower() in tl for k in RISK_MEDIUM_KW): return "medium"
    return "low"


# ══════════════════════════════════════════════
# IN-MEMORY KB SEARCH
# ══════════════════════════════════════════════

_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")
_AR_PUNCT      = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)
_AR_STOPWORDS  = {"في", "من", "على", "عن", "الى", "إلى", "هو", "هي", "ده", "دي", "دا",
                  "انا", "انت", "انتي", "احنا", "هم"}


def _ar_normalize(text: str) -> str:
    if not text: return ""
    t_ = _AR_DIACRITICS.sub("", text.strip())
    for a, b in [("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه"),
                 ("ؤ", "و"), ("ئ", "ي"), ("ـ", "")]:
        t_ = t_.replace(a, b)
    return re.sub(r"\s+", " ", _AR_PUNCT.sub(" ", t_.lower())).strip()


def _tokenize(text: str) -> List[str]:
    return [w for w in _ar_normalize(text).split() if len(w) >= 2 and w not in _AR_STOPWORDS]


def _score_kb_item(q_tokens: List[str], item: Dict[str, Any]) -> int:
    if not q_tokens: return 1
    tags  = _ar_normalize(" ".join(item.get("tags", [])))
    tip   = _ar_normalize(item.get("tip", ""))
    both  = tags + " " + tip
    score = sum(6 if tok in tags else (4 if tok in tip else 0) for tok in q_tokens)
    if all(tok in both for tok in q_tokens[:3]): score += 6
    score += sum(1 for tok in q_tokens if len(tok) >= 4 and (tok[:4] in tags or tok[:4] in tip))
    return score


class KbSearchResult(BaseModel):
    tips: List[Dict[str, Any]] = []
    matched: bool = False
    match_count: int = 0
    used_default: bool = False


def kb_search_v2(topic: str, query: str, age: Optional[int]) -> KbSearchResult:
    tokens = _tokenize(query or "")
    scored: List[Tuple[int, Dict]] = []
    for item in KB:
        if topic and item["topic"] != topic: continue
        if age is not None and not (item["age_min"] <= age <= item["age_max"]): continue
        s = _score_kb_item(tokens, item)
        if tokens and s > 0: scored.append((s, item))
        elif not tokens:     scored.append((s, item))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        top     = [i for _, i in scored[:3]]
        matched = scored[0][0] >= 6 if tokens else True
        return KbSearchResult(tips=top, matched=matched, match_count=len(scored), used_default=not bool(tokens))
    defaults = [x for x in KB if x["topic"] == topic][:3]
    return KbSearchResult(tips=defaults, matched=False, match_count=0, used_default=True)


# ══════════════════════════════════════════════
# USER / MEMORY
# ══════════════════════════════════════════════

def ensure_user_exists(conn, user_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, notes) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, json.dumps([]))
    )
    conn.commit()


def get_memory(conn, user_id: str) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        "SELECT notes, child_age, name, email, preferred_language, parent_name, child_name "
        "FROM users WHERE user_id=%s",
        (user_id,)
    )
    row = cur.fetchone()
    if not row:
        return {"child_age": None, "name": None, "email": None, "notes": [],
                "last_summary": "", "preferred_language": "ar",
                "parent_name": None, "child_name": None}
    raw   = row[0]
    notes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return {"child_age": row[1], "name": row[2], "email": row[3], "notes": notes,
            "last_summary": "", "preferred_language": row[4] or "ar",
            "parent_name": row[5], "child_name": row[6]}


def update_memory(conn, user_id: str, topic: str,
                  child_age: Optional[int], note: str = "") -> None:
    ensure_user_exists(conn, user_id)
    cur = conn.cursor()
    cur.execute("SELECT notes FROM users WHERE user_id=%s", (user_id,))
    row   = cur.fetchone()
    notes = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
    compact = re.sub(r"\s+", " ", note or "").strip()[:160]
    if compact:
        notes.append(compact)
        notes = notes[-20:]
    cur.execute(
        "UPDATE users SET notes=%s, child_age=COALESCE(%s, child_age), updated_at=NOW() WHERE user_id=%s",
        (json.dumps(notes), child_age, user_id)
    )
    conn.commit()


# ══════════════════════════════════════════════
# ANALYTICS
# ══════════════════════════════════════════════

def log_event(conn, user_id: str, event_type: str, value: str = "") -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analytics (event_id, user_id, event_type, value) VALUES (%s,%s,%s,%s)",
        ("ev_" + uuid.uuid4().hex[:10], user_id, event_type, value[:300])
    )
    conn.commit()


# ══════════════════════════════════════════════
# AUTO-LEARNING
# ══════════════════════════════════════════════

_autolearn_logger = logging.getLogger("rafiq.autolearn")
if not _autolearn_logger.handlers:
    _al_handler = logging.StreamHandler()
    _al_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _autolearn_logger.addHandler(_al_handler)
_autolearn_logger.setLevel(logging.INFO)

_AL_MIN_QUESTION_LEN     = 15
_AL_MIN_ANSWER_LEN       = 60
_AL_MAX_ANSWER_LEN       = 3000
_AL_SIMILARITY_THRESHOLD = 0.75

_AL_LEARNABLE_TOPICS = {
    "teen_communication", "anger", "screen_addiction", "bullying",
    "study_focus", "siblings_jealousy", "parents_conflict", "lying",
    "general_parenting", "kids_stories", "activities_games",
    "book_recommendations", "assessment_personality",
}

_AL_CLARIFICATION_RE = re.compile(
    "|".join([
        r"هل يمكنك.*؟", r"هل تقصد.*؟", r"ما هو.*؟", r"ما هي.*؟",
        r"هل.*عمر.*الطفل",
        r"could you (clarify|tell me|share|provide)",
        r"can you (tell|give|share|provide|clarify)",
        r"what (is|are|do you mean)",
        r"please (clarify|share|tell me|provide)",
        r"i need (more|a bit more) (context|information|detail)",
        r"could you elaborate",
    ]),
    re.IGNORECASE,
)
_AL_GENERIC_RE = re.compile(
    "|".join([
        r"^(sorry|عذرًا|عذرا)[،,.]?\s*(i|لم|لا)",
        r"^(i'm not sure|لست متأكد)",
        r"^(i don't know|لا أعرف)",
    ]),
    re.IGNORECASE,
)


def _al_normalize_text(text: str) -> str:
    t_ = _AR_DIACRITICS.sub("", text.lower())
    for a, b in [("أ","ا"),("إ","ا"),("آ","ا"),("ى","ي"),("ة","ه"),("ؤ","و"),("ئ","ي")]:
        t_ = t_.replace(a, b)
    return re.sub(r"[^\w\u0600-\u06FF]+", " ", t_).strip()


def _al_token_overlap(a: str, b: str) -> float:
    ta = set(_al_normalize_text(a).split())
    tb = set(_al_normalize_text(b).split())
    if not ta or not tb: return 0.0
    return len(ta & tb) / len(ta | tb)


def _al_passes_quality_gate(question: str, answer: str, topic: str) -> Tuple[bool, str]:
    q, a = question.strip(), answer.strip()
    if len(q) < _AL_MIN_QUESTION_LEN:
        reason = f"question too short ({len(q)} chars)"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason); return False, reason
    if len(a) < _AL_MIN_ANSWER_LEN:
        reason = f"answer too short ({len(a)} chars)"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason); return False, reason
    if len(a) > _AL_MAX_ANSWER_LEN:
        reason = f"answer too long ({len(a)} chars)"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason); return False, reason
    if topic not in _AL_LEARNABLE_TOPICS:
        reason = f"topic '{topic}' not learnable"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason); return False, reason
    if _AL_CLARIFICATION_RE.search(a):
        reason = "answer contains clarifying question"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason); return False, reason
    if _AL_GENERIC_RE.match(a):
        reason = "answer is generic/error response"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason); return False, reason
    if len(q.split()) < 4:
        reason = f"question too fragmented ({len(q.split())} words)"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason); return False, reason
    _autolearn_logger.info("[autolearn] quality-gate PASS — topic=%s q_len=%d a_len=%d", topic, len(q), len(a))
    return True, "ok"


def _al_is_duplicate(conn: Any, question: str, topic: str, lang: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT question FROM faq_knowledge_base WHERE topic=%s AND lang=%s ORDER BY created_at DESC LIMIT 200",
        (topic, lang),
    )
    rows = cur.fetchall()
    _autolearn_logger.info("[autolearn] dedup check — topic=%s lang=%s candidates=%d", topic, lang, len(rows))
    for (stored_q,) in rows:
        sim = _al_token_overlap(question, stored_q)
        if sim >= _AL_SIMILARITY_THRESHOLD:
            _autolearn_logger.info("[autolearn] duplicate detected — similarity=%.2f", sim)
            return True
    _autolearn_logger.info("[autolearn] no duplicate found")
    return False


def _al_insert_learned_pair(conn, question, answer, topic, lang, child_age):
    tags = [topic, "auto_learned"]
    if child_age is not None: tags.append(f"age_{child_age}")
    _autolearn_logger.info("[autolearn] attempting INSERT — topic=%s lang=%s", topic, lang)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO faq_knowledge_base (topic, question, answer, tags, lang, created_at) "
            "VALUES (%s,%s,%s,%s,%s,NOW()) RETURNING id",
            (topic, question, answer, tags, lang),
        )
        row = cur.fetchone()
        if row is None:
            _autolearn_logger.error("[autolearn] INSERT returned None — rolling back")
            conn.rollback(); return None
        new_id = row[0]
        conn.commit()
        _autolearn_logger.info("[autolearn] COMMIT successful — id=%s", new_id)
        return new_id
    except Exception as exc:
        _autolearn_logger.error("[autolearn] INSERT/COMMIT failed — %s", exc, exc_info=True)
        try: conn.rollback()
        except Exception: pass
        return None


def maybe_learn_from_interaction(
    user_message: str, reply_text: str, topic: str, lang: str,
    child_age: Optional[int], conn_factory: Callable[[], Any],
) -> None:
    _autolearn_logger.info("[autolearn] maybe_learn_from_interaction called — topic=%s", topic)
    try:
        should_store, reason = _al_passes_quality_gate(user_message, reply_text, topic)
        if not should_store: return
        try:
            dedup_conn = conn_factory()
        except Exception as e:
            _autolearn_logger.error("[autolearn] could not open dedup conn: %s", e); return
        try:
            is_dup = _al_is_duplicate(dedup_conn, user_message, topic, lang)
        except Exception as e:
            _autolearn_logger.error("[autolearn] dedup raised — skipping: %s", e)
            try: dedup_conn.rollback()
            except: pass
            try: dedup_conn.close()
            except: pass
            return
        finally:
            try: dedup_conn.close()
            except: pass
        if is_dup: return
        try:
            write_conn = conn_factory()
        except Exception as e:
            _autolearn_logger.error("[autolearn] could not open write conn: %s", e); return
        try:
            new_id = _al_insert_learned_pair(write_conn, user_message, reply_text, topic, lang, child_age)
            if new_id is not None:
                _autolearn_logger.info("[autolearn] SUCCESS — id=%s", new_id)
            else:
                _autolearn_logger.error("[autolearn] FAILED — insert returned None")
        finally:
            try: write_conn.close()
            except: pass
    except Exception as exc:
        _autolearn_logger.error("[autolearn] top-level error: %s", exc, exc_info=True)


# ══════════════════════════════════════════════
# ASSESSMENT ENGINE
# ══════════════════════════════════════════════

ASSESSMENT_OPTIONS = ["Never", "Rarely", "Sometimes", "Often", "Always"]

ASSESSMENT_QUESTIONS: List[Dict[str, Any]] = [
    {"id": "q01", "trait": "focus",        "age_min": 4,  "age_max": 18, "weights": {"focus": 2},
     "text": "My child stays focused on a task until it is completed."},
    {"id": "q02", "trait": "focus",        "age_min": 7,  "age_max": 18, "weights": {"focus": 2, "self_control": 1},
     "text": "My child finishes homework or assignments before switching to play."},
    {"id": "q03", "trait": "focus",        "age_min": 4,  "age_max": 18, "weights": {"focus": 3},
     "text": "My child can sit quietly and concentrate during story time or a lesson."},
    {"id": "q04", "trait": "empathy",      "age_min": 4,  "age_max": 18, "weights": {"empathy": 2},
     "text": "My child notices when a friend or sibling is upset and tries to comfort them."},
    {"id": "q05", "trait": "empathy",      "age_min": 6,  "age_max": 18, "weights": {"empathy": 2, "sociability": 1},
     "text": "My child apologizes genuinely after hurting someone's feelings."},
    {"id": "q06", "trait": "empathy",      "age_min": 4,  "age_max": 18, "weights": {"empathy": 3},
     "text": "My child shows concern for animals or people who are struggling."},
    {"id": "q07", "trait": "curiosity",    "age_min": 4,  "age_max": 18, "weights": {"curiosity": 2},
     "text": "My child frequently asks 'why' or 'how' questions about the world."},
    {"id": "q08", "trait": "curiosity",    "age_min": 6,  "age_max": 18, "weights": {"curiosity": 2, "adaptability": 1},
     "text": "My child enjoys trying new activities or experimenting with new ideas."},
    {"id": "q09", "trait": "curiosity",    "age_min": 4,  "age_max": 18, "weights": {"curiosity": 3},
     "text": "My child enjoys solving puzzles, riddles, or figuring things out independently."},
    {"id": "q10", "trait": "leadership",   "age_min": 5,  "age_max": 18, "weights": {"leadership": 2},
     "text": "My child naturally takes charge and organizes activities when playing with others."},
    {"id": "q11", "trait": "leadership",   "age_min": 8,  "age_max": 18, "weights": {"leadership": 2, "focus": 1},
     "text": "My child steps up to help make decisions in group settings."},
    {"id": "q12", "trait": "leadership",   "age_min": 5,  "age_max": 18, "weights": {"leadership": 3},
     "text": "My child is comfortable taking responsibility for a task or group project."},
    {"id": "q13", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 2},
     "text": "My child makes friends quickly and easily in new environments."},
    {"id": "q14", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 2, "empathy": 1},
     "text": "My child enjoys being around others and actively seeks social interaction."},
    {"id": "q15", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 3},
     "text": "My child is comfortable sharing, taking turns, and cooperating in group play."},
    {"id": "q16", "trait": "adaptability", "age_min": 4,  "age_max": 18, "weights": {"adaptability": 2},
     "text": "My child adjusts well to changes in routine (new school, travel, schedule changes)."},
    {"id": "q17", "trait": "adaptability", "age_min": 6,  "age_max": 18, "weights": {"adaptability": 2, "self_control": 1},
     "text": "When plans change unexpectedly, my child handles it calmly."},
    {"id": "q18", "trait": "self_control", "age_min": 4,  "age_max": 18, "weights": {"self_control": 2},
     "text": "My child can calm themselves down after getting upset without adult intervention."},
    {"id": "q19", "trait": "self_control", "age_min": 6,  "age_max": 18, "weights": {"self_control": 3},
     "text": "My child resists the urge to act impulsively (e.g., waits their turn, thinks before acting)."},
    {"id": "q20", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 2},
     "text": "My child gets upset easily by criticism, loud noises, or unexpected changes."},
    {"id": "q21", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 3},
     "text": "My child feels emotions deeply and needs extra reassurance after conflict or disappointment."},
]

_QS_NORM: Dict[str, Dict[str, Any]] = {
    q["id"].strip().lower(): q for q in ASSESSMENT_QUESTIONS
}

ARCHETYPES: List[Dict[str, Any]] = [
    {"id": "leader",      "name": "The Leader",
     "description": "Takes initiative, organizes peers, and thrives when given responsibility.",
     "needs": "Clear boundaries, meaningful responsibilities, and leadership opportunities.",
     "profile": {"leadership": 80, "focus": 60, "sociability": 55}, "traits_focus": ["leadership", "focus"]},
    {"id": "explorer",    "name": "The Explorer",
     "description": "Curious, adventurous, and constantly seeking new experiences and knowledge.",
     "needs": "New challenges, hands-on projects, and freedom to experiment.",
     "profile": {"curiosity": 80, "adaptability": 65}, "traits_focus": ["curiosity", "adaptability"]},
    {"id": "thinker",     "name": "The Thinker",
     "description": "Reflective and analytical — prefers depth over breadth.",
     "needs": "Quiet time, intellectual challenges, and space for independent thought.",
     "profile": {"focus": 80, "curiosity": 65, "sociability": 30}, "traits_focus": ["focus", "curiosity"]},
    {"id": "helper",      "name": "The Helper",
     "description": "Warm, caring, and highly attuned to the emotions of others.",
     "needs": "Recognition of emotional contributions and opportunities to support peers.",
     "profile": {"empathy": 85, "sociability": 60}, "traits_focus": ["empathy", "sociability"]},
    {"id": "peacemaker",  "name": "The Peacemaker",
     "description": "Conflict-averse, diplomatic, and focused on harmony in relationships.",
     "needs": "Teaching assertiveness, safe expression of opinions, and conflict resolution skills.",
     "profile": {"empathy": 75, "self_control": 70}, "traits_focus": ["empathy", "self_control"]},
    {"id": "energetic",   "name": "The Energetic",
     "description": "High energy, enthusiastic, and socially motivated.",
     "needs": "Physical outlets, structured energy release, and consistent boundaries.",
     "profile": {"sociability": 75, "curiosity": 60, "self_control": 35}, "traits_focus": ["sociability", "self_control"]},
    {"id": "sensitive",   "name": "The Sensitive",
     "description": "Deeply empathetic and emotionally aware — feels things intensely.",
     "needs": "Emotional validation, predictable routines, and a calm safe environment.",
     "profile": {"sensitivity": 85, "empathy": 65}, "traits_focus": ["sensitivity", "empathy"]},
    {"id": "independent", "name": "The Independent",
     "description": "Values autonomy and personal space — prefers doing things on their own terms.",
     "needs": "Structured choices, respected boundaries, and gradual responsibility.",
     "profile": {"leadership": 55, "sociability": 25, "focus": 60}, "traits_focus": ["leadership", "focus"]},
    {"id": "planner",     "name": "The Planner",
     "description": "Orderly, methodical, and motivated by structure, routine, and clear goals.",
     "needs": "Simple schedules, clear expectations, and positive reinforcement for progress.",
     "profile": {"focus": 85, "self_control": 75}, "traits_focus": ["focus", "self_control"]},
    {"id": "challenger",  "name": "The Challenger",
     "description": "Questions authority, tests limits, and learns best through debate and negotiation.",
     "needs": "Few but firm rules, negotiation space, and consistent logical consequences.",
     "profile": {"leadership": 65, "self_control": 30, "sensitivity": 50}, "traits_focus": ["leadership", "self_control"]},
]


def _normalize_answer_id(raw_id: Any) -> str:
    return str(raw_id or "").strip().lower()


def _extract_answer_value(answer: Dict[str, Any]) -> Optional[int]:
    raw = answer.get("value") if answer.get("value") is not None else answer.get("score")
    try:
        v = int(raw)
        return v if 1 <= v <= 5 else None
    except (TypeError, ValueError):
        return None


def get_assessment_questions(child_age: Optional[int]) -> List[Dict[str, Any]]:
    if child_age is None: return ASSESSMENT_QUESTIONS
    return [q for q in ASSESSMENT_QUESTIONS if q["age_min"] <= child_age <= q["age_max"]]


def _format_questions_for_api(questions: List[Dict]) -> List[Dict]:
    return [{"id": q["id"], "text": q["text"], "trait": q["trait"], "options": ASSESSMENT_OPTIONS}
            for q in questions]


def compute_personality_profile(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw:  Dict[str, float] = {tr: 0.0 for tr in ALL_TRAITS}
    max_: Dict[str, float] = {tr: 0.0 for tr in ALL_TRAITS}
    matched_ids:   List[str] = []
    unmatched_ids: List[str] = []

    for a in answers:
        qid_raw = a.get("question_id") or a.get("id")
        qid     = _normalize_answer_id(qid_raw)
        val     = _extract_answer_value(a)
        q = _QS_NORM.get(qid)
        if q is None: unmatched_ids.append(str(qid_raw)); continue
        if val is None: unmatched_ids.append(f"{qid_raw}(bad_value)"); continue
        matched_ids.append(qid)
        for trait, w in q["weights"].items():
            raw[trait]  += val * w
            max_[trait] += 5 * w

    bs = behavior_signals or {}
    if max_["focus"] > 0:
        raw["focus"] = min(raw["focus"] + max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2, max_["focus"])
    if max_["empathy"] > 0:
        raw["empathy"] = min(raw["empathy"] + int(bs.get("helps_others", 0)) * 2, max_["empathy"])

    def _norm(r: float, m: float) -> int:
        return max(0, min(100, int(round(r / m * 100)))) if m > 0 else 0

    scores = {tr: _norm(raw[tr], max_[tr]) for tr in ALL_TRAITS}

    def _sim(arch_profile: Dict[str, int]) -> float:
        return sum(100 - abs(scores.get(tr, 50) - v) for tr, v in arch_profile.items()) / max(1, len(arch_profile))

    ranked = sorted(
        [{"id": a["id"], "name": a["name"], "description": a["description"],
          "needs": a["needs"], "match_pct": int(round(_sim(a["profile"])))}
         for a in ARCHETYPES],
        key=lambda x: x["match_pct"], reverse=True
    )
    top_archetype  = ranked[0]
    top_traits     = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    low_traits     = sorted(scores.items(), key=lambda kv: kv[1])[:2]

    return {
        "child_age":              child_age,
        "trait_scores":           scores,
        "top_traits":             [{"trait": tr, "score": v} for tr, v in top_traits],
        "low_traits":             [{"trait": tr, "score": v} for tr, v in low_traits],
        "possible_personalities": ranked[:5],
        "recommendations":        _build_recommendations(scores, top_archetype, low_traits),
        "note":                   t("assessment_note", "en"),
        "_debug":                 {"matched": matched_ids, "unmatched": unmatched_ids},
    }


def _build_recommendations(scores, top_arch, low_traits):
    recs = [
        f"Your child most resembles '{top_arch['name']}' — {top_arch['description']}",
        f"What they need most: {top_arch['needs']}",
    ]
    for trait, score in low_traits:
        if score < 40:
            advice = {
                "focus":        "Try the Pomodoro method: 20 min focused work + 5 min break.",
                "empathy":      "Use emotion cards or role-play scenarios.",
                "curiosity":    "Introduce science kits, mystery books, or nature walks.",
                "leadership":   "Give small responsibilities and praise initiative.",
                "sociability":  "Arrange structured playdates; teach conversation starters.",
                "adaptability": "Warn about changes in advance; use visual schedules.",
                "self_control": "Practice 'stop and breathe'; use a feelings chart.",
                "sensitivity":  "Create a calm-down corner; validate feelings before problem-solving.",
            }.get(trait, "Provide consistent support and positive reinforcement.")
            recs.append(f"Low {trait.replace('_', ' ').title()} ({score}%): {advice}")
    return recs


def compute_assessment_confidence(answers, child_age, behavior_signals):
    q_ids  = {q["id"].strip().lower() for q in ASSESSMENT_QUESTIONS}
    total  = len(ASSESSMENT_QUESTIONS)
    valid  = 0
    matched_dbg:   List[str] = []
    unmatched_dbg: List[str] = []
    for a in answers or []:
        qid_raw = a.get("question_id") or a.get("id")
        qid     = _normalize_answer_id(qid_raw)
        val     = _extract_answer_value(a)
        if qid in q_ids and val is not None:
            valid += 1; matched_dbg.append(qid)
        else:
            unmatched_dbg.append(f"{qid_raw}(val={val})")
    coverage = int(round(valid / total * 100)) if total else 0
    score    = int(round(valid / total * 65))  if total else 0
    notes    = [f"coverage={coverage}%"]
    if child_age is not None:    score += 15; notes.append("age_provided")
    if behavior_signals:          score += 10; notes.append("behavior_signals_included")
    if valid < max(3, total // 3 if total else 3):
        score = max(0, score - 15); notes.append("low_answer_count_penalty")
    return {
        "confidence": max(0, min(100, score)), "valid_answers": valid,
        "total_questions": total, "coverage": coverage, "notes": notes,
        "debug": {"received_count": len(answers or []),
                  "matched_questions": matched_dbg, "unmatched_questions": unmatched_dbg},
    }


# ══════════════════════════════════════════════
# PROFILE NORMALISATION HELPERS
# ══════════════════════════════════════════════

def _norm_traits(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({"trait": str(item.get("trait") or item.get("name") or ""),
                        "score": int(item.get("score", 0))})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"trait": str(item[0]), "score": int(item[1])})
    return out


def _norm_personalities(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({"id":          str(item.get("id", "")),
                        "name":        str(item.get("name", "Unknown")),
                        "description": str(item.get("description", "")),
                        "needs":       str(item.get("needs", "")),
                        "match_pct":   int(item.get("match_pct") or item.get("match") or 0)})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"id": str(item[0]), "name": str(item[0]),
                        "description": "", "needs": "", "match_pct": int(item[1])})
    return out


def _norm_scores(raw: Any) -> Dict[str, int]:
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}
    out = {}
    for item in (raw or []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out[str(item[0])] = int(item[1])
    return out


# ══════════════════════════════════════════════
# GEMINI HELPERS
# ══════════════════════════════════════════════

def _require_gemini() -> None:
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini disabled: set GEMINI_API_KEY")


def gemini_route_decision(user_text, history, fallback_age):
    _require_gemini()
    system = (
        "You are the router for Rafiq, a family support assistant. "
        "Rafiq only handles: family communication, parenting, teen issues, anger, screen addiction, "
        "bullying, study focus, sibling jealousy, parent conflict, lying, kids stories, educational games, "
        "book recommendations for children, and child personality assessment.\n"
        "Forbidden: programming/tech, medical diagnosis, medications.\n"
        "If out of scope → action=refuse_out_of_scope, in_scope=false.\n"
        "Output ONLY valid JSON matching the schema."
    )
    history_str = "\n".join(f"{m.role}: {m.content}" for m in history[-6:])
    prompt = (
        f"System: {system}\n\nConversation:\n{history_str}\n\n"
        f"User message:\n{user_text}\n\nKnown child age: {fallback_age}"
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RouteDecision,
            temperature=0,
            safety_settings=[
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,         threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,        threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,  threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
            ],
        ),
    )
    try:
        return RouteDecision.model_validate_json(resp.text)
    except Exception:
        return RouteDecision(
            in_scope=False, topic="out_of_scope", action="refuse_out_of_scope",
            reason=f"Router parse failed. raw={resp.text[:100]}",
        )


# ══════════════════════════════════════════════
# v5.1 — QUERY EXPANSION (NEW)
# ══════════════════════════════════════════════

_rag_logger = logging.getLogger("rafiq.rag")
if not _rag_logger.handlers:
    _rh = logging.StreamHandler()
    _rh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _rag_logger.addHandler(_rh)
_rag_logger.setLevel(logging.INFO)

# Minimum word count to skip expansion (already detailed enough)
_EXPANSION_MIN_WORDS = 8

_QUERY_EXPANSION_PROMPT = """You are a semantic query expansion assistant for Rafiq, 
a bilingual (Arabic/English) AI parenting coach.

Your job: Take a short or vague user query and rewrite it as a rich, contextual sentence 
that captures the full parenting/family intent — suitable for semantic similarity search.

Rules:
- Output ONE single paragraph (2-3 sentences max).
- Preserve the original language (Arabic stays Arabic, English stays English).
- Do NOT answer the question — only expand it semantically.
- Add likely context: who is involved (parent, child, teen), what the challenge probably is,
  and what kind of guidance they are likely seeking.
- Do NOT contradict or add information inconsistent with the original query.
- Output plain text only. No bullet points, no markdown, no preamble, no labels.

Examples:
Input:  نوم الطفل
Output: الوالد يسأل عن صعوبات نوم الطفل وأنماط النوم، ويحتاج إلى إرشادات تربوية عملية لتحسين روتين النوم وتهدئة الطفل قبل النوم.

Input:  my teen won't talk to me
Output: A parent is struggling to communicate with their teenager who has become withdrawn and unresponsive, and needs practical strategies to rebuild trust and open dialogue with their child.

Input:  موبايل
Output: الوالد قلق من إدمان طفله أو مراهقه على الهاتف والشاشات، ويبحث عن طرق لتنظيم وقت الشاشة ووضع حدود صحية داخل المنزل.

Input:  my child cries a lot
Output: A parent is concerned about their young child who cries frequently and intensely, and is looking for parenting strategies to understand the emotional triggers and help the child self-regulate.
"""


def expand_query_for_embedding(query: str, lang: Lang) -> str:
    """
    Semantically expand a short/vague query before embedding.
    Queries with >= _EXPANSION_MIN_WORDS words are returned as-is.
    Falls back to the original query on any error.
    """
    if not GEMINI_ENABLED or client is None:
        return query

    word_count = len(query.strip().split())
    if word_count >= _EXPANSION_MIN_WORDS:
        _rag_logger.info("[expansion] Skipped — query already detailed (%d words)", word_count)
        return query

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{_QUERY_EXPANSION_PROMPT}\n\nInput: {query.strip()}\nOutput:",
            config=genai_types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=150,
            ),
        )
        expanded = strip_markdown((resp.text or "").strip())
        if expanded and len(expanded) > len(query):
            _rag_logger.info(
                "[expansion] Expanded: '%s' → '%s'",
                query[:60], expanded[:100],
            )
            return expanded
    except Exception as exc:
        _rag_logger.warning("[expansion] Failed, using original query: %s", exc)

    return query


# ══════════════════════════════════════════════
# v5.0 — 15-DAY PLAN GENERATION
# ══════════════════════════════════════════════

_plan_logger = logging.getLogger("rafiq.plan")
if not _plan_logger.handlers:
    _ph = logging.StreamHandler()
    _ph.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _plan_logger.addHandler(_ph)
_plan_logger.setLevel(logging.INFO)


def gemini_generate_intro_letter(
    parent_name: str,
    child_name: str,
    child_age: Optional[int],
    top_archetype: str,
    archetype_desc: str,
    top_traits: List[Dict],
    lang: Lang,
) -> str:
    _require_gemini()
    age_str   = f"{child_age} years old" if child_age else "your child"
    child_str = child_name or "your child"
    traits_str = ", ".join(t["trait"].replace("_", " ").title() for t in top_traits[:3])

    if lang == "ar":
        prompt = (
            f"أنت مدرب تربوي دافئ ومتخصص. اكتب رسالة افتتاحية شخصية دافئة وعاطفية لوالد/ة اسمه/ا {parent_name}، "
            f"طفله/ا اسمه/ا {child_str} وعمره/ا {age_str}. "
            f"النمط الشخصي للطفل هو {top_archetype} — {archetype_desc}. "
            f"أبرز صفاته: {traits_str}.\n\n"
            "الرسالة يجب أن:\n"
            "- تبدأ بـ «عزيزتي/عزيزي {parent_name}،»\n"
            "- تكون داعمة وعاطفية ومحفّزة\n"
            "- تذكر نقاط قوة الطفل تحديدًا\n"
            "- تشجع الوالد/ة على رحلة الـ15 يومًا القادمة\n"
            "- تكون 3-4 فقرات قصيرة\n"
            "لا تستخدم رموز Markdown. اكتب النص فقط باللغة العربية."
        )
    else:
        prompt = (
            f"You are a warm, professional parenting coach. Write a personalized, heartfelt introductory letter "
            f"for a parent named {parent_name}. Their child's name is {child_str}, aged {age_str}. "
            f"The child's personality profile is '{top_archetype}' — {archetype_desc}. "
            f"Their top strengths are: {traits_str}.\n\n"
            "The letter must:\n"
            "- Start with 'Dear {parent_name},'\n"
            "- Be warm, emotionally supportive, and motivating\n"
            "- Specifically mention the child's strengths\n"
            "- Encourage the parent on the upcoming 15-day journey\n"
            "- Be 3-4 short paragraphs\n"
            "No Markdown formatting. Write plain text only in English."
        )

    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.75, max_output_tokens=600),
    )
    return strip_markdown((resp.text or "").strip())


def gemini_generate_15day_plan_json(
    parent_name: str,
    child_name: str,
    child_age: Optional[int],
    top_archetype: str,
    archetype_desc: str,
    archetype_needs: str,
    traits_text: str,
    scores_text: str,
    lang: Lang,
) -> List[Dict[str, Any]]:
    _require_gemini()
    age_str   = f"{child_age} years old" if child_age else "age not specified"
    child_str = child_name or "the child"

    schema_example = json.dumps([
        {
            "day": 1,
            "goal": "Build trust through connection",
            "activity": "20-minute device-free play",
            "how_to_do_it": "Sit on the floor together. Let your child lead. Follow their cues.",
            "why_it_helps": "Uninterrupted attention strengthens the secure attachment bond.",
            "tip": "Put your phone in another room during this time."
        }
    ], indent=2)

    if lang == "ar":
        prompt = (
            f"أنت مدرب تربوي محترف. أنشئ خطة تربوية مخصصة لـ15 يومًا.\n\n"
            f"معلومات الطفل:\n"
            f"- الاسم: {child_str}\n"
            f"- العمر: {age_str}\n"
            f"- النمط الشخصي: {top_archetype} — {archetype_desc}\n"
            f"- الاحتياجات: {archetype_needs}\n"
            f"- أبرز الصفات:\n{traits_text}\n"
            f"- جميع الدرجات:\n{scores_text}\n\n"
            f"أعد المخرجات كـ JSON array فقط بدون أي نص إضافي، بهذا الشكل:\n{schema_example}\n\n"
            "المفاتيح بالعربية:\n"
            "day (رقم), goal (هدف اليوم), activity (النشاط), "
            "how_to_do_it (كيفية التنفيذ), why_it_helps (لماذا يفيد), tip (نصيحة سريعة)\n"
            "15 يومًا فقط. JSON فقط."
        )
    else:
        prompt = (
            f"You are a professional parenting coach. Generate a personalized 15-day parenting plan.\n\n"
            f"Child info:\n"
            f"- Name: {child_str}\n"
            f"- Age: {age_str}\n"
            f"- Personality: {top_archetype} — {archetype_desc}\n"
            f"- Needs: {archetype_needs}\n"
            f"- Top traits:\n{traits_text}\n"
            f"- All trait scores:\n{scores_text}\n\n"
            f"Return ONLY a valid JSON array, no extra text, following this schema:\n{schema_example}\n\n"
            "Exactly 15 day objects. JSON only."
        )

    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.6, max_output_tokens=4000),
    )
    raw_text = (resp.text or "").strip()
    raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text).rstrip("`").strip()

    try:
        days = json.loads(raw_text)
        if isinstance(days, list) and len(days) > 0:
            return days
    except json.JSONDecodeError:
        pass

    match = re.search(r'\[.*\]', raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    _plan_logger.error("[plan] Failed to parse 15-day JSON from Gemini. raw=%s", raw_text[:300])
    return []


def plan_days_to_plain_text(days: List[Dict[str, Any]]) -> str:
    lines = []
    for d in days:
        day_num = d.get("day", "?")
        lines.append(f"Day {day_num}")
        lines.append(f"Goal: {d.get('goal', '')}")
        lines.append(f"Activity: {d.get('activity', '')}")
        lines.append(f"How to do it: {d.get('how_to_do_it', '')}")
        lines.append(f"Why it helps: {d.get('why_it_helps', '')}")
        lines.append(f"Tip: {d.get('tip', '')}")
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════
# RAG INGESTION PIPELINE  (v5.1 — with query expansion on embeddings)
# ══════════════════════════════════════════════

def _gemini_embed_text(text: str) -> Optional[List[float]]:
    """Generate a text embedding using Gemini embedding model."""
    if not GEMINI_ENABLED or client is None:
        return None
    try:
        result = client.models.embed_content(
            model="models/text-embedding-004",
            content=text,
            config=genai_types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
        )
        return result.embeddings[0].values
    except Exception as exc:
        _rag_logger.warning("[rag] Embedding generation failed: %s", exc)
        return None


def ingest_plan_to_knowledge_base(
    plan_id: int,
    user_id: str,
    parent_name: str,
    child_name: str,
    child_age: Optional[int],
    child_profile: str,
    plan_days: List[Dict[str, Any]],
    intro_letter: str,
    lang: Lang,
    conn_factory: Callable[[], Any],
) -> Dict[str, Any]:
    """
    Full RAG ingestion pipeline for a generated parenting plan.

    Steps:
      1. Convert each day to a Q/A pair and insert into faq_knowledge_base (FTS).
      2. Create Gemini text embeddings per day chunk (with query expansion for richer vectors).
      3. Store embeddings in plan_embeddings (pgvector) if available.
    """
    _rag_logger.info("[rag] Starting ingestion — plan_id=%s user_id=%s lang=%s days=%d",
                     plan_id, user_id, lang, len(plan_days))

    fts_inserted  = 0
    emb_inserted  = 0
    errors: List[str] = []

    # ── Step 1: FTS ingestion (faq_knowledge_base) ───────────────────
    try:
        fts_conn = conn_factory()
        fts_cur  = fts_conn.cursor()

        child_ref  = child_name or "your child"
        age_tag    = f"age_{child_age}" if child_age else "age_unknown"

        for day in plan_days:
            day_num  = day.get("day", "?")
            question = (
                f"Day {day_num} plan for {child_ref}: "
                f"{day.get('goal', '')} — {day.get('activity', '')}"
            )
            answer = (
                f"Goal: {day.get('goal', '')}\n"
                f"Activity: {day.get('activity', '')}\n"
                f"How to do it: {day.get('how_to_do_it', '')}\n"
                f"Why it helps: {day.get('why_it_helps', '')}\n"
                f"Tip: {day.get('tip', '')}"
            )
            tags = [
                "parenting_plan", f"day_{day_num}", age_tag,
                child_profile.lower().replace(" ", "_"),
                f"user_{user_id}", "generated_plan",
            ]
            try:
                fts_cur.execute(
                    """
                    INSERT INTO faq_knowledge_base
                        (topic, question, answer, tags, lang, source, source_plan_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id
                    """,
                    ("general_parenting", question, answer, tags, lang,
                     "generated_parenting_plan", plan_id),
                )
                row = fts_cur.fetchone()
                if row:
                    fts_inserted += 1
                    _rag_logger.info("[rag] FTS inserted — day=%s faq_id=%s", day_num, row[0])
                else:
                    _rag_logger.warning("[rag] FTS INSERT day=%s returned no id", day_num)
            except Exception as day_exc:
                errors.append(f"FTS day {day_num}: {day_exc}")
                _rag_logger.error("[rag] FTS INSERT error day=%s: %s", day_num, day_exc)
                fts_conn.rollback()

        fts_conn.commit()
        _rag_logger.info("[rag] FTS commit — inserted=%d / %d days", fts_inserted, len(plan_days))
        fts_conn.close()
    except Exception as exc:
        errors.append(f"FTS batch: {exc}")
        _rag_logger.error("[rag] FTS batch error: %s", exc, exc_info=True)

    _rag_logger.info("[rag] Plan saved in DB (FTS) ✔ — fts_rows=%d", fts_inserted)

    # ── Step 2 + 3: Embedding + pgvector storage ─────────────────────
    if not _PGVECTOR_AVAILABLE:
        _rag_logger.info("[rag] pgvector not available — skipping embedding storage")
    else:
        try:
            emb_conn = conn_factory()
            emb_cur  = emb_conn.cursor()

            for i, day in enumerate(plan_days):
                day_num   = day.get("day", i + 1)
                # Build raw chunk text
                chunk_txt = (
                    f"Day {day_num}: {day.get('goal', '')}. "
                    f"Activity: {day.get('activity', '')}. "
                    f"How to do it: {day.get('how_to_do_it', '')}. "
                    f"Why it helps: {day.get('why_it_helps', '')}. "
                    f"Tip: {day.get('tip', '')}."
                )

                # v5.1: expand chunk before embedding for richer semantic representation
                expanded_chunk = expand_query_for_embedding(chunk_txt, lang)
                _rag_logger.info(
                    "[rag] Day %s chunk expansion: original=%d words → expanded=%d words",
                    day_num,
                    len(chunk_txt.split()),
                    len(expanded_chunk.split()),
                )

                embedding = _gemini_embed_text(expanded_chunk)
                if embedding is None:
                    _rag_logger.warning("[rag] No embedding for day=%s — storing chunk without vector", day_num)
                else:
                    _rag_logger.info("[rag] Embedding created — day=%s dim=%d", day_num, len(embedding))

                try:
                    emb_cur.execute(
                        """
                        INSERT INTO plan_embeddings
                            (plan_id, user_id, chunk_index, chunk_text, embedding,
                             child_age, child_profile, lang, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        RETURNING id
                        """,
                        (plan_id, user_id, i, chunk_txt,  # store original chunk_txt, embed expanded
                         embedding,
                         child_age, child_profile, lang),
                    )
                    row = emb_cur.fetchone()
                    if row:
                        emb_inserted += 1
                        _rag_logger.info("[rag] Embedding stored — day=%s emb_id=%s", day_num, row[0])
                except Exception as emb_day_exc:
                    errors.append(f"EMB day {day_num}: {emb_day_exc}")
                    _rag_logger.error("[rag] Embedding INSERT error day=%s: %s", day_num, emb_day_exc)
                    emb_conn.rollback()

            emb_conn.commit()
            _rag_logger.info("[rag] Embedding commit — stored=%d / %d chunks", emb_inserted, len(plan_days))
            emb_conn.close()
        except Exception as exc:
            errors.append(f"EMB batch: {exc}")
            _rag_logger.error("[rag] Embedding batch error: %s", exc, exc_info=True)

        _rag_logger.info("[rag] Added to knowledge base ✔ — emb_rows=%d", emb_inserted)

    summary = {
        "plan_id":      plan_id,
        "fts_inserted": fts_inserted,
        "emb_inserted": emb_inserted,
        "errors":       errors,
    }
    _rag_logger.info("[rag] Ingestion complete — %s", summary)
    return summary


def retrieve_plan_context_for_user(
    user_id: str,
    query: str,
    lang: Lang,
    limit: int = 3,
    conn_factory: Optional[Callable] = None,
) -> List[Dict[str, Any]]:
    """
    Retrieve relevant chunks from the user's own generated plan.
    v5.1: Expands the query before embedding for better recall.
    First tries pgvector similarity search; falls back to FTS with user tag filter.
    """
    results: List[Dict[str, Any]] = []

    # v5.1: expand the query before embedding
    expanded_query = expand_query_for_embedding(query, lang)

    # pgvector path
    if _PGVECTOR_AVAILABLE and GEMINI_ENABLED and conn_factory:
        try:
            embedding = _gemini_embed_text(expanded_query)
            if embedding:
                conn = conn_factory()
                register_vector(conn)
                cur  = conn.cursor()
                cur.execute(
                    """
                    SELECT chunk_text, embedding <=> %s::vector AS distance
                    FROM   plan_embeddings
                    WHERE  user_id = %s
                    ORDER  BY distance ASC
                    LIMIT  %s
                    """,
                    (embedding, user_id, limit),
                )
                for row in cur.fetchall():
                    results.append({"answer": row[0], "rank": 1 - float(row[1]),
                                    "method": "pgvector", "source": "generated_parenting_plan"})
                conn.close()
                if results:
                    return results
        except Exception as exc:
            _rag_logger.warning("[rag] pgvector retrieval failed: %s", exc)

    # FTS fallback with source filter — use original query for FTS tokenisation
    try:
        fts_tag_filter = f"%user_{user_id}%"
        conn = (conn_factory or get_conn)()
        cur  = conn.cursor()
        raw_tokens = [re.sub(r"[^\w\u0600-\u06FF]", "", tok)
                      for tok in query.strip().split() if len(tok) >= 2]
        tokens = [tok for tok in raw_tokens if tok]
        if tokens:
            tsquery_str = " | ".join(tokens)
            cur.execute(
                """
                SELECT question, answer, ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS rank
                FROM   faq_knowledge_base
                WHERE  search_vector @@ to_tsquery('simple', %s)
                  AND  source = 'generated_parenting_plan'
                  AND  array_to_string(tags, ' ') LIKE %s
                ORDER  BY rank DESC
                LIMIT  %s
                """,
                [tsquery_str, tsquery_str, fts_tag_filter, limit],
            )
            for row in cur.fetchall():
                results.append({"question": row[0], "answer": row[1], "rank": float(row[2]),
                                 "method": "fts_plan", "source": "generated_parenting_plan"})
        conn.close()
    except Exception as exc:
        _rag_logger.warning("[rag] FTS plan retrieval failed: %s", exc)

    return results


# ══════════════════════════════════════════════
# PDF HELPERS  (v5.0 — unchanged)
# ══════════════════════════════════════════════

def _safe_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _shape_arabic(text: str) -> str:
    if not _ARABIC_SHAPING: return text
    return bidi_display(arabic_reshaper.reshape(text))


def _pdf_text(text: str, lang: Lang) -> str:
    return _shape_arabic(text) if lang == "ar" else text


def _pick_font(bold: bool, lang: Lang) -> str:
    if lang == "ar" and _FONT_ARABIC_REGISTERED:
        return "NotoArabicBold" if bold else "NotoArabic"
    if lang == "en" and _FONT_LATIN_REGISTERED:
        return "NotoLatin"
    return "Helvetica-Bold" if bold else "Helvetica"


def _build_parenting_plan_pdf(
    user_id: str,
    parent_name: str,
    child_name: str,
    child_age: Optional[int],
    top_archetype: str,
    intro_letter: str,
    plan_days: List[Dict[str, Any]],
    generated_at: str,
    lang: Lang = "en",
) -> bytes:
    buf    = io.BytesIO()
    W, H   = A4
    styles = getSampleStyleSheet()

    text_align  = TA_RIGHT if lang == "ar" else TA_LEFT
    brand_green  = colors.HexColor("#1B6B3A")
    brand_light  = colors.HexColor("#E8F5E9")
    brand_dark   = colors.HexColor("#0D4A28")
    accent_gold  = colors.HexColor("#C8860A")
    accent_light = colors.HexColor("#FFF8E7")
    text_dark    = colors.HexColor("#1A1A1A")
    text_muted   = colors.HexColor("#555555")
    day_bg       = colors.HexColor("#F0F7F2")
    day_num_bg   = colors.HexColor("#1B6B3A")

    font_body = _pick_font(False, lang)
    font_bold = _pick_font(True,  lang)

    s_title     = ParagraphStyle("Title5", fontSize=22, textColor=colors.white,
                                  alignment=TA_CENTER, fontName=font_bold)
    s_subtitle  = ParagraphStyle("Sub5",   fontSize=13, textColor=colors.white,
                                  alignment=TA_CENTER, fontName=font_body, spaceAfter=4)
    s_lbl       = ParagraphStyle("Lbl5",   fontSize=9,  textColor=brand_green, fontName=font_bold)
    s_val       = ParagraphStyle("Val5",   fontSize=9,  textColor=text_dark,   fontName=font_body)
    s_letter    = ParagraphStyle("Ltr5",   fontSize=11, textColor=text_dark,   fontName=font_body,
                                  leading=18, spaceAfter=8, alignment=text_align)
    s_letter_hd = ParagraphStyle("LtrHd5", fontSize=13, textColor=brand_dark,  fontName=font_bold,
                                  spaceBefore=10, spaceAfter=6)
    s_day_num   = ParagraphStyle("DayN5",  fontSize=14, textColor=colors.white,
                                  fontName=font_bold, alignment=TA_CENTER)
    s_day_goal  = ParagraphStyle("DayG5",  fontSize=11, textColor=brand_dark,  fontName=font_bold,
                                  spaceAfter=3, alignment=text_align)
    s_field_lbl = ParagraphStyle("FLbl5",  fontSize=9,  textColor=accent_gold, fontName=font_bold,
                                  spaceBefore=5)
    s_field_val = ParagraphStyle("FVal5",  fontSize=10, textColor=text_dark,   fontName=font_body,
                                  leading=15, spaceAfter=3, alignment=text_align)
    s_tip       = ParagraphStyle("Tip5",   fontSize=9,  textColor=colors.HexColor("#2E7D32"),
                                  fontName=font_bold, leading=14, alignment=text_align)
    s_footer    = ParagraphStyle("Ftr5",   fontSize=8,  textColor=text_muted,
                                  alignment=TA_CENTER, fontName=font_body)

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm,
        title=f"Rafiq Parenting Plan — {user_id}",
    )

    story = []

    # ── PAGE 1: Banner + Info Card ────────────────────────────────────
    title_txt    = _pdf_text(t("pdf_main_title", lang), lang)
    subtitle_txt = _pdf_text(t("pdf_subtitle",   lang), lang)

    banner = Table(
        [[Paragraph(_safe_xml(title_txt), s_title)],
         [Paragraph(_safe_xml(subtitle_txt), s_subtitle)]],
        colWidths=[W - 4*cm],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_green),
        ("TOPPADDING",    (0, 0), (-1, -1), 18),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))
    story.append(banner)
    story.append(Spacer(1, 0.5*cm))

    def _lbl(k): return Paragraph(_safe_xml(_pdf_text(t(k, lang), lang)), s_lbl)
    def _val(v): return Paragraph(_safe_xml(_pdf_text(str(v), lang)), s_val)

    age_disp  = f"{child_age} {'سنة' if lang == 'ar' else 'years'}" if child_age else t("pdf_label_age_unknown", lang)
    date_disp = generated_at[:10] if generated_at else "—"

    info_data = [
        [_lbl("pdf_label_parent_name"), _val(parent_name or "—"),
         _lbl("pdf_label_child_name"),  _val(child_name  or "—")],
        [_lbl("pdf_label_child_age"),   _val(age_disp),
         _lbl("pdf_label_archetype"),   _val(top_archetype)],
        [_lbl("pdf_label_generated"),   _val(date_disp),
         _lbl("pdf_label_user_id"),     _val(user_id)],
    ]
    cw = (W - 4*cm) / 4
    info_table = Table(info_data, colWidths=[cw*0.28, cw*0.72*0.7, cw*0.28, cw*0.72*0.7])
    info_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_light),
        ("BACKGROUND",    (0, 0), (0, -1),  colors.HexColor("#D0EAD8")),
        ("BACKGROUND",    (2, 0), (2, -1),  colors.HexColor("#D0EAD8")),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#BBDDC7")),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=brand_green, spaceAfter=6))
    story.append(Spacer(1, 0.2*cm))

    # ── PAGE 2: Intro Letter ──────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph(_safe_xml(_pdf_text(
        "Dear " + (parent_name or "Parent") if lang == "en" else "رسالة شخصية", lang)),
        s_letter_hd))
    story.append(HRFlowable(width="50%", thickness=1.5, color=accent_gold, spaceAfter=10))

    for para in intro_letter.split("\n\n"):
        para = para.strip()
        if para:
            story.append(Paragraph(_safe_xml(_pdf_text(para, lang)), s_letter))
            story.append(Spacer(1, 0.15*cm))

    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=4))

    # ── PAGE 3+: Day Cards ────────────────────────────────────────────
    story.append(PageBreak())

    for day in plan_days:
        day_num  = day.get("day", "?")
        goal     = day.get("goal", "")
        activity = day.get("activity", "")
        how_to   = day.get("how_to_do_it", "")
        why      = day.get("why_it_helps", "")
        tip      = day.get("tip", "")

        day_badge = Table([[Paragraph(f"Day {day_num}", s_day_num)]],
                          colWidths=[2.5*cm])
        day_badge.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), day_num_bg),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ]))

        goal_p = Paragraph(_safe_xml(_pdf_text(goal, lang)), s_day_goal)
        header_row = Table([[day_badge, Spacer(0.3*cm, 0), goal_p]],
                           colWidths=[2.5*cm, 0.3*cm, W - 4*cm - 2.8*cm])
        header_row.setStyle(TableStyle([
            ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",  (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))

        def _field(label: str, value: str) -> Table:
            return Table(
                [[Paragraph(_safe_xml(_pdf_text(label, lang)), s_field_lbl)],
                 [Paragraph(_safe_xml(_pdf_text(value, lang)), s_field_val)]],
                colWidths=[W - 4*cm - 0.4*cm],
            )

        tip_table = Table(
            [[Paragraph("💡 " + _safe_xml(_pdf_text(tip, lang)), s_tip)]],
            colWidths=[W - 4*cm - 0.4*cm],
        )
        tip_table.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), accent_light),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ]))

        card_inner = [
            header_row, Spacer(1, 0.2*cm),
            _field("Activity:", activity),
            _field("How to do it:", how_to),
            _field("Why it helps:", why),
            Spacer(1, 0.1*cm),
            tip_table,
        ]

        card = Table(
            [[inner] for inner in card_inner],
            colWidths=[W - 4*cm],
        )
        card.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), day_bg),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#B2DFBB")),
        ]))

        story.append(KeepTogether([card, Spacer(1, 0.35*cm)]))

    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=6))
    story.append(Paragraph(_safe_xml(_pdf_text(t("pdf_footer_line1", lang), lang)), s_footer))

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════
# FCM NOTIFICATION HELPER
# ══════════════════════════════════════════════

def _send_fcm_notification(
    user_id: str,
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
    conn_factory: Optional[Callable] = None,
) -> Dict[str, Any]:
    if not FIREBASE_ENABLED:
        return {"sent": False, "warning": "Firebase not configured"}

    try:
        conn = (conn_factory or get_conn)()
        cur  = conn.cursor()
        cur.execute("SELECT fcm_token FROM users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as exc:
        return {"sent": False, "warning": f"DB error fetching FCM token: {exc}"}

    if not row or not row[0]:
        return {"sent": False, "warning": "No FCM token registered for this user"}

    fcm_token = row[0]
    try:
        message = fb_messaging.Message(
            notification=fb_messaging.Notification(title=title, body=body),
            token=fcm_token,
            data=data or {},
        )
        fb_messaging.send(message)
        return {"sent": True, "warning": None}
    except fb_messaging.UnregisteredError:
        try:
            conn2 = (conn_factory or get_conn)()
            conn2.cursor().execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (user_id,))
            conn2.commit(); conn2.close()
        except Exception:
            pass
        return {"sent": False, "warning": "FCM token expired — cleared. User must re-register."}
    except Exception as exc:
        return {"sent": False, "warning": f"FCM send error: {exc}"}


# ══════════════════════════════════════════════
# ROUTES — SYSTEM
# ══════════════════════════════════════════════

@app.get("/", tags=["System"])
def home():
    return {"status": "Rafiq running 🚀", "version": "5.1.0",
            "retrieval": "PostgreSQL FTS + pgvector RAG + query expansion", "plan_duration": "15 days"}


@app.get("/health", tags=["System"])
def health():
    return {
        "ok": True, "model": GEMINI_MODEL, "gemini_enabled": GEMINI_ENABLED,
        "verify": ENABLE_VERIFY, "db": bool(DATABASE_URL), "debug": DEBUG,
        "arabic_shaping": _ARABIC_SHAPING, "pdf": _REPORTLAB_AVAILABLE,
        "retrieval": "postgres_fts+pgvector", "pgvector": _PGVECTOR_AVAILABLE,
        "firebase": FIREBASE_ENABLED, "plan_duration": 15,
        "query_expansion": GEMINI_ENABLED,
    }


@app.get("/test_gemini", tags=["System"])
def test_gemini():
    _require_gemini()
    r = client.models.generate_content(model=GEMINI_MODEL, contents="Reply with OK only.")
    return {"text": r.text}


# ══════════════════════════════════════════════
# ROUTES — USERS
# ══════════════════════════════════════════════

@app.post("/users", tags=["Users"])
def upsert_user(req: UserUpsertReq):
    conn = get_conn()
    cur  = conn.cursor()
    lang = req.preferred_language if req.preferred_language in ("ar", "en") else "ar"
    try:
        cur.execute(
            """
            INSERT INTO users (user_id, name, email, child_age, notes, preferred_language, parent_name, child_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                name               = COALESCE(EXCLUDED.name,               users.name),
                email              = COALESCE(EXCLUDED.email,              users.email),
                child_age          = COALESCE(EXCLUDED.child_age,          users.child_age),
                preferred_language = COALESCE(EXCLUDED.preferred_language, users.preferred_language),
                parent_name        = COALESCE(EXCLUDED.parent_name,        users.parent_name),
                child_name         = COALESCE(EXCLUDED.child_name,         users.child_name),
                updated_at         = NOW()
            RETURNING user_id, name, email, child_age, preferred_language, parent_name, child_name, created_at, updated_at
            """,
            (req.user_id, req.name, req.email, req.child_age, json.dumps([]), lang,
             req.parent_name, req.child_name)
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "ok": True, "message": t("ok", lang),
            "user": {
                "user_id": row[0], "name": row[1], "email": row[2], "child_age": row[3],
                "preferred_language": row[4], "parent_name": row[5], "child_name": row[6],
                "created_at": row[7].isoformat() if row[7] else None,
                "updated_at": row[8].isoformat() if row[8] else None,
            }
        }
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Email already registered to another user.")
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.get("/memory/{user_id}", tags=["Users"])
def memory_get(user_id: str):
    conn = get_conn()
    data = get_memory(conn, user_id)
    conn.close()
    return {"user_id": user_id, "memory": data}


# ══════════════════════════════════════════════
# ROUTES — FCM
# ══════════════════════════════════════════════

@app.post("/register-token", tags=["Notifications"])
def register_token(req: RegisterTokenReq):
    conn = get_conn(); lang: Lang = "ar"
    try:
        ensure_user_exists(conn, req.user_id)
        cur = conn.cursor()
        cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
        row = cur.fetchone()
        if row: lang = row[0] or "ar"
        cur.execute("UPDATE users SET fcm_token=%s, updated_at=NOW() WHERE user_id=%s",
                    (req.fcm_token, req.user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=t("user_not_found", lang))
        conn.commit()
        log_event(conn, req.user_id, "fcm_token_registered", value=req.fcm_token[:20])
        return {"ok": True, "user_id": req.user_id, "message": t("token_saved", lang)}
    except HTTPException: raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


@app.post("/send-daily-tip", tags=["Notifications"])
def send_daily_tip(req: SendDailyTipReq):
    conn = get_conn(); lang: Lang = "ar"
    try:
        cur = conn.cursor()
        cur.execute("SELECT fcm_token, preferred_language FROM users WHERE user_id=%s", (req.user_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        fcm_token: Optional[str] = row[0]; lang = row[1] or "ar"
        if not fcm_token: raise HTTPException(status_code=422, detail=t("no_fcm_token", lang))
        ensure_user_exists(conn, req.user_id)
        cur.execute("INSERT INTO daily_tips (user_id, tip) VALUES (%s,%s)", (req.user_id, req.tip))
        conn.commit()
        if not FIREBASE_ENABLED:
            return {"ok": True, "user_id": req.user_id, "tip_saved": True,
                    "notification_sent": False, "warning": t("firebase_not_configured", lang)}
        try:
            fb_messaging.send(fb_messaging.Message(
                notification=fb_messaging.Notification(
                    title=t("daily_tip_notif_title", lang), body=req.tip[:200]),
                token=fcm_token, data={"user_id": req.user_id, "type": "daily_tip"},
            ))
        except fb_messaging.UnregisteredError:
            cur.execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (req.user_id,))
            conn.commit()
            raise HTTPException(status_code=410, detail=t("fcm_token_expired", lang))
        except Exception as fb_exc:
            raise HTTPException(status_code=502, detail=f"Firebase error: {fb_exc}")
        log_event(conn, req.user_id, "daily_tip_sent", value=req.tip[:100])
        return {"ok": True, "user_id": req.user_id, "tip_saved": True, "notification_sent": True}
    except HTTPException: raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


@app.get("/daily-tip/{user_id}", tags=["Notifications"])
def get_daily_tips(user_id: str, limit: int = 50):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
        if not cur.fetchone(): raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        cur.execute("SELECT id, tip, created_at FROM daily_tips WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                    (user_id, max(1, min(200, limit))))
        rows = cur.fetchall()
        return {"user_id": user_id, "total": len(rows),
                "tips": [{"id": r[0], "tip": r[1],
                           "created_at": r[2].isoformat() if r[2] else None} for r in rows]}
    except HTTPException: raise
    except Exception as exc: raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally: conn.close()


# ══════════════════════════════════════════════
# ROUTES — KNOWLEDGE BASE
# ══════════════════════════════════════════════

@app.get("/kb/topics", tags=["KB"])
def kb_topics():
    topics = sorted({x["topic"] for x in KB})
    return {"topics": topics, "count": len(topics)}


@app.get("/kb/search", tags=["KB"])
def kb_search_api(topic: str, q: str = "", age: Optional[int] = None,
                   lang: Optional[str] = None, user_id: Optional[str] = None):
    db_results = fts_knowledge_base(query=q, topic=topic, lang=lang, user_id=user_id, limit=3)
    if db_results:
        return {"topic": topic, "age": age, "matched": True, "source": "postgres_fts",
                "match_count": len(db_results), "tips": db_results}
    res = kb_search_v2(topic=topic, query=q, age=age)
    return {"topic": topic, "age": age, "matched": res.matched, "source": "in_memory_kb",
            "match_count": res.match_count, "tips": res.tips}


@app.post("/kb/add", tags=["KB"])
def kb_add(req: KbAddRequest):
    if req.admin_key != ADMIN_KEY: raise HTTPException(status_code=401, detail="Invalid admin_key")
    new_id = "kb_" + uuid.uuid4().hex[:6]
    KB.append({"id": new_id, "topic": req.topic, "age_min": req.age_min,
               "age_max": req.age_max, "tags": req.tags, "tip": req.tip})
    return {"ok": True, "kb_id": new_id, "total": len(KB)}


@app.post("/kb/faq/add", tags=["KB"])
def faq_kb_add(req: FaqKbAddRequest):
    if req.admin_key != ADMIN_KEY: raise HTTPException(status_code=401, detail="Invalid admin_key")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO faq_knowledge_base (topic, question, answer, tags, age_min, age_max, lang) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (req.topic, req.question, req.answer, req.tags, req.age_min, req.age_max, req.lang)
        )
        new_id = cur.fetchone()[0]; conn.commit()
        return {"ok": True, "id": new_id, "topic": req.topic, "lang": req.lang}
    except Exception as exc:
        conn.rollback(); raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally: conn.close()


@app.get("/kb/faq/search", tags=["KB"])
def faq_kb_search_api(q: str, topic: Optional[str] = None, lang: Optional[str] = None,
                       limit: int = 3, user_id: Optional[str] = None):
    results = fts_knowledge_base(query=q, topic=topic, lang=lang, user_id=user_id, limit=limit)
    return {"query": q, "topic": topic, "lang": lang, "count": len(results), "results": results}


# ══════════════════════════════════════════════
# ROUTES — ASSESSMENT
# ══════════════════════════════════════════════

@app.get("/assessment/questions", tags=["Assessment"])
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return {
        "child_age": age, "total_questions": len(qs),
        "scale": {"min": 1, "max": 5, "labels": {"1": "Never", "2": "Rarely", "3": "Sometimes", "4": "Often", "5": "Always"}},
        "questions": _format_questions_for_api(qs),
    }


@app.post("/assessment/submit", tags=["Assessment"])
def assessment_submit(req: AssessmentSubmitReq):
    conn = get_conn()
    lang: Lang = req.preferred_language if req.preferred_language in ("ar", "en") else "ar"  # type: ignore
    try:
        ensure_user_exists(conn, req.user_id)
        if req.preferred_language is None:
            cur = conn.cursor()
            cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
            row = cur.fetchone()
            if row and row[0]: lang = row[0]
        profile     = compute_personality_profile(req.answers, req.child_age, req.behavior_signals)
        assess_conf = compute_assessment_confidence(req.answers, req.child_age, req.behavior_signals)
        profile_to_store = {k: v for k, v in profile.items() if k != "_debug"}
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO assessments (user_id, child_age, assessment_confidence, result, created_at) "
            "VALUES (%s,%s,%s,%s,NOW())",
            (req.user_id, req.child_age, assess_conf["confidence"], json.dumps(profile_to_store))
        )
        conn.commit()
        update_memory(conn, req.user_id, "assessment_personality", req.child_age, note="Assessment submitted")
        log_event(conn, req.user_id, "assessment_submit", value=f"confidence={assess_conf['confidence']}")
        return {
            "ok": True, "message": t("ok", lang),
            "trait_scores": profile["trait_scores"], "top_traits": profile["top_traits"],
            "low_traits": profile["low_traits"], "possible_personalities": profile["possible_personalities"],
            "recommendations": profile["recommendations"], "confidence": assess_conf["confidence"],
            "assessment_meta": assess_conf, "note": t("assessment_note", lang),
            "debug": profile.get("_debug", {}),
        }
    except Exception as exc:
        conn.rollback(); raise HTTPException(status_code=500, detail=str(exc))
    finally: conn.close()


@app.get("/assessment/{user_id}", tags=["Assessment"])
def get_assessments(user_id: str):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "SELECT id, child_age, assessment_confidence, result, created_at "
        "FROM assessments WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
    rows = cur.fetchall(); conn.close()
    return {"assessments": [
        {"id": r[0], "child_age": r[1], "confidence": float(r[2]), "result": r[3],
         "created_at": r[4].isoformat() if r[4] else None} for r in rows
    ]}


# ══════════════════════════════════════════════
# ROUTES — ANALYTICS
# ══════════════════════════════════════════════

@app.post("/analytics/event", tags=["Analytics"])
def analytics_event(req: AppEventRequest):
    conn = get_conn(); ensure_user_exists(conn, req.user_id)
    log_event(conn, req.user_id, req.event_name, value=json.dumps(req.meta)[:300])
    conn.close(); return {"ok": True}


@app.get("/analytics/summary", tags=["Analytics"])
def analytics_summary():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT event_type, COUNT(*) FROM analytics GROUP BY event_type")
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM analytics")
    total = cur.fetchone()[0]; conn.close()
    return {"total_events": total, "by_type": {r[0]: r[1] for r in rows}}


@app.get("/analytics/user/{user_id}", tags=["Analytics"])
def analytics_user(user_id: str):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "SELECT event_id, event_type, value, created_at FROM analytics "
        "WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (user_id,))
    rows = cur.fetchall(); conn.close()
    return {"user_id": user_id, "recent_events": [
        {"event_id": r[0], "event_type": r[1], "value": r[2],
         "created_at": r[3].isoformat() if r[3] else None} for r in rows
    ]}


# ══════════════════════════════════════════════
# ROUTES — FEEDBACK
# ══════════════════════════════════════════════

@app.post("/feedback", tags=["Feedback"])
def feedback(req: FeedbackReq):
    conn = get_conn()
    try:
        ensure_user_exists(conn, req.user_id)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO feedback (user_id, message_id, rating, comment, topic, created_at) "
            "VALUES (%s,%s,%s,%s,%s,NOW())",
            (req.user_id, req.message_id, req.rating, req.comment, req.topic))
        conn.commit()
        if req.comment:
            update_memory(conn, req.user_id, req.topic or "general_parenting", None,
                          note=f"FEEDBACK:{req.rating}:{req.comment}")
        log_event(conn, req.user_id, "feedback", value=f"{req.rating}:{req.message_id}")
        return {"ok": True}
    finally: conn.close()


# ══════════════════════════════════════════════
# ROUTES — CHAT HISTORY
# ══════════════════════════════════════════════

@app.get("/chat/{user_id}", tags=["Chat"])
def get_chat_history(user_id: str, limit: int = 50):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "SELECT message_id, message, response, created_at FROM chat_messages "
        "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, max(1, min(200, limit))))
    rows = cur.fetchall(); conn.close()
    return {"messages": [
        {"message_id": r[0], "user_message": r[1], "bot_reply": r[2],
         "created_at": r[3].isoformat() if r[3] else None} for r in rows
    ]}


# ══════════════════════════════════════════════
# ROUTES — CHAT (plan-aware RAG + query expansion)
# ══════════════════════════════════════════════

@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages list is empty")
    last_msg     = req.messages[-1]
    user_message = (last_msg.content or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="User message content is empty")
    if not GEMINI_ENABLED or client is None:
        return ChatResponse(reply=t("gemini_disabled", detect_lang(user_message)))

    lang: Lang = (
        req.preferred_language  # type: ignore
        if req.preferred_language in ("ar", "en")
        else detect_lang(user_message)
    )

    if hard_out_of_scope(user_message) or hard_medical(user_message):
        return ChatResponse(reply=t("out_of_scope_reply", lang))

    risk_level = detect_risk_level(user_message)
    if risk_level == "high":
        conn = get_conn()
        try: ensure_user_exists(conn, req.user_id); log_event(conn, req.user_id, "risk_high", value=user_message[:200])
        finally: conn.close()
        return ChatResponse(reply=t("risk_high", lang))

    if kids_safety_guard(user_message):
        return ChatResponse(reply=t("kids_safety", lang))

    topic = "general_parenting"
    age   = req.child_age

    try:
        decision = gemini_route_decision(user_message, req.messages, req.child_age)
        if not decision.in_scope or decision.action == "refuse_out_of_scope":
            return ChatResponse(reply=t("scope_refusal", lang))
        topic = decision.topic
        age   = decision.extracted_child_age or req.child_age
    except Exception as exc:
        if DEBUG: print(f"[CHAT] Router error: {exc}")

    # ── Retrieve: user's own plan first (with query expansion), then general KB ─
    plan_context = retrieve_plan_context_for_user(
        user_id=req.user_id, query=user_message, lang=lang, limit=2,
        conn_factory=get_conn,
    )
    general_context, from_db = fts_or_kb_fallback(
        query=user_message, topic=topic, age=age, lang=lang,
        user_id=req.user_id, limit=3,
    )
    retrieved_context = plan_context + [
        c for c in general_context
        if c.get("source") != "generated_parenting_plan"
    ]

    if retrieved_context:
        context_lines = []
        for i, item in enumerate(retrieved_context, 1):
            q_text = item.get("question", "").strip()
            a_text = item.get("answer", item.get("tip", "")).strip()
            src    = item.get("source", "")
            label  = "[Your Plan] " if src == "generated_parenting_plan" else ""
            if a_text:
                context_lines.append(f"[{i}] {label}{f'Q: {q_text}' + chr(10) + '    A: ' if q_text else ''}{a_text}")
        context_block = "\n\n".join(context_lines) or "No specific context found."
    else:
        context_block = "No specific context found."

    prompt = f"""You are a professional parenting assistant called Rafiq.

Rules:
- ALWAYS provide a direct, helpful answer.
- NEVER ask follow-up questions.
- NEVER request more information from the user.
- If [Your Plan] context is present, prioritise it — it is from the user's own personalised parenting plan.
- Use the Knowledge Base Context below if it is relevant; otherwise rely on your general parenting knowledge.
- Respond in the same language as the User Question.
- Do NOT use Markdown formatting symbols such as **, *, or # anywhere in your response.
- Output ONLY the final plain-text answer — no preamble, no metadata.

Knowledge Base Context:
{context_block}

User Question:
{user_message}

Now generate only the final answer."""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=600),
        )
        reply_text = strip_markdown((response.text or "").strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

    if not reply_text:
        reply_text = ("عذرًا، لم أتمكن من توليد رد. حاول مرة أخرى."
                      if lang == "ar" else "Sorry, I couldn't generate a response. Please try again.")

    maybe_learn_from_interaction(
        user_message=user_message, reply_text=reply_text, topic=topic,
        lang=lang, child_age=age, conn_factory=get_conn,
    )

    message_id = "msg_" + uuid.uuid4().hex[:10]
    try:
        conn = get_conn()
        try:
            ensure_user_exists(conn, req.user_id)
            update_memory(conn, req.user_id, topic, age, note=user_message)
            log_event(conn, req.user_id, "chat_message", value=user_message[:300])
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO chat_messages (message_id, user_id, message, response) VALUES (%s,%s,%s,%s)",
                (message_id, req.user_id, user_message, reply_text))
            conn.commit()
        finally: conn.close()
    except Exception as db_exc:
        print(f"[CHAT] DB persistence error (non-fatal): {db_exc}")

    return ChatResponse(reply=reply_text)


# ══════════════════════════════════════════════
# ROUTES — PARENTING PLAN  (v5.1)
# ══════════════════════════════════════════════

@app.post("/generate-parenting-plan/{user_id}", tags=["Parenting Plan"])
def generate_parenting_plan(user_id: str, req: Optional[GeneratePlanRequest] = None):
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini is disabled.")

    lang: Lang = "en"
    _plan_logger.info("[plan] Generate request — user=%s", user_id)

    conn = get_conn()
    try:
        ensure_user_exists(conn, user_id)
        cur = conn.cursor()

        cur.execute(
            "SELECT child_age, preferred_language, parent_name, child_name FROM users WHERE user_id=%s",
            (user_id,)
        )
        user_row = cur.fetchone()
        db_parent_name = user_row[2] if user_row else None
        db_child_name  = user_row[3] if user_row else None

        parent_name = (req and req.parent_name) or db_parent_name or "Parent"
        child_name  = (req and req.child_name)  or db_child_name  or ""

        cur.execute(
            "SELECT id, child_age, assessment_confidence, result, created_at "
            "FROM assessments WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=t("no_assessment_found", lang))

        assessment_id, child_age, assessment_confidence, result_raw, assessed_at = row

        try:
            result: Dict[str, Any] = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to parse assessment: {exc}")

        top_traits             = _norm_traits(result.get("top_traits", []))
        possible_personalities = _norm_personalities(result.get("possible_personalities", []))
        trait_scores           = _norm_scores(result.get("trait_scores", {}))

        top_arch_entry  = possible_personalities[0] if possible_personalities else {}
        top_archetype   = top_arch_entry.get("name",        "Not specified")
        archetype_desc  = top_arch_entry.get("description", "")
        archetype_needs = top_arch_entry.get("needs",       "")

        traits_text = "\n".join(f"  - {tr['trait'].replace('_',' ').title()}: {tr['score']}%" for tr in top_traits) or "  - No data"
        scores_text = "\n".join(f"  - {k.replace('_',' ').title()}: {v}%" for k, v in trait_scores.items()) or "  - No data"

        _plan_logger.info("[plan] Generating intro letter — parent=%s child=%s", parent_name, child_name)
        try:
            intro_letter = gemini_generate_intro_letter(
                parent_name=parent_name, child_name=child_name, child_age=child_age,
                top_archetype=top_archetype, archetype_desc=archetype_desc,
                top_traits=top_traits, lang=lang,
            )
        except Exception as exc:
            _plan_logger.warning("[plan] Intro letter failed (non-fatal): %s", exc)
            intro_letter = f"Dear {parent_name},\n\nWelcome to your personalised 15-day parenting plan. This plan has been carefully designed based on your child's unique personality and needs. We hope it helps you build an even deeper connection with your child.\n\nWarm regards,\nRafiq AI"

        _plan_logger.info("[plan] Generating 15-day plan JSON")
        try:
            plan_days = gemini_generate_15day_plan_json(
                parent_name=parent_name, child_name=child_name, child_age=child_age,
                top_archetype=top_archetype, archetype_desc=archetype_desc,
                archetype_needs=archetype_needs, traits_text=traits_text,
                scores_text=scores_text, lang=lang,
            )
        except HTTPException: raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

        if not plan_days:
            raise HTTPException(status_code=502, detail="Gemini returned an empty plan.")

        plan_text = plan_days_to_plain_text(plan_days)
        _plan_logger.info("[plan] Plan generated ✔ — days=%d", len(plan_days))

        try:
            cur.execute(
                """
                INSERT INTO parenting_plans
                    (user_id, plan_text, plan_language, plan_days, parent_name,
                     child_name, intro_letter, plan_duration, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                RETURNING id, created_at
                """,
                (user_id, plan_text, lang, json.dumps(plan_days),
                 parent_name, child_name, intro_letter, 15)
            )
            plan_row        = cur.fetchone()
            conn.commit()
            plan_id         = plan_row[0]
            plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
            _plan_logger.info("[plan] Plan saved in DB ✔ — plan_id=%s", plan_id)
        except Exception as exc:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"DB error saving plan: {exc}")

        log_event(conn, user_id, "parenting_plan_generated",
                  value=f"plan_id={plan_id}, lang={lang}, assessment_id={assessment_id}")

        notif_result = _send_fcm_notification(
            user_id=user_id,
            title=t("plan_notif_title", lang),
            body=t("plan_notif_body",  lang),
            data={"type": "parenting_plan", "user_id": str(user_id), "plan_id": str(plan_id)},
            conn_factory=get_conn,
        )
        _plan_logger.info("[plan] FCM notification — sent=%s warning=%s",
                          notif_result["sent"], notif_result.get("warning"))

        app_message = (
            f"Hi {parent_name} 👋\n\n"
            f"Your personalised 15-day parenting plan has been created successfully.\n"
            f"Your PDF is ready — use GET /export-plan-pdf/{user_id} to download it.\n\n"
            f"Your child's profile: {top_archetype}\n"
            f"Duration: 15 days\n"
            f"Plan ID: {plan_id}"
        )
        try:
            cur.execute(
                "INSERT INTO chat_messages (message_id, user_id, message, response) VALUES (%s,%s,%s,%s)",
                ("plan_" + uuid.uuid4().hex[:10], user_id,
                 "[SYSTEM] Parenting plan generated", app_message)
            )
            conn.commit()
        except Exception as msg_exc:
            _plan_logger.warning("[plan] In-app message save failed (non-fatal): %s", msg_exc)

        try:
            rag_summary = ingest_plan_to_knowledge_base(
                plan_id=plan_id, user_id=user_id,
                parent_name=parent_name, child_name=child_name,
                child_age=child_age, child_profile=top_archetype,
                plan_days=plan_days, intro_letter=intro_letter,
                lang=lang, conn_factory=get_conn,
            )
            _plan_logger.info("[plan] RAG ingestion complete — %s", rag_summary)
        except Exception as rag_exc:
            _plan_logger.error("[plan] RAG ingestion failed (non-fatal): %s", rag_exc, exc_info=True)
            rag_summary = {"error": str(rag_exc)}

        response_payload: Dict[str, Any] = {
            "ok":                  True,
            "message":             t("plan_created_title", lang),
            "user_id":             user_id,
            "plan_id":             plan_id,
            "created_at":          plan_created_at,
            "plan_language":       lang,
            "plan_duration_days":  15,
            "child_age":           child_age,
            "parent_name":         parent_name,
            "child_name":          child_name,
            "top_archetype":       top_archetype,
            "assessment_id":       assessment_id,
            "notification_sent":   notif_result["sent"],
            "plan_days":           plan_days,
            "plan_text":           plan_text,
            "rag_ingestion":       rag_summary,
            "pdf_export_url":      f"/export-plan-pdf/{user_id}",
        }
        if notif_result.get("warning"):
            response_payload["notification_warning"] = notif_result["warning"]
        return response_payload

    except HTTPException: raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        conn.close()


@app.post("/ingest-plan-to-kb/{plan_id}", tags=["Parenting Plan"])
def ingest_plan_to_kb(plan_id: int, admin_key: str):
    """Manually trigger RAG ingestion for an existing plan (back-fill for pre-v5.0 plans)."""
    if admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, plan_days, plan_language, parent_name, child_name, "
            "intro_letter FROM parenting_plans WHERE id=%s",
            (plan_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
        user_id, plan_days_raw, lang, parent_name, child_name, intro_letter = row

        cur.execute(
            "SELECT a.child_age, a.result FROM assessments a "
            "JOIN parenting_plans pp ON pp.user_id=a.user_id "
            "WHERE pp.id=%s ORDER BY a.created_at DESC LIMIT 1",
            (plan_id,)
        )
        arow = cur.fetchone()
        child_age     = arow[0] if arow else None
        child_profile = "Unknown"
        if arow and arow[1]:
            try:
                res = json.loads(arow[1]) if isinstance(arow[1], str) else arow[1]
                pp  = _norm_personalities(res.get("possible_personalities", []))
                if pp: child_profile = pp[0].get("name", "Unknown")
            except Exception: pass

        try:
            plan_days = json.loads(plan_days_raw) if isinstance(plan_days_raw, str) else plan_days_raw
        except Exception:
            raise HTTPException(status_code=500, detail="plan_days is not valid JSON")

        conn.close()

        rag_summary = ingest_plan_to_knowledge_base(
            plan_id=plan_id, user_id=user_id,
            parent_name=parent_name or "Parent", child_name=child_name or "",
            child_age=child_age, child_profile=child_profile,
            plan_days=plan_days or [], intro_letter=intro_letter or "",
            lang=lang or "en", conn_factory=get_conn,
        )
        return {"ok": True, "plan_id": plan_id, "rag_summary": rag_summary}

    except HTTPException: raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try: conn.close()
        except Exception: pass


@app.get("/parenting-plans/{user_id}", tags=["Parenting Plan"])
def get_parenting_plans(user_id: str, limit: int = 10):
    """
    v5.1 FIX: uses COALESCE(plan_duration, 15) so the query works even if
    the plan_duration column migration hasn't been applied yet on older DBs.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        cur.execute(
            """
            SELECT id, plan_text, plan_language,
                   COALESCE(plan_duration, 15) AS plan_duration,
                   parent_name, child_name, created_at
            FROM parenting_plans
            WHERE user_id=%s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, max(1, min(50, limit)))
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id, "total": len(rows),
            "plans": [
                {
                    "id":            r[0],
                    "plan_text":     r[1],
                    "plan_language": r[2],
                    "plan_duration": r[3],
                    "parent_name":   r[4],
                    "child_name":    r[5],
                    "created_at":    r[6].isoformat() if r[6] else None,
                }
                for r in rows
            ],
        }
    except HTTPException: raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


# ══════════════════════════════════════════════
# ROUTES — PDF EXPORT  (v5.0 redesign — unchanged)
# ══════════════════════════════════════════════

@app.get("/export-plan-pdf/{user_id}", tags=["Parenting Plan"])
def export_plan_pdf(user_id: str):
    if not _REPORTLAB_AVAILABLE:
        raise HTTPException(status_code=503, detail=t("pdf_unavailable", "en"))

    PDF_LANG: Lang = "en"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pp.id, pp.plan_text, pp.plan_days, pp.intro_letter,
                   pp.parent_name, pp.child_name, pp.created_at,
                   u.child_age, a.result
            FROM   parenting_plans pp
            LEFT   JOIN users       u ON u.user_id  = pp.user_id
            LEFT   JOIN assessments a ON a.user_id  = pp.user_id
            WHERE  pp.user_id = %s
            ORDER  BY pp.created_at DESC
            LIMIT  1
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No parenting plan found for this user.")

        plan_id, plan_text, plan_days_raw, intro_letter, parent_name, child_name, \
            created_at, child_age, result_raw = row

        generated_at = created_at.isoformat() if created_at else ""

        plan_days: List[Dict] = []
        if plan_days_raw:
            try:
                plan_days = json.loads(plan_days_raw) if isinstance(plan_days_raw, str) else plan_days_raw
            except Exception:
                pass

        if not plan_days and plan_text:
            current_day: Dict[str, Any] = {}
            for line in plan_text.splitlines():
                line = line.strip()
                if line.startswith("Day ") and line[4:].isdigit():
                    if current_day: plan_days.append(current_day)
                    current_day = {"day": int(line[4:]), "goal": "", "activity": "",
                                   "how_to_do_it": "", "why_it_helps": "", "tip": ""}
                elif line.startswith("Goal:"):          current_day["goal"]         = line[5:].strip()
                elif line.startswith("Activity:"):      current_day["activity"]     = line[9:].strip()
                elif line.startswith("How to do it:"): current_day["how_to_do_it"] = line[13:].strip()
                elif line.startswith("Why it helps:"): current_day["why_it_helps"] = line[13:].strip()
                elif line.startswith("Tip:"):           current_day["tip"]          = line[4:].strip()
            if current_day: plan_days.append(current_day)

        top_archetype = "Not specified"
        if result_raw:
            try:
                result_obj    = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
                personalities = _norm_personalities(result_obj.get("possible_personalities", []))
                if personalities:
                    arch_id  = personalities[0].get("id", "")
                    arch_obj = next((a for a in ARCHETYPES if a["id"] == arch_id), None)
                    top_archetype = arch_obj["name"] if arch_obj else (personalities[0].get("name") or "Not specified")
            except Exception: pass

        try:
            pdf_bytes = _build_parenting_plan_pdf(
                user_id=user_id,
                parent_name=parent_name or "Parent",
                child_name=child_name or "",
                child_age=child_age,
                top_archetype=top_archetype,
                intro_letter=intro_letter or "",
                plan_days=plan_days,
                generated_at=generated_at,
                lang=PDF_LANG,
            )
        except Exception as pdf_exc:
            raise HTTPException(status_code=500, detail=f"PDF generation failed: {pdf_exc}")

        _plan_logger.info("[plan] PDF exported — user=%s plan_id=%s bytes=%d", user_id, plan_id, len(pdf_bytes))

        filename = f"parenting_plan_{user_id}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length":      str(len(pdf_bytes)),
            },
        )

    except HTTPException: raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        conn.close()
