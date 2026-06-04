"""
rafiq_bot_api.py
Rafiq Chatbot API (Gemini + PostgreSQL FIXED)
"""

from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import re
from typing import List, Optional, Dict, Any, Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from google import genai
except Exception:
    genai = None


# =======================
# CONFIG
# =======================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_ENABLED else None

app = FastAPI(title="Rafiq Bot API")


# =======================
# CORS
# =======================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =======================
# MEMORY (TEMP)
# =======================
USER_MEMORY = {}


# =======================
# DB CONNECTION (FIXED)
# =======================
def get_conn():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL is missing")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


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
    return USER_MEMORY.get(user_id, {
        "child_age": None,
        "notes": [],
        "preferred_language": None
    })


def update_memory(user_id: str, note: str, age: Optional[int]):
    mem = get_user_memory(user_id)

    if age is not None:
        mem["child_age"] = age

    mem["notes"].append(note[:150])
    USER_MEMORY[user_id] = mem


def detect_language(text: str) -> str:
    return "ar" if re.findall(r'[\u0600-\u06FF]', text) else "en"


def clean_response(text: str) -> str:
    return re.sub(r"[\*`#]", "", text).strip()


def detect_risk(text: str):
    t = text.lower()
    return "high" if ("انتحار" in t or "أموت" in t) else "low"


# =======================
# GEMINI
# =======================
def compose_reply(user_text, memory, lang):

    if not GEMINI_ENABLED:
        return "Rafiq is unavailable." if lang == "en" else "رفيق غير متاح"

    prompt = f"""
You are Rafiq, a helpful parenting assistant.

Rules:
- Be simple and friendly
- No markdown
- No stars

User:
{user_text}

Language:
{lang}

Memory:
{memory}
"""

    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )

    return clean_response(r.text or "...")


# =======================
# HOME
# =======================
@app.get("/")
def home():
    return {"message": "Rafiq Bot API is running 🚀"}


# =======================
# CHAT ENDPOINT
# =======================
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):

    msg_id = "msg_" + uuid.uuid4().hex[:10]

    user_text = req.messages[-1].content

    mem = get_user_memory(req.user_id)
    update_memory(req.user_id, user_text, req.child_age)

    lang = detect_language(user_text)
    risk = detect_risk(user_text)

    if risk == "high":
        reply = "Please talk to someone you trust ❤️" if lang == "en" else "أنا قلق عليك ❤️ كلم حد قريب منك"

        return ChatResponse(
            message_id=msg_id,
            reply=reply,
            cards=[]
        )

    reply = compose_reply(
        user_text=user_text,
        memory=mem,
        lang=lang
    )

    # =======================
    # DB SAVE (SAFE FIXED)
    # =======================
    try:
        conn = get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO chat_messages (user_id, message, response)
            VALUES (%s, %s, %s)
        """, (req.user_id, user_text, reply))

        cursor.execute("""
            INSERT INTO analytics (user_id, event_type, value)
            VALUES (%s, %s, %s)
        """, (req.user_id, "chat", user_text[:100]))

        conn.commit()
        cursor.close()
        conn.close()

    except Exception as e:
        print("DB ERROR:", str(e))

    return ChatResponse(
        message_id=msg_id,
        reply=reply,
        cards=[]
    )
