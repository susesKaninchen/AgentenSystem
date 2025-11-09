"""
Planner -> Google-/DuckDuckGo-Suche -> Evaluator -> Writer Pipeline.

Der Workflow läuft vollständig automatisiert:
- Planner erstellt einen strukturierten Plan inklusive Suchqueries.
- Standardmäßig liefert Google Custom Search die Treffer; fehlt die Google-Konfiguration,
  fällt der Workflow automatisch auf DuckDuckGo zurück.
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
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Protocol, Tuple

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tracing import set_tracing_disabled
from openai import AsyncOpenAI

from tools.identity_loader import get_identity_summary, load_identity
from tools.google_search import (
    GoogleSearchResult,
    iter_queries,
    search_google,
    store_search_results as store_google_results,
    load_cached_results as load_cached_google_results,
)
from tools.duckduckgo import (
    DuckDuckGoResult,
    search_duckduckgo,
    store_search_results as store_duckduckgo_results,
    load_cached_results as load_cached_duckduckgo_results,
)
from tools.northdata import (
    NorthDataError,
    fetch_suggestions,
    format_top_suggestion,
    store_suggestions,
)
from tools.web_search_agent import run_web_search_agent


STAGING_NOTES = Path("data/staging/research_notes.md")
STAGING_ACCEPTED = Path("data/staging/candidates_selected.json")
OUTPUT_DIR = Path("outputs/letters")
LOG_DIR = Path("logs")
PIPELINE_LOG = LOG_DIR / "pipeline.log"
ENV_PATH = Path(".env")

# Disable tracing to avoid noisy warnings when no tracing key is configured.
set_tracing_disabled(True)


def console(message: str) -> None:
    print(f"[PIPELINE] {message}")

DEFAULT_TARGET_CANDIDATES = 50
MAX_ITERATIONS = 5
MAX_RESULTS_PER_QUERY = 6
EVALUATION_ACCEPT_THRESHOLD = 0.62
NORTHDATA_COUNTRIES = "DE"
MAX_QA_RETRIES = 3
MAX_LETTER_WORDS = 190
DUCKDUCKGO_QUERY_DELAY = 2.5
PROMISE_PATTERNS = [
    r"\bversprech",
    r"\bgarantier",
    r"\bverpflicht",
    r"\bsichern\s+zu",
    r"\bdefinitiv\b",
]
TARGET_PROFILE_DESCRIPTION = (
    "Fokus auf nicht-kommerzielle Maker:innen, Hackervereine, offene Werkstätten, Kultur-"
    "und Technik-Kollektive (z. B. Chaotikum, Fuchsbau) aus Norddeutschland. "
    "Bevorzugt Projekte mit Mitmach-Charakter, Bildungsschwerpunkt oder DIY-Kultur."
)
WEB_SEARCH_LOCATION = "Norddeutschland (Lübeck, Hamburg, Kiel, Bremen)"
FALLBACK_SEED_CANDIDATES = [
    {
        "name": "Chaotikum e.V.",
        "url": "https://chaotikum.org",
        "summary": "Hackspace und Kulturverein in Lübeck, bietet offene Werkstatt, Workshops und DIY-Projekte.",
    },
    {
        "name": "Der Fuchsbau",
        "url": "https://fuchsbau-luebeck.de",
        "summary": "Sozio-kulturelles Zentrum und Bastelkollektiv aus Lübeck, Fokus auf Kunst, Technik und Bildung.",
    },
    {
        "name": "Freies Labor Kiel",
        "url": "https://freieslabor.org",
        "summary": "Gemeinnütziger Makerspace in Kiel mit Schwerpunkt auf Bildung, Reparatur und offenen Technologien.",
    },
    {
        "name": "Hackerspace Bremen e.V.",
        "url": "https://www.hackerspace-bremen.de",
        "summary": "Offener Raum für Technik- und Bastelprojekte, workshops und non-kommerzielle Maker-Events.",
    },
    {
        "name": "Chaostreff Flensburg",
        "url": "https://chaostreff-flensburg.de",
        "summary": "Community für IT, Elektronik und kreative Projekte im Raum Flensburg.",
    },
]
MAX_FALLBACK_TARGET = len(FALLBACK_SEED_CANDIDATES)


def web_search_tool_enabled() -> bool:
    value = os.environ.get("ENABLE_WEB_SEARCH_TOOL", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


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


@dataclass
class QAResult:
    approved: bool
    letter: str
    notes: str


class SearchResult(Protocol):
    query: str
    title: str
    url: str
    snippet: str
    source: str


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
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def build_models() -> tuple[OpenAIChatCompletionsModel, Optional[OpenAIResponsesModel]]:
    model_name = os.environ["OPENAI_MODEL"]
    base_url = os.environ["OPENAI_BASE_URL"]
    api_key = os.environ.get("OPENAI_API_KEY", "")
    os.environ.setdefault("OPENAI_DEFAULT_MODEL", model_name)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    print(f"Verwende Endpoint: {client.base_url}")
    chat_model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
    responses_model: Optional[OpenAIResponsesModel] = None
    if web_search_tool_enabled():
        try:
            responses_model = OpenAIResponsesModel(model=model_name, openai_client=client)
        except Exception as exc:  # pragma: no cover
            print(f"Warnung: Responses-WebSearch steht nicht zur Verfuegung ({exc}).")
    return chat_model, responses_model


def append_log(event: str, **fields: object) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event}
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    with PIPELINE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def word_count(text: str) -> int:
    return len([token for token in re.split(r"\s+", text.strip()) if token])


def contains_promises(text: str) -> bool:
    normalized = text.lower()
    return any(re.search(pattern, normalized) for pattern in PROMISE_PATTERNS)


def has_google_config() -> bool:
    return bool(
        os.environ.get("GOOGLE_API_KEY") and os.environ.get("GOOGLE_SEARCH_ENGINE_ID")
    )


def select_search_backend() -> Tuple[
    str,
    Callable[..., List[SearchResult]],
    Callable[[str, Sequence[SearchResult]], Path],
]:
    if has_google_config():
        return "google", search_google, store_google_results
    return "duckduckgo", search_duckduckgo, store_duckduckgo_results


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
    console("Starte Planner-Aufruf ...")
    agent = Agent(
        name="Planner",
        instructions=(
            "Du planst eine Recherche nach Ausstellenden für die Maker Faire Lübeck 2026. "
            "Zielgruppe: nicht-kommerzielle Maker:innen, Hackerspaces, offene Werkstätten, Kultur- "
            "und Bildungskollektive (z. B. Chaotikum, Fuchsbau) aus Norddeutschland."
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
        "Gesuchtes Profil:\n"
        f"{TARGET_PROFILE_DESCRIPTION}\n"
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

    console(
        f"Planner lieferte {len(steps or [])} Schritte, {len(queries)} Queries, Ziel {target} Kandidaten."
    )
    return PlannerPlan(steps=steps or DEFAULT_PLAN_STEPS, search_queries=queries, target_candidates=target), raw_output


DEFAULT_PLAN_STEPS = [
    "Thematische Schwerpunkte und Zielgruppen klären.",
    "Passende Maker-/Technikprojekte online recherchieren.",
    "Treffer bewerten, anreichern und zur Ansprache vorbereiten.",
]

DEFAULT_FALLBACK_QUERIES = [
    "Chaotikum Hackerspace Projekte Lübeck",
    "Fuchsbau Lübeck DIY Kollektiv",
    "Offene Werkstatt Schleswig-Holstein gemeinnützig",
    "Hackerverein Hamburg non-profit",
    "Freies Labor Kiel Maker Projekte",
    "DIY Elektronik Verein Bremen",
    "Jugend hackt Club Norddeutschland",
]


def build_candidates_from_search(
    query: str, results: Sequence[SearchResult]
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


def build_seed_candidates() -> List[CandidateInfo]:
    candidates: List[CandidateInfo] = []
    for seed in FALLBACK_SEED_CANDIDATES:
        candidates.append(
            CandidateInfo(
                name=seed["name"],
                url=seed["url"],
                summary=seed["summary"],
                source_query="fallback:seed",
                snippet=seed["summary"],
            )
        )
    return candidates


async def evaluate_candidate(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
) -> EvaluationResult:
    console(f"Bewerte Kandidat: {candidate.name} ({candidate.url}) ...")
    agent = Agent(
        name="Evaluator",
        instructions=(
            "Bewerte, ob ein Projekt/Organisation zu einer Maker Faire passt. "
            "Bevorzuge nicht-kommerzielle Kollektive, offene Werkstätten, Hackerspaces, "
            "Schul-/Uni-Teams und DIY-Kulturschaffende aus Norddeutschland. "
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
        "Gesuchtes Profil:\n"
        f"{TARGET_PROFILE_DESCRIPTION}\n\n"
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
    console(
        f"Evaluator Ergebnis: {candidate.name} -> Score {score:.2f}, accepted={accepted}, Grund: {reason}"
    )
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
            "Erzeuge neue Suchqueries (Google Custom Search oder DuckDuckGo) für non-kommerzielle Maker:innen, "
            "Hackspaces, offene Werkstätten, DIY-Kollektive und ähnliche Gruppen. "
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
        f"Gesuchtes Profil:\n{TARGET_PROFILE_DESCRIPTION}\n\n"
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


def store_letter(
    candidate: CandidateInfo, letter_content: str, qa_notes: str = ""
) -> Path:
    file_path = OUTPUT_DIR / f"{slugify(candidate.name)}.md"
    metadata_lines = [
        "---",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"candidate: {candidate.name}",
        f"source_url: {candidate.url}",
        f"words: {word_count(letter_content)}",
    ]
    if qa_notes:
        metadata_lines.append(f"qa_notes: {qa_notes}")
    metadata_lines.append("---\n")
    content = "\n".join(metadata_lines) + letter_content.strip() + "\n"
    file_path.write_text(content, encoding="utf-8")
    return file_path


async def run_writer_agent(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
    feedback: str = "",
) -> str:
    console(f"Writer erstellt Entwurf fuer {candidate.name} ...")
    agent = Agent(
        name="LetterWriter",
        instructions=(
            "Du verfasst personalisierte, freundliche Einladungen fuer die Maker Faire. "
            f"Verwende hoechstens {MAX_LETTER_WORDS} Woerter, damit der Text auf eine DIN-A4-Seite passt. "
            "Mache keine verbindlichen Versprechen oder Garantien; bleibe bei einladender, realistischer Sprache. "
            "Hebe die Vorteile fuer den Empfaenger hervor, betone, dass wir kleinteilige, kreative, "
            "nicht-kommerzielle Projekte wie Chaotikum/Fuchsbau suchen und dass gemeinnuet zig/nicht-kommerzielle Teams "
            "einen kostenfreien Stand erhalten. Wenn Feedback bereitgestellt wird, arbeite es praezise ein."
        ),
        model=model,
    )
    prompt = (
        f"Identitaet:\n{identity_summary}\n\n"
        f"Gesuchtes Profil:\n{TARGET_PROFILE_DESCRIPTION}\n\n"
        f"Kandidat:\nName: {candidate.name}\nURL: {candidate.url}\n"
        f"Summary: {candidate.summary}\nNotizen: {candidate.notes}\n"
        f"NorthData: {candidate.northdata_info or 'Keine Zusatzinformationen'}\n\n"
        "Schreibe nun ein Einladungsschreiben (Markdown) mit persoenlicher Ansprache."
        "\nBetone explizit, dass der Stand fuer gemeinnuetzige und nicht-kommerzielle Teams kostenfrei ist."
    )
    if feedback:
        prompt += (
            "\n\nBeruecksichtige das folgende Feedback "
            "und passe den Text entsprechend an:\n"
            f"{feedback.strip()}\n"
        )
    result = await Runner.run(agent, prompt)
    return result.final_output or ""


async def run_qa_agent(
    model: OpenAIChatCompletionsModel,
    letter_content: str,
    candidate: CandidateInfo,
) -> QAResult:
    console(f"QA prueft Anschreiben fuer {candidate.name} ...")
    instructions = (
        "Du bist ein QA-Agent fuer Anschreiben. "
        "Pruefe, ob der Text hoeﬂich, faktenbasiert und frei von Versprechen/Garantien ist. "
        f"Der Text darf max. {MAX_LETTER_WORDS} Woerter enthalten (DIN-A4). "
        "Gib nur JSON zurueck: "
        '{"approved": true/false, "notes": "<Begruendung>", "suggested_rewrite": "<Text oder leer>"}'
    )
    agent = Agent(name="QAAgent", instructions=instructions, model=model)
    prompt = (
        "Pruefe dieses Anschreiben:\n\n"
        f"{letter_content}\n\n"
        "Kandidat:\n"
        f"Name: {candidate.name}\n"
        f"URL: {candidate.url}\n"
        f"Summary: {candidate.summary}\n"
    )
    result = await Runner.run(agent, prompt)
    data: dict[str, object] = {}
    if result.final_output:
        try:
            data = json.loads(extract_json_block(result.final_output))
        except json.JSONDecodeError:
            data = {}
    approved = bool(data.get("approved"))
    notes = str(data.get("notes") or "").strip()
    suggestion = str(data.get("suggested_rewrite") or "").strip()
    letter = suggestion if suggestion else letter_content
    console(f"QA Ergebnis fuer {candidate.name}: approved={approved}, notes='{notes}'")
    return QAResult(approved=approved, letter=letter, notes=notes)


async def generate_letter_with_guardrails(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
) -> QAResult:
    feedback = ""
    for attempt in range(1, MAX_QA_RETRIES + 1):
        letter = await run_writer_agent(
            model=model,
            identity_summary=identity_summary,
            candidate=candidate,
            feedback=feedback,
        )
        wc = word_count(letter)
        issues = []
        if wc > MAX_LETTER_WORDS:
            issues.append(f"{wc} Woerter > {MAX_LETTER_WORDS}")
        if contains_promises(letter):
            issues.append("Text enthaelt Versprechen oder Garantien.")
        if issues:
            feedback = (
                "Bitte kuerze den Text (max. "
                f"{MAX_LETTER_WORDS} Woerter) und entferne Versprechen. Gefundene Probleme: "
                + "; ".join(issues)
            )
            append_log("writer.retry", attempt=attempt, issues=issues)
            continue

        qa_result = await run_qa_agent(model, letter, candidate)
        append_log(
            "qa.review",
            attempt=attempt,
            approved=qa_result.approved,
            notes=qa_result.notes,
        )
        if qa_result.approved:
            return qa_result
        feedback = (
            "Der QA-Check hat folgende Hinweise geliefert: "
            f"{qa_result.notes or 'Bitte kuerzen und ohne Versprechen schreiben.'}"
        )

    raise RuntimeError("QA konnte das Anschreiben nicht freigeben.")


async def orchestrate_search(
    model: OpenAIChatCompletionsModel,
    search_model: Optional[OpenAIResponsesModel],
    identity_summary: str,
    plan: PlannerPlan,
) -> tuple[List[CandidateInfo], List[CandidateInfo], str, int]:
    accepted: List[CandidateInfo] = []
    all_candidates: List[CandidateInfo] = []
    seen_urls: set[str] = set()
    used_queries: List[str] = []
    feedback_pool: List[str] = []

    backend_name, search_fn, store_fn = select_search_backend()
    append_log(
        "search.backend",
        backend=backend_name,
        google_config=has_google_config(),
        web_tool_enabled=bool(search_model),
    )

    queries = list(iter_queries(plan.search_queries))
    iteration = 0
    empty_searches = 0
    seeds_used = False

    while len(accepted) < plan.target_candidates and iteration < MAX_ITERATIONS and queries:
        iteration += 1
        console(f"--- Suchiteration {iteration} mit {len(queries)} Queries ---")
        new_candidates_in_iteration: List[CandidateInfo] = []

        for query in queries:
            used_queries.append(query)
            results: List[SearchResult] = []
            backend_used = None

            if search_model is not None:
                try:
                    results = await run_web_search_agent(
                        search_model,
                        query=query,
                        max_results=MAX_RESULTS_PER_QUERY,
                        location_hint=WEB_SEARCH_LOCATION,
                    )
                    backend_used = "web_tool"
                    if results:
                        append_log("search.web_tool", query=query, count=len(results))
                        console(
                            f"WebSearchTool lieferte {len(results)} Ergebnisse fuer '{query}'."
                        )
                except Exception as exc:  # pragma: no cover
                    append_log("search.web_tool_error", query=query, error=str(exc))
                    console(f"WebSearchTool Fehler fuer '{query}': {exc}")

            if not results:
                results = await asyncio.to_thread(
                    search_fn,
                    query,
                    max_results=MAX_RESULTS_PER_QUERY,
                )
                backend_used = backend_name
                console(f"{backend_name} Suche fuer '{query}' gestartet ...")

            if not results:
                cached_results = (
                    load_cached_duckduckgo_results(query)
                    if backend_name == "duckduckgo"
                    else load_cached_google_results(query)
                )
                if cached_results:
                    results = cached_results
                    backend_used = f"{backend_name}_cache"
                    append_log("search.cache_hit", backend=backend_name, query=query)
                    console(f"Cache-Treffer fuer '{query}' ({len(results)} Ergebnisse).")

            if results:
                store_fn(query, results)
                append_log(
                    "search.results",
                    backend=backend_used,
                    query=query,
                    count=len(results),
                )
                console(
                    f"Suche '{query}' via {backend_used} -> {len(results)} Ergebnisse."
                )

            if not results:
                empty_searches += 1
                if backend_used == "duckduckgo" and DUCKDUCKGO_QUERY_DELAY > 0:
                    await asyncio.sleep(DUCKDUCKGO_QUERY_DELAY)
                continue

            if backend_used == "duckduckgo" and DUCKDUCKGO_QUERY_DELAY > 0:
                await asyncio.sleep(DUCKDUCKGO_QUERY_DELAY)

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
                    console(f"Kandidat akzeptiert: {candidate.name}")
                else:
                    feedback_pool.append(evaluation.search_adjustment)

        if len(accepted) >= plan.target_candidates:
            break

        remaining = plan.target_candidates - len(accepted)
        new_queries = await refine_queries(
            model=model,
            identity_summary=identity_summary,
            base_task="Finde nicht-kommerzielle Maker-Kollektive fuer die Maker Faire Lübeck.",
            feedback_hints=feedback_pool[-10:],  # letzte Hinweise reichen
            used_queries=used_queries,
            missing=remaining,
        )
        queries = [q for q in iter_queries(new_queries) if q not in used_queries]

        if not new_candidates_in_iteration and not queries:
            if not seeds_used:
                console("Keine Treffer vom Backend – nutze lokale Seed-Kandidaten.")
                seeds_used = True
                seed_candidates = build_seed_candidates()
                for candidate in seed_candidates:
                    if candidate.url in seen_urls:
                        continue
                    seen_urls.add(candidate.url)
                    evaluation = await evaluate_candidate(model, identity_summary, candidate)
                    candidate.evaluation = evaluation
                    candidate.notes = evaluation.reason
                    all_candidates.append(candidate)
                    if evaluation.accepted and len(accepted) < plan.target_candidates:
                        accepted.append(candidate)
                continue
            console("Keine neuen Kandidaten gefunden, Abbruch.")
            break

    return accepted, all_candidates, backend_name, empty_searches


async def async_main() -> None:
    ensure_dirs()
    load_env_file(ENV_PATH)
    ensure_required_env(["OPENAI_BASE_URL", "OPENAI_MODEL"])

    identity = load_identity()
    identity_summary = get_identity_summary(identity)

    chat_model, search_model = build_models()

    task = (
        "Finde mindestens 50 nicht-kommerzielle Maker:innen, Vereine oder Kollektive "
        "aus Norddeutschland (z. B. Chaotikum, Fuchsbau, offene Werkstätten), "
        "die zur Maker Faire Lübeck 2026 passen."
    )
    append_log("pipeline.start", task=task)
    plan, planner_raw = await run_planner(chat_model, task, identity_summary)
    console("Plan-Schritte:")
    for step in plan.steps:
        console(f"- {step}")
    console(f"Start-Queries ({len(plan.search_queries)}): {plan.search_queries}")
    append_log(
        "planner.done",
        steps=len(plan.steps),
        queries=len(plan.search_queries),
        target=plan.target_candidates,
    )

    if search_model is None and not has_google_config() and plan.target_candidates > MAX_FALLBACK_TARGET:
        plan.target_candidates = MAX_FALLBACK_TARGET
        append_log(
            "planner.adjust_target",
            reason="no_google_config",
            target=plan.target_candidates,
        )

    accepted, all_candidates, backend_name, empty_searches = await orchestrate_search(
        chat_model, search_model, identity_summary, plan
    )
    if not accepted:
        hint = ""
        if backend_name == "duckduckgo" and not has_google_config() and search_model is None:
            hint = (
                " Hinweis: DuckDuckGo wurde nur als Fallback genutzt und hat wiederholt "
                f"Rate-Limits bzw. leere Treffer geliefert (leer: {empty_searches}). "
                "Setze `GOOGLE_API_KEY` und `GOOGLE_SEARCH_ENGINE_ID`, "
                "oder reduziere die Anzahl an Queries."
            )
        elif search_model is None:
            hint = " Hinweis: WebSearchTool ist deaktiviert – bitte OPENAI Responses/WebSearch freischalten."
        raise RuntimeError(
            "Keine passenden Kandidaten gefunden – bitte Suchkriterien oder Suchbackend prüfen."
            + hint
        )
    append_log(
        "search.done",
        accepted=len(accepted),
        considered=len(all_candidates),
        backend=backend_name,
        empty_searches=empty_searches,
    )

    enrich_with_northdata(accepted)
    store_candidates_snapshot(accepted, all_candidates)

    rejected = [c for c in all_candidates if not (c.evaluation and c.evaluation.accepted)]
    store_research_notes(plan, planner_raw, accepted, rejected)
    console(f"Recherche-Notizen gespeichert: {STAGING_NOTES}")

    top_candidate = accepted[0]
    qa_result = await generate_letter_with_guardrails(chat_model, identity_summary, top_candidate)
    letter_path = store_letter(top_candidate, qa_result.letter, qa_notes=qa_result.notes)
    append_log(
        "letter.saved",
        candidate=top_candidate.name,
        path=str(letter_path),
        qa_notes=qa_result.notes,
    )
    console(f"Anschreiben gespeichert unter: {letter_path}")
    append_log("pipeline.done", accepted=len(accepted), letter=str(letter_path))


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except Exception as exc:  # pragma: no cover
        append_log("pipeline.failed", error=str(exc))
        raise
