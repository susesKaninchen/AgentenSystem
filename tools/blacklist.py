"""Utility helpers to persist and query website/domain blacklists."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

BLACKLIST_PATH = Path("data/staging/blacklist.json")


def _domain_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1].lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host or url.lower()


@dataclass
class BlacklistEntry:
    domain: str
    url: str
    reason: str
    tag: str
    added_at: str
    source: str = "pipeline"
    meta: Dict[str, str] = field(default_factory=dict)


def load_blacklist(path: Path = BLACKLIST_PATH) -> Dict[str, BlacklistEntry]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    entries: Dict[str, BlacklistEntry] = {}
    for item in payload.get("entries", []):
        domain = item.get("domain")
        url = item.get("url")
        if not domain or not url:
            continue
        entries[domain] = BlacklistEntry(
            domain=domain,
            url=url,
            reason=item.get("reason", "ohne Grund"),
            tag=item.get("tag", "unspecified"),
            added_at=item.get("added_at", datetime.now(timezone.utc).isoformat()),
            source=item.get("source", "pipeline"),
            meta=item.get("meta") or {},
        )
    return entries


def save_blacklist(entries: Dict[str, BlacklistEntry], path: Path = BLACKLIST_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": [asdict(entry) for entry in sorted(entries.values(), key=lambda e: e.domain)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class BlacklistManager:
    def __init__(self, path: Path = BLACKLIST_PATH):
        self.path = path
        self.entries = load_blacklist(path)
        self.changed = False

    def is_blacklisted(self, url: str) -> Optional[BlacklistEntry]:
        domain = _domain_key(url)
        return self.entries.get(domain)

    def add(self, url: str, *, reason: str, tag: str = "auto", source: str = "pipeline", meta: Optional[Dict[str, str]] = None) -> BlacklistEntry:
        domain = _domain_key(url)
        entry = BlacklistEntry(
            domain=domain,
            url=url,
            reason=reason,
            tag=tag,
            added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source=source,
            meta=meta or {},
        )
        self.entries[domain] = entry
        self.changed = True
        return entry

    def persist(self) -> Optional[Path]:
        if not self.changed:
            return None
        self.changed = False
        return save_blacklist(self.entries, self.path)

    def __len__(self) -> int:  # pragma: no cover - helper
        return len(self.entries)


__all__ = [
    "BlacklistEntry",
    "BlacklistManager",
    "load_blacklist",
    "save_blacklist",
]
