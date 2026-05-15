"""
Batch-extract Creo model parameters from a folder via CREOSON (creopyson).

Requires: Creo Parametric running with CREOSON server, and `pip install -r requirements.txt`.
API reference: https://creoson.com/functions.html

Created by: Michael P. Bourque
Date: 2026-05-14
Version: 1.0.0
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import socket
import sys
import threading
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
SETTINGS_PATH = _APP_DIR / "prt_parameter_extractor_settings.json"


def _settings_defaults() -> dict[str, object]:
    return {
        "models_folder": "",
        "output_file": "",
        "creoson_host": "localhost",
        "creoson_port": 9056,
    }


def _normalize_settings(raw: dict | None) -> dict[str, object]:
    """Merge JSON dict into defaults with type checks (supports older 2-key files)."""
    d = _settings_defaults()
    if not raw:
        return d
    for key in ("models_folder", "output_file", "creoson_host"):
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
    return d


def load_ui_settings(path: Path) -> dict[str, object]:
    """Load UI settings from JSON. Create file with defaults if missing."""
    if not path.exists():
        data = _settings_defaults()
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return dict(data)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_settings_defaults())
    if not isinstance(raw, dict):
        return dict(_settings_defaults())
    return _normalize_settings(raw)


def save_ui_settings(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# HTTP timeouts: "light" requests vs heavy Creo work (open/regen).
_DEFAULT_CONNECT = 15.0
_DEFAULT_READ = 300.0
# `connection: connect` should return quickly if Creo + J-Link are ready.
_CONNECTION_READ = 60.0
_PRT_NAME = re.compile(r"(?i)^(?P<base>.+)\.prt(?:\.(?P<ver>\d+))?$")
# Disk backup like model.prt.7 — Creo/CREOSON "open" only accepts model.prt.
_VERSIONED_PRT_DISK = re.compile(r"(?i)^(?P<stem>.+)\.prt\.(?P<num>\d+)$")


def is_versioned_prt_disk_filename(name: str) -> bool:
    return _VERSIONED_PRT_DISK.match(name) is not None


def plain_prt_open_filename(versioned_disk_name: str) -> str:
    m = _VERSIONED_PRT_DISK.match(versioned_disk_name)
    if not m:
        return versioned_disk_name
    return f"{m.group('stem')}.prt"


def stage_versioned_prt_beside_original(disk_path: Path) -> tuple[str, Path]:
    """
    Copy ``*.prt.N`` next to the original as a uniquely named ``*.prt``.

    Keeps the same directory as the backup so references to other models in
    that folder still resolve (staging under %TEMP% breaks that).
    """
    plain = plain_prt_open_filename(disk_path.name)
    stem = Path(plain).stem
    staged_name = f"{stem}.__cextmp_{secrets.token_hex(4)}.prt"
    staged_path = disk_path.parent / staged_name
    shutil.copy2(disk_path, staged_path)
    return staged_name, staged_path


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


def iter_latest_prt_files(folder: Path) -> list[Path]:
    """
    For each logical model name (*.prt or *.prt.N), pick one file to open:
    - If `name.prt` exists, it is treated as the current revision (wins over numbered).
    - Otherwise choose the path whose numeric suffix is largest (.10 over .9).
    """
    groups: dict[str, list[tuple[int, Path]]] = {}
    for path in folder.iterdir():
        if not path.is_file():
            continue
        m = _PRT_NAME.match(path.name)
        if not m:
            continue
        base = m.group("base")
        ver_s = m.group("ver")
        rank = int(ver_s) if ver_s is not None else 10**9
        groups.setdefault(base, []).append((rank, path))

    chosen: list[Path] = []
    for base in sorted(groups.keys(), key=str.lower):
        entries = groups[base]
        plain = next((p for r, p in entries if r == 10**9), None)
        if plain is not None:
            chosen.append(plain)
        else:
            entries.sort(key=lambda t: t[0], reverse=True)
            chosen.append(entries[0][1])
    return chosen


def format_part_block(
    disk_name: str,
    paramlist: list[dict],
    *,
    creo_session_name: str | None = None,
) -> str:
    """
    One model section for the report: banner with full disk filename (incl. .prt.N),
    then ``Name: value`` for each parameter.
    """
    bar = "=" * 72
    lines: list[str] = [
        bar,
        f"MODEL FILE (disk): {disk_name}",
    ]
    if creo_session_name and creo_session_name != disk_name:
        lines.append(f"Creo session name: {creo_session_name}")
    lines.extend([bar, ""])
    for p in sorted(paramlist, key=lambda x: str(x.get("name", "")).lower()):
        name = p.get("name")
        if not name:
            continue
        lines.append(f"{name}: {_format_param_value(p)}")
    return "\n".join(lines) + "\n"


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


def extract_all(
    folder: Path,
    host: str,
    port: int,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, list[str]]:
    """
    Connect to CREOSON, open each latest .prt, list parameters, erase from session.

    Creo Parametric must be running; CREOSON drives it through J-Link.

    Models are opened with ``display=False`` (non-display mode) for stable batch
    automation; showing each model in a window is not supported reliably here.

    Numbered disk backups (``*.prt.N``) are copied next to the original as a
    uniquely named ``*.prt`` before open (Creo cannot open ``.prt.N`` by name,
    and a copy under %TEMP% breaks references to other files in the folder).

    Returns:
        (full_report_text, list of error lines)
    """
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    errors: list[str] = []
    parts: list[str] = []
    log(f"Checking TCP {host}:{port} …")
    _check_tcp(host, port)
    log(
        "Connecting to CREOSON (JSON session: connection: connect) … "
        f"(waits at most ~{_CONNECTION_READ:.0f}s if Creo/J-Link does not answer)"
    )
    client = TimeoutClient(host, port)
    client.connect()
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

        wd = str(folder.resolve())
        log(f"Setting Creo working directory to:\n  {wd}")
        client.creo_cd(wd)
        prt_paths = iter_latest_prt_files(folder)
        log(f"Found {len(prt_paths)} .prt file(s) to process (after version filter).")
        for disk_path in prt_paths:
            disk_name = disk_path.name
            staged_path: Path | None = None
            open_dir = wd
            open_name = disk_name
            in_session = open_name
            try:
                if is_versioned_prt_disk_filename(disk_name):
                    open_name, staged_path = stage_versioned_prt_beside_original(
                        disk_path
                    )
                    log(
                        f"Opening {disk_name} … "
                        f"(staged as {open_name} in same folder for references)"
                    )
                else:
                    log(f"Opening {disk_name} …")

                opened = client.file_open(
                    open_name,
                    dirname=open_dir,
                    display=False,
                    activate=True,
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
                parts.append(
                    format_part_block(
                        disk_name,
                        plist,
                        creo_session_name=in_session,
                    )
                )
                client.file_erase(file_=in_session)
            except Exception as ex:  # noqa: BLE001 — surface any Creo/Creoson failure
                errors.append(f"{disk_name}: {ex}")
                log(f"  Error: {ex}")
                try:
                    if client.file_open_errors(file_=open_name):
                        log(
                            "  Creo file_open_errors() is true — check the model "
                            "in Creo for regen failures or missing references."
                        )
                except Exception:
                    pass
                try:
                    client.file_erase(file_=in_session)
                except Exception:
                    try:
                        client.file_erase(file_=open_name)
                    except Exception:
                        pass
            finally:
                if staged_path is not None:
                    try:
                        staged_path.unlink(missing_ok=True)
                    except OSError:
                        pass
    finally:
        log("Disconnecting from CREOSON …")
        try:
            client.disconnect()
        except Exception:
            pass

    sep = ("\n" + ("=" * 72) + "\n\n") if len(parts) > 1 else "\n\n"
    report = sep.join(parts) if parts else "(no .prt files found in folder)\n"
    if errors:
        report += "\n--- Errors ---\n" + "\n".join(errors) + "\n"
    return report, errors


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Creo PRT parameter extractor (CREOSON)")
        self.geometry("820x560")
        self.minsize(640, 420)

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
        self.run_btn.pack(side=tk.LEFT)

        tk.Label(frm, text="Log").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(frm, height=18, wrap=tk.WORD, font=("Consolas", 10))
        self.log.pack(fill=tk.BOTH, expand=True)

        self._apply_loaded_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_loaded_settings(self) -> None:
        s = load_ui_settings(SETTINGS_PATH)
        self.folder_var.set(str(s.get("models_folder", "")))
        self.out_var.set(str(s.get("output_file", "")))
        self.host_var.set(str(s.get("creoson_host", "localhost")))
        try:
            p = int(s.get("creoson_port", 9056))
        except (TypeError, ValueError):
            p = 9056
        if not (1 <= p <= 65535):
            p = 9056
        self.port_var.set(str(p))

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
            "output_file": self.out_var.get().strip(),
            "creoson_host": self.host_var.get().strip() or "localhost",
            "creoson_port": pi,
        }

    def _save_settings(self) -> None:
        save_ui_settings(SETTINGS_PATH, self._gather_settings())

    def _on_close(self) -> None:
        try:
            self._save_settings()
        except OSError:
            pass
        self.destroy()

    def _browse_folder(self) -> None:
        p = filedialog.askdirectory(title="Folder containing .prt files")
        if p:
            self.folder_var.set(p)

    def _browse_out(self) -> None:
        p = filedialog.asksaveasfilename(
            title="Save parameter report",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
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

    def _run_done(self, outp: Path, errs: list[str]) -> None:
        """UI follow-up after a successful extract (main thread only)."""
        self.run_btn.config(state=tk.NORMAL)
        self._append_log(f"Wrote {outp.resolve()}")
        if errs:
            self._append_log(f"Completed with {len(errs)} error(s).")
            messagebox.showwarning("Finished with errors", "\n".join(errs[:12]))
        else:
            self._append_log("Done.")
            messagebox.showinfo("Done", f"Report saved to:\n{outp.resolve()}")

    def _run_fail(self, err: BaseException) -> None:
        """UI follow-up after extract or save failed (main thread only)."""
        self.run_btn.config(state=tk.NORMAL)
        self._append_log(f"Failed: {err}")
        messagebox.showerror("Error", str(err))

    def _run(self) -> None:
        folder = Path(self.folder_var.get().strip())
        if not folder.is_dir():
            messagebox.showerror("Invalid folder", "Choose a valid folder with Creo models.")
            return
        out_path = self.out_var.get().strip()
        if not out_path:
            messagebox.showerror("Output file", "Choose where to save the report.")
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Port", "Port must be an integer.")
            return

        host = self.host_var.get().strip() or "localhost"
        self.run_btn.config(state=tk.DISABLED)
        self.log.delete("1.0", tk.END)
        self._append_log("Starting…")

        def job() -> None:
            try:
                report, errs = extract_all(
                    folder,
                    host,
                    port,
                    progress=self._log_from_worker,
                )
                outp = Path(out_path)
                outp.write_text(report, encoding="utf-8")
                # Default-arg binding: exception/loop variables are cleared before
                # Tk runs idle callbacks (Python 3.11+), so closures must not rely
                # on bare `ex` / outer locals without capturing copies.
                self.after(0, lambda o=outp, e=errs: self._run_done(o, e))
            except Exception as ex:  # noqa: BLE001
                self.after(0, lambda err=ex: self._run_fail(err))

        threading.Thread(target=job, daemon=True).start()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
