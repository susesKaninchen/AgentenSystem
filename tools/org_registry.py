"""Persistent organization registry to deduplicate candidates across runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

REGISTRY_PATH = Path("data/staging/organizations_registry.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class OrganizationRecord:
    slug: str
    name: str
    domain: str
    primary_url: str
    status: str = "seen"  # seen | accepted | contacted | rejected
    notes: str = ""
    last_seen: str = field(default_factory=_now)

    def as_dict(self) -> dict:
        return asdict(self)


class OrganizationRegistry:
    def __init__(self, path: Path = REGISTRY_PATH):
        self.path = path
        self.records: Dict[str, OrganizationRecord] = {}
        self.changed = False
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for item in payload.get("records", []):
            slug = item.get("slug")
            domain = item.get("domain")
            if not slug or not domain:
                continue
            record = OrganizationRecord(
                slug=slug,
                name=item.get("name", slug),
                domain=domain,
                primary_url=item.get("primary_url", ""),
                status=item.get("status", "seen"),
                notes=item.get("notes", ""),
                last_seen=item.get("last_seen", _now()),
            )
            self.records[slug] = record

    def save(self) -> Optional[Path]:
        if not self.changed:
            return None
        payload = {
            "generated_at": _now(),
            "records": [record.as_dict() for record in sorted(self.records.values(), key=lambda r: r.slug)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.changed = False
        return self.path

    def get(self, slug: str) -> Optional[OrganizationRecord]:
        return self.records.get(slug)

    def upsert(self, slug: str, *, name: str, domain: str, url: str, status: str = "seen", notes: str = "") -> OrganizationRecord:
        record = self.records.get(slug)
        if record is None:
            record = OrganizationRecord(slug=slug, name=name, domain=domain, primary_url=url, status=status, notes=notes)
            self.records[slug] = record
        else:
            record.name = name or record.name
            record.primary_url = url or record.primary_url
            record.notes = notes or record.notes
            record.status = status or record.status
            record.last_seen = _now()
        self.changed = True
        return record

    def mark_status(self, slug: str, status: str, notes: str = "") -> None:
        record = self.records.get(slug)
        if record is None:
            return
        record.status = status
        if notes:
            record.notes = notes
        record.last_seen = _now()
        self.changed = True

    def recent_records(self, limit: int = 12) -> List[OrganizationRecord]:
        return sorted(self.records.values(), key=lambda rec: rec.last_seen, reverse=True)[:limit]

    def is_active(self, slug: str) -> bool:
        record = self.records.get(slug)
        if record is None:
            return False
        return record.status in {"accepted", "contacted", "seen"}

    def __len__(self) -> int:
        return len(self.records)


__all__ = ["OrganizationRegistry", "OrganizationRecord", "REGISTRY_PATH"]
