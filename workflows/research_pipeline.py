"""
Automatisierte Recherche- und Outreach-Pipeline.

Steuerung:
- `config/brief.yaml` definiert Auftrag, Zielprofil und Template-Pfad.
- `config/outreach_template.md` ist die Vorlage, die der Writer personalisiert.

Workflow (high level):
- Planner erzeugt Schritte + Suchqueries.
- Suche via WebSearchTool (falls verfügbar) oder Google/DuckDuckGo-Fallback.
- ResultFilter/Evaluator/Coordinator priorisieren passende Kandidaten.
- Site-Scraper sammelt Kontext + Kontaktmails; optional NorthData als Zusatzsignal.
- Profile-Enricher erzeugt strukturierte Profile (JSON), Writer + QA generieren Entwürfe.
- Zwischenergebnisse werden gespeichert (u. a. `data/staging/*`, `outputs/*`, `logs/pipeline.log`).
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
from workflows.brief import DEFAULT_BRIEF_PATH, CampaignBrief, load_campaign_brief, load_message_template
from workflows.settings import PipelineSettings, load_pipeline_settings
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
from tools.site_scraper import ContactInfo, SiteSnapshot, fetch_site_snapshot, fetch_related_snapshots
from tools.web_search_agent import run_web_search_agent


STAGING_NOTES = Path("data/staging/research_notes.md")
STAGING_ACCEPTED = Path("data/staging/candidates_selected.json")
OUTPUT_DIR = Path("outputs/letters")
PROFILES_DIR = Path("outputs/profiles")
LOG_DIR = Path("logs")
PIPELINE_LOG = LOG_DIR / "pipeline.log"
ENV_PATH = Path(".env")
STOP_FILE_DEFAULT = Path("data/staging/stop.flag")
LAST_RUN_PATH = Path("data/staging/last_run.json")

# Disable tracing to avoid noisy warnings when no tracing key is configured.
set_tracing_disabled(True)


def console(message: str) -> None:
    print(f"[PIPELINE] {message}")

DEFAULT_TARGET_CANDIDATES = 5
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_MAX_RESULTS_PER_QUERY = 10
EVALUATION_ACCEPT_THRESHOLD = 0.62
NORTHDATA_COUNTRIES = "DE"
MAX_QA_RETRIES = 3
DUCKDUCKGO_QUERY_DELAY = 2.5
DEFAULT_SEARCH_RETRIES = 2
DEFAULT_SEARCH_RETRY_BACKOFF = 3.0
PROMISE_PATTERNS = [
    r"\bversprech",
    r"\bgarantier",
    r"\bverpflicht",
    r"\bsichern\s+zu",
    r"\bdefinitiv\b",
]
DEFAULT_NEGATIVE_URL_SUFFIXES = (".pdf", ".csv", ".doc", ".ppt", ".xls", ".zip")
DEFAULT_NEGATIVE_DOMAINS = {
    "bundestag.de",
    "scribd.com",
    "editeur.org",
    "b-u-b.de",
    "telekom-stiftung.de",
    "bne-portal.de",
    "schleswig-holstein.de",
}
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
PARTNER_LINK_LIMIT = 5
DEFAULT_PHASE = "acquire"
DEFAULT_REGION = "nord"  # placeholder for macro areas


def generate_region_queries(brief: CampaignBrief, region: str, *, limit: int = 10) -> List[str]:
    region_hint = (region or "").strip()
    if not region_hint or region_hint.lower() in {"any", "all", "global", "world"}:
        return []
    region_hint = region_hint.replace("-", " ").strip()

    terms: List[str] = []
    if brief.focus_areas:
        terms.extend([area.strip() for area in brief.focus_areas[:3] if area.strip()])
    if brief.search_focus_keywords:
        terms.extend([kw.strip() for kw in brief.search_focus_keywords[:3] if kw.strip()])
    if not terms:
        return []

    proposals: List[str] = []
    for term in terms:
        proposals.append(f"{region_hint} {term}")
        proposals.append(f"{region_hint} {term} Kontakt E-Mail")
        proposals.append(f"{region_hint} {term} Ansprechpartner E-Mail")
        if len(proposals) >= limit:
            break

    unique: List[str] = []
    seen: set[str] = set()
    for query in proposals:
        key = query.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(query)
        if len(unique) >= limit:
            break
    return unique


def fallback_region_queries(
    used_queries: Iterable[str],
    missing: int,
    region: str,
    brief: CampaignBrief,
) -> List[str]:
    pool = generate_region_queries(brief, region, limit=14)
    if not pool:
        return []
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


def should_skip_url(url: str, brief: CampaignBrief) -> bool:
    lowered = url.lower()
    suffixes = {suffix.lower() for suffix in (brief.exclude_url_suffixes or [])} | {
        suffix.lower() for suffix in DEFAULT_NEGATIVE_URL_SUFFIXES
    }
    if any(lowered.endswith(suffix) for suffix in suffixes):
        return True
    domain = urlparse(url).netloc.lower()
    blocked_domains = {neg.lower() for neg in (brief.exclude_domains or [])} | {
        neg.lower() for neg in DEFAULT_NEGATIVE_DOMAINS
    }
    if any(domain.endswith(neg) for neg in blocked_domains):
        return True
    return False


def candidate_matches_region(candidate: CandidateInfo, region: str) -> bool:
    if not region:
        return True
    if region.strip().lower() in {"any", "all", "global", "world"}:
        return True
    text = (_candidate_text(candidate) + " " + candidate.source_query).lower()
    positives = NORD_REGION_KEYWORDS + REGION_POSITIVE_KEYWORDS.get(region.lower(), [])
    if any(keyword in text for keyword in OFF_REGION_KEYWORDS):
        return False
    if any(keyword in text for keyword in positives):
        return True
    return True  # Unbekannt => nicht blockieren


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recherche- und Outreach-Pipeline")
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
        "--brief",
        default=os.environ.get("PIPELINE_BRIEF", str(DEFAULT_BRIEF_PATH)),
        help="Pfad zum Briefing YAML (Standard: config/brief.yaml).",
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
    parser.add_argument(
        "--stop-file",
        default=str(STOP_FILE_DEFAULT),
        help="Wenn diese Datei existiert, werden keine neuen Such-/Scrape-Aufgaben mehr gestartet (laufende Tasks beenden sauber).",
    )
    parser.add_argument(
        "--search-retries",
        type=int,
        default=os.environ.get("PIPELINE_SEARCH_RETRIES"),
        help="Anzahl Wiederholungen pro Query bei Suchfehlern.",
    )
    parser.add_argument(
        "--search-retry-backoff",
        type=float,
        default=os.environ.get("PIPELINE_SEARCH_BACKOFF"),
        help="Backoff (Sekunden) zwischen Such-Retries.",
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
    org_type: str = ""
    org_size: str = ""
    region_hint: str = ""
    nonprofit: bool = False
    maker_focus: bool = False
    outreach_priority: float = 0.0
    contact_quality: float = 0.0


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
    profile_path: str = ""
    contacts: List[ContactInfo] = field(default_factory=list)

    def as_markdown(self) -> str:
        base = (
            f"## {self.name}\n"
            f"- URL: {self.url}\n"
            f"- Quelle: {self.source_query}\n"
            f"- Kurzbeschreibung: {self.summary or self.snippet}\n"
            f"- Bewertung: {self.notes or (self.evaluation.reason if self.evaluation else 'Noch nicht bewertet')}\n"
        )
        if self.evaluation:
            base += f"- Score: {self.evaluation.score:.2f}\n"
            if self.evaluation.category:
                base += f"- Kategorie: {self.evaluation.category}\n"
            if self.evaluation.org_type or self.evaluation.org_size:
                base += f"- Typ/Größe: {self.evaluation.org_type or '—'} / {self.evaluation.org_size or '—'}\n"
            if self.evaluation.contact_quality:
                base += f"- Kontaktqualität: {self.evaluation.contact_quality:.2f}\n"
        if self.contacts:
            emails = ", ".join(contact.email for contact in self.contacts[:4])
            base += f"- Kontakt: {emails}\n"
        if self.northdata_info:
            base += f"- NorthData: {self.northdata_info}\n"
        if self.profile_path:
            base += f"- Profil: {self.profile_path}\n"
        if self.letter_path:
            base += f"- Letter: {self.letter_path} ({self.letter_status})\n"
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
    contacts: List[ContactInfo] = field(default_factory=list)
    partner_links: List[str] = field(default_factory=list)


class FeedbackBus:
    def __init__(self, limit: int = 30) -> None:
        self.limit = limit
        self._hints: List[str] = []

    def add(self, hint: str) -> None:
        text = (hint or "").strip()
        if not text:
            return
        self._hints.append(text)
        if len(self._hints) > self.limit:
            self._hints = self._hints[-self.limit :]

    def recent(self, n: int = 10) -> List[str]:
        return self._hints[-n:]


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


def store_last_run_summary(
    *,
    brief_path: Path,
    brief: CampaignBrief,
    phase: str,
    region: str,
    plan: PlannerPlan,
    accepted: Sequence[CandidateInfo],
    all_candidates: Sequence[CandidateInfo],
    backend: str,
    empty_searches: int,
    letter_stats: dict[str, int],
    contacts_export: Optional[Path],
) -> None:
    letters_done = int(letter_stats.get("completed", 0) or 0)
    candidates_payload = [
        {
            "name": c.name,
            "url": c.url,
            "org_slug": c.org_slug,
            "letter_status": c.letter_status,
            "letter_path": c.letter_path,
            "emails": [contact.email for contact in (c.contacts or [])],
            "score": (c.evaluation.score if c.evaluation else None),
            "category": (c.evaluation.category if c.evaluation else ""),
            "org_type": (c.evaluation.org_type if c.evaluation else ""),
        }
        for c in accepted[:50]
    ]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "brief_path": str(brief_path),
        "brief": {
            "task": brief.task,
            "target_profile": brief.target_profile,
            "focus_areas": brief.focus_areas,
            "template_path": brief.message_template_path,
            "language": brief.language,
            "commercial_mode": brief.commercial_mode,
            "require_contact_email": brief.require_contact_email,
            "max_message_words": brief.max_message_words,
        },
        "run": {
            "phase": phase,
            "region": region,
            "target_candidates": plan.target_candidates,
            "accepted": len(accepted),
            "considered": len(all_candidates),
            "letters_done": letters_done,
            "backend": backend,
            "empty_searches": empty_searches,
        },
        "artifacts": {
            "snapshot": str(STAGING_ACCEPTED),
            "notes": str(STAGING_NOTES),
            "log": str(PIPELINE_LOG),
            "contacts_csv": str(contacts_export) if contacts_export else "",
        },
        "accepted_preview": candidates_payload,
    }
    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class StopSignal:
    def __init__(self, path: Optional[Path]) -> None:
        self.path = path

    def triggered(self) -> bool:
        return bool(self.path and self.path.exists())


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
    model: OpenAIChatCompletionsModel,
    *,
    brief: CampaignBrief,
    identity_summary: str,
    region: str,
) -> tuple[PlannerPlan, str]:
    console("Starte Planner-Aufruf ...")
    agent = Agent(
        name="Planner",
        instructions=(
            "Du planst eine Web-Recherche, um passende Gegenueber fuer einen Outreach-Auftrag zu finden. "
            "Gib ausschließlich JSON zurück mit den Feldern:\n"
            '{"plan_steps": ["Schritt 1", ...], "search_queries": ["query1", ...], "target_candidates": 50}\n'
            "Die Suchqueries sollen konkrete Websuchen fuer passende Gegenueber zum Auftrag sein. "
            "Erzeuge mindestens 5 Queries. Verwende keine doppelten Werte und keine extrem langen Queries.\n"
            "Gib nur JSON zurück, kein Fließtext."
        ),
        model=model,
    )
    focus = ", ".join(brief.focus_areas) if brief.focus_areas else "—"
    prompt = (
        "Auftrag:\n"
        f"{brief.task}\n\n"
        "Identität des Auftraggebers:\n"
        f"{identity_summary}\n"
        f"Region-Fokus: {region}\n"
        f"Fokus-Themen: {focus}\n\n"
        f"Gesuchtes Profil:\n{brief.target_profile}\n"
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
        queries.extend(generate_fallback_queries(brief, region))

    console(
        f"Planner lieferte {len(steps or [])} Schritte, {len(queries)} Queries, Ziel {target} Kandidaten."
    )
    return PlannerPlan(steps=steps or DEFAULT_PLAN_STEPS, search_queries=queries, target_candidates=target), raw_output


DEFAULT_PLAN_STEPS = [
    "Zielgruppe, Kriterien und Randbedingungen klären.",
    "Passende Gegenüber online recherchieren und priorisieren.",
    "Treffer bewerten, anreichern und Outreach-Entwürfe erstellen.",
]

def generate_fallback_queries(brief: CampaignBrief, region: str) -> List[str]:
    region_hint = region.strip()
    if region_hint.lower() in {"any", "all", "global", "world"}:
        region_hint = ""
    region_hint = region_hint.replace("-", " ").strip()

    seeds: List[str] = []
    if brief.focus_areas:
        seeds.extend([area.strip() for area in brief.focus_areas if area.strip()])
    if brief.search_focus_keywords:
        seeds.extend([kw.strip() for kw in brief.search_focus_keywords[:4] if kw.strip()])
    if not seeds:
        seeds = ["Kontakt", "Impressum", "E-Mail"]

    proposals: List[str] = []
    for seed in seeds:
        base = seed.strip()
        if not base:
            continue
        if region_hint:
            proposals.append(f"{region_hint} {base} Kontakt E-Mail")
        else:
            proposals.append(f"{base} Kontakt E-Mail")
        proposals.append(f"{base} Impressum E-Mail")
        proposals.append(f"{base} Ansprechpartner E-Mail")
        if len(proposals) >= 8:
            break

    unique: List[str] = []
    seen: set[str] = set()
    for query in proposals:
        key = query.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(query)
        if len(unique) >= 6:
            break
    return unique


def build_candidates_from_search(
    query: str, results: Sequence[SearchResult], *, brief: CampaignBrief
) -> List[CandidateInfo]:
    candidates: List[CandidateInfo] = []
    exclude_terms = {term.lower() for term in (brief.exclude_text_terms or []) if term.strip()}
    for item in results:
        if not item.url:
            continue
        if should_skip_url(item.url, brief):
            append_log(
                "search.filtered",
                url=item.url,
                reason="negative_source",
            )
            continue
        title = item.title or item.url
        combined_text = f"{title} {item.snippet}".lower()
        if exclude_terms and any(term in combined_text for term in exclude_terms):
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
    brief: CampaignBrief,
    query: str,
    results: Sequence[SearchResult],
) -> Sequence[SearchResult]:
    if len(results) <= 3:
        return results
    agent = Agent(
        name="ResultFilter",
        instructions=(
            "Du bist ein Recherche-Koordinator. Entferne Duplikate (gleiche Domains), offensichtlichen Spam "
            "und reine Verzeichnis-/Newsseiten. "
            "Behalte höchstens 6 Ergebnisse pro Query. Bevorzuge echte Projekt-/About-/Kontakt-Seiten, die "
            "zum Auftrag/Zielprofil passen. "
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
        f"Auftrag:\n{brief.task}\n\n"
        f"Zielprofil:\n{brief.target_profile}\n\n"
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


def candidate_from_partner_link(parent: CandidateInfo, url: str) -> CandidateInfo:
    parsed = urlparse(url)
    hostname = parsed.netloc.replace("www.", "")
    name = hostname.split(":")[0] or url
    return CandidateInfo(
        name=name,
        url=url,
        summary=f"Gefunden als Partner-/Netzwerk-Link auf {parent.url}",
        source_query=f"partner:{parent.url}",
        snippet="",
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
                category=str(evaluation_data.get("category", "") or ""),
                org_type=str(evaluation_data.get("org_type", "") or ""),
                org_size=str(evaluation_data.get("org_size", "") or ""),
                region_hint=str(evaluation_data.get("region_hint", "") or ""),
                nonprofit=bool(evaluation_data.get("nonprofit", False)),
                maker_focus=bool(evaluation_data.get("maker_focus", False)),
                outreach_priority=float(evaluation_data.get("outreach_priority", 0.0) or 0.0),
                contact_quality=float(evaluation_data.get("contact_quality", 0.0) or 0.0),
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
            profile_path=entry.get("profile_path", ""),
        )
        contacts: List[ContactInfo] = []
        for contact in entry.get("contacts", []) or []:
            if not isinstance(contact, dict):
                continue
            email = contact.get("email")
            if not email:
                continue
            contacts.append(
                ContactInfo(
                    email=email,
                    name=contact.get("name", ""),
                    context=contact.get("context", ""),
                    source_url=contact.get("source_url", candidate.url),
                )
            )
        candidate.contacts = dedupe_contacts(contacts)
        candidate.evaluation = evaluation
        candidates.append(candidate)
    return candidates


def extend_plan_with_region(plan: PlannerPlan, region: str, brief: CampaignBrief) -> None:
    region_queries = generate_region_queries(brief, region, limit=10)
    if not region_queries:
        return
    existing = set(q.lower() for q in plan.search_queries)
    additions = [q for q in region_queries if q.lower() not in existing]
    if additions:
        plan.search_queries.extend(additions)
        console(f"Region '{region}' Queries hinzugefügt: {additions}")


def enforce_focus_keywords(queries: Sequence[str], brief: CampaignBrief) -> List[str]:
    keywords = [keyword.strip().lower() for keyword in (brief.search_focus_keywords or []) if keyword.strip()]
    if not keywords:
        return list(queries)
    suffix = " ".join((brief.search_focus_keywords or [])[:2]).strip()
    normalized: List[str] = []
    for query in queries:
        lowered = query.lower()
        if any(keyword in lowered for keyword in keywords):
            normalized.append(query)
        else:
            normalized.append(f"{query} {suffix}".strip() if suffix else query)
    return normalized


async def evaluate_candidate(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    brief: CampaignBrief,
    candidate: CandidateInfo,
    context: Optional[CandidateContext],
    *,
    region: str,
) -> EvaluationResult:
    console(f"Bewerte Kandidat: {candidate.name} ({candidate.url}) ...")
    agent = Agent(
        name="Evaluator",
        instructions=(
            "Bewerte, ob der Kandidat zum Auftrag und Zielprofil passt. "
            "Warnung: Reine Verzeichnisse/Sammelseiten (z. B. Listen, Übersichten, Guides) dürfen nicht akzeptiert werden; "
            "bevorzuge stattdessen konkrete Gruppen/Organisationen mit eigener Kontaktmöglichkeit. "
            "Antworte ausschließlich als JSON mit Feldern:\n"
            '{"score": 0.0-1.0, "accepted": true/false, "reason": "...", "search_adjustment": "...", '
            '"category": "kurzes label", "org_type": "verein|firma|einzelperson|schule|uni|kollektiv|oeffentlich|unbekannt", '
            '"org_size": "solo|klein|mittel|gross|unbekannt", "region_hint": "...", '
            '"nonprofit": true/false, "maker_focus": true/false, '
            '"outreach_priority": 0.0-1.0, "contact_quality": 0.0-1.0}\n'
            "score beschreibt die Passung (>=0.62 wird typischerweise akzeptiert). "
            '"search_adjustment" enthält einen Hinweis, wie künftige Queries präzisiert werden können '
            "(z. B. \"mehr Bildungspartner\" oder \"weniger reine Händler\"). "
            "Wenn kein Hinweis nötig ist, verwende einen leeren String."
        ),
        model=model,
    )
    context_block = format_context_for_prompt(context)
    focus = ", ".join(brief.focus_areas) if brief.focus_areas else "—"
    prompt = (
        "Identität des Auftraggebers:\n"
        f"{identity_summary}\n\n"
        f"Auftrag:\n{brief.task}\n\n"
        f"Region-Fokus: {region}\n"
        f"Fokus-Themen: {focus}\n\n"
        f"Gesuchtes Profil:\n{brief.target_profile}\n\n"
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
            "org_type": "unbekannt",
            "org_size": "unbekannt",
            "region_hint": "",
            "nonprofit": False,
            "maker_focus": False,
            "outreach_priority": 0.0,
            "contact_quality": 0.0,
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
        org_type=str(data.get("org_type", "")).strip(),
        org_size=str(data.get("org_size", "")).strip(),
        region_hint=str(data.get("region_hint", "")).strip(),
        nonprofit=bool(data.get("nonprofit", False)),
        maker_focus=bool(data.get("maker_focus", False)),
        outreach_priority=float(data.get("outreach_priority", 0.0) or 0.0),
        contact_quality=float(data.get("contact_quality", 0.0) or 0.0),
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
    brief: CampaignBrief,
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
    focus = ", ".join(brief.focus_areas) if brief.focus_areas else "—"
    prompt = (
        f"Identität:\n{identity_summary}\n\n"
        f"Auftrag:\n{brief.task}\n\n"
        f"Gesuchtes Profil:\n{brief.target_profile}\n\n"
        f"Region-Fokus: {region}\n"
        f"Fokus-Themen: {focus}\n\n"
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
    brief: CampaignBrief,
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
            "Erzeuge neue Suchqueries (Google Custom Search oder DuckDuckGo) passend zum Auftrag und Zielprofil. "
            "Nutze außerdem direkte Webseiten-Hinweise: Wenn du konkrete Vereine oder URLs kennst, füge sie unter "
            "\"direct_urls\" ein, dann crawlt das System diese Seiten direkt ohne Websuche. "
            "Antworte ausschließlich im JSON-Format: "
            '{"new_queries": ["..."], "direct_urls": [{"name": "...", "url": "...", "summary": "..."}]}. '
            "Meide bereits verwendete Queries und fokussiere dich auf die Hinweise."
        ),
        model=model,
    )
    focus = ", ".join(brief.focus_areas) if brief.focus_areas else "—"
    prompt = (
        f"Auftrag: {brief.task}\n\n"
        "Identität:\n"
        f"{identity_summary}\n\n"
        f"Region-Fokus: {region}\n"
        f"Fokus-Themen: {focus}\n\n"
        f"Noch benötigte Kandidaten: {missing}\n"
        "Bereits verwendete Queries:\n"
        + "\n".join(f"- {query}" for query in used_queries)
        + "\n\n"
        "Hinweise aus bisherigen Bewertungen:\n"
        + ("\n".join(f"- {hint}" for hint in hints) if hints else "- gezielt nach konkreten Vereinen/Offenen Werkstätten in Norddeutschland suchen")
        + "\n\n"
        "Bereits akzeptierte Kandidaten (verwende Themen/Ortsangaben als Inspiration für neue Suchbegriffe oder Direktlinks):\n"
        f"{accepted_block}\n\n"
        f"Gesuchtes Profil:\n{brief.target_profile}\n\n"
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
        queries = fallback_region_queries(used_queries, missing, region, brief)
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


def export_contacts(accepted: Sequence[CandidateInfo]) -> Optional[Path]:
    if not accepted:
        return None
    rows: List[str] = ["name,url,email,org_slug,notes"]
    for candidate in accepted:
        emails = candidate.contacts or []
        if not emails:
            continue
        for contact in emails:
            email = contact.email.replace(",", " ")
            notes = (candidate.notes or "").replace(",", " ")
            rows.append(
                ",".join(
                    [
                        candidate.name.replace(",", " "),
                        candidate.url,
                        email,
                        candidate.org_slug or "",
                        notes,
                    ]
                )
            )
    if len(rows) == 1:
        return None
    export_path = Path("outputs") / "contacts.csv"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text("\n".join(rows), encoding="utf-8")
    return export_path


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
    contact_emails = ", ".join(contact.email for contact in candidate.contacts) if candidate.contacts else ""
    metadata_lines = [
        "---",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"candidate: {candidate.name}",
        f"source_url: {candidate.url}",
        f"words: {word_count(letter_content)}",
    ]
    if contact_emails:
        metadata_lines.append(f"contact_emails: {contact_emails}")
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
    if snapshot.contacts:
        contact_lines = "\n".join(
            f"  * {contact.email} ({contact.name or 'Kontakt'}, {contact.context or 'Quelle unbekannt'})"
            for contact in snapshot.contacts
        )
        parts.append("- Kontakte:\n" + contact_lines)
    if snapshot.links:
        link_lines = "\n".join(f"  * {link}" for link in snapshot.links[:5])
        parts.append("- Partner-/Netzwerk-Links (ausgehende Links):\n" + link_lines)
    return "\n".join(parts)


def format_context_for_prompt(context: Optional[CandidateContext]) -> str:
    if context is None:
        return ""
    parts: List[str] = []
    if context.primary:
        parts.append("Hauptseite:\n" + format_snapshot_for_prompt(context.primary))
    for idx, snapshot in enumerate(context.related, start=1):
        parts.append(f"Subseite #{idx}:\n" + format_snapshot_for_prompt(snapshot))
    if context.contacts:
        contact_lines = "\n".join(
            f"- {contact.email} ({contact.name or 'Kontakt'}, {contact.context or 'Quelle unbekannt'})"
            for contact in context.contacts
        )
        parts.append("Gesammelte Kontakte:\n" + contact_lines)
    if context.partner_links:
        link_lines = "\n".join(f"- {link}" for link in context.partner_links[:8])
        parts.append("Gefundene Partner-/Netzwerk-Links:\n" + link_lines)
    return "\n\n".join(part for part in parts if part)


def dedupe_contacts(contacts: Iterable[ContactInfo]) -> List[ContactInfo]:
    unique: List[ContactInfo] = []
    seen: set[str] = set()
    for contact in contacts:
        email = contact.email.strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        unique.append(contact)
    return unique


def dedupe_links(links: Iterable[str], base_url: str) -> List[str]:
    base_domain = domain_key(base_url)
    unique: List[str] = []
    seen: set[str] = set()
    for link in links:
        normalized = link.rstrip("/")
        if not normalized or normalized in seen:
            continue
        if domain_key(normalized) == base_domain:
            continue
        seen.add(normalized)
        unique.append(normalized)
        if len(unique) >= 12:
            break
    return unique


def collect_candidate_context(candidate: CandidateInfo) -> CandidateContext:
    primary = fetch_site_snapshot(candidate.url, retries=1, backoff=2.0)
    related = fetch_related_snapshots(candidate.url, max_pages=3, retries=1, backoff=2.0)
    all_contacts: List[ContactInfo] = []
    partner_links: List[str] = []
    if primary:
        all_contacts.extend(primary.contacts)
        partner_links.extend(primary.links)
    for snapshot in related:
        all_contacts.extend(snapshot.contacts)
        partner_links.extend(snapshot.links)
    contacts = dedupe_contacts(all_contacts)
    partner_links = dedupe_links(partner_links, base_url=candidate.url)
    return CandidateContext(primary=primary, related=related, contacts=contacts, partner_links=partner_links)


async def build_candidate_profile(
    model: OpenAIChatCompletionsModel,
    *,
    identity_summary: str,
    brief: CampaignBrief,
    candidate: CandidateInfo,
    context: Optional[CandidateContext],
) -> dict[str, object]:
    agent = Agent(
        name="ProfileEnricher",
        instructions=(
            "Du extrahierst und normalisierst Informationen ueber das Gegenueber fuer Outreach. "
            "Nutze die Webseiten-Snapshots, Notizen und vorhandene Kontakte. "
            "Antworte ausschließlich als JSON im Schema:\n"
            "{"
            '"name": "...", "url": "...", '
            '"org_type": "verein|firma|einzelperson|schule|uni|kollektiv|oeffentlich|unbekannt", '
            '"org_size": "solo|klein|mittel|gross|unbekannt", '
            '"category": "kurzes label", "location": "...", '
            '"what_they_do": ["..."], "values": ["..."], '
            '"contact_person": {"name": "...", "role": "..."}, '
            '"contact_emails": ["..."], '
            '"summary": "...", '
            '"confidence": 0.0-1.0, '
            '"missing": ["..."]'
            "}\n"
            "Wenn etwas unbekannt ist, nutze leere Strings/Listen und setze 'missing' entsprechend."
        ),
        model=model,
    )
    context_block = format_context_for_prompt(context)
    contacts = candidate.contacts or (context.contacts if context else [])
    contacts_block = ""
    if contacts:
        contact_lines = "\n".join(
            f"- {c.email} ({c.name or 'Kontakt'}, {c.context or 'Quelle unbekannt'})"
            for c in contacts[:6]
        )
        contacts_block = "Kontakte:\n" + contact_lines

    evaluation_block = "{}"
    if candidate.evaluation:
        evaluation_block = json.dumps(asdict(candidate.evaluation), ensure_ascii=False)
    prompt = (
        f"Identitaet:\n{identity_summary}\n\n"
        f"Auftrag:\n{brief.task}\n\n"
        f"Zielprofil:\n{brief.target_profile}\n\n"
        f"Kandidat:\n- Name: {candidate.name}\n- URL: {candidate.url}\n- Summary: {candidate.summary}\n"
        f"- Notizen: {candidate.notes}\n- NorthData: {candidate.northdata_info or '—'}\n\n"
        f"Evaluator (JSON):\n{evaluation_block}\n\n"
        + (contacts_block + "\n\n" if contacts_block else "")
    )
    if context_block:
        prompt += "Webseiten-Kontext:\n" + context_block + "\n"
    result = await Runner.run(agent, prompt)

    raw = result.final_output or ""
    try:
        data = json.loads(extract_json_block(raw))
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    profile: dict[str, object] = dict(data)
    profile.setdefault("name", candidate.name)
    profile.setdefault("url", candidate.url)
    if candidate.evaluation:
        profile.setdefault("org_type", candidate.evaluation.org_type)
        profile.setdefault("org_size", candidate.evaluation.org_size)
        profile.setdefault("category", candidate.evaluation.category)

    email_set: set[str] = set()
    raw_emails = profile.get("contact_emails")
    if isinstance(raw_emails, list):
        for email in raw_emails:
            normalized = str(email).strip().lower()
            if normalized:
                email_set.add(normalized)
    for contact in contacts:
        normalized = contact.email.strip().lower()
        if normalized:
            email_set.add(normalized)
    profile["contact_emails"] = sorted(email_set)
    return profile


def store_candidate_profile(candidate: CandidateInfo, profile: dict[str, object]) -> Path:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    slug = candidate.org_slug or slugify(candidate.name)
    path = PROFILES_DIR / f"{slug}.json"
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def run_writer_agent(
    model: OpenAIChatCompletionsModel,
    identity_summary: str,
    brief: CampaignBrief,
    message_template: str,
    candidate: CandidateInfo,
    feedback: str = "",
    snapshot: Optional[SiteSnapshot] = None,
    context: Optional[CandidateContext] = None,
    profile: Optional[dict[str, object]] = None,
) -> str:
    console(f"Writer erstellt Entwurf fuer {candidate.name} ...")
    agent = Agent(
        name="LetterWriter",
        instructions=(
            "Du verfasst personalisierte Erstkontakt-Nachrichten (z. B. E-Mail) passend zum Auftrag. "
            f"Schreibe auf {brief.language} und verwende hoechstens {brief.max_message_words} Woerter. "
            "Halte dich an die bereitgestellte Vorlage (Struktur/Abschnitte), ersetze Platzhalter sinnvoll "
            "(wenn Informationen fehlen, lasse Platzhalter weg statt zu halluzinieren). "
            "Mache keine verbindlichen Versprechen oder Garantien; bleibe bei realistischer Sprache. "
            "Wenn Feedback bereitgestellt wird, arbeite es praezise ein."
        ),
        model=model,
    )
    context_block = format_context_for_prompt(context)
    if not context_block:
        context_block = format_snapshot_for_prompt(snapshot)
    contacts_block = ""
    contacts = candidate.contacts or (context.contacts if context else [])
    if contacts:
        contact_lines = "\n".join(
            f"- {c.email} ({c.name or 'Kontakt'}, {c.context or 'Quelle unbekannt'})"
            for c in contacts
        )
        contacts_block = "\nGefundene Kontakte:\n" + contact_lines

    template_block = message_template.strip() if message_template else ""
    prompt = (
        f"Identitaet:\n{identity_summary}\n\n"
        f"Auftrag:\n{brief.task}\n\n"
        f"Zielprofil:\n{brief.target_profile}\n\n"
        "Vorlage (Struktur beibehalten, Platzhalter ersetzen):\n"
        f"{template_block or '(keine Vorlage gesetzt)'}\n\n"
        f"Kandidat:\nName: {candidate.name}\nURL: {candidate.url}\n"
        f"Summary: {candidate.summary}\nNotizen: {candidate.notes}\n"
        f"NorthData: {candidate.northdata_info or 'Keine Zusatzinformationen'}\n\n"
        "Schreibe nun die Nachricht als Markdown."
    )
    if profile:
        prompt += (
            "\n\nStrukturiertes Profil (JSON, als Faktenbasis nutzen):\n"
            + json.dumps(profile, ensure_ascii=False, indent=2)
            + "\n"
        )
    if context_block:
        prompt += (
            "\n\nZusatzinfos aus der Webseitenanalyse:\n"
            f"{context_block}\n"
        )
    if contacts_block:
        prompt += contacts_block
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
    brief: CampaignBrief,
    letter_content: str,
    candidate: CandidateInfo,
) -> QAResult:
    console(f"QA prueft Anschreiben fuer {candidate.name} ...")
    instructions = (
        "Du bist ein QA-Agent fuer Anschreiben. "
        "Pruefe, ob der Text hoeﬂich, faktenbasiert und frei von Versprechen/Garantien ist. "
        f"Der Text darf max. {brief.max_message_words} Woerter enthalten. "
        "Gib nur JSON zurueck: "
        '{"approved": true/false, "notes": "<Begruendung>", "suggested_rewrite": "<Text oder leer>"}'
    )
    agent = Agent(name="QAAgent", instructions=instructions, model=model)
    prompt = (
        f"Auftrag:\n{brief.task}\n\n"
        "Pruefe diese Nachricht:\n\n"
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
    brief: CampaignBrief,
    message_template: str,
    candidate: CandidateInfo,
    snapshot: Optional[SiteSnapshot] = None,
    context: Optional[CandidateContext] = None,
    profile: Optional[dict[str, object]] = None,
) -> QAResult:
    feedback = ""
    for attempt in range(1, MAX_QA_RETRIES + 1):
        letter = await run_writer_agent(
            model=model,
            identity_summary=identity_summary,
            brief=brief,
            message_template=message_template,
            candidate=candidate,
            feedback=feedback,
            snapshot=snapshot,
            context=context,
            profile=profile,
        )
        wc = word_count(letter)
        issues = []
        if wc > brief.max_message_words:
            issues.append(f"{wc} Woerter > {brief.max_message_words}")
        if brief.avoid_promises and contains_promises(letter):
            issues.append("Text enthaelt Versprechen oder Garantien.")
        if issues:
            feedback = (
                "Bitte kuerze den Text (max. "
                f"{brief.max_message_words} Woerter) und entferne Versprechen. Gefundene Probleme: "
                + "; ".join(issues)
            )
            append_log("writer.retry", attempt=attempt, issues=issues)
            continue

        qa_result = await run_qa_agent(model, brief, letter, candidate)
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
        brief: CampaignBrief,
        message_template: str,
        blacklist: BlacklistManager,
        org_registry: OrganizationRegistry,
    ) -> None:
        self.limit = max(0, limit)
        self.model = model
        self.identity_summary = identity_summary
        self.brief = brief
        self.message_template = message_template
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
        if not self._preflight_ok(candidate):
            candidate.letter_status = "skipped"
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
            if context is None:
                try:
                    context = await asyncio.to_thread(collect_candidate_context, candidate)
                except Exception as exc:
                    append_log("context.error", candidate=candidate.name, url=candidate.url, error=str(exc))
                    context = None
                else:
                    candidate.context = context
                    if context and context.contacts:
                        candidate.contacts = context.contacts
            snapshot = context.primary if context else None
            if snapshot is None:
                try:
                    snapshot = await asyncio.to_thread(fetch_site_snapshot, candidate.url)
                except Exception as exc:
                    append_log("snapshot.error", candidate=candidate.name, url=candidate.url, error=str(exc))
                    snapshot = None

            profile: Optional[dict[str, object]] = None
            try:
                profile = await build_candidate_profile(
                    self.model,
                    identity_summary=self.identity_summary,
                    brief=self.brief,
                    candidate=candidate,
                    context=context,
                )
                profile_path = store_candidate_profile(candidate, profile)
                candidate.profile_path = str(profile_path)
                append_log("profile.saved", candidate=candidate.name, path=str(profile_path))
            except Exception as exc:
                append_log("profile.error", candidate=candidate.name, url=candidate.url, error=str(exc))

            qa_result = await generate_letter_with_guardrails(
                self.model,
                self.identity_summary,
                self.brief,
                self.message_template,
                candidate,
                snapshot=snapshot,
                context=context,
                profile=profile,
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

    def _preflight_ok(self, candidate: CandidateInfo) -> bool:
        """Manuelle Checkliste: nur senden, wenn Mindestanforderungen erfüllt sind."""
        if self.brief.require_contact_email and not candidate.contacts:
            return False
        return True

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
    brief: CampaignBrief,
    plan: PlannerPlan,
    blacklist: BlacklistManager,
    org_registry: OrganizationRegistry,
    *,
    max_iterations: int,
    max_results_per_query: int,
    region: str,
    accept_threshold: float,
    stop_signal: Optional[StopSignal] = None,
    search_retries: int = DEFAULT_SEARCH_RETRIES,
    search_retry_backoff: float = DEFAULT_SEARCH_RETRY_BACKOFF,
    letter_dispatcher: Optional[LetterDispatcher] = None,
) -> tuple[List[CandidateInfo], List[CandidateInfo], str, int]:
    accepted: List[CandidateInfo] = []
    all_candidates: List[CandidateInfo] = []
    seen_urls: set[str] = set()
    used_queries: List[str] = []
    feedback_bus = FeedbackBus()
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
    candidate_concurrency = max(1, get_int_setting("PIPELINE_CANDIDATE_CONCURRENCY", 4))
    candidate_semaphore = asyncio.Semaphore(candidate_concurrency)
    state_lock = asyncio.Lock()

    async def process_candidate(candidate: CandidateInfo, depth: int = 0) -> int:
        """Evaluates a candidate and expands directory-style pages if useful."""
        context_obj: Optional[CandidateContext] = None
        async with state_lock:
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
            reason = f"Außerhalb Zielregion '{region}' (Geo-Heuristik)."
            evaluation = EvaluationResult(
                score=0.15,
                accepted=False,
                reason=reason,
                search_adjustment=f"Region '{region}' stärker einschränken",
            )
            candidate.evaluation = evaluation
            candidate.notes = reason
            all_candidates.append(candidate)
            feedback_bus.add(evaluation.search_adjustment)
            return 1

        org_slug, slug_reason = await resolve_candidate_slug(
            model=model,
            identity_summary=identity_summary,
            candidate=candidate,
            registry=org_registry,
        )
        candidate.org_slug = org_slug
        candidate.duplicate_reason = slug_reason
        async with state_lock:
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
            candidate.contacts = context_obj.contacts
            evaluation = await evaluate_candidate(
                model,
                identity_summary,
                brief,
                candidate,
                context_obj,
                region=region,
            )
        candidate.evaluation = evaluation

        coordination: Optional[CoordinatorDecision] = None
        if not is_directory:
            coordination = await coordinate_candidate(
                model=model,
                identity_summary=identity_summary,
                brief=brief,
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
        if candidate.contacts:
            emails_preview = ", ".join(contact.email for contact in candidate.contacts[:3])
            note_parts.append(f"Kontakt-E-Mails: {emails_preview}")
        candidate.notes = " / ".join(part for part in note_parts if part)
        all_candidates.append(candidate)

        if coordination and coordination.keyword_hints:
            for hint in coordination.keyword_hints:
                feedback_bus.add(hint)

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

        if should_accept and brief.require_contact_email and not candidate.contacts:
            should_accept = False
            contact_hint = "Kontakt-E-Mail fehlt – Kontakt/Impressum stärker scrapen."
            feedback_bus.add(contact_hint)
            append_log(
                "candidate.no_contact_email",
                name=candidate.name,
                url=candidate.url,
                reason=contact_hint,
            )
            candidate.notes = (candidate.notes + " / " if candidate.notes else "") + contact_hint

        accepted_now = False
        if should_accept and not is_directory:
            async with state_lock:
                if len(accepted) < plan.target_candidates:
                    accepted.append(candidate)
                    accepted_now = True
                    if candidate.org_slug:
                        org_registry.mark_status(candidate.org_slug, "accepted")
        if accepted_now:
            console(f"Kandidat akzeptiert: {candidate.name}")
            if letter_dispatcher:
                await letter_dispatcher.enqueue(candidate)
        elif evaluation.search_adjustment:
            feedback_bus.add(evaluation.search_adjustment)

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

        partner_links = context_obj.partner_links if context_obj else []
        if partner_links and depth < DIRECTORY_MAX_DEPTH:
            limited_links = partner_links[:PARTNER_LINK_LIMIT]
            append_log(
                "partner_links.found",
                source=candidate.url,
                count=len(limited_links),
            )
            for link in limited_links:
                derived_candidate = candidate_from_partner_link(candidate, link)
                processed += await process_candidate(derived_candidate, depth + 1)
        return processed

    while len(accepted) < plan.target_candidates and iteration < max_iterations and queries:
        if stop_signal and stop_signal.triggered():
            console("Stop-Flag erkannt – keine neuen Aufgaben, laufende Tasks werden beendet.")
            append_log("pipeline.stop_flag", accepted=len(accepted), considered=len(all_candidates))
            break
        iteration += 1
        console(f"--- Suchiteration {iteration} mit {len(queries)} Queries ---")
        new_candidates_in_iteration = 0

        for query in queries:
            if stop_signal and stop_signal.triggered():
                console("Stop-Flag erkannt – breche neue Suche ab, vorhandene Ergebnisse werden verwendet.")
                break
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
                attempt = 0
                while attempt <= search_retries:
                    try:
                        results = await asyncio.to_thread(
                            search_fn,
                            query,
                            max_results=max_results_per_query,
                        )
                        backend_used = backend_name
                        console(f"{backend_name} Suche fuer '{query}' gestartet (Versuch {attempt+1}) ...")
                        break
                    except Exception as exc:
                        append_log(
                            "search.error",
                            backend=backend_name,
                            query=query,
                            error=str(exc),
                            attempt=attempt + 1,
                        )
                        attempt += 1
                        if attempt > search_retries:
                            console(f"{backend_name} Suche abgebrochen (Limit/Fehler): {exc}")
                            search_failed = True
                            break
                        await asyncio.sleep(search_retry_backoff * attempt)
                if search_failed:
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
                results = await filter_search_results(model, identity_summary, brief, query, results)
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

            candidates = build_candidates_from_search(query, results, brief=brief)
            async def process_one(candidate: CandidateInfo) -> int:
                async with candidate_semaphore:
                    try:
                        return await process_candidate(candidate)
                    except Exception as exc:
                        append_log(
                            "candidate.error",
                            candidate=getattr(candidate, "name", "unknown"),
                            url=getattr(candidate, "url", ""),
                            error=str(exc),
                            query=query,
                        )
                        console(f"[WARN] Fehler bei Kandidat {getattr(candidate, 'name', 'unknown')}: {exc} (suche laeuft weiter)")
                        return 0

            tasks = [asyncio.create_task(process_one(candidate)) for candidate in candidates]
            if tasks:
                results_processed = await asyncio.gather(*tasks, return_exceptions=True)
                for processed in results_processed:
                    if isinstance(processed, Exception):
                        continue
                    try:
                        new_candidates_in_iteration += int(processed)
                    except (TypeError, ValueError):
                        continue

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
            brief=brief,
            feedback_hints=feedback_bus.recent(10),  # letzte Hinweise reichen
            used_queries=used_queries,
            missing=remaining,
            region=region,
            recent_accepts=recent_accepts,
        )
        if direct_candidates:
            async def process_direct(candidate: CandidateInfo) -> int:
                async with candidate_semaphore:
                    try:
                        return await process_candidate(candidate)
                    except Exception as exc:
                        append_log(
                            "candidate.error",
                            candidate=getattr(candidate, "name", "unknown"),
                            url=getattr(candidate, "url", ""),
                            error=str(exc),
                            source="refine_direct",
                        )
                        console(
                            f"[WARN] Fehler bei Direkt-Kandidat {getattr(candidate, 'name', 'unknown')}: {exc} (suche laeuft weiter)"
                        )
                        return 0

            direct_results = await asyncio.gather(
                *(process_direct(cand) for cand in direct_candidates),
                return_exceptions=True,
            )
            for processed in direct_results:
                if isinstance(processed, Exception):
                    continue
                try:
                    new_candidates_in_iteration += int(processed)
                except (TypeError, ValueError):
                    continue
        queries = [q for q in iter_queries(new_queries) if q not in used_queries]

        if new_candidates_in_iteration == 0 and not queries:
            console("Keine neuen Kandidaten gefunden, Abbruch.")
            break

    return accepted, all_candidates, backend_name, empty_searches


async def async_main() -> None:
    args = parse_args()
    settings = load_pipeline_settings()
    if getattr(settings, "candidate_concurrency", None):
        os.environ.setdefault("PIPELINE_CANDIDATE_CONCURRENCY", str(settings.candidate_concurrency))
    brief_path = Path(args.brief) if args.brief else DEFAULT_BRIEF_PATH
    brief = load_campaign_brief(brief_path)
    message_template = load_message_template(Path(brief.message_template_path))
    stop_file_arg = args.stop_file
    if stop_file_arg == str(STOP_FILE_DEFAULT) and settings.stop_file:
        stop_file_arg = settings.stop_file
    stop_path: Optional[Path] = None
    if stop_file_arg and str(stop_file_arg).lower() not in {"none", "false", "0"}:
        stop_path = Path(stop_file_arg).resolve()
    stop_signal = StopSignal(stop_path)
    presets = phase_presets(args.phase)
    ensure_dirs()
    load_env_file(ENV_PATH)
    ensure_required_env(["OPENAI_BASE_URL", "OPENAI_MODEL"])

    identity = load_identity()
    identity_summary = get_identity_summary(identity)

    chat_model, search_model = build_models()
    max_iterations = (
        args.max_iterations
        if args.max_iterations is not None
        else get_int_setting("PIPELINE_MAX_ITERATIONS", settings.max_iterations or presets["max_iterations"])
    )
    max_iterations = max(3, int(max_iterations))

    max_results_per_query = (
        args.results_per_query
        if args.results_per_query is not None
        else get_int_setting("PIPELINE_RESULTS_PER_QUERY", settings.results_per_query or presets["results_per_query"])
    )
    max_results_per_query = max(3, int(max_results_per_query))

    requested_letters = (
        args.letters_per_run
        if args.letters_per_run is not None
        else get_int_setting("MAX_LETTERS_PER_RUN", settings.letters_per_run)
    )
    requested_letters = max(0, int(requested_letters))

    search_retries = (
        args.search_retries
        if args.search_retries is not None
        else settings.search_retries or DEFAULT_SEARCH_RETRIES
    )
    search_retry_backoff = (
        args.search_retry_backoff
        if args.search_retry_backoff is not None
        else settings.search_retry_backoff or DEFAULT_SEARCH_RETRY_BACKOFF
    )
    target_override = args.target_candidates
    if target_override is not None:
        try:
            target_override = int(target_override)
        except (TypeError, ValueError):
            target_override = None
    console(
        f"Phase: {args.phase} | Region: {args.region} | Iterationen: {max_iterations} | Treffer/Query: {max_results_per_query} | Briefe-Wunsch: {requested_letters}"
    )

    append_log(
        "pipeline.start",
        task=brief.task,
        brief_path=str(brief_path),
        template_path=brief.message_template_path,
        commercial_mode=brief.commercial_mode,
    )
    plan, planner_raw = await run_planner(
        chat_model,
        brief=brief,
        identity_summary=identity_summary,
        region=args.region,
    )
    extend_plan_with_region(plan, args.region, brief)
    plan.search_queries = enforce_focus_keywords(plan.search_queries, brief)
    if target_override is None:
        target_override = (
            requested_letters
            or plan.target_candidates
            or settings.target_candidates
            or DEFAULT_TARGET_CANDIDATES
        )
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
        brief=brief,
        message_template=message_template,
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
            brief,
            plan,
            blacklist,
            org_registry,
            max_iterations=max_iterations,
            max_results_per_query=max_results_per_query,
            region=args.region,
            accept_threshold=accept_threshold,
            stop_signal=stop_signal,
            search_retries=search_retries,
            search_retry_backoff=search_retry_backoff,
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

    contacts_export = export_contacts(accepted)
    if contacts_export:
        console(f"Kontakt-Export gespeichert: {contacts_export}")
        append_log("contacts.export", path=str(contacts_export))
    store_last_run_summary(
        brief_path=brief_path,
        brief=brief,
        phase=args.phase,
        region=args.region,
        plan=plan,
        accepted=accepted,
        all_candidates=all_candidates,
        backend=backend_name,
        empty_searches=empty_searches,
        letter_stats=letter_stats,
        contacts_export=contacts_export,
    )

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
