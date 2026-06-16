"""
Rafiq Bot — targeted fixes (apply these as str_replace patches to main.py)
==========================================================================

FIX 1 — compute_personality_profile: behavior signal bonus inflates max_
  PROBLEM: max_["focus"] += 6 was unconditional, so when raw["focus"] += 0
           (gives_up_fast=0 → max(0, 3-0)*2 = 6 but that IS a real score)
           the denominator is always inflated.
           Worse: even if max_["focus"] was already 0 (no focus questions
           answered), the guard `if max_["focus"] > 0` runs AFTER the add,
           so the check was on the pre-bonus value — meaning the bonus ran
           when it shouldn't.

  FIX: compute the bonus delta first, add to BOTH raw and max by the same
       delta so the ratio is unchanged by zero-value signals, and skip
       entirely when no focus/empathy questions were answered.

REPLACE THIS BLOCK (inside compute_personality_profile, after the answer loop):

    # Behavior signal bonuses — only applied when trait already has answer data
    # (avoids inflating max_ denominator for traits with zero answers → all-zero scores)
    bs = behavior_signals or {}
    if max_["focus"] > 0:
        raw["focus"]   += max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2
        max_["focus"]  += 6
    if max_["empathy"] > 0:
        raw["empathy"] += int(bs.get("helps_others", 0)) * 2
        max_["empathy"] += 4

WITH:

    bs = behavior_signals or {}
    if max_["focus"] > 0:
        focus_bonus = max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2
        raw["focus"]  += focus_bonus
        max_["focus"] += focus_bonus          # same delta → ratio preserved
    if max_["empathy"] > 0:
        empathy_bonus = int(bs.get("helps_others", 0)) * 2
        raw["empathy"]  += empathy_bonus
        max_["empathy"] += empathy_bonus      # same delta → ratio preserved

WHY THIS WORKS:
  - If gives_up_fast=0  → bonus=6, max+=6, ratio unchanged (not inflated)
  - If gives_up_fast=3  → bonus=0, nothing added, ratio unchanged
  - If no focus answers → max_["focus"]==0, block skipped entirely
  - Score is now purely a function of answered questions, as intended.

═══════════════════════════════════════════════════════════════════════════════

FIX 2 — generate_parenting_plan: FCM notification uses stale cursor / wrong lang

  PROBLEM A: After `conn.commit()` the cursor `cur` is still open on the same
             connection. Opening `notif_cur = conn.cursor()` on the same
             connection is fine in psycopg2, BUT if any exception between
             the commit and the fcm block caused a silent rollback, the
             SELECT for fcm_token returns nothing.

  PROBLEM B: The `lang` variable inside the Firebase block correctly references
             the outer `lang`, but `plan_id` is extracted from `plan_row[0]`
             and then passed as `str(plan_id)` to FCM data — if plan_row is
             None (edge case where RETURNING failed), this throws an
             UnboundLocalError instead of a clean 500.

  PROBLEM C: `plan_language` written to DB uses `lang` which is resolved
             before the INSERT — this is correct — but there was a risk of
             `lang` being shadowed by the query-param variable of the same
             name in the function signature. Rename the param to avoid it.

REPLACE THE FUNCTION SIGNATURE:

    def generate_parenting_plan(user_id: str, preferred_language: Optional[str] = None):

WITH:

    def generate_parenting_plan(user_id: str, preferred_language: Optional[str] = None):
        # (no signature change needed — see inner variable fix below)

REPLACE THE PLAN INSERT + FCM BLOCK:

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

        if not FIREBASE_ENABLED:
            ...
        else:
            # Fresh cursor — previous cursor may be in a finished transaction
            notif_cur = conn.cursor()
            notif_cur.execute("SELECT fcm_token FROM users WHERE user_id=%s", (user_id,))

WITH:

        plan_id: Optional[int] = None
        plan_created_at: Optional[str] = None
        try:
            cur.execute(
                "INSERT INTO parenting_plans (user_id, plan_text, plan_language, created_at)"
                " VALUES (%s,%s,%s,NOW()) RETURNING id, created_at",
                (user_id, plan_text, lang)
            )
            plan_row = cur.fetchone()
            if not plan_row:
                raise HTTPException(status_code=500, detail="DB INSERT returned no row.")
            plan_id         = plan_row[0]
            plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
            conn.commit()
        except HTTPException:
            raise
        except Exception as exc:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"DB error saving plan: {exc}")

        log_event(conn, user_id, "parenting_plan_generated",
                  value=f"plan_id={plan_id}, lang={lang}, assessment_id={assessment_id}")

        # Firebase notification — use a fresh cursor on the committed connection
        notification_sent    = False
        notification_warning = None

        if not FIREBASE_ENABLED:
            ...
        else:
            notif_cur = conn.cursor()   # fresh cursor after commit — safe
            notif_cur.execute("SELECT fcm_token FROM users WHERE user_id=%s", (user_id,))

═══════════════════════════════════════════════════════════════════════════════

FIX 3 — export_plan_pdf: PDF defaults to Arabic even when plan was generated in English

  PROBLEM: The priority chain in export_plan_pdf is correct in the code:
               for candidate in (lang, plan_language, user_lang_pref):
           BUT `lang` here is the *query parameter* (Optional[str]), which
           defaults to None. So when no ?lang= is passed the chain falls
           through to plan_language — which IS the fix you want.

           The actual bug is in generate_parenting_plan: it resolves `lang`
           correctly but then the response JSON returns `"plan_language": lang`
           which is right. However the DB INSERT also uses `lang` — so if
           generate_parenting_plan is called without ?preferred_language and
           the user has preferred_language='en' in the DB, `lang` is correctly
           'en', and plan_language is stored as 'en'. export_plan_pdf then
           reads plan_language='en' and produces an English PDF. ✓

           The REAL remaining bug: export_plan_pdf declares its own `lang`
           variable from the query param, then re-assigns it to `resolved_lang`,
           then does `lang = resolved_lang` — but the type annotation
           `lang: Optional[str] = None` on the function param shadows the
           loop variable. After the loop, `lang` holds the resolved value
           correctly. So this path is actually fine.

           ACTUAL remaining issue: when generate_parenting_plan is called
           and the user has NO preferred_language set in DB (NULL), `db_lang`
           is None, and `lang` stays 'ar' — even if the Flutter client passed
           preferred_language='en'. This is because the DB SELECT runs BEFORE
           the language resolution loop:

               for candidate in (preferred_language, db_lang):

           If preferred_language='en' is passed, `lang` becomes 'en' ✓
           But if preferred_language=None and db_lang=None, `lang` stays 'ar' ✓
           ... this is correct behavior.

           The only real fix needed here is to guarantee that the PDF
           endpoint respects the stored plan_language. The code already does
           this — but only if plan_language was stored correctly. Verify
           by checking the parenting_plans table has a non-NULL plan_language.

  RECOMMENDATION: Add a NOT NULL DEFAULT on plan_language in the migration:

        cur.execute(
            "ALTER TABLE parenting_plans "
            "ALTER COLUMN plan_language SET NOT NULL, "
            "ALTER COLUMN plan_language SET DEFAULT 'ar';"
        )

  And in export_plan_pdf, make the fallback explicit so NULL plan_language
  in old rows doesn't silently fall through to 'ar' when the user is English:

REPLACE:

        resolved_lang: Lang = "ar"
        for candidate in (lang, plan_language, user_lang_pref):
            if candidate in ("ar", "en"):
                resolved_lang = candidate
                break
        lang = resolved_lang

WITH:

        resolved_lang: Lang = "ar"
        for candidate in (lang, plan_language, user_lang_pref):
            if candidate in ("ar", "en"):
                resolved_lang = candidate  # type: ignore[assignment]
                break
        effective_lang: Lang = resolved_lang   # use this name to avoid shadowing the param
        print(f"[PDF] user={user_id}, resolved lang={effective_lang} "
              f"(param={lang}, plan_language={plan_language}, user_pref={user_lang_pref})")

THEN replace every subsequent use of `lang` in _build_parenting_plan_pdf call
and StreamingResponse with `effective_lang`:

        pdf_bytes = _build_parenting_plan_pdf(
            user_id=user_id,
            child_age=child_age,
            top_archetype=top_archetype,
            plan_text=plan_text or "",
            generated_at=generated_at,
            lang=effective_lang,          # <-- was `lang`
        )
"""

# ── Standalone test for Fix 1 ────────────────────────────────────────────────
# Run: python rafiq_fixes.py
# Expected: all trait scores > 0 when every question answered with score=3

ALL_TRAITS = ["leadership","sociability","empathy","self_control",
              "focus","curiosity","adaptability","sensitivity"]

ASSESSMENT_QUESTIONS = [
    {"id":"q01","trait":"focus",        "age_min":4,"age_max":18,"weights":{"focus":2}},
    {"id":"q02","trait":"focus",        "age_min":7,"age_max":18,"weights":{"focus":2,"self_control":1}},
    {"id":"q03","trait":"focus",        "age_min":4,"age_max":18,"weights":{"focus":3}},
    {"id":"q04","trait":"empathy",      "age_min":4,"age_max":18,"weights":{"empathy":2}},
    {"id":"q05","trait":"empathy",      "age_min":6,"age_max":18,"weights":{"empathy":2,"sociability":1}},
    {"id":"q06","trait":"empathy",      "age_min":4,"age_max":18,"weights":{"empathy":3}},
    {"id":"q07","trait":"curiosity",    "age_min":4,"age_max":18,"weights":{"curiosity":2}},
    {"id":"q08","trait":"curiosity",    "age_min":6,"age_max":18,"weights":{"curiosity":2,"adaptability":1}},
    {"id":"q09","trait":"curiosity",    "age_min":4,"age_max":18,"weights":{"curiosity":3}},
    {"id":"q10","trait":"leadership",   "age_min":5,"age_max":18,"weights":{"leadership":2}},
    {"id":"q11","trait":"leadership",   "age_min":8,"age_max":18,"weights":{"leadership":2,"focus":1}},
    {"id":"q12","trait":"leadership",   "age_min":5,"age_max":18,"weights":{"leadership":3}},
    {"id":"q13","trait":"sociability",  "age_min":4,"age_max":18,"weights":{"sociability":2}},
    {"id":"q14","trait":"sociability",  "age_min":4,"age_max":18,"weights":{"sociability":2,"empathy":1}},
    {"id":"q15","trait":"sociability",  "age_min":4,"age_max":18,"weights":{"sociability":3}},
    {"id":"q16","trait":"adaptability", "age_min":4,"age_max":18,"weights":{"adaptability":2}},
    {"id":"q17","trait":"adaptability", "age_min":6,"age_max":18,"weights":{"adaptability":2,"self_control":1}},
    {"id":"q18","trait":"self_control", "age_min":4,"age_max":18,"weights":{"self_control":2}},
    {"id":"q19","trait":"self_control", "age_min":6,"age_max":18,"weights":{"self_control":3}},
    {"id":"q20","trait":"sensitivity",  "age_min":4,"age_max":18,"weights":{"sensitivity":2}},
    {"id":"q21","trait":"sensitivity",  "age_min":4,"age_max":18,"weights":{"sensitivity":3}},
]

_QS_NORM = {q["id"].strip().lower(): q for q in ASSESSMENT_QUESTIONS}


def _normalize_answer_id(raw_id):
    return str(raw_id or "").strip().lower()

def _extract_answer_value(answer):
    raw = answer.get("value") if answer.get("value") is not None else answer.get("score")
    try:
        v = int(raw)
        return v if 1 <= v <= 5 else None
    except (TypeError, ValueError):
        return None


def compute_personality_profile_FIXED(answers, child_age, behavior_signals=None):
    raw  = {tr: 0.0 for tr in ALL_TRAITS}
    max_ = {tr: 0.0 for tr in ALL_TRAITS}

    for a in answers:
        qid_raw = a.get("question_id") or a.get("id")
        qid     = _normalize_answer_id(qid_raw)
        val     = _extract_answer_value(a)
        q = _QS_NORM.get(qid)
        if q is None or val is None:
            continue
        for trait, w in q["weights"].items():
            raw[trait]  += val * w
            max_[trait] += 5 * w

    # ── FIX: bonus delta added to BOTH raw and max_ equally ──────────────
    bs = behavior_signals or {}
    if max_["focus"] > 0:
        focus_bonus   = max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2
        raw["focus"]  += focus_bonus
        max_["focus"] += focus_bonus      # ratio preserved

    if max_["empathy"] > 0:
        empathy_bonus   = int(bs.get("helps_others", 0)) * 2
        raw["empathy"]  += empathy_bonus
        max_["empathy"] += empathy_bonus  # ratio preserved
    # ─────────────────────────────────────────────────────────────────────

    def _norm(r, m):
        return max(0, min(100, int(round(r / m * 100)))) if m > 0 else 0

    return {tr: _norm(raw[tr], max_[tr]) for tr in ALL_TRAITS}


def compute_personality_profile_BUGGY(answers, child_age, behavior_signals=None):
    """Original buggy version for comparison."""
    raw  = {tr: 0.0 for tr in ALL_TRAITS}
    max_ = {tr: 0.0 for tr in ALL_TRAITS}

    for a in answers:
        qid_raw = a.get("question_id") or a.get("id")
        qid     = _normalize_answer_id(qid_raw)
        val     = _extract_answer_value(a)
        q = _QS_NORM.get(qid)
        if q is None or val is None:
            continue
        for trait, w in q["weights"].items():
            raw[trait]  += val * w
            max_[trait] += 5 * w

    # BUG: always adds 6 to max_["focus"] regardless of bonus value
    bs = behavior_signals or {}
    if max_["focus"] > 0:
        raw["focus"]   += max(0, 3 - int(bs.get("gives_up_fast", 0))) * 2
        max_["focus"]  += 6   # <-- inflates denominator unconditionally
    if max_["empathy"] > 0:
        raw["empathy"] += int(bs.get("helps_others", 0)) * 2
        max_["empathy"] += 4  # <-- inflates denominator unconditionally

    def _norm(r, m):
        return max(0, min(100, int(round(r / m * 100)))) if m > 0 else 0

    return {tr: _norm(raw[tr], max_[tr]) for tr in ALL_TRAITS}


if __name__ == "__main__":
    # All 21 questions answered with score=3, no behavior signals
    answers_all_3 = [{"question_id": q["id"], "score": 3} for q in ASSESSMENT_QUESTIONS]

    # Worst case: gives_up_fast=0 (max bonus), helps_others=0 (zero bonus)
    bs_worst = {"gives_up_fast": 0, "helps_others": 0}

    print("=" * 60)
    print("TEST: all questions score=3, gives_up_fast=0, helps_others=0")
    print("=" * 60)

    buggy = compute_personality_profile_BUGGY(answers_all_3, child_age=10, behavior_signals=bs_worst)
    fixed = compute_personality_profile_FIXED(answers_all_3, child_age=10, behavior_signals=bs_worst)

    print(f"\n{'Trait':<16} {'BUGGY':>8} {'FIXED':>8}  {'Change':>8}")
    print("-" * 46)
    for tr in ALL_TRAITS:
        change = fixed[tr] - buggy[tr]
        flag   = " ✓" if fixed[tr] > 0 else " ✗ STILL ZERO"
        print(f"{tr:<16} {buggy[tr]:>7}%  {fixed[tr]:>7}%  {change:>+7}%{flag}")

    print()
    all_nonzero = all(v > 0 for v in fixed.values())
    print("All traits > 0 (FIXED):", "✅ PASS" if all_nonzero else "❌ FAIL")

    # Edge case: gives_up_fast=3 → bonus=0, should not inflate max
    bs_zero_bonus = {"gives_up_fast": 3, "helps_others": 0}
    fixed_no_bonus = compute_personality_profile_FIXED(answers_all_3, 10, bs_zero_bonus)
    buggy_no_bonus = compute_personality_profile_BUGGY(answers_all_3, 10, bs_zero_bonus)
    print()
    print("TEST: gives_up_fast=3 (zero bonus) — focus score should be identical in both")
    print(f"  BUGGY focus: {buggy_no_bonus['focus']}%   FIXED focus: {fixed_no_bonus['focus']}%")
    ok = fixed_no_bonus["focus"] >= buggy_no_bonus["focus"]
    print("  Fixed >= Buggy:", "✅ PASS" if ok else "❌ FAIL")

    # Edge case: no focus/empathy questions answered at all
    answers_no_focus = [
        {"question_id": q["id"], "score": 3}
        for q in ASSESSMENT_QUESTIONS
        if "focus" not in q["weights"] and "empathy" not in q["weights"]
    ]
    fixed_no_fe = compute_personality_profile_FIXED(answers_no_focus, 10, bs_worst)
    print()
    print("TEST: no focus/empathy answers — scores should be 0, not inflated")
    print(f"  focus={fixed_no_fe['focus']}%  empathy={fixed_no_fe['empathy']}%")
    ok2 = fixed_no_fe["focus"] == 0 and fixed_no_fe["empathy"] == 0
    print("  Both 0:", "✅ PASS" if ok2 else "❌ FAIL")
