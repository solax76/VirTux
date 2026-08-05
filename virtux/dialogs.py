"""Modal windows: confirmations, snapshot creation, preferences, shortcuts, about.

Gtk.Dialog, Gtk.MessageDialog, Gtk.AboutDialog and Gtk.ShortcutsWindow are all
deprecated in current GTK4, so confirmations use Gtk.AlertDialog and the rest are
plain Gtk.Window with a header bar.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # noqa: E402

from . import __version__, icons  # noqa: E402
from .backend import DomainDetails  # noqa: E402
from .commands import ACCELS  # noqa: E402
from .config import MAX_REFRESH, MIN_REFRESH, Config  # noqa: E402
from .i18n import _  # noqa: E402


def confirm(
    parent: Gtk.Window,
    heading: str,
    body: str,
    action_label: str,
    on_confirm: Callable[[], None],
    destructive: bool = True,
) -> None:
    """Ask before doing something irreversible, then call on_confirm()."""
    dialog = Gtk.AlertDialog()
    dialog.set_modal(True)
    dialog.set_message(heading)
    dialog.set_detail(body)
    dialog.set_buttons([_("Cancel"), action_label])
    dialog.set_cancel_button(0)
    dialog.set_default_button(0)
    if destructive:
        # Index of the button that GTK should style/announce as dangerous.
        dialog.set_cancel_button(0)

    def on_choice(dlg: Gtk.AlertDialog, result) -> None:
        try:
            choice = dlg.choose_finish(result)
        except GLib.Error:
            # Dismissed with Escape or the window manager.
            return
        if choice == 1:
            on_confirm()

    dialog.choose(parent, None, on_choice)


def _shell(parent: Gtk.Window, title: str, width: int = 460) -> tuple[Gtk.Window, Gtk.Box]:
    """A modal window with a header bar and a vertical content box."""
    window = Gtk.Window(
        title=title,
        transient_for=parent,
        modal=True,
        default_width=width,
        resizable=False,
    )
    window.set_titlebar(Gtk.HeaderBar())
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        margin_top=18,
        margin_bottom=18,
        margin_start=18,
        margin_end=18,
    )
    window.set_child(box)
    # Escape closes it.
    escape = Gtk.ShortcutController()
    escape.add_shortcut(
        Gtk.Shortcut(
            trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
            action=Gtk.CallbackAction.new(lambda *_a: (window.close(), True)[1]),
        )
    )
    window.add_controller(escape)
    return window, box


def _labelled(box: Gtk.Box, text: str, widget: Gtk.Widget) -> None:
    label = Gtk.Label(label=text, xalign=0.0)
    label.add_css_class("info-key")
    box.append(label)
    box.append(widget)


def _button_row(box: Gtk.Box, cancel_label: str, ok_label: str) -> tuple[Gtk.Button, Gtk.Button]:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
    row.set_margin_top(6)
    cancel = Gtk.Button(label=cancel_label)
    ok = Gtk.Button(label=ok_label)
    ok.add_css_class("suggested-action")
    row.append(cancel)
    row.append(ok)
    box.append(row)
    return cancel, ok


# -- snapshot creation ------------------------------------------------------


class SnapshotDialog:
    """Collects the name, description and mode for a new snapshot.

    Internal snapshots live inside the qcow2 files and include memory when the VM
    is running. libvirt refuses them for a UEFI domain whose NVRAM image is raw,
    so in that case external (disk-only) is preselected and explained.
    """

    def __init__(
        self,
        parent: Gtk.Window,
        details: DomainDetails,
        is_running: bool,
        on_create: Callable[[str, str, bool, bool], None],
    ) -> None:
        self._on_create = on_create
        self.window, box = _shell(parent, _("New snapshot of {name}").format(name=details.name))

        default_name = datetime.now().strftime("snapshot-%Y%m%d-%H%M%S")
        self._name = Gtk.Entry(text=default_name, activates_default=True)
        _labelled(box, _("Name"), self._name)

        self._description = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
            left_margin=6, right_margin=6,
        )
        frame = Gtk.Frame()
        scroller = Gtk.ScrolledWindow(min_content_height=64, child=self._description)
        frame.set_child(scroller)
        _labelled(box, _("Description"), frame)

        internal_ok = details.internal_snapshot_ok
        internal_label = _("Internal — stored inside the disk image")
        if internal_ok and is_running:
            internal_label = _("Internal — full checkpoint including memory")
        self._internal = Gtk.CheckButton(label=internal_label)
        self._external = Gtk.CheckButton(label=_("External — disk-only overlay files"))
        self._external.set_group(self._internal)
        self._internal.set_active(internal_ok)
        self._external.set_active(not internal_ok)
        self._internal.set_sensitive(internal_ok)

        mode_label = Gtk.Label(label=_("Mode"), xalign=0.0)
        mode_label.add_css_class("info-key")
        mode_label.set_margin_top(6)
        box.append(mode_label)
        box.append(self._internal)
        box.append(self._external)

        self._quiesce = Gtk.CheckButton(label=_("Freeze guest filesystems first"))
        # Freezing is a guest-agent operation, so it is pointless without one.
        self._quiesce.set_sensitive(is_running and details.has_guest_agent)
        if is_running and not details.has_guest_agent:
            self._quiesce.set_tooltip_text(
                _("This machine has no qemu-guest-agent channel configured.")
            )
        box.append(self._quiesce)

        hint_parts: list[str] = []
        if details.needs_qcow2_nvram and is_running:
            # Verified on libvirt 12.0 + QEMU 10.2: refused while running, fine when off.
            hint_parts.append(
                _(
                    "While this machine is running, libvirt cannot take an internal "
                    "snapshot: its UEFI NVRAM image is in raw format, and a full "
                    "checkpoint would have to include it. Shut the machine down to "
                    "take an internal snapshot, or use an external one now."
                )
            )
        elif details.needs_qcow2_nvram:
            hint_parts.append(
                _(
                    "An internal snapshot works because the machine is shut off. "
                    "While it is running, only external snapshots are possible."
                )
            )
        elif is_running:
            hint_parts.append(
                _("An internal snapshot of a running machine also stores its memory.")
            )
        hint_parts.append(
            _(
                "External snapshots never include memory, and the overlay files are "
                "written next to the disk image, so QEMU needs write access there."
            )
        )
        hint = Gtk.Label(label="\n\n".join(hint_parts), xalign=0.0, wrap=True)
        hint.add_css_class("dim-label")
        hint.set_margin_top(6)
        box.append(hint)

        cancel, create = _button_row(box, _("Cancel"), _("Create"))
        cancel.connect("clicked", lambda _b: self.window.close())
        create.connect("clicked", self._on_create_clicked)
        self._name.connect("activate", self._on_create_clicked)
        self.window.set_default_widget(create)

    def present(self) -> None:
        self.window.present()
        self._name.grab_focus()
        self._name.select_region(0, -1)

    def _on_create_clicked(self, _widget: Gtk.Widget) -> None:
        name = self._name.get_text().strip()
        if not name:
            self._name.grab_focus()
            return
        buffer = self._description.get_buffer()
        description = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False
        ).strip()
        external = self._external.get_active()
        quiesce = self._quiesce.get_active() and self._quiesce.get_sensitive()
        self.window.close()
        self._on_create(name, description, external, quiesce)


# -- preferences ------------------------------------------------------------


class PreferencesDialog:
    """Edits the viewer command, connection URI and refresh interval."""

    def __init__(
        self,
        parent: Gtk.Window,
        config: Config,
        on_apply: Callable[[Config], None],
    ) -> None:
        self._on_apply = on_apply
        self.window, box = _shell(parent, _("Preferences"), width=520)

        self._uri = Gtk.Entry(text=config.uri, activates_default=True)
        _labelled(box, _("Connection URI"), self._uri)
        uri_hint = Gtk.Label(
            label=_("Changing this reconnects. Example: qemu:///system"),
            xalign=0.0,
            wrap=True,
        )
        uri_hint.add_css_class("dim-label")
        box.append(uri_hint)

        self._viewer = Gtk.Entry(text=config.viewer_command, activates_default=True)
        self._viewer.set_margin_top(6)
        _labelled(box, _("Viewer command"), self._viewer)
        viewer_hint = Gtk.Label(
            label=_(
                "The connection URI and the machine name are appended automatically. "
                "Extra flags are allowed, e.g. “virt-viewer --full-screen”."
            ),
            xalign=0.0,
            wrap=True,
        )
        viewer_hint.add_css_class("dim-label")
        box.append(viewer_hint)

        self._interval = Gtk.SpinButton.new_with_range(MIN_REFRESH, MAX_REFRESH, 1)
        self._interval.set_value(config.refresh_interval)
        self._interval.set_margin_top(6)
        _labelled(box, _("Refresh interval (seconds)"), self._interval)

        cancel, apply_button = _button_row(box, _("Cancel"), _("Apply"))
        cancel.connect("clicked", lambda _b: self.window.close())
        apply_button.connect("clicked", self._on_apply_clicked)
        self.window.set_default_widget(apply_button)

    def present(self) -> None:
        self.window.present()

    def _on_apply_clicked(self, _widget: Gtk.Widget) -> None:
        config = Config(
            uri=self._uri.get_text().strip() or Config().uri,
            viewer_command=self._viewer.get_text().strip() or Config().viewer_command,
            refresh_interval=int(self._interval.get_value()),
        )
        self.window.close()
        self._on_apply(config)


# -- shortcuts and about ----------------------------------------------------


def format_accel(accel: str) -> str:
    """Turn '<Control><Shift>r' into a readable 'Ctrl+Shift+R'."""
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    if not ok:
        return accel
    return Gtk.accelerator_get_label(keyval, mods)


_SHORTCUT_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        _("Machine"),
        [
            ("power", _("Start / restore saved state")),
            ("pause", _("Pause / resume")),
            ("shutdown", _("Shut down")),
            ("reboot", _("Reboot")),
            ("force-reboot", _("Force reboot")),
            ("force-stop", _("Force stop")),
            ("save-state", _("Save state")),
            ("discard-saved", _("Discard saved state")),
            ("viewer", _("Open the viewer")),
        ],
    ),
    (
        _("General"),
        [
            ("snapshot-new", _("New snapshot")),
            ("refresh", _("Refresh now")),
            ("preferences", _("Preferences")),
            ("shortcuts", _("Keyboard shortcuts")),
            ("quit", _("Quit")),
        ],
    ),
]


class ShortcutsDialog:
    """Lists the accelerators. Hand-rolled because Gtk.ShortcutsWindow is deprecated."""

    def __init__(self, parent: Gtk.Window) -> None:
        self.window, box = _shell(parent, _("Keyboard Shortcuts"), width=420)
        for title, entries in _SHORTCUT_GROUPS:
            heading = Gtk.Label(label=title, xalign=0.0)
            heading.add_css_class("section-heading")
            heading.set_margin_top(6)
            box.append(heading)

            grid = Gtk.Grid(column_spacing=18, row_spacing=6)
            for row, (action_id, description) in enumerate(entries):
                accel = ACCELS.get(action_id, "")
                key = Gtk.Label(label=format_accel(accel), xalign=1.0)
                key.add_css_class("accel-label")
                text = Gtk.Label(label=description, xalign=0.0, hexpand=True)
                grid.attach(key, 0, row, 1, 1)
                grid.attach(text, 1, row, 1, 1)
            box.append(grid)

    def present(self) -> None:
        self.window.present()


class AboutDialog:
    """Minimal about window — Gtk.AboutDialog is deprecated."""

    def __init__(self, parent: Gtk.Window, uri: str) -> None:
        self.window, box = _shell(parent, _("About VirTux"), width=380)
        icon = Gtk.Image.new_from_icon_name(icons.NAME)
        icon.set_pixel_size(64)
        box.append(icon)

        title = Gtk.Label(label="VirTux")
        title.add_css_class("about-title")
        box.append(title)

        for text in (
            _("Version {version}").format(version=__version__),
            _("A GTK4 manager for KVM/libvirt virtual machines"),
            _("Connected to {uri}").format(uri=uri),
        ):
            label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER)
            label.add_css_class("dim-label")
            box.append(label)

        close = Gtk.Button(label=_("Close"), halign=Gtk.Align.CENTER)
        close.set_margin_top(6)
        close.connect("clicked", lambda _b: self.window.close())
        box.append(close)

    def present(self) -> None:
        self.window.present()
