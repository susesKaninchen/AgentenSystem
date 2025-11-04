# Agentenleitfaden

## Vision
- Aufbau eines mehrstufigen Recherche- und Kontakt-Agentensystems, das automatische Informationsgewinnung, Bewertung und personalisierte Anschreiben ermöglicht (z. B. für Maker Faire Lübeck).
- Nutzung des [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) als Grundlage für Agenten, Handoffs, Guardrails und Sessions.
- Fokus auf reproduzierbaren Deployments in unserer Cloud-Umgebung mit klarer Dokumentation und nachvollziehbaren Entscheidungen.

## Betriebsregeln
- `git status` beim Start jedes Arbeitssprints prüfen; bei offenen Änderungen kurz zusammenfassen und Freigabe einholen.
- Geheimnisse (API-Keys, Basis-URLs, Tokens) ausschließlich in `.env`/`.env.local` etc. ablegen, die bereits durch `.gitignore` geschützt sind.
- Änderungen stets in `Agents.md` und `README.md` dokumentieren, damit Wissensstand und Bedienung konsistent bleiben.
- Erweiterungen nur zusammenführen, wenn Tests/Checks (sofern vorhanden) grün sind und die wichtigsten Sicherheitsregeln eingehalten werden.
- Agenten erhalten Zugriff auf eine zentrale Identitätsdatei (z. B. `config/identity.yaml`) mit Informationen über uns, die für Personalisierung und Bewertung benötigt werden.

## Laufzeitkonfiguration
- Sensible Zugangsdaten ausschließlich in `.env` oder `.env.local` pflegen (liegen unter Versionskontrolle im Ignore).
- Erwartete Variablen (mit geheimen Werten, die hier nicht dokumentiert werden):
  - `OPENAI_API_KEY`
  - `OPENAI_BASE_URL`
  - `OPENAI_MODEL`
- Bei lokalen Tests `dotenv` laden oder Environment im Deployment konfigurieren.
- Beispielkonfiguration liegt in `.env.example` (nur Platzhalter, keine echten Werte).

## Wissensquellen & Referenzen
- OpenAI Agents SDK Dokumentation: Kernkonzepte wie `Agent`, `Runner`, `handoffs`, `guardrails`, `sessions`.
- Beispiele aus `examples/` des SDK (Hello World, Handoffs, Tools, Sessions) als Startpunkt für unsere Implementierung.
- Eigene Sammlungen:
  - `docs/use-cases/` (geplante Vertiefungen, z. B. Maker Faire Recherche).
  - Datenspeicher für gecrawlte Ergebnisse (z. B. `data/staging/`) und persistierte Anschreiben (`outputs/letters/`).
- NorthData (https://www.northdata.de) für Firmenhintergründe via Suggest-API, Ergebnisse werden automatisch unter `data/staging/enrichment/` abgelegt.
- DuckDuckGo-Suche (`tools/duckduckgo.py`) als primäre Web-Recherchequelle; Query- und Ergebnis-Logs liegen in `data/staging/search/`.
- OpenAI-Beispiel *research_bot* demonstriert bewährte Muster: Planner mit Pydantic-Ausgabe, asynchroner Such-Manager (`Runner.run` in Tasks) und Tool-basierte Websuche (`WebSearchTool`). Diese Konzepte übernehmen wir für planbare, streaming-fähige Feedback-Loops.
- Pipeline nutzt inzwischen `Runner.run` asynchron für Planner-, Research- und Writer-Agenten; weitere Schritte (Tool-Aufrufe, echte Suche) folgen nach API-/Proxy-Verfügbarkeit.
- Externe Recherchequellen: öffentliche Webseiten, Verzeichnisse zu Maker-/FabLab-Ausstellern, Social Media Profile. Agenten sollten Quellen jeweils protokollieren (URL + Datum).

## Geplante Agentenrollen
- **Planner/Triage-Agent**: Nimmt Nutzeranfragen entgegen, bricht sie in Recherche-Tasks herunter und erstellt Suchaufträge.
- **Recherche-Agenten** / **Search-Orchestrator**: Führen gezielte DuckDuckGo-Recherchen durch, persistieren Treffer und reichen sie an Evaluator:innen weiter.
- **Evaluator-Agenten**: Prüfen Treffer auf Relevanz, verwerfen ungeeignete Ergebnisse und priorisieren vielversprechende Kandidaten.
- **Query-Refiner**: Analysiert Evaluator-Feedback und generiert neue Suchqueries, bis Zielanzahl erreicht oder Suchraum ausgeschöpft ist.
- **Crawler/Extractor**: Extrahieren Kerninformationen aus Webseiten/Dokumenten (Kontakt, Projekte, USP, Anforderungen).
- **Personalisierungs-Agent**: Kombiniert extrahierte Daten mit Identitätsdatei, erstellt strukturierte Profile.
- **Writer-Agent**: Generiert personalisierte Anschreiben und legt sie als Dateien ab (inkl. Quellenverweis).
- **QA/Guardrail-Agent**: Prüft Anschreiben auf Tonalität, Faktenkonsistenz und Datenschutzvorgaben, bevor sie freigegeben werden.

## Risiken & Stolperfallen
- Fehlende Guardrails können zu falschen oder unsicheren Anschreiben führen → Validierung verpflichtend.
- Unvollständige oder veraltete Identitätsinformationen schwächen die Personalisierung → Identity-Datei regelmäßig prüfen.
- Ungespeicherte Recherchequellen erschweren Nachvollziehbarkeit → Jede Quelle protokollieren.
- Rate Limits/Quota der verwendeten LLMs beachten → Backoff/Retries planen und Monitoring einführen.
- Datenschutz: Kontakte dürfen nur mit Einwilligung gespeichert/verarbeitet werden → Aufbewahrungsregeln definieren.

## Datenhaltung
- Primärer Speicher sind Textdateien/Markdown/JSON in `data/` und `outputs/`; keine Datenbanken im Standardbetrieb.
- Strukturierte Ergebnisse pro Auftrag als eigenständige Datei ablegen (`data/staging/` für Rohdaten, `outputs/letters/` für Anschreiben).
- Externe Anreicherungen (z. B. NorthData) separat versionierbar speichern (`data/staging/enrichment/`).
- Suchtreffer und Evaluations-Snapshots versionierbar halten (`data/staging/search/`, `data/staging/candidates_selected.json`), um Feedback-Loops nachvollziehen zu können.
- Metadaten (z. B. Bewertung, Quelle, Zeitstempel) in YAML/JSON neben den Texten speichern, damit Versionierung per Git möglich bleibt.

## Identität & Kontext
- `config/identity.yaml` enthält das aktuelle Profil von Marco Gabrecht und der Maker Faire Lübeck 2026.
- `tools/identity_loader.py` stellt Hilfsfunktionen bereit (`load_identity`, `get_identity_summary`).
- Workflows sollen diese Funktionen nutzen, um Anschreiben und Bewertungen zu personalisieren.

## Umsetzungsschritte / TODOs
- [x] Projektstruktur festlegen (`agents/`, `tools/`, `workflows/`, `data/`, `config/`).
- [x] `config/identity.yaml` (oder JSON) definieren: Wer sind wir? Ziele? Schlüsselargumente? Kontaktinfos.
- [x] Basiskonfiguration für OpenAI Agents SDK erstellen (`pyproject.toml`, virtuelle Umgebung via uv vorbereiten).
- [x] Proof-of-Concept: Einfache Pipeline (Diagnostics-Agent über `workflows/poc.py`) implementieren und Verbindung testen.
- [ ] Datenpersistenz entwerfen (Dateisystem, SQLite, ggf. Redis Sessions) und Logging-Format festlegen.
- [ ] Guardrails konfigurieren (Output-Filter, maximale Anschreibenlänge, sensible Wörter).
- [ ] Automatisierte Tests/Smoke-Checks für zentrale Agenten (z. B. Parser, Prompt-Vorlagen).
- [ ] Deployment-Strategie für Cloud-Umgebung dokumentieren (Container, Secrets-Management, Monitoring).
- [x] Recherche-Workflow erweitern (Planner → Recherche → Writer) mit Datei-Ausgaben anlegen (`workflows/research_pipeline.py`).
- [x] Datenquellen-Anbindung (DuckDuckGo-Suche + NorthData-Enrichment) automatisieren, inklusive Feedback-Schleifen und Persistenz.
- [x] Offizielles Tooling aus dem OpenAI *research_bot* nachbilden (Pydantic-Planner, Tool-Aufrufe per `Runner.run` in async-Tasks, Trace-Integration).
- [ ] QA-Agent integrieren, der Anschreiben vor Versand validiert.

## Offene Fragen
- Welche konkreten Datenquellen stehen für die Recherche langfristig zur Verfügung (APIs, interne Datenbanken)?
- Benötigen wir einen Scheduler/Queue, um mehrere Suchaufträge parallel zu bearbeiten?
- Wie werden Nutzerfeedback und Korrekturen zurück in den Agenten-Workflow gespeist?
- Welche Compliance-/Datenschutzanforderungen gelten für gespeicherte Kontakte?

> Halte diese Datei aktuell. Ergänze neue Regeln, Agentenrollen, Datenquellen oder TODOs unmittelbar nach Entscheidungen oder Änderungen.
