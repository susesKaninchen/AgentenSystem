"""
Einfache Proof-of-Concept-Pipeline zur Ueberpruefung der LLM-Verbindung.

- Laedt optional Werte aus `.env`.
- Startet einen minimalen Agenten und laesst ihn eine kurze Nachricht beantworten.
- Persistiert das Ergebnis als Textdatei in `data/staging/connection_check.txt`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI


ENV_PATH = Path(".env")
OUTPUT_PATH = Path("data/staging/connection_check.txt")


def load_env_file(path: Path) -> None:
    """Simple `.env` parser to populate os.environ without extra dependencies."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def ensure_required_env(vars_: Iterable[str]) -> None:
    missing = [var for var in vars_ if not os.environ.get(var)]
    if missing:
        missing_str = ", ".join(missing)
        raise RuntimeError(
            f"Fehlende Umgebungsvariablen: {missing_str}. "
            "Bitte Werte in `.env` oder der Shell setzen."
        )


def run_diagnostics() -> str:
    model_name = os.environ["OPENAI_MODEL"]
    base_url = os.environ["OPENAI_BASE_URL"]
    api_key = os.environ.get("OPENAI_API_KEY", "")

    os.environ.setdefault("OPENAI_DEFAULT_MODEL", model_name)

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
    )
    model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
    agent = Agent(
        name="ConnectivityCheck",
        instructions=(
            "Du bist ein Diagnostik-Agent. Antworte sehr kurz und bestaetige, "
            "dass die Verbindung funktioniert."
        ),
        model=model,
    )
    result = Runner.run_sync(
        agent,
        "Sage in einem Satz, dass die Verbindung zum Modell funktioniert.",
    )
    return result.final_output


def store_result(content: str) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUTPUT_PATH.write_text(
        f"[{timestamp}] {content}\n",
        encoding="utf-8",
    )


def main() -> None:
    load_env_file(ENV_PATH)
    ensure_required_env(["OPENAI_BASE_URL", "OPENAI_MODEL"])

    print("Starte Verbindungsdiagnose...")
    print("Erforderliche Umgebungsvariablen gefunden.")

    response = run_diagnostics()
    print("Antwort des Agenten:", response)
    store_result(response)
    print(f"Ergebnis gespeichert unter: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
