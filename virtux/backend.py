"""libvirt backend for VirTux.

All state is read through the libvirt API (never by parsing localized virsh text
output), so it works regardless of the system locale.

Adapted from the author's lazyvirtmanager backend, extended with a force reset,
an explicit managed-save restore, domain XML parsing for the info pane, live stat
sampling and snapshot modes.

This module is deliberately toolkit-agnostic: it imports nothing from GTK, so it
can be exercised from a plain REPL.
"""

from __future__ import annotations

import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

import libvirt

DEFAULT_URI = "qemu:///system"

# libvirt prints every error to stderr unless a handler is registered. The UI
# reports failures itself, so silence the default chatter.
libvirt.registerErrorHandler(lambda _ctx, _err: None, None)

# Map libvirt domain state codes to short canonical English labels. The UI
# translates these; see virtux.commands.state_label().
_STATE_LABELS = {
    libvirt.VIR_DOMAIN_NOSTATE: "no state",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutting down",
    libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
}


@dataclass
class VMInfo:
    """A snapshot-in-time view of a domain, safe to pass to the UI thread."""

    name: str
    id: int | None
    state: int
    state_label: str
    is_active: bool
    is_paused: bool
    has_saved: bool


@dataclass
class SnapshotInfo:
    name: str
    creation_time: str
    state: str
    is_current: bool
    description: str = ""
    # Raw epoch seconds: the sort key, since several snapshots can share the same
    # formatted timestamp when they are taken within the same second.
    creation_epoch: int = 0


@dataclass
class DiskInfo:
    target: str
    device: str
    bus: str
    driver_type: str
    source: str
    capacity: int | None = None
    allocation: int | None = None


@dataclass
class NicInfo:
    mac: str
    type: str
    source: str
    model: str


@dataclass
class GraphicsInfo:
    type: str
    port: str
    listen: str


@dataclass
class DomainDetails:
    """Everything the info pane shows about one domain."""

    name: str
    uuid: str
    state_label: str
    is_active: bool
    arch: str
    machine: str
    firmware: str
    is_pflash: bool
    nvram_format: str | None
    has_guest_agent: bool
    vcpus: int
    vcpu_placement: str
    max_memory_kib: int
    current_memory_kib: int
    autostart: bool
    persistent: bool
    has_saved: bool
    disks: list[DiskInfo] = field(default_factory=list)
    nics: list[NicInfo] = field(default_factory=list)
    graphics: list[GraphicsInfo] = field(default_factory=list)

    @property
    def needs_qcow2_nvram(self) -> bool:
        """True when this domain's UEFI varstore blocks internal snapshots."""
        return self.is_pflash and self.nvram_format != "qcow2"

    @property
    def internal_snapshot_ok(self) -> bool:
        """Can libvirt take an internal snapshot of this domain right now?

        Verified against libvirt 12.0 + QEMU 10.2: an internal snapshot of a
        *running* pflash domain is refused with "internal snapshots of a VM with
        pflash based firmware require QCOW2 nvram format", because libvirt has to
        snapshot the NVRAM image alongside the memory state. Shut off there is no
        such state to capture and the same domain snapshots fine.
        """
        if not self.needs_qcow2_nvram:
            return True
        return not self.is_active


@dataclass
class DomainStats:
    """One sample of live resource usage. CPU% needs two samples to compute."""

    cpu_time_ns: int
    vcpus: int
    mem_max_kib: int
    # None when the guest has no balloon driver reporting usable numbers.
    mem_used_kib: int | None


class LibvirtError(Exception):
    """Wraps libvirt.libvirtError with a clean message for the UI."""


class Libvirt:
    """Thin wrapper around a libvirt connection and domain/snapshot operations."""

    def __init__(self, uri: str = DEFAULT_URI) -> None:
        self.uri = uri
        self._conn: libvirt.virConnect | None = None
        # libvirt connections are thread-safe, but serialising keeps the polling
        # thread from interleaving with a user action mid-reconnect.
        self._lock = threading.RLock()

    # -- connection ---------------------------------------------------------

    def connect(self, uri: str | None = None) -> None:
        """Open (or reopen) the connection to the given URI."""
        with self._lock:
            if uri is not None:
                self.uri = uri
            self.close()
            try:
                conn = libvirt.open(self.uri)
            except libvirt.libvirtError as exc:
                raise LibvirtError(f"Cannot connect to {self.uri}: {exc}") from exc
            if conn is None:
                raise LibvirtError(f"Cannot connect to {self.uri}")
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except libvirt.libvirtError:
                    pass
                self._conn = None

    @property
    def conn(self) -> libvirt.virConnect:
        if self._conn is None:
            raise LibvirtError("Not connected")
        return self._conn

    def is_alive(self) -> bool:
        """True when the connection is still usable."""
        with self._lock:
            if self._conn is None:
                return False
            try:
                return bool(self._conn.isAlive())
            except libvirt.libvirtError:
                return False

    def host_summary(self) -> str:
        """Short 'hostname — hypervisor version' string for the header bar."""
        with self._lock:
            try:
                hostname = self.conn.getHostname()
                ver = self.conn.getVersion() or 0
            except libvirt.libvirtError as exc:
                raise LibvirtError(f"Cannot query host: {exc}") from exc
        major, rest = divmod(ver, 1000000)
        minor, release = divmod(rest, 1000)
        return f"{hostname} — QEMU {major}.{minor}.{release}"

    # -- domains ------------------------------------------------------------

    def _domain(self, name: str) -> libvirt.virDomain:
        try:
            return self.conn.lookupByName(name)
        except libvirt.libvirtError as exc:
            raise LibvirtError(f"VM '{name}' not found: {exc}") from exc

    def list_domains(self) -> list[VMInfo]:
        """Return every defined/active domain with its current state."""
        with self._lock:
            try:
                domains = self.conn.listAllDomains()
            except libvirt.libvirtError as exc:
                raise LibvirtError(f"Cannot list VMs: {exc}") from exc

            infos: list[VMInfo] = []
            for dom in domains:
                try:
                    state = dom.state()[0]
                    is_active = bool(dom.isActive())
                    try:
                        has_saved = bool(dom.hasManagedSaveImage())
                    except libvirt.libvirtError:
                        has_saved = False
                    dom_id = dom.ID() if is_active else None
                    infos.append(
                        VMInfo(
                            name=dom.name(),
                            id=dom_id if dom_id and dom_id >= 0 else None,
                            state=state,
                            state_label=_STATE_LABELS.get(state, "unknown"),
                            is_active=is_active,
                            is_paused=state == libvirt.VIR_DOMAIN_PAUSED,
                            has_saved=has_saved,
                        )
                    )
                except libvirt.libvirtError:
                    # Skip a domain that vanished mid-iteration.
                    continue

        infos.sort(key=lambda vm: vm.name.lower())
        return infos

    # -- lifecycle actions --------------------------------------------------

    def start(self, name: str) -> None:
        """Boot the domain. If a managed-save image exists libvirt restores it."""
        self._run(name, lambda d: d.create(), "start")

    def restore_saved_state(self, name: str) -> None:
        """Resume from a managed-save image.

        There is no separate 'restore' API for managed save: create() detects the
        saved image and resumes from it instead of cold-booting. Kept as its own
        method so the UI layer reads clearly.
        """
        self._run(name, lambda d: d.create(), "restore the saved state of")

    def shutdown(self, name: str) -> None:
        self._run(name, lambda d: d.shutdown(), "shut down")

    def force_stop(self, name: str) -> None:
        self._run(name, lambda d: d.destroy(), "force stop")

    def pause(self, name: str) -> None:
        self._run(name, lambda d: d.suspend(), "pause")

    def resume(self, name: str) -> None:
        self._run(name, lambda d: d.resume(), "resume")

    def reboot(self, name: str) -> None:
        self._run(name, lambda d: d.reboot(), "reboot")

    def force_reboot(self, name: str) -> None:
        """Hard reset, equivalent to the physical reset button."""
        self._run(name, lambda d: d.reset(), "force reboot")

    def save_state(self, name: str) -> None:
        """managedsave: save RAM+state to a libvirt-managed file and stop."""
        self._run(name, lambda d: d.managedSave(), "save the state of")

    def remove_saved_state(self, name: str) -> None:
        self._run(name, lambda d: d.managedSaveRemove(), "discard the saved state of")

    def set_autostart(self, name: str, enabled: bool) -> None:
        self._run(name, lambda d: d.setAutostart(1 if enabled else 0), "change autostart for")

    def _run(self, name: str, op, verb: str) -> None:
        with self._lock:
            dom = self._domain(name)
            try:
                op(dom)
            except libvirt.libvirtError as exc:
                raise LibvirtError(f"Failed to {verb} '{name}': {exc}") from exc

    # -- domain details -----------------------------------------------------

    def domain_details(self, name: str) -> DomainDetails:
        """Parse one XMLDesc() plus info() into everything the info pane needs."""
        with self._lock:
            dom = self._domain(name)
            try:
                xml = dom.XMLDesc(0)
                state, max_mem, cur_mem, vcpus, _cpu_time = dom.info()
                uuid = dom.UUIDString()
                persistent = bool(dom.isPersistent())
            except libvirt.libvirtError as exc:
                raise LibvirtError(f"Cannot read details of '{name}': {exc}") from exc
            try:
                autostart = bool(dom.autostart())
            except libvirt.libvirtError:
                autostart = False
            try:
                has_saved = bool(dom.hasManagedSaveImage())
            except libvirt.libvirtError:
                has_saved = False

            root = ET.fromstring(xml)
            disks = self._parse_disks(root, dom)

        os_type = root.find("os/type")
        arch = os_type.get("arch", "-") if os_type is not None else "-"
        machine = os_type.get("machine", "-") if os_type is not None else "-"

        firmware, is_pflash, nvram_format = self._parse_firmware(root)

        vcpu_el = root.find("vcpu")
        placement = vcpu_el.get("placement", "-") if vcpu_el is not None else "-"

        return DomainDetails(
            name=name,
            uuid=uuid,
            state_label=_STATE_LABELS.get(state, "unknown"),
            is_active=state not in (libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_NOSTATE),
            arch=arch,
            machine=machine,
            firmware=firmware,
            is_pflash=is_pflash,
            nvram_format=nvram_format,
            has_guest_agent=self._parse_guest_agent(root),
            vcpus=vcpus,
            vcpu_placement=placement,
            max_memory_kib=max_mem,
            current_memory_kib=cur_mem,
            autostart=autostart,
            persistent=persistent,
            has_saved=has_saved,
            disks=disks,
            nics=self._parse_nics(root),
            graphics=self._parse_graphics(root),
        )

    @staticmethod
    def _parse_firmware(root: ET.Element) -> tuple[str, bool, str | None]:
        """Return (label, uses pflash, NVRAM format). Format is None for BIOS."""
        loader = root.find("os/loader")
        nvram = root.find("os/nvram")
        if loader is None and nvram is None:
            return "BIOS", False, None
        label = "UEFI"
        if loader is not None and loader.get("secure") == "yes":
            label = "UEFI (Secure Boot)"
        is_pflash = loader is not None and loader.get("type") == "pflash"
        nvram_format = nvram.get("format", "raw") if nvram is not None else None
        return label, is_pflash, nvram_format

    @staticmethod
    def _parse_guest_agent(root: ET.Element) -> bool:
        """Is a qemu-guest-agent channel configured? Filesystem freeze needs it."""
        for channel in root.findall("devices/channel"):
            target = channel.find("target")
            if target is not None and target.get("name") == "org.qemu.guest_agent.0":
                return True
        return False

    def _parse_disks(self, root: ET.Element, dom: libvirt.virDomain) -> list[DiskInfo]:
        disks: list[DiskInfo] = []
        for el in root.findall("devices/disk"):
            target = el.find("target")
            driver = el.find("driver")
            source = el.find("source")
            path = "-"
            if source is not None:
                path = (
                    source.get("file")
                    or source.get("dev")
                    or source.get("name")
                    or source.get("volume")
                    or "-"
                )
            disk = DiskInfo(
                target=target.get("dev", "-") if target is not None else "-",
                device=el.get("device", "disk"),
                bus=target.get("bus", "-") if target is not None else "-",
                driver_type=driver.get("type", "-") if driver is not None else "-",
                source=path,
            )
            if disk.target != "-":
                try:
                    capacity, allocation, _physical = dom.blockInfo(disk.target)
                    disk.capacity = capacity
                    disk.allocation = allocation
                except libvirt.libvirtError:
                    # Empty cdrom, or a volume libvirtd cannot stat right now.
                    pass
            disks.append(disk)
        return disks

    @staticmethod
    def _parse_nics(root: ET.Element) -> list[NicInfo]:
        nics: list[NicInfo] = []
        for el in root.findall("devices/interface"):
            mac = el.find("mac")
            model = el.find("model")
            source = el.find("source")
            src = "-"
            if source is not None:
                src = (
                    source.get("network")
                    or source.get("bridge")
                    or source.get("dev")
                    or source.get("path")
                    or "-"
                )
            nics.append(
                NicInfo(
                    mac=mac.get("address", "-") if mac is not None else "-",
                    type=el.get("type", "-"),
                    source=src,
                    model=model.get("type", "-") if model is not None else "-",
                )
            )
        return nics

    @staticmethod
    def _parse_graphics(root: ET.Element) -> list[GraphicsInfo]:
        graphics: list[GraphicsInfo] = []
        for el in root.findall("devices/graphics"):
            listen_el = el.find("listen")
            listen = el.get("listen") or "-"
            if listen == "-" and listen_el is not None:
                listen = listen_el.get("address") or listen_el.get("type") or "-"
            graphics.append(
                GraphicsInfo(
                    type=el.get("type", "-"),
                    port=el.get("port", "-"),
                    listen=listen,
                )
            )
        return graphics

    # -- live stats ---------------------------------------------------------

    def sample_stats(self, name: str) -> DomainStats:
        """Take one usage sample. CPU% is derived from two samples by the caller."""
        with self._lock:
            dom = self._domain(name)
            try:
                state, max_mem, cur_mem, vcpus, cpu_time = dom.info()
            except libvirt.libvirtError as exc:
                raise LibvirtError(f"Cannot sample '{name}': {exc}") from exc

            used: int | None = None
            try:
                stats = dom.memoryStats()
            except libvirt.libvirtError:
                stats = {}

        # 'rss' is the host-side footprint; available-unused is what the guest
        # itself reports. Prefer the guest view, both need a balloon driver.
        if "available" in stats and "unused" in stats:
            used = max(0, stats["available"] - stats["unused"])
        elif "rss" in stats:
            used = stats["rss"]
        elif state == libvirt.VIR_DOMAIN_RUNNING:
            # Without balloon stats cur_mem is just the allocation, not usage.
            used = None
        return DomainStats(
            cpu_time_ns=cpu_time,
            vcpus=vcpus or 1,
            mem_max_kib=max_mem,
            mem_used_kib=used,
        )

    # -- snapshots ----------------------------------------------------------

    def list_snapshots(self, name: str) -> list[SnapshotInfo]:
        with self._lock:
            dom = self._domain(name)
            try:
                snaps = dom.listAllSnapshots()
            except libvirt.libvirtError as exc:
                raise LibvirtError(f"Cannot list snapshots of '{name}': {exc}") from exc

            infos: list[SnapshotInfo] = []
            for snap in snaps:
                try:
                    infos.append(self._snapshot_info(snap))
                except libvirt.libvirtError:
                    continue
        infos.sort(key=lambda s: (s.creation_epoch, s.name), reverse=True)
        return infos

    def _snapshot_info(self, snap: libvirt.virDomainSnapshot) -> SnapshotInfo:
        root = ET.fromstring(snap.getXMLDesc())
        ct_raw = root.findtext("creationTime")
        epoch = 0
        if ct_raw and ct_raw.isdigit():
            epoch = int(ct_raw)
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone()
            creation = dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            creation = "-"
        state = root.findtext("state") or "-"
        try:
            current = bool(snap.isCurrent())
        except libvirt.libvirtError:
            current = False
        return SnapshotInfo(
            name=snap.getName(),
            creation_time=creation,
            state=state,
            is_current=current,
            description=root.findtext("description") or "",
            creation_epoch=epoch,
        )

    def create_snapshot(
        self,
        name: str,
        snap_name: str,
        description: str = "",
        external: bool = False,
        quiesce: bool = False,
    ) -> None:
        """Create a snapshot of the domain.

        Internal (the default) stores the state inside the qcow2 files and, for a
        running domain, includes memory — a full checkpoint. External instead
        creates disk-only overlay files, which is the only mode libvirt allows for
        a UEFI domain whose NVRAM image is raw.

        quiesce freezes the guest filesystems first for a consistent disk-only
        snapshot; it needs qemu-guest-agent, so it is retried without the flag.
        """
        root = ET.Element("domainsnapshot")
        if snap_name:
            ET.SubElement(root, "name").text = snap_name
        if description:
            ET.SubElement(root, "description").text = description

        flags = 0
        if external:
            flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
            flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
            # Ask libvirt to pick the overlay paths itself.
            memory = ET.SubElement(root, "memory")
            memory.set("snapshot", "no")
        xml = ET.tostring(root, encoding="unicode")

        with self._lock:
            dom = self._domain(name)
            attempts = [flags | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE] if quiesce else []
            attempts.append(flags)
            last: libvirt.libvirtError | None = None
            for attempt in attempts:
                try:
                    dom.snapshotCreateXML(xml, attempt)
                    return
                except libvirt.libvirtError as exc:
                    last = exc
            raise LibvirtError(
                f"Failed to create a snapshot of '{name}': {last}"
            ) from last

    def revert_snapshot(self, name: str, snap_name: str) -> None:
        with self._lock:
            dom = self._domain(name)
            try:
                snap = dom.snapshotLookupByName(snap_name)
                dom.revertToSnapshot(snap, 0)
            except libvirt.libvirtError as exc:
                raise LibvirtError(
                    f"Failed to revert '{name}' to snapshot '{snap_name}': {exc}"
                ) from exc

    def delete_snapshot(self, name: str, snap_name: str) -> None:
        with self._lock:
            dom = self._domain(name)
            try:
                snap = dom.snapshotLookupByName(snap_name)
                snap.delete(0)
            except libvirt.libvirtError as exc:
                raise LibvirtError(
                    f"Failed to delete snapshot '{snap_name}' of '{name}': {exc}"
                ) from exc
