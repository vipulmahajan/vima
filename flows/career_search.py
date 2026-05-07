"""Career Search flow — Sub-stages 1A (Clarity) + 1B (Target Role Definition).

States (stored in user_state.interview_step, reusing that field for career_search
to avoid schema changes; distinguished by flow='career_search'):

  WELCOME            → Introduce Career Search stage; advance to Q1.
  Q1                 → Push/pull factor — what's driving the move.
  Q2                 → Current role, title, company, tenure, team.
  Q3                 → What gives energy (strengths enjoyed, not just had).
  Q4                 → Anti-patterns — what they'd never want again.
  Q5                 → Initial target — role, level, company type, sector.
  Q6                 → Constraints — CTC range, geography, notice period.
  Q7                 → Timeline and urgency.
  PROFILE_CONFIRM    → Read back Career Profile Summary; await user confirmation.
  TARGET_GENERATING  → Generate target role cards; send to user.
  TARGET_REVIEW      → User selects, refines, or rejects target roles.
  COMPLETE           → Target confirmed; cross-stage context saved.

All collected answers are stored in user_state.data.career_profile (dict).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from models.database import (
    get_user_state,
    upsert_user_state,
    merge_user_state_data,
)
from services.claude_service import (
    ClaudeService,
    ClaudeUnavailable,
    CLAUDE_EXHAUSTED_MSG,
)
from services.messenger import get_messenger

log = logging.getLogger(__name__)


# ── State constants ─────────────────────────────────────────────────────────
WELCOME           = "welcome"
Q1                = "cs_q1"   # push / pull factor
Q2                = "cs_q2"   # current situation
Q3                = "cs_q3"   # what's working
Q4                = "cs_q4"   # what isn't working
Q5                = "cs_q5"   # initial target
Q6                = "cs_q6"   # constraints
Q7                = "cs_q7"   # timeline / urgency
PROFILE_CONFIRM   = "cs_profile_confirm"
TARGET_GENERATING = "cs_target_generating"
TARGET_REVIEW     = "cs_target_review"
COMPLETE          = "cs_complete"

INITIAL_STEP = WELCOME

# Step name used in state storage (reuses interview_step column).
_STEP_KEY = "interview_step"

# All active steps (not terminal).
ACTIVE_STEPS = {Q1, Q2, Q3, Q4, Q5, Q6, Q7, PROFILE_CONFIRM,
                TARGET_GENERATING, TARGET_REVIEW}

# Phrases that mean "yes, looks right" in PROFILE_CONFIRM.
_CONFIRM_PHRASES = {
    "yes", "correct", "right", "looks good", "good", "accurate",
    "that's right", "thats right", "perfect", "confirmed", "confirm",
    "proceed", "continue", "go ahead", "next", "yep", "yup",
}

# Phrases that mean "this is my target" in TARGET_REVIEW.
_TARGET_CONFIRM_PHRASES = {
    "a", "1", "role a", "target a", "option a",
    "b", "2", "role b", "target b", "option b",
    "c", "3", "role c", "target c", "option c",
    "this", "this one", "confirmed", "confirm", "that's my target",
    "thats my target", "looks good", "go with this", "i'll go with this",
}


# ── Public entry point ──────────────────────────────────────────────────────

async def handle(
    sender: str,
    message: dict[str, Any],
    state: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Drive the career search conversation one step forward."""
    step = state.get(_STEP_KEY) or INITIAL_STEP
    text = (message.get("text") or "").strip()

    log.info("career_search.handle: user=%s step=%s", _mask(sender), step)

    if step == WELCOME:
        return await _handle_welcome(sender, message, text)
    if step == Q1:
        return await _handle_q1(sender, text)
    if step == Q2:
        return await _handle_q2(sender, text)
    if step == Q3:
        return await _handle_q3(sender, text)
    if step == Q4:
        return await _handle_q4(sender, text)
    if step == Q5:
        return await _handle_q5(sender, text)
    if step == Q6:
        return await _handle_q6(sender, text)
    if step == Q7:
        return await _handle_q7(sender, text)
    if step == PROFILE_CONFIRM:
        return await _handle_profile_confirm(sender, text)
    if step == TARGET_GENERATING:
        # Shouldn't receive input here — regenerate if stuck.
        return await _generate_targets(sender)
    if step == TARGET_REVIEW:
        return await _handle_target_review(sender, text)
    if step == COMPLETE:
        return _text(sender,
            "Your target is confirmed. Reply *resume* to build a tailored resume, "
            "*interview* to prep for interviews, or *menu* to see all options."
        )

    # Fallback.
    return _text(sender, _q1_text())


# ── State handlers ──────────────────────────────────────────────────────────

async def _handle_welcome(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Introduce Career Search and fire Q1."""
    await upsert_user_state(sender, {_STEP_KEY: Q1})
    return _text(sender,
        "I'll help you get clear on what you're targeting and why — "
        "so your resume, interviews, and outreach are all pointed in the right direction.\n\n"
        + _q1_text()
    )


async def _handle_q1(sender: str, text: str) -> dict[str, Any]:
    if not text or len(text) < 10:
        return _text(sender, _q1_text())

    await _save_profile(sender, {"push_pull": text})
    await upsert_user_state(sender, {_STEP_KEY: Q2})
    return _text(sender, _q2_text())


async def _handle_q2(sender: str, text: str) -> dict[str, Any]:
    if not text or len(text) < 10:
        return _text(sender, _q2_text())

    await _save_profile(sender, {"current_role": text})
    await upsert_user_state(sender, {_STEP_KEY: Q3})
    return _text(sender, _q3_text())


async def _handle_q3(sender: str, text: str) -> dict[str, Any]:
    if not text or len(text) < 10:
        return _text(sender, _q3_text())

    await _save_profile(sender, {"energising_work": text})
    await upsert_user_state(sender, {_STEP_KEY: Q4})
    return _text(sender, _q4_text())


async def _handle_q4(sender: str, text: str) -> dict[str, Any]:
    if not text or len(text) < 10:
        return _text(sender, _q4_text())

    await _save_profile(sender, {"anti_patterns": text})
    await upsert_user_state(sender, {_STEP_KEY: Q5})
    return _text(sender, _q5_text())


async def _handle_q5(sender: str, text: str) -> dict[str, Any]:
    if not text or len(text) < 5:
        return _text(sender, _q5_text())

    await _save_profile(sender, {"initial_target": text})
    await upsert_user_state(sender, {_STEP_KEY: Q6})
    return _text(sender, _q6_text())


async def _handle_q6(sender: str, text: str) -> dict[str, Any]:
    if not text or len(text) < 5:
        return _text(sender, _q6_text())

    # Parse out CTC / geography / notice period from free text — store raw.
    await _save_profile(sender, {"constraints": text})
    await upsert_user_state(sender, {_STEP_KEY: Q7})
    return _text(sender, _q7_text())


async def _handle_q7(sender: str, text: str) -> dict[str, Any]:
    if not text or len(text) < 5:
        return _text(sender, _q7_text())

    urgency = "active" if any(w in text.lower() for w in
                               ("active", "actively", "now", "urgent", "immediately")) \
              else "exploratory"
    await _save_profile(sender, {"timeline_urgency": text, "urgency": urgency})
    await upsert_user_state(sender, {_STEP_KEY: PROFILE_CONFIRM})

    # Generate the Career Profile Summary via Claude, then present it.
    messenger = await get_messenger(sender)
    try:
        await messenger.send_text(sender,
            "Let me pull together what you've shared — give me a moment..."
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("career_search: ack send failed: %s", exc)

    return await _send_profile_summary(sender)


async def _send_profile_summary(sender: str) -> dict[str, Any]:
    state = await get_user_state(sender)
    data  = state.get("data") or {}
    profile = data.get("career_profile") or {}

    claude = ClaudeService()
    try:
        summary = await claude.generate_career_profile_summary(
            career_profile=profile,
            sender_phone=sender,
        )
    except ClaudeUnavailable:
        summary = _fallback_profile_summary(profile)
    except Exception as exc:  # noqa: BLE001
        log.warning("career_search: generate_career_profile_summary failed: %s", exc)
        summary = _fallback_profile_summary(profile)

    await _save_profile(sender, {"summary": summary})

    return _text(sender,
        f"{summary}\n\n"
        "Does this capture it accurately? "
        "Reply *yes* to continue, or correct anything you'd like to change."
    )


async def _handle_profile_confirm(sender: str, text: str) -> dict[str, Any]:
    t = text.lower().strip()

    if t in _CONFIRM_PHRASES or len(t) < 4 and t in {"y", "ok", "k"}:
        await _save_profile(sender, {"confirmed": True})
        await upsert_user_state(sender, {_STEP_KEY: TARGET_GENERATING})
        return await _generate_targets(sender)

    # User wants to correct something — treat their message as an amendment,
    # update the raw profile, and re-generate the summary.
    await _save_profile(sender, {"amendment": text, "confirmed": False})
    messenger = await get_messenger(sender)
    try:
        await messenger.send_text(sender, "Got it — let me update your profile...")
    except Exception as exc:  # noqa: BLE001
        log.warning("career_search: amendment ack failed: %s", exc)
    return await _send_profile_summary(sender)


async def _generate_targets(sender: str) -> dict[str, Any]:
    """Call Claude to generate 2-3 target role cards, then present them."""
    state = await get_user_state(sender)
    data  = state.get("data") or {}
    profile = data.get("career_profile") or {}

    messenger = await get_messenger(sender)
    try:
        await messenger.send_text(sender,
            "Analysing your profile and identifying the strongest role archetypes for you..."
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("career_search: target gen ack failed: %s", exc)

    claude = ClaudeService()
    try:
        roles = await claude.generate_target_roles(
            career_profile=profile,
            sender_phone=sender,
        )
    except ClaudeUnavailable:
        return _text(sender, CLAUDE_EXHAUSTED_MSG)
    except Exception as exc:  # noqa: BLE001
        log.exception("career_search: generate_target_roles failed: %s", exc)
        return _text(sender, CLAUDE_EXHAUSTED_MSG)

    # Check gap on Q5 initial target — optional challenge step.
    initial_target = profile.get("initial_target") or ""
    if initial_target:
        try:
            gap = await claude.assess_gap(
                career_profile=profile,
                target_role=initial_target,
                sender_phone=sender,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("career_search: assess_gap failed: %s", exc)
            gap = {"size": "NONE", "rationale": ""}
    else:
        gap = {"size": "NONE", "rationale": ""}

    # Store roles and gap in state.
    await merge_user_state_data(sender, {
        "target_roles": roles,
        "gap_assessment": gap,
    })
    await upsert_user_state(sender, {_STEP_KEY: TARGET_REVIEW})

    # If gap is LARGE, send challenge message before the role cards.
    if gap.get("size") == "LARGE":
        try:
            challenge_msg = await claude.generate_challenge_message(
                career_profile=profile,
                target_role=initial_target,
                gap_rationale=gap.get("rationale", ""),
                sender_phone=sender,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("career_search: generate_challenge_message failed: %s", exc)
            challenge_msg = ""

        if challenge_msg:
            try:
                await messenger.send_text(sender, challenge_msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("career_search: challenge msg send failed: %s", exc)

    # Push target role cards via WebMessenger event, or format as text for WhatsApp.
    return await _deliver_target_roles(sender, roles)


async def _deliver_target_roles(
    sender: str,
    roles: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Push target role cards to web; format as structured text for WhatsApp."""
    if not roles:
        return _text(sender,
            "I wasn't able to generate role options right now. "
            "Reply *retry* and I'll try again."
        )

    # Web channel — push a structured event; the frontend renders role cards.
    if "@" in sender:
        messenger = await get_messenger(sender)
        try:
            await messenger.send({
                "to":    sender,
                "type":  "target_role",
                "roles": roles,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("career_search: target_role event push failed: %s", exc)
            # Fallback to text.
            await messenger.send(_text(sender, _format_roles_text(roles)))
        return None

    # WhatsApp — plain text.
    return _text(sender, _format_roles_text(roles))


def _format_roles_text(roles: list[dict[str, Any]]) -> str:
    """Format target role cards as WhatsApp-friendly text."""
    lines = ["Here are your strongest target role options:\n"]
    labels = ["A", "B", "C"]
    for i, role in enumerate(roles[:3]):
        label = labels[i] if i < len(labels) else str(i + 1)
        title   = role.get("title", "")
        sector  = role.get("sector", "")
        why     = role.get("why_fits", "")
        stretch = role.get("stretch", "")
        comp    = role.get("competition", "")
        timeline = role.get("timeline", "")
        archetypes = role.get("company_archetypes", "")
        ctc     = role.get("ctc_range", "")

        heading = f"*{label}. {title}"
        if sector:
            heading += f" — {sector}"
        heading += "*"
        lines.append(heading)
        if why:
            lines.append(why)
        parts = []
        if stretch:
            parts.append(f"Stretch: {stretch}")
        if comp:
            parts.append(f"Competition: {comp}")
        if timeline:
            parts.append(f"Timeline: {timeline}")
        if parts:
            lines.append(" · ".join(parts))
        if archetypes:
            lines.append(f"Best companies: {archetypes}")
        if ctc:
            lines.append(f"CTC range: {ctc}")
        lines.append("")

    lines.append(
        "Reply *A*, *B*, or *C* to confirm your target, "
        "or describe any adjustments you'd like."
    )
    return "\n".join(lines)


async def _handle_target_review(sender: str, text: str) -> dict[str, Any]:
    t = text.lower().strip()
    state = await get_user_state(sender)
    data  = state.get("data") or {}
    roles = data.get("target_roles") or []

    # Map letter / number to role index.
    role_index: Optional[int] = None
    if t in ("a", "1", "role a", "target a", "option a") and len(roles) >= 1:
        role_index = 0
    elif t in ("b", "2", "role b", "target b", "option b") and len(roles) >= 2:
        role_index = 1
    elif t in ("c", "3", "role c", "target c", "option c") and len(roles) >= 3:
        role_index = 2
    elif t in ("this", "this one", "confirmed", "confirm", "looks good",
               "go with this", "i'll go with this") and roles:
        role_index = 0

    if role_index is not None:
        confirmed_role = roles[role_index]
        confirmed_role["confirmed"] = True
        await merge_user_state_data(sender, {
            "target_roles": [r if i != role_index else confirmed_role
                             for i, r in enumerate(roles)],
        })
        return await _complete(sender, confirmed_role)

    # User wants adjustments — treat as amendment, regenerate.
    await _save_profile(sender, {"target_amendment": text})
    await upsert_user_state(sender, {_STEP_KEY: TARGET_GENERATING})
    messenger = await get_messenger(sender)
    try:
        await messenger.send_text(sender, "Got it — let me adjust the options...")
    except Exception as exc:  # noqa: BLE001
        log.warning("career_search: target amendment ack failed: %s", exc)
    return await _generate_targets(sender)


async def _complete(sender: str, confirmed_role: dict[str, Any]) -> dict[str, Any]:
    """Mark career search complete; inject confirmed target into cross-stage fields."""
    target_title  = confirmed_role.get("title", "")
    target_sector = confirmed_role.get("sector", "")
    target_label  = f"{target_title}, {target_sector}".strip(", ")

    # Cross-stage context injection: pre-populate current_role_target so the
    # resume and interview flows can skip asking about the target role.
    await merge_user_state_data(sender, {
        "current_role_target": target_label,
        "target_confirmed":    True,
    })
    await upsert_user_state(sender, {"flow": "idle", _STEP_KEY: COMPLETE})

    return _text(sender,
        f"Excellent — *{target_label}* is your confirmed target. "
        "I've saved this so your resume and interview prep are already aligned.\n\n"
        "What would you like to do next?\n\n"
        "*1.* Build a tailored resume for this role\n"
        "*2.* Prepare for interviews\n"
        "*3.* Back to main menu"
    )


# ── Claude-service calls ────────────────────────────────────────────────────

# (Implemented as methods on ClaudeService — called above via claude.<method>)


# ── Static question texts ────────────────────────────────────────────────────

def _q1_text() -> str:
    return (
        "What's making you consider a move right now?\n\n"
        "Something you're running from, something you're running toward — or both?\n"
        "Be as honest as you like — this is just between us."
    )


def _q2_text() -> str:
    return (
        "Tell me about your current role — your title, company, "
        "how long you've been there, and roughly what your team looks like."
    )


def _q3_text() -> str:
    return (
        "Even if you're ready to leave, something's probably been worth staying for. "
        "What parts of your current work give you the most energy?"
    )


def _q4_text() -> str:
    return (
        "What would you never want to do again in your next role?\n"
        "Be honest — this shapes everything we build together."
    )


def _q5_text() -> str:
    return (
        "If you could describe your next role in one sentence — even roughly — "
        "what does it look like? Role, level, company type, sector?"
    )


def _q6_text() -> str:
    return (
        "A few practical questions: What CTC range are you targeting? "
        "Any geographic constraints? And what's your notice period?"
    )


def _q7_text() -> str:
    return (
        "Are you actively looking now, or is this more exploratory?\n"
        "Is there any urgency driving this — a deadline, a situation at work, "
        "or something else?"
    )


# ── Profile storage helper ──────────────────────────────────────────────────

async def _save_profile(sender: str, updates: dict[str, Any]) -> None:
    """Shallow-merge updates into user_state.data.career_profile."""
    state = await get_user_state(sender)
    data  = state.get("data") or {}
    profile = dict(data.get("career_profile") or {})
    profile.update(updates)
    await merge_user_state_data(sender, {"career_profile": profile})


# ── Fallback ────────────────────────────────────────────────────────────────

def _fallback_profile_summary(profile: dict[str, Any]) -> str:
    """Minimal profile summary used when Claude generation fails."""
    role    = profile.get("current_role") or "your current role"
    target  = profile.get("initial_target") or "a new role"
    urgency = profile.get("urgency") or "exploratory"
    return (
        f"Here's what I've captured: You're currently in {role} and targeting {target}. "
        f"Your search is {urgency}. "
        "Does this capture it accurately? Anything to correct or add?"
    )


# ── Tiny helpers ────────────────────────────────────────────────────────────

def _text(to: str, body: str) -> dict[str, Any]:
    return {"to": to, "type": "text", "text": body}


def _mask(user: str) -> str:
    if not user or len(user) < 4:
        return "***"
    if "@" in user:
        parts = user.split("@")
        return f"{parts[0][:3]}***@{parts[1]}"
    return f"{user[:5]}****{user[-3:]}"


__all__ = [
    "handle", "INITIAL_STEP", "WELCOME", "Q1", "Q2", "Q3", "Q4", "Q5",
    "Q6", "Q7", "PROFILE_CONFIRM", "TARGET_GENERATING", "TARGET_REVIEW",
    "COMPLETE", "ACTIVE_STEPS", "_STEP_KEY",
]
