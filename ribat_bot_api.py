"""
rafiq_bot_api.py
Enhanced Rafiq Chatbot API
FastAPI + Gemini
"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
import uuid
from typing import List, Optional, Dict, Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    from google import genai
except Exception:
    genai = None


# =======================
# CONFIG
# =======================
DEBUG = os.getenv("RIBAT_DEBUG", "0") == "1"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

DATA_DIR = "data"

MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")

ANALYTICS_FILE = os.path.join(DATA_DIR, "analytics.json")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_ENABLED else None

app = FastAPI(title="Rafiq Chatbot API")


# =======================
# STORAGE
# =======================
USER_MEMORY = {}

ANALYTICS = []


# =======================
# FILE HELPERS
# =======================
def safe_load_json(path):

    try:

        if os.path.exists(path):

            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

    except:
        return {}

    return {}


def safe_write_json(path, data):

    try:

        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        if DEBUG:
            print(e)


# =======================
# MEMORY
# =======================
def save_memory():

    safe_write_json(
        MEMORY_FILE,
        USER_MEMORY
    )


def load_memory():

    global USER_MEMORY

    USER_MEMORY = safe_load_json(MEMORY_FILE) or {}


def get_user_memory(user_id):

    return USER_MEMORY.get(
        user_id,
        {
            "child_age": None,
            "notes": [],
            "topics": {}
        }
    )


def update_memory(
    user_id,
    note,
    age,
    topic="general"
):

    mem = get_user_memory(user_id)

    if age:
        mem["child_age"] = age

    mem["notes"].append(note[:150])

    mem["topics"][topic] = mem["topics"].get(topic, 0) + 1

    USER_MEMORY[user_id] = mem

    save_memory()


# =======================
# ANALYTICS
# =======================
def save_analytics():

    safe_write_json(
        ANALYTICS_FILE,
        ANALYTICS
    )


def load_analytics():

    global ANALYTICS

    ANALYTICS = safe_load_json(ANALYTICS_FILE) or []


# =======================
# LOAD DATA
# =======================
load_memory()

load_analytics()


# =======================
# KB
# =======================
KB = [

    {
        "id": "kb_001",
        "topic": "teen_communication",
        "tags": [
            "مراهق",
            "ساكت",
            "مش بيرد"
        ],
        "tip": "ابدئي بهدوء وقولي: أنا عايزة أفهمك مش ألومك."
    },

    {
        "id": "kb_002",
        "topic": "anger",
        "tags": [
            "غضب",
            "صراخ",
            "عصبية"
        ],
        "tip": "وقت الغضب قللي الكلام وبعدها اتكلموا بهدوء."
    },

    {
        "id": "kb_003",
        "topic": "screen_addiction",
        "tags": [
            "موبايل",
            "شاشات",
            "تيك توك"
        ],
        "tip": "قللي وقت الشاشة تدريجيًا مع نشاط بديل ممتع."
    }
]


# =======================
# MODELS
# =======================
class ChatMessage(BaseModel):

    role: Literal["user", "assistant"]

    content: str


class ChatRequest(BaseModel):

    user_id: str

    messages: List[ChatMessage]

    child_age: Optional[int] = None


class Card(BaseModel):

    type: str

    title: str

    body: str

    meta: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):

    message_id: str

    reply: str

    cards: List[Card] = []


# =======================
# RISK
# =======================
RISK_HIGH = [

    "انتحار",
    "هنتحر",
    "أذي نفسي",
    "أموت"
]

RISK_MEDIUM = [

    "اكتئاب",
    "قلق",
    "خوف شديد",
    "هلع"
]


def detect_risk(text):

    t = text.lower()

    if any(x in t for x in RISK_HIGH):
        return "high"

    if any(x in t for x in RISK_MEDIUM):
        return "medium"

    return "low"


# =======================
# FOLLOW UPS
# =======================
FOLLOWUPS = {

    "anger": [
        "العصبية بتحصل إمتى أكتر؟"
    ],

    "teen_communication": [
        "هو ساكت ولا بيرد بعصبية؟"
    ],

    "general": [
        "ممكن تفاصيل أكتر؟"
    ]
}


def pick_followup(topic):

    return FOLLOWUPS.get(
        topic,
        FOLLOWUPS["general"]
    )[0]


# =======================
# CONFIDENCE
# =======================
def compute_confidence(
    user_text,
    risk
):

    score = 50

    if len(user_text.split()) > 5:
        score += 20

    if risk == "low":
        score += 20

    if risk == "medium":
        score -= 10

    if risk == "high":
        score -= 30

    return max(
        0,
        min(score, 100)
    )


# =======================
# EMPATHY
# =======================
def empathy_reflect(user_text):

    short = user_text[:80]

    return f"""
حاسس/ة إن الموقف ده مضايقك فعلًا ❤️

إنت قلت:
"{short}"

"""


# =======================
# KB SEARCH
# =======================
def kb_search(user_text):

    for item in KB:

        for tag in item["tags"]:

            if tag in user_text:
                return item

    return None


# =======================
# GEMINI
# =======================
def compose_reply(
    user_text,
    topic,
    tips,
    memory,
    confidence
):

    if not GEMINI_ENABLED:

        return """
رفيق غير مفعل حاليًا.
ضيف GEMINI_API_KEY.
"""

    prompt = f"""
أنت مساعد ذكي اسمه رفيق.

مهمتك:
- دعم الأسرة
- الرد بالمصري
- تقديم نصائح تربوية
- كن هادئ ومطمئن
- لا تعطي تشخيص طبي

Confidence:
{confidence}

User:
{user_text}

Tips:
{tips}

Memory:
{memory}
"""

    try:

        r = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )

        return r.text or "ممكن توضحي أكتر؟"

    except Exception:

        return """
حصلت مشكلة مؤقتة.
حاولي مرة تانية بعد شوية.
"""


# =======================
# HEALTH
# =======================
@app.get("/health")
def health():

    return {

        "ok": True,

        "gemini_enabled": GEMINI_ENABLED,

        "memory_users": len(USER_MEMORY),

        "analytics_events": len(ANALYTICS)
    }


# =======================
# HOME
# =======================
@app.get("/")
def home():

    return {
        "message": "Rafiq API Running 🚀"
    }


# =======================
# CHAT
# =======================
@app.post(
    "/chat",
    response_model=ChatResponse
)
def chat(req: ChatRequest):

    if not req.messages:

        raise HTTPException(
            status_code=400,
            detail="messages required"
        )

    msg_id = "msg_" + uuid.uuid4().hex[:10]

    user_text = req.messages[-1].content

    risk = detect_risk(user_text)

    confidence = compute_confidence(
        user_text,
        risk
    )

    topic = "general"

    kb_result = kb_search(user_text)

    tips = []

    if kb_result:

        topic = kb_result["topic"]

        tips.append(kb_result["tip"])

    update_memory(
        req.user_id,
        user_text,
        req.child_age,
        topic
    )

    mem = get_user_memory(req.user_id)

    ANALYTICS.append({

        "message_id": msg_id,

        "user_id": req.user_id,

        "message": user_text,

        "topic": topic,

        "risk": risk
    })

    save_analytics()

    # =======================
    # HIGH RISK
    # =======================
    if risk == "high":

        return ChatResponse(

            message_id=msg_id,

            reply="""
أنا قلقان عليك ❤️

حاول تتكلم فورًا مع شخص قريب منك أو مختص.
""",

            cards=[

                Card(
                    type="warning",
                    title="تنبيه مهم",
                    body="لو في خطر فوري اطلب مساعدة من شخص بالغ أو مختص."
                )
            ]
        )

    # =======================
    # MEDIUM RISK
    # =======================
    if risk == "medium":

        return ChatResponse(

            message_id=msg_id,

            reply="""
واضح إن الموضوع تقيل عليك شوية ❤️

ممكن تحكيلي أكتر عن اللي حاصل؟
""",

            cards=[

                Card(
                    type="warning",
                    title="دعم نفسي",
                    body="لو الإحساس مستمر حاول تتكلم مع مختص."
                )
            ]
        )

    # =======================
    # NORMAL FLOW
    # =======================
    intro = empathy_reflect(user_text)

    reply = intro + compose_reply(

        user_text=user_text,

        topic=topic,

        tips=tips,

        memory=mem,

        confidence=confidence
    )

    cards = []

    cards.append(

        Card(
            type="tip",
            title="درجة الثقة",
            body=f"{confidence}%"
        )
    )

    cards.append(

        Card(
            type="tip",
            title="سؤال متابعة",
            body=pick_followup(topic)
        )
    )

    if tips:

        for t in tips:

            cards.append(

                Card(
                    type="tip",
                    title="نصيحة",
                    body=t
                )
            )

    return ChatResponse(

        message_id=msg_id,

        reply=reply,

        cards=cards
    )
