"""Site checks. Run by build.py; can also be run on its own.

    python tool/check.py                  run all checks (exit 1 on failure)
    python tool/check.py --record-app-ads record the hash of static/app-ads.txt
                                          (only after replacing the file on purpose)
"""

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
DIST = ROOT / "dist"
# Ad files that must reach the site root byte-for-byte. ads.txt is listed in case it is added later.
AD_FILES = ["app-ads.txt", "ads.txt"]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_file(name):
    return ROOT / (Path(name).stem + ".sha256")


def record(name):
    src = STATIC / name
    if not src.is_file() or src.stat().st_size == 0:
        sys.exit(f"{src} is missing or empty")
    hash_file(name).write_text(f"{sha256(src)}  {name}\n", encoding="ascii", newline="\n")
    lines = src.read_bytes().count(b"\n")
    print(f"Recorded {name}: {lines} lines, sha256 {sha256(src)} -> {hash_file(name).name}")


def check_ad_files(errors):
    for name in AD_FILES:
        src, hf = STATIC / name, hash_file(name)
        if not src.exists() and not hf.exists():
            continue  # this file isn't used by the site
        if not src.is_file() or src.stat().st_size == 0:
            errors.append(f"{name}: static/{name} is missing or empty")
            continue
        if not hf.is_file():
            errors.append(f"{name}: {hf.name} is missing (run: python tool/check.py --record-app-ads)")
            continue
        expected = hf.read_text(encoding="ascii").split()[0]
        if sha256(src) != expected:
            errors.append(f"{name}: static/{name} SHA-256 {sha256(src)} differs from {hf.name} ({expected})")
        if DIST.is_dir():
            out = DIST / name
            if not out.is_file() or out.stat().st_size == 0:
                errors.append(f"{name}: dist/{name} is missing or empty")
            elif out.read_bytes() != src.read_bytes():
                errors.append(f"{name}: dist/{name} differs from static/{name}")
            elif sha256(out) != expected:
                errors.append(f"{name}: dist/{name} SHA-256 differs from {hf.name}")


def check_private_untracked(errors):
    try:
        out = subprocess.run(["git", "ls-files", "--", "private"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return  # not a git checkout (e.g. a downloaded copy): nothing can be tracked
    tracked = out.split()
    if tracked:
        errors.append(f"git tracks {len(tracked)} file(s) under private/, e.g. {tracked[0]}")


def main():
    if "--record-app-ads" in sys.argv:
        record("app-ads.txt")
        return
    errors = []
    check_ad_files(errors)
    check_private_untracked(errors)
    for e in errors:
        print("FAIL", e)
    if errors:
        sys.exit(1)
    print("checks passed")


if __name__ == "__main__":
    main()
