"""Reusable GTK4 widgets: list items, the toast overlay, the info grid, live stats.

Plain GTK4 only — no libadwaita — so VirTux looks native on GNOME, XFCE and KDE
alike.
"""

from __future__ import annotations

from collections import deque

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, GObject, Gtk  # noqa: E402

from .backend import SnapshotInfo, VMInfo  # noqa: E402
from .commands import state_icon, state_label  # noqa: E402
from .i18n import _  # noqa: E402

# How many samples the sparkline keeps.
HISTORY = 60


# -- formatting helpers -----------------------------------------------------


def format_bytes(value: int | None) -> str:
    """Human-readable size, or '-' when unknown."""
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def format_kib(value: int | None) -> str:
    """Human-readable size from a KiB count, as libvirt reports memory."""
    if value is None:
        return "-"
    return format_bytes(value * 1024)


# -- list model items -------------------------------------------------------


class VMItem(GObject.Object):
    """Sidebar row model. Properties are bound so rows update without a rebuild."""

    __gtype_name__ = "VirtuxVMItem"

    name = GObject.Property(type=str, default="")
    state_text = GObject.Property(type=str, default="")
    icon_name = GObject.Property(type=str, default="")

    def __init__(self, info: VMInfo) -> None:
        super().__init__()
        self.info = info
        self.update(info)

    def update(self, info: VMInfo) -> None:
        """Refresh in place, so the ListView keeps selection and focus."""
        self.info = info
        self.props.name = info.name
        self.props.state_text = state_label(info)
        self.props.icon_name = state_icon(info)


class SnapItem(GObject.Object):
    """One row of the snapshot table."""

    __gtype_name__ = "VirtuxSnapItem"

    name = GObject.Property(type=str, default="")
    created = GObject.Property(type=str, default="")
    vm_state = GObject.Property(type=str, default="")
    current = GObject.Property(type=str, default="")

    def __init__(self, info: SnapshotInfo) -> None:
        super().__init__()
        self.info = info
        self.update(info)

    def update(self, info: SnapshotInfo) -> None:
        self.info = info
        self.props.name = info.name
        self.props.created = info.creation_time
        self.props.vm_state = info.state
        self.props.current = "✓" if info.is_current else ""


# -- toast ------------------------------------------------------------------

_SEVERITY = {
    "info": ("dialog-information-symbolic", "toast-info", 4),
    "success": ("emblem-ok-symbolic", "toast-success", 4),
    "warning": ("dialog-warning-symbolic", "toast-warning", 6),
    "error": ("dialog-error-symbolic", "toast-error", 10),
}


class Toast(Gtk.Revealer):
    """A transient message strip.

    Gtk.InfoBar is deprecated since GTK 4.10, so this is a Revealer holding a
    styled box, placed in the window's Gtk.Overlay.
    """

    __gtype_name__ = "VirtuxToast"

    def __init__(self) -> None:
        super().__init__(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.START,
            margin_top=10,
        )
        self._icon = Gtk.Image()
        self._label = Gtk.Label(wrap=True, max_width_chars=80, xalign=0.0, selectable=True)
        close = Gtk.Button(icon_name="window-close-symbolic", has_frame=False)
        close.set_tooltip_text(_("Dismiss"))
        close.connect("clicked", lambda _btn: self.dismiss())

        self._box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._box.add_css_class("toast")
        self._box.append(self._icon)
        self._box.append(self._label)
        self._box.append(close)
        self.set_child(self._box)
        self._timeout_id = 0

    def show_message(self, text: str, severity: str = "info") -> None:
        icon, css, seconds = _SEVERITY.get(severity, _SEVERITY["info"])
        for _name, klass, _secs in _SEVERITY.values():
            self._box.remove_css_class(klass)
        self._box.add_css_class(css)
        self._icon.set_from_icon_name(icon)
        self._label.set_text(text)
        self.set_reveal_child(True)
        self._arm(seconds)

    def dismiss(self) -> None:
        self._cancel()
        self.set_reveal_child(False)

    def _arm(self, seconds: int) -> None:
        self._cancel()
        self._timeout_id = GLib.timeout_add_seconds(seconds, self._on_timeout)

    def _cancel(self) -> None:
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0

    def _on_timeout(self) -> bool:
        self._timeout_id = 0
        self.set_reveal_child(False)
        return GLib.SOURCE_REMOVE


# -- info grid --------------------------------------------------------------


class InfoGrid(Gtk.Grid):
    """A two-column key/value grid with section headings."""

    __gtype_name__ = "VirtuxInfoGrid"

    def __init__(self) -> None:
        super().__init__(
            column_spacing=18,
            row_spacing=6,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self._row = 0

    def clear(self) -> None:
        child = self.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt
        self._row = 0

    def add_section(self, title: str) -> None:
        label = Gtk.Label(label=title, xalign=0.0)
        label.add_css_class("section-heading")
        if self._row:
            label.set_margin_top(14)
        self.attach(label, 0, self._row, 2, 1)
        self._row += 1

    def add_row(self, key: str, value: str) -> None:
        key_label = Gtk.Label(label=key, xalign=0.0, valign=Gtk.Align.START)
        key_label.add_css_class("info-key")
        value_label = Gtk.Label(
            label=value,
            xalign=0.0,
            selectable=True,
            wrap=True,
            max_width_chars=60,
            hexpand=True,
        )
        value_label.add_css_class("info-value")
        self.attach(key_label, 0, self._row, 1, 1)
        self.attach(value_label, 1, self._row, 1, 1)
        self._row += 1

    def add_placeholder(self, text: str) -> None:
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("dim-label")
        self.attach(label, 0, self._row, 2, 1)
        self._row += 1


# -- live stats -------------------------------------------------------------


class Sparkline(Gtk.DrawingArea):
    """A tiny percentage history plot — no external plotting dependency."""

    __gtype_name__ = "VirtuxSparkline"

    def __init__(self) -> None:
        super().__init__(content_height=64, hexpand=True)
        self._values: deque[float] = deque(maxlen=HISTORY)
        self.set_draw_func(self._draw)

    def push(self, value: float | None) -> None:
        self._values.append(max(0.0, min(100.0, value or 0.0)))
        self.queue_draw()

    def reset(self) -> None:
        self._values.clear()
        self.queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        color = self.get_color()

        # A faint plot area, so the chart reads as a chart even while it is empty.
        radius = 6.0
        cr.new_sub_path()
        cr.arc(width - radius, radius, radius, -1.5708, 0.0)
        cr.arc(width - radius, height - radius, radius, 0.0, 1.5708)
        cr.arc(radius, height - radius, radius, 1.5708, 3.1416)
        cr.arc(radius, radius, radius, 3.1416, 4.7124)
        cr.close_path()
        cr.set_source_rgba(color.red, color.green, color.blue, 0.05)
        cr.fill()

        # Gridlines at 0/50/100%.
        cr.set_source_rgba(color.red, color.green, color.blue, 0.12)
        cr.set_line_width(1.0)
        for frac in (0.0, 0.5, 1.0):
            y = height - 0.5 - frac * (height - 1)
            cr.move_to(0, y)
            cr.line_to(width, y)
        cr.stroke()

        if len(self._values) < 2:
            return

        step = width / (HISTORY - 1)
        points = [
            (i * step, height - (v / 100.0) * (height - 2) - 1)
            for i, v in enumerate(self._values)
        ]
        # Right-align the history so the newest sample sits at the right edge.
        shift = width - points[-1][0]
        points = [(x + shift, y) for x, y in points]

        cr.move_to(points[0][0], height)
        for x, y in points:
            cr.line_to(x, y)
        cr.line_to(points[-1][0], height)
        cr.close_path()
        cr.set_source_rgba(color.red, color.green, color.blue, 0.18)
        cr.fill()

        cr.set_source_rgba(color.red, color.green, color.blue, 0.85)
        cr.set_line_width(1.5)
        cr.move_to(*points[0])
        for x, y in points[1:]:
            cr.line_to(x, y)
        cr.stroke()


class StatsPane(Gtk.Box):
    """CPU and memory usage of the running VM, sampled by the window."""

    __gtype_name__ = "VirtuxStatsPane"

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self._cpu_bar, self._cpu_label = self._add_metric(_("CPU"))
        self._cpu_plot = Sparkline()
        self.append(self._cpu_plot)

        self._mem_bar, self._mem_label = self._add_metric(_("Memory"))
        self._mem_plot = Sparkline()
        self.append(self._mem_plot)

        self._hint = Gtk.Label(xalign=0.0, wrap=True)
        self._hint.add_css_class("dim-label")
        self._hint.set_margin_top(6)
        self.append(self._hint)
        self.reset(_("Start the machine to see live statistics."))

    def _add_metric(self, title: str) -> tuple[Gtk.LevelBar, Gtk.Label]:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=title, xalign=0.0, hexpand=True)
        name.add_css_class("section-heading")
        value = Gtk.Label(label="-", xalign=1.0)
        value.add_css_class("metric-value")
        header.append(name)
        header.append(value)
        self.append(header)

        bar = Gtk.LevelBar(min_value=0.0, max_value=100.0, value=0.0, hexpand=True)
        bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        self.append(bar)
        return bar, value

    def reset(self, hint: str = "") -> None:
        for bar in (self._cpu_bar, self._mem_bar):
            bar.set_value(0.0)
        for label in (self._cpu_label, self._mem_label):
            label.set_text("-")
        self._cpu_plot.reset()
        self._mem_plot.reset()
        self._hint.set_text(hint)
        self._hint.set_visible(bool(hint))

    def update(
        self,
        cpu_percent: float | None,
        mem_used_kib: int | None,
        mem_max_kib: int,
    ) -> None:
        if cpu_percent is None:
            self._cpu_label.set_text(_("sampling…"))
        else:
            self._cpu_bar.set_value(cpu_percent)
            self._cpu_label.set_text(f"{cpu_percent:.1f} %")
            self._cpu_plot.push(cpu_percent)

        if mem_used_kib is None:
            # No balloon driver in the guest: usage is not reportable.
            self._mem_label.set_text(_("n/a of {total}").format(total=format_kib(mem_max_kib)))
            self._mem_bar.set_value(0.0)
            self._hint.set_text(
                _(
                    "Memory usage needs the guest balloon driver "
                    "(install qemu-guest-agent / virtio drivers in the guest)."
                )
            )
            self._hint.set_visible(True)
        else:
            percent = (mem_used_kib / mem_max_kib * 100.0) if mem_max_kib else 0.0
            self._mem_bar.set_value(max(0.0, min(100.0, percent)))
            self._mem_label.set_text(
                f"{format_kib(mem_used_kib)} / {format_kib(mem_max_kib)}  ({percent:.0f} %)"
            )
            self._mem_plot.push(percent)
            self._hint.set_visible(False)
