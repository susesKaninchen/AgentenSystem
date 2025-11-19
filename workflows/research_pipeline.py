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

import argparse
import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Protocol, Tuple, Mapping
from urllib.parse import urlparse

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tracing import set_tracing_disabled
from openai import AsyncOpenAI

from tools.identity_loader import get_identity_summary, load_identity
from tools.blacklist import BlacklistManager
from tools.org_registry import OrganizationRegistry
from tools.directory_parser import DirectoryEntry, DirectoryParserError, expand_directory
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
from tools.site_scraper import SiteSnapshot, fetch_site_snapshot, fetch_related_snapshots
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
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_MAX_RESULTS_PER_QUERY = 10
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
NEGATIVE_URL_SUFFIXES = (".pdf", ".csv", ".doc", ".ppt", ".xls", ".zip")
NEGATIVE_DOMAINS = {
    "bundestag.de",
    "scribd.com",
    "editeur.org",
    "b-u-b.de",
    "telekom-stiftung.de",
    "bne-portal.de",
    "schleswig-holstein.de",
}
NEGATIVE_TEXT_TERMS = [
    "outlet",
    "shopping center",
    "shopping-centre",
    "bank",
    "sparkasse",
    "versicher",
    "immobilien",
    "wohnungsbau",
    "stiftung",
    "gmbh",
    "aktiengesellschaft",
    "investment",
    "einkaufszentrum",
]
SEARCH_FOCUS_KEYWORDS = [
    "verein",
    "e.v.",
    "makerspace",
    "hackerspace",
    "labor",
    "offene werkstatt",
    "repair café",
    "repair cafe",
    "gemeinnützig",
    "gemeinnuetzig",
    "open lab",
    "fablab",
    "maker",
]
NORD_REGION_KEYWORDS = [
    "norddeutsch",
    "schleswig",
    "holstein",
    "hamburg",
    "bremen",
    "niedersachsen",
    "mecklenburg",
    "vorpommern",
    "lübeck",
    "luebeck",
    "kiel",
    "flensburg",
    "rostock",
    "greifswald",
    "oldenburg",
    "bremerhaven",
    "hannover",
    "braunschweig",
]
REGION_POSITIVE_KEYWORDS = {
    "hamburg": ["hamburg", "altona", "harburg", "wandsbek", "ottensen"],
    "luebeck": ["lübeck", "luebeck", "stockelsdorf", "bad schwartau", "arfrade", "travemünde"],
    "kiel": ["kiel", "schleswig", "neumünster", "plön"],
    "hannover": ["hannover", "braunschweig", "hildesheim", "celle"],
}
OFF_REGION_KEYWORDS = [
    "bochum",
    "oberhausen",
    "dortmund",
    "essen",
    "münchen",
    "munchen",
    "bayern",
    "stuttgart",
    "rheinland",
    "saarland",
    "thüringen",
    "thueringen",
    "sachsen",
    "leipzig",
    "düsseldorf",
    "dusseldorf",
    "köln",
    "koeln",
    "berlin",
    "frankfurt",
    "freiburg",
    "augsburg",
    "nürnberg",
    "nuernberg",
]
TARGET_PROFILE_DESCRIPTION = (
    "Fokus auf nicht-kommerzielle Maker:innen, Hackervereine, offene Werkstätten, Kultur-"
    "und Technik-Kollektive (z. B. Chaotikum, Fuchsbau) aus Norddeutschland. "
    "Bevorzugt Projekte mit Mitmach-Charakter, Bildungsschwerpunkt oder DIY-Kultur."
)
WEB_SEARCH_LOCATION = "Norddeutschland (Lübeck, Hamburg, Kiel, Bremen)"
DIRECTORY_HINT_KEYWORDS = [
    "liste",
    "listen",
    "übersicht",
    "uebersicht",
    "werkstätten",
    "werkstaetten",
    "labs",
    "netzwerk",
    "netzwerke",
    "verzeichnis",
    "guide",
    "sammlung",
    "aussteller",
    "meet the makers",
    "maker*innen",
    "makerspaces",
    "top ",
    "top-",
    "map",
    "directory",
    "diy-werkstätten",
    "diy-werkstaetten",
    "labs-werkstaetten",
    "open labs",
    "labore und werkstätten",
]
DIRECTORY_EXPANSION_MIN_SCORE = 0.5
DIRECTORY_MAX_ENTRIES = 25
DIRECTORY_MAX_DEPTH = 2
REGIONAL_QUERY_PERMUTATIONS = [
    "Makerspace Hamburg gemeinnützig",
    "Offene Werkstatt Kiel Verein",
    "Hackerspace Bremen e.V.",
    "DIY Labor Lübeck",
    "Kreativlabor Niedersachsen Schule",
    "Open Lab Flensburg Universität",
    "Freies Labor Schleswig-Holstein",
    "Maker Kollektiv Rostock",
    "Reparatur Café Norddeutschland",
    "Community Werkstatt Mecklenburg",
]
DEFAULT_MAX_LETTERS = 3
DEFAULT_PHASE = "acquire"
DEFAULT_REGION = "nord"  # placeholder for macro areas


def fallback_region_queries(used_queries: Iterable[str], missing: int, region: str = DEFAULT_REGION) -> List[str]:
    pool = REGIONAL_QUERY_SETS.get(region.lower(), REGIONAL_QUERY_PERMUTATIONS)
    remaining = max(1, min(len(pool), max(missing, 5)))
    used = set(q.lower() for q in used_queries)
    candidates: List[str] = []
    for query in pool:
        if query.lower() in used:
            continue
        candidates.append(query)
        if len(candidates) >= remaining:
            break
    return candidates


def web_search_tool_enabled() -> bool:
    value = os.environ.get("ENABLE_WEB_SEARCH_TOOL", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def get_int_setting(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def should_skip_url(url: str) -> bool:
    lowered = url.lower()
    if any(lowered.endswith(suffix) for suffix in NEGATIVE_URL_SUFFIXES):
        return True
    domain = urlparse(url).netloc.lower()
    if any(domain.endswith(neg) for neg in NEGATIVE_DOMAINS):
        return True
    return False


def candidate_matches_region(candidate: CandidateInfo, region: str) -> bool:
    text = (_candidate_text(candidate) + " " + candidate.source_query).lower()
    positives = NORD_REGION_KEYWORDS + REGION_POSITIVE_KEYWORDS.get(region.lower(), [])
    if any(keyword in text for keyword in OFF_REGION_KEYWORDS):
        return False
    if any(keyword in text for keyword in positives):
        return True
    return True  # Unbekannt => nicht blockieren


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recherche-Pipeline für Maker Faire Lübeck")
    parser.add_argument(
        "--phase",
        choices=["explore", "refine", "acquire"],
        default=os.environ.get("PIPELINE_PHASE", DEFAULT_PHASE),
        help="Steuert Score-Schwellen, Query-Breite und Letter-Batching.",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("PIPELINE_REGION", DEFAULT_REGION),
        help="Makroregion (z. B. nord, hamburg, luebeck, kiel, hannover).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=os.environ.get("PIPELINE_MAX_ITERATIONS"),
        help="Übersteuert die Anzahl Suchiteration (sonst Phase/env).")
    parser.add_argument(
        "--results-per-query",
        type=int,
        default=os.environ.get("PIPELINE_RESULTS_PER_QUERY"),
        help="Übersteuert Trefferzahl pro Query (sonst Phase/env).",
    )
    parser.add_argument(
        "--letters-per-run",
        type=int,
        default=os.environ.get("MAX_LETTERS_PER_RUN"),
        help="Übersteuert Zahl der Anschreiben pro Lauf.",
    )
    parser.add_argument(
        "--target-candidates",
        type=int,
        default=os.environ.get("PIPELINE_TARGET_CANDIDATES"),
        help="Zielgröße akzeptierter Kandidaten (Standard: Planner / Briefe).",
    )
    parser.add_argument(
        "--resume-candidates",
        nargs="?",
        const=str(STAGING_ACCEPTED),
        help="Überspringt die Websuche und lädt akzeptierte Kandidaten aus der angegebenen Snapshot-Datei (Standard: data/staging/candidates_selected.json).",
    )
    return parser.parse_args()


def normalize_resume_path(value: Optional[str]) -> Optional[Path]:
    if value is None:
        return None
    candidate = (value or "").strip().strip('"').strip("'")
    if candidate in {"", "="}:
        candidate = str(STAGING_ACCEPTED)
    path = Path(candidate)
    return path


def phase_presets(phase: str) -> dict:
    phase = phase.lower()
    if phase == "explore":
        return {
            "accept_threshold": 0.55,
            "max_iterations": 12,
            "results_per_query": 12,
            "letters_per_run": 1,
        }
    if phase == "refine":
        return {
            "accept_threshold": 0.65,
            "max_iterations": 10,
            "results_per_query": 10,
            "letters_per_run": 2,
        }
    # acquire (default)
    return {
        "accept_threshold": 0.7,
        "max_iterations": 8,
        "results_per_query": 8,
        "letters_per_run": 3,
    }


REGIONAL_QUERY_SETS = {
    "nord": REGIONAL_QUERY_PERMUTATIONS,
    "hamburg": [
        "Makerspace Hamburg gemeinnützig",
        "Open Lab Hamburg Hochschule",
        "Hamburg DIY Kollektiv nicht kommerziell",
        "Fab City Hamburg Werkstatt",
    ],
    "luebeck": [
        "Lübeck offene Werkstatt",
        "Maker Lübeck Verein",
        "Lübeck Kulturtechnik Kollektiv",
        "Offene Werkstatt Bad Schwartau",
        "Gemeinschaftsprojekt Travemünde",
        "Maker Ostholstein",
    ],
    "kiel": [
        "Kiel Makerspace",
        "Kiel Open Lab Schule",
        "Kiel Hackerspace Verein",
        "Offene Werkstatt Eckernförde",
        "Rendsburg DIY Verein",
    ],
    "luebeck-local": [
        "Lübeck Makerspace",
        "Arfrade Hofprojekt",
        "Bad Oldesloe offene Werkstatt",
        "Reparatur Café Lübeck",
        "Schwartau DIY", 
        "Travemünde Maker",
        "Neustadt in Holstein Werkstatt",
    ],
    "hannover": [
        "Hannover Kreativlabor",
        "Hackerspace Hannover",
        "FabLab Hannover",
    ],
}
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
    category: str = ""
    region_hint: str = ""
    nonprofit: bool = False
    maker_focus: bool = False
    outreach_priority: float = 0.0


@dataclass
class CoordinatorDecision:
    approved: bool
    reason: str
    dialogue: List[str] = field(default_factory=list)
    keyword_hints: List[str] = field(default_factory=list)
    blacklist: bool = False
    blacklist_reason: str = ""


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
    coordination: Optional[CoordinatorDecision] = field(default=None, repr=False)
    context: Optional["CandidateContext"] = field(default=None, repr=False)
    org_slug: str = ""
    duplicate_reason: str = ""
    letter_status: str = "pending"
    letter_path: str = ""

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


def _candidate_text(candidate: CandidateInfo) -> str:
    return " ".join(
        filter(
            None,
            [
                candidate.name,
                candidate.summary,
                candidate.snippet,
                candidate.url,
            ],
        )
    ).lower()


def looks_like_directory_candidate(candidate: CandidateInfo) -> bool:
    text = _candidate_text(candidate)
    return any(keyword in text for keyword in DIRECTORY_HINT_KEYWORDS)


def domain_key(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def default_org_slug(candidate: CandidateInfo) -> str:
    domain = domain_key(candidate.url)
    parsed = urlparse(candidate.url)
    parts = [part for part in parsed.path.split("/") if part]
    generic_prefixes = {"event", "events", "tag", "tags", "blog", "category", "projekt", "project"}
    key = ""
    if parts:
        primary = parts[0]
        if primary not in generic_prefixes and not primary.isdigit():
            key = primary
    if not key:
        name_base = "".join(ch for ch in (candidate.name or "") if not ch.isdigit()).strip() or domain
        key = "-".join(name_base.split()[:4])
    base = f"{domain}-{key}"
    return slugify(base)


@dataclass
class QAResult:
    approved: bool
    letter: str
    notes: str


@dataclass
class CandidateContext:
    primary: Optional[SiteSnapshot]
    related: List[SiteSnapshot] = field(default_factory=list)


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
        if should_skip_url(item.url):
            append_log(
                "search.filtered",
                url=item.url,
                reason="negative_source",
            )
            continue
        title = item.title or item.url
        combined_text = f"{title} {item.snippet}".lower()
        if any(term in combined_text for term in NEGATIVE_TEXT_TERMS):
            append_log(
                "search.filtered",
                url=item.url,
                reason="negative_text",
            )
            continue
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


async def filter_search_results(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    query: str,
    results: Sequence[SearchResult],
) -> Sequence[SearchResult]:
    if len(results) <= 3:
        return results
    agent = Agent(
        name="ResultFilter",
        instructions=(
            "Du bist ein Recherche-Koordinator. Entferne Duplikate (gleiche Domains), "
            "rein kommerzielle Shops und reine Verzeichnis-/Newsseiten. "
            "Behalte höchstens 6 Ergebnisse pro Query und markiere ggf. interessante Unterseiten "
            "(z. B. /kontakt, /about). "
            "Antwort nur als JSON: "
            '{"keep_indexes": [0,2,...], "notes": "..."}'
        ),
        model=model,
    )
    serialized = [
        {
            "index": idx,
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
        }
        for idx, item in enumerate(results)
    ]
    prompt = (
        f"Identität:\n{identity_summary}\n\n"
        f"Query: {query}\n"
        "Suchergebnisse:\n"
        + json.dumps(serialized, ensure_ascii=False, indent=2)
        + "\n\nWähle die relevantesten Einträge."
    )
    result = await Runner.run(agent, prompt)
    try:
        data = json.loads(extract_json_block(result.final_output or ""))
        keep = [
            int(idx)
            for idx in data.get("keep_indexes", [])
            if isinstance(idx, int)
        ]
    except (json.JSONDecodeError, ValueError, TypeError):
        keep = []
    keep = [idx for idx in keep if 0 <= idx < len(results)]
    if not keep:
        return results[:6]
    return [results[idx] for idx in keep]


def candidate_from_directory_entry(
    parent: CandidateInfo,
    entry: DirectoryEntry,
) -> CandidateInfo:
    summary = entry.description or f"Automatisch aus {parent.name} übernommen."
    return CandidateInfo(
        name=entry.name.strip(),
        url=entry.url.strip(),
        summary=summary,
        source_query=f"directory:{parent.url}",
        snippet=summary,
    )


def candidate_score(candidate: CandidateInfo) -> float:
    if not candidate.evaluation:
        return 0.0
    base = float(candidate.evaluation.score)
    if candidate.evaluation.outreach_priority:
        base += 0.1 * float(candidate.evaluation.outreach_priority)
    if candidate.evaluation.maker_focus:
        base += 0.05
    if candidate.evaluation.nonprofit:
        base += 0.05
    return base


def select_letter_candidates(
    accepted: Sequence[CandidateInfo],
    limit: int,
) -> List[CandidateInfo]:
    if not accepted or limit <= 0:
        return []
    ranked = sorted(accepted, key=candidate_score, reverse=True)
    chosen: List[CandidateInfo] = []
    seen_urls: set[str] = set()
    for candidate in ranked:
        if candidate.url in seen_urls:
            continue
        if looks_like_directory_candidate(candidate):
            continue
        chosen.append(candidate)
        seen_urls.add(candidate.url)
        if len(chosen) >= limit:
            break
    if not chosen:
        for candidate in ranked:
            if candidate.url in seen_urls:
                continue
            chosen.append(candidate)
            seen_urls.add(candidate.url)
            if len(chosen) >= limit:
                break
    return chosen


def summarize_candidates_for_prompt(
    candidates: Sequence[CandidateInfo], limit: int = 5
) -> str:
    if not candidates:
        return "- Noch keine akzeptierten Kandidaten."
    lines: List[str] = []
    for candidate in candidates[-limit:]:
        location = ""
        if (
            candidate.context
            and candidate.context.primary
            and candidate.context.primary.detected_location
        ):
            location = candidate.context.primary.detected_location
        summary = candidate.summary or ""
        if not summary and candidate.context and candidate.context.primary:
            summary = candidate.context.primary.summary
        snippet = (summary or candidate.snippet or "").strip()
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        lines.append(
            f"- {candidate.name} ({location or 'Region offen'}) – {snippet or 'Keine Kurzbeschreibung'}"
        )
    return "\n".join(lines)


def candidate_from_direct_hint(hint: Mapping[str, object]) -> Optional[CandidateInfo]:
    url = str(hint.get("url") or "").strip()
    if not url:
        return None
    name = str(hint.get("name") or hint.get("title") or url).strip()
    summary = str(hint.get("summary") or hint.get("description") or "").strip()
    text = summary or "Direkter Crawl-Hinweis vom Query-Refiner."
    return CandidateInfo(
        name=name or url,
        url=url,
        summary=text,
        source_query="refiner:direct",
        snippet=summary,
    )


def summarize_run(plan: PlannerPlan, accepted: Sequence[CandidateInfo], all_candidates: Sequence[CandidateInfo], letter_stats: dict[str, int]) -> None:
    total = len(all_candidates)
    accepted_count = len(accepted)
    letters_done = letter_stats.get("completed", 0)
    scheduled = letter_stats.get("scheduled", 0)
    failed = letter_stats.get("failed", 0)
    queued = max(0, scheduled - letters_done - failed)
    pending = max(0, accepted_count - letters_done - failed)
    rejection_counter = Counter(
        (c.evaluation.category if c.evaluation else "unbekannt")
        for c in all_candidates
        if not (c.evaluation and c.evaluation.accepted)
    )
    console("=== Laufzusammenfassung ===")
    console(
        f"Kandidaten gesamt: {total} | akzeptiert: {accepted_count}/{plan.target_candidates} | Briefe abgeschlossen: {letters_done}"
    )
    console(
        f"Warteschlange Briefe: queued={queued} pending={pending} failed={failed}"
    )
    if rejection_counter:
        top_rejections = ", ".join(
            f"{cat}: {count}" for cat, count in rejection_counter.most_common(4)
        )
        console(f"Top Ablehnungsgründe: {top_rejections}")
    highlight_lines = []
    for candidate in accepted[:5]:
        status = candidate.letter_status or "pending"
        score = candidate.evaluation.score if candidate.evaluation else 0.0
        highlight_lines.append(
            f"- {candidate.name} ({status}, Score {score:.2f})"
        )
    if highlight_lines:
        console("Akzeptiert & Status:\n" + "\n".join(highlight_lines))
    append_log(
        "pipeline.summary",
        total_candidates=total,
        accepted=accepted_count,
        target=plan.target_candidates,
        letters_done=letters_done,
        queued=queued,
        pending=pending,
        failed=failed,
        top_rejections=rejection_counter.most_common(6),
    )


def load_candidates_from_snapshot(path: Path) -> List[CandidateInfo]:
    if not path.exists():
        raise FileNotFoundError(f"Snapshot {path} nicht gefunden.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("accepted") or []
    candidates: List[CandidateInfo] = []
    for entry in entries:
        evaluation_data = entry.get("evaluation") or {}
        evaluation = None
        if evaluation_data:
            evaluation = EvaluationResult(
                score=float(evaluation_data.get("score", 0.0)),
                accepted=bool(evaluation_data.get("accepted", False)),
                reason=str(evaluation_data.get("reason", "")),
                search_adjustment=str(evaluation_data.get("search_adjustment", "")),
            )
        candidate = CandidateInfo(
            name=entry.get("name", ""),
            url=entry.get("url", ""),
            summary=entry.get("summary", ""),
            source_query=entry.get("source_query", "resume"),
            snippet=entry.get("snippet", ""),
            notes=entry.get("notes", ""),
            northdata_info=entry.get("northdata_info", ""),
            org_slug=entry.get("org_slug", ""),
            letter_status=entry.get("letter_status", "pending"),
            letter_path=entry.get("letter_path", ""),
        )
        candidate.evaluation = evaluation
        candidates.append(candidate)
    return candidates


def extend_plan_with_region(plan: PlannerPlan, region: str) -> None:
    region_queries = REGIONAL_QUERY_SETS.get(region.lower())
    if not region_queries:
        return
    existing = set(q.lower() for q in plan.search_queries)
    additions = [q for q in region_queries if q.lower() not in existing]
    if additions:
        plan.search_queries.extend(additions)
        console(f"Region '{region}' Queries hinzugefügt: {additions}")


def enforce_focus_keywords(queries: Sequence[str]) -> List[str]:
    normalized: List[str] = []
    for query in queries:
        lowered = query.lower()
        if any(keyword in lowered for keyword in SEARCH_FOCUS_KEYWORDS):
            normalized.append(query)
        else:
            normalized.append(f"{query} Makerspace Verein")
    return normalized


async def evaluate_candidate(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
    context: Optional[CandidateContext],
) -> EvaluationResult:
    console(f"Bewerte Kandidat: {candidate.name} ({candidate.url}) ...")
    agent = Agent(
        name="Evaluator",
        instructions=(
            "Bewerte, ob ein Projekt/Organisation zu einer Maker Faire passt. "
            "Bevorzuge nicht-kommerzielle Kollektive, offene Werkstätten, Hackerspaces, "
            "Schul-/Uni-Teams und DIY-Kulturschaffende aus Norddeutschland. "
            "Warnung: Reine Verzeichnisse/Sammelseiten (z. B. Listen, Übersichten, Guides) dürfen nicht akzeptiert werden; fordere stattdessen konkrete Gruppen mit eigener Kontaktmöglichkeit. "
            "Antworte ausschließlich als JSON mit Feldern:\n"
            '{"score": 0.0-1.0, "accepted": true/false, "reason": "...", "search_adjustment": "...", '
            '"category": "verein|schule|directory|kommerziell|event|sonstiges", '
            '"region_hint": "...", "nonprofit": true/false, "maker_focus": true/false, "outreach_priority": 0.0-1.0}\n'
            "score beschreibt die Passung (>=0.62 akzeptiert). "
            '"search_adjustment" enthält einen Hinweis, wie künftige Queries präzisiert werden können '
            "(z. B. \"mehr Bildungspartner\" oder \"weniger reine Händler\"). "
            "Wenn kein Hinweis nötig ist, verwende einen leeren String."
        ),
        model=model,
    )
    context_block = format_context_for_prompt(context)
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
    if context_block:
        prompt += "\n\nKontext aus Website & Unterseiten:\n" + context_block
    result = await Runner.run(agent, prompt)
    output = extract_json_block(result.final_output or "")
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        data = {
            "score": 0.0,
            "accepted": False,
            "reason": "Bewertung fehlgeschlagen.",
            "search_adjustment": "präzisere Suchbegriffe verwenden",
            "category": "unbekannt",
            "region_hint": "",
            "nonprofit": False,
            "maker_focus": False,
            "outreach_priority": 0.0,
        }

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
        category=str(data.get("category", "")).strip(),
        region_hint=str(data.get("region_hint", "")).strip(),
        nonprofit=bool(data.get("nonprofit", False)),
        maker_focus=bool(data.get("maker_focus", False)),
        outreach_priority=float(data.get("outreach_priority", 0.0) or 0.0),
    )


async def resolve_candidate_slug(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
    registry: OrganizationRegistry,
) -> tuple[str, str]:
    known = [
        {
            "slug": record.slug,
            "name": record.name,
            "domain": record.domain,
            "url": record.primary_url,
            "status": record.status,
        }
        for record in registry.recent_records(limit=10)
    ]
    agent = Agent(
        name="Supervisor",
        instructions=(
            "Du bist ein Organisations-Supervisor. "
            "Vergleiche den neuen Kandidaten mit bekannten Organisationen und entscheide, "
            "ob er bereits existiert oder neu angelegt werden muss. "
            "Antworte ausschließlich als JSON: "
            '{"action": "use_existing|create_new", "slug": "kürzel", "reason": "..."}'
        ),
        model=model,
    )
    prompt = (
        f"Identität:\n{identity_summary}\n\n"
        "Kandidat:\n"
        f"- Name: {candidate.name}\n"
        f"- URL: {candidate.url}\n"
        f"- Query: {candidate.source_query}\n"
        f"- Zusammenfassung: {candidate.summary or candidate.snippet}\n\n"
        "Bereits bekannte Organisationen:\n"
        + json.dumps(known, ensure_ascii=False, indent=2)
        + "\n\n"
        "Bestimme, ob der Kandidat einer existierenden Organisation entspricht."
    )
    slug = default_org_slug(candidate)
    reason = ""
    try:
        result = await Runner.run(agent, prompt)
        data = json.loads(extract_json_block(result.final_output or ""))
        if isinstance(data.get("slug"), str) and data.get("slug").strip():
            slug = slugify(data["slug"].strip())
        action = str(data.get("action", "")).lower()
        reason = str(data.get("reason", "")).strip()
        if action == "use_existing":
            return slug, reason or "Supervisor: existierende Organisation erkannt."
    except (json.JSONDecodeError, TypeError, ValueError):
        reason = "Supervisor: Fallback-Slug genutzt."
    return slug, reason or "Supervisor: neue Organisation angelegt."


async def coordinate_candidate(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
    evaluation: EvaluationResult,
    region: str,
    context: Optional[CandidateContext],
) -> CoordinatorDecision:
    agent = Agent(
        name="Coordinator",
        instructions=(
            "Du koordinierst Recherche- und Bewertungs-Agenten. "
            "Simuliere eine kurze Unterhaltung zwischen Research (liefert Website-Eindruck) "
            "und Evaluator (liefert Score/Reason) und treffe danach eine finale Entscheidung. "
            "Extrahiere bei Ablehnung 1-3 Schlagwörter, die wir für neue Suchqueries nutzen können. "
            "Wenn die Seite bereits ein Verzeichnis/Spam oder offensichtlich ungeeignet ist, "
            "setze 'blacklist' auf true. "
            "Antwort ausschließlich als JSON mit Feldern:\n"
            '{"approved": bool, "reason": "...", "dialogue": ["Research: ...", "Evaluator: ...", "Coordinator: ..."], '
            '"keyword_hints": ["..."], "blacklist": bool, "blacklist_reason": "..."}\n'
            "Gib nur JSON zurück."
        ),
        model=model,
    )
    evaluation_context = json.dumps(asdict(evaluation), ensure_ascii=False)
    context_block = format_context_for_prompt(context)
    prompt = (
        f"Identität:\n{identity_summary}\n\n"
        f"Region-Fokus: {region}\n"
        "Research-Agent Beobachtung:\n"
        f"- Query: {candidate.source_query}\n"
        f"- Name: {candidate.name}\n"
        f"- URL: {candidate.url}\n"
        f"- Zusammenfassung/Snippet: {candidate.summary or candidate.snippet}\n\n"
        "Evaluator-Agent Einschätzung (JSON):\n"
        f"{evaluation_context}\n\n"
        "Koordinator-Aufgabe: Entscheide, ob wir diesen Kontakt wirklich übernehmen. "
        "Wenn Zweifel bestehen, lehne lieber ab. "
        "Extrahiere hilfreiche Stichwörter für zukünftige Suchen."
    )
    if context_block:
        prompt += "\n\nZusätzlicher Kontext (Kontakt-/About-Seiten):\n" + context_block
    result = await Runner.run(agent, prompt)
    try:
        data = json.loads(extract_json_block(result.final_output or ""))
    except json.JSONDecodeError:
        decision = CoordinatorDecision(
            approved=False,
            reason="Koordinator: Antwort konnte nicht interpretiert werden.",
        )
    else:
        dialogue = data.get("dialogue") or []
        if isinstance(dialogue, list):
            dialogue_lines = [str(line).strip() for line in dialogue if str(line).strip()]
        elif isinstance(dialogue, str):
            dialogue_lines = [dialogue.strip()]
        else:
            dialogue_lines = []
        decision = CoordinatorDecision(
            approved=bool(data.get("approved")),
            reason=str(data.get("reason") or "").strip() or "Koordinator: keine Begründung.",
            dialogue=dialogue_lines,
            keyword_hints=[
                hint.strip()
                for hint in (data.get("keyword_hints") or [])
                if isinstance(hint, str) and hint.strip()
            ],
            blacklist=bool(data.get("blacklist")),
            blacklist_reason=str(data.get("blacklist_reason") or "").strip(),
        )
    append_log(
        "coordinator.decision",
        candidate=candidate.name,
        approved=decision.approved,
        blacklist=decision.blacklist,
        hints=len(decision.keyword_hints),
    )
    return decision


async def refine_queries(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    base_task: str,
    feedback_hints: Sequence[str],
    used_queries: Iterable[str],
    missing: int,
    region: str,
    recent_accepts: Sequence[CandidateInfo],
) -> tuple[List[str], List[CandidateInfo]]:
    hints = [hint for hint in feedback_hints if hint]
    accepted_block = summarize_candidates_for_prompt(recent_accepts)

    agent = Agent(
        name="QueryRefiner",
        instructions=(
            "Erzeuge neue Suchqueries (Google Custom Search oder DuckDuckGo) für non-kommerzielle Maker:innen, "
            "Hackspaces, offene Werkstätten, DIY-Kollektive und ähnliche Gruppen. "
            "Nutze außerdem direkte Webseiten-Hinweise: Wenn du konkrete Vereine oder URLs kennst, füge sie unter "
            "\"direct_urls\" ein, dann crawlt das System diese Seiten direkt ohne Websuche. "
            "Antworte ausschließlich im JSON-Format: "
            '{"new_queries": ["..."], "direct_urls": [{"name": "...", "url": "...", "summary": "..."}]}. '
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
        + ("\n".join(f"- {hint}" for hint in hints) if hints else "- gezielt nach konkreten Vereinen/Offenen Werkstätten in Norddeutschland suchen")
        + "\n\n"
        "Bereits akzeptierte Kandidaten (verwende Themen/Ortsangaben als Inspiration für neue Suchbegriffe oder Direktlinks):\n"
        f"{accepted_block}\n\n"
        f"Gesuchtes Profil:\n{TARGET_PROFILE_DESCRIPTION}\n\n"
        "Erzeuge maximal 5 neue Queries und gib bei bekannten URLs (inkl. kurzer Zusammenfassung) Einträge in direct_urls zurück."
    )
    result = await Runner.run(agent, prompt)
    try:
        data = json.loads(extract_json_block(result.final_output or ""))
    except json.JSONDecodeError:
        queries = []
        direct_urls: List[CandidateInfo] = []
    else:
        queries = [q.strip() for q in data.get("new_queries", []) if isinstance(q, str) and q.strip()]
        direct_urls = []
        for entry in data.get("direct_urls", []) or []:
            if isinstance(entry, Mapping):
                candidate = candidate_from_direct_hint(entry)
                if candidate:
                    direct_urls.append(candidate)
    if not queries:
        queries = fallback_region_queries(used_queries, missing, region)
    return queries, direct_urls


def enrich_with_northdata(candidates: Sequence[CandidateInfo]) -> None:
    for candidate in candidates:
        query = candidate.name
        try:
            suggestions = fetch_suggestions(query, countries=NORTHDATA_COUNTRIES)
        except NorthDataError as exc:
            candidate.northdata_info = f"NorthData-Fehler: {exc}"
            continue
        except Exception as exc:  # pragma: no cover - Netzwerkzeitüberschreitungen u.ä.
            append_log("northdata.fetch_error", query=query, error=str(exc))
            candidate.northdata_info = f"NorthData-Timeout/Fehler: {exc}"
            continue

        try:
            store_suggestions(query, suggestions)
        except OSError as exc:
            append_log("northdata.store_error", query=query, error=str(exc))
            candidate.northdata_info = f"NorthData nicht gespeichert ({exc})"
            continue
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
    normalized = (
        value.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    normalized = re.sub(r"[^a-z0-9-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or "candidate"


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


def format_snapshot_for_prompt(snapshot: Optional[SiteSnapshot]) -> str:
    if snapshot is None:
        return ""
    parts = [
        f"- Titel: {snapshot.title.strip() or snapshot.url}",
        f"- Zusammenfassung: {snapshot.summary.strip()}",
    ]
    if snapshot.detected_location:
        parts.append(f"- Standort-Hinweis: {snapshot.detected_location}")
    if snapshot.highlights:
        highlight_lines = "\n".join(f"  * {text}" for text in snapshot.highlights)
        parts.append("- Highlights:\n" + highlight_lines)
    return "\n".join(parts)


def format_context_for_prompt(context: Optional[CandidateContext]) -> str:
    if context is None:
        return ""
    parts: List[str] = []
    if context.primary:
        parts.append("Hauptseite:\n" + format_snapshot_for_prompt(context.primary))
    for idx, snapshot in enumerate(context.related, start=1):
        parts.append(f"Subseite #{idx}:\n" + format_snapshot_for_prompt(snapshot))
    return "\n\n".join(part for part in parts if part)


def collect_candidate_context(candidate: CandidateInfo) -> CandidateContext:
    primary = fetch_site_snapshot(candidate.url)
    related = fetch_related_snapshots(candidate.url, max_pages=3)
    return CandidateContext(primary=primary, related=related)


async def run_writer_agent(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    candidate: CandidateInfo,
    feedback: str = "",
    snapshot: Optional[SiteSnapshot] = None,
    context: Optional[CandidateContext] = None,
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
    context_block = format_context_for_prompt(context)
    if not context_block:
        context_block = format_snapshot_for_prompt(snapshot)

    prompt = (
        f"Identitaet:\n{identity_summary}\n\n"
        f"Gesuchtes Profil:\n{TARGET_PROFILE_DESCRIPTION}\n\n"
        f"Kandidat:\nName: {candidate.name}\nURL: {candidate.url}\n"
        f"Summary: {candidate.summary}\nNotizen: {candidate.notes}\n"
        f"NorthData: {candidate.northdata_info or 'Keine Zusatzinformationen'}\n\n"
        "Schreibe nun ein Einladungsschreiben (Markdown) mit persoenlicher Ansprache."
        "\nBetone explizit, dass der Stand fuer gemeinnuetzige und nicht-kommerzielle Teams kostenfrei ist."
    )
    if context_block:
        prompt += (
            "\n\nZusatzinfos aus der Webseitenanalyse:\n"
            f"{context_block}\n"
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
    snapshot: Optional[SiteSnapshot] = None,
    context: Optional[CandidateContext] = None,
) -> QAResult:
    feedback = ""
    for attempt in range(1, MAX_QA_RETRIES + 1):
        letter = await run_writer_agent(
            model=model,
            identity_summary=identity_summary,
            candidate=candidate,
            feedback=feedback,
            snapshot=snapshot,
            context=context,
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


class LetterDispatcher:
    """Schedules letter generation tasks as soon as candidates are akzeptiert."""

    def __init__(
        self,
        *,
        limit: int,
        model: OpenAIChatCompletionsModel,
        identity_summary: str,
        blacklist: BlacklistManager,
        org_registry: OrganizationRegistry,
    ) -> None:
        self.limit = max(0, limit)
        self.model = model
        self.identity_summary = identity_summary
        self.blacklist = blacklist
        self.org_registry = org_registry
        self._lock = asyncio.Lock()
        self._scheduled = 0
        self._completed = 0
        self._failed = 0
        self._tasks: set[asyncio.Task] = set()

    async def enqueue(self, candidate: CandidateInfo) -> bool:
        if self.limit == 0:
            return False
        async with self._lock:
            if self._scheduled >= self.limit:
                return False
            self._scheduled += 1
            candidate.letter_status = "queued"
        console(f"LetterDispatcher: starte Anschreiben für {candidate.name}.")
        task = asyncio.create_task(self._process_candidate(candidate))
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.discard(t))
        return True

    async def _process_candidate(self, candidate: CandidateInfo) -> None:
        try:
            context = candidate.context
            snapshot = context.primary if context else None
            if snapshot is None:
                snapshot = await asyncio.to_thread(fetch_site_snapshot, candidate.url)
            qa_result = await generate_letter_with_guardrails(
                self.model,
                self.identity_summary,
                candidate,
                snapshot=snapshot,
                context=context,
            )
            letter_path = store_letter(
                candidate, qa_result.letter, qa_notes=qa_result.notes
            )
            append_log(
                "letter.saved",
                candidate=candidate.name,
                path=str(letter_path),
                qa_notes=qa_result.notes,
            )
            console(f"Anschreiben gespeichert unter: {letter_path}")
            candidate.letter_status = "sent"
            candidate.letter_path = str(letter_path)
            if candidate.org_slug:
                self.org_registry.mark_status(candidate.org_slug, "contacted")
            self.blacklist.add(
                candidate.url,
                reason="Bereits angeschrieben (Einladung gesendet).",
                tag="contacted",
                meta={"letter_path": str(letter_path)},
            )
            append_log(
                "blacklist.add",
                url=candidate.url,
                reason="Bereits angeschrieben",
                tag="contacted",
            )
            async with self._lock:
                self._completed += 1
        except Exception as exc:  # pragma: no cover - defensive logging
            append_log("letter.error", candidate=candidate.name, error=str(exc))
            console(f"[WARN] Anschreiben für {candidate.name} fehlgeschlagen: {exc}")
            candidate.letter_status = "failed"
            async with self._lock:
                self._failed += 1

    def stats(self) -> dict[str, int]:
        return {
            "scheduled": self._scheduled,
            "completed": self._completed,
            "failed": self._failed,
        }

    async def finalize(self) -> dict[str, int]:
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            return self.stats()


async def orchestrate_search(
    model: OpenAIChatCompletionsModel,
    search_model: Optional[OpenAIResponsesModel],
    identity_summary: str,
    plan: PlannerPlan,
    blacklist: BlacklistManager,
    org_registry: OrganizationRegistry,
    *,
    max_iterations: int,
    max_results_per_query: int,
    region: str,
    accept_threshold: float,
    letter_dispatcher: Optional[LetterDispatcher] = None,
) -> tuple[List[CandidateInfo], List[CandidateInfo], str, int]:
    accepted: List[CandidateInfo] = []
    all_candidates: List[CandidateInfo] = []
    seen_urls: set[str] = set()
    used_queries: List[str] = []
    feedback_pool: List[str] = []
    seen_org_slugs: set[str] = set()

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
    expanded_directories: set[str] = set()

    search_failed = False

    async def process_candidate(candidate: CandidateInfo, depth: int = 0) -> int:
        """Evaluates a candidate and expands directory-style pages if useful."""
        if candidate.url in seen_urls:
            return 0
        seen_urls.add(candidate.url)

        blacklist_entry = blacklist.is_blacklisted(candidate.url)
        if blacklist_entry:
            candidate.notes = f"Blacklist: {blacklist_entry.reason}"
            all_candidates.append(candidate)
            append_log(
                "candidate.blacklist_skip",
                url=candidate.url,
                domain=blacklist_entry.domain,
                reason=blacklist_entry.reason,
            )
            return 1

        if not candidate_matches_region(candidate, region):
            reason = "Außerhalb Norddeutschland (Geo-Heuristik)."
            evaluation = EvaluationResult(
                score=0.15,
                accepted=False,
                reason=reason,
                search_adjustment="Region Norddeutschland stärker einschränken",
            )
            candidate.evaluation = evaluation
            candidate.notes = reason
            all_candidates.append(candidate)
            feedback_pool.append(evaluation.search_adjustment)
            return 1

        org_slug, slug_reason = await resolve_candidate_slug(
            model=model,
            identity_summary=identity_summary,
            candidate=candidate,
            registry=org_registry,
        )
        candidate.org_slug = org_slug
        candidate.duplicate_reason = slug_reason
        existing_record = org_registry.get(org_slug)
        if org_slug in seen_org_slugs:
            append_log(
                "candidate.duplicate.run_skip",
                slug=org_slug,
                name=candidate.name,
                url=candidate.url,
                reason=slug_reason,
            )
            candidate.notes = slug_reason or "Bereits im aktuellen Lauf aufgenommen."
            all_candidates.append(candidate)
            return 1
        if existing_record and existing_record.status in {"accepted", "contacted"}:
            reason = slug_reason or f"Organisation bereits {existing_record.status}."
            append_log(
                "candidate.duplicate.registry_skip",
                slug=org_slug,
                name=candidate.name,
                status=existing_record.status,
                reason=reason,
            )
            candidate.notes = reason
            all_candidates.append(candidate)
            return 1
        org_registry.upsert(
            org_slug,
            name=candidate.name,
            domain=domain_key(candidate.url),
            url=candidate.url,
            status=existing_record.status if existing_record else "seen",
            notes=slug_reason,
        )
        seen_org_slugs.add(org_slug)

        is_directory = looks_like_directory_candidate(candidate)

        if is_directory:
            evaluation = EvaluationResult(
                score=0.4,
                accepted=False,
                reason="Sammelseite/Verzeichnis – wird zur Kandidatensuche genutzt und nicht direkt angeschrieben.",
                search_adjustment="konkrete Organisation mit eigener Kontaktseite finden",
            )
        else:
            context_obj = await asyncio.to_thread(collect_candidate_context, candidate)
            candidate.context = context_obj
            evaluation = await evaluate_candidate(model, identity_summary, candidate, context_obj)
        candidate.evaluation = evaluation

        coordination: Optional[CoordinatorDecision] = None
        if not is_directory:
            coordination = await coordinate_candidate(
                model=model,
                identity_summary=identity_summary,
                candidate=candidate,
                evaluation=evaluation,
                region=region,
                context=candidate.context,
            )
            candidate.coordination = coordination

        note_parts = [evaluation.reason]
        if coordination:
            note_parts.append(f"Koordinator: {coordination.reason}")
            if coordination.dialogue:
                note_parts.append("Dialog: " + " | ".join(coordination.dialogue[:3]))
        candidate.notes = " / ".join(part for part in note_parts if part)
        all_candidates.append(candidate)

        if coordination and coordination.keyword_hints:
            feedback_pool.extend(coordination.keyword_hints)

        if coordination and coordination.blacklist:
            reason = coordination.blacklist_reason or coordination.reason
            blacklist.add(
                candidate.url,
                reason=reason,
                tag="coordinator",
                source="coordinator",
                meta={"candidate": candidate.name},
            )
            append_log(
                "blacklist.add",
                url=candidate.url,
                reason=reason,
                tag="coordinator",
            )

        processed = 1
        coordinator_override = False
        if coordination and coordination.approved and not evaluation.accepted:
            score_gate = evaluation.score >= max(0.25, accept_threshold * 0.85)
            coordinator_override = score_gate and not coordination.blacklist

        should_accept = (
            (
                evaluation.accepted
                and evaluation.score >= accept_threshold
            )
            or coordinator_override
        )
        if coordination:
            should_accept = (
                should_accept
                and coordination.approved
                and not coordination.blacklist
            )

        if (
            should_accept
            and not is_directory
            and len(accepted) < plan.target_candidates
        ):
            accepted.append(candidate)
            console(f"Kandidat akzeptiert: {candidate.name}")
            if candidate.org_slug:
                org_registry.mark_status(candidate.org_slug, "accepted")
            if letter_dispatcher:
                await letter_dispatcher.enqueue(candidate)
        elif evaluation.search_adjustment:
            feedback_pool.append(evaluation.search_adjustment)

        should_expand = (
            depth < DIRECTORY_MAX_DEPTH
            and candidate.url not in expanded_directories
            and not (coordination and coordination.blacklist)
            and (
                is_directory
                or (
                    evaluation.score >= DIRECTORY_EXPANSION_MIN_SCORE
                    and looks_like_directory_candidate(candidate)
                )
            )
        )
        if should_expand:
            try:
                entries = await asyncio.to_thread(
                    expand_directory,
                    candidate.url,
                    max_entries=DIRECTORY_MAX_ENTRIES,
                )
            except DirectoryParserError as exc:
                append_log("directory.error", source=candidate.url, error=str(exc))
            else:
                expanded_directories.add(candidate.url)
                if entries:
                    append_log(
                        "directory.expand",
                        source=candidate.url,
                        count=len(entries),
                    )
                    console(
                        f"Directory {candidate.name} lieferte {len(entries)} Untereintraege."
                    )
                    for entry in entries:
                        derived_candidate = candidate_from_directory_entry(
                            candidate,
                            entry,
                        )
                        processed += await process_candidate(derived_candidate, depth + 1)
        return processed

    while len(accepted) < plan.target_candidates and iteration < max_iterations and queries:
        iteration += 1
        console(f"--- Suchiteration {iteration} mit {len(queries)} Queries ---")
        new_candidates_in_iteration = 0

        for query in queries:
            used_queries.append(query)
            results: List[SearchResult] = []
            backend_used = None

            if search_model is not None:
                try:
                    results = await run_web_search_agent(
                        search_model,
                        query=query,
                        max_results=max_results_per_query,
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
                try:
                    results = await asyncio.to_thread(
                        search_fn,
                        query,
                        max_results=max_results_per_query,
                    )
                    backend_used = backend_name
                    console(f"{backend_name} Suche fuer '{query}' gestartet ...")
                except Exception as exc:
                    append_log(
                        "search.error",
                        backend=backend_name,
                        query=query,
                        error=str(exc),
                    )
                    console(f"{backend_name} Suche abgebrochen (Limit/Fehler): {exc}")
                    search_failed = True
                    break

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
                results = await filter_search_results(model, identity_summary, query, results)
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
                processed = await process_candidate(candidate)
                new_candidates_in_iteration += processed

        if len(accepted) >= plan.target_candidates:
            break

        if search_failed:
            console("Suche wurde aufgrund eines Fehlers/Limit erreicht. Nutze vorhandene Kandidaten.")
            append_log("search.partial", reason="search_failed", accepted=len(accepted), considered=len(all_candidates))
            break

        remaining = plan.target_candidates - len(accepted)
        recent_accepts = accepted[-5:]
        new_queries, direct_candidates = await refine_queries(
            model=model,
            identity_summary=identity_summary,
            base_task="Finde nicht-kommerzielle Maker-Kollektive fuer die Maker Faire Lübeck.",
            feedback_hints=feedback_pool[-10:],  # letzte Hinweise reichen
            used_queries=used_queries,
            missing=remaining,
            region=region,
            recent_accepts=recent_accepts,
        )
        for direct_candidate in direct_candidates:
            processed = await process_candidate(direct_candidate)
            new_candidates_in_iteration += processed
        queries = [q for q in iter_queries(new_queries) if q not in used_queries]

        if new_candidates_in_iteration == 0 and not queries:
            console("Keine neuen Kandidaten gefunden, Abbruch.")
            break

    return accepted, all_candidates, backend_name, empty_searches


async def async_main() -> None:
    args = parse_args()
    presets = phase_presets(args.phase)
    ensure_dirs()
    load_env_file(ENV_PATH)
    ensure_required_env(["OPENAI_BASE_URL", "OPENAI_MODEL"])

    identity = load_identity()
    identity_summary = get_identity_summary(identity)

    chat_model, search_model = build_models()
    max_iterations = args.max_iterations
    if max_iterations is None:
        max_iterations = get_int_setting("PIPELINE_MAX_ITERATIONS", presets["max_iterations"])
    max_iterations = max(3, int(max_iterations))

    max_results_per_query = args.results_per_query
    if max_results_per_query is None:
        max_results_per_query = get_int_setting(
            "PIPELINE_RESULTS_PER_QUERY", presets["results_per_query"]
        )
    max_results_per_query = max(3, int(max_results_per_query))

    requested_letters = args.letters_per_run
    if requested_letters is None:
        requested_letters = get_int_setting("MAX_LETTERS_PER_RUN", DEFAULT_MAX_LETTERS)
    requested_letters = max(0, int(requested_letters))
    target_override = args.target_candidates
    if target_override is not None:
        try:
            target_override = int(target_override)
        except (TypeError, ValueError):
            target_override = None
    console(
        f"Phase: {args.phase} | Region: {args.region} | Iterationen: {max_iterations} | Treffer/Query: {max_results_per_query} | Briefe-Wunsch: {requested_letters}"
    )

    task = (
        "Finde mindestens 50 nicht-kommerzielle Maker:innen, Vereine oder Kollektive "
        "aus Norddeutschland (z. B. Chaotikum, Fuchsbau, offene Werkstätten), "
        "die zur Maker Faire Lübeck 2026 passen."
    )
    append_log("pipeline.start", task=task)
    plan, planner_raw = await run_planner(chat_model, task, identity_summary)
    extend_plan_with_region(plan, args.region)
    plan.search_queries = enforce_focus_keywords(plan.search_queries)
    if target_override is None:
        target_override = requested_letters or plan.target_candidates or DEFAULT_TARGET_CANDIDATES
    final_target = max(1, int(target_override))
    if requested_letters:
        final_target = max(final_target, requested_letters)
    plan.target_candidates = final_target
    os.environ["MAX_LETTERS_PER_RUN"] = str(plan.target_candidates)
    console(f"Kandidaten-Ziel gesetzt auf {plan.target_candidates} (Briefe 1:1).")
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
    append_log(
        "pipeline.config",
        phase=args.phase,
        region=args.region,
        max_iterations=max_iterations,
        results_per_query=max_results_per_query,
        letters_per_run=plan.target_candidates,
        requested_letters=requested_letters,
        target_candidates=plan.target_candidates,
    )

    blacklist = BlacklistManager()
    console(f"Geladene Blacklist-Eintraege: {len(blacklist)}")
    append_log("blacklist.loaded", entries=len(blacklist))
    org_registry = OrganizationRegistry()
    console(f"Geladene Organisations-Registry: {len(org_registry)}")
    append_log("registry.loaded", entries=len(org_registry))
    letter_dispatcher = LetterDispatcher(
        limit=plan.target_candidates,
        model=chat_model,
        identity_summary=identity_summary,
        blacklist=blacklist,
        org_registry=org_registry,
    )

    accept_threshold = presets["accept_threshold"]

    resume_candidates: Optional[List[CandidateInfo]] = None
    resume_path = normalize_resume_path(args.resume_candidates)
    if resume_path:
        resume_candidates = load_candidates_from_snapshot(resume_path)
        if not resume_candidates:
            raise RuntimeError(f"Snapshot {resume_path} enthält keine akzeptierten Kandidaten.")
        console(f"Resume-Modus: {len(resume_candidates)} Kandidaten aus {resume_path} geladen.")
        append_log(
            "resume.loaded",
            path=str(resume_path),
            candidates=len(resume_candidates),
        )

    if resume_candidates is not None:
        accepted = resume_candidates
        all_candidates = list(resume_candidates)
        backend_name = "resume"
        empty_searches = 0
        for candidate in accepted:
            candidate.org_slug = candidate.org_slug or default_org_slug(candidate)
            org_registry.upsert(
                candidate.org_slug,
                name=candidate.name,
                domain=domain_key(candidate.url),
                url=candidate.url,
                status="accepted",
                notes="Resume-Modus",
            )
        for candidate in accepted:
            await letter_dispatcher.enqueue(candidate)
    else:
        accepted, all_candidates, backend_name, empty_searches = await orchestrate_search(
            chat_model,
            search_model,
            identity_summary,
            plan,
            blacklist,
            org_registry,
            max_iterations=max_iterations,
            max_results_per_query=max_results_per_query,
            region=args.region,
            accept_threshold=accept_threshold,
            letter_dispatcher=letter_dispatcher,
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

    if len(accepted) < plan.target_candidates:
        missing = plan.target_candidates - len(accepted)
        warning_msg = (
            f"Warnung: Ziel von {plan.target_candidates} Kandidaten nicht erreicht (Fehlen {missing})."
        )
        console(warning_msg)
        append_log(
            "search.target_shortfall",
            target=plan.target_candidates,
            actual=len(accepted),
            missing=missing,
        )

    letter_stats = await letter_dispatcher.finalize()
    letters_written = letter_stats.get("completed", 0)
    if letters_written == 0 and plan.target_candidates > 0:
        console("Keine passenden Kandidaten für Anschreiben gefunden.")
    append_log(
        "pipeline.done",
        accepted=len(accepted),
        letters=letters_written,
        letter_stats=letter_stats,
    )
    summarize_run(plan, accepted, all_candidates, letter_stats)

    persisted = blacklist.persist()
    if persisted:
        append_log("blacklist.persisted", path=str(persisted))
        console(f"Blacklist aktualisiert: {persisted}")
    reg_path = org_registry.save()
    if reg_path:
        append_log("registry.persisted", path=str(reg_path))
        console(f"Organisations-Registry aktualisiert: {reg_path}")


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except Exception as exc:  # pragma: no cover
        append_log("pipeline.failed", error=str(exc))
        raise
