"""
Rafiq Bot API - Production
==========================
Core features: Users, Memory, Chat, Assessment, Parenting Plan, PDF Export,
Analytics, Feedback, Notifications.
RAG system: pgvector-powered semantic knowledge base with Gemini embeddings.
"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
import uuid
import re
import io
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import psycopg2

# ---------------------------------------------------------------------------
# OPTIONAL DEPENDENCIES
# ---------------------------------------------------------------------------

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle,
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    print("WARNING: reportlab not installed - PDF export disabled.")

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_display
    _ARABIC_SHAPING = True
except ImportError:
    _ARABIC_SHAPING = False
    print("WARNING: arabic-reshaper / python-bidi not installed - Arabic PDF text may not render correctly.")

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

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

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
    print("WARNING: RAFIQ_ADMIN_KEY is default. Set a strong value in production.")

# ---------------------------------------------------------------------------
# GEMINI CLIENT
# ---------------------------------------------------------------------------
client = None

if GEMINI_ENABLED:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini initialized successfully.")
    except Exception as exc:
        print(f"Gemini init failed: {exc}")
# ---------------------------------------------------------------------------
# FIREBASE CLIENT
# ---------------------------------------------------------------------------

FIREBASE_ENABLED = False
_FIREBASE_CREDS_JSON = os.getenv("FIREBASE_CREDENTIALS", "").strip()
if _FIREBASE_AVAILABLE and _FIREBASE_CREDS_JSON:
    try:
        if _FIREBASE_CREDS_JSON.startswith("{"):
            _fb_cred_dict = json.loads(_FIREBASE_CREDS_JSON)
            _fb_cred = fb_credentials.Certificate(_fb_cred_dict)
        elif os.path.exists(_FIREBASE_CREDS_JSON):
            _fb_cred = fb_credentials.Certificate(_FIREBASE_CREDS_JSON)
        else:
            raise ValueError(
                f"FIREBASE_CREDENTIALS is neither a valid JSON string "
                f"nor an existing file path: {_FIREBASE_CREDS_JSON[:80]}"
            )
        firebase_admin.initialize_app(_fb_cred)
        FIREBASE_ENABLED = True
        print("Firebase initialized successfully.")
    except Exception as exc:
        print(f"Firebase init failed: {exc}")

# ---------------------------------------------------------------------------
# FONT REGISTRATION (reportlab)
# ---------------------------------------------------------------------------

_FONT_ARABIC_REGISTERED = False
_FONT_LATIN_REGISTERED  = False

if _REPORTLAB_AVAILABLE:
    try:
        if os.path.exists(FONT_NOTO_ARABIC):
            pdfmetrics.registerFont(TTFont("RafiqRegular", FONT_NOTO_ARABIC))
            _FONT_ARABIC_REGISTERED = True
        if os.path.exists(FONT_NOTO_BOLD):
            pdfmetrics.registerFont(TTFont("RafiqBold", FONT_NOTO_BOLD))
        if os.path.exists(FONT_NOTO_LATIN) and not _FONT_ARABIC_REGISTERED:
            pdfmetrics.registerFont(TTFont("RafiqRegular", FONT_NOTO_LATIN))
            _FONT_LATIN_REGISTERED = True
    except Exception as exc:
        print(f"Font registration warning: {exc}")

# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Rafiq Bot API",
    version="5.0.0",
    description="Family support assistant with RAG-powered knowledge base.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# TRANSLATIONS
# ---------------------------------------------------------------------------

Lang = Literal["ar", "en"]

_T: Dict[str, Any] = {
    "gemini_disabled": {
        "ar": "ميزة الشات غير مفعّلة. التقييم والـ Memory شغالين.",
        "en": "Chat feature is currently disabled. Assessment and Memory are working.",
    },
    "ok": {"ar": "تم بنجاح", "en": "Success"},
    "out_of_scope_reply": {
        "ar": "انا بوت رفيق متخصص في دعم الاسرة. مش بقدر اساعد في برمجة/ادوية/تشخيص.",
        "en": "I'm Rafiq, a family support assistant. I can't help with programming, medication, or diagnosis.",
    },
    "out_of_scope_card": {
        "ar": "اسال عن: مراهقة، عصبية، موبايل، تنمر، مذاكرة، قصص اطفال، العاب، تقييم شخصية.",
        "en": "Ask about: teen communication, anger, screen time, bullying, studying, kids stories, games, personality assessment.",
    },
    "scope_refusal": {
        "ar": "سؤالك خارج نطاق رفيق. اسال عن مشكلة اسرية/تربوية وانا اساعدك فورا.",
        "en": "Your question is outside Rafiq's scope. Ask about a parenting or family issue and I'll help right away.",
    },
    "risk_high": {
        "ar": "انا قلقان عليك جدا. تواصل فورا مع شخص كبير موثوق قريب منك او خدمات الطوارئ.",
        "en": "I'm very concerned about you. Please immediately reach out to a trusted adult or call emergency services.",
    },
    "risk_high_card": {
        "ar": "في الحالات العاجلة لازم تدخل مختص فورا. رفيق للدعم العام فقط.",
        "en": "In urgent cases a specialist must intervene immediately. Rafiq is for general support only.",
    },
    "kids_safety": {
        "ar": "خلينا نخلي المحتوى مناسب للاطفال. قوليلي سن الطفل والموضوع.",
        "en": "Let's keep content child-appropriate. Please share the child's age and topic.",
    },
    "low_conf_prefix": {
        "ar": "الموضوع محتاج تفاصيل اكتر. ",
        "en": "I need a bit more context to help effectively. ",
    },
    "low_conf_suffix": {
        "ar": " ولو تقدر احكيلي موقف حصل قريب.",
        "en": " If you can, share a recent situation that happened.",
    },
    "confidence_score": {"ar": "درجة الثقة", "en": "Confidence Score"},
    "follow_up":        {"ar": "سؤال متابعة", "en": "Follow-up"},
    "verify_fallback": {
        "ar": "انا معاك. بس خليني اسالك: ",
        "en": "I'm here for you. Let me ask: ",
    },
    "assessment_note": {
        "ar": "النتيجة ارشادية وليست تشخيصا طبيا.",
        "en": "This result is indicative, not a clinical diagnosis.",
    },
    "assessment_result_title": {
        "ar": "نتيجة تقييم شخصية الطفل",
        "en": "Child Personality Assessment Result",
    },
    "daily_tip_notif_title": {
        "ar": "نصيحة جديدة من رفيق",
        "en": "New Parenting Tip from Rafiq",
    },
    "plan_notif_title": {
        "ar": "تم انشاء خطة تربوية جديدة",
        "en": "New Parenting Plan Created",
    },
    "plan_notif_body": {
        "ar": "تم اعداد خطة مخصصة لطفلك بناء على نتائج التقييم.",
        "en": "A personalized parenting plan has been generated based on your child's assessment.",
    },
    "token_saved":            {"ar": "تم حفظ رمز الاشعار بنجاح", "en": "FCM token saved successfully"},
    "no_fcm_token":           {"ar": "المستخدم لا يملك رمز اشعار. استدع POST /register-token اولا.", "en": "User has no registered FCM token. Call POST /register-token first."},
    "fcm_token_expired":      {"ar": "رمز FCM لم يعد صالحا. يرجى اعادة التسجيل عبر POST /register-token.", "en": "FCM token is no longer valid. Please re-register via POST /register-token."},
    "firebase_not_configured":{"ar": "Firebase غير مفعّل - تم حفظ الخطة لكن لم يرسل اشعار.", "en": "Firebase is not configured - plan saved but no push notification sent."},
    "no_assessment_found":    {"ar": "لا يوجد تقييم لهذا المستخدم. اكمل التقييم عبر POST /assessment/submit اولا.", "en": "No assessment found for this user. Please complete an assessment first via POST /assessment/submit."},
    "no_plan_found":          {"ar": "لا توجد خطة تربوية لهذا المستخدم.", "en": "No parenting plan found for this user."},
    "user_not_found":         {"ar": "المستخدم غير موجود.", "en": "User not found."},
    "pdf_unavailable":        {"ar": "تصدير PDF غير متاح - مكتبة reportlab غير مثبتة.", "en": "PDF export is unavailable - reportlab is not installed. Run: pip install reportlab"},
    "pdf_main_title":         {"ar": "خطة تربوية مخصصة - رفيق AI", "en": "Personalised Parenting Plan - Rafiq AI"},
    "pdf_subtitle":           {"ar": "خطة 30 يوما", "en": "30-Day Plan"},
    "pdf_label_user_id":      {"ar": "معرف المستخدم", "en": "User ID"},
    "pdf_label_child_age":    {"ar": "عمر الطفل", "en": "Child Age"},
    "pdf_label_archetype":    {"ar": "النمط الشخصي", "en": "Top Archetype"},
    "pdf_label_generated":    {"ar": "تاريخ الانشاء", "en": "Generated"},
    "pdf_label_age_unknown":  {"ar": "غير محدد", "en": "Not specified"},
    "pdf_section_plan":       {"ar": "الخطة التربوية", "en": "Parenting Plan"},
    "pdf_footer_line1":       {"ar": "انشئت بواسطة رفيق AI - هذه الخطة ارشادية وليست تشخيصا طبيا.", "en": "Generated by Rafiq AI - This plan is for guidance only and is not a clinical diagnosis."},
    "card_out_of_scope":      {"ar": "خارج نطاق رفيق", "en": "Out of scope"},
    "card_important":         {"ar": "مهم جدا", "en": "Important"},
    "card_tip":               {"ar": "نصيحة عملية", "en": "Practical Tip"},
    "card_story":             {"ar": "قصة للاطفال", "en": "Kids Story"},
    "card_game":              {"ar": "لعبة / نشاط", "en": "Activity / Game"},
    "card_books":             {"ar": "اقتراح قراءة", "en": "Book Suggestion"},
    "card_assessment":        {"ar": "تقييم شخصية الطفل", "en": "Personality Assessment"},
    "card_refusal_reason_prefix": {"ar": "السبب: ", "en": "Reason: "},
    "child_appropriate_content":  {"ar": "محتوى مناسب للاطفال", "en": "Child-appropriate content"},
    "choose_safe_topic":          {"ar": "اختر موضوعا مناسبا للاطفال.", "en": "Choose a safe, age-appropriate topic."},
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
    ar = len(re.findall(r"[\u0600-\u06FF]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return "ar" if ar >= en else "en"


def user_lang(preferred_language: Optional[str], fallback_text: str = "") -> Lang:
    if preferred_language in ("ar", "en"):
        return preferred_language  # type: ignore[return-value]
    return detect_lang(fallback_text)

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured.")
    return psycopg2.connect(DATABASE_URL)

# ---------------------------------------------------------------------------
# STATIC KB (in-memory parenting tips)
# ---------------------------------------------------------------------------

KB: List[Dict[str, Any]] = [
    {"id": "kb_anger_01", "topic": "anger", "age_min": 4, "age_max": 18,
     "tags": ["anger", "calm", "outburst", "emotion", "regulation", "temper", "rage", "explosion", "trigger"],
     "tip": "When your child has an outburst, wait until they're calm before discussing rules. Reacting immediately escalates conflict. Try the 'pause and reconnect' technique: give 5-10 minutes of quiet space, then approach with empathy first."},
    {"id": "kb_anger_02", "topic": "anger", "age_min": 4, "age_max": 12,
     "tags": ["anger", "calm", "feelings", "chart", "emotion", "young", "child", "breathing"],
     "tip": "Use a 'feelings thermometer' to help children identify anger levels before they explode. Point to a simple 1-5 chart each day and ask where they are — this builds emotional awareness that prevents outbursts."},
    {"id": "kb_screen_01", "topic": "screen_addiction", "age_min": 4, "age_max": 18,
     "tags": ["screen", "phone", "mobile", "addiction", "games", "youtube", "tiktok", "limit", "time", "internet"],
     "tip": "Replace screen battles with the '1:1 rule': for every hour of screen time, the child spends one hour on any offline activity of their choice. This maintains autonomy while building real-world skills. Gradually reduce ratios over weeks."},
    {"id": "kb_screen_02", "topic": "screen_addiction", "age_min": 8, "age_max": 18,
     "tags": ["screen", "phone", "internet", "social", "media", "limit", "teen", "boundary"],
     "tip": "Create a 'Tech-Free Zone' agreement together — not as a punishment but as a family value. Include all adults to model the behavior. Common zones: dinner table, bedrooms after 9 pm. Let the child help design the rules."},
    {"id": "kb_teen_01", "topic": "teen_communication", "age_min": 12, "age_max": 18,
     "tags": ["teen", "teenager", "communication", "silent", "distance", "talk", "relationship", "trust", "listen"],
     "tip": "Teens respond to curiosity, not interrogation. Replace 'How was school?' with a specific open question: 'What was the most annoying thing that happened today?' Specific, low-stakes questions invite more honest answers."},
    {"id": "kb_teen_02", "topic": "teen_communication", "age_min": 12, "age_max": 18,
     "tags": ["teen", "communicate", "trust", "argue", "fight", "rebellion", "listen", "respect"],
     "tip": "The 'car conversation' technique: have important talks while driving or walking side-by-side, not face-to-face. Removing direct eye contact reduces the confrontational feel and helps teens open up more naturally."},
    {"id": "kb_bully_01", "topic": "bullying", "age_min": 6, "age_max": 18,
     "tags": ["bullying", "bully", "hurt", "school", "victim", "social", "protect", "peer", "abuse"],
     "tip": "Teach the 'STOP, WALK, TALK' model: stop engaging with the bully, walk away to a safe place, then talk to a trusted adult. Rehearse this as a role-play at home so the child has a practiced, automatic response."},
    {"id": "kb_study_01", "topic": "study_focus", "age_min": 6, "age_max": 18,
     "tags": ["study", "homework", "focus", "concentration", "school", "distraction", "attention", "learn", "read"],
     "tip": "Use the Pomodoro method adapted for children: 20 minutes of focused study followed by a 5-minute break with a physical activity (stretch, water, snack). After 3 cycles, offer a longer 20-minute break. This prevents mental fatigue."},
    {"id": "kb_story_01", "topic": "kids_stories", "age_min": 3, "age_max": 8,
     "tags": ["story", "bedtime", "read", "book", "tale", "narrative", "young", "child", "imagination"],
     "tip": "The Rabbit Who Lost His Colours: A little rabbit wakes up one morning to find all colours have vanished from the world. By performing one kind act for each friend he meets, a colour returns to the world. By the end, the rainbow is restored. Message: kindness creates beauty."},
    {"id": "kb_game_01", "topic": "activities_games", "age_min": 4, "age_max": 10,
     "tags": ["game", "activity", "play", "indoor", "family", "creative", "fun", "emotion"],
     "tip": "Emotion Charades: write emotion words on cards (happy, scared, frustrated, proud). Each player draws a card and acts out the emotion without speaking while others guess. This builds emotional vocabulary and empathy through play."},
    {"id": "kb_books_01", "topic": "book_recommendations", "age_min": 4, "age_max": 8,
     "tags": ["book", "read", "recommend", "library", "story", "young", "child", "picture"],
     "tip": "Recommended reads for ages 4-8: 'The Colour Monster' by Anna Llenas (emotions), 'Enemy Pie' by Derek Munson (friendship and judgement), 'Each Kindness' by Jacqueline Woodson (regret and compassion). All three spark excellent conversations."},
    {"id": "kb_assessment_01", "topic": "assessment_personality", "age_min": 4, "age_max": 18,
     "tags": ["assessment", "personality", "type", "traits", "leader", "social", "curious", "sensitive"],
     "tip": "We can run a personality assessment to help you understand your child better. Call GET /assessment/questions?age=X then POST /assessment/submit with the answers."},
]

# ---------------------------------------------------------------------------
# PYDANTIC SCHEMAS
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    user_id: str
    messages: List[ChatMessage]
    child_age: Optional[int] = None
    preferred_language: Optional[str] = None

class ChatResponse(BaseModel):
    message_id: str
    reply: str
    cards: List[Dict[str, Any]] = []

class UserUpsertReq(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    child_age: Optional[int] = None
    preferred_language: Optional[str] = "ar"

class AppEventRequest(BaseModel):
    user_id: str
    event_name: Literal[
        "open_app", "view_content", "save_tip", "start_chat", "complete_activity",
        "behavior_event", "view_assessment", "assessment_submit",
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

# RAG schemas
class RagKbAddRequest(BaseModel):
    question: str
    answer: str
    category: str

class RagKbSearchRequest(BaseModel):
    query: str
    limit: int = 5

class RagChatRequest(BaseModel):
    user_id: str
    question: str
    preferred_language: Optional[str] = None

class RagChatResponse(BaseModel):
    answer: str
    sources: List[Dict[str, Any]] = []
    used_rag: bool = False

# Router decision schema
AllowedTopic = Literal[
    "teen_communication", "anger", "screen_addiction", "bullying", "study_focus",
    "siblings_jealousy", "parents_conflict", "lying", "general_parenting",
    "kids_stories", "activities_games", "book_recommendations",
    "assessment_personality", "out_of_scope",
]
AllowedAction = Literal[
    "answer_with_tips", "recommend_booking", "refuse_out_of_scope",
]

class RouteDecision(BaseModel):
    in_scope: bool          = Field(description="Is question within Rafiq scope?")
    topic: AllowedTopic     = Field(description="Detected topic")
    action: AllowedAction   = Field(description="Action to take")
    extracted_child_age: Optional[int] = Field(default=None)
    reason: str             = Field(description="Short reason")

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

PARENTING_TOPICS    = {
    "teen_communication", "anger", "screen_addiction", "bullying",
    "study_focus", "siblings_jealousy", "parents_conflict",
    "lying", "general_parenting",
}
KIDS_CONTENT_TOPICS = {"kids_stories", "activities_games", "book_recommendations"}
ASSESSMENT_TOPIC    = "assessment_personality"
ALL_TRAITS          = [
    "leadership", "sociability", "empathy", "self_control",
    "focus", "curiosity", "adaptability", "sensitivity",
]

OUT_OF_SCOPE_KW = [
    "برمجة", "كود", "flutter", "android", "python", "java", "c++",
    "backend", "front", "database", "debug", "algorithm",
]
MEDICAL_KW = [
    "جرعة", "دواء", "حبوب", "مضاد", "تشخيص", "روشتة", "وصفة",
    "medication", "diagnosis",
]
KIDS_UNSAFE_KW = ["انتحار", "إباحية", "اباحية", "سلاح", "مخدرات"]
RISK_HIGH_KW   = [
    "عايز أموت", "مش عايز أعيش", "هأذي نفسي", "انتحار",
    "هنتحر", "هقتل", "هموت", "أذي نفسي",
]
RISK_MEDIUM_KW = [
    "خوف شديد", "هلع", "نوبات", "قلق جامد", "اكتئاب",
    "حزين طول الوقت", "مش قادر", "مخنوق طول الوقت",
]

# ---------------------------------------------------------------------------
# GUARD UTILITIES
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# IN-MEMORY KB SEARCH
# ---------------------------------------------------------------------------

_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")
_AR_PUNCT      = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)
_AR_STOPWORDS  = {
    "في", "من", "على", "عن", "الى", "إلى", "هو", "هي", "ده", "دي", "دا",
    "انا", "انت", "انتي", "احنا", "هم",
}

def _ar_normalize(text: str) -> str:
    if not text:
        return ""
    t_ = _AR_DIACRITICS.sub("", text.strip())
    for a, b in [("أ","ا"),("إ","ا"),("آ","ا"),("ى","ي"),("ة","ه"),("ؤ","و"),("ئ","ي"),("ـ","")]:
        t_ = t_.replace(a, b)
    return re.sub(r"\s+", " ", _AR_PUNCT.sub(" ", t_.lower())).strip()

def _tokenize(text: str) -> List[str]:
    return [w for w in _ar_normalize(text).split() if len(w) >= 2 and w not in _AR_STOPWORDS]

def _score_kb_item(q_tokens: List[str], item: Dict[str, Any]) -> int:
    if not q_tokens:
        return 1
    tags = _ar_normalize(" ".join(item.get("tags", [])))
    tip  = _ar_normalize(item.get("tip", ""))
    both = tags + " " + tip
    score = sum(6 if tok in tags else (4 if tok in tip else 0) for tok in q_tokens)
    if all(tok in both for tok in q_tokens[:3]):
        score += 6
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
        if topic and item["topic"] != topic:
            continue
        if age is not None and not (item["age_min"] <= age <= item["age_max"]):
            continue
        s = _score_kb_item(tokens, item)
        if tokens and s > 0:
            scored.append((s, item))
        elif not tokens:
            scored.append((s, item))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        top     = [i for _, i in scored[:3]]
        matched = scored[0][0] >= 6 if tokens else True
        return KbSearchResult(tips=top, matched=matched, match_count=len(scored), used_default=not bool(tokens))
    defaults = [x for x in KB if x["topic"] == topic][:3]
    return KbSearchResult(tips=defaults, matched=False, match_count=0, used_default=True)

# ---------------------------------------------------------------------------
# USER / MEMORY
# ---------------------------------------------------------------------------

def ensure_user_exists(conn, user_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, notes) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, json.dumps([])),
    )
    conn.commit()

def get_memory(conn, user_id: str) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        "SELECT notes, child_age, name, email, preferred_language FROM users WHERE user_id=%s",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"child_age": None, "name": None, "email": None, "notes": [],
                "last_summary": "", "preferred_language": "ar"}
    raw   = row[0]
    notes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return {
        "child_age":          row[1],
        "name":               row[2],
        "email":              row[3],
        "notes":              notes,
        "last_summary":       "",
        "preferred_language": row[4] or "ar",
    }

def update_memory(conn, user_id: str, topic: str, child_age: Optional[int], note: str = "") -> None:
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
        (json.dumps(notes), child_age, user_id),
    )
    conn.commit()

# ---------------------------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------------------------

def log_event(conn, user_id: str, event_type: str, value: str = "") -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analytics (event_id, user_id, event_type, value) VALUES (%s,%s,%s,%s)",
        ("ev_" + uuid.uuid4().hex[:10], user_id, event_type, value[:300]),
    )
    conn.commit()

# ---------------------------------------------------------------------------
# ASSESSMENT ENGINE
# ---------------------------------------------------------------------------

ASSESSMENT_OPTIONS = ["Never", "Rarely", "Sometimes", "Often", "Always"]

ASSESSMENT_QUESTIONS: List[Dict[str, Any]] = [
    {"id": "q01", "trait": "focus",        "age_min": 4,  "age_max": 18, "weights": {"focus": 2},                      "text": "My child stays focused on a task until it is completed."},
    {"id": "q02", "trait": "focus",        "age_min": 7,  "age_max": 18, "weights": {"focus": 2, "self_control": 1},   "text": "My child finishes homework or assignments before switching to play."},
    {"id": "q03", "trait": "focus",        "age_min": 4,  "age_max": 18, "weights": {"focus": 3},                      "text": "My child can sit quietly and concentrate during story time or a lesson."},
    {"id": "q04", "trait": "empathy",      "age_min": 4,  "age_max": 18, "weights": {"empathy": 2},                    "text": "My child notices when a friend or sibling is upset and tries to comfort them."},
    {"id": "q05", "trait": "empathy",      "age_min": 6,  "age_max": 18, "weights": {"empathy": 2, "sociability": 1},  "text": "My child apologizes genuinely after hurting someone's feelings."},
    {"id": "q06", "trait": "empathy",      "age_min": 4,  "age_max": 18, "weights": {"empathy": 3},                    "text": "My child shows concern for animals or people who are struggling."},
    {"id": "q07", "trait": "curiosity",    "age_min": 4,  "age_max": 18, "weights": {"curiosity": 2},                  "text": "My child frequently asks 'why' or 'how' questions about the world."},
    {"id": "q08", "trait": "curiosity",    "age_min": 6,  "age_max": 18, "weights": {"curiosity": 2, "adaptability": 1}, "text": "My child enjoys trying new activities or experimenting with new ideas."},
    {"id": "q09", "trait": "curiosity",    "age_min": 4,  "age_max": 18, "weights": {"curiosity": 3},                  "text": "My child enjoys solving puzzles, riddles, or figuring things out independently."},
    {"id": "q10", "trait": "leadership",   "age_min": 5,  "age_max": 18, "weights": {"leadership": 2},                 "text": "My child naturally takes charge and organizes activities when playing with others."},
    {"id": "q11", "trait": "leadership",   "age_min": 8,  "age_max": 18, "weights": {"leadership": 2, "focus": 1},     "text": "My child steps up to help make decisions in group settings."},
    {"id": "q12", "trait": "leadership",   "age_min": 5,  "age_max": 18, "weights": {"leadership": 3},                 "text": "My child is comfortable taking responsibility for a task or group project."},
    {"id": "q13", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 2},                "text": "My child makes friends quickly and easily in new environments."},
    {"id": "q14", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 2, "empathy": 1},  "text": "My child enjoys being around others and actively seeks social interaction."},
    {"id": "q15", "trait": "sociability",  "age_min": 4,  "age_max": 18, "weights": {"sociability": 3},                "text": "My child is comfortable sharing, taking turns, and cooperating in group play."},
    {"id": "q16", "trait": "adaptability", "age_min": 4,  "age_max": 18, "weights": {"adaptability": 2},               "text": "My child adjusts well to changes in routine (new school, travel, schedule changes)."},
    {"id": "q17", "trait": "adaptability", "age_min": 6,  "age_max": 18, "weights": {"adaptability": 2, "self_control": 1}, "text": "When plans change unexpectedly, my child handles it calmly."},
    {"id": "q18", "trait": "self_control", "age_min": 4,  "age_max": 18, "weights": {"self_control": 2},               "text": "My child can calm themselves down after getting upset without adult intervention."},
    {"id": "q19", "trait": "self_control", "age_min": 6,  "age_max": 18, "weights": {"self_control": 3},               "text": "My child resists the urge to act impulsively (e.g., waits their turn, thinks before acting)."},
    {"id": "q20", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 2},                "text": "My child gets upset easily by criticism, loud noises, or unexpected changes."},
    {"id": "q21", "trait": "sensitivity",  "age_min": 4,  "age_max": 18, "weights": {"sensitivity": 3},                "text": "My child feels emotions deeply and needs extra reassurance after conflict or disappointment."},
]

_QS_NORM: Dict[str, Dict[str, Any]] = {q["id"].strip().lower(): q for q in ASSESSMENT_QUESTIONS}

ARCHETYPES: List[Dict[str, Any]] = [
    {"id": "leader",      "name": "The Leader",      "description": "Takes initiative, organizes peers, and thrives when given responsibility.",           "needs": "Clear boundaries, meaningful responsibilities, and leadership opportunities.",               "profile": {"leadership": 80, "focus": 60, "sociability": 55},    "traits_focus": ["leadership", "focus"]},
    {"id": "explorer",    "name": "The Explorer",    "description": "Curious, adventurous, and constantly seeking new experiences and knowledge.",          "needs": "New challenges, hands-on projects, and freedom to experiment.",                            "profile": {"curiosity": 80, "adaptability": 65},                  "traits_focus": ["curiosity", "adaptability"]},
    {"id": "thinker",     "name": "The Thinker",     "description": "Reflective and analytical - prefers depth over breadth.",                             "needs": "Quiet time, intellectual challenges, and space for independent thought.",                   "profile": {"focus": 80, "curiosity": 65, "sociability": 30},      "traits_focus": ["focus", "curiosity"]},
    {"id": "helper",      "name": "The Helper",      "description": "Warm, caring, and highly attuned to the emotions of others.",                         "needs": "Recognition of emotional contributions and opportunities to support peers.",                "profile": {"empathy": 85, "sociability": 60},                     "traits_focus": ["empathy", "sociability"]},
    {"id": "peacemaker",  "name": "The Peacemaker",  "description": "Conflict-averse, diplomatic, and focused on harmony in relationships.",                "needs": "Teaching assertiveness, safe expression of opinions, and conflict resolution skills.",       "profile": {"empathy": 75, "self_control": 70},                    "traits_focus": ["empathy", "self_control"]},
    {"id": "energetic",   "name": "The Energetic",   "description": "High energy, enthusiastic, and socially motivated.",                                   "needs": "Physical outlets, structured energy release, and consistent boundaries.",                   "profile": {"sociability": 75, "curiosity": 60, "self_control": 35}, "traits_focus": ["sociability", "self_control"]},
    {"id": "sensitive",   "name": "The Sensitive",   "description": "Deeply empathetic and emotionally aware - feels things intensely.",                   "needs": "Emotional validation, predictable routines, and a calm safe environment.",                  "profile": {"sensitivity": 85, "empathy": 65},                     "traits_focus": ["sensitivity", "empathy"]},
    {"id": "independent", "name": "The Independent", "description": "Values autonomy and personal space - prefers doing things on their own terms.",        "needs": "Structured choices, respected boundaries, and gradual responsibility.",                     "profile": {"leadership": 55, "sociability": 25, "focus": 60},     "traits_focus": ["leadership", "focus"]},
    {"id": "planner",     "name": "The Planner",     "description": "Orderly, methodical, and motivated by structure, routine, and clear goals.",           "needs": "Simple schedules, clear expectations, and positive reinforcement for progress.",            "profile": {"focus": 85, "self_control": 75},                      "traits_focus": ["focus", "self_control"]},
    {"id": "challenger",  "name": "The Challenger",  "description": "Questions authority, tests limits, and learns best through debate and negotiation.",   "needs": "Few but firm rules, negotiation space, and consistent logical consequences.",               "profile": {"leadership": 65, "self_control": 30, "sensitivity": 50}, "traits_focus": ["leadership", "self_control"]},
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
    return [{"id": q["id"], "text": q["text"], "trait": q["trait"], "options": ASSESSMENT_OPTIONS} for q in questions]

def compute_personality_profile(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw:  Dict[str, float] = {tr: 0.0 for tr in ALL_TRAITS}
    max_: Dict[str, float] = {tr: 0.0 for tr in ALL_TRAITS}
    matched_ids: List[str]   = []
    unmatched_ids: List[str] = []

    for a in answers:
        qid_raw = a.get("question_id") or a.get("id")
        qid     = _normalize_answer_id(qid_raw)
        val     = _extract_answer_value(a)
        q       = _QS_NORM.get(qid)
        if q is None:
            unmatched_ids.append(str(qid_raw))
            continue
        if val is None:
            unmatched_ids.append(f"{qid_raw}(bad_value)")
            continue
        matched_ids.append(qid)
        for trait, w in q["weights"].items():
            raw[trait]  += val * w
            max_[trait] += 5 * w

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
        key=lambda x: x["match_pct"],
        reverse=True,
    )
    top_archetype    = ranked[0]
    top_traits       = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    low_traits       = sorted(scores.items(), key=lambda kv: kv[1])[:2]
    recommendations  = _build_recommendations(scores, top_archetype, low_traits)

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

def compute_assessment_confidence(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    all_qs = ASSESSMENT_QUESTIONS
    q_ids  = {q["id"].strip().lower() for q in all_qs}
    total  = len(all_qs)
    valid  = 0
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

    coverage = int(round(valid / total * 100)) if total else 0
    score    = int(round(valid / total * 65))  if total else 0
    notes    = [f"coverage={coverage}%"]
    if child_age is not None:
        score += 15; notes.append("age_provided")
    if behavior_signals:
        score += 10; notes.append("behavior_signals_included")
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

# ---------------------------------------------------------------------------
# FOLLOW-UP QUESTIONS
# ---------------------------------------------------------------------------

FOLLOW_UP_BANK: Dict[str, List[str]] = {
    "anger":                  ["When does the anger peak most? (before bed / after school / screen time)", "What usually happens in the 60 seconds before the outburst?"],
    "screen_addiction":       ["How many hours per day approximately? What mostly (YouTube/games/TikTok)?", "Is there a specific time cutting off causes the biggest reaction?"],
    "teen_communication":     ["When is your teen most calm and open to talking?", "Is it that they don't respond at all, or respond with anger?"],
    "bullying":               ["Where does the bullying happen most? (classroom/bus/club)", "Is there any adult at school your child already trusts?"],
    "study_focus":            ["How many minutes can they focus before getting distracted?", "Which subject creates the most resistance?"],
    "kids_stories":           ["How old is the child so I can pick the right story?", "What theme do you prefer — honesty, sharing, courage, or respect?"],
    "activities_games":       ["Do you prefer a calm activity or something active and physical?", "Do you have simple supplies like paper, pencils, or building blocks?"],
    "book_recommendations":   ["How old is your child and what kind of stories do they enjoy?", "Values-based books or adventure stories?"],
    "assessment_personality": ["Would you like to start a quick personality assessment?", "How old is your child so I can tailor the questions?"],
    "general_parenting":      ["How old is your child?", "When does the situation occur most often and what usually triggers it?"],
}

def pick_followups(topic: str) -> List[str]:
    return (FOLLOW_UP_BANK.get(topic) or ["Can you share a recent situation?", "How old is your child?"])[:2]

# ---------------------------------------------------------------------------
# CONFIDENCE SCORING
# ---------------------------------------------------------------------------

def compute_confidence(
    topic: str, kb_res: KbSearchResult, age: Optional[int],
    user_text: str, in_scope: bool, risk_level: str,
) -> int:
    score = 40
    if in_scope and topic != "out_of_scope":                                        score += 15
    if age is not None:                                                              score += 10
    if kb_res.matched:                                                               score += 25 + min(10, kb_res.match_count * 3)
    elif kb_res.used_default and topic in (KIDS_CONTENT_TOPICS | {ASSESSMENT_TOPIC}): score += 15
    else:                                                                            score -= 10
    if len((user_text or "").split()) >= 10:                                         score += 5
    if risk_level == "medium":                                                       score -= 10
    elif risk_level == "high":                                                       score -= 25
    return max(0, min(100, score))

# ---------------------------------------------------------------------------
# EMPATHY REFLECTION
# ---------------------------------------------------------------------------

_EMPATHY_AR: Dict[str, str] = {
    "anger":               "واضح ان الموضوع ده متعبك وبيستنزف اعصابك.",
    "screen_addiction":    "حاسّة بقلقك من موضوع الشاشات وتاثيره عليه.",
    "teen_communication":  "واضح ان قلة التواصل مضايقاكي وبتوجع.",
    "bullying":            "طبيعي تقلقي جدا لما تحسي ان ابنك بيتاذى.",
    "study_focus":         "الاحساس بالحيرة مع المذاكرة بيكون مرهق فعلا.",
    "general_parenting":   "الامومة مليانة مواقف بتخلينا نحتار.",
}
_EMPATHY_EN: Dict[str, str] = {
    "anger":               "It sounds exhausting — dealing with these outbursts takes so much energy.",
    "screen_addiction":    "Screen time worries are so common right now, and your concern makes complete sense.",
    "teen_communication":  "That distance from your teen can feel really painful. You're not alone in this.",
    "bullying":            "It's completely natural to feel alarmed when your child is being hurt.",
    "study_focus":         "The homework struggle is real — it's draining for the whole family.",
    "kids_stories":        "How lovely that you want to share a special story moment together.",
    "activities_games":    "It's great that you're looking for meaningful ways to engage with your child.",
    "assessment_personality": "Understanding your child better is one of the kindest things you can do for them.",
    "general_parenting":   "Parenting is full of moments that leave us uncertain — you're doing the right thing.",
}

def empathy_reflect(user_text: str, topic: str, risk_level: str, lang: Lang) -> str:
    if lang == "ar":
        empathy = _EMPATHY_AR.get(topic, "حاسة بيكي، والموضوع ده مش سهل.")
        if risk_level == "medium":
            empathy += " خلينا نمشي بهدوء ونفهم الصورة كاملة."
        snippet = (user_text[:77] + "...") if len(user_text) > 80 else user_text
        return f"{empathy}\n\nانتِ بتقوليلي: \"{snippet}\"\n"
    else:
        empathy = _EMPATHY_EN.get(topic, "I hear you — this situation sounds genuinely challenging.")
        if risk_level == "medium":
            empathy += " Let's go through this carefully together."
        snippet = (user_text[:77] + "...") if len(user_text) > 80 else user_text
        return f"{empathy}\n\nYou said: \"{snippet}\"\n"

# ---------------------------------------------------------------------------
# GEMINI HELPERS
# ---------------------------------------------------------------------------

def _require_gemini() -> None:
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini disabled: set GEMINI_API_KEY")

def _lang_instruction(lang: Lang) -> str:
    if lang == "ar":
        return "Reply in warm, clear Modern Standard Arabic (Egyptian dialect warmth is welcome)."
    return "Reply in clear, warm, professional English."

def gemini_route_decision(user_text: str, history: List[ChatMessage], fallback_age: Optional[int]) -> RouteDecision:
    _require_gemini()
    system = (
        "You are the router for Rafiq, a family support assistant. "
        "Rafiq only handles: family communication, parenting, teen issues, anger, screen addiction, "
        "bullying, study focus, sibling jealousy, parent conflict, lying, kids stories, educational games, "
        "book recommendations for children, and child personality assessment.\n"
        "Forbidden: programming/tech, medical diagnosis, medications.\n"
        "If out of scope: action=refuse_out_of_scope, in_scope=false.\n"
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
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,        threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,       threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
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

def gemini_compose_answer(
    user_text: str, topic: str, tips: List[Dict], memory: Dict,
    followups: List[str], confidence: int, risk_level: str, lang: Lang,
) -> str:
    _require_gemini()
    payload = {
        "topic": topic, "tips": tips, "memory": memory,
        "followups": followups, "confidence": confidence, "risk_level": risk_level,
    }
    system = (
        f"You are Rafiq, a supportive family assistant. {_lang_instruction(lang)}\n"
        "Rules: NO diagnosis, NO medication advice, NO programming.\n"
        "Use ONLY the data provided in ALLOWED DATA.\n"
        "If confidence < 65 or tips empty: give a short empathetic reply + ONE follow-up question.\n"
        "If confidence >= 65: give 2-3 practical bullet points + ONE follow-up.\n"
        "Max 350 words."
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{system}\n\nUSER:\n{user_text}\n\nALLOWED DATA:\n{json.dumps(payload, ensure_ascii=False)}",
        config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=500),
    )
    fallback = "ممكن تقوليلي تفاصيل اكتر؟" if lang == "ar" else "Could you share more details?"
    return (resp.text or "").strip() or fallback

def gemini_verify_answer(user_text: str, answer: str, allowed_payload: Dict) -> Dict[str, Any]:
    _require_gemini()
    prompt = (
        f"Check if this reply violates Rafiq rules (no diagnosis/meds/programming).\n"
        f"Output ONLY JSON: {{\"ok\": true/false, \"reason\": \"brief\"}}\n\n"
        f"USER:\n{user_text}\n\nANSWER:\n{answer}\n\nALLOWED:\n{json.dumps(allowed_payload, ensure_ascii=False)}"
    )
    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0, max_output_tokens=150
        ),
    )
    try:
        data = json.loads(r.text)
        return {"ok": bool(data.get("ok", True)), "reason": str(data.get("reason", ""))}
    except Exception:
        return {"ok": True, "reason": ""}

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
            "انت مدرب تربوي محترف متخصص في التطوير الشخصي للاطفال.\n\n"
            "فيما يلي نتائج تقييم شخصية الطفل:\n"
            f"- عمر الطفل: {child_age if child_age is not None else 'غير محدد'} سنة\n"
            f"- النمط الشخصي الابرز: {top_archetype} — {archetype_desc}\n"
            f"- احتياجات الطفل: {archetype_needs}\n\n"
            f"ابرز الصفات:\n{traits_text}\n\n"
            f"درجات جميع الصفات:\n{scores_text}\n\n"
            "المطلوب: انشئ خطة تربوية مخصصة لمدة 30 يوما (4 اسابيع).\n"
            "يجب ان تتضمن الخطة:\n"
            "1. هدف الاسبوع\n"
            "2. انشطة يومية عملية ومناسبة لعمر الطفل\n"
            "3. اساليب التعزيز الايجابي\n"
            "4. توصيات خاصة بالوالدين\n"
            "5. ملاحظة ختامية للمتابعة\n\n"
            "الاسلوب: دافئ، واضح، وعملي. تجنب المصطلحات الطبية.\n"
            "اعد الخطة كاملة باللغة العربية."
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
            "Task: Create a personalised 30-day parenting plan (4 weeks) based on this data.\n"
            "The plan must include:\n"
            "1. Weekly goal\n"
            "2. Daily practical activities appropriate for the child's age\n"
            "3. Positive reinforcement strategies per week\n"
            "4. Specific recommendations for parents to support the child\n"
            "5. A closing note for follow-up after the plan ends\n\n"
            "Style: warm, clear, practical. Avoid medical/diagnostic terminology.\n"
            "Write the entire plan in English."
        )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.6, max_output_tokens=2000),
    )
    return (resp.text or "").strip()

# ---------------------------------------------------------------------------
# RAG SERVICES  (drop-in replacement for the broken section in main.py)
# ---------------------------------------------------------------------------
# This patch replaces everything from:
#     EMBEDDING_MODEL = "gemini-embedding-exp-03-07"
# down to (but not including):
#     def rag_insert_entry(...)
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "gemini-embedding-exp-03-07"
EMBEDDING_DIM = 3072


def generate_embedding(text: str) -> List[float]:
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    if not GEMINI_ENABLED or client is None:
        raise HTTPException(
            status_code=503,
            detail="Gemini disabled: set GEMINI_API_KEY",
        )

    try:
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=[text],
        )
        return result.embeddings[0].values

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Embedding generation failed: {exc}",
        )


def ensure_faq_kb_table(conn) -> None:
    """
    Create the faq_knowledge_base table and its ivfflat index if they do not
    already exist.  Safe to call on every request (all statements are
    idempotent).
    """
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS faq_knowledge_base (
            id        SERIAL PRIMARY KEY,
            question  TEXT        NOT NULL,
            answer    TEXT        NOT NULL,
            category  TEXT        NOT NULL DEFAULT '',
            embedding vector({EMBEDDING_DIM}),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # ivfflat index — requires at least one row to build, so we use
    # CREATE INDEX IF NOT EXISTS which is a no-op when it already exists.
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS faq_kb_embedding_idx
        ON faq_knowledge_base
        USING ivfflat (embedding vector_cosine_ops)
        """
    )
    conn.commit()


def init_db() -> None:
    """
    One-time initialisation helper (optional – call from a startup event or
    a management script, not on every request).
    """
    conn = get_conn()
    try:
        ensure_faq_kb_table(conn)
    finally:
        conn.close()

def rag_insert_entry(conn, question: str, answer: str, category: str, embedding: List[float]) -> int:
    """Insert a FAQ entry with its embedding into the database."""
    cur = conn.cursor()
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
    cur.execute(
        "INSERT INTO faq_knowledge_base (question, answer, category, embedding) "
        "VALUES (%s, %s, %s, %s::vector) RETURNING id",
        (question, answer, category, embedding_str),
    )
    row = cur.fetchone()
    conn.commit()
    return row[0]


def rag_semantic_search(conn, query_embedding: List[float], limit: int = 5) -> List[Dict[str, Any]]:
    """Perform cosine similarity search using pgvector."""
    cur = conn.cursor()
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    cur.execute(
        """
        SELECT id, question, answer, category,
               1 - (embedding <=> %s::vector) AS similarity
        FROM faq_knowledge_base
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (embedding_str, embedding_str, limit),
    )
    rows = cur.fetchall()
    return [
        {
            "id":         row[0],
            "question":   row[1],
            "answer":     row[2],
            "category":   row[3],
            "similarity": float(row[4]),
        }
        for row in rows
    ]


def rag_build_context(results: List[Dict[str, Any]]) -> str:
    """Build a context string from retrieved FAQ results."""
    if not results:
        return ""
    parts = []
    for i, r in enumerate(results, start=1):
        parts.append(f"[{i}] Q: {r['question']}\n    A: {r['answer']}")
    return "\n\n".join(parts)


def rag_llm_answer(question: str, context: str, lang: Lang) -> str:
    """Send context and question to Gemini and return the final answer."""
    _require_gemini()
    if context:
        system = (
            f"You are Rafiq, a helpful family support assistant. {_lang_instruction(lang)}\n"
            "Answer the user's question using ONLY the provided context.\n"
            "If the context does not fully address the question, supplement with general knowledge "
            "but stay within the parenting and family support domain.\n"
            "Be warm, concise, and practical. Max 300 words."
        )
        prompt = f"{system}\n\nCONTEXT:\n{context}\n\nUSER QUESTION:\n{question}"
    else:
        system = (
            f"You are Rafiq, a helpful family support assistant. {_lang_instruction(lang)}\n"
            "No specific FAQ was found. Answer the question directly using your general knowledge "
            "about parenting and family support.\n"
            "Be warm, concise, and practical. Max 300 words."
        )
        prompt = f"{system}\n\nUSER QUESTION:\n{question}"

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=500),
    )
    fallback = "يمكنك مشاركة تفاصيل اكتر؟" if lang == "ar" else "Could you share more details?"
    return (resp.text or "").strip() or fallback

# ---------------------------------------------------------------------------
# PDF HELPERS
# ---------------------------------------------------------------------------

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

def _pick_font(bold: bool, lang: Lang = "en") -> str:
    if _FONT_ARABIC_REGISTERED or _FONT_LATIN_REGISTERED:
        return "RafiqBold" if bold else "RafiqRegular"
    return "Helvetica-Bold" if bold else "Helvetica"

def _build_parenting_plan_pdf(
    user_id: str,
    child_age: Optional[int],
    top_archetype: str,
    plan_text: str,
    generated_at: str,
    lang: Lang = "en",
) -> bytes:
    buf  = io.BytesIO()
    W, H = A4
    styles = getSampleStyleSheet()

    text_align  = TA_RIGHT if lang == "ar" else TA_LEFT
    brand_green = colors.HexColor("#1B6B3A")
    brand_light = colors.HexColor("#E8F5E9")
    text_dark   = colors.HexColor("#1A1A1A")
    text_muted  = colors.HexColor("#555555")
    accent_gold = colors.HexColor("#C8860A")
    font_body   = _pick_font(False, lang)
    font_bold   = _pick_font(True,  lang)

    style_subtitle = ParagraphStyle(
        "SubTitle", parent=styles["Normal"],
        fontSize=12, textColor=text_muted, spaceAfter=2, alignment=TA_CENTER, fontName=font_body,
    )
    style_section_heading = ParagraphStyle(
        "SectionHeading", parent=styles["Heading1"],
        fontSize=13, textColor=brand_green, spaceBefore=14, spaceAfter=4, fontName=font_bold,
    )
    style_plan_heading = ParagraphStyle(
        "PlanHeading", parent=styles["Heading2"],
        fontSize=12, textColor=accent_gold, spaceBefore=10, spaceAfter=3, fontName=font_bold,
    )
    style_plan_body = ParagraphStyle(
        "PlanBody", parent=styles["Normal"],
        fontSize=10.5, textColor=text_dark, fontName=font_body, leading=17, spaceAfter=4, alignment=text_align,
    )
    style_bullet = ParagraphStyle(
        "Bullet", parent=styles["Normal"],
        fontSize=10.5, textColor=text_dark, fontName=font_body, leading=17,
        leftIndent=16, spaceAfter=3, bulletIndent=4, alignment=text_align,
    )
    style_footer = ParagraphStyle(
        "Footer", parent=styles["Normal"],
        fontSize=8, textColor=text_muted, alignment=TA_CENTER, fontName=font_body,
    )

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm, topMargin=2.5*cm, bottomMargin=2*cm,
        title=f"Rafiq Parenting Plan — {user_id}", author="Rafiq AI",
    )
    story = []

    banner_title = _pdf_text(t("pdf_main_title", lang), lang)
    banner_sub   = _pdf_text(t("pdf_subtitle", lang), lang)
    banner_style = ParagraphStyle(
        "BannerTitle", parent=styles["Title"],
        fontSize=20, textColor=colors.white, alignment=TA_CENTER, fontName=font_bold,
    )
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
        else t("pdf_label_age_unknown", lang), lang,
    )
    date_display = generated_at[:10] if generated_at else "—"

    def lbl(k: str) -> str:
        return _pdf_text(t(k, lang), lang)

    lbl_style = ParagraphStyle("MetaLbl", parent=styles["Normal"], fontSize=9, textColor=brand_green, fontName=font_bold)
    val_style = ParagraphStyle("MetaVal", parent=styles["Normal"], fontSize=9, textColor=text_dark,  fontName=font_body)

    meta_data = [
        [Paragraph(lbl("pdf_label_user_id"),   lbl_style), Paragraph(user_id,     val_style),
         Paragraph(lbl("pdf_label_child_age"), lbl_style), Paragraph(age_display, val_style)],
        [Paragraph(lbl("pdf_label_archetype"), lbl_style), Paragraph(_pdf_text(top_archetype, lang), val_style),
         Paragraph(lbl("pdf_label_generated"), lbl_style), Paragraph(date_display, val_style)],
    ]
    full_w = W - 4*cm
    meta_table = Table(meta_data, colWidths=[full_w*0.18, full_w*0.32, full_w*0.18, full_w*0.32])
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

    week_keywords  = ("Week ", "الأسبوع", "أسبوع")
    bullet_markers = ("*", "-", "–", "•", "·")

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

# ---------------------------------------------------------------------------
# PROFILE NORMALISATION HELPERS
# ---------------------------------------------------------------------------

def _norm_traits(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({"trait": str(item.get("trait") or item.get("name") or ""), "score": int(item.get("score", 0))})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"trait": str(item[0]), "score": int(item[1])})
    return out

def _norm_personalities(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({
                "id":          str(item.get("id", "")),
                "name":        str(item.get("name", "Not specified")),
                "description": str(item.get("description", "")),
                "needs":       str(item.get("needs", "")),
                "match_pct":   int(item.get("match_pct") or item.get("match") or 0),
            })
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"id": str(item[0]), "name": str(item[0]), "description": "", "needs": "", "match_pct": int(item[1])})
    return out

def _norm_scores(raw: Any) -> Dict[str, int]:
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}
    out = {}
    for item in (raw or []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out[str(item[0])] = int(item[1])
    return out

# ---------------------------------------------------------------------------
# FCM HELPER
# ---------------------------------------------------------------------------

def _send_fcm_notification(
    user_id: str,
    title: str,
    body: str,
    data: Dict[str, str],
) -> Dict[str, Any]:
    if not _FIREBASE_AVAILABLE:
        return {"sent": False, "warning": "firebase-admin package not installed."}
    if not _FIREBASE_CREDS_JSON:
        return {"sent": False, "warning": "FIREBASE_CREDENTIALS env var is not set."}
    if not FIREBASE_ENABLED:
        return {"sent": False, "warning": "Firebase failed to initialise at startup."}

    notif_conn = None
    try:
        notif_conn = get_conn()
        nc = notif_conn.cursor()
        nc.execute("SELECT fcm_token FROM users WHERE user_id=%s", (user_id,))
        row   = nc.fetchone()
        token: Optional[str] = row[0] if row else None
        if not token:
            return {"sent": False, "warning": "No FCM token registered. Call POST /register-token first."}
        message = fb_messaging.Message(
            notification=fb_messaging.Notification(title=title, body=body),
            token=token,
            data=data,
        )
        fb_messaging.send(message)
        return {"sent": True, "warning": None}
    except AttributeError as ae:
        return {"sent": False, "warning": f"Firebase messaging object is None: {ae}"}
    except Exception as exc:
        err = str(exc)
        if "UNREGISTERED" in err.upper() or "registration-token-not-registered" in err:
            if notif_conn:
                try:
                    notif_conn.cursor().execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (user_id,))
                    notif_conn.commit()
                except Exception:
                    pass
            return {"sent": False, "warning": "FCM token expired/unregistered - cleared. User must re-register."}
        return {"sent": False, "warning": f"Firebase send error: {err}"}
    finally:
        if notif_conn:
            try:
                notif_conn.close()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# PARENTING PLAN CORE
# ---------------------------------------------------------------------------

def _generate_and_save_plan(
    conn,
    user_id: str,
    assessment_id: int,
    result: Dict[str, Any],
    child_age: Optional[int],
) -> Dict[str, Any]:
    PLAN_LANG: Lang = "en"

    top_traits             = _norm_traits(result.get("top_traits", []))
    possible_personalities = _norm_personalities(result.get("possible_personalities", []))
    trait_scores           = _norm_scores(result.get("trait_scores", {}))

    top_arch_entry  = possible_personalities[0] if possible_personalities else {}
    top_archetype   = top_arch_entry.get("name", "Not specified")
    archetype_desc  = top_arch_entry.get("description", "")
    archetype_needs = top_arch_entry.get("needs", "")

    traits_text = (
        "\n".join(f"  - {t_['trait'].replace('_', ' ').title()}: {t_['score']}%" for t_ in top_traits)
        or "  - No data"
    )
    scores_text = (
        "\n".join(f"  - {k.replace('_', ' ').title()}: {v}%" for k, v in trait_scores.items())
        or "  - No data"
    )

    try:
        plan_text = gemini_generate_parenting_plan(
            child_age=child_age,
            top_archetype=top_archetype,
            archetype_desc=archetype_desc,
            archetype_needs=archetype_needs,
            traits_text=traits_text,
            scores_text=scores_text,
            lang=PLAN_LANG,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini plan generation error: {exc}")

    if not plan_text:
        raise HTTPException(status_code=502, detail="Gemini returned an empty plan.")

    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO parenting_plans (user_id, plan_text, plan_language, created_at) "
            "VALUES (%s, %s, %s, NOW()) RETURNING id, created_at",
            (user_id, plan_text, PLAN_LANG),
        )
        plan_row        = cur.fetchone()
        plan_id         = plan_row[0]
        plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error saving plan: {exc}")

    log_event(conn, user_id, "parenting_plan_generated", value=f"plan_id={plan_id},assessment_id={assessment_id}")

    return {
        "plan_id":       plan_id,
        "created_at":    plan_created_at,
        "plan_language": PLAN_LANG,
        "plan_text":     plan_text,
        "top_archetype": top_archetype,
        "child_age":     child_age,
        "assessment_id": assessment_id,
    }

# ===========================================================================
# ROUTES
# ===========================================================================

# ---------------------------------------------------------------------------
# SYSTEM
# ---------------------------------------------------------------------------

@app.get("/", tags=["System"])
def home():
    return {"status": "Rafiq running", "version": "5.0.0"}

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
        "firebase":       FIREBASE_ENABLED,
    }

@app.get("/test_gemini", tags=["System"])
def test_gemini():
    _require_gemini()
    r = client.models.generate_content(model=GEMINI_MODEL, contents="Reply with OK only.")
    return {"text": r.text}

# ---------------------------------------------------------------------------
# USERS
# ---------------------------------------------------------------------------

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
                name               = COALESCE(EXCLUDED.name,      users.name),
                email              = COALESCE(EXCLUDED.email,     users.email),
                child_age          = COALESCE(EXCLUDED.child_age, users.child_age),
                preferred_language = COALESCE(EXCLUDED.preferred_language, users.preferred_language),
                updated_at         = NOW()
            RETURNING user_id, name, email, child_age, preferred_language, created_at, updated_at
            """,
            (req.user_id, req.name, req.email, req.child_age, json.dumps([]), lang),
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

# ---------------------------------------------------------------------------
# NOTIFICATIONS / FCM
# ---------------------------------------------------------------------------

@app.post("/register-token", tags=["Notifications"])
def register_token(req: RegisterTokenReq):
    conn = get_conn()
    lang: Lang = "ar"
    try:
        ensure_user_exists(conn, req.user_id)
        cur = conn.cursor()
        cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
        row = cur.fetchone()
        if row:
            lang = row[0] or "ar"
        cur.execute(
            "UPDATE users SET fcm_token=%s, updated_at=NOW() WHERE user_id=%s",
            (req.fcm_token, req.user_id),
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
    conn  = get_conn()
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
            return {
                "ok": True, "user_id": req.user_id, "tip_saved": True,
                "notification_sent": False, "warning": t("firebase_not_configured", lang),
            }

        try:
            message = fb_messaging.Message(
                notification=fb_messaging.Notification(title=t("daily_tip_notif_title", lang), body=req.tip[:200]),
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
            (user_id, max(1, min(200, limit))),
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id,
            "total":   len(rows),
            "tips":    [{"id": r[0], "tip": r[1], "created_at": r[2].isoformat() if r[2] else None} for r in rows],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# KB (in-memory)
# ---------------------------------------------------------------------------

@app.get("/kb/topics", tags=["KB"])
def kb_topics():
    topics = sorted({x["topic"] for x in KB})
    return {"topics": topics, "count": len(topics)}

@app.get("/kb/search", tags=["KB"])
def kb_search_api(topic: str, q: str = "", age: Optional[int] = None):
    res = kb_search_v2(topic=topic, query=q, age=age)
    return {
        "topic": topic, "age": age,
        "matched": res.matched, "match_count": res.match_count,
        "used_default": res.used_default, "tips": res.tips,
    }

# ---------------------------------------------------------------------------
# RAG KNOWLEDGE BASE
# ---------------------------------------------------------------------------

@app.post("/kb/add", tags=["RAG"])
def rag_kb_add(req: RagKbAddRequest):
    """
    Add a FAQ entry to the vector knowledge base.
    Generates an embedding from the combined question and answer text,
    then stores the entry in PostgreSQL with pgvector.
    """
    if not req.question.strip() or not req.answer.strip():
        raise HTTPException(status_code=422, detail="question and answer must not be empty.")

    combined_text = f"Q: {req.question}\nA: {req.answer}"
    embedding     = generate_embedding(combined_text)

    conn = get_conn()
    try:
        ensure_faq_kb_table(conn)
        entry_id = rag_insert_entry(
            conn=conn,
            question=req.question,
            answer=req.answer,
            category=req.category,
            embedding=embedding,
        )
        return {"ok": True, "id": entry_id, "category": req.category}
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


@app.post("/kb/semantic-search", tags=["RAG"])
def rag_kb_search(req: RagKbSearchRequest):
    """
    Perform a semantic similarity search over the FAQ knowledge base.
    Returns the top relevant entries ordered by cosine similarity.
    """
    if not req.query.strip():
        raise HTTPException(status_code=422, detail="query must not be empty.")

    query_embedding = generate_embedding(req.query)
    limit           = max(1, min(20, req.limit))

    conn = get_conn()
    try:
        ensure_faq_kb_table(conn)
        results = rag_semantic_search(conn, query_embedding, limit=limit)
        return {"query": req.query, "total": len(results), "results": results}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Search error: {exc}")
    finally:
        conn.close()


@app.post("/chat/rag", response_model=RagChatResponse, tags=["RAG"])
def rag_chat(req: RagChatRequest):
    """
    RAG-powered chat endpoint.

    Flow:
    1. Receive user question.
    2. Generate embedding for the question.
    3. Retrieve top 5 relevant FAQs from the vector knowledge base.
    4. Build context string from retrieved results.
    5. Send context and question to Gemini LLM.
    6. Return final answer. If no relevant FAQs are found, Gemini answers directly.
    """
    if not req.question.strip():
        raise HTTPException(status_code=422, detail="question must not be empty.")

    lang: Lang = req.preferred_language if req.preferred_language in ("ar", "en") else detect_lang(req.question)  # type: ignore[assignment]

    query_embedding = generate_embedding(req.question)

    conn = get_conn()
    try:
        ensure_faq_kb_table(conn)
        results = rag_semantic_search(conn, query_embedding, limit=5)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Retrieval error: {exc}")
    finally:
        conn.close()

    # Filter results with a minimum similarity threshold to avoid noise
    SIMILARITY_THRESHOLD = 0.5
    relevant = [r for r in results if r["similarity"] >= SIMILARITY_THRESHOLD]
    used_rag = bool(relevant)
    context  = rag_build_context(relevant) if used_rag else ""

    answer = rag_llm_answer(question=req.question, context=context, lang=lang)

    sources = [
        {"id": r["id"], "question": r["question"], "category": r["category"], "similarity": r["similarity"]}
        for r in relevant
    ]
    return RagChatResponse(answer=answer, sources=sources, used_rag=used_rag)

# ---------------------------------------------------------------------------
# ASSESSMENT
# ---------------------------------------------------------------------------

@app.get("/assessment/questions", tags=["Assessment"])
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return {
        "child_age":      age,
        "total_questions": len(qs),
        "scale": {
            "min": 1, "max": 5,
            "labels": {"1": "Never", "2": "Rarely", "3": "Sometimes", "4": "Often", "5": "Always"},
        },
        "questions": _format_questions_for_api(qs),
    }

@app.post("/assessment/submit", tags=["Assessment"])
def assessment_submit(req: AssessmentSubmitReq):
    conn  = get_conn()
    lang: Lang = req.preferred_language if req.preferred_language in ("ar", "en") else "ar"  # type: ignore[assignment]
    try:
        ensure_user_exists(conn, req.user_id)

        if req.preferred_language is None:
            cur = conn.cursor()
            cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
            row = cur.fetchone()
            if row and row[0]:
                lang = row[0]

        profile      = compute_personality_profile(req.answers, req.child_age, req.behavior_signals)
        assess_conf  = compute_assessment_confidence(req.answers, req.child_age, req.behavior_signals)
        profile_to_store = {k: v for k, v in profile.items() if k != "_debug"}

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO assessments "
            "(user_id, child_age, assessment_confidence, result, created_at) "
            "VALUES (%s, %s, %s, %s, NOW()) RETURNING id",
            (req.user_id, req.child_age, assess_conf["confidence"], json.dumps(profile_to_store)),
        )
        assessment_id = cur.fetchone()[0]
        conn.commit()
        update_memory(conn, req.user_id, "assessment_personality", req.child_age, note="Assessment submitted")
        log_event(conn, req.user_id, "assessment_submit", value=f"confidence={assess_conf['confidence']}")

        plan_result:   Dict[str, Any] = {}
        plan_generated = False
        plan_warning:  Optional[str]  = None

        if not GEMINI_ENABLED or client is None:
            plan_warning = "Gemini disabled — plan not generated. Set GEMINI_API_KEY."
        else:
            try:
                plan_result    = _generate_and_save_plan(
                    conn=conn, user_id=req.user_id, assessment_id=assessment_id,
                    result=profile_to_store, child_age=req.child_age,
                )
                plan_generated = True
            except HTTPException as he:
                plan_warning = f"Plan generation failed: {he.detail}"
            except Exception as exc:
                plan_warning = f"Plan generation error: {exc}"

        notif_result = _send_fcm_notification(
            user_id=req.user_id,
            title="Your Parenting Plan is Ready",
            body="A personalized parenting plan has been generated based on your assessment.",
            data={
                "type":          "parenting_plan",
                "user_id":       str(req.user_id),
                "plan_id":       str(plan_result.get("plan_id", "")),
                "assessment_id": str(assessment_id),
            },
        )

        response: Dict[str, Any] = {
            "ok":                     True,
            "message":                t("ok", lang),
            "assessment_saved":       True,
            "assessment_id":          assessment_id,
            "trait_scores":           profile["trait_scores"],
            "top_traits":             profile["top_traits"],
            "low_traits":             profile["low_traits"],
            "possible_personalities": profile["possible_personalities"],
            "recommendations":        profile["recommendations"],
            "confidence":             assess_conf["confidence"],
            "assessment_meta":        assess_conf,
            "note":                   t("assessment_note", lang),
            "debug":                  profile.get("_debug", {}),
            "plan_generated":         plan_generated,
            "plan_id":                plan_result.get("plan_id"),
            "plan_created_at":        plan_result.get("created_at"),
            "plan_language":          plan_result.get("plan_language", "en"),
            "top_archetype":          plan_result.get("top_archetype"),
            "notification_sent":      notif_result["sent"],
        }
        if plan_warning:
            response["plan_warning"] = plan_warning
        if notif_result["warning"]:
            response["notification_warning"] = notif_result["warning"]

        return response

    except HTTPException:
        raise
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
    return {
        "assessments": [
            {
                "id":         r[0],
                "child_age":  r[1],
                "confidence": float(r[2]),
                "result":     r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]
    }

# ---------------------------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------------------------

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
    return {
        "user_id": user_id,
        "recent_events": [
            {"event_id": r[0], "event_type": r[1], "value": r[2],
             "created_at": r[3].isoformat() if r[3] else None}
            for r in rows
        ],
    }

# ---------------------------------------------------------------------------
# FEEDBACK
# ---------------------------------------------------------------------------

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
        if req.comment:
            update_memory(conn, req.user_id, req.topic or "general_parenting", None,
                          note=f"FEEDBACK:{req.rating}:{req.comment}")
        log_event(conn, req.user_id, "feedback", value=f"{req.rating}:{req.message_id}")
        return {"ok": True}
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# CHAT HISTORY
# ---------------------------------------------------------------------------

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
    return {
        "messages": [
            {"message_id": r[0], "user_message": r[1], "bot_reply": r[2],
             "created_at": r[3].isoformat() if r[3] else None}
            for r in rows
        ]
    }

# ---------------------------------------------------------------------------
# CHAT (main parenting assistant)
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages list is empty.")

    message_id = "msg_" + uuid.uuid4().hex[:10]
    user_text  = req.messages[-1].content.strip()
    lang: Lang = req.preferred_language if req.preferred_language in ("ar", "en") else detect_lang(user_text)  # type: ignore[assignment]

    if hard_out_of_scope(user_text) or hard_medical(user_text):
        return ChatResponse(
            message_id=message_id,
            reply=t("out_of_scope_reply", lang),
            cards=[{"type": "refusal", "title": t("card_out_of_scope", lang), "body": t("out_of_scope_card", lang)}],
        )

    if not GEMINI_ENABLED or client is None:
        return ChatResponse(
            message_id=message_id,
            reply=t("gemini_disabled", lang),
            cards=[{"type": "warning", "title": "Gemini disabled", "body": "Set GEMINI_API_KEY in environment variables."}],
        )

    conn = get_conn()
    try:
        mem_check = get_memory(conn, req.user_id)
        if req.preferred_language is None and mem_check.get("preferred_language"):
            lang = mem_check["preferred_language"]

        risk_level = detect_risk_level(user_text)

        if risk_level == "high":
            ensure_user_exists(conn, req.user_id)
            log_event(conn, req.user_id, "risk_high", value=user_text[:200])
            return ChatResponse(
                message_id=message_id,
                reply=t("risk_high", lang),
                cards=[{"type": "warning", "title": t("card_important", lang), "body": t("risk_high_card", lang),
                        "meta": {"risk_level": "high"}}],
            )

        try:
            decision = gemini_route_decision(user_text, req.messages, req.child_age)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Router failed: {exc}")

        ensure_user_exists(conn, req.user_id)
        log_event(conn, req.user_id, "chat_message", value=user_text[:300])

        if not decision.in_scope or decision.action == "refuse_out_of_scope":
            return ChatResponse(
                message_id=message_id,
                reply=t("scope_refusal", lang),
                cards=[{"type": "refusal",
                        "title": t("card_out_of_scope", lang),
                        "body":  t("card_refusal_reason_prefix", lang) + decision.reason}],
            )

        topic = decision.topic

        if topic in KIDS_CONTENT_TOPICS and kids_safety_guard(user_text):
            return ChatResponse(
                message_id=message_id,
                reply=t("kids_safety", lang),
                cards=[{"type": "warning", "title": t("child_appropriate_content", lang), "body": t("choose_safe_topic", lang)}],
            )

        age = decision.extracted_child_age or req.child_age
        update_memory(conn, req.user_id, topic, age, note=user_text)
        mem       = get_memory(conn, req.user_id)
        kb_res    = kb_search_v2(topic=topic, query=user_text, age=age)
        tips      = kb_res.tips
        followups = pick_followups(topic)
        conf      = compute_confidence(topic, kb_res, age, user_text, decision.in_scope, risk_level)

        if topic in PARENTING_TOPICS and not kb_res.matched and conf < 65:
            q = followups[0] if followups else ("How old is your child?" if lang == "en" else "سن الطفل قد ايه؟")
            return ChatResponse(
                message_id=message_id,
                reply=t("low_conf_prefix", lang) + q + t("low_conf_suffix", lang),
                cards=[
                    {"type": "confidence", "title": t("confidence_score", lang), "body": f"{conf}%",
                     "meta": {"confidence": conf, "matched": kb_res.matched}},
                    {"type": "warning", "title": t("follow_up", lang), "body": q,
                     "meta": {"followups": followups}},
                ],
            )

        intro = empathy_reflect(user_text, topic, risk_level, lang)
        try:
            final_text = intro + gemini_compose_answer(
                user_text=user_text, topic=topic, tips=tips,
                memory=mem, followups=followups, confidence=conf, risk_level=risk_level, lang=lang,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Compose failed: {exc}")

        if ENABLE_VERIFY:
            verdict = gemini_verify_answer(
                user_text, final_text,
                {"topic": topic, "tips": tips, "memory": mem, "followups": followups, "confidence": conf},
            )
            if not verdict.get("ok", True):
                q = followups[0] if followups else ("How old is your child?" if lang == "en" else "سن الطفل قد ايه؟")
                final_text = t("verify_fallback", lang) + q

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (message_id, user_id, message, response) VALUES (%s,%s,%s,%s)",
            (message_id, req.user_id, user_text, final_text),
        )
        conn.commit()

        cards: List[Dict] = []
        ctype_map  = {"kids_stories": "story", "activities_games": "game",
                      "book_recommendations": "books", "assessment_personality": "assessment_question"}
        ctitle_key = {"kids_stories": "card_story", "activities_games": "card_game",
                      "book_recommendations": "card_books", "assessment_personality": "card_assessment"}

        for tip_item in tips:
            ctype  = ctype_map.get(topic, "tip")
            ctitle = t(ctitle_key.get(topic, "card_tip"), lang)
            cards.append({"type": ctype, "title": ctitle, "body": tip_item["tip"],
                          "meta": {"kb_id": tip_item["id"], "age_used": age, "matched": kb_res.matched}})

        cards.append({"type": "confidence", "title": t("confidence_score", lang),
                      "body": f"{conf}%", "meta": {"confidence": conf, "risk_level": risk_level}})

        if conf < 70 or (topic in PARENTING_TOPICS and not kb_res.matched):
            cards.append({"type": "warning", "title": t("follow_up", lang),
                          "body": followups[0] if followups else "",
                          "meta": {"followups": followups}})

        return ChatResponse(message_id=message_id, reply=final_text, cards=cards)

    finally:
        conn.close()

# ---------------------------------------------------------------------------
# PARENTING PLAN
# ---------------------------------------------------------------------------

@app.post("/generate-parenting-plan/{user_id}", tags=["Parenting Plan"])
def generate_parenting_plan(user_id: str):
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini is disabled. Set GEMINI_API_KEY.")

    conn = get_conn()
    try:
        ensure_user_exists(conn, user_id)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, child_age, result FROM assessments "
            "WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No assessment found. Submit one first.")

        assessment_id, child_age, result_raw = row
        try:
            result: Dict[str, Any] = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Cannot parse assessment result: {exc}")

        plan = _generate_and_save_plan(
            conn=conn, user_id=user_id, assessment_id=assessment_id,
            result=result, child_age=child_age,
        )

        notif = _send_fcm_notification(
            user_id=user_id,
            title="Your Parenting Plan is Ready",
            body="A personalized parenting plan has been generated based on your assessment.",
            data={
                "type":          "parenting_plan",
                "user_id":       str(user_id),
                "plan_id":       str(plan["plan_id"]),
                "assessment_id": str(assessment_id),
            },
        )

        response: Dict[str, Any] = {
            "ok":                True,
            "message":           "Parenting plan generated successfully",
            "user_id":           user_id,
            "plan_generated":    True,
            "plan_id":           plan["plan_id"],
            "created_at":        plan["created_at"],
            "plan_language":     plan["plan_language"],
            "child_age":         plan["child_age"],
            "top_archetype":     plan["top_archetype"],
            "assessment_id":     assessment_id,
            "notification_sent": notif["sent"],
            "plan_text":         plan["plan_text"],
        }
        if notif["warning"]:
            response["notification_warning"] = notif["warning"]
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
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=t("user_not_found", "ar"))
        cur.execute(
            "SELECT id, plan_text, plan_language, created_at FROM parenting_plans "
            "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, max(1, min(50, limit))),
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id,
            "total":   len(rows),
            "plans": [
                {"id": r[0], "plan_text": r[1], "plan_language": r[2],
                 "created_at": r[3].isoformat() if r[3] else None}
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# PDF EXPORT
# ---------------------------------------------------------------------------

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
            SELECT pp.id, pp.plan_text, pp.created_at, u.child_age, a.result
            FROM   parenting_plans pp
            LEFT   JOIN users       u ON u.user_id  = pp.user_id
            LEFT   JOIN assessments a ON a.user_id  = pp.user_id
            WHERE  pp.user_id = %s
            ORDER  BY pp.created_at DESC
            LIMIT  1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No parenting plan found for this user.")

        plan_id, plan_text, created_at, child_age, result_raw = row
        generated_at = created_at.isoformat() if created_at else ""

        top_archetype = "Not specified"
        if result_raw:
            try:
                result_obj     = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
                personalities  = _norm_personalities(result_obj.get("possible_personalities", []))
                if personalities:
                    arch_id    = personalities[0].get("id", "")
                    arch_obj   = next((a for a in ARCHETYPES if a["id"] == arch_id), None)
                    top_archetype = arch_obj["name"] if arch_obj else (personalities[0].get("name") or "Not specified")
            except Exception:
                pass

        try:
            pdf_bytes = _build_parenting_plan_pdf(
                user_id=user_id,
                child_age=child_age,
                top_archetype=top_archetype,
                plan_text=plan_text or "",
                generated_at=generated_at,
                lang=PDF_LANG,
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
