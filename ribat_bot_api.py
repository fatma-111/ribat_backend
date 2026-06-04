"""
rafiq_bot_api.py
Rafiq Chatbot API (Production Fixed + PostgreSQL Memory)
"""

from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import re
import json
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
GEMINI_ENABLED = bool(GEMINI_API_KEY) and genai is not None

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
# DB
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
# MEMORY (POSTGRESQL)
# =======================
def get_user_profile(conn, user_id: str):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT user_id, child_age, notes, topics, preferred_language
            FROM users
            WHERE user_id = %s
        """, (user_id,))
        return cur.fetchone()


def update_user_memory(conn, user_id: str, text: str, child_age: Optional[int]):
    with conn.cursor() as cur:

        cur.execute("""
            SELECT notes FROM users WHERE user_id = %s
        """, (user_id,))
        row = cur.fetchone()

        if row:
            notes = row[0] or []
            notes.append(text[:150])

            cur.execute("""
                UPDATE users
                SET notes = %s,
                    child_age = COALESCE(%s, child_age),
                    updated_at = NOW()
                WHERE user_id = %s
            """, (json.dumps(notes), child_age, user_id))
        else:
            cur.execute("""
                INSERT INTO users (user_id, child_age, notes)
                VALUES (%s, %s, %s)
            """, (user_id, child_age, json.dumps([text[:150]])))


# =======================
# HELPERS
# =======================
def detect_language(text: str) -> str:
    return "ar" if re.findall(r'[\u0600-\u06FF]', text) else "en"


def clean_response(text: str) -> str:
    return re.sub(r"[\*`#]", "", text or "").strip()


def detect_risk(text: str):
    t = text.lower()
    return "high" if ("انتحار" in t or "أموت" in t or "kill myself" in t) else "low"


def build_memory_text(profile):
    if not profile:
        return "No memory"

    return {
        "child_age": profile["child_age"],
        "notes": profile["notes"],
        "topics": profile["topics"],
        "preferred_language": profile["preferred_language"]
    }


# =======================
# GEMINI
# =======================
def compose_reply(user_text, memory, lang):

    if not GEMINI_ENABLED:
        return "Rafiq is unavailable." if lang == "en" else "رفيق غير متاح"

    prompt = f"""
You are Rafiq, a helpful parenting assistant.

Rules:
- Simple & friendly
- No markdown
- No symbols
- Short responses

User message:
{user_text}

Language:
{lang}

User memory:
{memory}
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )

    return clean_response(response.text)


# =======================
# CHAT ENDPOINT
# =======================
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):

    msg_id = "msg_" + uuid.uuid4().hex[:10]
    user_text = req.messages[-1].content

    risk = detect_risk(user_text)
    lang = detect_language(user_text)

    try:
        conn = get_conn()

        # =======================
        # LOAD MEMORY
        # =======================
        profile = get_user_profile(conn, req.user_id)
        memory = build_memory_text(profile)

        # =======================
        # RISK HANDLING
        # =======================
        if risk == "high":
            reply = "Please talk to someone you trust ❤️" if lang == "en" else "أنا قلق عليك ❤️ كلم حد قريب منك"

            return ChatResponse(
                message_id=msg_id,
                reply=reply,
                cards=[]
            )

        # =======================
        # AI RESPONSE
        # =======================
        reply = compose_reply(user_text, memory, lang)

        # =======================
        # UPDATE MEMORY
        # =======================
        update_user_memory(conn, req.user_id, user_text, req.child_age)

        # =======================
        # SAVE CHAT
        # =======================
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages (message_id, user_id, message, response)
                VALUES (%s, %s, %s, %s)
            """, (msg_id, req.user_id, user_text, reply))

            cur.execute("""
                INSERT INTO analytics (user_id, event_type, value)
                VALUES (%s, %s, %s)
            """, (req.user_id, "chat", user_text[:100]))

        conn.commit()
        conn.close()

        return ChatResponse(
            message_id=msg_id,
            reply=reply,
            cards=[]
        )

    except Exception as e:
        print("ERROR:", str(e))

        return ChatResponse(
            message_id=msg_id,
            reply="System error occurred",
            cards=[]
        )


# =======================
# HEALTH CHECK
# =======================
@app.get("/")
def home():
    return {
        "status": "running",
        "gemini": GEMINI_ENABLED
    }
