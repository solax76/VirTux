"""Desktop integration: put the entry and the icon where the shell can see them.

A shell pairs a window with a desktop entry by application id, and that pairing is
the only way it can tell which icon belongs to a window: GNOME's compositor does
not implement the xdg-toplevel-icon protocol, so on Wayland an application cannot
hand its icon over itself. An AppImage keeps its entry inside the mounted image
and a checkout keeps it in ``data/``, so in both cases the shell finds no entry and
falls back to a generic icon in the task bar and the window switcher.

``install()`` copies both out to ``~/.local/share`` with ``Exec`` pointing at
whatever is actually running — the AppImage, or the checkout's ``run.sh``. This
module imports no GTK, so the command works without a display.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import APP_ID
from .i18n import _

ENTRY = f"{APP_ID}.desktop"

_ROOT = Path(__file__).resolve().parent.parent


def data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base)


def entry_path() -> Path:
    return data_home() / "applications" / ENTRY


def _sources() -> tuple[Path, Path]:
    """The entry and the icon theme root to copy from: AppImage first, then checkout."""
    candidates = []
    appdir = os.environ.get("APPDIR")
    if appdir:
        share = Path(appdir) / "usr" / "share"
        candidates.append((share / "applications" / ENTRY, share / "icons"))
    candidates.append((_ROOT / "data" / ENTRY, _ROOT / "data" / "icons"))

    for entry, icons in candidates:
        if entry.is_file():
            return entry, icons
    raise FileNotFoundError(
        _("{entry} was not found — this copy of VirTux ships no desktop entry.").format(
            entry=ENTRY
        )
    )


def _exec_value() -> str:
    """What the entry should launch, from where this process was started."""
    # The AppImage runtime exports APPIMAGE as the absolute path of the image.
    target = os.environ.get("APPIMAGE") or ""
    if not target:
        run_script = _ROOT / "run.sh"
        target = str(run_script) if run_script.is_file() else "virtux"
    # Desktop entries split Exec on unquoted spaces.
    return f'"{target}"' if " " in target else target


def install() -> list[Path]:
    """Copy the entry and every icon size into the user's data directory."""
    entry_source, icons_source = _sources()

    lines = [
        f"Exec={_exec_value()}" if line.startswith("Exec=") else line
        for line in entry_source.read_text(encoding="utf-8").splitlines()
    ]
    entry = entry_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written = [entry]

    for icon in sorted(icons_source.glob(f"*/*/apps/{APP_ID}.*")):
        target = data_home() / "icons" / icon.relative_to(icons_source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon, target)
        written.append(target)

    _refresh_caches()
    return written


def uninstall() -> list[Path]:
    """Undo install(). Returns what was actually there to remove."""
    removed = []
    entry = entry_path()
    if entry.is_file():
        entry.unlink()
        removed.append(entry)
    for icon in sorted((data_home() / "icons").glob(f"*/*/apps/{APP_ID}.*")):
        icon.unlink()
        removed.append(icon)
    if removed:
        _refresh_caches()
    return removed


def _refresh_caches() -> None:
    """Nudge the desktop caches. Both are optional — the desktop works without them."""
    for argv in (
        ["update-desktop-database", str(data_home() / "applications")],
        # -t builds a cache even though ~/.local/share/icons/hicolor has no theme
        # index. GTK compares the cache against the directory's mtime, so a later
        # icon added by something else is not hidden by this.
        ["gtk-update-icon-cache", "-qtf", str(data_home() / "icons" / "hicolor")],
    ):
        if shutil.which(argv[0]):
            subprocess.run(
                argv, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
