from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from locked_template import LockedTemplate
from settings import get_settings


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def template_paths() -> tuple[Path, Path, Path]:
    s = get_settings()
    base = s.template_root / s.template_id / f"v{s.template_version}"
    return base / "manifest.json", base / "catalog.json", base / "reference.png"


def load_manifest_and_catalog() -> tuple[dict, dict, Path]:
    manifest_path, catalog_path, reference_path = template_paths()
    for p in (manifest_path, catalog_path, reference_path):
        if not p.exists():
            raise RuntimeError(f"Missing locked template artifact: {p}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    s = get_settings()

    if manifest.get("template_id") != s.template_id or int(manifest.get("version", -1)) != s.template_version:
        raise RuntimeError("Template manifest id/version does not match configured template")
    if catalog.get("template_id") != s.template_id:
        raise RuntimeError("Template catalog id does not match configured template")

    renderer = manifest.get("renderer") or {}
    if renderer.get("engine") != "pdftoppm":
        raise RuntimeError("Locked template renderer contract is not pdftoppm")
    if int(renderer.get("dpi", -1)) != s.render_dpi:
        raise RuntimeError(
            f"CHARGESHEET_RENDER_DPI={s.render_dpi} does not match locked template DPI={renderer.get('dpi')}"
        )

    if _sha256(catalog_path) != manifest.get("catalog_sha256"):
        raise RuntimeError("Locked template catalog checksum mismatch")
    if _sha256(reference_path) != manifest.get("reference_sha256"):
        raise RuntimeError("Locked template reference checksum mismatch")

    policy = catalog.get("policy") or {}
    if policy.get("runtime_code_source") != "locked_coordinates_only":
        raise RuntimeError("Unsafe template policy: runtime code source is not locked coordinates")
    if policy.get("ai_can_add_codes") is not False:
        raise RuntimeError("Unsafe template policy: AI is allowed to add codes")
    if policy.get("selected_mark") != "circle_only":
        raise RuntimeError("Unsafe template policy: selected mark is not circle_only")
    if policy.get("unknown_template") != "fail_closed":
        raise RuntimeError("Unsafe template policy: unknown template does not fail closed")

    return manifest, catalog, reference_path


@lru_cache(maxsize=1)
def locked_template() -> LockedTemplate:
    _, catalog, reference_path = load_manifest_and_catalog()
    return LockedTemplate(catalog=catalog, reference_path=str(reference_path))
