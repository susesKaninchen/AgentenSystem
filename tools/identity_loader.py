"""
Hilfsfunktionen zum Laden und Verwenden der Identitaetsdaten aus `config/identity.yaml`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


IDENTITY_PATH = Path("config/identity.yaml")


def load_identity(path: Path = IDENTITY_PATH) -> dict[str, Any]:
    """
    Gibt die Identitaetsdaten als Dictionary zurueck.

    Raises:
        FileNotFoundError: falls die Datei nicht existiert.
        yaml.YAMLError: bei ungueltigem YAML.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Identity-Datei nicht gefunden: {path}. Bitte `config/identity.yaml` bereitstellen."
        )
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def get_identity_summary(identity: dict[str, Any]) -> str:
    """
    Erstellt eine kurze textuelle Zusammenfassung der Identitaet als Prompt-String.
    """
    org = identity.get("organization", {})
    rep = identity.get("representative", {})
    event = identity.get("event", {})
    msg = identity.get("messaging_guidelines", {})

    parts: list[str] = []

    parts.append(
        f"{rep.get('name', 'Unbekannt')} ({rep.get('role', 'Rolle unbekannt')}) "
        f"vom {org.get('name', 'Organisation unbekannt')}."
    )
    if org_desc := org.get("description"):
        parts.append(f"Organisation: {org_desc}")
    if event_name := event.get("name"):
        venue = event.get("venue", "Veranstaltungsort unbekannt")
        parts.append(f"Plant aktuell: {event_name} am Standort {venue}.")
    if event.get("target_actors"):
        parts.append(f"Ziel: ca. {event['target_actors']} Ausstellende.")
    if msg_keypoints := msg.get("key_points", []):
        parts.append("Wichtige Botschaften: " + "; ".join(msg_keypoints))
    if msg_call := msg.get("call_to_action"):
        parts.append(f"Call-to-Action: {msg_call}")

    return " ".join(parts)
