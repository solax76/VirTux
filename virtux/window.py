"""The main window: VM sidebar on the left, details and actions on the right."""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Callable
from typing import Any

import gi
import libvirt

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from . import commands as cmds  # noqa: E402
from .backend import DomainDetails, DomainStats, Libvirt, LibvirtError, SnapshotInfo, VMInfo  # noqa: E402
from .config import Config, save as save_config  # noqa: E402
from .dialogs import (  # noqa: E402
    AboutDialog,
    PreferencesDialog,
    ShortcutsDialog,
    SnapshotDialog,
    confirm,
)
from .i18n import _  # noqa: E402
from .widgets import (  # noqa: E402
    InfoGrid,
    SnapItem,
    StatsPane,
    Toast,
    VMItem,
    format_bytes,
    format_kib,
)

# Live stats are sampled on their own faster timer while the page is visible.
STATS_INTERVAL_MS = 1000

_STATE_CSS = {
    libvirt.VIR_DOMAIN_RUNNING: "state-running",
    libvirt.VIR_DOMAIN_PAUSED: "state-paused",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "state-paused",
    libvirt.VIR_DOMAIN_CRASHED: "state-crashed",
}


class VirTuxWindow(Gtk.ApplicationWindow):
    """Two-column libvirt manager."""

    __gtype_name__ = "VirTuxWindow"

    def __init__(self, application: Gtk.Application, backend: Libvirt, config: Config) -> None:
        super().__init__(application=application, title="VirTux", default_width=1080,
                         default_height=720)
        self.backend = backend
        self.config = config

        self._selected: str | None = None
        self._details: DomainDetails | None = None
        self._vm_cache: dict[str, VMInfo] = {}
        self._last_state: int | None = None
        self._syncing = False
        self._busy = False
        self._listing = False
        self._connected = False
        self._refresh_source = 0
        self._stats_source = 0
        self._prev_sample: tuple[int, int] | None = None

        self._build_ui()
        self._register_actions(application)
        self._connect_backend(config.uri)

    # -- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        self.set_titlebar(self._build_header())

        self._paned = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            position=280,
            shrink_start_child=False,
            shrink_end_child=False,
            resize_start_child=False,
        )
        self._paned.set_start_child(self._build_sidebar())
        self._paned.set_end_child(self._build_right_pane())

        self._toast = Toast()
        overlay = Gtk.Overlay(child=self._paned)
        overlay.add_overlay(self._toast)
        self.set_child(overlay)

        self.connect("close-request", self._on_close_request)

    def _build_header(self) -> Gtk.HeaderBar:
        header = Gtk.HeaderBar()

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label="VirTux")
        title.add_css_class("title-label")
        self._subtitle = Gtk.Label(label=self.config.uri)
        self._subtitle.add_css_class("subtitle-label")
        self._subtitle.set_ellipsize(3)  # Pango.EllipsizeMode.END
        title_box.append(title)
        title_box.append(self._subtitle)
        header.set_title_widget(title_box)

        menu = Gio.Menu()
        first = Gio.Menu()
        first.append(_("Preferences"), "win.preferences")
        first.append(_("Keyboard Shortcuts"), "win.shortcuts")
        menu.append_section(None, first)
        second = Gio.Menu()
        second.append(_("About VirTux"), "win.about")
        second.append(_("Quit"), "win.quit")
        menu.append_section(None, second)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_button.set_tooltip_text(_("Main menu"))
        header.pack_start(menu_button)

        self._refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh_button.set_tooltip_text(_("Refresh now"))
        self._refresh_button.connect("clicked", lambda _b: self.refresh_vms())
        header.pack_end(self._refresh_button)
        return header

    def _build_sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("sidebar")

        self._sidebar_heading = Gtk.Label(label=_("Virtual machines"), xalign=0.0)
        self._sidebar_heading.add_css_class("section-heading")
        self._sidebar_heading.set_margin_top(12)
        self._sidebar_heading.set_margin_bottom(6)
        self._sidebar_heading.set_margin_start(12)
        box.append(self._sidebar_heading)

        self._vm_store = Gio.ListStore(item_type=VMItem)
        self._vm_selection = Gtk.SingleSelection(model=self._vm_store, autoselect=False)
        self._vm_selection.connect("selection-changed", self._on_vm_selected)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_vm_row)
        factory.connect("bind", self._bind_vm_row)
        factory.connect("unbind", self._unbind_row)

        self._vm_list = Gtk.ListView(model=self._vm_selection, factory=factory, vexpand=True)
        self._vm_list.add_css_class("navigation-sidebar")
        box.append(Gtk.ScrolledWindow(child=self._vm_list, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER))
        return box

    def _setup_vm_row(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(10)
        row.set_margin_end(10)
        icon = Gtk.Image()
        # A little above the default 16px, so the state reads at a glance.
        icon.set_pixel_size(20)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name = Gtk.Label(xalign=0.0, ellipsize=3)
        state = Gtk.Label(xalign=0.0)
        state.add_css_class("dim-label")
        text.append(name)
        text.append(state)
        row.append(icon)
        row.append(text)
        row.icon, row.name_label, row.state_label = icon, name, state
        row.bindings = []
        item.set_child(row)

    def _bind_vm_row(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        row = item.get_child()
        vm_item = item.get_item()
        flags = GObject.BindingFlags.SYNC_CREATE
        row.bindings = [
            vm_item.bind_property("name", row.name_label, "label", flags),
            vm_item.bind_property("state_text", row.state_label, "label", flags),
            vm_item.bind_property("icon_name", row.icon, "icon-name", flags),
        ]

    @staticmethod
    def _unbind_row(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        row = item.get_child()
        for binding in getattr(row, "bindings", []):
            binding.unbind()
        row.bindings = []

    def _build_right_pane(self) -> Gtk.Widget:
        self._right_stack = Gtk.Stack()

        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        icon.set_pixel_size(72)
        icon.add_css_class("dim-label")
        message = Gtk.Label(label=_("Select a virtual machine"))
        message.add_css_class("dim-label")
        placeholder.append(icon)
        placeholder.append(message)
        self._right_stack.add_named(placeholder, "empty")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._build_vm_header())
        box.append(self._build_toolbar())
        box.append(Gtk.Separator())
        box.append(self._build_pages())
        self._right_stack.add_named(box, "details")
        self._right_stack.set_visible_child_name("empty")
        return self._right_stack

    def _build_vm_header(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_margin_top(14)
        row.set_margin_bottom(10)
        row.set_margin_start(18)
        row.set_margin_end(18)

        self._vm_name = Gtk.Label(xalign=0.0, hexpand=True, ellipsize=3)
        self._vm_name.add_css_class("vm-title")
        self._vm_state = Gtk.Label()
        self._vm_state.add_css_class("state-pill")
        row.append(self._vm_name)
        row.append(self._vm_state)
        return row

    def _build_toolbar(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_start(18)
        outer.set_margin_end(18)
        outer.set_margin_bottom(14)

        self._buttons: dict[str, Gtk.Button] = {}

        primary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        #primary.add_css_class("linked")
        for key in cmds.PRIMARY_ROW:
            primary.append(self._make_action_button(key))
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.append(primary)
        viewer = self._make_action_button("viewer")
        viewer.add_css_class("suggested-action")
        viewer.set_halign(Gtk.Align.END)
        viewer.set_hexpand(True)
        top.append(viewer)
        outer.append(top)

        secondary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for key in cmds.SECONDARY_ROW:
            secondary.append(self._make_action_button(key))
        outer.append(secondary)
        return outer

    def _make_action_button(self, key: str) -> Gtk.Button:
        """Build a toolbar button. 'power' and 'pause' morph with the VM state."""
        command = cmds.BY_ID[self._resolve_id(key)]
        button = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name(command.icon)
        label = Gtk.Label(label=command.label)
        content.append(icon)
        content.append(label)
        button.set_child(content)
        button.icon_widget, button.label_widget = icon, label
        button.set_tooltip_text(command.tooltip or command.label)
        if command.destructive:
            button.add_css_class("destructive-action")
        button.connect("clicked", lambda _b, k=key: self.activate_command(k))
        self._buttons[key] = button
        return button

    def _build_pages(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self._info_grid = InfoGrid()
        self._stack.add_titled(
            Gtk.ScrolledWindow(child=self._info_grid, hscrollbar_policy=Gtk.PolicyType.NEVER),
            "info",
            _("Info"),
        )
        self._stack.add_titled(self._build_snapshots_page(), "snapshots", _("Snapshots"))

        self._stats = StatsPane()
        self._stack.add_titled(
            Gtk.ScrolledWindow(child=self._stats, hscrollbar_policy=Gtk.PolicyType.NEVER),
            "performance",
            _("Performance"),
        )
        self._stack.connect("notify::visible-child-name", self._on_page_changed)

        switcher = Gtk.StackSwitcher(stack=self._stack, halign=Gtk.Align.CENTER)
        switcher.set_margin_top(6)
        switcher.set_margin_bottom(6)
        box.append(switcher)
        box.append(Gtk.Separator())
        box.append(self._stack)
        return box

    def _build_snapshots_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        self._snap_store = Gio.ListStore(item_type=SnapItem)
        self._snap_selection = Gtk.SingleSelection(model=self._snap_store, autoselect=False)
        self._snap_selection.connect("selection-changed", lambda *_a: self._update_snapshot_buttons())

        self._snap_view = Gtk.ColumnView(model=self._snap_selection, vexpand=True)
        self._snap_view.add_css_class("data-table")
        self._snap_view.append_column(self._text_column(_("Name"), "name", expand=True))
        self._snap_view.append_column(self._text_column(_("Created"), "created"))
        self._snap_view.append_column(self._text_column(_("State"), "vm_state"))
        self._snap_view.append_column(self._text_column(_("Current"), "current"))

        scroller = Gtk.ScrolledWindow(child=self._snap_view, vexpand=True)
        scroller.add_css_class("frame-like")
        box.append(scroller)

        self._snap_empty = Gtk.Label(label=_("This machine has no snapshots yet."), xalign=0.0)
        self._snap_empty.add_css_class("dim-label")
        box.append(self._snap_empty)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._snap_new = Gtk.Button(label=_("New snapshot…"))
        self._snap_new.connect("clicked", lambda _b: self.action_new_snapshot())
        self._snap_revert = Gtk.Button(label=_("Revert"))
        self._snap_revert.connect("clicked", lambda _b: self.action_revert_snapshot())
        self._snap_delete = Gtk.Button(label=_("Delete"))
        self._snap_delete.add_css_class("destructive-action")
        self._snap_delete.connect("clicked", lambda _b: self.action_delete_snapshot())
        row.append(self._snap_new)
        row.append(self._snap_revert)
        row.append(self._snap_delete)
        box.append(row)
        return box

    @staticmethod
    def _text_column(title: str, prop: str, expand: bool = False) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_f: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            label = Gtk.Label(xalign=0.0, ellipsize=3, margin_start=6, margin_end=6)
            label.bindings = []
            item.set_child(label)

        def bind(_f: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            label = item.get_child()
            label.bindings = [
                item.get_item().bind_property(prop, label, "label", GObject.BindingFlags.SYNC_CREATE)
            ]

        def unbind(_f: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            label = item.get_child()
            for binding in label.bindings:
                binding.unbind()
            label.bindings = []

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        return column

    # -- actions ------------------------------------------------------------

    def _register_actions(self, application: Gtk.Application) -> None:
        handlers: dict[str, Callable[[], None]] = {
            "refresh": self.refresh_vms,
            "snapshot-new": self.action_new_snapshot,
            "preferences": self.action_preferences,
            "shortcuts": self.action_shortcuts,
            "about": self.action_about,
            "quit": self.close,
        }
        for key in (*cmds.PRIMARY_ROW, *cmds.SECONDARY_ROW, "viewer"):
            handlers[key] = lambda k=key: self.activate_command(k)

        for name, handler in handlers.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=handler: fn())
            self.add_action(action)

        for name, accel in cmds.ACCELS.items():
            application.set_accels_for_action(f"win.{name}", [accel])

    def _resolve_id(self, key: str) -> str:
        """Map a toolbar key to the command it currently represents."""
        vm = self._current_vm()
        if key == "power":
            return cmds.power_id(vm)
        if key == "pause":
            return cmds.pause_id(vm)
        return key

    def _current_vm(self) -> VMInfo | None:
        if self._selected is None:
            return None
        return self._vm_cache.get(self._selected)

    def activate_command(self, key: str) -> None:
        vm = self._current_vm()
        if vm is None:
            self.notify_user(_("No machine selected"), "warning")
            return
        command = cmds.BY_ID[self._resolve_id(key)]
        if self._busy or not command.enabled(vm):
            self.notify_user(
                _("“{action}” is not available for {name} ({state})").format(
                    action=command.label, name=vm.name, state=cmds.state_label(vm)
                ),
                "warning",
            )
            return

        if command.id == "viewer":
            self._launch_viewer(vm.name)
            return

        name = vm.name

        def execute() -> None:
            self._run_operation(command, name)

        if command.confirm_heading:
            confirm(
                self,
                command.confirm_heading.format(name=name),
                command.confirm_body.format(name=name),
                command.label,
                execute,
            )
        else:
            execute()

    def _run_operation(self, command: cmds.Command, name: str) -> None:
        operations: dict[str, Callable[[str], None]] = {
            "start": self.backend.start,
            "restore": self.backend.restore_saved_state,
            "pause": self.backend.pause,
            "resume": self.backend.resume,
            "shutdown": self.backend.shutdown,
            "reboot": self.backend.reboot,
            "force-reboot": self.backend.force_reboot,
            "force-stop": self.backend.force_stop,
            "save-state": self.backend.save_state,
            "discard-saved": self.backend.remove_saved_state,
        }
        operation = operations[command.id]
        self._set_busy(True)

        def done(_result: Any) -> None:
            self._set_busy(False)
            if command.done:
                self.notify_user(command.done.format(name=name), "success")
            self._prev_sample = None
            self.refresh_vms()
            self._load_details(name)

        def failed(error: LibvirtError) -> None:
            self._set_busy(False)
            self.notify_user(str(error), "error")
            self.refresh_vms()

        self._run_async(lambda: operation(name), done, failed)

    def _launch_viewer(self, name: str) -> None:
        """Open the configured viewer as a detached background process."""
        if self._details is not None and not self._details.graphics:
            # virt-viewer would just sit there waiting for a display that
            # this domain does not have.
            self.notify_user(
                _("{name} has no graphical console configured.").format(name=name),
                "warning",
            )
            return
        argv = self.config.viewer_argv()
        executable = shutil.which(argv[0])
        if executable is None:
            self.notify_user(
                _("“{command}” was not found. Set another one in Preferences.").format(
                    command=argv[0]
                ),
                "error",
            )
            return
        try:
            subprocess.Popen(
                [executable, *argv[1:], "--connect", self.backend.uri, name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self.notify_user(_("Cannot start the viewer: {error}").format(error=exc), "error")
        else:
            self.notify_user(_("Opening the viewer for {name}").format(name=name))

    # -- snapshots ----------------------------------------------------------

    def _selected_snapshot(self) -> SnapshotInfo | None:
        item = self._snap_selection.get_selected_item()
        return item.info if item is not None else None

    def action_new_snapshot(self) -> None:
        vm = self._current_vm()
        if vm is None or self._details is None:
            self.notify_user(_("No machine selected"), "warning")
            return
        name = vm.name

        def create(snap_name: str, description: str, external: bool, quiesce: bool) -> None:
            self._set_busy(True)

            def done(_result: Any) -> None:
                self._set_busy(False)
                self.notify_user(
                    _("Snapshot “{snapshot}” created").format(snapshot=snap_name), "success"
                )
                self._load_snapshots(name)

            def failed(error: LibvirtError) -> None:
                self._set_busy(False)
                self.notify_user(str(error), "error")

            self._run_async(
                lambda: self.backend.create_snapshot(
                    name, snap_name, description, external=external, quiesce=quiesce
                ),
                done,
                failed,
            )

        SnapshotDialog(self, self._details, vm.is_active, create).present()

    def action_revert_snapshot(self) -> None:
        vm = self._current_vm()
        snapshot = self._selected_snapshot()
        if vm is None or snapshot is None:
            self.notify_user(_("No snapshot selected"), "warning")
            return
        name, snap_name = vm.name, snapshot.name

        def execute() -> None:
            self._run_snapshot_op(
                lambda: self.backend.revert_snapshot(name, snap_name),
                name,
                _("{name} reverted to “{snapshot}”").format(name=name, snapshot=snap_name),
            )

        confirm(
            self,
            _("Revert “{name}” to snapshot “{snapshot}”?").format(name=name, snapshot=snap_name),
            _("The current disk and memory state is discarded and replaced by the snapshot."),
            _("Revert"),
            execute,
        )

    def action_delete_snapshot(self) -> None:
        vm = self._current_vm()
        snapshot = self._selected_snapshot()
        if vm is None or snapshot is None:
            self.notify_user(_("No snapshot selected"), "warning")
            return
        name, snap_name = vm.name, snapshot.name

        def execute() -> None:
            self._run_snapshot_op(
                lambda: self.backend.delete_snapshot(name, snap_name),
                name,
                _("Snapshot “{snapshot}” deleted").format(snapshot=snap_name),
            )

        confirm(
            self,
            _("Delete snapshot “{snapshot}”?").format(snapshot=snap_name),
            _("The snapshot of “{name}” is removed permanently.").format(name=name),
            _("Delete"),
            execute,
        )

    def _run_snapshot_op(self, operation: Callable[[], None], name: str, message: str) -> None:
        self._set_busy(True)

        def done(_result: Any) -> None:
            self._set_busy(False)
            self.notify_user(message, "success")
            self._load_snapshots(name)
            self.refresh_vms()

        def failed(error: LibvirtError) -> None:
            self._set_busy(False)
            self.notify_user(str(error), "error")
            self._load_snapshots(name)

        self._run_async(operation, done, failed)

    # -- dialogs ------------------------------------------------------------

    def action_preferences(self) -> None:
        PreferencesDialog(self, self.config, self._apply_config).present()

    def action_shortcuts(self) -> None:
        ShortcutsDialog(self).present()

    def action_about(self) -> None:
        AboutDialog(self, self.backend.uri).present()

    def _apply_config(self, config: Config) -> None:
        reconnect = config.uri != self.config.uri
        interval_changed = config.refresh_interval != self.config.refresh_interval
        self.config = config
        try:
            save_config(config)
        except OSError as exc:
            self.notify_user(_("Cannot save preferences: {error}").format(error=exc), "error")

        if reconnect:
            self._selected = None
            self._details = None
            self._vm_store.remove_all()
            self._connect_backend(config.uri)
        elif interval_changed:
            self._schedule_refresh()
        if not reconnect:
            self.notify_user(_("Preferences saved"), "success")

    # -- connection ---------------------------------------------------------

    def _connect_backend(self, uri: str) -> None:
        self._subtitle.set_text(uri)

        def done(_result: Any) -> None:
            self._connected = True
            self.notify_user(_("Connected to {uri}").format(uri=uri), "success")
            self._schedule_refresh()
            self.refresh_vms()
            self._run_async(self.backend.host_summary, self._subtitle.set_text, lambda _e: None)

        def failed(error: LibvirtError) -> None:
            self._connected = False
            self.notify_user(str(error), "error")
            # Keep retrying on the normal cadence.
            self._schedule_refresh()

        self._run_async(lambda: self.backend.connect(uri), done, failed)

    # -- refresh ------------------------------------------------------------

    def _schedule_refresh(self) -> None:
        if self._refresh_source:
            GLib.source_remove(self._refresh_source)
        self._refresh_source = GLib.timeout_add_seconds(
            max(1, self.config.refresh_interval), self._on_refresh_tick
        )

    def _on_refresh_tick(self) -> bool:
        if not self._connected:
            self._reconnect_quietly()
        else:
            self.refresh_vms()
        return GLib.SOURCE_CONTINUE

    def _reconnect_quietly(self) -> None:
        """Retry a dropped connection without spamming the toast."""
        if self._listing:
            return
        self._listing = True
        uri = self.config.uri

        def done(_result: Any) -> None:
            self._listing = False
            self._connected = True
            self.notify_user(_("Reconnected to {uri}").format(uri=uri), "success")
            self.refresh_vms()

        def failed(_error: LibvirtError) -> None:
            self._listing = False

        self._run_async(lambda: self.backend.connect(uri), done, failed)

    def refresh_vms(self) -> None:
        if self._listing or not self._connected:
            return
        self._listing = True

        def done(vms: list[VMInfo]) -> None:
            self._listing = False
            self._apply_vm_list(vms)

        def failed(error: LibvirtError) -> None:
            self._listing = False
            self._connected = self.backend.is_alive()
            self.notify_user(str(error), "error")

        self._run_async(self.backend.list_domains, done, failed)

    def _apply_vm_list(self, vms: list[VMInfo]) -> None:
        self._vm_cache = {vm.name: vm for vm in vms}
        self._sync_store(vms)
        self._sidebar_heading.set_text(
            _("Virtual machines ({count})").format(count=len(vms))
        )

        if not vms:
            self._selected = None
            self._details = None
            self._right_stack.set_visible_child_name("empty")
            return

        names = [vm.name for vm in vms]
        target = self._selected if self._selected in names else names[0]
        if target != self._selected:
            self._select_by_name(target)
        else:
            self._select_by_name(target, notify=False)
            self._on_state_maybe_changed()

    def _sync_store(self, vms: list[VMInfo]) -> None:
        """Update the store in place so the ListView keeps selection and scroll."""
        self._syncing = True
        try:
            existing = {
                self._vm_store.get_item(i).props.name: i
                for i in range(self._vm_store.get_n_items())
            }
            wanted = {vm.name for vm in vms}

            for name, index in sorted(existing.items(), key=lambda kv: kv[1], reverse=True):
                if name not in wanted:
                    self._vm_store.remove(index)

            for position, vm in enumerate(vms):
                item = (
                    self._vm_store.get_item(position)
                    if position < self._vm_store.get_n_items()
                    else None
                )
                if item is not None and item.props.name == vm.name:
                    item.update(vm)
                    continue
                # Either a new VM, or one that moved: find and relocate it.
                moved = None
                for index in range(position, self._vm_store.get_n_items()):
                    if self._vm_store.get_item(index).props.name == vm.name:
                        moved = self._vm_store.get_item(index)
                        self._vm_store.remove(index)
                        break
                if moved is None:
                    self._vm_store.insert(position, VMItem(vm))
                else:
                    moved.update(vm)
                    self._vm_store.insert(position, moved)
        finally:
            self._syncing = False

    def _select_by_name(self, name: str, notify: bool = True) -> None:
        for index in range(self._vm_store.get_n_items()):
            if self._vm_store.get_item(index).props.name == name:
                if self._vm_selection.get_selected() != index:
                    self._syncing = True
                    self._vm_selection.set_selected(index)
                    self._syncing = False
                break
        if notify and name != self._selected:
            self._activate_vm(name)

    def _on_vm_selected(self, *_args: Any) -> None:
        if self._syncing:
            return
        item = self._vm_selection.get_selected_item()
        if item is None:
            return
        if item.props.name != self._selected:
            self._activate_vm(item.props.name)

    def _activate_vm(self, name: str) -> None:
        self._selected = name
        self._details = None
        self._last_state = None
        self._prev_sample = None
        self._stats.reset(_("Loading…"))
        self._right_stack.set_visible_child_name("details")
        self._update_vm_header()
        self._load_details(name)
        self._load_snapshots(name)
        self._restart_stats_timer()

    def _on_state_maybe_changed(self) -> None:
        """Reload details when the selected VM changed state."""
        vm = self._current_vm()
        if vm is None:
            return
        self._update_vm_header()
        if vm.state != self._last_state:
            self._last_state = vm.state
            self._prev_sample = None
            if self._selected:
                self._load_details(self._selected)
                self._load_snapshots(self._selected)
            self._restart_stats_timer()

    # -- detail loading -----------------------------------------------------

    def _load_details(self, name: str) -> None:
        def done(details: DomainDetails) -> None:
            if name != self._selected:
                return
            self._details = details
            self._populate_info(details)
            self._update_actions()

        def failed(error: LibvirtError) -> None:
            if name == self._selected:
                self.notify_user(str(error), "error")

        self._run_async(lambda: self.backend.domain_details(name), done, failed)

    def _load_snapshots(self, name: str) -> None:
        def done(snapshots: list[SnapshotInfo]) -> None:
            if name != self._selected:
                return
            self._populate_snapshots(snapshots)

        def failed(error: LibvirtError) -> None:
            if name == self._selected:
                self._populate_snapshots([])
                self.notify_user(str(error), "error")

        self._run_async(lambda: self.backend.list_snapshots(name), done, failed)

    def _populate_snapshots(self, snapshots: list[SnapshotInfo]) -> None:
        previous = self._selected_snapshot()
        previous_name = previous.name if previous else None
        self._snap_store.remove_all()
        for snapshot in snapshots:
            self._snap_store.append(SnapItem(snapshot))
        if snapshots:
            index = next(
                (i for i, s in enumerate(snapshots) if s.name == previous_name), 0
            )
            self._snap_selection.set_selected(index)
        self._snap_view.set_visible(bool(snapshots))
        self._snap_empty.set_visible(not snapshots)
        self._update_snapshot_buttons()

    def _populate_info(self, details: DomainDetails) -> None:
        grid = self._info_grid
        grid.clear()

        grid.add_section(_("General"))
        grid.add_row(_("Name"), details.name)
        grid.add_row(_("UUID"), details.uuid)
        grid.add_row(_("Architecture"), details.arch)
        grid.add_row(_("Machine type"), details.machine)
        firmware = details.firmware
        if details.nvram_format:
            firmware = _("{firmware}, NVRAM {format}").format(
                firmware=details.firmware, format=details.nvram_format
            )
        grid.add_row(_("Firmware"), firmware)
        grid.add_row(_("Autostart"), _("yes") if details.autostart else _("no"))
        grid.add_row(_("Persistent"), _("yes") if details.persistent else _("no"))
        grid.add_row(
            _("Saved state"),
            _("present") if details.has_saved else _("none"),
        )

        grid.add_section(_("CPU and memory"))
        grid.add_row(_("Virtual CPUs"), f"{details.vcpus} ({details.vcpu_placement})")
        grid.add_row(_("Maximum memory"), format_kib(details.max_memory_kib))
        grid.add_row(_("Current allocation"), format_kib(details.current_memory_kib))

        grid.add_section(_("Storage"))
        if not details.disks:
            grid.add_placeholder(_("No disks attached."))
        for disk in details.disks:
            label = _("{target} ({device}, {bus})").format(
                target=disk.target, device=disk.device, bus=disk.bus
            )
            size = ""
            if disk.capacity:
                size = _("  —  {allocated} used of {capacity}").format(
                    allocated=format_bytes(disk.allocation),
                    capacity=format_bytes(disk.capacity),
                )
            grid.add_row(label, f"{disk.source}\n{disk.driver_type}{size}")

        grid.add_section(_("Network"))
        if not details.nics:
            grid.add_placeholder(_("No network interfaces."))
        for nic in details.nics:
            grid.add_row(
                nic.mac,
                _("{type} “{source}”, model {model}").format(
                    type=nic.type, source=nic.source, model=nic.model
                ),
            )

        grid.add_section(_("Graphics"))
        if not details.graphics:
            grid.add_placeholder(_("No graphical console configured."))
        for graphics in details.graphics:
            port = graphics.port if graphics.port not in ("-", "") else _("auto")
            grid.add_row(
                graphics.type,
                _("port {port}, listening on {listen}").format(
                    port=port, listen=graphics.listen
                ),
            )

    # -- presentation state -------------------------------------------------

    def _update_vm_header(self) -> None:
        vm = self._current_vm()
        if vm is None:
            return
        self._vm_name.set_text(vm.name)
        label = cmds.state_label(vm)
        if vm.has_saved and not vm.is_active:
            label = _("{state} · state saved").format(state=label)
        self._vm_state.set_text(label)
        for css in set(_STATE_CSS.values()) | {"state-stopped"}:
            self._vm_state.remove_css_class(css)
        self._vm_state.add_css_class(_STATE_CSS.get(vm.state, "state-stopped"))
        self._update_actions()

    def _update_actions(self) -> None:
        vm = self._current_vm()
        for key, button in self._buttons.items():
            command = cmds.BY_ID[self._resolve_id(key)]
            button.label_widget.set_text(command.label)
            button.icon_widget.set_from_icon_name(command.icon)
            button.set_tooltip_text(command.tooltip or command.label)
            if command.destructive:
                button.add_css_class("destructive-action")
            else:
                button.remove_css_class("destructive-action")
            button.set_sensitive(
                vm is not None and not self._busy and command.enabled(vm)
            )
        self._update_snapshot_buttons()

    def _update_snapshot_buttons(self) -> None:
        vm = self._current_vm()
        has_selection = self._snap_selection.get_selected_item() is not None
        available = vm is not None and not self._busy
        self._snap_new.set_sensitive(available and self._details is not None)
        self._snap_revert.set_sensitive(available and has_selection)
        self._snap_delete.set_sensitive(available and has_selection)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.set_sensitive(not busy)
        self._update_actions()

    # -- live stats ---------------------------------------------------------

    def _on_page_changed(self, *_args: Any) -> None:
        self._restart_stats_timer()

    def _restart_stats_timer(self) -> None:
        if self._stats_source:
            GLib.source_remove(self._stats_source)
            self._stats_source = 0

        vm = self._current_vm()
        on_page = self._stack.get_visible_child_name() == "performance"
        if not on_page or vm is None:
            return
        if not vm.is_active:
            self._stats.reset(_("Start the machine to see live statistics."))
            return
        self._stats.reset()
        self._stats_source = GLib.timeout_add(STATS_INTERVAL_MS, self._on_stats_tick)
        self._sample_stats()

    def _on_stats_tick(self) -> bool:
        self._sample_stats()
        return GLib.SOURCE_CONTINUE

    def _sample_stats(self) -> None:
        vm = self._current_vm()
        if vm is None or not vm.is_active:
            return
        name = vm.name
        # Monotonic microseconds, converted to the nanoseconds libvirt reports.
        taken_at = GLib.get_monotonic_time() * 1000

        def done(stats: DomainStats) -> None:
            if name != self._selected:
                return
            percent: float | None = None
            if self._prev_sample is not None:
                previous_time, previous_cpu = self._prev_sample
                elapsed = taken_at - previous_time
                used = stats.cpu_time_ns - previous_cpu
                if elapsed > 0:
                    percent = max(0.0, min(100.0, used / (elapsed * stats.vcpus) * 100.0))
            self._prev_sample = (taken_at, stats.cpu_time_ns)
            self._stats.update(percent, stats.mem_used_kib, stats.mem_max_kib)

        self._run_async(lambda: self.backend.sample_stats(name), done, lambda _e: None)

    # -- infrastructure -----------------------------------------------------

    def notify_user(self, message: str, severity: str = "info") -> None:
        self._toast.show_message(message, severity)

    def _run_async(
        self,
        operation: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[LibvirtError], None] | None = None,
    ) -> None:
        """Run a blocking libvirt call off the GTK thread."""

        def worker() -> None:
            try:
                result = operation()
            except LibvirtError as error:
                handler = on_error or (lambda exc: self.notify_user(str(exc), "error"))
                GLib.idle_add(self._deliver, handler, error)
            except Exception as error:  # noqa: BLE001 - never lose a worker traceback
                GLib.idle_add(
                    self._deliver,
                    lambda exc: self.notify_user(f"{type(exc).__name__}: {exc}", "error"),
                    error,
                )
            else:
                if on_success is not None:
                    GLib.idle_add(self._deliver, on_success, result)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _deliver(callback: Callable[[Any], None], value: Any) -> bool:
        callback(value)
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, *_args: Any) -> bool:
        for source in (self._refresh_source, self._stats_source):
            if source:
                GLib.source_remove(source)
        self._refresh_source = self._stats_source = 0
        self.backend.close()
        return False
