"""
Rafiq Bot API — PRODUCTION v4
==============================
Changes in v4:
- Full i18n via translations.py (ar / en)
- preferred_language stored on users table
- Assessment scoring bug fixed:
    * accepts both "value" and "score" fields from Flutter
    * case-insensitive question ID matching (q01 == Q01)
    * debug diagnostics in response
    * age filter made optional (None → all questions valid)
- Bilingual Gemini prompts (parenting plan, chat, verification)
- PDF: RTL Arabic support via arabic-reshaper + python-bidi
  with graceful fallback when libs missing
- All hardcoded Arabic strings replaced with t() calls
- preferred_language written to users table on upsert
"""

from dotenv import load_dotenv
load_dotenv()

import os, json, uuid, re, io
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import psycopg2

# ── Translations (inlined) ────────────────────────────────────────────
from typing import Literal

Lang = Literal["ar", "en"]

_T: dict[str, dict[str, str]] = {
    # ── System / generic ──────────────────────────────────────────────
    "gemini_disabled": {
        "ar": "ميزة الشات غير مفعّلة. التقييم والـ Memory شغالين ✅",
        "en": "Chat feature is currently disabled. Assessment and Memory are working ✅",
    },
    "ok": {
        "ar": "تم بنجاح",
        "en": "Success",
    },

    # ── Out of scope ───────────────────────────────────────────────────
    "out_of_scope_reply": {
        "ar": "أنا بوت (رفيق) متخصص في دعم الأسرة. مش بقدر أساعد في برمجة/أدوية/تشخيص.",
        "en": "I'm Rafiq, a family support assistant. I can't help with programming, medication, or diagnosis.",
    },
    "out_of_scope_card": {
        "ar": "اسأل عن: مراهقة، عصبية، موبايل، تنمر، مذاكرة، قصص أطفال، ألعاب، تقييم شخصية.",
        "en": "Ask about: teen communication, anger, screen time, bullying, studying, kids stories, games, personality assessment.",
    },
    "scope_refusal": {
        "ar": "سؤالك خارج نطاق رفيق. اسأل عن مشكلة أسرية/تربوية وأنا أساعدك فورًا ✅",
        "en": "Your question is outside Rafiq's scope. Ask about a parenting or family issue and I'll help right away ✅",
    },

    # ── Risk ──────────────────────────────────────────────────────────
    "risk_high": {
        "ar": "أنا قلقان عليك جدًا. تواصل فورًا مع شخص كبير موثوق قريب منك أو خدمات الطوارئ.",
        "en": "I'm very concerned about you. Please immediately reach out to a trusted adult or call emergency services.",
    },
    "risk_high_card": {
        "ar": "في الحالات العاجلة لازم تدخل مختص فورًا. رفيق للدعم العام فقط.",
        "en": "In urgent cases a specialist must intervene immediately. Rafiq is for general support only.",
    },

    # ── Kids safety ───────────────────────────────────────────────────
    "kids_safety": {
        "ar": "خلّينا نخلي المحتوى مناسب للأطفال 🙏 قوليلي سن الطفل والموضوع.",
        "en": "Let's keep content child-appropriate 🙏 Please share the child's age and topic.",
    },

    # ── Booking ───────────────────────────────────────────────────────
    "missing_slot": {
        "ar": "ابعت رقم الموعد — مثال: احجز sl_001",
        "en": "Please send the slot number — example: book sl_001",
    },
    "slot_unavailable": {
        "ar": "الموعد مش متاح. اختر ميعاد تاني.",
        "en": "This slot is no longer available. Please choose another.",
    },
    "booking_success": {
        "ar": "تم الحجز ✅ رقم الحجز: ",
        "en": "Booking confirmed ✅ Booking ID: ",
    },
    "booking_details": {
        "ar": "تفاصيل الحجز",
        "en": "Booking details",
    },
    "available_slots": {
        "ar": "مواعيد متاحة",
        "en": "Available Slots",
    },
    "slots_suffix_ar": "\n\nللحجز ابعت: احجز sl_001",
    "slots_suffix_en": "\n\nTo book: send 'book sl_001'",

    # ── Confidence / follow-up ────────────────────────────────────────
    "low_conf_prefix": {
        "ar": "الموضوع محتاج تفاصيل أكتر. ",
        "en": "I need a bit more context to help effectively. ",
    },
    "low_conf_suffix": {
        "ar": " ولو تقدر احكيلي موقف حصل قريب.",
        "en": " If you can, share a recent situation that happened.",
    },
    "confidence_score": {
        "ar": "درجة الثقة",
        "en": "Confidence Score",
    },
    "follow_up": {
        "ar": "سؤال متابعة",
        "en": "Follow-up",
    },

    # ── Verify fallback ───────────────────────────────────────────────
    "verify_fallback": {
        "ar": "أنا معاك ✅ بس خلّيني أسألك: ",
        "en": "I'm here for you ✅ Let me ask: ",
    },

    # ── Assessment ────────────────────────────────────────────────────
    "assessment_note": {
        "ar": "النتيجة إرشادية وليست تشخيصًا طبيًا.",
        "en": "This result is indicative, not a clinical diagnosis.",
    },
    "assessment_result_title": {
        "ar": "نتيجة تقييم شخصية الطفل",
        "en": "Child Personality Assessment Result",
    },

    # ── Notifications ─────────────────────────────────────────────────
    "daily_tip_notif_title": {
        "ar": "💡 نصيحة جديدة من رفيق",
        "en": "💡 New Parenting Tip from Rafiq",
    },
    "daily_tip_notif_body_prefix": {
        "ar": "",   # body IS the tip in Arabic
        "en": "",
    },
    "plan_notif_title": {
        "ar": "📋 تم إنشاء خطة تربوية جديدة",
        "en": "📋 New Parenting Plan Created",
    },
    "plan_notif_body": {
        "ar": "تم إعداد خطة مخصصة لطفلك بناءً على نتائج التقييم.",
        "en": "A personalized parenting plan has been generated based on your child's assessment.",
    },

    # ── API response messages ─────────────────────────────────────────
    "plan_created_title": {
        "ar": "تم إنشاء الخطة بنجاح",
        "en": "Parenting plan generated successfully",
    },
    "token_saved": {
        "ar": "تم حفظ رمز الإشعار بنجاح",
        "en": "FCM token saved successfully",
    },
    "no_fcm_token": {
        "ar": "المستخدم لا يملك رمز إشعار. استدعِ POST /register-token أولًا.",
        "en": "User has no registered FCM token. Call POST /register-token first.",
    },
    "fcm_token_expired": {
        "ar": "رمز FCM لم يعد صالحًا. يُرجى إعادة التسجيل عبر POST /register-token.",
        "en": "FCM token is no longer valid (device unregistered). Please re-register via POST /register-token.",
    },
    "firebase_not_configured": {
        "ar": "Firebase غير مُفعَّل — تم حفظ الخطة لكن لم يُرسَل إشعار.",
        "en": "Firebase is not configured — plan saved but no push notification sent.",
    },
    "no_assessment_found": {
        "ar": "لا يوجد تقييم لهذا المستخدم. أكمل التقييم عبر POST /assessment/submit أولًا.",
        "en": "No assessment found for this user. Please complete an assessment first via POST /assessment/submit.",
    },
    "no_plan_found": {
        "ar": "لا توجد خطة تربوية لهذا المستخدم. أنشئ خطة عبر POST /generate-parenting-plan/{user_id} أولًا.",
        "en": "No parenting plan found for this user. Generate one first via POST /generate-parenting-plan/{user_id}.",
    },
    "user_not_found": {
        "ar": "المستخدم غير موجود.",
        "en": "User not found.",
    },
    "pdf_unavailable": {
        "ar": "تصدير PDF غير متاح — مكتبة reportlab غير مثبّتة.",
        "en": "PDF export is unavailable — reportlab is not installed. Run: pip install reportlab",
    },

    # ── PDF labels ────────────────────────────────────────────────────
    "pdf_main_title": {
        "ar": "خطة تربوية مخصصة — رفيق AI",
        "en": "Personalised Parenting Plan — Rafiq AI",
    },
    "pdf_subtitle": {
        "ar": "خطة 30 يومًا",
        "en": "30-Day Plan",
    },
    "pdf_label_user_id": {
        "ar": "معرف المستخدم",
        "en": "User ID",
    },
    "pdf_label_child_age": {
        "ar": "عمر الطفل",
        "en": "Child Age",
    },
    "pdf_label_archetype": {
        "ar": "النمط الشخصي",
        "en": "Top Archetype",
    },
    "pdf_label_generated": {
        "ar": "تاريخ الإنشاء",
        "en": "Generated",
    },
    "pdf_label_age_unknown": {
        "ar": "غير محدد",
        "en": "Not specified",
    },
    "pdf_section_plan": {
        "ar": "الخطة التربوية",
        "en": "Parenting Plan",
    },
    "pdf_footer_line1": {
        "ar": "أُنشئت بواسطة رفيق AI — هذه الخطة إرشادية وليست تشخيصًا طبيًا.",
        "en": "Generated by Rafiq AI — This plan is for guidance only and is not a clinical diagnosis.",
    },

    # ── Card titles ───────────────────────────────────────────────────
    "card_out_of_scope": {
        "ar": "خارج نطاق رفيق",
        "en": "Out of scope",
    },
    "card_important": {
        "ar": "مهم جدًا",
        "en": "Important",
    },
    "card_tip": {
        "ar": "نصيحة عملية",
        "en": "Practical Tip",
    },
    "card_story": {
        "ar": "قصة للأطفال",
        "en": "Kids Story",
    },
    "card_game": {
        "ar": "لعبة / نشاط",
        "en": "Activity / Game",
    },
    "card_books": {
        "ar": "اقتراح قراءة",
        "en": "Book Suggestion",
    },
    "card_assessment": {
        "ar": "تقييم شخصية الطفل",
        "en": "Personality Assessment",
    },
    "card_specialist": {
        "ar": "مختص موصى به",
        "en": "Recommended Specialist",
    },
    "card_specialist_body_en": "Price: {price} EGP | Rating: {rating}",
    "card_specialist_body_ar": "السعر: {price} جنيه | التقييم: {rating}",
    "card_missing_booking": {
        "ar": "ناقص بيانات الحجز",
        "en": "Missing booking data",
    },
    "card_slot_unavailable": {
        "ar": "الموعد غير متاح",
        "en": "Slot unavailable",
    },
    "card_refusal_reason_prefix": {
        "ar": "السبب: ",
        "en": "Reason: ",
    },
    "child_appropriate_content": {
        "ar": "محتوى مناسب للأطفال",
        "en": "Child-appropriate content",
    },
    "choose_safe_topic": {
        "ar": "اختر موضوعًا مناسبًا للأطفال.",
        "en": "Choose a safe, age-appropriate topic.",
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    """
    Look up a translation key.  Falls back to Arabic, then the key itself.
    Supports simple .format() substitutions via kwargs.
    """
    lang = lang if lang in ("ar", "en") else "ar"
    entry = _T.get(key, {})
    if isinstance(entry, dict):
        text = entry.get(lang) or entry.get("ar") or key
    else:
        # plain string (e.g. slots_suffix_ar)
        text = entry or key
    return text.format(**kwargs) if kwargs else text


def detect_lang(text: str) -> Lang:
    """Return 'ar' if Arabic chars dominate, else 'en'."""
    import re
    ar = len(re.findall(r'[\u0600-\u06FF]', text))
    en = len(re.findall(r'[a-zA-Z]', text))
    return "ar" if ar >= en else "en"


def user_lang(preferred_language: str | None, fallback_text: str = "") -> Lang:
    """
    Resolve language from user's DB preference, falling back to text detection.
    """
    if preferred_language in ("ar", "en"):
        return preferred_language  # type: ignore[return-value]
    return detect_lang(fallback_text)

# ── reportlab ──────────────────────────────────────────────────────────
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    print("WARNING: reportlab not installed — PDF export disabled.")

# ── Arabic text shaping / bidi ─────────────────────────────────────────
try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_display
    _ARABIC_SHAPING = True
except ImportError:
    _ARABIC_SHAPING = False
    print("WARNING: arabic-reshaper / python-bidi not installed — Arabic PDF text may not render correctly.")

# ── Gemini ─────────────────────────────────────────────────────────────
try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

# ── Firebase ───────────────────────────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials, messaging as fb_messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    firebase_admin = fb_credentials = fb_messaging = None
    _FIREBASE_AVAILABLE = False

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DEBUG          = os.getenv("RAFIQ_DEBUG", "0") == "1"
DATABASE_URL   = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENABLED = bool(GEMINI_API_KEY) and (genai is not None)
ADMIN_KEY      = os.getenv("RAFIQ_ADMIN_KEY", "change-me")
ENABLE_VERIFY  = os.getenv("RAFIQ_VERIFY_OUTPUT", "0") == "1"

# Font paths (override via env)
FONT_DIR           = os.getenv("RAFIQ_FONT_DIR", "/app/fonts")
FONT_NOTO_ARABIC   = os.getenv("RAFIQ_FONT_ARABIC",  os.path.join(FONT_DIR, "NotoSansArabic-Regular.ttf"))
FONT_NOTO_BOLD     = os.getenv("RAFIQ_FONT_BOLD",    os.path.join(FONT_DIR, "NotoSansArabic-Bold.ttf"))
FONT_NOTO_LATIN    = os.getenv("RAFIQ_FONT_LATIN",   os.path.join(FONT_DIR, "NotoSans-Regular.ttf"))

if ADMIN_KEY == "change-me":
    print("WARNING: RAFIQ_ADMIN_KEY is default.")

client = None
if GEMINI_ENABLED:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini initialized ✔")
    except Exception as exc:
        print("Gemini init failed:", exc)

FIREBASE_ENABLED = False
_FIREBASE_CREDS_JSON = os.getenv("FIREBASE_CREDENTIALS", "").strip()
if _FIREBASE_AVAILABLE and _FIREBASE_CREDS_JSON:
    try:
        _fb_cred_dict = json.loads(_FIREBASE_CREDS_JSON)
        _fb_cred      = fb_credentials.Certificate(_fb_cred_dict)
        firebase_admin.initialize_app(_fb_cred)
        FIREBASE_ENABLED = True
        print("Firebase initialized ✔")
    except Exception as _fb_exc:
        print(f"Firebase init failed: {_fb_exc}")

# ── Register PDF fonts ─────────────────────────────────────────────────
_FONT_ARABIC_REGISTERED = False
_FONT_LATIN_REGISTERED  = False

def _register_fonts() -> None:
    global _FONT_ARABIC_REGISTERED, _FONT_LATIN_REGISTERED
    if not _REPORTLAB_AVAILABLE:
        return
    try:
        if os.path.exists(FONT_NOTO_ARABIC):
            pdfmetrics.registerFont(TTFont("NotoArabic",     FONT_NOTO_ARABIC))
            _FONT_ARABIC_REGISTERED = True
        if os.path.exists(FONT_NOTO_BOLD):
            pdfmetrics.registerFont(TTFont("NotoArabicBold", FONT_NOTO_BOLD))
        if os.path.exists(FONT_NOTO_LATIN):
            pdfmetrics.registerFont(TTFont("NotoLatin",      FONT_NOTO_LATIN))
            _FONT_LATIN_REGISTERED = True
        if _FONT_ARABIC_REGISTERED:
            print("PDF Arabic fonts registered ✔")
        else:
            print("PDF Arabic fonts NOT found — falling back to Helvetica (Arabic may be unreadable).")
    except Exception as exc:
        print(f"Font registration warning: {exc}")

# ──────────────────────────────────────────────
# APP
# ──────────────────────────────────────────────
app = FastAPI(
    title="Rafiq Bot API",
    version="4.0.0",
    description="Family support & parenting assistant API — bilingual (ar/en)",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    _run_schema_migrations()
    _register_fonts()

# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def _run_schema_migrations() -> None:
    if not DATABASE_URL:
        print("Skipping DB migrations — DATABASE_URL not set")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur  = conn.cursor()
        # existing
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token TEXT;")
        # v4 additions
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language VARCHAR(5) DEFAULT 'ar';")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_tips (
                id         SERIAL PRIMARY KEY,
                user_id    VARCHAR(100),
                tip        TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS parenting_plans (
                id            SERIAL PRIMARY KEY,
                user_id       VARCHAR(100),
                plan_text     TEXT,
                plan_language VARCHAR(5) DEFAULT 'ar',
                created_at    TIMESTAMP DEFAULT NOW()
            );
            """
        )
        cur.execute("ALTER TABLE parenting_plans ADD COLUMN IF NOT EXISTS plan_language VARCHAR(5) DEFAULT 'ar';")
        conn.commit()
        conn.close()
        print("DB migrations applied ✔")
    except Exception as exc:
        print(f"DB migration warning: {exc}")

# ──────────────────────────────────────────────
# KNOWLEDGE BASE (unchanged content)
# ──────────────────────────────────────────────
KB: List[Dict[str, Any]] = [
    {"id": "kb_001", "topic": "teen_communication", "age_min": 12, "age_max": 18,
     "tags": ["مراهق", "مراهقة", "مش بيرد", "ساكت", "قافل"],
     "tip": "ابدئي في وقت هدوء بجملة: «أنا مهتمة أفهمك مش ألومك». اسألي سؤال واحد مفتوح وسيبي مساحة للرد."},
    {"id": "kb_002", "topic": "anger", "age_min": 6, "age_max": 18,
     "tags": ["عصبية", "غضب", "صراخ", "بيزعق"],
     "tip": "وقت الغضب قللي الكلام وثبتي حدود هادية. بعد ما يهدى: «إيه اللي ضايقك؟ وإيه الحل المرة الجاية؟»."},
    {"id": "kb_003", "topic": "screen_addiction", "age_min": 8, "age_max": 18,
     "tags": ["موبايل", "شاشات", "تيك توك", "إدمان"],
     "tip": "اعملي اتفاق مكتوب: وقت شاشة + وقت عيلة. قلّلي تدريجيًا (15 دقيقة) مع بديل ممتع مش عقاب."},
    {"id": "kb_004", "topic": "bullying", "age_min": 6, "age_max": 18,
     "tags": ["تنمر", "مدرسة", "سخرية", "بيضرب"],
     "tip": "صدّقي مشاعره، خدي تفاصيل بسيطة، تواصلي مع المدرسة، ودرّبيه على ردود قصيرة وطلب المساعدة."},
    {"id": "kb_005", "topic": "study_focus", "age_min": 8, "age_max": 18,
     "tags": ["مذاكرة", "تركيز", "تسويف", "واجب"],
     "tip": "قسّمي المذاكرة لبلوكات 25 دقيقة + 5 راحة. خلي البداية سهلة (أول 5 دقائق) لتكسير حاجز البدء."},
    {"id": "kb_100", "topic": "kids_stories", "age_min": 4, "age_max": 10,
     "tags": ["قصة", "قصص", "حكاية", "قبل النوم", "احكي"],
     "tip": (
         "قصة قصيرة (5 دقايق) — «نجمة والمشاركة»\n"
         "نجمة عندها لعبة جديدة، وكل ما أصحابها ييجوا تلعب لوحدها. "
         "في يوم، صحابها زعلوا ومشيوا. نجمة حسّت بالوحدة.\n"
         "ماما قالت: «المشاركة مش بتقلل لعبتك… بتكبر فرحتك».\n"
         "نجمة جرّبت تدي كل واحد دوره دقيقة، ولعبوا وضحكوا.\n"
         "الدرس: المشاركة + الدور.\nسؤال للطفل: إنت كنت هتعمل إيه لو كنت مكان نجمة؟"
     )},
    {"id": "kb_101", "topic": "activities_games", "age_min": 4, "age_max": 12,
     "tags": ["لعبة", "نشاط", "ملل", "بيت", "وقت فراغ"],
     "tip": (
         "لعبة 10 دقايق: «صيد المشاعر»\n"
         "الأدوات: ورق + قلم.\n"
         "اكتبوا 6 مشاعر، اسحبوا ورقة، الطفل يمثل موقف للمشاعر دي.\n"
         "وبعدها: «إيه اللي يساعدني لما أحس كده؟»\n"
         "الهدف: التعبير عن المشاعر + التهدئة."
     )},
    {"id": "kb_102", "topic": "book_recommendations", "age_min": 4, "age_max": 12,
     "tags": ["كتاب", "كتب", "قراءة", "اقترح كتب"],
     "tip": (
         "اقتراح كتب حسب السن:\n"
         "- سن 4–7: كتب مصوّرة عن الصداقة/المشاركة/الصدق.\n"
         "- سن 8–12: مغامرات + قيم (مسؤولية/شجاعة/تعاون).\n"
         "بعد القراءة اسألي: «إيه أكتر موقف عجبك؟ وإيه الدرس؟»"
     )},
    {"id": "kb_103", "topic": "assessment_personality", "age_min": 4, "age_max": 18,
     "tags": ["تقييم", "assessment", "شخصية", "قيادي", "اجتماعي"],
     "tip": (
         "We can run a personality assessment to help you understand your child better. "
         "Call GET /assessment/questions?age=X then POST /assessment/submit with the answers."
     )},
]

SPECIALISTS: List[Dict[str, Any]] = [
    {"id": "sp_001", "name": "Dr. Mariam Ali",    "title": "Family Counselor",
     "topics": ["teen_communication","anger","general_parenting"],
     "traits_focus": ["self_control","empathy","sociability"], "price_egp": 350, "rating": 4.8},
    {"id": "sp_002", "name": "Dr. Ahmed Hassan",  "title": "Child Psychologist",
     "topics": ["bullying","study_focus","sensitivity"],
     "traits_focus": ["focus","sensitivity","adaptability"],   "price_egp": 400, "rating": 4.6},
    {"id": "sp_003", "name": "Ms. Sara Mahmoud",  "title": "Behavior Modification Specialist",
     "topics": ["screen_addiction","anger","self_control"],
     "traits_focus": ["self_control","adaptability","focus"],  "price_egp": 300, "rating": 4.7},
    {"id": "sp_004", "name": "Dr. Layla Mostafa", "title": "Child Development Specialist",
     "topics": ["kids_stories","activities_games","assessment_personality"],
     "traits_focus": ["curiosity","sociability","leadership"],  "price_egp": 380, "rating": 4.9},
]

SLOTS: List[Dict[str, Any]] = [
    {"slot_id": "sl_001", "specialist_id": "sp_001", "start": "2026-07-10T18:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_002", "specialist_id": "sp_001", "start": "2026-07-11T20:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_003", "specialist_id": "sp_002", "start": "2026-07-10T19:00:00+02:00", "duration_min": 45, "available": True},
    {"slot_id": "sl_004", "specialist_id": "sp_003", "start": "2026-07-12T21:00:00+02:00", "duration_min": 30, "available": True},
    {"slot_id": "sl_005", "specialist_id": "sp_004", "start": "2026-07-13T17:00:00+02:00", "duration_min": 45, "available": True},
]

# ──────────────────────────────────────────────
# PYDANTIC MODELS
# ──────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    user_id: str
    messages: List[ChatMessage]
    child_age: Optional[int] = None
    preferred_language: Optional[str] = None   # "ar" | "en"

class ChatResponse(BaseModel):
    message_id: str
    reply: str
    cards: List[Dict[str, Any]] = []

class UserUpsertReq(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    child_age: Optional[int] = None
    preferred_language: Optional[str] = "ar"   # "ar" | "en"

class KbAddRequest(BaseModel):
    admin_key: str
    topic: str
    age_min: int = 6
    age_max: int = 18
    tags: List[str] = []
    tip: str

class AppEventRequest(BaseModel):
    user_id: str
    event_name: Literal[
        "open_app","view_content","save_tip","start_chat","complete_activity",
        "request_booking","complete_booking","behavior_event",
        "view_assessment","assessment_submit"
    ]
    meta: Dict[str, Any] = {}

class BookingReq(BaseModel):
    user_id: str
    specialist_id: str
    slot_id: str

class FeedbackReq(BaseModel):
    user_id: str
    message_id: str
    rating: Literal["up","down"]
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

AllowedTopic = Literal[
    "teen_communication","anger","screen_addiction","bullying","study_focus",
    "siblings_jealousy","parents_conflict","lying","general_parenting",
    "kids_stories","activities_games","book_recommendations",
    "assessment_personality","out_of_scope"
]
AllowedAction = Literal[
    "answer_with_tips","recommend_booking","book_appointment","refuse_out_of_scope"
]

class RouteDecision(BaseModel):
    in_scope: bool        = Field(description="Is question within Rafiq scope?")
    topic: AllowedTopic   = Field(description="Detected topic")
    action: AllowedAction = Field(description="Action to take")
    extracted_child_age: Optional[int] = Field(default=None)
    reason: str           = Field(description="Short reason")
    slot_id: Optional[str] = None
    specialist_id: Optional[str] = None

# ──────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────
PARENTING_TOPICS     = {"teen_communication","anger","screen_addiction","bullying",
                         "study_focus","siblings_jealousy","parents_conflict",
                         "lying","general_parenting"}
KIDS_CONTENT_TOPICS  = {"kids_stories","activities_games","book_recommendations"}
ASSESSMENT_TOPIC     = "assessment_personality"
ALL_TRAITS           = ["leadership","sociability","empathy","self_control",
                        "focus","curiosity","adaptability","sensitivity"]

OUT_OF_SCOPE_KW = ["برمجة","كود","flutter","android","python","java","c++",
                    "backend","front","database","debug","algorithm"]
MEDICAL_KW      = ["جرعة","دواء","حبوب","مضاد","تشخيص","روشتة","وصفة","medication","diagnosis"]
KIDS_UNSAFE_KW  = ["انتحار","إباحية","اباحية","سلاح","مخدرات"]
RISK_HIGH_KW    = ["عايز أموت","مش عايز أعيش","هأذي نفسي","انتحار","هنتحر","هقتل","هموت","أذي نفسي"]
RISK_MEDIUM_KW  = ["خوف شديد","هلع","نوبات","قلق جامد","اكتئاب",
                    "حزين طول الوقت","مش قادر","مخنوق طول الوقت"]

# ──────────────────────────────────────────────
# GUARDS & UTILS
# ──────────────────────────────────────────────
def hard_out_of_scope(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in OUT_OF_SCOPE_KW)

def hard_medical(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in MEDICAL_KW)

def kids_safety_guard(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in KIDS_UNSAFE_KW)

def detect_risk_level(text: str) -> Literal["low","medium","high"]:
    tl = text.lower()
    if any(k.lower() in tl for k in RISK_HIGH_KW):   return "high"
    if any(k.lower() in tl for k in RISK_MEDIUM_KW): return "medium"
    return "low"

def extract_slot_id(text: str) -> Optional[str]:
    m = re.search(r"\bsl_\d{3}\b", text.lower())
    return m.group(0) if m else None

# ──────────────────────────────────────────────
# KB SEARCH v2
# ──────────────────────────────────────────────
_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")
_AR_PUNCT      = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)
_AR_STOPWORDS  = {"في","من","على","عن","الى","إلى","هو","هي","ده","دي","دا",
                   "انا","انت","انتي","احنا","هم"}

def _ar_normalize(text: str) -> str:
    if not text: return ""
    t_ = _AR_DIACRITICS.sub("", text.strip())
    for a, b in [("أ","ا"),("إ","ا"),("آ","ا"),("ى","ي"),("ة","ه"),("ؤ","و"),("ئ","ي"),("ـ","")]:
        t_ = t_.replace(a, b)
    return re.sub(r"\s+", " ", _AR_PUNCT.sub(" ", t_.lower())).strip()

def _tokenize(text: str) -> List[str]:
    return [w for w in _ar_normalize(text).split() if len(w) >= 2 and w not in _AR_STOPWORDS]

def _score_kb_item(q_tokens: List[str], item: Dict[str, Any]) -> int:
    if not q_tokens: return 1
    tags = _ar_normalize(" ".join(item.get("tags", [])))
    tip  = _ar_normalize(item.get("tip", ""))
    both = tags + " " + tip
    score = sum(6 if tok in tags else (4 if tok in tip else 0) for tok in q_tokens)
    if all(tok in both for tok in q_tokens[:3]): score += 6
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
        if topic and item["topic"] != topic: continue
        if age is not None and not (item["age_min"] <= age <= item["age_max"]): continue
        s = _score_kb_item(tokens, item)
        if tokens and s > 0: scored.append((s, item))
        elif not tokens:      scored.append((s, item))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        top     = [i for _, i in scored[:3]]
        matched = scored[0][0] >= 6 if tokens else True
        return KbSearchResult(tips=top, matched=matched, match_count=len(scored), used_default=not bool(tokens))
    defaults = [x for x in KB if x["topic"] == topic][:3]
    return KbSearchResult(tips=defaults, matched=False, match_count=0, used_default=True)

# ──────────────────────────────────────────────
# SPECIALISTS & SLOTS
# ──────────────────────────────────────────────
def recommend_specialists(topic: str) -> List[Dict[str, Any]]:
    rec = sorted(
        [s for s in SPECIALISTS if topic in s["topics"]],
        key=lambda x: (-x["rating"], x["price_egp"])
    )
    return rec[:3] or SPECIALISTS[:2]

def available_slots(specialist_id: str) -> List[Dict[str, Any]]:
    return [sl for sl in SLOTS if sl["specialist_id"] == specialist_id and sl["available"]][:3]

def sync_slots_with_booked(conn) -> None:
    cur = conn.cursor()
    cur.execute("SELECT slot_id FROM appointments WHERE status != 'cancelled'")
    booked = {r[0] for r in cur.fetchall()}
    for sl in SLOTS:
        if sl["slot_id"] in booked:
            sl["available"] = False

def book_slot(conn, user_id: str, specialist_id: str, slot_id: str) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM appointments WHERE slot_id=%s AND status != 'cancelled'",
        (slot_id,)
    )
    if cur.fetchone()[0] > 0:
        raise ValueError("Slot not available")
    slot = next((s for s in SLOTS if s["slot_id"] == slot_id and s["specialist_id"] == specialist_id), None)
    if not slot or not slot["available"]:
        raise ValueError("Slot not available")
    appt_id = "ap_" + uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO appointments (appointment_id, user_id, specialist_id, slot_id, status) VALUES (%s,%s,%s,%s,'pending')",
        (appt_id, user_id, specialist_id, slot_id)
    )
    conn.commit()
    slot["available"] = False
    return {"appointment_id": appt_id, "user_id": user_id,
            "specialist_id": specialist_id, "slot_id": slot_id,
            "status": "pending", "created_at": datetime.utcnow().isoformat() + "Z"}

# ──────────────────────────────────────────────
# USER / MEMORY
# ──────────────────────────────────────────────
def ensure_user_exists(conn, user_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, notes) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, json.dumps([]))
    )
    conn.commit()

def get_memory(conn, user_id: str) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        "SELECT notes, child_age, name, email, preferred_language FROM users WHERE user_id=%s",
        (user_id,)
    )
    row = cur.fetchone()
    if not row:
        return {"child_age": None, "name": None, "email": None, "notes": [],
                "last_summary": "", "preferred_language": "ar"}
    raw = row[0]
    notes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return {"child_age": row[1], "name": row[2], "email": row[3], "notes": notes,
            "last_summary": "", "preferred_language": row[4] or "ar"}

def update_memory(conn, user_id: str, topic: str,
                  child_age: Optional[int], note: str = "") -> None:
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
        (json.dumps(notes), child_age, user_id)
    )
    conn.commit()

# ──────────────────────────────────────────────
# ANALYTICS
# ──────────────────────────────────────────────
def log_event(conn, user_id: str, event_type: str, value: str = "") -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analytics (event_id, user_id, event_type, value) VALUES (%s,%s,%s,%s)",
        ("ev_" + uuid.uuid4().hex[:10], user_id, event_type, value[:300])
    )
    conn.commit()

# ──────────────────────────────────────────────
# ASSESSMENT ENGINE — v4 (bug-fixed)
# ──────────────────────────────────────────────
ASSESSMENT_OPTIONS = ["Never", "Rarely", "Sometimes", "Often", "Always"]

ASSESSMENT_QUESTIONS: List[Dict[str, Any]] = [
    # FOCUS
    {"id": "q01","trait":"focus",       "age_min":4, "age_max":18,"weights":{"focus":2},
     "text":"My child stays focused on a task until it is completed."},
    {"id": "q02","trait":"focus",       "age_min":7, "age_max":18,"weights":{"focus":2,"self_control":1},
     "text":"My child finishes homework or assignments before switching to play."},
    {"id": "q03","trait":"focus",       "age_min":4, "age_max":18,"weights":{"focus":3},
     "text":"My child can sit quietly and concentrate during story time or a lesson."},
    # EMPATHY
    {"id": "q04","trait":"empathy",     "age_min":4, "age_max":18,"weights":{"empathy":2},
     "text":"My child notices when a friend or sibling is upset and tries to comfort them."},
    {"id": "q05","trait":"empathy",     "age_min":6, "age_max":18,"weights":{"empathy":2,"sociability":1},
     "text":"My child apologizes genuinely after hurting someone's feelings."},
    {"id": "q06","trait":"empathy",     "age_min":4, "age_max":18,"weights":{"empathy":3},
     "text":"My child shows concern for animals or people who are struggling."},
    # CURIOSITY
    {"id": "q07","trait":"curiosity",   "age_min":4, "age_max":18,"weights":{"curiosity":2},
     "text":"My child frequently asks 'why' or 'how' questions about the world."},
    {"id": "q08","trait":"curiosity",   "age_min":6, "age_max":18,"weights":{"curiosity":2,"adaptability":1},
     "text":"My child enjoys trying new activities or experimenting with new ideas."},
    {"id": "q09","trait":"curiosity",   "age_min":4, "age_max":18,"weights":{"curiosity":3},
     "text":"My child enjoys solving puzzles, riddles, or figuring things out independently."},
    # LEADERSHIP
    {"id": "q10","trait":"leadership",  "age_min":5, "age_max":18,"weights":{"leadership":2},
     "text":"My child naturally takes charge and organizes activities when playing with others."},
    {"id": "q11","trait":"leadership",  "age_min":8, "age_max":18,"weights":{"leadership":2,"focus":1},
     "text":"My child steps up to help make decisions in group settings."},
    {"id": "q12","trait":"leadership",  "age_min":5, "age_max":18,"weights":{"leadership":3},
     "text":"My child is comfortable taking responsibility for a task or group project."},
    # SOCIABILITY
    {"id": "q13","trait":"sociability", "age_min":4, "age_max":18,"weights":{"sociability":2},
     "text":"My child makes friends quickly and easily in new environments."},
    {"id": "q14","trait":"sociability", "age_min":4, "age_max":18,"weights":{"sociability":2,"empathy":1},
     "text":"My child enjoys being around others and actively seeks social interaction."},
    {"id": "q15","trait":"sociability", "age_min":4, "age_max":18,"weights":{"sociability":3},
     "text":"My child is comfortable sharing, taking turns, and cooperating in group play."},
    # ADAPTABILITY
    {"id": "q16","trait":"adaptability","age_min":4, "age_max":18,"weights":{"adaptability":2},
     "text":"My child adjusts well to changes in routine (new school, travel, schedule changes)."},
    {"id": "q17","trait":"adaptability","age_min":6, "age_max":18,"weights":{"adaptability":2,"self_control":1},
     "text":"When plans change unexpectedly, my child handles it calmly."},
    # SELF CONTROL
    {"id": "q18","trait":"self_control","age_min":4, "age_max":18,"weights":{"self_control":2},
     "text":"My child can calm themselves down after getting upset without adult intervention."},
    {"id": "q19","trait":"self_control","age_min":6, "age_max":18,"weights":{"self_control":3},
     "text":"My child resists the urge to act impulsively (e.g., waits their turn, thinks before acting)."},
    # SENSITIVITY
    {"id": "q20","trait":"sensitivity", "age_min":4, "age_max":18,"weights":{"sensitivity":2},
     "text":"My child gets upset easily by criticism, loud noises, or unexpected changes."},
    {"id": "q21","trait":"sensitivity", "age_min":4, "age_max":18,"weights":{"sensitivity":3},
     "text":"My child feels emotions deeply and needs extra reassurance after conflict or disappointment."},
]

# Normalised lookup: "q01" / "Q01" / " Q01 " all map to the same entry
_QS_NORM: Dict[str, Dict[str, Any]] = {
    q["id"].strip().lower(): q for q in ASSESSMENT_QUESTIONS
}

ARCHETYPES: List[Dict[str, Any]] = [
    {"id":"leader",      "name":"The Leader",
     "description":"Takes initiative, organizes peers, and thrives when given responsibility.",
     "needs":"Clear boundaries, meaningful responsibilities, and leadership opportunities.",
     "profile":{"leadership":80,"focus":60,"sociability":55},"traits_focus":["leadership","focus"]},
    {"id":"explorer",    "name":"The Explorer",
     "description":"Curious, adventurous, and constantly seeking new experiences and knowledge.",
     "needs":"New challenges, hands-on projects, and freedom to experiment.",
     "profile":{"curiosity":80,"adaptability":65},"traits_focus":["curiosity","adaptability"]},
    {"id":"thinker",     "name":"The Thinker",
     "description":"Reflective and analytical — prefers depth over breadth.",
     "needs":"Quiet time, intellectual challenges, and space for independent thought.",
     "profile":{"focus":80,"curiosity":65,"sociability":30},"traits_focus":["focus","curiosity"]},
    {"id":"helper",      "name":"The Helper",
     "description":"Warm, caring, and highly attuned to the emotions of others.",
     "needs":"Recognition of emotional contributions and opportunities to support peers.",
     "profile":{"empathy":85,"sociability":60},"traits_focus":["empathy","sociability"]},
    {"id":"peacemaker",  "name":"The Peacemaker",
     "description":"Conflict-averse, diplomatic, and focused on harmony in relationships.",
     "needs":"Teaching assertiveness, safe expression of opinions, and conflict resolution skills.",
     "profile":{"empathy":75,"self_control":70},"traits_focus":["empathy","self_control"]},
    {"id":"energetic",   "name":"The Energetic",
     "description":"High energy, enthusiastic, and socially motivated.",
     "needs":"Physical outlets, structured energy release, and consistent boundaries.",
     "profile":{"sociability":75,"curiosity":60,"self_control":35},"traits_focus":["sociability","self_control"]},
    {"id":"sensitive",   "name":"The Sensitive",
     "description":"Deeply empathetic and emotionally aware — feels things intensely.",
     "needs":"Emotional validation, predictable routines, and a calm safe environment.",
     "profile":{"sensitivity":85,"empathy":65},"traits_focus":["sensitivity","empathy"]},
    {"id":"independent", "name":"The Independent",
     "description":"Values autonomy and personal space — prefers doing things on their own terms.",
     "needs":"Structured choices, respected boundaries, and gradual responsibility.",
     "profile":{"leadership":55,"sociability":25,"focus":60},"traits_focus":["leadership","focus"]},
    {"id":"planner",     "name":"The Planner",
     "description":"Orderly, methodical, and motivated by structure, routine, and clear goals.",
     "needs":"Simple schedules, clear expectations, and positive reinforcement for progress.",
     "profile":{"focus":85,"self_control":75},"traits_focus":["focus","self_control"]},
    {"id":"challenger",  "name":"The Challenger",
     "description":"Questions authority, tests limits, and learns best through debate and negotiation.",
     "needs":"Few but firm rules, negotiation space, and consistent logical consequences.",
     "profile":{"leadership":65,"self_control":30,"sensitivity":50},"traits_focus":["leadership","self_control"]},
]


def _normalize_answer_id(raw_id: Any) -> str:
    """Normalize question IDs: strip, lowercase → 'q01'"""
    return str(raw_id or "").strip().lower()


def _extract_answer_value(answer: Dict[str, Any]) -> Optional[int]:
    """
    Accept both 'value' (legacy) and 'score' (Flutter) fields.
    Returns int 1-5 or None if invalid.
    """
    raw = answer.get("value") if answer.get("value") is not None else answer.get("score")
    try:
        v = int(raw)
        return v if 1 <= v <= 5 else None
    except (TypeError, ValueError):
        return None


def get_assessment_questions(child_age: Optional[int]) -> List[Dict[str, Any]]:
    """Return all questions; age filter is advisory only (None → all)."""
    if child_age is None:
        return ASSESSMENT_QUESTIONS
    return [q for q in ASSESSMENT_QUESTIONS if q["age_min"] <= child_age <= q["age_max"]]


def _format_questions_for_api(questions: List[Dict]) -> List[Dict]:
    return [
        {"id": q["id"], "text": q["text"], "trait": q["trait"], "options": ASSESSMENT_OPTIONS}
        for q in questions
    ]


def compute_personality_profile(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw: Dict[str, float] = {tr: 0.0 for tr in ALL_TRAITS}
    max_: Dict[str, float] = {tr: 0.0 for tr in ALL_TRAITS}

    matched_ids: List[str] = []
    unmatched_ids: List[str] = []

    for a in answers:
        qid_raw = a.get("question_id") or a.get("id")
        qid     = _normalize_answer_id(qid_raw)
        val     = _extract_answer_value(a)

        if DEBUG:
            print(f"[DEBUG] answer qid_raw={qid_raw!r} → normalized={qid!r} | value={val}")

        q = _QS_NORM.get(qid)
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

    if DEBUG:
        print(f"[DEBUG] matched={matched_ids}")
        print(f"[DEBUG] unmatched={unmatched_ids}")
        print(f"[DEBUG] raw scores={raw}")

    # Behavior signal bonuses
    bs = behavior_signals or {}
    raw["focus"]   += max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2;  max_["focus"]   += 6
    raw["empathy"] += int(bs.get("helps_others", 0)) * 2;               max_["empathy"] += 4

    def _norm(r: float, m: float) -> int:
        return max(0, min(100, int(round(r / m * 100)))) if m > 0 else 0

    scores = {tr: _norm(raw[tr], max_[tr]) for tr in ALL_TRAITS}

    def _sim(arch_profile: Dict[str, int]) -> float:
        return sum(100 - abs(scores.get(tr, 50) - v) for tr, v in arch_profile.items()) / max(1, len(arch_profile))

    ranked = sorted(
        [{"id": a["id"], "name": a["name"], "description": a["description"],
          "needs": a["needs"], "match_pct": int(round(_sim(a["profile"])))}
         for a in ARCHETYPES],
        key=lambda x: x["match_pct"], reverse=True
    )

    top_archetype = ranked[0]
    top_traits    = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    low_traits    = sorted(scores.items(), key=lambda kv: kv[1])[:2]
    recommendations = _build_recommendations(scores, top_archetype, low_traits)

    return {
        "child_age": child_age,
        "trait_scores": scores,
        "top_traits":  [{"trait": tr, "score": v} for tr, v in top_traits],
        "low_traits":  [{"trait": tr, "score": v} for tr, v in low_traits],
        "possible_personalities": ranked[:5],
        "recommendations": recommendations,
        "note": t("assessment_note", "en"),
        "_debug": {"matched": matched_ids, "unmatched": unmatched_ids},
    }


def _build_recommendations(
    scores: Dict[str, int],
    top_arch: Dict[str, Any],
    low_traits: List[Tuple[str, int]],
) -> List[str]:
    recs: List[str] = []
    recs.append(f"Your child most resembles '{top_arch['name']}' — {top_arch['description']}")
    recs.append(f"What they need most: {top_arch['needs']}")
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
            recs.append(f"Low {trait.replace('_',' ').title()} ({score}%): {advice}")
    return recs


def compute_assessment_confidence(
    answers: List[Dict[str, Any]],
    child_age: Optional[int],
    behavior_signals: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Bug-fixed version:
    - uses _normalize_answer_id so 'q01'/'Q01' both match
    - accepts 'score' OR 'value' field
    - does NOT filter by child_age (age filter is advisory only)
    """
    all_qs  = ASSESSMENT_QUESTIONS           # full bank (no age filter here)
    q_ids   = {q["id"].strip().lower() for q in all_qs}
    total   = len(all_qs)
    valid   = 0
    matched_dbg: List[str] = []
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

    if DEBUG:
        print(f"[CONFIDENCE] valid={valid}/{total}, matched={matched_dbg}, unmatched={unmatched_dbg}")

    coverage = int(round(valid / total * 100)) if total else 0
    score    = int(round(valid / total * 65))  if total else 0
    notes    = [f"coverage={coverage}%"]
    if child_age is not None: score += 15; notes.append("age_provided")
    if behavior_signals:       score += 10; notes.append("behavior_signals_included")
    if valid < max(3, total // 3 if total else 3):
        score = max(0, score - 15); notes.append("low_answer_count_penalty")

    return {
        "confidence":       max(0, min(100, score)),
        "valid_answers":    valid,
        "total_questions":  total,
        "coverage":         coverage,
        "notes":            notes,
        "debug": {
            "received_count":    len(answers or []),
            "matched_questions": matched_dbg,
            "unmatched_questions": unmatched_dbg,
        },
    }


def recommend_specialist_for_profile(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    low_traits  = [item["trait"] for item in profile.get("low_traits", [])]
    top_arch_id = profile["possible_personalities"][0]["id"] if profile.get("possible_personalities") else ""
    archetype   = next((a for a in ARCHETYPES if a["id"] == top_arch_id), None)
    focus_traits = archetype["traits_focus"] if archetype else low_traits

    best_sp, best_score = None, -1
    for sp in SPECIALISTS:
        score = sum(1 for tr in focus_traits if tr in sp["traits_focus"])
        score += sum(1 for tr in low_traits   if tr in sp["traits_focus"])
        if score > best_score:
            best_score, best_sp = score, sp

    if not best_sp:
        return None

    reasons = []
    if low_traits:
        reasons.append(f"Low {', '.join(low_traits)} detected in your child's profile.")
    reasons.append(f"{best_sp['name']} specializes in {', '.join(best_sp['traits_focus'])} development.")
    return {
        "id":        best_sp["id"],
        "name":      best_sp["name"],
        "title":     best_sp["title"],
        "reason":    " ".join(reasons),
        "price_egp": best_sp["price_egp"],
        "rating":    best_sp["rating"],
    }

# ──────────────────────────────────────────────
# FOLLOW-UP QUESTIONS
# ──────────────────────────────────────────────
FOLLOW_UP_BANK: Dict[str, List[str]] = {
    "anger":               ["When does the anger peak most? (before bed / after school / screen time)","What usually happens in the 60 seconds before the outburst?"],
    "screen_addiction":    ["How many hours per day approximately? What mostly (YouTube/games/TikTok)?","Is there a specific time cutting off causes the biggest reaction?"],
    "teen_communication":  ["When is your teen most calm and open to talking?","Is it that they don't respond at all, or respond with anger?"],
    "bullying":            ["Where does the bullying happen most? (classroom/bus/club)","Is there any adult at school your child already trusts?"],
    "study_focus":         ["How many minutes can they focus before getting distracted?","Which subject creates the most resistance?"],
    "kids_stories":        ["How old is the child so I can pick the right story?","What theme do you prefer — honesty, sharing, courage, or respect?"],
    "activities_games":    ["Do you prefer a calm activity or something active and physical?","Do you have simple supplies like paper, pencils, or building blocks?"],
    "book_recommendations":["How old is your child and what kind of stories do they enjoy?","Values-based books or adventure stories?"],
    "assessment_personality":["Would you like to start a quick personality assessment?","How old is your child so I can tailor the questions?"],
    "general_parenting":   ["How old is your child?","When does the situation occur most often and what usually triggers it?"],
}

def pick_followups(topic: str) -> List[str]:
    return (FOLLOW_UP_BANK.get(topic) or ["Can you share a recent situation?", "How old is your child?"])[:2]

# ──────────────────────────────────────────────
# CONFIDENCE SCORING
# ──────────────────────────────────────────────
def compute_confidence(topic, kb_res, age, user_text, in_scope, risk_level) -> int:
    score = 40
    if in_scope and topic != "out_of_scope": score += 15
    if age is not None:                       score += 10
    if kb_res.matched:                        score += 25 + min(10, kb_res.match_count * 3)
    elif kb_res.used_default and topic in (KIDS_CONTENT_TOPICS | {ASSESSMENT_TOPIC}): score += 15
    else:                                     score -= 10
    if len((user_text or "").split()) >= 10:  score += 5
    if risk_level == "medium":                score -= 10
    elif risk_level == "high":                score -= 25
    return max(0, min(100, score))

# ──────────────────────────────────────────────
# EMPATHY REFLECT
# ──────────────────────────────────────────────
_EMPATHY_AR: Dict[str, str] = {
    "anger":               "واضح إن الموضوع ده متعبك وبيستنزف أعصابك.",
    "screen_addiction":    "حاسّة بقلقك من موضوع الشاشات وتأثيره عليه.",
    "teen_communication":  "واضح إن قلة التواصل مضايقاكي وبتوجع.",
    "bullying":            "طبيعي تقلقي جدًا لما تحسي إن ابنك بيتأذى.",
    "study_focus":         "الإحساس بالحيرة مع المذاكرة بيكون مرهق فعلًا.",
    "general_parenting":   "الأمومة مليانة مواقف بتخلينا نحتار.",
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
        if risk_level == "medium": empathy += " خلّينا نمشي بهدوء ونفهم الصورة كاملة."
        snippet = (user_text[:77] + "...") if len(user_text) > 80 else user_text
        return f"{empathy}\n\nإنتِ بتقولي: «{snippet}»\n"
    else:
        empathy = _EMPATHY_EN.get(topic, "I hear you — this situation sounds genuinely challenging.")
        if risk_level == "medium": empathy += " Let's go through this carefully together."
        snippet = (user_text[:77] + "...") if len(user_text) > 80 else user_text
        return f"{empathy}\n\nYou said: \"{snippet}\"\n"

# ──────────────────────────────────────────────
# GEMINI HELPERS
# ──────────────────────────────────────────────
def _require_gemini() -> None:
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini disabled: set GEMINI_API_KEY")

def _lang_instruction(lang: Lang) -> str:
    if lang == "ar":
        return "Reply in warm, clear Modern Standard Arabic (Egyptian dialect warmth is welcome)."
    return "Reply in clear, warm, professional English."

def gemini_route_decision(user_text, history, fallback_age) -> RouteDecision:
    _require_gemini()
    system = (
        "You are the router for Rafiq, a family support assistant. "
        "Rafiq only handles: family communication, parenting, teen issues, anger, screen addiction, "
        "bullying, study focus, sibling jealousy, parent conflict, lying, kids stories, educational games, "
        "book recommendations for children, and child personality assessment.\n"
        "Forbidden: programming/tech, medical diagnosis, medications.\n"
        "If out of scope → action=refuse_out_of_scope, in_scope=false.\n"
        "If user writes 'book sl_001' or 'احجز sl_001' → extract slot_id.\n"
        "Output ONLY valid JSON matching the schema."
    )
    history_str = "\n".join(f"{m.role}: {m.content}" for m in history[-6:])
    prompt = (
        f"System: {system}\n\nConversation:\n{history_str}\n\n"
        f"User message:\n{user_text}\n\nKnown child age: {fallback_age}"
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RouteDecision,
            temperature=0,
            safety_settings=[
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,         threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,        threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
                genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,  threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE),
            ],
        ),
    )
    try:
        return RouteDecision.model_validate_json(resp.text)
    except Exception:
        return RouteDecision(in_scope=False, topic="out_of_scope", action="refuse_out_of_scope",
                             reason=f"Router parse failed. raw={resp.text[:100]}")

def gemini_compose_answer(user_text, topic, tips, specialists, slots,
                           memory, followups, confidence, risk_level, lang: Lang) -> str:
    _require_gemini()
    payload = {"topic": topic, "tips": tips, "specialists": specialists, "slots": slots,
               "memory": memory, "followups": followups, "confidence": confidence, "risk_level": risk_level}
    system = (
        f"You are Rafiq, a supportive family assistant. {_lang_instruction(lang)}\n"
        "Rules: NO diagnosis, NO medication advice, NO programming.\n"
        "Use ONLY the data provided in ALLOWED DATA.\n"
        "If confidence < 65 or tips empty: give a short empathetic reply + ONE follow-up question.\n"
        "If confidence >= 65: give 2-3 practical bullet points + ONE follow-up + suggest booking if relevant.\n"
        "Max 350 words."
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{system}\n\nUSER:\n{user_text}\n\nALLOWED DATA:\n{json.dumps(payload, ensure_ascii=False)}",
        config=genai_types.GenerateContentConfig(temperature=0.4, max_output_tokens=500),
    )
    fallback = "ممكن تقوليلي تفاصيل أكتر؟" if lang == "ar" else "Could you share more details?"
    return (resp.text or "").strip() or fallback

def gemini_verify_answer(user_text, answer, allowed_payload) -> Dict[str, Any]:
    _require_gemini()
    prompt = (
        f"Check if this reply violates Rafiq rules (no diagnosis/meds/programming).\n"
        f"Output ONLY JSON: {{\"ok\": true/false, \"reason\": \"brief\"}}\n\n"
        f"USER:\n{user_text}\n\nANSWER:\n{answer}\n\nALLOWED:\n{json.dumps(allowed_payload, ensure_ascii=False)}"
    )
    r = client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(response_mime_type="application/json",
                                                  temperature=0, max_output_tokens=150)
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
    """Generate a 30-day parenting plan in the user's language."""
    _require_gemini()

    if lang == "ar":
        prompt = (
            "أنت مدرب تربوي محترف متخصص في التطوير الشخصي للأطفال.\n\n"
            "فيما يلي نتائج تقييم شخصية الطفل:\n"
            f"- عمر الطفل: {child_age if child_age is not None else 'غير محدد'} سنة\n"
            f"- النمط الشخصي الأبرز: {top_archetype} — {archetype_desc}\n"
            f"- احتياجات الطفل: {archetype_needs}\n\n"
            f"أبرز الصفات:\n{traits_text}\n\n"
            f"درجات جميع الصفات:\n{scores_text}\n\n"
            "المطلوب:\n"
            "أنشئ خطة تربوية مخصصة لمدة 30 يومًا (4 أسابيع).\n"
            "يجب أن تتضمن الخطة:\n"
            "1. هدف الأسبوع\n"
            "2. أنشطة يومية عملية ومناسبة لعمر الطفل\n"
            "3. أساليب التعزيز الإيجابي\n"
            "4. توصيات خاصة بالوالدين\n"
            "5. ملاحظة ختامية للمتابعة\n\n"
            "الأسلوب: دافئ، واضح، وعملي. تجنب المصطلحات الطبية.\n"
            "أعد الخطة كاملةً باللغة العربية."
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
            "Task:\n"
            "Create a personalised 30-day parenting plan (4 weeks) based on this data.\n"
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
        model=GEMINI_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.6, max_output_tokens=2000),
    )
    return (resp.text or "").strip()

# ──────────────────────────────────────────────
# PDF HELPERS
# ──────────────────────────────────────────────
def _safe_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _shape_arabic(text: str) -> str:
    """Reshape + apply bidi for Arabic strings in ReportLab."""
    if not _ARABIC_SHAPING:
        return text
    reshaped = arabic_reshaper.reshape(text)
    return bidi_display(reshaped)

def _pdf_text(text: str, lang: Lang) -> str:
    """Prepare text for PDF: shape Arabic if needed."""
    if lang == "ar":
        return _shape_arabic(text)
    return text

def _pick_font(bold: bool, lang: Lang) -> str:
    if lang == "ar" and _FONT_ARABIC_REGISTERED:
        return "NotoArabicBold" if bold else "NotoArabic"
    if lang == "en" and _FONT_LATIN_REGISTERED:
        return "NotoLatin"
    return "Helvetica-Bold" if bold else "Helvetica"

def _build_parenting_plan_pdf(
    user_id: str,
    child_age: Optional[int],
    top_archetype: str,
    plan_text: str,
    generated_at: str,
    lang: Lang = "ar",
) -> bytes:
    buf = io.BytesIO()
    W, H = A4
    styles = getSampleStyleSheet()

    text_align = TA_RIGHT if lang == "ar" else TA_LEFT
    brand_green = colors.HexColor("#1B6B3A")
    brand_light = colors.HexColor("#E8F5E9")
    text_dark   = colors.HexColor("#1A1A1A")
    text_muted  = colors.HexColor("#555555")
    accent_gold = colors.HexColor("#C8860A")

    font_body  = _pick_font(False, lang)
    font_bold  = _pick_font(True,  lang)

    style_subtitle = ParagraphStyle("SubTitle", parent=styles["Normal"],
        fontSize=12, textColor=text_muted, spaceAfter=2, alignment=TA_CENTER, fontName=font_body)
    style_section_heading = ParagraphStyle("SectionHeading", parent=styles["Heading1"],
        fontSize=13, textColor=brand_green, spaceBefore=14, spaceAfter=4, fontName=font_bold)
    style_plan_heading = ParagraphStyle("PlanHeading", parent=styles["Heading2"],
        fontSize=12, textColor=accent_gold, spaceBefore=10, spaceAfter=3, fontName=font_bold)
    style_plan_body = ParagraphStyle("PlanBody", parent=styles["Normal"],
        fontSize=10.5, textColor=text_dark, fontName=font_body, leading=17, spaceAfter=4,
        alignment=text_align)
    style_bullet = ParagraphStyle("Bullet", parent=styles["Normal"],
        fontSize=10.5, textColor=text_dark, fontName=font_body, leading=17,
        leftIndent=16, spaceAfter=3, bulletIndent=4, alignment=text_align)
    style_footer = ParagraphStyle("Footer", parent=styles["Normal"],
        fontSize=8, textColor=text_muted, alignment=TA_CENTER, fontName=font_body)

    doc = SimpleDocTemplate(buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm, topMargin=2.5*cm, bottomMargin=2*cm,
        title=f"Rafiq Parenting Plan — {user_id}", author="Rafiq AI")

    story = []

    # Banner
    banner_title = _pdf_text(t("pdf_main_title", lang), lang)
    banner_sub   = _pdf_text(t("pdf_subtitle",   lang), lang)
    banner_style = ParagraphStyle("BannerTitle", parent=styles["Title"],
        fontSize=20, textColor=colors.white, alignment=TA_CENTER, fontName=font_bold)
    banner_table = Table(
        [[Paragraph(banner_title, banner_style)]],
        colWidths=[W - 4*cm],
    )
    banner_table.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), brand_green),
        ("TOPPADDING",   (0,0),(-1,-1), 14),
        ("BOTTOMPADDING",(0,0),(-1,-1), 14),
        ("LEFTPADDING",  (0,0),(-1,-1), 16),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(banner_sub, style_subtitle))
    story.append(Spacer(1, 0.25*cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=brand_green, spaceAfter=10))

    # Meta table
    age_display  = _pdf_text(
        f"{child_age} {'سنة' if lang=='ar' else 'years'}" if child_age
        else t("pdf_label_age_unknown", lang), lang)
    date_display = generated_at[:10] if generated_at else "—"

    lbl = lambda k: _pdf_text(t(k, lang), lang)
    lbl_style = ParagraphStyle("MetaLbl", parent=styles["Normal"],
        fontSize=9, textColor=brand_green, fontName=font_bold)
    val_style = ParagraphStyle("MetaVal", parent=styles["Normal"],
        fontSize=9, textColor=text_dark,  fontName=font_body)

    meta_data = [
        [Paragraph(lbl("pdf_label_user_id"),   lbl_style), Paragraph(user_id,      val_style),
         Paragraph(lbl("pdf_label_child_age"), lbl_style), Paragraph(age_display,  val_style)],
        [Paragraph(lbl("pdf_label_archetype"), lbl_style), Paragraph(_pdf_text(top_archetype, lang), val_style),
         Paragraph(lbl("pdf_label_generated"), lbl_style), Paragraph(date_display, val_style)],
    ]
    cw = (W-4*cm) / 4
    meta_table = Table(meta_data, colWidths=[cw*0.22, cw*0.78*0.6, cw*0.22, cw*0.78*0.6])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND",     (0,0),(-1,-1), brand_light),
        ("BACKGROUND",     (0,0),(0,-1),  colors.HexColor("#D0EAD8")),
        ("BACKGROUND",     (2,0),(2,-1),  colors.HexColor("#D0EAD8")),
        ("TOPPADDING",     (0,0),(-1,-1), 7),
        ("BOTTOMPADDING",  (0,0),(-1,-1), 7),
        ("LEFTPADDING",    (0,0),(-1,-1), 8),
        ("GRID",           (0,0),(-1,-1), 0.5, colors.HexColor("#BBDDC7")),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=6))

    # Plan content
    story.append(Paragraph(_pdf_text(t("pdf_section_plan", lang), lang), style_section_heading))
    story.append(Spacer(1, 0.2*cm))

    week_keywords  = ("الأسبوع", "Week ", "أسبوع")
    bullet_markers = ("•", "-", "–", "*", "·")

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

    # Footer
    story.append(Spacer(1, 0.6*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=6))
    story.append(Paragraph(_safe_xml(_pdf_text(t("pdf_footer_line1", lang), lang)), style_footer))

    doc.build(story)
    buf.seek(0)
    return buf.read()

# ──────────────────────────────────────────────
# PROFILE NORMALISATION HELPERS
# ──────────────────────────────────────────────
def _norm_traits(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({"trait": str(item.get("trait") or item.get("name") or ""),
                        "score": int(item.get("score", 0))})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"trait": str(item[0]), "score": int(item[1])})
    return out

def _norm_personalities(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({"id": str(item.get("id","")), "name": str(item.get("name","غير محدد")),
                        "description": str(item.get("description","")),
                        "needs": str(item.get("needs","")),
                        "match_pct": int(item.get("match_pct") or item.get("match") or 0)})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"id": str(item[0]), "name": str(item[0]),
                        "description":"","needs":"","match_pct":int(item[1])})
    return out

def _norm_scores(raw: Any) -> Dict[str, int]:
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}
    out = {}
    for item in (raw or []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out[str(item[0])] = int(item[1])
    return out

# ──────────────────────────────────────────────
# ROUTES — SYSTEM
# ──────────────────────────────────────────────
@app.get("/", tags=["System"])
def home():
    return {"status": "Rafiq running 🚀", "version": "4.0.0"}

@app.get("/health", tags=["System"])
def health():
    return {"ok": True, "model": GEMINI_MODEL, "gemini_enabled": GEMINI_ENABLED,
            "verify": ENABLE_VERIFY, "db": bool(DATABASE_URL), "debug": DEBUG,
            "arabic_shaping": _ARABIC_SHAPING, "pdf": _REPORTLAB_AVAILABLE}

@app.get("/test_gemini", tags=["System"])
def test_gemini():
    _require_gemini()
    r = client.models.generate_content(model=GEMINI_MODEL, contents="Reply with OK only.")
    return {"text": r.text}

# ──────────────────────────────────────────────
# ROUTES — USERS
# ──────────────────────────────────────────────
@app.post("/users", tags=["Users"])
def upsert_user(req: UserUpsertReq):
    conn = get_conn()
    cur  = conn.cursor()
    lang = req.preferred_language if req.preferred_language in ("ar","en") else "ar"
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
            (req.user_id, req.name, req.email, req.child_age, json.dumps([]), lang)
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "ok": True,
            "message": t("ok", lang),
            "user": {
                "user_id":            row[0],
                "name":               row[1],
                "email":              row[2],
                "child_age":          row[3],
                "preferred_language": row[4],
                "created_at":         row[5].isoformat() if row[5] else None,
                "updated_at":         row[6].isoformat() if row[6] else None,
            }
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

# ──────────────────────────────────────────────
# ROUTES — FCM / PUSH
# ──────────────────────────────────────────────
@app.post("/register-token", tags=["Notifications"])
def register_token(req: RegisterTokenReq):
    conn = get_conn()
    lang: Lang = "ar"
    try:
        ensure_user_exists(conn, req.user_id)
        cur = conn.cursor()
        cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
        row = cur.fetchone()
        if row: lang = row[0] or "ar"
        cur.execute(
            "UPDATE users SET fcm_token=%s, updated_at=NOW() WHERE user_id=%s",
            (req.fcm_token, req.user_id)
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
    conn = get_conn()
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
            return {"ok": True, "user_id": req.user_id, "tip_saved": True,
                    "notification_sent": False,
                    "warning": t("firebase_not_configured", lang)}

        try:
            message = fb_messaging.Message(
                notification=fb_messaging.Notification(
                    title=t("daily_tip_notif_title", lang),
                    body=req.tip[:200],
                ),
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
            (user_id, max(1, min(200, limit)))
        )
        rows = cur.fetchall()
        return {"user_id": user_id, "total": len(rows),
                "tips": [{"id": r[0], "tip": r[1],
                          "created_at": r[2].isoformat() if r[2] else None} for r in rows]}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()

# ──────────────────────────────────────────────
# ROUTES — KB
# ──────────────────────────────────────────────
@app.get("/kb/topics", tags=["KB"])
def kb_topics():
    topics = sorted({x["topic"] for x in KB})
    return {"topics": topics, "count": len(topics)}

@app.get("/kb/search", tags=["KB"])
def kb_search_api(topic: str, q: str = "", age: Optional[int] = None):
    res = kb_search_v2(topic=topic, query=q, age=age)
    return {"topic": topic, "age": age, "matched": res.matched,
            "match_count": res.match_count, "used_default": res.used_default, "tips": res.tips}

@app.post("/kb/add", tags=["KB"])
def kb_add(req: KbAddRequest):
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key")
    new_id = "kb_" + uuid.uuid4().hex[:6]
    KB.append({"id": new_id, "topic": req.topic, "age_min": req.age_min,
               "age_max": req.age_max, "tags": req.tags, "tip": req.tip})
    return {"ok": True, "kb_id": new_id, "total": len(KB)}

# ──────────────────────────────────────────────
# ROUTES — ASSESSMENT
# ──────────────────────────────────────────────
@app.get("/assessment/questions", tags=["Assessment"])
def assessment_questions(age: Optional[int] = None):
    qs = get_assessment_questions(age)
    return {
        "child_age": age,
        "total_questions": len(qs),
        "scale": {"min":1,"max":5,"labels":{"1":"Never","2":"Rarely","3":"Sometimes","4":"Often","5":"Always"}},
        "questions": _format_questions_for_api(qs),
    }

@app.post("/assessment/submit", tags=["Assessment"])
def assessment_submit(req: AssessmentSubmitReq):
    conn = get_conn()
    lang: Lang = req.preferred_language if req.preferred_language in ("ar","en") else "ar"  # type: ignore[assignment]
    try:
        ensure_user_exists(conn, req.user_id)

        # Fetch stored lang preference if not provided
        if req.preferred_language is None:
            cur = conn.cursor()
            cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (req.user_id,))
            row = cur.fetchone()
            if row and row[0]: lang = row[0]

        if DEBUG:
            print(f"[ASSESSMENT] user={req.user_id}, child_age={req.child_age}, answers_count={len(req.answers)}")
            for a in req.answers:
                print(f"  answer: {a}")

        profile     = compute_personality_profile(req.answers, req.child_age, req.behavior_signals)
        assess_conf = compute_assessment_confidence(req.answers, req.child_age, req.behavior_signals)
        recommended = recommend_specialist_for_profile(profile)

        # Remove internal debug key before storing
        profile_to_store = {k: v for k, v in profile.items() if k != "_debug"}

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO assessments (user_id, child_age, assessment_confidence, result, created_at) VALUES (%s,%s,%s,%s,NOW())",
            (req.user_id, req.child_age, assess_conf["confidence"], json.dumps(profile_to_store))
        )
        conn.commit()
        update_memory(conn, req.user_id, "assessment_personality", req.child_age, note="Assessment submitted")
        log_event(conn, req.user_id, "assessment_submit", value=f"confidence={assess_conf['confidence']}")

        return {
            "ok":                     True,
            "message":                t("ok", lang),
            "trait_scores":           profile["trait_scores"],
            "top_traits":             profile["top_traits"],
            "low_traits":             profile["low_traits"],
            "possible_personalities": profile["possible_personalities"],
            "recommendations":        profile["recommendations"],
            "confidence":             assess_conf["confidence"],
            "assessment_meta":        assess_conf,
            "recommended_specialist": recommended,
            "note":                   t("assessment_note", lang),
            "debug":                  profile.get("_debug", {}),
        }
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
        "SELECT id, child_age, assessment_confidence, result, created_at FROM assessments WHERE user_id=%s ORDER BY created_at DESC",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "assessments": [
            {"id": r[0], "child_age": r[1], "confidence": float(r[2]), "result": r[3],
             "created_at": r[4].isoformat() if r[4] else None}
            for r in rows
        ]
    }

# ──────────────────────────────────────────────
# ROUTES — SPECIALISTS & SLOTS
# ──────────────────────────────────────────────
@app.get("/specialists", tags=["Specialists"])
def specialists_list():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT id, name, title, topics, price_egp, rating FROM specialists ORDER BY rating DESC")
    rows = cur.fetchall()
    conn.close()
    if rows:
        return {"specialists": [{"id": r[0], "name": r[1], "title": r[2], "topics": r[3],
                                  "price_egp": float(r[4]), "rating": float(r[5])} for r in rows]}
    return {"specialists": sorted(SPECIALISTS, key=lambda x: -x["rating"])}

@app.get("/slots/{specialist_id}", tags=["Specialists"])
def get_slots(specialist_id: str):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT slot_id, start_time, duration_min, available FROM slots WHERE specialist_id=%s ORDER BY start_time",
        (specialist_id,)
    )
    rows = cur.fetchall()
    conn.close()
    if rows:
        return {"slots": [{"slot_id": r[0], "start_time": r[1], "duration_min": r[2], "available": r[3]} for r in rows]}
    return {"slots": [s for s in SLOTS if s["specialist_id"] == specialist_id]}

# ──────────────────────────────────────────────
# ROUTES — APPOINTMENTS
# ──────────────────────────────────────────────
@app.post("/appointments/book", tags=["Appointments"])
def book(req: BookingReq):
    conn = get_conn()
    try:
        ensure_user_exists(conn, req.user_id)
        sync_slots_with_booked(conn)
        appt = book_slot(conn, req.user_id, req.specialist_id, req.slot_id)
        log_event(conn, req.user_id, "booking_created", value=req.slot_id)
        return {"ok": True, "appointment": appt}
    except ValueError:
        raise HTTPException(status_code=400, detail="Slot not available")
    finally:
        conn.close()

@app.get("/appointments/{user_id}", tags=["Appointments"])
def get_appointments(user_id: str, limit: int = 50):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT appointment_id, specialist_id, slot_id, status, created_at FROM appointments WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, max(1, min(200, limit)))
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "appointments": [
            {"appointment_id": r[0], "specialist_id": r[1], "slot_id": r[2],
             "status": r[3], "created_at": r[4].isoformat() if r[4] else None}
            for r in rows
        ]
    }

# ──────────────────────────────────────────────
# ROUTES — ANALYTICS
# ──────────────────────────────────────────────
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
        "SELECT event_id, event_type, value, created_at FROM analytics WHERE user_id=%s ORDER BY created_at DESC LIMIT 100",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "user_id": user_id,
        "recent_events": [{"event_id": r[0], "event_type": r[1], "value": r[2],
                           "created_at": r[3].isoformat() if r[3] else None} for r in rows]
    }

# ──────────────────────────────────────────────
# ROUTES — FEEDBACK
# ──────────────────────────────────────────────
@app.post("/feedback", tags=["Feedback"])
def feedback(req: FeedbackReq):
    conn = get_conn()
    try:
        ensure_user_exists(conn, req.user_id)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO feedback (user_id, message_id, rating, comment, topic, created_at) VALUES (%s,%s,%s,%s,%s,NOW())",
            (req.user_id, req.message_id, req.rating, req.comment, req.topic)
        )
        conn.commit()
        if req.comment:
            update_memory(conn, req.user_id, req.topic or "general_parenting", None,
                          note=f"FEEDBACK:{req.rating}:{req.comment}")
        log_event(conn, req.user_id, "feedback", value=f"{req.rating}:{req.message_id}")
        return {"ok": True}
    finally:
        conn.close()

# ──────────────────────────────────────────────
# ROUTES — CHAT HISTORY
# ──────────────────────────────────────────────
@app.get("/chat/{user_id}", tags=["Chat"])
def get_chat_history(user_id: str, limit: int = 50):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT message_id, message, response, created_at FROM chat_messages WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, max(1, min(200, limit)))
    )
    rows = cur.fetchall()
    conn.close()
    return {
        "messages": [{"message_id": r[0], "user_message": r[1], "bot_reply": r[2],
                      "created_at": r[3].isoformat() if r[3] else None} for r in rows]
    }

# ──────────────────────────────────────────────
# ROUTES — CHAT (main)
# ──────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages list is empty")

    message_id = "msg_" + uuid.uuid4().hex[:10]
    user_text  = req.messages[-1].content.strip()
    lang: Lang = req.preferred_language if req.preferred_language in ("ar","en") else detect_lang(user_text)  # type: ignore[assignment]

    # Hard guards
    if hard_out_of_scope(user_text) or hard_medical(user_text):
        return ChatResponse(
            message_id=message_id,
            reply=t("out_of_scope_reply", lang),
            cards=[{"type":"refusal",
                    "title": t("card_out_of_scope", lang),
                    "body":  t("out_of_scope_card", lang)}]
        )

    if not GEMINI_ENABLED or client is None:
        return ChatResponse(
            message_id=message_id,
            reply=t("gemini_disabled", lang),
            cards=[{"type":"warning","title":"Gemini disabled",
                    "body":"Set GEMINI_API_KEY in environment variables."}]
        )

    conn = get_conn()
    try:
        # Prefer DB-stored language if not passed
        mem_check = get_memory(conn, req.user_id)
        if req.preferred_language is None and mem_check.get("preferred_language"):
            lang = mem_check["preferred_language"]

        slot_from_text = extract_slot_id(user_text)
        wants_booking  = any(x in user_text for x in
                             ["احجز","حجز","استشارة","مختص","دكتور","book","specialist","appointment"])
        risk_level     = detect_risk_level(user_text)

        if risk_level == "high":
            ensure_user_exists(conn, req.user_id)
            log_event(conn, req.user_id, "risk_high", value=user_text[:200])
            return ChatResponse(
                message_id=message_id,
                reply=t("risk_high", lang),
                cards=[{"type":"warning",
                        "title": t("card_important", lang),
                        "body":  t("risk_high_card", lang),
                        "meta": {"risk_level":"high"}}]
            )

        try:
            decision = gemini_route_decision(user_text, req.messages, req.child_age)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Router failed: {exc}")

        if slot_from_text and decision.topic != "out_of_scope":
            decision.action  = "book_appointment"
            decision.slot_id = slot_from_text

        ensure_user_exists(conn, req.user_id)
        log_event(conn, req.user_id, "chat_message", value=user_text[:300])

        if not decision.in_scope or decision.action == "refuse_out_of_scope":
            return ChatResponse(
                message_id=message_id,
                reply=t("scope_refusal", lang),
                cards=[{"type":"refusal",
                        "title": t("card_out_of_scope", lang),
                        "body": t("card_refusal_reason_prefix", lang) + decision.reason}]
            )

        topic = decision.topic

        if topic in KIDS_CONTENT_TOPICS and kids_safety_guard(user_text):
            return ChatResponse(
                message_id=message_id,
                reply=t("kids_safety", lang),
                cards=[{"type":"warning",
                        "title": t("child_appropriate_content", lang),
                        "body":  t("choose_safe_topic", lang)}]
            )

        age = decision.extracted_child_age or req.child_age
        update_memory(conn, req.user_id, topic, age, note=user_text)
        mem = get_memory(conn, req.user_id)

        # Booking flow
        if decision.action == "book_appointment":
            slot_id       = decision.slot_id
            specialist_id = decision.specialist_id
            if slot_id and not specialist_id:
                ms = next((s for s in SLOTS if s["slot_id"] == slot_id), None)
                if ms: specialist_id = ms["specialist_id"]

            if not slot_id or not specialist_id:
                return ChatResponse(
                    message_id=message_id,
                    reply=t("missing_slot", lang),
                    cards=[{"type":"warning",
                            "title": t("card_missing_booking", lang),
                            "body": "Send slot_id like sl_001."}]
                )

            sync_slots_with_booked(conn)
            try:
                appt = book_slot(conn, req.user_id, specialist_id, slot_id)
                sp   = next((x for x in SPECIALISTS if x["id"] == specialist_id), None)
                log_event(conn, req.user_id, "booking_created", value=slot_id)
                return ChatResponse(
                    message_id=message_id,
                    reply=f"{t('booking_success', lang)}{appt['appointment_id']}.",
                    cards=[{"type":"booking",
                            "title": t("booking_details", lang),
                            "body": f"Specialist: {sp['name'] if sp else specialist_id}\nslot_id: {slot_id}",
                            "meta": appt}]
                )
            except ValueError:
                return ChatResponse(
                    message_id=message_id,
                    reply=t("slot_unavailable", lang),
                    cards=[{"type":"warning",
                            "title": t("card_slot_unavailable", lang),
                            "body": "Try a different slot_id."}]
                )

        # Normal answer
        kb_res    = kb_search_v2(topic=topic, query=user_text, age=age)
        tips      = kb_res.tips
        followups = pick_followups(topic)
        conf      = compute_confidence(topic, kb_res, age, user_text, decision.in_scope, risk_level)
        show_sp   = wants_booking or decision.action == "recommend_booking" or risk_level == "medium"
        spec_list: List[Dict] = recommend_specialists(topic) if show_sp else []
        slots_list: List[Dict]= available_slots(spec_list[0]["id"]) if spec_list else []

        if topic in PARENTING_TOPICS and not kb_res.matched and conf < 65:
            q = followups[0] if followups else ("How old is your child?" if lang == "en" else "سن الطفل قد إيه؟")
            return ChatResponse(
                message_id=message_id,
                reply=t("low_conf_prefix", lang) + q + t("low_conf_suffix", lang),
                cards=[
                    {"type":"confidence","title": t("confidence_score", lang),"body":f"{conf}%",
                     "meta":{"confidence":conf,"matched":kb_res.matched}},
                    {"type":"warning","title": t("follow_up", lang),"body":q,
                     "meta":{"followups":followups}},
                ]
            )

        intro = empathy_reflect(user_text, topic, risk_level, lang)
        try:
            final_text = intro + gemini_compose_answer(
                user_text=user_text, topic=topic, tips=tips,
                specialists=spec_list, slots=slots_list, memory=mem,
                followups=followups, confidence=conf, risk_level=risk_level, lang=lang,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Compose failed: {exc}")

        if ENABLE_VERIFY:
            verdict = gemini_verify_answer(user_text, final_text,
                {"topic":topic,"tips":tips,"specialists":spec_list,
                 "slots":slots_list,"memory":mem,"followups":followups,"confidence":conf})
            if not verdict.get("ok", True):
                q = followups[0] if followups else ("How old is your child?" if lang == "en" else "سن الطفل قد إيه؟")
                final_text = t("verify_fallback", lang) + q

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (message_id, user_id, message, response) VALUES (%s,%s,%s,%s)",
            (message_id, req.user_id, user_text, final_text)
        )
        conn.commit()

        # Build cards
        cards: List[Dict] = []
        ctype_map  = {"kids_stories":"story","activities_games":"game",
                      "book_recommendations":"books","assessment_personality":"assessment_question"}
        ctitle_key = {
            "kids_stories":"card_story","activities_games":"card_game",
            "book_recommendations":"card_books","assessment_personality":"card_assessment",
        }
        for tip_item in tips:
            ctype  = ctype_map.get(topic, "tip")
            ctitle = t(ctitle_key.get(topic, "card_tip"), lang)
            cards.append({"type":ctype,"title":ctitle,"body":tip_item["tip"],
                          "meta":{"kb_id":tip_item["id"],"age_used":age,"matched":kb_res.matched}})

        cards.append({"type":"confidence","title": t("confidence_score", lang),
                      "body":f"{conf}%","meta":{"confidence":conf,"risk_level":risk_level}})

        if conf < 70 or (topic in PARENTING_TOPICS and not kb_res.matched):
            cards.append({"type":"warning","title": t("follow_up", lang),
                          "body":followups[0] if followups else "",
                          "meta":{"followups":followups}})

        if show_sp:
            for sp in spec_list:
                body = t(f"card_specialist_body_{lang}", lang,
                         price=sp["price_egp"], rating=sp["rating"])
                cards.append({"type":"specialist",
                               "title":f"{sp['name']} — {sp['title']}",
                               "body": body,
                               "meta":{"specialist_id":sp["id"]}})

        if slots_list and show_sp:
            duration_label = "دقيقة" if lang == "ar" else "min"
            sb = "\n".join([f"- {s['slot_id']}: {s['start']} ({s['duration_min']} {duration_label})"
                            for s in slots_list])
            sb += t(f"slots_suffix_{lang}", lang)
            cards.append({"type":"booking",
                           "title": t("available_slots", lang),
                           "body": sb,
                           "meta":{"slot_ids":[s["slot_id"] for s in slots_list],
                                   "specialist_id":spec_list[0]["id"] if spec_list else None}})

        return ChatResponse(message_id=message_id, reply=final_text, cards=cards)

    finally:
        conn.close()

# ──────────────────────────────────────────────
# ROUTES — PARENTING PLAN
# ──────────────────────────────────────────────
@app.post("/generate-parenting-plan/{user_id}", tags=["Parenting Plan"])
def generate_parenting_plan(user_id: str, preferred_language: Optional[str] = None):
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(status_code=503, detail="Gemini is disabled. Set GEMINI_API_KEY.")

    conn = get_conn()
    try:
        ensure_user_exists(conn, user_id)
        cur = conn.cursor()

        # Resolve language
        lang: Lang = "ar"
        if preferred_language in ("ar","en"):
            lang = preferred_language  # type: ignore[assignment]
        else:
            cur.execute("SELECT preferred_language FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            if row and row[0] in ("ar","en"):
                lang = row[0]

        # Fetch latest assessment
        cur.execute(
            "SELECT id, child_age, assessment_confidence, result, created_at FROM assessments WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=t("no_assessment_found", lang))

        assessment_id, child_age, assessment_confidence, result_raw, assessed_at = row

        try:
            result: Dict[str, Any] = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to parse assessment result: {exc}")

        if DEBUG:
            print(f"[PLAN] assessment result for user={user_id}: {json.dumps(result, ensure_ascii=False)[:400]}")

        top_traits             = _norm_traits(result.get("top_traits", []))
        low_traits_data        = _norm_traits(result.get("low_traits", []))
        possible_personalities = _norm_personalities(result.get("possible_personalities", []))
        trait_scores           = _norm_scores(result.get("trait_scores", {}))

        top_arch_entry  = possible_personalities[0] if possible_personalities else {}
        top_archetype   = top_arch_entry.get("name", "غير محدد" if lang == "ar" else "Not specified")
        archetype_desc  = top_arch_entry.get("description", "")
        archetype_needs = top_arch_entry.get("needs", "")

        if lang == "ar":
            traits_text = "\n".join(f"  - {t_['trait']}: {t_['score']}%" for t_ in top_traits) or "  - لا توجد بيانات"
            scores_text = "\n".join(f"  - {k}: {v}%" for k, v in trait_scores.items()) or "  - لا توجد بيانات"
        else:
            traits_text = "\n".join(f"  - {t_['trait'].replace('_',' ').title()}: {t_['score']}%" for t_ in top_traits) or "  - No data"
            scores_text = "\n".join(f"  - {k.replace('_',' ').title()}: {v}%" for k, v in trait_scores.items()) or "  - No data"

        try:
            plan_text = gemini_generate_parenting_plan(
                child_age=child_age,
                top_archetype=top_archetype,
                archetype_desc=archetype_desc,
                archetype_needs=archetype_needs,
                traits_text=traits_text,
                scores_text=scores_text,
                lang=lang,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

        if not plan_text:
            raise HTTPException(status_code=502, detail="Gemini returned an empty plan.")

        # Persist
        try:
            cur.execute(
                "INSERT INTO parenting_plans (user_id, plan_text, plan_language, created_at) VALUES (%s,%s,%s,NOW()) RETURNING id, created_at",
                (user_id, plan_text, lang)
            )
            plan_row        = cur.fetchone()
            conn.commit()
            plan_id         = plan_row[0]
            plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
        except Exception as exc:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"DB error saving plan: {exc}")

        log_event(conn, user_id, "parenting_plan_generated",
                  value=f"plan_id={plan_id}, lang={lang}, assessment_id={assessment_id}")

        # Firebase notification
        notification_sent    = False
        notification_warning = None

        if FIREBASE_ENABLED:
            cur.execute("SELECT fcm_token FROM users WHERE user_id=%s", (user_id,))
            token_row = cur.fetchone()
            fcm_token: Optional[str] = token_row[0] if token_row else None

            if not fcm_token:
                notification_warning = t("no_fcm_token", lang)
            else:
                try:
                    message = fb_messaging.Message(
                        notification=fb_messaging.Notification(
                            title=t("plan_notif_title", lang),
                            body=t("plan_notif_body",  lang),
                        ),
                        token=fcm_token,
                        data={"user_id": user_id, "type": "parenting_plan", "plan_id": str(plan_id)},
                    )
                    fb_messaging.send(message)
                    notification_sent = True
                except fb_messaging.UnregisteredError:
                    cur.execute("UPDATE users SET fcm_token=NULL WHERE user_id=%s", (user_id,))
                    conn.commit()
                    notification_warning = t("fcm_token_expired", lang)
                except Exception as fb_exc:
                    notification_warning = f"Firebase send error: {fb_exc}"
        else:
            notification_warning = t("firebase_not_configured", lang)

        response: Dict[str, Any] = {
            "ok":                True,
            "message":           t("plan_created_title", lang),
            "user_id":           user_id,
            "plan_id":           plan_id,
            "created_at":        plan_created_at,
            "plan_language":     lang,
            "child_age":         child_age,
            "top_archetype":     top_archetype,
            "assessment_id":     assessment_id,
            "notification_sent": notification_sent,
            "plan_text":         plan_text,
        }
        if notification_warning:
            response["notification_warning"] = notification_warning
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
            "SELECT id, plan_text, plan_language, created_at FROM parenting_plans WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, max(1, min(50, limit)))
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id, "total": len(rows),
            "plans": [{"id":r[0],"plan_text":r[1],"plan_language":r[2],
                       "created_at":r[3].isoformat() if r[3] else None} for r in rows],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()

# ──────────────────────────────────────────────
# ROUTES — PDF EXPORT
# ──────────────────────────────────────────────
@app.get("/export-plan-pdf/{user_id}", tags=["Parenting Plan"])
def export_plan_pdf(user_id: str):
    if not _REPORTLAB_AVAILABLE:
        raise HTTPException(status_code=503, detail=t("pdf_unavailable", "en"))

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pp.id, pp.plan_text, pp.created_at, pp.plan_language,
                   u.child_age, u.preferred_language,
                   a.result
            FROM   parenting_plans pp
            LEFT   JOIN users       u  ON u.user_id  = pp.user_id
            LEFT   JOIN assessments a  ON a.user_id  = pp.user_id
            WHERE  pp.user_id = %s
            ORDER  BY pp.created_at DESC
            LIMIT  1
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=t("no_plan_found", "ar"))

        plan_id, plan_text, created_at, plan_language, child_age, user_lang_pref, result_raw = row
        generated_at = created_at.isoformat() if created_at else ""

        # Resolve language: plan_language > user preferred > "ar"
        lang: Lang = "ar"
        for candidate in (plan_language, user_lang_pref):
            if candidate in ("ar","en"):
                lang = candidate  # type: ignore[assignment]
                break

        # Extract archetype name
        top_archetype = "Not specified" if lang == "en" else "غير محدد"
        if result_raw:
            try:
                result_obj = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
                personalities = _norm_personalities(result_obj.get("possible_personalities", []))
                if personalities:
                    top_archetype = personalities[0].get("name") or top_archetype
            except Exception as parse_exc:
                print(f"[PDF] Could not parse archetype: {parse_exc}")

        try:
            pdf_bytes = _build_parenting_plan_pdf(
                user_id=user_id,
                child_age=child_age,
                top_archetype=top_archetype,
                plan_text=plan_text or "",
                generated_at=generated_at,
                lang=lang,
            )
        except Exception as pdf_exc:
            raise HTTPException(status_code=500, detail=f"PDF generation failed: {pdf_exc}")

        filename = f"parenting_plan_{user_id}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(pdf_bytes)),
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        conn.close()
