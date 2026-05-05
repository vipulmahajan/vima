"""Claude (Anthropic) integration.

Wraps the Anthropic SDK with prompt caching, persona system prompt, and
domain-specific helpers for resume rewriting, strategy commentary, and
mock interviews.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from anthropic import AsyncAnthropic

from config import settings

log = logging.getLogger(__name__)


class ClaudeUnavailable(Exception):
    """Raised when Claude calls fail after all retries exhaust."""


# Reassurance message dispatched between retry attempts.
_REASSURE_MSG = "Give me a moment, I'm thinking deeply about your profile..."

# Exhausted-retries message the FLOW sends when it catches ClaudeUnavailable.
CLAUDE_EXHAUSTED_MSG = (
    "I'm hitting some technical wind right now. Reply *retry* in a few "
    "minutes and I'll pick up exactly where we left off."
)

_RETRY_DELAYS_SEC = (2, 5)  # 2 retries → 3 attempts total


def _mask_phone(phone: Optional[str]) -> str:
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:5]}****{phone[-3:]}"

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Generation budget for the full resume — must fit a 2-3 page A4 layout.
_RESUME_MAX_TOKENS   = 6000
_STRATEGY_MAX_TOKENS = 600


# ── Interview prep system prompt (cached alongside persona) ─────────────────

_INTERVIEW_PREP_SYSTEM = """\
You are ViMa's interview-prep engine.

Your job: produce a single JSON object — the candidate's interview prep kit
for ONE specific upcoming interview round. Output ONLY the JSON object;
no prose, no markdown fences.

Conform exactly to PREP_SCHEMA:

{
  "company_brief": {
    "summary":         str,    // 4-6 sentences. What the company does today,
                                //   stage, scale, recent direction. Indian context
                                //   when relevant (CTC bands, regulators, etc).
    "recent_news":     [str],  // 3-5 items from the last ~12 months. Date-stamp
                                //   loosely ("Q1 2025", "early 2025"). Concrete only;
                                //   never invent. If you don't know, drop the item.
    "culture":         [str],  // 3-5 culture signals (mission, working style, hiring
                                //   bar, public values). Cite the source when natural.
    "investor_thesis": str | null,  // 2-3 sentences: why investors back this co. Skip
                                     //   if unknown — do not fabricate.
    "challenges":      [str]   // 3-5 real strategic challenges the candidate could
                                //   credibly speak to (regulation, competition, unit
                                //   economics, scale, talent). Tie to the role.
  },

  "likely_questions": [        // 5-10 items, prioritized by likelihood for THIS round
    {
      "question":  str,
      "category":  "behavioral" | "technical" | "situational",
      "why_asked": str          // 1 sentence: what the interviewer is screening for
    }
  ],

  "suggested_answers": [       // Cover the top 4-6 likely_questions
    {
      "question": str,
      "star": {
        "situation": str,       // 1-2 sentences
        "task":      str,       // 1 sentence
        "action":    str,       // 2-3 sentences, specific verbs + numbers
        "result":    str        // 1-2 sentences with quantified outcome
      },
      "anchor_evidence": str | null  // which role/project from the candidate's
                                     // material this draws from. If candidate
                                     // material lacks evidence, leave null and
                                     // mark the gap in the action/result rather
                                     // than inventing.
    }
  ],

  "questions_to_ask": [        // 2-3 smart questions the candidate should ask
    {
      "question": str,
      "signals":  str          // what asking this conveys (seniority, ownership, etc)
    }
  ],

  "red_flags": [               // 3-5 common mistakes for this role level + round
    {
      "mistake": str,
      "instead": str
    }
  ]
}

Hard rules:
- Tailor everything to the SPECIFIC round_label (screening vs technical vs hiring
  manager vs leadership vs HR). The same role gets very different questions
  across rounds — do not produce generic prep.
- Use the candidate's ACTUAL experience (from resume_sources / superpower) for
  STAR answers. If the candidate hasn't lived a question, write a STAR using
  the closest analogous experience and flag the substitution in `anchor_evidence`.
- Address the candidate's stated `concern` somewhere in the kit — usually inside
  red_flags or via a likely_question that prepares them for it.
- If interviewers' LinkedIn/names are provided, reference what we'd want to know
  about them inside `culture` or `questions_to_ask` (e.g. background-fit signals).
- Quantify wherever the candidate's material allows. NEVER fabricate metrics,
  employer names, dates, news headlines, or investor names.
- Indian-corporate context where relevant. No emojis. No hype words.

Output ONLY the JSON object — start with { and end with }. No fences, no prose.
"""


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


class ClaudeService:
    """Thin async wrapper around the Anthropic SDK."""

    def __init__(self) -> None:
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._persona          = _load_prompt("persona.txt")
        self._resume_prompt    = _load_prompt("resume.txt")
        self._interview_prompt = _load_prompt("interview.txt")

    # ── Internal: retry wrapper around Claude API ──────────────────────────

    async def _create_with_retry(
        self,
        *,
        sender_phone: Optional[str] = None,
        purpose: str = "claude_call",
        **create_kwargs: Any,
    ) -> Any:
        """Call ``messages.create`` with up to 2 retries (3 attempts total).

        Between attempts, dispatches a reassuring "thinking deeply" message
        to ``sender_phone`` if provided, then sleeps with exponential backoff
        (2s, 5s). On exhaustion, raises ``ClaudeUnavailable``.

        Logs token usage on success at INFO and per-attempt failures at WARNING.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(len(_RETRY_DELAYS_SEC) + 1):  # 3 attempts
            try:
                response = await self._client.messages.create(**create_kwargs)
                usage = getattr(response, "usage", None)
                in_tokens  = getattr(usage, "input_tokens", None)
                out_tokens = getattr(usage, "output_tokens", None)
                cache_read = getattr(usage, "cache_read_input_tokens", None)
                log.info(
                    "claude.ok purpose=%s phone=%s attempt=%d in=%s out=%s cache_read=%s model=%s",
                    purpose, _mask_phone(sender_phone), attempt + 1,
                    in_tokens, out_tokens, cache_read,
                    create_kwargs.get("model"),
                )
                return response
            except Exception as exc:  # noqa: BLE001 — anthropic raises diverse types
                last_exc = exc
                log.warning(
                    "claude.fail purpose=%s phone=%s attempt=%d/%d err=%s",
                    purpose, _mask_phone(sender_phone),
                    attempt + 1, len(_RETRY_DELAYS_SEC) + 1, exc,
                )
                if attempt >= len(_RETRY_DELAYS_SEC):
                    break

                # Reassure user (best-effort) before backoff. Routes through
                # the channel-agnostic messenger so web and WhatsApp users
                # both get the "thinking deeply" cue.
                if sender_phone:
                    try:
                        # Inline import: avoid circular load at module init.
                        from services.messenger import get_messenger
                        messenger = await get_messenger(sender_phone)
                        await messenger.send_typing_indicator(sender_phone)
                        await messenger.send_text(sender_phone, _REASSURE_MSG)
                    except Exception as send_exc:  # noqa: BLE001
                        log.warning("claude.reassure_send_failed err=%s", send_exc)

                await asyncio.sleep(_RETRY_DELAYS_SEC[attempt])

        log.error(
            "claude.exhausted purpose=%s phone=%s last_err=%s",
            purpose, _mask_phone(sender_phone), last_exc,
        )
        raise ClaudeUnavailable(str(last_exc) if last_exc else "unknown")

    # ── Persona chat ────────────────────────────────────────────────────────

    async def chat_with_persona(self, sender: str, user_text: str) -> str:
        """Free-form coaching reply using the ViMa persona system prompt."""
        response = await self._create_with_retry(
            sender_phone=sender,
            purpose="chat_with_persona",
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=[
                {
                    "type": "text",
                    "text": self._persona,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_text}],
        )
        return _first_text(response)

    # ── Dynamic clarifying question (Q6) ────────────────────────────────────

    async def generate_resume_clarifying_question(
        self,
        profile: dict[str, Any],
        sender_phone: Optional[str] = None,
    ) -> str:
        """Ask the single best clarifying question for a resume rewrite."""
        try:
            response = await self._create_with_retry(
                sender_phone=sender_phone,
                purpose="resume_q6",
                model=settings.claude_model,
                max_tokens=300,
                system=[
                    {
                        "type": "text",
                        "text": self._persona,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Based on this candidate profile, ask ONE focused "
                            "clarifying question (under 25 words) that would most "
                            "strengthen their resume rewrite. Plain text only.\n\n"
                            f"Profile JSON:\n{json.dumps(profile, default=str)[:8000]}"
                        ),
                    }
                ],
            )
            return _first_text(response).strip()
        except ClaudeUnavailable:
            return (
                "What's one specific outcome from your last role you'd want me "
                "to highlight — ideally with a number?"
            )

    # ── Resume generation (the main event) ──────────────────────────────────

    async def generate_resume(
        self,
        *,
        sender_phone:        Optional[str]      = None,
        current_role_target: Optional[str]      = None,
        resume_sources:      Optional[list[dict[str, Any]]] = None,
        resume_text:         Optional[str]      = None,
        linkedin_url:        Optional[str]      = None,
        jd_text:             Optional[str]      = None,
        jd_url:              Optional[str]      = None,
        hiring_manager:      Optional[dict[str, Any]] = None,
        superpower:          Optional[str]      = None,
        q6_question:         Optional[str]      = None,
        q6_answer:           Optional[str]      = None,
        company_research:    Optional[dict[str, Any]] = None,
        edit_instruction:    Optional[str]      = None,
    ) -> dict[str, Any]:
        """Generate a tailored resume as a structured dict.

        ``resume_sources`` is a list of {type, text, filename} entries — the
        candidate may send multiple resume versions plus a LinkedIn export.
        Claude is instructed to *synthesize* across all sources: pick the
        strongest achievements, the most relevant framing, and the most
        complete career timeline rather than picking just one.

        ``resume_text`` is kept as a fallback for the legacy single-source
        callers; it is only used when ``resume_sources`` is empty.

        Steps Claude performs internally:
          1. Analyse the JD for key requirements and ATS keywords.
          2. Map the candidate's experience across ALL sources to those
             requirements; reconcile conflicts using the most-recent or
             best-evidenced version.
          3. Produce a 2-3 page resume with quantified bullets, ATS keywords,
             and grouped skills, conforming to RESUME_SCHEMA in resume.txt.

        Returns a dict matching RESUME_SCHEMA. Falls back to a minimal
        skeleton on parse failure so the downstream PDF render still works.
        """
        user_payload = _build_resume_user_payload(
            current_role_target=current_role_target,
            resume_sources=resume_sources or [],
            resume_text=resume_text,
            linkedin_url=linkedin_url,
            jd_text=jd_text,
            jd_url=jd_url,
            hiring_manager=hiring_manager,
            superpower=superpower,
            q6_question=q6_question,
            q6_answer=q6_answer,
            company_research=company_research,
            edit_instruction=edit_instruction,
        )

        # Retries are handled inside `_create_with_retry`; on exhaustion this
        # raises ClaudeUnavailable which the flow catches and surfaces.
        response = await self._create_with_retry(
            sender_phone=sender_phone,
            purpose="generate_resume",
            model=settings.claude_model,
            max_tokens=_RESUME_MAX_TOKENS,
            system=[
                # Persona first — cached. Resume schema spec also cached.
                {
                    "type": "text",
                    "text": self._persona,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": self._resume_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[
                {"role": "user", "content": user_payload},
            ],
        )
        raw = _first_text(response)
        parsed = _safe_parse_json(raw)
        if not parsed:
            log.warning("Could not parse resume JSON; returning fallback skeleton.")
            return _resume_fallback(resume_text, current_role_target)
        return parsed

    # ── Strategy notes (sent alongside the PDF preview) ─────────────────────

    async def generate_strategy_notes(
        self,
        *,
        sender_phone: Optional[str]             = None,
        resume_data:  dict[str, Any],
        current_role_target: Optional[str]      = None,
        jd_text:      Optional[str]             = None,
        hiring_manager: Optional[dict[str, Any]] = None,
        superpower:   Optional[str]             = None,
        company_research: Optional[dict[str, Any]] = None,
    ) -> str:
        """Return a short paragraph (2-4 sentences) explaining positioning choices.

        Examples of what this should cover:
          - Why the headline / summary leads with X
          - Which JD keywords were emphasised and where
          - What was de-prioritised or trimmed and why
          - Any hiring-manager / company signal that shaped the framing
        """
        prompt = (
            "You just generated the resume below for this candidate. Now write "
            "a *strategy note* the candidate will read in WhatsApp before "
            "opening the PDF.\n\n"
            "Rules:\n"
            "- 2-4 sentences. No bullet points. No emojis.\n"
            "- Plain WhatsApp text, *bold* allowed for emphasis.\n"
            "- Explain WHY you made the top 1-3 positioning choices (lead-in, "
            "  keyword emphasis, what you trimmed). Tie each choice to the JD, "
            "  the hiring manager, the company, or the candidate's superpower.\n"
            "- Sound like a senior friend, not a marketer. No hype words.\n"
            "- Output only the paragraph — no preamble, no headings.\n\n"
            f"Target role: {current_role_target or '(unspecified)'}\n"
            f"Candidate's superpower: {superpower or '(not given)'}\n"
            f"Hiring manager: {json.dumps(hiring_manager) if hiring_manager else '(skipped)'}\n"
            f"Company research keys: "
            f"{list((company_research or {}).keys())}\n\n"
            f"JD excerpt:\n{(jd_text or '')[:2500]}\n\n"
            f"Resume just generated (JSON):\n"
            f"{json.dumps(resume_data, default=str)[:8000]}"
        )

        try:
            response = await self._create_with_retry(
                sender_phone=sender_phone,
                purpose="strategy_notes",
                model=settings.claude_model,
                max_tokens=_STRATEGY_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": self._persona,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            return _first_text(response).strip()
        except ClaudeUnavailable:
            return (
                "I led with your strongest, role-relevant impact and mirrored "
                "the JD's top keywords in the summary and skills. Older roles "
                "are condensed so recent, on-target work gets the page space."
            )

    # ── Interview prep kit ──────────────────────────────────────────────────

    async def generate_interview_prep(
        self,
        *,
        sender_phone:     Optional[str]            = None,
        company:          Optional[str]            = None,
        target_role:      Optional[str]            = None,
        jd_text:          Optional[str]            = None,
        jd_url:           Optional[str]            = None,
        round_label:      Optional[str]            = None,
        round_kind:       Optional[str]            = None,
        hiring_manager:   Optional[dict[str, Any]] = None,
        interviewers:     Optional[list[dict[str, Any]]] = None,
        prior_experience: Optional[str]            = None,
        concern:          Optional[str]            = None,
        resume_sources:   Optional[list[dict[str, Any]]] = None,
        superpower:       Optional[str]            = None,
        company_research: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Return a structured interview prep kit conforming to PREP_SCHEMA below.

        PREP_SCHEMA:
          {
            "company_brief": {
              "summary":       str,                       # 4-6 sentence overview
              "recent_news":   [str, ...],                # 3-5 items, last 12 months
              "culture":       [str, ...],                # 3-5 cultural signals
              "investor_thesis": str | null,              # 2-3 sentences if available
              "challenges":    [str, ...]                 # 3-5 strategic challenges
            },
            "likely_questions": [
              {
                "question":    str,
                "category":    "behavioral" | "technical" | "situational",
                "why_asked":   str                        # 1 sentence rationale
              }, ... 5-10 items
            ],
            "suggested_answers": [
              {
                "question":    str,                       # echo of the q
                "star": {
                  "situation": str,
                  "task":      str,
                  "action":    str,
                  "result":    str
                },
                "anchor_evidence": str | null             # which role/project this draws from
              }, ... covers the top 4-6 likely_questions
            ],
            "questions_to_ask": [
              {
                "question":    str,
                "signals":     str                        # what asking this conveys
              }, ... 2-3 items
            ],
            "red_flags": [
              {
                "mistake":     str,                       # the common mistake
                "instead":     str                        # what to do instead
              }, ... 3-5 items
            ]
          }

        Falls back to a minimal valid skeleton on parse / API failure.
        """
        user_payload = _build_interview_prep_payload(
            company=company, target_role=target_role,
            jd_text=jd_text, jd_url=jd_url,
            round_label=round_label, round_kind=round_kind,
            hiring_manager=hiring_manager, interviewers=interviewers or [],
            prior_experience=prior_experience, concern=concern,
            resume_sources=resume_sources or [],
            superpower=superpower, company_research=company_research,
        )

        # Retries handled inside _create_with_retry; on exhaustion this raises
        # ClaudeUnavailable which the flow catches and surfaces.
        response = await self._create_with_retry(
            sender_phone=sender_phone,
            purpose="interview_prep",
            model=settings.claude_model,
            max_tokens=_RESUME_MAX_TOKENS,  # prep kit ≈ same volume as resume
            system=[
                {"type": "text", "text": self._persona,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _INTERVIEW_PREP_SYSTEM,
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[
                {"role": "user", "content": user_payload},
            ],
        )
        raw = _first_text(response)
        parsed = _safe_parse_json(raw)
        if not parsed:
            log.warning("Could not parse prep JSON; returning fallback skeleton.")
            return _interview_prep_fallback(company, target_role, round_label)
        return parsed

    # ── Compatibility shim: old callers ─────────────────────────────────────

    async def rewrite_resume(
        self,
        parsed_resume: dict[str, Any],
        research:      dict[str, Any],
        state:         dict[str, Any],
        sender_phone:  Optional[str] = None,
    ) -> dict[str, Any]:
        """Backwards-compat wrapper that forwards to generate_resume()."""
        return await self.generate_resume(
            sender_phone        = sender_phone,
            current_role_target = state.get("current_role_target"),
            resume_sources      = state.get("resume_sources"),
            resume_text         = parsed_resume.get("raw_text") or state.get("resume_text"),
            linkedin_url        = state.get("linkedin_url"),
            jd_text             = state.get("jd_text"),
            jd_url              = state.get("jd_url"),
            hiring_manager      = state.get("hiring_manager"),
            superpower          = state.get("superpower"),
            q6_question         = state.get("q6_question"),
            q6_answer           = state.get("q6_answer"),
            company_research    = research or state.get("company_research"),
        )

    # ── Interview helpers (unchanged stubs) ─────────────────────────────────

    async def next_interview_question(
        self,
        state: dict[str, Any],
        last_answer: str,
        sender_phone: Optional[str] = None,
    ) -> str:
        # TODO: thread through _create_with_retry once a real implementation lands.
        _ = (state, last_answer, sender_phone)
        return ""

    async def interview_feedback(
        self,
        state: dict[str, Any],
        sender_phone: Optional[str] = None,
    ) -> str:
        # TODO: thread through _create_with_retry once a real implementation lands.
        _ = (state, sender_phone)
        return ""


# ── Module helpers ──────────────────────────────────────────────────────────

def _first_text(response: Any) -> str:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _build_resume_user_payload(**fields: Any) -> str:
    """Render the user-turn for generate_resume.

    Keeps the structure stable so prompt caching (system blocks above) hits.
    """
    hm = fields.get("hiring_manager")
    cr = fields.get("company_research") or {}
    sources: list[dict[str, Any]] = list(fields.get("resume_sources") or [])

    multi_source = len(sources) > 1

    sections = [
        "I need a 2-3 page resume tailored to the target role described "
        "below. Output JSON ONLY, conforming exactly to RESUME_SCHEMA "
        "from the system prompt. Do not wrap in markdown fences.",
        "",
        "Process internally before writing the JSON:",
        "  1. Analyse the JD for the top 8-12 requirements and ATS keywords.",
        "  2. SYNTHESIZE across ALL candidate sources below (multiple resume "
        "     versions, LinkedIn exports, LinkedIn URLs). For each role, "
        "     pick the strongest achievements across versions, prefer the "
        "     most quantified phrasing, and reconcile dates/titles using "
        "     the most recent or best-evidenced source. Build the most "
        "     complete career timeline — do not drop a role just because "
        "     it's missing from one version.",
        "  3. Map evidence to JD requirements; lead the summary and the "
        "     most-recent role with the strongest mapped material. Mirror "
        "     keywords naturally; no stuffing. Never invent metrics.",
        "  4. Group skills under 2-4 categories (e.g. Functional, Technical, "
        "     Leadership) when there are 8+ skills; otherwise a flat list.",
        "  5. Use 4-6 quantified bullets per recent role; 3 for older ones.",
        "",
        "── TARGET ─────────────────────────────────",
        f"Current role + target level: {fields.get('current_role_target') or '(not provided)'}",
    ]

    if fields.get("jd_text"):
        sections += [
            "",
            "── JOB DESCRIPTION (full text) ────────────",
            fields["jd_text"][:12000],
        ]
    elif fields.get("jd_url"):
        sections += [
            "",
            "── JOB DESCRIPTION (link only) ────────────",
            f"URL: {fields['jd_url']}",
            "(Full JD text was not extracted; use the URL + title as signal "
            " and lean harder on the candidate's own material.)",
        ]

    if hm:
        sections += [
            "",
            "── HIRING MANAGER ─────────────────────────",
            json.dumps(hm, ensure_ascii=False),
        ]

    if cr:
        sections += [
            "",
            "── COMPANY / ROLE RESEARCH ────────────────",
            json.dumps(cr, ensure_ascii=False, default=str)[:6000],
        ]

    if fields.get("superpower"):
        sections += [
            "",
            "── CANDIDATE'S SUPERPOWER (their words) ──",
            fields["superpower"],
        ]

    if fields.get("q6_question") or fields.get("q6_answer"):
        sections += [
            "",
            "── CLARIFYING Q&A ─────────────────────────",
            f"Q: {fields.get('q6_question') or '(none)'}",
            f"A: {fields.get('q6_answer') or '(none)'}",
        ]

    if fields.get("linkedin_url"):
        sections += [
            "",
            "── CANDIDATE'S LINKEDIN URL ───────────────",
            fields["linkedin_url"],
        ]

    # ── Sources block ─────────────────────────────────────────────────────
    if sources:
        header = (
            "── CANDIDATE'S RESUME SOURCES "
            f"({len(sources)} provided — synthesize across all) ──"
        )
        sections += ["", header]

        # Budget per source so we don't blow past the model's window.
        per_source_budget = max(2500, 18000 // max(len(sources), 1))

        for idx, src in enumerate(sources, start=1):
            kind     = src.get("type", "resume")
            filename = src.get("filename") or "(no filename)"
            text     = (src.get("text") or "").strip()

            if kind == "linkedin_url":
                sections += [
                    f"\n[Source {idx}: LinkedIn URL]",
                    f"URL: {text or '(empty)'}",
                ]
            elif kind == "linkedin_pdf":
                sections += [
                    f"\n[Source {idx}: LinkedIn PDF export — {filename}]",
                    text[:per_source_budget] or "(no extractable text)",
                ]
            else:
                sections += [
                    f"\n[Source {idx}: Resume — {filename}]",
                    text[:per_source_budget] or "(no extractable text)",
                ]

        if multi_source:
            sections += [
                "",
                "Synthesis reminder: where the same role appears in more "
                "than one source, MERGE the bullets — keep the strongest, "
                "most quantified phrasing — instead of picking only one "
                "version. Use older sources to fill timeline gaps.",
            ]
    else:
        # Legacy single-string fallback.
        sections += [
            "",
            "── EXISTING RESUME (parsed text) ──────────",
            (fields.get("resume_text") or "(no existing resume provided)")[:18000],
        ]

    if fields.get("edit_instruction"):
        sections += [
            "",
            "── REVISION INSTRUCTION (from the candidate) ──",
            fields["edit_instruction"],
            "Apply this revision across the entire resume where relevant. "
            "Keep everything else unchanged.",
        ]

    sections += [
        "",
        "IMPORTANT: Respond with ONLY a valid JSON object matching RESUME_SCHEMA. "
        "No preamble, no explanation, no markdown fences. "
        "Start your response directly with { and end with }.",
    ]
    return "\n".join(sections)


def _safe_parse_json(text: str) -> Optional[dict[str, Any]]:
    """Parse JSON from a model response, tolerating fences and trailing prose."""
    if not text:
        return None
    candidate = text.strip()

    # Strip optional ```json fences.
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)

    # Try direct parse first.
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fallback: extract the largest balanced { ... } substring.
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = candidate[start : i + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    return None
    return None


def _resume_fallback(
    resume_text: Optional[str],
    current_role_target: Optional[str],
) -> dict[str, Any]:
    """Minimal RESUME_SCHEMA-shaped dict used when Claude generation fails.

    The PDF will still render; the user gets a clear message in the strategy
    note and can retry. Better to produce *something* than to drop the work.
    """
    return {
        "name":     "",
        "headline": (current_role_target or "")[:120],
        "contact":  {},
        "summary":  (resume_text or "").strip()[:600],
        "skills":   [],
        "experience":  [],
        "education":   [],
        "certifications": [],
        "projects":    [],
        "achievements":[],
        "languages":   [],
    }


# ── Interview prep payload + fallback ───────────────────────────────────────

def _build_interview_prep_payload(**fields: Any) -> str:
    """User-turn for generate_interview_prep. Stable shape for cache hits."""
    sources: list[dict[str, Any]] = list(fields.get("resume_sources") or [])
    interviewers = fields.get("interviewers") or []
    cr = fields.get("company_research") or {}

    sections = [
        "Generate the interview prep kit JSON for this candidate's upcoming "
        "interview. Conform exactly to PREP_SCHEMA from the system prompt.",
        "",
        "── INTERVIEW CONTEXT ──────────────────────",
        f"Company:       {fields.get('company') or '(not provided)'}",
        f"Target role:   {fields.get('target_role') or '(not provided)'}",
        f"Round:         {fields.get('round_label') or '(not specified)'} "
        f"[{fields.get('round_kind') or 'mixed'}]",
    ]

    if fields.get("jd_text"):
        sections += [
            "",
            "── JOB DESCRIPTION ────────────────────────",
            fields["jd_text"][:8000],
        ]
    elif fields.get("jd_url"):
        sections += [
            "",
            "── JOB DESCRIPTION (URL only) ────────────",
            f"URL: {fields['jd_url']}",
            "(JD text was not extracted — use URL + role title as signal.)",
        ]

    if fields.get("hiring_manager"):
        sections += [
            "",
            "── HIRING MANAGER ─────────────────────────",
            json.dumps(fields["hiring_manager"], ensure_ascii=False),
        ]

    if interviewers:
        sections += [
            "",
            "── INTERVIEWERS (this round) ──────────────",
            json.dumps(interviewers, ensure_ascii=False)[:3000],
        ]

    if fields.get("prior_experience"):
        sections += [
            "",
            "── CANDIDATE'S PRIOR EXPERIENCE WITH THIS COMPANY ──",
            str(fields["prior_experience"])[:2000],
        ]

    if fields.get("concern"):
        sections += [
            "",
            "── CANDIDATE'S BIGGEST CONCERN ────────────",
            str(fields["concern"])[:1500],
            "(Address this somewhere in the kit — usually a red_flag entry "
            "or by including a likely_question that prepares them for it.)",
        ]

    if fields.get("superpower"):
        sections += [
            "",
            "── CANDIDATE'S SUPERPOWER (their words) ──",
            str(fields["superpower"])[:1500],
        ]

    if cr:
        sections += [
            "",
            "── COMPANY / ROLE RESEARCH (pre-fetched) ──",
            json.dumps(cr, ensure_ascii=False, default=str)[:6000],
        ]

    if sources:
        sections += [
            "",
            f"── CANDIDATE'S RESUME SOURCES ({len(sources)}) ──",
        ]
        per_budget = max(1500, 12000 // max(len(sources), 1))
        for idx, src in enumerate(sources, start=1):
            kind = src.get("type", "resume")
            fn   = src.get("filename") or "(no filename)"
            txt  = (src.get("text") or "").strip()
            if kind == "linkedin_url":
                sections += [f"\n[Source {idx}: LinkedIn URL] {txt or '(empty)'}"]
            else:
                label = "LinkedIn PDF" if kind == "linkedin_pdf" else "Resume"
                sections += [
                    f"\n[Source {idx}: {label} — {fn}]",
                    txt[:per_budget] or "(no extractable text)",
                ]

    sections += [
        "",
        "IMPORTANT: Respond with ONLY a valid JSON object matching PREP_SCHEMA. "
        "No preamble, no explanation, no markdown fences. "
        "Start your response directly with { and end with }.",
    ]
    return "\n".join(sections)


def _interview_prep_fallback(
    company: Optional[str],
    target_role: Optional[str],
    round_label: Optional[str],
) -> dict[str, Any]:
    """Minimal valid PREP_SCHEMA dict used when generation fails."""
    co = company or "the company"
    rl = round_label or "this round"
    return {
        "company_brief": {
            "summary":          f"Prep kit for {target_role or 'the role'} at {co} "
                                f"could not be auto-generated this time. Use the "
                                f"sections below as a starting checklist.",
            "recent_news":      [],
            "culture":          [],
            "investor_thesis":  None,
            "challenges":       [],
        },
        "likely_questions": [
            {"question": "Walk me through your background.",
             "category": "behavioral",
             "why_asked": f"Standard opener in {rl}; sets tone and pace."},
            {"question": "Why this role and why now?",
             "category": "behavioral",
             "why_asked": "Tests motivation and fit."},
        ],
        "suggested_answers": [],
        "questions_to_ask": [
            {"question": "What does success in the first 90 days look like for this role?",
             "signals":  "Outcome-orientation and ownership."},
        ],
        "red_flags": [
            {"mistake": "Generic answers that don't tie to the company's stage.",
             "instead": "Anchor every answer in a concrete moment from your own experience."},
        ],
    }
