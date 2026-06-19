"""
auto_learning.py — Rafiq Bot Auto-Learning System
===================================================
Version: 1.0.0
Attaches to the /chat endpoint after Gemini responds.
Evaluates quality, deduplicates, and persists high-value
Q/A pairs into faq_knowledge_base for future RAG retrieval.

INTEGRATION (in main.py):
    from auto_learning import maybe_learn_from_interaction
    # Call this after Gemini returns a reply, before return:
    maybe_learn_from_interaction(
        user_message=user_message,
        reply_text=reply_text,
        topic=topic,
        lang=lang,
        child_age=age,
        conn_factory=get_conn,
    )
"""

import re
import uuid
import logging
from datetime import datetime
from typing import Optional, Callable, Any

logger = logging.getLogger("rafiq.autolearn")

# ──────────────────────────────────────────────
# QUALITY THRESHOLDS
# ──────────────────────────────────────────────

# Minimum character counts to consider a Q/A pair worth storing
MIN_QUESTION_LEN  = 15   # Reject very short / incomplete questions
MIN_ANSWER_LEN    = 60   # Reject trivial one-liners
MAX_ANSWER_LEN    = 3000 # Reject runaway / broken responses

# If the answer contains these patterns it asked a clarifying question → skip
CLARIFICATION_PATTERNS = [
    r"هل يمكنك.*؟",           # "Can you ...?"  (Arabic)
    r"هل تقصد.*؟",
    r"ما هو.*؟",
    r"ما هي.*؟",
    r"هل.*عمر.*الطفل",
    r"could you (clarify|tell me|share|provide)",
    r"can you (tell|give|share|provide|clarify)",
    r"what (is|are|do you mean)",
    r"please (clarify|share|tell me|provide)",
    r"i need (more|a bit more) (context|information|detail)",
    r"could you elaborate",
]
_CLARIFICATION_RE = re.compile(
    "|".join(CLARIFICATION_PATTERNS), re.IGNORECASE
)

# Generic / useless reply indicators
GENERIC_REPLY_PATTERNS = [
    r"^(sorry|عذرًا|عذرا)[،,.]?\s*(i|لم|لا)",
    r"^(i'm not sure|لست متأكد)",
    r"^(i don't know|لا أعرف)",
]
_GENERIC_RE = re.compile("|".join(GENERIC_REPLY_PATTERNS), re.IGNORECASE)

# Topics that represent genuinely reusable parenting knowledge
LEARNABLE_TOPICS = {
    "teen_communication", "anger", "screen_addiction", "bullying",
    "study_focus", "siblings_jealousy", "parents_conflict", "lying",
    "general_parenting", "kids_stories", "activities_games",
    "book_recommendations", "assessment_personality",
}

# Deduplication: how similar must a stored question be to block insert?
# We do a lightweight token-overlap check (no vectors needed)
SIMILARITY_THRESHOLD = 0.75   # 75 % token overlap → treat as duplicate


# ──────────────────────────────────────────────
# QUALITY GATE
# ──────────────────────────────────────────────

def _passes_quality_gate(
    question: str,
    answer: str,
    topic: str,
) -> tuple[bool, str]:
    """
    Returns (should_store: bool, reason: str).
    All checks must pass for the pair to be learned.
    """
    q = question.strip()
    a = answer.strip()

    # Length checks
    if len(q) < MIN_QUESTION_LEN:
        return False, f"question too short ({len(q)} chars)"
    if len(a) < MIN_ANSWER_LEN:
        return False, f"answer too short ({len(a)} chars)"
    if len(a) > MAX_ANSWER_LEN:
        return False, f"answer too long ({len(a)} chars)"

    # Topic must be a real parenting topic
    if topic not in LEARNABLE_TOPICS:
        return False, f"topic '{topic}' not learnable"

    # Answer must not be a clarification request
    if _CLARIFICATION_RE.search(a):
        return False, "answer contains clarifying question"

    # Answer must not be a generic refusal / error
    if _GENERIC_RE.match(a):
        return False, "answer is a generic/error response"

    # Question must not look like a single word / fragment
    word_count = len(q.split())
    if word_count < 4:
        return False, f"question too fragmented ({word_count} words)"

    return True, "ok"


# ──────────────────────────────────────────────
# DEDUPLICATION (token-overlap, no embeddings)
# ──────────────────────────────────────────────

_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670]")


def _normalize_text(text: str) -> str:
    t = _AR_DIACRITICS.sub("", text.lower())
    for a, b in [("أ","ا"),("إ","ا"),("آ","ا"),("ى","ي"),("ة","ه"),("ؤ","و"),("ئ","ي")]:
        t = t.replace(a, b)
    return re.sub(r"[^\w\u0600-\u06FF]+", " ", t).strip()


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity between token sets of two strings."""
    ta = set(_normalize_text(a).split())
    tb = set(_normalize_text(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _is_duplicate(conn: Any, question: str, topic: str, lang: str) -> bool:
    """
    Check the last 200 stored questions for this topic/lang.
    Returns True if a near-duplicate already exists.
    This avoids re-storing the same advice repeatedly.
    """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT question
            FROM   faq_knowledge_base
            WHERE  topic = %s AND lang = %s
            ORDER  BY created_at DESC
            LIMIT  200
            """,
            (topic, lang),
        )
        rows = cur.fetchall()
        for (stored_q,) in rows:
            if _token_overlap(question, stored_q) >= SIMILARITY_THRESHOLD:
                return True
        return False
    except Exception as exc:
        logger.warning("[autolearn] dedup check failed (non-fatal): %s", exc)
        return False   # fail-open: allow insert if check fails


# ──────────────────────────────────────────────
# DB INSERT
# ──────────────────────────────────────────────

def _insert_learned_pair(
    conn: Any,
    question: str,
    answer: str,
    topic: str,
    lang: str,
    child_age: Optional[int],
) -> Optional[int]:
    """
    Insert one learned Q/A pair into faq_knowledge_base.
    Returns the new row id, or None on failure.
    The tsvector trigger handles search_vector automatically.
    """
    # Build a minimal tag list from topic + age
    tags: list[str] = [topic]
    if child_age is not None:
        tags.append(f"age_{child_age}")
    tags.append("auto_learned")

    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO faq_knowledge_base
                (topic, question, answer, tags, lang, created_at)
            VALUES
                (%s, %s, %s, %s, %s, NOW())
            RETURNING id
            """,
            (topic, question, answer, tags, lang),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    except Exception as exc:
        conn.rollback()
        logger.error("[autolearn] DB insert failed: %s", exc)
        return None


# ──────────────────────────────────────────────
# PUBLIC API — call this from /chat
# ──────────────────────────────────────────────

def maybe_learn_from_interaction(
    user_message: str,
    reply_text: str,
    topic: str,
    lang: str,
    child_age: Optional[int],
    conn_factory: Callable[[], Any],
) -> None:

    print("AUTO LEARNING CALLED")

    """
    Entry point called after every successful /chat Gemini response.

    This function is entirely non-blocking from the caller's perspective:
    all failures are caught and logged — the /chat response is never affected.

    Args:
        user_message:  The raw user question.
        reply_text:    The Gemini-generated answer.
        topic:         Detected topic (from RouteDecision).
        lang:          'ar' or 'en'.
        child_age:     Child age if known.
        conn_factory:  Callable that returns a psycopg2 connection (i.e. get_conn).
    """
    try:
        # ── 1. Quality gate ──────────────────────────────────────────
        should_store, reason = _passes_quality_gate(user_message, reply_text, topic)
        if not should_store:
            logger.debug("[autolearn] skipped — %s", reason)
            return

        # ── 2. Get a DB connection ───────────────────────────────────
        conn = conn_factory()

        try:
            # ── 3. Deduplication ────────────────────────────────────
            if _is_duplicate(conn, user_message, topic, lang):
                logger.debug("[autolearn] skipped — duplicate detected")
                return

            # ── 4. Persist ───────────────────────────────────────────
            new_id = _insert_learned_pair(
                conn=conn,
                question=user_message,
                answer=reply_text,
                topic=topic,
                lang=lang,
                child_age=child_age,
            )

            if new_id:
                logger.info(
                    "[autolearn] ✅ learned id=%s | topic=%s | lang=%s | q_len=%d | a_len=%d",
                    new_id, topic, lang, len(user_message), len(reply_text),
                )
            else:
                logger.warning("[autolearn] insert returned no id (possible silent failure)")

        finally:
            conn.close()

    except Exception as exc:
        # Absolute safety net — learning must NEVER break the chat response
        logger.error("[autolearn] unexpected error (non-fatal): %s", exc)
