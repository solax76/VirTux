#!/usr/bin/env bash
# Re-extract translatable strings into po/virtux.pot and merge them into the
# existing catalogs. Run this after adding or changing any _() string.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="$(python3 -c 'import virtux; print(virtux.__version__)')"

echo ">> Extracting strings into po/virtux.pot..."
mkdir -p po
# shellcheck disable=SC2046  # the file list must word-split
xgettext --language=Python --from-code=UTF-8 \
    --keyword=_ --keyword=ngettext:1,2 \
    --add-comments=Translators --sort-by-file \
    --package-name=VirTux --package-version="$VERSION" \
    -o po/virtux.pot \
    $(find virtux -name '*.py' | sort)

shopt -s nullglob
for po in po/*.po; do
    echo ">> Merging $po..."
    msgmerge --quiet --update --backup=none "$po" po/virtux.pot
done

echo ">> Done. Compile the catalogs with ./scripts/build-mo.sh"
