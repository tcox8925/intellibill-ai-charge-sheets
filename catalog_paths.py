"""Shared catalog path helpers.

Catalog JSON files live under the repository-local `catalogues/` directory.
These helpers keep lookup and write behavior consistent across the pipeline.
"""

import os


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOGUES_DIR = os.path.join(BASE_DIR, "catalogues")


def ensure_catalogues_dir() -> str:
    os.makedirs(CATALOGUES_DIR, exist_ok=True)
    return CATALOGUES_DIR


def catalog_input_path(path: str) -> str:
    """Resolve a catalog path for reading.

    Bare filenames are read from `catalogues/` by default, but an existing
    legacy root-level file is still honored as a fallback.
    """
    if os.path.isabs(path) or os.path.dirname(path):
        return os.path.abspath(path)
    catalogue_path = os.path.join(CATALOGUES_DIR, path)
    if os.path.exists(catalogue_path) or not os.path.exists(path):
        return catalogue_path
    return os.path.abspath(path)


def catalog_output_path(path: str) -> str:
    """Resolve a catalog path for writing.

    Bare filenames are written under `catalogues/`; explicit relative or
    absolute paths are preserved.
    """
    if os.path.isabs(path) or os.path.dirname(path):
        return os.path.abspath(path)
    ensure_catalogues_dir()
    return os.path.join(CATALOGUES_DIR, path)


def catalog_glob_pattern() -> str:
    ensure_catalogues_dir()
    return os.path.join(CATALOGUES_DIR, "catalog*.json")