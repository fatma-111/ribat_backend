"""
Rafiq Bot API — CHAT-ONLY v5.0
================================
Clean, production-ready chat backend.

Features:
- Single /chat endpoint
- PostgreSQL FTS knowledge base retrieval (ILIKE fallback)
- Gemini generation (direct answer, no follow-up questions)
- Bilingual (ar / en)
- Risk guard (high-risk messages get emergency response)
- Out-of-scope guard (programming, medical)
- Health check + KB admin endpoints
"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
import re
import uuid
from typing import Any, Dict, List, Literal, Optional

import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ──────────────────────────────────────────────
# ENVIRONMENT
# ──────────────────────────────────────────────
DATABASE_URL   = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ADMIN_KEY      = os.getenv("RAFIQ_ADMIN_KEY", "change-me")
DEBUG          = os.getenv("RAFIQ_DEBUG", "0") == "1"

if ADMIN_KEY == "change-me":
    print("WARNING: RAFIQ_ADMIN_KEY is using default value.")

# ──────────────────────────────────────────────
# GEMINI CLIENT
# ──────────────────────────────────────────────
GEMINI_ENABLED = False
gemini_client  = None

if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as genai_types
        gemini_client  = genai.Client(api_key=GEMINI_API_KEY)
        GEMINI_ENABLED = True
        print(f"Gemini initialized ✔  model={GEMINI_MODEL}")
    except Exception as exc:
        print(f"Gemini init failed: {exc}")
else:
    print("WARNING: GEMINI_API_KEY not set — Gemini disabled.")

# ──────────────────────────────────────────────
# APP
# ──────────────────────────────────────────────
app = FastAPI(
    title="Rafiq Bot API",
    version="5.0.0",
    description="Family support chat assistant — bilingual (ar/en) | Chat-only",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    _run_schema_migrations()


# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured.")
    try:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB connection failed: {exc}")


def _run_schema_migrations() -> None:
    if not DATABASE_URL:
        print("Skipping DB migrations — DATABASE_URL not set.")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur  = conn.cursor()

        # Users table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    VARCHAR(100) PRIMARY KEY,
                notes      TEXT         DEFAULT '[]',
                child_age  INTEGER,
                name       VARCHAR(200),
                email      VARCHAR(200) UNIQUE,
                preferred_language VARCHAR(5) DEFAULT 'ar',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
            """
        )

        # Chat messages table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         SERIAL PRIMARY KEY,
                message_id VARCHAR(30) UNIQUE,
                user_id    VARCHAR(100),
                message    TEXT,
                response   TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """
        )

        # Analytics table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics (
                id         SERIAL PRIMARY KEY,
                event_id   VARCHAR(30),
                user_id    VARCHAR(100),
                event_type VARCHAR(100),
                value      TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """
        )

        # Knowledge base table with FTS
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS faq_knowledge_base (
                id            SERIAL PRIMARY KEY,
                topic         VARCHAR(100)  NOT NULL,
                question      TEXT          NOT NULL,
                answer        TEXT          NOT NULL,
                tags          TEXT[]        DEFAULT '{}',
                age_min       INTEGER       DEFAULT 4,
                age_max       INTEGER       DEFAULT 18,
                lang          VARCHAR(5)    DEFAULT 'ar',
                search_vector TSVECTOR,
                created_at    TIMESTAMP     DEFAULT NOW(),
                updated_at    TIMESTAMP     DEFAULT NOW()
            );
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_faq_kb_fts
            ON faq_knowledge_base USING GIN (search_vector);
            """
        )

        # Trigger to keep search_vector in sync
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION faq_kb_search_vector_update()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('simple', COALESCE(NEW.question, '')), 'A') ||
                    setweight(to_tsvector('simple', COALESCE(NEW.answer,   '')), 'B') ||
                    setweight(to_tsvector('simple',
                        COALESCE(array_to_string(NEW.tags, ' '), '')), 'C');
                NEW.updated_at := NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        cur.execute(
            "DROP TRIGGER IF EXISTS trig_faq_kb_fts ON faq_knowledge_base;"
        )
        cur.execute(
            """
            CREATE TRIGGER trig_faq_kb_fts
            BEFORE INSERT OR UPDATE ON faq_knowledge_base
            FOR EACH ROW EXECUTE FUNCTION faq_kb_search_vector_update();
            """
        )

        # Backfill any NULL search_vectors
        cur.execute(
            """
            UPDATE faq_knowledge_base
            SET search_vector =
                setweight(to_tsvector('simple', COALESCE(question, '')), 'A') ||
                setweight(to_tsvector('simple', COALESCE(answer,   '')), 'B') ||
                setweight(to_tsvector('simple',
                    COALESCE(array_to_string(tags, ' '), '')), 'C')
            WHERE search_vector IS NULL;
            """
        )

        conn.commit()
        conn.close()
        print("DB migrations applied ✔")
    except Exception as exc:
        print(f"DB migration warning: {exc}")


# ──────────────────────────────────────────────
# LANGUAGE DETECTION
# ──────────────────────────────────────────────
Lang = Literal["ar", "en"]


def detect_lang(text: str) -> Lang:
    ar = len(re.findall(r"[\u0600-\u06FF]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return "ar" if ar >= en else "en"


# ──────────────────────────────────────────────
# GUARDS
# ──────────────────────────────────────────────
OUT_OF_SCOPE_KW = [
    "برمجة", "كود", "flutter", "android", "python", "java", "c++",
    "backend", "frontend", "database", "debug", "algorithm", "api",
]
MEDICAL_KW = [
    "جرعة", "دواء", "حبوب", "مضاد", "تشخيص", "روشتة", "وصفة",
    "medication", "diagnosis", "prescription", "dosage",
]
RISK_HIGH_KW = [
    "عايز أموت", "مش عايز أعيش", "هأذي نفسي", "انتحار",
    "هنتحر", "هقتل", "هموت", "أذي نفسي",
    "suicide", "kill myself", "end my life",
]


def is_out_of_scope(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in OUT_OF_SCOPE_KW)


def is_medical(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in MEDICAL_KW)


def is_high_risk(text: str) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in RISK_HIGH_KW)


# ──────────────────────────────────────────────
# KNOWLEDGE BASE RETRIEVAL
# ──────────────────────────────────────────────
def retrieve_kb_context(query: str, lang: Optional[str] = None, limit: int = 3) -> str:
    """
    Retrieve relevant knowledge base entries from PostgreSQL.

    Strategy:
      1. tsvector / tsquery (preferred)
      2. ILIKE fallback for short / punctuation-heavy queries

    Returns formatted context string for the Gemini prompt.
    """
    if not query or not query.strip() or not DATABASE_URL:
        return ""

    results: List[Dict[str, Any]] = []

    try:
        conn = get_conn()
        cur  = conn.cursor()

        lang_filter        = "AND lang = %s" if lang else ""
        lang_filter_params = [lang] if lang else []

        # ── 1. FTS (tsvector / tsquery) ───────────────────────────────
        raw_tokens = [
            re.sub(r"[^\w\u0600-\u06FF]", "", tok)
            for tok in query.strip().split()
            if len(tok) >= 2
        ]
        tokens = [tok for tok in raw_tokens if tok]

        if tokens:
            tsquery_str = " | ".join(tokens)
            fts_sql = f"""
                SELECT question, answer, tags,
                       ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS rank
                FROM faq_knowledge_base
                WHERE search_vector @@ to_tsquery('simple', %s)
                {lang_filter}
                ORDER BY rank DESC
                LIMIT %s;
            """
            params = [tsquery_str, tsquery_str] + lang_filter_params + [limit]
            cur.execute(fts_sql, params)
            rows = cur.fetchall()

            if DEBUG:
                print(f"[KB] FTS tsquery='{tsquery_str}' → {len(rows)} rows")

            for row in rows:
                results.append({
                    "question": row[0],
                    "answer":   row[1],
                    "tags":     row[2] or [],
                })

        # ── 2. ILIKE fallback ─────────────────────────────────────────
        if not results:
            search_term  = tokens[0] if tokens else query.strip()
            like_pattern = f"%{search_term}%"
            ilike_sql = f"""
                SELECT question, answer, tags
                FROM faq_knowledge_base
                WHERE (
                    question ILIKE %s
                    OR answer   ILIKE %s
                    OR array_to_string(tags, ' ') ILIKE %s
                )
                {lang_filter}
                ORDER BY
                    CASE WHEN question ILIKE %s THEN 0 ELSE 1 END,
                    updated_at DESC
                LIMIT %s;
            """
            params = (
                [like_pattern, like_pattern, like_pattern, like_pattern]
                + lang_filter_params
                + [limit]
            )
            cur.execute(ilike_sql, params)
            rows = cur.fetchall()

            if DEBUG:
                print(f"[KB] ILIKE pattern='{like_pattern}' → {len(rows)} rows")

            for row in rows:
                results.append({
                    "question": row[0],
                    "answer":   row[1],
                    "tags":     row[2] or [],
                })

        conn.close()

    except Exception as exc:
        print(f"[KB] retrieval error: {exc}")
        return ""

    if not results:
        return ""

    lines = []
    for i, item in enumerate(results, 1):
        lines.append(f"[{i}] Q: {item['question']}\n    A: {item['answer']}")
    return "\n\n".join(lines)


# ──────────────────────────────────────────────
# GEMINI GENERATION
# ──────────────────────────────────────────────
CHAT_PROMPT_TEMPLATE = """\
You are a professional parenting assistant called Rafiq.

Your job is to answer user questions clearly, directly, and helpfully.

Rules:
- ALWAYS provide a direct, complete answer.
- NEVER ask follow-up questions.
- NEVER request more information from the user.
- Use the Knowledge Base Context below if it is relevant; otherwise rely on your general parenting knowledge.
- Respond in the same language as the user's question.
- Output ONLY the final answer — no preamble, no meta-commentary.

Knowledge Base Context:
{context}

User Question:
{user_message}

Now generate only the final answer.
"""


def call_gemini(user_message: str, context: str) -> str:
    if not GEMINI_ENABLED or gemini_client is None:
        raise HTTPException(
            status_code=503,
            detail="Gemini is disabled. Please set the GEMINI_API_KEY environment variable."
        )

    context_block = context.strip() if context.strip() else "No specific knowledge base entries found."
    prompt = CHAT_PROMPT_TEMPLATE.format(
        context=context_block,
        user_message=user_message.strip(),
    )

    try:
        resp = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        text = (resp.text or "").strip()
        if not text:
            raise ValueError("Gemini returned an empty response.")
        return text
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini generation failed: {exc}")


# ──────────────────────────────────────────────
# DB HELPERS
# ──────────────────────────────────────────────
def ensure_user(conn, user_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, notes) VALUES (%s, '[]') ON CONFLICT (user_id) DO NOTHING",
        (user_id,)
    )
    conn.commit()


def log_event(conn, user_id: str, event_type: str, value: str = "") -> None:
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO analytics (event_id, user_id, event_type, value) VALUES (%s, %s, %s, %s)",
            ("ev_" + uuid.uuid4().hex[:10], user_id, event_type, value[:300])
        )
        conn.commit()
    except Exception as exc:
        print(f"[analytics] log_event failed: {exc}")


def save_chat_message(conn, message_id: str, user_id: str, message: str, response: str) -> None:
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (message_id, user_id, message, response) VALUES (%s, %s, %s, %s)",
            (message_id, user_id, message, response)
        )
        conn.commit()
    except Exception as exc:
        print(f"[chat] save_chat_message failed: {exc}")


# ──────────────────────────────────────────────
# PYDANTIC MODELS
# ──────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    user_id: str
    messages: List[ChatMessage]
    preferred_language: Optional[str] = None


class ChatResponse(BaseModel):
    message_id: str
    reply: str


class FaqKbAddRequest(BaseModel):
    admin_key: str
    topic: str
    question: str
    answer: str
    tags: List[str] = []
    age_min: int = 4
    age_max: int = 18
    lang: str = "ar"


class UserUpsertReq(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    child_age: Optional[int] = None
    preferred_language: Optional[str] = "ar"


# ──────────────────────────────────────────────
# ROUTES — SYSTEM
# ──────────────────────────────────────────────
@app.get("/", tags=["System"])
def home():
    return {
        "status":  "Rafiq running 🚀",
        "version": "5.0.0",
        "mode":    "chat-only",
    }


@app.get("/health", tags=["System"])
def health():
    db_ok = False
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            conn.close()
            db_ok = True
        except Exception:
            pass

    return {
        "ok":             True,
        "gemini_enabled": GEMINI_ENABLED,
        "gemini_model":   GEMINI_MODEL,
        "db_connected":   db_ok,
        "debug":          DEBUG,
    }


# ──────────────────────────────────────────────
# ROUTES — USERS
# ──────────────────────────────────────────────
@app.post("/users", tags=["Users"])
def upsert_user(req: UserUpsertReq):
    conn = get_conn()
    try:
        lang = req.preferred_language if req.preferred_language in ("ar", "en") else "ar"
        cur  = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (user_id, name, email, child_age, notes, preferred_language)
            VALUES (%s, %s, %s, %s, '[]', %s)
            ON CONFLICT (user_id) DO UPDATE SET
                name               = COALESCE(EXCLUDED.name,               users.name),
                email              = COALESCE(EXCLUDED.email,              users.email),
                child_age          = COALESCE(EXCLUDED.child_age,          users.child_age),
                preferred_language = COALESCE(EXCLUDED.preferred_language, users.preferred_language),
                updated_at         = NOW()
            RETURNING user_id, name, email, child_age, preferred_language, created_at, updated_at
            """,
            (req.user_id, req.name, req.email, req.child_age, lang)
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "ok":   True,
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


# ──────────────────────────────────────────────
# ROUTES — KNOWLEDGE BASE
# ──────────────────────────────────────────────
@app.post("/kb/add", tags=["KB"])
def kb_add(req: FaqKbAddRequest):
    """Add a Q&A pair to the persistent knowledge base."""
    if req.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin_key.")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO faq_knowledge_base (topic, question, answer, tags, age_min, age_max, lang)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (req.topic, req.question, req.answer, req.tags, req.age_min, req.age_max, req.lang)
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": new_id, "topic": req.topic, "lang": req.lang}
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()


@app.get("/kb/search", tags=["KB"])
def kb_search_api(q: str, lang: Optional[str] = None, limit: int = 3):
    """Search the knowledge base (for testing/admin)."""
    context = retrieve_kb_context(query=q, lang=lang, limit=limit)
    return {"query": q, "lang": lang, "context": context}


# ──────────────────────────────────────────────
# ROUTES — CHAT HISTORY
# ──────────────────────────────────────────────
@app.get("/chat/{user_id}", tags=["Chat"])
def get_chat_history(user_id: str, limit: int = 50):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message_id, message, response, created_at
            FROM chat_messages
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, max(1, min(200, limit)))
        )
        rows = cur.fetchall()
        return {
            "user_id":  user_id,
            "messages": [
                {
                    "message_id":   r[0],
                    "user_message": r[1],
                    "bot_reply":    r[2],
                    "created_at":   r[3].isoformat() if r[3] else None,
                }
                for r in rows
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()


# ──────────────────────────────────────────────
# ROUTES — CHAT (main)
# ──────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(req: ChatRequest):
    # ── 1. Validate & extract user message ────────────────────────────
    if not req.messages:
        raise HTTPException(status_code=400, detail="'messages' list is empty.")

    user_message = req.messages[-1].content.strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Last message content is empty.")

    message_id = "msg_" + uuid.uuid4().hex[:10]

    # Detect language
    lang: Lang = (
        req.preferred_language  # type: ignore[assignment]
        if req.preferred_language in ("ar", "en")
        else detect_lang(user_message)
    )

    # ── 2. Hard guards ─────────────────────────────────────────────────
    if is_out_of_scope(user_message) or is_medical(user_message):
        refusal = (
            "أنا بوت (رفيق) متخصص في دعم الأسرة والتربية. "
            "مش بقدر أساعد في البرمجة أو الأدوية أو التشخيص الطبي."
            if lang == "ar"
            else
            "I'm Rafiq, a family and parenting support assistant. "
            "I can't help with programming, medications, or medical diagnosis."
        )
        return ChatResponse(message_id=message_id, reply=refusal)

    if is_high_risk(user_message):
        emergency = (
            "أنا قلقان عليك جدًا. تواصل فورًا مع شخص كبير موثوق قريب منك أو خدمات الطوارئ. "
            "رفيق للدعم العام فقط وليس بديلًا عن المتخصصين."
            if lang == "ar"
            else
            "I'm very concerned about you. Please immediately reach out to a trusted adult "
            "or contact emergency services. Rafiq is for general support only."
        )
        # Still log the event before returning
        if DATABASE_URL:
            try:
                conn = get_conn()
                ensure_user(conn, req.user_id)
                log_event(conn, req.user_id, "risk_high", value=user_message[:200])
                conn.close()
            except Exception:
                pass
        return ChatResponse(message_id=message_id, reply=emergency)

    # ── 3. Retrieve KB context from PostgreSQL ─────────────────────────
    context = retrieve_kb_context(query=user_message, lang=lang, limit=3)

    if DEBUG:
        print(f"[CHAT] user_id={req.user_id} | lang={lang} | kb_context_len={len(context)}")

    # ── 4. Call Gemini ─────────────────────────────────────────────────
    reply = call_gemini(user_message=user_message, context=context)

    # ── 5. Persist & log ───────────────────────────────────────────────
    if DATABASE_URL:
        try:
            conn = get_conn()
            ensure_user(conn, req.user_id)
            save_chat_message(conn, message_id, req.user_id, user_message, reply)
            log_event(conn, req.user_id, "chat_message", value=user_message[:300])
            conn.close()
        except Exception as db_exc:
            # Non-fatal — still return the reply
            print(f"[CHAT] DB persistence error: {db_exc}")

    return ChatResponse(message_id=message_id, reply=reply)
