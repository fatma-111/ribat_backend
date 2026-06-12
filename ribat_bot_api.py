# ════════════════════════════════════════════════════════════════════════════
# HOW TO ADD THIS TO main.py (ribat_bot_api.py)
# ────────────────────────────────────────────────────────────────────────────
# Find this existing line near the BOTTOM of your file:
#
#     # ROUTES — CHAT (Main)
#
# Paste EVERYTHING below this comment block DIRECTLY ABOVE that line.
# Do NOT paste it at the top of the file — app must already be defined.
# ════════════════════════════════════════════════════════════════════════════


# ──────────────────────────────────────────────
# ROUTES — PARENTING PLAN
# ──────────────────────────────────────────────

@app.post("/generate-parenting-plan/{user_id}", tags=["Parenting Plan"])
def generate_parenting_plan(user_id: str):
    """
    Generate a personalised 30-day parenting plan from the user's latest
    assessment result, persist it in `parenting_plans`, then push a
    Firebase notification to the user's device.
    """

    # ── 1. Require Gemini ──────────────────────────────────────────────
    if not GEMINI_ENABLED or client is None:
        raise HTTPException(
            status_code=503,
            detail="Gemini is disabled. Set GEMINI_API_KEY to use this feature."
        )

    conn = get_conn()
    try:
        # ── 2. Ensure user exists ──────────────────────────────────────
        ensure_user_exists(conn, user_id)
        cur = conn.cursor()

        # ── 3. Fetch latest assessment ─────────────────────────────────
        cur.execute(
            """
            SELECT id, child_age, assessment_confidence, result, created_at
            FROM   assessments
            WHERE  user_id = %s
            ORDER  BY created_at DESC
            LIMIT  1
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No assessment found for user '{user_id}'. "
                    "Please complete an assessment first via POST /assessment/submit."
                )
            )

        assessment_id, child_age, assessment_confidence, result_raw, assessed_at = row

        # ── 4. Parse result JSON ───────────────────────────────────────
        try:
            result: Dict[str, Any] = (
                json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse assessment result JSON: {exc}"
            )

        top_traits             = result.get("top_traits", [])
        possible_personalities = result.get("possible_personalities", [])
        trait_scores           = result.get("trait_scores", {})

        # ── 5. Build Gemini prompt ─────────────────────────────────────
        top_archetype   = (
            possible_personalities[0].get("name", "غير محدد")
            if possible_personalities else "غير محدد"
        )
        archetype_desc  = (
            possible_personalities[0].get("description", "")
            if possible_personalities else ""
        )
        archetype_needs = (
            possible_personalities[0].get("needs", "")
            if possible_personalities else ""
        )

        traits_text = "\n".join(
            f"  - {t['trait'].replace('_', ' ').title()}: {t['score']}%"
            for t in top_traits
        ) or "  - لا توجد بيانات كافية"

        scores_text = "\n".join(
            f"  - {k.replace('_', ' ').title()}: {v}%"
            for k, v in trait_scores.items()
        ) or "  - لا توجد بيانات كافية"

        prompt = f"""أنت مدرب تربوي محترف متخصص في التطوير الشخصي للأطفال.

فيما يلي نتائج تقييم شخصية الطفل:
- عمر الطفل: {child_age if child_age is not None else 'غير محدد'} سنة
- النمط الشخصي الأبرز: {top_archetype} — {archetype_desc}
- احتياجات الطفل: {archetype_needs}

أبرز الصفات:
{traits_text}

درجات جميع الصفات:
{scores_text}

المطلوب:
أنشئ خطة تربوية مخصصة لمدة 30 يومًا (4 أسابيع) بناءً على هذه البيانات.
يجب أن تتضمن الخطة:
1. هدف الأسبوع (لكل أسبوع من الأربعة)
2. أنشطة يومية عملية ومناسبة لعمر الطفل
3. أساليب التعزيز الإيجابي المقترحة لكل أسبوع
4. توصيات خاصة بالوالدين لدعم الطفل
5. ملاحظة ختامية للمتابعة بعد انتهاء الخطة

الأسلوب: دافئ، واضح، وعملي. تجنب المصطلحات الطبية أو التشخيصية.
أعد الخطة كاملةً باللغة العربية."""

        # ── 6. Call Gemini ─────────────────────────────────────────────
        try:
            gemini_response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.6,
                    max_output_tokens=2000,
                ),
            )
            plan_text: str = (gemini_response.text or "").strip()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Gemini API error while generating plan: {exc}"
            )

        if not plan_text:
            raise HTTPException(
                status_code=502,
                detail="Gemini returned an empty plan. Please try again."
            )

        # ── 7. Persist plan ────────────────────────────────────────────
        try:
            cur.execute(
                """
                INSERT INTO parenting_plans (user_id, plan_text, created_at)
                VALUES (%s, %s, NOW())
                RETURNING id, created_at
                """,
                (user_id, plan_text)
            )
            plan_row        = cur.fetchone()
            conn.commit()
            plan_id         = plan_row[0]
            plan_created_at = plan_row[1].isoformat() if plan_row[1] else None
        except Exception as exc:
            conn.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Database error while saving plan: {exc}"
            )

        # ── 8. Log analytics event ─────────────────────────────────────
        log_event(
            conn, user_id,
            "parenting_plan_generated",
            value=f"plan_id={plan_id}, assessment_id={assessment_id}"
        )

        # ── 9. Firebase push notification ──────────────────────────────
        notification_sent    = False
        notification_warning = None

        if FIREBASE_ENABLED:
            cur.execute(
                "SELECT fcm_token FROM users WHERE user_id = %s",
                (user_id,)
            )
            token_row  = cur.fetchone()
            fcm_token: Optional[str] = token_row[0] if token_row else None

            if not fcm_token:
                notification_warning = (
                    "No FCM token registered for this user — "
                    "plan saved but no push notification sent. "
                    "Call POST /register-token to enable notifications."
                )
            else:
                try:
                    message = fb_messaging.Message(
                        notification=fb_messaging.Notification(
                            title="🎯 خطة تربوية جديدة",
                            body="تم إنشاء خطة مخصصة لطفلك بناءً على نتائج التقييم.",
                        ),
                        token=fcm_token,
                        data={
                            "user_id": user_id,
                            "type":    "parenting_plan",
                            "plan_id": str(plan_id),
                        },
                    )
                    fb_messaging.send(message)
                    notification_sent = True
                except fb_messaging.UnregisteredError:
                    # Stale token — clear it so we don't retry
                    cur.execute(
                        "UPDATE users SET fcm_token = NULL WHERE user_id = %s",
                        (user_id,)
                    )
                    conn.commit()
                    notification_warning = (
                        "FCM token is no longer valid (device unregistered). "
                        "Token cleared — please re-register via POST /register-token."
                    )
                except Exception as fb_exc:
                    notification_warning = f"Firebase send error: {fb_exc}"
        else:
            notification_warning = (
                "Firebase is not configured — plan saved but no push notification sent."
            )

        # ── 10. Return response ────────────────────────────────────────
        response: Dict[str, Any] = {
            "ok":                True,
            "user_id":           user_id,
            "plan_generated":    True,
            "plan_id":           plan_id,
            "created_at":        plan_created_at,
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
    """Return all saved parenting plans for a user, newest first. limit capped at 50."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
        cur.execute(
            """
            SELECT id, plan_text, created_at
            FROM   parenting_plans
            WHERE  user_id = %s
            ORDER  BY created_at DESC
            LIMIT  %s
            """,
            (user_id, max(1, min(50, limit)))
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id,
            "total":   len(rows),
            "plans": [
                {
                    "id":         r[0],
                    "plan_text":  r[1],
                    "created_at": r[2].isoformat() if r[2] else None,
                }
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# ↓↓↓  EXISTING CODE CONTINUES BELOW — do not delete anything below here  ↓↓↓
# ════════════════════════════════════════════════════════════════════════════
