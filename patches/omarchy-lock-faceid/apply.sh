#!/usr/bin/env bash
# Install the Face ID-style indicator into the running Omarchy shell's lock
# plugin. Needs root for /usr/share/omarchy; keeps *.orig backups beside the
# files. Undo with revert.sh.
#
# `diffs/*/` holds one lockview.diff + service.diff pair per Omarchy baseline
# this has been rebased against. Rather than assume which one matches what is
# actually installed, this tries each against a scratch copy of the live
# files and keeps the first one where both diffs apply with no rejected
# hunks. An Omarchy version close enough to one of those baselines gets
# patched; one too far from all of them is left on the stock lock screen
# instead of installing wiring the running shell cannot load.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
lock=${OMARCHY_PATH:-/usr/share/omarchy}/shell/plugins/lock
if (( EUID != 0 )); then echo "run with sudo: sudo $0" >&2; exit 1; fi
[[ -f $lock/Service.qml ]] || { echo "lock plugin not found at $lock" >&2; exit 1; }

# Get a clean, unpatched base for each file: back it up the first time, or
# restore the backup if it is already patched from an earlier apply. Either
# way, *.orig ends up holding the real stock file this Omarchy ships, which
# is also what revert.sh restores.
for f in LockView.qml Service.qml; do
  if grep -q 'faceMessagePrefix\|FaceUnlockIndicator' "$lock/$f"; then
    cp -p "$lock/$f.orig" "$lock/$f"
  else
    cp -p "$lock/$f" "$lock/$f.orig"
  fi
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

matched=""
for set_dir in $(find "$here/diffs" -mindepth 1 -maxdepth 1 -type d | sort -rV); do
  cp "$lock/LockView.qml" "$tmp/LockView.qml"
  cp "$lock/Service.qml" "$tmp/Service.qml"
  if patch -p0 -s -f "$tmp/LockView.qml" < "$set_dir/lockview.diff" \
    && patch -p0 -s -f "$tmp/Service.qml" < "$set_dir/service.diff"; then
    matched=$set_dir
    break
  fi
done

if [[ -z $matched ]]; then
  echo "none of the bundled lock-screen patches (${here}/diffs/*) apply to" \
    "$lock's stock files — leaving them as stock. Face unlock still works" \
    "through PAM; only this indicator doesn't. File an issue with" \
    "'pacman -Q omarchy' if you'd like a patch rebased for your version." >&2
  exit 1
fi

install -m 644 "$tmp/LockView.qml" "$lock/LockView.qml"
install -m 644 "$tmp/Service.qml" "$lock/Service.qml"
install -m 644 "$here/FaceUnlockIndicator.qml" "$lock/FaceUnlockIndicator.qml"
install -m 644 "$here/unlock-spin.png" "$lock/unlock-spin.png"
echo "patched $lock using the $(basename "$matched") diff (backups: *.orig)." \
  "Now, as your user: omarchy-restart-shell"
