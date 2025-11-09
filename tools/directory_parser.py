"""
Helper to expand directory/overview pages into concrete Maker candidates.

The parser is intentionally lightweight: it fetches the HTML, scans for links
whose anchor text resembles Maker-/Hackerspace-Namen and returns them as
structured entries. Results are cached on disk (`data/staging/directory_expansions`)
so repeated runs do not hammer the same sources.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from lxml import html

DIRECTORY_CACHE_DIR = Path("data/staging/directory_expansions")
USER_AGENT = "AgentenSystem/DirectoryParser/1.0"
MAX_FETCH_BYTES = 2_500_000  # 2.5 MB safety net
ENTRY_KEYWORDS = [
    "maker",
    "hack",
    "werkstatt",
    "werkstaetten",
    "space",
    "labor",
    "lab",
    "fablab",
    "kollektiv",
    "initiative",
    "verein",
    "freiraum",
    "fab",
    "open lab",
    "openlab",
    "repair",
    "reparatur",
    "club",
    "sojus",
    "chaos",
    "fablab",
    "diy",
]


class DirectoryParserError(RuntimeError):
    """Signals issues while fetching or parsing a directory page."""


@dataclass
class DirectoryEntry:
    name: str
    url: str
    description: str


def _slugify(value: str) -> str:
    return (
        value.lower()
        .replace("https://", "")
        .replace("http://", "")
        .replace("/", "-")
        .replace("\\", "-")
        .replace("?", "-")
        .replace("&", "-")
        .replace("=", "-")
        .replace("#", "-")
        .strip("-")
    )


def _cache_path(source_url: str) -> Path:
    DIRECTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(source_url)
    return DIRECTORY_CACHE_DIR / f"{slug}.json"


def load_cached_entries(source_url: str) -> List[DirectoryEntry]:
    path = _cache_path(source_url)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = payload.get("entries") or []
    entries: List[DirectoryEntry] = []
    for item in items:
        name = (item.get("name") or "").strip()
        url = (item.get("url") or "").strip()
        description = (item.get("description") or "").strip()
        if not name or not url:
            continue
        entries.append(DirectoryEntry(name=name, url=url, description=description))
    return entries


def store_entries(source_url: str, entries: Iterable[DirectoryEntry]) -> Path:
    path = _cache_path(source_url)
    payload = {
        "source": source_url,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": [asdict(entry) for entry in entries],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _fetch_html(url: str, *, timeout: float = 15.0) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read(MAX_FETCH_BYTES)
    except HTTPError as exc:  # pragma: no cover - network dependent
        raise DirectoryParserError(f"HTTP {exc.code} für {url}") from exc
    except URLError as exc:  # pragma: no cover
        raise DirectoryParserError(f"Seite nicht erreichbar ({exc})") from exc
    return content.decode("utf-8", errors="ignore")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _anchor_matches(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in ENTRY_KEYWORDS)


def parse_directory_entries(
    url: str,
    *,
    max_entries: int = 25,
    min_links: int = 3,
) -> List[DirectoryEntry]:
    """
    Returns potential Maker entries extracted from an overview page.
    """
    html_text = _fetch_html(url)
    doc = html.fromstring(html_text)
    anchors = doc.xpath("//a[@href]")
    parsed = urlparse(url)
    base_host = parsed.netloc

    entries: List[DirectoryEntry] = []
    seen_urls: set[str] = set()

    for anchor in anchors:
        if len(entries) >= max_entries:
            break
        text = _normalize(anchor.text_content() or "")
        if not text or len(text) < 3:
            continue
        if not _anchor_matches(text):
            continue
        href = anchor.get("href") or ""
        if href.startswith("#"):
            continue
        resolved = urljoin(url, href)
        parsed_target = urlparse(resolved)
        if not parsed_target.scheme.startswith("http"):
            continue
        # Avoid jumping to completely unrelated TLDs unless explicitly linked.
        if parsed_target.netloc == base_host and parsed_target.path in {"/", ""}:
            continue
        resolved = resolved.rstrip("/")
        if resolved in seen_urls:
            continue
        parent_text = anchor.getparent().text_content() if anchor.getparent() is not None else text
        description = _normalize(parent_text).replace(text, "").strip(" :-–|")
        if not description:
            description = f"Gefunden über {url}"
        entries.append(DirectoryEntry(name=text, url=resolved, description=description))
        seen_urls.add(resolved)

    if len(entries) < min_links:
        return []
    return entries


def expand_directory(
    url: str,
    *,
    max_entries: int = 25,
    min_links: int = 3,
    use_cache: bool = True,
) -> List[DirectoryEntry]:
    """
    Fetches (or loads cached) directory entries for the given URL.
    """
    cached = load_cached_entries(url) if use_cache else []
    if cached:
        return cached[:max_entries]

    entries = parse_directory_entries(url, max_entries=max_entries, min_links=min_links)
    if entries:
        store_entries(url, entries)
    return entries


__all__ = [
    "DirectoryEntry",
    "DirectoryParserError",
    "expand_directory",
    "load_cached_entries",
    "parse_directory_entries",
    "store_entries",
]
