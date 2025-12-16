"""
LLM-gestuetzter Chat-Einstieg fuer die Recherche-Pipeline.

Funktionen:
- Liest Kontext (Identity, Notizen, Snapshot-Stats) und schlaegt Einstellungen fuer die
  `workflows/research_pipeline.py` vor.
- Fuehlt sich wie ein moderierter Chat an: Das Modell fasst zusammen, fragt nach fehlenden
  Infos und bereitet konkrete Datei-Updates vor (nur nach Bestaetigung schreiben).
- Defensive Fehlerbehandlung: fehlende Env-Variablen, LLM-Fehler und Pipeline-Fehler
  werden als Hinweise ausgegeben und brechen den Chat nicht ab.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from tools.identity_loader import get_identity_summary, load_identity
from workflows.brief import DEFAULT_BRIEF_PATH, brief_summary, load_campaign_brief, load_message_template
from workflows import research_pipeline
from workflows.settings import load_pipeline_settings


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
CHAT_STATE_PATH = REPO_ROOT / "data/staging/chat_state.json"
LAST_RUN_PATH = REPO_ROOT / "data/staging/last_run.json"

SETTINGS = load_pipeline_settings()
DEFAULTS = {
    "phase": SETTINGS.phase,
    "region": SETTINGS.region,
    "max_iterations": SETTINGS.max_iterations,
    "results_per_query": SETTINGS.results_per_query,
    "letters": SETTINGS.letters_per_run,
    "target_candidates": SETTINGS.target_candidates,
    "brief_path": str(DEFAULT_BRIEF_PATH),
}

ALLOWED_EDIT_PATHS: Dict[str, Path] = {
    "identity": REPO_ROOT / "config/identity.yaml",
    "brief": REPO_ROOT / "config/brief.yaml",
    "template": REPO_ROOT / "config/outreach_template.md",
    "notes": REPO_ROOT / "data/staging/research_notes.md",
    "registry": REPO_ROOT / "data/staging/organizations_registry.json",
    "blacklist": REPO_ROOT / "data/staging/blacklist.json",
    "candidates": REPO_ROOT / "data/staging/candidates_selected.json",
}
PIPELINE_LOG = REPO_ROOT / "logs/pipeline.log"
LETTERS_DIR = REPO_ROOT / "outputs/letters"

ALLOWED_EDIT_RESOLVED = {p.resolve() for p in ALLOWED_EDIT_PATHS.values()}
MAX_EDIT_CHARS = 12_000
PIPELINE_SETTINGS_KEYS = {
    "phase",
    "region",
    "max_iterations",
    "results_per_query",
    "letters",
    "target_candidates",
    "resume",
    "resume_path",
    "brief_path",
}
BRIEF_SCHEMA_KEYS = {
    "task",
    "target_profile",
    "message_template_path",
    "language",
    "focus_areas",
    "commercial_mode",
    "require_contact_email",
    "max_message_words",
    "avoid_promises",
    "search_focus_keywords",
    "exclude_url_suffixes",
    "exclude_domains",
    "exclude_text_terms",
}


@dataclass
class ChatConfig:
    phase: str = DEFAULTS["phase"]
    region: str = DEFAULTS["region"]
    max_iterations: int = DEFAULTS["max_iterations"]
    results_per_query: int = DEFAULTS["results_per_query"]
    letters: int = DEFAULTS["letters"]
    target_candidates: int = DEFAULTS["target_candidates"]
    resume_path: Optional[str] = None
    brief_path: str = DEFAULTS["brief_path"]

    def normalize(self) -> None:
        def coerce_int(value: Any, default: int) -> int:
            if value is None:
                return default
            if isinstance(value, str):
                if not value.strip():
                    return default
                try:
                    return int(value.strip())
                except Exception:
                    return default
            try:
                return int(value)
            except Exception:
                return default

        self.phase = str(self.phase or DEFAULTS["phase"]).strip()
        self.region = str(self.region or DEFAULTS["region"]).strip()
        self.max_iterations = max(1, coerce_int(self.max_iterations, DEFAULTS["max_iterations"]))
        self.results_per_query = max(1, coerce_int(self.results_per_query, DEFAULTS["results_per_query"]))
        self.letters = max(0, coerce_int(self.letters, DEFAULTS["letters"]))
        target_default = self.letters if self.letters > 0 else DEFAULTS["target_candidates"]
        self.target_candidates = max(1, coerce_int(self.target_candidates, target_default))
        if self.resume_path is None:
            self.resume_path = None
        else:
            resume = str(self.resume_path).strip()
            self.resume_path = resume or None
        self.brief_path = str(self.brief_path or DEFAULTS["brief_path"]).strip() or DEFAULTS["brief_path"]


def load_chat_state() -> ChatConfig:
    cfg = ChatConfig()
    if not CHAT_STATE_PATH.exists():
        return cfg
    try:
        data = json.loads(CHAT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return cfg
    if isinstance(data, dict):
        for key in ["phase", "region", "max_iterations", "results_per_query", "letters", "target_candidates", "resume_path", "brief_path"]:
            if key in data:
                setattr(cfg, key, data.get(key))
    cfg.normalize()
    return cfg


def save_chat_state(cfg: ChatConfig) -> None:
    cfg.normalize()
    CHAT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHAT_STATE_PATH.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


def console(msg: str) -> None:
    print(f"[CHAT] {msg}")


def load_env() -> None:
    research_pipeline.load_env_file(ENV_PATH)


def try_build_chat_model() -> Optional[OpenAIChatCompletionsModel]:
    try:
        research_pipeline.ensure_required_env(["OPENAI_BASE_URL", "OPENAI_MODEL"])
    except Exception as exc:
        console(f"LLM-Setup unvollstaendig (OPENAI_* fehlen): {exc}")
        return None

    try:
        chat_model, _ = research_pipeline.build_models()
    except Exception as exc:
        console(f"LLM konnte nicht initialisiert werden: {exc}")
        return None
    return chat_model


def read_optional_file(path: Path, limit: int = 2000) -> str:
    if not path.exists():
        return f"{path} (fehlt)"
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"{path} (Fehler beim Lesen: {exc})"
    if len(content) > limit:
        return content[:limit] + "\n... (gekürzt)"
    return content


def tail_file(path: Path, lines: int = 15) -> str:
    if not path.exists():
        return f"{path} (fehlt)"
    try:
        content = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return f"{path} (Fehler beim Lesen: {exc})"
    tail = content[-lines:]
    return "\n".join(tail)


def summarize_last_run(path: Path = LAST_RUN_PATH) -> str:
    if not path.exists():
        return "kein letzter Lauf gefunden"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "letzter Lauf: nicht lesbar"
    if not isinstance(data, dict):
        return "letzter Lauf: unbekanntes Format"
    generated_at = str(data.get("generated_at") or "").strip()
    run = data.get("run") if isinstance(data.get("run"), dict) else {}
    brief = data.get("brief") if isinstance(data.get("brief"), dict) else {}
    accepted = run.get("accepted")
    target = run.get("target_candidates")
    letters = run.get("letters_done")
    region = run.get("region")
    task = str(brief.get("task") or "").strip()
    task_short = (task[:90] + "...") if len(task) > 90 else task
    return f"{generated_at} | accepted={accepted}/{target} letters={letters} region={region} | task={task_short or '—'}"


def list_recent_letters(limit: int = 3) -> str:
    if not LETTERS_DIR.exists():
        return "Keine Briefe vorhanden."
    files = sorted(LETTERS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "Keine Briefe vorhanden."
    lines = []
    for file in files[:limit]:
        preview = file.read_text(encoding="utf-8").splitlines()[:6]
        lines.append(f"- {file.name}: " + " | ".join(preview))
    return "\n".join(lines)


def ensure_notes_file() -> None:
    path = ALLOWED_EDIT_PATHS["notes"]
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Research Notes\n\n*Automatisch angelegt.*\n", encoding="utf-8")
    console(f"[INFO] Research-Notizen angelegt: {path}")


def validate_resume(cfg: ChatConfig) -> ChatConfig:
    cfg.normalize()
    if not cfg.resume_path:
        return cfg
    path = REPO_ROOT / cfg.resume_path
    if path.exists():
        return cfg
    console(f"[WARN] Resume-Snapshot nicht gefunden: {path}")
    choice = input("Resume deaktivieren und frisch starten? (ja/nein): ").strip().lower()
    if choice in {"", "j", "ja", "y", "yes"}:
        cfg.resume_path = None
    return cfg


def summarize_candidates_snapshot(path: Path) -> str:
    if not path.exists():
        return "Kein Snapshot vorhanden."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        accepted = len(data.get("accepted", []))
        all_candidates = len(data.get("all_candidates", []))
        return f"{accepted} akzeptiert, {all_candidates} gesamt (aus {path})"
    except Exception as exc:
        return f"Snapshot {path} konnte nicht gelesen werden: {exc}"


def context_summary(cfg: Optional[ChatConfig] = None) -> str:
    brief_rel = (cfg.brief_path if cfg else DEFAULTS["brief_path"]) or DEFAULTS["brief_path"]
    brief_candidate = Path(brief_rel)
    brief_path = (brief_candidate if brief_candidate.is_absolute() else (REPO_ROOT / brief_candidate)).resolve()
    brief = load_campaign_brief(brief_path)
    template_candidate = Path(brief.message_template_path)
    template_path = template_candidate if template_candidate.is_absolute() else (REPO_ROOT / template_candidate)
    template_text = load_message_template(template_path)
    identity_text = ""
    try:
        identity_text = get_identity_summary(load_identity())
    except Exception as exc:
        identity_text = f"Identity nicht ladbar: {exc}"

    brief_text = brief_summary(brief)
    template_preview = (template_text[:600] + "\n... (gekürzt)") if len(template_text) > 600 else (template_text or "(keine Vorlage)")
    identity_yaml = read_optional_file(ALLOWED_EDIT_PATHS["identity"], limit=2400)
    brief_yaml = read_optional_file(brief_path, limit=2400)

    notes_path = ALLOWED_EDIT_PATHS["notes"]
    notes_text = read_optional_file(notes_path, limit=1200)
    snapshot_info = summarize_candidates_snapshot(ALLOWED_EDIT_PATHS["candidates"])

    log_tail = tail_file(PIPELINE_LOG, lines=12)
    letters_list = list_recent_letters(limit=3)
    last_run = read_optional_file(LAST_RUN_PATH, limit=800)
    return textwrap.dedent(
        f"""\
        Identity Summary: {identity_text}
        Identity YAML (Auszug):
        {identity_yaml}

        Briefing:
        {brief_text}
        Briefing YAML (Auszug):
        {brief_yaml}

        Template (Auszug):
        {template_preview}

        Research Notes (Auszug):
        {notes_text}

        Snapshot: {snapshot_info}
        Last Run (raw):
        {last_run}
        Logs (Tail):
        {log_tail}

        Letzte Briefe:
        {letters_list}
        """
    ).strip()


def config_summary(cfg: ChatConfig) -> str:
    cfg.normalize()
    return (
        f"Phase={cfg.phase}, Region={cfg.region}, Iterationen={cfg.max_iterations}, "
        f"Treffer/Query={cfg.results_per_query}, Briefe={cfg.letters}, "
        f"Ziel Kandidaten={cfg.target_candidates}, Resume={cfg.resume_path or 'nein'}, Brief={cfg.brief_path}"
    )


def parse_llm_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        payload = research_pipeline.extract_json_block(text)
        return json.loads(payload)
    except Exception:
        return {}


def apply_settings_from_llm(cfg: ChatConfig, payload: dict[str, Any]) -> None:
    settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
    if not isinstance(settings, dict):
        return

    if "phase" in settings:
        cfg.phase = settings.get("phase") or cfg.phase
    if "region" in settings:
        cfg.region = settings.get("region") or cfg.region
    if "max_iterations" in settings:
        max_iterations = settings.get("max_iterations")
        if max_iterations is not None:
            cfg.max_iterations = max_iterations
    if "results_per_query" in settings:
        results_per_query = settings.get("results_per_query")
        if results_per_query is not None:
            cfg.results_per_query = results_per_query
    if "letters" in settings:
        letters = settings.get("letters")
        if letters is not None:
            cfg.letters = letters
    if "target_candidates" in settings:
        target_candidates = settings.get("target_candidates")
        if target_candidates is not None:
            cfg.target_candidates = target_candidates
    if "resume_path" in settings:
        resume_val = settings.get("resume_path")
        if resume_val is None:
            cfg.resume_path = None
        else:
            resume_str = str(resume_val).strip()
            cfg.resume_path = None if resume_str.lower() in {"none", "null", "-"} else (resume_str or None)
    if "brief_path" in settings:
        cfg.brief_path = settings.get("brief_path") or cfg.brief_path
    cfg.normalize()


def normalize_text_for_write(text: str) -> str:
    text = (text or "").replace("\r\n", "\n")
    return text.rstrip() + "\n"


def looks_like_secret(content: str) -> bool:
    content = content or ""
    patterns = [
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        re.compile(r"OPENAI_API_KEY", re.IGNORECASE),
        re.compile(r"GOOGLE_API_KEY", re.IGNORECASE),
    ]
    return any(p.search(content) for p in patterns)


def validate_edit_content(target_path: Path, content: str) -> Optional[str]:
    """
    Returns an error string if invalid, else None.
    """

    content = content or ""

    resolved = target_path.resolve()

    if target_path.suffix.lower() == ".json":
        try:
            json.loads(content)
        except Exception as exc:
            return f"ungueltiges JSON ({exc})"
        return None

    if target_path.suffix.lower() in {".yaml", ".yml"}:
        try:
            data = yaml.safe_load(content)
        except Exception as exc:
            return f"ungueltiges YAML ({exc})"
        if not isinstance(data, dict):
            return "YAML muss ein Mapping (dict) sein"

        if resolved == ALLOWED_EDIT_PATHS["brief"].resolve():
            keys = set(map(str, data.keys()))
            if keys & PIPELINE_SETTINGS_KEYS:
                return "sieht nach Pipeline-Settings aus (bitte ueber settings aendern, nicht brief.yaml)"
            if not (keys & BRIEF_SCHEMA_KEYS):
                return "sieht nicht wie ein CampaignBrief aus (task/target_profile/... fehlt)"

        if resolved == ALLOWED_EDIT_PATHS["identity"].resolve():
            if "organization" not in data or "representative" not in data:
                return "identity.yaml muss mindestens organization und representative enthalten"
            if not isinstance(data.get("organization"), dict) or not isinstance(data.get("representative"), dict):
                return "identity.yaml: organization und representative muessen Mappings (dict) sein"

        return None

    return None


def is_noop_edit(path: Path, new_content: str) -> bool:
    if not path.exists():
        return False
    try:
        existing = path.read_text(encoding="utf-8")
    except Exception:
        return False
    if path.suffix.lower() == ".json":
        try:
            return json.loads(existing) == json.loads(new_content)
        except Exception:
            pass
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            return (yaml.safe_load(existing) or {}) == (yaml.safe_load(new_content) or {})
        except Exception:
            pass
    return normalize_text_for_write(existing) == normalize_text_for_write(new_content)


def normalize_edit_proposals(payload: dict[str, Any]) -> List[dict[str, str]]:
    edits = payload.get("edits", []) if isinstance(payload, dict) else []
    if not isinstance(edits, list):
        return []

    normalized: List[dict[str, str]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        path_str = str(edit.get("path", "")).strip()
        reason = str(edit.get("reason", "")).strip()
        content = edit.get("content")
        if not path_str or content is None:
            continue
        path = (REPO_ROOT / path_str).resolve()
        if path not in ALLOWED_EDIT_RESOLVED:
            console(f"Edit-Vorschlag ignoriert (Pfad nicht erlaubt): {path_str}")
            continue
        text_content = str(content)
        if len(text_content) > MAX_EDIT_CHARS:
            console(f"Edit-Vorschlag zu groß ({len(text_content)} Zeichen) – abgelehnt: {path_str}")
            continue
        if looks_like_secret(text_content):
            console(f"Edit-Vorschlag wirkt wie ein Secret – abgelehnt: {path_str}")
            continue
        if err := validate_edit_content(path, text_content):
            console(f"Edit-Vorschlag ungueltig – abgelehnt: {path_str} ({err})")
            continue
        if is_noop_edit(path, text_content):
            continue
        normalized.append({"path": path_str, "reason": reason, "content": normalize_text_for_write(text_content)})
    return normalized


def render_edit_overview(edits: List[dict[str, str]]) -> str:
    lines: List[str] = []
    for edit in edits:
        path_str = edit.get("path", "").strip()
        reason = edit.get("reason", "").strip() or "—"
        preview = (edit.get("content", "")[:260] or "").strip()
        if len(preview) == 260:
            preview += " …"
        lines.append(f"- {path_str} ({reason})")
        if preview:
            lines.append(f"  Vorschau: {preview.replace(chr(10), ' | ')}")
    return "\n".join(lines).strip()


def apply_pending_edits(edits: List[dict[str, str]]) -> None:
    for edit in edits:
        path_str = edit["path"].strip()
        content = edit["content"]
        path = (REPO_ROOT / path_str).resolve()
        if path not in ALLOWED_EDIT_RESOLVED:
            console(f"Edit ignoriert (Pfad nicht erlaubt): {path_str}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(normalize_text_for_write(content), encoding="utf-8")
            console(f"Gespeichert: {path_str}")
        except Exception as exc:
            console(f"Konnte {path_str} nicht schreiben: {exc}")


def render_history(history: List[tuple[str, str]], limit: int = 6) -> str:
    if not history:
        return "Keine Unterhaltung bisher."
    return "\n".join(f"{role}: {text}" for role, text in history[-limit:])


def classify_confirmation(text: str) -> Optional[bool]:
    raw = (text or "").strip().lower()
    if not raw:
        return None
    raw = raw.replace("ß", "ss")
    if raw in {"ja", "j", "yes", "y", "ok", "okay", "passt", "go", "mach", "bitte", "gern"}:
        return True
    if raw in {"nein", "n", "no", "stop", "abbruch", "abbrechen", "nicht"}:
        return False

    if raw.startswith(("ja ", "yes ", "ok ")):
        if any(word in raw for word in ["aber", "jedoch", "und", "nur", "sondern"]):
            return None
        return True
    if raw.startswith(("nein ", "no ")):
        return False
    return None


def is_exit_message(text: str) -> bool:
    raw = (text or "").strip().lower()
    return raw in {"exit", "quit", "q", "ende", "tschuss", "abbruch", "abbrechen"}


def maybe_unescape_newlines(text: str) -> str:
    return (text or "").replace("\\n", "\n")


async def llm_reply(
    model: OpenAIChatCompletionsModel,
    cfg: ChatConfig,
    history: List[tuple[str, str]],
    user_message: str,
) -> tuple[str, dict[str, Any]]:
    cfg.normalize()
    agent = Agent(
        name="ModeratorChat",
        instructions=(
            "Du bist ein menschlich wirkender Moderator fuer ein lokales Recherche- und Outreach-System. "
            "Dein Ziel: in einem natuerlichen Dialog fehlende Infos klaeren (\"Wer sind wir? Wen suchen wir? Region?\"), "
            "den aktuellen Stand kurz zusammenfassen und erst nach Bestaetigung konkrete Datei-Updates vorbereiten. "
            "WICHTIG: Keine Slash-Commands, keine Programm-Erklaerungen, kein \"Tippe /...\". "
            "Wenn du Aenderungen vorschlaegst, bitte um explizite Zustimmung (ja/nein), bevor gespeichert oder gestartet wird. "
            "WICHTIG: Phase/Region/Iterationen/Treffer/Briefe/Zielkandidaten sind Laufzeit-Einstellungen und gehoeren in das JSON-Feld settings "
            "(nicht als Datei-Edit in config/brief.yaml). "
            "config/brief.yaml ist ein CampaignBrief und enthaelt z. B. task, target_profile, message_template_path, Filter/Guardrails. "
            "Du darfst nur diese Dateien zum Ueberschreiben vorschlagen: "
            "config/identity.yaml, config/brief.yaml, config/outreach_template.md, "
            "data/staging/research_notes.md, data/staging/organizations_registry.json, "
            "data/staging/blacklist.json, data/staging/candidates_selected.json. "
            "Antworte AUSSCHLIESSLICH als JSON-Objekt im Schema: "
            "{\"assistant_message\": str, "
            "\"settings\": {\"phase\": str?, \"region\": str?, \"max_iterations\": int?, \"results_per_query\": int?, "
            "\"letters\": int?, \"target_candidates\": int?, \"resume_path\": str|null?, \"brief_path\": str?}?, "
            "\"edits\": [{\"path\": \"relative/path\", \"reason\": \"...\", \"content\": \"...\"}]?, "
            "\"start_pipeline\": bool?}. "
            "assistant_message ist das, was der Nutzer sieht (freundlich, kompakt, mit Rueckfragen). "
            "Wenn du edits lieferst, liefere den KOMPLETTEN neuen Datei-Inhalt."
        ),
        model=model,
    )

    prompt = textwrap.dedent(
        f"""\
        Kontext:
        {context_summary(cfg)}

        Aktuelle Einstellungen:
        {config_summary(cfg)}

        Unterhaltung:
        {render_history(history)}

        Nutzer: {user_message}

        Bitte schlanke Antwort + genau EINEN JSON-Block.
        """
    )
    result = await Runner.run(agent, prompt)
    reply = result.final_output or ""
    parsed = parse_llm_json(reply)
    assistant_message = str(parsed.get("assistant_message") or "").strip()
    if not assistant_message:
        assistant_message = reply.strip()
    return maybe_unescape_newlines(assistant_message), parsed


def run_pipeline(cfg: ChatConfig) -> int:
    cfg.normalize()
    cmd = [
        sys.executable,
        str(REPO_ROOT / "workflows" / "research_pipeline.py"),
        "--brief",
        cfg.brief_path,
        "--phase",
        cfg.phase,
        "--region",
        cfg.region,
        "--max-iterations",
        str(cfg.max_iterations),
        "--results-per-query",
        str(cfg.results_per_query),
        "--letters-per-run",
        str(cfg.letters),
        "--target-candidates",
        str(cfg.target_candidates),
    ]
    if cfg.resume_path:
        cmd += ["--resume-candidates", cfg.resume_path]

    env = os.environ.copy()
    env.setdefault("MAX_LETTERS_PER_RUN", str(cfg.letters))
    env.setdefault("PIPELINE_TARGET_CANDIDATES", str(cfg.target_candidates))
    console(f"Starte Pipeline mit: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
    except Exception as exc:
        console(f"Pipeline konnte nicht gestartet werden: {exc}")
        return 1
    return result.returncode


def manual_set(cfg: ChatConfig, key: str, value: str) -> None:
    key = key.lower()
    if key == "phase":
        cfg.phase = value
    elif key == "region":
        cfg.region = value
    elif key in {"max_iterations", "iterations"}:
        cfg.max_iterations = int(value)
    elif key in {"results_per_query", "results"}:
        cfg.results_per_query = int(value)
    elif key in {"letters", "letters_per_run"}:
        cfg.letters = int(value)
    elif key in {"target", "target_candidates"}:
        cfg.target_candidates = int(value)
    elif key == "resume":
        cfg.resume_path = value or None
    elif key in {"brief", "brief_path"}:
        cfg.brief_path = value
    cfg.normalize()


def manual_edit_file(name: str) -> None:
    path = ALLOWED_EDIT_PATHS.get(name)
    if not path:
        console(f"Unbekannter Edit-Befehl: {name}")
        return
    console(f"Aktueller Inhalt von {path} (Auszug):")
    print(read_optional_file(path, limit=800))
    choice = input("Datei ueberschreiben? (y/N): ").strip().lower()
    if choice != "y":
        return
    console("Gib den neuen Inhalt ein. Ende mit einer einzelnen Zeile 'EOF'.")
    lines: List[str] = []
    while True:
        line = input()
        if line.strip() == "EOF":
            break
        lines.append(line)
    content = "\n".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
        console(f"{path} aktualisiert.")
    except Exception as exc:
        console(f"Konnte {path} nicht schreiben: {exc}")


def show_brief_and_template(cfg: ChatConfig) -> None:
    cfg.normalize()
    brief_candidate = Path(cfg.brief_path)
    brief_path = brief_candidate if brief_candidate.is_absolute() else (REPO_ROOT / brief_candidate)
    brief = load_campaign_brief(brief_path)
    console("Briefing:\n" + brief_summary(brief))
    template_candidate = Path(brief.message_template_path)
    template_path = template_candidate if template_candidate.is_absolute() else (REPO_ROOT / template_candidate)
    template_text = load_message_template(template_path)
    if len(template_text) > 900:
        template_text = template_text[:900] + "\n... (gekürzt)"
    console("Template:\n" + (template_text or "(keine Vorlage)"))


async def interactive_loop() -> None:
    load_env()
    cfg = load_chat_state()
    history: List[tuple[str, str]] = []
    pending_edits: List[dict[str, str]] = []
    pending_start: bool = False
    awaiting: Optional[str] = None  # "save" | "start"
    chat_model = try_build_chat_model()
    ensure_notes_file()

    console(f"Letzter Lauf: {summarize_last_run()}")
    console("Moderator-Chat gestartet. Antworte einfach normal (\"ja\", \"nein\", oder mit Aenderungswuenschen). 'exit' beendet.")
    if not chat_model:
        console("LLM nicht verfuegbar. Bitte bearbeite config/brief.yaml, config/outreach_template.md und config/identity.yaml manuell.")
        return

    console("Ich schaue mir kurz an, was schon da ist ...")
    assistant_msg, parsed = await llm_reply(
        chat_model,
        cfg,
        history,
        "Starte den Moderator-Dialog: Fasse kurz zusammen, was du ueber Identitaet, Briefing und Vorlage weisst. "
        "Wenn Infos fehlen, stelle gezielte Rueckfragen. "
        "Wenn sinnvoll, schlage konkrete Verbesserungen an config/brief.yaml / config/outreach_template.md / config/identity.yaml vor "
        "und bitte um Bestaetigung.",
    )
    apply_settings_from_llm(cfg, parsed)
    pending_edits = normalize_edit_proposals(parsed)
    pending_start = bool(parsed.get("start_pipeline")) if isinstance(parsed, dict) else False
    save_chat_state(cfg)
    history.append(("assistant", assistant_msg))
    print(f"Moderator: {assistant_msg}")
    if pending_edits:
        overview = render_edit_overview(pending_edits)
        if overview:
            print("\nVorgeschlagene Datei-Aenderungen:\n" + overview)
        print("\nSoll ich diese Aenderungen speichern? (ja/nein)")
        awaiting = "save"
    elif pending_start:
        print(f"\nSoll ich jetzt die Pipeline starten mit: {config_summary(cfg)} ? (ja/nein)")
        awaiting = "start"

    while True:
        user = input("Du> ").strip()
        if not user:
            continue

        if is_exit_message(user):
            console("Abbruch ohne Pipeline-Start.")
            return

        if awaiting:
            decision = classify_confirmation(user)
            if decision is True:
                if awaiting == "save":
                    apply_pending_edits(pending_edits)
                    pending_edits = []
                    awaiting = None
                    if pending_start:
                        awaiting = "start"
                        print(f"Moderator: Alles klar. Soll ich jetzt die Pipeline starten mit: {config_summary(cfg)} ? (ja/nein)")
                        continue
                    print("Moderator: Alles klar. Was moechtest du als naechstes anpassen?")
                    continue
                if awaiting == "start":
                    cfg = validate_resume(cfg)
                    console(f"Starte mit: {config_summary(cfg)}")
                    save_chat_state(cfg)
                    rc = run_pipeline(cfg)
                    console(f"Pipeline beendet mit Code {rc}.")
                    return
            if decision is False:
                if awaiting == "save":
                    pending_edits = []
                    awaiting = None
                    if pending_start:
                        awaiting = "start"
                        print(f"Moderator: Alles klar. Ohne Speichern starten mit: {config_summary(cfg)} ? (ja/nein)")
                        continue
                    print("Moderator: Alles klar, ich speichere nichts. Was soll ich stattdessen aendern?")
                    continue
                if awaiting == "start":
                    pending_start = False
                    awaiting = None
                    print("Moderator: Alles klar, ich starte nicht. Was soll ich stattdessen aendern?")
                    continue
            pending_edits = []
            pending_start = False
            awaiting = None

        history.append(("user", user))
        assistant_msg, parsed = await llm_reply(chat_model, cfg, history, user)
        apply_settings_from_llm(cfg, parsed)
        pending_edits = normalize_edit_proposals(parsed)
        pending_start = bool(parsed.get("start_pipeline")) if isinstance(parsed, dict) else False
        save_chat_state(cfg)
        history.append(("assistant", assistant_msg))
        print(f"Moderator: {assistant_msg}")
        if pending_edits:
            overview = render_edit_overview(pending_edits)
            if overview:
                print("\nVorgeschlagene Datei-Aenderungen:\n" + overview)
            print("\nSoll ich diese Aenderungen speichern? (ja/nein)")
            awaiting = "save"
        elif pending_start:
            print(f"\nSoll ich jetzt die Pipeline starten mit: {config_summary(cfg)} ? (ja/nein)")
            awaiting = "start"
        else:
            awaiting = None


def main() -> None:
    try:
        asyncio.run(interactive_loop())
    except KeyboardInterrupt:
        console("Abgebrochen (KeyboardInterrupt).")
    except Exception as exc:
        console(f"Unerwarteter Fehler im Chat: {exc}")


if __name__ == "__main__":
    main()
