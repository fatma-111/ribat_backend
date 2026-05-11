"""
rafiq_bot_api.py
Rafiq Chatbot API (Gemini) - Full
"""

from dotenv import load_dotenv
load_dotenv()

import os
import uuid
from typing import List, Optional, Dict, Any, Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-1.5-flash"
)

GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)

ADMIN_KEY = os.getenv("RIBAT_ADMIN_KEY", "change-me")

if ADMIN_KEY == "change-me":
    print("WARNING: ADMIN key is default. Set it in ENV for production.")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_ENABLED else None

app = FastAPI(title="Rafiq Bot API")


# =======================
# CORS
# =======================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # للتجربة فقط
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =======================
# DUMMY DATA
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
            "notes": []
        }
    )


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
        return "رفيق غير مفعل حاليًا. تحقق من GEMINI_API_KEY"

    prompt = f"""
أنت مساعد اسمه "رفيق" متخصص في دعم الأسرة والتربية.

تكلم بطريقة:
- بسيطة
- دافئة
- قصيرة
- مطمئنة

User Message:
{user_text}

Topic:
{topic}

Tips:
{tips}

Memory:
{memory}
"""

    try:

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )

        print("GEMINI RESPONSE:")
        print(response)

        if hasattr(response, "text") and response.text:
            return response.text

        return "ممكن توضحي أكتر؟"

    except Exception as e:

        print("GEMINI ERROR:", str(e))

        return f"حدث خطأ أثناء التواصل مع Gemini: {str(e)}"


# =======================
# ROUTES
# =======================
@app.get("/")
def home():
    return {
        "message": "Rafiq Bot API is running 🚀"
    }


# =======================
# CHAT
# =======================
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):

    try:

        msg_id = "msg_" + uuid.uuid4().hex[:10]

        if not req.messages:
            return ChatResponse(
                message_id=msg_id,
                reply="لا توجد رسائل."
            )

        user_text = req.messages[-1].content

        mem = get_user_memory(req.user_id)

        update_memory(
            req.user_id,
            user_text,
            req.child_age
        )

        risk = detect_risk(user_text)

        if risk == "high":
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

        topic = "general"

        tips = [
            {
                "tip": "حاول تتكلم بهدوء"
            }
        ]

        reply = compose_reply(
            user_text,
            topic,
            tips,
            mem
        )

        return ChatResponse(
            message_id=msg_id,
            reply=reply,
            cards=[
                Card(
                    type="tip",
                    title="نصيحة",
                    body="التواصل الهادئ يساعد الطفل يشعر بالأمان."
                )
            ]
        )

    except Exception as e:

        print("CHAT ERROR:", str(e))

        return ChatResponse(
            message_id="error",
            reply=f"حدث خطأ داخلي: {str(e)}",
            cards=[]
        )
