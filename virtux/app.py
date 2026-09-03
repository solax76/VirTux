"""The Gtk.Application: owns the CSS provider and the single main window.

Import this only after calling ``i18n.setup()``, because importing it pulls in the
modules that build translated constants at import time.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, icons  # noqa: E402
from .backend import Libvirt  # noqa: E402
from .config import load as load_config  # noqa: E402
from .theme import ThemeManager  # noqa: E402
from .window import VirTuxWindow  # noqa: E402


class VirTuxApplication(Gtk.Application):
    """Application shell. One window, one libvirt connection."""

    def __init__(self, uri: str | None = None, application_id: str = APP_ID) -> None:
        # Before GTK opens the display, which is when the X11 WM_CLASS is derived:
        # that and the Wayland app_id both come from the program name, which has to
        # match it.dirida.VirTux.desktop for a shell to pair our window with the
        # entry — and therefore with the icon it declares. Running as
        # `python3 -m virtux` would otherwise announce itself as "__main__.py".
        GLib.set_prgname(APP_ID)
        GLib.set_application_name("VirTux")
        super().__init__(
            application_id=application_id, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        self._uri = uri
        self.window: VirTuxWindow | None = None
        self.theme: ThemeManager | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        icons.register()
        self.load_css()
        self.theme = ThemeManager()

    def do_activate(self) -> None:
        if self.window is None:
            config = load_config()
            if self._uri:
                config.uri = self._uri
            # Before the window exists, so it is never drawn with the wrong stylesheet.
            if self.theme is not None:
                self.theme.set_preference(config.color_scheme)
            self.window = VirTuxWindow(self, Libvirt(config.uri), config)
        self.window.present()

    @staticmethod
    def load_css() -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
