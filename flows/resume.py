"""Resume discovery + rewrite flow — full state machine.

States (stored in user_state.resume_step):

  WELCOME          → Show stage-selection buttons; on response, move to RESUME_Q1.
  RESUME_Q1        → Ask current role + target level. Save free text.
  RESUME_Q2        → Ask user to upload existing resume (PDF/DOCX/image).
                     Download via WhatsApp, OCR-parse, store text.
  RESUME_Q3        → Ask for the target JD (PDF upload OR website / LinkedIn URL).
  RESUME_Q4        → Ask for hiring manager (LinkedIn URL OR
                     name + company + title; "skip" allowed).
  RESUME_Q5        → Ask for the user's "superpower" / differentiator.
  RESUME_PROC1     → Acknowledge, fire off company / JD / HM research in the
                     background, advance to Q6 when research is ready (or in
                     parallel: ack now, do research before Q6 actually fires).
  RESUME_Q6        → Ask the single best Claude-generated clarifying question
                     informed by JD + HM + company research + user's strengths.
  RESUME_PROC2     → Acknowledge, kick off generation pipeline.
  DONE             → Resume delivered; offer follow-ups.

All collected user data is shallow-merged into ``user_state.data`` (jsonb).
Every state validates its input; if the user sends something unexpected, the
state re-prompts rather than advancing.
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
from services.document_parser import extract_text
from config import settings
from services.payment_service import PaymentService
from services.pdf_service import render_resume_pdf, render_resume_docx
from services.research_service import research_role
from services.messenger import get_messenger

log = logging.getLogger(__name__)


# ── State constants ─────────────────────────────────────────────────────────
WELCOME           = "welcome"
RESUME_Q1         = "resume_q1"          # current role + target level
RESUME_Q2         = "resume_q2"          # existing resume upload
RESUME_Q3         = "resume_q3"          # target JD (file or URL)
RESUME_Q4         = "resume_q4"          # hiring manager
RESUME_Q5         = "resume_q5"          # superpower
RESUME_PROC1      = "resume_processing1" # ack + start analysing
RESUME_Q6         = "resume_q6"          # dynamic clarifying question
RESUME_PROC2      = "resume_processing2" # ack + generate
AWAITING_PAYMENT  = "awaiting_payment"   # paused waiting for Razorpay confirm
DONE              = "delivered"

INITIAL_STEP = WELCOME

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/\S+", re.IGNORECASE)
LINKEDIN_PROFILE_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/in/\S+",
    re.IGNORECASE,
)

# Words / phrases that mean "I'm done uploading" in Q2.
DONE_TRIGGERS = {
    "done", "that's all", "thats all", "no more", "next",
    "go ahead", "proceed", "continue", "finish", "finished",
    "all done", "i'm done", "im done", "that is all",
}


# ── Public entry point ──────────────────────────────────────────────────────

async def handle(
    sender: str,
    message: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Drive the resume conversation one step forward."""
    step = state.get("resume_step") or INITIAL_STEP
    text = (message.get("text") or "").strip()

    log.info("resume.handle: phone=%s step=%s msg_type=%s", _mask(sender), step, message.get("type"))

    # Each handler returns the outbound reply; advancement is its responsibility.
    if step == WELCOME:
        return await _handle_welcome(sender, message, text)
    if step == RESUME_Q1:
        return await _handle_q1(sender, message, text)
    if step == RESUME_Q2:
        return await _handle_q2(sender, message, text)
    if step == RESUME_Q3:
        return await _handle_q3(sender, message, text)
    if step == RESUME_Q4:
        return await _handle_q4(sender, message, text)
    if step == RESUME_Q5:
        return await _handle_q5(sender, message, text)
    if step == RESUME_PROC1:
        return await _handle_proc1(sender, message, text)
    if step == RESUME_Q6:
        return await _handle_q6(sender, message, text)
    if step == RESUME_PROC2:
        return await _handle_proc2(sender, message, text)

    if step == AWAITING_PAYMENT:
        if text.lower().strip() == "retry":
            return _text(sender, await _payment_gate_body(sender))
        return _text(sender,
            "I'm waiting for your payment to come through — once it does, "
            "your resume will arrive automatically. If you've already paid, "
            "give it a minute and reply *menu* if nothing arrives."
        )

    return _resume_done(sender)


# ── State handlers ──────────────────────────────────────────────────────────

async def _handle_welcome(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """First touch in the resume flow — show stage-selection buttons."""
    # If the user has already sent something usable, skip the menu and start Q1.
    if text and text.lower() not in {"resume", "cv", "start", "hi", "hello"}:
        await upsert_user_state(sender, {"resume_step": RESUME_Q1})
        return _ask_q1(sender)

    await upsert_user_state(sender, {"resume_step": RESUME_Q1})
    return {
        "to": sender,
        "type": "buttons",
        "header": "ViMa — Resume Coach",
        "text": (
            "Welcome. I'll help you tailor a sharp, role-specific resume "
            "for the Indian corporate market.\n\n"
            "Where are you in your job search?"
        ),
        "footer": "Tap a stage or type your reply.",
        "buttons": [
            {"id": "stage_exploring",  "title": "Exploring"},
            {"id": "stage_active",     "title": "Actively applying"},
            {"id": "stage_offer",      "title": "Have an offer"},
        ],
    }


async def _handle_q1(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Capture current role + target level."""
    if not text or len(text) < 5:
        question = (
            "Tell me your *current role* and the *level you're targeting*.\n"
            "Example: \"Senior PM at Flipkart, targeting Director PM at fintech.\""
        )
        return _text(sender, warm_reprompt(text, question, flow="resume"))

    await merge_user_state_data(sender, {"current_role_target": text})
    await upsert_user_state(sender, {"resume_step": RESUME_Q2})
    return _ask_q2(sender)


async def _handle_q2(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Collect multiple resume sources — files + LinkedIn URLs — until 'done'.

    Each accepted upload is appended to ``data.resume_sources``:
        {"type": "resume" | "linkedin_pdf" | "linkedin_url",
         "text": str,            # extracted text (or "" for url-only entries)
         "filename": str | None}

    LinkedIn profile URLs are also persisted at ``data.linkedin_url`` for the
    later research step.
    """
    msg_type = message.get("type")

    state = await get_user_state(sender)
    data  = state.get("data") or {}
    sources: list[dict[str, Any]] = list(data.get("resume_sources") or [])

    text_lower = text.lower().strip().rstrip(".!")

    # ── Branch 0: voice/audio capability question ──────────────────────────
    _VOICE_KW = {"voice", "audio", "speak", "record", "mic", "microphone", "talk"}
    if msg_type == "text" and any(kw in text_lower for kw in _VOICE_KW):
        return _text(sender,
            "Yes! Tap the mic icon in the input bar, hold to record, and "
            "release to send. I'll transcribe what you say.\n\n"
            "You can also upload your resume as a PDF or Word doc, send a "
            "photo of it, or paste your LinkedIn URL — whatever's easiest."
        )

    # ── Branch 1: file / image upload ──────────────────────────────────────
    if msg_type in ("document", "image") and message.get("media_url"):
        try:
            extracted = await extract_text(
                message["media_url"],
                mime_type=message.get("mime_type"),
                filename=message.get("filename"),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Resume extract_text failed: %s", exc)
            return _text(sender,
                "I'm having trouble reading that file — could you try again, "
                "or paste the resume text directly? Either works."
            )

        if not extracted or len(extracted) < 100:
            log.warning(
                "Resume extract under threshold len=%d phone=%s",
                len(extracted or ""), _mask(sender),
            )
            return _text(sender,
                "I'm having trouble reading that file — could you try again, "
                "or paste the resume text directly? Either works."
            )

        filename = (message.get("filename") or "").lower()
        is_li_pdf = "linkedin" in filename or "profile" in filename
        kind = "linkedin_pdf" if is_li_pdf else "resume"

        sources.append({
            "type":     kind,
            "text":     extracted,
            "filename": message.get("filename"),
        })
        await merge_user_state_data(sender, {"resume_sources": sources})

        kind_label = "LinkedIn PDF" if kind == "linkedin_pdf" else "resume"
        return _text(sender,
            f"Got your {kind_label}. Send another version, paste your "
            "LinkedIn profile URL, or type *done* when you're finished."
        )

    # ── Branch 2: LinkedIn profile URL pasted as text ──────────────────────
    li = LINKEDIN_PROFILE_RE.search(text)
    if li:
        url = li.group(0).rstrip(".,);")

        # Avoid duplicate entries if the same URL is sent twice.
        already = any(
            s.get("type") == "linkedin_url" and s.get("text") == url
            for s in sources
        )
        if not already:
            sources.append({"type": "linkedin_url", "text": url, "filename": None})
            await merge_user_state_data(sender, {
                "resume_sources": sources,
                "linkedin_url":   url,
            })
        else:
            await merge_user_state_data(sender, {"linkedin_url": url})

        return _text(sender,
            "Got your LinkedIn — I'll use this to understand your full "
            "professional story.\n\nSend more if you have other versions, "
            "or type *done* when you're finished."
        )

    # ── Branch 3: completion signal ────────────────────────────────────────
    if text_lower in DONE_TRIGGERS:
        if not sources:
            return _text(sender,
                "I'll need at least one resume — or your LinkedIn URL — to "
                "tailor anything useful. Share whatever you have (even a "
                "rough older version helps), then type *done*."
            )

        # Build a friendly summary of what we received.
        n_resume = sum(1 for s in sources if s.get("type") == "resume")
        n_li_pdf = sum(1 for s in sources if s.get("type") == "linkedin_pdf")
        n_li_url = sum(1 for s in sources if s.get("type") == "linkedin_url")

        bits: list[str] = []
        if n_resume:
            bits.append(f"{n_resume} resume" + ("s" if n_resume > 1 else ""))
        if n_li_pdf:
            bits.append(f"{n_li_pdf} LinkedIn PDF" + ("s" if n_li_pdf > 1 else ""))
        if n_li_url:
            bits.append("your LinkedIn profile")

        summary = _join_human(bits)

        await upsert_user_state(sender, {"resume_step": RESUME_Q3})
        ack = _text(sender,
            f"Perfect — I've got {summary}. Plenty to work with!\n\n" + _q3_body()
        )
        return ack

    # ── Branch 4: anything else — re-prompt ────────────────────────────────
    if not sources:
        # First touch in Q2.
        return _text(sender, _q2_body())

    return _text(sender,
        "Send another resume version or paste your LinkedIn URL. "
        "Type *done* when you're finished."
    )


async def _handle_q3(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Capture target JD — either a document upload or a URL."""
    msg_type = message.get("type")

    # Document path.
    if msg_type in ("document", "image") and message.get("media_url"):
        try:
            jd_text = await extract_text(
                message["media_url"],
                mime_type=message.get("mime_type"),
                filename=message.get("filename"),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("JD extract_text failed: %s", exc)
            jd_text = ""

        if not jd_text or len(jd_text) < 80:
            return _text(sender,
                "Couldn't read enough text from that JD. "
                "Try sharing the JD as a clearer PDF, or paste the link instead."
            )

        await merge_user_state_data(sender, {"jd_source": "file", "jd_text": jd_text})
        await upsert_user_state(sender, {"resume_step": RESUME_Q4})
        return _ask_q4(sender)

    # URL path.
    url_match = URL_RE.search(text)
    if url_match:
        url = url_match.group(0).rstrip(".,);")
        if re.search(r"linkedin\.com/jobs/", url, re.IGNORECASE):
            return _text(sender,
                "LinkedIn job pages are locked to logged-in users so I can't "
                "read them directly. Could you copy and paste the job description "
                "text? Just open the LinkedIn post, select all the text, and paste "
                "it here — takes 30 seconds and gives me everything I need."
            )
        await merge_user_state_data(sender, {"jd_source": "url", "jd_url": url})
        await upsert_user_state(sender, {"resume_step": RESUME_Q4})
        return _ask_q4(sender)

    return _text(sender,
        "Share the *target JD* — either upload the JD as a PDF, or paste a "
        "link to the job posting / LinkedIn job URL."
    )


async def _handle_q4(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Capture hiring manager — LinkedIn URL OR name+company+title, with skip."""
    if text.lower().strip() in {"skip", "no", "none", "n/a", "na"}:
        await merge_user_state_data(sender, {"hiring_manager": None})
        await upsert_user_state(sender, {"resume_step": RESUME_Q5})
        return _ask_q5(sender)

    li = LINKEDIN_RE.search(text)
    if li:
        await merge_user_state_data(sender, {
            "hiring_manager": {"linkedin_url": li.group(0).rstrip(".,);")},
        })
        await upsert_user_state(sender, {"resume_step": RESUME_Q5})
        return _ask_q5(sender)

    # Look for "name + company + title" — at least 2 commas or 3 words on each side.
    parts = [p.strip() for p in re.split(r"[,;|]", text) if p.strip()]
    if len(parts) >= 3:
        await merge_user_state_data(sender, {
            "hiring_manager": {
                "name":    parts[0],
                "company": parts[1],
                "title":   parts[2],
            },
        })
        await upsert_user_state(sender, {"resume_step": RESUME_Q5})
        return _ask_q5(sender)

    return _text(sender,
        "Got it. For the *hiring manager*, share either:\n"
        "• their LinkedIn URL, or\n"
        "• name, company, title — separated by commas.\n\n"
        "Example: _Anjali Rao, Razorpay, VP Engineering_\n"
        "Or reply *skip* if you don't have it."
    )


async def _handle_q5(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Capture user's superpower / differentiator."""
    if not text or len(text) < 10:
        question = (
            "What's your *superpower* — the one thing you do better than 9 "
            "out of 10 people in your space? One or two sentences is enough."
        )
        return _text(sender, warm_reprompt(text, question, flow="resume"))

    await merge_user_state_data(sender, {"superpower": text})
    await upsert_user_state(sender, {"resume_step": RESUME_PROC1})
    return await _handle_proc1(sender, message, text)


async def _handle_proc1(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Acknowledge + run JD/company/HM research, then queue Q6.

    The research call is awaited inline (it's lightweight stub today). We
    push an ack message via WhatsAppService and return the Q6 prompt as the
    second message — matches the "ack first, slow work next" pattern used
    elsewhere in ViMa.
    """
    state = await get_user_state(sender)
    data  = state.get("data") or {}

    # Fire the ack as a first message so the user isn't waiting silent.
    try:
        messenger = await get_messenger(sender)
        await messenger.send_typing_indicator(sender)
        await messenger.send_text(
            sender,
            "Thanks. I'm pulling up the JD, the company, and any signals "
            "on your hiring manager. Give me a moment...",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("proc1 ack send failed: %s", exc)

    # Research the role/company. research_role accepts a free-text target.
    target = data.get("current_role_target") or data.get("jd_url") or ""
    try:
        research = await research_role(target)
    except Exception as exc:  # noqa: BLE001
        log.warning("research_role failed: %s", exc)
        research = {}

    # TODO: also crawl JD URL, hiring-manager LinkedIn, company investor decks.
    await merge_user_state_data(sender, {"company_research": research})

    # Generate the dynamic Q6.
    refreshed = await get_user_state(sender)
    profile = refreshed.get("data") or {}
    claude = ClaudeService()
    try:
        question = await claude.generate_resume_clarifying_question(
            profile, sender_phone=sender,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Q6 generation failed (%s); using default.", exc)
        question = (
            "What's one specific outcome from your last role you'd want me "
            "to highlight — ideally with a number?"
        )

    await merge_user_state_data(sender, {"q6_question": question})
    await upsert_user_state(sender, {"resume_step": RESUME_Q6})
    return _text(sender, f"One last question:\n\n{question}")


async def _handle_q6(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Capture the dynamic clarifying answer, then move to generation."""
    if not text or len(text) < 5:
        state = await get_user_state(sender)
        question = (state.get("data") or {}).get("q6_question") \
                   or "Could you share one concrete example for me to anchor on?"
        return _text(sender, f"Take your time. {question}")

    await merge_user_state_data(sender, {"q6_answer": text})
    await upsert_user_state(sender, {"resume_step": RESUME_PROC2})
    return await _handle_proc2(sender, message, text)


async def _handle_proc2(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Acknowledge + run the resume generation + delivery pipeline."""
    payments = PaymentService()
    if not await payments.is_subscribed(sender):
        # Park the flow until the webhook fires.
        await merge_user_state_data(sender, {"pending_action": "resume_proc2"})
        await upsert_user_state(sender, {"resume_step": AWAITING_PAYMENT})
        return _text(sender, await _payment_gate_body(sender))

    # Ack first.
    messenger = await get_messenger(sender)
    try:
        await messenger.send_typing_indicator(sender)
        await messenger.send_text(
            sender,
            "Got everything. Drafting a resume tailored to this role — "
            "this usually takes about a minute.",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("proc2 ack send failed: %s", exc)

    state = await get_user_state(sender)
    data  = state.get("data") or {}

    claude = ClaudeService()

    # 1. Generate the resume JSON. _create_with_retry handles retry + reassurance;
    # if it raises ClaudeUnavailable we keep the user in PROC2 so they can reply
    # 'retry' and we'll try again with full context intact.
    try:
        resume_json = await claude.generate_resume(
            sender_phone        = sender,
            current_role_target = data.get("current_role_target"),
            resume_sources      = data.get("resume_sources"),
            resume_text         = data.get("resume_text"),         # legacy fallback
            linkedin_url        = data.get("linkedin_url"),
            jd_text             = data.get("jd_text"),
            jd_url              = data.get("jd_url"),
            hiring_manager      = data.get("hiring_manager"),
            superpower          = data.get("superpower"),
            q6_question         = data.get("q6_question"),
            q6_answer           = data.get("q6_answer"),
            company_research    = data.get("company_research"),
        )
    except ClaudeUnavailable:
        log.error("generate_resume exhausted retries; pausing flow phone=%s", _mask(sender))
        # State stays at PROC2 so 'retry' re-runs this handler.
        return _text(sender, CLAUDE_EXHAUSTED_MSG)
    except Exception as exc:  # noqa: BLE001
        log.exception("generate_resume unexpected error: %s", exc)
        return _text(sender, CLAUDE_EXHAUSTED_MSG)

    # Persist the generated resume JSON so retries / edits don't recompute.
    await merge_user_state_data(sender, {"resume_json": resume_json})

    # 2. Generate strategy notes alongside the resume.
    try:
        strategy = await claude.generate_strategy_notes(
            sender_phone        = sender,
            resume_data         = resume_json,
            current_role_target = data.get("current_role_target"),
            jd_text             = data.get("jd_text"),
            hiring_manager      = data.get("hiring_manager"),
            superpower          = data.get("superpower"),
            company_research    = data.get("company_research"),
        )
    except (ClaudeUnavailable, Exception) as exc:  # noqa: BLE001
        log.warning("generate_strategy_notes skipped: %s", exc)
        strategy = ""

    # 3. Send strategy note as a separate message *before* the document
    #    so the user reads the positioning rationale first.
    if strategy:
        try:
            await messenger.send_text(sender, f"*Strategy note*\n\n{strategy}")
        except Exception as exc:  # noqa: BLE001
            log.warning("strategy note send failed: %s", exc)

    # 4. Render BOTH PDF and DOCX in-memory.
    pdf_bytes:  Optional[bytes] = None
    docx_bytes: Optional[bytes] = None
    pdf_error  = None
    docx_error = None

    try:
        pdf_bytes = render_resume_pdf(resume_json or {})
    except Exception as exc:  # noqa: BLE001
        log.exception("PDF render failed: %s", exc)
        pdf_error = exc

    try:
        docx_bytes = render_resume_docx(resume_json or {}, template="executive")
    except Exception as exc:  # noqa: BLE001
        log.exception("DOCX render failed: %s", exc)
        docx_error = exc

    if not pdf_bytes and not docx_bytes:
        return _text(sender,
            "I drafted your resume — strategy note above. Both PDF and Word "
            "renders hit a snag. Reply *retry* and I'll try again."
        )

    # 5. Deliver via the channel-agnostic messenger. PDF first, DOCX follow-up.
    if pdf_bytes:
        try:
            await messenger.send_document(
                sender,
                pdf_bytes,
                filename="ViMa-Resume.pdf",
                caption="Your tailored resume is ready.",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("PDF delivery failed: %s", exc)
    elif pdf_error:
        try:
            await messenger.send_text(
                sender,
                "PDF render hit a snag, but I've got the editable Word "
                "version coming through next.",
            )
        except Exception:  # noqa: BLE001
            pass

    await upsert_user_state(sender, {"resume_step": DONE})

    if docx_bytes:
        try:
            await messenger.send_document(
                sender,
                docx_bytes,
                filename="ViMa-Resume.docx",
                caption=(
                    "And here's an editable Word version — feel free to tweak "
                    "anything before submitting."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("DOCX delivery failed: %s", exc)
        # Both formats already dispatched; no further reply needed.
        return None  # type: ignore[return-value]

    # PDF succeeded, DOCX failed — note that and end the turn.
    return _text(sender,
        "Word version hit a render snag — I'll have it ready shortly. "
        "Reply *menu* anytime."
    )


# ── Static prompts ──────────────────────────────────────────────────────────

def _ask_q1(sender: str) -> dict[str, Any]:
    return _text(sender,
        "Great. First, tell me your *current role* and the *level you're "
        "targeting*.\n\n"
        "Example: _Senior PM at Flipkart, targeting Director PM at fintech._"
    )


def _q2_body() -> str:
    return (
        "Share your resume — you can send *multiple versions* if you have them. "
        "A LinkedIn profile also helps (paste the URL or send the downloaded PDF). "
        "The more I know about your background, the sharper your resume will be.\n\n"
        "When you're done sharing, just type *done*."
    )


def _ask_q2(sender: str) -> dict[str, Any]:
    return _text(sender, _q2_body())


def _q3_body() -> str:
    return (
        "Share the *target JD* — upload the JD as a PDF, or paste the job "
        "posting / LinkedIn job URL."
    )


def _ask_q3(sender: str) -> dict[str, Any]:
    return _text(sender, _q3_body())


def _join_human(items: list[str]) -> str:
    """Join 1-3 strings as 'a', 'a and b', or 'a, b, and c'."""
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _ask_q4(sender: str) -> dict[str, Any]:
    return _text(sender,
        "Who's the *hiring manager* (or recruiter you're routed through)?\n\n"
        "Send their LinkedIn URL, or _name, company, title_ separated by commas.\n"
        "Reply *skip* if you don't know yet."
    )


def _ask_q5(sender: str) -> dict[str, Any]:
    return _text(sender,
        "What's your *superpower* — the one thing you're known for doing "
        "better than your peers? One or two sentences is enough."
    )


def _resume_done(sender: str) -> dict[str, Any]:
    return _text(sender,
        "Your resume is delivered. Reply *interview* to run a mock interview, "
        "or *menu* to start over."
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
        payments = PaymentService()
        link = await payments.create_access_pass_link(sender, user_name=user_name)
    except Exception:  # noqa: BLE001
        link = None

    if link:
        return body + f"\n\nPay here: {link}"
    return body + (
        "\n\nThe payment link couldn't be created right now — reply "
        "*retry* in a minute and I'll try again."
    )


# ── Tiny helpers ────────────────────────────────────────────────────────────

def _text(to: str, body: str) -> dict[str, Any]:
    return {"to": to, "type": "text", "text": body}


def _mask(phone: str) -> str:
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:5]}****{phone[-3:]}"


__all__ = ["handle", "INITIAL_STEP", "WELCOME", "RESUME_Q1", "RESUME_Q2",
           "RESUME_Q3", "RESUME_Q4", "RESUME_Q5", "RESUME_PROC1",
           "RESUME_Q6", "RESUME_PROC2", "DONE"]
