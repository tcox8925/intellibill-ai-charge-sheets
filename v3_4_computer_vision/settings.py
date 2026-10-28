"""Configuration for the standalone charge-sheet v3 package.

Environment variable names are unchanged from v2 so existing deployment scripts
keep working. Everything has a working default; nothing here must be set to run
locally.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    # Locked template contract
    template_id: str
    template_version: int
    template_root: Path
    render_dpi: int

    # Models
    header_model: str
    mark_model: str
    enable_header_notes: bool

    # v3 decision policy
    promote_confidence: float
    hint_margin: float
    min_physical_coverage: float
    enable_page_audit: bool

    # Auth (Key Vault / Foundry path)
    key_vault_url: str
    anthropic_key_secret: str
    anthropic_foundry_endpoint: str

    pipeline_version: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = Path(__file__).resolve().parent
    return Settings(
        template_id=os.getenv(
            "CHARGESHEET_TEMPLATE_ID",
            "nwa_internal_medicine_superbill_locked_v1").strip(),
        template_version=_int("CHARGESHEET_TEMPLATE_VERSION", 1),
        template_root=Path(os.getenv("CHARGESHEET_TEMPLATE_ROOT",
                                     str(root / "templates"))),
        render_dpi=_int("CHARGESHEET_RENDER_DPI", 200),

        header_model=os.getenv("CHARGESHEET_HEADER_MODEL", "claude-opus-4-6").strip(),
        mark_model=os.getenv("CHARGESHEET_MARK_MODEL", "claude-opus-4-6").strip(),
        enable_header_notes=_bool("CHARGESHEET_ENABLE_HEADER_NOTES", True),

        # Retained for compatibility/telemetry. Physical circle/arc decisions are not
        # promoted or rejected by a numeric confidence threshold.
        promote_confidence=_float("CHARGESHEET_PROMOTE_CONFIDENCE", 0.75),
        # Retained for geometry diagnostic metadata only. It has no authority
        # to confirm, veto, downgrade, or tie-break visual ownership.
        hint_margin=_float("CHARGESHEET_HINT_MARGIN", 0.12),
        min_physical_coverage=_float("CHARGESHEET_MIN_PHYSICAL_COVERAGE", 0.70),
        enable_page_audit=_bool("CHARGESHEET_ENABLE_PAGE_AUDIT", True),

        key_vault_url=os.getenv(
            "AZURE_KEY_VAULT_URL",
            "https://keyvault-834analytics.vault.azure.net/").strip(),
        anthropic_key_secret=os.getenv("ANTHROPIC_KEY_SECRET", "834-claude-key").strip(),
        anthropic_foundry_endpoint=os.getenv(
            "ANTHROPIC_FOUNDRY_ENDPOINT",
            "https://sql-test-resource.services.ai.azure.com/anthropic/").strip(),

        pipeline_version=os.getenv(
            "CHARGESHEET_PIPELINE_VERSION",
            "mark-centric-physical-overlap-v3.4").strip(),
    )
