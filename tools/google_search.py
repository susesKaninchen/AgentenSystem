"""
Google Custom Search (Programmable Search) Wrapper.

Dieses Modul kapselt Aufrufe an die Google-Suche über die Custom Search JSON API.
Ergebnisse werden strukturiert zurückgegeben und auf Wunsch im Dateisystem persistiert.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SEARCH_OUTPUT_DIR = Path("data/staging/search")
GOOGLE_SEARCH_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
MAX_RESULTS_PER_CALL = 10
MAX_RESULTS_TOTAL = 100


class GoogleSearchError(RuntimeError):
    """Signalisiert Fehler bei der Kommunikation mit der Google-Suche."""


@dataclass
class GoogleSearchResult:
    query: str
    title: str
    url: str
    snippet: str
    source: str = "google"


def ensure_search_dir(path: Path = SEARCH_OUTPUT_DIR) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _perform_request(params: dict[str, object]) -> dict:
    query_string = urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{GOOGLE_SEARCH_ENDPOINT}?{query_string}"
    request = Request(url, headers={"User-Agent": "AgentenSystem/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:  # pragma: no cover - Netzwerkfehler schwer testbar
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise GoogleSearchError(f"Google API HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:  # pragma: no cover
        raise GoogleSearchError(f"Google API nicht erreichbar: {exc}") from exc
    return json.loads(payload)


def search_google(
    query: str,
    *,
    max_results: int = 6,
    api_key: Optional[str] = None,
    search_engine_id: Optional[str] = None,
    max_retries: int = 3,
    pause_seconds: float = 1.0,
) -> List[GoogleSearchResult]:
    """
    Führt eine Google-Suche über die Custom Search API aus.
    """
    api_key = api_key or os.environ.get("GOOGLE_API_KEY")
    search_engine_id = search_engine_id or os.environ.get("GOOGLE_SEARCH_ENGINE_ID")
    if not api_key or not search_engine_id:
        raise RuntimeError(
            "GOOGLE_API_KEY und GOOGLE_SEARCH_ENGINE_ID müssen gesetzt sein, "
            "um die Google-Suche zu verwenden."
        )

    ensure_search_dir()
    results: List[GoogleSearchResult] = []
    start_index = 1
    retry_delay = pause_seconds

    max_results = max(1, min(max_results, MAX_RESULTS_TOTAL))

    while len(results) < max_results:
        print(
            f"[GoogleSearch] Query '{query}' (Start {start_index}) – Ziel {max_results} Ergebnisse."
        )
        num = min(MAX_RESULTS_PER_CALL, max_results - len(results))
        params = {
            "key": api_key,
            "cx": search_engine_id,
            "q": query,
            "num": num,
            "start": start_index,
            "safe": "active",
            "lr": "lang_de",
        }
        try:
            data = _perform_request(params)
        except GoogleSearchError as exc:
            if max_retries <= 0:
                raise
            print(f"Google-Suche fehlgeschlagen ({exc}); neuer Versuch in {retry_delay:.1f}s.")
            time.sleep(retry_delay)
            retry_delay *= 2
            max_retries -= 1
            continue

        if "error" in data:
            message = data["error"].get("message", "Unbekannter Google-API-Fehler.")
            raise GoogleSearchError(message)

        items = data.get("items") or []
        if not items:
            break

        for item in items:
            results.append(
                GoogleSearchResult(
                    query=query,
                    title=item.get("title", "").strip(),
                    url=item.get("link", "").strip(),
                    snippet=item.get("snippet", "").strip(),
                )
            )
            if len(results) >= max_results:
                break

        if len(items) < num:
            break

        start_index += num
        time.sleep(pause_seconds)

    print(f"[GoogleSearch] Query '{query}' lieferte {len(results)} Treffer.")
    return results


def store_search_results(
    query: str, results: Iterable[GoogleSearchResult], *, directory: Path = SEARCH_OUTPUT_DIR
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
) -> List[GoogleSearchResult]:
    ensure_search_dir(directory)
    slug = slugify(query)
    files = sorted(directory.glob(f"{slug}_*.json"), reverse=True)
    for file in files:
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items = payload.get("results") or []
        results: List[GoogleSearchResult] = []
        for item in items:
            url = item.get("url") or item.get("link") or ""
            if not url:
                continue
            results.append(
                GoogleSearchResult(
                    query=item.get("query", query),
                    title=item.get("title", "").strip(),
                    url=url.strip(),
                    snippet=item.get("snippet", "").strip(),
                    source=item.get("source", "google"),
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
