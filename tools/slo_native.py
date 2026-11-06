"""Versioned, in-process wire adapter for the required Rust SLO processor.

Python supplies scalar coercion/representation and final numeric text formatting,
so Python import callers retain their original types and Python's exact g formats.
The Rust core owns validation, merging, duration arithmetic, ordering and rules.
This module never builds, installs, searches Cargo outputs or runs an oracle.
"""

from __future__ import annotations

import builtins
import ctypes
import hashlib
import json
import math
import platform
import secrets
import struct
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
ABI = 1


def host_platform(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    system = {"win32": "windows"}.get(system, system)
    machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)
    if system not in {"linux", "darwin", "windows"} or machine not in {"aarch64", "x86_64"}:
        raise RuntimeError(f"unsupported SLO native platform: {system}/{machine}")
    return system, machine


def library_name(system: str) -> str:
    return {"darwin": "libslo_core.dylib", "linux": "libslo_core.so", "windows": "slo_core.dll"}[
        system
    ]


def native_directory() -> Path:
    return REPO / "tools" / "_native" / "-".join(host_platform())


def source_fingerprint() -> str:
    root = REPO / "native" / "slo-core"
    paths = [root / "Cargo.toml", root / "Cargo.lock", *sorted((root / "src").rglob("*.rs"))]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _library():
    directory = native_directory()
    path = directory / library_name(host_platform()[0])
    instruction = "build explicitly with `python tools/build_slo_native.py` (or `make slo-native`)"
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if (
            manifest["abi"] != ABI
            or manifest["platform"] != list(host_platform())
            or manifest["source_sha256"] != source_fingerprint()
            or manifest["library_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise ValueError("stale or mismatched native artifact")
        lib = ctypes.CDLL(str(path))
        lib.slo_core_abi_version.argtypes = []
        lib.slo_core_abi_version.restype = ctypes.c_uint32
        if lib.slo_core_abi_version() != ABI:
            raise ValueError("SLO native ABI mismatch")
        lib.slo_core_call.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.slo_core_call.restype = ctypes.c_void_p
        lib.slo_core_free.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.slo_core_free.restype = None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"SLO native core unavailable: {exc}; {instruction}") from exc
    return lib


class _Wire:
    def __init__(self):
        self.refs: list[Any] = []
        self.nodes: list[dict[str, Any]] = []
        self._ids: dict[int, int] = {}
        self._packed = 0
        self.surrogates: dict[str, str] = {}
        self.marker = "\0SLOSURROGATE" + secrets.token_hex(16)

    def json_safe(self, value: Any) -> Any:
        # YAML rejects lone surrogates, but direct Python import callers can
        # supply them. Reversible, per-call markers keep JSON valid without
        # changing str/repr, substitution or the resulting Python exception.
        if isinstance(value, str):
            for char in set(value):
                if 0xD800 <= ord(char) <= 0xDFFF:
                    marker = self.marker + str(ord(char)) + "\0"
                    self.surrogates[marker] = char
                    value = value.replace(char, marker)
            return value
        if isinstance(value, list):
            return [self.json_safe(v) for v in value]
        if isinstance(value, dict):
            return {k: self.json_safe(v) for k, v in value.items()}
        return value

    def restore(self, text: str) -> str:
        for marker, char in self.surrogates.items():
            text = text.replace(marker, char)
        return text

    def pack(self, value: Any) -> dict[str, Any]:
        # A flat object graph preserves aliases and cycles without recursive
        # descent. The original object remains the output ref.
        root = self._reference(value)["node"]
        while self._packed < len(self.refs):
            index = self._packed
            self._packed += 1
            self.nodes[index].update(self._pack_node(self.refs[index]))
        return self.nodes[root]

    def _reference(self, value: Any) -> dict[str, int]:
        identity = id(value)
        if identity not in self._ids:
            self._ids[identity] = len(self.refs)
            self.refs.append(value)
            self.nodes.append({"id": len(self.nodes)})
        return {"node": self._ids[identity]}

    def _pack_node(self, value: Any) -> dict[str, Any]:
        node: dict[str, Any] = {"type": type(value).__name__}
        # Representation can fail even for an otherwise valid ignored value
        # (e.g. Python's large decimal-int limit). Rust raises it only when the
        # legacy operation would actually require that representation.
        for name, function in (("str", str), ("repr", repr)):
            try:
                node[name] = function(value)
            except Exception as exc:
                node[name + "_error"] = {"type": type(exc).__name__, "message": str(exc)}
        # Coercion metadata is lazy *in Rust*: a conversion error is raised only
        # if that operation actually requests float(value). No validation here.
        try:
            node["number"] = {"bits": struct.pack("!d", float(value)).hex()}
        except Exception as exc:
            node["number"] = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        # Hex is exact and is not subject to Python's decimal digit limit.
        # Integral floats use the same canonical form for Python key equality.
        if isinstance(value, (int, bool)):
            node["integer"] = hex(int(value))
        elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
            node["integer"] = hex(int(value))
        if isinstance(value, str):
            node["type"] = "str"
            node["capitalize_parts"] = "".join(part.capitalize() for part in value.split("-"))
            # Character property only; Rust performs the duration grammar and
            # unbounded decimal multiplication/comparison, including Python's $.
            node["decimal"] = "".join(
                str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value
            )
            digits = node["decimal"].removesuffix("\n")[:-1]
            limit = sys.get_int_max_str_digits()
            if limit and len(digits) > limit and digits.isascii() and digits.isdecimal():
                try:
                    int(digits)
                except ValueError as exc:
                    node["int_error"] = str(exc)
        elif isinstance(value, dict):
            node["type"] = "dict"
            node["items"] = [[self._reference(k), self._reference(v)] for k, v in value.items()]
        elif isinstance(value, (list, tuple)):
            node["type"] = "list" if isinstance(value, list) else "tuple"
            node["items"] = [self._reference(v) for v in value]
        return node

    def unpack(self, value: dict[str, Any]) -> Any:
        if "ref" in value:
            index = value["ref"]
            if not isinstance(index, int) or not 0 <= index < len(self.refs):
                raise RuntimeError("invalid SLO native reference")
            return self.refs[index]
        if "dict" in value:
            return {self.unpack(k): self.unpack(v) for k, v in value["dict"]}
        if "list" in value:
            return [self.unpack(v) for v in value["list"]]
        if "text" in value:
            return "".join(self.unpack(v) for v in value["text"])
        if "format" in value:
            number = struct.unpack("!d", bytes.fromhex(value["format"]))[0]
            return format(number, f".{value['precision']}g")
        if "str" in value:
            return self.restore(value["str"])
        if "integer" in value:
            # int(input) may be within Python's digit limit while its duration
            # product exceeds it. Decode in bounded chunks, as a wire integer.
            digits = value["integer"]
            number = 0
            for start in range(0, len(digits), 512):
                chunk = digits[start : start + 512]
                number = number * 10 ** len(chunk) + int(chunk)
            return number
        if "none" in value:
            return None
        raise RuntimeError("invalid SLO native result")


def call(operation: str, *args: Any) -> Any:
    wire = _Wire()
    arguments = [{"node": wire.pack(v)["id"]} for v in args]
    payload = json.dumps(
        wire.json_safe({"abi": ABI, "op": operation, "args": arguments, "nodes": wire.nodes}),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    lib = _library()
    size = ctypes.c_size_t()
    pointer = lib.slo_core_call(payload, len(payload), ctypes.byref(size))
    if not pointer:
        raise RuntimeError("SLO native core returned a null buffer")
    try:
        result = json.loads(ctypes.string_at(pointer, size.value))
        if result.get("abi") != ABI:
            raise RuntimeError("SLO native response ABI mismatch")
        if "error" in result:
            error = result["error"]
            kind = error["type"]
            if kind not in {
                "ValueError",
                "TypeError",
                "KeyError",
                "AttributeError",
                "OverflowError",
                "RecursionError",
            }:
                raise RuntimeError(error["message"])
            raise getattr(builtins, kind)(wire.restore(error["message"]))
        return wire.unpack(result["value"])
    finally:
        lib.slo_core_free(pointer, size.value)
