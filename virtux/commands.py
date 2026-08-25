"""The action table: what each command does, and when it is available.

Keeping availability in one declarative table means the toolbar buttons, the
keyboard accelerators and the confirmation dialogs all derive from the same
source of truth instead of drifting apart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import libvirt

from .backend import VMInfo
from .i18n import _

# Translated counterparts of the canonical labels in backend._STATE_LABELS. The
# backend stays language-neutral; the mapping lives here so xgettext finds it.
_STATE_LABELS = {
    "no state": _("no state"),
    "running": _("running"),
    "blocked": _("blocked"),
    "paused": _("paused"),
    "shutting down": _("shutting down"),
    "stopped": _("stopped"),
    "crashed": _("crashed"),
    "suspended": _("suspended"),
    "unknown": _("unknown"),
}


def state_label(vm: VMInfo) -> str:
    """Localized state text for a VM."""
    return _STATE_LABELS.get(vm.state_label, vm.state_label)


def state_icon(vm: VMInfo) -> str:
    """Icon name representing the VM state in the sidebar."""
    if vm.state == libvirt.VIR_DOMAIN_RUNNING:
        return "media-playback-start-symbolic"
    if vm.is_paused or vm.state == libvirt.VIR_DOMAIN_PMSUSPENDED:
        return "media-playback-pause-symbolic"
    if vm.state == libvirt.VIR_DOMAIN_CRASHED:
        return "dialog-warning-symbolic"
    if vm.has_saved:
        return "document-save-symbolic"
    return "media-playback-stop-symbolic"


@dataclass
class Command:
    """One VM action, its button presentation and its availability rule."""

    id: str
    label: str
    icon: str
    # Given the selected VM, is this command available?
    enabled: Callable[[VMInfo], bool]
    tooltip: str = ""
    destructive: bool = False
    suggested: bool = False
    # Confirmation heading/body templates; '{name}' is substituted. Empty = no prompt.
    confirm_heading: str = ""
    confirm_body: str = ""
    # Message shown in the toast once the call returns.
    done: str = ""


def _running(vm: VMInfo) -> bool:
    return vm.state == libvirt.VIR_DOMAIN_RUNNING


def _resumable(vm: VMInfo) -> bool:
    return vm.is_paused or vm.state == libvirt.VIR_DOMAIN_PMSUSPENDED


COMMANDS: list[Command] = [
    Command(
        "start",
        _("Start"),
        "media-playback-start-symbolic",
        lambda vm: not vm.is_active and not vm.has_saved,
        tooltip=_("Boot the virtual machine"),
        done=_("{name} started"),
        suggested=lambda vm: not vm.is_active and not vm.has_saved,
    ),
    Command(
        "restore",
        _("Restore saved state"),
        "document-revert-symbolic",
        lambda vm: not vm.is_active and vm.has_saved,
        tooltip=_("Resume the machine from its saved state"),
        done=_("{name} restored from the saved state"),
        suggested=lambda vm: not vm.is_active and vm.has_saved,
    ),
    Command(
        "pause",
        _("Pause"),
        "media-playback-pause-symbolic",
        _running,
        tooltip=_("Suspend the machine, keeping it in memory"),
        done=_("{name} paused"),
        suggested=_running,
    ),
    Command(
        "resume",
        _("Resume"),
        "media-playback-start-symbolic",
        _resumable,
        tooltip=_("Resume the paused machine"),
        done=_("{name} resumed"),
        suggested=_resumable,
    ),
    Command(
        "shutdown",
        _("Shut down"),
        "system-shutdown-symbolic",
        _running,
        tooltip=_("Ask the guest OS to shut down cleanly"),
        done=_("Shutdown requested for {name}"),
    ),
    Command(
        "reboot",
        _("Reboot"),
        "view-refresh-symbolic",
        _running,
        tooltip=_("Ask the guest OS to reboot cleanly"),
        done=_("Reboot requested for {name}"),
    ),
    Command(
        "force-reboot",
        _("Force reboot"),
        "system-reboot-symbolic",
        lambda vm: vm.is_active,
        tooltip=_("Reset the machine immediately, like the physical reset button"),
        destructive=True,
        confirm_heading=_("Force reboot “{name}”?"),
        confirm_body=_(
            "The machine is reset immediately without telling the guest OS. "
            "Unsaved data will be lost."
        ),
        done=_("{name} was reset"),
    ),
    Command(
        "force-stop",
        _("Force stop"),
        "process-stop-symbolic",
        lambda vm: vm.is_active,
        tooltip=_("Power the machine off immediately"),
        destructive=True,
        confirm_heading=_("Force stop “{name}”?"),
        confirm_body=_(
            "This is like pulling the power cord. Unsaved data will be lost."
        ),
        done=_("{name} was forced off"),
    ),
    Command(
        "save-state",
        _("Save state"),
        "document-save-symbolic",
        lambda vm: vm.is_active and (_running(vm) or vm.is_paused),
        tooltip=_("Write memory and CPU state to disk, then stop the machine"),
        done=_("State of {name} saved"),
        suggested=lambda vm: vm.is_active and (_running(vm) or vm.is_paused),
    ),
    Command(
        "discard-saved",
        _("Discard saved state"),
        "user-trash-symbolic",
        lambda vm: not vm.is_active and vm.has_saved,
        tooltip=_("Delete the saved state so the machine boots from scratch"),
        destructive=True,
        confirm_heading=_("Discard the saved state of “{name}”?"),
        confirm_body=_(
            "The saved memory image is deleted and the machine will cold-boot "
            "next time it is started."
        ),
        done=_("Saved state of {name} discarded"),
    ),
    Command(
        "viewer",
        _("Viewer"),
        "video-display-symbolic",
        lambda vm: vm.is_active,
        tooltip=_("Open the graphical console"),
    ),
]

BY_ID: dict[str, Command] = {cmd.id: cmd for cmd in COMMANDS}


def power_id(vm: VMInfo | None) -> str:
    """Which command the morphing Start/Restore button represents."""
    if vm is not None and not vm.is_active and vm.has_saved:
        return "restore"
    return "start"


def pause_id(vm: VMInfo | None) -> str:
    """Which command the morphing Pause/Resume button represents."""
    if vm is not None and _resumable(vm):
        return "resume"
    return "pause"


# Toolbar layout: (button key, how to resolve its command id).
# 'power' and 'pause' morph with the VM state; the rest are fixed.
PRIMARY_ROW = ["power", "pause", "shutdown", "reboot"]
SECONDARY_ROW = ["force-reboot", "force-stop", "save-state", "discard-saved"]

# Accelerators, registered on the application as win.<id> actions.
ACCELS: dict[str, str] = {
    "power": "<Control>u",
    "pause": "<Control>p",
    "shutdown": "<Control>d",
    "reboot": "<Control>r",
    "force-reboot": "<Control><Shift>r",
    "force-stop": "<Control><Shift>x",
    "save-state": "<Control>s",
    "discard-saved": "<Control><Shift>s",
    "viewer": "<Control>o",
    "refresh": "F5",
    "snapshot-new": "<Control>n",
    "preferences": "<Control>comma",
    "shortcuts": "<Control>question",
    "quit": "<Control>q",
}
