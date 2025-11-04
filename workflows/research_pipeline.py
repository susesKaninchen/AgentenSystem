"""
Planner -> DuckDuckGo-Suche -> Evaluator -> Writer Pipeline.

Der Workflow läuft vollständig automatisiert:
- Planner erstellt einen strukturierten Plan inklusive Suchqueries.
- DuckDuckGo liefert Treffer, die iterativ bewertet werden.
- Ein Evaluator-Agent filtert ungeeignete Kandidaten und liefert Feedback.
- Bei Bedarf generiert ein Query-Refiner neue Suchen, bis genügend Kandidaten akzeptiert sind.
- Akzeptierte Kandidaten werden via NorthData angereichert und in Anschreiben überführt.

Zwischenstände (Suche, Evaluationsentscheidungen, NorthData-Ergebnisse) werden persistiert,
so dass der Ablauf auditierbar bleibt.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from tools.identity_loader import get_identity_summary, load_identity
from tools.duckduckgo import (
    DuckDuckGoResult,
    iter_queries,
    search_duckduckgo,
    store_search_results,
)
from tools.northdata import (
    NorthDataError,
    fetch_suggestions,
    format_top_suggestion,
    store_suggestions,
)


STAGING_NOTES = Path("data/staging/research_notes.md")
STAGING_ACCEPTED = Path("data/staging/candidates_selected.json")
OUTPUT_DIR = Path("outputs/letters")
ENV_PATH = Path(".env")

DEFAULT_TARGET_CANDIDATES = 50
MAX_ITERATIONS = 5
MAX_RESULTS_PER_QUERY = 6
EVALUATION_ACCEPT_THRESHOLD = 0.62
NORTHDATA_COUNTRIES = "DE"


@dataclass
class PlannerPlan:
    steps: List[str]
    search_queries: List[str]
    target_candidates: int


@dataclass
class EvaluationResult:
    score: float
    accepted: bool
    reason: str
    search_adjustment: str


@dataclass
class CandidateInfo:
    name: str
    url: str
    summary: str
    source_query: str
    snippet: str
    notes: str = ""
    northdata_info: str = ""
    evaluation: Optional[EvaluationResult] = field(default=None, repr=False)

    def as_markdown(self) -> str:
        base = (
            f"## {self.name}\n"
            f"- URL: {self.url}\n"
            f"- Quelle: {self.source_query}\n"
            f"- Kurzbeschreibung: {self.summary or self.snippet}\n"
            f"- Bewertung: {self.notes or (self.evaluation.reason if self.evaluation else 'Noch nicht bewertet')}\n"
        )
        if self.northdata_info:
            base += f"- NorthData: {self.northdata_info}\n"
        return base


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def ensure_required_env(vars_: Iterable[str]) -> None:
    missing = [var for var in vars_ if not os.environ.get(var)]
    if missing:
        raise RuntimeError(
            "Diese Umgebungsvariablen werden benötigt, sind aber nicht gesetzt: "
            + ", ".join(missing)
        )


def ensure_dirs() -> None:
    STAGING_NOTES.parent.mkdir(parents=True, exist_ok=True)
    STAGING_ACCEPTED.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def build_model() -> OpenAIChatCompletionsModel:
    model_name = os.environ["OPENAI_MODEL"]
    base_url = os.environ["OPENAI_BASE_URL"]
    api_key = os.environ.get("OPENAI_API_KEY", "")
    os.environ.setdefault("OPENAI_DEFAULT_MODEL", model_name)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    print(f"Verwende Endpoint: {client.base_url}")
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def extract_json_block(text: str) -> str:
    """
    Versucht, aus einem LLM-Output den JSON-Teil zu extrahieren.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


async def run_planner(
    model: OpenAIChatCompletionsModel, task: str, identity_summary: str
) -> tuple[PlannerPlan, str]:
    agent = Agent(
        name="Planner",
        instructions=(
            "Du planst eine Recherche nach Ausstellenden für die Maker Faire Lübeck 2026. "
            "Gib ausschließlich JSON zurück mit den Feldern:\n"
            '{"plan_steps": ["Schritt 1", ...], "search_queries": ["query1", ...], "target_candidates": 50}\n'
            "Die Suchqueries sollen konkrete Websuchen für passende Maker-/Technikprojekte sein. "
            "Erzeuge mindestens 5 Queries. Verwende keine doppelten Werte.\n"
            "Gib nur JSON zurück, kein Fließtext."
        ),
        model=model,
    )
    prompt = (
        "Auftrag:\n"
        f"{task}\n\n"
        "Identität des Auftraggebers:\n"
        f"{identity_summary}\n"
        "Erstelle Rechercheplan."
    )
    result = await Runner.run(agent, prompt)
    raw_output = result.final_output or ""
    try:
        plan_data = json.loads(extract_json_block(raw_output))
    except json.JSONDecodeError:
        plan_data = {}

    steps = [step.strip() for step in plan_data.get("plan_steps", []) if step and isinstance(step, str)]
    queries = [q.strip() for q in plan_data.get("search_queries", []) if q and isinstance(q, str)]
    target = plan_data.get("target_candidates", DEFAULT_TARGET_CANDIDATES)
    if not isinstance(target, int) or target <= 0:
        target = DEFAULT_TARGET_CANDIDATES

    if len(queries) < 3:
        queries.extend(DEFAULT_FALLBACK_QUERIES)

    return PlannerPlan(steps=steps or DEFAULT_PLAN_STEPS, search_queries=queries, target_candidates=target), raw_output


DEFAULT_PLAN_STEPS = [
    "Thematische Schwerpunkte und Zielgruppen klären.",
    "Passende Maker-/Technikprojekte online recherchieren.",
    "Treffer bewerten, anreichern und zur Ansprache vorbereiten.",
]

DEFAULT_FALLBACK_QUERIES = [
    "Maker Faire Projekte Schleswig-Holstein",
    "DIY Robotics Verein Deutschland",
    "Makerspace Lübeck Projekte",
    "Offene Werkstatt Bildung Projekte Deutschland",
    "Jugend Robotik Club Deutschland",
]


def build_candidates_from_search(
    query: str, results: Sequence[DuckDuckGoResult]
) -> List[CandidateInfo]:
    candidates: List[CandidateInfo] = []
    for item in results:
        if not item.url:
            continue
        title = item.title or item.url
        summary = item.snippet or ""
        candidate = CandidateInfo(
            name=title.strip(),
            url=item.url.strip(),
            summary=summary.strip(),
            source_query=query,
            snippet=item.snippet or "",
        )
        candidates.append(candidate)
    return candidates


async def evaluate_candidate(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
) -> EvaluationResult:
    agent = Agent(
        name="Evaluator",
        instructions=(
            "Bewerte, ob ein Projekt/Organisation zu einer Maker Faire passt. "
            "Antworte ausschließlich als JSON mit Feldern:\n"
            '{"score": 0.0-1.0, "accepted": true/false, "reason": "...", "search_adjustment": "..."}\n'
            "score beschreibt die Passung (>=0.62 akzeptiert). "
            '"search_adjustment" enthält einen Hinweis, wie künftige Queries präzisiert werden können '
            "(z. B. \"mehr Bildungspartner\" oder \"weniger reine Händler\"). "
            "Wenn kein Hinweis nötig ist, verwende einen leeren String."
        ),
        model=model,
    )
    prompt = (
        "Identität des Auftraggebers:\n"
        f"{identity_summary}\n\n"
        "Kandidat:\n"
        f"Name: {candidate.name}\n"
        f"URL: {candidate.url}\n"
        f"Quelle-Query: {candidate.source_query}\n"
        f"Zusammenfassung: {candidate.summary or candidate.snippet}\n"
        "Bewerte die Passung."
    )
    result = await Runner.run(agent, prompt)
    output = extract_json_block(result.final_output or "")
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        data = {"score": 0.0, "accepted": False, "reason": "Bewertung fehlgeschlagen.", "search_adjustment": "präzisere Suchbegriffe verwenden"}

    score = float(data.get("score", 0.0))
    accepted = bool(data.get("accepted", score >= EVALUATION_ACCEPT_THRESHOLD))
    reason = str(data.get("reason", "")).strip() or "Keine Begründung angegeben."
    adjustment = str(data.get("search_adjustment", "")).strip()
    return EvaluationResult(
        score=max(0.0, min(score, 1.0)),
        accepted=accepted,
        reason=reason,
        search_adjustment=adjustment,
    )


async def refine_queries(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    base_task: str,
    feedback_hints: Sequence[str],
    used_queries: Iterable[str],
    missing: int,
) -> List[str]:
    hints = [hint for hint in feedback_hints if hint]
    if not hints:
        return []

    agent = Agent(
        name="QueryRefiner",
        instructions=(
            "Erzeuge neue DuckDuckGo-Suchqueries, um weitere passende Maker-Projekte zu finden. "
            "Antworte ausschließlich im JSON-Format: {\"new_queries\": [\"...\"]}. "
            "Meide bereits verwendete Queries und fokussiere dich auf die Hinweise."
        ),
        model=model,
    )
    prompt = (
        f"Auftrag: {base_task}\n\n"
        "Identität:\n"
        f"{identity_summary}\n\n"
        f"Noch benötigte Kandidaten: {missing}\n"
        "Bereits verwendete Queries:\n"
        + "\n".join(f"- {query}" for query in used_queries)
        + "\n\n"
        "Hinweise aus bisherigen Bewertungen:\n"
        + ("\n".join(f"- {hint}" for hint in hints) if hints else "- keine")
        + "\n\n"
        "Erzeuge maximal 5 neue Queries."
    )
    result = await Runner.run(agent, prompt)
    try:
        data = json.loads(extract_json_block(result.final_output or ""))
    except json.JSONDecodeError:
        return []
    queries = [q.strip() for q in data.get("new_queries", []) if isinstance(q, str) and q.strip()]
    return queries


def enrich_with_northdata(candidates: Sequence[CandidateInfo]) -> None:
    for candidate in candidates:
        query = candidate.name
        try:
            suggestions = fetch_suggestions(query, countries=NORTHDATA_COUNTRIES)
        except NorthDataError as exc:
            candidate.northdata_info = f"NorthData-Fehler: {exc}"
            continue

        store_suggestions(query, suggestions)
        if suggestions:
            candidate.northdata_info = format_top_suggestion(suggestions)
        else:
            candidate.northdata_info = "NorthData: keine Treffer."


def store_candidates_snapshot(
    accepted: Sequence[CandidateInfo],
    all_candidates: Sequence[CandidateInfo],
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accepted": [candidate_to_dict(c) for c in accepted],
        "all_candidates": [candidate_to_dict(c) for c in all_candidates],
    }
    STAGING_ACCEPTED.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def candidate_to_dict(candidate: CandidateInfo) -> dict:
    data = asdict(candidate)
    if candidate.evaluation:
        data["evaluation"] = asdict(candidate.evaluation)
    return data


def store_research_notes(plan: PlannerPlan, planner_raw: str, accepted: Sequence[CandidateInfo], rejected: Sequence[CandidateInfo]) -> None:
    accepted_sections = "\n".join(
        candidate.as_markdown() for candidate in accepted
    ) or "Noch keine geeigneten Kandidaten gefunden."

    rejected_summary = "\n".join(
        f"- {c.name} ({c.url}) – {c.notes or (c.evaluation.reason if c.evaluation else 'Keine Begründung')}"
        for c in rejected
    ) or "Keine abgelehnten Kandidaten dokumentiert."

    content = (
        "# Recherche-Notizen\n\n"
        "## Planergebnis (Rohformat)\n"
        f"{planner_raw.strip() or 'Keine Planner-Antwort'}\n\n"
        "## Strukturierte Schritte\n"
        + "\n".join(f"- {step}" for step in plan.steps)
        + "\n\n"
        "## Akzeptierte Kandidaten\n"
        f"{accepted_sections}\n\n"
        "## Abgelehnte Kandidaten (Kurzbegründung)\n"
        f"{rejected_summary}\n"
    )
    STAGING_NOTES.write_text(content, encoding="utf-8")


def slugify(value: str) -> str:
    return (
        value.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("_", "-")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .strip("-")
    )


def store_letter(candidate: CandidateInfo, letter_content: str) -> Path:
    file_path = OUTPUT_DIR / f"{slugify(candidate.name)}.md"
    file_path.write_text(letter_content, encoding="utf-8")
    return file_path


async def run_writer_agent(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
) -> str:
    agent = Agent(
        name="LetterWriter",
        instructions=(
            "Du verfasst personalisierte, freundliche Einladungen fuer die Maker Faire. "
            "Verwende hoechstens 220 Woerter. Hebe die Vorteile fuer den Empfaenger hervor."
        ),
        model=model,
    )
    prompt = (
        f"Identitaet:\n{identity_summary}\n\n"
        f"Kandidat:\nName: {candidate.name}\nURL: {candidate.url}\n"
        f"Summary: {candidate.summary}\nNotizen: {candidate.notes}\n"
        f"NorthData: {candidate.northdata_info or 'Keine Zusatzinformationen'}\n\n"
        "Schreibe nun ein Einladungsschreiben (Markdown) mit persoenlicher Ansprache."
    )
    result = await Runner.run(agent, prompt)
    return result.final_output or ""


async def orchestrate_search(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    plan: PlannerPlan,
) -> tuple[List[CandidateInfo], List[CandidateInfo]]:
    accepted: List[CandidateInfo] = []
    all_candidates: List[CandidateInfo] = []
    seen_urls: set[str] = set()
    used_queries: List[str] = []
    feedback_pool: List[str] = []

    queries = list(iter_queries(plan.search_queries))
    iteration = 0

    while len(accepted) < plan.target_candidates and iteration < MAX_ITERATIONS and queries:
        iteration += 1
        print(f"--- Suchiteration {iteration} mit {len(queries)} Queries ---")
        new_candidates_in_iteration: List[CandidateInfo] = []

        for query in queries:
            used_queries.append(query)
            results = await asyncio.to_thread(
                search_duckduckgo,
                query,
                max_results=MAX_RESULTS_PER_QUERY,
                region="de-de",
                safesearch="moderate",
            )
            store_search_results(query, results)
            candidates = build_candidates_from_search(query, results)
            for candidate in candidates:
                if candidate.url in seen_urls:
                    continue
                seen_urls.add(candidate.url)
                evaluation = await evaluate_candidate(model, identity_summary, candidate)
                candidate.evaluation = evaluation
                candidate.notes = evaluation.reason
                all_candidates.append(candidate)
                new_candidates_in_iteration.append(candidate)
                if evaluation.accepted and len(accepted) < plan.target_candidates:
                    accepted.append(candidate)
                else:
                    feedback_pool.append(evaluation.search_adjustment)

        if len(accepted) >= plan.target_candidates:
            break

        remaining = plan.target_candidates - len(accepted)
        new_queries = await refine_queries(
            model=model,
            identity_summary=identity_summary,
            base_task="Finde passende Aussteller fuer die Maker Faire Lübeck.",
            feedback_hints=feedback_pool[-10:],  # letzte Hinweise reichen
            used_queries=used_queries,
            missing=remaining,
        )
        queries = [q for q in iter_queries(new_queries) if q not in used_queries]

        if not new_candidates_in_iteration and not queries:
            print("Keine neuen Kandidaten gefunden, Abbruch.")
            break

    return accepted, all_candidates


async def async_main() -> None:
    ensure_dirs()
    load_env_file(ENV_PATH)
    ensure_required_env(["OPENAI_BASE_URL", "OPENAI_MODEL"])

    identity = load_identity()
    identity_summary = get_identity_summary(identity)

    model = build_model()

    task = "Finde mindestens 50 passende Aussteller, Projekte oder Organisationen für die Maker Faire Lübeck 2026."
    plan, planner_raw = await run_planner(model, task, identity_summary)
    print("Plan-Schritte:")
    for step in plan.steps:
        print(f"- {step}")
    print(f"Start-Queries ({len(plan.search_queries)}): {plan.search_queries}")

    accepted, all_candidates = await orchestrate_search(model, identity_summary, plan)
    if not accepted:
        raise RuntimeError("Keine passenden Kandidaten gefunden – bitte Suchkriterien prüfen.")

    enrich_with_northdata(accepted)
    store_candidates_snapshot(accepted, all_candidates)

    rejected = [c for c in all_candidates if not (c.evaluation and c.evaluation.accepted)]
    store_research_notes(plan, planner_raw, accepted, rejected)
    print(f"Recherche-Notizen gespeichert: {STAGING_NOTES}")

    top_candidate = accepted[0]
    letter = await run_writer_agent(model, identity_summary, top_candidate)
    letter_path = store_letter(top_candidate, letter)
    print(f"Anschreiben gespeichert unter: {letter_path}")


if __name__ == "__main__":
    asyncio.run(async_main())
