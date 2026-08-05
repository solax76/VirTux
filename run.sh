#!/usr/bin/env bash
# Launch VirTux from the checkout.
#
# No virtual environment is needed: PyGObject (python3-gi), the GTK4 typelib
# (gir1.2-gtk-4.0) and libvirt-python (python3-libvirt) all come from the system
# packages, and VirTux has no other dependencies.
set -euo pipefail

cd "$(dirname "$0")"

missing=()
python3 -c 'import gi' 2>/dev/null || missing+=("python3-gi")
python3 -c 'import gi; gi.require_version("Gtk", "4.0")' 2>/dev/null || missing+=("gir1.2-gtk-4.0")
python3 -c 'import libvirt' 2>/dev/null || missing+=("python3-libvirt")

if [ ${#missing[@]} -gt 0 ]; then
    echo "Missing dependencies. Install them with:" >&2
    echo "    sudo apt install ${missing[*]}" >&2
    exit 1
fi

exec python3 -m virtux "$@"
