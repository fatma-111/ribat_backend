"""
Rafiq Bot API - SAFE FINAL VERSION
FastAPI + PostgreSQL + Safe Gemini + Memory + Chat + Assessment
"""

from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import re
import json
from typing import List, Optional, Literal, Dict, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import psycopg2

# Gemini (safe import)
try:
    from google import genai
except:
    genai = None


# ======================
# CONFIG
# ======================
DATABASE_URL = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

client = None

# SAFE INIT GEMINI
if genai and GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini initialized ✔")
    except Exception as e:
        print("Gemini init failed:", e)
        client = None
else:
    print("Gemini disabled (missing key or library)")


app = FastAPI(title="Rafiq Safe API 🚀")


# ======================
# CORS
# ======================
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
# MODELS
# ======================
class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatReq(BaseModel):
    user_id: str
    messages: List[Msg]
    child_age: Optional[int] = None


# ======================
# UTIL
# ======================
def detect_lang(text):
    return "ar" if re.findall(r'[\u0600-\u06FF]', text) else "en"


def clean(text):
    return re.sub(r"[\*`#]", "", text or "").strip()


def detect_risk(text):
    t = text.lower()
    return "high" if ("انتحار" in t or "kill" in t) else "low"


# ======================
# MEMORY
# ======================
def get_memory(conn, user_id):
    cur = conn.cursor()
    cur.execute("""
        SELECT notes, child_age
        FROM users
        WHERE user_id=%s
    """, (user_id,))
    row = cur.fetchone()

    if not row:
        return {}

    return {
        "notes": row[0],
        "child_age": row[1]
    }


def update_memory(conn, user_id, text, child_age):
    cur = conn.cursor()

    cur.execute("SELECT notes FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()

    if row:
        notes = row[0] or []
        notes.append(text[:150])

        cur.execute("""
            UPDATE users
            SET notes=%s,
                child_age=COALESCE(%s, child_age),
                updated_at=NOW()
            WHERE user_id=%s
        """, (json.dumps(notes), child_age, user_id))

    else:
        cur.execute("""
            INSERT INTO users (user_id, child_age, notes)
            VALUES (%s, %s, %s)
        """, (user_id, child_age, json.dumps([text[:150]])))


# ======================
# SAFE AI
# ======================
def ai_reply(text, memory, lang):

    print("AI START")

    if client is None:
        print("NO CLIENT")
        return "AI is not enabled"

    try:
        res = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=text
        )

        print("AI SUCCESS")
        return res.text

    except Exception as e:
        print("AI ERROR:", e)
        return "AI failed"


# ======================
# ROUTES
# ======================

@app.get("/")
def home():
    return {"status": "Rafiq running 🚀"}


# ======================
# CHAT
# ======================
@app.post("/chat")
def chat(req: ChatReq):

    try:
        user_text = req.messages[-1].content
        lang = detect_lang(user_text)
        risk = detect_risk(user_text)
        msg_id = "msg_" + uuid.uuid4().hex[:10]

        conn = get_conn()

        memory = get_memory(conn, req.user_id)

        if risk == "high":
            return {
                "message_id": msg_id,
                "reply": "Please talk to someone you trust ❤️" if lang == "en"
                else "أنا قلق عليك ❤️ كلم حد قريب منك"
            }

        reply = ai_reply(user_text, memory, lang)

        update_memory(conn, req.user_id, user_text, req.child_age)

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO chat_messages (message_id, user_id, message, response)
            VALUES (%s, %s, %s, %s)
        """, (msg_id, req.user_id, user_text, reply))

        cur.execute("""
            INSERT INTO analytics
            (
                event_id,
                user_id,
                event_type,
                value
            )
            VALUES (%s,%s,%s,%s)
        """, (
            "evt_" + uuid.uuid4().hex[:10],
            req.user_id,
            "chat",
            user_text[:100]
        ))
        conn.commit()
        conn.close()

        return {
            "message_id": msg_id,
            "reply": reply
        }

    except Exception as e:
        print("CHAT ERROR:", e)
        return {
            "error": "internal_error",
            "details": str(e)
        }


# ======================
# GET CHAT HISTORY
# ======================
@app.get("/chat/{user_id}")
def get_chat(user_id: str, limit: int = 50):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT message_id, message, response, created_at
        FROM chat_messages
        WHERE user_id=%s
        ORDER BY created_at DESC
        LIMIT %s
    """, (user_id, limit))

    rows = cur.fetchall()
    conn.close()

    return {
        "messages": [
            {
                "message_id": r[0],
                "user_message": r[1],
                "bot_reply": r[2],
                "created_at": r[3].isoformat() if r[3] else None
            }
            for r in rows
        ]
    }


# ======================
# MEMORY
# ======================
@app.get("/memory/{user_id}")
def memory(user_id: str):

    conn = get_conn()
    data = get_memory(conn, user_id)
    conn.close()

    return data


# ======================
# APPOINTMENTS (SAFE)
# ======================
@app.post("/appointments")
def book(user_id: str, specialist_id: str, slot_id: str):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO appointments
        (appointment_id, user_id, specialist_id, slot_id, status)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        str(uuid.uuid4()),
        user_id,
        specialist_id,
        slot_id,
        "pending"
    ))

    conn.commit()
    conn.close()

    return {"status": "booked"}


# ======================
# ASSESSMENT
# ======================

class AssessmentReq(BaseModel):
    user_id: str
    child_age: int
    result: Dict[str, Any]
    confidence: float = 1.0


@app.post("/assessment")
def save_assessment(req: AssessmentReq):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO assessments
        (
            user_id,
            child_age,
            assessment_confidence,
            result,
            created_at
        )
        VALUES (%s,%s,%s,%s,NOW())
    """, (
        req.user_id,
        req.child_age,
        req.confidence,
        json.dumps(req.result)
    ))

    conn.commit()
    conn.close()

    return {"status": "saved"}


@app.get("/assessment/{user_id}")
def get_assessments(user_id: str):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            child_age,
            assessment_confidence,
            result,
            created_at
        FROM assessments
        WHERE user_id=%s
        ORDER BY created_at DESC
    """, (user_id,))

    rows = cur.fetchall()
    conn.close()

    return {
        "assessments": [
            {
                "id": r[0],
                "child_age": r[1],
                "confidence": float(r[2]),
                "result": r[3],
                "created_at": r[4]
            }
            for r in rows
        ]
    }


# ======================
# SPECIALISTS
# ======================

@app.get("/specialists")
def specialists():

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            name,
            title,
            topics,
            price_egp,
            rating
        FROM specialists
        ORDER BY rating DESC
    """)

    rows = cur.fetchall()
    conn.close()

    return {
        "specialists": [
            {
                "id": r[0],
                "name": r[1],
                "title": r[2],
                "topics": r[3],
                "price_egp": float(r[4]),
                "rating": float(r[5])
            }
            for r in rows
        ]
    }


# ======================
# SLOTS
# ======================

@app.get("/slots/{specialist_id}")
def get_slots(specialist_id: str):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            slot_id,
            start_time,
            duration_min,
            available
        FROM slots
        WHERE specialist_id=%s
        ORDER BY start_time
    """, (specialist_id,))

    rows = cur.fetchall()
    conn.close()

    return {
        "slots": [
            {
                "slot_id": r[0],
                "start_time": r[1],
                "duration_min": r[2],
                "available": r[3]
            }
            for r in rows
        ]
    }


# ======================
# GET APPOINTMENTS
# ======================

@app.get("/appointments/{user_id}")
def get_user_appointments(user_id: str):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            appointment_id,
            specialist_id,
            slot_id,
            status,
            created_at
        FROM appointments
        WHERE user_id=%s
        ORDER BY created_at DESC
    """, (user_id,))

    rows = cur.fetchall()
    conn.close()

    return {
        "appointments": [
            {
                "appointment_id": r[0],
                "specialist_id": r[1],
                "slot_id": r[2],
                "status": r[3],
                "created_at": r[4]
            }
            for r in rows
        ]
    }

