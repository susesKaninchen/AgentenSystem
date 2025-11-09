# Datenpersistenz- und Logging-Konzept

## Ziele
- Reproduzierbarer Pipeline-Lauf, dessen Zwischenergebnisse versionierbar sind.
- Klare Trennung zwischen Rohdaten (`data/`), endgültigen Artefakten (`outputs/`) und Protokollen (`logs/`).
- Einfaches Debugging durch nachvollziehbares Logging im Dateisystem.

## Verzeichnisstruktur
- `data/raw/` – unveränderte Quellen (z. B. crawlbare HTML/Text-Snippets).
- `data/staging/`
  - `search/`: JSON-Dateien pro Query (`<timestamp>_<slug>.json`) mit Rohtreffern.
  - `enrichment/`: NorthData-Suggest-Ergebnisse (`northdata_<slug>.json`).
  - `candidates_selected.json`: Snapshot mit akzeptierten/abgelehnten Kandidaten.
  - `research_notes.md`: Markdown-Zusammenfassung (Plan, Bewertungen, Quellen).
  - `connection_check.txt`: Health-Check-Ergebnisse (`workflows/poc.py`).
- `outputs/letters/`: Finale Anschreiben (`<slug>.md`) inklusive Quellenhinweisen.
- `logs/`
  - `pipeline.log`: Chronologisches Log pro Lauf mit strukturierter JSON-Linie.
  - Weitere Logs (Tool-spezifisch) optional, Schema identisch.

## Dateiformate
- **Suchergebnisse (`data/staging/search/*.json`)**
  ```json
  {
    "query": "Maker Faire Lübeck Aussteller",
    "fetched_at": "20250105T143211Z",
    "results": [
      {
        "title": "FabLab Lübeck",
        "url": "https://fablab-luebeck.de",
        "snippet": "Offene Werkstatt ...",
        "source": "google"
      }
    ]
  }
  ```
  - `fetched_at` nutzt ein Dateinamen-taugliches UTC-Format (`YYYYMMDDTHHMMSSZ`), damit Windows/Linux identische Dateien schreiben können.
  - Das Feld `source` kennzeichnet, ob Google Custom Search oder DuckDuckGo verwendet wurde.
- **Kandidaten-Snapshot (`data/staging/candidates_selected.json`)**
  ```json
  {
    "generated_at": "2025-01-05T14:35:00Z",
    "accepted": [
      {
        "name": "FabLab Lübeck",
        "url": "https://fablab-luebeck.de",
        "score": 0.82,
        "reason": "Bringt Robotik-Projekte zur Maker Faire mit.",
        "sources": ["search/2025-01-05T143211_fablab.json"],
        "northdata": "northdata_fablab-luebeck.json"
      }
    ],
    "rejected": [
      {
        "name": "Beliebige GmbH",
        "url": "https://example.org",
        "score": 0.21,
        "reason": "Kein Maker-/MINT-Bezug."
      }
    ]
  }
  ```
- **Anschreiben (`outputs/letters/*.md`)**
  - Markdown mit Metadaten-Header:
    ```markdown
    ---
    generated_at: 2025-01-05T14:36:40Z
    candidate: FabLab Lübeck
    source_url: https://fablab-luebeck.de
    words: 198
    ---
    ```
  - Anschreiben-Text (max. 1 DIN-A4-Seite, keine Versprechungen, Quellenhinweis).

- **Logs (`logs/pipeline.log`)**
  - Eine JSON-Zeile pro Event:
    ```json
    {"timestamp":"2025-01-05T14:31:59Z","event":"planner.start","queries":8}
    ```
  - Pflichtfelder: `timestamp`, `event`. Optionale Felder für Kontext (z. B. `query`, `candidate`, `decision`).

## Logging-Richtlinien
- Jeder Pipeline-Lauf beginnt mit `pipeline.start` (inkl. Taskbeschreibung) und endet mit `pipeline.done` / `pipeline.failed`.
- Kritische Schritte loggen: Planner-Output, Query-Refinement, Evaluator-Entscheidungen, Writer/QA-Ergebnisse.
- Für Debugging können Tool-spezifische Logs (z. B. Google-API-Requests) unter `logs/tools/<tool>.log` abgelegt werden.
- Log-Dateien werden nicht überschrieben, sondern per Append erweitert; Rotation/Archivierung erfolgt manuell via Git.

## Offene Punkte
- Automatisierte Rotation (z. B. täglich) optional prüfen.
- Langfristig könnte SQLite für aggregierte Auswertungen ergänzt werden; aktueller Fokus bleibt Dateisystem.
