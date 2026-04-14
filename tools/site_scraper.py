"""Fetches and summarizes basic info from candidate web pages."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
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
XML_DECLARATION_RE = re.compile(r"^\s*<\?xml[^>]*encoding[^>]*\?>", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
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
LINK_LIMIT = 12


class SiteScraperError(RuntimeError):
    """Signals fetch/parse issues for site snapshots."""


@dataclass
class ContactInfo:
    email: str
    name: str = ""
    context: str = ""
    source_url: str = ""


@dataclass
class SiteSnapshot:
    url: str
    title: str
    summary: str
    highlights: List[str]
    detected_location: Optional[str]
    contacts: List[ContactInfo] = field(default_factory=list)
    links: List[str] = field(default_factory=list)


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


def _extract_contacts(doc: html.HtmlElement, url: str) -> List[ContactInfo]:
    contacts: List[ContactInfo] = []
    seen: set[str] = set()

    def add_contact(email: str, name: str = "", context: str = "") -> None:
        normalized = email.strip().lower()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        contacts.append(ContactInfo(email=normalized, name=name.strip(), context=context.strip(), source_url=url))

    for node in doc.xpath("//a[starts-with(@href, 'mailto:')]"):
        href = node.attrib.get("href", "")
        mail = href.split("mailto:")[-1].split("?")[0]
        text = " ".join(node.text_content().split())
        add_contact(mail, name=text, context="mailto link")

    text_blob = " ".join(doc.text_content().split())
    for match in EMAIL_RE.findall(text_blob):
        add_contact(match, context="page text")

    if not contacts:
        # Suche nach Kontaktblöcken (Impressum/Kontakt)
        for section in doc.xpath("//section[contains(translate(., 'KONTAKT', 'kontakt'), 'kontakt') or contains(translate(., 'IMPRESSUM', 'impressum'), 'impressum')]"):
            section_text = " ".join(section.text_content().split())
            for match in EMAIL_RE.findall(section_text):
                add_contact(match, context="contact section")

    return contacts


def _extract_links(doc: html.HtmlElement, url: str) -> List[str]:
    parsed = urlparse(url)
    base_domain = parsed.netloc.lower()
    links: List[str] = []
    seen: set[str] = set()
    for node in doc.xpath("//a[@href]"):
        href = node.attrib.get("href", "").strip()
        if not href:
            continue
        if href.startswith("#") or href.startswith("mailto:"):
            continue
        absolute = urljoin(url, href)
        parsed_link = urlparse(absolute)
        if not parsed_link.scheme.startswith("http"):
            continue
        domain = parsed_link.netloc.lower()
        if domain == base_domain:
            continue
        normalized = absolute.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= LINK_LIMIT:
            break
    return links


def _detect_location(text: str) -> Optional[str]:
    lowered = text.lower()
    for cue in LOCATION_CUES:
        if cue in lowered:
            return cue.title()
    match = re.search(r"\b([A-ZÄÖÜ][a-zäöü]+)\b", text)
    if match:
        return match.group(1)
    return None


def _strip_encoding_declaration(text: str) -> str:
    return XML_DECLARATION_RE.sub("", text, count=1)


def _parse_snapshot(url: str, html_text: str) -> SiteSnapshot:
    sanitized = _strip_encoding_declaration(html_text)
    try:
        doc = html.fromstring(sanitized)
    except ValueError as exc:
        message = str(exc)
        if "encoding declaration" in message.lower():
            try:
                doc = html.fromstring(sanitized.encode("utf-8", errors="ignore"))
            except ValueError as inner_exc:
                raise SiteScraperError(f"Fehler beim HTML-Parsing für {url}: {inner_exc}") from inner_exc
        else:
            raise SiteScraperError(f"Fehler beim HTML-Parsing für {url}: {exc}") from exc
    title = (" ".join(doc.xpath("//title/text()")) or url).strip()
    summary = _extract_summary(doc)
    highlights = _extract_highlights(doc)
    location = _detect_location(summary + " " + " ".join(highlights))
    contacts = _extract_contacts(doc, url)
    return SiteSnapshot(
        url=url,
        title=title,
        summary=summary,
        highlights=highlights,
        detected_location=location,
        contacts=contacts,
        links=_extract_links(doc, url),
    )


def load_cached_snapshot(url: str) -> Optional[SiteSnapshot]:
    path = _cache_path(url)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    contacts: List[ContactInfo] = []
    for entry in payload.get("contacts", []) or []:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if not email:
            continue
        contacts.append(
            ContactInfo(
                email=email,
                name=entry.get("name", ""),
                context=entry.get("context", ""),
                source_url=entry.get("source_url", payload.get("url", url)),
            )
        )
    return SiteSnapshot(
        url=payload.get("url", url),
        title=payload.get("title", url),
        summary=payload.get("summary", ""),
        highlights=payload.get("highlights", []) or [],
        detected_location=payload.get("detected_location"),
        contacts=contacts,
        links=payload.get("links", []) or [],
    )


def store_snapshot(snapshot: SiteSnapshot) -> Path:
    path = _cache_path(snapshot.url)
    payload = asdict(snapshot)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fetch_site_snapshot(url: str, *, use_cache: bool = True, retries: int = 1, backoff: float = 2.0) -> Optional[SiteSnapshot]:
    if use_cache:
        cached = load_cached_snapshot(url)
        if cached:
            return cached
    attempt = 0
    while attempt <= retries:
        try:
            html_text = _fetch(url)
            snapshot = _parse_snapshot(url, html_text)
            store_snapshot(snapshot)
            return snapshot
        except SiteScraperError:
            attempt += 1
            if attempt > retries:
                return None
            time.sleep(backoff * attempt)
    return None


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


def fetch_related_snapshots(url: str, *, max_pages: int = MAX_RELATED_PAGES, use_cache: bool = True, retries: int = 1, backoff: float = 2.0) -> List[SiteSnapshot]:
    snapshots: List[SiteSnapshot] = []
    for candidate in build_related_urls(url)[:max_pages]:
        snapshot = fetch_site_snapshot(candidate, use_cache=use_cache, retries=retries, backoff=backoff)
        if snapshot:
            snapshots.append(snapshot)
    return snapshots


__all__ = ["ContactInfo", "SiteSnapshot", "SiteScraperError", "fetch_site_snapshot", "fetch_related_snapshots", "build_related_urls"]
