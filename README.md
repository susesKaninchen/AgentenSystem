# AgentenSystem

Mehrstufiges Agenten-Framework, inspiriert vom [OpenAI Agents SDK](https://github.com/openai/openai-agents-python), um trotz kleinerer/OSS-LLMs zuverlässige Recherche- und Outreach-Aufgaben zu automatisieren.

## Überblick
- Ziel: Automatisierte Recherche, Bewertung und Erstellung personalisierter Anschreiben (z. B. Maker Faire Lübeck).
- Architektur: Planner → Recherche-Agenten → Evaluator/Crawler → Writer → QA-Guardrails.
- Wissensbasis & Regeln sind in `Agents.md` dokumentiert und werden nach jeder Änderung aktualisiert.

## Schnellstart
1. Installiere `uv` (z. B. `pip install uv`) oder stelle sicher, dass es auf dem System verfügbar ist.
2. `uv venv` ausführen, um eine `.venv` anzulegen, und die Umgebung aktivieren (`uv pip show` nutzt automatisch dieselbe venv).
3. Abhaengigkeiten synchronisieren: `uv sync` liest `pyproject.toml` und installiert u. a. `openai-agents`.
4. Lege eine `.env`/`.env.local` an (siehe `.env.example`) und trage dort deine geheimen Verbindungsdaten (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`) ein – niemals direkt committen.
5. Projektstruktur ist bereits vorbereitet (`agents/`, `tools/`, `workflows/`, `config/`, `data/`, `outputs/`); einfach weiter befuellen.
6. `config/identity.yaml` enthaelt eine Vorlage mit FabLab-Luebeck-Daten – vor dem Versand aktualisieren.
7. Verbindung testen: `uv run python workflows/poc.py` ausfuehren. Ergebnis landet in `data/staging/connection_check.txt`.
8. Identitaetsdatei `config/identity.yaml` als Kontext in Agenten einlesen, um Personalierungen zu ermoeglichen.

## Workflows
- `workflows/poc.py`: Netzwerkdiagnose, schreibt Ergebnis nach `data/staging/connection_check.txt`.
- `workflows/research_pipeline.py`: Simulierter Planner → Research → Writer Lauf. Erwartet JSON-Dateien in `data/raw/` (z. B. `robotics_club.json`) und erzeugt:
  - `data/staging/research_notes.md` (Plan + Bewertung)
  - `outputs/letters/<slug>.md` (Anschreiben, nutzt `config/identity.yaml`)
  - Ausfuehrung mit vorhandener `.env`: `uv run --env-file .env python workflows/research_pipeline.py`

## Arbeitsweise & Qualitätssicherung
- Zu Beginn jeder Session `git status` prüfen und offene Änderungen mit dem Team abstimmen.
- Ergebnisse und neue Entscheidungen sofort in `Agents.md` und hier dokumentieren.
- Guardrails & Tests einplanen, bevor wir Anschreiben verschicken oder Ergebnisse veröffentlichen.
- Quellen, Kontaktinformationen und generierte Schreiben unter `data/` bzw. `outputs/` nachvollziehbar ablegen.

## Weitere Schritte
- Detailplanung, Rollenbeschreibung und offene Fragen siehe `Agents.md` (Abschnitte TODOs & Risiken).
- Entscheidung über Persistenz/Storage (Dateien vs. SQLite/Redis) und Deployment-Strategie für die Cloud treffen.
- Recherche-Pipeline aufsetzen und reale Datenquellen anbinden, anschließend Writer/QA-Agenten integrieren.
