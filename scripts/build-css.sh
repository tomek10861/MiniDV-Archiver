#!/bin/sh
# Regenerate frontend/vendor/tailadmin.css from tailadmin.src.css (TailAdmin v4 theme +
# Tailwind CSS v4). Requires Node + npm. The built file is committed, so running the app
# needs no build step — only re-run this after editing index.html / app.js / *.src.css.
set -eu
cd "$(dirname "$0")/.."
ROOT=$PWD
VENDOR="$ROOT/frontend/vendor"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

( cd "$TMP" && npm init -y >/dev/null 2>&1 && npm i -D tailwindcss@4 @tailwindcss/cli@4 >/dev/null 2>&1 )

# rewrite @source to absolute paths so the class scan finds the real frontend files
sed "s#@source \"../index.html\";#@source \"$ROOT/frontend/index.html\";#; \
     s#@source \"../app.js\";#@source \"$ROOT/frontend/app.js\";#" \
  "$VENDOR/tailadmin.src.css" > "$TMP/input.css"

"$TMP/node_modules/.bin/tailwindcss" -i "$TMP/input.css" -o "$VENDOR/tailadmin.css" --minify
echo "wrote frontend/vendor/tailadmin.css ($(wc -c < "$VENDOR/tailadmin.css") bytes)"
