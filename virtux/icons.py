"""The application icon: where it lives and how GTK is told about it.

The icon ships as ``data/icons/hicolor/scalable/apps/it.dirida.VirTux.svg`` —
named after the application id, as the icon naming convention wants. An installed
copy under ``/usr/share/icons`` — or the AppImage, whose AppRun puts its own share
directory on ``XDG_DATA_DIRS`` — is found by GTK on its own. A plain checkout is
not, so ``register()`` adds the repository's icon directory to the icon theme
search path before anything asks for the icon by name.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from . import APP_ID  # noqa: E402

NAME = APP_ID

# <repo>/data/icons, next to the virtux package. Absent in installed copies.
_CHECKOUT_ICONS = Path(__file__).resolve().parent.parent / "data" / "icons"


def register() -> None:
    """Make NAME resolvable, and use it for windows that set no icon of their own."""
    display = Gdk.Display.get_default()
    if display is not None and _CHECKOUT_ICONS.is_dir():
        Gtk.IconTheme.get_for_display(display).add_search_path(str(_CHECKOUT_ICONS))
    Gtk.Window.set_default_icon_name(NAME)
