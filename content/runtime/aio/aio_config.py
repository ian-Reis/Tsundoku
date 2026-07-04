from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

DEFAULT_OUTPUT_DIR = "manga"
CANONICAL_HID_MARKER = ".series_hid"
LEGACY_HID_MARKER = ".mangafire_hid"
SUPPORTED_HID_MARKERS = (CANONICAL_HID_MARKER, LEGACY_HID_MARKER)
CONFIG_FILENAME = "aio_config.json"


def _config_path(base_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(base_dir) if base_dir else Path.cwd()
    return root / CONFIG_FILENAME


def load_aio_config(base_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = _config_path(base_dir)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def resolve_output_dir(
    cli_value: Optional[str] = None,
    *,
    base_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve the library output root.

    Priority: explicit CLI value, AIO_OUTPUT_DIR, aio_config.json, default.
    Relative paths intentionally remain relative to the current working
    directory, matching the downloader's historical behavior.
    """
    if cli_value:
        return cli_value
    env_value = os.environ.get("AIO_OUTPUT_DIR", "").strip()
    if env_value:
        return env_value
    cfg = load_aio_config(base_dir)
    configured = cfg.get("output_dir")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return DEFAULT_OUTPUT_DIR


def supported_hid_markers(config: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    markers: list[str] = []
    if config:
        raw = config.get("supported_hid_markers")
        if isinstance(raw, list):
            markers.extend(str(item) for item in raw if str(item).strip())
    for marker in SUPPORTED_HID_MARKERS:
        if marker not in markers:
            markers.append(marker)
    return tuple(markers)


def read_hid_marker(folder: str | os.PathLike[str]) -> str | None:
    cfg = load_aio_config()
    for marker in supported_hid_markers(cfg):
        path = Path(folder) / marker
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8", errors="ignore").strip()
                if value:
                    return value
        except Exception:
            continue
    return None


def write_hid_marker(folder: str | os.PathLike[str], hid: str) -> None:
    path = Path(folder) / CANONICAL_HID_MARKER
    try:
        path.write_text(str(hid), encoding="utf-8")
    except Exception:
        pass


def ignored_library_filenames(config: Mapping[str, Any] | None = None) -> set[str]:
    return {".DS_Store", *supported_hid_markers(config)}
