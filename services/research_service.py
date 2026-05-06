"""Role / company research helper.

Fetches public signal about a target role + company so the resume/interview
flows can tailor outputs (job description keywords, company values, recent
news). Defaults to lightweight web search; can swap in better providers.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


async def research_role(target: str) -> dict[str, Any]:
    """Return a research summary for a 'role at company' string."""
    if not target:
        return {}

    # TODO: pluggable backends - SerpAPI, Tavily, Brave, or Claude web tool.
    summary = await _basic_search(target)
    return {
        "target":        target,
        "summary":       summary,
        "keywords":      [],
        "company_values": [],
        "recent_news":   [],
    }


async def research_person(name: str, company: str) -> str:
    """Return a 150-200 word summary of a hiring manager's public profile.

    Searches for "[name] [company]", plus title/article/talk variants and
    domain-specific sources. Returns an empty string if the name is blank or
    no useful signal is found.
    """
    if not name or not name.strip():
        return ""

    name    = name.strip()
    company = (company or "").strip()

    queries = [
        f"{name} {company}",
        f"{name} {company} interview OR article OR talk",
        f"{name} {company} site:linkedin.com OR site:bloomberg.com OR site:forbes.com",
    ]

    snippets: list[str] = []
    async with httpx.AsyncClient(timeout=10.0, headers=_BROWSER_HEADERS, follow_redirects=True) as client:
        for query in queries:
            try:
                # DuckDuckGo HTML search — no API key needed; best-effort.
                resp = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                )
                if resp.status_code == 200:
                    text = _strip_html(resp.text)
                    if len(text) > 100:
                        snippets.append(text[:2000])
            except Exception as exc:  # noqa: BLE001
                log.debug("research_person search failed for %r: %s", query, exc)

    if not snippets:
        return ""

    # Use Claude to compress all snippets into a 150-200 word summary.
    try:
        from services.claude_service import ClaudeService
        claude = ClaudeService()
        combined = "\n\n---\n\n".join(snippets)[:8000]
        prompt = (
            f"Based on the following search results about {name}"
            + (f" at {company}" if company else "")
            + ", write a 150-200 word professional summary covering:\n"
            "1. Confirmed current role and seniority\n"
            "2. Career background and known previous roles\n"
            "3. Any notable public statements, articles, or talks\n"
            "4. Recent context at their current company\n\n"
            "Be factual — only state what the search results clearly support. "
            "If information is absent or unclear, skip that point. No hype.\n\n"
            f"Search results:\n{combined}"
        )
        summary = await claude._create_with_retry(
            purpose="research_person",
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        from services.claude_service import _first_text
        return _first_text(summary).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("research_person summarise failed: %s", exc)
        # Return raw snippets truncated as a fallback.
        return " ".join(snippets)[:500]


async def fetch_url_text(url: str, min_chars: int = 200) -> Optional[str]:
    """Fetch a URL and return its readable text content, or None on failure.

    Returns None if:
    - HTTP status is not 200 (e.g. 403 / 429 / redirect loop)
    - Extracted text is shorter than min_chars
    - Request times out or raises a network error
    """
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                log.debug("fetch_url_text status=%d url=%s", resp.status_code, url)
                return None
            text = _strip_html(resp.text)
            if len(text) < min_chars:
                log.debug("fetch_url_text too short len=%d url=%s", len(text), url)
                return None
            return text
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_url_text failed url=%s err=%s", url, exc)
        return None


async def _basic_search(query: str) -> str:
    # TODO: replace with real search provider; placeholder noop.
    _ = query
    return ""


def _strip_html(html: str) -> str:
    """Very lightweight HTML-to-text: strip tags, collapse whitespace."""
    # Remove script/style blocks.
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html, flags=re.S | re.I)
    # Strip all remaining tags.
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities.
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(ent, ch)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text
