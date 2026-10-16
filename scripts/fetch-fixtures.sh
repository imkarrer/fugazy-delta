#!/usr/bin/env bash
# Download real GoPro sample files and build GoPro's reference GPMF parser
# into tests/fixtures/real/ (gitignored). Tests marked `fixtures` skip
# until this has run.
#
# Source: github.com/gopro/gpmf-parser, dual-licensed Apache-2.0 OR MIT.
# The samples are fetched rather than committed to keep the repo small;
# pinned to a commit and checked by SHA-256 so the fixtures cannot drift.
set -euo pipefail

COMMIT=9a7150632892c7356c91145c889016f07b0ed48d
BASE="https://raw.githubusercontent.com/gopro/gpmf-parser/$COMMIT"
DEST="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/real"
mkdir -p "$DEST"

fetch() {
  local name=$1 sha=$2
  if [ -f "$DEST/$name" ] && echo "$sha  $DEST/$name" | shasum -a 256 -c --status; then
    return
  fi
  curl -fsSL "$BASE/samples/$name" -o "$DEST/$name"
  echo "$sha  $DEST/$name" | shasum -a 256 -c --status || {
    echo "checksum mismatch for $name" >&2
    exit 1
  }
}

fetch hero5.mp4 04e45b2f41dff195b525fa18b7ed5517e5fc2d651127d1b57d1d931de306f915
fetch hero6.mp4 84aebc4e370ef9081f9015bf310d7a858d431258f6b0f2160d731c4308249c67
fetch hero8.mp4 0e068f543ebf59bccb4228e7b5950a4f681753d2bbbf1c838ae60336bc75c1bd

if [ ! -x "$DEST/gpmf_demo" ]; then
  src=$(mktemp -d)
  trap 'rm -rf "$src"' EXIT
  for f in GPMF_parser.c GPMF_parser.h GPMF_utils.c GPMF_utils.h GPMF_common.h GPMF_bitstream.h \
           demo/GPMF_demo.c demo/GPMF_mp4reader.c demo/GPMF_mp4reader.h demo/GPMF_print.c; do
    mkdir -p "$src/$(dirname "$f")"
    curl -fsSL "$BASE/$f" -o "$src/$f"
  done
  cc -O1 -w -o "$DEST/gpmf_demo" \
    "$src/demo/GPMF_demo.c" "$src/demo/GPMF_mp4reader.c" "$src/demo/GPMF_print.c" \
    "$src/GPMF_parser.c" "$src/GPMF_utils.c" -lm
fi

echo "fixtures ready in $DEST"
