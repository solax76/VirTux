# VirTux

A GTK4 desktop application to manage KVM/libvirt virtual machines: virtual
machines on the left, everything about the selected one on the right.

```
┌─ VirTux ───────────────────── diridell — QEMU 10.2.1 ──────────────── ⟳ ─┐
│ Virtual machines (5) │ win11-lv2024                            running   │
│  ▶ win10-lv2017      │ [Start][Pause][Shut down][Reboot]      [Viewer]  │
│    running           │ [Force reboot][Force stop][Save state][Discard]   │
│  ■ win10-zscaler     ├───────────────────────────────────────────────────┤
│    stopped           │        Info  │  Snapshots  │  Performance         │
│  ■ win11-FTOptix     │                                                   │
│    stopped           │  General                                          │
│  …                   │  Name          win11-lv2024                       │
└──────────────────────┴───────────────────────────────────────────────────┘
```

- **Left column** — every domain on the connection, with its state, refreshed
  automatically.
- **Info** — UUID, architecture, machine type, firmware, autostart, vCPUs, memory,
  disks (with used/total), network interfaces and graphics.
- **Snapshots** — list, create, revert and delete.
- **Performance** — live CPU % and memory use with a rolling chart.
- **Actions** — start, pause/resume, shut down, reboot, force reboot, force stop,
  save state, restore saved state, discard saved state, and open the viewer.

## Requirements

Everything comes from system packages — there is no virtual environment and
nothing to install from PyPI:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 python3-libvirt virt-viewer
```

You also need to be in the `libvirt` group to use `qemu:///system` without root:

```sh
sudo usermod -aG libvirt "$USER"   # then log out and back in
```

Tested on Ubuntu 26.04 with GTK 4.22, PyGObject 3.56, libvirt 12.0 and QEMU 10.2.

## Running

```sh
./run.sh                        # qemu:///system
./run.sh --connect qemu:///session
./run.sh --lang it              # Italian interface
./run.sh --lang auto            # follow the system locale
```

## Keyboard shortcuts

| Shortcut | Action | Shortcut | Action |
|---|---|---|---|
| `Ctrl+U` | Start / restore saved state | `Ctrl+Shift+R` | Force reboot |
| `Ctrl+P` | Pause / resume | `Ctrl+Shift+X` | Force stop |
| `Ctrl+D` | Shut down | `Ctrl+S` | Save state |
| `Ctrl+R` | Reboot | `Ctrl+Shift+S` | Discard saved state |
| `Ctrl+O` | Open the viewer | `Ctrl+N` | New snapshot |
| `F5` | Refresh now | `Ctrl+,` | Preferences |
| `Ctrl+?` | Keyboard shortcuts | `Ctrl+Q` | Quit |

## Configuration

Preferences (`Ctrl+,`) are stored in `~/.config/virtux/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `uri` | `qemu:///system` | libvirt connection |
| `viewer_command` | `virt-viewer` | The connection URI and machine name are appended. Extra flags are allowed, e.g. `virt-viewer --full-screen`. |
| `refresh_interval` | `2` | Seconds between VM list refreshes |

## What each button really does

Understanding these matters, because libvirt's names and everyday expectations do
not always line up.

| Button | libvirt call | Notes |
|---|---|---|
| Start | `create()` | Cold boot |
| Restore saved state | `create()` | Same call: libvirt resumes from a managed-save image when one exists, so this replaces Start whenever a saved state is present |
| Discard saved state | `managedSaveRemove()` | Deletes the saved memory image; the next Start cold-boots |
| Save state | `managedSave()` | Writes memory and CPU state to disk, then stops the machine — like hibernating |
| Pause / Resume | `suspend()` / `resume()` | Freezes vCPUs; memory stays on the host and is lost on a host reboot |
| Shut down | `shutdown()` | **Advisory.** Sends an ACPI power-button request; a guest that ignores it simply stays running, which is why the message says "requested" |
| Reboot | `reboot()` | Advisory in the same way |
| Force reboot | `reset()` | Immediate hardware reset. The guest gets no warning and will likely run a disk check on the way back up |
| Force stop | `destroy()` | Like pulling the power cord |

Actions that lose data ask for confirmation first.

## Snapshots, and the UEFI/NVRAM catch

There are two kinds:

- **Internal** — stored inside the qcow2 disk image. For a running machine this is
  a full checkpoint, memory included.
- **External** — new overlay files next to the disk image, disk only, never memory.
  QEMU needs write access to that directory.

If a machine boots with UEFI firmware whose NVRAM image is in `raw` format —
which is what `virt-install` produces by default — then **libvirt refuses internal
snapshots while that machine is running**:

```
internal snapshots of a VM with pflash based firmware require QCOW2 nvram format
```

libvirt has to capture the NVRAM alongside the memory state, and it cannot do that
with a raw varstore. Shut the machine down and internal snapshots work fine,
because there is no memory or NVRAM state left to capture.

VirTux detects this per machine and per state, so the New-snapshot dialog disables
the option that cannot work, preselects the one that can, and explains why. The
Info tab shows the NVRAM format next to the firmware so the situation is visible
before you get there.

Converting a varstore to qcow2 would lift the restriction, but VirTux does not
offer to do it: it needs root, it rewrites UEFI variables, and on a Windows guest
using BitLocker or measured boot that can trigger a recovery-key prompt.

## Building an AppImage

```sh
./packaging/build-appimage.sh      # -> dist/VirTux-0.1.0-x86_64.AppImage
```

Roughly 39 MB. It bundles the Python interpreter, GTK4, PyGObject,
libvirt-python, the GI typelibs, the GSettings schemas, the gdk-pixbuf loaders and
the Adwaita icon theme, so the target machine needs none of those.

What it deliberately does **not** bundle, because these have to match the host
kernel and drivers: glibc, libstdc++/libgcc, and the OpenGL/Vulkan/DRM/X11
libraries. That is where the portability limit comes from — the AppImage runs on
any distribution whose glibc is at least as new as the machine that built it, and
not on older ones. Build on the oldest distribution you intend to support.

Still required on the target machine:

- a running `libvirtd`, and membership of the `libvirt` group
- `virt-viewer` for the guest console — it is a separate GTK3 program, so it
  cannot be bundled alongside a GTK4 application

The first build downloads `appimagetool` into `~/.cache/virtux-build`; after that
the build is offline. Running the result needs FUSE; on a host without it, use
`./VirTux-0.1.0-x86_64.AppImage --appimage-extract-and-run`.

The script asserts that the GTK stack really is inside the bundle before
packaging, and smoke-tests the finished AppImage in a stripped environment. Both
checks exist because the first working version of this script quietly borrowed the
*host's* `libgtk-4.so.1`: GTK is loaded through its typelib by soname, so it never
appears in any `ldd` output, and the bundle looked fine on the build machine while
being broken anywhere else.

## Translating

The interface is English and every string is translatable.

```sh
./scripts/update-po.sh     # refresh po/virtux.pot and merge into po/*.po
cp po/virtux.pot po/de.po  # start a new language
./scripts/build-mo.sh      # compile po/*.po into locale/
./run.sh --lang de
```

An Italian catalog ships in `po/it.po`. `locale/` is a build artifact and is not
tracked, so the interface stays English until you compile a catalog — even on a
non-English system. Use `--lang auto` if you would rather follow the system locale.

## Notes

- The guest display always opens in an external viewer. Embedding it is not
  possible here: `gtk-vnc` and `spice-gtk` are built against GTK 3, and GTK 3 and
  GTK 4 cannot share one process.
- Memory usage needs a balloon driver in the guest; without one, VirTux says so
  rather than showing a misleading number.
- The VM list is polled rather than event-driven. Live statistics need periodic
  sampling anyway, so one timer covers both.

## Layout

```
virtux/
├── backend.py    libvirt wrapper: domains, lifecycle, details, stats, snapshots
├── commands.py   the action table — labels, icons, availability, confirmations
├── window.py     main window: sidebar, detail pane, threading, refresh
├── widgets.py    list items, toast, info grid, live stats, sparkline
├── dialogs.py    confirmations, snapshot creation, preferences, shortcuts, about
├── config.py     ~/.config/virtux/config.json
├── i18n.py       gettext setup
└── style.css     theme-agnostic styling
```

`backend.py` imports no GTK, so it can be driven from a plain Python REPL:

```python
from virtux.backend import Libvirt
lv = Libvirt(); lv.connect()
print(lv.list_domains())
print(lv.domain_details("win11-lv2024"))
```
