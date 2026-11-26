"""Timestamped notebook snapshot/undo manager and Windows nTop GUI process conflict helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


MAX_SNAPSHOTS = 20


def _history_dir(notebook_path: Path) -> Path:
    return notebook_path.parent / ".ntop_history" / notebook_path.stem


def create_snapshot(notebook_path: str | Path, label: str = "edit") -> Optional[Path]:
    """Save a timestamped backup snapshot of `notebook_path` before overwriting."""
    src = Path(notebook_path).resolve()
    if not src.is_file():
        return None
    hdir = _history_dir(src)
    hdir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if (c.isalnum() or c in "-_") else "_" for c in label)[:32] or "edit"
    ts = time.strftime("%Y%m%d_%H%M%S")
    # Avoid collision within the same second
    snap = hdir / f"{ts}_{safe_label}.ntop"
    idx = 1
    while snap.exists():
        snap = hdir / f"{ts}_{idx:02d}_{safe_label}.ntop"
        idx += 1
    shutil.copy2(src, snap)

    # Prune old snapshots beyond retention limit
    all_snaps = sorted(hdir.glob("*.ntop"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in all_snaps[MAX_SNAPSHOTS:]:
        old.unlink(missing_ok=True)
    return snap


def list_snapshots(notebook_path: str | Path) -> List[Dict[str, Any]]:
    """List available snapshots for `notebook_path`, newest first."""
    src = Path(notebook_path).resolve()
    hdir = _history_dir(src)
    if not hdir.is_dir():
        return []
    snaps = sorted(hdir.glob("*.ntop"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "snapshot_name": p.name,
            "path": str(p),
            "size_bytes": p.stat().st_size,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
        }
        for p in snaps
    ]


def restore_snapshot(
    notebook_path: str | Path,
    snapshot_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore a notebook from its latest (or named) snapshot, saving current state first."""
    src = Path(notebook_path).resolve()
    snaps = list_snapshots(src)
    if not snaps:
        raise FileNotFoundError(f"No snapshots found for {src}")

    chosen: Optional[Dict[str, Any]] = None
    if snapshot_name is None:
        chosen = snaps[0]
    else:
        for s in snaps:
            if s["snapshot_name"] == snapshot_name or s["path"] == snapshot_name:
                chosen = s
                break
        if chosen is None:
            raise FileNotFoundError(
                f"Snapshot {snapshot_name!r} not found. Available: {[s['snapshot_name'] for s in snaps]}"
            )

    pre_restore_backup = create_snapshot(src, label="before_undo") if src.is_file() else None
    shutil.copy2(Path(chosen["path"]), src)
    return {
        "restored_to": str(src),
        "from_snapshot": chosen["snapshot_name"],
        "snapshot_created_at": chosen["created_at"],
        "pre_undo_backup": str(pre_restore_backup) if pre_restore_backup else None,
    }


def find_open_ntop_processes(notebook_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """Detect running `ntop.exe` GUI processes (optionally filtered to those opening `notebook_path`)."""
    if os.name != "nt":
        return []
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'ntop.exe'\" "
        "| Select-Object ProcessId, CommandLine, CreationDate "
        "| ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        items = parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        return []

    target_norm = str(Path(notebook_path).resolve()).lower() if notebook_path else None
    results: List[Dict[str, Any]] = []
    for item in items:
        pid = item.get("ProcessId")
        cmdline = item.get("CommandLine") or ""
        if target_norm:
            if target_norm not in cmdline.lower():
                continue
        results.append({"pid": pid, "command_line": cmdline})
    return results


def close_ntop_processes(pids: List[int], wait_seconds: float = 8.0) -> List[int]:
    """Politely terminate specified `ntop.exe` PIDs on Windows and wait until they exit."""
    closed: List[int] = []
    if os.name != "nt" or not pids:
        return closed
    for pid in pids:
        try:
            proc = subprocess.run(
                ["taskkill", "/PID", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                closed.append(int(pid))
        except Exception:
            pass

    if closed and wait_seconds > 0:
        deadline = time.time() + wait_seconds
        remaining = set(closed)
        while remaining and time.time() < deadline:
            still_alive = {
                int(p["pid"])
                for p in find_open_ntop_processes()
                if p.get("pid") is not None
            }
            remaining &= still_alive
            if remaining:
                time.sleep(0.3)
    return closed


def check_unsaved_autosave(notebook_path: str | Path, tolerance_seconds: float = 2.0) -> Dict[str, Any]:
    """Check if nTop's Autosave folder has a newer version of `notebook_path` than the file on disk.

    nTop writes autosaves to `%APPDATA%\\nTopology\\Autosave\\<stem>.<uuid>.ntop`.
    If an autosave file is newer than the saved `.ntop` on disk, the user likely has unsaved edits in GUI.
    """
    src = Path(notebook_path).resolve()
    appdata = os.environ.get("APPDATA")
    if not appdata or not src.is_file():
        return {"has_newer_autosave": False, "autosave_path": None}

    autosave_dir = Path(appdata) / "nTopology" / "Autosave"
    if not autosave_dir.is_dir():
        return {"has_newer_autosave": False, "autosave_path": None}

    disk_mtime = src.stat().st_mtime
    newest_path: Optional[Path] = None
    newest_mtime = 0.0
    prefix = f"{src.stem}."
    for candidate in autosave_dir.glob("*.ntop"):
        if candidate.name.startswith(prefix):
            mt = candidate.stat().st_mtime
            if mt > newest_mtime:
                newest_mtime = mt
                newest_path = candidate

    if newest_path is not None and newest_mtime > disk_mtime + tolerance_seconds:
        return {
            "has_newer_autosave": True,
            "autosave_path": str(newest_path),
            "autosave_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest_mtime)),
            "disk_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(disk_mtime)),
        }
    return {
        "has_newer_autosave": False,
        "autosave_path": str(newest_path) if newest_path else None,
    }


def reload_notebook_in_gui(
    ntop_gui_path: Path,
    notebook_path: Path,
    force_if_unsaved: bool = False,
    open_if_not_running: bool = True,
) -> Dict[str, Any]:
    """Safely close open `ntop.exe` window(s) for `notebook_path` and launch a fresh single instance.

    Refuses to close an existing window if a newer Autosave is detected unless `force_if_unsaved=True`.
    """
    nb = notebook_path.resolve()
    open_procs = find_open_ntop_processes(nb)
    pids = [int(p["pid"]) for p in open_procs if p.get("pid") is not None]

    if pids and not force_if_unsaved:
        autosave_check = check_unsaved_autosave(nb)
        if autosave_check["has_newer_autosave"]:
            return {
                "reloaded": False,
                "blocked_by_unsaved_changes": True,
                "open_pids": pids,
                "autosave": autosave_check,
                "message": (
                    f"Detected a newer nTop Autosave ({autosave_check['autosave_mtime']}) than {nb.name} on disk "
                    f"({autosave_check['disk_mtime']}). Save in nTop GUI (Ctrl+S) first, or pass force_if_unsaved=True."
                ),
            }

    if not pids and not open_if_not_running:
        return {"reloaded": False, "was_open": False, "message": "Notebook was not open in nTop GUI."}

    closed = close_ntop_processes(pids) if pids else []
    proc = subprocess.Popen([str(ntop_gui_path), str(nb)], cwd=str(nb.parent))
    return {
        "reloaded": True,
        "was_open": bool(pids),
        "closed_previous_pids": closed,
        "new_pid": proc.pid,
        "notebook": str(nb),
    }
