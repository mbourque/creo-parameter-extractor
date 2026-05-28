"""
Core implementation for CreoParameterExtractor.

Batch-extract Creo model parameters from a folder via CREOSON (creopyson).

Requires: Creo Parametric running with CREOSON server, and `pip install -r requirements.txt`.
API reference: https://creoson.com/functions.html

Created by: Michael P. Bourque
Date: 2026-05-14
Version: 1.0.0
"""

from __future__ import annotations

import html
import json
import logging
import os
import webbrowser
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext

import requests

import creopyson
from creopyson.exceptions import ErrorJsonDecode, MissingKey

lg = logging.getLogger(__name__)


def _app_dir() -> Path:
    """Directory for settings JSON: next to script, or next to .exe when frozen (PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_APP_DIR = _app_dir()
_SETTINGS_FILENAME = "CreoParameterExtractor_settings.json"
_LEGACY_SETTINGS_FILENAME = "prt_parameter_extractor_settings.json"


def _settings_file_candidates() -> list[Path]:
    """Install-dir settings first, then a per-user folder if the install dir is not writable."""
    primary = _APP_DIR / _SETTINGS_FILENAME
    legacy_primary = _APP_DIR / _LEGACY_SETTINGS_FILENAME
    if sys.platform == "win32":
        user_dir = Path(os.environ.get("APPDATA", Path.home())) / "CreoParameterExtractor"
    else:
        user_dir = Path.home() / ".config" / "creo_parameter_extractor"
    return [
        primary,
        user_dir / _SETTINGS_FILENAME,
        legacy_primary,
        user_dir / _LEGACY_SETTINGS_FILENAME,
    ]


def _can_write_settings_dir(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _resolve_settings_path() -> Path:
    candidates = _settings_file_candidates()
    preferred = candidates[:2]
    legacy = candidates[2:]

    for path in preferred:
        if path.exists():
            return path

    # Migrate legacy settings file to the new name on first run.
    for old_path in legacy:
        if old_path.exists():
            for new_path in preferred:
                if _can_write_settings_dir(new_path):
                    try:
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(old_path, new_path)
                        return new_path
                    except OSError:
                        continue
            return old_path

    for path in preferred:
        if _can_write_settings_dir(path):
            return path
    return preferred[0]


SETTINGS_PATH = _resolve_settings_path()


def _settings_defaults() -> dict[str, object]:
    return {
        "models_folder": "",
        "parameter_names": "",
        "output_file": "",
        "creoson_host": "localhost",
        "creoson_port": 9056,
        "recursive_search": False,
        "scan_parts": True,
        "scan_assemblies": True,
        "scan_drawings": True,
    }


def _normalize_settings(raw: dict | None) -> dict[str, object]:
    """Merge JSON dict into defaults with type checks (supports older 2-key files)."""
    d = _settings_defaults()
    if not raw:
        return d
    for key in ("models_folder", "parameter_names", "output_file", "creoson_host"):
        v = raw.get(key)
        if isinstance(v, str):
            d[key] = v
    p = raw.get("creoson_port")
    if isinstance(p, int) and 1 <= p <= 65535:
        d["creoson_port"] = p
    elif isinstance(p, str) and p.strip().isdigit():
        pi = int(p.strip())
        if 1 <= pi <= 65535:
            d["creoson_port"] = pi
    r = raw.get("recursive_search")
    if isinstance(r, bool):
        d["recursive_search"] = r
    for key in ("scan_parts", "scan_assemblies", "scan_drawings"):
        v = raw.get(key)
        if isinstance(v, bool):
            d[key] = v
    if not (d["scan_parts"] or d["scan_assemblies"] or d["scan_drawings"]):
        d["scan_parts"] = True
        d["scan_assemblies"] = True
        d["scan_drawings"] = True
    return d


def load_ui_settings(path: Path) -> dict[str, object]:
    """Load UI settings from JSON. Create file with defaults if missing and writable."""
    defaults = dict(_settings_defaults())
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(defaults, indent=2) + "\n", encoding="utf-8")
        except OSError as ex:
            lg.warning("Could not create settings file at %s: %s", path, ex)
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as ex:
        lg.warning("Could not read settings from %s: %s", path, ex)
        return defaults
    if not isinstance(raw, dict):
        return defaults
    return _normalize_settings(raw)


def save_ui_settings(path: Path, data: dict[str, object]) -> bool:
    """Persist settings; return False if the path could not be written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True
    except OSError as ex:
        lg.warning("Could not save settings to %s: %s", path, ex)
        return False


# HTTP timeouts: "light" requests vs heavy Creo work (open/regen).
_DEFAULT_CONNECT = 15.0
_DEFAULT_READ = 300.0
# `connection: connect` should return quickly if Creo + J-Link are ready.
_CONNECTION_READ = 60.0
_MODEL_NAME = re.compile(r"(?i)^(?P<base>.+)\.(?P<ext>prt|asm|drw)(?:\.(?P<ver>\d+))?$")
# Numbered model on disk (e.g. model.prt.7) — Creo/CREOSON "open" only accepts model.prt.
_VERSIONED_MODEL_DISK = re.compile(
    r"(?i)^(?P<stem>.+)\.(?P<ext>prt|asm|drw)\.(?P<num>\d+)$"
)


def is_versioned_model_disk_filename(name: str) -> bool:
    return _VERSIONED_MODEL_DISK.match(name) is not None


def plain_model_open_filename(versioned_disk_name: str) -> str:
    m = _VERSIONED_MODEL_DISK.match(versioned_disk_name)
    if not m:
        return versioned_disk_name
    return f"{m.group('stem')}.{m.group('ext').lower()}"


def model_kind_from_filename(name: str) -> str:
    """Return normalized model kind for report filters: part/assembly/drawing."""
    m = _MODEL_NAME.match(name)
    ext = m.group("ext").lower() if m else Path(plain_model_open_filename(name)).suffix.lower().lstrip(".")
    if ext == "asm":
        return "assembly"
    if ext == "drw":
        return "drawing"
    return "part"


def model_kind_priority(name: str) -> int:
    """Process parts first, then assemblies, then drawings."""
    kind = model_kind_from_filename(name)
    if kind == "part":
        return 0
    if kind == "assembly":
        return 1
    return 2


def model_file_in_scan(
    name: str,
    *,
    scan_parts: bool,
    scan_assemblies: bool,
    scan_drawings: bool,
) -> bool:
    """True if this filename's extension is enabled in Scan settings."""
    m = _MODEL_NAME.match(name)
    if not m:
        return False
    ext = m.group("ext").lower()
    if ext == "prt":
        return scan_parts
    if ext == "asm":
        return scan_assemblies
    if ext == "drw":
        return scan_drawings
    return False


def absolute_preserve_drive(path: Path) -> Path:
    """
    Make a path absolute without resolve().

    On Windows, resolve() often rewrites mapped drives (Z:) to UNC (\\\\server\\share),
    which Creo and CREOSON handle poorly for working directories.
    """
    return path.absolute()


def is_unc_path(path: Path) -> bool:
    """True if path is a UNC path (\\\\server\\share\\...)."""
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    try:
        return not path.drive and path.anchor.startswith("\\\\")
    except (ValueError, OSError):
        return False


def is_windows_mapped_network_drive(path: Path) -> bool:
    """True if path is on a Windows drive letter that maps to a network share."""
    if sys.platform != "win32":
        return False
    drive = path.drive
    if len(drive) != 2 or drive[1] != ":":
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(f"{drive[0]}:\\") == 4  # DRIVE_REMOTE
    except (AttributeError, OSError):
        return False


def models_folder_path_warnings(folder: Path) -> str | None:
    """User-facing warning when the models folder may fail with Creo/CREOSON."""
    folder = absolute_preserve_drive(folder)
    if is_unc_path(folder):
        return (
            "The models folder is a UNC path (\\\\server\\share\\...).\n\n"
            "Creo usually cannot use a UNC path as the working directory, and "
            "this tool avoids converting mapped drives to UNC. Use a local folder "
            "or a mapped drive letter (e.g. Z:\\...) instead."
        )
    if is_windows_mapped_network_drive(folder):
        return (
            f"The models folder is on mapped drive {folder.drive}\\\n\n"
            "Creo may see this drive, but CREOSON sometimes cannot (\"Directory "
            "does not exist\"). If extraction fails, use a local copy of the "
            "library or ask IT to map the drive for the CREOSON service."
        )
    return None


def prt_file_link_path(disk_path: Path) -> Path:
    """
    Path for file:// links in the HTML report.

    ``mypart.prt.5`` → ``mypart.prt`` in the same folder so Creo opens the latest
    file on disk (recursive and non-recursive).
    """
    plain = plain_model_open_filename(disk_path.name)
    if plain == disk_path.name:
        return disk_path
    return disk_path.parent / plain


def file_uri_full_path(disk_path: Path) -> str:
    """``file:///`` URI with a fully qualified absolute path (never relative)."""
    return absolute_preserve_drive(prt_file_link_path(disk_path)).as_uri()


def part_display_name(disk_path: Path) -> str:
    """Report Model name label (filename only, no .ext.N suffix)."""
    return plain_model_open_filename(disk_path.name)


def extract_work_dir() -> Path:
    """
    Per-run local mirror folder for Creo/CREOSON opens.

    This avoids UNC/mapped-drive working-directory issues by using only local paths.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path.home() / ".cache"
    run_dir = base / "CreoParameterExtractor" / "work" / f"run_{secrets.token_hex(6)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def build_local_model_mirror(
    source_root: Path,
    work_dir: Path,
    *,
    recursive: bool,
) -> dict[Path, Path]:
    """
    Copy latest models into a local mirror tree.

    Always includes parts, assemblies, and drawings so Creo can resolve references
    when opening assemblies or drawings (Scan menu only limits extraction).

    Returns a mapping of source absolute path -> local mirrored absolute path.
    Numbered models are copied as plain ``name.ext`` in the mirrored directory.
    """
    mapping: dict[Path, Path] = {}
    model_paths = iter_latest_model_files(
        source_root,
        recursive=recursive,
        scan_parts=True,
        scan_assemblies=True,
        scan_drawings=True,
    )
    for src in model_paths:
        try:
            rel_dir = src.parent.relative_to(source_root)
        except ValueError:
            rel_dir = Path(".")
        dest_dir = work_dir / rel_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / plain_model_open_filename(src.name)
        shutil.copy2(src, dest_file)
        mapping[absolute_preserve_drive(src)] = absolute_preserve_drive(dest_file)
    return mapping


def _format_param_value(entry: dict) -> str:
    """Turn a parameter list entry into a single-line display value."""
    if entry.get("encoded"):
        raw = entry.get("value")
        return str(raw) if raw is not None else ""
    val = entry.get("value")
    if val is None:
        return ""
    if isinstance(val, bool):
        return "yes" if val else "no"
    return str(val)


def _model_group_key(folder: Path, path: Path, *, recursive: bool) -> str:
    """Unique key per folder + model name; subfolders are separate when recursive."""
    m = _MODEL_NAME.match(path.name)
    if not m:
        return path.name
    base = f"{m.group('base')}.{m.group('ext').lower()}"
    if not recursive:
        return base
    try:
        rel_parent = path.parent.relative_to(folder)
    except ValueError:
        return base
    if rel_parent.parts:
        return (rel_parent / base).as_posix()
    return base


def iter_latest_model_files(
    folder: Path,
    *,
    recursive: bool = False,
    scan_parts: bool = True,
    scan_assemblies: bool = True,
    scan_drawings: bool = True,
) -> list[Path]:
    """
    For each logical model name (*.prt/*.asm/*.drw with optional .N), pick one file:
    - If only ``name.ext`` exists, it is used.
    - If only versioned files exist (``name.ext.5``, …), the highest number wins.
    - If both ``name.ext`` and versioned files exist, the highest version wins
      (e.g. ``name.ext.10`` over ``name.ext`` and ``name.ext.9``).

    When ``recursive`` is True, scans all subfolders; the same filename in different
    folders is treated as separate models.
    """
    groups: dict[str, list[tuple[int, Path]]] = {}
    if recursive:
        candidates = (p for p in folder.rglob("*") if p.is_file())
    else:
        candidates = (p for p in folder.iterdir() if p.is_file())
    for path in candidates:
        m = _MODEL_NAME.match(path.name)
        if not m:
            continue
        if not model_file_in_scan(
            path.name,
            scan_parts=scan_parts,
            scan_assemblies=scan_assemblies,
            scan_drawings=scan_drawings,
        ):
            continue
        key = _model_group_key(folder, path, recursive=recursive)
        ver_s = m.group("ver")
        # Plain name.prt is rank 0; name.prt.N uses N so the highest wins when both exist.
        rank = int(ver_s) if ver_s is not None else 0
        groups.setdefault(key, []).append((rank, path))

    chosen: list[Path] = []
    for base in sorted(groups.keys(), key=str.lower):
        entries = groups[base]
        entries.sort(key=lambda t: t[0], reverse=True)
        chosen.append(absolute_preserve_drive(entries[0][1]))
    return chosen


def parse_parameter_names(text: str) -> list[str]:
    """Split comma-separated parameter names; spaces after commas are ignored."""
    return [part.strip() for part in text.split(",") if part.strip()]


def parameter_values_in_order(
    paramlist: list[dict], names: list[str]
) -> list[str]:
    """Lookup by parameter name (case-insensitive; Creo treats names as case-insensitive)."""
    by_name: dict[str, str] = {}
    for entry in paramlist:
        pname = entry.get("name")
        if pname:
            by_name[str(pname).casefold()] = _format_param_value(entry)
    return [by_name.get(name.casefold(), "") for name in names]


def ordered_parameter_columns(part_plists: list[list[dict]]) -> list[str]:
    """
    Union of parameter names across parts, in Creo list order (first occurrence wins).

    Case-insensitive deduplication; spelling comes from the first model that defines the name.
    """
    seen: set[str] = set()
    columns: list[str] = []
    for plist in part_plists:
        for entry in plist:
            pname = entry.get("name")
            if not pname:
                continue
            key = str(pname).casefold()
            if key not in seen:
                seen.add(key)
                columns.append(str(pname))
    return columns


_PART_DRAG_TOOLTIP = "Drag this link into Creo to open the model. Click does nothing."


def html_part_file_link(file_uri: str, part_name: str) -> str:
    """Model name as link text; click disabled, drag supplies the file URI to Creo."""
    esc_uri_attr = html.escape(file_uri, quote=True)
    esc_label = html.escape(part_name)
    esc_tip = html.escape(_PART_DRAG_TOOLTIP)
    return (
        f'<a href="javascript:void(0)" class="part-link" draggable="true" '
        f'data-file-uri="{esc_uri_attr}" title="{esc_tip}" '
        f'onclick="return false;">{esc_label}</a>'
    )


def build_html_report(
    *,
    title: str,
    parameter_names: list[str],
    rows: list[dict[str, object]],
    errors: list[str],
) -> str:
    """Build a single HTML page with title, search controls, and parameter table."""
    headers = ["Model name", *parameter_names]
    field_options = ['<option value="all">All fields</option>']
    kind_options = [
        '<option value="all">All models</option>',
        '<option value="part">Parts</option>',
        '<option value="assembly">Assemblies</option>',
        '<option value="drawing">Drawings</option>',
    ]
    for i, h in enumerate(headers):
        field_options.append(
            f'<option value="{i}">{html.escape(h)}</option>'
        )
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body { font-family: Segoe UI, Arial, sans-serif; margin: 1.5rem; }",
        "h1 { font-size: 1.35rem; margin-bottom: 1rem; }",
        ".search-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; "
        "margin-bottom: 1rem; padding: 10px 12px; background: #f0f4f8; border: 1px solid #ccc; "
        "border-radius: 4px; }",
        ".search-bar label { font-weight: 600; }",
        ".search-bar input[type=search] { min-width: 14rem; padding: 4px 8px; }",
        ".search-bar select { padding: 4px 8px; }",
        ".search-bar button { padding: 5px 14px; cursor: pointer; }",
        "#searchStatus { color: #444; font-size: 0.9rem; }",
        "table { border-collapse: collapse; width: 100%; }",
        "th, td { border: 1px solid #bbb; padding: 6px 10px; text-align: left; }",
        "th { background: #e8e8e8; }",
        "th.sortable { cursor: pointer; user-select: none; }",
        "th.sortable:hover { background: #ddd; }",
        "th.sortable .sort-ind { margin-left: 0.35em; font-size: 0.7em; color: #555; }",
        "th.sortable.asc .sort-ind::after { content: '▲'; }",
        "th.sortable.desc .sort-ind::after { content: '▼'; }",
        "tbody tr:nth-child(even) { background: #f8f8f8; }",
        "tbody tr.hidden { display: none; }",
        "tr.error td { background: #fff0f0; color: #800; }",
        "a { color: #0645ad; }",
        "a.part-link { cursor: grab; }",
        "a.part-link:active { cursor: grabbing; }",
        ".errors { margin-top: 1.5rem; color: #800; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{html.escape(title)}</h1>",
        '<div class="search-bar">',
        '<label for="searchInput">Search</label>',
        '<input type="search" id="searchInput" placeholder="Text to find…" autocomplete="off">',
        '<label for="searchField">Field</label>',
        f'<select id="searchField">{"".join(field_options)}</select>',
        '<label for="modelTypeFilter">Model type</label>',
        f'<select id="modelTypeFilter">{"".join(kind_options)}</select>',
        '<button type="button" id="searchBtn">Search</button>',
        '<button type="button" id="searchClearBtn">Clear</button>',
        f'<span id="searchStatus" aria-live="polite">{len(rows)} row(s)</span>',
        "</div>",
        '<table id="reportTable">',
        "<thead><tr>",
    ]
    for i, h in enumerate(headers):
        parts.append(
            f'<th class="sortable" data-col="{i}" scope="col" '
            f'title="Click to sort; click again to reverse">'
            f"{html.escape(h)}<span class=\"sort-ind\"></span></th>"
        )
    parts.append("</tr></thead><tbody>")
    for row in rows:
        err = row.get("error")
        file_path = row["file_path"]
        assert isinstance(file_path, Path)
        part_name = str(row.get("part_name", ""))
        model_kind = str(row.get("model_kind", "part"))
        uri = file_uri_full_path(file_path)
        link = html_part_file_link(uri, part_name)
        if err:
            colspan = max(1, len(parameter_names))
            parts.append(
                f'<tr class="error" data-model-kind="{html.escape(model_kind)}"><td class="part-name">{link}</td>'
                f'<td colspan="{colspan}">{html.escape(str(err))}</td></tr>'
            )
            continue
        values = row.get("values")
        if not isinstance(values, list):
            values = []
        parts.append(f'<tr data-model-kind="{html.escape(model_kind)}">')
        parts.append(f'<td class="part-name">{link}</td>')
        for val in values:
            parts.append(f"<td>{html.escape(str(val))}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    if errors:
        parts.append('<div class="errors"><p><strong>Errors</strong></p><ul>')
        for msg in errors:
            parts.append(f"<li>{html.escape(msg)}</li>")
        parts.append("</ul></div>")
    parts.extend(
        [
            "<script>",
            "(function () {",
            "  const STORAGE_KEY = 'creoParamExtractor.searchField';",
            "  const table = document.getElementById('reportTable');",
            "  const thead = table.querySelector('thead');",
            "  const tbody = table.querySelector('tbody');",
            "  const fieldSelect = document.getElementById('searchField');",
            "  const searchInput = document.getElementById('searchInput');",
            "  const searchBtn = document.getElementById('searchBtn');",
            "  const searchClearBtn = document.getElementById('searchClearBtn');",
            "  const modelTypeFilter = document.getElementById('modelTypeFilter');",
            "  const status = document.getElementById('searchStatus');",
            "  let sortCol = -1;",
            "  let sortAsc = true;",
            "  const saved = localStorage.getItem(STORAGE_KEY);",
            "  if (saved && Array.from(fieldSelect.options).some(function (o) {",
            "    return o.value === saved;",
            "  })) {",
            "    fieldSelect.value = saved;",
            "  }",
            "  fieldSelect.addEventListener('change', function () {",
            "    localStorage.setItem(STORAGE_KEY, fieldSelect.value);",
            "  });",
            "  function cellText(td) {",
            "    return (td ? td.textContent : '').trim();",
            "  }",
            "  function sortKey(tr, colIdx) {",
            "    const cells = tr.querySelectorAll('td');",
            "    if (colIdx === 0) return cellText(cells[0]);",
            "    if (cells[colIdx]) return cellText(cells[colIdx]);",
            "    if (tr.classList.contains('error') && cells[1]) return cellText(cells[1]);",
            "    return '';",
            "  }",
            "  function compareRows(ra, rb) {",
            "    const a = sortKey(ra, sortCol);",
            "    const b = sortKey(rb, sortCol);",
            "    const aN = Number(a), bN = Number(b);",
            "    let cmp;",
            "    if (a !== '' && b !== '' && !isNaN(aN) && !isNaN(bN)) {",
            "      cmp = aN - bN;",
            "    } else {",
            "      cmp = a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true });",
            "    }",
            "    return sortAsc ? cmp : -cmp;",
            "  }",
            "  function updateSortHeaders() {",
            "    thead.querySelectorAll('th.sortable').forEach(function (th) {",
            "      const col = parseInt(th.getAttribute('data-col'), 10);",
            "      th.classList.remove('asc', 'desc');",
            "      if (col === sortCol) th.classList.add(sortAsc ? 'asc' : 'desc');",
            "    });",
            "  }",
            "  function applySort() {",
            "    if (sortCol < 0) return;",
            "    const rows = Array.from(tbody.querySelectorAll('tr'));",
            "    rows.sort(compareRows);",
            "    rows.forEach(function (tr) { tbody.appendChild(tr); });",
            "  }",
            "  function toggleSort(colIdx) {",
            "    if (sortCol === colIdx) sortAsc = !sortAsc;",
            "    else { sortCol = colIdx; sortAsc = true; }",
            "    updateSortHeaders();",
            "    applySort();",
            "  }",
            "  thead.querySelectorAll('th.sortable').forEach(function (th) {",
            "    th.addEventListener('click', function () {",
            "      toggleSort(parseInt(th.getAttribute('data-col'), 10));",
            "    });",
            "  });",
            "  function syncSearchButton() {",
            "    const hasQuery = searchInput.value.trim() !== '';",
            "    const hasModelFilter = modelTypeFilter.value !== 'all';",
            "    searchBtn.disabled = !hasQuery;",
            "    searchClearBtn.disabled = !(hasQuery || hasModelFilter);",
            "  }",
            "  function runSearch() {",
            "    const q = searchInput.value.trim().toLowerCase();",
            "    const field = fieldSelect.value;",
            "    const kind = modelTypeFilter.value;",
            "    let visible = 0;",
            "    let total = 0;",
            "    tbody.querySelectorAll('tr').forEach(function (tr) {",
            "      total += 1;",
            "      const rowKind = tr.getAttribute('data-model-kind') || 'part';",
            "      const cells = tr.querySelectorAll('td');",
            "      let match = (kind === 'all' || rowKind === kind);",
            "      if (q) {",
            "        if (field === 'all') {",
            "          match = Array.from(cells).some(function (td) {",
            "            return cellText(td).toLowerCase().indexOf(q) !== -1;",
            "          });",
            "        } else {",
            "          const idx = parseInt(field, 10);",
            "          const td = cells[idx];",
            "          match = td && cellText(td).toLowerCase().indexOf(q) !== -1;",
            "        }",
            "      }",
            "      tr.classList.toggle('hidden', !match);",
            "      if (match) visible += 1;",
            "    });",
            "    status.textContent = (q || kind !== 'all') ? (visible + ' of ' + total + ' row(s)') : (total + ' row(s)');",
            "  }",
            "  function clearSearch() {",
            "    searchInput.value = '';",
            "    fieldSelect.value = 'all';",
            "    localStorage.setItem(STORAGE_KEY, 'all');",
            "    modelTypeFilter.value = 'all';",
            "    syncSearchButton();",
            "    runSearch();",
            "  }",
            "  searchBtn.addEventListener('click', runSearch);",
            "  searchClearBtn.addEventListener('click', clearSearch);",
            "  modelTypeFilter.addEventListener('change', function () {",
            "    syncSearchButton();",
            "    runSearch();",
            "  });",
            "  searchInput.addEventListener('input', syncSearchButton);",
            "  searchInput.addEventListener('keydown', function (e) {",
            "    if (e.key === 'Enter' && !searchBtn.disabled) runSearch();",
            "  });",
            "  syncSearchButton();",
            "  document.querySelectorAll('a.part-link').forEach(function (a) {",
            "    a.addEventListener('dragstart', function (e) {",
            "      var url = a.getAttribute('data-file-uri');",
            "      if (!url || !e.dataTransfer) return;",
            "      e.dataTransfer.setData('text/uri-list', url);",
            "      e.dataTransfer.setData('text/plain', url);",
            "      e.dataTransfer.effectAllowed = 'copy';",
            "    });",
            "  });",
            "  runSearch();",
            "})();",
            "</script>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(parts)


def _check_tcp(host: str, port: int, *, timeout: float = 5.0) -> None:
    """Fail fast if nothing is accepting connections on the CREOSON port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        raise ConnectionError(
            f"Cannot reach CREOSON at {host}:{port} ({e}). "
            "Confirm the server is started and the host/port match."
        ) from e


class TimeoutClient(creopyson.Client):
    """Same as creopyson Client, but HTTP requests use timeouts (default creopyson has none)."""

    def __init__(
        self,
        ip_adress: str = "localhost",
        port: int = 9056,
        *,
        connect_timeout: float = _DEFAULT_CONNECT,
        read_timeout: float = _DEFAULT_READ,
        connection_read_timeout: float = _CONNECTION_READ,
    ) -> None:
        super().__init__(ip_adress, port)
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._connection_read_timeout = float(connection_read_timeout)

    def _http_timeout_tuple(self, command: str, function: str) -> tuple[float, float]:
        """Shorter read timeout for session handshake; long read for file/model work."""
        read = self._read_timeout
        if command == "connection" and function in (
            "connect",
            "disconnect",
            "is_creo_running",
        ):
            read = self._connection_read_timeout
        return (self._connect_timeout, read)

    def _creoson_post(self, command, function, data=None, key_data=None):
        request = {
            "sessionId": self.sessionId,
            "command": command,
            "function": function,
            "data": data,
        }
        timeout = self._http_timeout_tuple(command, function)
        lg.debug("request: %s", str(request))
        try:
            r = requests.post(
                self.server,
                data=json.dumps(request),
                timeout=timeout,
            )
        except requests.exceptions.Timeout as e:
            raise ConnectionError(
                f"CREOSON timed out after {timeout[1]:.0f}s on {command}:{function}. "
                "Start Creo Parametric first and ensure CREOSON is linked to it (J-Link). "
                "Dismiss any modal dialogs in Creo."
            ) from e
        except requests.exceptions.RequestException as e:
            raise ConnectionError(e) from e

        if r.status_code != 200:
            raise ConnectionError(f"Status code: {r.status_code}")

        try:
            json_result = r.json()
        except TypeError as e:
            raise ErrorJsonDecode("Cannot decode JSON, creoson result invalid.") from e

        lg.debug("response: %s", str(json_result))

        if "status" not in json_result:
            raise MissingKey("Missing `status` in creoson result.")
        if "error" not in json_result["status"]:
            raise MissingKey("Missing `error` in status creoson result.")

        status = json_result["status"]["error"]
        if status:
            error_msg = json_result["status"]["message"]
            raise RuntimeError(error_msg)

        if request["command"] == "connection" and request["function"] == "connect":
            if "sessionId" not in json_result:
                raise MissingKey("Missing `sessionId` in creoson result.")
            return json_result["sessionId"]

        if key_data is not None:
            if "data" not in json_result:
                raise MissingKey("Missing `data` in creoson return")
            if key_data not in json_result["data"]:
                raise MissingKey(f"Missing `{key_data}` in creoson result")
            return json_result["data"][key_data]

        return json_result.get("data", None)


def _erase_from_session(
    client: creopyson.Client, *, in_session: str, open_name: str
) -> None:
    """Best-effort erase so Creo does not keep the model in memory after a part finishes."""
    for name in (in_session, open_name):
        if not name:
            continue
        try:
            client.file_erase(file_=name)
            return
        except Exception:
            pass


def _session_cleanup_end_of_run(client: creopyson.Client, log: Callable[[str], None]) -> None:
    """Clear non-displayed models from session at the end of a run."""
    try:
        if hasattr(client, "file_erase_not_displayed"):
            client.file_erase_not_displayed()
        else:
            # Compatibility fallback: call CREOSON directly if helper is unavailable.
            client._creoson_post("file", "erase_not_displayed")
        log("Erased non-displayed models from Creo session.")
    except Exception as ex:  # noqa: BLE001
        log(f"Could not erase non-displayed models: {ex}")


def extract_all(
    folder: Path,
    host: str,
    port: int,
    *,
    output_path: Path,
    parameter_names: list[str],
    report_title: str = "Creo Parameter Extractor Report",
    recursive: bool = False,
    scan_parts: bool = True,
    scan_assemblies: bool = True,
    scan_drawings: bool = True,
    cancel_event: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, list[str], bool]:
    """
    Connect to CREOSON, open each latest model, list parameters, erase from session.

    Models are copied to a local per-run mirror under the user profile; Creo/CREOSON
    only see that local path (not the models folder on a share).

    If ``parameter_names`` is empty, every parameter found on any processed model is
    included (union of names, columns in Creo list order).

    Returns:
        (html_report, list of error lines, cancelled_by_user)
    """
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    folder = absolute_preserve_drive(folder)
    stopped = False
    errors: list[str] = []
    pending: list[dict[str, object]] = []
    extract_all_params = not parameter_names
    if extract_all_params:
        log("Parameters blank — will include all parameters from each model.")
    log(f"Checking TCP {host}:{port} …")
    _check_tcp(host, port)
    log(
        "Connecting to CREOSON (JSON session: connection: connect) … "
        f"(waits at most ~{_CONNECTION_READ:.0f}s if Creo/J-Link does not answer)"
    )
    client = TimeoutClient(host, port)
    client.connect()
    work_dir = extract_work_dir()
    work_dir_s = str(work_dir)
    try:
        try:
            running = client.is_creo_running()
            log(f"Creo running (per CREOSON): {running}")
            if not running:
                log(
                    "Warning: CREOSON reports Creo is not running. "
                    "Start Creo Parametric manually, then retry."
                )
        except Exception as ex:  # noqa: BLE001
            log(f"Could not query is_creo_running: {ex}")

        log(f"Local mirror folder (for Creo/CREOSON):\n  {work_dir_s}")
        log(f"Models source folder:\n  {folder}")
        local_model_map = build_local_model_mirror(folder, work_dir, recursive=recursive)
        mirrored_count = len(local_model_map)
        model_paths = [
            p
            for p in local_model_map.keys()
            if model_file_in_scan(
                p.name,
                scan_parts=scan_parts,
                scan_assemblies=scan_assemblies,
                scan_drawings=scan_drawings,
            )
        ]
        model_paths.sort(key=lambda p: (model_kind_priority(p.name), p.name.casefold()))
        if recursive:
            log("Recursive search enabled — including model files in subfolders.")
        scan_labels: list[str] = []
        if scan_parts:
            scan_labels.append("parts")
        if scan_assemblies:
            scan_labels.append("assemblies")
        if scan_drawings:
            scan_labels.append("drawings")
        log(
            f"Local mirror: {mirrored_count} model file(s) (all types, for Creo references)."
        )
        log(f"Extracting: {', '.join(scan_labels)}.")
        log("Processing order: parts -> assemblies -> drawings.")
        log(f"Found {len(model_paths)} model file(s) to process (after version filter).")
        if cancelled():
            stopped = True
            log("Stop requested before processing models.")
        for disk_path in model_paths:
            if cancelled():
                stopped = True
                log("Stop requested — no further models will be opened.")
                break
            label = part_display_name(disk_path)
            in_session = ""
            open_name = ""
            aborted_by_stop = False
            try:
                local_path = local_model_map.get(absolute_preserve_drive(disk_path))
                if not local_path:
                    raise FileNotFoundError(
                        f"No local mirrored copy for source model: {disk_path}"
                    )
                open_name = local_path.name
                open_dir = str(absolute_preserve_drive(local_path.parent))
                if not local_path.is_file():
                    raise FileNotFoundError(f"Local mirrored file is missing: {local_path}")
                if cancelled():
                    stopped = True
                    aborted_by_stop = True
                    log(f"Stop before opening {label} — skipped.")
                    break
                log(
                    f"Opening {label} … (local mirror: {open_name}, "
                    f"{local_path.stat().st_size:,} bytes)"
                )
                client.creo_cd(open_dir)
                in_session = open_name

                opened = client.file_open(
                    open_name,
                    dirname=open_dir,
                    display=False,
                    activate=False,
                )
                if isinstance(opened, dict):
                    fl = opened.get("files")
                    if isinstance(fl, list) and fl:
                        in_session = fl[0]
                    elif isinstance(fl, str):
                        in_session = fl

                log(f"  In-session name: {in_session} — listing parameters …")
                plist = client.parameter_list(file_=in_session)
                if not plist:
                    plist = []
                log(f"  Got {len(plist)} parameter(s). Erasing from session …")
                pending.append(
                    {
                        "part_name": label,
                        "model_kind": model_kind_from_filename(disk_path.name),
                        "file_path": absolute_preserve_drive(disk_path),
                        "plist": plist,
                        "error": None,
                    }
                )
                _erase_from_session(client, in_session=in_session, open_name=open_name)
            except Exception as ex:  # noqa: BLE001 — surface any Creo/Creoson failure
                log(f"  Error: {ex}")
                if cancelled() or aborted_by_stop:
                    log(f"  Not recording {label} in report (stop in progress).")
                else:
                    errors.append(f"{label}: {ex}")
                    pending.append(
                        {
                            "part_name": label,
                            "model_kind": model_kind_from_filename(disk_path.name),
                            "file_path": absolute_preserve_drive(disk_path),
                            "plist": None,
                            "error": str(ex),
                        }
                    )
                try:
                    if open_name and client.file_open_errors(file_=open_name):
                        log(
                            "  Creo file_open_errors() is true — check the model "
                            "in Creo for regen failures or missing references."
                        )
                except Exception:
                    pass
                _erase_from_session(client, in_session=in_session, open_name=open_name)
    finally:
        _session_cleanup_end_of_run(client, log)
        log("Disconnecting from CREOSON …")
        try:
            client.disconnect()
        except Exception:
            pass
        removed = False
        for attempt in range(1, 6):
            try:
                shutil.rmtree(work_dir)
                log("Removed local mirror folder.")
                removed = True
                break
            except OSError as ex:
                if attempt == 5:
                    log(f"Could not remove local mirror folder {work_dir_s}: {ex}")
                else:
                    # Creo can briefly keep a handle after disconnect; retry shortly.
                    time.sleep(0.25 * attempt)
        if not removed:
            pass

    if parameter_names:
        column_names = parameter_names
    elif pending:
        ok_plists = [
            p["plist"]
            for p in pending
            if p.get("plist") is not None and not p.get("error")
        ]
        column_names = ordered_parameter_columns(
            [pl for pl in ok_plists if isinstance(pl, list)]
        )
        if extract_all_params and column_names:
            log(f"Report columns: {len(column_names)} parameter(s) (union across models).")
    else:
        column_names = []

    table_rows: list[dict[str, object]] = []
    for p in pending:
        if stopped and p.get("error"):
            continue
        err = p.get("error")
        if err:
            table_rows.append(
                {
                    "part_name": p["part_name"],
                    "model_kind": p.get("model_kind", "part"),
                    "file_path": p["file_path"],
                    "values": [""] * len(column_names),
                    "error": str(err),
                }
            )
            continue
        plist = p.get("plist")
        if not isinstance(plist, list):
            plist = []
        table_rows.append(
            {
                "part_name": p["part_name"],
                "model_kind": p.get("model_kind", "part"),
                "file_path": p["file_path"],
                "values": parameter_values_in_order(plist, column_names),
            }
        )

    if not table_rows and not errors:
        report = build_html_report(
            title=report_title,
            parameter_names=column_names,
            rows=[],
            errors=["No model files found in folder (check Scan types and folder path)."],
        )
    else:
        report = build_html_report(
            title=report_title,
            parameter_names=column_names,
            rows=table_rows,
            errors=errors,
        )
    if stopped:
        errors = [*errors, "Extraction stopped by user."]
    return report, errors, stopped


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Creo Parameter Extractor")
        self.geometry("820x560")
        self.minsize(640, 420)

        self.recursive_var = tk.BooleanVar(value=False)
        self.scan_parts_var = tk.BooleanVar(value=True)
        self.scan_assemblies_var = tk.BooleanVar(value=True)
        self.scan_drawings_var = tk.BooleanVar(value=True)
        menubar = tk.Menu(self)
        self.config(menu=menubar)
        scan_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Scan", menu=scan_menu)
        scan_menu.add_checkbutton(
            label="Parts",
            variable=self.scan_parts_var,
            command=lambda: self._on_scan_toggle("parts"),
        )
        scan_menu.add_checkbutton(
            label="Assemblies",
            variable=self.scan_assemblies_var,
            command=lambda: self._on_scan_toggle("assemblies"),
        )
        scan_menu.add_checkbutton(
            label="Drawings",
            variable=self.scan_drawings_var,
            command=lambda: self._on_scan_toggle("drawings"),
        )
        options_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Options", menu=options_menu)
        options_menu.add_checkbutton(
            label="Recursively find files",
            variable=self.recursive_var,
            command=self._save_settings,
        )

        frm = tk.Frame(self, padx=8, pady=8)
        frm.pack(fill=tk.BOTH, expand=True)

        row1 = tk.Frame(frm)
        row1.pack(fill=tk.X, pady=(0, 4))
        tk.Label(row1, text="Models folder", width=14, anchor="w").pack(side=tk.LEFT)
        self.folder_var = tk.StringVar()
        tk.Entry(row1, textvariable=self.folder_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        tk.Button(row1, text="Browse…", command=self._browse_folder).pack(side=tk.LEFT)

        row2 = tk.Frame(frm)
        row2.pack(fill=tk.X, pady=(0, 4))
        tk.Label(row2, text="CREOSON host", width=14, anchor="w").pack(side=tk.LEFT)
        self.host_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.host_var, width=20).pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(row2, text="Port").pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.port_var, width=8).pack(side=tk.LEFT, padx=(4, 0))

        row_params = tk.Frame(frm)
        row_params.pack(fill=tk.X, pady=(0, 2))
        tk.Label(row_params, text="Parameters", width=14, anchor="w").pack(
            side=tk.LEFT
        )
        self.params_var = tk.StringVar()
        tk.Entry(row_params, textvariable=self.params_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        tk.Label(
            frm,
            text="Leave blank to include all parameters. Otherwise list names separated by "
            "commas (spaces after commas are ignored; matching is case-insensitive).",
            wraplength=780,
            justify=tk.LEFT,
            fg="#444",
        ).pack(anchor="w", pady=(0, 4))

        row3 = tk.Frame(frm)
        row3.pack(fill=tk.X, pady=(0, 4))
        tk.Label(row3, text="Output file", width=14, anchor="w").pack(side=tk.LEFT)
        self.out_var = tk.StringVar()
        tk.Entry(row3, textvariable=self.out_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        tk.Button(row3, text="Browse…", command=self._browse_out).pack(side=tk.LEFT)

        tk.Label(
            frm,
            text=(
                "Creo Parametric must be running before you extract. "
                "CREOSON talks to Creo through J-Link; starting only the CREOSON "
                "Java server is not enough."
            ),
            wraplength=780,
            justify=tk.LEFT,
            fg="#444",
        ).pack(anchor="w", pady=(2, 0))

        btn_row = tk.Frame(frm)
        btn_row.pack(fill=tk.X, pady=(4, 6))
        self.run_btn = tk.Button(btn_row, text="Extract parameters", command=self._run)
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.stop_btn = tk.Button(
            btn_row,
            text="Stop",
            command=self._stop_extract,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT)
        self._cancel_event = threading.Event()

        tk.Label(frm, text="Log").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(frm, height=18, wrap=tk.WORD, font=("Consolas", 10))
        self.log.pack(fill=tk.BOTH, expand=True)

        self._apply_loaded_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_loaded_settings(self) -> None:
        s = load_ui_settings(SETTINGS_PATH)
        self.folder_var.set(str(s.get("models_folder", "")))
        self.params_var.set(str(s.get("parameter_names", "")))
        self.out_var.set(str(s.get("output_file", "")))
        self.host_var.set(str(s.get("creoson_host", "localhost")))
        try:
            p = int(s.get("creoson_port", 9056))
        except (TypeError, ValueError):
            p = 9056
        if not (1 <= p <= 65535):
            p = 9056
        self.port_var.set(str(p))
        self.recursive_var.set(bool(s.get("recursive_search", False)))
        self.scan_parts_var.set(bool(s.get("scan_parts", True)))
        self.scan_assemblies_var.set(bool(s.get("scan_assemblies", True)))
        self.scan_drawings_var.set(bool(s.get("scan_drawings", True)))

    def _scan_types_selected(self) -> bool:
        return bool(
            self.scan_parts_var.get()
            or self.scan_assemblies_var.get()
            or self.scan_drawings_var.get()
        )

    def _on_scan_toggle(self, which: str) -> None:
        if self._scan_types_selected():
            self._save_settings()
            return
        messagebox.showwarning(
            "Scan",
            "At least one model type must be selected (Parts, Assemblies, or Drawings).",
        )
        if which == "parts":
            self.scan_parts_var.set(True)
        elif which == "assemblies":
            self.scan_assemblies_var.set(True)
        else:
            self.scan_drawings_var.set(True)

    def _gather_settings(self) -> dict[str, object]:
        port_s = self.port_var.get().strip()
        try:
            pi = int(port_s)
        except ValueError:
            pi = 9056
        if not (1 <= pi <= 65535):
            pi = 9056
        return {
            "models_folder": self.folder_var.get().strip(),
            "parameter_names": self.params_var.get().strip(),
            "output_file": self.out_var.get().strip(),
            "creoson_host": self.host_var.get().strip() or "localhost",
            "creoson_port": pi,
            "recursive_search": bool(self.recursive_var.get()),
            "scan_parts": bool(self.scan_parts_var.get()),
            "scan_assemblies": bool(self.scan_assemblies_var.get()),
            "scan_drawings": bool(self.scan_drawings_var.get()),
        }

    def _save_settings(self) -> bool:
        return save_ui_settings(SETTINGS_PATH, self._gather_settings())

    def _on_close(self) -> None:
        self._save_settings()
        self.destroy()

    def _browse_folder(self) -> None:
        p = filedialog.askdirectory(title="Folder containing .prt/.asm/.drw files")
        if p:
            self.folder_var.set(p)

    def _browse_out(self) -> None:
        p = filedialog.asksaveasfilename(
            title="Save HTML parameter report",
            defaultextension=".html",
            filetypes=[("HTML", "*.html"), ("All files", "*.*")],
        )
        if p:
            self.out_var.set(p)

    def _append_log(self, text: str) -> None:
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.update_idletasks()

    def _log_from_worker(self, msg: str) -> None:
        """Thread-safe: schedule log append on the Tk main thread."""
        self.after(0, lambda m=msg: self._append_log(m))

    def _open_html_report(self, path: Path) -> None:
        resolved = absolute_preserve_drive(path)
        try:
            if sys.platform == "win32":
                os.startfile(resolved)  # noqa: S606 — default app for .html
            else:
                webbrowser.open(resolved.as_uri())
        except OSError as ex:
            messagebox.showerror("Open report", f"Could not open report:\n{ex}")

    def _show_completion_dialog(
        self, outp: Path, errs: list[str], *, stopped: bool = False
    ) -> None:
        resolved = absolute_preserve_drive(outp)
        dlg = tk.Toplevel(self)
        if stopped:
            dlg.title("Stopped")
        elif errs:
            dlg.title("Finished with errors")
        else:
            dlg.title("Done")
        dlg.transient(self)
        dlg.resizable(False, False)

        body = tk.Frame(dlg, padx=16, pady=12)
        body.pack()

        if stopped:
            tk.Label(
                body,
                text="Extraction stopped. Partial report saved.",
                justify=tk.LEFT,
            ).pack(anchor="w")
        elif errs:
            tk.Label(
                body,
                text=f"Report saved with {len(errs)} error(s).",
                justify=tk.LEFT,
            ).pack(anchor="w")
            err_preview = "\n".join(errs[:8])
            if len(errs) > 8:
                err_preview += f"\n… and {len(errs) - 8} more (see log)."
            tk.Label(
                body,
                text=err_preview,
                justify=tk.LEFT,
                fg="#800",
                wraplength=420,
            ).pack(anchor="w", pady=(4, 8))
        elif not stopped:
            tk.Label(body, text="Report saved successfully.", justify=tk.LEFT).pack(
                anchor="w"
            )

        if stopped and errs:
            err_preview = "\n".join(errs[:8])
            if len(errs) > 8:
                err_preview += f"\n… and {len(errs) - 8} more (see log)."
            tk.Label(
                body,
                text=err_preview,
                justify=tk.LEFT,
                fg="#800",
                wraplength=420,
            ).pack(anchor="w", pady=(4, 8))

        tk.Label(
            body,
            text=str(resolved),
            justify=tk.LEFT,
            wraplength=420,
            fg="#333",
        ).pack(anchor="w", pady=(0, 12))

        btn_row = tk.Frame(body)
        btn_row.pack(anchor="e")
        tk.Button(
            btn_row,
            text="Open report",
            default=tk.ACTIVE,
            command=lambda: (self._open_html_report(outp), dlg.destroy()),
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(btn_row, text="Close", command=dlg.destroy).pack(side=tk.LEFT)

        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.grab_set()
        dlg.focus_force()
        dlg.update_idletasks()
        dlg.geometry(f"+{self.winfo_rootx() + 40}+{self.winfo_rooty() + 40}")

    def _set_extract_idle(self) -> None:
        self.run_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self._cancel_event.clear()

    def _stop_extract(self) -> None:
        if not self._cancel_event.is_set():
            self._cancel_event.set()
            self.stop_btn.config(state=tk.DISABLED)
            self._append_log(
                "Stop requested — finishing the current part, then cleaning up …"
            )

    def _run_done(self, outp: Path, errs: list[str]) -> None:
        """UI follow-up after a successful extract (main thread only)."""
        self._set_extract_idle()
        self._append_log(f"Wrote {absolute_preserve_drive(outp)}")
        if errs:
            self._append_log(f"Completed with {len(errs)} error(s).")
        else:
            self._append_log("Done.")
        self._show_completion_dialog(outp, errs)

    def _run_stopped(self, outp: Path, errs: list[str]) -> None:
        """UI follow-up after the user stopped extraction (main thread only)."""
        self._set_extract_idle()
        self._append_log(f"Stopped. Partial report saved to {absolute_preserve_drive(outp)}")
        self._show_completion_dialog(outp, errs, stopped=True)

    def _run_fail(self, err: BaseException) -> None:
        """UI follow-up after extract or save failed (main thread only)."""
        self._set_extract_idle()
        self._append_log(f"Failed: {err}")
        messagebox.showerror("Error", str(err))

    def _run(self) -> None:
        folder = absolute_preserve_drive(Path(self.folder_var.get().strip()))
        if not folder.is_dir():
            messagebox.showerror("Invalid folder", "Choose a valid folder with Creo models.")
            return
        path_warn = models_folder_path_warnings(folder)
        if path_warn and not messagebox.askyesno(
            "Network path warning", f"{path_warn}\n\nContinue anyway?"
        ):
            return
        if not self._scan_types_selected():
            messagebox.showerror(
                "Scan",
                "Select at least one model type under Scan (Parts, Assemblies, or Drawings).",
            )
            return
        param_names = parse_parameter_names(self.params_var.get())
        out_path = self.out_var.get().strip()
        if not out_path:
            messagebox.showerror("Output file", "Choose where to save the HTML report.")
            return
        if not out_path.lower().endswith(".html"):
            out_path = str(Path(out_path).with_suffix(".html"))
            self.out_var.set(out_path)
        outp = absolute_preserve_drive(Path(out_path))
        try:
            outp.parent.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            messagebox.showerror(
                "Output file",
                f"Cannot create output folder:\n{outp.parent}\n\n{ex}",
            )
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Port", "Port must be an integer.")
            return

        if not self._save_settings():
            self._append_log(
                f"Warning: could not save settings to {SETTINGS_PATH} before extract."
            )

        host = self.host_var.get().strip() or "localhost"
        self._cancel_event.clear()
        self.run_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self._append_log("Starting…")
        if path_warn:
            self._append_log("Note: network path warning was acknowledged.")

        def job() -> None:
            try:
                report, errs, stopped = extract_all(
                    folder,
                    host,
                    port,
                    output_path=outp,
                    parameter_names=param_names,
                    recursive=bool(self.recursive_var.get()),
                    scan_parts=bool(self.scan_parts_var.get()),
                    scan_assemblies=bool(self.scan_assemblies_var.get()),
                    scan_drawings=bool(self.scan_drawings_var.get()),
                    cancel_event=self._cancel_event,
                    progress=self._log_from_worker,
                )
                outp.write_text(report, encoding="utf-8")
                # Default-arg binding: exception/loop variables are cleared before
                # Tk runs idle callbacks (Python 3.11+), so closures must not rely
                # on bare `ex` / outer locals without capturing copies.
                if stopped:
                    self.after(0, lambda o=outp, e=errs: self._run_stopped(o, e))
                else:
                    self.after(0, lambda o=outp, e=errs: self._run_done(o, e))
            except Exception as ex:  # noqa: BLE001
                self.after(0, lambda err=ex: self._run_fail(err))

        threading.Thread(target=job, daemon=True).start()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()

