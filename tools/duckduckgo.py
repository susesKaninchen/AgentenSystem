"""
DuckDuckGo-Suche über die Bibliothek `duckduckgo-search`.

Wird als Fallback verwendet, wenn die Google Custom Search API nicht verfügbar ist.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List

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
    max_retries: int = 2,
) -> List[DuckDuckGoResult]:
    ensure_search_dir()
    attempt = 0
    delay = 3.0
    while attempt <= max_retries:
        try:
            print(f"[DuckDuckGo] Query '{query}' (Versuch {attempt + 1}/{max_retries + 1}) ...")
            time.sleep(1.0 if attempt == 0 else delay / 2)
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
            print(f"[DuckDuckGo] Rate-Limit fuer '{query}', warte {delay:.1f}s ...")
            attempt += 1
            if attempt > max_retries:
                break
            print(f"Rate-Limit bei DuckDuckGo, warte {delay:.1f}s und versuche erneut...")
            time.sleep(delay)
            delay *= 2
    print(f"[DuckDuckGo] Keine Ergebnisse fuer '{query}'.")
    return []


def store_search_results(
    query: str, results: Iterable[DuckDuckGoResult], *, directory: Path = SEARCH_OUTPUT_DIR
) -> Path:
    ensure_search_dir(directory)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_path = directory / f"{slugify(query)}_{timestamp}.json"
    payload = {
        "query": query,
        "fetched_at": timestamp,
        "results": [asdict(result) for result in results],
    }
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return file_path


def load_cached_results(
    query: str, *, directory: Path = SEARCH_OUTPUT_DIR
) -> List[DuckDuckGoResult]:
    ensure_search_dir(directory)
    slug = slugify(query)
    files = sorted(directory.glob(f"{slug}_*.json"), reverse=True)
    for file in files:
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items = payload.get("results") or []
        results: List[DuckDuckGoResult] = []
        for item in items:
            url = item.get("url") or item.get("href") or ""
            if not url:
                continue
            results.append(
                DuckDuckGoResult(
                    query=item.get("query", query),
                    title=item.get("title", "").strip(),
                    url=url.strip(),
                    snippet=item.get("snippet") or item.get("body", "") or "",
                    source=item.get("source", "duckduckgo"),
                )
            )
        if results:
            return results
    return []


def slugify(value: str) -> str:
    sanitized = (
        value.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("\\", "-")
        .replace(".", "-")
        .replace("_", "-")
        .replace(",", "-")
        .replace('"', "")
        .replace("'", "")
        .replace(":", "-")
        .replace(";", "-")
        .replace("|", "-")
        .replace("?", "-")
        .replace("*", "-")
        .replace("<", "-")
        .replace(">", "-")
    )
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    return sanitized.strip("-")


def iter_queries(queries: Iterable[str]) -> Iterator[str]:
    seen: set[str] = set()
    for query in queries:
        normalized = query.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        yield normalized
