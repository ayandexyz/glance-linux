"""`glancectl install-service` — the user service, without a source checkout.

`packaging/install.sh` has always done this, but it lives in the repository
and never reaches a wheel, so anyone who installed from PyPI ended up with a
`glancectl` they could not run as a service. This is the same unit, written
by the command itself.

The unit is generated here rather than read from `packaging/systemd/`, so it
works identically from a checkout, a wheel and a distribution package. The
two must not drift: `tests/test_servicesetup.py` checks this against the file
that `packaging/install.sh` installs.

`ExecStart` is the absolute path of the running `glancectl`, resolved through
its symlinks. A pipx install puts a link in `~/.local/bin` pointing into a
venv that pipx may move on upgrade, and a unit that points at the link would
break silently the first time that happened.

The models are fetched first if they are not already on disk, because a
service that starts without them only fails later, in the journal, where
nobody is looking. `--no-fetch` skips it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "glanced.service"

#: Matches packaging/systemd/glanced.service. Keep the two in step.
UNIT_TEMPLATE = """\
[Unit]
Description=Glance face unlock daemon
Documentation=https://github.com/ayandexyz/glance-linux
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start} daemon --mode {mode}
Restart=on-failure
RestartSec=2

# The daemon needs the camera, its models, and its own data directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/glance
RuntimeDirectory=glance
RuntimeDirectoryMode=0700
RestrictAddressFamilies=AF_UNIX
MemoryDenyWriteExecute=false

[Install]
WantedBy=graphical-session.target
"""


def unit_dir() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def glancectl_path() -> Path:
    """The interpreter's own console script, with symlinks resolved.

    `sys.argv[0]` is what the user typed, which may be a link; the script
    beside the running interpreter is the real one whatever the caller did.
    """
    candidate = Path(sys.argv[0])
    if candidate.name == "glancectl" and candidate.exists():
        return candidate.resolve()

    beside = Path(sys.executable).resolve().parent / "glancectl"
    if beside.exists():
        return beside.resolve()
    return candidate.resolve()


def render_unit(exec_start: Path | str, mode: str = "light") -> str:
    return UNIT_TEMPLATE.format(exec_start=exec_start, mode=mode)


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def models_present() -> bool:
    from . import paths

    return paths.landmarker_task().exists() and paths.arcface_model().exists()


def setup(
    remove: bool = False,
    mode: str = "light",
    enable: bool = True,
    fetch: bool = True,
) -> int:
    path = unit_path()

    if remove:
        if not path.exists():
            print(f"no unit at {path}")
            return 0
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
        path.unlink()
        _systemctl("daemon-reload")
        print(f"removed {path}")
        print("your enrollment and models are untouched")
        return 0

    exec_start = glancectl_path()
    if not exec_start.exists():
        print(f"install-service: cannot find the glancectl binary at {exec_start}", file=sys.stderr)
        return 1

    if fetch and not models_present():
        from . import models

        print("fetching the models (~16MB), one time only")
        models.fetch()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(exec_start, mode))
    _systemctl("daemon-reload")
    print(f"service: {path} -> {exec_start}")

    if enable:
        _systemctl("enable", "--now", UNIT_NAME)
        print("enabled and started")
    else:
        print(f"start it with: systemctl --user enable --now {UNIT_NAME}")

    return 0
