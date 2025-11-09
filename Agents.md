# Agentenleitfaden

## Vision
- Aufbau eines mehrstufigen Recherche- und Kontakt-Agentensystems, das automatische Informationsgewinnung, Bewertung und personalisierte Anschreiben ermöglicht (z. B. für Maker Faire Lübeck).
- Nutzung des [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) als Grundlage für Agenten, Handoffs, Guardrails und Sessions.
- Fokus auf reproduzierbaren lokalen Deployments (Cloud-Anbindung optional) mit klarer Dokumentation und nachvollziehbaren Entscheidungen.
- Zielgruppe: nicht-kommerzielle Maker:innen, Hackspaces, offene Werkstätten und Kultur-/Bildungskollektive (Chaotikum, Fuchsbau, freie Labore) aus Norddeutschland.

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
  - `GOOGLE_API_KEY` (optional – ohne fällt die Suche auf DuckDuckGo zurück)
  - `GOOGLE_SEARCH_ENGINE_ID` (optional – benötigt für Google Custom Search)
- Feintuning-Variablen:
  - `PIPELINE_MAX_ITERATIONS` (Standard 10) – bestimmt, wie viele Suchrunden gefahren werden.
  - `PIPELINE_RESULTS_PER_QUERY` (Standard 10) – Anzahl der Webresultate pro Query.
  - `MAX_LETTERS_PER_RUN` (Standard 3) – wie viele Anschreiben pro Lauf generiert werden.
- Laufzeit-Presets können auch über CLI gewählt werden: `python workflows/research_pipeline.py --phase <explore|refine|acquire> --region <nord|hamburg|luebeck|...>`. Ohne Flags greifen die oben genannten Env-Variablen.
- Bei lokalen Tests `dotenv` laden oder Environment im Deployment konfigurieren.
- Beispielkonfiguration liegt in `.env.example` (nur Platzhalter, keine echten Werte).
- `ENABLE_WEB_SEARCH_TOOL=0/1` steuert, ob das OpenAI-WebSearchTool genutzt wird. Bei `0` laufen wir direkt über DuckDuckGo/Seeds.

## Wissensquellen & Referenzen
- OpenAI Agents SDK Dokumentation: Kernkonzepte wie `Agent`, `Runner`, `handoffs`, `guardrails`, `sessions`.
- Beispiele aus `examples/` des SDK (Hello World, Handoffs, Tools, Sessions) als Startpunkt für unsere Implementierung.
- Eigene Sammlungen:
  - `docs/use-cases/` (geplante Vertiefungen, z. B. Maker Faire Recherche).
  - Datenspeicher für gecrawlte Ergebnisse (z. B. `data/staging/`) und persistierte Anschreiben (`outputs/letters/`).
- NorthData (https://www.northdata.de) für Firmenhintergründe via Suggest-API, Ergebnisse werden automatisch unter `data/staging/enrichment/` abgelegt.
- WebSearchTool (OpenAI Responses API) ist der Standard für Recherche; wenn das nicht verfügbar ist, fällt das System auf Google/DuckDuckGo (`tools/google_search.py` / `tools/duckduckgo.py`) und eine Seed-Liste (Chaotikum, Fuchsbau, Freies Labor, Hackerspace Bremen, Chaostreff Flensburg) zurück. Query- und Ergebnis-Logs liegen in `data/staging/search/`.
- Sammelseiten/Verzeichnisse (z. B. „Werkstätten-Übersichten“, „Meet the Makers“) werden automatisch mit `tools/directory_parser.py` ausgewertet; die daraus extrahierten Untereinträge landen als neue Kandidat:innen in der Pipeline und werden in `data/staging/directory_expansions/` gecacht.
- PDF-/Listenquellen (z. B. bundestag.de, scribd.com) sowie andere „schwergewichtige“ Domains werden vor der Evaluierung herausgefiltert, um irrelevante Treffer zu vermeiden.
- Bei wiederholten Rate-Limits zieht der Workflow lokale Seed-Kandidaten (Chaotikum, Fuchsbau, Freies Labor, Hackerspace Bremen, Chaostreff Flensburg) heran, damit Evaluator:innen und Writer weiterarbeiten können.
- OpenAI-Beispiel *research_bot* demonstriert bewährte Muster: Planner mit Pydantic-Ausgabe, asynchroner Such-Manager (`Runner.run` in Tasks) und Tool-basierte Websuche (`WebSearchTool`). Diese Konzepte übernehmen wir für planbare, streaming-fähige Feedback-Loops. Ergänzend nutzt der Writer strukturierte Snapshots aus `tools/site_scraper.py`, damit Anschreiben konkreten Kontext enthalten.
- Pipeline nutzt inzwischen `Runner.run` asynchron für Planner-, Research- und Writer-Agenten; weitere Schritte (Tool-Aufrufe, echte Suche) folgen nach API-/Proxy-Verfügbarkeit. Writer personalisiert Anschreiben anhand strukturierter Snapshots aus `tools/site_scraper.py`, die automatisch aus den Kandidaten-Webseiten extrahiert werden.
- Externe Recherchequellen: öffentliche Webseiten, Verzeichnisse zu Maker-/FabLab-Ausstellern, Social Media Profile. Agenten sollten Quellen jeweils protokollieren (URL + Datum).

## Geplante Agentenrollen
- **Planner/Triage-Agent**: Nimmt Nutzeranfragen entgegen, bricht sie in Recherche-Tasks herunter und erstellt Suchaufträge.
- **Recherche-Agenten** / **Search-Orchestrator**: Fokussieren Suchqueries auf non-kommerzielle Kollektive (Chaotikum, Fuchsbau, offene Werkstätten) und führen Google- bzw. DuckDuckGo-Recherchen durch, persistieren Treffer und reichen sie an Evaluator:innen weiter.
- **Evaluator-Agenten**: Prüfen Treffer auf Relevanz, verwerfen ungeeignete Ergebnisse und priorisieren vielversprechende Kandidaten.
- **Query-Refiner**: Analysiert Evaluator-Feedback und generiert neue Suchqueries, bis Zielanzahl erreicht oder Suchraum ausgeschöpft ist.
- **Crawler/Extractor**: Extrahieren Kerninformationen aus Webseiten/Dokumenten (Kontakt, Projekte, USP, Anforderungen).
- **Personalisierungs-Agent**: Kombiniert extrahierte Daten mit Identitätsdatei, erstellt strukturierte Profile.
- **Writer-Agent**: Generiert personalisierte Anschreiben, betont kostenlosen Stand für gemeinnützige/nicht-kommerzielle Teams und legt die Dateien (inkl. Quellenverweis) ab.
- **QA/Guardrail-Agent** (aktiv in `workflows/research_pipeline.py`): Erzwingt DIN-A4-konforme Länge, keine Versprechen/Garantien und protokolliert Freigaben.

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
- Ergebnisse der Directory-Parser-Läufe (`data/staging/directory_expansions/`) speichern, damit nachvollziehbar bleibt, welche Sammelseiten wir in konkrete Kontakte aufgelöst haben.
- Automatische Webseiten-Snapshots für personalisierte Anschreiben liegen in `data/staging/snapshots/` (generiert durch `tools/site_scraper.py`).
- Geo-Heuristiken (Nominatim-Stub) filtern Off-Region-Treffer direkt in der Pipeline; zukünftige echte Geocode-APIs können dieselbe Schnittstelle weiterverwenden.
- Metadaten (z. B. Bewertung, Quelle, Zeitstempel) in YAML/JSON neben den Texten speichern, damit Versionierung per Git möglich bleibt.
- Details zu Ablageformaten, QA-Metadaten und Logging stehen in `docs/data_persistence.md`. Pipelinelogs landen in `logs/pipeline.log` (JSON pro Event).

## Identität & Kontext
- `config/identity.yaml` enthält das aktuelle Profil von Marco Gabrecht und der Maker Faire Lübeck 2026.
- `tools/identity_loader.py` stellt Hilfsfunktionen bereit (`load_identity`, `get_identity_summary`).
- Workflows sollen diese Funktionen nutzen, um Anschreiben und Bewertungen zu personalisieren.

## Umsetzungsschritte / TODOs
- [x] Projektstruktur festlegen (`agents/`, `tools/`, `workflows/`, `data/`, `config/`).
- [x] `config/identity.yaml` (oder JSON) definieren: Wer sind wir? Ziele? Schlüsselargumente? Kontaktinfos.
- [x] Basiskonfiguration für OpenAI Agents SDK erstellen (`pyproject.toml`, virtuelle Umgebung via uv vorbereiten).
- [x] Proof-of-Concept: Einfache Pipeline (Diagnostics-Agent über `workflows/poc.py`) implementieren und Verbindung testen.
- [x] Datenpersistenz (Dateisystem) und Logging-Format definieren (`docs/data_persistence.md`, `logs/pipeline.log`).
- [x] Guardrails konfigurieren (DIN-A4-Limit, keine Versprechen, QA-Freigabe).
- [ ] Automatisierte Tests/Smoke-Checks für zentrale Agenten (zurzeit zurückgestellt, sobald stabiler Workflow benötigt wird).
- [x] Recherche-Workflow erweitern (Planner → Recherche → Writer) mit Datei-Ausgaben anlegen (`workflows/research_pipeline.py`).
- [x] Datenquellen-Anbindung (Google Custom Search + NorthData-Enrichment) automatisieren, inklusive Feedback-Schleifen und Persistenz.
- [x] Offizielles Tooling aus dem OpenAI *research_bot* nachbilden (Pydantic-Planner, Tool-Aufrufe per `Runner.run` in async-Tasks, Trace-Integration).
- [x] QA-Agent integrieren, der Anschreiben vor Versand validiert.
- [x] Directory-Parser integrieren, damit Sammelseiten automatisiert neue Kandidat:innen liefern.
- [ ] Quellen- und Domainfilter verschärfen (reine Verzeichnisse/Event-Seiten früh blocken).
- [ ] Nonprofit/Maker-Scoring einführen (Region, Quelle, DIY/Nonprofit-Merkmale bevor `accepted=True`).
- [ ] Deduplizierung & Kanonisierung von Organisationen (Subseiten als Tags, eine Kontaktkartei).
- [ ] Kontakt-Extraktion (Impressum/Schema.org) + CSV/Markdown-Export für Outreach.
- [ ] Manuelle Checkliste/Sicherheitsnetz vor Versand der Briefe (Region, Nonprofit, Kontakt plausibel).

## Offene Fragen
- Welche konkreten Datenquellen stehen für die Recherche langfristig zur Verfügung (APIs, interne Datenbanken)?
- Benötigen wir einen Scheduler/Queue, um mehrere Suchaufträge parallel zu bearbeiten?
- Wie werden Nutzerfeedback und Korrekturen zurück in den Agenten-Workflow gespeist?
- Welche Compliance-/Datenschutzanforderungen gelten für gespeicherte Kontakte?

> Halte diese Datei aktuell. Ergänze neue Regeln, Agentenrollen, Datenquellen oder TODOs unmittelbar nach Entscheidungen oder Änderungen.
