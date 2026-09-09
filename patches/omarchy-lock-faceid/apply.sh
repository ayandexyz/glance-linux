#!/usr/bin/env bash
# Install the Face ID-style indicator into the running Omarchy shell's lock
# plugin. Needs root for /usr/share/omarchy; keeps *.orig backups beside the
# files. Undo with revert.sh. An Omarchy update overwrites this — it is a
# preview of an upstream change, not a permanent install.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
lock=${OMARCHY_PATH:-/usr/share/omarchy}/shell/plugins/lock
if (( EUID != 0 )); then echo "run with sudo: sudo $0" >&2; exit 1; fi
[[ -f $lock/Service.qml ]] || { echo "lock plugin not found at $lock" >&2; exit 1; }
for f in LockView.qml Service.qml; do
  [[ -f $lock/$f.orig ]] || cp -p "$lock/$f" "$lock/$f.orig"
done
install -m 644 "$here/FaceUnlockIndicator.qml" "$lock/FaceUnlockIndicator.qml"
install -m 644 "$here/LockView.qml" "$lock/LockView.qml"
install -m 644 "$here/Service.qml" "$lock/Service.qml"
echo "patched $lock (backups: *.orig). Now, as your user: omarchy-restart-shell"
