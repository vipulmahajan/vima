"""Helpers for warmly redirecting off-topic messages back into a flow.

When a user is mid-flow and replies with something the handler can't parse,
we want the re-prompt to feel like a friend gently steering — not a robot
saying "invalid input". This module detects "off-topic-feeling" text and
prepends a warm acknowledgement to the re-prompt.

Convention: re-prompt sites pass the raw user text + the question they
want to repeat. ``warm_reprompt(text, question, flow="resume")`` returns
a ready-to-send body string.
"""

from __future__ import annotations

import re

# ── Off-topic detection ────────────────────────────────────────────────────

_GREETING_TOKENS = {
    "hi", "hii", "hey", "hello", "namaste", "yo", "sup", "hola",
    "good morning", "good afternoon", "good evening", "thanks", "thank you",
    "ok", "okay", "cool", "great", "nice", "lol", "haha", "fine",
}

_QUESTION_OPENERS = (
    "what ", "how ", "why ", "when ", "where ", "who ",
    "can you", "could you", "would you", "do you", "are you",
    "is it", "is this", "tell me about",
)


def looks_off_topic(text: str) -> bool:
    """Return True if the text looks like a greeting / random question.

    The heuristic is intentionally loose. We'd rather over-reassure than
    leave a user feeling unheard.
    """
    if not text:
        return False
    t = text.strip().lower()
    if len(t) < 3:
        return True
    if t in _GREETING_TOKENS:
        return True
    if t.endswith("?"):
        return True
    if any(t.startswith(opener) for opener in _QUESTION_OPENERS):
        return True
    # "what ... ?" mid-sentence questions.
    if re.search(r"\b(what|why|how|when|where|who)\b.*\?", t):
        return True
    return False


# ── Re-prompt builder ──────────────────────────────────────────────────────

_FLOW_NOUN = {
    "resume":    "resume sharp",
    "interview": "interview prep on track",
}


def warm_reprompt(
    user_text: str,
    question: str,
    flow: str = "resume",
) -> str:
    """If the user's text looks off-topic, prepend a warm redirect.

    Otherwise just return the question itself — over-prefixing on legitimate
    re-prompts (e.g. "input too short") would feel patronising.
    """
    noun = _FLOW_NOUN.get(flow, "this on track")
    if looks_off_topic(user_text):
        return (
            f"I hear you — let's stay focused on getting your {noun} first.\n\n"
            f"{question}"
        )
    return question
