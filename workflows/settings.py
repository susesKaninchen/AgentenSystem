from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


DEFAULT_CONFIG_PATH = Path("config/pipeline.yaml")


@dataclass
class PipelineSettings:
    phase: str = "refine"
    region: str = "luebeck-local"
    target_candidates: int = 5
    max_iterations: int = 10
    results_per_query: int = 6
    letters_per_run: int = 5
    candidate_concurrency: int = 4
    search_retries: int = 2
    search_retry_backoff: float = 3.0
    stop_file: str = "data/staging/stop.flag"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineSettings":
        return cls(
            phase=str(data.get("phase", cls.phase)),
            region=str(data.get("region", cls.region)),
            target_candidates=int(data.get("target_candidates", cls.target_candidates)),
            max_iterations=int(data.get("max_iterations", cls.max_iterations)),
            results_per_query=int(data.get("results_per_query", cls.results_per_query)),
            letters_per_run=int(data.get("letters_per_run", cls.letters_per_run)),
            candidate_concurrency=int(data.get("candidate_concurrency", cls.candidate_concurrency)),
            search_retries=int(data.get("search_retries", cls.search_retries)),
            search_retry_backoff=float(data.get("search_retry_backoff", cls.search_retry_backoff)),
            stop_file=str(data.get("stop_file", cls.stop_file)),
        )


def load_pipeline_settings(path: Path = DEFAULT_CONFIG_PATH) -> PipelineSettings:
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
        settings = PipelineSettings.from_dict(raw)
    else:
        settings = PipelineSettings()
    # Env overrides (simple mapping)
    settings.phase = os.environ.get("PIPELINE_PHASE", settings.phase)
    settings.region = os.environ.get("PIPELINE_REGION", settings.region)
    if env_val := os.environ.get("PIPELINE_TARGET_CANDIDATES"):
        settings.target_candidates = max(1, int(env_val))
    if env_val := os.environ.get("PIPELINE_MAX_ITERATIONS"):
        settings.max_iterations = max(1, int(env_val))
    if env_val := os.environ.get("PIPELINE_RESULTS_PER_QUERY"):
        settings.results_per_query = max(1, int(env_val))
    if env_val := os.environ.get("MAX_LETTERS_PER_RUN"):
        settings.letters_per_run = max(0, int(env_val))
    if env_val := os.environ.get("PIPELINE_CANDIDATE_CONCURRENCY"):
        settings.candidate_concurrency = max(1, int(env_val))
    return settings
