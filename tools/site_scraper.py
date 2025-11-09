"""Fetches and summarizes basic info from candidate web pages."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from lxml import html

SNAPSHOT_DIR = Path("data/staging/snapshots")
USER_AGENT = "AgentenSystem/SiteScraper/1.0"
MAX_FETCH_BYTES = 3_000_000
HIGHLIGHT_LIMIT = 5
SUMMARY_MIN_CHARS = 60
SUMMARY_MAX_CHARS = 480
LOCATION_CUES = [
    "lübeck",
    "hamburg",
    "kiel",
    "bremen",
    "flensburg",
    "oldenburg",
    "bremerhaven",
    "rostock",
    "greifswald",
    "hannover",
    "braunschweig",
    "norderstedt",
]
RELATED_PATH_HINTS = [
    "kontakt",
    "contact",
    "kontaktformular",
    "impressum",
    "about",
    "ueber-uns",
    "über-uns",
    "team",
    "mitmachen",
]
MAX_RELATED_PAGES = 4


class SiteScraperError(RuntimeError):
    """Signals fetch/parse issues for site snapshots."""


@dataclass
class SiteSnapshot:
    url: str
    title: str
    summary: str
    highlights: List[str]
    detected_location: Optional[str]


def _slugify(url: str) -> str:
    parsed = urlparse(url)
    return (
        f"{parsed.netloc}{parsed.path}".replace("/", "-")
        .replace(".", "-")
        .replace("_", "-")
        .strip("-")
    ) or "index"


def _cache_path(url: str) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return SNAPSHOT_DIR / f"{_slugify(url)}.json"


def _fetch(url: str, *, timeout: float = 15.0) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read(MAX_FETCH_BYTES)
    except HTTPError as exc:  # pragma: no cover
        raise SiteScraperError(f"HTTP {exc.code} für {url}") from exc
    except URLError as exc:  # pragma: no cover
        raise SiteScraperError(f"{url} nicht erreichbar ({exc})") from exc
    return content.decode("utf-8", errors="ignore")


def _extract_summary(doc: html.HtmlElement) -> str:
    paragraphs = doc.xpath("//p")
    for paragraph in paragraphs:
        text = " ".join(paragraph.text_content().split())
        if SUMMARY_MIN_CHARS <= len(text) <= SUMMARY_MAX_CHARS:
            return text
    if paragraphs:
        text = " ".join(paragraphs[0].text_content().split())
        return text[:SUMMARY_MAX_CHARS]
    body_text = " ".join(doc.text_content().split())
    return body_text[:SUMMARY_MAX_CHARS]


def _extract_highlights(doc: html.HtmlElement) -> List[str]:
    highlights: List[str] = []
    for bullet in doc.xpath("//li"):
        text = " ".join(bullet.text_content().split())
        if 20 <= len(text) <= 200 and text not in highlights:
            highlights.append(text)
        if len(highlights) >= HIGHLIGHT_LIMIT:
            break
    return highlights


def _detect_location(text: str) -> Optional[str]:
    lowered = text.lower()
    for cue in LOCATION_CUES:
        if cue in lowered:
            return cue.title()
    match = re.search(r"\b([A-ZÄÖÜ][a-zäöü]+)\b", text)
    if match:
        return match.group(1)
    return None


def _parse_snapshot(url: str, html_text: str) -> SiteSnapshot:
    doc = html.fromstring(html_text)
    title = (" ".join(doc.xpath("//title/text()")) or url).strip()
    summary = _extract_summary(doc)
    highlights = _extract_highlights(doc)
    location = _detect_location(summary + " " + " ".join(highlights))
    return SiteSnapshot(
        url=url,
        title=title,
        summary=summary,
        highlights=highlights,
        detected_location=location,
    )


def load_cached_snapshot(url: str) -> Optional[SiteSnapshot]:
    path = _cache_path(url)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return SiteSnapshot(
        url=payload.get("url", url),
        title=payload.get("title", url),
        summary=payload.get("summary", ""),
        highlights=payload.get("highlights", []) or [],
        detected_location=payload.get("detected_location"),
    )


def store_snapshot(snapshot: SiteSnapshot) -> Path:
    path = _cache_path(snapshot.url)
    payload = asdict(snapshot)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fetch_site_snapshot(url: str, *, use_cache: bool = True) -> Optional[SiteSnapshot]:
    if use_cache:
        cached = load_cached_snapshot(url)
        if cached:
            return cached
    try:
        html_text = _fetch(url)
    except SiteScraperError:
        return None
    snapshot = _parse_snapshot(url, html_text)
    store_snapshot(snapshot)
    return snapshot


def build_related_urls(url: str, hints: Iterable[str] = RELATED_PATH_HINTS) -> List[str]:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    related: List[str] = []
    seen: set[str] = set()
    for hint in hints:
        candidate = urljoin(base, f"/{hint.strip('/')}")
        candidate = candidate.rstrip("/")
        if candidate.lower() == url.rstrip("/").lower():
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        related.append(candidate)
        if len(related) >= MAX_RELATED_PAGES:
            break
    return related


def fetch_related_snapshots(url: str, *, max_pages: int = MAX_RELATED_PAGES, use_cache: bool = True) -> List[SiteSnapshot]:
    snapshots: List[SiteSnapshot] = []
    for candidate in build_related_urls(url)[:max_pages]:
        snapshot = fetch_site_snapshot(candidate, use_cache=use_cache)
        if snapshot:
            snapshots.append(snapshot)
    return snapshots


__all__ = ["SiteSnapshot", "SiteScraperError", "fetch_site_snapshot", "fetch_related_snapshots", "build_related_urls"]
