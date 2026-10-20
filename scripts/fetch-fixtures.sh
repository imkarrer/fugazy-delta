#!/usr/bin/env bash
# Download real device sample files, and build GoPro's reference GPMF
# parser, into tests/fixtures/real/ (gitignored). Tests marked `fixtures`
# skip until this has run.
#
# Every file is pinned to a commit of its source repository and checked by
# SHA-256, so the fixtures cannot drift. They are fetched rather than
# committed because their licences do not let this repo redistribute them:
# GPL or AGPL, or no licence at all. Downloading them at test time does not
# redistribute them. Sources and licences are listed beside each file.
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/real"
mkdir -p "$DEST"

# fetch <repo> <commit> <path in repo, URL-encoded> <local name> <sha256>
fetch() {
  local repo=$1 commit=$2 src=$3 name=$4 sha=$5
  mkdir -p "$(dirname "$DEST/$name")"
  if [ -f "$DEST/$name" ] && echo "$sha  $DEST/$name" | shasum -a 256 -c --status; then
    return
  fi
  curl -fsSL "https://raw.githubusercontent.com/$repo/$commit/$src" -o "$DEST/$name"
  echo "$sha  $DEST/$name" | shasum -a 256 -c --status || {
    echo "checksum mismatch for $name" >&2
    exit 1
  }
}

# GoPro's samples, github.com/gopro/gpmf-parser, Apache-2.0 OR MIT. Small
# enough to commit, but kept beside the parser they are tested against.
GOPRO=9a7150632892c7356c91145c889016f07b0ed48d
fetch gopro/gpmf-parser $GOPRO samples/hero5.mp4 hero5.mp4 04e45b2f41dff195b525fa18b7ed5517e5fc2d651127d1b57d1d931de306f915
fetch gopro/gpmf-parser $GOPRO samples/hero6.mp4 hero6.mp4 84aebc4e370ef9081f9015bf310d7a858d431258f6b0f2160d731c4308249c67
fetch gopro/gpmf-parser $GOPRO samples/hero8.mp4 hero8.mp4 0e068f543ebf59bccb4228e7b5950a4f681753d2bbbf1c838ae60336bc75c1bd

# VBO. github.com/modtesla3uk-cmd/mt3uk (no licence): a RaceBox Mini export
# from Thruxton and a VBOX Sport file from Shelsley Walsh.
# github.com/haruiz/apexai (no licence): a VBOX Video HD2 file from
# Thunderhill.
MT3UK=5c4a56374ac650507fd5f48a819094b3c7cd6f48
fetch modtesla3uk-cmd/mt3uk $MT3UK tests/fixtures/thruxton-trimmed.vbo vbo/thruxton-trimmed.vbo 35393cdc96d5c0243f77b750c48823339f56d78d4ee537cfe349b9bc724d1292
fetch modtesla3uk-cmd/mt3uk $MT3UK tests/fixtures/shelsley-climb-and-descent.vbo vbo/shelsley-climb-and-descent.vbo d729ea27915a5b7f1176eb03001edc6670656a1d57c0369b2f51e4fdf4c06c92
fetch haruiz/apexai 796a1ed4c63c07b4c5a0794a1aaaefb973856660 telemetry-data/VBOX_files/VBOX0133.vbo vbo/VBOX0133.vbo 1a633252ba46a81e39f788026cede59382f7564e6970e4928e2dfe5fb7921173

# RaceBox CSV and GPX exports of one session (Lake Elsinore, 21 June
# 2026), github.com/beadon/RacePlex sample_race_files/, GPL-3.0.
RACEPLEX=1edc3716a0f8e487a39bd77feec2f2f461e6e5f6
fetch beadon/RacePlex $RACEPLEX "sample_race_files/RaceBox%20Track%20Sessionon%2021-06-2026%2013-43.csv" racebox/session.csv c4953baf0991437107bf074d983b36efe8b2840057bbbfccb37cce167e1b3824

# RaceChrono Pro CSV v3 export, Massa Finalese, 4 August 2026,
# github.com/ClassicMiniDIY/UltraLog exampleLogs/racechrono/, AGPL-3.0.
fetch ClassicMiniDIY/UltraLog ecfb727f5877b52884caca7f503c028e29e5b524 exampleLogs/racechrono/session_massa_finalese_v3_excerpt.csv racechrono/massa-finalese-v3.csv 63acea8298e71944f5b1737a33a90739ed6d9aa31e0d6a7684dcfe9020ea7099

# AiM logger files from libxrk's test data, github.com/m3rlin45/libxrk.
# The repository is MIT, but where the data came from is not stated, so
# they are fetched: AiM's own sample (Adria Kart, 23 January 2016, XRK)
# and a KK-SII session at Tsukuba (23 May 2026, XRZ).
LIBXRK=4e44d38debc9445b8e5446c9b7a241f5730ac920
fetch m3rlin45/libxrk $LIBXRK tests/test_data/aim_official/test.xrk aim/adria-kart.xrk 39b3598b9d4cf6cb3815450aec95d5272f453ad9ac16ac789aea7d7ad22f2f50
fetch m3rlin45/libxrk $LIBXRK "tests/test_data/issue84/CMD_KK-SII_Tsukuba_Car_Qualifying%20testing_a_0159.xrz" aim/tsukuba.xrz 6c08bb9c223a63a0a337a93af5238c9e5be9cfde477ff7c0f0141db654bb0ef3

if [ ! -x "$DEST/gpmf_demo" ]; then
  src=$(mktemp -d)
  trap 'rm -rf "$src"' EXIT
  for f in GPMF_parser.c GPMF_parser.h GPMF_utils.c GPMF_utils.h GPMF_common.h GPMF_bitstream.h \
           demo/GPMF_demo.c demo/GPMF_mp4reader.c demo/GPMF_mp4reader.h demo/GPMF_print.c; do
    mkdir -p "$src/$(dirname "$f")"
    curl -fsSL "https://raw.githubusercontent.com/gopro/gpmf-parser/$GOPRO/$f" -o "$src/$f"
  done
  cc -O1 -w -o "$DEST/gpmf_demo" \
    "$src/demo/GPMF_demo.c" "$src/demo/GPMF_mp4reader.c" "$src/demo/GPMF_print.c" \
    "$src/GPMF_parser.c" "$src/GPMF_utils.c" -lm
fi

# Tests check this stamp, so a checkout whose script lists more files than
# were fetched skips the real-file tests instead of failing them.
shasum -a 256 "$0" | cut -d' ' -f1 > "$DEST/.fetched"
echo "fixtures ready in $DEST"
