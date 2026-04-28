"""Role / company research helper.

Fetches public signal about a target role + company so the resume/interview
flows can tailor outputs (job description keywords, company values, recent
news). Defaults to lightweight web search; can swap in better providers.
"""

from typing import Any

import httpx


async def research_role(target: str) -> dict[str, Any]:
    """Return a research summary for a 'role at company' string."""
    if not target:
        return {}

    # TODO: pluggable backends - SerpAPI, Tavily, Brave, or Claude web tool.
    summary = await _basic_search(target)
    return {
        "target": target,
        "summary": summary,
        "keywords": [],          # TODO: extract role-specific keywords.
        "company_values": [],    # TODO: scrape "About" / careers page.
        "recent_news": [],       # TODO: latest 30-day news headlines.
    }


async def _basic_search(query: str) -> str:
    # TODO: replace with real search provider; placeholder noop.
    _ = query
    async with httpx.AsyncClient(timeout=10.0) as client:
        _ = client
    return ""
