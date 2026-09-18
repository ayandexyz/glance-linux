#!/usr/bin/env bash
# Install the user service and the Omarchy plugin from a source checkout.
#
#   packaging/install.sh            # service + plugin
#   packaging/install.sh --no-plugin
#   packaging/install.sh --uninstall
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
plugin_dir=${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins
plugin_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$repo/plugin/manifest.json")
data_dir=${XDG_DATA_HOME:-$HOME/.local/share}/glance

want_plugin=1
uninstall=0
for arg in "$@"; do
  case $arg in
    --no-plugin) want_plugin=0 ;;
    --uninstall) uninstall=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if (( uninstall )); then
  systemctl --user disable --now glanced.service 2>/dev/null || true
  rm -f "$unit_dir/glanced.service"
  rm -f "$plugin_dir/$plugin_id"
  [[ -L $HOME/.local/bin/glancectl ]] && rm -f "$HOME/.local/bin/glancectl"
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/hooks/post-update.d/repair-glance-lock.hook"
  systemctl --user daemon-reload
  echo "removed the service, the plugin link and the post-update hook (enrollment in $data_dir is untouched)"
  echo "still wired, if you ran them: glancectl setup-pam --remove, glancectl setup-lock --remove"
  exit 0
fi

# Prefer the checkout's own venv, then whatever is on PATH.
if [[ -x $repo/.venv/bin/glancectl ]]; then
  glancectl=$repo/.venv/bin/glancectl
elif command -v glancectl >/dev/null 2>&1; then
  glancectl=$(command -v glancectl)
else
  echo "glancectl not found: create the venv first (python -m venv .venv && .venv/bin/pip install -e '.[runtime,dev]')" >&2
  exit 1
fi

mkdir -p "$unit_dir" "$data_dir" "$HOME/.local/bin"
chmod 700 "$data_dir"

# ~/.local/bin is on the PATH your terminals share; the venv is not. The link
# is for `glancectl` at a prompt. The plugin does not use it: it runs only an
# absolute path it has checked, and refuses symlinks, so it gets the venv
# binary itself through its "glancectl path" setting (printed below).
if [[ $glancectl != "$HOME/.local/bin/glancectl" ]]; then
  ln -sfn "$glancectl" "$HOME/.local/bin/glancectl"
  echo "link:    ~/.local/bin/glancectl -> $glancectl"
fi
sed "s|^ExecStart=.*|ExecStart=$glancectl daemon --mode light|" \
  "$repo/packaging/systemd/glanced.service" > "$unit_dir/glanced.service"
systemctl --user daemon-reload
systemctl --user enable --now glanced.service
echo "service: $unit_dir/glanced.service -> $glancectl"

if (( want_plugin )); then
  mkdir -p "$plugin_dir"
  ln -sfn "$repo/plugin" "$plugin_dir/$plugin_id"
  echo "plugin:  $plugin_dir/$plugin_id -> $repo/plugin"
  if command -v omarchy-shell >/dev/null 2>&1; then
    omarchy-shell shell rescanPlugins 2>/dev/null || true
  fi
  echo "enable it with: omarchy plugin enable $plugin_id"
  echo "then set the widget's 'glancectl path' setting to: $glancectl"
fi

echo
echo "next: glancectl fetch-model && glancectl enroll --name \"$USER\" --remember"
