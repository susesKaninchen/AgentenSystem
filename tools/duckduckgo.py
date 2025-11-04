"""
Wrapper für DuckDuckGo-Suchen über die Bibliothek `duckduckgo-search`.

Dieses Modul kapselt Suchanfragen und sorgt für persistente Ablagen
der Rohtreffer, damit spätere Evaluierungen und Audits möglich bleiben.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List

import time

from duckduckgo_search import DDGS
from duckduckgo_search.exceptions import RatelimitException

SEARCH_OUTPUT_DIR = Path("data/staging/search")


@dataclass
class DuckDuckGoResult:
    query: str
    title: str
    url: str
    snippet: str
    source: str = "duckduckgo"


def ensure_search_dir(path: Path = SEARCH_OUTPUT_DIR) -> None:
    path.mkdir(parents=True, exist_ok=True)


def search_duckduckgo(
    query: str,
    *,
    max_results: int = 6,
    region: str = "de-de",
    safesearch: str = "moderate",
    max_retries: int = 4,
) -> List[DuckDuckGoResult]:
    """
    Führt eine DuckDuckGo-Websuche durch und liefert strukturierte Resultate zurück.

    Args:
        query: Suchstring.
        max_results: Maximale Anzahl an Treffern pro Query.
        region: Regionaleinstellung (z. B. "de-de" für deutschsprachige Treffer).
        safesearch: DuckDuckGo-Safesearch-Level ("off", "moderate", "strict").
    """
    ensure_search_dir()
    attempt = 0
    delay = 2.0
    while attempt <= max_retries:
        try:
            time.sleep(1.5 if attempt == 0 else delay / 2)
            results: List[DuckDuckGoResult] = []
            with DDGS() as ddgs:
                for item in ddgs.text(
                    query,
                    region=region,
                    safesearch=safesearch,
                    max_results=max_results,
                    backend="lite",
                ):
                    results.append(
                        DuckDuckGoResult(
                            query=query,
                            title=item.get("title", "").strip(),
                            url=item.get("href", "").strip(),
                            snippet=item.get("body", "").strip(),
                        )
                    )
            return results
        except RatelimitException:
            attempt += 1
            if attempt > max_retries:
                break
            print(f"Rate-Limit bei DuckDuckGo, warte {delay:.1f}s und versuche erneut...")
            time.sleep(delay)
            delay *= 2
    return []


def store_search_results(
    query: str, results: Iterable[DuckDuckGoResult], *, directory: Path = SEARCH_OUTPUT_DIR
) -> Path:
    """
    Persistiert die Ergebnisse einer Suchanfrage als JSON-Datei.
    """
    ensure_search_dir(directory)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    file_path = directory / f"{slugify(query)}_{timestamp}.json"
    payload = {
        "query": query,
        "fetched_at": timestamp,
        "results": [asdict(result) for result in results],
    }
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return file_path


def slugify(value: str) -> str:
    return (
        value.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace(".", "-")
        .replace("_", "-")
        .replace(",", "-")
        .strip("-")
    )


def iter_queries(queries: Iterable[str]) -> Iterator[str]:
    """
    Normalisiert Eingabewerte (leerzeilen, Duplikate) und liefert eindeutige Queries.
    """
    seen: set[str] = set()
    for query in queries:
        normalized = query.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        yield normalized
