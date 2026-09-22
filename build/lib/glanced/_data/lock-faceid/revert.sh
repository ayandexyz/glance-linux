#!/usr/bin/env bash
set -euo pipefail
lock=${OMARCHY_PATH:-/usr/share/omarchy}/shell/plugins/lock
if (( EUID != 0 )); then echo "run with sudo: sudo $0" >&2; exit 1; fi
for f in LockView.qml Service.qml; do
  [[ -f $lock/$f.orig ]] && mv -f "$lock/$f.orig" "$lock/$f"
done
rm -f "$lock/FaceUnlockIndicator.qml" "$lock/unlock-spin.png"
echo "restored $lock. Now, as your user: omarchy-restart-shell"
