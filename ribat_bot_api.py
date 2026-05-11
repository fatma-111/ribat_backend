"""
ribat_bot_api.py
Ribat Chatbot API (Gemini) - Full
Backend-only FastAPI app:
- Parenting/Family support + Kids stories/games/books + Personality Assessment
Includes:
- User Memory (optional file persistence)
- Smart follow-ups
- Confidence scoring (REAL match-aware)
- Risk escalation (high/medium/low)
- Optional verifier
- Feedback loop
- ✅ Assessment confidence (separate, questionnaire-based)

✅ Updates in this version:
1) Gemini OPTIONAL (server runs even if GEMINI_API_KEY missing)
2) GET /kb/search (for mobile UI)
3) Assessment questions by age: 4–6, 7–10, 11–18
4) KB assessment item expanded to 4–18

✅ Patch:
5) Analytics + Appointments persisted to JSON files (not RAM-only)
6) kb_search_v2(): Arabic normalization + tokenization + scoring (no external libs)
7) /kb/search and /chat use kb_search_v2 by default

✅ Production-ready tweaks:
8) DATA_DIR default is "data" (instead of ".")
9) _safe_write_json prints errors only when RIBAT_DEBUG=1
10) Fix: remove stray typing import, clean startup load order, safer multi-instance booking load/sync
"""

from dotenv import load_dotenv
load_dotenv()
import google.generativeai as genai
import os
import json
import uuid
import re
from datetime import datetime
from typing import List, Optional, Dict, Any, Literal, Tuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None

# =======================
# CONFIG
# =======================
DEBUG = os.getenv("RIBAT_DEBUG", "0") == "1"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
genai.configure(api_key=GEMINI_API_KEY)
GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)

ADMIN_KEY = os.getenv("RIBAT_ADMIN_KEY", "change-me")
if ADMIN_KEY == "change-me":
    print("WARNING: RIBAT_ADMIN_KEY is default. Set it in ENV for production.")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Optional features
ENABLE_VERIFY = os.getenv("RIBAT_VERIFY_OUTPUT", "0") == "1"
PERSIST_MEMORY = os.getenv("RIBAT_PERSIST_MEMORY", "1") == "1"

# ✅ Production-ready default: keep data files in /data
DATA_DIR = os.getenv("RIBAT_DATA_DIR", "data")

MEMORY_FILE = os.path.join(DATA_DIR, "ribat_user_memory.json")
ANALYTICS_FILE = os.path.join(DATA_DIR, "ribat_analytics.json")
APPOINTMENTS_FILE = os.path.join(DATA_DIR, "ribat_appointments.json")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_ENABLED else None
app = FastAPI(title="Ribat Chatbot API (Gemini) - Full")


# ============================================================
# DUMMY DATA (KB + Specialists + Slots)
# ============================================================
KB = [
    {
        "id": "kb_001",
        "topic": "teen_communication",
        "age_min": 12, "age_max": 18,
        "tags": ["مراهق", "مراهقة", "مش بيرد", "ساكت", "قافل"],
        "tip": "ابدئي في وقت هدوء بجملة: «أنا مهتمة أفهمك مش ألومك». اسألي سؤال واحد مفتوح وسيبي مساحة للرد."
    },
    {
        "id": "kb_002",
        "topic": "anger",
        "age_min": 6, "age_max": 18,
        "tags": ["عصبية", "غضب", "صراخ", "بيزعق"],
        "tip": "وقت الغضب قللي الكلام وثبتي حدود هادية. بعد ما يهدى: «إيه اللي ضايقك؟ وإيه الحل المرة الجاية؟»."
    },
    {
        "id": "kb_003",
        "topic": "screen_addiction",
        "age_min": 8, "age_max": 18,
        "tags": ["موبايل", "شاشات", "تيك توك", "إدمان"],
        "tip": "اعملي اتفاق مكتوب: وقت شاشة + وقت عيلة. قلّلي تدريجيًا (15 دقيقة) مع بديل ممتع مش عقاب."
    },
    {
        "id": "kb_004",
        "topic": "bullying",
        "age_min": 6, "age_max": 18,
        "tags": ["تنمر", "مدرسة", "سخرية", "بيضرب"],
        "tip": "صدّقي مشاعره، خدي تفاصيل بسيطة، تواصلي مع المدرسة، ودرّبيه على ردود قصيرة وطلب المساعدة."
    },
    {
        "id": "kb_005",
        "topic": "study_focus",
        "age_min": 8, "age_max": 18,
        "tags": ["مذاكرة", "تركيز", "تسويف", "واجب"],
        "tip": "قسّمي المذاكرة لبلوكات 25 دقيقة + 5 راحة. خلي البداية سهلة (أول 5 دقائق) لتكسير حاجز البدء."
    },

    # ---- Kids content ----
    {
        "id": "kb_100",
        "topic": "kids_stories",
        "age_min": 4, "age_max": 10,
        "tags": ["قصة", "قصص", "حكاية", "قبل النوم", "احكي"],
        "tip": (
            "قصة قصيرة (5 دقايق) — عنوان: «نجمة والمشاركة»\n"
            "نجمة عندها لعبة جديدة، وكل ما أصحابها ييجوا تلعب لوحدها. "
            "في يوم، صحابها زعلوا ومشيوا. نجمة حسّت بالوحدة.\n"
            "ماما قالت: «المشاركة مش بتقلل لعبتك… بتكبر فرحتك».\n"
            "نجمة جرّبت تدي كل واحد دوره دقيقة، ولعبوا وضحكوا.\n"
            "الدرس: المشاركة + الدور.\n"
            "سؤال للطفل: إنت كنت هتعمل إيه لو كنت مكان نجمة؟"
        )
    },
    {
        "id": "kb_101",
        "topic": "activities_games",
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
        "id": "kb_102",
        "topic": "book_recommendations",
        "age_min": 4, "age_max": 12,
        "tags": ["كتاب", "كتب", "قراءة", "اقترح كتب"],
        "tip": (
            "اقتراح كتب حسب السن (فكرة عامة بدون أسماء محددة):\n"
            "- سن 4–7: كتب مصوّرة قصيرة (قصة + صورة) عن الصداقة/المشاركة/الصدق.\n"
            "- سن 8–12: مغامرات قصيرة + قيم (تحمل مسؤولية/شجاعة/تعاون).\n"
            "بعد القراءة اسألي: «إيه أكتر موقف عجبك؟ وإيه الدرس؟»"
        )
    },

    # ---- Personality assessment guidance ----
    {
        "id": "kb_103",
        "topic": "assessment_personality",
        "age_min": 4, "age_max": 18,
        "tags": ["تقييم", "assessment", "شخصية", "قيادي", "اجتماعي", "انطوائي"],
        "tip": (
            "نقدر نعمل تقييم شخصية (إرشادي) يساعدك تفهم ابنك: "
            "افتحي endpoint: /assessment/questions (مع age لو تحبي)، "
            "وبعدها ابعتي الإجابات على /assessment/submit."
        )
    },
]

SPECIALISTS = [
    {"id": "sp_001", "name": "د. مريم علي", "title": "أخصائي إرشاد أسري", "topics": ["teen_communication", "anger"], "price_egp": 350, "rating": 4.8},
    {"id": "sp_002", "name": "د. أحمد حسن", "title": "أخصائي نفسي", "topics": ["bullying", "study_focus"], "price_egp": 400, "rating": 4.6},
    {"id": "sp_003", "name": "أ. سارة محمود", "title": "أخصائي تعديل سلوك", "topics": ["screen_addiction", "anger"], "price_egp": 300, "rating": 4.7},
]

SLOTS = [
    {"slot_id": "sl_001", "specialist_id": "sp_001", "start": "2026-01-24T18:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_002", "specialist_id": "sp_001", "start": "2026-01-25T20:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_003", "specialist_id": "sp_002", "start": "2026-01-24T19:00:00+02:00", "duration_min": 45, "available": True},
    {"slot_id": "sl_004", "specialist_id": "sp_003", "start": "2026-01-26T21:00:00+02:00", "duration_min": 30, "available": True},
]

# ✅ Will be loaded from files (if exist)
APPOINTMENTS: List[Dict[str, Any]] = []
ANALYTICS: List[Dict[str, Any]] = []
USER_USAGE: Dict[str, Dict[str, int]] = {}

# Memory store (in-memory + optional file persistence)
USER_MEMORY: Dict[str, Dict[str, Any]] = {}


# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================
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


class AnalyticsEvent(BaseModel):
    event_id: str
    user_id: str
    ts: str
    event_type: Literal["chat_message", "booking_created", "app_event"]
    topic: Optional[str] = None
    in_scope: Optional[bool] = None
    booked: Optional[bool] = None
    meta: Dict[str, Any] = {}


class AppEventRequest(BaseModel):
    user_id: str
    event_name: Literal[
        "open_app", "view_content", "save_tip", "start_chat", "complete_activity",
        "request_booking", "complete_booking",
        "behavior_event", "view_assessment", "assessment_submit"
    ]
    meta: Dict[str, Any] = {}


# ============================================================
# ROUTER (Structured Output)
# ============================================================
AllowedTopic = Literal[
    "teen_communication", "anger", "screen_addiction", "bullying", "study_focus",
    "siblings_jealousy", "parents_conflict", "lying", "general_parenting",
    "kids_stories", "activities_games", "book_recommendations",
    "assessment_personality",
    "out_of_scope"
]

AllowedAction = Literal[
    "answer_with_tips",
    "recommend_booking",
    "book_appointment",
    "refuse_out_of_scope"
]


class RouteDecision(BaseModel):
    in_scope: bool = Field(description="هل السؤال داخل نطاق رباط (أسرة/تربية/تواصل)؟")
    topic: AllowedTopic = Field(description="موضوع السؤال داخل رباط أو out_of_scope")
    action: AllowedAction = Field(description="ماذا نفعل؟")
    extracted_child_age: Optional[int] = Field(default=None, description="سن الطفل لو اتذكر")
    reason: str = Field(description="سبب مختصر للقرار")
    slot_id: Optional[str] = None
    specialist_id: Optional[str] = None


# ============================================================
# CONSTANTS (topic sets)
# ============================================================
PARENTING_TOPICS = {
    "teen_communication", "anger", "screen_addiction", "bullying", "study_focus",
    "siblings_jealousy", "parents_conflict", "lying", "general_parenting",
}
KIDS_CONTENT_TOPICS = {"kids_stories", "activities_games", "book_recommendations"}
ASSESSMENT_TOPIC = "assessment_personality"


# ============================================================
# HARD GUARDS (منع البرمجة/التقنية + تشخيص/أدوية) + Kids safety
# ============================================================
OUT_OF_SCOPE_KEYWORDS = [
    "برمجة", "كود", "flutter", "android", "python", "java", "c++", "sql",
    "api", "backend", "front", "database", "debug", "algorithm"
]
MEDICAL_OR_DIAGNOSIS_KEYWORDS = [
    "جرعة", "دواء", "حبوب", "مضاد", "تشخيص", "روشتة", "وصفة", "medication", "diagnosis"
]
KIDS_UNSAFE_KEYWORDS = [
    "انتحار", "إباحية", "اباحية", "سلاح", "مخدرات"
]


def hard_out_of_scope(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in OUT_OF_SCOPE_KEYWORDS)


def hard_medical(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in MEDICAL_OR_DIAGNOSIS_KEYWORDS)


def kids_safety_guard(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in KIDS_UNSAFE_KEYWORDS)


def extract_slot_id(text: str) -> Optional[str]:
    m = re.search(r"\bsl_\d{3}\b", text.lower())
    return m.group(0) if m else None


# ============================================================
# RISK ESCALATION (low/medium/high)
# ============================================================
RISK_HIGH_KEYWORDS = [
    "عايز أموت", "مش عايز أعيش", "هأذي نفسي", "انتحار", "هنتحر",
    "هقتل", "هموت", "أذي نفسي", "أؤذي نفسي"
]
RISK_MEDIUM_KEYWORDS = [
    "خوف شديد", "هلع", "نوبات", "قلق جامد", "اكتئاب", "حزين طول الوقت",
    "مش قادر", "مخنوق طول الوقت", "بانك"
]


def detect_risk_level(text: str) -> Literal["low", "medium", "high"]:
    t = text.lower()
    if any(k.lower() in t for k in RISK_HIGH_KEYWORDS):
        return "high"
    if any(k.lower() in t for k in RISK_MEDIUM_KEYWORDS):
        return "medium"
    return "low"


# ============================================================
# ANALYTICS HELPERS
# ============================================================
def bump_usage(user_id: str, key: str, inc: int = 1):
    USER_USAGE.setdefault(user_id, {})
    USER_USAGE[user_id][key] = USER_USAGE[user_id].get(key, 0) + inc


# ============================================================
# PERSIST (JSON) HELPERS
# ============================================================
def _safe_load_json(path: str) -> Dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return {}
    return {}


def _safe_write_json(path: str, data: Dict[str, Any]):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # ✅ Print errors only in development/debug mode
        if DEBUG:
            print(f"[RIBAT_DEBUG] Failed to write JSON: {path} | error={repr(e)}")


def load_analytics():
    global ANALYTICS
    data = _safe_load_json(ANALYTICS_FILE)
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        ANALYTICS = data["events"]
    else:
        ANALYTICS = []


def save_analytics():
    _safe_write_json(ANALYTICS_FILE, {"events": ANALYTICS})


def load_appointments():
    global APPOINTMENTS
    data = _safe_load_json(APPOINTMENTS_FILE)
    if isinstance(data, dict) and isinstance(data.get("appointments"), list):
        APPOINTMENTS = data["appointments"]
    else:
        APPOINTMENTS = []


def save_appointments():
    _safe_write_json(APPOINTMENTS_FILE, {"appointments": APPOINTMENTS})


# ============================================================
# MEMORY (User Memory) - optional persistence
# ============================================================
def load_memory():
    global USER_MEMORY
    if PERSIST_MEMORY:
        USER_MEMORY = _safe_load_json(MEMORY_FILE) or {}


def save_memory():
    if PERSIST_MEMORY:
        _safe_write_json(MEMORY_FILE, USER_MEMORY)


def get_user_memory(user_id: str) -> Dict[str, Any]:
    return USER_MEMORY.get(user_id, {
        "child_age": None,
        "topics": {},
        "notes": [],
        "last_summary": ""
    })


def _compact_note(text: str, max_len: int = 160) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t[:max_len]


def update_user_memory(user_id: str, topic: str, child_age: Optional[int], note: str = ""):
    mem = get_user_memory(user_id)
    if child_age is not None:
        mem["child_age"] = child_age
    mem["topics"][topic] = mem["topics"].get(topic, 0) + 1
    if note:
        mem["notes"].append(_compact_note(note, 160))
        mem["notes"] = mem["notes"][-20:]
    USER_MEMORY[user_id] = mem
    save_memory()


def sync_slots_with_appointments():
    booked = {a.get("slot_id") for a in APPOINTMENTS}
    for sl in SLOTS:
        if sl["slot_id"] in booked:
            sl["available"] = False


# ✅ Load everything on startup (clean order)
load_memory()
load_analytics()
load_appointments()
sync_slots_with_appointments()


# ============================================================
# LOCAL TOOLS (KB + Specialists + Slots)
# ============================================================
class KbSearchResult(BaseModel):
    tips: List[Dict[str, Any]] = []
    matched: bool = False
    match_count: int = 0
    used_default: bool = False


# ---------------------------
# OLD kb_search (keep it)
# ---------------------------
def kb_search(topic: str, query: str, age: Optional[int]) -> KbSearchResult:
    results: List[Dict[str, Any]] = []
    q_words = [w for w in (query or "").split() if len(w) >= 2]

    for item in KB:
        if topic and item["topic"] != topic:
            continue
        if age is not None and not (item["age_min"] <= age <= item["age_max"]):
            continue
        hay = " ".join(item["tags"]) + " " + item["tip"]
        if not q_words:
            results.append(item)
        else:
            if any(w in hay for w in q_words):
                results.append(item)

    if results:
        return KbSearchResult(tips=results[:3], matched=True, match_count=len(results), used_default=False)

    if topic in PARENTING_TOPICS:
        return KbSearchResult(tips=[], matched=False, match_count=0, used_default=False)

    defaults = [x for x in KB if x["topic"] == topic][:2]
    if defaults:
        return KbSearchResult(tips=defaults[:3], matched=False, match_count=0, used_default=True)

    return KbSearchResult(tips=[], matched=False, match_count=0, used_default=False)


# ============================================================
# ✅ kb_search_v2 (Arabic normalize + tokenization + scoring)
# ============================================================
_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")
_AR_PUNCT = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)

def _ar_normalize(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = _AR_DIACRITICS.sub("", t)              # remove tashkeel
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = t.replace("ؤ", "و").replace("ئ", "ي")
    t = t.replace("ـ", "")
    t = t.lower()
    t = _AR_PUNCT.sub(" ", t)                  # keep letters/digits/arabic
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _tokenize(text: str) -> List[str]:
    t = _ar_normalize(text)
    if not t:
        return []
    toks = [w for w in t.split() if len(w) >= 2]
    stop = {"في", "من", "على", "عن", "الى", "إلى", "هو", "هي", "ده", "دي", "دا", "انا", "انت", "انتي", "احنا", "هم"}
    return [x for x in toks if x not in stop]

def _score_item(q_tokens: List[str], item: Dict[str, Any]) -> int:
    tags = " ".join(item.get("tags", []))
    tip = item.get("tip", "")
    hay_tags = _ar_normalize(tags)
    hay_tip = _ar_normalize(tip)
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
        if len(tok) >= 4:
            if any(tok[:4] in x for x in [hay_tags, hay_tip]):
                score += 1

    return score

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
        matched = True if q_tokens and scored[0][0] >= 6 else (False if q_tokens else True)
        return KbSearchResult(
            tips=top,
            matched=matched,
            match_count=len(scored),
            used_default=(not bool(q_tokens))
        )

    if topic in PARENTING_TOPICS:
        return KbSearchResult(tips=[], matched=False, match_count=0, used_default=False)

    defaults = [x for x in KB if x["topic"] == topic][:2]
    if defaults:
        return KbSearchResult(tips=defaults[:3], matched=False, match_count=0, used_default=True)

    return KbSearchResult(tips=[], matched=False, match_count=0, used_default=False)


def recommend_specialists(topic: str) -> List[Dict[str, Any]]:
    rec = [s for s in SPECIALISTS if topic in s["topics"]]
    rec.sort(key=lambda x: (-x["rating"], x["price_egp"]))
    return rec[:3] if rec else SPECIALISTS[:2]


def available_slots(specialist_id: str) -> List[Dict[str, Any]]:
    return [sl for sl in SLOTS if sl["specialist_id"] == specialist_id and sl["available"]][:3]


def book_appointment(user_id: str, specialist_id: str, slot_id: str) -> Dict[str, Any]:
    for sl in SLOTS:
        if sl["slot_id"] == slot_id and sl["specialist_id"] == specialist_id and sl["available"]:
            sl["available"] = False
            appt = {
                "appointment_id": "ap_" + uuid.uuid4().hex[:8],
                "user_id": user_id,
                "specialist_id": specialist_id,
                "slot_id": slot_id,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }
            APPOINTMENTS.append(appt)
            save_appointments()
            return appt
    raise ValueError("Slot not available")


# ============================================================
# FOLLOW-UP QUESTIONS + CONFIDENCE (match-aware)
# ============================================================
FOLLOW_UP_BANK: Dict[str, List[str]] = {
    "anger": [
        "العصبية بتظهر إمتى أكتر؟ (قبل النوم/بعد المدرسة/وقت الموبايل)",
        "في آخر مرة اتعصب، إيه كان السبب قبلها بدقيقة؟"
    ],
    "screen_addiction": [
        "بيستخدم الشاشة كام ساعة تقريبًا؟ وعلى إيه أكتر (يوتيوب/ألعاب/تيك توك)؟",
        "هل في وقت معين بيقاوم فيه الإغلاق أكتر؟"
    ],
    "teen_communication": [
        "إيه أكتر وقت بيكون فيه هادي وقابل للكلام؟",
        "المشكلة إنه مش بيرد ولا بيرد بعصبية؟"
    ],
    "bullying": [
        "التنمر بيحصل فين أكتر؟ (فصل/باص/نادي)",
        "هل في حد بالغ في المدرسة يثق فيه الطفل؟"
    ],
    "study_focus": [
        "بيذاكر قد إيه قبل ما يتشتت؟",
        "أكتر مادة بتعمل مقاومة عنده؟"
    ],
    "kids_stories": [
        "سن الطفل قد إيه عشان أختار قصة مناسبة؟",
        "تحبي القصة تكون عن (الصدق/المشاركة/الشجاعة/الاحترام)؟"
    ],
    "activities_games": [
        "تحبي نشاط هادي ولا حركة؟",
        "عندكم أدوات بسيطة زي ورق/أقلام/مكعبات؟"
    ],
    "book_recommendations": [
        "سن الطفل قد إيه وبيحب أنهي نوع قصص؟",
        "تحبي كتب قيم وسلوك ولا مغامرات؟"
    ],
    "assessment_personality": [
        "تحبي نبدأ بتقييم سريع؟",
        "سن الطفل قد إيه عشان الأسئلة تبقى مناسبة؟"
    ],
    "general_parenting": [
        "سن الطفل قد إيه؟",
        "الموقف بيتكرر إمتى وأكتر حاجة بتسبق المشكلة إيه؟"
    ],
}

def pick_followups(topic: str) -> List[str]:
    return (FOLLOW_UP_BANK.get(topic) or ["ممكن تحكيلي موقف حصل قريب؟", "سن الطفل قد إيه؟"])[:2]


def compute_confidence(
    topic: str,
    kb_res: KbSearchResult,
    age: Optional[int],
    user_text: str,
    in_scope: bool,
    risk_level: str
) -> int:
    score = 40

    if in_scope and topic != "out_of_scope":
        score += 15

    if age is not None:
        score += 10

    if kb_res.matched:
        score += 25
        score += min(10, kb_res.match_count * 3)
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


# ============================================================
# ASSESSMENT (Questionnaire-based) - Personality Profiling
# ============================================================
TRAITS = [
    "leadership",
    "sociability",
    "empathy",
    "self_control",
    "focus",
    "curiosity",
    "adaptability",
    "sensitivity"
]

ASSESSMENT_QUESTIONS = [
    # 4–6
    {"id": "a46_1", "text": "بيقدر يهدى بعد الزعل بمساعدة بسيطة (حضن/كلمة).", "age_min": 4, "age_max": 6, "weights": {"self_control": 2}},
    {"id": "a46_2", "text": "بيشارك لعبه أو أدواته مع غيره.", "age_min": 4, "age_max": 6, "weights": {"sociability": 2, "empathy": 1}},
    {"id": "a46_3", "text": "بيسمع تعليمات بسيطة من خطوتين.", "age_min": 4, "age_max": 6, "weights": {"focus": 2}},
    {"id": "a46_4", "text": "لو حد زعل منه، بيقبل يصلّح أو يعتذر (حتى لو بكلمة).", "age_min": 4, "age_max": 6, "weights": {"empathy": 2}},

    # 7–10
    {"id": "a710_1", "text": "بيكمل واجب بسيط قبل ما يسيبه.", "age_min": 7, "age_max": 10, "weights": {"focus": 2, "self_control": 1}},
    {"id": "a710_2", "text": "بيحاول يحل خلاف مع أصحابه بالكلام.", "age_min": 7, "age_max": 10, "weights": {"self_control": 2, "empathy": 1}},
    {"id": "a710_3", "text": "بيحب يتعلم حاجة جديدة ويجرب.", "age_min": 7, "age_max": 10, "weights": {"curiosity": 2}},
    {"id": "a710_4", "text": "بيتقبل الخسارة في لعبة من غير نوبة كبيرة.", "age_min": 7, "age_max": 10, "weights": {"self_control": 2}},

    # 11–18
    {"id": "q1",  "text": "ابنك/بنتك يحب يبادر ويقترح أفكار جديدة.", "age_min": 11, "age_max": 18, "weights": {"leadership": 2, "curiosity": 1}},
    {"id": "q2",  "text": "بيحب يكون وسط الناس ويعمل صحاب بسرعة.", "age_min": 11, "age_max": 18, "weights": {"sociability": 2}},
    {"id": "q3",  "text": "لو حد زعل، بيحس بيه وبيحاول يواسيه.", "age_min": 11, "age_max": 18, "weights": {"empathy": 2}},
    {"id": "q4",  "text": "لما يتعصب بيقدر يهدي نفسه بسرعة.", "age_min": 11, "age_max": 18, "weights": {"self_control": 2}},
    {"id": "q5",  "text": "بيكمل مهامه للنهاية حتى لو زهق.", "age_min": 11, "age_max": 18, "weights": {"focus": 2, "self_control": 1}},
    {"id": "q6",  "text": "بيسأل أسئلة كتير وبيحب يعرف (ليه؟ وإزاي؟).", "age_min": 11, "age_max": 18, "weights": {"curiosity": 2}},
    {"id": "q7",  "text": "بيتقبل التغيير بسرعة (مكان/نظام جديد).", "age_min": 11, "age_max": 18, "weights": {"adaptability": 2}},
    {"id": "q8",  "text": "بيتضايق بسرعة من النقد أو بيتوتر من المواقف.", "age_min": 11, "age_max": 18, "weights": {"sensitivity": 2}},
    {"id": "q9",  "text": "بيحب يكون مسئول (ينظم/يقود لعبة/يوزع أدوار).", "age_min": 11, "age_max": 18, "weights": {"leadership": 2, "focus": 1}},
    {"id": "q10", "text": "لما يحصل خلاف، بيحاول يحل بهدوء بدل ما يزعق.", "age_min": 11, "age_max": 18, "weights": {"self_control": 2, "empathy": 1}},
]

def get_assessment_questions(child_age: Optional[int]) -> List[Dict[str, Any]]:
    if child_age is None:
        return ASSESSMENT_QUESTIONS
    return [q for q in ASSESSMENT_QUESTIONS if q["age_min"] <= child_age <= q["age_max"]]

def _normalize_score(raw: float, raw_max: float) -> int:
    if raw_max <= 0:
        return 0
    val = int(round((raw / raw_max) * 100))
    return max(0, min(100, val))

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
        weights = qs[qid]["weights"]
        for t, w in weights.items():
            raw[t] += v * w
            raw_max[t] += 5 * w

    behavior_signals = behavior_signals or {}
    raw["focus"] += max(0, 3 - int(behavior_signals.get("gives_up_fast", 0))) * 2
    raw_max["focus"] += 6
    raw["empathy"] += int(behavior_signals.get("helps_others", 0)) * 2
    raw_max["empathy"] += 6

    scores = {t: _normalize_score(raw[t], raw_max[t]) for t in TRAITS}

    ARCHETYPES = [
        {"id": "leader",      "name": "القائد",              "need": "مساحة مسؤولية + قواعد واضحة",               "profile": {"leadership": 80, "focus": 60, "sociability": 50}},
        {"id": "explorer",    "name": "المستكشف",            "need": "تجارب جديدة + مشاريع صغيرة",                "profile": {"curiosity": 80, "adaptability": 60}},
        {"id": "thinker",     "name": "المفكر",              "need": "وقت هادئ + تحديات ذهنية",                   "profile": {"focus": 75, "curiosity": 60, "sociability": 35}},
        {"id": "helper",      "name": "المُسانِد",           "need": "تقدير مشاعره + فرص مساعدة",                 "profile": {"empathy": 80, "sociability": 55}},
        {"id": "peacemaker",  "name": "صانع السلام",         "need": "تعليم حدود + تشجيع التعبير",                "profile": {"empathy": 70, "self_control": 70}},
        {"id": "energetic",   "name": "الحركي/النشيط",       "need": "تفريغ طاقة + قواعد ثابتة",                  "profile": {"sociability": 70, "curiosity": 55, "self_control": 40}},
        {"id": "sensitive",   "name": "الحساس",              "need": "طمأنة + تقليل ضغط + روتين آمن",             "profile": {"sensitivity": 80, "empathy": 60}},
        {"id": "independent", "name": "المستقل",             "need": "اختيارات + احترام المساحة + متابعة ذكية",   "profile": {"leadership": 55, "sociability": 30, "focus": 55}},
        {"id": "planner",     "name": "المنظم",              "need": "جداول بسيطة + أهداف صغيرة + مكافأة معنوية", "profile": {"focus": 80, "self_control": 70}},
        {"id": "challenger",  "name": "المُجادِل/المتحدي",   "need": "قواعد قليلة وواضحة + تفاوض + عواقب ثابتة",   "profile": {"leadership": 65, "self_control": 35, "sensitivity": 45}},
    ]

    def similarity(arch_profile: Dict[str, int], child_scores: Dict[str, int]) -> float:
        vals = []
        for t, target in arch_profile.items():
            vals.append(100 - abs(child_scores.get(t, 50) - target))
        return sum(vals) / max(1, len(vals))

    ranked = []
    for a in ARCHETYPES:
        sim = similarity(a["profile"], scores)
        ranked.append({"id": a["id"], "name": a["name"], "match": int(round(sim)), "need": a["need"]})
    ranked.sort(key=lambda x: x["match"], reverse=True)

    top_traits = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    low_traits = sorted(scores.items(), key=lambda kv: kv[1])[:2]

    return {
        "child_age": child_age,
        "trait_scores": scores,
        "top_traits": top_traits,
        "low_traits": low_traits,
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
    total_questions = len(qs)

    valid_answers = 0
    for a in answers or []:
        qid = a.get("question_id") or a.get("id")
        v = a.get("value")
        if qid in q_ids and v is not None:
            try:
                v = int(v)
            except Exception:
                continue
            if 1 <= v <= 5:
                valid_answers += 1

    score = 0
    notes = []

    if total_questions > 0:
        coverage = valid_answers / total_questions
        score += int(round(coverage * 65))
        notes.append(f"coverage={int(round(coverage*100))}%")
    else:
        notes.append("no_questions")

    if child_age is not None:
        score += 15
        notes.append("age_provided")

    if behavior_signals:
        score += 10
        notes.append("behavior_signals")

    if valid_answers < max(3, total_questions // 3 if total_questions else 3):
        score = max(0, score - 15)
        notes.append("low_answer_count_penalty")

    score = max(0, min(100, score))

    return {
        "confidence": score,
        "valid_answers": valid_answers,
        "total_questions": total_questions,
        "notes": notes
    }


# ============================================================
# GEMINI CALLS (Router + Composer + Optional Verifier)
# ============================================================
def _require_gemini():
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini disabled: missing GEMINI_API_KEY")


def gemini_route_decision(user_text: str, history: List[ChatMessage], fallback_age: Optional[int]) -> RouteDecision:
    _require_gemini()

    system = (
        "أنت Router خاص بتطبيق (رباط). "
        "رباط يجاوب فقط على: التواصل الأسري، التربية، المراهقين، العناد، العصبية، الشاشات، التنمر، المذاكرة، الخلافات الأسرية، "
        "قصص للأطفال، ألعاب وأنشطة تربوية، اقتراح كتب مناسبة للسن، وتقييم شخصية الطفل (Assessment).\n"
        "ممنوع: البرمجة/التقنية/أي موضوع خارج رباط.\n"
        "ممنوع: تشخيص/أدوية/جرعات.\n"
        "لو السؤال خارج رباط => action=refuse_out_of_scope و in_scope=false.\n"
        "لو المستخدم كتب (احجز sl_001) استخرج slot_id.\n"
        "اخرج JSON فقط حسب الـschema."
    )

    short_history = "\n".join([f"{m.role}: {m.content}" for m in history[-6:]])

    prompt = f"""
System: {system}

Conversation:
{short_history}

User message:
{user_text}

Known child age (if any): {fallback_age}
"""

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RouteDecision,
            temperature=0,
            safety_settings=[
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
            ],
        ),
    )

    try:
        return RouteDecision.model_validate_json(resp.text)
    except Exception:
        return RouteDecision(
            in_scope=False,
            topic="out_of_scope",
            action="refuse_out_of_scope",
            extracted_child_age=None,
            reason=f"Failed to parse routing JSON. raw={resp.text[:200]}",
        )


def gemini_compose_answer(
    user_text: str,
    topic: str,
    tips: List[Dict[str, Any]],
    specialists: List[Dict[str, Any]],
    slots: List[Dict[str, Any]],
    memory: Dict[str, Any],
    followups: List[str],
    confidence: int,
    risk_level: str
) -> str:
    _require_gemini()

    system = (
        "أنت مساعد توعوي داخل تطبيق (رباط) لدعم الأسرة وتقوية التواصل.\n"
        "قواعد صارمة:\n"
        "- لا تشخيص ولا أدوية ولا جرعات.\n"
        "- لا تتكلم في البرمجة أو أي موضوع خارج رباط.\n"
        "- استخدم فقط المعلومات المعطاة في ALLOWED DATA.\n"
        "- لو confidence أقل من 65 أو tips فاضية: اكتب رد احتوائي قصير + سؤال متابعة واحد فقط (من followups) بدون نصايح تفصيلية.\n"
        "- لو الموضوع قصص/ألعاب للأطفال: خلي المحتوى مناسب للسن وبدون أي محتوى غير مناسب.\n"
        "- اكتب بالعربي العامي المحترم.\n"
        "الأسلوب:\n"
        "- لو confidence عالي: 3 نقاط عملية + سؤال متابعة واحد + اقتراح حجز لو مناسب.\n"
        "- لو confidence منخفض: احتواء + سؤال متابعة واحد.\n"
    )

    payload = {
        "topic": topic,
        "tips": tips,
        "specialists": specialists,
        "slots": slots,
        "memory": memory,
        "followups": followups,
        "confidence": confidence,
        "risk_level": risk_level
    }

    prompt = f"""
{system}

USER QUESTION:
{user_text}

ALLOWED DATA (JSON):
{json.dumps(payload, ensure_ascii=False)}
"""

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=420,
        ),
    )
    return (resp.text or "").strip() or "ممكن تقوليلي تفاصيل أكتر؟"


def gemini_verify_answer(user_text: str, answer: str, allowed_payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_gemini()

    prompt = f"""
راجع الرد التالي: هل خرج عن نطاق رباط أو ذكر تشخيص/أدوية/برمجة؟
أخرج JSON فقط بالشكل:
{{"ok": true/false, "reason": "مختصر"}}

USER:
{user_text}

ANSWER:
{answer}

ALLOWED DATA:
{json.dumps(allowed_payload, ensure_ascii=False)}
"""
    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
            max_output_tokens=180
        ),
    )
    try:
        data = json.loads(r.text)
        return {"ok": bool(data.get("ok", True)), "reason": (data.get("reason") or "").strip()}
    except Exception:
        return {"ok": True, "reason": ""}


def empathy_reflect(user_text: str, topic: str, risk_level: str) -> str:
    t = user_text.strip()
    reflection = t[:77] + "..." if len(t) > 80 else t

    empathy_map = {
        "anger": "واضح إن الموضوع ده متعبك وبيستنزف أعصابك.",
        "screen_addiction": "حاسّة بقلقك من موضوع الشاشات وتأثيره عليه.",
        "teen_communication": "واضح إن قلة التواصل مضايقاكي وبتوجع.",
        "bullying": "طبيعي تقلقي جدًا لما تحسي إن ابنك بيتأذى.",
        "study_focus": "الإحساس بالحيرة مع المذاكرة بيكون مرهق فعلًا.",
        "kids_stories": "تحبّي تعملي حاجة لطيفة ومناسبة لسنّه.",
        "activities_games": "واضح إنك بتحاولي تملّي وقته بحاجة مفيدة.",
        "assessment_personality": "حلو إنك عايزة تفهمي شخصيته أكتر.",
        "general_parenting": "الأمومة مليانة مواقف بتخلينا نحتار."
    }

    empathy = empathy_map.get(topic, "حاسة بيكي، والموقف ده مش سهل.")
    if risk_level == "medium":
        empathy += " خلّينا نمشي بهدوء ونفهم الصورة كاملة."
    elif risk_level == "high":
        empathy += " أهم حاجة دلوقتي الأمان والدعم."

    return f"{empathy}\n\nإنتِ بتقولي: «{reflection}»\n"


# ============================================================
# ENDPOINTS
@app.get("/")
def home():
    return {"message": "Ribat Bot API is running 🚀"}
# ============================================================
@app.get("/health")
def health():
    return {
        "ok": True,
        "model": GEMINI_MODEL,
        "gemini_enabled": GEMINI_ENABLED,
        "verify": ENABLE_VERIFY,
        "persist_memory": PERSIST_MEMORY,
        "data_dir": DATA_DIR,
        "debug": DEBUG
    }


@app.get("/test_gemini")
def test_gemini():
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini disabled: missing GEMINI_API_KEY")
    r = client.models.generate_content(model=GEMINI_MODEL, contents="OK فقط")
    return {"text": r.text}


# ---------- KB ----------
@app.get("/kb/topics")
def kb_topics():
    topics = sorted(list({x["topic"] for x in KB}))
    return {"topics": topics, "count": len(topics)}


@app.get("/kb/search")
def kb_search_api(topic: str, q: str = "", age: Optional[int] = None):
    """
    ✅ Mobile:
    /kb/search?topic=anger&age=7&q=صراخ
    """
    res = kb_search_v2(topic=topic, query=q or "", age=age)
    return {
        "topic": topic,
        "age": age,
        "matched": res.matched,
        "match_count": res.match_count,
        "used_default": res.used_default,
        "tips": res.tips
    }


@app.post("/kb/add")
def kb_add(req: KbAddRequest):
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")

    new_id = "kb_" + uuid.uuid4().hex[:6]
    KB.append({
        "id": new_id,
        "topic": req.topic,
        "age_min": req.age_min,
        "age_max": req.age_max,
        "tags": req.tags,
        "tip": req.tip
    })
    return {"ok": True, "kb_id": new_id, "total": len(KB)}


# ---------- Analytics ----------
@app.post("/analytics/event")
def analytics_event(req: AppEventRequest):
    bump_usage(req.user_id, req.event_name, 1)

    ev = AnalyticsEvent(
        event_id="ev_" + uuid.uuid4().hex[:10],
        user_id=req.user_id,
        ts=datetime.utcnow().isoformat() + "Z",
        event_type="app_event",
        meta={"event_name": req.event_name, **req.meta}
    )
    ANALYTICS.append(ev.model_dump())
    save_analytics()
    return {"ok": True}


@app.get("/analytics/summary")
def analytics_summary():
    total = len(ANALYTICS)
    by_type: Dict[str, int] = {}
    for e in ANALYTICS:
        by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1
    return {"total_events": total, "by_type": by_type}


@app.get("/analytics/user/{user_id}")
def analytics_user(user_id: str):
    usage = USER_USAGE.get(user_id, {})
    events = [e for e in ANALYTICS if e["user_id"] == user_id][-100:]
    return {"user_id": user_id, "usage": usage, "recent_events": events}


# ---------- Memory (debug/helper) ----------
@app.get("/memory/user/{user_id}")
def memory_user(user_id: str):
    return {"user_id": user_id, "memory": get_user_memory(user_id)}


# ---------- Assessment ----------
class AssessmentQuestionsResponse(BaseModel):
    child_age: Optional[int] = None
    scale: Dict[str, Any]
    questions: List[Dict[str, Any]]


@app.get("/assessment/questions", response_model=AssessmentQuestionsResponse)
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return AssessmentQuestionsResponse(
        child_age=age,
        scale={
            "min": 1, "max": 5,
            "labels": {"1": "أبدًا", "2": "نادرًا", "3": "أحيانًا", "4": "غالبًا", "5": "دائمًا"}
        },
        questions=[{"id": q["id"], "text": q["text"]} for q in qs]
    )


class AssessmentSubmitRequest(BaseModel):
    user_id: str
    child_age: Optional[int] = None
    answers: List[Dict[str, Any]] = []
    behavior_signals: Optional[Dict[str, Any]] = None


@app.post("/assessment/submit")
def assessment_submit(req: AssessmentSubmitRequest):
    profile = compute_personality_profile(
        answers=req.answers,
        child_age=req.child_age,
        behavior_signals=req.behavior_signals
    )

    assess_conf = compute_assessment_confidence(
        answers=req.answers,
        child_age=req.child_age,
        behavior_signals=req.behavior_signals
    )
    assessment_confidence = assess_conf["confidence"]

    ANALYTICS.append(AnalyticsEvent(
        event_id="ev_" + uuid.uuid4().hex[:10],
        user_id=req.user_id,
        ts=datetime.utcnow().isoformat() + "Z",
        event_type="app_event",
        meta={
            "event_name": "assessment_submit",
            "assessment_confidence": assessment_confidence,
            "valid_answers": assess_conf["valid_answers"],
            "total_questions": assess_conf["total_questions"],
            "profile": profile
        }
    ).model_dump())
    save_analytics()

    bump_usage(req.user_id, "assessment_submit", 1)
    update_user_memory(req.user_id, "assessment_personality", req.child_age, note="Assessment submitted")

    cards = [
        Card(
            type="assessment_result",
            title="نتيجة تقييم شخصية الطفل (إرشادي)",
            body=(
                "أقرب الشخصيات المحتملة:\n" +
                "\n".join([f"- {p['name']} (تطابق {p['match']}%) — يحتاج: {p['need']}" for p in profile["possible_personalities"]]) +
                "\n\nأقوى السمات:\n" +
                "\n".join([f"- {t}: {v}%" for t, v in profile["top_traits"]]) +
                "\n\nسمات تحتاج دعم:\n" +
                "\n".join([f"- {t}: {v}%" for t, v in profile["low_traits"]]) +
                f"\n\nملاحظة: {profile['note']}"
            ),
            meta=profile
        ),
        Card(
            type="confidence",
            title="درجة ثقة التقييم",
            body=f"{assessment_confidence}%",
            meta={
                "assessment_confidence": assessment_confidence,
                "valid_answers": assess_conf["valid_answers"],
                "total_questions": assess_conf["total_questions"],
                "notes": assess_conf["notes"]
            }
        )
    ]

    return {
        "ok": True,
        "profile": profile,
        "assessment_confidence": assessment_confidence,
        "assessment_meta": {
            "valid_answers": assess_conf["valid_answers"],
            "total_questions": assess_conf["total_questions"],
            "notes": assess_conf["notes"]
        },
        "cards": [c.model_dump() for c in cards]
    }


# ---------- Booking direct ----------
class BookRequest(BaseModel):
    user_id: str
    specialist_id: str
    slot_id: str


@app.get("/appointments/list")
def appointments_list(user_id: Optional[str] = None, limit: int = 50):
    """
    ✅ ترجع الحجوزات من الملف (persistent)
    أمثلة:
    - /appointments/list
    - /appointments/list?user_id=u_123
    - /appointments/list?user_id=u_123&limit=20
    """
    load_appointments()
    sync_slots_with_appointments()

    items = APPOINTMENTS
    if user_id:
        items = [a for a in items if a.get("user_id") == user_id]

    items = sorted(items, key=lambda x: (x.get("created_at") or ""), reverse=True)

    limit = max(1, min(200, int(limit)))
    items = items[:limit]

    return {
        "ok": True,
        "user_id": user_id,
        "count": len(items),
        "appointments": items
    }


@app.post("/appointments/book")
def book(req: BookRequest):
    # ✅ safer for multi-instance / restarts: always reload latest file state
    load_appointments()
    sync_slots_with_appointments()

    try:
        appt = book_appointment(req.user_id, req.specialist_id, req.slot_id)
        bump_usage(req.user_id, "complete_booking", 1)

        ANALYTICS.append(AnalyticsEvent(
            event_id="ev_" + uuid.uuid4().hex[:10],
            user_id=req.user_id,
            ts=datetime.utcnow().isoformat() + "Z",
            event_type="booking_created",
            topic=None,
            in_scope=True,
            booked=True,
            meta={"appointment_id": appt["appointment_id"], "slot_id": req.slot_id, "specialist_id": req.specialist_id}
        ).model_dump())
        save_analytics()
        return {"ok": True, "appointment": appt}
    except ValueError:
        raise HTTPException(status_code=400, detail="Slot not available")


# ---------- Feedback loop ----------
class FeedbackRequest(BaseModel):
    user_id: str
    message_id: str
    rating: Literal["up", "down"]
    comment: Optional[str] = None
    topic: Optional[str] = None


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    ANALYTICS.append(AnalyticsEvent(
        event_id="ev_" + uuid.uuid4().hex[:10],
        user_id=req.user_id,
        ts=datetime.utcnow().isoformat() + "Z",
        event_type="app_event",
        meta={
            "event_name": "feedback",
            "message_id": req.message_id,
            "rating": req.rating,
            "comment": req.comment,
            "topic": req.topic
        }
    ).model_dump())
    save_analytics()

    if req.comment:
        update_user_memory(req.user_id, req.topic or "general_parenting", None, note=f"FEEDBACK:{req.rating}:{req.comment}")
    return {"ok": True}


# ============================================================
# CHAT
# ============================================================
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages is empty")

    message_id = "msg_" + uuid.uuid4().hex[:10]
    user_text = req.messages[-1].content.strip()
    bump_usage(req.user_id, "start_chat", 1)

    if hard_out_of_scope(user_text) or hard_medical(user_text):
        ANALYTICS.append(AnalyticsEvent(
            event_id="ev_" + uuid.uuid4().hex[:10],
            user_id=req.user_id,
            ts=datetime.utcnow().isoformat() + "Z",
            event_type="chat_message",
            topic="out_of_scope",
            in_scope=False,
            booked=False,
            meta={"message_id": message_id, "message": user_text[:300], "blocked_by": "hard_guard"}
        ).model_dump())
        save_analytics()
        return ChatResponse(
            message_id=message_id,
            reply="أنا بوت (رباط) متخصص في دعم الأسرة والتواصل بين الأهل والأبناء، ومش بقدر أساعد في طلبات البرمجة/الأدوية/التشخيص. لو عندك سؤال عن تربية أو تواصل أسري اكتبِه وأنا معاكِ.",
            cards=[Card(type="refusal", title="خارج نطاق رباط", body="اسألي عن: مراهقة، عصبية، موبايل، تنمر، مذاكرة، قصص للأطفال، ألعاب تربوية، تقييم شخصية الطفل…")]
        )

    if not GEMINI_ENABLED or client is None:
        return ChatResponse(
            message_id=message_id,
            reply="ميزة الشات غير مفعّلة حاليًا لأن GEMINI_API_KEY مش موجود. التقييم (Assessment) والـ KB والـ Memory شغالين عادي ✅",
            cards=[Card(type="warning", title="Gemini غير مفعّل", body="ضيفي GEMINI_API_KEY في Environment Variables لتفعيل /chat.")]
        )

    slot_from_text = extract_slot_id(user_text)
    wants_booking_word = any(x in user_text for x in ["احجز", "حجز", "استشارة", "مختص", "دكتور"])
    if wants_booking_word:
        bump_usage(req.user_id, "request_booking", 1)

    try:
        decision = gemini_route_decision(user_text, req.messages, req.child_age)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gemini route failed: {str(e)}")

    if slot_from_text and decision.topic != "out_of_scope":
        decision.action = "book_appointment"
        decision.slot_id = slot_from_text

    risk_level = detect_risk_level(user_text)

    ANALYTICS.append(AnalyticsEvent(
        event_id="ev_" + uuid.uuid4().hex[:10],
        user_id=req.user_id,
        ts=datetime.utcnow().isoformat() + "Z",
        event_type="chat_message",
        topic=decision.topic,
        in_scope=decision.in_scope,
        booked=False,
        meta={"message_id": message_id, "message": user_text[:300], "router_reason": decision.reason, "risk_level": risk_level}
    ).model_dump())
    save_analytics()

    if (not decision.in_scope) or decision.action == "refuse_out_of_scope" or decision.topic == "out_of_scope":
        return ChatResponse(
            message_id=message_id,
            reply="أنا بوت (رباط) متخصص في دعم الأسرة والتواصل بين الأهل والأبناء. سؤالك ده خارج نطاق رباط. اسألي عن مشكلة أسرية/تربوية وأنا أساعدك فورًا ✅",
            cards=[Card(type="refusal", title="خارج نطاق رباط", body=f"السبب: {decision.reason}")]
        )

    if risk_level == "high":
        return ChatResponse(
            message_id=message_id,
            reply=(
                "أنا قلقان/ة عليك جدًا. لو في خطر فوري أو إحساس قوي بعدم الأمان، "
                "اتواصلي فورًا مع شخص كبير موثوق قريب منك (أهل/قريب/مدرسة)، "
                "ولو الموضوع عاجل اتصلي بخدمات الطوارئ في بلدك. "
                "لو تحبي، قوليلي: هل إنتِ/الطفل دلوقتي في مكان آمن ومعاكم حد كبير؟"
            ),
            cards=[Card(type="warning", title="مهم جدًا", body="في الحالات العاجلة لازم تدخل إنسان/مختص فورًا. رباط هنا للدعم العام فقط.", meta={"risk_level": "high"})]
        )

    mem = get_user_memory(req.user_id)
    age = decision.extracted_child_age or req.child_age or mem.get("child_age")

    if decision.topic in ["kids_stories", "activities_games"] and kids_safety_guard(user_text):
        return ChatResponse(
            message_id=message_id,
            reply="خلّينا نخلي المحتوى مناسب للأطفال 🙏 قوليلي سن الطفل وعايزين قصة/لعبة عن (الصدق/المشاركة/الشجاعة/الاحترام)؟",
            cards=[Card(type="warning", title="محتوى مناسب للأطفال", body="اختاري موضوع آمن ومناسب للسن.")]
        )

    update_user_memory(req.user_id, decision.topic, age, note=user_text)
    mem = get_user_memory(req.user_id)

    # Booking flow
    if decision.action == "book_appointment":
        slot_id = decision.slot_id
        specialist_id = decision.specialist_id

        if slot_id and not specialist_id:
            match = next((s for s in SLOTS if s["slot_id"] == slot_id), None)
            if match:
                specialist_id = match["specialist_id"]

        if not slot_id or not specialist_id:
            return ChatResponse(
                message_id=message_id,
                reply="تمام، ابعتي رقم الموعد بالشكل ده: (احجز sl_001).",
                cards=[Card(type="warning", title="ناقص بيانات الحجز", body="محتاجين slot_id زي sl_001.")]
            )

        # ✅ reload latest state before booking (safer)
        load_appointments()
        sync_slots_with_appointments()

        try:
            appt = book_appointment(req.user_id, specialist_id, slot_id)
            sp = next((x for x in SPECIALISTS if x["id"] == specialist_id), None)

            bump_usage(req.user_id, "complete_booking", 1)
            ANALYTICS.append(AnalyticsEvent(
                event_id="ev_" + uuid.uuid4().hex[:10],
                user_id=req.user_id,
                ts=datetime.utcnow().isoformat() + "Z",
                event_type="booking_created",
                topic=decision.topic,
                in_scope=True,
                booked=True,
                meta={"message_id": message_id, "appointment_id": appt["appointment_id"], "slot_id": slot_id, "specialist_id": specialist_id}
            ).model_dump())
            save_analytics()

            return ChatResponse(
                message_id=message_id,
                reply=f"تم الحجز ✅ رقم الحجز: {appt['appointment_id']}.",
                cards=[Card(
                    type="booking",
                    title="تفاصيل الحجز",
                    body=f"المختص: {sp['name'] if sp else specialist_id}\nslot_id: {slot_id}",
                    meta=appt
                )]
            )
        except ValueError:
            return ChatResponse(
                message_id=message_id,
                reply="الموعد ده مش متاح دلوقتي. اختاري ميعاد تاني من اللي ظاهر.",
                cards=[Card(type="warning", title="الموعد غير متاح", body="جرّبي slot_id مختلف.")]
            )

    # Normal answer flow
    topic = decision.topic
    kb_res = kb_search_v2(topic=topic, query=user_text, age=age)
    tips = kb_res.tips

    show_specialists = wants_booking_word or decision.action in ["recommend_booking", "book_appointment"] or (risk_level == "medium")
    specialists = recommend_specialists(topic=topic) if show_specialists else []

    slots: List[Dict[str, Any]] = []
    if show_specialists and specialists:
        best_sp = specialists[0]
        slots = available_slots(best_sp["id"])

    followups = pick_followups(topic)
    conf = compute_confidence(topic, kb_res, age, user_text, decision.in_scope, risk_level)

    if topic in PARENTING_TOPICS and (not kb_res.matched) and conf < 65:
        q = followups[0] if followups else "سن الطفل قد إيه؟"
        return ChatResponse(
            message_id=message_id,
            reply=(
                "حاسّة إن الموضوع مُتعب ومحتاج نفهمه صح قبل ما أدي خطوات محددة. "
                f"{q} "
                "ولو تقدري احكيلي موقف واحد حصل قريب (إيه اللي حصل قبلها بدقيقة وبعدها؟)."
            ),
            cards=[
                Card(type="confidence", title="درجة الثقة (إرشادي)", body=f"{conf}%", meta={"confidence": conf, "matched": kb_res.matched, "used_default": kb_res.used_default}),
                Card(type="warning", title="سؤال متابعة سريع", body=q, meta={"followups": followups}),
            ]
        )

    allowed_payload = {
        "topic": topic,
        "tips": tips,
        "specialists": specialists,
        "slots": slots,
        "memory": mem,
        "followups": followups,
        "confidence": conf,
        "risk_level": risk_level
    }

    intro = empathy_reflect(user_text, topic, risk_level)
    try:
        final_text = intro + gemini_compose_answer(
            user_text=user_text,
            topic=topic,
            tips=tips,
            specialists=specialists,
            slots=slots,
            memory=mem,
            followups=followups,
            confidence=conf,
            risk_level=risk_level
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gemini compose failed: {str(e)}")

    if ENABLE_VERIFY:
        verdict = gemini_verify_answer(user_text, final_text, allowed_payload)
        if not verdict.get("ok", True):
            final_text = "أنا معاكِ ✅ بس خلّيني أسألك سؤال صغير يساعدني أديك رد أدق: " + (followups[0] if followups else "سن الطفل قد إيه؟")

    cards: List[Card] = []

    for t in tips:
        ctype = "tip"
        title = "نصيحة عملية"
        if topic == "kids_stories":
            ctype, title = "story", "قصة للأطفال"
        elif topic == "activities_games":
            ctype, title = "game", "لعبة/نشاط"
        elif topic == "book_recommendations":
            ctype, title = "books", "اقتراح قراءة"
        elif topic == "assessment_personality":
            ctype, title = "assessment_question", "تقييم شخصية الطفل"

        cards.append(Card(
            type=ctype,
            title=title,
            body=t["tip"],
            meta={"kb_id": t["id"], "topic": t["topic"], "age_used": age, "matched": kb_res.matched, "used_default": kb_res.used_default}
        ))

    cards.append(Card(
        type="confidence",
        title="درجة الثقة (إرشادي)",
        body=f"{conf}%",
        meta={"confidence": conf, "matched": kb_res.matched, "used_default": kb_res.used_default, "risk_level": risk_level}
    ))

    if conf < 70 or (topic in PARENTING_TOPICS and not kb_res.matched):
        cards.append(Card(
            type="warning",
            title="سؤال متابعة سريع",
            body="- " + ("\n- ".join(followups[:1])),
            meta={"followups": followups}
        ))

    if show_specialists:
        for s in specialists:
            cards.append(Card(
                type="specialist",
                title=f"{s['name']} — {s['title']}",
                body=f"السعر: {s['price_egp']} جنيه | التقييم: {s['rating']}",
                meta={"specialist_id": s["id"], "topics": s["topics"]}
            ))

    if slots and show_specialists:
        body = "\n".join([f"- {sl['slot_id']}: {sl['start']} ({sl['duration_min']} دقيقة)" for sl in slots])
        cards.append(Card(
            type="booking",
            title="مواعيد متاحة (ديمو)",
            body=body + "\n\nللحجز ابعتي: احجز sl_001",
            meta={"slot_ids": [sl["slot_id"] for sl in slots], "specialist_id": specialists[0]["id"] if specialists else None}
        ))

    return ChatResponse(message_id=message_id, reply=final_text, cards=cards)
