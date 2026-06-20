"""
Rafiq Bot API — PRODUCTION v4.4
================================
Changes in v4.4 vs v4.3:
- FIXED auto-learning subsystem (5 bugs — see AUTO-LEARNING section comments)
  1. Logger level raised to INFO so rejections appear in Railway logs
  2. _al_passes_quality_gate now logs at INFO (was DEBUG, invisible in prod)
  3. _al_is_duplicate no longer swallows exceptions silently; raises so caller
     can detect a broken psycopg2 connection before attempting the INSERT
  4. maybe_learn_from_interaction now uses TWO separate DB connections:
     one read-only for dedup, one fresh write connection for INSERT — prevents
     psycopg2 InFailedSqlTransaction from silently aborting the insert
  5. _al_insert_learned_pair guards fetchone() returning None and logs
     every step (pre-commit, commit, rollback) at INFO/ERROR level
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
    "plan_notif_title":       {"ar": "📋 تم إنشاء خطة تربوية جديدة",  "en": "📋 New Parenting Plan Created"},
    "plan_notif_body":        {"ar": "تم إعداد خطة مخصصة لطفلك بناءً على نتائج التقييم.",
                               "en": "A personalized parenting plan has been generated based on your child's assessment."},
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
    "no_plan_found":          {"ar": "لا توجد خطة تربوية لهذا المستخدم. أنشئ خطة عبر POST /generate-parenting-plan/{user_id} أولًا.",
                               "en": "No parenting plan found for this user. Generate one first via POST /generate-parenting-plan/{user_id}."},
    "user_not_found":         {"ar": "المستخدم غير موجود.",            "en": "User not found."},
    "pdf_unavailable":        {"ar": "تصدير PDF غير متاح — مكتبة reportlab غير مثبّتة.",
                               "en": "PDF export is unavailable — reportlab is not installed. Run: pip install reportlab"},
    "pdf_main_title":         {"ar": "خطة تربوية مخصصة — رفيق AI",    "en": "Personalised Parenting Plan — Rafiq AI"},
    "pdf_subtitle":           {"ar": "خطة 30 يومًا",                   "en": "30-Day Plan"},
    "pdf_label_user_id":      {"ar": "معرف المستخدم",                  "en": "User ID"},
    "pdf_label_child_age":    {"ar": "عمر الطفل",                      "en": "Child Age"},
    "pdf_label_archetype":    {"ar": "النمط الشخصي",                   "en": "Top Archetype"},
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

# Pre-compiled patterns for performance
_MD_BOLD_ITALIC = re.compile(r'\*{1,3}(.+?)\*{1,3}', re.DOTALL)
_MD_BOLD_UNDER  = re.compile(r'_{2}(.+?)_{2}',        re.DOTALL)
_MD_ITALIC_UNDER= re.compile(r'_(.+?)_',              re.DOTALL)
_MD_HEADING     = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MD_HR          = re.compile(r'^[-*_]{3,}\s*$', re.MULTILINE)
_MD_BACKTICK    = re.compile(r'`{1,3}(.+?)`{1,3}', re.DOTALL)


def strip_markdown(text: str) -> str:
    """
    Remove common Markdown formatting symbols from AI-generated text,
    returning clean plain text suitable for display in chat UIs.

    Handles:
      - **bold**, *italic*, ***bold-italic***
      - __bold__, _italic_
      - # Headings (removes the # prefix only)
      - Horizontal rules (---, ***, ___)
      - `inline code` and ```code blocks```
    """
    if not text:
        return text

    # Remove bold/italic markers (keep inner text)
    text = _MD_BOLD_ITALIC.sub(r'\1', text)
    text = _MD_BOLD_UNDER.sub(r'\1',  text)
    text = _MD_ITALIC_UNDER.sub(r'\1', text)

    # Remove heading markers (#, ##, …)
    text = _MD_HEADING.sub('', text)

    # Remove horizontal rules
    text = _MD_HR.sub('', text)

    # Remove backtick code markers (keep inner text)
    text = _MD_BACKTICK.sub(r'\1', text)

    # Collapse any double blank lines left behind
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# ══════════════════════════════════════════════
# OPTIONAL DEPENDENCIES
# ══════════════════════════════════════════════

# ── reportlab ─────────────────────────────────
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    print("WARNING: reportlab not installed — PDF export disabled.")

# ── Arabic text shaping / bidi ────────────────
try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_display
    _ARABIC_SHAPING = True
except ImportError:
    _ARABIC_SHAPING = False
    print("WARNING: arabic-reshaper / python-bidi not installed — Arabic PDF text may not render correctly.")

# ── Gemini ────────────────────────────────────
try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

# ── Firebase ──────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials, messaging as fb_messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    firebase_admin = fb_credentials = fb_messaging = None
    _FIREBASE_AVAILABLE = False


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
        print("Gemini initialized ✔ (generation only — no embeddings)")
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

# ── PDF font registration ──────────────────────
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
            print("PDF Arabic fonts NOT found — falling back to Helvetica.")
    except Exception as exc:
        print(f"Font registration warning: {exc}")


# ══════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════

app = FastAPI(
    title="Rafiq Bot API",
    version="4.4.0",
    description="Family support & parenting assistant API — bilingual (ar/en) | FTS-based retrieval",
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
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _run_schema_migrations() -> None:
    """Apply all schema migrations idempotently (no pgvector, FTS only)."""
    if not DATABASE_URL:
        print("Skipping DB migrations — DATABASE_URL not set")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur  = conn.cursor()

        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language VARCHAR(5) DEFAULT 'ar';")

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
                plan_language VARCHAR(5) DEFAULT 'ar',
                created_at    TIMESTAMP DEFAULT NOW()
            );
            """
        )
        cur.execute(
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_language VARCHAR(5) DEFAULT 'ar';"
        )

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
                created_at    TIMESTAMP     DEFAULT NOW(),
                updated_at    TIMESTAMP     DEFAULT NOW()
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_faq_kb_fts   ON faq_knowledge_base USING GIN (search_vector);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_faq_kb_topic ON faq_knowledge_base (topic);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_faq_kb_lang  ON faq_knowledge_base (lang);"
        )
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
        conn.commit()
        conn.close()
        print("DB migrations applied ✔ (no pgvector, FTS ready)")
    except Exception as exc:
        print(f"DB migration warning: {exc}")


# ══════════════════════════════════════════════
# FULL-TEXT SEARCH
# ══════════════════════════════════════════════

def fts_knowledge_base(
    query: str,
    topic: Optional[str] = None,
    lang: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Retrieve KB entries using PostgreSQL FTS with ILIKE fallback."""
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
                       ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS rank
                FROM   faq_knowledge_base
                WHERE  search_vector @@ to_tsquery('simple', %s)
                {where_extra}
                ORDER  BY rank DESC
                LIMIT  %s;
            """
            cur.execute(fts_sql, [tsquery_str, tsquery_str] + params_fts + [limit])
            rows = cur.fetchall()
            if DEBUG:
                print(f"[FTS] tsquery='{tsquery_str}' | rows={len(rows)}")
            for row in rows:
                results.append({
                    "topic": row[0], "question": row[1], "answer": row[2],
                    "tags": row[3] or [], "rank": float(row[4]), "method": "fts",
                })

        if not results:
            search_term  = tokens[0] if tokens else query.strip()
            like_pattern = f"%{search_term}%"
            ilike_sql = f"""
                SELECT topic, question, answer, tags, 1.0 AS rank
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
            if DEBUG:
                print(f"[FTS] ILIKE fallback pattern='{like_pattern}' | rows={len(rows)}")
            for row in rows:
                results.append({
                    "topic": row[0], "question": row[1], "answer": row[2],
                    "tags": row[3] or [], "rank": float(row[4]), "method": "ilike",
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
    limit: int = 3,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Try FTS first; fall back to in-memory KB. Returns (results, from_db)."""
    db_results = fts_knowledge_base(query=query, topic=topic, lang=lang, limit=limit)
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
# IN-MEMORY KNOWLEDGE BASE (fallback)
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
        "SELECT notes, child_age, name, email, preferred_language FROM users WHERE user_id=%s",
        (user_id,)
    )
    row = cur.fetchone()
    if not row:
        return {"child_age": None, "name": None, "email": None, "notes": [],
                "last_summary": "", "preferred_language": "ar"}
    raw   = row[0]
    notes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return {"child_age": row[1], "name": row[2], "email": row[3], "notes": notes,
            "last_summary": "", "preferred_language": row[4] or "ar"}


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
# AUTO-LEARNING  (merged from auto_learning.py)
# ══════════════════════════════════════════════
#
# FIX SUMMARY (v4.4):
#
# BUG 1 — Logger was inheriting root level (WARNING), so all debug/info
#          calls were invisible in Railway. Fixed: set level to INFO and
#          attach a StreamHandler so every gate decision is always visible.
#
# BUG 2 — _al_passes_quality_gate logged rejections at DEBUG level only.
#          Fixed: now logs at INFO so every rejection appears in Railway.
#
# BUG 3 — _al_is_duplicate swallowed DB exceptions and returned False.
#          This left the psycopg2 connection in aborted-transaction state
#          (InFailedSqlTransaction). The subsequent INSERT on the SAME
#          connection then silently failed. Fixed: exceptions now propagate
#          so the caller can detect and handle a broken connection.
#
# BUG 4 — maybe_learn_from_interaction shared one connection between the
#          dedup SELECT and the INSERT. A failed SELECT aborted the tx,
#          making the INSERT fail with "insert returned no id".
#          Fixed: two separate short-lived connections — one read-only for
#          dedup, one fresh write connection for INSERT + COMMIT.
#
# BUG 5 — _al_insert_learned_pair did not guard fetchone() returning None
#          (possible if a trigger/constraint silently rejected the row).
#          Fixed: explicit None check, rollback, and ERROR log.
# ══════════════════════════════════════════════

_autolearn_logger = logging.getLogger("rafiq.autolearn")

# FIX 1 — ensure INFO messages are always visible regardless of root logger config
if not _autolearn_logger.handlers:
    _al_handler = logging.StreamHandler()
    _al_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    _autolearn_logger.addHandler(_al_handler)
_autolearn_logger.setLevel(logging.INFO)

# Quality thresholds
_AL_MIN_QUESTION_LEN      = 15
_AL_MIN_ANSWER_LEN        = 60
_AL_MAX_ANSWER_LEN        = 3000
_AL_SIMILARITY_THRESHOLD  = 0.75

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

# Shared normaliser (reuses _AR_DIACRITICS already defined above)
def _al_normalize_text(text: str) -> str:
    t_ = _AR_DIACRITICS.sub("", text.lower())
    for a, b in [("أ","ا"),("إ","ا"),("آ","ا"),("ى","ي"),("ة","ه"),("ؤ","و"),("ئ","ي")]:
        t_ = t_.replace(a, b)
    return re.sub(r"[^\w\u0600-\u06FF]+", " ", t_).strip()


def _al_token_overlap(a: str, b: str) -> float:
    """Jaccard similarity between token sets."""
    ta = set(_al_normalize_text(a).split())
    tb = set(_al_normalize_text(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── FIX 2: every rejection path now logs at INFO (was DEBUG) ──────────
def _al_passes_quality_gate(
    question: str,
    answer: str,
    topic: str,
) -> Tuple[bool, str]:
    q, a = question.strip(), answer.strip()

    if len(q) < _AL_MIN_QUESTION_LEN:
        reason = f"question too short ({len(q)} chars, min={_AL_MIN_QUESTION_LEN})"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason)
        return False, reason

    if len(a) < _AL_MIN_ANSWER_LEN:
        reason = f"answer too short ({len(a)} chars, min={_AL_MIN_ANSWER_LEN})"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason)
        return False, reason

    if len(a) > _AL_MAX_ANSWER_LEN:
        reason = f"answer too long ({len(a)} chars, max={_AL_MAX_ANSWER_LEN})"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason)
        return False, reason

    if topic not in _AL_LEARNABLE_TOPICS:
        reason = f"topic '{topic}' not learnable"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason)
        return False, reason

    if _AL_CLARIFICATION_RE.search(a):
        reason = "answer contains clarifying question (matched _AL_CLARIFICATION_RE)"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason)
        return False, reason

    if _AL_GENERIC_RE.match(a):
        reason = "answer is a generic/error response (matched _AL_GENERIC_RE)"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason)
        return False, reason

    if len(q.split()) < 4:
        reason = f"question too fragmented ({len(q.split())} words, min=4)"
        _autolearn_logger.info("[autolearn] quality-gate FAIL — %s", reason)
        return False, reason

    _autolearn_logger.info(
        "[autolearn] quality-gate PASS — topic=%s q_len=%d a_len=%d",
        topic, len(q), len(a),
    )
    return True, "ok"


# ── FIX 3: no longer swallows exceptions — raises so the caller knows ─
#    the connection is broken and must NOT be reused for the INSERT      ─
def _al_is_duplicate(conn: Any, question: str, topic: str, lang: str) -> bool:
    """
    Returns True if a sufficiently similar question already exists in the DB.
    Raises on any DB error so the caller can handle a broken connection
    rather than proceeding with an INSERT on an aborted transaction.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT question FROM faq_knowledge_base WHERE topic=%s AND lang=%s "
        "ORDER BY created_at DESC LIMIT 200",
        (topic, lang),
    )
    rows = cur.fetchall()
    _autolearn_logger.info(
        "[autolearn] dedup check — topic=%s lang=%s candidates=%d",
        topic, lang, len(rows),
    )
    for (stored_q,) in rows:
        similarity = _al_token_overlap(question, stored_q)
        if similarity >= _AL_SIMILARITY_THRESHOLD:
            _autolearn_logger.info(
                "[autolearn] duplicate detected — similarity=%.2f stored_q_preview='%s'",
                similarity, stored_q[:60],
            )
            return True
    _autolearn_logger.info("[autolearn] no duplicate found")
    return False


# ── FIX 5: guard fetchone() returning None; log every step explicitly ─
def _al_insert_learned_pair(
    conn: Any,
    question: str,
    answer: str,
    topic: str,
    lang: str,
    child_age: Optional[int],
) -> Optional[int]:
    tags: List[str] = [topic]
    if child_age is not None:
        tags.append(f"age_{child_age}")
    tags.append("auto_learned")

    _autolearn_logger.info(
        "[autolearn] attempting INSERT — topic=%s lang=%s tags=%s q_len=%d a_len=%d",
        topic, lang, tags, len(question), len(answer),
    )

    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO faq_knowledge_base
                (topic, question, answer, tags, lang, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            RETURNING id
            """,
            (topic, question, answer, tags, lang),
        )

        # FIX 5 — fetchone() can return None if a trigger or constraint
        # silently rejected the row without raising an exception.
        row = cur.fetchone()
        if row is None:
            _autolearn_logger.error(
                "[autolearn] INSERT executed but RETURNING id returned None — "
                "possible trigger/constraint rejection. Rolling back."
            )
            conn.rollback()
            return None

        new_id = row[0]
        _autolearn_logger.info(
            "[autolearn] INSERT successful id=%s (pre-commit)", new_id
        )

        conn.commit()
        _autolearn_logger.info(
            "[autolearn] COMMIT successful — id=%s is now persisted in DB", new_id
        )
        return new_id

    except Exception as exc:
        _autolearn_logger.error(
            "[autolearn] INSERT/COMMIT failed — error=%s", exc, exc_info=True
        )
        try:
            conn.rollback()
            _autolearn_logger.info("[autolearn] rollback completed after insert failure")
        except Exception as rb_exc:
            _autolearn_logger.error(
                "[autolearn] rollback itself failed: %s", rb_exc
            )
        return None


# ── FIX 4: two separate connections — dedup on its own, INSERT on a fresh one ─
def maybe_learn_from_interaction(
    user_message: str,
    reply_text: str,
    topic: str,
    lang: str,
    child_age: Optional[int],
    conn_factory: Callable[[], Any],
) -> None:
    """
    Called after every successful /chat Gemini response.
    Evaluates quality, deduplicates, and persists high-value Q/A pairs
    into faq_knowledge_base for future FTS retrieval.
    All failures are caught — the /chat response is never affected.

    Two separate DB connections are used:
      1. dedup_conn  — read-only SELECT for duplicate detection, closed immediately.
      2. write_conn  — fresh connection for INSERT + COMMIT.
    This prevents a failed SELECT from leaving the psycopg2 connection in
    an aborted-transaction state (InFailedSqlTransaction) that would
    silently kill the subsequent INSERT.
    """
    _autolearn_logger.info(
        "[autolearn] maybe_learn_from_interaction called — "
        "topic=%s lang=%s child_age=%s q_len=%d a_len=%d",
        topic, lang, child_age, len(user_message), len(reply_text),
    )

    try:
        # ── Step 1: Quality gate ──────────────────────────────────────
        should_store, reason = _al_passes_quality_gate(user_message, reply_text, topic)
        if not should_store:
            # Rejection already logged inside _al_passes_quality_gate
            return

        # ── Step 2: Duplicate check on its own dedicated connection ──
        # If this connection or query fails for any reason, we skip the
        # insert entirely (safer than risking a duplicate).
        try:
            dedup_conn = conn_factory()
        except Exception as conn_exc:
            _autolearn_logger.error(
                "[autolearn] could not open dedup DB connection — skipping. error=%s",
                conn_exc, exc_info=True,
            )
            return

        try:
            is_dup = _al_is_duplicate(dedup_conn, user_message, topic, lang)
        except Exception as dedup_exc:
            # _al_is_duplicate raised — the connection may be in a broken
            # state; we skip the insert to avoid writing a duplicate.
            _autolearn_logger.error(
                "[autolearn] dedup check raised an exception — skipping insert "
                "to avoid duplicates. error=%s", dedup_exc, exc_info=True,
            )
            try:
                dedup_conn.rollback()
            except Exception:
                pass
            try:
                dedup_conn.close()
            except Exception:
                pass
            return
        finally:
            # Always close the dedup connection whether or not an exception occurred
            try:
                dedup_conn.close()
                _autolearn_logger.info("[autolearn] dedup connection closed")
            except Exception as close_exc:
                _autolearn_logger.warning(
                    "[autolearn] could not close dedup connection: %s", close_exc
                )

        if is_dup:
            _autolearn_logger.info("[autolearn] skipped — duplicate detected")
            return

        # ── Step 3: INSERT on a brand-new connection ─────────────────
        # A fresh connection guarantees a clean transaction state,
        # completely independent of anything that happened during dedup.
        try:
            write_conn = conn_factory()
        except Exception as conn_exc:
            _autolearn_logger.error(
                "[autolearn] could not open write DB connection — skipping. error=%s",
                conn_exc, exc_info=True,
            )
            return

        try:
            new_id = _al_insert_learned_pair(
                conn=write_conn,
                question=user_message,
                answer=reply_text,
                topic=topic,
                lang=lang,
                child_age=child_age,
            )
            if new_id is not None:
                _autolearn_logger.info(
                    "[autolearn] SUCCESS — learned id=%s | topic=%s | lang=%s | "
                    "q_len=%d | a_len=%d",
                    new_id, topic, lang, len(user_message), len(reply_text),
                )
            else:
                _autolearn_logger.error(
                    "[autolearn] FAILED — insert returned None "
                    "(see errors above) | topic=%s lang=%s",
                    topic, lang,
                )
        finally:
            try:
                write_conn.close()
                _autolearn_logger.info("[autolearn] write connection closed")
            except Exception as close_exc:
                _autolearn_logger.warning(
                    "[autolearn] could not close write connection: %s", close_exc
                )

    except Exception as exc:
        _autolearn_logger.error(
            "[autolearn] unexpected top-level error (non-fatal): %s",
            exc, exc_info=True,
        )


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
    if child_age is None:
        return ASSESSMENT_QUESTIONS
    return [q for q in ASSESSMENT_QUESTIONS if q["age_min"] <= child_age <= q["age_max"]]


def _format_questions_for_api(questions: List[Dict]) -> List[Dict]:
    return [
        {"id": q["id"], "text": q["text"], "trait": q["trait"], "options": ASSESSMENT_OPTIONS}
        for q in questions
    ]


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
        if DEBUG:
            print(f"[DEBUG] answer qid_raw={qid_raw!r} → normalized={qid!r} | value={val}")
        q = _QS_NORM.get(qid)
        if q is None:
            unmatched_ids.append(str(qid_raw)); continue
        if val is None:
            unmatched_ids.append(f"{qid_raw}(bad_value)"); continue
        matched_ids.append(qid)
        for trait, w in q["weights"].items():
            raw[trait]  += val * w
            max_[trait] += 5 * w

    if DEBUG:
        print(f"[DEBUG] matched={matched_ids}")
        print(f"[DEBUG] unmatched={unmatched_ids}")
        print(f"[DEBUG] raw scores={raw}")

    bs = behavior_signals or {}
    if max_["focus"] > 0:
        focus_bonus = max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2
        raw["focus"] = min(raw["focus"] + focus_bonus, max_["focus"])
    if max_["empathy"] > 0:
        empathy_bonus = int(bs.get("helps_others", 0)) * 2
        raw["empathy"] = min(raw["empathy"] + empathy_bonus, max_["empathy"])

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

    top_archetype   = ranked[0]
    top_traits      = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    low_traits      = sorted(scores.items(), key=lambda kv: kv[1])[:2]
    recommendations = _build_recommendations(scores, top_archetype, low_traits)

    return {
        "child_age":              child_age,
        "trait_scores":           scores,
        "top_traits":             [{"trait": tr, "score": v} for tr, v in top_traits],
        "low_traits":             [{"trait": tr, "score": v} for tr, v in low_traits],
        "possible_personalities": ranked[:5],
        "recommendations":        recommendations,
        "note":                   t("assessment_note", "en"),
        "_debug":                 {"matched": matched_ids, "unmatched": unmatched_ids},
    }


def _build_recommendations(
    scores: Dict[str, int],
    top_arch: Dict[str, Any],
    low_traits: List[Tuple[str, int]],
) -> List[str]:
    recs: List[str] = []
    recs.append(f"Your child most resembles '{top_arch['name']}' — {top_arch['description']}")
    recs.append(f"What they need most: {top_arch['needs']}")
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


def compute_assessment_confidence(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    all_qs  = ASSESSMENT_QUESTIONS
    q_ids   = {q["id"].strip().lower() for q in all_qs}
    total   = len(all_qs)
    valid   = 0
    matched_dbg:   List[str] = []
    unmatched_dbg: List[str] = []

    for a in answers or []:
        qid_raw = a.get("question_id") or a.get("id")
        qid     = _normalize_answer_id(qid_raw)
        val     = _extract_answer_value(a)
        if qid in q_ids and val is not None:
            valid += 1
            matched_dbg.append(qid)
        else:
            unmatched_dbg.append(f"{qid_raw}(val={val})")

    if DEBUG:
        print(f"[CONFIDENCE] valid={valid}/{total}, matched={matched_dbg}, unmatched={unmatched_dbg}")

    coverage = int(round(valid / total * 100)) if total else 0
    score    = int(round(valid / total * 65))  if total else 0
    notes    = [f"coverage={coverage}%"]
    if child_age is not None:    score += 15; notes.append("age_provided")
    if behavior_signals:          score += 10; notes.append("behavior_signals_included")
    if valid < max(3, total // 3 if total else 3):
        score = max(0, score - 15); notes.append("low_answer_count_penalty")

    return {
        "confidence":      max(0, min(100, score)),
        "valid_answers":   valid,
        "total_questions": total,
        "coverage":        coverage,
        "notes":           notes,
        "debug": {
            "received_count":      len(answers or []),
            "matched_questions":   matched_dbg,
            "unmatched_questions": unmatched_dbg,
        },
    }


# ══════════════════════════════════════════════
# PDF HELPERS
# ══════════════════════════════════════════════

def _safe_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _shape_arabic(text: str) -> str:
    if not _ARABIC_SHAPING:
        return text
    reshaped = arabic_reshaper.reshape(text)
    return bidi_display(reshaped)


def _pdf_text(text: str, lang: Lang) -> str:
    if lang == "ar":
        return _shape_arabic(text)
    return text


def _pick_font(bold: bool, lang: Lang) -> str:
    if lang == "ar" and _FONT_ARABIC_REGISTERED:
        return "NotoArabicBold" if bold else "NotoArabic"
    if lang == "en" and _FONT_LATIN_REGISTERED:
        return "NotoLatin"
    return "Helvetica-Bold" if bold else "Helvetica"


def _build_parenting_plan_pdf(
    user_id: str,
    child_age: Optional[int],
    top_archetype: str,
    plan_text: str,
    generated_at: str,
    lang: Lang = "ar",
) -> bytes:
    buf = io.BytesIO()
    W, H = A4
    styles = getSampleStyleSheet()

    text_align  = TA_RIGHT if lang == "ar" else TA_LEFT
    brand_green = colors.HexColor("#1B6B3A")
    brand_light = colors.HexColor("#E8F5E9")
    text_dark   = colors.HexColor("#1A1A1A")
    text_muted  = colors.HexColor("#555555")
    accent_gold = colors.HexColor("#C8860A")

    font_body = _pick_font(False, lang)
    font_bold = _pick_font(True,  lang)

    style_subtitle        = ParagraphStyle("SubTitle", parent=styles["Normal"],
        fontSize=12, textColor=text_muted, spaceAfter=2, alignment=TA_CENTER, fontName=font_body)
    style_section_heading = ParagraphStyle("SectionHeading", parent=styles["Heading1"],
        fontSize=13, textColor=brand_green, spaceBefore=14, spaceAfter=4, fontName=font_bold)
    style_plan_heading    = ParagraphStyle("PlanHeading", parent=styles["Heading2"],
        fontSize=12, textColor=accent_gold, spaceBefore=10, spaceAfter=3, fontName=font_bold)
    style_plan_body       = ParagraphStyle("PlanBody", parent=styles["Normal"],
        fontSize=10.5, textColor=text_dark, fontName=font_body, leading=17, spaceAfter=4,
        alignment=text_align)
    style_bullet          = ParagraphStyle("Bullet", parent=styles["Normal"],
        fontSize=10.5, textColor=text_dark, fontName=font_body, leading=17,
        leftIndent=16, spaceAfter=3, bulletIndent=4, alignment=text_align)
    style_footer          = ParagraphStyle("Footer", parent=styles["Normal"],
        fontSize=8, textColor=text_muted, alignment=TA_CENTER, fontName=font_body)

    doc = SimpleDocTemplate(buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm, topMargin=2.5*cm, bottomMargin=2*cm,
        title=f"Rafiq Parenting Plan — {user_id}", author="Rafiq AI")

    story = []

    banner_title = _pdf_text(t("pdf_main_title", lang), lang)
    banner_sub   = _pdf_text(t("pdf_subtitle",   lang), lang)
    banner_style = ParagraphStyle("BannerTitle", parent=styles["Title"],
        fontSize=20, textColor=colors.white, alignment=TA_CENTER, fontName=font_bold)
    banner_table = Table([[Paragraph(banner_title, banner_style)]], colWidths=[W - 4*cm])
    banner_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_green),
        ("TOPPADDING",    (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING",   (0, 0), (-1, -1), 16),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(banner_sub, style_subtitle))
    story.append(Spacer(1, 0.25*cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=brand_green, spaceAfter=10))

    age_display  = _pdf_text(
        f"{child_age} {'سنة' if lang == 'ar' else 'years'}" if child_age
        else t("pdf_label_age_unknown", lang), lang)
    date_display = generated_at[:10] if generated_at else "—"

    lbl       = lambda k: _pdf_text(t(k, lang), lang)
    lbl_style = ParagraphStyle("MetaLbl", parent=styles["Normal"],
        fontSize=9, textColor=brand_green, fontName=font_bold)
    val_style = ParagraphStyle("MetaVal", parent=styles["Normal"],
        fontSize=9, textColor=text_dark,  fontName=font_body)

    meta_data = [
        [Paragraph(lbl("pdf_label_user_id"),   lbl_style), Paragraph(user_id,     val_style),
         Paragraph(lbl("pdf_label_child_age"), lbl_style), Paragraph(age_display, val_style)],
        [Paragraph(lbl("pdf_label_archetype"), lbl_style), Paragraph(_pdf_text(top_archetype, lang), val_style),
         Paragraph(lbl("pdf_label_generated"), lbl_style), Paragraph(date_display, val_style)],
    ]
    cw = (W - 4*cm) / 4
    meta_table = Table(meta_data, colWidths=[cw*0.22, cw*0.78*0.6, cw*0.22, cw*0.78*0.6])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_light),
        ("BACKGROUND",    (0, 0), (0,  -1), colors.HexColor("#D0EAD8")),
        ("BACKGROUND",    (2, 0), (2,  -1), colors.HexColor("#D0EAD8")),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#BBDDC7")),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=6))
    story.append(Paragraph(_pdf_text(t("pdf_section_plan", lang), lang), style_section_heading))
    story.append(Spacer(1, 0.2*cm))

    week_keywords  = ("الأسبوع", "Week ", "أسبوع")
    bullet_markers = ("•", "-", "–", "*", "·")

    for raw_line in plan_text.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 0.18*cm))
            continue
        shaped = _pdf_text(line, lang)
        if any(line.startswith(kw) for kw in week_keywords) or (
            len(line) < 80 and line.endswith(":") and not line.startswith(" ")
        ):
            story.append(Paragraph(_safe_xml(shaped), style_plan_heading))
            continue
        if len(line) > 2 and line[0].isdigit() and line[1] in (".", ")"):
            story.append(Paragraph(f"&#x25CF;&nbsp;&nbsp;{_safe_xml(shaped[2:].strip())}", style_bullet))
            continue
        if line[0] in bullet_markers:
            story.append(Paragraph(f"&#x25CF;&nbsp;&nbsp;{_safe_xml(shaped[1:].strip())}", style_bullet))
            continue
        story.append(Paragraph(_safe_xml(shaped), style_plan_body))

    story.append(Spacer(1, 0.6*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=6))
    story.append(Paragraph(_safe_xml(_pdf_text(t("pdf_footer_line1", lang), lang)), style_footer))

    doc.build(story)
    buf.seek(0)
    return buf.read()


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
                        "name":        str(item.get("name", "غير محدد")),
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


def gemini_route_decision(
    user_text: str,
    history: List[ChatMessage],
    fallback_age: Optional[int],
) -> RouteDecision:
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


def gemini_generate_parenting_plan(
    child_age: Optional[int],
    top_archetype: str,
    archetype_desc: str,
    archetype_needs: str,
    traits_text: str,
    scores_text: str,
    lang: Lang,
) -> str:
    _require_gemini()

    if lang == "ar":
        prompt = (
            "أنت مدرب تربوي محترف متخصص في التطوير الشخصي للأطفال.\n\n"
            "فيما يلي نتائج تقييم شخصية الطفل:\n"
            f"- عمر الطفل: {child_age if child_age is not None else 'غير محدد'} سنة\n"
            f"- النمط الشخصي الأبرز: {top_archetype} — {archetype_desc}\n"
            f"- احتياجات الطفل: {archetype_needs}\n\n"
            f"أبرز الصفات:\n{traits_text}\n\n"
            f"درجات جميع الصفات:\n{scores_text}\n\n"
            "المطلوب:\n"
            "أنشئ خطة تربوية مخصصة لمدة 30 يومًا (4 أسابيع).\n"
            "يجب أن تتضمن الخطة:\n"
            "1. هدف الأسبوع\n2. أنشطة يومية عملية ومناسبة لعمر الطفل\n"
            "3. أساليب التعزيز الإيجابي\n4. توصيات خاصة بالوالدين\n"
            "5. ملاحظة ختامية للمتابعة\n\n"
            "الأسلوب: دافئ، واضح، وعملي. تجنب المصطلحات الطبية.\n"
            "لا تستخدم رموز Markdown مثل ** أو * أو # في الرد.\n"
            "أعد الخطة كاملةً باللغة العربية."
        )
    else:
        prompt = (
            "You are a professional parenting coach specializing in child development.\n\n"
            "Below are the results of a child personality assessment:\n"
            f"- Child age: {child_age if child_age is not None else 'Not specified'} years\n"
            f"- Top personality archetype: {top_archetype} — {archetype_desc}\n"
            f"- Child's needs: {archetype_needs}\n\n"
            f"Top traits:\n{traits_text}\n\n"
            f"All trait scores:\n{scores_text}\n\n"
            "Task:\nCreate a personalised 30-day parenting plan (4 weeks) based on this data.\n"
            "The plan must include:\n"
            "1. Weekly goal\n2. Daily practical activities appropriate for the child's age\n"
            "3. Positive reinforcement strategies per week\n"
            "4. Specific recommendations for parents to support the child\n"
            "5. A closing note for follow-up after the plan ends\n\n"
            "Style: warm, clear, practical. Avoid medical/diagnostic terminology.\n"
            "Do NOT use Markdown formatting symbols such as **, *, or # in your response.\n"
            "Write the entire plan in English."
        )

    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.6, max_output_tokens=2000),
    )
    return strip_markdown((resp.text or "").strip())


# ══════════════════════════════════════════════
# ROUTES — SYSTEM
# ══════════════════════════════════════════════

@app.get("/", tags=["System"])
def home():
    return {"status": "Rafiq running 🚀", "version": "4.4.0",
            "retrieval": "PostgreSQL FTS (tsvector/tsquery) — no pgvector"}


@app.get("/health", tags=["System"])
def health():
    return {
        "ok":             True,
        "model":          GEMINI_MODEL,
        "gemini_enabled": GEMINI_ENABLED,
        "verify":         ENABLE_VERIFY,
        "db":             bool(DATABASE_URL),
        "debug":          DEBUG,
        "arabic_shaping": _ARABIC_SHAPING,
        "pdf":            _REPORTLAB_AVAILABLE,
        "retrieval":      "postgres_fts",
        "embeddings":     False,
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
            INSERT INTO users (user_id, name, email, child_age, notes, preferred_language)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                name               = COALESCE(EXCLUDED.name,               users.name),
                email              = COALESCE(EXCLUDED.email,              users.email),
                child_age          = COALESCE(EXCLUDED.child_age,          users.child_age),
                preferred_language = COALESCE(EXCLUDED.preferred_language, users.preferred_language),
                updated_at         = NOW()
            RETURNING user_id, name, email, child_age, preferred_language, created_at, updated_at
            """,
            (req.user_id, req.name, req.email, req.child_age, json.dumps([]), lang)
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "ok":      True,
            "message": t("ok", lang),
            "user": {
                "user_id":            row[0],
                "name":               row[1],
                "email":              row[2],
                "child_age":          row[3],
                "preferred_language": row[4],
                "created_at":         row[5].isoformat() if row[5] else None,
                "updated_at":         row[6].isoformat() if row[6] else None,
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
# ROUTES — FCM / PUSH NOTIFICATIONS
# ══════════════════════════════════════════════

@app.post("/register-token", tags=["Notifications"])
def register_token(req: RegisterTokenReq):
    conn = get_conn()
    lang: Lang = "ar"
    try:
        ensure_user_exists(conn, req.user_id)
        cur = conn.cursor()
        cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
        row = cur.fetchone()
        if row: lang = row[0] or "ar"
        cur.execute(
            "UPDATE users SET fcm_token=%s, updated_at=NOW() WHERE user_id=%s",
            (req.fcm_token, req.user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=t("user_not_found", lang))
        conn.commit()
        log_event(conn, req.user_id, "fcm_token_registered", value=req.fcm_token[:20])
        return {"ok": True, "user_id": req.user_id, "message": t("token_saved", lang)}
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


@app.post("/send-daily-tip", tags=["Notifications"])
def send_daily_tip(req: SendDailyTipReq):
    conn = get_conn()
    lang: Lang = "ar"
    try:
        cur = conn.cursor()
        cur.execute("SELECT fcm_token, preferred_language FROM users WHERE user_id=%s", (req.user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        fcm_token: Optional[str] = row[0]
        lang = row[1] or "ar"
        if not fcm_token:
            raise HTTPException(status_code=422, detail=t("no_fcm_token", lang))

        ensure_user_exists(conn, req.user_id)
        cur.execute("INSERT INTO daily_tips (user_id, tip) VALUES (%s,%s)", (req.user_id, req.tip))
        conn.commit()

        if not FIREBASE_ENABLED:
            return {"ok": True, "user_id": req.user_id, "tip_saved": True,
                    "notification_sent": False,
                    "warning": t("firebase_not_configured", lang)}

        try:
            message = fb_messaging.Message(
                notification=fb_messaging.Notification(
                    title=t("daily_tip_notif_title", lang),
                    body=req.tip[:200],
                ),
                token=fcm_token,
                data={"user_id": req.user_id, "type": "daily_tip"},
            )
            fb_messaging.send(message)
        except fb_messaging.UnregisteredError:
            cur.execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (req.user_id,))
            conn.commit()
            raise HTTPException(status_code=410, detail=t("fcm_token_expired", lang))
        except Exception as fb_exc:
            raise HTTPException(status_code=502, detail=f"Firebase error: {fb_exc}")

        log_event(conn, req.user_id, "daily_tip_sent", value=req.tip[:100])
        return {"ok": True, "user_id": req.user_id, "tip_saved": True, "notification_sent": True}
    except HTTPException:
        raise
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
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        cur.execute(
            "SELECT id, tip, created_at FROM daily_tips WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, max(1, min(200, limit)))
        )
        rows = cur.fetchall()
        return {"user_id": user_id, "total": len(rows),
                "tips": [{"id": r[0], "tip": r[1],
                          "created_at": r[2].isoformat() if r[2] else None} for r in rows]}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


# ══════════════════════════════════════════════
# ROUTES — KNOWLEDGE BASE
# ══════════════════════════════════════════════

@app.get("/kb/topics", tags=["KB"])
def kb_topics():
    topics = sorted({x["topic"] for x in KB})
    return {"topics": topics, "count": len(topics)}


@app.get("/kb/search", tags=["KB"])
def kb_search_api(topic: str, q: str = "", age: Optional[int] = None, lang: Optional[str] = None):
    """Search knowledge base: tries PostgreSQL FTS first, falls back to in-memory KB."""
    db_results = fts_knowledge_base(query=q, topic=topic, lang=lang, limit=3)
    if db_results:
        return {"topic": topic, "age": age, "matched": True, "source": "postgres_fts",
                "match_count": len(db_results), "used_default": False, "tips": db_results}
    res = kb_search_v2(topic=topic, query=q, age=age)
    return {"topic": topic, "age": age, "matched": res.matched, "source": "in_memory_kb",
            "match_count": res.match_count, "used_default": res.used_default, "tips": res.tips}


@app.post("/kb/add", tags=["KB"])
def kb_add(req: KbAddRequest):
    """Add to in-memory KB (legacy — use /kb/faq/add for DB persistence)."""
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")
    new_id = "kb_" + uuid.uuid4().hex[:6]
    KB.append({"id": new_id, "topic": req.topic, "age_min": req.age_min,
               "age_max": req.age_max, "tags": req.tags, "tip": req.tip})
    return {"ok": True, "kb_id": new_id, "total": len(KB)}


@app.post("/kb/faq/add", tags=["KB"])
def faq_kb_add(req: FaqKbAddRequest):
    """Add a Q&A pair to the persistent faq_knowledge_base table."""
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO faq_knowledge_base (topic, question, answer, tags, age_min, age_max, lang)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (req.topic, req.question, req.answer, req.tags, req.age_min, req.age_max, req.lang)
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": new_id, "topic": req.topic, "lang": req.lang}
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()


@app.get("/kb/faq/search", tags=["KB"])
def faq_kb_search_api(q: str, topic: Optional[str] = None, lang: Optional[str] = None, limit: int = 3):
    """Direct FTS search against faq_knowledge_base table."""
    results = fts_knowledge_base(query=q, topic=topic, lang=lang, limit=limit)
    return {"query": q, "topic": topic, "lang": lang, "count": len(results), "results": results}


# ══════════════════════════════════════════════
# ROUTES — ASSESSMENT
# ══════════════════════════════════════════════

@app.get("/assessment/questions", tags=["Assessment"])
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return {
        "child_age":       age,
        "total_questions": len(qs),
        "scale": {"min": 1, "max": 5, "labels": {"1": "Never", "2": "Rarely", "3": "Sometimes", "4": "Often", "5": "Always"}},
        "questions": _format_questions_for_api(qs),
    }


@app.post("/assessment/submit", tags=["Assessment"])
def assessment_submit(req: AssessmentSubmitReq):
    conn = get_conn()
    lang: Lang = req.preferred_language if req.preferred_language in ("ar", "en") else "ar"  # type: ignore[assignment]
    try:
        ensure_user_exists(conn, req.user_id)

        if req.preferred_language is None:
            cur = conn.cursor()
            cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
            row = cur.fetchone()
            if row and row[0]: lang = row[0]

        if DEBUG:
            print(f"[ASSESSMENT] user={req.user_id}, child_age={req.child_age}, answers_count={len(req.answers)}")

        profile     = compute_personality_profile(req.answers, req.child_age, req.behavior_signals)
        assess_conf = compute_assessment_confidence(req.answers, req.child_age, req.behavior_signals)

        profile_to_store = {k: v for k, v in profile.items() if k != "_debug"}

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO assessments (user_id, child_age, assessment_confidence, result, created_at) VALUES (%s,%s,%s,%s,NOW())",
            (req.user_id, req.child_age, assess_conf["confidence"], json.dumps(profile_to_store))
        )
        conn.commit()
        update_memory(conn, req.user_id, "assessment_personality", req.child_age, note="Assessment submitted")
        log_event(conn, req.user_id, "assessment_submit", value=f"confidence={assess_conf['confidence']}")

        return {
            "ok":                     True,
            "message":                t("ok", lang),
            "trait_scores":           profile["trait_scores"],
            "top_traits":             profile["top_traits"],
            "low_traits":             profile["low_traits"],
            "possible_personalities": profile["possible_personalities"],
            "recommendations":        profile["recommendations"],
            "confidence":             assess_conf["confidence"],
            "assessment_meta":        assess_conf,
            "note":                   t("assessment_note", lang),
            "debug":                  profile.get("_debug", {}),
        }
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.get("/assessment/{user_id}", tags=["Assessment"])
def get_assessments(user_id: str):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, child_age, assessment_confidence, result, created_at FROM assessments WHERE user_id=%s ORDER BY created_at DESC",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "assessments": [
            {"id": r[0], "child_age": r[1], "confidence": float(r[2]), "result": r[3],
             "created_at": r[4].isoformat() if r[4] else None}
            for r in rows
        ]
    }


# ══════════════════════════════════════════════
# ROUTES — ANALYTICS
# ══════════════════════════════════════════════

@app.post("/analytics/event", tags=["Analytics"])
def analytics_event(req: AppEventRequest):
    conn = get_conn()
    ensure_user_exists(conn, req.user_id)
    log_event(conn, req.user_id, req.event_name, value=json.dumps(req.meta)[:300])
    conn.close()
    return {"ok": True}


@app.get("/analytics/summary", tags=["Analytics"])
def analytics_summary():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT event_type, COUNT(*) FROM analytics GROUP BY event_type")
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM analytics")
    total = cur.fetchone()[0]
    conn.close()
    return {"total_events": total, "by_type": {r[0]: r[1] for r in rows}}


@app.get("/analytics/user/{user_id}", tags=["Analytics"])
def analytics_user(user_id: str):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT event_id, event_type, value, created_at FROM analytics WHERE user_id=%s ORDER BY created_at DESC LIMIT 100",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "user_id": user_id,
        "recent_events": [{"event_id": r[0], "event_type": r[1], "value": r[2],
                           "created_at": r[3].isoformat() if r[3] else None} for r in rows]
    }


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
            "INSERT INTO feedback (user_id, message_id, rating, comment, topic, created_at) VALUES (%s,%s,%s,%s,%s,NOW())",
            (req.user_id, req.message_id, req.rating, req.comment, req.topic)
        )
        conn.commit()
        if req.comment:
            update_memory(conn, req.user_id, req.topic or "general_parenting", None,
                          note=f"FEEDBACK:{req.rating}:{req.comment}")
        log_event(conn, req.user_id, "feedback", value=f"{req.rating}:{req.message_id}")
        return {"ok": True}
    finally:
        conn.close()


# ══════════════════════════════════════════════
# ROUTES — CHAT HISTORY
# ══════════════════════════════════════════════

@app.get("/chat/{user_id}", tags=["Chat"])
def get_chat_history(user_id: str, limit: int = 50):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT message_id, message, response, created_at FROM chat_messages WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, max(1, min(200, limit)))
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "messages": [{"message_id": r[0], "user_message": r[1], "bot_reply": r[2],
                      "created_at": r[3].isoformat() if r[3] else None} for r in rows]
    }


# ══════════════════════════════════════════════
# ROUTES — CHAT (main)
# ══════════════════════════════════════════════

@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(req: ChatRequest):
    # ── 1. Safe extraction ────────────────────────────────────────────
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages list is empty")

    last_msg     = req.messages[-1]
    user_message = (last_msg.content or "").strip()

    if not user_message:
        raise HTTPException(status_code=400, detail="User message content is empty")

    # ── 2. Gemini availability ────────────────────────────────────────
    if not GEMINI_ENABLED or client is None:
        return ChatResponse(reply=t("gemini_disabled", detect_lang(user_message)))

    # ── 3. Detect language ────────────────────────────────────────────
    lang: Lang = (
        req.preferred_language  # type: ignore[assignment]
        if req.preferred_language in ("ar", "en")
        else detect_lang(user_message)
    )

    # ── 4. Hard guards ────────────────────────────────────────────────
    if hard_out_of_scope(user_message) or hard_medical(user_message):
        return ChatResponse(reply=t("out_of_scope_reply", lang))

    # ── 5. Risk guard ─────────────────────────────────────────────────
    risk_level = detect_risk_level(user_message)
    if risk_level == "high":
        conn = get_conn()
        try:
            ensure_user_exists(conn, req.user_id)
            log_event(conn, req.user_id, "risk_high", value=user_message[:200])
        finally:
            conn.close()
        return ChatResponse(reply=t("risk_high", lang))

    # ── 6. Kids content safety ────────────────────────────────────────
    if kids_safety_guard(user_message):
        return ChatResponse(reply=t("kids_safety", lang))

    # ── 7. Route & retrieve KB context ───────────────────────────────
    topic = "general_parenting"
    age   = req.child_age

    try:
        decision = gemini_route_decision(user_message, req.messages, req.child_age)
        if not decision.in_scope or decision.action == "refuse_out_of_scope":
            return ChatResponse(reply=t("scope_refusal", lang))
        topic = decision.topic
        age   = decision.extracted_child_age or req.child_age
    except Exception as exc:
        if DEBUG:
            print(f"[CHAT] Router error (non-fatal, using general_parenting): {exc}")

    retrieved_context, from_db = fts_or_kb_fallback(
        query=user_message, topic=topic, age=age, lang=lang, limit=3,
    )

    if DEBUG:
        print(f"[CHAT] retrieval source={'postgres_fts' if from_db else 'in_memory_kb'} "
              f"| results={len(retrieved_context)}")

    # ── 8. Build context block ────────────────────────────────────────
    if retrieved_context:
        context_lines = []
        for i, item in enumerate(retrieved_context, 1):
            q_text = item.get("question", "").strip()
            a_text = item.get("answer", item.get("tip", "")).strip()
            if a_text:
                context_lines.append(
                    f"[{i}] Q: {q_text}\n    A: {a_text}" if q_text else f"[{i}] {a_text}"
                )
        context_block = "\n\n".join(context_lines) if context_lines else "No specific context found."
    else:
        context_block = "No specific context found."

    # ── 9. Build Gemini prompt ────────────────────────────────────────
    prompt = f"""You are a professional parenting assistant called Rafiq.

Rules:
- ALWAYS provide a direct, helpful answer.
- NEVER ask follow-up questions.
- NEVER request more information from the user.
- Use the Knowledge Base Context below if it is relevant; otherwise rely on your general parenting knowledge.
- Respond in the same language as the User Question.
- Do NOT use Markdown formatting symbols such as **, *, or # anywhere in your response.
- Output ONLY the final plain-text answer — no preamble, no metadata, no formatting markers.

Knowledge Base Context:
{context_block}

User Question:
{user_message}

Now generate only the final answer."""

    # ── 10. Call Gemini ───────────────────────────────────────────────
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=600),
        )
        reply_text = strip_markdown((response.text or "").strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

    if not reply_text:
        reply_text = (
            "عذرًا، لم أتمكن من توليد رد. حاول مرة أخرى."
            if lang == "ar"
            else "Sorry, I couldn't generate a response. Please try again."
        )

    # ── 11. Auto-learn ────────────────────────────────────────────────
    maybe_learn_from_interaction(
        user_message=user_message,
        reply_text=reply_text,
        topic=topic,
        lang=lang,
        child_age=age,
        conn_factory=get_conn,
    )

    # ── 12. Persist to DB ─────────────────────────────────────────────
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
                (message_id, req.user_id, user_message, reply_text)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as db_exc:
        print(f"[CHAT] DB persistence error (non-fatal): {db_exc}")

    # ── 13. Return ────────────────────────────────────────────────────
    return ChatResponse(reply=reply_text)


# ══════════════════════════════════════════════
# ROUTES — PARENTING PLAN
# ══════════════════════════════════════════════

@app.post("/generate-parenting-plan/{user_id}", tags=["Parenting Plan"])
def generate_parenting_plan(user_id: str):
    """Generate a personalised 30-day parenting plan (English)."""
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini is disabled. Set GEMINI_API_KEY.")

    lang: Lang = "en"
    print(f"[PLAN] Generating parenting plan for user={user_id}, language=EN")

    conn = get_conn()
    try:
        ensure_user_exists(conn, user_id)
        cur = conn.cursor()

        cur.execute(
            "SELECT id, child_age, assessment_confidence, result, created_at FROM assessments WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=t("no_assessment_found", lang))

        assessment_id, child_age, assessment_confidence, result_raw, assessed_at = row

        try:
            result: Dict[str, Any] = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to parse assessment result: {exc}")

        if DEBUG:
            print(f"[PLAN] assessment result: {json.dumps(result, ensure_ascii=False)[:400]}")

        top_traits             = _norm_traits(result.get("top_traits", []))
        possible_personalities = _norm_personalities(result.get("possible_personalities", []))
        trait_scores           = _norm_scores(result.get("trait_scores", {}))

        top_arch_entry  = possible_personalities[0] if possible_personalities else {}
        top_archetype   = top_arch_entry.get("name",        "Not specified")
        archetype_desc  = top_arch_entry.get("description", "")
        archetype_needs = top_arch_entry.get("needs",       "")

        traits_text = "\n".join(f"  - {tr['trait'].replace('_', ' ').title()}: {tr['score']}%" for tr in top_traits) or "  - No data"
        scores_text = "\n".join(f"  - {k.replace('_', ' ').title()}: {v}%" for k, v in trait_scores.items()) or "  - No data"

        try:
            plan_text = gemini_generate_parenting_plan(
                child_age=child_age, top_archetype=top_archetype,
                archetype_desc=archetype_desc, archetype_needs=archetype_needs,
                traits_text=traits_text, scores_text=scores_text, lang=lang,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

        if not plan_text:
            raise HTTPException(status_code=502, detail="Gemini returned an empty plan.")

        try:
            cur.execute(
                "INSERT INTO parenting_plans (user_id, plan_text, plan_language, created_at) VALUES (%s,%s,%s,NOW()) RETURNING id, created_at",
                (user_id, plan_text, lang)
            )
            plan_row        = cur.fetchone()
            conn.commit()
            plan_id         = plan_row[0]
            plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
        except Exception as exc:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"DB error saving plan: {exc}")

        log_event(conn, user_id, "parenting_plan_generated",
                  value=f"plan_id={plan_id}, lang={lang}, assessment_id={assessment_id}")

        # FCM notification
        notification_sent    = False
        notification_warning = None

        if not _FIREBASE_AVAILABLE:
            notification_warning = "firebase-admin package is not installed."
        elif not _FIREBASE_CREDS_JSON:
            notification_warning = "FIREBASE_CREDENTIALS environment variable is not set."
        elif not FIREBASE_ENABLED:
            notification_warning = "Firebase failed to initialise at startup."
        else:
            notif_conn = None
            try:
                notif_conn = get_conn()
                notif_cur  = notif_conn.cursor()
                notif_cur.execute("SELECT fcm_token FROM users WHERE user_id=%s", (user_id,))
                token_row  = notif_cur.fetchone()
                fcm_token: Optional[str] = token_row[0] if token_row else None

                if not fcm_token:
                    notification_warning = "User has no registered FCM token. Call POST /register-token first."
                else:
                    message = fb_messaging.Message(
                        notification=fb_messaging.Notification(
                            title="📋 Your parenting plan is ready",
                            body="Your personalized 30-day plan has been generated.",
                        ),
                        token=fcm_token,
                        data={"type": "parenting_plan", "user_id": str(user_id), "plan_id": str(plan_id)},
                    )
                    fb_messaging.send(message)
                    notification_sent = True
            except AttributeError as ae:
                notification_warning = f"Firebase messaging object is None: {ae}"
            except Exception as fb_exc:
                err_str = str(fb_exc)
                if "UNREGISTERED" in err_str.upper() or "registration-token-not-registered" in err_str:
                    if notif_conn:
                        fix_cur = notif_conn.cursor()
                        fix_cur.execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (user_id,))
                        notif_conn.commit()
                    notification_warning = "FCM token expired — cleared. User must re-register."
                else:
                    notification_warning = f"Firebase send error: {err_str}"
            finally:
                if notif_conn:
                    try:
                        notif_conn.close()
                    except Exception:
                        pass

        response_payload: Dict[str, Any] = {
            "ok":                True,
            "message":           t("plan_created_title", lang),
            "user_id":           user_id,
            "plan_id":           plan_id,
            "created_at":        plan_created_at,
            "plan_language":     lang,
            "child_age":         child_age,
            "top_archetype":     top_archetype,
            "assessment_id":     assessment_id,
            "notification_sent": notification_sent,
            "plan_text":         plan_text,
        }
        if notification_warning:
            response_payload["notification_warning"] = notification_warning
        return response_payload

    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        conn.close()


@app.get("/parenting-plans/{user_id}", tags=["Parenting Plan"])
def get_parenting_plans(user_id: str, limit: int = 10):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        cur.execute(
            "SELECT id, plan_text, plan_language, created_at FROM parenting_plans WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, max(1, min(50, limit)))
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id, "total": len(rows),
            "plans": [{"id": r[0], "plan_text": r[1], "plan_language": r[2],
                       "created_at": r[3].isoformat() if r[3] else None} for r in rows],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


# ══════════════════════════════════════════════
# ROUTES — PDF EXPORT
# ══════════════════════════════════════════════

@app.get("/export-plan-pdf/{user_id}", tags=["Parenting Plan"])
def export_plan_pdf(user_id: str):
    """Export the latest parenting plan as a PDF (English)."""
    if not _REPORTLAB_AVAILABLE:
        raise HTTPException(status_code=503, detail=t("pdf_unavailable", "en"))

    PDF_LANG: Lang = "en"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pp.id, pp.plan_text, pp.created_at,
                   u.child_age,
                   a.result
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

        plan_id, plan_text, created_at, child_age, result_raw = row
        generated_at = created_at.isoformat() if created_at else ""

        top_archetype = "Not specified"
        if result_raw:
            try:
                result_obj    = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
                personalities = _norm_personalities(result_obj.get("possible_personalities", []))
                if personalities:
                    arch_id   = personalities[0].get("id", "")
                    arch_obj  = next((a for a in ARCHETYPES if a["id"] == arch_id), None)
                    top_archetype = arch_obj["name"] if arch_obj else (personalities[0].get("name") or "Not specified")
            except Exception as parse_exc:
                print(f"[PDF] Could not parse archetype: {parse_exc}")

        try:
            pdf_bytes = _build_parenting_plan_pdf(
                user_id=user_id, child_age=child_age,
                top_archetype=top_archetype, plan_text=plan_text or "",
                generated_at=generated_at, lang=PDF_LANG,
            )
        except Exception as pdf_exc:
            raise HTTPException(status_code=500, detail=f"PDF generation failed: {pdf_exc}")

        filename = f"parenting_plan_{user_id}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length":      str(len(pdf_bytes)),
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        conn.close()
