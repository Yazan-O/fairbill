"""Stage everything the Lambda proxy serves from disk, then zip it.

The proxy is self-contained: the page, its assets, the gallery images and the
pre-baked read-only API all ship inside the function package, so a request that
needs no agent work never leaves Lambda.

    python deploy/bundle_proxy.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROXY = REPO / "deploy" / "lambda_proxy"
ZIP_PATH = REPO / "_runs" / "2026-09-13_phase6_deploy" / "fairbill_proxy.zip"


def stage() -> None:
    for name, src in (("static", REPO / "web"), ("gallery", REPO / "gallery" / "bills")):
        dest = PROXY / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for p in sorted(src.iterdir()):
            if p.is_file() and p.suffix.lower() != ".md":
                shutil.copy2(p, dest / p.name)
        print(f"{name}/: {len(list(dest.iterdir()))} files from {src.relative_to(REPO)}")
    subprocess.run([sys.executable, str(REPO / "deploy" / "prebake.py")], check=True)


def build_zip() -> None:
    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(PROXY.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and ".bin" not in p.parts:
                arc = p.relative_to(PROXY).as_posix()
                zi = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                zi.create_system = 3
                zi.external_attr = 0o644 << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, p.read_bytes())
                n += 1
    size = ZIP_PATH.stat().st_size
    print(f"proxy zip: {ZIP_PATH} ({n} entries, {size:,} bytes, {size / 1e6:.1f} MB)")
    if size > 50e6:
        print("WARNING: over the 50 MB direct-upload limit; upload via S3 instead")


if __name__ == "__main__":
    stage()
    build_zip()
