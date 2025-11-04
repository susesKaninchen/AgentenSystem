# AgentenSystem

Mehrstufiges Agenten-Framework, inspiriert vom [OpenAI Agents SDK](https://github.com/openai/openai-agents-python), um trotz kleinerer/OSS-LLMs zuverlässige Recherche- und Outreach-Aufgaben zu automatisieren.

## Überblick
- Ziel: Automatisierte Suche, Bewertung und Erstellung personalisierter Anschreiben (z. B. Maker Faire Lübeck).
- Architektur: Planner → DuckDuckGo-Suche → Evaluator (mit Feedback-Loop) → NorthData-Enrichment → Writer → QA-Guardrails.
- Wissensbasis & Regeln sind in `Agents.md` dokumentiert und werden nach jeder Änderung aktualisiert.
- Referenz: OpenAI-Beispiel [`examples/research_bot`](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot) dient als Blaupause für Pydantic-basierte Planner, asynchrone `Runner.run`-Orchestrierung und Tool-Einbindung (`WebSearchTool`).

## Schnellstart
1. Installiere `uv` (z. B. `pip install uv`) oder stelle sicher, dass es auf dem System verfügbar ist.
2. `uv venv` ausführen, um eine `.venv` anzulegen, und die Umgebung aktivieren (`uv pip show` nutzt automatisch dieselbe venv).
3. Abhängigkeiten synchronisieren: `uv sync` liest `pyproject.toml` und installiert u. a. `openai-agents` und `duckduckgo-search`.
4. Lege eine `.env`/`.env.local` an (siehe `.env.example`) und trage dort deine geheimen Verbindungsdaten (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`) ein – niemals direkt committen.
5. Projektstruktur ist bereits vorbereitet (`agents/`, `tools/`, `workflows/`, `config/`, `data/`, `outputs/`); einfach weiter befuellen.
6. `config/identity.yaml` enthaelt eine Vorlage mit FabLab-Luebeck-Daten – vor dem Versand aktualisieren.
7. Verbindung testen: `uv run python workflows/poc.py` ausfuehren. Ergebnis landet in `data/staging/connection_check.txt`.
8. Automatisierten Recherchelauf starten: `uv run --env-file .env python workflows/research_pipeline.py`.
9. Identitaetsdatei `config/identity.yaml` als Kontext in Agenten einlesen, um Personalierungen zu ermoeglichen.

## Workflows
- `workflows/poc.py`: Netzwerkdiagnose, schreibt Ergebnis nach `data/staging/connection_check.txt`.
- `workflows/research_pipeline.py`: Automatisierter Planner → DuckDuckGo → Evaluator → NorthData → Writer Lauf. Erzeugt:
  - `data/staging/search/*.json` (Rohtreffer pro Query)
  - `data/staging/candidates_selected.json` (Snapshot akzeptierter + abgelehnter Kandidaten)
  - `data/staging/research_notes.md` (Plan, Bewertungen, Zusammenfassungen)
  - `data/staging/enrichment/northdata_<slug>.json` (NorthData-Suggest-Ergebnisse)
  - `outputs/letters/<slug>.md` (Anschreiben, nutzt `config/identity.yaml`)
  - Der Evaluator liefert Feedback an den Query-Refiner; es werden so lange neue Queries erzeugt, bis das Ziel (Standard 50 Kandidaten) erreicht ist oder kein Fortschritt mehr möglich ist.
  - Für künftige Erweiterungen werden Planner-/Search-Agenten an das Muster des OpenAI-*research_bot* angepasst (async Tasks, Pydantic-Output, Tool-Aufrufe).

## Arbeitsweise & Qualitätssicherung
- Zu Beginn jeder Session `git status` prüfen und offene Änderungen mit dem Team abstimmen.
- Ergebnisse und neue Entscheidungen sofort in `Agents.md` und hier dokumentieren.
- Guardrails & Tests einplanen, bevor wir Anschreiben verschicken oder Ergebnisse veröffentlichen.
- Quellen, Kontaktinformationen und generierte Schreiben unter `data/` bzw. `outputs/` nachvollziehbar ablegen.

## Weitere Schritte
- Detailplanung, Rollenbeschreibung und offene Fragen siehe `Agents.md` (Abschnitte TODOs & Risiken).
- Entscheidung über Persistenz/Storage (Dateien vs. SQLite/Redis) und Deployment-Strategie für die Cloud treffen.
- Recherche-Pipeline aufsetzen und reale Datenquellen anbinden, anschließend Writer/QA-Agenten integrieren.
