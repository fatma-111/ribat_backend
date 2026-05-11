"""
rafiq_bot_api.py
Rafiq Chatbot API (Gemini) - Full

Backend-only FastAPI app:
- Parenting/Family support
- Kids stories/games/books
- Personality Assessment

Includes:
- User Memory
- Smart follow-ups
- Confidence scoring
- Risk escalation
- Feedback loop
- Analytics
- Booking system

✅ Updated:
1) Gemini OPTIONAL
2) Supports GEMINI_API_KEY or GOOGLE_API_KEY
3) Safe Gemini initialization
4) Project renamed to Rafiq
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


# ============================================================
# CONFIG
# ============================================================

DEBUG = os.getenv("RAFIQ_DEBUG", "0") == "1"

# =======================
# GEMINI CONFIG
# =======================

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
    or ""
).strip()

GEMINI_ENABLED = bool(GEMINI_API_KEY and genai)

if GEMINI_ENABLED:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        client = None
        GEMINI_ENABLED = False

        if DEBUG:
            print(f"[RAFIQ_DEBUG] Gemini init failed: {repr(e)}")
else:
    client = None


ADMIN_KEY = os.getenv("RAFIQ_ADMIN_KEY", "change-me")

if ADMIN_KEY == "change-me":
    print("WARNING: RAFIQ_ADMIN_KEY is default. Set it in ENV for production.")


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ENABLE_VERIFY = os.getenv("RAFIQ_VERIFY_OUTPUT", "0") == "1"
PERSIST_MEMORY = os.getenv("RAFIQ_PERSIST_MEMORY", "1") == "1"

DATA_DIR = os.getenv("RAFIQ_DATA_DIR", "data")

MEMORY_FILE = os.path.join(DATA_DIR, "rafiq_user_memory.json")
ANALYTICS_FILE = os.path.join(DATA_DIR, "rafiq_analytics.json")
APPOINTMENTS_FILE = os.path.join(DATA_DIR, "rafiq_appointments.json")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Rafiq Chatbot API (Gemini) - Full"
)


# ============================================================
# DUMMY DATA
# ============================================================

KB = [
    {
        "id": "kb_001",
        "topic": "teen_communication",
        "age_min": 12,
        "age_max": 18,
        "tags": ["مراهق", "مش بيرد", "ساكت"],
        "tip": "ابدئي وقت هدوء واسأليه سؤال مفتوح."
    },
    {
        "id": "kb_002",
        "topic": "anger",
        "age_min": 6,
        "age_max": 18,
        "tags": ["عصبية", "غضب", "صراخ"],
        "tip": "قللي الكلام وقت الغضب وثبتي الحدود."
    },
    {
        "id": "kb_003",
        "topic": "screen_addiction",
        "age_min": 8,
        "age_max": 18,
        "tags": ["موبايل", "شاشات"],
        "tip": "اعملي وقت شاشة ثابت مع بديل ممتع."
    },
]


SPECIALISTS = [
    {
        "id": "sp_001",
        "name": "د. مريم علي",
        "title": "أخصائي إرشاد أسري",
        "topics": ["teen_communication", "anger"],
        "price_egp": 350,
        "rating": 4.8
    }
]


SLOTS = [
    {
        "slot_id": "sl_001",
        "specialist_id": "sp_001",
        "start": "2026-01-24T18:00:00+02:00",
        "duration_min": 30,
        "available": True
    }
]


# ============================================================
# STORAGE
# ============================================================

APPOINTMENTS: List[Dict[str, Any]] = []
ANALYTICS: List[Dict[str, Any]] = []
USER_MEMORY: Dict[str, Dict[str, Any]] = {}
USER_USAGE: Dict[str, Dict[str, int]] = {}


# ============================================================
# HELPERS
# ============================================================

def _safe_load_json(path: str):

    try:
        if os.path.exists(path):

            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

    except Exception:
        return {}

    return {}


def _safe_write_json(path: str, data):

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
            print(f"[RAFIQ_DEBUG] JSON WRITE ERROR: {repr(e)}")


# ============================================================
# MEMORY
# ============================================================

def load_memory():

    global USER_MEMORY

    if PERSIST_MEMORY:
        USER_MEMORY = _safe_load_json(MEMORY_FILE) or {}


def save_memory():

    if PERSIST_MEMORY:
        _safe_write_json(
            MEMORY_FILE,
            USER_MEMORY
        )


def get_user_memory(user_id: str):

    return USER_MEMORY.get(
        user_id,
        {
            "child_age": None,
            "topics": {},
            "notes": []
        }
    )


# ============================================================
# MODELS
# ============================================================

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


# ============================================================
# GEMINI
# ============================================================

def _require_gemini():

    if not GEMINI_ENABLED or client is None:

        raise HTTPException(
            status_code=503,
            detail="Gemini disabled"
        )


def gemini_reply(prompt: str):

    _require_gemini()

    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )

    return (r.text or "").strip()


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "ok": True,
        "project": "Rafiq",
        "gemini_enabled": GEMINI_ENABLED,
        "model": GEMINI_MODEL,
        "persist_memory": PERSIST_MEMORY,
        "data_dir": DATA_DIR
    }


# ============================================================
# TEST GEMINI
# ============================================================

@app.get("/test_gemini")
def test_gemini():

    _require_gemini()

    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents="قل OK فقط"
    )

    return {
        "ok": True,
        "response": r.text
    }


# ============================================================
# KB
# ============================================================

@app.get("/kb/topics")
def kb_topics():

    topics = list(set(x["topic"] for x in KB))

    return {
        "topics": topics,
        "count": len(topics)
    }


@app.get("/kb/search")
def kb_search(
    topic: str,
    q: str = ""
):

    results = []

    for item in KB:

        if item["topic"] != topic:
            continue

        hay = (
            " ".join(item["tags"])
            + " "
            + item["tip"]
        )

        if q in hay or q == "":
            results.append(item)

    return {
        "topic": topic,
        "count": len(results),
        "results": results
    }


# ============================================================
# BOOKINGS
# ============================================================

@app.get("/appointments/list")
def appointments_list():

    return {
        "count": len(APPOINTMENTS),
        "appointments": APPOINTMENTS
    }


# ============================================================
# CHAT
# ============================================================

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):

    if not req.messages:

        raise HTTPException(
            status_code=400,
            detail="messages empty"
        )

    message_id = "msg_" + uuid.uuid4().hex[:8]

    user_text = req.messages[-1].content.strip()

    # ========================================================
    # Gemini disabled
    # ========================================================

    if not GEMINI_ENABLED or client is None:

        return ChatResponse(
            message_id=message_id,
            reply=(
                "ميزة الشات غير مفعّلة حاليًا "
                "لأن Gemini API Key غير موجود."
            ),
            cards=[
                Card(
                    type="warning",
                    title="Gemini Disabled",
                    body=(
                        "ضيفي GEMINI_API_KEY "
                        "أو GOOGLE_API_KEY"
                    )
                )
            ]
        )

    # ========================================================
    # Gemini response
    # ========================================================

    try:

        final_reply = gemini_reply(
            f"""
            أنت مساعد اسمه رفيق.
            متخصص في التربية والأسرة فقط.

            USER:
            {user_text}
            """
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    return ChatResponse(
        message_id=message_id,
        reply=final_reply,
        cards=[]
    )


# ============================================================
# RUN
# ============================================================

"""
تشغيل:

uvicorn rafiq_bot_api:app --reload
"""
