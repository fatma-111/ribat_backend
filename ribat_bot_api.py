"""
rafiq_bot_api.py
Rafiq Chatbot API (Gemini) - Full
"""

from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import re

from typing import List, Optional, Dict, Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel

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

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_ENABLED else None

app = FastAPI(title="Rafiq Bot API")

from fastapi.middleware.cors import CORSMiddleware


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
# MEMORY
# =======================
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
    return USER_MEMORY.get(
        user_id,
        {
            "child_age": None,
            "notes": [],
            "preferred_language": None
        }
    )


def update_memory(user_id: str, note: str, age: Optional[int]):
    mem = get_user_memory(user_id)

    if age:
        mem["child_age"] = age

    mem["notes"].append(note[:150])

    USER_MEMORY[user_id] = mem


def set_user_language(user_id: str, lang: str):
    mem = get_user_memory(user_id)
    mem["preferred_language"] = lang
    USER_MEMORY[user_id] = mem


def get_user_language(user_id: str):
    mem = get_user_memory(user_id)
    return mem.get("preferred_language")


def detect_language(text: str) -> str:
    arabic_chars = re.findall(r'[\u0600-\u06FF]', text)

    if len(arabic_chars) > 0:
        return "ar"

    return "en"


def detect_language_command(text: str):
    t = text.lower()

    english_commands = [
        "speak english",
        "english please",
        "talk in english",
        "reply in english",
        "اتكلم انجليزي",
        "اتكلم بالانجليزي",
        "خليك انجليزي",
    ]

    arabic_commands = [
        "اتكلم عربي",
        "رد بالعربي",
        "arabic please",
        "speak arabic",
        "reply in arabic",
    ]

    for cmd in english_commands:
        if cmd in t:
            return "en"

    for cmd in arabic_commands:
        if cmd in t:
            return "ar"

    return None


def clean_response(text: str) -> str:
    if not text:
        return ""

    # remove markdown stars
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)

    # remove markdown symbols
    text = re.sub(r"`", "", text)
    text = re.sub(r"#", "", text)

    return text.strip()


def detect_risk(text: str):
    t = text.lower()

    if "انتحار" in t or "أموت" in t:
        return "high"

    return "low"


# =======================
# GEMINI
# =======================
def compose_reply(user_text, topic, tips, memory, lang):

    if not GEMINI_ENABLED:

        if lang == "en":
            return "Rafiq is currently unavailable."

        return "رفيق غير مفعل حاليًا."

    if lang == "ar":

        prompt = f"""
أنت مساعد ذكي اسمه "رفيق" متخصص في دعم الأسرة والأطفال.

قواعد مهمة:
- رد بالعربية العامية المصرية البسيطة.
- كن داعم وهادئ ومطمئن.
- لا تستخدم أي markdown.
- لا تستخدم نجوم *.
- اجعل الرد طبيعي وبشري.

رسالة المستخدم:
{user_text}

الموضوع:
{topic}

النصائح:
{tips}

الذاكرة:
{memory}
"""

    else:

        prompt = f"""
You are an AI assistant called "Rafiq" specialized in parenting and family support.

Rules:
- Reply in natural simple English.
- Be warm and supportive.
- Do NOT use markdown.
- Do NOT use stars (*).
- Make the reply conversational.

User Message:
{user_text}

Topic:
{topic}

Tips:
{tips}

Memory:
{memory}
"""

    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )

    raw_text = r.text or (
        "Can you explain more?"
        if lang == "en"
        else "ممكن توضحي أكتر؟"
    )

    return clean_response(raw_text)


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

    # =======================
    # CHANGE LANGUAGE
    # =======================
    lang_command = detect_language_command(user_text)

    if lang_command:

        set_user_language(req.user_id, lang_command)

        if lang_command == "en":

            return ChatResponse(
                message_id=msg_id,
                reply="Done! I will speak English with you from now on.",
                cards=[]
            )

        return ChatResponse(
            message_id=msg_id,
            reply="تمام، هتكلم معاك بالعربي من دلوقتي.",
            cards=[]
        )

    # =======================
    # LANGUAGE
    # =======================
    saved_lang = get_user_language(req.user_id)

    lang = saved_lang if saved_lang else detect_language(user_text)

    # =======================
    # RISK CHECK
    # =======================
    risk = detect_risk(user_text)

    if risk == "high":

        if lang == "en":

            return ChatResponse(
                message_id=msg_id,
                reply="I'm worried about you ❤️ Please talk to someone you trust.",
                cards=[
                    Card(
                        type="warning",
                        title="Important",
                        body="Please seek support from a trusted adult or specialist."
                    )
                ]
            )

        return ChatResponse(
            message_id=msg_id,
            reply="أنا قلق عليك ❤️ حاول تتكلم مع شخص قريب منك فورًا.",
            cards=[
                Card(
                    type="warning",
                    title="تنبيه مهم",
                    body="اطلب دعم فوري من شخص بالغ أو مختص."
                )
            ]
        )

    # =======================
    # TOPIC + TIPS
    # =======================
    topic = "general"

    tips = [
        {
            "tip":
            "حاول تتكلم بهدوء"
            if lang == "ar"
            else "Try to communicate calmly"
        }
    ]

    # =======================
    # AI RESPONSE
    # =======================
    reply = compose_reply(
        user_text=user_text,
        topic=topic,
        tips=tips,
        memory=mem,
        lang=lang
    )

    # =======================
    # FINAL RESPONSE
    # =======================
    return ChatResponse(
        message_id=msg_id,
        reply=reply,
        cards=[
            Card(
                type="tip",
                title="نصيحة" if lang == "ar" else "Tip",
                body=(
                    "التواصل الهادئ هو الحل الأفضل."
                    if lang == "ar"
                    else "Calm communication is usually the best approach."
                )
            )
        ]
    )
