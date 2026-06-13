"""
Rafiq Bot API — PRODUCTION v3
==============================
Changes in v3:
- User registration endpoint POST /users (upsert with name + email)
- Memory returns name + email
- Full English assessment (20 questions, 8 traits, archetypes, recommendations)
- AI specialist recommendation after assessment
- ensure_user_exists() guards all FK writes
- Language detection drives ALL static reply strings (ar / en)
- Refactored helpers, removed duplicate logic
"""

from dotenv import load_dotenv
load_dotenv()

import os, json, uuid, re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
import psycopg2
import io

# reportlab — PDF generation
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    print("WARNING: reportlab not installed — PDF export disabled. Run: pip install reportlab")

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None          # type: ignore
    genai_types = None    # type: ignore

try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials, messaging as fb_messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    firebase_admin = None  # type: ignore
    fb_credentials = None  # type: ignore
    fb_messaging   = None  # type: ignore
    _FIREBASE_AVAILABLE = False

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DEBUG         = os.getenv("RAFIQ_DEBUG", "0") == "1"
DATABASE_URL  = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY= os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL  = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENABLED= bool(GEMINI_API_KEY) and (genai is not None)
ADMIN_KEY     = os.getenv("RAFIQ_ADMIN_KEY", "change-me")
ENABLE_VERIFY = os.getenv("RAFIQ_VERIFY_OUTPUT", "0") == "1"

if ADMIN_KEY == "change-me":
    print("WARNING: RAFIQ_ADMIN_KEY is default.")

client = None
if GEMINI_ENABLED:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini initialized ✔")
    except Exception as exc:
        print("Gemini init failed:", exc)
else:
    print("Gemini disabled")

# ── Firebase Admin SDK init ────────────────────
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
else:
    if not _FIREBASE_AVAILABLE:
        print("firebase-admin not installed — FCM disabled")
    else:
        print("FIREBASE_CREDENTIALS not set — FCM disabled")

app = FastAPI(
    title="Rafiq Bot API",
    version="3.0.0",
    description="Family support & parenting assistant API"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    _run_schema_migrations()

# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def _run_schema_migrations() -> None:
    """Apply schema additions that may not exist yet (idempotent)."""
    if not DATABASE_URL:
        print("Skipping DB migrations — DATABASE_URL not set")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur  = conn.cursor()
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token TEXT;")
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
        conn.commit()
        conn.close()
        print("DB migrations applied ✔")
    except Exception as exc:
        print(f"DB migration warning: {exc}")

# ──────────────────────────────────────────────
# KNOWLEDGE BASE
# ──────────────────────────────────────────────
KB: List[Dict[str, Any]] = [
    {
        "id": "kb_001", "topic": "teen_communication",
        "age_min": 12, "age_max": 18,
        "tags": ["مراهق", "مراهقة", "مش بيرد", "ساكت", "قافل"],
        "tip": "ابدئي في وقت هدوء بجملة: «أنا مهتمة أفهمك مش ألومك». اسألي سؤال واحد مفتوح وسيبي مساحة للرد."
    },
    {
        "id": "kb_002", "topic": "anger",
        "age_min": 6, "age_max": 18,
        "tags": ["عصبية", "غضب", "صراخ", "بيزعق"],
        "tip": "وقت الغضب قللي الكلام وثبتي حدود هادية. بعد ما يهدى: «إيه اللي ضايقك؟ وإيه الحل المرة الجاية؟»."
    },
    {
        "id": "kb_003", "topic": "screen_addiction",
        "age_min": 8, "age_max": 18,
        "tags": ["موبايل", "شاشات", "تيك توك", "إدمان"],
        "tip": "اعملي اتفاق مكتوب: وقت شاشة + وقت عيلة. قلّلي تدريجيًا (15 دقيقة) مع بديل ممتع مش عقاب."
    },
    {
        "id": "kb_004", "topic": "bullying",
        "age_min": 6, "age_max": 18,
        "tags": ["تنمر", "مدرسة", "سخرية", "بيضرب"],
        "tip": "صدّقي مشاعره، خدي تفاصيل بسيطة، تواصلي مع المدرسة، ودرّبيه على ردود قصيرة وطلب المساعدة."
    },
    {
        "id": "kb_005", "topic": "study_focus",
        "age_min": 8, "age_max": 18,
        "tags": ["مذاكرة", "تركيز", "تسويف", "واجب"],
        "tip": "قسّمي المذاكرة لبلوكات 25 دقيقة + 5 راحة. خلي البداية سهلة (أول 5 دقائق) لتكسير حاجز البدء."
    },
    {
        "id": "kb_100", "topic": "kids_stories",
        "age_min": 4, "age_max": 10,
        "tags": ["قصة", "قصص", "حكاية", "قبل النوم", "احكي"],
        "tip": (
            "قصة قصيرة (5 دقايق) — «نجمة والمشاركة»\n"
            "نجمة عندها لعبة جديدة، وكل ما أصحابها ييجوا تلعب لوحدها. "
            "في يوم، صحابها زعلوا ومشيوا. نجمة حسّت بالوحدة.\n"
            "ماما قالت: «المشاركة مش بتقلل لعبتك… بتكبر فرحتك».\n"
            "نجمة جرّبت تدي كل واحد دوره دقيقة، ولعبوا وضحكوا.\n"
            "الدرس: المشاركة + الدور.\nسؤال للطفل: إنت كنت هتعمل إيه لو كنت مكان نجمة؟"
        )
    },
    {
        "id": "kb_101", "topic": "activities_games",
        "age_min": 4, "age_max": 12,
        "tags": ["لعبة", "نشاط", "ملل", "بيت", "وقت فراغ"],
        "tip": (
            "لعبة 10 دقايق: «صيد المشاعر»\n"
            "الأدوات: ورق + قلم.\n"
            "اكتبوا 6 مشاعر، اسحبوا ورقة، الطفل يمثل موقف للمشاعر دي.\n"
            "وبعدها: «إيه اللي يساعدني لما أحس كده؟»\n"
            "الهدف: التعبير عن المشاعر + التهدئة."
        )
    },
    {
        "id": "kb_102", "topic": "book_recommendations",
        "age_min": 4, "age_max": 12,
        "tags": ["كتاب", "كتب", "قراءة", "اقترح كتب"],
        "tip": (
            "اقتراح كتب حسب السن:\n"
            "- سن 4–7: كتب مصوّرة عن الصداقة/المشاركة/الصدق.\n"
            "- سن 8–12: مغامرات + قيم (مسؤولية/شجاعة/تعاون).\n"
            "بعد القراءة اسألي: «إيه أكتر موقف عجبك؟ وإيه الدرس؟»"
        )
    },
    {
        "id": "kb_103", "topic": "assessment_personality",
        "age_min": 4, "age_max": 18,
        "tags": ["تقييم", "assessment", "شخصية", "قيادي", "اجتماعي"],
        "tip": (
            "We can run a personality assessment to help you understand your child better. "
            "Call GET /assessment/questions?age=X then POST /assessment/submit with the answers."
        )
    },
]

SPECIALISTS: List[Dict[str, Any]] = [
    {
        "id": "sp_001", "name": "Dr. Mariam Ali",
        "title": "Family Counselor",
        "topics": ["teen_communication", "anger", "general_parenting"],
        "traits_focus": ["self_control", "empathy", "sociability"],
        "price_egp": 350, "rating": 4.8
    },
    {
        "id": "sp_002", "name": "Dr. Ahmed Hassan",
        "title": "Child Psychologist",
        "topics": ["bullying", "study_focus", "sensitivity"],
        "traits_focus": ["focus", "sensitivity", "adaptability"],
        "price_egp": 400, "rating": 4.6
    },
    {
        "id": "sp_003", "name": "Ms. Sara Mahmoud",
        "title": "Behavior Modification Specialist",
        "topics": ["screen_addiction", "anger", "self_control"],
        "traits_focus": ["self_control", "adaptability", "focus"],
        "price_egp": 300, "rating": 4.7
    },
    {
        "id": "sp_004", "name": "Dr. Layla Mostafa",
        "title": "Child Development Specialist",
        "topics": ["kids_stories", "activities_games", "assessment_personality"],
        "traits_focus": ["curiosity", "sociability", "leadership"],
        "price_egp": 380, "rating": 4.9
    },
]

SLOTS: List[Dict[str, Any]] = [
    {"slot_id": "sl_001", "specialist_id": "sp_001", "start": "2026-07-10T18:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_002", "specialist_id": "sp_001", "start": "2026-07-11T20:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_003", "specialist_id": "sp_002", "start": "2026-07-10T19:00:00+02:00", "duration_min": 45, "available": True},
    {"slot_id": "sl_004", "specialist_id": "sp_003", "start": "2026-07-12T21:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_005", "specialist_id": "sp_004", "start": "2026-07-13T17:00:00+02:00", "duration_min": 45, "available": True},
]

# ──────────────────────────────────────────────
# PYDANTIC MODELS
# ──────────────────────────────────────────────
ANSWER_OPTIONS = ["Never", "Rarely", "Sometimes", "Often", "Always"]

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    user_id: str
    messages: List[ChatMessage]
    child_age: Optional[int] = None

class ChatResponse(BaseModel):
    message_id: str
    reply: str
    cards: List[Dict[str, Any]] = []

class UserUpsertReq(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    child_age: Optional[int] = None

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
        "request_booking", "complete_booking",
        "behavior_event", "view_assessment", "assessment_submit"
    ]
    meta: Dict[str, Any] = {}

class BookingReq(BaseModel):
    user_id: str
    specialist_id: str
    slot_id: str

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

class RegisterTokenReq(BaseModel):
    user_id: str
    fcm_token: str

class SendDailyTipReq(BaseModel):
    user_id: str
    tip: str

# Router model for Gemini structured output
AllowedTopic = Literal[
    "teen_communication", "anger", "screen_addiction", "bullying", "study_focus",
    "siblings_jealousy", "parents_conflict", "lying", "general_parenting",
    "kids_stories", "activities_games", "book_recommendations",
    "assessment_personality", "out_of_scope"
]
AllowedAction = Literal[
    "answer_with_tips", "recommend_booking", "book_appointment", "refuse_out_of_scope"
]

class RouteDecision(BaseModel):
    in_scope: bool       = Field(description="Is question within Rafiq scope?")
    topic: AllowedTopic  = Field(description="Detected topic")
    action: AllowedAction= Field(description="Action to take")
    extracted_child_age: Optional[int] = Field(default=None)
    reason: str          = Field(description="Short reason")
    slot_id: Optional[str] = None
    specialist_id: Optional[str] = None

# ──────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────
PARENTING_TOPICS = {
    "teen_communication", "anger", "screen_addiction", "bullying", "study_focus",
    "siblings_jealousy", "parents_conflict", "lying", "general_parenting",
}
KIDS_CONTENT_TOPICS = {"kids_stories", "activities_games", "book_recommendations"}
ASSESSMENT_TOPIC    = "assessment_personality"
ALL_TRAITS          = ["leadership", "sociability", "empathy", "self_control",
                       "focus", "curiosity", "adaptability", "sensitivity"]

OUT_OF_SCOPE_KW = [
    "برمجة", "كود", "flutter", "android", "python", "java", "c++",
    "backend", "front", "database", "debug", "algorithm"
]
MEDICAL_KW = [
    "جرعة", "دواء", "حبوب", "مضاد", "تشخيص", "روشتة", "وصفة", "medication", "diagnosis"
]
KIDS_UNSAFE_KW  = ["انتحار", "إباحية", "اباحية", "سلاح", "مخدرات"]
RISK_HIGH_KW    = [
    "عايز أموت", "مش عايز أعيش", "هأذي نفسي", "انتحار", "هنتحر",
    "هقتل", "هموت", "أذي نفسي"
]
RISK_MEDIUM_KW  = [
    "خوف شديد", "هلع", "نوبات", "قلق جامد", "اكتئاب",
    "حزين طول الوقت", "مش قادر", "مخنوق طول الوقت"
]

# ──────────────────────────────────────────────
# BILINGUAL STRINGS
# ──────────────────────────────────────────────
_STR: Dict[str, Dict[str, str]] = {
    "out_of_scope_reply": {
        "ar": "أنا بوت (رفيق) متخصص في دعم الأسرة. مش بقدر أساعد في برمجة/أدوية/تشخيص.",
        "en": "I'm Rafiq, a family support assistant. I can't help with programming, medication, or diagnosis.",
    },
    "out_of_scope_card": {
        "ar": "اسأل عن: مراهقة، عصبية، موبايل، تنمر، مذاكرة، قصص أطفال، ألعاب، تقييم شخصية.",
        "en": "Ask about: teen communication, anger, screen time, bullying, studying, kids stories, games, personality assessment.",
    },
    "gemini_disabled": {
        "ar": "ميزة الشات غير مفعّلة. التقييم والـ Memory شغالين ✅",
        "en": "Chat is currently disabled. Assessment and Memory are working ✅",
    },
    "risk_high": {
        "ar": "أنا قلقان عليك جدًا. تواصل فورًا مع شخص كبير موثوق قريب منك أو خدمات الطوارئ.",
        "en": "I'm very concerned. Please immediately reach out to a trusted adult or call emergency services.",
    },
    "risk_high_card": {
        "ar": "في الحالات العاجلة لازم تدخل مختص فورًا. رفيق للدعم العام فقط.",
        "en": "In urgent cases a specialist must intervene immediately. Rafiq is for general support only.",
    },
    "scope_refusal": {
        "ar": "سؤالك خارج نطاق رفيق. اسأل عن مشكلة أسرية/تربوية وأنا أساعدك فورًا ✅",
        "en": "Your question is outside Rafiq's scope. Ask about a parenting or family issue and I'll help right away ✅",
    },
    "kids_safety": {
        "ar": "خلّينا نخلي المحتوى مناسب للأطفال 🙏 قوليلي سن الطفل والموضوع (صدق/مشاركة/شجاعة).",
        "en": "Let's keep content child-appropriate 🙏 Tell me the child's age and topic (honesty/sharing/courage).",
    },
    "missing_slot": {
        "ar": "ابعت رقم الموعد: احجز sl_001",
        "en": "Please send the slot number: book sl_001",
    },
    "slot_unavailable": {
        "ar": "الموعد مش متاح. اختر ميعاد تاني.",
        "en": "This slot is no longer available. Please choose another.",
    },
    "low_conf_prefix": {
        "ar": "الموضوع محتاج تفاصيل أكتر. ",
        "en": "I need a bit more context to help effectively. ",
    },
    "low_conf_suffix": {
        "ar": " ولو تقدر احكيلي موقف حصل قريب.",
        "en": " If you can, share a recent situation that happened.",
    },
    "verify_fallback": {
        "ar": "أنا معاك ✅ بس خلّيني أسألك: ",
        "en": "I'm here for you ✅ Let me ask: ",
    },
    "booking_success": {
        "ar": "تم الحجز ✅ رقم الحجز: ",
        "en": "Booking confirmed ✅ Booking ID: ",
    },
    "assessment_result_title": {
        "ar": "نتيجة تقييم شخصية الطفل",
        "en": "Child Personality Assessment Result",
    },
    "assessment_note": {
        "ar": "النتيجة إرشادية وليست تشخيصًا.",
        "en": "This result is indicative, not a clinical diagnosis.",
    },
}

def tr(key: str, lang: str) -> str:
    return _STR.get(key, {}).get(lang) or _STR.get(key, {}).get("ar", "")

# ──────────────────────────────────────────────
# GUARDS & UTILS
# ──────────────────────────────────────────────
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

def extract_slot_id(text: str) -> Optional[str]:
    m = re.search(r"\bsl_\d{3}\b", text.lower())
    return m.group(0) if m else None

def detect_lang(text: str) -> Literal["ar", "en"]:
    ar = len(re.findall(r'[\u0600-\u06FF]', text))
    en = len(re.findall(r'[a-zA-Z]', text))
    return "ar" if ar >= en else "en"

# ──────────────────────────────────────────────
# KB SEARCH v2
# ──────────────────────────────────────────────
_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")
_AR_PUNCT      = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)
_AR_STOPWORDS  = {"في","من","على","عن","الى","إلى","هو","هي","ده","دي","دا","انا","انت","انتي","احنا","هم"}

def _ar_normalize(text: str) -> str:
    if not text: return ""
    t = _AR_DIACRITICS.sub("", text.strip())
    for a, b in [("أ","ا"),("إ","ا"),("آ","ا"),("ى","ي"),("ة","ه"),("ؤ","و"),("ئ","ي"),("ـ","")]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", _AR_PUNCT.sub(" ", t.lower())).strip()

def _tokenize(text: str) -> List[str]:
    return [w for w in _ar_normalize(text).split() if len(w) >= 2 and w not in _AR_STOPWORDS]

def _score_kb_item(q_tokens: List[str], item: Dict[str, Any]) -> int:
    if not q_tokens: return 1
    tags = _ar_normalize(" ".join(item.get("tags", [])))
    tip  = _ar_normalize(item.get("tip", ""))
    both = tags + " " + tip
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
        elif not tokens:      scored.append((s, item))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        top     = [i for _, i in scored[:3]]
        matched = scored[0][0] >= 6 if tokens else True
        return KbSearchResult(tips=top, matched=matched, match_count=len(scored), used_default=not bool(tokens))
    defaults = [x for x in KB if x["topic"] == topic][:3]
    return KbSearchResult(tips=defaults, matched=False, match_count=0, used_default=True)

# ──────────────────────────────────────────────
# SPECIALISTS & SLOTS
# ──────────────────────────────────────────────
def recommend_specialists(topic: str) -> List[Dict[str, Any]]:
    rec = sorted(
        [s for s in SPECIALISTS if topic in s["topics"]],
        key=lambda x: (-x["rating"], x["price_egp"])
    )
    return rec[:3] or SPECIALISTS[:2]

def available_slots(specialist_id: str) -> List[Dict[str, Any]]:
    return [sl for sl in SLOTS if sl["specialist_id"] == specialist_id and sl["available"]][:3]

def sync_slots_with_booked(conn) -> None:
    cur = conn.cursor()
    cur.execute("SELECT slot_id FROM appointments WHERE status != 'cancelled'")
    booked = {r[0] for r in cur.fetchall()}
    for sl in SLOTS:
        if sl["slot_id"] in booked:
            sl["available"] = False

def book_slot(conn, user_id: str, specialist_id: str, slot_id: str) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM appointments WHERE slot_id=%s AND status != 'cancelled'",
        (slot_id,)
    )
    if cur.fetchone()[0] > 0:
        raise ValueError("Slot not available")
    slot = next((s for s in SLOTS if s["slot_id"] == slot_id and s["specialist_id"] == specialist_id), None)
    if not slot or not slot["available"]:
        raise ValueError("Slot not available")
    appt_id = "ap_" + uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO appointments (appointment_id, user_id, specialist_id, slot_id, status) VALUES (%s,%s,%s,%s,'pending')",
        (appt_id, user_id, specialist_id, slot_id)
    )
    conn.commit()
    slot["available"] = False
    return {"appointment_id": appt_id, "user_id": user_id,
            "specialist_id": specialist_id, "slot_id": slot_id,
            "status": "pending", "created_at": datetime.utcnow().isoformat() + "Z"}

# ──────────────────────────────────────────────
# USER / MEMORY
# ──────────────────────────────────────────────
def ensure_user_exists(conn, user_id: str) -> None:
    """Upsert bare user row — prevents FK violations on all child tables."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, notes)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id, json.dumps([]))
    )
    conn.commit()

def get_memory(conn, user_id: str) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        "SELECT notes, child_age, name, email FROM users WHERE user_id=%s",
        (user_id,)
    )
    row = cur.fetchone()
    if not row:
        return {"child_age": None, "name": None, "email": None, "notes": [], "last_summary": ""}
    raw = row[0]
    notes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return {"child_age": row[1], "name": row[2], "email": row[3], "notes": notes, "last_summary": ""}

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

# ──────────────────────────────────────────────
# ANALYTICS
# ──────────────────────────────────────────────
def log_event(conn, user_id: str, event_type: str, value: str = "") -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analytics (event_id, user_id, event_type, value) VALUES (%s,%s,%s,%s)",
        ("ev_" + uuid.uuid4().hex[:10], user_id, event_type, value[:300])
    )
    conn.commit()

# ──────────────────────────────────────────────
# ASSESSMENT ENGINE  (full English)
# ──────────────────────────────────────────────
ASSESSMENT_OPTIONS = ["Never", "Rarely", "Sometimes", "Often", "Always"]

ASSESSMENT_QUESTIONS: List[Dict[str, Any]] = [
    # FOCUS
    {"id": "q01", "trait": "focus",        "age_min": 4,  "age_max": 18, "weights": {"focus": 2},
     "text": "My child stays focused on a task until it is completed."},
    {"id": "q02", "trait": "focus",        "age_min": 7,  "age_max": 18, "weights": {"focus": 2, "self_control": 1},
     "text": "My child finishes homework or assignments before switching to play."},
    {"id": "q03", "trait": "focus",        "age_min": 4,  "age_max": 18, "weights": {"focus": 3},
     "text": "My child can sit quietly and concentrate during story time or a lesson."},
    # EMPATHY
    {"id": "q04", "trait": "empathy",      "age_min": 4,  "age_max": 18, "weights": {"empathy": 2},
     "text": "My child notices when a friend or sibling is upset and tries to comfort them."},
    {"id": "q05", "trait": "empathy",      "age_min": 6,  "age_max": 18, "weights": {"empathy": 2, "sociability": 1},
     "text": "My child apologizes genuinely after hurting someone's feelings."},
    {"id": "q06", "trait": "empathy",      "age_min": 4,  "age_max": 18, "weights": {"empathy": 3},
     "text": "My child shows concern for animals or people who are struggling."},
    # CURIOSITY
    {"id": "q07", "trait": "curiosity",    "age_min": 4,  "age_max": 18, "weights": {"curiosity": 2},
     "text": "My child frequently asks 'why' or 'how' questions about the world."},
    {"id": "q08", "trait": "curiosity",    "age_min": 6,  "age_max": 18, "weights": {"curiosity": 2, "adaptability": 1},
     "text": "My child enjoys trying new activities or experimenting with new ideas."},
    {"id": "q09", "trait": "curiosity",    "age_min": 4,  "age_max": 18, "weights": {"curiosity": 3},
     "text": "My child enjoys solving puzzles, riddles, or figuring things out independently."},
    # LEADERSHIP
    {"id": "q10", "trait": "leadership",   "age_min": 5,  "age_max": 18, "weights": {"leadership": 2},
     "text": "My child naturally takes charge and organizes activities when playing with others."},
    {"id": "q11", "trait": "leadership",   "age_min": 8,  "age_max": 18, "weights": {"leadership": 2, "focus": 1},
     "text": "My child steps up to help make decisions in group settings."},
    {"id": "q12", "trait": "leadership",   "age_min": 5,  "age_max": 18, "weights": {"leadership": 3},
     "text": "My child is comfortable taking responsibility for a task or group project."},
    # SOCIABILITY
    {"id": "q13", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 2},
     "text": "My child makes friends quickly and easily in new environments."},
    {"id": "q14", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 2, "empathy": 1},
     "text": "My child enjoys being around others and actively seeks social interaction."},
    {"id": "q15", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 3},
     "text": "My child is comfortable sharing, taking turns, and cooperating in group play."},
    # ADAPTABILITY
    {"id": "q16", "trait": "adaptability", "age_min": 4,  "age_max": 18, "weights": {"adaptability": 2},
     "text": "My child adjusts well to changes in routine (new school, travel, schedule changes)."},
    {"id": "q17", "trait": "adaptability", "age_min": 6,  "age_max": 18, "weights": {"adaptability": 2, "self_control": 1},
     "text": "When plans change unexpectedly, my child handles it calmly."},
    # SELF CONTROL
    {"id": "q18", "trait": "self_control", "age_min": 4,  "age_max": 18, "weights": {"self_control": 2},
     "text": "My child can calm themselves down after getting upset without adult intervention."},
    {"id": "q19", "trait": "self_control", "age_min": 6,  "age_max": 18, "weights": {"self_control": 3},
     "text": "My child resists the urge to act impulsively (e.g., waits their turn, thinks before acting)."},
    # SENSITIVITY
    {"id": "q20", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 2},
     "text": "My child gets upset easily by criticism, loud noises, or unexpected changes."},
    {"id": "q21", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 3},
     "text": "My child feels emotions deeply and needs extra reassurance after conflict or disappointment."},
]

ARCHETYPES: List[Dict[str, Any]] = [
    {
        "id": "leader",
        "name": "The Leader",
        "description": "Takes initiative, organizes peers, and thrives when given responsibility.",
        "needs": "Clear boundaries, meaningful responsibilities, and leadership opportunities.",
        "profile": {"leadership": 80, "focus": 60, "sociability": 55},
        "traits_focus": ["leadership", "focus"],
    },
    {
        "id": "explorer",
        "name": "The Explorer",
        "description": "Curious, adventurous, and constantly seeking new experiences and knowledge.",
        "needs": "New challenges, hands-on projects, and freedom to experiment.",
        "profile": {"curiosity": 80, "adaptability": 65},
        "traits_focus": ["curiosity", "adaptability"],
    },
    {
        "id": "thinker",
        "name": "The Thinker",
        "description": "Reflective and analytical — prefers depth over breadth and enjoys solving complex problems.",
        "needs": "Quiet time, intellectual challenges, and space for independent thought.",
        "profile": {"focus": 80, "curiosity": 65, "sociability": 30},
        "traits_focus": ["focus", "curiosity"],
    },
    {
        "id": "helper",
        "name": "The Helper",
        "description": "Warm, caring, and highly attuned to the emotions of others.",
        "needs": "Recognition of emotional contributions and opportunities to support peers.",
        "profile": {"empathy": 85, "sociability": 60},
        "traits_focus": ["empathy", "sociability"],
    },
    {
        "id": "peacemaker",
        "name": "The Peacemaker",
        "description": "Conflict-averse, diplomatic, and focused on harmony in relationships.",
        "needs": "Teaching assertiveness, safe expression of opinions, and conflict resolution skills.",
        "profile": {"empathy": 75, "self_control": 70},
        "traits_focus": ["empathy", "self_control"],
    },
    {
        "id": "energetic",
        "name": "The Energetic",
        "description": "High energy, enthusiastic, and socially motivated — brings excitement to every group.",
        "needs": "Physical outlets, structured energy release, and consistent boundaries.",
        "profile": {"sociability": 75, "curiosity": 60, "self_control": 35},
        "traits_focus": ["sociability", "self_control"],
    },
    {
        "id": "sensitive",
        "name": "The Sensitive",
        "description": "Deeply empathetic and emotionally aware — feels things intensely.",
        "needs": "Emotional validation, predictable routines, and a calm safe environment.",
        "profile": {"sensitivity": 85, "empathy": 65},
        "traits_focus": ["sensitivity", "empathy"],
    },
    {
        "id": "independent",
        "name": "The Independent",
        "description": "Values autonomy and personal space — prefers doing things on their own terms.",
        "needs": "Structured choices, respected boundaries, and gradual responsibility.",
        "profile": {"leadership": 55, "sociability": 25, "focus": 60},
        "traits_focus": ["leadership", "focus"],
    },
    {
        "id": "planner",
        "name": "The Planner",
        "description": "Orderly, methodical, and motivated by structure, routine, and clear goals.",
        "needs": "Simple schedules, clear expectations, and positive reinforcement for progress.",
        "profile": {"focus": 85, "self_control": 75},
        "traits_focus": ["focus", "self_control"],
    },
    {
        "id": "challenger",
        "name": "The Challenger",
        "description": "Questions authority, tests limits, and learns best through debate and negotiation.",
        "needs": "Few but firm rules, negotiation space, and consistent logical consequences.",
        "profile": {"leadership": 65, "self_control": 30, "sensitivity": 50},
        "traits_focus": ["leadership", "self_control"],
    },
]

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
    qs = {q["id"]: q for q in get_assessment_questions(child_age)}
    raw: Dict[str, float] = {t: 0.0 for t in ALL_TRAITS}
    max_: Dict[str, float] = {t: 0.0 for t in ALL_TRAITS}

    for a in answers:
        qid = a.get("question_id") or a.get("id")
        val = a.get("value")
        if qid not in qs or val is None:
            continue
        try:
            v = max(1, min(5, int(val)))
        except (TypeError, ValueError):
            continue
        for trait, w in qs[qid]["weights"].items():
            raw[trait]  += v * w
            max_[trait] += 5 * w

    # Behavior signal bonuses
    bs = behavior_signals or {}
    raw["focus"]    += max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2;  max_["focus"]    += 6
    raw["empathy"]  += int(bs.get("helps_others", 0)) * 2;               max_["empathy"]  += 4

    def _norm(r: float, m: float) -> int:
        return max(0, min(100, int(round(r / m * 100)))) if m > 0 else 0

    scores = {t: _norm(raw[t], max_[t]) for t in ALL_TRAITS}

    # Archetype matching
    def _sim(arch_profile: Dict[str, int]) -> float:
        return sum(100 - abs(scores.get(t, 50) - v) for t, v in arch_profile.items()) / max(1, len(arch_profile))

    ranked = sorted(
        [{"id": a["id"], "name": a["name"], "description": a["description"],
          "needs": a["needs"], "match_pct": int(round(_sim(a["profile"])))}
         for a in ARCHETYPES],
        key=lambda x: x["match_pct"], reverse=True
    )

    top_archetype = ranked[0]
    top_traits = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    low_traits  = sorted(scores.items(), key=lambda kv: kv[1])[:2]

    # Personalized recommendations
    recommendations = _build_recommendations(scores, top_archetype, low_traits)

    return {
        "child_age": child_age,
        "trait_scores": scores,
        "top_traits": [{"trait": t, "score": v} for t, v in top_traits],
        "low_traits":  [{"trait": t, "score": v} for t, v in low_traits],
        "possible_personalities": ranked[:5],
        "recommendations": recommendations,
        "note": tr("assessment_note", "en"),
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
                "focus":        "Try the Pomodoro method: 20 min focused work + 5 min break. Start with easy tasks to build momentum.",
                "empathy":      "Use emotion cards or role-play scenarios. Ask 'How do you think they felt?' after stories.",
                "curiosity":    "Introduce science kits, mystery books, or nature walks. Reward questions, not just answers.",
                "leadership":   "Give small responsibilities (organize a game, plan a family activity). Praise initiative.",
                "sociability":  "Arrange structured playdates. Teach conversation starters and taking turns.",
                "adaptability": "Warn about changes in advance. Use visual schedules and transition rituals.",
                "self_control": "Practice 'stop and breathe' in calm moments. Use a feelings chart to name emotions.",
                "sensitivity":  "Create a calm-down corner. Validate feelings before problem-solving.",
            }.get(trait, "Provide consistent support and positive reinforcement.")
            recs.append(f"Low {trait.replace('_',' ').title()} ({score}%): {advice}")
    return recs

def compute_assessment_confidence(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    qs    = get_assessment_questions(child_age)
    q_ids = {q["id"] for q in qs}
    total = len(qs)
    valid = 0
    for a in answers or []:
        qid = a.get("question_id") or a.get("id")
        try:
            v = int(a.get("value"))
        except Exception:
            continue
        if qid in q_ids and 1 <= v <= 5:
            valid += 1

    coverage = int(round(valid / total * 100)) if total else 0
    score    = int(round(valid / total * 65))  if total else 0
    notes    = [f"coverage={coverage}%"]
    if child_age is not None: score += 15; notes.append("age_provided")
    if behavior_signals:       score += 10; notes.append("behavior_signals_included")
    if valid < max(3, total // 3 if total else 3):
        score = max(0, score - 15); notes.append("low_answer_count_penalty")
    return {"confidence": max(0, min(100, score)), "valid_answers": valid,
            "total_questions": total, "notes": notes}

def recommend_specialist_for_profile(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Match specialist based on child's low traits + top archetype needs."""
    low_traits  = [item["trait"] for item in profile.get("low_traits", [])]
    top_arch_id = profile["possible_personalities"][0]["id"] if profile.get("possible_personalities") else ""

    archetype = next((a for a in ARCHETYPES if a["id"] == top_arch_id), None)
    focus_traits = archetype["traits_focus"] if archetype else low_traits

    best_sp, best_score = None, -1
    for sp in SPECIALISTS:
        score = sum(1 for t in focus_traits if t in sp["traits_focus"])
        score += sum(1 for t in low_traits   if t in sp["traits_focus"])
        if score > best_score:
            best_score, best_sp = score, sp

    if not best_sp:
        return None

    reasons = []
    if low_traits:
        reasons.append(f"Low {', '.join(low_traits)} detected in your child's profile.")
    reasons.append(f"{best_sp['name']} specializes in {', '.join(best_sp['traits_focus'])} development.")
    return {
        "id":     best_sp["id"],
        "name":   best_sp["name"],
        "title":  best_sp["title"],
        "reason": " ".join(reasons),
        "price_egp": best_sp["price_egp"],
        "rating":    best_sp["rating"],
    }

# ──────────────────────────────────────────────
# FOLLOW-UP QUESTIONS
# ──────────────────────────────────────────────
FOLLOW_UP_BANK: Dict[str, List[str]] = {
    "anger":               ["When does the anger peak most? (before bed / after school / during screen time)", "What usually happens in the 60 seconds before the outburst?"],
    "screen_addiction":    ["How many hours per day approximately? And what mostly (YouTube / games / TikTok)?", "Is there a specific time of day when cutting off causes the biggest reaction?"],
    "teen_communication":  ["When is your teen most calm and open to talking?", "Is it that they don't respond at all, or respond with anger?"],
    "bullying":            ["Where does the bullying happen most? (classroom / bus / club)", "Is there any adult at school your child already trusts?"],
    "study_focus":         ["How many minutes can they focus before getting distracted?", "Which subject creates the most resistance?"],
    "kids_stories":        ["How old is the child so I can pick the right story?", "What theme do you prefer — honesty, sharing, courage, or respect?"],
    "activities_games":    ["Do you prefer a calm activity or something active and physical?", "Do you have simple supplies like paper, pencils, or building blocks?"],
    "book_recommendations":["How old is your child and what kind of stories do they enjoy?", "Values-based books or adventure stories?"],
    "assessment_personality": ["Would you like to start a quick personality assessment?", "How old is your child so I can tailor the questions?"],
    "general_parenting":   ["How old is your child?", "When does the situation occur most often and what usually triggers it?"],
}

def pick_followups(topic: str) -> List[str]:
    return (FOLLOW_UP_BANK.get(topic) or ["Can you share a recent situation that happened?", "How old is your child?"])[:2]

# ──────────────────────────────────────────────
# CONFIDENCE SCORING
# ──────────────────────────────────────────────
def compute_confidence(
    topic: str, kb_res: KbSearchResult,
    age: Optional[int], user_text: str,
    in_scope: bool, risk_level: str,
) -> int:
    score = 40
    if in_scope and topic != "out_of_scope": score += 15
    if age is not None:                       score += 10
    if kb_res.matched:                        score += 25 + min(10, kb_res.match_count * 3)
    elif kb_res.used_default and topic in (KIDS_CONTENT_TOPICS | {ASSESSMENT_TOPIC}): score += 15
    else:                                     score -= 10
    if len((user_text or "").split()) >= 10:  score += 5
    if risk_level == "medium":                score -= 10
    elif risk_level == "high":                score -= 25
    return max(0, min(100, score))

# ──────────────────────────────────────────────
# EMPATHY REFLECT
# ──────────────────────────────────────────────
_EMPATHY: Dict[str, str] = {
    "anger":                "It sounds exhausting — dealing with these outbursts takes so much energy.",
    "screen_addiction":     "Screen time worries are so common right now, and your concern makes complete sense.",
    "teen_communication":   "That distance from your teen can feel really painful. You're not alone in this.",
    "bullying":             "It's completely natural to feel alarmed when your child is being hurt.",
    "study_focus":          "The homework struggle is real — it's draining for the whole family.",
    "kids_stories":         "How lovely that you want to share a special story moment together.",
    "activities_games":     "It's great that you're looking for meaningful ways to engage with your child.",
    "assessment_personality": "Understanding your child better is one of the kindest things you can do for them.",
    "general_parenting":    "Parenting is full of moments that leave us uncertain — you're doing the right thing by seeking support.",
}

def empathy_reflect(user_text: str, topic: str, risk_level: str, lang: str) -> str:
    if lang != "ar":
        empathy = _EMPATHY.get(topic, "I hear you — this situation sounds genuinely challenging.")
        if risk_level == "medium": empathy += " Let's go through this carefully together."
        snippet  = (user_text[:77] + "...") if len(user_text) > 80 else user_text
        return f"{empathy}\n\nYou said: \"{snippet}\"\n"
    # Arabic
    ar_empathy = {
        "anger":             "واضح إن الموضوع ده متعبك وبيستنزف أعصابك.",
        "screen_addiction":  "حاسّة بقلقك من موضوع الشاشات وتأثيره عليه.",
        "teen_communication":"واضح إن قلة التواصل مضايقاكي وبتوجع.",
        "bullying":          "طبيعي تقلقي جدًا لما تحسي إن ابنك بيتأذى.",
        "study_focus":       "الإحساس بالحيرة مع المذاكرة بيكون مرهق فعلًا.",
        "general_parenting": "الأمومة مليانة مواقف بتخلينا نحتار.",
    }.get(topic, "حاسة بيكي، والموضوع ده مش سهل.")
    if risk_level == "medium": ar_empathy += " خلّينا نمشي بهدوء ونفهم الصورة كاملة."
    snippet = (user_text[:77] + "...") if len(user_text) > 80 else user_text
    return f"{ar_empathy}\n\nإنتِ بتقولي: «{snippet}»\n"

# ──────────────────────────────────────────────
# GEMINI HELPERS
# ──────────────────────────────────────────────
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
        "If user writes 'book sl_001' or 'احجز sl_001' → extract slot_id.\n"
        "Output ONLY valid JSON matching the schema."
    )
    history_str = "\n".join(f"{m.role}: {m.content}" for m in history[-6:])
    prompt = (
        f"System: {system}\n\nConversation:\n{history_str}\n\n"
        f"User message:\n{user_text}\n\nKnown child age: {fallback_age}"
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
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
        return RouteDecision(in_scope=False, topic="out_of_scope", action="refuse_out_of_scope",
                             reason=f"Router parse failed. raw={resp.text[:100]}")

def gemini_compose_answer(
    user_text: str, topic: str,
    tips: List[Dict], specialists: List[Dict], slots: List[Dict],
    memory: Dict, followups: List[str],
    confidence: int, risk_level: str, lang: str,
) -> str:
    _require_gemini()
    lang_instruction = (
        "Reply in formal but warm Modern Standard Arabic (Egyptian dialect is fine for warmth)."
        if lang == "ar" else
        "Reply in clear, warm, professional English."
    )
    payload = {"topic": topic, "tips": tips, "specialists": specialists, "slots": slots,
               "memory": memory, "followups": followups, "confidence": confidence, "risk_level": risk_level}
    system = (
        f"You are Rafiq, a supportive family assistant. {lang_instruction}\n"
        "Rules: NO diagnosis, NO medication advice, NO programming.\n"
        "Use ONLY the data provided in ALLOWED DATA.\n"
        "If confidence < 65 or tips empty: give a short empathetic reply + ONE follow-up question.\n"
        "If confidence >= 65: give 2-3 practical bullet points + ONE follow-up question + suggest booking if relevant.\n"
        "Max 350 words."
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{system}\n\nUSER:\n{user_text}\n\nALLOWED DATA:\n{json.dumps(payload, ensure_ascii=False)}",
        config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=500),
    )
    return (resp.text or "").strip() or ("ممكن تقوليلي تفاصيل أكتر؟" if lang == "ar" else "Could you share more details?")

def gemini_verify_answer(user_text: str, answer: str, allowed_payload: Dict) -> Dict[str, Any]:
    _require_gemini()
    prompt = (
        f"Check if this reply violates Rafiq rules (no diagnosis/meds/programming).\n"
        f"Output ONLY JSON: {{\"ok\": true/false, \"reason\": \"brief\"}}\n\n"
        f"USER:\n{user_text}\n\nANSWER:\n{answer}\n\nALLOWED:\n{json.dumps(allowed_payload, ensure_ascii=False)}"
    )
    r = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(response_mime_type="application/json",
                                                  temperature=0, max_output_tokens=150)
    )
    try:
        data = json.loads(r.text)
        return {"ok": bool(data.get("ok", True)), "reason": str(data.get("reason", ""))}
    except Exception:
        return {"ok": True, "reason": ""}

# ──────────────────────────────────────────────
# ROUTES — SYSTEM
# ──────────────────────────────────────────────
@app.get("/", tags=["System"])
def home():
    return {"status": "Rafiq running 🚀", "version": "3.0.0"}

@app.get("/health", tags=["System"])
def health():
    return {"ok": True, "model": GEMINI_MODEL, "gemini_enabled": GEMINI_ENABLED,
            "verify": ENABLE_VERIFY, "db": bool(DATABASE_URL), "debug": DEBUG}

@app.get("/test_gemini", tags=["System"])
def test_gemini():
    _require_gemini()
    r = client.models.generate_content(model=GEMINI_MODEL, contents="Reply with OK only.")
    return {"text": r.text}

# ──────────────────────────────────────────────
# ROUTES — USERS
# ──────────────────────────────────────────────
@app.post("/users", tags=["Users"], summary="Register or update a user profile")
def upsert_user(req: UserUpsertReq):
    """
    Create user if not exists, update if already exists.
    Supports: user_id, name, email, child_age.
    """
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO users (user_id, name, email, child_age, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                name      = COALESCE(EXCLUDED.name,      users.name),
                email     = COALESCE(EXCLUDED.email,     users.email),
                child_age = COALESCE(EXCLUDED.child_age, users.child_age),
                updated_at = NOW()
            RETURNING user_id, name, email, child_age, created_at, updated_at
            """,
            (req.user_id, req.name, req.email, req.child_age, json.dumps([]))
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "ok": True,
            "user": {
                "user_id":   row[0],
                "name":      row[1],
                "email":     row[2],
                "child_age": row[3],
                "created_at": row[4].isoformat() if row[4] else None,
                "updated_at": row[5].isoformat() if row[5] else None,
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

# ──────────────────────────────────────────────
# ROUTES — FCM / PUSH NOTIFICATIONS
# ──────────────────────────────────────────────

@app.post("/register-token", tags=["Notifications"])
def register_token(req: RegisterTokenReq):
    """Save or update a user's FCM device token."""
    conn = get_conn()
    try:
        ensure_user_exists(conn, req.user_id)
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET fcm_token = %s, updated_at = NOW() WHERE user_id = %s",
            (req.fcm_token, req.user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"User '{req.user_id}' not found")
        conn.commit()
        log_event(conn, req.user_id, "fcm_token_registered", value=req.fcm_token[:20])
        return {"ok": True, "user_id": req.user_id, "message": "FCM token saved successfully"}
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


@app.post("/send-daily-tip", tags=["Notifications"])
def send_daily_tip(req: SendDailyTipReq):
    """
    Save a parenting tip for the user, then push a Firebase notification to their device.
    """
    conn = get_conn()
    try:
        # 1 — Fetch the user's FCM token
        cur = conn.cursor()
        cur.execute("SELECT fcm_token FROM users WHERE user_id = %s", (req.user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"User '{req.user_id}' not found")
        fcm_token: Optional[str] = row[0]
        if not fcm_token:
            raise HTTPException(
                status_code=422,
                detail="User has no registered FCM token. Call POST /register-token first."
            )

        # 2 — Persist the tip
        ensure_user_exists(conn, req.user_id)
        cur.execute(
            "INSERT INTO daily_tips (user_id, tip) VALUES (%s, %s)",
            (req.user_id, req.tip)
        )
        conn.commit()

        # 3 — Send Firebase push notification
        if not FIREBASE_ENABLED:
            return {
                "ok": True,
                "user_id": req.user_id,
                "tip_saved": True,
                "notification_sent": False,
                "warning": "Firebase not configured — tip saved but no push notification sent."
            }

        try:
            message = fb_messaging.Message(
                notification=fb_messaging.Notification(
                    title="💡 نصيحة اليوم من رفيق",
                    body=req.tip[:200],
                ),
                token=fcm_token,
                data={"user_id": req.user_id, "type": "daily_tip"},
            )
            fb_messaging.send(message)
        except fb_messaging.UnregisteredError:
            # Token expired — clear it so we don't retry
            cur.execute(
                "UPDATE users SET fcm_token = NULL WHERE user_id = %s", (req.user_id,)
            )
            conn.commit()
            raise HTTPException(
                status_code=410,
                detail="FCM token is no longer valid (device unregistered). Token cleared — please re-register."
            )
        except Exception as fb_exc:
            raise HTTPException(status_code=502, detail=f"Firebase error: {fb_exc}")

        log_event(conn, req.user_id, "daily_tip_sent", value=req.tip[:100])
        return {
            "ok": True,
            "user_id": req.user_id,
            "tip_saved": True,
            "notification_sent": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


@app.get("/daily-tip/{user_id}", tags=["Notifications"])
def get_daily_tips(user_id: str, limit: int = 50):
    """Return all saved tips for a user, newest first."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        # Verify user exists
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

        cur.execute(
            "SELECT id, tip, created_at FROM daily_tips WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, max(1, min(200, limit)))
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id,
            "total": len(rows),
            "tips": [
                {
                    "id": r[0],
                    "tip": r[1],
                    "created_at": r[2].isoformat() if r[2] else None,
                }
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()

# ──────────────────────────────────────────────
# ROUTES — KNOWLEDGE BASE
# ──────────────────────────────────────────────
@app.get("/kb/topics", tags=["KB"])
def kb_topics():
    topics = sorted({x["topic"] for x in KB})
    return {"topics": topics, "count": len(topics)}

@app.get("/kb/search", tags=["KB"])
def kb_search_api(topic: str, q: str = "", age: Optional[int] = None):
    res = kb_search_v2(topic=topic, query=q, age=age)
    return {"topic": topic, "age": age, "matched": res.matched,
            "match_count": res.match_count, "used_default": res.used_default, "tips": res.tips}

@app.post("/kb/add", tags=["KB"])
def kb_add(req: KbAddRequest):
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")
    new_id = "kb_" + uuid.uuid4().hex[:6]
    KB.append({"id": new_id, "topic": req.topic, "age_min": req.age_min,
               "age_max": req.age_max, "tags": req.tags, "tip": req.tip})
    return {"ok": True, "kb_id": new_id, "total": len(KB)}

# ──────────────────────────────────────────────
# ROUTES — ASSESSMENT
# ──────────────────────────────────────────────
@app.get("/assessment/questions", tags=["Assessment"])
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return {
        "child_age": age,
        "total_questions": len(qs),
        "scale": {"min": 1, "max": 5,
                  "labels": {"1": "Never", "2": "Rarely", "3": "Sometimes", "4": "Often", "5": "Always"}},
        "questions": _format_questions_for_api(qs),
    }

@app.post("/assessment/submit", tags=["Assessment"])
def assessment_submit(req: AssessmentSubmitReq):
    conn = get_conn()
    try:
        ensure_user_exists(conn, req.user_id)
        profile      = compute_personality_profile(req.answers, req.child_age, req.behavior_signals)
        assess_conf  = compute_assessment_confidence(req.answers, req.child_age, req.behavior_signals)
        recommended  = recommend_specialist_for_profile(profile)

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO assessments (user_id, child_age, assessment_confidence, result, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            """,
            (req.user_id, req.child_age, assess_conf["confidence"], json.dumps(profile))
        )
        conn.commit()
        update_memory(conn, req.user_id, "assessment_personality", req.child_age, note="Assessment submitted")
        log_event(conn, req.user_id, "assessment_submit", value=f"confidence={assess_conf['confidence']}")

        return {
            "ok": True,
            "trait_scores":          profile["trait_scores"],
            "top_traits":            profile["top_traits"],
            "low_traits":            profile["low_traits"],
            "possible_personalities":profile["possible_personalities"],
            "recommendations":       profile["recommendations"],
            "confidence":            assess_conf["confidence"],
            "assessment_meta":       assess_conf,
            "recommended_specialist": recommended,
            "note":                  profile["note"],
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

# ──────────────────────────────────────────────
# ROUTES — SPECIALISTS & SLOTS
# ──────────────────────────────────────────────
@app.get("/specialists", tags=["Specialists"])
def specialists_list():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT id, name, title, topics, price_egp, rating FROM specialists ORDER BY rating DESC")
    rows = cur.fetchall()
    conn.close()
    if rows:
        return {"specialists": [{"id": r[0], "name": r[1], "title": r[2], "topics": r[3],
                                  "price_egp": float(r[4]), "rating": float(r[5])} for r in rows]}
    return {"specialists": sorted(SPECIALISTS, key=lambda x: -x["rating"])}

@app.get("/slots/{specialist_id}", tags=["Specialists"])
def get_slots(specialist_id: str):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT slot_id, start_time, duration_min, available FROM slots WHERE specialist_id=%s ORDER BY start_time",
        (specialist_id,)
    )
    rows = cur.fetchall()
    conn.close()
    if rows:
        return {"slots": [{"slot_id": r[0], "start_time": r[1], "duration_min": r[2], "available": r[3]} for r in rows]}
    return {"slots": [s for s in SLOTS if s["specialist_id"] == specialist_id]}

# ──────────────────────────────────────────────
# ROUTES — APPOINTMENTS
# ──────────────────────────────────────────────
@app.post("/appointments/book", tags=["Appointments"])
def book(req: BookingReq):
    conn = get_conn()
    try:
        ensure_user_exists(conn, req.user_id)
        sync_slots_with_booked(conn)
        appt = book_slot(conn, req.user_id, req.specialist_id, req.slot_id)
        log_event(conn, req.user_id, "booking_created", value=req.slot_id)
        return {"ok": True, "appointment": appt}
    except ValueError:
        raise HTTPException(status_code=400, detail="Slot not available")
    finally:
        conn.close()

@app.get("/appointments/{user_id}", tags=["Appointments"])
def get_appointments(user_id: str, limit: int = 50):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT appointment_id, specialist_id, slot_id, status, created_at FROM appointments WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, max(1, min(200, limit)))
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "appointments": [
            {"appointment_id": r[0], "specialist_id": r[1], "slot_id": r[2],
             "status": r[3], "created_at": r[4].isoformat() if r[4] else None}
            for r in rows
        ]
    }

# ──────────────────────────────────────────────
# ROUTES — ANALYTICS
# ──────────────────────────────────────────────
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

# ──────────────────────────────────────────────
# ROUTES — FEEDBACK
# ──────────────────────────────────────────────
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

# ──────────────────────────────────────────────
# ROUTES — CHAT HISTORY
# ──────────────────────────────────────────────
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


# ──────────────────────────────────────────────
# ROUTES — CHAT (Main)
# ──────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages list is empty")

    message_id = "msg_" + uuid.uuid4().hex[:10]
    user_text  = req.messages[-1].content.strip()
    lang       = detect_lang(user_text)

    # Hard guards (pre-DB)
    if hard_out_of_scope(user_text) or hard_medical(user_text):
        return ChatResponse(
            message_id=message_id,
            reply=tr("out_of_scope_reply", lang),
            cards=[{"type": "refusal",
                    "title": "Out of scope" if lang == "en" else "خارج نطاق رفيق",
                    "body": tr("out_of_scope_card", lang)}]
        )

    if not GEMINI_ENABLED or client is None:
        return ChatResponse(
            message_id=message_id,
            reply=tr("gemini_disabled", lang),
            cards=[{"type": "warning", "title": "Gemini disabled",
                    "body": "Set GEMINI_API_KEY in environment variables."}]
        )

    conn = get_conn()
    try:
        slot_from_text = extract_slot_id(user_text)
        wants_booking  = any(x in user_text for x in
                             ["احجز","حجز","استشارة","مختص","دكتور","book","specialist","appointment"])
        risk_level     = detect_risk_level(user_text)

        if risk_level == "high":
            ensure_user_exists(conn, req.user_id)
            log_event(conn, req.user_id, "risk_high", value=user_text[:200])
            return ChatResponse(
                message_id=message_id,
                reply=tr("risk_high", lang),
                cards=[{"type": "warning",
                        "title": "Important" if lang == "en" else "مهم جدًا",
                        "body": tr("risk_high_card", lang),
                        "meta": {"risk_level": "high"}}]
            )

        try:
            decision = gemini_route_decision(user_text, req.messages, req.child_age)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Router failed: {exc}")

        if slot_from_text and decision.topic != "out_of_scope":
            decision.action  = "book_appointment"
            decision.slot_id = slot_from_text

        ensure_user_exists(conn, req.user_id)
        log_event(conn, req.user_id, "chat_message", value=user_text[:300])

        if not decision.in_scope or decision.action == "refuse_out_of_scope":
            return ChatResponse(
                message_id=message_id,
                reply=tr("scope_refusal", lang),
                cards=[{"type": "refusal",
                        "title": "Out of scope" if lang == "en" else "خارج نطاق رفيق",
                        "body": f"Reason: {decision.reason}" if lang == "en" else f"السبب: {decision.reason}"}]
            )

        topic = decision.topic

        if topic in KIDS_CONTENT_TOPICS and kids_safety_guard(user_text):
            return ChatResponse(
                message_id=message_id,
                reply=tr("kids_safety", lang),
                cards=[{"type": "warning",
                        "title": "Child-appropriate content" if lang == "en" else "محتوى مناسب للأطفال",
                        "body": "Choose a safe, age-appropriate topic."}]
            )

        age = decision.extracted_child_age or req.child_age
        update_memory(conn, req.user_id, topic, age, note=user_text)
        mem = get_memory(conn, req.user_id)

        # Booking flow
        if decision.action == "book_appointment":
            slot_id       = decision.slot_id
            specialist_id = decision.specialist_id
            if slot_id and not specialist_id:
                ms = next((s for s in SLOTS if s["slot_id"] == slot_id), None)
                if ms: specialist_id = ms["specialist_id"]

            if not slot_id or not specialist_id:
                return ChatResponse(
                    message_id=message_id,
                    reply=tr("missing_slot", lang),
                    cards=[{"type": "warning",
                            "title": "Missing booking data" if lang == "en" else "ناقص بيانات الحجز",
                            "body": "Send slot_id like sl_001."}]
                )

            sync_slots_with_booked(conn)
            try:
                appt = book_slot(conn, req.user_id, specialist_id, slot_id)
                sp   = next((x for x in SPECIALISTS if x["id"] == specialist_id), None)
                log_event(conn, req.user_id, "booking_created", value=slot_id)
                return ChatResponse(
                    message_id=message_id,
                    reply=f"{tr('booking_success', lang)}{appt['appointment_id']}.",
                    cards=[{"type": "booking",
                            "title": "Booking details" if lang == "en" else "تفاصيل الحجز",
                            "body": f"Specialist: {sp['name'] if sp else specialist_id}\nslot_id: {slot_id}",
                            "meta": appt}]
                )
            except ValueError:
                return ChatResponse(
                    message_id=message_id,
                    reply=tr("slot_unavailable", lang),
                    cards=[{"type": "warning",
                            "title": "Slot unavailable" if lang == "en" else "الموعد غير متاح",
                            "body": "Try a different slot_id."}]
                )

        # Normal answer
        kb_res    = kb_search_v2(topic=topic, query=user_text, age=age)
        tips      = kb_res.tips
        followups = pick_followups(topic)
        conf      = compute_confidence(topic, kb_res, age, user_text, decision.in_scope, risk_level)
        show_sp   = wants_booking or decision.action == "recommend_booking" or risk_level == "medium"
        spec_list: List[Dict] = recommend_specialists(topic) if show_sp else []
        slots_list: List[Dict]= available_slots(spec_list[0]["id"]) if spec_list else []

        if topic in PARENTING_TOPICS and not kb_res.matched and conf < 65:
            q = followups[0] if followups else ("How old is your child?" if lang == "en" else "سن الطفل قد إيه؟")
            return ChatResponse(
                message_id=message_id,
                reply=tr("low_conf_prefix", lang) + q + tr("low_conf_suffix", lang),
                cards=[
                    {"type": "confidence", "title": "Confidence", "body": f"{conf}%",
                     "meta": {"confidence": conf, "matched": kb_res.matched}},
                    {"type": "warning",
                     "title": "Follow-up" if lang == "en" else "سؤال متابعة",
                     "body": q, "meta": {"followups": followups}},
                ]
            )

        intro = empathy_reflect(user_text, topic, risk_level, lang)
        try:
            final_text = intro + gemini_compose_answer(
                user_text=user_text, topic=topic, tips=tips,
                specialists=spec_list, slots=slots_list, memory=mem,
                followups=followups, confidence=conf, risk_level=risk_level, lang=lang,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Compose failed: {exc}")

        if ENABLE_VERIFY:
            verdict = gemini_verify_answer(user_text, final_text,
                         {"topic": topic, "tips": tips, "specialists": spec_list,
                          "slots": slots_list, "memory": mem, "followups": followups, "confidence": conf})
            if not verdict.get("ok", True):
                q = followups[0] if followups else ("How old is your child?" if lang == "en" else "سن الطفل قد إيه؟")
                final_text = tr("verify_fallback", lang) + q

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (message_id, user_id, message, response) VALUES (%s,%s,%s,%s)",
            (message_id, req.user_id, user_text, final_text)
        )
        conn.commit()

        # Cards
        cards: List[Dict] = []
        ctype_map  = {"kids_stories":"story","activities_games":"game",
                      "book_recommendations":"books","assessment_personality":"assessment_question"}
        ctitle_map = {
            "kids_stories":          ("Kids Story",             "قصة للأطفال"),
            "activities_games":      ("Activity / Game",        "لعبة / نشاط"),
            "book_recommendations":  ("Book Suggestion",        "اقتراح قراءة"),
            "assessment_personality":("Personality Assessment", "تقييم شخصية الطفل"),
        }
        for tip_item in tips:
            ctype  = ctype_map.get(topic, "tip")
            titles = ctitle_map.get(topic, ("Practical Tip", "نصيحة عملية"))
            cards.append({"type": ctype, "title": titles[0 if lang=="en" else 1],
                          "body": tip_item["tip"],
                          "meta": {"kb_id": tip_item["id"], "age_used": age, "matched": kb_res.matched}})

        cards.append({"type": "confidence",
                      "title": "Confidence Score" if lang=="en" else "درجة الثقة",
                      "body": f"{conf}%",
                      "meta": {"confidence": conf, "risk_level": risk_level}})

        if conf < 70 or (topic in PARENTING_TOPICS and not kb_res.matched):
            cards.append({"type": "warning",
                          "title": "Follow-up" if lang=="en" else "سؤال متابعة",
                          "body": followups[0] if followups else "",
                          "meta": {"followups": followups}})

        if show_sp:
            for sp in spec_list:
                cards.append({"type": "specialist",
                               "title": f"{sp['name']} — {sp['title']}",
                               "body": f"Price: {sp['price_egp']} EGP | Rating: {sp['rating']}" if lang=="en"
                                       else f"السعر: {sp['price_egp']} جنيه | التقييم: {sp['rating']}",
                               "meta": {"specialist_id": sp["id"]}})

        if slots_list and show_sp:
            if lang == "en":
                sb = "\n".join([f"- {s['slot_id']}: {s['start']} ({s['duration_min']} min)" for s in slots_list])
                sb += "\n\nTo book: send 'book sl_001'"
            else:
                sb = "\n".join([f"- {s['slot_id']}: {s['start']} ({s['duration_min']} دقيقة)" for s in slots_list])
                sb += "\n\nللحجز ابعت: احجز sl_001"
            cards.append({"type": "booking",
                           "title": "Available Slots" if lang=="en" else "مواعيد متاحة",
                           "body": sb,
                           "meta": {"slot_ids": [s["slot_id"] for s in slots_list],
                                    "specialist_id": spec_list[0]["id"] if spec_list else None}})

        return ChatResponse(message_id=message_id, reply=final_text, cards=cards)

    finally:
        conn.close()


# ──────────────────────────────────────────────
# ROUTES — PARENTING PLAN
# ──────────────────────────────────────────────

@app.post("/generate-parenting-plan/{user_id}", tags=["Parenting Plan"])
def generate_parenting_plan(user_id: str):
    """
    Generate a personalised 30-day parenting plan from the user's latest
    assessment result, persist it in parenting_plans, then push a
    Firebase notification to the user's device.
    """

    # 1. Require Gemini
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(
            status_code=503,
            detail="Gemini is disabled. Set GEMINI_API_KEY to use this feature."
        )

    conn = get_conn()
    try:
        # 2. Ensure user exists
        ensure_user_exists(conn, user_id)
        cur = conn.cursor()

        # 3. Fetch latest assessment
        cur.execute(
            """
            SELECT id, child_age, assessment_confidence, result, created_at
            FROM   assessments
            WHERE  user_id = %s
            ORDER  BY created_at DESC
            LIMIT  1
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No assessment found for user '{user_id}'. "
                    "Please complete an assessment first via POST /assessment/submit."
                )
            )

        assessment_id, child_age, assessment_confidence, result_raw, assessed_at = row

        # 4. Parse result JSON
        try:
            result: Dict[str, Any] = (
                json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse assessment result JSON: {exc}"
            )

        # Debug log — shows exact structure before any processing
        print(f"[DEBUG] assessment result for user={user_id}: {json.dumps(result, ensure_ascii=False)[:600]}")

        # ── Normalise helpers ──────────────────────────────────────────
        # top_traits / low_traits can be:
        #   format A (dict):  [{"trait": "focus", "score": 100}, ...]
        #   format B (list):  [["focus", 100], ...]
        def _norm_traits(raw: Any) -> List[Dict[str, Any]]:
            out = []
            for item in (raw or []):
                if isinstance(item, dict):
                    # already the expected dict format
                    out.append({
                        "trait": str(item.get("trait") or item.get("name") or ""),
                        "score": int(item.get("score", 0)),
                    })
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    # ["focus", 100]  or  ("focus", 100)
                    out.append({"trait": str(item[0]), "score": int(item[1])})
            return out

        # possible_personalities can be:
        #   format A (dict):  [{"id":"thinker","name":"المفكر","description":"...","needs":"...","match_pct":60}, ...]
        #   format B (dict):  [{"id":"thinker","name":"المفكر","match":60}, ...]   ← no description/needs
        #   format C (list):  [["thinker", 60], ...]   (unlikely but handled)
        def _norm_personalities(raw: Any) -> List[Dict[str, Any]]:
            out = []
            for item in (raw or []):
                if isinstance(item, dict):
                    out.append({
                        "id":          str(item.get("id", "")),
                        "name":        str(item.get("name", "غير محدد")),
                        "description": str(item.get("description", "")),
                        "needs":       str(item.get("needs", "")),
                        "match_pct":   int(item.get("match_pct") or item.get("match") or 0),
                    })
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.append({
                        "id": str(item[0]), "name": str(item[0]),
                        "description": "", "needs": "",
                        "match_pct": int(item[1]),
                    })
            return out

        # trait_scores can be:
        #   format A (dict):  {"focus": 100, "leadership": 0, ...}
        #   format B (list):  [["focus", 100], ...]
        def _norm_scores(raw: Any) -> Dict[str, int]:
            if isinstance(raw, dict):
                return {str(k): int(v) for k, v in raw.items()}
            out = {}
            for item in (raw or []):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out[str(item[0])] = int(item[1])
            return out

        top_traits             = _norm_traits(result.get("top_traits", []))
        low_traits             = _norm_traits(result.get("low_traits", []))
        possible_personalities = _norm_personalities(result.get("possible_personalities", []))
        trait_scores           = _norm_scores(result.get("trait_scores", {}))

        print(f"[DEBUG] normalised — top_traits={top_traits}, personalities={possible_personalities}, scores={trait_scores}")

        # 5. Build Gemini prompt
        top_arch_entry  = possible_personalities[0] if possible_personalities else {}
        top_archetype   = top_arch_entry.get("name", "غير محدد")
        archetype_desc  = top_arch_entry.get("description", "")
        archetype_needs = top_arch_entry.get("needs", "")

        traits_text = "\n".join(
            f"  - {t['trait'].replace('_', ' ').title()}: {t['score']}%"
            for t in top_traits
        ) or "  - لا توجد بيانات كافية"

        scores_text = "\n".join(
            f"  - {k.replace('_', ' ').title()}: {v}%"
            for k, v in trait_scores.items()
        ) or "  - لا توجد بيانات كافية"

        prompt = (
            "أنت مدرب تربوي محترف متخصص في التطوير الشخصي للأطفال.\n\n"
            "فيما يلي نتائج تقييم شخصية الطفل:\n"
            f"- عمر الطفل: {child_age if child_age is not None else 'غير محدد'} سنة\n"
            f"- النمط الشخصي الأبرز: {top_archetype} — {archetype_desc}\n"
            f"- احتياجات الطفل: {archetype_needs}\n\n"
            f"أبرز الصفات:\n{traits_text}\n\n"
            f"درجات جميع الصفات:\n{scores_text}\n\n"
            "المطلوب:\n"
            "أنشئ خطة تربوية مخصصة لمدة 30 يومًا (4 أسابيع) بناءً على هذه البيانات.\n"
            "يجب أن تتضمن الخطة:\n"
            "1. هدف الأسبوع (لكل أسبوع من الأربعة)\n"
            "2. أنشطة يومية عملية ومناسبة لعمر الطفل\n"
            "3. أساليب التعزيز الإيجابي المقترحة لكل أسبوع\n"
            "4. توصيات خاصة بالوالدين لدعم الطفل\n"
            "5. ملاحظة ختامية للمتابعة بعد انتهاء الخطة\n\n"
            "الأسلوب: دافئ، واضح، وعملي. تجنب المصطلحات الطبية أو التشخيصية.\n"
            "أعد الخطة كاملةً باللغة العربية."
        )

        # 6. Call Gemini
        try:
            gemini_response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.6,
                    max_output_tokens=2000,
                ),
            )
            plan_text: str = (gemini_response.text or "").strip()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Gemini API error while generating plan: {exc}"
            )

        if not plan_text:
            raise HTTPException(
                status_code=502,
                detail="Gemini returned an empty plan. Please try again."
            )

        # 7. Persist plan
        try:
            cur.execute(
                """
                INSERT INTO parenting_plans (user_id, plan_text, created_at)
                VALUES (%s, %s, NOW())
                RETURNING id, created_at
                """,
                (user_id, plan_text)
            )
            plan_row        = cur.fetchone()
            conn.commit()
            plan_id         = plan_row[0]
            plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
        except Exception as exc:
            conn.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Database error while saving plan: {exc}"
            )

        # 8. Log analytics event
        log_event(
            conn, user_id,
            "parenting_plan_generated",
            value=f"plan_id={plan_id}, assessment_id={assessment_id}"
        )

        # 9. Firebase push notification
        notification_sent    = False
        notification_warning = None

        if FIREBASE_ENABLED:
            cur.execute(
                "SELECT fcm_token FROM users WHERE user_id = %s",
                (user_id,)
            )
            token_row = cur.fetchone()
            fcm_token: Optional[str] = token_row[0] if token_row else None

            if not fcm_token:
                notification_warning = (
                    "No FCM token registered for this user — "
                    "plan saved but no push notification sent. "
                    "Call POST /register-token to enable notifications."
                )
            else:
                try:
                    message = fb_messaging.Message(
                        notification=fb_messaging.Notification(
                            title="🎯 خطة تربوية جديدة",
                            body="تم إنشاء خطة مخصصة لطفلك بناءً على نتائج التقييم.",
                        ),
                        token=fcm_token,
                        data={
                            "user_id": user_id,
                            "type":    "parenting_plan",
                            "plan_id": str(plan_id),
                        },
                    )
                    fb_messaging.send(message)
                    notification_sent = True
                except fb_messaging.UnregisteredError:
                    cur.execute(
                        "UPDATE users SET fcm_token = NULL WHERE user_id = %s",
                        (user_id,)
                    )
                    conn.commit()
                    notification_warning = (
                        "FCM token is no longer valid (device unregistered). "
                        "Token cleared — please re-register via POST /register-token."
                    )
                except Exception as fb_exc:
                    notification_warning = f"Firebase send error: {fb_exc}"
        else:
            notification_warning = (
                "Firebase is not configured — plan saved but no push notification sent."
            )

        # 10. Return response
        response: Dict[str, Any] = {
            "ok":                True,
            "user_id":           user_id,
            "plan_generated":    True,
            "plan_id":           plan_id,
            "created_at":        plan_created_at,
            "child_age":         child_age,
            "top_archetype":     top_archetype,
            "assessment_id":     assessment_id,
            "notification_sent": notification_sent,
            "plan_text":         plan_text,
        }
        if notification_warning:
            response["notification_warning"] = notification_warning

        return response

    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        conn.close()


@app.get("/parenting-plans/{user_id}", tags=["Parenting Plan"])
def get_parenting_plans(user_id: str, limit: int = 10):
    """Return all saved parenting plans for a user, newest first. limit capped at 50."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
        cur.execute(
            """
            SELECT id, plan_text, created_at
            FROM   parenting_plans
            WHERE  user_id = %s
            ORDER  BY created_at DESC
            LIMIT  %s
            """,
            (user_id, max(1, min(50, limit)))
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id,
            "total":   len(rows),
            "plans": [
                {
                    "id":         r[0],
                    "plan_text":  r[1],
                    "created_at": r[2].isoformat() if r[2] else None,
                }
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


# ──────────────────────────────────────────────
# ROUTES — PDF EXPORT
# ──────────────────────────────────────────────

def _build_parenting_plan_pdf(
    user_id: str,
    child_age: Optional[int],
    top_archetype: str,
    plan_text: str,
    generated_at: str,
) -> bytes:
    """
    Render a professional Arabic-friendly PDF for the parenting plan.
    Returns raw PDF bytes.
    """
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2 * cm,
        title=f"Rafiq Parenting Plan — {user_id}",
        author="Rafiq AI",
    )

    W, H = A4
    styles = getSampleStyleSheet()

    # ── Custom styles ──────────────────────────────────────────────────
    brand_green  = colors.HexColor("#1B6B3A")
    brand_light  = colors.HexColor("#E8F5E9")
    text_dark    = colors.HexColor("#1A1A1A")
    text_muted   = colors.HexColor("#555555")
    accent_gold  = colors.HexColor("#C8860A")

    style_main_title = ParagraphStyle(
        "MainTitle",
        parent=styles["Title"],
        fontSize=26,
        textColor=brand_green,
        spaceAfter=4,
        spaceBefore=0,
        alignment=TA_CENTER,
        fontName="Helvetica-Bold",
    )
    style_subtitle = ParagraphStyle(
        "SubTitle",
        parent=styles["Normal"],
        fontSize=12,
        textColor=text_muted,
        spaceAfter=2,
        alignment=TA_CENTER,
        fontName="Helvetica",
    )
    style_section_heading = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading1"],
        fontSize=13,
        textColor=brand_green,
        spaceBefore=14,
        spaceAfter=4,
        fontName="Helvetica-Bold",
        borderPad=4,
    )
    style_label = ParagraphStyle(
        "Label",
        parent=styles["Normal"],
        fontSize=10,
        textColor=text_muted,
        fontName="Helvetica-Bold",
        spaceAfter=1,
    )
    style_value = ParagraphStyle(
        "Value",
        parent=styles["Normal"],
        fontSize=11,
        textColor=text_dark,
        fontName="Helvetica",
        spaceAfter=6,
    )
    style_plan_heading = ParagraphStyle(
        "PlanHeading",
        parent=styles["Heading2"],
        fontSize=12,
        textColor=accent_gold,
        spaceBefore=10,
        spaceAfter=3,
        fontName="Helvetica-Bold",
    )
    style_plan_body = ParagraphStyle(
        "PlanBody",
        parent=styles["Normal"],
        fontSize=10.5,
        textColor=text_dark,
        fontName="Helvetica",
        leading=16,
        spaceAfter=4,
    )
    style_bullet = ParagraphStyle(
        "Bullet",
        parent=styles["Normal"],
        fontSize=10.5,
        textColor=text_dark,
        fontName="Helvetica",
        leading=16,
        leftIndent=16,
        spaceAfter=3,
        bulletIndent=4,
    )
    style_footer = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=8,
        textColor=text_muted,
        alignment=TA_CENTER,
        fontName="Helvetica",
    )

    story = []

    # ── Header banner (coloured table row) ────────────────────────────
    banner_table = Table(
        [[Paragraph("&#x1F916; Rafiq AI", style_main_title),
          Paragraph("رفيق", ParagraphStyle("AR", parent=style_main_title, fontSize=22))]],
        colWidths=[(W - 4 * cm) * 0.7, (W - 4 * cm) * 0.3],
    )
    banner_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), brand_green),
        ("TEXTCOLOR",   (0, 0), (-1, -1), colors.white),
        ("ALIGN",       (0, 0), (0, 0),   "LEFT"),
        ("ALIGN",       (1, 0), (1, 0),   "RIGHT"),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",  (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING",(0, 0),(-1, -1), 14),
        ("LEFTPADDING", (0, 0), (0, 0),   16),
        ("RIGHTPADDING",(1, 0), (1, 0),   16),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 0.35 * cm))

    story.append(Paragraph("Personalised 30-Day Parenting Plan", style_subtitle))
    story.append(Paragraph("خطة تربوية مخصصة — 30 يومًا", style_subtitle))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=brand_green, spaceAfter=10))

    # ── Meta info table ────────────────────────────────────────────────
    age_display    = f"{child_age} years" if child_age else "Not specified"
    arch_display   = top_archetype or "Not specified"
    date_display   = generated_at[:10] if generated_at else "—"

    meta_data = [
        ["User ID",            user_id,        "Child Age",         age_display],
        ["Top Archetype",      arch_display,   "Generated",         date_display],
    ]
    meta_table = Table(
        meta_data,
        colWidths=[
            (W - 4 * cm) * 0.18,
            (W - 4 * cm) * 0.32,
            (W - 4 * cm) * 0.18,
            (W - 4 * cm) * 0.32,
        ],
        hAlign="LEFT",
    )
    meta_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), brand_light),
        ("BACKGROUND",    (0, 0), (0, -1), colors.HexColor("#D0EAD8")),
        ("BACKGROUND",    (2, 0), (2, -1), colors.HexColor("#D0EAD8")),
        ("TEXTCOLOR",     (0, 0), (0, -1), brand_green),
        ("TEXTCOLOR",     (2, 0), (2, -1), brand_green),
        ("FONTNAME",      (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",      (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTNAME",      (1, 0), (1, -1), "Helvetica"),
        ("FONTNAME",      (3, 0), (3, -1), "Helvetica"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (0, 0), (-1, -1), "LEFT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#BBDDC7")),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.5 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=6))

    # ── Plan content ───────────────────────────────────────────────────
    story.append(Paragraph("Parenting Plan / الخطة التربوية", style_section_heading))
    story.append(Spacer(1, 0.2 * cm))

    # Smart line-by-line rendering
    week_keywords  = ("الأسبوع", "Week", "أسبوع")
    bullet_markers = ("•", "-", "–", "*", "·")

    for raw_line in plan_text.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 0.18 * cm))
            continue

        # Section/week headings
        if any(line.startswith(kw) for kw in week_keywords) or (
            len(line) < 80 and line.endswith(":") and not line.startswith(" ")
        ):
            story.append(Paragraph(_safe_xml(line), style_plan_heading))
            continue

        # Numbered list items  (1. / ١.)
        if (len(line) > 2 and line[0].isdigit() and line[1] in (".", ")")):
            story.append(Paragraph(f"&#x25CF;&nbsp;&nbsp;{_safe_xml(line[2:].strip())}",
                                    style_bullet))
            continue

        # Bullet items
        if line[0] in bullet_markers:
            story.append(Paragraph(f"&#x25CF;&nbsp;&nbsp;{_safe_xml(line[1:].strip())}",
                                    style_bullet))
            continue

        # Regular paragraph
        story.append(Paragraph(_safe_xml(line), style_plan_body))

    # ── Footer ─────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.6 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=6))
    story.append(Paragraph(
        "Generated by Rafiq AI &nbsp;|&nbsp; This plan is for guidance only and is not a clinical diagnosis.",
        style_footer,
    ))
    story.append(Paragraph(
        "أُنشئت بواسطة رفيق AI &nbsp;|&nbsp; هذه الخطة إرشادية وليست تشخيصًا طبيًا.",
        style_footer,
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()


def _safe_xml(text: str) -> str:
    """Escape characters that break ReportLab's XML parser inside Paragraph."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@app.get("/export-plan-pdf/{user_id}", tags=["Parenting Plan"])
def export_plan_pdf(user_id: str):
    """
    Generate and stream a PDF of the user's latest parenting plan.
    Returns application/pdf with filename parenting_plan_{user_id}.pdf
    """
    if not _REPORTLAB_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="PDF export is unavailable — reportlab is not installed. "
                   "Run: pip install reportlab"
        )

    conn = get_conn()
    try:
        # ── 1. Fetch latest parenting plan ─────────────────────────────
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pp.id, pp.plan_text, pp.created_at,
                   u.child_age,
                   a.result
            FROM   parenting_plans pp
            LEFT   JOIN users       u  ON u.user_id  = pp.user_id
            LEFT   JOIN assessments a  ON a.user_id  = pp.user_id
            WHERE  pp.user_id = %s
            ORDER  BY pp.created_at DESC
            LIMIT  1
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"No parenting plan found for user '{user_id}'. "
                       "Generate one first via POST /generate-parenting-plan/{user_id}."
            )

        plan_id, plan_text, created_at, child_age, result_raw = row
        generated_at = created_at.isoformat() if created_at else ""

        # ── 2. Extract top archetype from assessment result ─────────────
        top_archetype = "Not specified"
        if result_raw:
            try:
                result_obj = (
                    json.loads(result_raw) if isinstance(result_raw, str) else result_raw
                )
                personalities = result_obj.get("possible_personalities", [])
                if personalities:
                    first = personalities[0]
                    if isinstance(first, dict):
                        top_archetype = first.get("name") or first.get("id") or top_archetype
                    elif isinstance(first, (list, tuple)) and len(first) >= 1:
                        top_archetype = str(first[0])
            except Exception as parse_exc:
                print(f"[PDF] Could not parse archetype: {parse_exc}")

        # ── 3. Build PDF bytes ──────────────────────────────────────────
        try:
            pdf_bytes = _build_parenting_plan_pdf(
                user_id=user_id,
                child_age=child_age,
                top_archetype=top_archetype,
                plan_text=plan_text or "",
                generated_at=generated_at,
            )
        except Exception as pdf_exc:
            raise HTTPException(
                status_code=500,
                detail=f"PDF generation failed: {pdf_exc}"
            )

        # ── 4. Stream PDF response ──────────────────────────────────────
        filename = f"parenting_plan_{user_id}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(pdf_bytes)),
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        conn.close()
