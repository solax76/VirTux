"""User configuration, stored as JSON under XDG_CONFIG_HOME."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .backend import DEFAULT_URI

DEFAULT_VIEWER = "virt-viewer --hotkeys=release-cursor=Super_L"
DEFAULT_REFRESH = 2
MIN_REFRESH = 1
MAX_REFRESH = 60

# Colour scheme preference. Kept here rather than in theme.py so that reading the
# configuration never pulls GTK in. CHOICES doubles as the order of the combo box
# in the preferences dialog, so the two cannot drift apart.
SYSTEM = "system"
LIGHT = "light"
DARK = "dark"
CHOICES = (SYSTEM, LIGHT, DARK)


@dataclass
class Config:
    uri: str = DEFAULT_URI
    viewer_command: str = DEFAULT_VIEWER
    refresh_interval: int = DEFAULT_REFRESH
    color_scheme: str = SYSTEM

    def viewer_argv(self) -> list[str]:
        """Split the viewer command so extra flags work, e.g. 'remote-viewer -f'."""
        try:
            argv = shlex.split(self.viewer_command)
        except ValueError:
            argv = []
        return argv or [DEFAULT_VIEWER]


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "virtux"


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> Config:
    """Read the config file, falling back to defaults for anything missing."""
    cfg = Config()
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg

    if isinstance(raw.get("uri"), str) and raw["uri"].strip():
        cfg.uri = raw["uri"].strip()
    if isinstance(raw.get("viewer_command"), str) and raw["viewer_command"].strip():
        cfg.viewer_command = raw["viewer_command"].strip()
    interval = raw.get("refresh_interval")
    if isinstance(interval, int) and not isinstance(interval, bool):
        cfg.refresh_interval = max(MIN_REFRESH, min(MAX_REFRESH, interval))
    if raw.get("color_scheme") in CHOICES:
        cfg.color_scheme = raw["color_scheme"]
    return cfg


def save(cfg: Config) -> None:
    """Write the config atomically so a crash cannot truncate it."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cfg), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, config_path())
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
