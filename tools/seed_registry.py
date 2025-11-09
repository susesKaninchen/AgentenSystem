from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlparse

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.org_registry import OrganizationRegistry

CANDIDATES_FILE = Path("data/staging/candidates_selected.json")
LETTERS_DIR = Path("outputs/letters")


def slugify(value: str) -> str:
    value = value.lower()
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
    for key, val in replacements.items():
        value = value.replace(key, val)
    value = re.sub(r"[^a-z0-9\-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-")


def domain_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host or slugify(url)


def generate_org_slug(name: str, url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    generic = {"event", "events", "tag", "tags", "blog", "category", "projekt", "project"}
    key = ""
    if parts:
        primary = parts[0]
        if primary not in generic and not primary.isdigit():
            key = primary
    if not key:
        cleaned = "".join(ch for ch in name if not ch.isdigit()).strip() or domain_key(url)
        key = "-".join(cleaned.split()[:4])
    return slugify(f"{domain_key(url)}-{key}")


def parse_letters() -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = []
    if not LETTERS_DIR.exists():
        return entries
    for path in LETTERS_DIR.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        front_matter = {}
        for line in parts[1].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            front_matter[key.strip()] = value.strip().strip('"')
        name = front_matter.get("candidate")
        url = front_matter.get("source_url")
        if name and url:
            entries.append((name, url))
    return entries


def dedupe_candidates() -> Dict[str, dict]:
    if not CANDIDATES_FILE.exists():
        return {}
    data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    accepted = data.get("accepted", [])
    deduped: List[dict] = []
    registry_map: Dict[str, dict] = {}
    duplicates: List[dict] = []
    for entry in accepted:
        name = entry.get("name") or entry.get("url", "")
        url = entry.get("url", "")
        slug = generate_org_slug(name, url)
        if slug in registry_map:
            duplicates.append(entry)
            continue
        entry["org_slug"] = slug
        registry_map[slug] = entry
        deduped.append(entry)
    if duplicates:
        data["accepted"] = deduped
        data["duplicates_removed"] = duplicates
        CANDIDATES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return registry_map


def seed_registry(registry_map: Dict[str, dict]) -> OrganizationRegistry:
    registry = OrganizationRegistry()
    for slug, entry in registry_map.items():
        registry.upsert(
            slug,
            name=entry.get("name", slug),
            domain=domain_key(entry.get("url", "")),
            url=entry.get("url", ""),
            status="accepted",
            notes="Seeded from candidates_selected.json",
        )
    for name, url in parse_letters():
        slug = generate_org_slug(name, url)
        registry.upsert(
            slug,
            name=name,
            domain=domain_key(url),
            url=url,
            status="contacted",
            notes="Seeded from outputs/letters",
        )
    registry.save()
    return registry


def main() -> None:
    registry_map = dedupe_candidates()
    registry = seed_registry(registry_map)
    print(f"Deduplizierte Kandidaten: {len(registry_map)}")
    print(f"Registry-Einträge gesamt: {len(registry)}")


if __name__ == "__main__":
    main()
