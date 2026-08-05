#!/usr/bin/env bash
# Compile po/<lang>.po into locale/<lang>/LC_MESSAGES/virtux.mo, which is where
# virtux/i18n.py looks when running from a checkout.
set -euo pipefail

cd "$(dirname "$0")/.."

shopt -s nullglob
found=0
for po in po/*.po; do
    lang="$(basename "$po" .po)"
    dest="locale/$lang/LC_MESSAGES"
    mkdir -p "$dest"
    echo ">> $po -> $dest/virtux.mo"
    msgfmt --check --statistics -o "$dest/virtux.mo" "$po"
    found=1
done

if [ "$found" -eq 0 ]; then
    echo "No catalogs in po/. Nothing to build." >&2
    exit 0
fi

echo ">> Done. Try it with:  LANGUAGE=it ./run.sh"
