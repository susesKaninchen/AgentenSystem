# AgentenSystem

Mehrstufiges Agenten-Framework, inspiriert vom [OpenAI Agents SDK](https://github.com/openai/openai-agents-python), um trotz kleinerer/OSS-LLMs zuverlässige Recherche- und Outreach-Aufgaben zu automatisieren.

## Überblick
- Ziel: Automatisierte Suche, Bewertung und Erstellung personalisierter Anschreiben (z. B. Maker Faire Lübeck).
- Fokus: Non-kommerzielle Maker:innen, Hackspaces, offene Werkstätten und Kultur-/Bildungskollektive (Chaotikum, Fuchsbau, freie Labore).
- Architektur: Planner → WebSearchTool (OpenAI Responses API, optionaler DuckDuckGo/Seed-Fallback) → Evaluator (mit Feedback-Loop) → NorthData-Enrichment → Writer → QA-Guardrails.
- Wissensbasis & Regeln sind in `Agents.md` dokumentiert und werden nach jeder Änderung aktualisiert.
- Persistenz & Logging (Dateistruktur, Metadaten, `logs/pipeline.log`) sind in `docs/data_persistence.md` beschrieben.
- Referenz: OpenAI-Beispiel [`examples/research_bot`](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot) dient als Blaupause für Pydantic-basierte Planner, asynchrone `Runner.run`-Orchestrierung und Tool-Einbindung (`WebSearchTool`).

## Schnellstart
1. Installiere `uv` (z. B. `pip install uv`) oder stelle sicher, dass es auf dem System verfügbar ist.
2. `uv venv` ausführen, um eine `.venv` anzulegen, und die Umgebung aktivieren (`uv pip show` nutzt automatisch dieselbe venv).
3. Abhängigkeiten synchronisieren: `uv sync` liest `pyproject.toml` und installiert die benötigten Pakete (u. a. `openai-agents`, `lxml`, `duckduckgo-search`).
4. Lege eine `.env`/`.env.local` an (siehe `.env.example`) und trage dort deine geheimen Verbindungsdaten (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`). Optional: `GOOGLE_API_KEY` + `GOOGLE_SEARCH_ENGINE_ID`, sonst übernimmt DuckDuckGo die Websuche. Mit `ENABLE_WEB_SEARCH_TOOL=0` kannst du das OpenAI-WebSearchTool deaktivieren, falls dein Gateway die Responses-API nicht proxyt. Niemals direkt committen.
5. Projektstruktur ist bereits vorbereitet (`agents/`, `tools/`, `workflows/`, `config/`, `data/`, `outputs/`); einfach weiter befuellen.
6. `config/identity.yaml` enthaelt eine Vorlage mit FabLab-Luebeck-Daten – vor dem Versand aktualisieren.
7. Verbindung testen: `uv run python workflows/poc.py` ausfuehren. Ergebnis landet in `data/staging/connection_check.txt`.
8. Automatisierten Recherchelauf starten: `uv run --env-file .env python workflows/research_pipeline.py`. Über `--phase <explore|refine|acquire>` und `--region <macro>` (z. B. `--region luebeck-local` für Lübeck + Ostholstein + Kiel) kannst du Laufprofile und regionale Schwerpunkte per CLI setzen; alternativ greifen die entsprechenden Env-Variablen (`PIPELINE_PHASE`, `PIPELINE_REGION`). Zusätzliche Finetuning-Flags: `PIPELINE_MAX_ITERATIONS`, `PIPELINE_RESULTS_PER_QUERY`, `MAX_LETTERS_PER_RUN`.
9. Identitaetsdatei `config/identity.yaml` als Kontext in Agenten einlesen, um Personalierungen zu ermoeglichen.

## Workflows
- `workflows/poc.py`: Netzwerkdiagnose, schreibt Ergebnis nach `data/staging/connection_check.txt`.
- `workflows/research_pipeline.py`: Automatisierter Planner → WebSearchTool (Fallback DuckDuckGo + Seed-Kandidaten) → Evaluator (mit Phase/Region-Presets und Negativfiltern) → Directory-Parser → NorthData → Site-Scraper → Writer → QA Lauf. Sammelseiten werden automatisch expandiert, ungeeignete Quellen (PDFs, Regierungslisten) sowie Off-Region-Treffer werden vorab gefiltert, und der Site-Scraper legt Kontext-Snapshots der akzeptierten Kandidat*innen ab. Pro Lauf entstehen mehrere Anschreiben (Standard: 3, per CLI `--letters-per-run` anpassbar). Erzeugt:
  - `data/staging/search/*.json` (Rohtreffer pro Query)
  - `data/staging/candidates_selected.json` (Snapshot akzeptierter + abgelehnter Kandidaten)
  - `data/staging/research_notes.md` (Plan, Bewertungen, Zusammenfassungen)
  - `data/staging/directory_expansions/*.json` (Cache der aus Sammelseiten extrahierten Untereinträge)
  - `data/staging/snapshots/*.json` (automatische Web-Snapshots für personalisierte Anschreiben)
  - `data/staging/enrichment/northdata_<slug>.json` (NorthData-Suggest-Ergebnisse)
  - `outputs/letters/<slug>.md` (Anschreiben + QA-Metadaten, nutzt `config/identity.yaml`, max. DIN-A4, erwähnt kostenlosen Stand für gemeinnützige/nicht-kommerzielle Teams, keine Versprechen)
  - `logs/pipeline.log` (JSON-Events: planner/search/QA/Laufstatus)
  - CLI/Phase-Profile: `--phase explore` (weite Suche mit niedrigem Score-Limit), `--phase refine`, `--phase acquire` (fokussierter, mehr Anschreiben); `--region <macro>` fügt automatisch passende Query-Sets hinzu (z. B. Hamburg, Lübeck, Kiel). Eine Geo-Heuristik (Nominatim-ready Stub) filtert Off-Region-Treffer, und die Pipeline loggt Warnungen, wenn das Zielvolumen nicht erreicht wird.
  - Hinweis: Ohne `GOOGLE_*`-Variablen greift automatisch DuckDuckGo (gedrosselt). Bei Rate-Limits Google-API aktivieren oder Query-Anzahl reduzieren. `ENABLE_WEB_SEARCH_TOOL=0` erzwingt den Fallback, wenn dein Modell/Proxy keine Responses-Tools unterstützt.
  - Fallback: Wenn alle Suchanfragen leer bleiben, nutzt die Pipeline lokale Seed-Kandidaten (Chaotikum, Fuchsbau, Freies Labor etc.), damit Evaluator/Writer weiterlaufen können.
  - Der Evaluator liefert Feedback an den Query-Refiner; es werden so lange neue Queries erzeugt, bis das Ziel (Standard 50 Kandidaten) erreicht ist oder kein Fortschritt mehr möglich ist.
  - Planner-, Research- und Writer-Agenten laufen bereits analog zum OpenAI-*research_bot* via `Runner.run` (async); Query-Refiner/Evaluator schließen den Feedback-Loop, sobald stabile Websuche/Proxy verfügbar ist.

## Arbeitsweise & Qualitätssicherung
- Zu Beginn jeder Session `git status` prüfen und offene Änderungen mit dem Team abstimmen.
- Ergebnisse und neue Entscheidungen sofort in `Agents.md` und hier dokumentieren.
- Guardrails sind aktiv (DIN-A4-Limit, keine Versprechen, QA-Agent). Automatisierte Tests werden nachgereicht, sobald weitere Kernmodule stabil sind.
- Quellen, Kontaktinformationen und generierte Schreiben unter `data/` bzw. `outputs/` nachvollziehbar ablegen.

## Weitere Schritte
- Detailplanung, Rollenbeschreibung und offene Fragen siehe `Agents.md` (Abschnitte TODOs & Risiken).
- Dateibasierte Persistenz & Logging sind definiert (`docs/data_persistence.md`); Anpassungen erfolgen bei Bedarf.
- Automatisierte Tests/Smoke-Checks bleiben als nächster Ausbauschritt auf der Agenda, sobald neue Module entstehen.
- CLI-Profile: `python workflows/research_pipeline.py --phase explore` (breiter Suchkorridor, wenige Anschreiben), `--phase refine` (Balance) oder `--phase acquire` (fokussiert, mehrere Briefe). Mit `--region hamburg` (oder `luebeck`, `kiel`, `hannover`, `nord`) erweiterst du die Query-Liste automatisch um regionale Makro-Sets. Abweichungen kannst du zusätzlich per Env-Variablen oder Flags (`--max-iterations`, `--results-per-query`, `--letters-per-run`) festlegen.
- Offene Verbesserungen (Work-in-Progress): strengere Quellenfilter (keine Verzeichnisse/Eventseiten), Nonprofit/Maker-Scoring vor Acceptance, Deduplizierung von Subseiten (z. B. TextilLab→FabLab Lübeck), robuste Kontakt-Extraktion + CSV-Export für Outreach, manuell prüfbare Checkliste vor Versand.
