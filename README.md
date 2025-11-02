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
7. Erstes Experiment: Minimalen Agenten mit `Runner.run_sync` aus dem SDK starten und lokale Tools anbinden.

## Arbeitsweise & Qualitätssicherung
- Zu Beginn jeder Session `git status` prüfen und offene Änderungen mit dem Team abstimmen.
- Ergebnisse und neue Entscheidungen sofort in `Agents.md` und hier dokumentieren.
- Guardrails & Tests einplanen, bevor wir Anschreiben verschicken oder Ergebnisse veröffentlichen.
- Quellen, Kontaktinformationen und generierte Schreiben unter `data/` bzw. `outputs/` nachvollziehbar ablegen.

## Weitere Schritte
- Detailplanung, Rollenbeschreibung und offene Fragen siehe `Agents.md` (Abschnitte TODOs & Risiken).
- Entscheidung über Persistenz/Storage (Dateien vs. SQLite/Redis) und Deployment-Strategie für die Cloud treffen.
- Recherche-Pipeline aufsetzen und reale Datenquellen anbinden, anschließend Writer/QA-Agenten integrieren.
