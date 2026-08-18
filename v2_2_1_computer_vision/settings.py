from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Settings:
    template_id: str
    template_version: int
    template_root: Path
    render_dpi: int
    pipeline_version: str
    header_model: str
    header_extractor_version: str
    enable_header_notes: bool
    mark_model: str
    mark_resolver_version: str
    enable_mark_ai: bool
    mark_promote_confidence: float
    mark_suppress_confidence: float
    mark_display_candidates: int
    match_min_override: float | None
    key_vault_url: str
    anthropic_key_secret: str
    anthropic_foundry_endpoint: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = Path(__file__).resolve().parent
    return Settings(
        template_id=os.getenv(
            "CHARGESHEET_TEMPLATE_ID",
            "nwa_internal_medicine_superbill_locked_v1",
        ).strip(),
        template_version=_int("CHARGESHEET_TEMPLATE_VERSION", 1),
        template_root=Path(os.getenv("CHARGESHEET_TEMPLATE_ROOT", str(root / "templates"))),
        render_dpi=_int("CHARGESHEET_RENDER_DPI", 200),
        pipeline_version=os.getenv(
            "CHARGESHEET_PIPELINE_VERSION",
            "locked-candidates-ai-verified-v2.2.1-manual-proc-review",
        ).strip(),
        header_model=os.getenv("CHARGESHEET_HEADER_MODEL", "claude-opus-4-6").strip(),
        header_extractor_version=os.getenv(
            "CHARGESHEET_HEADER_EXTRACTOR_VERSION",
            "header_notes_v1",
        ).strip(),
        enable_header_notes=_bool("CHARGESHEET_ENABLE_HEADER_NOTES", True),
        mark_model=os.getenv("CHARGESHEET_MARK_MODEL", "claude-opus-4-6").strip(),
        mark_resolver_version=os.getenv(
            "CHARGESHEET_MARK_RESOLVER_VERSION",
            "candidate_crop_resolver_v2_2_1_manual_proc_review",
        ).strip(),
        enable_mark_ai=_bool("CHARGESHEET_ENABLE_MARK_AI", True),
        mark_promote_confidence=_float("CHARGESHEET_MARK_PROMOTE_CONFIDENCE", 0.85),
        mark_suppress_confidence=_float("CHARGESHEET_MARK_SUPPRESS_CONFIDENCE", 0.92),
        mark_display_candidates=_int("CHARGESHEET_MARK_DISPLAY_CANDIDATES", 2),
        match_min_override=_optional_float("CHARGESHEET_MATCH_MIN"),
        # Kept for diagnostics/backward compatibility. auth.py owns the defaults.
        key_vault_url=os.getenv(
            "AZURE_KEY_VAULT_URL", "https://keyvault-834analytics.vault.azure.net/"
        ).strip(),
        anthropic_key_secret=os.getenv("ANTHROPIC_KEY_SECRET", "834-claude-key").strip(),
        anthropic_foundry_endpoint=os.getenv(
            "ANTHROPIC_FOUNDRY_ENDPOINT",
            "https://sql-test-resource.services.ai.azure.com/anthropic/",
        ).strip(),
    )
