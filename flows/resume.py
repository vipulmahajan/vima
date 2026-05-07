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

import asyncio
import logging
import re
from typing import Any, Optional

from models.database import (
    get_user_state,
    upsert_user_state,
    merge_user_state_data,
    record_artifact,
)
from flows._redirect import warm_reprompt
from services.claude_service import (
    ClaudeService,
    ClaudeUnavailable,
    CLAUDE_EXHAUSTED_MSG,
)
from services.document_parser import extract_text
from services.payment_service import PaymentService
from services.pdf_service import try_render_resume_pdf, render_resume_docx
from services.research_service import research_role, research_person, fetch_url_text
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
        if await PaymentService().is_subscribed(sender):
            await upsert_user_state(sender, {"resume_step": RESUME_PROC2})
            return await _handle_proc2(sender, message, text)
        if text.lower().strip() == "retry":
            return await _payment_gate(sender)
        return _text(sender,
            "I'm waiting for your payment to come through — once it does, "
            "your resume will arrive automatically. If you've already paid, "
            "give it a minute and reply *menu* if nothing arrives."
        )

    if step == DONE:
        return await _handle_done(sender, message, text)

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
        "header": "Vima — Resume Coach",
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


_LI_TEXT_KEYWORDS = {"experience", "education", "skills", "summary", "connections", "years"}


async def _handle_q2(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Collect multiple resume sources — files + pasted LinkedIn text — until 'done'.

    Each accepted upload is appended to ``data.resume_sources``:
        {"type": "resume" | "linkedin_pdf" | "linkedin_text",
         "text": str,
         "filename": str | None}
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
            "You can also upload your resume as a PDF or Word doc, or send "
            "a photo of it — whatever's easiest."
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
            f"Got your {kind_label}. Send another version or type *done* when you're finished."
        )

    # ── Branch 2: LinkedIn profile URL — reject and guide ──────────────────
    if LINKEDIN_PROFILE_RE.search(text):
        return _text(sender,
            "LinkedIn profiles can't be read directly — but you can export yours "
            "as a PDF in 30 seconds: Open your LinkedIn profile → click *More* → "
            "*Save to PDF* → send that file here. It gives me your full profile "
            "including recent experience."
        )

    # ── Branch 3: pasted LinkedIn profile text (> 400 chars, 2+ keywords) ──
    if len(text) > 400:
        kw_hits = sum(1 for kw in _LI_TEXT_KEYWORDS if kw in text_lower)
        if kw_hits >= 2:
            sources.append({
                "type":     "linkedin_text",
                "text":     text,
                "filename": "LinkedIn profile (pasted)",
            })
            await merge_user_state_data(sender, {"resume_sources": sources})
            return _text(sender,
                "Got your LinkedIn profile — I can see your full career story here. "
                "Send your resume file too if you have one, or type *done*."
            )

    # ── Branch 4: completion signal ────────────────────────────────────────
    if text_lower in DONE_TRIGGERS:
        if not sources:
            return _text(sender,
                "I'll need at least one resume to tailor anything useful. "
                "Share whatever you have (even a rough older version helps), "
                "then type *done*."
            )

        n_resume   = sum(1 for s in sources if s.get("type") == "resume")
        n_li_pdf   = sum(1 for s in sources if s.get("type") == "linkedin_pdf")
        n_li_text  = sum(1 for s in sources if s.get("type") == "linkedin_text")

        bits: list[str] = []
        if n_resume:
            bits.append(f"{n_resume} resume" + ("s" if n_resume > 1 else ""))
        if n_li_pdf:
            bits.append(f"{n_li_pdf} LinkedIn PDF" + ("s" if n_li_pdf > 1 else ""))
        if n_li_text:
            bits.append("your LinkedIn profile")

        summary = _join_human(bits)

        await upsert_user_state(sender, {"resume_step": RESUME_Q3})
        return _text(sender,
            f"Perfect — I've got {summary}. Plenty to work with!\n\n" + _q3_body()
        )

    # ── Branch 5: anything else — re-prompt ────────────────────────────────
    if not sources:
        return _text(sender, _q2_body())

    return _text(sender,
        "Send another resume version or type *done* when you're finished."
    )


async def _handle_q3(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Capture target JD — document upload, URL fetch, or pasted text."""
    msg_type = message.get("type")

    # ── Document upload ────────────────────────────────────────────────────
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
                "Try a clearer PDF, or paste the job description text directly."
            )

        await merge_user_state_data(sender, {"jd_source": "file", "jd_text": jd_text})
        await upsert_user_state(sender, {"resume_step": RESUME_Q4})
        return _ask_q4(sender)

    # ── URL path ───────────────────────────────────────────────────────────
    url_match = URL_RE.search(text)
    if url_match:
        url = url_match.group(0).rstrip(".,);")

        # LinkedIn job URLs can't be fetched — instruct the user to paste.
        if re.search(r"linkedin\.com", url, re.IGNORECASE):
            return _text(sender,
                "LinkedIn job links don't work — open the posting, select all "
                "text, and paste here instead."
            )

        # Attempt to fetch the page.
        fetched = await fetch_url_text(url, min_chars=200)
        if fetched:
            await merge_user_state_data(sender, {
                "jd_source": "url_fetched",
                "jd_url":    url,
                "jd_text":   fetched[:12000],
            })
            await upsert_user_state(sender, {"resume_step": RESUME_Q4})
            return _ask_q4(sender)

        # Fetch failed or returned too little.
        await merge_user_state_data(sender, {"jd_url": url})
        return _text(sender,
            "I couldn't read that page directly — could you paste the job "
            "description text? Select all on the page and paste here."
        )

    # ── Plain-text paste ───────────────────────────────────────────────────
    if len(text) >= 200:
        await merge_user_state_data(sender, {"jd_source": "text", "jd_text": text})
        await upsert_user_state(sender, {"resume_step": RESUME_Q4})
        return _ask_q4(sender)

    return _text(sender, _q3_body())


_Q4_SKIP_PHRASES = {"skip", "no", "none", "n/a", "na", "don't know", "not sure", "unsure", "no idea"}


async def _handle_q4(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Capture hiring manager — pasted LinkedIn text, free-text description, or skip."""
    t_lower = text.lower().strip()

    if t_lower in _Q4_SKIP_PHRASES:
        await merge_user_state_data(sender, {"hiring_manager": None})
        await upsert_user_state(sender, {"resume_step": RESUME_Q5})
        return _ask_q5(sender)

    # Reject LinkedIn URLs — guide user to paste text instead.
    if LINKEDIN_RE.search(text):
        return _text(sender,
            "LinkedIn URLs can't be read directly. Instead, open their profile, "
            "select all text (Ctrl+A / Cmd+A), and paste it here — or just share "
            "a short description like their name, title, and company."
        )

    if not text or len(text) < 3:
        return _text(sender, _q4_body())

    # Long input (> 300 chars) → treat as pasted LinkedIn profile text.
    if len(text) > 300:
        await merge_user_state_data(sender, {
            "hiring_manager": {"type": "linkedin_text", "raw": text},
        })
        await upsert_user_state(sender, {"resume_step": RESUME_Q5})
        return _ask_q5(sender)

    # Short input → free-text description (no comma requirement).
    await merge_user_state_data(sender, {
        "hiring_manager": {"type": "description", "raw": text},
    })
    await upsert_user_state(sender, {"resume_step": RESUME_Q5})
    return _ask_q5(sender)


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


async def _extract_hm_name_company(hm: dict[str, Any]) -> tuple[str, str]:
    """Extract (name, company) from hiring_manager data. Uses Claude for long text."""
    if not hm:
        return "", ""

    hm_type = hm.get("type", "")
    raw     = hm.get("raw", "")

    if hm_type == "description" and raw:
        # Short free-text: try simple comma split first.
        parts = [p.strip() for p in re.split(r"[,;|@]", raw) if p.strip()]
        name    = parts[0] if parts else raw.split()[0] if raw.split() else ""
        company = parts[1] if len(parts) > 1 else ""
        return name, company

    if hm_type == "linkedin_text" and raw:
        # Long text dump — use Claude to extract name + company quickly.
        try:
            from services.claude_service import ClaudeService, _first_text
            claude = ClaudeService()
            resp = await claude._create_with_retry(
                purpose="hm_extract",
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract the person's full name and current company from "
                        "this LinkedIn profile text. Reply with ONLY: "
                        "Name: <name>\nCompany: <company>\n\n"
                        f"{raw[:3000]}"
                    ),
                }],
            )
            lines = _first_text(resp).strip().splitlines()
            name    = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("name:")), "")
            company = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("company:")), "")
            return name, company
        except Exception as exc:  # noqa: BLE001
            log.warning("hm_extract failed: %s", exc)

    # Legacy formats.
    name    = hm.get("name", "")
    company = hm.get("company", "")
    return name, company


async def _handle_proc1(
    sender: str,
    message: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Acknowledge + run parallel research (role, HM, JD URL), then queue Q6."""
    state = await get_user_state(sender)
    data  = state.get("data") or {}

    hm = data.get("hiring_manager")
    hm_name, hm_company = await _extract_hm_name_company(hm) if hm else ("", "")

    target  = data.get("current_role_target") or data.get("jd_url") or ""
    jd_url  = data.get("jd_url") or ""
    jd_text = data.get("jd_text") or ""

    # Build personalised ack message.
    if hm_name:
        ack_text = (
            f"I'm researching {target.split(' at ')[-1] if ' at ' in target else 'the company'}, "
            f"the role, and {hm_name} — give me a moment..."
        )
    else:
        company_hint = target.split(" at ")[-1] if " at " in target else "the company"
        ack_text = f"I'm pulling up the JD and researching {company_hint} — give me a moment..."

    try:
        messenger = await get_messenger(sender)
        await messenger.send_typing_indicator(sender)
        await messenger.send_text(sender, ack_text)
    except Exception as exc:  # noqa: BLE001
        log.warning("proc1 ack send failed: %s", exc)

    # ── Three parallel research tasks ──────────────────────────────────────
    async def _do_role_research() -> dict[str, Any]:
        try:
            return await research_role(target)
        except Exception as exc:  # noqa: BLE001
            log.warning("research_role failed: %s", exc)
            return {}

    async def _do_hm_research() -> str:
        if not hm_name:
            return ""
        try:
            return await research_person(hm_name, hm_company)
        except Exception as exc:  # noqa: BLE001
            log.warning("research_person failed: %s", exc)
            return ""

    async def _do_jd_fetch() -> str:
        if jd_text or not jd_url:
            return ""
        try:
            fetched = await fetch_url_text(jd_url, min_chars=200)
            return fetched or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("JD URL fetch in proc1 failed: %s", exc)
            return ""

    role_research, hm_research, fetched_jd = await asyncio.gather(
        _do_role_research(), _do_hm_research(), _do_jd_fetch()
    )

    updates: dict[str, Any] = {"company_research": role_research}
    if hm_research:
        updates["hm_research"] = hm_research
    if fetched_jd:
        updates["jd_text"]   = fetched_jd[:12000]
        updates["jd_source"] = "url_fetched"
    await merge_user_state_data(sender, updates)

    # Generate the dynamic Q6 with HM research context.
    refreshed = await get_user_state(sender)
    profile   = refreshed.get("data") or {}
    claude    = ClaudeService()
    try:
        question = await claude.generate_resume_clarifying_question(
            profile,
            sender_phone=sender,
            hm_research=hm_research or None,
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
    state = await get_user_state(sender)
    data  = state.get("data") or {}
    question = data.get("q6_question") or "Could you share one concrete example for me to anchor on?"

    if not text or len(text) < 5:
        return _text(sender, f"Take your time. {question}")

    # Push back once on very short answers to get a quantified example.
    if len(text) < 15 and not data.get("q6_pushed"):
        await merge_user_state_data(sender, {"q6_pushed": True})
        return _text(sender,
            "Could you add a specific number or outcome — even approximate? "
            "For example, 'reduced processing time by 40%' or 'managed a team of 12'. "
            "It makes a real difference."
        )

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
        await merge_user_state_data(sender, {"pending_action": "resume_proc2"})
        await upsert_user_state(sender, {"resume_step": AWAITING_PAYMENT})
        return await _payment_gate(sender)

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
            hm_research         = data.get("hm_research"),
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
            hm_research         = data.get("hm_research"),
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

    # 4. Render PDF (best-effort) and DOCX in-memory.
    # try_render_resume_pdf swallows all WeasyPrint/GTK failures and returns None.
    pdf_bytes  = try_render_resume_pdf(resume_json or {})
    docx_bytes: Optional[bytes] = None

    try:
        docx_bytes = render_resume_docx(resume_json or {}, template="executive")
    except Exception as exc:  # noqa: BLE001
        log.exception("DOCX render failed: %s", exc)

    if not docx_bytes:
        return _text(sender,
            "I drafted your resume — strategy note above. The Word render hit "
            "a snag. Reply *retry* and I'll try again."
        )

    await upsert_user_state(sender, {"resume_step": DONE})

    # 5. Deliver — PDF silently skipped if unavailable, DOCX always sent.
    if pdf_bytes:
        try:
            await messenger.send_document(
                sender,
                pdf_bytes,
                filename="Vima-Resume.pdf",
                caption="Your tailored resume is ready.",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("PDF delivery failed: %s", exc)

    docx_storage_path = f"{sender}/Vima-Resume.docx"
    try:
        await messenger.send_document(
            sender,
            docx_bytes,
            filename="Vima-Resume.docx",
            caption=(
                "Your tailored resume is ready — here's your editable Word version. "
                "Feel free to tweak anything before submitting."
            ),
        )
        try:
            await record_artifact(sender, "resume", docx_storage_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("record_artifact(resume) failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("DOCX delivery failed: %s", exc)

    return None  # type: ignore[return-value]


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
        "Upload a PDF or Word doc, or send a photo of it. "
        "LinkedIn export PDF also works (open your profile → More → Save to PDF).\n\n"
        "The more I know about your background, the sharper your resume will be.\n\n"
        "When you're done sharing, type *done*."
    )


def _ask_q2(sender: str) -> dict[str, Any]:
    return _text(sender, _q2_body())


def _q3_body() -> str:
    return (
        "Share the target JD — three ways that work:\n"
        "• Paste the full job description text directly\n"
        "• Upload the JD as a PDF\n"
        "• Share a company careers page URL (not LinkedIn — paste those as text)\n\n"
        "LinkedIn job links don't work — open the posting, select all text, and paste here instead."
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


def _q4_body() -> str:
    return (
        "Who do you think the hiring manager is — the person you'd report to, not HR?\n\n"
        "Three ways to share:\n"
        "1. Short description — 'Rahul Sharma, VP Risk at Morgan Stanley, ex-Goldman'\n"
        "2. Paste their LinkedIn profile — open their profile, select all text "
        "(Ctrl+A / Cmd+A), paste here\n"
        "3. Skip — type *skip* if you're not sure\n\n"
        "Even a name and company helps me tailor your resume to what they value."
    )


def _ask_q4(sender: str) -> dict[str, Any]:
    return _text(sender, _q4_body())


def _ask_q5(sender: str) -> dict[str, Any]:
    return _text(sender,
        "What's your *superpower* — the one thing you're known for doing "
        "better than your peers? One or two sentences is enough."
    )


_EDIT_KEYWORDS = {
    "highlight", "add", "change", "update", "more", "better", "fix",
    "include", "remove", "emphasise", "emphasize", "strengthen", "rewrite",
    "adjust", "tweak", "rephrase", "mention", "focus", "improve",
    "shorten", "expand",
}

_RESEND_KEYWORDS = {
    "same resume", "resume again", "send again", "resend",
    "download", "get my resume", "share resume", "my resume",
}


def _is_edit_request(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _EDIT_KEYWORDS)


def _is_resend_request(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _RESEND_KEYWORDS)


async def _resend_resume_artifact(sender: str) -> dict[str, Any]:
    """Find the most recent resume artifact and re-deliver it via a fresh signed URL.

    Returns a _text reply if no artifact exists.
    """
    from models.database import get_user_artifacts
    from services.storage_service import StorageService

    artifacts = await get_user_artifacts(sender)
    resume_art = next((a for a in artifacts if a.get("kind") == "resume"), None)

    if not resume_art:
        return _text(sender,
            "I don't have a saved resume for you yet — reply *2* to build one now."
        )

    storage_path = resume_art.get("storage_path", "")
    filename = storage_path.split("/")[-1] or "Vima-Resume.docx"

    try:
        storage = StorageService()
        signed_url = await storage.create_signed_url(storage_path, ttl_seconds=3600)
    except Exception as exc:  # noqa: BLE001
        log.warning("resend_artifact signed_url failed: %s", exc)
        return _text(sender,
            "I couldn't generate the download link right now — try again in a moment."
        )

    messenger = await get_messenger(sender)

    # Web channel — push a document event with the signed URL directly.
    if hasattr(messenger, "send_document_url"):
        await messenger.send_document_url(
            sender,
            url=signed_url,
            storage_path=storage_path,
            filename=filename,
            caption="Here's your resume — the most recent version I created for you.",
        )
        return None  # type: ignore[return-value]

    # WhatsApp / other channel — just send the URL as text.
    return _text(sender,
        f"Here's your resume — the most recent version I created for you:\n\n{signed_url}"
    )


async def _handle_done(
    sender: str, message: dict[str, Any], text: str,
) -> dict[str, Any]:
    """After delivery, handle resend requests, edit requests, and fallback nudge."""
    if _is_resend_request(text):
        return await _resend_resume_artifact(sender)

    if not _is_edit_request(text):
        return _text(sender,
            "Your resume is ready above. Reply *interview* to prep for this role, "
            "or *menu* to start something new."
        )

    messenger = await get_messenger(sender)
    try:
        await messenger.send_text(sender, "Good call — let me sharpen that. Give me a moment...")
    except Exception as exc:  # noqa: BLE001
        log.warning("edit ack send failed: %s", exc)

    state = await get_user_state(sender)
    data  = state.get("data") or {}
    claude = ClaudeService()

    try:
        resume_json = await claude.generate_resume(
            sender_phone        = sender,
            current_role_target = data.get("current_role_target"),
            resume_sources      = data.get("resume_sources"),
            resume_text         = data.get("resume_text"),
            linkedin_url        = data.get("linkedin_url"),
            jd_text             = data.get("jd_text"),
            jd_url              = data.get("jd_url"),
            hiring_manager      = data.get("hiring_manager"),
            superpower          = data.get("superpower"),
            q6_question         = data.get("q6_question"),
            q6_answer           = data.get("q6_answer"),
            company_research    = data.get("company_research"),
            hm_research         = data.get("hm_research"),
            edit_instruction    = text,
        )
    except ClaudeUnavailable:
        log.error("generate_resume (edit) exhausted retries; phone=%s", _mask(sender))
        return _text(sender, CLAUDE_EXHAUSTED_MSG)
    except Exception as exc:  # noqa: BLE001
        log.exception("generate_resume (edit) unexpected error: %s", exc)
        return _text(sender, CLAUDE_EXHAUSTED_MSG)

    # Persist updated JSON so further edits build on the latest version.
    await merge_user_state_data(sender, {"resume_json": resume_json})

    docx_bytes: Optional[bytes] = None
    try:
        docx_bytes = render_resume_docx(resume_json or {}, template="executive")
    except Exception as exc:  # noqa: BLE001
        log.exception("DOCX render failed (edit): %s", exc)

    if not docx_bytes:
        return _text(sender,
            "I updated the resume but hit a snag generating the Word file. "
            "Reply *retry* and I'll try again."
        )

    try:
        await messenger.send_document(
            sender,
            docx_bytes,
            filename="Vima-Resume-v2.docx",
            caption="Here's your updated resume.",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("DOCX delivery failed (edit): %s", exc)

    return None  # type: ignore[return-value]


def _resume_done(sender: str) -> dict[str, Any]:
    return _text(sender,
        "Your resume is delivered. Reply *interview* to run a mock interview, "
        "or *menu* to start over."
    )


# ── Payment gate ────────────────────────────────────────────────────────────

async def _payment_gate(sender: str) -> Optional[dict[str, Any]]:
    """Push a Checkout JS payment event (web) or return a text reply (WhatsApp)."""
    state_now  = await get_user_state(sender)
    data       = state_now.get("data") or {}
    user_name  = data.get("user_name") or ""
    user_email = sender if "@" in sender else ""

    # Web channel — push a payment event for Checkout JS.
    if "@" in sender:
        try:
            order = await PaymentService().create_order(
                user_id    = sender,
                user_name  = user_name,
                user_email = user_email,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("create_order failed for web user %s: %s", _mask(sender), exc)
            return _text(sender,
                "Couldn't set up the payment right now — reply *retry* in a minute."
            )
        messenger = await get_messenger(sender)
        await messenger.send_payment_request(
            user_id    = sender,
            order_id   = order["order_id"],
            amount     = order["amount"],
            currency   = order["currency"],
            key_id     = order["key_id"],
            user_name  = order["user_name"],
            user_email = order["user_email"],
        )
        return None  # event pushed; no further reply needed

    # WhatsApp channel — fall back to a payment link in text.
    body = (
        "Your tailored output is ready to be created.\n\n"
        "Unlock *60 days of access for ₹1,799* — covers everything I "
        "can help you with: resume rebuilds, interview prep, and "
        "unlimited iterations."
    )
    try:
        link = await PaymentService().create_access_pass_link(
            sender, user_name=user_name or None
        )
    except Exception:  # noqa: BLE001
        link = None

    if link:
        return _text(sender, body + f"\n\nPay here: {link}")
    return _text(sender, body + (
        "\n\nThe payment link couldn't be created right now — reply "
        "*retry* in a minute and I'll try again."
    ))


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
