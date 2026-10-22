#!/usr/bin/env python3
"""Write (and check) a hash-pinned lock that installs on x86_64 *and* aarch64.

The lock pins every package to one version and to the SHA-256 of every Linux
wheel of that version that the target interpreter could pick, for each
architecture. pip in --require-hashes mode then accepts exactly those files
and nothing else, on either machine. One `pip install --require-hashes -r`
line keeps working everywhere.

Why not `pip install --dry-run --report` (how the 0.3.x locks were made):
it records the one wheel pip picked on the machine it ran on, so an x86_64
lock carries x86_64 hashes only and fails on ARM with "THESE PACKAGES DO NOT
MATCH THE HASHES". And `pip --platform` evaluates environment markers for the
machine pip runs on, not the target, so it cannot resolve a dependency like
`mediapipe; platform_machine == 'x86_64'` for aarch64 correctly. uv's
resolver does evaluate markers for a target (`--python-platform`), so each
architecture is resolved with `uv pip compile`, and this script

* keeps, per package, only the wheels for that architecture and Python
  (manylinux, cp3XY / abi3 / py3, not free-threaded), looked up by hash in
  PyPI's JSON API for that exact release;
* merges the architectures: one line when they agree on the version, else one
  line per architecture with a `platform_machine` marker (a package only one
  of them needs, like mediapipe, gets a marker too);
* pins a local wheel (a release candidate, a patched build) by its own hash.

    tools/mklock.py make -r 'glanced[runtime,gui]==0.3.7' -o glanced-0.3.7.lock
    tools/mklock.py make -r 'glanced[runtime,gui] @ file:///.../glanced-0.3.7-py3-none-any.whl' \
        -c old.lock -o glanced-0.3.7.lock          # -c: keep these versions where possible
    tools/mklock.py verify glanced-0.3.7.lock    # every hash is PyPI's, nothing missing

Needs `uv` on PATH for `make`, and network access to pypi.org for both.
Install with:  pip install --require-hashes --only-binary :all: -r <lock>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

ARCHES = ("x86_64", "aarch64")
DEFAULT_PYTHON = "3.14"
#: glibc the resolver may assume. Omarchy is Arch, which is far newer; wheels
#: needing a newer glibc than a machine has are simply never picked there.
DEFAULT_GLIBC = "2_40"


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --- wheel tags ------------------------------------------------------------------


def wheel_supports(filename: str, arch: str, python: str = DEFAULT_PYTHON) -> bool:
    """Could CPython `python` on glibc Linux `arch` install this wheel?"""
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    py_tags, abi_tags, plat_tags = (set(p.split(".")) for p in parts[-3:])
    major, minor = (int(x) for x in python.split(".")[:2])
    cp = f"cp{major}{minor}"
    if not any(p == "any" or (p.startswith("manylinux") and p.endswith("_" + arch)) for p in plat_tags):
        return False
    if abi_tags & {cp, "none"}:
        return cp in py_tags or any(t in (f"py{major}", f"py{major}{minor}") for t in py_tags)
    if "abi3" in abi_tags:
        return any(re.fullmatch(rf"cp{major}(\d+)", t) and int(t[3:]) <= minor for t in py_tags)
    return False


# --- parsing ---------------------------------------------------------------------


@dataclass
class Pin:
    name: str
    version: str
    hashes: set[str] = field(default_factory=set)
    #: For a local wheel: its path.
    local: Optional[Path] = None


_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?:==\s*([^\s;\\]+)|@\s*(\S+))")


def parse_compiled(text: str) -> dict[str, Pin]:
    """`uv pip compile --generate-hashes` output (or a lock) -> pins by name."""
    pins: dict[str, Pin] = {}
    current: Optional[Pin] = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("--hash") else raw.strip()
        if not line.strip():
            continue
        if line.lstrip().startswith("--hash="):
            if current is not None:
                current.hashes.update(re.findall(r"sha256:([0-9a-f]{64})", line))
            continue
        match = _REQ.match(line.strip())
        if not match:
            continue
        name, version, url = match.groups()
        local = None
        if url:
            path = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
            local = path
            version = path.name.split("-")[1]
        current = Pin(canonical(name), version, set(re.findall(r"sha256:([0-9a-f]{64})", line)), local)
        pins[current.name] = current
    return pins


# --- PyPI ------------------------------------------------------------------------

Index = Callable[[str, str], dict[str, str]]  # (name, version) -> {sha256: filename}

_cache: dict[tuple[str, str], dict[str, str]] = {}


def pypi_files(name: str, version: str) -> dict[str, str]:
    key = (name, version)
    if key not in _cache:
        url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.load(response)
        _cache[key] = {f["digests"]["sha256"]: f["filename"] for f in data["urls"]}
    return _cache[key]


# --- making ----------------------------------------------------------------------


@dataclass
class Entry:
    name: str
    version: str
    hashes: list[str]
    #: Architectures this line applies to; all of them means no marker.
    arches: tuple[str, ...]
    comment: str = ""


def wheels_for(pin: Pin, arch: str, python: str, index: Index) -> tuple[set[str], list[str]]:
    if pin.local is not None:
        digest = hashlib.sha256(pin.local.read_bytes()).hexdigest()
        return {digest}, [pin.local.name]
    files = index(pin.name, pin.version)
    chosen = {h: f for h, f in files.items() if wheel_supports(f, arch, python)}
    if not chosen:
        raise SystemExit(f"{pin.name}=={pin.version}: PyPI has no {arch} wheel for Python {python}")
    return set(chosen), sorted(chosen.values())


def merge(per_arch: dict[str, dict[str, Pin]], python: str, index: Index) -> list[Entry]:
    arches = tuple(per_arch)
    names = sorted({name for pins in per_arch.values() for name in pins})
    entries: list[Entry] = []
    for name in names:
        by_version: dict[str, list[str]] = {}
        for arch in arches:
            pin = per_arch[arch].get(name)
            if pin is not None:
                by_version.setdefault(pin.version, []).append(arch)
        for version, version_arches in sorted(by_version.items()):
            hashes: set[str] = set()
            for arch in version_arches:
                found, _ = wheels_for(per_arch[arch][name], arch, python, index)
                hashes |= found
            entries.append(Entry(name, version, sorted(hashes), tuple(version_arches)))
    return entries


def marker(arches: Iterable[str], all_arches: tuple[str, ...]) -> str:
    arches = tuple(arches)
    if set(arches) == set(all_arches):
        return ""
    return " ; " + " or ".join(f"platform_machine == '{a}'" for a in arches)


def render(entries: list[Entry], all_arches: tuple[str, ...], header: str) -> str:
    out = [header.rstrip(), ""]
    for entry in entries:
        pin = f"{entry.name}=={entry.version}{marker(entry.arches, all_arches)}"
        out.append(" \\\n".join([pin] + [f"    --hash=sha256:{h}" for h in entry.hashes]))
    return "\n".join(out) + "\n"


def compile_for(arch: str, requirement: str, python: str, glibc: str, constraint: Optional[Path]) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        req = Path(tmp) / "requirements.in"
        req.write_text(requirement + "\n")
        out = Path(tmp) / "out.txt"
        command = ["uv", "pip", "compile", "--quiet", "--python-platform", f"{arch}-manylinux_{glibc}",
                   "--python-version", python, "--generate-hashes", "--no-header", "--no-annotate",
                   "--only-binary", ":all:", str(req), "-o", str(out)]
        if constraint is not None:
            cons = Path(tmp) / "constraints.txt"
            cons.write_text("\n".join(f"{p.name}=={p.version}" for p in parse_compiled(constraint.read_text()).values()
                                      if p.local is None) + "\n")
            command[3:3] = ["-c", str(cons)]
        subprocess.run(command, check=True)
        return out.read_text()


def _make(args) -> int:
    per_arch = {arch: parse_compiled(compile_for(arch, args.requirement, args.python, args.glibc, args.constraint))
                for arch in args.arch}
    entries = merge(per_arch, args.python, pypi_files)
    header = "\n".join([
        f"# {args.requirement}",
        f"# Linux {' + '.join(args.arch)}, CPython {args.python}; every package pinned to one version and",
        "# to the SHA-256 of each wheel of it that interpreter can install on that architecture.",
        "# A line with a platform_machine marker applies to that architecture only.",
        "#",
        f"# Generated with: tools/mklock.py make -r '{args.requirement}'"
        + (f" -c {args.constraint.name}" if args.constraint else "")
        + "".join(f" --arch {a}" for a in args.arch),
        "# Install with:   pip install --require-hashes --only-binary :all: -r <this file>",
    ])
    args.output.write_text(render(entries, tuple(args.arch), header))
    print(f"wrote {args.output}: {len(entries)} pins for {', '.join(args.arch)}")
    return 0


# --- verifying -------------------------------------------------------------------

_MARKER_ARCH = re.compile(r"platform_machine\s*==\s*['\"]([^'\"]+)['\"]")


def parse_lock(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for block in re.split(r"\n(?=[A-Za-z0-9])", "\n".join(l for l in text.splitlines() if not l.startswith("#"))):
        block = block.strip()
        if not block:
            continue
        head = block.split("\\", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9._-]+)==([^\s;]+)\s*(?:;(.*))?$", head)
        if not match:
            raise SystemExit(f"cannot parse lock entry: {head!r}")
        name, version, mark = match.groups()
        arches = tuple(_MARKER_ARCH.findall(mark or ""))
        entries.append(Entry(canonical(name), version, sorted(set(re.findall(r"sha256:([0-9a-f]{64})", block))),
                             arches))
    return entries


def check(entries: list[Entry], all_arches: tuple[str, ...], python: str, index: Index,
          local: Iterable[str] = ()) -> list[str]:
    """Problems with the lock, empty when it is complete and every hash is PyPI's."""
    problems = []
    local = {canonical(n) for n in local}
    seen: dict[str, set[str]] = {}
    for entry in entries:
        arches = entry.arches or all_arches
        for arch in arches:
            seen.setdefault(entry.name, set())
            if arch in seen[entry.name]:
                problems.append(f"{entry.name}: two lines apply on {arch}")
            seen[entry.name].add(arch)
        if not entry.hashes:
            problems.append(f"{entry.name}=={entry.version}: no hashes")
        if entry.name in local:
            continue
        files = index(entry.name, entry.version)
        unknown = [h for h in entry.hashes if h not in files]
        if unknown:
            problems.append(f"{entry.name}=={entry.version}: hashes PyPI does not have: {unknown}")
        for arch in arches:
            usable = {h: f for h, f in files.items() if wheel_supports(f, arch, python)}
            if not usable:
                problems.append(f"{entry.name}=={entry.version}: no {arch} wheel on PyPI")
            missing = sorted(f for h, f in usable.items() if h not in entry.hashes)
            if missing:
                problems.append(f"{entry.name}=={entry.version}: {arch} wheels pip could pick but the lock refuses: {missing}")
    return problems


def _verify(args) -> int:
    entries = parse_lock(args.lock.read_text())
    problems = check(entries, tuple(args.arch), args.python, pypi_files, local=args.local)
    for entry in entries:
        where = ", ".join(entry.arches) if entry.arches else "all"
        print(f"{entry.name}=={entry.version} [{where}]: {len(entry.hashes)} hash(es)")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("lock is complete and matches PyPI" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make", help="resolve and write a multi-architecture lock")
    make.add_argument("-r", "--requirement", required=True)
    make.add_argument("-c", "--constraint", type=Path, help="a lock or requirements file whose versions to keep")
    make.add_argument("-o", "--output", type=Path, required=True)
    make.set_defaults(func=_make)
    verify = sub.add_parser("verify", help="check a lock against PyPI")
    verify.add_argument("lock", type=Path)
    verify.add_argument("--local", action="append", default=[], help="a package pinned from a local wheel")
    verify.set_defaults(func=_verify)
    for p in (make, verify):
        p.add_argument("--arch", action="append", choices=ARCHES)
        p.add_argument("--python", default=DEFAULT_PYTHON)
    make.add_argument("--glibc", default=DEFAULT_GLIBC, help="manylinux level to resolve for, e.g. 2_40")
    args = parser.parse_args(argv)
    args.arch = tuple(args.arch or ARCHES)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
