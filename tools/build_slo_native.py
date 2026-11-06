#!/usr/bin/env python3
"""Explicitly build and stage the host Rust SLO library; never invoked on import."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

if __package__:
    from .slo_native import (
        ABI,
        REPO,
        host_platform,
        library_name,
        native_directory,
        source_fingerprint,
    )
else:
    from slo_native import (
        ABI,
        REPO,
        host_platform,
        library_name,
        native_directory,
        source_fingerprint,
    )


def build() -> Path:
    fingerprint = source_fingerprint()
    # --message-format locates the artifact even with a shared CARGO_TARGET_DIR.
    # A foreign --target artifact must never be installed as a host library.
    command = [
        "cargo",
        "build",
        "--manifest-path",
        str(REPO / "native/slo-core/Cargo.toml"),
        "--locked",
        "--release",
        "--message-format=json-render-diagnostics",
    ]
    result = subprocess.run(command, cwd=REPO, text=True, stdout=subprocess.PIPE, check=True)
    filename = library_name(host_platform()[0])
    artifacts = []
    for line in result.stdout.splitlines():
        message = json.loads(line)
        if message.get("reason") == "compiler-artifact" and message["target"]["name"] == "slo_core":
            artifacts.extend(Path(p) for p in message["filenames"] if Path(p).name == filename)
    if len(artifacts) != 1:
        raise RuntimeError(f"expected one host SLO cdylib, found {artifacts}")
    # Cargo's configured target can differ from the interpreter's host. Validate
    # it in a fresh process before staging, without importing generator code.
    subprocess.run(
        [
            os.sys.executable,
            "-c",
            (
                "import ctypes, sys; lib = ctypes.CDLL(sys.argv[1]); "
                "lib.slo_core_abi_version.restype = ctypes.c_uint32; "
                f"assert lib.slo_core_abi_version() == {ABI}"
            ),
            str(artifacts[0]),
        ],
        check=True,
    )
    if source_fingerprint() != fingerprint:
        raise RuntimeError("SLO native source changed during the build; rebuild explicitly")
    directory = native_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=directory) as temporary:
        staged = Path(temporary) / filename
        shutil.copyfile(artifacts[0], staged)
        manifest = {
            "abi": ABI,
            "platform": list(host_platform()),
            "source_sha256": fingerprint,
            "library_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        }
        metadata = Path(temporary) / "manifest.json"
        metadata.write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(staged, directory / filename)
        os.replace(metadata, directory / "manifest.json")
    print(f"staged {directory / filename}")
    return directory / filename


if __name__ == "__main__":
    build()
