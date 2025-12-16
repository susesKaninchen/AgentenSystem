from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml


DEFAULT_BRIEF_PATH = Path("config/brief.yaml")
DEFAULT_TEMPLATE_PATH = Path("config/outreach_template.md")


DEFAULT_TASK = (
    "Recherchiere passende Organisationen/Personen und erstelle personalisierte Erstkontakt-Nachrichten."
)

DEFAULT_TARGET_PROFILE = (
    "Suche nach passenden Gegenübern für den oben beschriebenen Auftrag. "
    "Bevorzuge Treffer mit klarer, seriöser Selbstdarstellung und einer erreichbaren Kontaktmöglichkeit "
    "(idealerweise E-Mail)."
)

DEFAULT_SEARCH_FOCUS_KEYWORDS = [
    "verein",
    "e.v.",
    "initiative",
    "kollektiv",
    "makerspace",
    "hackerspace",
    "offene werkstatt",
    "fablab",
    "open lab",
    "repair café",
    "repair cafe",
    "gemeinnützig",
    "gemeinnuetzig",
]

DEFAULT_EXCLUDE_URL_SUFFIXES = [
    ".pdf",
    ".csv",
    ".doc",
    ".ppt",
    ".xls",
    ".zip",
]

DEFAULT_EXCLUDE_DOMAINS = [
    "bundestag.de",
    "scribd.com",
]

DEFAULT_EXCLUDE_TEXT_TERMS = [
    "outlet",
    "shopping center",
    "shopping-centre",
    "einkaufszentrum",
]


@dataclass
class CampaignBrief:
    """
    Persistenter Kurzbrief, der Suche + Enrichment + Outreach steuert.

    Werte werden absichtlich als Text gehalten (statt komplexer Modelle), damit ein LLM sie
    leicht überarbeiten und stabil als YAML persistieren kann.
    """

    task: str = DEFAULT_TASK
    target_profile: str = DEFAULT_TARGET_PROFILE
    message_template_path: str = str(DEFAULT_TEMPLATE_PATH)
    language: str = "de"
    focus_areas: List[str] = field(default_factory=list)
    commercial_mode: str = "prefer_noncommercial"  # allow | prefer_noncommercial | exclude_commercial
    require_contact_email: bool = True
    max_message_words: int = 260
    avoid_promises: bool = True

    search_focus_keywords: List[str] = field(default_factory=lambda: list(DEFAULT_SEARCH_FOCUS_KEYWORDS))
    exclude_url_suffixes: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_URL_SUFFIXES))
    exclude_domains: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DOMAINS))
    exclude_text_terms: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_TEXT_TERMS))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CampaignBrief":
        def list_of_strings(key: str, fallback: List[str]) -> List[str]:
            raw = data.get(key, fallback)
            if isinstance(raw, list):
                return [str(item).strip() for item in raw if str(item).strip()]
            if isinstance(raw, str) and raw.strip():
                return [raw.strip()]
            return list(fallback)

        return cls(
            task=str(data.get("task", cls.task)).strip() or DEFAULT_TASK,
            target_profile=str(data.get("target_profile", cls.target_profile)).strip() or DEFAULT_TARGET_PROFILE,
            message_template_path=str(data.get("message_template_path", cls.message_template_path)).strip()
            or str(DEFAULT_TEMPLATE_PATH),
            language=str(data.get("language", cls.language)).strip() or "de",
            focus_areas=list_of_strings("focus_areas", []),
            commercial_mode=str(data.get("commercial_mode", cls.commercial_mode)).strip()
            or "prefer_noncommercial",
            require_contact_email=bool(data.get("require_contact_email", cls.require_contact_email)),
            max_message_words=int(data.get("max_message_words", cls.max_message_words) or 260),
            avoid_promises=bool(data.get("avoid_promises", cls.avoid_promises)),
            search_focus_keywords=list_of_strings("search_focus_keywords", DEFAULT_SEARCH_FOCUS_KEYWORDS),
            exclude_url_suffixes=list_of_strings("exclude_url_suffixes", DEFAULT_EXCLUDE_URL_SUFFIXES),
            exclude_domains=list_of_strings("exclude_domains", DEFAULT_EXCLUDE_DOMAINS),
            exclude_text_terms=list_of_strings("exclude_text_terms", DEFAULT_EXCLUDE_TEXT_TERMS),
        )


def load_campaign_brief(path: Path = DEFAULT_BRIEF_PATH) -> CampaignBrief:
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return CampaignBrief.from_dict(raw)
    return CampaignBrief()


def load_message_template(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def brief_summary(brief: CampaignBrief) -> str:
    areas = ", ".join(brief.focus_areas) if brief.focus_areas else "—"
    return (
        f"Task: {brief.task}\n"
        f"Zielprofil: {brief.target_profile}\n"
        f"Fokus: {areas}\n"
        f"Template: {brief.message_template_path}\n"
        f"Commercial: {brief.commercial_mode} | Kontakt-Mail Pflicht: {brief.require_contact_email}\n"
        f"Sprache: {brief.language} | Max Wörter: {brief.max_message_words}"
    )


__all__ = [
    "CampaignBrief",
    "DEFAULT_BRIEF_PATH",
    "DEFAULT_TEMPLATE_PATH",
    "brief_summary",
    "load_campaign_brief",
    "load_message_template",
]

