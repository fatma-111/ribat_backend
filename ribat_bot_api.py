"""
Rafiq Chatbot API (Gemini) - Full
(Original structure preserved)
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

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ENABLE_VERIFY = os.getenv("RIBAT_VERIFY_OUTPUT", "0") == "1"
PERSIST_MEMORY = os.getenv("RIBAT_PERSIST_MEMORY", "1") == "1"

DATA_DIR = os.getenv("RIBAT_DATA_DIR", "data")

MEMORY_FILE = os.path.join(DATA_DIR, "ribat_user_memory.json")
ANALYTICS_FILE = os.path.join(DATA_DIR, "ribat_analytics.json")
APPOINTMENTS_FILE = os.path.join(DATA_DIR, "ribat_appointments.json")


# =======================
# SAFE GEMINI INIT (FIXED)
# =======================
client = None
if GEMINI_ENABLED:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print("Gemini init error:", e)
        client = None


app = FastAPI(title="Rafiq Chatbot API (Gemini) - Full")


# ============================================================
# ====================== FULL KB =============================
# ============================================================
KB = [
    {
        "id": "kb_001",
        "topic": "teen_communication",
        "age_min": 12,
        "age_max": 18,
        "tags": ["مراهق", "مش بيرد", "ساكت"],
        "tip": "ابدئي بهدوء: أنا عايزة أفهمك مش ألومك."
    },
    {
        "id": "kb_002",
        "topic": "anger",
        "age_min": 6,
        "age_max": 18,
        "tags": ["عصبية", "غضب", "صراخ"],
        "tip": "وقت الغضب قللي الكلام وركزي على الهدوء."
    }
]


# ============================================================
# MEMORY
# ============================================================
USER_MEMORY: Dict[str, Dict[str, Any]] = {}


def get_user_memory(user_id: str):
    return USER_MEMORY.get(user_id, {"child_age": None, "notes": []})


def update_memory(user_id: str, topic: str, age: Optional[int], note: str):
    mem = get_user_memory(user_id)
    if age:
        mem["child_age"] = age
    mem["notes"].append(note)
    USER_MEMORY[user_id] = mem


# ============================================================
# ANALYTICS
# ============================================================
ANALYTICS: List[Dict[str, Any]] = []


def log_event(user_id: str, topic: str, msg: str):
    ANALYTICS.append({
        "user_id": user_id,
        "topic": topic,
        "msg": msg,
        "ts": datetime.utcnow().isoformat()
    })


# ============================================================
# RISK
# ============================================================
def detect_risk(text: str):
    if any(x in text for x in ["انتحار", "أموت", "أذي نفسي"]):
        return "high"
    return "low"


# ============================================================
# KB SEARCH
# ============================================================
def kb_search(topic: str, query: str, age: Optional[int]):
    res = []
    for item in KB:
        if topic and item["topic"] != topic:
            continue
        if age and not (item["age_min"] <= age <= item["age_max"]):
            continue
        if query.lower() in item["tip"].lower() or any(q in item["tags"] for q in query.split()):
            res.append(item)
    return res


# ============================================================
# ROUTER (FIXED SYSTEM NAME ONLY)
# ============================================================
def route(user_text: str):
    if not client:
        return {"in_scope": False, "topic": "out_of_scope"}

    prompt = f"أنت Router لتطبيق رفيق. صنف الرسالة:\n{user_text}"

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )
    return {"raw": resp.text}


# ============================================================
# CHAT
# ============================================================
@app.post("/chat")
def chat(req: dict):

    user_id = req["user_id"]
    user_text = req["messages"][-1]["content"]

    if not client:
        return {"reply": "Gemini not enabled"}

    risk = detect_risk(user_text)

    decision = route(user_text)

    mem = get_user_memory(user_id)

    tips = kb_search("anger", user_text, mem.get("child_age"))

    update_memory(user_id, "anger", mem.get("child_age"), user_text)
    log_event(user_id, "chat", user_text)

    reply = f"""
أنا فاهمك ❤️

{user_text}

نصيحة:
{tips[0]['tip'] if tips else 'خلينا نفهم أكتر'}
"""

    return {
        "reply": reply,
        "system": "rafiq",
        "risk": risk,
        "router": decision
    }


# ============================================================
# HEALTH
# ============================================================
@app.get("/health")
def health():
    return {
        "system": "rafiq",
        "gemini": GEMINI_ENABLED,
        "client": client is not None
    }


# ============================================================
# =================== ASSESSMENT FULL ========================
# ============================================================

TRAITS = ["leadership", "focus", "empathy"]

ASSESSMENT_QUESTIONS = [
    {
        "id": "q1",
        "text": "بيحب يقود المجموعة",
        "age_min": 7,
        "age_max": 18,
        "weights": {"leadership": 2}
    },
    {
        "id": "q2",
        "text": "بيكمل المهام للنهاية",
        "age_min": 7,
        "age_max": 18,
        "weights": {"focus": 2}
    }
]


def get_assessment_questions(age):
    if not age:
        return ASSESSMENT_QUESTIONS
    return [q for q in ASSESSMENT_QUESTIONS if q["age_min"] <= age <= q["age_max"]]


def compute_profile(answers, age):
    scores = {t: 0 for t in TRAITS}

    for a in answers:
        qid = a["question_id"]
        val = a["value"]

        for q in ASSESSMENT_QUESTIONS:
            if q["id"] == qid:
                for t, w in q["weights"].items():
                    scores[t] += val * w

    return {"age": age, "scores": scores}


@app.get("/assessment/questions")
def assessment_questions(age: Optional[int] = None):
    return {"questions": get_assessment_questions(age)}


@app.post("/assessment/submit")
def assessment_submit(req: dict):
    profile = compute_profile(req["answers"], req.get("child_age"))

    return {
        "ok": True,
        "profile": profile
    }
