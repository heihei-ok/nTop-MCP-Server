"""Configuration and executable discovery for nTop MCP."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


DEFAULT_SEARCH_DIRS = [
    Path(r"D:\NTOP"),
    Path(r"C:\Program Files\nTopology\nTopology"),
    Path(r"C:\Program Files\nTop\nTop"),
    Path(r"C:\Program Files\nTopology"),
    Path(r"C:\Program Files\nTop"),
]


def _find_executable(env_var: str, names: list[str], extra_dirs: list[Path] | None = None) -> Optional[Path]:
    """Locate an executable via env var, PATH, or common install directories."""
    env_val = os.environ.get(env_var)
    if env_val:
        p = Path(env_val)
        if p.is_file():
            return p

    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    dirs = list(extra_dirs or []) + DEFAULT_SEARCH_DIRS
    for d in dirs:
        if not d.is_dir():
            continue
        for name in names:
            candidate = d / name
            if candidate.is_file():
                return candidate
    return None


@dataclass
class NTopConfig:
    """Runtime configuration for the nTop MCP server."""

    ntopcl_path: Optional[Path] = None
    ntop_gui_path: Optional[Path] = None
    workdir: Path = field(default_factory=lambda: Path(os.environ.get("NTOP_WORKDIR", os.getcwd())).resolve())
    license_path: Optional[str] = field(default_factory=lambda: os.environ.get("NTOP_LICENSE") or None)
    username: Optional[str] = field(default_factory=lambda: os.environ.get("NTOP_USERNAME") or None)
    password: Optional[str] = field(default_factory=lambda: os.environ.get("NTOP_PASSWORD") or None)
    login_proxy: Optional[str] = field(default_factory=lambda: os.environ.get("NTOP_LOGIN_PROXY") or None)
    default_max_threads: Optional[int] = None
    default_timeout: int = int(os.environ.get("NTOP_TIMEOUT", "600"))
    custom_block_libraries: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ntopcl_path is None:
            self.ntopcl_path = _find_executable("NTOP_CL_PATH", ["ntopcl.exe", "ntopcl"])
        else:
            self.ntopcl_path = Path(self.ntopcl_path)

        extra_dirs = [self.ntopcl_path.parent] if self.ntopcl_path else []
        if self.ntop_gui_path is None:
            self.ntop_gui_path = _find_executable("NTOP_GUI_PATH", ["ntop.exe", "ntop"], extra_dirs=extra_dirs)
        else:
            self.ntop_gui_path = Path(self.ntop_gui_path)

        threads_env = os.environ.get("NTOP_MAX_THREADS")
        if self.default_max_threads is None and threads_env:
            try:
                self.default_max_threads = int(threads_env)
            except ValueError:
                pass

    def resolve_path(self, path_str: str) -> Path:
        """Resolve a path relative to the current working directory if not absolute."""
        p = Path(path_str)
        if not p.is_absolute():
            p = self.workdir / p
        return p.resolve()

    def get_ntop_version(self) -> Optional[str]:
        """Query `ntopcl --version` if available."""
        if not self.ntopcl_path or not self.ntopcl_path.is_file():
            return None
        try:
            proc = subprocess.run(
                [str(self.ntopcl_path), "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            out = (proc.stdout or proc.stderr or "").strip()
            return out or None
        except Exception:
            return None

    def to_summary(self) -> dict:
        """Return a safe, serializable summary of environment status."""
        return {
            "ntopcl_path": str(self.ntopcl_path) if self.ntopcl_path else None,
            "ntopcl_available": bool(self.ntopcl_path and self.ntopcl_path.is_file()),
            "ntop_gui_path": str(self.ntop_gui_path) if self.ntop_gui_path else None,
            "ntop_gui_available": bool(self.ntop_gui_path and self.ntop_gui_path.is_file()),
            "ntop_version": self.get_ntop_version(),
            "workdir": str(self.workdir),
            "workdir_exists": self.workdir.is_dir(),
            "license_configured": bool(self.license_path),
            "credentials_configured": bool(self.username and self.password),
            "default_max_threads": self.default_max_threads,
            "default_timeout_seconds": self.default_timeout,
            "custom_block_libraries": dict(self.custom_block_libraries),
        }
