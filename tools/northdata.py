"""
Hilfsfunktionen zur Abfrage der NorthData-Suggest-API.

Ermöglicht es, Firmeninformationen anhand eines Namens (und optional Länderfilter)
aufzurufen und die Ergebnisse lokal zu persistieren. Die Implementierung nutzt
Standard-Bibliotheken, um zusätzliche Abhängigkeiten zu vermeiden.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional


NORTHDATA_BASE_URL = "https://www.northdata.de"
ENRICHMENT_DIR = Path("data/staging/enrichment")


@dataclass
class NorthDataSuggestion:
    """Repräsentiert einen Eintrag aus der NorthData-Suggest-API."""

    title: str
    name: str
    description: Optional[str]
    url: str
    type: Optional[str] = None


class NorthDataError(RuntimeError):
    """Eigener Fehler, falls die Suggest-API nicht erreichbar ist."""


def _build_suggest_url(query: str, countries: Optional[str] = None) -> str:
    params = {"query": query}
    if countries:
        params["countries"] = countries
    encoded = urllib.parse.urlencode(params, doseq=True)
    return f"{NORTHDATA_BASE_URL}/suggest.json?{encoded}"


def fetch_suggestions(
    query: str,
    *,
    countries: Optional[str] = None,
    timeout: float = 10.0,
    user_agent: str = "Agentensystem/0.1 (+https://fablab-luebeck.de)",
) -> List[NorthDataSuggestion]:
    """
    Ruft NorthData-Suggest-API auf und liefert gefundene Vorschläge zurück.

    Args:
        query: Anfrage-String (z. B. Firmenname + Ort).
        countries: Optionaler Ländercode (z. B. "DE,AT").
        timeout: Timeout in Sekunden für den HTTP-Request.
        user_agent: User-Agent-Header zur höflichen Kennzeichnung.
    """
    url = _build_suggest_url(query, countries)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.URLError as exc:  # pragma: no cover - Netzwerkteil
        raise NorthDataError(f"NorthData-Suggest nicht erreichbar: {exc}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise NorthDataError("Antwort von NorthData konnte nicht geparst werden.") from exc

    results = data.get("results", [])
    suggestions: List[NorthDataSuggestion] = []
    for item in results:
        suggestions.append(
            NorthDataSuggestion(
                title=item.get("title") or item.get("name") or query,
                name=item.get("name") or item.get("title") or query,
                description=item.get("description"),
                url=item.get("url", ""),
                type=item.get("type"),
            )
        )
    return suggestions


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


def store_suggestions(
    query: str,
    suggestions: Iterable[NorthDataSuggestion],
    *,
    target_dir: Path = ENRICHMENT_DIR,
) -> Path:
    """
    Persistiert die Suggestions als JSON-Datei unterhalb von data/staging/enrichment.

    Die Datei kann für spätere Auswertungen oder Debugging genutzt werden.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    file_path = target_dir / f"northdata_{slugify(query)}.json"
    payload = {
        "query": query,
        "fetched_at": timestamp,
        "results": [asdict(suggestion) for suggestion in suggestions],
    }
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return file_path


def format_top_suggestion(suggestions: List[NorthDataSuggestion]) -> str:
    """
    Liefert einen kurzen beschreibenden Text für den ersten Treffer.
    """
    if not suggestions:
        return "NorthData: keine Treffer."

    top = suggestions[0]
    parts = [f"{top.title}"]
    if top.description:
        parts.append(f"Standort: {top.description}")
    parts.append(f"Quelle: {top.url}")
    return " | ".join(parts)
