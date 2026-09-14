"""Build the AgentCore Runtime code artifact (deterministic zip).

The zip root is the repo root, because src/fairbill/config.py computes
REPO_ROOT = Path(__file__).resolve().parents[2]; with src/fairbill/ two levels
down from the zip root, gallery/ and data/ resolve exactly as they do locally.
/var/task (the zip root) is on sys.path, not /var/task/src, so main.py at the
root adds src/ before importing the entrypoint module.

Dependencies are installed for Linux ARM64 / CPython 3.13 because AgentCore
Runtime is ARM64 only ("Runtime currently only supports ARM64 architecture" --
_sources/agentcore/runtime-get-started-code-deploy-python.md).

    python deploy/build_runtime_zip.py [--skip-deps]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN_DIR = REPO / "_runs" / "2026-09-13_phase6_deploy"
ZIP_PATH = RUN_DIR / "fairbill_runtime.zip"
BUILD_DIR = RUN_DIR / "build"

# fastapi/uvicorn are the local web tier (the Lambda proxy replaces them) and
# reportlab only renders the gallery PDFs at build time.
DROP_PACKAGES = {"fastapi", "uvicorn", "reportlab"}

MAIN_PY = '''"""AgentCore Runtime entrypoint shim: the zip root is on sys.path, src/ is not."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from fairbill.runtime import app  # noqa: E402

app.run()
'''

# Everything the runtime reads at request time. A whitelist, never a glob over
# data/: data/raw/ alone is 1.5 GB and would blow the 250 MB artifact limit.
CODE_MODULES = ["config", "schema", "tools", "mrf", "codes", "reader", "fetch",
                "audit", "letters", "ledger", "guard", "decisions", "runtime", "__init__"]
DATA_FILES = ["data/hospitals.json", "data/gallery_codes.txt",
              "data/fixtures/norman_regional_slice.json", "data/fixtures/ou_health_slice.json",
              "gallery/truth.json"]
DATA_GLOBS = ["gallery/results/*.json", "gallery/bills/*"]


def requirements() -> list[str]:
    out = []
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name = line.split("==")[0].split(">")[0].split("[")[0].strip().lower()
        if name in DROP_PACKAGES:
            continue
        out.append(line)
    return out


# pip keeps the *running* interpreter's sys_platform when resolving for another
# --platform, so on Windows `mcp` drags in pywin32, which has no aarch64 wheel
# and makes the resolve impossible. So: resolve natively to get the exact
# version set, drop the Windows-only packages, then install that pinned set for
# aarch64 with --no-deps.
WINDOWS_ONLY = {"pywin32", "pywin32-ctypes", "colorama"}


def resolve(reqs: list[str], work: Path) -> list[str]:
    report = work / "resolve.json"
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--dry-run",
                    "--ignore-installed", "--report", str(report), "--target", str(work / "x"),
                    *reqs], check=True)
    doc = json.loads(report.read_text(encoding="utf-8"))
    pins = []
    for item in doc["install"]:
        meta = item["metadata"]
        name = meta["name"].lower().replace("_", "-")
        if name in WINDOWS_ONLY:
            continue
        pins.append(f"{meta['name']}=={meta['version']}")
    return sorted(pins)


def install_deps(dest: Path) -> None:
    work = dest.parent / "resolve"
    work.mkdir(parents=True, exist_ok=True)
    pins = resolve(requirements(), work)
    print(f"resolved {len(pins)} packages; installing for aarch64 / cp313 -> {dest}")
    (dest.parent / "pinned_requirements.txt").write_text(chr(10).join(pins) + chr(10), encoding="utf-8")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--target", str(dest),
           # Two tags, newest last so pip prefers it: pymupdf and opencv stopped
           # publishing manylinux2014 aarch64 wheels, and the AgentCore Python
           # 3.13 base is Amazon Linux 2023 (glibc 2.34), which satisfies 2_28.
           "--platform", "manylinux2014_aarch64", "--platform", "manylinux_2_28_aarch64",
           "--only-binary=:all:",
           "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313", *pins]
    subprocess.run(cmd, check=True)
    for junk in list(dest.rglob("__pycache__")) + list(dest.glob("*.dist-info/RECORD")):
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)


def elf_machines(root: Path) -> dict[str, int]:
    """e_machine of every bundled shared object. 0xB7 == EM_AARCH64."""
    out = {}
    for so in sorted(root.rglob("*.so")) + sorted(root.rglob("*.so.*")):
        head = so.open("rb").read(20)
        if head[:4] == b"\x7fELF":
            out[str(so.relative_to(root))] = int.from_bytes(head[18:20], "little")
    return out


def collect() -> list[tuple[Path, str]]:
    """(source path, archive name) pairs, sorted for a deterministic zip."""
    pairs: list[tuple[Path, str]] = []
    for mod in CODE_MODULES:
        p = REPO / "src" / "fairbill" / f"{mod}.py"
        if p.exists():
            pairs.append((p, f"src/fairbill/{mod}.py"))
    for rel in DATA_FILES:
        pairs.append((REPO / rel, rel))
    for pat in DATA_GLOBS:
        for p in sorted(REPO.glob(pat)):
            if p.is_file():
                pairs.append((p, p.relative_to(REPO).as_posix()))
    missing = [a for s, a in pairs if not s.exists()]
    if missing:
        raise SystemExit(f"missing required files: {missing}")
    return pairs


def write_zip(pairs: list[tuple[Path, str]], deps: Path | None) -> None:
    """The docs require 644 on files and 755 on directories. zipfile.write() on
    Windows stamps create_system=0, which makes Unix extractors ignore the mode
    bits entirely, so every entry is written through an explicit ZipInfo."""
    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    dirs: set[str] = set()
    entries: list[tuple[Path | None, str, bytes | None]] = [(None, "main.py", MAIN_PY.encode())]
    entries += [(s, a, None) for s, a in pairs]
    if deps is not None:
        for p in sorted(deps.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                entries.append((p, p.relative_to(deps).as_posix(), None))
    for _s, arc, _b in entries:
        parts = arc.split("/")[:-1]
        for i in range(len(parts)):
            dirs.add("/".join(parts[: i + 1]) + "/")

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in sorted(dirs):
            zi = zipfile.ZipInfo(d, date_time=(1980, 1, 1, 0, 0, 0))
            zi.create_system = 3
            zi.external_attr = (0o755 << 16) | 0x10
            z.writestr(zi, b"")
        for src, arc, blob in sorted(entries, key=lambda e: e[1]):
            zi = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            zi.create_system = 3
            zi.external_attr = 0o644 << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, blob if blob is not None else src.read_bytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-deps", action="store_true",
                    help="code+data only; used by the local layout proof")
    a = ap.parse_args()

    t0 = time.time()
    deps = None
    if not a.skip_deps:
        if BUILD_DIR.exists():
            shutil.rmtree(BUILD_DIR)
        BUILD_DIR.mkdir(parents=True)
        install_deps(BUILD_DIR)
        deps = BUILD_DIR
        machines = elf_machines(BUILD_DIR)
        bad = {k: hex(v) for k, v in machines.items() if v != 0xB7}
        print(f"ELF check: {len(machines)} shared objects, {len(bad)} not AARCH64")
        if bad:
            for k, v in list(bad.items())[:10]:
                print(f"  NOT ARM64 {k} e_machine={v}")
            return 1

    pairs = collect()
    write_zip(pairs, deps)

    unzipped = sum(zi.file_size for zi in zipfile.ZipFile(ZIP_PATH).infolist())
    n = len(zipfile.ZipFile(ZIP_PATH).infolist())
    print(f"zip:      {ZIP_PATH}")
    print(f"entries:  {n}")
    print(f"zipped:   {ZIP_PATH.stat().st_size:,} bytes "
          f"({ZIP_PATH.stat().st_size / 1e6:.1f} MB of the 250 MB limit)")
    print(f"unzipped: {unzipped:,} bytes ({unzipped / 1e6:.1f} MB of the 750 MB limit)")
    print(f"built in  {time.time() - t0:.1f}s")
    if ZIP_PATH.stat().st_size > 250e6 or unzipped > 750e6:
        print("OVER LIMIT")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
