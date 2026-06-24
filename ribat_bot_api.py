"""
Rafiq Bot API — PRODUCTION v6.0
================================
Changes vs v5.1:
- REMOVED: pgvector, vector extension, embeddings, similarity search, semantic search
- REMOVED: plan_embeddings table and all related migrations
- REMOVED: expand_query_for_embedding, _gemini_embed_text, ingest_plan_to_knowledge_base,
           retrieve_plan_context_for_user
- FIXED:   PDF export reads plan_text reliably; no blank pages
- FIXED:   plan_text is always validated before DB insert (never NULL / empty)
- CHANGED: faq_knowledge_base auto-grows via Gemini answers (INSERT on every chat answer)
- ADDED:   Debug prints at every critical step (assessment → Gemini → plan → PDF)
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
    "gemini_disabled":        {"ar": "ميزة الشات غير مفعّلة.",
                               "en": "Chat feature is currently disabled."},
    "ok":                     {"ar": "تم بنجاح",       "en": "Success"},
    "out_of_scope_reply":     {"ar": "أنا بوت (رفيق) متخصص في دعم الأسرة.",
                               "en": "I'm Rafiq, a family support assistant."},
    "scope_refusal":          {"ar": "سؤالك خارج نطاق رفيق.",
                               "en": "Your question is outside Rafiq's scope."},
    "risk_high":              {"ar": "أنا قلقان عليك جدًا. تواصل فورًا مع شخص كبير موثوق.",
                               "en": "I'm very concerned. Please reach out to a trusted adult immediately."},
    "kids_safety":            {"ar": "خلّينا نخلي المحتوى مناسب للأطفال 🙏",
                               "en": "Let's keep content child-appropriate 🙏"},
    "assessment_note":        {"ar": "النتيجة إرشادية وليست تشخيصًا طبيًا.",
                               "en": "This result is indicative, not a clinical diagnosis."},
    "plan_notif_title":       {"ar": "📋 خطتك التربوية جاهزة 🎉",     "en": "Your Parenting Plan is Ready 🎉"},
    "plan_notif_body":        {"ar": "أعددنا خطة مخصصة لـ 15 يومًا لطفلك.",
                               "en": "We created a personalized 15-day plan for your child."},
    "plan_created_title":     {"ar": "تم إنشاء الخطة بنجاح",          "en": "Parenting plan generated successfully"},
    "token_saved":            {"ar": "تم حفظ رمز الإشعار بنجاح",      "en": "FCM token saved successfully"},
    "no_fcm_token":           {"ar": "المستخدم لا يملك رمز إشعار.",
                               "en": "User has no registered FCM token."},
    "fcm_token_expired":      {"ar": "رمز FCM لم يعد صالحًا.",
                               "en": "FCM token is no longer valid."},
    "firebase_not_configured":{"ar": "Firebase غير مُفعَّل.",
                               "en": "Firebase is not configured."},
    "no_assessment_found":    {"ar": "لا يوجد تقييم لهذا المستخدم.",
                               "en": "No assessment found for this user."},
    "no_plan_found":          {"ar": "لا توجد خطة تربوية لهذا المستخدم.",
                               "en": "No parenting plan found for this user."},
    "user_not_found":         {"ar": "المستخدم غير موجود.",            "en": "User not found."},
    "pdf_unavailable":        {"ar": "تصدير PDF غير متاح.",
                               "en": "PDF export is unavailable — reportlab is not installed."},
    "daily_tip_notif_title":  {"ar": "💡 نصيحة جديدة من رفيق",        "en": "💡 New Parenting Tip from Rafiq"},
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in ("ar", "en") else "ar"
    entry = _T.get(key, {})
    text = entry.get(lang) or entry.get("ar") or key if isinstance(entry, dict) else (entry or key)
    return text.format(**kwargs) if kwargs else text


def detect_lang(text: str) -> Lang:
    ar = len(re.findall(r'[\u0600-\u06FF]', text))
    en = len(re.findall(r'[a-zA-Z]', text))
    return "ar" if ar >= en else "en"


def user_lang(preferred_language: Optional[str], fallback_text: str = "") -> Lang:
    if preferred_language in ("ar", "en"):
        return preferred_language  # type: ignore
    return detect_lang(fallback_text)


# ══════════════════════════════════════════════
# MARKDOWN STRIPPING
# ══════════════════════════════════════════════

_MD_BOLD_ITALIC  = re.compile(r'\*{1,3}(.+?)\*{1,3}', re.DOTALL)
_MD_BOLD_UNDER   = re.compile(r'_{2}(.+?)_{2}',        re.DOTALL)
_MD_ITALIC_UNDER = re.compile(r'_(.+?)_',              re.DOTALL)
_MD_HEADING      = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MD_HR           = re.compile(r'^[-*_]{3,}\s*$', re.MULTILINE)
_MD_BACKTICK     = re.compile(r'`{1,3}(.+?)`{1,3}', re.DOTALL)


def strip_markdown(text: str) -> str:
    if not text:
        return text
    text = _MD_BOLD_ITALIC.sub(r'\1', text)
    text = _MD_BOLD_UNDER.sub(r'\1', text)
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
        KeepTogether,
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


# ══════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════

DEBUG          = os.getenv("RAFIQ_DEBUG", "0") == "1"
DATABASE_URL   = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)
ADMIN_KEY      = os.getenv("RAFIQ_ADMIN_KEY", "change-me")

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
            pdfmetrics.registerFont(TTFont("NotoArabic", FONT_NOTO_ARABIC))
            _FONT_ARABIC_REGISTERED = True
        if os.path.exists(FONT_NOTO_BOLD):
            pdfmetrics.registerFont(TTFont("NotoArabicBold", FONT_NOTO_BOLD))
        if os.path.exists(FONT_NOTO_LATIN):
            pdfmetrics.registerFont(TTFont("NotoLatin", FONT_NOTO_LATIN))
            _FONT_LATIN_REGISTERED = True
        print("PDF fonts registered ✔" if _FONT_ARABIC_REGISTERED else
              "PDF fonts NOT found — falling back to Helvetica.")
    except Exception as exc:
        print(f"Font registration warning: {exc}")


# ══════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════

app = FastAPI(
    title="Rafiq Bot API",
    version="6.0.0",
    description="Family support & parenting assistant — bilingual (ar/en) | FTS + Gemini RAG (no vectors)",
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
    """
    Apply all schema migrations idempotently.
    NOTE: No vector extension, no embeddings tables — removed in v6.0.
    """
    if not DATABASE_URL:
        print("Skipping DB migrations — DATABASE_URL not set")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur  = conn.cursor()

        # ── users ─────────────────────────────────────────────────────────────
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language VARCHAR(5) DEFAULT 'ar';")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS parent_name VARCHAR(200);")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS child_name  VARCHAR(200);")

        # ── daily_tips ────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_tips (
                id         SERIAL PRIMARY KEY,
                user_id    VARCHAR(100),
                tip        TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # ── parenting_plans ───────────────────────────────────────────────────
        # Minimal schema: id, user_id, plan_text, plan_language,
        #                 assessment_id, parent_name, child_name,
        #                 plan_days (JSONB), intro_letter, plan_duration, created_at
        cur.execute("""
            CREATE TABLE IF NOT EXISTS parenting_plans (
                id            SERIAL PRIMARY KEY,
                user_id       VARCHAR(100)  NOT NULL,
                plan_text     TEXT          NOT NULL,
                plan_language VARCHAR(5)    DEFAULT 'en',
                assessment_id INTEGER,
                parent_name   VARCHAR(200),
                child_name    VARCHAR(200),
                plan_days     JSONB,
                intro_letter  TEXT,
                plan_duration INTEGER       DEFAULT 15,
                created_at    TIMESTAMP     DEFAULT NOW()
            );
        """)
        for col_sql in [
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_language VARCHAR(5)  DEFAULT 'en';",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS assessment_id INTEGER;",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS parent_name   VARCHAR(200);",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS child_name    VARCHAR(200);",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_days     JSONB;",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS intro_letter  TEXT;",
            "ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_duration INTEGER DEFAULT 15;",
        ]:
            cur.execute(col_sql)

        # ── faq_knowledge_base ────────────────────────────────────────────────
        # category replaces topic for simplicity; no vectors anywhere.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS faq_knowledge_base (
                id            SERIAL PRIMARY KEY,
                question      TEXT          NOT NULL,
                answer        TEXT          NOT NULL,
                category      VARCHAR(100)  DEFAULT 'parenting',
                lang          VARCHAR(5)    DEFAULT 'ar',
                search_vector TSVECTOR,
                created_at    TIMESTAMP     DEFAULT NOW(),
                updated_at    TIMESTAMP     DEFAULT NOW()
            );
        """)
        for col_sql in [
            "ALTER TABLE faq_knowledge_base ADD COLUMN IF NOT EXISTS category VARCHAR(100) DEFAULT 'parenting';",
            "ALTER TABLE faq_knowledge_base ADD COLUMN IF NOT EXISTS lang     VARCHAR(5)   DEFAULT 'ar';",
        ]:
            cur.execute(col_sql)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_kb_fts      ON faq_knowledge_base USING GIN (search_vector);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_kb_category ON faq_knowledge_base (category);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_kb_lang     ON faq_knowledge_base (lang);")

        cur.execute("""
            CREATE OR REPLACE FUNCTION faq_kb_search_vector_update()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('simple', COALESCE(NEW.question, '')), 'A') ||
                    setweight(to_tsvector('simple', COALESCE(NEW.answer,   '')), 'B');
                NEW.updated_at := NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        cur.execute("DROP TRIGGER IF EXISTS trig_faq_kb_fts ON faq_knowledge_base;")
        cur.execute("""
            CREATE TRIGGER trig_faq_kb_fts
            BEFORE INSERT OR UPDATE ON faq_knowledge_base
            FOR EACH ROW EXECUTE FUNCTION faq_kb_search_vector_update();
        """)
        cur.execute("""
            UPDATE faq_knowledge_base
            SET search_vector =
                setweight(to_tsvector('simple', COALESCE(question, '')), 'A') ||
                setweight(to_tsvector('simple', COALESCE(answer,   '')), 'B')
            WHERE search_vector IS NULL;
        """)

        conn.commit()
        conn.close()
        print("DB migrations applied ✔ (v6.0 — no vectors)")
    except Exception as exc:
        print(f"DB migration warning: {exc}")


# ══════════════════════════════════════════════
# FULL-TEXT SEARCH  (FTS only — no vectors)
# ══════════════════════════════════════════════

def fts_knowledge_base(
    query: str,
    category: Optional[str] = None,
    lang: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Retrieve relevant Q/A pairs from faq_knowledge_base using PostgreSQL FTS."""
    if not query or not query.strip():
        return []

    results: List[Dict[str, Any]] = []
    try:
        conn = get_conn()
        cur  = conn.cursor()

        filter_clauses: List[str] = []
        params_extra:   List[Any] = []

        if category:
            filter_clauses.append("category = %s")
            params_extra.append(category)
        if lang:
            filter_clauses.append("lang = %s")
            params_extra.append(lang)

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
                SELECT id, question, answer, category,
                       ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS rank
                FROM faq_knowledge_base
                WHERE search_vector @@ to_tsquery('simple', %s)
                {where_extra}
                ORDER BY rank DESC
                LIMIT %s;
            """
            cur.execute(fts_sql, [tsquery_str, tsquery_str] + params_extra + [limit])
            for row in cur.fetchall():
                results.append({
                    "id": row[0], "question": row[1], "answer": row[2],
                    "category": row[3], "rank": float(row[4]), "method": "fts",
                })

        if not results and tokens:
            like_pattern = f"%{tokens[0]}%"
            ilike_sql = f"""
                SELECT id, question, answer, category, 0.0 AS rank
                FROM faq_knowledge_base
                WHERE (question ILIKE %s OR answer ILIKE %s)
                {where_extra}
                ORDER BY CASE WHEN question ILIKE %s THEN 0 ELSE 1 END, updated_at DESC
                LIMIT %s;
            """
            cur.execute(ilike_sql,
                        [like_pattern, like_pattern, like_pattern] + params_extra + [limit])
            for row in cur.fetchall():
                results.append({
                    "id": row[0], "question": row[1], "answer": row[2],
                    "category": row[3], "rank": float(row[4]), "method": "ilike",
                })

        conn.close()
    except Exception as exc:
        print(f"[FTS] retrieval error: {exc}")

    return results


def fts_insert_qa(question: str, answer: str, category: str, lang: str) -> Optional[int]:
    """
    Insert a new Q/A pair into faq_knowledge_base.
    Called after every successful Gemini chat reply so the KB grows organically.
    Skips duplicate questions (token overlap >= 0.8).
    """
    if not question.strip() or not answer.strip():
        return None

    # Light duplicate check via FTS before inserting
    existing = fts_knowledge_base(query=question, category=category, lang=lang, limit=5)
    for ex in existing:
        if _token_overlap(question, ex.get("question", "")) >= 0.8:
            print(f"[KB] Skipping duplicate — question similar to id={ex['id']}")
            return None

    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO faq_knowledge_base (question, answer, category, lang, created_at) "
            "VALUES (%s, %s, %s, %s, NOW()) RETURNING id",
            (question.strip(), answer.strip(), category, lang),
        )
        row = cur.fetchone()
        conn.commit()
        new_id = row[0] if row else None
        conn.close()
        print(f"[KB] Inserted new Q/A pair — id={new_id} category={category}")
        return new_id
    except Exception as exc:
        print(f"[KB] Insert error: {exc}")
        return None


def _token_overlap(a: str, b: str) -> float:
    """Simple Jaccard overlap for duplicate detection."""
    _diac = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")

    def _tok(text: str):
        t = _diac.sub("", text.lower())
        for x, y in [("أ","ا"),("إ","ا"),("آ","ا"),("ى","ي"),("ة","ه")]:
            t = t.replace(x, y)
        return set(w for w in re.sub(r"[^\w\u0600-\u06FF]+", " ", t).split() if len(w) >= 2)

    ta, tb = _tok(a), _tok(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ══════════════════════════════════════════════
# IN-MEMORY KB  (fallback when DB has nothing)
# ══════════════════════════════════════════════

KB: List[Dict[str, Any]] = [
    {"topic": "teen_communication", "age_min": 12, "age_max": 18,
     "tags": ["مراهق", "مراهقة", "مش بيرد", "ساكت"],
     "tip": "ابدئي في وقت هدوء بجملة: «أنا مهتمة أفهمك مش ألومك». اسألي سؤال واحد مفتوح."},
    {"topic": "anger", "age_min": 6, "age_max": 18,
     "tags": ["عصبية", "غضب", "صراخ"],
     "tip": "وقت الغضب قللي الكلام وثبتي حدود هادية. بعد ما يهدى: «إيه اللي ضايقك؟»."},
    {"topic": "screen_addiction", "age_min": 8, "age_max": 18,
     "tags": ["موبايل", "شاشات", "إدمان"],
     "tip": "اعملي اتفاق مكتوب: وقت شاشة + وقت عيلة. قلّلي تدريجيًا مع بديل ممتع."},
    {"topic": "bullying", "age_min": 6, "age_max": 18,
     "tags": ["تنمر", "مدرسة", "سخرية"],
     "tip": "صدّقي مشاعره، خدي تفاصيل بسيطة، تواصلي مع المدرسة."},
    {"topic": "study_focus", "age_min": 8, "age_max": 18,
     "tags": ["مذاكرة", "تركيز", "واجب"],
     "tip": "قسّمي المذاكرة لبلوكات 25 دقيقة + 5 راحة."},
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


class AssessmentSubmitReq(BaseModel):
    user_id: str
    child_age: Optional[int] = None
    answers: List[Dict[str, Any]] = []
    behavior_signals: Optional[Dict[str, Any]] = None
    preferred_language: Optional[str] = None


class FaqKbAddRequest(BaseModel):
    admin_key: str
    question: str
    answer: str
    category: str = "parenting"
    lang: str = "ar"


class GeneratePlanRequest(BaseModel):
    parent_name: Optional[str] = None
    child_name:  Optional[str] = None


class RegisterTokenReq(BaseModel):
    user_id: str
    fcm_token: str


class SendDailyTipReq(BaseModel):
    user_id: str
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

OUT_OF_SCOPE_KW = ["برمجة", "كود", "flutter", "android", "python", "java", "c++",
                   "backend", "front", "database", "debug", "algorithm"]
MEDICAL_KW      = ["جرعة", "دواء", "حبوب", "مضاد", "تشخيص", "روشتة", "medication", "diagnosis"]
KIDS_UNSAFE_KW  = ["انتحار", "إباحية", "اباحية", "سلاح", "مخدرات"]
RISK_HIGH_KW    = ["عايز أموت", "مش عايز أعيش", "هأذي نفسي", "انتحار", "هنتحر", "هقتل", "أذي نفسي"]
RISK_MEDIUM_KW  = ["خوف شديد", "هلع", "نوبات", "قلق جامد", "اكتئاب", "حزين طول الوقت"]


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
                "preferred_language": "ar", "parent_name": None, "child_name": None}
    raw   = row[0]
    notes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return {"child_age": row[1], "name": row[2], "email": row[3], "notes": notes,
            "preferred_language": row[4] or "ar", "parent_name": row[5], "child_name": row[6]}


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
# ASSESSMENT ENGINE
# ══════════════════════════════════════════════

ASSESSMENT_OPTIONS = ["Never", "Rarely", "Sometimes", "Often", "Always"]
ALL_TRAITS         = ["leadership", "sociability", "empathy", "self_control",
                      "focus", "curiosity", "adaptability", "sensitivity"]

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
     "text": "My child resists the urge to act impulsively."},
    {"id": "q20", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 2},
     "text": "My child gets upset easily by criticism, loud noises, or unexpected changes."},
    {"id": "q21", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 3},
     "text": "My child feels emotions deeply and needs extra reassurance after conflict."},
]

_QS_NORM: Dict[str, Dict[str, Any]] = {q["id"].strip().lower(): q for q in ASSESSMENT_QUESTIONS}

ARCHETYPES: List[Dict[str, Any]] = [
    {"id": "leader",      "name": "The Leader",
     "description": "Takes initiative, organizes peers, and thrives when given responsibility.",
     "needs": "Clear boundaries, meaningful responsibilities, and leadership opportunities.",
     "profile": {"leadership": 80, "focus": 60, "sociability": 55}},
    {"id": "explorer",    "name": "The Explorer",
     "description": "Curious, adventurous, and constantly seeking new experiences.",
     "needs": "New challenges, hands-on projects, and freedom to experiment.",
     "profile": {"curiosity": 80, "adaptability": 65}},
    {"id": "thinker",     "name": "The Thinker",
     "description": "Reflective and analytical — prefers depth over breadth.",
     "needs": "Quiet time, intellectual challenges, and space for independent thought.",
     "profile": {"focus": 80, "curiosity": 65, "sociability": 30}},
    {"id": "helper",      "name": "The Helper",
     "description": "Warm, caring, and highly attuned to the emotions of others.",
     "needs": "Recognition of emotional contributions and opportunities to support peers.",
     "profile": {"empathy": 85, "sociability": 60}},
    {"id": "peacemaker",  "name": "The Peacemaker",
     "description": "Conflict-averse, diplomatic, focused on harmony.",
     "needs": "Teaching assertiveness, safe expression of opinions.",
     "profile": {"empathy": 75, "self_control": 70}},
    {"id": "energetic",   "name": "The Energetic",
     "description": "High energy, enthusiastic, and socially motivated.",
     "needs": "Physical outlets, structured energy release, consistent boundaries.",
     "profile": {"sociability": 75, "curiosity": 60, "self_control": 35}},
    {"id": "sensitive",   "name": "The Sensitive",
     "description": "Deeply empathetic and emotionally aware — feels things intensely.",
     "needs": "Emotional validation, predictable routines, and a calm safe environment.",
     "profile": {"sensitivity": 85, "empathy": 65}},
    {"id": "independent", "name": "The Independent",
     "description": "Values autonomy — prefers doing things on their own terms.",
     "needs": "Structured choices, respected boundaries, gradual responsibility.",
     "profile": {"leadership": 55, "sociability": 25, "focus": 60}},
    {"id": "planner",     "name": "The Planner",
     "description": "Orderly, methodical, motivated by structure and clear goals.",
     "needs": "Simple schedules, clear expectations, positive reinforcement.",
     "profile": {"focus": 85, "self_control": 75}},
    {"id": "challenger",  "name": "The Challenger",
     "description": "Questions authority, tests limits, learns through debate.",
     "needs": "Few but firm rules, negotiation space, consistent logical consequences.",
     "profile": {"leadership": 65, "self_control": 30, "sensitivity": 50}},
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
        qid = _normalize_answer_id(a.get("question_id") or a.get("id"))
        val = _extract_answer_value(a)
        q   = _QS_NORM.get(qid)
        if q is None:
            unmatched_ids.append(str(a.get("question_id") or a.get("id")))
            continue
        if val is None:
            unmatched_ids.append(f"{qid}(bad_value)")
            continue
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
        return (sum(100 - abs(scores.get(tr, 50) - v) for tr, v in arch_profile.items())
                / max(1, len(arch_profile)))

    ranked = sorted(
        [{"id": a["id"], "name": a["name"], "description": a["description"],
          "needs": a["needs"], "match_pct": int(round(_sim(a["profile"])))}
         for a in ARCHETYPES],
        key=lambda x: x["match_pct"], reverse=True,
    )
    top_archetype = ranked[0]
    top_traits    = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    low_traits    = sorted(scores.items(), key=lambda kv: kv[1])[:2]

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
                "sensitivity":  "Create a calm-down corner; validate feelings first.",
            }.get(trait, "Provide consistent support and positive reinforcement.")
            recs.append(f"Low {trait.replace('_', ' ').title()} ({score}%): {advice}")
    return recs


def compute_assessment_confidence(answers, child_age, behavior_signals):
    total = len(ASSESSMENT_QUESTIONS)
    valid = sum(
        1 for a in (answers or [])
        if _QS_NORM.get(_normalize_answer_id(a.get("question_id") or a.get("id")))
        and _extract_answer_value(a) is not None
    )
    score = int(round(valid / total * 65)) if total else 0
    notes = [f"coverage={int(round(valid/total*100))}%" if total else "no_questions"]
    if child_age:        score += 15; notes.append("age_provided")
    if behavior_signals: score += 10; notes.append("behavior_signals")
    if valid < max(3, total // 3):
        score = max(0, score - 15); notes.append("low_answer_count_penalty")
    return {"confidence": max(0, min(100, score)), "valid_answers": valid,
            "total_questions": total, "notes": notes}


# ══════════════════════════════════════════════
# PROFILE NORMALISATION HELPERS
# ══════════════════════════════════════════════

def _norm_traits(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({"trait": str(item.get("trait") or item.get("name") or ""),
                        "score": int(item.get("score", 0))})
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
    return out


def _norm_scores(raw: Any) -> Dict[str, int]:
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}
    return {}


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
        "Output ONLY valid JSON matching the schema."
    )
    history_str = "\n".join(f"{m.role}: {m.content}" for m in history[-6:])
    prompt = (f"System: {system}\n\nConversation:\n{history_str}\n\n"
              f"User message:\n{user_text}\n\nKnown child age: {fallback_age}")
    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RouteDecision,
            temperature=0,
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
# PLAN GENERATION  (v6.0 — direct assessment → Gemini → plan_text)
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
    age_str    = f"{child_age} years old" if child_age else "your child"
    child_str  = child_name or "your child"
    traits_str = ", ".join(t_["trait"].replace("_", " ").title() for t_ in top_traits[:3])

    if lang == "ar":
        prompt = (
            f"أنت مدرب تربوي دافئ. اكتب رسالة افتتاحية شخصية لوالد/ة اسمه/ا {parent_name}، "
            f"طفله/ا اسمه/ا {child_str} وعمره/ا {age_str}. "
            f"النمط الشخصي للطفل هو {top_archetype} — {archetype_desc}. "
            f"أبرز صفاته: {traits_str}.\n\n"
            "الرسالة: تبدأ بـ «عزيزتي/عزيزي»، داعمة وعاطفية، 3-4 فقرات قصيرة. "
            "نص عربي فقط بدون Markdown."
        )
    else:
        prompt = (
            f"You are a warm parenting coach. Write a heartfelt letter for parent {parent_name}. "
            f"Child: {child_str}, aged {age_str}. Profile: '{top_archetype}' — {archetype_desc}. "
            f"Top strengths: {traits_str}.\n\n"
            "Start with 'Dear {parent_name},'. Warm, supportive, 3-4 short paragraphs. Plain text only."
        )

    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.75, max_output_tokens=600),
    )
    return strip_markdown((resp.text or "").strip())


def gemini_generate_15day_plan(
    parent_name: str,
    child_name: str,
    child_age: Optional[int],
    top_archetype: str,
    archetype_desc: str,
    archetype_needs: str,
    top_traits: List[Dict],
    trait_scores: Dict[str, int],
    lang: Lang,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Call Gemini to generate a structured 15-day parenting plan.

    Returns:
        plan_days  — list of 15 day dicts (structured data for JSON storage)
        plan_text  — plain-text version guaranteed non-empty (stored in DB)

    Raises:
        HTTPException 502 if Gemini returns unusable content.
    """
    _require_gemini()

    age_str    = f"{child_age} years old" if child_age else "age not specified"
    child_str  = child_name or "the child"
    traits_txt = "\n".join(f"  - {d['trait'].replace('_',' ').title()}: {d['score']}%"
                           for d in top_traits) or "  - No data"
    scores_txt = "\n".join(f"  - {k.replace('_',' ').title()}: {v}%"
                           for k, v in trait_scores.items()) or "  - No data"

    # ── DEBUG: print assessment data being sent to Gemini ─────────────────────
    print("=" * 60)
    print("[PLAN] Sending assessment data to Gemini:")
    print(f"  parent_name    : {parent_name}")
    print(f"  child_name     : {child_str}")
    print(f"  child_age      : {age_str}")
    print(f"  top_archetype  : {top_archetype}")
    print(f"  archetype_desc : {archetype_desc}")
    print(f"  archetype_needs: {archetype_needs}")
    print(f"  top_traits     :\n{traits_txt}")
    print(f"  all_scores     :\n{scores_txt}")
    print("=" * 60)

    schema_example = json.dumps([
        {
            "day": 1,
            "goal": "Build trust through connection",
            "activity": "20-minute device-free play",
            "how_to_do_it": "Sit on the floor together. Let your child lead.",
            "why_it_helps": "Uninterrupted attention strengthens the attachment bond.",
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
            f"- أبرز الصفات:\n{traits_txt}\n"
            f"- جميع الدرجات:\n{scores_txt}\n\n"
            f"أعد المخرجات كـ JSON array فقط بدون أي نص إضافي:\n{schema_example}\n\n"
            "المفاتيح: day, goal, activity, how_to_do_it, why_it_helps, tip\n"
            "15 يومًا بالضبط. JSON فقط."
        )
    else:
        prompt = (
            f"You are a professional parenting coach. Generate a personalized 15-day parenting plan.\n\n"
            f"Child info:\n"
            f"- Name: {child_str}\n"
            f"- Age: {age_str}\n"
            f"- Personality: {top_archetype} — {archetype_desc}\n"
            f"- Needs: {archetype_needs}\n"
            f"- Top traits:\n{traits_txt}\n"
            f"- All trait scores:\n{scores_txt}\n\n"
            f"Return ONLY a valid JSON array, no extra text:\n{schema_example}\n\n"
            "Exactly 15 day objects. JSON only."
        )

    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=8000),
    )
    raw_text = (resp.text or "").strip()
    raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text).rstrip("`").strip()

    # ── DEBUG: print Gemini raw response ──────────────────────────────────────
    print("[PLAN] Gemini raw response (first 500 chars):")
    print(raw_text[:500])
    print("=" * 60)

    plan_days: List[Dict[str, Any]] = []

    try:
        parsed = json.loads(raw_text)

        if isinstance(parsed, list) and len(parsed) > 0:
            plan_days = parsed

    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', raw_text, re.DOTALL)

        if match:
            try:
                plan_days = json.loads(match.group(0))
            except Exception:
                pass


    # ── Fallback: Gemini returned normal text instead of JSON ────────────────
    if not plan_days:
        _plan_logger.warning(
            "[plan] Gemini did not return valid JSON. Saving raw response as text."
        )

        plan_text = raw_text

        if not plan_text.strip():
            raise HTTPException(
                status_code=502,
                detail="Gemini returned empty response."
            )

        print("[PLAN] Using raw Gemini text as plan_text:")
        print(plan_text[:600])
        print("=" * 60)

        return [], plan_text


    # Convert structured days to guaranteed non-empty plain text
    plan_text = _days_to_plain_text(plan_days)

    if not plan_text.strip():
        raise HTTPException(
            status_code=502,
            detail="Plan text is empty after conversion."
        )


    # DEBUG
    print("[PLAN] Generated plan_text (first 600 chars):")
    print(plan_text[:600])
    print("=" * 60)


    return plan_days, plan_text


def _days_to_plain_text(days: List[Dict[str, Any]]) -> str:
    """Convert structured day dicts to human-readable plain text."""
    lines = []
    for d in days:
        day_num = d.get("day", "?")
        lines.append(f"Day {day_num}")
        lines.append(f"Goal: {d.get('goal', '').strip()}")
        lines.append(f"Activity: {d.get('activity', '').strip()}")
        lines.append(f"How to do it: {d.get('how_to_do_it', '').strip()}")
        lines.append(f"Why it helps: {d.get('why_it_helps', '').strip()}")
        lines.append(f"Tip: {d.get('tip', '').strip()}")
        lines.append("")
    return "\n".join(lines).strip()


# ══════════════════════════════════════════════
# PDF HELPERS
# ══════════════════════════════════════════════

def _safe_xml(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _shape_arabic(text: str) -> str:
    if not _ARABIC_SHAPING:
        return text
    return bidi_display(arabic_reshaper.reshape(text))


def _pdf_text(text: str, lang: str) -> str:
    return _shape_arabic(text) if lang == "ar" else text


def _pick_font(bold: bool, lang: str) -> str:
    if lang == "ar" and _FONT_ARABIC_REGISTERED:
        return "NotoArabicBold" if bold else "NotoArabic"
    if lang == "en" and _FONT_LATIN_REGISTERED:
        return "NotoLatin"
    return "Helvetica-Bold" if bold else "Helvetica"


def _parse_plan_days_from_text(plan_text: str) -> List[Dict[str, Any]]:
    """
    Parse structured day dicts from plan_text (plain-text fallback).
    Used when plan_days JSONB column is absent or empty.
    """
    days: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for raw_line in plan_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r'^Day \d+$', line):
            if current:
                days.append(current)
            current = {"day": int(line.split()[1]), "goal": "", "activity": "",
                       "how_to_do_it": "", "why_it_helps": "", "tip": ""}
        elif line.startswith("Goal:"):          current["goal"]         = line[5:].strip()
        elif line.startswith("Activity:"):      current["activity"]     = line[9:].strip()
        elif line.startswith("How to do it:"): current["how_to_do_it"] = line[13:].strip()
        elif line.startswith("Why it helps:"): current["why_it_helps"] = line[13:].strip()
        elif line.startswith("Tip:"):           current["tip"]          = line[4:].strip()
    if current:
        days.append(current)
    return days


def _build_parenting_plan_pdf(
    user_id: str,
    parent_name: str,
    child_name: str,
    child_age: Optional[int],
    top_archetype: str,
    intro_letter: str,
    plan_days: List[Dict[str, Any]],
    generated_at: str,
    lang: str = "en",
) -> bytes:
    """
    Build a clean PDF for the 15-day parenting plan.
    plan_days must be a non-empty list — validated by caller before this is invoked.
    """
    buf = io.BytesIO()
    W, _ = A4
    USABLE_W = W - 4 * cm

    brand_green  = colors.HexColor("#1B6B3A")
    brand_light  = colors.HexColor("#E8F5E9")
    brand_dark   = colors.HexColor("#0D4A28")
    label_bg     = colors.HexColor("#D0EAD8")
    accent_gold  = colors.HexColor("#C8860A")
    accent_light = colors.HexColor("#FFF8E7")
    text_dark    = colors.HexColor("#1A1A1A")
    text_muted   = colors.HexColor("#6B7280")
    day_bg       = colors.HexColor("#F7FAF8")
    border_color = colors.HexColor("#B2DFBB")

    text_align = TA_RIGHT if lang == "ar" else TA_LEFT
    font_body  = _pick_font(False, lang)
    font_bold  = _pick_font(True,  lang)

    s_title      = ParagraphStyle("T",   fontSize=20, textColor=colors.white,
                                   alignment=TA_CENTER, fontName=font_bold, leading=26)
    s_subtitle   = ParagraphStyle("Sub", fontSize=11, textColor=colors.HexColor("#C8F7DC"),
                                   alignment=TA_CENTER, fontName=font_body)
    s_lbl        = ParagraphStyle("Lbl", fontSize=8,  textColor=brand_dark, fontName=font_bold, leading=10)
    s_val        = ParagraphStyle("Val", fontSize=9,  textColor=text_dark, fontName=font_body, leading=12)
    s_letter_hd  = ParagraphStyle("LHd",fontSize=13, textColor=brand_dark, fontName=font_bold, spaceAfter=8)
    s_letter     = ParagraphStyle("Ltr", fontSize=10.5, textColor=text_dark, fontName=font_body,
                                   leading=17, spaceAfter=6, alignment=text_align)
    s_section_hd = ParagraphStyle("SHd", fontSize=12, textColor=brand_dark, fontName=font_bold,
                                   spaceBefore=12, spaceAfter=4)
    s_day_num    = ParagraphStyle("DN",  fontSize=11, textColor=colors.white, fontName=font_bold,
                                   alignment=TA_CENTER, leading=14)
    s_day_goal   = ParagraphStyle("DG",  fontSize=11, textColor=brand_dark, fontName=font_bold,
                                   alignment=text_align, leading=14)
    s_field_lbl  = ParagraphStyle("FL",  fontSize=8,  textColor=accent_gold, fontName=font_bold,
                                   spaceBefore=4, spaceAfter=1, leading=10)
    s_field_val  = ParagraphStyle("FV",  fontSize=9.5, textColor=text_dark, fontName=font_body,
                                   leading=14, spaceAfter=2, alignment=text_align)
    s_tip        = ParagraphStyle("Tip", fontSize=9,  textColor=colors.HexColor("#2E7D32"),
                                   fontName=font_bold, leading=13, alignment=text_align)
    s_footer     = ParagraphStyle("Ftr", fontSize=7.5, textColor=text_muted,
                                   alignment=TA_CENTER, fontName=font_body)

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
        title=f"Rafiq Parenting Plan — {user_id}",
    )
    story = []

    # ── Banner ────────────────────────────────────────────────────────────────
    banner = Table(
        [
            [Paragraph(_safe_xml(_pdf_text("Personalised Parenting Plan", lang)), s_title)],
            [Paragraph(_safe_xml(_pdf_text("15-Day Plan  \u2022  Rafiq AI", lang)), s_subtitle)],
        ],
        colWidths=[USABLE_W],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_green),
        ("TOPPADDING",    (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))
    story.append(banner)
    story.append(Spacer(1, 10))

    # ── Info table ────────────────────────────────────────────────────────────
    cw = [USABLE_W * 0.18, USABLE_W * 0.32, USABLE_W * 0.18, USABLE_W * 0.32]

    def _lbl(txt): return Paragraph(_safe_xml(_pdf_text(txt, lang)), s_lbl)
    def _val(txt): return Paragraph(_safe_xml(_pdf_text(str(txt), lang)), s_val)

    age_str  = (f"{child_age} {'سنة' if lang == 'ar' else 'years'}"
                if child_age else ("—"))
    date_str = generated_at[:10] if generated_at else "—"

    info_data = [
        [_lbl("Parent Name"),  _val(parent_name or "—"), _lbl("Child Name"),    _val(child_name or "—")],
        [_lbl("Child Age"),    _val(age_str),             _lbl("Child Profile"), _val(top_archetype)],
        [_lbl("Generated"),    _val(date_str),            _lbl("User ID"),       _val(user_id)],
    ]
    info_tbl = Table(info_data, colWidths=cw)
    info_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_light),
        ("BACKGROUND",    (0, 0), (0, -1),  label_bg),
        ("BACKGROUND",    (2, 0), (2, -1),  label_bg),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#BBDDC7")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.5, color=brand_green, spaceAfter=0))
    story.append(Spacer(1, 12))

    # ── Intro letter ──────────────────────────────────────────────────────────
    story.append(Paragraph(_safe_xml(_pdf_text("A Personal Note For You", lang)), s_letter_hd))
    story.append(HRFlowable(width="40%", thickness=1.5, color=accent_gold, spaceAfter=8))
    for para in (intro_letter or "").split("\n\n"):
        para = para.strip()
        if para:
            story.append(Paragraph(_safe_xml(_pdf_text(para, lang)), s_letter))
    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#DDDDDD"), spaceAfter=0))
    story.append(Spacer(1, 14))

    # ── Day cards ─────────────────────────────────────────────────────────────
    story.append(Paragraph(
        _safe_xml(_pdf_text("Your 15-Day Parenting Journey", lang)), s_section_hd))
    story.append(Spacer(1, 6))

    BADGE_W   = 1.8 * cm
    CONTENT_W = USABLE_W - BADGE_W - 0.3 * cm

    for day in plan_days:
        day_num  = day.get("day",         "?")
        goal     = day.get("goal",        "")
        activity = day.get("activity",    "")
        how_to   = day.get("how_to_do_it","")
        why      = day.get("why_it_helps","")
        tip_text = day.get("tip",         "")

        hdr = Table(
            [[Paragraph(f"Day<br/>{day_num}", s_day_num),
              Paragraph(_safe_xml(_pdf_text(goal, lang)), s_day_goal)]],
            colWidths=[BADGE_W, CONTENT_W],
        )
        hdr.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (0, 0), brand_green),
            ("BACKGROUND",    (1, 0), (1, 0), colors.HexColor("#EBF5EE")),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING",   (0, 0), (0, 0), 4),
            ("RIGHTPADDING",  (0, 0), (0, 0), 4),
            ("LEFTPADDING",   (1, 0), (1, 0), 10),
            ("RIGHTPADDING",  (1, 0), (1, 0), 8),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))

        tip_tbl = Table(
            [[Paragraph("\U0001f4a1 " + _safe_xml(_pdf_text(tip_text, lang)), s_tip)]],
            colWidths=[USABLE_W - 0.4 * cm],
        )
        tip_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), accent_light),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ]))

        inner_rows = [
            [Paragraph(_safe_xml(_pdf_text("Activity",      lang)), s_field_lbl)],
            [Paragraph(_safe_xml(_pdf_text(activity,         lang)), s_field_val)],
            [Paragraph(_safe_xml(_pdf_text("How to do it",  lang)), s_field_lbl)],
            [Paragraph(_safe_xml(_pdf_text(how_to,           lang)), s_field_val)],
            [Paragraph(_safe_xml(_pdf_text("Why it helps",  lang)), s_field_lbl)],
            [Paragraph(_safe_xml(_pdf_text(why,              lang)), s_field_val)],
        ]
        inner_tbl = Table(inner_rows, colWidths=[USABLE_W - 0.4 * cm])

        card_elems = [hdr, Spacer(1, 4), inner_tbl, Spacer(1, 6), tip_tbl, Spacer(1, 4)]
        card_wrapper = Table([[e] for e in card_elems], colWidths=[USABLE_W])
        card_wrapper.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), day_bg),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("BOX",           (0, 0), (-1, -1), 1, border_color),
        ]))
        story.append(KeepTogether([card_wrapper, Spacer(1, 8)]))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#CCCCCC"), spaceAfter=5))
    story.append(Paragraph(
        _safe_xml("Generated by Rafiq AI — This plan is for guidance only and is not a clinical diagnosis."),
        s_footer,
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════
# FCM NOTIFICATION
# ══════════════════════════════════════════════

def _send_fcm_notification(
    user_id: str,
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not FIREBASE_ENABLED:
        return {"sent": False, "warning": "Firebase not configured"}
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT fcm_token FROM users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as exc:
        return {"sent": False, "warning": f"DB error: {exc}"}
    if not row or not row[0]:
        return {"sent": False, "warning": "No FCM token"}
    try:
        fb_messaging.send(fb_messaging.Message(
            notification=fb_messaging.Notification(title=title, body=body),
            token=row[0], data=data or {},
        ))
        return {"sent": True, "warning": None}
    except fb_messaging.UnregisteredError:
        try:
            conn2 = get_conn()
            conn2.cursor().execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (user_id,))
            conn2.commit(); conn2.close()
        except Exception:
            pass
        return {"sent": False, "warning": "FCM token expired — cleared."}
    except Exception as exc:
        return {"sent": False, "warning": f"FCM error: {exc}"}


# ══════════════════════════════════════════════
# ROUTES — SYSTEM
# ══════════════════════════════════════════════

@app.get("/", tags=["System"])
def home():
    return {"status": "Rafiq running 🚀", "version": "6.0.0",
            "retrieval": "PostgreSQL FTS (no vectors)", "plan_duration": "15 days"}


@app.get("/health", tags=["System"])
def health():
    return {
        "ok": True, "model": GEMINI_MODEL, "gemini_enabled": GEMINI_ENABLED,
        "db": bool(DATABASE_URL), "debug": DEBUG,
        "arabic_shaping": _ARABIC_SHAPING, "pdf": _REPORTLAB_AVAILABLE,
        "retrieval": "postgres_fts_only", "firebase": FIREBASE_ENABLED,
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
            INSERT INTO users (user_id, name, email, child_age, notes,
                               preferred_language, parent_name, child_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET
                name               = COALESCE(EXCLUDED.name,               users.name),
                email              = COALESCE(EXCLUDED.email,              users.email),
                child_age          = COALESCE(EXCLUDED.child_age,          users.child_age),
                preferred_language = COALESCE(EXCLUDED.preferred_language, users.preferred_language),
                parent_name        = COALESCE(EXCLUDED.parent_name,        users.parent_name),
                child_name         = COALESCE(EXCLUDED.child_name,         users.child_name),
                updated_at         = NOW()
            RETURNING user_id, name, email, child_age,
                      preferred_language, parent_name, child_name, created_at, updated_at
            """,
            (req.user_id, req.name, req.email, req.child_age,
             json.dumps([]), lang, req.parent_name, req.child_name),
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
            },
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
        if not row:
            raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        fcm_token: Optional[str] = row[0]; lang = row[1] or "ar"
        if not fcm_token:
            raise HTTPException(status_code=422, detail=t("no_fcm_token", lang))
        ensure_user_exists(conn, req.user_id)
        cur.execute("INSERT INTO daily_tips (user_id, tip) VALUES (%s,%s)",
                    (req.user_id, req.tip))
        conn.commit()
        if not FIREBASE_ENABLED:
            return {"ok": True, "tip_saved": True, "notification_sent": False,
                    "warning": t("firebase_not_configured", lang)}
        try:
            fb_messaging.send(fb_messaging.Message(
                notification=fb_messaging.Notification(
                    title=t("daily_tip_notif_title", lang), body=req.tip[:200]),
                token=fcm_token,
                data={"user_id": req.user_id, "type": "daily_tip"},
            ))
        except fb_messaging.UnregisteredError:
            cur.execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (req.user_id,))
            conn.commit()
            raise HTTPException(status_code=410, detail=t("fcm_token_expired", lang))
        except Exception as fb_exc:
            raise HTTPException(status_code=502, detail=f"Firebase error: {fb_exc}")
        log_event(conn, req.user_id, "daily_tip_sent", value=req.tip[:100])
        return {"ok": True, "tip_saved": True, "notification_sent": True}
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
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        cur.execute(
            "SELECT id, tip, created_at FROM daily_tips "
            "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, max(1, min(200, limit))))
        rows = cur.fetchall()
        return {"user_id": user_id, "total": len(rows),
                "tips": [{"id": r[0], "tip": r[1],
                           "created_at": r[2].isoformat() if r[2] else None} for r in rows]}
    except HTTPException: raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


# ══════════════════════════════════════════════
# ROUTES — KNOWLEDGE BASE
# ══════════════════════════════════════════════

@app.get("/kb/search", tags=["KB"])
def kb_search_api(q: str, category: Optional[str] = None, lang: Optional[str] = None, limit: int = 3):
    results = fts_knowledge_base(query=q, category=category, lang=lang, limit=limit)
    return {"query": q, "category": category, "count": len(results), "results": results}


@app.post("/kb/faq/add", tags=["KB"])
def faq_kb_add(req: FaqKbAddRequest):
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")
    new_id = fts_insert_qa(req.question, req.answer, req.category, req.lang)
    if new_id is None:
        return {"ok": False, "detail": "Duplicate or insert failed"}
    return {"ok": True, "id": new_id, "category": req.category, "lang": req.lang}


# ══════════════════════════════════════════════
# ROUTES — ASSESSMENT
# ══════════════════════════════════════════════

@app.get("/assessment/questions", tags=["Assessment"])
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return {
        "child_age": age, "total_questions": len(qs),
        "scale": {"min": 1, "max": 5,
                  "labels": {"1": "Never", "2": "Rarely", "3": "Sometimes", "4": "Often", "5": "Always"}},
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
            if row and row[0]:
                lang = row[0]
        profile     = compute_personality_profile(req.answers, req.child_age, req.behavior_signals)
        assess_conf = compute_assessment_confidence(req.answers, req.child_age, req.behavior_signals)
        profile_to_store = {k: v for k, v in profile.items() if k != "_debug"}
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO assessments (user_id, child_age, assessment_confidence, result, created_at) "
            "VALUES (%s,%s,%s,%s,NOW())",
            (req.user_id, req.child_age, assess_conf["confidence"], json.dumps(profile_to_store)),
        )
        conn.commit()
        update_memory(conn, req.user_id, "assessment_personality", req.child_age, note="Assessment submitted")
        log_event(conn, req.user_id, "assessment_submit",
                  value=f"confidence={assess_conf['confidence']}")
        return {
            "ok": True, "message": t("ok", lang),
            "trait_scores": profile["trait_scores"],
            "top_traits": profile["top_traits"],
            "low_traits": profile["low_traits"],
            "possible_personalities": profile["possible_personalities"],
            "recommendations": profile["recommendations"],
            "confidence": assess_conf["confidence"],
            "note": t("assessment_note", lang),
            "debug": profile.get("_debug", {}),
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
        "SELECT id, child_age, assessment_confidence, result, created_at "
        "FROM assessments WHERE user_id=%s ORDER BY created_at DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return {"assessments": [
        {"id": r[0], "child_age": r[1], "confidence": float(r[2]),
         "result": r[3], "created_at": r[4].isoformat() if r[4] else None}
        for r in rows
    ]}


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
        "SELECT event_id, event_type, value, created_at FROM analytics "
        "WHERE user_id=%s ORDER BY created_at DESC LIMIT 100",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return {"user_id": user_id, "recent_events": [
        {"event_id": r[0], "event_type": r[1], "value": r[2],
         "created_at": r[3].isoformat() if r[3] else None}
        for r in rows
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
            (req.user_id, req.message_id, req.rating, req.comment, req.topic),
        )
        conn.commit()
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
        "SELECT message_id, message, response, created_at FROM chat_messages "
        "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, max(1, min(200, limit))),
    )
    rows = cur.fetchall()
    conn.close()
    return {"messages": [
        {"message_id": r[0], "user_message": r[1], "bot_reply": r[2],
         "created_at": r[3].isoformat() if r[3] else None}
        for r in rows
    ]}


# ══════════════════════════════════════════════
# ROUTES — CHAT  (FTS → Gemini, auto KB growth)
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

    # ── Safety guards ─────────────────────────────────────────────────────────
    if hard_out_of_scope(user_message) or hard_medical(user_message):
        return ChatResponse(reply=t("out_of_scope_reply", lang))
    if detect_risk_level(user_message) == "high":
        try:
            conn = get_conn()
            ensure_user_exists(conn, req.user_id)
            log_event(conn, req.user_id, "risk_high", value=user_message[:200])
            conn.close()
        except Exception:
            pass
        return ChatResponse(reply=t("risk_high", lang))
    if kids_safety_guard(user_message):
        return ChatResponse(reply=t("kids_safety", lang))

    # ── Route ─────────────────────────────────────────────────────────────────
    topic = "general_parenting"
    age   = req.child_age
    try:
        decision = gemini_route_decision(user_message, req.messages, req.child_age)
        if not decision.in_scope or decision.action == "refuse_out_of_scope":
            return ChatResponse(reply=t("scope_refusal", lang))
        topic = decision.topic
        age   = decision.extracted_child_age or req.child_age
    except Exception as exc:
        print(f"[CHAT] Router error: {exc}")

    # ── FTS retrieval from KB ─────────────────────────────────────────────────
    kb_results = fts_knowledge_base(query=user_message, category="parenting", lang=lang, limit=3)
    if kb_results:
        context_block = "\n\n".join(
            f"[{i+1}] Q: {r.get('question','')}\n    A: {r.get('answer','')}"
            for i, r in enumerate(kb_results)
        )
    else:
        context_block = "No specific context found."

    prompt = f"""You are a professional parenting assistant called Rafiq.

Rules:
- Provide a direct, helpful answer in plain text.
- Do NOT use Markdown symbols (**, *, #).
- Respond in the same language as the User Question.
- Use Knowledge Base Context if relevant; otherwise use your general parenting knowledge.
- Never ask the user for more information.

Knowledge Base Context:
{context_block}

User Question:
{user_message}

Answer:"""

    try:
        response   = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=600),
        )
        reply_text = strip_markdown((response.text or "").strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

    if not reply_text:
        reply_text = ("عذرًا، لم أتمكن من توليد رد. حاول مرة أخرى."
                      if lang == "ar" else
                      "Sorry, I couldn't generate a response. Please try again.")

    # ── Auto-grow KB: insert this Q/A pair ───────────────────────────────────
    fts_insert_qa(
        question=user_message,
        answer=reply_text,
        category="parenting",
        lang=lang,
    )

    # ── Persist to DB ─────────────────────────────────────────────────────────
    message_id = "msg_" + uuid.uuid4().hex[:10]
    try:
        conn = get_conn()
        try:
            ensure_user_exists(conn, req.user_id)
            update_memory(conn, req.user_id, topic, age, note=user_message)
            log_event(conn, req.user_id, "chat_message", value=user_message[:300])
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO chat_messages (message_id, user_id, message, response) "
                "VALUES (%s,%s,%s,%s)",
                (message_id, req.user_id, user_message, reply_text),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as db_exc:
        print(f"[CHAT] DB persistence error (non-fatal): {db_exc}")

    return ChatResponse(reply=reply_text)


# ══════════════════════════════════════════════
# ROUTES — PARENTING PLAN  (v6.0)
# ══════════════════════════════════════════════

@app.post("/generate-parenting-plan/{user_id}", tags=["Parenting Plan"])
def generate_parenting_plan(user_id: str, req: Optional[GeneratePlanRequest] = None):
    _require_gemini()
    lang: Lang = "en"
    _plan_logger.info("[plan] Generate request — user=%s", user_id)

    conn = get_conn()
    try:
        ensure_user_exists(conn, user_id)
        cur = conn.cursor()

        # ── Fetch user info (only columns that exist in users table) ──────────
        cur.execute(
            "SELECT child_age, preferred_language "
            "FROM users WHERE user_id=%s",
            (user_id,),
        )
        user_row = cur.fetchone()

        child_age_from_user = user_row[0] if user_row else None
        lang = (user_row[1] or "en") if user_row else "en"
        if lang not in ("ar", "en"):
            lang = "en"

        # parent_name و child_name بييجوا من الـ request body بس
        parent_name = (req and req.parent_name) or "Parent"
        child_name  = (req and req.child_name)  or ""

        # ── Fetch latest assessment ────────────────────────────────────────────
        cur.execute(
            "SELECT id, child_age, assessment_confidence, result, created_at "
            "FROM assessments WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=t("no_assessment_found", lang))

        assessment_id, child_age_from_assessment, assessment_confidence, result_raw, assessed_at = row

        # استخدم child_age من assessment أولاً، وإلا من users
        child_age = child_age_from_assessment or child_age_from_user

        # ── Parse assessment result ────────────────────────────────────────────
        try:
            result: Dict[str, Any] = (
                json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to parse assessment: {exc}")

        top_traits             = _norm_traits(result.get("top_traits", []))
        possible_personalities = _norm_personalities(result.get("possible_personalities", []))
        trait_scores           = _norm_scores(result.get("trait_scores", {}))

        top_arch_entry  = possible_personalities[0] if possible_personalities else {}
        top_archetype   = top_arch_entry.get("name",        "Not specified")
        archetype_desc  = top_arch_entry.get("description", "")
        archetype_needs = top_arch_entry.get("needs",       "")

        # ── Generate intro letter ──────────────────────────────────────────────
        _plan_logger.info("[plan] Generating intro letter")
        try:
            intro_letter = gemini_generate_intro_letter(
                parent_name=parent_name, child_name=child_name,
                child_age=child_age, top_archetype=top_archetype,
                archetype_desc=archetype_desc, top_traits=top_traits, lang=lang,
            )
        except Exception as exc:
            _plan_logger.warning("[plan] Intro letter failed (non-fatal): %s", exc)
            intro_letter = (
                f"Dear {parent_name},\n\n"
                "Welcome to your personalised 15-day parenting plan. "
                "This plan has been carefully designed based on your child's unique personality.\n\n"
                "Warm regards,\nRafiq AI"
            )

        # ── Generate 15-day plan ───────────────────────────────────────────────
        _plan_logger.info("[plan] Generating 15-day plan JSON")
        plan_days, plan_text = gemini_generate_15day_plan(
            parent_name=parent_name, child_name=child_name,
            child_age=child_age, top_archetype=top_archetype,
            archetype_desc=archetype_desc, archetype_needs=archetype_needs,
            top_traits=top_traits, trait_scores=trait_scores, lang=lang,
        )

        # ── Final validation before DB insert ─────────────────────────────────
        if not plan_text or not plan_text.strip():
            raise HTTPException(status_code=502,
                                detail="Generated plan_text is empty — aborting DB insert.")

        _plan_logger.info("[plan] plan_text length=%d chars", len(plan_text))

        # ── Save plan to DB ────────────────────────────────────────────────────
        try:
            cur.execute(
                """
                INSERT INTO parenting_plans
                    (user_id, plan_text, plan_language, assessment_id,
                     parent_name, child_name, plan_days, intro_letter,
                     plan_duration, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                RETURNING id, created_at
                """,
                (user_id, plan_text, lang, assessment_id,
                 parent_name, child_name, json.dumps(plan_days),
                 intro_letter, 15),
            )
            plan_row        = cur.fetchone()
            conn.commit()
            plan_id         = plan_row[0]
            plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
            _plan_logger.info("[plan] Saved to DB ✔ — plan_id=%s", plan_id)
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
        )
        _plan_logger.info("[plan] FCM — sent=%s warning=%s",
                          notif_result["sent"], notif_result.get("warning"))

        response_payload: Dict[str, Any] = {
            "ok":                 True,
            "message":            t("plan_created_title", lang),
            "user_id":            user_id,
            "plan_id":            plan_id,
            "created_at":         plan_created_at,
            "plan_language":      lang,
            "plan_duration_days": 15,
            "child_age":          child_age,
            "parent_name":        parent_name,
            "child_name":         child_name,
            "top_archetype":      top_archetype,
            "assessment_id":      assessment_id,
            "notification_sent":  notif_result["sent"],
            "plan_days":          plan_days,
            "plan_text":          plan_text,
            "pdf_export_url":     f"/export-plan-pdf/{user_id}",
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

@app.get("/parenting-plans/{user_id}", tags=["Parenting Plan"])
def get_parenting_plans(user_id: str, limit: int = 10):
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
            (user_id, max(1, min(50, limit))),
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
# ROUTES — PDF EXPORT  (v6.0 — robust, no blank pages)
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
            LEFT   JOIN users       u ON u.user_id = pp.user_id
            LEFT   JOIN assessments a ON a.user_id = pp.user_id
            WHERE  pp.user_id = %s
            ORDER  BY pp.created_at DESC
            LIMIT  1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404,
                                detail=t("no_plan_found", PDF_LANG))

        plan_id, plan_text, plan_days_raw, intro_letter, \
            parent_name, child_name, created_at, child_age, result_raw = row

        # ── DEBUG: print fetched plan_text ────────────────────────────────────
        print("=" * 60)
        print(f"[PDF] Fetched plan for user={user_id} plan_id={plan_id}")
        print(f"[PDF] plan_text length : {len(plan_text) if plan_text else 0} chars")
        print(f"[PDF] plan_days_raw    : {'present' if plan_days_raw else 'NULL'}")
        print(f"[PDF] intro_letter len : {len(intro_letter) if intro_letter else 0} chars")
        print("=" * 60)

        # ── Validate plan_text ────────────────────────────────────────────────
        if not plan_text or not plan_text.strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    f"plan_text is empty for plan_id={plan_id}. "
                    "Re-generate the plan via POST /generate-parenting-plan/{user_id}."
                ),
            )

        # ── Resolve plan_days: prefer JSONB, fall back to parsing plan_text ───
        plan_days: List[Dict] = []
        if plan_days_raw:
            try:
                parsed = (
                    json.loads(plan_days_raw)
                    if isinstance(plan_days_raw, str)
                    else plan_days_raw
                )
                if isinstance(parsed, list) and len(parsed) > 0:
                    plan_days = parsed
            except Exception as parse_exc:
                print(f"[PDF] Warning: plan_days JSON parse failed ({parse_exc}), "
                      "falling back to plan_text parsing.")

        if not plan_days:
            plan_days = _parse_plan_days_from_text(plan_text)
            print(f"[PDF] Parsed {len(plan_days)} days from plan_text (text fallback)")

        if not plan_days:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Could not extract day-by-day structure for plan_id={plan_id}. "
                    "plan_text may be malformed. Re-generate via POST /generate-parenting-plan/{user_id}."
                ),
            )

        print(f"[PDF] Using {len(plan_days)} day cards for PDF")

        # ── Resolve archetype ─────────────────────────────────────────────────
        top_archetype = "Not specified"
        if result_raw:
            try:
                result_obj    = (
                    json.loads(result_raw) if isinstance(result_raw, str) else result_raw
                )
                personalities = _norm_personalities(result_obj.get("possible_personalities", []))
                if personalities:
                    arch_id  = personalities[0].get("id", "")
                    arch_obj = next((a for a in ARCHETYPES if a["id"] == arch_id), None)
                    top_archetype = (arch_obj["name"] if arch_obj
                                     else personalities[0].get("name", "Not specified"))
            except Exception:
                pass

        # ── Build PDF ─────────────────────────────────────────────────────────
        try:
            pdf_bytes = _build_parenting_plan_pdf(
                user_id=user_id,
                parent_name=parent_name or "Parent",
                child_name=child_name or "",
                child_age=child_age,
                top_archetype=top_archetype,
                intro_letter=intro_letter or "",
                plan_days=plan_days,
                generated_at=created_at.isoformat() if created_at else "",
                lang=PDF_LANG,
            )
        except Exception as pdf_exc:
            raise HTTPException(status_code=500, detail=f"PDF generation failed: {pdf_exc}")

        _plan_logger.info("[pdf] Exported — user=%s plan_id=%s bytes=%d",
                          user_id, plan_id, len(pdf_bytes))

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
        try:
            conn.close()
        except Exception:
            pass
