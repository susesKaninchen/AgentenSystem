# AgentenSystem

Lokales Recherche- und Outreach-System, inspiriert vom [OpenAI Agents SDK](https://github.com/openai/openai-agents-python): Du beschreibst Auftrag + Identität + Vorlage, das System sucht passende Kontakte, reichert sie an und erzeugt personalisierte Nachrichten-Entwürfe.

## Kernkonzept
- Persistentes Briefing: `config/brief.yaml` (Auftrag, Zielprofil, Filter/Guardrails)
- Vorlage: `config/outreach_template.md` (wird personalisiert)
- Identität: `config/identity.yaml`
- Pipeline-Defaults: `config/pipeline.yaml` (Phase/Region/Iterationen/Treffer/Briefe/Concurrency/Stop-File)

## Schnellstart
1. Installiere `uv` (z. B. `pip install uv`) oder stelle sicher, dass es verfügbar ist.
2. Abhängigkeiten installieren: `uv sync`
3. `.env` anlegen (siehe `.env.example`) und `OPENAI_BASE_URL` + `OPENAI_MODEL` (und optional `OPENAI_API_KEY`) setzen. Optional: `GOOGLE_API_KEY` + `GOOGLE_SEARCH_ENGINE_ID` für Google Custom Search; sonst DuckDuckGo-Fallback.
4. Briefing/Vorlage/Identität anpassen: `config/brief.yaml`, `config/outreach_template.md`, `config/identity.yaml`
5. Start (Chat): `uv run --env-file .env python workflows/chat_entry.py`
   - Moderierter Dialog: fasst Kontext zusammen, stellt Rückfragen, schlägt Änderungen vor und schreibt sie erst nach `ja/nein` in die Config. `exit` beendet.

## Outputs & Resume
- Profile (strukturierte Gegenüber-Infos): `outputs/profiles/*.json`
- Nachrichten-Entwürfe: `outputs/letters/*.md`
- Kontakt-Export: `outputs/contacts.csv`
- Kandidaten-Snapshot: `data/staging/candidates_selected.json`
- Run-Zusammenfassung: `data/staging/last_run.json`
- Chat-Konfiguration: `data/staging/chat_state.json`
- Logs: `logs/pipeline.log` (JSON pro Event)

## CLI (optional)
- Direkter Lauf: `uv run --env-file .env python workflows/research_pipeline.py --phase refine --region any --brief config/brief.yaml`
- Resume (ohne neue Websuche): `… --resume-candidates data/staging/candidates_selected.json`

## Hinweise
- Concurrency: `config/pipeline.yaml` (`candidate_concurrency`) oder `PIPELINE_CANDIDATE_CONCURRENCY`
- Stop-Schalter: `--stop-file data/staging/stop.flag` (oder `config/pipeline.yaml`)
- Secrets nur in `.env`/`.env.local` (nicht committen)
