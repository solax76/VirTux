#!/usr/bin/env bash
# Build a self-contained VirTux AppImage.
#
# VirTux itself is a few hundred KB of Python, but it sits on PyGObject, GTK4 and
# libvirt-python, none of which a random machine is guaranteed to have. So the
# AppImage bundles the interpreter, the GTK4 stack, the GI typelibs, the GSettings
# schemas, the gdk-pixbuf loaders and the Adwaita icon theme, and AppRun points
# the runtime search paths at them.
#
# What is deliberately NOT bundled: glibc, libstdc++/libgcc, and the graphics and
# X11 libraries. Those must come from the host, because a bundled libGL cannot
# talk to the host's kernel driver. That is also where the portability floor comes
# from: the result runs on any distribution whose glibc is at least as new as this
# build machine's, and not on older ones.
#
# Requires network access the first time, to fetch appimagetool.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

ARCH="x86_64"
LIBDIR="x86_64-linux-gnu"
PIXBUF_VER="2.10.0"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/virtux-build"
APPDIR="$ROOT/build/VirTux.AppDir"
DIST="$ROOT/dist"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"

VERSION="$(python3 -c 'import virtux; print(virtux.__version__)')"

# Libraries that must come from the host, matched against the file name.
# Bundling any of these is how AppImages break on other machines.
EXCLUDE_RE='^(ld-linux.*|libc\.so\..*|libm\.so\..*|libpthread\.so\..*|libdl\.so\..*|librt\.so\..*|libutil\.so\..*|libnsl\.so\..*|libresolv\.so\..*|libanl\.so\..*|libBrokenLocale\.so\..*|libthread_db\.so\..*'
EXCLUDE_RE+='|libstdc\+\+\.so\..*|libgcc_s\.so\..*'
EXCLUDE_RE+='|libGL\.so\..*|libGLX\.so\..*|libGLU\.so\..*|libEGL\.so\..*|libGLdispatch\.so\..*|libOpenGL\.so\..*|libglapi\.so\..*|libgbm\.so\..*|libdrm.*\.so\..*|libvulkan\.so\..*'
EXCLUDE_RE+='|libX11.*\.so\..*|libxcb.*\.so\..*|libXext\.so\..*|libXrender\.so\..*|libXi\.so\..*|libXfixes\.so\..*|libXdamage\.so\..*|libXcomposite\.so\..*|libXrandr\.so\..*|libXcursor\.so\..*|libXinerama\.so\..*|libXau\.so\..*|libXdmcp\.so\..*|libxshmfence\.so\..*'
EXCLUDE_RE+='|libselinux\.so\..*|libsystemd\.so\..*|libudev\.so\..*|libcap\.so\..*)$'

say() { printf '\033[1m>> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# -- preflight --------------------------------------------------------------

say "Checking build prerequisites"
missing=()
python3 -c 'import gi; gi.require_version("Gtk", "4.0")' 2>/dev/null || missing+=("python3-gi gir1.2-gtk-4.0")
python3 -c 'import libvirt' 2>/dev/null || missing+=("python3-libvirt")
[ -d "/usr/lib/$LIBDIR/girepository-1.0" ] || missing+=("gir1.2-glib-2.0")
command -v msgfmt >/dev/null || missing+=("gettext")
if [ ${#missing[@]} -gt 0 ]; then
    die "missing build dependencies. Install them with: sudo apt install ${missing[*]}"
fi
command -v ldd >/dev/null || die "ldd not found"

PYTHON_BIN="$(readlink -f "$(command -v python3)")"
PYTHON_TAG="$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_STDLIB="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')"
DIST_PACKAGES="$(python3 -c 'import gi, os; print(os.path.dirname(os.path.dirname(gi.__file__)))')"
say "Python $PYTHON_TAG at $PYTHON_BIN (stdlib $PYTHON_STDLIB)"

# -- appimagetool -----------------------------------------------------------

mkdir -p "$CACHE"
TOOL="$CACHE/appimagetool"
if [ ! -x "$TOOL" ]; then
    say "Fetching appimagetool (needs network, cached in $CACHE)"
    curl -fsSL --retry 3 -o "$TOOL.part" "$APPIMAGETOOL_URL" \
        || die "could not download appimagetool from $APPIMAGETOOL_URL"
    mv "$TOOL.part" "$TOOL"
    chmod +x "$TOOL"
fi
# --appimage-extract-and-run avoids needing FUSE on the build machine.
"$TOOL" --appimage-extract-and-run --version >/dev/null 2>&1 \
    || die "appimagetool is present but will not run: $TOOL"

# -- translations -----------------------------------------------------------

say "Compiling translation catalogs"
./scripts/build-mo.sh >/dev/null

# -- AppDir skeleton --------------------------------------------------------

say "Building AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/lib/$LIBDIR" \
         "$APPDIR/usr/lib/virtux" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps" \
         "$APPDIR/usr/share/glib-2.0/schemas" \
         "$APPDIR/usr/share/locale"

# The application itself.
cp -r "$ROOT/virtux" "$APPDIR/usr/lib/virtux/"
find "$APPDIR/usr/lib/virtux" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
[ -d "$ROOT/locale" ] && cp -r "$ROOT/locale/." "$APPDIR/usr/share/locale/"

# Desktop entry and icon. appimagetool wants both at the AppDir root.
cp "$ROOT/data/virtux.desktop" "$APPDIR/virtux.desktop"
cp "$ROOT/data/virtux.desktop" "$APPDIR/usr/share/applications/virtux.desktop"
cp "$ROOT/data/icons/virtux.svg" "$APPDIR/virtux.svg"
cp "$ROOT/data/icons/virtux.svg" "$APPDIR/.DirIcon"
cp "$ROOT/data/icons/virtux.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/virtux.svg"

# -- Python interpreter and standard library --------------------------------

say "Bundling $PYTHON_TAG"
cp "$PYTHON_BIN" "$APPDIR/usr/bin/python3"
mkdir -p "$APPDIR/usr/lib/$PYTHON_TAG"
# Skip what a GUI app never needs; saves roughly 10 MB.
# tar is positional: every --exclude has to precede the operand it applies to.
tar -C "$(dirname "$PYTHON_STDLIB")" \
    --exclude='__pycache__' \
    --exclude='test' --exclude='tests' \
    --exclude='idlelib' --exclude='tkinter' --exclude='turtledemo' \
    --exclude='lib2to3' --exclude='ensurepip' \
    -cf - "$(basename "$PYTHON_STDLIB")" \
    | tar -C "$APPDIR/usr/lib" -xf -

say "Bundling PyGObject and libvirt-python"
mkdir -p "$APPDIR/usr/lib/python3/dist-packages"
for module in gi cairo pygtkcompat libvirt.py libvirtaio.py; do
    if [ -e "$DIST_PACKAGES/$module" ]; then
        cp -r "$DIST_PACKAGES/$module" "$APPDIR/usr/lib/python3/dist-packages/"
    fi
done
# The compiled libvirt extension modules, whose names carry the Python ABI tag.
# These have to be globbed inside dist-packages, not pattern-matched as a path.
shopt -s nullglob
libvirtmods=("$DIST_PACKAGES"/libvirtmod*.so)
shopt -u nullglob
[ ${#libvirtmods[@]} -gt 0 ] || die "libvirtmod*.so not found in $DIST_PACKAGES"
cp "${libvirtmods[@]}" "$APPDIR/usr/lib/python3/dist-packages/"
find "$APPDIR/usr/lib/python3/dist-packages" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# -- GI typelibs ------------------------------------------------------------

say "Bundling GI typelibs"
mkdir -p "$APPDIR/usr/lib/$LIBDIR/girepository-1.0"
# Everything the app can touch, directly or through GTK.
for typelib in Gtk-4.0 Gdk-4.0 Gsk-4.0 GdkWayland-4.0 GdkX11-4.0 \
               Gio-2.0 GioUnix-2.0 GLib-2.0 GLibUnix-2.0 GObject-2.0 GModule-2.0 \
               Graphene-1.0 Pango-1.0 PangoCairo-1.0 PangoFT2-1.0 cairo-1.0 \
               GdkPixbuf-2.0 HarfBuzz-0.0 freetype2-2.0 Gee-0.8 xlib-2.0; do
    src="/usr/lib/$LIBDIR/girepository-1.0/$typelib.typelib"
    [ -f "$src" ] && cp "$src" "$APPDIR/usr/lib/$LIBDIR/girepository-1.0/"
done

# -- shared libraries -------------------------------------------------------

# Seeds: everything that is linked or dlopened. ldd already reports the full
# transitive closure of each seed, so one pass per seed is enough; dlopened
# modules (pixbuf loaders, extension modules) have to be named explicitly
# because they never show up in anyone's ldd output.
say "Collecting shared libraries"
# Libraries nothing links against directly: GTK is reached through its typelib by
# soname, so it never appears in any ldd output and has to be copied outright.
# Miss this and the AppImage silently falls back to the host's GTK.
lib_seeds=(
    "/usr/lib/$LIBDIR/libgtk-4.so.1"
    "/usr/lib/$LIBDIR/libgirepository-2.0.so.0"
)
seeds=("$PYTHON_BIN" "${lib_seeds[@]}")
while IFS= read -r so; do seeds+=("$so"); done < <(
    find "$DIST_PACKAGES/gi" -name '*.so' 2>/dev/null
    ls "$DIST_PACKAGES"/libvirtmod*.so 2>/dev/null
    find "$PYTHON_STDLIB/lib-dynload" -name '*.so' 2>/dev/null
    ls "/usr/lib/$LIBDIR/gdk-pixbuf-2.0/$PIXBUF_VER/loaders/"*.so 2>/dev/null
)

copied=0
skipped=0
declare -A seen=()

# The seed libraries themselves, since ldd only reports a file's dependencies.
for seed in "${lib_seeds[@]}"; do
    [ -e "$seed" ] || die "expected library not found: $seed"
    base="$(basename "$(readlink -f "$seed")")"
    cp -L "$seed" "$APPDIR/usr/lib/$LIBDIR/$(basename "$seed")"
    seen["$(basename "$seed")"]=1
    seen["$base"]=1
    copied=$((copied + 1))
done

for seed in "${seeds[@]}"; do
    [ -e "$seed" ] || continue
    while read -r path; do
        [ -f "$path" ] || continue
        base="$(basename "$path")"
        [ -n "${seen[$base]:-}" ] && continue
        seen[$base]=1
        if [[ "$base" =~ $EXCLUDE_RE ]]; then
            skipped=$((skipped + 1))
            continue
        fi
        cp -L "$path" "$APPDIR/usr/lib/$LIBDIR/$base"
        copied=$((copied + 1))
    done < <(ldd "$seed" 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i ~ /^\//) {print $i; break}}')
done
say "  bundled $copied libraries, left $skipped to the host"

# Guard against the failure mode above: if one of these is missing the AppImage
# still starts here, because it quietly borrows the host's copy, and then fails
# on a machine that does not have it.
for required in libgtk-4.so.1 libgirepository-2.0.so.0 libglib-2.0.so.0 \
                libgobject-2.0.so.0 libgio-2.0.so.0 libvirt.so.0 \
                libgdk_pixbuf-2.0.so.0 libpango-1.0.so.0 libpangocairo-1.0.so.0 \
                libcairo.so.2 libgraphene-1.0.so.0 libharfbuzz.so.0; do
    [ -e "$APPDIR/usr/lib/$LIBDIR/$required" ] \
        || die "$required is not in the bundle — it would be taken from the host"
done

# -- gdk-pixbuf loaders -----------------------------------------------------

say "Bundling gdk-pixbuf loaders"
PIXBUF_DEST="$APPDIR/usr/lib/$LIBDIR/gdk-pixbuf-2.0/$PIXBUF_VER"
mkdir -p "$PIXBUF_DEST/loaders"
cp "/usr/lib/$LIBDIR/gdk-pixbuf-2.0/$PIXBUF_VER/loaders/"*.so "$PIXBUF_DEST/loaders/" 2>/dev/null || true
QUERY="/usr/lib/$LIBDIR/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders"
if [ -x "$QUERY" ]; then
    # The cache records absolute paths, but the AppImage mounts at a random
    # location. Strip the directory so only the file name is left and let
    # GDK_PIXBUF_MODULEDIR (set in AppRun) supply it at runtime.
    "$QUERY" "$PIXBUF_DEST/loaders/"*.so \
        | sed "s|\"$PIXBUF_DEST/loaders/|\"|" > "$PIXBUF_DEST/loaders.cache"
else
    sed "s|\".*/loaders/|\"|" \
        "/usr/lib/$LIBDIR/gdk-pixbuf-2.0/$PIXBUF_VER/loaders.cache" > "$PIXBUF_DEST/loaders.cache"
fi

# -- GSettings schemas and icons --------------------------------------------

# GTK4 aborts at startup if its own schemas are missing. gschemas.compiled is a
# single file holding every schema on the system, so copying it covers GTK's.
say "Bundling GSettings schemas"
cp /usr/share/glib-2.0/schemas/gschemas.compiled "$APPDIR/usr/share/glib-2.0/schemas/" \
    || die "gschemas.compiled not found — install libglib2.0-bin and run glib-compile-schemas"

say "Bundling the Adwaita icon theme"
ADW="$APPDIR/usr/share/icons/Adwaita"
mkdir -p "$ADW"
# Skip cursors: 12 MB of the theme's 16 MB, and the host supplies the cursor theme.
tar -C /usr/share/icons --exclude='cursors' -cf - Adwaita \
    | tar -C "$APPDIR/usr/share/icons" -xf -
[ -f /usr/share/icons/hicolor/index.theme ] \
    && install -Dm644 /usr/share/icons/hicolor/index.theme \
       "$APPDIR/usr/share/icons/hicolor/index.theme"

# -- AppRun -----------------------------------------------------------------

say "Writing AppRun"
cat > "$APPDIR/AppRun" <<APPRUN
#!/bin/sh
# Point every runtime search path at the bundle before starting VirTux.
HERE="\$(dirname "\$(readlink -f "\$0")")"
export APPDIR="\${APPDIR:-\$HERE}"

export LD_LIBRARY_PATH="\$APPDIR/usr/lib/$LIBDIR\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="\$APPDIR/usr/lib/$LIBDIR/girepository-1.0\${GI_TYPELIB_PATH:+:\$GI_TYPELIB_PATH}"
export GSETTINGS_SCHEMA_DIR="\$APPDIR/usr/share/glib-2.0/schemas"
export XDG_DATA_DIRS="\$APPDIR/usr/share:\${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GDK_PIXBUF_MODULEDIR="\$APPDIR/usr/lib/$LIBDIR/gdk-pixbuf-2.0/$PIXBUF_VER/loaders"
export GDK_PIXBUF_MODULE_FILE="\$APPDIR/usr/lib/$LIBDIR/gdk-pixbuf-2.0/$PIXBUF_VER/loaders.cache"

export PYTHONHOME="\$APPDIR/usr"
export PYTHONPATH="\$APPDIR/usr/lib/virtux:\$APPDIR/usr/lib/python3/dist-packages"
export PYTHONDONTWRITEBYTECODE=1
# Without this, "python -m virtux" prepends the current directory to sys.path, so
# launching the AppImage from a checkout would silently run that checkout's code
# instead of the bundled copy.
export PYTHONSAFEPATH=1
export VIRTUX_LOCALE_DIR="\$APPDIR/usr/share/locale"

# GTK >= 4.14 prefers the Vulkan renderer. A bundled GTK meeting a host with no
# matching Vulkan driver falls over, so ask for GL unless told otherwise.
export GSK_RENDERER="\${GSK_RENDERER:-gl}"

exec "\$APPDIR/usr/bin/python3" -m virtux "\$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# -- package ----------------------------------------------------------------

say "Packaging with appimagetool"
mkdir -p "$DIST"
OUTPUT="$DIST/VirTux-$VERSION-$ARCH.AppImage"
rm -f "$OUTPUT"
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUTPUT" >/dev/null 2>&1 \
    || die "appimagetool failed. Re-run without output suppression to see why."

chmod +x "$OUTPUT"

# Smoke test in a stripped environment, so nothing inherited from this shell can
# paper over a missing piece of the bundle.
say "Smoke-testing the AppImage"
reported="$(env -i HOME="$HOME" PATH=/usr/bin:/bin "$OUTPUT" --version 2>&1)" \
    || die "the AppImage does not start: $reported"
[ "$reported" = "VirTux $VERSION" ] \
    || die "unexpected --version output: $reported"
say "  $reported"

say "Built $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
cat <<NOTE

Bundle contents: $PYTHON_TAG, GTK4, PyGObject, libvirt-python, GI typelibs,
GSettings schemas, gdk-pixbuf loaders and the Adwaita icon theme.

Still needed on the target machine:
  - glibc at least as new as this build host's, plus its graphics/X11 libraries
  - a running libvirtd, and membership of the 'libvirt' group
  - virt-viewer, to open a guest console (it is a separate GTK3 program, so
    bundling it here is not possible)

Run it with:
  $OUTPUT
If the host has no FUSE:
  $OUTPUT --appimage-extract-and-run
NOTE
