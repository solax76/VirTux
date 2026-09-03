"""Follow the desktop's light/dark preference.

Plain GTK4 does not act on the desktop colour scheme by itself: it only reads
``Gtk.Settings:gtk-application-prefer-dark-theme``, and nothing maps the freedesktop
``color-scheme`` preference onto it. libadwaita is what normally does that mapping,
and VirTux deliberately does not use libadwaita — so on a desktop set to "prefer
dark" the window would be drawn with the light stylesheet, which is how a light
header bar ends up under a dark theme's white label colour.

This module does the same mapping, over the XDG desktop portal when it is
reachable (which is also the only route that works inside a Flatpak or an
AppImage with its own schema directory) and over GSettings otherwise. Every
lookup is best-effort: a desktop that expresses no preference at all is left
entirely alone, so ``settings.ini`` and ``GTK_THEME`` keep the last word.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .config import DARK, LIGHT, SYSTEM  # noqa: E402

# org.freedesktop.portal.Settings, the cross-desktop appearance namespace.
PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Settings"
APPEARANCE = "org.freedesktop.appearance"
COLOR_SCHEME = "color-scheme"
# The portal is on the session bus and answers instantly; if it does not, we are
# better off starting light than blocking the whole startup on it.
PORTAL_TIMEOUT_MS = 1000

# Values of org.freedesktop.appearance color-scheme.
_NO_PREFERENCE = 0
_PREFER_DARK = 1
_PREFER_LIGHT = 2

_GNOME_SCHEMA = "org.gnome.desktop.interface"


def _unwrap(value: object) -> object:
    """Peel GLib.Variant boxes off a portal reply.

    ReadOne hands back the value once boxed; the older Read boxes it twice.
    """
    while isinstance(value, GLib.Variant):
        value = value.unpack()
    return value


class ThemeManager:
    """Keeps GTK's dark-theme flag in step with the desktop, or with the user."""

    def __init__(self) -> None:
        self._preference = SYSTEM
        self._proxy: Gio.DBusProxy | None = None
        self._settings: Gio.Settings | None = None
        # What the toolkit had decided on its own, restored when neither the user
        # nor the desktop expresses a preference.
        settings = Gtk.Settings.get_default()
        self._initial = bool(
            settings.get_property("gtk-application-prefer-dark-theme")
        ) if settings is not None else False

        self._watch_portal()
        if self._proxy is None:
            self._watch_gsettings()
        self._apply()

    # -- public API ---------------------------------------------------------

    def set_preference(self, value: str) -> None:
        """Switch between following the desktop and forcing light or dark."""
        self._preference = value if value in (SYSTEM, LIGHT, DARK) else SYSTEM
        self._apply()

    # -- wiring -------------------------------------------------------------

    def _watch_portal(self) -> None:
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                PORTAL_NAME,
                PORTAL_PATH,
                PORTAL_IFACE,
                None,
            )
        except GLib.Error:
            return
        # Constructing the proxy succeeds even with nobody on the other end, so
        # the first read is what actually tells us whether the portal is there.
        if self._read_portal(proxy) is None:
            return
        proxy.connect("g-signal", self._on_portal_signal)
        self._proxy = proxy

    def _watch_gsettings(self) -> None:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return
        # The AppImage points GSETTINGS_SCHEMA_DIR at its own bundle, which may not
        # carry the GNOME schema at all — looking it up blind would abort.
        schema = source.lookup(_GNOME_SCHEMA, True)
        if schema is None or not schema.has_key(COLOR_SCHEME):
            return
        settings = Gio.Settings.new_full(schema, None, None)
        settings.connect(f"changed::{COLOR_SCHEME}", lambda *_a: self._apply())
        self._settings = settings

    def _on_portal_signal(
        self, _proxy: Gio.DBusProxy, _sender: str, signal: str, params: GLib.Variant
    ) -> None:
        if signal != "SettingChanged":
            return
        unpacked = params.unpack()
        if len(unpacked) >= 2 and unpacked[0] == APPEARANCE and unpacked[1] == COLOR_SCHEME:
            self._apply()

    # -- reading ------------------------------------------------------------

    def _read_portal(self, proxy: Gio.DBusProxy) -> int | None:
        for method in ("ReadOne", "Read"):
            try:
                reply = proxy.call_sync(
                    method,
                    GLib.Variant("(ss)", (APPEARANCE, COLOR_SCHEME)),
                    Gio.DBusCallFlags.NONE,
                    PORTAL_TIMEOUT_MS,
                    None,
                )
            except GLib.Error:
                continue
            value = _unwrap(reply.get_child_value(0))
            if isinstance(value, int):
                return value
        return None

    def _system_prefers_dark(self) -> bool | None:
        """True, False, or None when the desktop states no preference."""
        if self._proxy is not None:
            value = self._read_portal(self._proxy)
            if value == _PREFER_DARK:
                return True
            if value == _PREFER_LIGHT:
                return False
            if value == _NO_PREFERENCE:
                return None
        if self._settings is not None:
            scheme = self._settings.get_string(COLOR_SCHEME)
            if scheme == "prefer-dark":
                return True
            if scheme == "prefer-light":
                return False
        return None

    # -- applying -----------------------------------------------------------

    def _apply(self) -> None:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return
        if self._preference == DARK:
            dark: bool | None = True
        elif self._preference == LIGHT:
            dark = False
        else:
            dark = self._system_prefers_dark()
        if dark is None:
            dark = self._initial
        settings.set_property("gtk-application-prefer-dark-theme", dark)
