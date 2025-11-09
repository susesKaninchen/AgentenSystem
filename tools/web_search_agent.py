"""
Asynchronous WebSearchTool integration via the OpenAI Agents SDK.

Provides a thin wrapper that runs a dedicated search agent with the hosted
`WebSearchTool` so we can avoid hand-written DuckDuckGo/Google scraping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Sequence

from agents import Agent, Runner
from agents.model_settings import ModelSettings
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tool import WebSearchTool


@dataclass
class WebSearchAgentResult:
    query: str
    title: str
    url: str
    snippet: str
    source: str = "web_tool"


def _extract_json_block(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return "{}"


async def run_web_search_agent(
    model: OpenAIResponsesModel,
    *,
    query: str,
    max_results: int,
    location_hint: str | None = None,
) -> List[WebSearchAgentResult]:
    """
    Executes a web search through OpenAI's hosted WebSearchTool.
    """

    location_note = (
        f"Die Ergebnisse sollen sich, wenn möglich, auf {location_hint} beziehen.\n"
        if location_hint
        else ""
    )
    agent = Agent(
        name="WebSearchAgent",
        instructions=(
            "Du recherchierst zielsicher nach nicht-kommerziellen Maker:innen, Hackspaces, "
            "offenen Werkstätten und ähnlichen Projekten. "
            "Nutze das bereitgestellte web_search Tool und antworte ausschließlich mit JSON:\n"
            '{"results": [{"title": "...", "url": "...", "snippet": "..."}]}\n'
            "Gib maximal die angeforderte Anzahl an Treffern zurück. "
            "Konzentriere dich auf Quellen, die echte Projekte oder Vereine beschreiben."
        ),
        model=model,
        tools=[WebSearchTool()],
        model_settings=ModelSettings(tool_choice="required"),
    )
    prompt = (
        f"{location_note}"
        "Auftrag:\n"
        f"Suche nach: {query}\n"
        f"Gib bis zu {max_results} qualitativ hochwertige Treffer zurueck."
        "Fasse jeden Treffer in 30-40 Woertern zusammen."
    )
    result = await Runner.run(agent, prompt)
    raw = result.final_output or "{}"
    try:
        data = json.loads(_extract_json_block(raw))
    except json.JSONDecodeError:
        data = {}

    entries: Sequence[dict] = data.get("results") or []
    parsed: List[WebSearchAgentResult] = []
    for item in entries:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if not title or not url:
            continue
        parsed.append(
            WebSearchAgentResult(
                query=query,
                title=title,
                url=url,
                snippet=snippet,
            )
        )
    return parsed
