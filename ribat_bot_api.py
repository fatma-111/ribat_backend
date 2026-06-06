"""
Rafiq Bot API — MERGED FINAL VERSION
=====================================
دمج أحسن ما في الكودين:
- PostgreSQL persistence (من رفيق) بدل JSON files
- Smart Router بـ Gemini Structured Output (من رباط)
- kb_search_v2: Arabic normalize + tokenize + scoring (من رباط)
- Personality Assessment كامل + Confidence scoring (من رباط)
- Risk escalation: high/medium/low (من رباط)
- Empathy reflect + Follow-up questions (من رباط)
- Gemini Verifier اختياري (من رباط)
- Cards system (tip/specialist/booking/warning/assessment_result) (من رباط)
- Hard guards: out-of-scope + medical (من رباط)
- Kids safety guard (من رباط)
- Memory: topic frequency + notes + child_age (من رباط)
- Analytics: per-user + by-type summary (من رباط)
- Booking: sync slots + reload before book (من رباط)
- Feedback loop (up/down + comment) (من رباط)
- /health /test_gemini /kb/search /kb/add /analytics/* (من رباط)
- Bug fixes: indentation, json.loads memory, isoformat (من رفيق)
"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
import uuid
import re
from datetime import datetime
from typing import List, Optional, Dict, Any, Literal, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import psycopg2

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None


# ======================
# CONFIG
# ======================
DEBUG = os.getenv("RAFIQ_DEBUG", "0") == "1"

DATABASE_URL = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ADMIN_KEY = os.getenv("RAFIQ_ADMIN_KEY", "change-me")
if ADMIN_KEY == "change-me":
    print("WARNING: RAFIQ_ADMIN_KEY is default. Set it in ENV for production.")

ENABLE_VERIFY = os.getenv("RAFIQ_VERIFY_OUTPUT", "0") == "1"

client = None
if GEMINI_ENABLED:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini initialized ✔")
    except Exception as e:
        print("Gemini init failed:", e)
        client = None
else:
    print("Gemini disabled (missing key or library)")

app = FastAPI(title="Rafiq Bot API — Merged Final")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ======================
# DB
# ======================
def get_conn():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL missing")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ======================
# KNOWLEDGE BASE
# ======================
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
            "قصة قصيرة (5 دقايق) — عنوان: «نجمة والمشاركة»\n"
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
            "الخطوات: اكتبوا 6 مشاعر (فرح/زعل/غضب/خوف/غيرة/حماس). "
            "اسحبوا ورقة، والطفل يمثل موقف بسيط للمشاعر دي. "
            "وبعدها: «إيه اللي يساعدني لما أحس كده؟»\n"
            "الهدف التربوي: التعبير عن المشاعر + تهدئة."
        )
    },
    {
        "id": "kb_102", "topic": "book_recommendations",
        "age_min": 4, "age_max": 12,
        "tags": ["كتاب", "كتب", "قراءة", "اقترح كتب"],
        "tip": (
            "اقتراح كتب حسب السن:\n"
            "- سن 4–7: كتب مصوّرة قصيرة عن الصداقة/المشاركة/الصدق.\n"
            "- سن 8–12: مغامرات قصيرة + قيم (مسؤولية/شجاعة/تعاون).\n"
            "بعد القراءة اسألي: «إيه أكتر موقف عجبك؟ وإيه الدرس؟»"
        )
    },
    {
        "id": "kb_103", "topic": "assessment_personality",
        "age_min": 4, "age_max": 18,
        "tags": ["تقييم", "assessment", "شخصية", "قيادي", "اجتماعي", "انطوائي"],
        "tip": (
            "نقدر نعمل تقييم شخصية (إرشادي) يساعدك تفهم ابنك. "
            "افتح /assessment/questions?age=X وبعدين ابعت الإجابات على /assessment/submit."
        )
    },
]

SPECIALISTS: List[Dict[str, Any]] = [
    {"id": "sp_001", "name": "د. مريم علي",    "title": "أخصائي إرشاد أسري",    "topics": ["teen_communication", "anger"],        "price_egp": 350, "rating": 4.8},
    {"id": "sp_002", "name": "د. أحمد حسن",    "title": "أخصائي نفسي",           "topics": ["bullying", "study_focus"],            "price_egp": 400, "rating": 4.6},
    {"id": "sp_003", "name": "أ. سارة محمود",  "title": "أخصائي تعديل سلوك",    "topics": ["screen_addiction", "anger"],          "price_egp": 300, "rating": 4.7},
]

SLOTS: List[Dict[str, Any]] = [
    {"slot_id": "sl_001", "specialist_id": "sp_001", "start": "2026-07-10T18:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_002", "specialist_id": "sp_001", "start": "2026-07-11T20:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_003", "specialist_id": "sp_002", "start": "2026-07-10T19:00:00+02:00", "duration_min": 45, "available": True},
    {"slot_id": "sl_004", "specialist_id": "sp_003", "start": "2026-07-12T21:00:00+02:00", "duration_min": 30, "available": True},
]


# ======================
# PYDANTIC MODELS
# ======================
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    user_id: str
    messages: List[ChatMessage]
    child_age: Optional[int] = None


class Card(BaseModel):
    type: Literal[
        "tip", "specialist", "booking", "refusal", "warning",
        "story", "game", "books",
        "assessment_question", "assessment_result",
        "confidence"
    ]
    title: str
    body: str
    meta: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):
    message_id: str
    reply: str
    cards: List[Card] = []


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


# ======================
# ROUTER MODEL (Gemini Structured Output)
# ======================
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
    in_scope: bool = Field(description="هل السؤال داخل نطاق رفيق؟")
    topic: AllowedTopic = Field(description="موضوع السؤال")
    action: AllowedAction = Field(description="الإجراء المطلوب")
    extracted_child_age: Optional[int] = Field(default=None, description="سن الطفل لو اتذكر")
    reason: str = Field(description="سبب مختصر")
    slot_id: Optional[str] = None
    specialist_id: Optional[str] = None


# ======================
# CONSTANTS
# ======================
PARENTING_TOPICS = {
    "teen_communication", "anger", "screen_addiction", "bullying", "study_focus",
    "siblings_jealousy", "parents_conflict", "lying", "general_parenting",
}
KIDS_CONTENT_TOPICS = {"kids_stories", "activities_games", "book_recommendations"}
ASSESSMENT_TOPIC = "assessment_personality"

OUT_OF_SCOPE_KEYWORDS = [
    "برمجة", "كود", "flutter", "android", "python", "java", "c++", "sql",
    "api", "backend", "front", "database", "debug", "algorithm"
]
MEDICAL_KEYWORDS = [
    "جرعة", "دواء", "حبوب", "مضاد", "تشخيص", "روشتة", "وصفة", "medication", "diagnosis"
]
KIDS_UNSAFE_KEYWORDS = ["انتحار", "إباحية", "اباحية", "سلاح", "مخدرات"]

RISK_HIGH_KEYWORDS = [
    "عايز أموت", "مش عايز أعيش", "هأذي نفسي", "انتحار", "هنتحر",
    "هقتل", "هموت", "أذي نفسي", "أؤذي نفسي"
]
RISK_MEDIUM_KEYWORDS = [
    "خوف شديد", "هلع", "نوبات", "قلق جامد", "اكتئاب", "حزين طول الوقت",
    "مش قادر", "مخنوق طول الوقت"
]


# ======================
# GUARDS & UTILS
# ======================
def hard_out_of_scope(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in OUT_OF_SCOPE_KEYWORDS)


def hard_medical(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in MEDICAL_KEYWORDS)


def kids_safety_guard(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in KIDS_UNSAFE_KEYWORDS)


def detect_risk_level(text: str) -> Literal["low", "medium", "high"]:
    t = text.lower()
    if any(k.lower() in t for k in RISK_HIGH_KEYWORDS):
        return "high"
    if any(k.lower() in t for k in RISK_MEDIUM_KEYWORDS):
        return "medium"
    return "low"


def extract_slot_id(text: str) -> Optional[str]:
    m = re.search(r"\bsl_\d{3}\b", text.lower())
    return m.group(0) if m else None


def detect_lang(text: str) -> str:
    return "ar" if re.findall(r'[\u0600-\u06FF]', text) else "en"


# ======================
# KB SEARCH v2 (Arabic normalize + tokenize + scoring)
# ======================
_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")
_AR_PUNCT = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)

_AR_STOPWORDS = {
    "في", "من", "على", "عن", "الى", "إلى", "هو", "هي", "ده", "دي", "دا",
    "انا", "انت", "انتي", "احنا", "هم"
}


def _ar_normalize(text: str) -> str:
    if not text:
        return ""
    t = _AR_DIACRITICS.sub("", text.strip())
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = t.replace("ؤ", "و").replace("ئ", "ي").replace("ـ", "")
    t = _AR_PUNCT.sub(" ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _tokenize(text: str) -> List[str]:
    toks = [w for w in _ar_normalize(text).split() if len(w) >= 2]
    return [x for x in toks if x not in _AR_STOPWORDS]


def _score_item(q_tokens: List[str], item: Dict[str, Any]) -> int:
    hay_tags = _ar_normalize(" ".join(item.get("tags", [])))
    hay_tip = _ar_normalize(item.get("tip", ""))
    hay_all = (hay_tags + " " + hay_tip).strip()
    if not q_tokens:
        return 1
    score = 0
    for tok in q_tokens:
        if tok in hay_tags:
            score += 6
        if tok in hay_tip:
            score += 4
    if all(tok in hay_all for tok in q_tokens[:3]):
        score += 6
    for tok in q_tokens:
        if len(tok) >= 4 and any(tok[:4] in h for h in [hay_tags, hay_tip]):
            score += 1
    return score


class KbSearchResult(BaseModel):
    tips: List[Dict[str, Any]] = []
    matched: bool = False
    match_count: int = 0
    used_default: bool = False


def kb_search_v2(topic: str, query: str, age: Optional[int]) -> KbSearchResult:
    q_tokens = _tokenize(query or "")
    scored: List[Tuple[int, Dict[str, Any]]] = []

    for item in KB:
        if topic and item["topic"] != topic:
            continue
        if age is not None and not (item["age_min"] <= age <= item["age_max"]):
            continue
        s = _score_item(q_tokens, item)
        if q_tokens:
            if s > 0:
                scored.append((s, item))
        else:
            scored.append((s, item))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [it for _, it in scored[:3]]
        matched = True if q_tokens and scored[0][0] >= 6 else (not bool(q_tokens))
        return KbSearchResult(tips=top, matched=matched, match_count=len(scored), used_default=not bool(q_tokens))

    if topic in PARENTING_TOPICS:
        return KbSearchResult(tips=[], matched=False, match_count=0, used_default=False)

    defaults = [x for x in KB if x["topic"] == topic][:2]
    if defaults:
        return KbSearchResult(tips=defaults[:3], matched=False, match_count=0, used_default=True)

    return KbSearchResult(tips=[], matched=False, match_count=0, used_default=False)


# ======================
# SPECIALISTS / SLOTS / BOOKING
# ======================
def recommend_specialists(topic: str) -> List[Dict[str, Any]]:
    rec = [s for s in SPECIALISTS if topic in s["topics"]]
    rec.sort(key=lambda x: (-x["rating"], x["price_egp"]))
    return rec[:3] if rec else SPECIALISTS[:2]


def available_slots(specialist_id: str) -> List[Dict[str, Any]]:
    return [sl for sl in SLOTS if sl["specialist_id"] == specialist_id and sl["available"]][:3]


def sync_slots_with_booked(conn):
    """Sync in-memory SLOTS availability with DB appointments."""
    cur = conn.cursor()
    cur.execute("SELECT slot_id FROM appointments WHERE status != 'cancelled'")
    booked = {row[0] for row in cur.fetchall()}
    for sl in SLOTS:
        if sl["slot_id"] in booked:
            sl["available"] = False


def book_slot(conn, user_id: str, specialist_id: str, slot_id: str) -> Dict[str, Any]:
    # Verify slot is still available in DB first
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM appointments WHERE slot_id=%s AND status != 'cancelled'",
        (slot_id,)
    )
    already_booked = cur.fetchone()[0] > 0
    if already_booked:
        raise ValueError("Slot not available")

    # Check in-memory SLOTS list
    slot = next((s for s in SLOTS if s["slot_id"] == slot_id and s["specialist_id"] == specialist_id), None)
    if not slot or not slot["available"]:
        raise ValueError("Slot not available")

    appt_id = "ap_" + uuid.uuid4().hex[:8]
    cur.execute(
        """
        INSERT INTO appointments (appointment_id, user_id, specialist_id, slot_id, status)
        VALUES (%s, %s, %s, %s, 'pending')
        """,
        (appt_id, user_id, specialist_id, slot_id)
    )
    conn.commit()
    slot["available"] = False

    return {
        "appointment_id": appt_id,
        "user_id": user_id,
        "specialist_id": specialist_id,
        "slot_id": slot_id,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }


# ======================
# LANGUAGE DETECTION
# ======================
def detect_lang(text: str) -> Literal["ar", "en"]:
    """Detect if text is Arabic or English."""
    ar_chars = len(re.findall(r'[\u0600-\u06FF]', text))
    en_chars = len(re.findall(r'[a-zA-Z]', text))
    return "ar" if ar_chars >= en_chars else "en"


# Bilingual static strings
_STRINGS: Dict[str, Dict[str, str]] = {
    "out_of_scope_reply": {
        "ar": "أنا بوت (رفيق) متخصص في دعم الأسرة والتواصل بين الأهل والأبناء، ومش بقدر أساعد في طلبات البرمجة/الأدوية/التشخيص.",
        "en": "I'm Rafiq, a family support assistant. I can't help with programming, medication, or medical diagnosis requests.",
    },
    "out_of_scope_card": {
        "ar": "اسألي عن: مراهقة، عصبية، موبايل، تنمر، مذاكرة، قصص للأطفال، ألعاب تربوية، تقييم شخصية الطفل…",
        "en": "Ask about: teen communication, anger, screen time, bullying, studying, kids stories, educational games, or personality assessment.",
    },
    "gemini_disabled": {
        "ar": "ميزة الشات غير مفعّلة حاليًا. التقييم (Assessment) والـ KB والـ Memory شغالين ✅",
        "en": "Chat feature is currently disabled. Assessment, KB, and Memory are still working ✅",
    },
    "risk_high": {
        "ar": "أنا قلقان/ة عليك جدًا. لو في خطر فوري، اتواصلي فورًا مع شخص كبير موثوق قريب منك، ولو الموضوع عاجل اتصلي بخدمات الطوارئ في بلدك.",
        "en": "I'm very concerned about you. If there's immediate danger, please reach out to a trusted adult near you, or call emergency services in your country.",
    },
    "risk_high_card": {
        "ar": "في الحالات العاجلة لازم تدخل إنسان/مختص فورًا. رفيق هنا للدعم العام فقط.",
        "en": "In urgent cases, a human specialist must intervene immediately. Rafiq is here for general support only.",
    },
    "scope_refusal": {
        "ar": "أنا بوت (رفيق) متخصص في دعم الأسرة. سؤالك ده خارج نطاق رفيق. اسألي عن مشكلة أسرية/تربوية وأنا أساعدك فورًا ✅",
        "en": "I'm Rafiq, a family support assistant. Your question is outside my scope. Ask me about a parenting or family issue and I'll help right away ✅",
    },
    "kids_safety": {
        "ar": "خلّينا نخلي المحتوى مناسب للأطفال 🙏 قوليلي سن الطفل وعايزين قصة/لعبة عن (الصدق/المشاركة/الشجاعة/الاحترام)؟",
        "en": "Let's keep the content child-appropriate 🙏 Tell me the child's age and what theme you'd like (honesty/sharing/courage/respect)?",
    },
    "missing_slot": {
        "ar": "تمام، ابعتي رقم الموعد بالشكل ده: (احجز sl_001).",
        "en": "Please send the slot number like this: (book sl_001).",
    },
    "slot_unavailable": {
        "ar": "الموعد ده مش متاح دلوقتي. اختاري ميعاد تاني من اللي ظاهر.",
        "en": "This slot is no longer available. Please choose another slot from the list.",
    },
    "low_conf_followup": {
        "ar": "حاسّة إن الموضوع مُتعب ومحتاج نفهمه صح قبل ما أدي خطوات محددة. ",
        "en": "I sense this topic needs more context before I give specific advice. ",
    },
    "low_conf_suffix": {
        "ar": " ولو تقدري احكيلي موقف واحد حصل قريب.",
        "en": " And if you can, tell me about one recent situation that happened.",
    },
    "verify_fallback": {
        "ar": "أنا معاكِ ✅ بس خلّيني أسألك سؤال صغير: ",
        "en": "I'm here for you ✅ Let me ask one small question: ",
    },
    "assessment_result_title": {
        "ar": "نتيجة تقييم شخصية الطفل (إرشادي)",
        "en": "Child Personality Assessment Result (Indicative)",
    },
    "assessment_note": {
        "ar": "النتيجة إرشادية وليست تشخيصًا. الشخصية بتتغير حسب العمر والبيئة.",
        "en": "This result is indicative, not a diagnosis. Personality changes with age and environment.",
    },
    "booking_success": {
        "ar": "تم الحجز ✅ رقم الحجز: ",
        "en": "Booking confirmed ✅ Booking ID: ",
    },
}


def t(key: str, lang: str) -> str:
    """Get bilingual string."""
    return _STRINGS.get(key, {}).get(lang, _STRINGS.get(key, {}).get("ar", ""))


# ======================
# MEMORY (PostgreSQL)
# ======================
def ensure_user_exists(conn, user_id: str):
    """Upsert user row so FK constraints never fail."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, child_age, notes)
        VALUES (%s, NULL, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id, json.dumps([]))
    )
    conn.commit()


def get_memory(conn, user_id: str) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT notes, child_age FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        return {"child_age": None, "topics": {}, "notes": [], "last_summary": ""}
    raw_notes = row[0]
    if isinstance(raw_notes, str):
        try:
            notes = json.loads(raw_notes)
        except Exception:
            notes = []
    else:
        notes = raw_notes or []
    return {"child_age": row[1], "notes": notes, "topics": {}, "last_summary": ""}


def update_memory(conn, user_id: str, topic: str, child_age: Optional[int], note: str = ""):
    ensure_user_exists(conn, user_id)
    cur = conn.cursor()
    cur.execute("SELECT notes FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()

    compact = re.sub(r"\s+", " ", (note or "")).strip()[:160]

    if row:
        raw = row[0]
        notes = json.loads(raw) if isinstance(raw, str) else (raw or [])
        if compact:
            notes.append(compact)
            notes = notes[-20:]
        cur.execute(
            """
            UPDATE users
            SET notes=%s, child_age=COALESCE(%s, child_age), updated_at=NOW()
            WHERE user_id=%s
            """,
            (json.dumps(notes), child_age, user_id)
        )
    else:
        notes = [compact] if compact else []
        cur.execute(
            "INSERT INTO users (user_id, child_age, notes) VALUES (%s, %s, %s)",
            (user_id, child_age, json.dumps(notes))
        )
    conn.commit()


# ======================
# ANALYTICS (PostgreSQL)
# ======================
def log_event(conn, user_id: str, event_type: str, value: str = "", meta: Dict = None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO analytics (event_id, user_id, event_type, value)
        VALUES (%s, %s, %s, %s)
        """,
        ("ev_" + uuid.uuid4().hex[:10], user_id, event_type, value[:300])
    )
    conn.commit()


# ======================
# FOLLOW-UP QUESTIONS
# ======================
FOLLOW_UP_BANK: Dict[str, List[str]] = {
    "anger":               ["العصبية بتظهر إمتى أكتر؟ (قبل النوم/بعد المدرسة/وقت الموبايل)", "في آخر مرة اتعصب، إيه كان السبب قبلها بدقيقة؟"],
    "screen_addiction":    ["بيستخدم الشاشة كام ساعة تقريبًا؟ وعلى إيه أكتر (يوتيوب/ألعاب/تيك توك)؟", "هل في وقت معين بيقاوم فيه الإغلاق أكتر؟"],
    "teen_communication":  ["إيه أكتر وقت بيكون فيه هادي وقابل للكلام؟", "المشكلة إنه مش بيرد ولا بيرد بعصبية؟"],
    "bullying":            ["التنمر بيحصل فين أكتر؟ (فصل/باص/نادي)", "هل في حد بالغ في المدرسة يثق فيه الطفل؟"],
    "study_focus":         ["بيذاكر قد إيه قبل ما يتشتت؟", "أكتر مادة بتعمل مقاومة عنده؟"],
    "kids_stories":        ["سن الطفل قد إيه عشان أختار قصة مناسبة؟", "تحبي القصة تكون عن (الصدق/المشاركة/الشجاعة/الاحترام)؟"],
    "activities_games":    ["تحبي نشاط هادي ولا حركة؟", "عندكم أدوات بسيطة زي ورق/أقلام/مكعبات؟"],
    "book_recommendations":["سن الطفل قد إيه وبيحب أنهي نوع قصص؟", "تحبي كتب قيم وسلوك ولا مغامرات؟"],
    "assessment_personality": ["تحبي نبدأ بتقييم سريع؟", "سن الطفل قد إيه عشان الأسئلة تبقى مناسبة؟"],
    "general_parenting":   ["سن الطفل قد إيه؟", "الموقف بيتكرر إمتى وأكتر حاجة بتسبق المشكلة إيه؟"],
}


def pick_followups(topic: str) -> List[str]:
    return (FOLLOW_UP_BANK.get(topic) or ["ممكن تحكيلي موقف حصل قريب؟", "سن الطفل قد إيه؟"])[:2]


# ======================
# CONFIDENCE SCORING
# ======================
def compute_confidence(
    topic: str, kb_res: KbSearchResult,
    age: Optional[int], user_text: str,
    in_scope: bool, risk_level: str
) -> int:
    score = 40
    if in_scope and topic != "out_of_scope":
        score += 15
    if age is not None:
        score += 10
    if kb_res.matched:
        score += 25 + min(10, kb_res.match_count * 3)
    elif kb_res.used_default and topic in (KIDS_CONTENT_TOPICS | {ASSESSMENT_TOPIC}):
        score += 15
    else:
        score -= 10
    if len((user_text or "").split()) >= 10:
        score += 5
    if risk_level == "medium":
        score -= 10
    elif risk_level == "high":
        score -= 25
    return max(0, min(100, score))


# ======================
# EMPATHY REFLECT
# ======================
EMPATHY_MAP = {
    "anger":                 "واضح إن الموضوع ده متعبك وبيستنزف أعصابك.",
    "screen_addiction":      "حاسّة بقلقك من موضوع الشاشات وتأثيره عليه.",
    "teen_communication":    "واضح إن قلة التواصل مضايقاكي وبتوجع.",
    "bullying":              "طبيعي تقلقي جدًا لما تحسي إن ابنك بيتأذى.",
    "study_focus":           "الإحساس بالحيرة مع المذاكرة بيكون مرهق فعلًا.",
    "kids_stories":          "تحبّي تعملي حاجة لطيفة ومناسبة لسنّه.",
    "activities_games":      "واضح إنك بتحاولي تملّي وقته بحاجة مفيدة.",
    "assessment_personality":"حلو إنك عايزة تفهمي شخصيته أكتر.",
    "general_parenting":     "الأمومة مليانة مواقف بتخلينا نحتار."
}


def empathy_reflect(user_text: str, topic: str, risk_level: str) -> str:
    t = user_text.strip()
    reflection = (t[:77] + "...") if len(t) > 80 else t
    empathy = EMPATHY_MAP.get(topic, "حاسة بيكي، والموقف ده مش سهل.")
    if risk_level == "medium":
        empathy += " خلّينا نمشي بهدوء ونفهم الصورة كاملة."
    elif risk_level == "high":
        empathy += " أهم حاجة دلوقتي الأمان والدعم."
    return f"{empathy}\n\nإنتِ بتقولي: «{reflection}»\n"


# ======================
# ASSESSMENT
# ======================
TRAITS = ["leadership", "sociability", "empathy", "self_control", "focus", "curiosity", "adaptability", "sensitivity"]

ASSESSMENT_QUESTIONS: List[Dict[str, Any]] = [
    {"id": "a46_1",  "text": "بيقدر يهدى بعد الزعل بمساعدة بسيطة (حضن/كلمة).",           "age_min": 4,  "age_max": 6,  "weights": {"self_control": 2}},
    {"id": "a46_2",  "text": "بيشارك لعبه أو أدواته مع غيره.",                              "age_min": 4,  "age_max": 6,  "weights": {"sociability": 2, "empathy": 1}},
    {"id": "a46_3",  "text": "بيسمع تعليمات بسيطة من خطوتين.",                              "age_min": 4,  "age_max": 6,  "weights": {"focus": 2}},
    {"id": "a46_4",  "text": "لو حد زعل منه، بيقبل يصلّح أو يعتذر (حتى لو بكلمة).",       "age_min": 4,  "age_max": 6,  "weights": {"empathy": 2}},
    {"id": "a710_1", "text": "بيكمل واجب بسيط قبل ما يسيبه.",                               "age_min": 7,  "age_max": 10, "weights": {"focus": 2, "self_control": 1}},
    {"id": "a710_2", "text": "بيحاول يحل خلاف مع أصحابه بالكلام.",                          "age_min": 7,  "age_max": 10, "weights": {"self_control": 2, "empathy": 1}},
    {"id": "a710_3", "text": "بيحب يتعلم حاجة جديدة ويجرب.",                                "age_min": 7,  "age_max": 10, "weights": {"curiosity": 2}},
    {"id": "a710_4", "text": "بيتقبل الخسارة في لعبة من غير نوبة كبيرة.",                  "age_min": 7,  "age_max": 10, "weights": {"self_control": 2}},
    {"id": "q1",     "text": "ابنك/بنتك يحب يبادر ويقترح أفكار جديدة.",                     "age_min": 11, "age_max": 18, "weights": {"leadership": 2, "curiosity": 1}},
    {"id": "q2",     "text": "بيحب يكون وسط الناس ويعمل صحاب بسرعة.",                       "age_min": 11, "age_max": 18, "weights": {"sociability": 2}},
    {"id": "q3",     "text": "لو حد زعل، بيحس بيه وبيحاول يواسيه.",                         "age_min": 11, "age_max": 18, "weights": {"empathy": 2}},
    {"id": "q4",     "text": "لما يتعصب بيقدر يهدي نفسه بسرعة.",                            "age_min": 11, "age_max": 18, "weights": {"self_control": 2}},
    {"id": "q5",     "text": "بيكمل مهامه للنهاية حتى لو زهق.",                             "age_min": 11, "age_max": 18, "weights": {"focus": 2, "self_control": 1}},
    {"id": "q6",     "text": "بيسأل أسئلة كتير وبيحب يعرف (ليه؟ وإزاي؟).",                 "age_min": 11, "age_max": 18, "weights": {"curiosity": 2}},
    {"id": "q7",     "text": "بيتقبل التغيير بسرعة (مكان/نظام جديد).",                      "age_min": 11, "age_max": 18, "weights": {"adaptability": 2}},
    {"id": "q8",     "text": "بيتضايق بسرعة من النقد أو بيتوتر من المواقف.",                "age_min": 11, "age_max": 18, "weights": {"sensitivity": 2}},
    {"id": "q9",     "text": "بيحب يكون مسئول (ينظم/يقود لعبة/يوزع أدوار).",               "age_min": 11, "age_max": 18, "weights": {"leadership": 2, "focus": 1}},
    {"id": "q10",    "text": "لما يحصل خلاف، بيحاول يحل بهدوء بدل ما يزعق.",               "age_min": 11, "age_max": 18, "weights": {"self_control": 2, "empathy": 1}},
]

ARCHETYPES = [
    {"id": "leader",      "name": "القائد",           "need": "مساحة مسؤولية + قواعد واضحة",               "profile": {"leadership": 80, "focus": 60, "sociability": 50}},
    {"id": "explorer",    "name": "المستكشف",          "need": "تجارب جديدة + مشاريع صغيرة",                "profile": {"curiosity": 80, "adaptability": 60}},
    {"id": "thinker",     "name": "المفكر",            "need": "وقت هادئ + تحديات ذهنية",                   "profile": {"focus": 75, "curiosity": 60, "sociability": 35}},
    {"id": "helper",      "name": "المُسانِد",          "need": "تقدير مشاعره + فرص مساعدة",                 "profile": {"empathy": 80, "sociability": 55}},
    {"id": "peacemaker",  "name": "صانع السلام",       "need": "تعليم حدود + تشجيع التعبير",                "profile": {"empathy": 70, "self_control": 70}},
    {"id": "energetic",   "name": "الحركي/النشيط",     "need": "تفريغ طاقة + قواعد ثابتة",                  "profile": {"sociability": 70, "curiosity": 55, "self_control": 40}},
    {"id": "sensitive",   "name": "الحساس",            "need": "طمأنة + تقليل ضغط + روتين آمن",             "profile": {"sensitivity": 80, "empathy": 60}},
    {"id": "independent", "name": "المستقل",           "need": "اختيارات + احترام المساحة + متابعة ذكية",   "profile": {"leadership": 55, "sociability": 30, "focus": 55}},
    {"id": "planner",     "name": "المنظم",            "need": "جداول بسيطة + أهداف صغيرة + مكافأة معنوية", "profile": {"focus": 80, "self_control": 70}},
    {"id": "challenger",  "name": "المُجادِل",          "need": "قواعد قليلة وواضحة + تفاوض + عواقب ثابتة", "profile": {"leadership": 65, "self_control": 35, "sensitivity": 45}},
]


def get_assessment_questions(child_age: Optional[int]) -> List[Dict[str, Any]]:
    if child_age is None:
        return ASSESSMENT_QUESTIONS
    return [q for q in ASSESSMENT_QUESTIONS if q["age_min"] <= child_age <= q["age_max"]]


def compute_personality_profile(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    qs = {q["id"]: q for q in get_assessment_questions(child_age)}
    raw = {t: 0.0 for t in TRAITS}
    raw_max = {t: 0.0 for t in TRAITS}

    for a in answers:
        qid = a.get("question_id") or a.get("id")
        v = a.get("value")
        if qid not in qs or v is None:
            continue
        v = max(1, min(5, int(v)))
        for t, w in qs[qid]["weights"].items():
            raw[t] += v * w
            raw_max[t] += 5 * w

    bs = behavior_signals or {}
    raw["focus"] += max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2
    raw_max["focus"] += 6
    raw["empathy"] += int(bs.get("helps_others", 0)) * 2
    raw_max["empathy"] += 6

    def _norm(r, rm): return max(0, min(100, int(round(r / rm * 100)))) if rm > 0 else 0
    scores = {t: _norm(raw[t], raw_max[t]) for t in TRAITS}

    def sim(arch_profile): return sum(100 - abs(scores.get(t, 50) - v) for t, v in arch_profile.items()) / max(1, len(arch_profile))
    ranked = sorted([{"id": a["id"], "name": a["name"], "match": int(round(sim(a["profile"]))), "need": a["need"]} for a in ARCHETYPES], key=lambda x: x["match"], reverse=True)

    return {
        "child_age": child_age,
        "trait_scores": scores,
        "top_traits": sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3],
        "low_traits": sorted(scores.items(), key=lambda kv: kv[1])[:2],
        "possible_personalities": ranked[:5],
        "note": "النتيجة إرشادية وليست تشخيصًا. الشخصية بتتغير حسب العمر والبيئة."
    }


def compute_assessment_confidence(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    qs = get_assessment_questions(child_age)
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

    score = int(round((valid / total) * 65)) if total > 0 else 0
    notes = [f"coverage={int(round(valid/total*100))}%" if total > 0 else "no_questions"]
    if child_age is not None:
        score += 15
        notes.append("age_provided")
    if behavior_signals:
        score += 10
        notes.append("behavior_signals")
    if valid < max(3, total // 3 if total else 3):
        score = max(0, score - 15)
        notes.append("low_answer_count_penalty")
    return {"confidence": max(0, min(100, score)), "valid_answers": valid, "total_questions": total, "notes": notes}


# ======================
# GEMINI CALLS
# ======================
def _require_gemini():
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini disabled: missing GEMINI_API_KEY")


def gemini_route_decision(user_text: str, history: List[ChatMessage], fallback_age: Optional[int]) -> RouteDecision:
    _require_gemini()
    system = (
        "أنت Router خاص بتطبيق (رفيق). "
        "رفيق يجاوب فقط على: التواصل الأسري، التربية، المراهقين، العناد، العصبية، الشاشات، التنمر، المذاكرة، "
        "الخلافات الأسرية، قصص للأطفال، ألعاب وأنشطة تربوية، اقتراح كتب مناسبة للسن، وتقييم شخصية الطفل.\n"
        "ممنوع: البرمجة/التقنية/تشخيص/أدوية.\n"
        "لو السؤال خارج النطاق => action=refuse_out_of_scope و in_scope=false.\n"
        "لو المستخدم كتب (احجز sl_001) استخرج slot_id.\n"
        "اخرج JSON فقط حسب الـschema."
    )
    short_history = "\n".join([f"{m.role}: {m.content}" for m in history[-6:]])
    prompt = f"System: {system}\n\nConversation:\n{short_history}\n\nUser message:\n{user_text}\n\nKnown child age (if any): {fallback_age}"

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
                             extracted_child_age=None, reason=f"Failed to parse routing JSON. raw={resp.text[:200]}")


def gemini_compose_answer(
    user_text: str, topic: str, tips: List[Dict[str, Any]],
    specialists: List[Dict[str, Any]], slots: List[Dict[str, Any]],
    memory: Dict[str, Any], followups: List[str],
    confidence: int, risk_level: str
) -> str:
    _require_gemini()
    payload = {"topic": topic, "tips": tips, "specialists": specialists, "slots": slots,
               "memory": memory, "followups": followups, "confidence": confidence, "risk_level": risk_level}
    system = (
        "أنت مساعد توعوي داخل تطبيق (رفيق) لدعم الأسرة وتقوية التواصل.\n"
        "قواعد صارمة: لا تشخيص ولا أدوية ولا برمجة.\n"
        "استخدم فقط المعلومات المعطاة في ALLOWED DATA.\n"
        "لو confidence < 65 أو tips فاضية: رد احتوائي قصير + سؤال متابعة واحد فقط.\n"
        "اكتب بالعربي العامي المحترم.\n"
        "لو confidence عالي: 3 نقاط عملية + سؤال متابعة + اقتراح حجز لو مناسب."
    )
    prompt = f"{system}\n\nUSER QUESTION:\n{user_text}\n\nALLOWED DATA (JSON):\n{json.dumps(payload, ensure_ascii=False)}"
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=450),
    )
    return (resp.text or "").strip() or "ممكن تقوليلي تفاصيل أكتر؟"


def gemini_verify_answer(user_text: str, answer: str, allowed_payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_gemini()
    prompt = (
        f"راجع الرد التالي: هل خرج عن نطاق رفيق أو ذكر تشخيص/أدوية/برمجة؟\n"
        f"أخرج JSON فقط: {{\"ok\": true/false, \"reason\": \"مختصر\"}}\n\n"
        f"USER:\n{user_text}\n\nANSWER:\n{answer}\n\nALLOWED DATA:\n{json.dumps(allowed_payload, ensure_ascii=False)}"
    )
    r = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(response_mime_type="application/json", temperature=0, max_output_tokens=180)
    )
    try:
        data = json.loads(r.text)
        return {"ok": bool(data.get("ok", True)), "reason": (data.get("reason") or "").strip()}
    except Exception:
        return {"ok": True, "reason": ""}


# ======================
# ROUTES
# ======================

@app.get("/")
def home():
    return {"status": "Rafiq running 🚀", "version": "merged-final"}


@app.get("/health")
def health():
    return {
        "ok": True,
        "model": GEMINI_MODEL,
        "gemini_enabled": GEMINI_ENABLED,
        "verify": ENABLE_VERIFY,
        "db": bool(DATABASE_URL),
        "debug": DEBUG
    }


@app.get("/test_gemini")
def test_gemini():
    _require_gemini()
    r = client.models.generate_content(model=GEMINI_MODEL, contents="OK فقط")
    return {"text": r.text}


# ---------- KB ----------

@app.get("/kb/topics")
def kb_topics():
    topics = sorted({x["topic"] for x in KB})
    return {"topics": topics, "count": len(topics)}


@app.get("/kb/search")
def kb_search_api(topic: str, q: str = "", age: Optional[int] = None):
    res = kb_search_v2(topic=topic, query=q or "", age=age)
    return {"topic": topic, "age": age, "matched": res.matched,
            "match_count": res.match_count, "used_default": res.used_default, "tips": res.tips}


@app.post("/kb/add")
def kb_add(req: KbAddRequest):
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")
    new_id = "kb_" + uuid.uuid4().hex[:6]
    KB.append({"id": new_id, "topic": req.topic, "age_min": req.age_min,
               "age_max": req.age_max, "tags": req.tags, "tip": req.tip})
    return {"ok": True, "kb_id": new_id, "total": len(KB)}


# ---------- Memory ----------

@app.get("/memory/{user_id}")
def memory_get(user_id: str):
    conn = get_conn()
    data = get_memory(conn, user_id)
    conn.close()
    return {"user_id": user_id, "memory": data}


# ---------- Assessment ----------

@app.get("/assessment/questions")
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return {
        "child_age": age,
        "scale": {"min": 1, "max": 5, "labels": {"1": "أبدًا", "2": "نادرًا", "3": "أحيانًا", "4": "غالبًا", "5": "دائمًا"}},
        "questions": [{"id": q["id"], "text": q["text"]} for q in qs]
    }


@app.post("/assessment/submit")
def assessment_submit(req: AssessmentSubmitReq):
    # Detect language from first answer text or default to Arabic
    lang = detect_lang(req.user_id) if req.user_id else "ar"
    # Better: detect from any string field available
    sample = " ".join([str(a.get("value", "")) for a in (req.answers or [])])
    lang = detect_lang(sample) if sample.strip() else "ar"

    profile = compute_personality_profile(req.answers, req.child_age, req.behavior_signals)
    assess_conf = compute_assessment_confidence(req.answers, req.child_age, req.behavior_signals)

    conn = get_conn()

    # FIX: ensure user exists before any FK-constrained insert
    ensure_user_exists(conn, req.user_id)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO assessments (user_id, child_age, assessment_confidence, result, created_at)
        VALUES (%s, %s, %s, %s, NOW())
        """,
        (req.user_id, req.child_age, assess_conf["confidence"], json.dumps(profile))
    )
    conn.commit()

    note = "Assessment submitted" if lang == "en" else "تم إرسال التقييم"
    update_memory(conn, req.user_id, "assessment_personality", req.child_age, note=note)
    log_event(conn, req.user_id, "assessment_submit", value=f"confidence={assess_conf['confidence']}")
    conn.close()

    # Bilingual result body
    if lang == "en":
        personalities_str = "\n".join([f"- {p['name']} (match {p['match']}%) — needs: {p['need']}" for p in profile["possible_personalities"]])
        top_str    = "\n".join([f"- {tr}: {v}%" for tr, v in profile["top_traits"]])
        low_str    = "\n".join([f"- {tr}: {v}%" for tr, v in profile["low_traits"]])
        result_body = (
            f"Closest personality types:\n{personalities_str}\n\n"
            f"Strongest traits:\n{top_str}\n\n"
            f"Traits needing support:\n{low_str}\n\n"
            f"Note: {t('assessment_note', 'en')}"
        )
        conf_title = "Assessment Confidence Score"
    else:
        personalities_str = "\n".join([f"- {p['name']} (تطابق {p['match']}%) — يحتاج: {p['need']}" for p in profile["possible_personalities"]])
        top_str    = "\n".join([f"- {tr}: {v}%" for tr, v in profile["top_traits"]])
        low_str    = "\n".join([f"- {tr}: {v}%" for tr, v in profile["low_traits"]])
        result_body = (
            f"أقرب الشخصيات المحتملة:\n{personalities_str}\n\n"
            f"أقوى السمات:\n{top_str}\n\n"
            f"سمات تحتاج دعم:\n{low_str}\n\n"
            f"ملاحظة: {t('assessment_note', 'ar')}"
        )
        conf_title = "درجة ثقة التقييم"

    cards = [
        Card(
            type="assessment_result",
            title=t("assessment_result_title", lang),
            body=result_body,
            meta=profile
        ),
        Card(
            type="confidence",
            title=conf_title,
            body=f"{assess_conf['confidence']}%",
            meta=assess_conf
        )
    ]
    return {"ok": True, "profile": profile, "assessment_confidence": assess_conf["confidence"],
            "assessment_meta": assess_conf, "cards": [c.model_dump() for c in cards]}


@app.get("/assessment/{user_id}")
def get_assessments(user_id: str):
    conn = get_conn()
    cur = conn.cursor()
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


# ---------- Specialists & Slots ----------

@app.get("/specialists")
def specialists_list():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, title, topics, price_egp, rating FROM specialists ORDER BY rating DESC")
    rows = cur.fetchall()
    conn.close()
    if rows:
        return {"specialists": [{"id": r[0], "name": r[1], "title": r[2], "topics": r[3], "price_egp": float(r[4]), "rating": float(r[5])} for r in rows]}
    # fallback to in-memory
    return {"specialists": sorted(SPECIALISTS, key=lambda x: -x["rating"])}


@app.get("/slots/{specialist_id}")
def get_slots(specialist_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT slot_id, start_time, duration_min, available FROM slots WHERE specialist_id=%s ORDER BY start_time",
        (specialist_id,)
    )
    rows = cur.fetchall()
    conn.close()
    if rows:
        return {"slots": [{"slot_id": r[0], "start_time": r[1], "duration_min": r[2], "available": r[3]} for r in rows]}
    # fallback to in-memory
    return {"slots": [s for s in SLOTS if s["specialist_id"] == specialist_id]}


# ---------- Appointments ----------

@app.post("/appointments/book")
def book(req: BookingReq):
    conn = get_conn()
    sync_slots_with_booked(conn)
    try:
        appt = book_slot(conn, req.user_id, req.specialist_id, req.slot_id)
        log_event(conn, req.user_id, "booking_created", value=req.slot_id)
        conn.close()
        return {"ok": True, "appointment": appt}
    except ValueError:
        conn.close()
        raise HTTPException(status_code=400, detail="Slot not available")


@app.get("/appointments/{user_id}")
def get_appointments(user_id: str, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT appointment_id, specialist_id, slot_id, status, created_at
        FROM appointments WHERE user_id=%s ORDER BY created_at DESC LIMIT %s
        """,
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


# ---------- Analytics ----------

@app.post("/analytics/event")
def analytics_event(req: AppEventRequest):
    conn = get_conn()
    log_event(conn, req.user_id, req.event_name, value=json.dumps(req.meta)[:300])
    conn.close()
    return {"ok": True}


@app.get("/analytics/summary")
def analytics_summary():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT event_type, COUNT(*) FROM analytics GROUP BY event_type")
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM analytics")
    total = cur.fetchone()[0]
    conn.close()
    return {"total_events": total, "by_type": {r[0]: r[1] for r in rows}}


@app.get("/analytics/user/{user_id}")
def analytics_user(user_id: str):
    conn = get_conn()
    cur = conn.cursor()
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


# ---------- Feedback ----------

@app.post("/feedback")
def feedback(req: FeedbackReq):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO feedback (user_id, message_id, rating, comment, topic, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        """,
        (req.user_id, req.message_id, req.rating, req.comment, req.topic)
    )
    conn.commit()
    if req.comment:
        update_memory(conn, req.user_id, req.topic or "general_parenting", None,
                      note=f"FEEDBACK:{req.rating}:{req.comment}")
    log_event(conn, req.user_id, "feedback", value=f"{req.rating}:{req.message_id}")
    conn.close()
    return {"ok": True}


# ---------- Chat History ----------

@app.get("/chat/{user_id}")
def get_chat_history(user_id: str, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT message_id, message, response, created_at
        FROM chat_messages WHERE user_id=%s ORDER BY created_at DESC LIMIT %s
        """,
        (user_id, max(1, min(200, limit)))
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "messages": [
            {"message_id": r[0], "user_message": r[1], "bot_reply": r[2],
             "created_at": r[3].isoformat() if r[3] else None}
            for r in rows
        ]
    }


# ======================
# CHAT (Main Endpoint)
# ======================
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages is empty")

    message_id = "msg_" + uuid.uuid4().hex[:10]
    user_text = req.messages[-1].content.strip()
    lang = detect_lang(user_text)   # "ar" or "en" — drives ALL reply strings

    # Hard guards (no DB needed)
    if hard_out_of_scope(user_text) or hard_medical(user_text):
        return ChatResponse(
            message_id=message_id,
            reply=t("out_of_scope_reply", lang),
            cards=[Card(type="refusal",
                        title="Out of Rafiq scope" if lang == "en" else "خارج نطاق رفيق",
                        body=t("out_of_scope_card", lang))]
        )

    # Gemini check
    if not GEMINI_ENABLED or client is None:
        return ChatResponse(
            message_id=message_id,
            reply=t("gemini_disabled", lang),
            cards=[Card(type="warning",
                        title="Gemini disabled" if lang == "en" else "Gemini غير مفعّل",
                        body="Add GEMINI_API_KEY to Environment Variables." if lang == "en"
                             else "ضيفي GEMINI_API_KEY في Environment Variables.")]
        )

    conn = get_conn()

    slot_from_text = extract_slot_id(user_text)
    wants_booking = any(x in user_text for x in ["احجز", "حجز", "استشارة", "مختص", "دكتور", "book", "specialist", "appointment"])
    risk_level = detect_risk_level(user_text)

    # High risk → immediate response
    if risk_level == "high":
        log_event(conn, req.user_id, "risk_high", value=user_text[:200])
        conn.close()
        return ChatResponse(
            message_id=message_id,
            reply=t("risk_high", lang),
            cards=[Card(type="warning",
                        title="Important" if lang == "en" else "مهم جدًا",
                        body=t("risk_high_card", lang),
                        meta={"risk_level": "high"})]
        )

    # Route with Gemini
    try:
        decision = gemini_route_decision(user_text, req.messages, req.child_age)
    except HTTPException:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=502, detail=f"Gemini route failed: {str(e)}")

    if slot_from_text and decision.topic != "out_of_scope":
        decision.action = "book_appointment"
        decision.slot_id = slot_from_text

    # Log analytics (ensure user exists first)
    ensure_user_exists(conn, req.user_id)
    log_event(conn, req.user_id, "chat_message", value=user_text[:300])

    # Out of scope
    if not decision.in_scope or decision.action == "refuse_out_of_scope" or decision.topic == "out_of_scope":
        conn.close()
        return ChatResponse(
            message_id=message_id,
            reply=t("scope_refusal", lang),
            cards=[Card(type="refusal",
                        title="Out of Rafiq scope" if lang == "en" else "خارج نطاق رفيق",
                        body=f"Reason: {decision.reason}" if lang == "en" else f"السبب: {decision.reason}")]
        )

    # Kids safety
    topic = decision.topic
    if topic in KIDS_CONTENT_TOPICS and kids_safety_guard(user_text):
        conn.close()
        return ChatResponse(
            message_id=message_id,
            reply=t("kids_safety", lang),
            cards=[Card(type="warning",
                        title="Child-appropriate content" if lang == "en" else "محتوى مناسب للأطفال",
                        body="Choose a safe, age-appropriate topic." if lang == "en"
                             else "اختاري موضوع آمن ومناسب للسن.")]
        )

    # Memory
    age = decision.extracted_child_age or req.child_age
    update_memory(conn, req.user_id, topic, age, note=user_text)
    mem = get_memory(conn, req.user_id)

    # Booking flow
    if decision.action == "book_appointment":
        slot_id = decision.slot_id
        specialist_id = decision.specialist_id
        if slot_id and not specialist_id:
            match = next((s for s in SLOTS if s["slot_id"] == slot_id), None)
            if match:
                specialist_id = match["specialist_id"]

        if not slot_id or not specialist_id:
            conn.close()
            return ChatResponse(
                message_id=message_id,
                reply=t("missing_slot", lang),
                cards=[Card(type="warning",
                            title="Missing booking data" if lang == "en" else "ناقص بيانات الحجز",
                            body="We need a slot_id like sl_001." if lang == "en"
                                 else "محتاجين slot_id زي sl_001.")]
            )

        sync_slots_with_booked(conn)
        try:
            appt = book_slot(conn, req.user_id, specialist_id, slot_id)
            sp = next((x for x in SPECIALISTS if x["id"] == specialist_id), None)
            log_event(conn, req.user_id, "booking_created", value=slot_id)
            conn.close()
            sp_name = sp["name"] if sp else specialist_id
            return ChatResponse(
                message_id=message_id,
                reply=f"{t('booking_success', lang)}{appt['appointment_id']}.",
                cards=[Card(type="booking",
                            title="Booking details" if lang == "en" else "تفاصيل الحجز",
                            body=f"Specialist: {sp_name}\nslot_id: {slot_id}" if lang == "en"
                                 else f"المختص: {sp_name}\nslot_id: {slot_id}",
                            meta=appt)]
            )
        except ValueError:
            conn.close()
            return ChatResponse(
                message_id=message_id,
                reply=t("slot_unavailable", lang),
                cards=[Card(type="warning",
                            title="Slot unavailable" if lang == "en" else "الموعد غير متاح",
                            body="Try a different slot_id." if lang == "en" else "جرّبي slot_id مختلف.")]
            )

    # Normal answer flow
    kb_res = kb_search_v2(topic=topic, query=user_text, age=age)
    tips = kb_res.tips

    show_specialists = wants_booking or decision.action in ["recommend_booking"] or risk_level == "medium"
    specialists = recommend_specialists(topic=topic) if show_specialists else []
    slots_list: List[Dict[str, Any]] = []
    if show_specialists and specialists:
        slots_list = available_slots(specialists[0]["id"])

    followups = pick_followups(topic)
    conf = compute_confidence(topic, kb_res, age, user_text, decision.in_scope, risk_level)

    # Low confidence → ask follow-up first
    if topic in PARENTING_TOPICS and not kb_res.matched and conf < 65:
        q = followups[0] if followups else ("How old is the child?" if lang == "en" else "سن الطفل قد إيه؟")
        conn.close()
        return ChatResponse(
            message_id=message_id,
            reply=t("low_conf_followup", lang) + q + t("low_conf_suffix", lang),
            cards=[
                Card(type="confidence",
                     title="Confidence score" if lang == "en" else "درجة الثقة (إرشادي)",
                     body=f"{conf}%", meta={"confidence": conf, "matched": kb_res.matched}),
                Card(type="warning",
                     title="Quick follow-up" if lang == "en" else "سؤال متابعة سريع",
                     body=q, meta={"followups": followups}),
            ]
        )

    # Compose with Gemini — inject lang in system prompt
    intro = empathy_reflect(user_text, topic, risk_level) if lang == "ar" else ""
    try:
        final_text = intro + gemini_compose_answer(
            user_text=user_text, topic=topic, tips=tips,
            specialists=specialists, slots=slots_list,
            memory=mem, followups=followups,
            confidence=conf, risk_level=risk_level,
            lang=lang
        )
    except HTTPException:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=502, detail=f"Gemini compose failed: {str(e)}")

    # Optional verify
    if ENABLE_VERIFY:
        allowed_payload = {"topic": topic, "tips": tips, "specialists": specialists, "slots": slots_list,
                           "memory": mem, "followups": followups, "confidence": conf, "risk_level": risk_level}
        verdict = gemini_verify_answer(user_text, final_text, allowed_payload)
        if not verdict.get("ok", True):
            fallback_q = followups[0] if followups else ("How old is the child?" if lang == "en" else "سن الطفل قد إيه؟")
            final_text = t("verify_fallback", lang) + fallback_q

    # Save to DB
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_messages (message_id, user_id, message, response) VALUES (%s, %s, %s, %s)",
        (message_id, req.user_id, user_text, final_text)
    )
    conn.commit()
    conn.close()

    # Build cards (bilingual titles)
    cards: List[Card] = []
    card_labels = {
        "tip":                ("Practical tip",             "نصيحة عملية"),
        "kids_stories":       ("Kids story",                "قصة للأطفال"),
        "activities_games":   ("Activity / game",           "لعبة/نشاط"),
        "book_recommendations":("Reading suggestion",       "اقتراح قراءة"),
        "assessment_personality":("Personality assessment", "تقييم شخصية الطفل"),
    }

    for tip_item in tips:
        if topic in card_labels:
            ctype = topic if topic != "tip" else "tip"
            ctitle = card_labels[topic][0 if lang == "en" else 1]
        else:
            ctype, ctitle = "tip", card_labels["tip"][0 if lang == "en" else 1]
        # fix ctype for valid Card types
        ctype_map = {"kids_stories": "story", "activities_games": "game",
                     "book_recommendations": "books", "assessment_personality": "assessment_question"}
        ctype = ctype_map.get(ctype, "tip")
        cards.append(Card(type=ctype, title=ctitle, body=tip_item["tip"],
                          meta={"kb_id": tip_item["id"], "topic": tip_item["topic"], "age_used": age,
                                "matched": kb_res.matched, "used_default": kb_res.used_default}))

    cards.append(Card(
        type="confidence",
        title="Confidence score" if lang == "en" else "درجة الثقة (إرشادي)",
        body=f"{conf}%",
        meta={"confidence": conf, "matched": kb_res.matched, "risk_level": risk_level}
    ))

    if conf < 70 or (topic in PARENTING_TOPICS and not kb_res.matched):
        cards.append(Card(
            type="warning",
            title="Quick follow-up" if lang == "en" else "سؤال متابعة سريع",
            body="- " + "\n- ".join(followups[:1]),
            meta={"followups": followups}
        ))

    if show_specialists:
        for sp in specialists:
            cards.append(Card(
                type="specialist",
                title=f"{sp['name']} — {sp['title']}",
                body=f"Price: {sp['price_egp']} EGP | Rating: {sp['rating']}" if lang == "en"
                     else f"السعر: {sp['price_egp']} جنيه | التقييم: {sp['rating']}",
                meta={"specialist_id": sp["id"], "topics": sp["topics"]}
            ))

    if slots_list and show_specialists:
        if lang == "en":
            body = "\n".join([f"- {sl['slot_id']}: {sl['start']} ({sl['duration_min']} min)" for sl in slots_list])
            body += "\n\nTo book, send: book sl_001"
        else:
            body = "\n".join([f"- {sl['slot_id']}: {sl['start']} ({sl['duration_min']} دقيقة)" for sl in slots_list])
            body += "\n\nللحجز ابعتي: احجز sl_001"
        cards.append(Card(
            type="booking",
            title="Available slots" if lang == "en" else "مواعيد متاحة",
            body=body,
            meta={"slot_ids": [sl["slot_id"] for sl in slots_list],
                  "specialist_id": specialists[0]["id"] if specialists else None}
        ))

    return ChatResponse(message_id=message_id, reply=final_text, cards=cards)
