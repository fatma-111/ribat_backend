"""
rafiq_bot_api.py
Rafiq Chatbot API (Gemini) - Full
Backend-only FastAPI app:
- Parenting/Family support + Kids stories/games/books + Personality Assessment
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
GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)

ADMIN_KEY = os.getenv("RIBAT_ADMIN_KEY", "change-me")
if ADMIN_KEY == "change-me":
    print("WARNING: ADMIN key is default. Set it in ENV for production.")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ENABLE_VERIFY = os.getenv("RIBAT_VERIFY_OUTPUT", "0") == "1"
PERSIST_MEMORY = os.getenv("RIBAT_PERSIST_MEMORY", "1") == "1"

DATA_DIR = os.getenv("RIBAT_DATA_DIR", "data")

MEMORY_FILE = os.path.join(DATA_DIR, "rafiq_user_memory.json")
ANALYTICS_FILE = os.path.join(DATA_DIR, "rafiq_analytics.json")
APPOINTMENTS_FILE = os.path.join(DATA_DIR, "rafiq_appointments.json")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_ENABLED else None
app = FastAPI(title="Rafiq Chatbot API (Gemini) - Full")
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Rafiq Bot API")


# =======================
# CORS
# =======================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*"  # للتجربة فقط
        # بعدين حطي رابط الفرونت الحقيقي
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =======================
# DUMMY DATA
# =======================
KB = [
    {
        "id": "kb_001",
        "topic": "teen_communication",
        "age_min": 12, "age_max": 18,
        "tags": ["مراهق", "ساكت", "مش بيرد"],
        "tip": "ابدئي بهدوء: «أنا عايزة أفهمك مش ألومك»."
    },
    {
        "id": "kb_002",
        "topic": "anger",
        "age_min": 6, "age_max": 18,
        "tags": ["عصبية", "غضب", "صراخ"],
        "tip": "وقت الغضب قللي الكلام، وبعدها ناقشي بهدوء."
    },
]

SPECIALISTS = [
    {"id": "sp_001", "name": "د. مريم علي", "title": "أخصائي إرشاد أسري", "topics": ["teen_communication", "anger"], "price_egp": 350, "rating": 4.8},
]

SLOTS = [
    {"slot_id": "sl_001", "specialist_id": "sp_001", "start": "2026-01-24T18:00:00+02:00", "duration_min": 30, "available": True},
]

APPOINTMENTS = []
ANALYTICS = []
USER_MEMORY = {}


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
# HELPERS
# =======================
def get_user_memory(user_id: str):
    return USER_MEMORY.get(user_id, {"child_age": None, "notes": []})


def update_memory(user_id: str, note: str, age: Optional[int]):
    mem = get_user_memory(user_id)
    if age:
        mem["child_age"] = age
    mem["notes"].append(note[:150])
    USER_MEMORY[user_id] = mem


def detect_risk(text: str):
    t = text.lower()
    if "انتحار" in t or "أموت" in t:
        return "high"
    return "low"


# =======================
# GEMINI
# =======================
def compose_reply(user_text, topic, tips, memory):
    if not GEMINI_ENABLED:
        return "رفيق غير مفعل حاليًا (Gemini API missing)."

    prompt = f"""
أنت مساعد اسمه "رفيق" متخصص في دعم الأسرة.
أجب بطريقة بسيطة ومطمئنة.

User: {user_text}
Topic: {topic}
Tips: {tips}
Memory: {memory}
"""

    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )
    return r.text or "ممكن توضحي أكتر؟"


# =======================
# ROUTE: HOME
# =======================
@app.get("/")
def home():
    return {"message": "Rafiq Bot API is running 🚀"}


# =======================
# CHAT
# =======================
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    msg_id = "msg_" + uuid.uuid4().hex[:10]
    user_text = req.messages[-1].content

    mem = get_user_memory(req.user_id)
    update_memory(req.user_id, user_text, req.child_age)

    risk = detect_risk(user_text)

    if risk == "high":
        return ChatResponse(
            message_id=msg_id,
            reply="أنا قلق عليك ❤️ حاول تتكلم مع شخص قريب منك فورًا.",
            cards=[Card(type="warning", title="تنبيه مهم", body="اطلب دعم فوري من شخص بالغ أو مختص.")]
        )

    topic = "general"
    tips = [{"tip": "حاول تتكلم بهدوء"}]

    reply = compose_reply(user_text, topic, tips, mem)

    return ChatResponse(
        message_id=msg_id,
        reply=reply,
        cards=[
            Card(type="tip", title="نصيحة", body="التواصل الهادئ هو الحل الأفضل.")
        ]
    )
