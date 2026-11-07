#!/usr/bin/env python3
"""Prepare a checksum-verified Linux AMD64 worker bundle before running Harbor.

Requires Python 3.13 with pip. No credentials or running task environment needed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

VERSION = "2.2.0"
ARCHIVE = "mcp-grafana_Linux_x86_64.tar.gz"
SHA256 = "f93bfbd94cde68cc2e43cf5ba55da4f8e28b1a36b14fb14dadd566fc36b1312b"


def prepare(destination, *, server_only=False):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    lock = Path(__file__).with_name("requirements.lock")
    if not server_only:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--require-hashes",
                "-r",
                str(lock),
                "--platform",
                "manylinux2014_x86_64",
                "--platform",
                "manylinux_2_28_x86_64",
                "--platform",
                "manylinux_2_17_x86_64",
                "--python-version",
                "3.13",
                "--implementation",
                "cp",
                "--abi",
                "cp313",
                "--only-binary=:all:",
                "--dest",
                str(destination / "wheels"),
            ],
            check=True,
        )
    archive = destination / ARCHIVE
    if not archive.exists():
        urllib.request.urlretrieve(
            f"https://github.com/grafana/mcp-grafana/releases/download/v{VERSION}/{ARCHIVE}",
            archive,
        )
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("mcp-grafana archive checksum mismatch")
    with tarfile.open(archive) as source:
        for name in ("mcp-grafana", "LICENSE", "README.md"):
            members = [
                m
                for m in source.getmembers()
                if Path(m.name).name == name and m.isfile()
            ]
            if len(members) != 1:
                raise ValueError(f"release archive missing unique {name}")
            with source.extractfile(members[0]) as stream:
                (destination / name).write_bytes(stream.read())
    (destination / "mcp-grafana").chmod(0o755)
    shutil.copyfile(lock, destination / "requirements.lock")
    files = [
        p for p in destination.rglob("*") if p.is_file() and p.name != "manifest.json"
    ]
    manifest = {
        str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(files)
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Prepared worker runtime: {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination", nargs="?", default=".cache/grafana-mcp-runtime/linux-amd64"
    )
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="Only fetch/verify the server (protocol tests, not a worker bundle)",
    )
    args = parser.parse_args()
    prepare(args.destination, server_only=args.server_only)
