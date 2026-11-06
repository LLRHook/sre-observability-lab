"""Compatibility and packaging contracts; all inputs are synthetic or checked-in specs.

Native tests deliberately fail when the explicit build step is absent. The legacy
file is an immutable test oracle, never a production import or fallback.
"""

from __future__ import annotations

import contextlib
import copy
import ctypes
import hashlib
import importlib.util
import io
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import build_slo_native
import slo_gen
import slo_native

REPO = Path(__file__).resolve().parent.parent
ORACLE_PATH = REPO / "tests/oracles/slo_gen_legacy.py"
ORACLE_SHA256 = "36990188bc1d6c99c0041ae499a9df3d00631675268aa6ba00eb0da337f910c8"
spec = importlib.util.spec_from_file_location("slo_test_oracle", ORACLE_PATH)
ORACLE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ORACLE)
# The unchanged file computes REPO from __file__; relocate only its test context.
ORACLE.REPO = REPO
ORACLE.DEFAULT_SPEC = slo_gen.DEFAULT_SPEC
ORACLE.DEFAULT_OUT = slo_gen.DEFAULT_OUT


def synthetic_spec():
    return {
        "apiVersion": "lab/v1",
        "defaults": {
            "alerting": {
                "page": {
                    "for": "2m",
                    "windows": [{"long": "1h", "short": "5m", "burn_rate": 14.4}],
                },
                "ticket": {"windows": [{"long": "1d", "short": "2h", "burn_rate": 3}]},
            }
        },
        "slos": [
            {
                "name": "synthetic-api",
                "service": "synthetic",
                "objective": 99.9,
                "sli": {
                    "type": "ratio",
                    "errors": " sum(rate(errors[{{window}}]))\n ",
                    "total": "sum(rate(total[{{window}}]))",
                },
                "runbook_url": "https://example.invalid/runbook",
                "dashboard_url": "https://example.invalid/dashboard",
            }
        ],
    }


def outcome(function, *args):
    try:
        return ("value", function(*args))
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        return ("error", type(exc).__name__, str(exc))


def assert_parity(operation, *args):
    expected = outcome(getattr(ORACLE, operation), *args)
    actual = outcome(getattr(slo_gen, operation), *args)
    assert actual == expected
    return actual


def test_oracle_is_frozen_and_production_has_no_oracle_import():
    assert hashlib.sha256(ORACLE_PATH.read_text().encode()).hexdigest() == ORACLE_SHA256
    for path in (REPO / "tools").glob("*.py"):
        assert "slo_gen_legacy" not in path.read_text()


@pytest.mark.parametrize(
    "objective",
    [
        99.9,
        99.0,
        99.99999999999999,
        0.00000001,
        1.234565,
        9.999995,
        "99.9000",
        " ９９.９ ",
        "9_9.5",
        True,
        1,
        "1e-8",
    ],
)
def test_objective_coercion_and_python_numeric_formatting(objective):
    source = synthetic_spec()
    source["slos"][0]["objective"] = objective
    rules = assert_parity("generate", source)[1]
    assert slo_gen.to_yaml(rules, Path("synthetic.yaml")) == ORACLE.to_yaml(
        ORACLE.generate(source), Path("synthetic.yaml")
    )


@pytest.mark.parametrize(
    "duration",
    [
        "5m",
        "0s",
        "0001h",
        "1w",
        "60m",
        "１h",
        "١٢m",
        "𝟙d",
        "1h\n",
        "9" * 150 + "w",
        "5 minutes",
        "1ms",
        "1H",
        "1h\n\n",
        " 5m",
        "5m ",
        "²h",
        "5ｍ",
        "\u0301",
        "",
        None,
        True,
        5,
        1.5,
        ["1h"],
        b"1h",
        "9" * 4300 + "w",
        "9" * 4400 + "s",
        "\ud800h",
    ],
    ids=lambda value: str(value)[:80] if len(str(value)) > 80 else None,
)
def test_duration_unicode_unbounded_int_and_errors(duration):
    assert_parity("duration_seconds", duration)


@pytest.mark.parametrize(
    "query,window",
    [
        (" a\u001c\u001d\u001e\u001f {{window}}\u2003b\u0085c ", "1h"),
        ("x[{{window}}] + y[{{window}}]", "5m"),
        ("no template", "5m"),
        ("{{window}}", "\t1h\n"),
        (None, "1h"),
        ("x", 1),
        ("x", None),
        (b"x", "1h"),
        ("x\ud800{{window}}", "1h"),
    ],
)
def test_render_python_whitespace_and_replacement_contract(query, window):
    assert_parity("render", query, window)


def test_full_spec_golden_bytes_and_fresh_labels():
    source = yaml.safe_load(slo_gen.DEFAULT_SPEC.read_text())
    before = copy.deepcopy(source)
    expected = ORACLE.generate(source)
    actual = slo_gen.generate(source)
    assert actual == expected
    text = slo_gen.to_yaml(actual, Path("slo/slos.yaml"))
    assert text == slo_gen.DEFAULT_OUT.read_text()
    assert text == ORACLE.to_yaml(expected, Path("slo/slos.yaml"))
    assert "&id" not in text and "*id" not in text
    actual["groups"][0]["rules"][0]["labels"]["service"] = "changed"
    assert actual["groups"][0]["rules"][1]["labels"]["service"] == "ticketing-api"
    assert source == before


def test_defaults_overrides_order_and_permissive_legacy_semantics():
    source = synthetic_spec()
    source["defaults"]["window"] = "7d"
    base = source["defaults"]["alerting"]
    base["page"]["extra"] = {"leave": "alone"}
    base["page"]["windows"] = [
        {"long": "60m", "short": "5m", "burn_rate": "14.4000"},
        {"long": "1h", "short": "05m", "burn_rate": float("inf")},
    ]
    a = source["slos"][0]
    a.update({"team": None, "description": "服务 😀\nsynthetic", "window": "60m"})
    b = copy.deepcopy(a)
    b.update({"name": "0-b--", "service": 7, "description": None, "objective": "99.000"})
    b.pop("window")
    # validate_spec historically validates default ladders only. A per-SLO
    # override with reversed windows/nonpositive burn is still rendered.
    b["alerting"] = {
        "page": {"for": None, "windows": [{"long": "1m", "short": "2m", "burn_rate": -2}]},
        "ticket": {"windows": []},
    }
    source["slos"].append(b)
    before = copy.deepcopy(source)
    assert_parity("validate_spec", source)
    assert_parity("generate", source)
    for slo in source["slos"]:
        for operation in ("merged_alerting", "recording_rules", "alert_rules"):
            assert_parity(operation, source, slo)
    assert source == before
    rec = slo_gen.recording_rules(source, a)
    names = [r["record"] for r in rec if r["record"].startswith("slo:sli_error")]
    assert names == [f"slo:sli_error:ratio_rate{w}" for w in ["5m", "05m", "60m", "1h", "2h", "1d"]]


def test_seeded_differential_mutations_cover_complete_rule_sets():
    rng = random.Random(65106)
    for index in range(80):
        source = synthetic_spec()
        source["defaults"]["window"] = rng.choice(["30d", "1w", "60m", "１d"])
        for severity in ("page", "ticket"):
            config = source["defaults"]["alerting"][severity]
            config["windows"] = [
                {
                    "long": long,
                    "short": short,
                    "burn_rate": rng.choice([0.00001, 1, 14.4, "6.000", 1e8]),
                }
                for long, short in rng.sample(
                    [("1h", "5m"), ("60m", "05m"), ("1d", "2h"), ("3d", "6h")], rng.randrange(1, 5)
                )
            ]
        a = source["slos"][0]
        a["name"] = f"synthetic-{index}"
        a["objective"] = rng.uniform(0.001, 99.999999)
        if index % 3 == 0:
            a["objective"] = str(a["objective"])
        if index % 4 == 0:
            a["alerting"] = {"page": {"for": "9m"}}
        expected = ORACLE.generate(source)
        actual = slo_gen.generate(source)
        assert slo_gen.to_yaml(actual, Path("synthetic.yaml")) == ORACLE.to_yaml(
            expected, Path("synthetic.yaml")
        )


BAD_MUTATIONS = [
    ("apiVersion", "v2"),
    ("slos", None),
    ("slos", 3),
    ("defaults", None),
    ("slos.0.name", "Bad Name"),
    ("slos.0.name", "synthetic\n"),
    ("slos.0.name", 2),
    ("slos.0.objective", 0),
    ("slos.0.objective", 100),
    ("slos.0.objective", float("nan")),
    ("slos.0.objective", float("inf")),
    ("slos.0.objective", "nope"),
    ("slos.0.objective", None),
    ("slos.0.objective", []),
    ("slos.0.objective", {}),
    ("slos.0.sli", None),
    ("slos.0.sli.type", "counter"),
    ("slos.0.sli.errors", "up"),
    ("slos.0.sli.total", None),
    ("defaults.alerting", None),
    ("defaults.alerting.page", None),
    ("defaults.alerting.page.windows", None),
    ("defaults.alerting.page.windows", [None]),
    ("defaults.alerting.page.windows.0.long", "bad"),
    ("defaults.alerting.page.windows.0.short", "2h"),
    ("defaults.alerting.page.windows.0.burn_rate", 0),
    ("defaults.alerting.page.windows.0.burn_rate", "bad"),
    ("defaults.alerting.page", [["windows", []]]),
    ("defaults.alerting.page", [["windows"]]),
    ("defaults.alerting.page", [1]),
    ("defaults.alerting.page", [([], 1), ["bad"]]),
    ("defaults.alerting.page", [(([],), 1)]),
    ("slos.0.alerting", None),
    ("slos.0.window", None),
    ("slos.0.alerting", {"page": {"windows": [{"long": "bad", "short": "1m", "burn_rate": 1}]}}),
]


def mutate(source, path, value):
    keys = path.split(".")
    target = source
    for key in keys[:-1]:
        target = target[int(key) if isinstance(target, list) else key]
    key = int(keys[-1]) if isinstance(target, list) else keys[-1]
    target[key] = value


@pytest.mark.parametrize("path,value", BAD_MUTATIONS, ids=[p for p, _ in BAD_MUTATIONS])
def test_invalid_spec_exception_type_message_and_order(path, value):
    source = synthetic_spec()
    mutate(source, path, value)
    assert_parity("validate_spec", source)
    assert_parity("generate", source)


@pytest.mark.parametrize(
    "path",
    [
        "apiVersion",
        "slos",
        "slos.0.name",
        "slos.0.service",
        "slos.0.objective",
        "slos.0.sli",
        "slos.0.sli.type",
        "slos.0.sli.errors",
        "slos.0.sli.total",
        "slos.0.runbook_url",
        "slos.0.dashboard_url",
        "defaults.alerting.page",
        "defaults.alerting.ticket.windows",
        "defaults.alerting.page.windows.0.short",
        "defaults.alerting.page.windows.0.burn_rate",
    ],
)
def test_missing_key_errors_match(path):
    source = synthetic_spec()
    keys = path.split(".")
    target = source
    for key in keys[:-1]:
        target = target[int(key) if isinstance(target, list) else key]
    del target[keys[-1]]
    assert_parity("validate_spec", source)
    assert_parity("generate", source)


def test_duplicate_names_empty_specs_and_import_helpers():
    source = synthetic_spec()
    source["slos"].append(copy.deepcopy(source["slos"][0]))
    assert_parity("generate", source)
    source["slos"] = []
    assert_parity("generate", source)
    for root in [None, [], "lab/v1", 1, {}, {"apiVersion": "lab/v1"}]:
        assert_parity("generate", root)
    for name in ["a-b", "服务--x", "straße", "a\ud800-b", None, 1, b"a-b"]:
        assert_parity("metric_name", name)


@pytest.mark.parametrize(
    "field,value",
    [
        ("objective", 100),
        ("objective", "bad"),
        ("name", "straße--服务"),
        ("name", 42),
        ("name", b"synthetic"),
        ("description", None),
        ("service", ["synthetic", 1]),
        ("window", None),
    ],
)
def test_direct_rule_helper_import_contracts_without_implicit_validation(field, value):
    source = synthetic_spec()
    slo = source["slos"][0]
    slo[field] = value
    for operation in ("recording_rules", "alert_rules", "merged_alerting"):
        assert_parity(operation, source, slo)


@pytest.mark.parametrize("populated", [True, False])
def test_alert_helper_defers_large_name_error_until_clause_interpolation(populated):
    source = synthetic_spec()
    slo = source["slos"][0]
    slo["name"] = 10**4400
    if not populated:
        source["defaults"]["alerting"]["page"]["windows"] = []
    expected = outcome(ORACLE.alert_rules, source, slo)
    assert expected[0] == "error"
    assert expected[1] == ("ValueError" if populated else "AttributeError")
    assert_parity("alert_rules", source, slo)


def invoke_main(function, args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = outcome(function, args)
    return result, stdout.getvalue(), stderr.getvalue()


def test_main_write_check_missing_and_stale_out_contract(tmp_path):
    source = tmp_path / "input.yaml"
    source.write_text(yaml.safe_dump(synthetic_spec()))
    out = tmp_path / "nested/rules.yml"
    for flags in [["--check"], [], ["--check"]]:
        args = ["--spec", str(source), "--out", str(out), *flags]
        if out.exists():
            old = out.read_bytes()
        else:
            old = None
        expected = invoke_main(ORACLE.main, args)
        expected_bytes = out.read_bytes() if out.exists() else None
        if old is None:
            out.unlink(missing_ok=True)
        else:
            out.write_bytes(old)
        assert invoke_main(slo_gen.main, args) == expected
        assert (out.read_bytes() if out.exists() else None) == expected_bytes
    out.write_text(out.read_text() + "\n# synthetic drift\n")
    args = ["--spec", str(source), "--out", str(out), "--check"]
    assert invoke_main(slo_gen.main, args) == invoke_main(ORACLE.main, args)
    for text in ["", "[invalid", "apiVersion: lab/v1\n", "apiVersion: wrong\n"]:
        source.write_text(text)
        assert invoke_main(slo_gen.main, args) == invoke_main(ORACLE.main, args)
    source.unlink()
    assert invoke_main(slo_gen.main, args) == invoke_main(ORACLE.main, args)


def child_env(tmp_path):
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(REPO / "tools")}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def test_normal_cli_and_import_do_not_launch_cargo_or_subprocess(tmp_path, monkeypatch):
    calls = []
    native = slo_native.call

    def tracked(operation, *args):
        calls.append(operation)
        return native(operation, *args)

    def no_subprocess(*args, **kwargs):
        pytest.fail("runtime tried to spawn a subprocess")

    monkeypatch.setattr(slo_gen, "_native", tracked)
    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", no_subprocess)
        source = tmp_path / "input.yaml"
        source.write_text(yaml.safe_dump(synthetic_spec()))
        assert slo_gen.main(["--spec", str(source), "--out", str(tmp_path / "out.yml")]) == 0
    assert calls == ["generate"]  # full spec once, including rule count reporting
    result = subprocess.run(
        [sys.executable, str(REPO / "tools/slo_gen.py"), "--check"],
        env=child_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "is up to date" in result.stdout
    env = child_env(tmp_path)
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tools import slo_gen; assert slo_gen.duration_seconds('1h') == 3600",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_missing_library_cli_fails_without_build_or_legacy_fallback(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ["slo_gen.py", "slo_native.py"]:
        shutil.copyfile(REPO / "tools" / name, tools / name)
    source = tmp_path / "synthetic.yaml"
    source.write_text(yaml.safe_dump(synthetic_spec()))
    out = tmp_path / "out.yaml"
    result = subprocess.run(
        [sys.executable, str(tools / "slo_gen.py"), "--spec", str(source), "--out", str(out)],
        env=child_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "SLO native core unavailable" in result.stderr
    assert "build explicitly" in result.stderr
    assert not out.exists()


@pytest.mark.parametrize(
    "system,machine,tag,filename",
    [
        ("darwin", "arm64", "darwin-aarch64", "libslo_core.dylib"),
        ("darwin", "x86_64", "darwin-x86_64", "libslo_core.dylib"),
        ("linux", "aarch64", "linux-aarch64", "libslo_core.so"),
        ("linux", "x86_64", "linux-x86_64", "libslo_core.so"),
        ("win32", "AMD64", "windows-x86_64", "slo_core.dll"),
        ("win32", "ARM64", "windows-aarch64", "slo_core.dll"),
    ],
)
def test_host_architecture_lookup(system, machine, tag, filename):
    host = slo_native.host_platform(system, machine)
    assert "-".join(host) == tag
    assert slo_native.library_name(host[0]) == filename


def test_unsupported_architecture_is_actionable():
    with pytest.raises(RuntimeError, match="unsupported SLO native platform"):
        slo_native.host_platform("linux", "mips")


def test_wire_scalar_roundtrip_preserves_python_types_and_long_integer_products():
    wire = slo_native._Wire()
    for value in [None, False, "服务\ud800\n", 123, 1.5, {"x": ["y"]}]:
        packed = wire.json_safe(wire.pack(value))
        json.dumps(packed, allow_nan=False).encode()
        assert wire.unpack({"ref": packed["id"]}) is value
        assert wire.unpack({"str": packed["str"]}) == str(value)
    digits = "9" * 4300
    expected = int(digits) * 604800
    # The product is longer than the interpreter's normal decimal-input limit.
    assert wire.unpack({"integer": "604799" + "9" * 4294 + "395200"}) == expected


def test_wire_ignored_metadata_is_finite_and_defers_representation_errors():
    recursive = []
    recursive.append(recursive)
    large = 10**4400
    source = synthetic_spec()
    source["metadata"] = {"recursive": recursive, "large": large}
    wire = slo_native._Wire()
    root = wire.pack(source)
    json.dumps(wire.json_safe({"args": [root], "nodes": wire.nodes}), allow_nan=False)
    recursive_node = next(n for n, v in zip(wire.nodes, wire.refs, strict=True) if v is recursive)
    assert recursive_node["items"] == [{"node": recursive_node["id"]}]
    large_node = next(n for n, v in zip(wire.nodes, wire.refs, strict=True) if v is large)
    assert large_node["str_error"]["type"] == "ValueError"
    assert large_node["integer"] == hex(large)
    assert wire.unpack({"ref": recursive_node["id"]}) is recursive


def test_wire_numeric_keys_retain_exact_integers():
    wire = slo_native._Wire()
    values = [2**53, 2**53 + 1, float(2**53), True, 1, 2**4400]
    nodes = [wire.pack(v) for v in values]
    assert nodes[0]["number"] == nodes[1]["number"]
    assert nodes[0]["integer"] != nodes[1]["integer"]
    assert nodes[0]["integer"] == nodes[2]["integer"]
    assert nodes[3]["integer"] == nodes[4]["integer"]
    assert nodes[5]["integer"] == hex(values[5])


def test_wire_builtin_error_metadata_matches_the_running_interpreter():
    wire = slo_native._Wire()
    for value in (None, 1):
        node = wire.pack(value)
        expected = outcome(lambda value=value: "{{window}}" in value)
        assert (node["contains_error"]["type"], node["contains_error"]["message"]) == expected[1:]
        expected = outcome(lambda value=value: dict([value]))
        assert (node["dict_item_error"]["type"], node["dict_item_error"]["message"]) == expected[1:]
    for value in ([], ([],)):
        node = wire.pack(value)
        expected = outcome(lambda value=value: {value: None})
        failure = node["dict_key_errors"]["list"]
        assert (failure["type"], failure["message"]) == expected[1:]


@pytest.mark.parametrize("invalid_item", [None, 1, ([], 1)])
def test_dict_update_error_contract_after_a_valid_prefix(invalid_item):
    source = synthetic_spec()
    page = source["defaults"]["alerting"]["page"]
    source["defaults"]["alerting"]["page"] = [("windows", page["windows"]), invalid_item]
    assert_parity("merged_alerting", source, source["slos"][0])


@pytest.mark.parametrize("invalid", [False, True])
def test_wire_ignored_metadata_reaches_native_lookup(monkeypatch, invalid):
    source = synthetic_spec()
    source["metadata"] = []
    source["metadata"].extend([source["metadata"], 10**4400])
    if invalid:
        source["apiVersion"] = "wrong"
    calls = []

    def lookup():
        calls.append(True)
        raise LookupError("native lookup sentinel")

    monkeypatch.setattr(slo_native, "_library", lookup)
    with pytest.raises(LookupError, match="native lookup sentinel"):
        slo_gen.validate_spec(source)
    assert calls == [True]


@pytest.mark.parametrize("invalid", [False, True])
def test_ignored_metadata_preserves_generation_and_first_validation_error(invalid):
    source = synthetic_spec()
    recursive = []
    recursive.append(recursive)
    source["metadata"] = {"recursive": recursive, "large": 10**4400}
    if invalid:
        source["apiVersion"] = "wrong"
    assert_parity("validate_spec", source)
    assert_parity("generate", source)


def test_recursive_yaml_metadata_cli_matches_frozen_oracle(tmp_path):
    source = slo_gen.DEFAULT_SPEC.read_text() + "\nmetadata: &note [*note]\n"
    spec_path = tmp_path / "recursive.yaml"
    spec_path.write_text(source)
    paths = [tmp_path / "old.yml", tmp_path / "new.yml"]
    for module, output in zip((ORACLE, slo_gen), paths, strict=True):
        with contextlib.redirect_stdout(io.StringIO()):
            assert module.main(["--spec", str(spec_path), "--out", str(output)]) == 0
    assert paths[0].read_bytes() == paths[1].read_bytes()


def test_merge_preserves_opaque_recursive_values_and_large_integer_keys():
    source = synthetic_spec()
    recursive = []
    recursive.append(recursive)
    base = source["defaults"]["alerting"]["page"]
    base.update({2**53: "old", "metadata": recursive})
    slo = source["slos"][0]
    slo["alerting"] = {"page": {2**53 + 1: "new", float(2**53): "same-key"}}
    expected = ORACLE.merged_alerting(source, slo)
    actual = slo_gen.merged_alerting(source, slo)
    assert list(actual["page"]) == list(expected["page"])
    assert actual["page"][2**53] == "same-key"
    assert actual["page"][2**53 + 1] == "new"
    assert actual["page"]["metadata"] is recursive


@pytest.mark.parametrize(
    "old_key,new_key",
    [
        (2**53, 2**53 + 1),
        (2**53 + 1, float(2**53)),
        (-(2**53) - 1, float(-(2**53))),
        (1, True),
        (2**100, float(2**100)),
        ((2**53,), (2**53 + 1,)),
    ],
)
def test_merge_numeric_key_equality_matches_python(old_key, new_key):
    source = synthetic_spec()
    source["defaults"]["alerting"]["page"][old_key] = "old"
    slo = source["slos"][0]
    slo["alerting"] = {"page": {new_key: "new"}}
    assert_parity("merged_alerting", source, slo)


@pytest.mark.parametrize("source_changed", [False, True])
def test_explicit_builder_discovers_shared_cargo_artifact_and_stages_manifest(
    tmp_path, monkeypatch, source_changed
):
    filename = slo_native.library_name(slo_native.host_platform()[0])
    artifact = tmp_path / "shared-target/release" / filename
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"synthetic-library")
    staged = tmp_path / "staged-host"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "cargo":
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "reason": "compiler-artifact",
                        "target": {"name": "slo_core"},
                        "filenames": [str(artifact), str(artifact.with_suffix(".rlib"))],
                    }
                )
                + "\n"
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_slo_native.subprocess, "run", run)
    monkeypatch.setattr(build_slo_native, "native_directory", lambda: staged)
    if source_changed:
        fingerprints = iter(["before", "after"])
        monkeypatch.setattr(build_slo_native, "source_fingerprint", lambda: next(fingerprints))
        with pytest.raises(RuntimeError, match="changed during the build"):
            build_slo_native.build()
        assert not staged.exists()
        return
    assert build_slo_native.build() == staged / filename
    manifest = json.loads((staged / "manifest.json").read_text())
    assert manifest["source_sha256"] == slo_native.source_fingerprint()
    assert manifest["library_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert manifest["platform"] == list(slo_native.host_platform())
    assert "--locked" in calls[0] and "--release" in calls[0]
    assert calls[1][0] == sys.executable  # host load check before staging


@pytest.mark.parametrize(
    "mutation", ["absent", "stale-source", "wrong-arch", "corrupt-library", "wrong-abi"]
)
def test_missing_stale_or_foreign_package_fails_closed(tmp_path, monkeypatch, mutation):
    filename = slo_native.library_name(slo_native.host_platform()[0])
    artifact = tmp_path / filename
    artifact.write_bytes(b"synthetic")
    manifest = {
        "abi": slo_native.ABI,
        "platform": list(slo_native.host_platform()),
        "source_sha256": slo_native.source_fingerprint(),
        "library_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    if mutation == "stale-source":
        manifest["source_sha256"] = "stale"
    elif mutation == "wrong-arch":
        manifest["platform"] = ["linux", "mips"]
    elif mutation == "corrupt-library":
        artifact.write_bytes(b"changed")
    elif mutation == "wrong-abi":
        manifest["abi"] = 99
    if mutation != "absent":
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(slo_native, "native_directory", lambda: tmp_path)

    def no_load(path):
        pytest.fail("mismatched artifact was loaded")

    monkeypatch.setattr(ctypes, "CDLL", no_load)
    with pytest.raises(RuntimeError, match="build explicitly"):
        slo_native._library.__wrapped__()


class FakeLibrary:
    def __init__(self, response):
        self.buffer = ctypes.create_string_buffer(response)
        self.freed = []

    def slo_core_call(self, payload, length, size):
        assert len(payload) == length
        size._obj.value = len(self.buffer.raw) - 1
        return ctypes.addressof(self.buffer)

    def slo_core_free(self, pointer, size):
        self.freed.append((pointer, size))


@pytest.mark.parametrize(
    "response,exception",
    [
        (b"{", json.JSONDecodeError),
        (b'{"abi":9,"value":{"none":true}}', RuntimeError),
        (b'{"abi":1,"error":{"type":"ValueError","message":"synthetic"}}', ValueError),
        (b'{"abi":1,"error":{"type":"KeyError","message":"missing"}}', KeyError),
        (b'{"abi":1,"error":{"type":"RuntimeError","message":"panic"}}', RuntimeError),
        (b'{"abi":1,"value":{"ref":99999}}', RuntimeError),
    ],
)
def test_native_buffer_freed_on_decode_protocol_and_error_paths(monkeypatch, response, exception):
    lib = FakeLibrary(response)
    monkeypatch.setattr(slo_native, "_library", lambda: lib)
    with pytest.raises(exception):
        slo_native.call("generate", synthetic_spec())
    assert lib.freed == [(ctypes.addressof(lib.buffer), len(response))]


def test_native_null_buffer_is_reported(monkeypatch):
    lib = SimpleNamespace(slo_core_call=lambda *args: None)
    monkeypatch.setattr(slo_native, "_library", lambda: lib)
    with pytest.raises(RuntimeError, match="null buffer"):
        slo_native.call("generate", synthetic_spec())


def test_native_success_buffer_freed_once(monkeypatch):
    lib = FakeLibrary(b'{"abi":1,"value":{"str":"synthetic"}}')
    monkeypatch.setattr(slo_native, "_library", lambda: lib)
    assert slo_native.call("metric_name", "a-b") == "synthetic"
    assert len(lib.freed) == 1


@pytest.mark.parametrize("failure", ["abi", "load", "symbol"])
def test_native_loader_checks_real_abi_and_required_symbols(tmp_path, monkeypatch, failure):
    filename = slo_native.library_name(slo_native.host_platform()[0])
    artifact = tmp_path / filename
    artifact.write_bytes(b"synthetic-library")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "abi": slo_native.ABI,
                "platform": list(slo_native.host_platform()),
                "source_sha256": slo_native.source_fingerprint(),
                "library_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        )
    )
    monkeypatch.setattr(slo_native, "native_directory", lambda: tmp_path)

    class Version:
        def __call__(self):
            return 9 if failure == "abi" else slo_native.ABI

    def load(path):
        if failure == "load":
            raise OSError("synthetic wrong binary architecture")
        return SimpleNamespace(slo_core_abi_version=Version())

    monkeypatch.setattr(ctypes, "CDLL", load)
    with pytest.raises(RuntimeError, match="build explicitly"):
        slo_native._library.__wrapped__()


def test_real_native_abi_errors_and_repeated_ownership():
    lib = slo_native._library()
    assert lib.slo_core_abi_version() == slo_native.ABI
    for payload in [b"", b"\xff", b"{", b"null", b'{"abi":9}', b'{"abi":1,"op":"bad","args":[]}']:
        size = ctypes.c_size_t()
        pointer = lib.slo_core_call(payload, len(payload), ctypes.byref(size))
        assert pointer
        try:
            result = json.loads(ctypes.string_at(pointer, size.value))
            assert result["error"]["type"] == "RuntimeError"
        finally:
            lib.slo_core_free(pointer, size.value)
    for _ in range(64):
        assert slo_gen.duration_seconds("1h") == 3600


def test_unicode_scalar_labels_and_shallow_merge_identity():
    source = synthetic_spec()
    slo = source["slos"][0]
    slo["service"] = {"synthetic": ["服务", 7]}
    slo["team"] = False
    slo["runbook_url"] = None
    slo["dashboard_url"] = 42
    assert_parity("generate", source)
    merged = slo_gen.merged_alerting(source, slo)
    assert merged["page"]["windows"] is source["defaults"]["alerting"]["page"]["windows"]
