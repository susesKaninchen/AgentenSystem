"""
Einfacher Planner -> Recherche -> Writer Workflow.

Dies ist ein Mock-Testlauf, der zeigt, wie Agenten zusammenarbeiten koennen:
- Planner bricht Aufgabe in Unteraufgaben auf (hier statisch simuliert).
- Recherche-Agent parst vorbereitete Texte aus `data/raw/` und extrahiert Kerninfos.
- Writer-Agent erstellt ein Anschreiben unter Nutzung der Identitaet.

Alle Ergebnisse werden in Textdateien abgelegt:
- `data/staging/research_notes.md`
- `outputs/letters/<slug>.md`
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from tools.identity_loader import get_identity_summary, load_identity


RAW_DATA_DIR = Path("data/raw")
STAGING_FILE = Path("data/staging/research_notes.md")
OUTPUT_DIR = Path("outputs/letters")

ENV_PATH = Path(".env")


@dataclass
class CandidateInfo:
    name: str
    summary: str
    url: str
    notes: str

    def to_markdown(self) -> str:
        return (
            f"## {self.name}\n"
            f"- URL: {self.url}\n"
            f"- Zusammenfassung: {self.summary}\n"
            f"- Notizen: {self.notes}\n"
        )


def list_raw_files() -> List[Path]:
    return sorted(p for p in RAW_DATA_DIR.glob("*.json"))


def load_candidates() -> list[CandidateInfo]:
    candidates: list[CandidateInfo] = []
    for file in list_raw_files():
        data = json.loads(file.read_text(encoding="utf-8"))
        candidates.append(
            CandidateInfo(
                name=data.get("name", "Unbekannt"),
                summary=data.get("summary", ""),
                url=data.get("url", ""),
                notes=data.get("notes", ""),
            )
        )
    return candidates


def ensure_dirs() -> None:
    STAGING_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def planner_agent_prompts(task: str) -> str:
    return (
        "Du bist ein Planungs-Agent. Zerlege die Aufgabe in maximal drei Schritte "
        "und liefere sie als nummerierte Liste.\n"
        f"Aufgabe: {task}"
    )


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def ensure_required_env(vars_: Iterable[str]) -> None:
    missing = [var for var in vars_ if not os.environ.get(var)]
    if missing:
        raise RuntimeError(
            "Diese Umgebungsvariablen werden benötigt, sind aber nicht gesetzt: "
            + ", ".join(missing)
        )


def build_model() -> OpenAIChatCompletionsModel:
    model_name = os.environ["OPENAI_MODEL"]
    base_url = os.environ["OPENAI_BASE_URL"]
    api_key = os.environ["OPENAI_API_KEY"]
    os.environ.setdefault("OPENAI_DEFAULT_MODEL", model_name)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    print(f"Verwende Endpoint: {client.base_url}")
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def run_planner(task: str) -> str:
    model = build_model()
    agent = Agent(
        name="Planner",
        instructions=(
            "Strukturierte Kurzplanung liefern. Gib nur nummerierte Schritte aus."
        ),
        model=model,
    )
    result = Runner.run_sync(agent, planner_agent_prompts(task))
    return result.final_output or ""


def run_research_agent(identity_summary: str, candidates: Iterable[CandidateInfo]) -> str:
    model = build_model()
    agent = Agent(
        name="Researcher",
        instructions=(
            "Analysiere die folgenden Kandidaten basierend auf ihrer Beschreibung. "
            "Bewerte, ob sie fuer eine Maker Faire interessant sind, und fasse wichtige Infos "
            "als Markdown-Liste zusammen. Beziehe die Identitaet des Auftraggebers mit ein."
        ),
        model=model,
    )

    bullet_points = []
    for candidate in candidates:
        bullet_points.append(
            f"- Name: {candidate.name}\n"
            f"  URL: {candidate.url}\n"
            f"  Summary: {candidate.summary}\n"
            f"  Notes: {candidate.notes}\n"
        )
    prompt = (
        "Identitaet:\n"
        f"{identity_summary}\n\n"
        "Kandidaten:\n"
        + "\n".join(bullet_points)
        + "\n\nErstelle eine strukturierte Bewertung."
    )
    result = Runner.run_sync(agent, prompt)
    return result.final_output or ""


def run_writer_agent(identity_summary: str, candidate: CandidateInfo) -> str:
    model = build_model()
    agent = Agent(
        name="LetterWriter",
        instructions=(
            "Du verfasst personalisierte, freundliche Einladungen fuer die Maker Faire. "
            "Verwende hoechstens 220 Woerter. Hebe die Vorteile fuer den Empfaenger hervor."
        ),
        model=model,
    )

    prompt = (
        f"Identitaet:\n{identity_summary}\n\n"
        f"Kandidat:\nName: {candidate.name}\nURL: {candidate.url}\n"
        f"Summary: {candidate.summary}\nNotizen: {candidate.notes}\n\n"
        "Schreibe nun ein Einladungsschreiben (Markdown) mit persoenlicher Ansprache."
    )
    result = Runner.run_sync(agent, prompt)
    return result.final_output or ""


def store_research_notes(planner_output: str, research_output: str) -> None:
    STAGING_FILE.parent.mkdir(parents=True, exist_ok=True)
    STAGING_FILE.write_text(
        "# Recherche-Notizen\n\n"
        "## Aufgabenplan\n"
        f"{planner_output}\n\n"
        "## Bewertung\n"
        f"{research_output}\n",
        encoding="utf-8",
    )


def slugify(value: str) -> str:
    return (
        value.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("_", "-")
        .strip("-")
    )


def store_letter(candidate: CandidateInfo, letter_content: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    file_path = OUTPUT_DIR / f"{slugify(candidate.name)}.md"
    file_path.write_text(letter_content, encoding="utf-8")
    return file_path


def main() -> None:
    ensure_dirs()

    load_env_file(ENV_PATH)
    ensure_required_env(["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"])

    identity = load_identity()
    identity_summary = get_identity_summary(identity)

    planner_output = run_planner("Finde passende Aussteller fuer die Maker Faire Lübeck.")
    print("Planner Output:\n", planner_output)

    candidates = load_candidates()
    if not candidates:
        raise RuntimeError(
            f"Keine Kandidaten gefunden. Bitte JSON-Dateien in {RAW_DATA_DIR} ablegen."
        )

    research_output = run_research_agent(identity_summary, candidates)
    store_research_notes(planner_output, research_output)
    print("Recherche-Notizen gespeichert:", STAGING_FILE)

    # Writer erstellt Anschreiben fuer den ersten Kandidaten
    letter = run_writer_agent(identity_summary, candidates[0])
    letter_path = store_letter(candidates[0], letter)
    print("Anschreiben gespeichert unter:", letter_path)


if __name__ == "__main__":
    main()
