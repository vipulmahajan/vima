"""Interview preparation flow.

Two phases:

  1. DISCOVERY — collect just enough context to build a sharp prep kit.
     If the user already went through the resume flow we re-use that
     context (target role, JD, hiring manager, resume sources) and skip
     the redundant questions, jumping straight to interview-specific Q1.

  2. PREP KIT GENERATION — call Claude to produce a structured prep kit,
     render it to PDF + DOCX, deliver both, then offer a mock interview.

States (stored in user_state.interview_step):

  WELCOME            → Acknowledge resume context (if present) and start.
                       If no resume context, ask for role/JD/company first.
  AWAIT_ROLE         → (only when no resume context) capture role + level.
  AWAIT_COMPANY      → (only when no resume context) capture target company.
  AWAIT_JD           → (only when no resume context) capture JD url/file/skip.
  Q1_ROUND           → Which interview round (numbered list).
  Q2_PRIOR           → Any prior experience interviewing at this company.
  Q3_INTERVIEWER     → Who's taking the interview (names / LinkedIn / skip).
  Q4_CONCERN         → Biggest concern about this interview.
  PREP_GENERATING    → Ack + call Claude + render + deliver both formats.
  PREP_DELIVERED     → Offer the mock-interview practice run.

  MOCK_IN_PROGRESS   → (existing) per-question mock interview turns.
  MOCK_FEEDBACK      → (existing) end-of-interview feedback.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from models.database import (
    get_user_state,
    upsert_user_state,
    merge_user_state_data,
)
from flows._redirect import warm_reprompt
from services.claude_service import (
    ClaudeService,
    ClaudeUnavailable,
    CLAUDE_EXHAUSTED_MSG,
)
from config import settings
from services.payment_service import PaymentService
from services.pdf_service import (
    render_interview_prep_pdf,
    render_interview_prep_docx,
)
from services.voice_service import transcribe_voice_note
from services.messenger import get_messenger

log = logging.getLogger(__name__)


# ── State constants ─────────────────────────────────────────────────────────
WELCOME            = "welcome"
AWAIT_ROLE         = "await_role"
AWAIT_COMPANY      = "await_company"
AWAIT_JD           = "await_jd"
Q1_ROUND           = "q1_round"
Q2_PRIOR           = "q2_prior"
Q3_INTERVIEWER     = "q3_interviewer"
Q4_CONCERN         = "q4_concern"
PREP_GENERATING    = "prep_generating"
AWAITING_PAYMENT   = "awaiting_payment"
PREP_DELIVERED     = "prep_delivered"

MOCK_IN_PROGRESS   = "mock_in_progress"
MOCK_FEEDBACK      = "mock_feedback"

INITIAL_STEP = WELCOME

# Numbered round → (label, type)
ROUNDS: dict[str, tuple[str, str]] = {
    "1": ("Screening call",         "screening"),
    "2": ("Technical / Functional", "technical"),
    "3": ("Hiring manager",         "hiring_manager"),
    "4": ("Leadership / Culture",   "leadership"),
    "5": ("HR final",               "hr_final"),
}

LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/\S+", re.IGNORECASE)
URL_RE      = re.compile(r"https?://\S+", re.IGNORECASE)

SKIP_WORDS = {"skip", "no", "none", "n/a", "na", "no idea", "dont know", "don't know"}


# ── Public entry ────────────────────────────────────────────────────────────

async def handle(
    sender: str,
    message: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    step = state.get("interview_step") or INITIAL_STEP
    text = (message.get("text") or "").strip()

    log.info("interview.handle: phone=%s step=%s msg_type=%s",
             _mask(sender), step, message.get("type"))

    if step == WELCOME:           return await _handle_welcome(sender, message, text)
    if step == AWAIT_ROLE:        return await _handle_await_role(sender, message, text)
    if step == AWAIT_COMPANY:     return await _handle_await_company(sender, message, text)
    if step == AWAIT_JD:          return await _handle_await_jd(sender, message, text)
    if step == Q1_ROUND:          return await _handle_q1_round(sender, message, text)
    if step == Q2_PRIOR:          return await _handle_q2_prior(sender, message, text)
    if step == Q3_INTERVIEWER:    return await _handle_q3_interviewer(sender, message, text)
    if step == Q4_CONCERN:        return await _handle_q4_concern(sender, message, text)
    if step == PREP_GENERATING:   return await _handle_prep_generating(sender, message, text)
    if step == AWAITING_PAYMENT:
        if text.lower().strip() == "retry":
            return _text(sender, await _payment_gate_body(sender))
        return _text(sender,
            "I'm waiting for your payment to come through — once it does, your "
            "interview prep will arrive automatically. If you've already paid, "
            "give it a minute and reply *menu* if nothing arrives."
        )
    if step == PREP_DELIVERED:    return await _handle_prep_delivered(sender, message, text)

    if step == MOCK_IN_PROGRESS:  return await _handle_mock_turn(sender, message, text)
    if step == MOCK_FEEDBACK:     return await _handle_mock_feedback(sender, message, text)

    return _text(sender,
        "Reply *interview* to start a new interview prep, or *menu* for options.")


# ── Discovery: WELCOME ──────────────────────────────────────────────────────

async def _handle_welcome(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    """Branch on whether the user already has resume context."""
    state = await get_user_state(sender)
    data  = state.get("data") or {}

    has_resume_ctx = bool(
        data.get("current_role_target")
        or data.get("resume_sources")
        or data.get("jd_text")
        or data.get("jd_url")
    )

    if has_resume_ctx:
        # Reuse what we already know. Pre-seed the interview-side fields.
        await merge_user_state_data(sender, {
            "iv_target_role": data.get("current_role_target"),
            "iv_jd_text":     data.get("jd_text"),
            "iv_jd_url":      data.get("jd_url"),
            "iv_company":     _guess_company_from_data(data),
        })
        await upsert_user_state(sender, {"interview_step": Q1_ROUND})

        target = data.get("current_role_target") or "your target role"
        company = _guess_company_from_data(data)
        ack = (
            f"I've got context from your resume work — I know you're targeting "
            f"*{target}*"
            f"{f' at *{company}*' if company else ''}, "
            "and I've got the JD and your background. Let's go straight into "
            "interview-specific questions."
        )
        return _text(sender, f"{ack}\n\n{_q1_body()}")

    # Fresh entry — collect role first.
    await upsert_user_state(sender, {"interview_step": AWAIT_ROLE})
    return _text(sender,
        "Welcome — I'll help you walk into this interview prepared and calm.\n\n"
        "First, what *role and seniority* are you interviewing for?\n"
        "Example: _Senior PM, Director PM, VP Engineering._"
    )


# ── Discovery: fresh-entry questions ────────────────────────────────────────

async def _handle_await_role(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    if not text or len(text) < 3:
        return _text(sender, "Tell me the role and seniority you're interviewing for.")
    await merge_user_state_data(sender, {"iv_target_role": text})
    await upsert_user_state(sender, {"interview_step": AWAIT_COMPANY})
    return _text(sender, "Which *company* is the interview with?")


async def _handle_await_company(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    if not text or len(text) < 2:
        return _text(sender, "Which company is the interview with?")
    await merge_user_state_data(sender, {"iv_company": text})
    await upsert_user_state(sender, {"interview_step": AWAIT_JD})
    return _text(sender,
        "Got it. Share the *JD* if you have one — paste the link, send a "
        "PDF, or reply *skip* if you don't have it."
    )


async def _handle_await_jd(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    if text.lower().strip() in SKIP_WORDS:
        await merge_user_state_data(sender, {"iv_jd_skipped": True})
        await upsert_user_state(sender, {"interview_step": Q1_ROUND})
        return _text(sender, _q1_body())

    if message.get("type") in ("document", "image") and message.get("media_url"):
        # Inline import to keep this flow's surface small.
        from services.document_parser import extract_text
        try:
            jd_text = await extract_text(
                message["media_url"],
                mime_type=message.get("mime_type"),
                filename=message.get("filename"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("JD extract failed: %s", exc)
            jd_text = ""

        if jd_text and len(jd_text) >= 80:
            await merge_user_state_data(sender, {"iv_jd_text": jd_text})
            await upsert_user_state(sender, {"interview_step": Q1_ROUND})
            return _text(sender, _q1_body())

        return _text(sender,
            "I'm having trouble reading that file — could you try again, "
            "or paste the JD text directly? You can also share the link, "
            "or reply *skip*."
        )

    url = URL_RE.search(text)
    if url:
        await merge_user_state_data(sender, {"iv_jd_url": url.group(0).rstrip(".,);")})
        await upsert_user_state(sender, {"interview_step": Q1_ROUND})
        return _text(sender, _q1_body())

    return _text(sender,
        "Share the JD as a PDF, paste the link, or reply *skip*."
    )


# ── Discovery: Q1-Q4 ────────────────────────────────────────────────────────

async def _handle_q1_round(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    code = _parse_round_choice(text)
    if not code:
        question = (
            "Pick the round you're prepping for — reply with a number:\n\n"
            + _round_menu_lines()
        )
        return _text(sender, warm_reprompt(text, question, flow="interview"))
    label, kind = ROUNDS[code]
    await merge_user_state_data(sender, {
        "iv_round_code": code,
        "iv_round":      kind,
        "iv_round_label": label,
    })
    await upsert_user_state(sender, {"interview_step": Q2_PRIOR})
    return _text(sender,
        f"Got it — *{label}*.\n\n"
        "Have you *interviewed at this company before*? If yes, share what "
        "you remember (round you got to, what stuck out). If not, just say *no*."
    )


async def _handle_q2_prior(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    if not text:
        return _text(sender, "Have you interviewed at this company before? If not, say *no*.")
    await merge_user_state_data(sender, {"iv_prior_experience": text})
    await upsert_user_state(sender, {"interview_step": Q3_INTERVIEWER})
    return _text(sender,
        "Who's *taking the interview* — share their *names* or *LinkedIn URLs* "
        "(one per line is fine).\n\n"
        "Reply *skip* if you don't know yet."
    )


async def _handle_q3_interviewer(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    text_lower = text.lower().strip()
    if text_lower in SKIP_WORDS:
        await merge_user_state_data(sender, {"iv_interviewers": []})
        await upsert_user_state(sender, {"interview_step": Q4_CONCERN})
        return _text(sender, _q4_body())

    interviewers: list[dict[str, Any]] = []
    # Extract every LinkedIn URL.
    for m in LINKEDIN_RE.finditer(text):
        url = m.group(0).rstrip(".,);")
        interviewers.append({"linkedin_url": url})

    # Pull any name-like lines that aren't URLs.
    for line in text.splitlines():
        line = line.strip(" -•\t")
        if not line or LINKEDIN_RE.search(line):
            continue
        # Skip very short noise.
        if len(line) >= 3 and len(line) <= 120:
            interviewers.append({"name": line})

    if not interviewers:
        return _text(sender,
            "Send their LinkedIn URLs (one per line), names, or reply *skip*."
        )

    await merge_user_state_data(sender, {"iv_interviewers": interviewers})
    await upsert_user_state(sender, {"interview_step": Q4_CONCERN})
    return _text(sender, _q4_body())


async def _handle_q4_concern(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    if not text or len(text) < 5:
        question = (
            "What's your *biggest concern* about this interview? "
            "It could be a topic gap, a tough question you're dreading, "
            "imposter syndrome, anything. One or two sentences is fine."
        )
        return _text(sender, warm_reprompt(text, question, flow="interview"))
    await merge_user_state_data(sender, {"iv_concern": text})
    await upsert_user_state(sender, {"interview_step": PREP_GENERATING})
    return await _handle_prep_generating(sender, message, text)


# ── Prep generation + dual delivery ─────────────────────────────────────────

async def _handle_prep_generating(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    """Subscription gate, ack, generate prep kit, render PDF + DOCX, deliver both."""
    payments = PaymentService()
    if not await payments.is_subscribed(sender):
        await merge_user_state_data(sender, {"pending_action": "interview_prep"})
        await upsert_user_state(sender, {"interview_step": AWAITING_PAYMENT})
        return _text(sender, await _payment_gate_body(sender))

    # 1. Ack first so the user isn't waiting silent.
    messenger = await get_messenger(sender)
    try:
        await messenger.send_typing_indicator(sender)
        await messenger.send_text(
            sender,
            "Got everything I need. Building your prep kit — company brief, "
            "likely questions with model answers, smart questions to ask. "
            "Give me about a minute.",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("interview ack send failed: %s", exc)

    state = await get_user_state(sender)
    data  = state.get("data") or {}

    claude = ClaudeService()
    try:
        prep_json = await claude.generate_interview_prep(
            sender_phone        = sender,
            company             = data.get("iv_company"),
            target_role         = data.get("iv_target_role"),
            jd_text             = data.get("iv_jd_text") or data.get("jd_text"),
            jd_url              = data.get("iv_jd_url")  or data.get("jd_url"),
            round_label         = data.get("iv_round_label"),
            round_kind          = data.get("iv_round"),
            hiring_manager      = data.get("hiring_manager"),
            interviewers        = data.get("iv_interviewers") or [],
            prior_experience    = data.get("iv_prior_experience"),
            concern             = data.get("iv_concern"),
            resume_sources      = data.get("resume_sources"),
            superpower          = data.get("superpower"),
            company_research    = data.get("company_research"),
        )
    except ClaudeUnavailable:
        log.error("interview prep exhausted retries; pausing flow phone=%s", _mask(sender))
        # State stays at PREP_GENERATING so 'retry' re-runs this handler.
        return _text(sender, CLAUDE_EXHAUSTED_MSG)
    except Exception as exc:  # noqa: BLE001
        log.exception("generate_interview_prep unexpected error: %s", exc)
        return _text(sender, CLAUDE_EXHAUSTED_MSG)

    # Persist for retry / mock interview reuse.
    await merge_user_state_data(sender, {"interview_prep_json": prep_json})

    # 2. Render BOTH formats in-memory.
    pdf_bytes:  Optional[bytes] = None
    docx_bytes: Optional[bytes] = None

    try:
        pdf_bytes = render_interview_prep_pdf(prep_json or {})
    except Exception as exc:  # noqa: BLE001
        log.exception("Interview prep PDF render failed: %s", exc)

    try:
        docx_bytes = render_interview_prep_docx(prep_json or {}, template="executive")
    except Exception as exc:  # noqa: BLE001
        log.exception("Interview prep DOCX render failed: %s", exc)

    if not pdf_bytes and not docx_bytes:
        return _text(sender,
            "I drafted your prep kit but both PDF and Word renders failed. "
            "Reply *retry* and I'll try again."
        )

    # 3. Deliver via the channel-agnostic messenger. PDF first, DOCX follow-up.
    if pdf_bytes:
        try:
            await messenger.send_document(
                sender, pdf_bytes,
                filename="ViMa-Interview-Prep.pdf",
                caption="Your interview prep kit.",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Prep PDF delivery failed: %s", exc)

    if docx_bytes:
        try:
            await messenger.send_document(
                sender, docx_bytes,
                filename="ViMa-Interview-Prep.docx",
                caption=(
                    "And here's an editable Word version — feel free "
                    "to tweak anything before submitting."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Prep DOCX delivery failed: %s", exc)

    # 5. Mock interview offer as the final reply.
    await upsert_user_state(sender, {"interview_step": PREP_DELIVERED})
    return _text(sender,
        "Want to do a *practice run*? I'll ask you questions one at a time "
        "and give you feedback on each answer.\n\n"
        "Reply *yes* to start, or *menu* to come back to this later."
    )


# ── Post-prep: mock interview offer ─────────────────────────────────────────

async def _handle_prep_delivered(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    t = text.lower().strip()
    if t in {"yes", "y", "start", "go", "let's go", "lets go", "sure", "okay", "ok"}:
        await merge_user_state_data(sender, {"questions_asked": 0, "transcript": []})
        await upsert_user_state(sender, {"interview_step": MOCK_IN_PROGRESS})
        # Trigger the first question by entering MOCK_IN_PROGRESS with no answer.
        return await _handle_mock_turn(sender, message, "")

    if t in {"no", "later", "not now", "menu"}:
        return _text(sender,
            "No problem — your prep kit is ready when you need it. "
            "Reply *interview* anytime to do a practice run."
        )

    return _text(sender,
        "Reply *yes* to start a practice run, or *menu* to come back later."
    )


# ── Mock interview (existing behaviour, lightly cleaned up) ─────────────────

DEFAULT_QUESTION_COUNT = 6


async def _handle_mock_turn(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    state = await get_user_state(sender)
    data  = state.get("data") or {}

    answer_text = await _resolve_answer_text(sender, message)

    transcript: list[dict[str, str]] = list(data.get("transcript") or [])
    if answer_text:
        transcript.append({"role": "candidate", "text": answer_text})

    questions_asked = int(data.get("questions_asked") or 0)
    if questions_asked >= DEFAULT_QUESTION_COUNT:
        await upsert_user_state(sender, {"interview_step": MOCK_FEEDBACK})
        return await _handle_mock_feedback(sender, message, text)

    claude = ClaudeService()
    try:
        next_q = await claude.next_interview_question(
            {"target_role": data.get("iv_target_role"),
             "round":       data.get("iv_round_label"),
             "transcript":  transcript},
            answer_text,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("next_interview_question failed: %s", exc)
        next_q = "Walk me through your background."

    transcript.append({"role": "interviewer", "text": next_q})
    await merge_user_state_data(sender, {
        "transcript":      transcript,
        "questions_asked": questions_asked + 1,
    })

    return _text(sender, next_q or "Tell me about yourself.")


async def _handle_mock_feedback(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    state = await get_user_state(sender)
    data  = state.get("data") or {}
    claude = ClaudeService()
    try:
        feedback = await claude.interview_feedback({
            "target_role": data.get("iv_target_role"),
            "round":       data.get("iv_round_label"),
            "transcript":  data.get("transcript") or [],
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("interview_feedback failed: %s", exc)
        feedback = "Hit a snag generating feedback. Reply *retry* to try again."

    await upsert_user_state(sender, {"interview_step": PREP_DELIVERED})
    return _text(sender, feedback or "Reply *menu* anytime.")


# ── Static prompts ──────────────────────────────────────────────────────────

def _round_menu_lines() -> str:
    return "\n".join(
        f"*{code}.* {label}" for code, (label, _) in ROUNDS.items()
    )


def _q1_body() -> str:
    return (
        "Which *interview round* are you prepping for? Reply with the number:\n\n"
        + _round_menu_lines()
    )


def _q4_body() -> str:
    return (
        "What's your *biggest concern* about this interview? "
        "It could be a topic gap, a tough question you're dreading, "
        "imposter syndrome, anything. One or two sentences is fine."
    )


# ── Payment gate ────────────────────────────────────────────────────────────

async def _payment_gate_body(sender: str) -> str:
    """Return the payment prompt text, using a direct link when configured."""
    body = (
        "Your tailored output is ready to be created.\n\n"
        "Unlock *60 days of access for ₹1,799* — covers everything I "
        "can help you with: resume rebuilds, interview prep, and "
        "unlimited iterations."
    )
    direct = (settings.razorpay_payment_link or "").strip()
    if direct:
        return body + f"\n\nPay here: {direct}"

    # No direct link configured — try the Razorpay API.
    try:
        state_now = await get_user_state(sender)
        user_name = (state_now.get("data") or {}).get("user_name")
        link = await PaymentService().create_access_pass_link(sender, user_name=user_name)
    except Exception:  # noqa: BLE001
        link = None

    if link:
        return body + f"\n\nPay here: {link}"
    return body + (
        "\n\nThe payment link couldn't be created right now — reply "
        "*retry* in a minute and I'll try again."
    )


# ── Helpers ─────────────────────────────────────────────────────────────────

def _parse_round_choice(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.strip().lower().lstrip("(").rstrip(").: ").strip()
    if t in ROUNDS:
        return t
    # Substring fallback for free-text replies.
    haystack = text.lower()
    for code, (label, kind) in ROUNDS.items():
        if label.lower() in haystack or kind in haystack:
            return code
    return None


def _guess_company_from_data(data: dict[str, Any]) -> Optional[str]:
    """Pull a company name out of resume context if available."""
    hm = data.get("hiring_manager") or {}
    if isinstance(hm, dict) and hm.get("company"):
        return hm["company"]
    target = (data.get("current_role_target") or "")
    # "Senior PM at Flipkart, targeting Director at Razorpay" — grab the last "at <name>".
    matches = re.findall(r"\bat\s+([A-Z][A-Za-z0-9&.\-]+(?:\s+[A-Z][A-Za-z0-9&.\-]+){0,3})", target)
    if matches:
        return matches[-1]
    return None


async def _resolve_answer_text(sender: str, message: dict[str, Any]) -> str:
    if message.get("type") == "audio" and message.get("media_url"):
        return await transcribe_voice_note(message["media_url"], sender=sender)
    return message.get("text", "") or ""


def _text(to: str, body: str) -> dict[str, Any]:
    return {"to": to, "type": "text", "text": body}


def _mask(phone: str) -> str:
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:5]}****{phone[-3:]}"


__all__ = [
    "handle", "INITIAL_STEP",
    "WELCOME", "AWAIT_ROLE", "AWAIT_COMPANY", "AWAIT_JD",
    "Q1_ROUND", "Q2_PRIOR", "Q3_INTERVIEWER", "Q4_CONCERN",
    "PREP_GENERATING", "PREP_DELIVERED",
    "MOCK_IN_PROGRESS", "MOCK_FEEDBACK",
]
