"""Unit tests for the SLO rule generator."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

import slo_gen

REPO = Path(__file__).resolve().parent.parent
SPEC = yaml.safe_load((REPO / "slo" / "slos.yaml").read_text())


def test_committed_rules_are_up_to_date(tmp_path):
    assert slo_gen.main(["--check"]) == 0


def test_duration_parsing():
    assert slo_gen.duration_seconds("5m") == 300
    assert slo_gen.duration_seconds("1d") == 86400
    with pytest.raises(ValueError):
        slo_gen.duration_seconds("5 minutes")


def test_every_window_gets_sli_and_burn_rate_rules():
    rules = slo_gen.generate(SPEC)
    rec = next(
        g for g in rules["groups"] if g["name"] == "slo-ticketing-api-availability-recording"
    )
    records = [r["record"] for r in rec["rules"]]
    for window in ("5m", "30m", "1h", "2h", "6h", "1d", "3d", "30d"):
        assert f"slo:sli_error:ratio_rate{window}" in records
        assert f"slo:burn_rate:ratio_rate{window}" in records
    assert "slo:error_budget_remaining:ratio" in records
    assert all(
        r["labels"] == {"slo": "ticketing-api-availability", "service": "ticketing-api"}
        for r in rec["rules"]
    )


def test_burn_rate_divides_by_error_budget():
    rules = slo_gen.generate(SPEC)
    rec = next(
        g for g in rules["groups"] if g["name"] == "slo-ticketing-api-availability-recording"
    )
    br = next(r for r in rec["rules"] if r["record"] == "slo:burn_rate:ratio_rate1h")
    assert br["expr"] == 'slo:sli_error:ratio_rate1h{slo="ticketing-api-availability"} / 0.001'
    rec_lat = next(g for g in rules["groups"] if g["name"] == "slo-ticketing-api-latency-recording")
    br_lat = next(r for r in rec_lat["rules"] if r["record"] == "slo:burn_rate:ratio_rate1h")
    assert br_lat["expr"].endswith("/ 0.01")


def test_alerts_follow_sre_workbook_ladder():
    rules = slo_gen.generate(SPEC)
    alerts = next(
        g for g in rules["groups"] if g["name"] == "slo-ticketing-api-availability-alerts"
    )
    page = next(r for r in alerts["rules"] if r["alert"].endswith("Page"))
    ticket = next(r for r in alerts["rules"] if r["alert"].endswith("Ticket"))
    assert (
        "ratio_rate1h" in page["expr"]
        and "> 14.4" in page["expr"]
        and "ratio_rate5m" in page["expr"]
    )
    assert (
        "ratio_rate6h" in page["expr"] and "> 6" in page["expr"] and "ratio_rate30m" in page["expr"]
    )
    assert (
        "ratio_rate1d" in ticket["expr"]
        and "> 3" in ticket["expr"]
        and "ratio_rate2h" in ticket["expr"]
    )
    assert (
        "ratio_rate3d" in ticket["expr"]
        and "> 1" in ticket["expr"]
        and "ratio_rate6h" in ticket["expr"]
    )
    assert page["labels"]["severity"] == "page" and ticket["labels"]["severity"] == "ticket"
    for rule in (page, ticket):
        for key in ("summary", "description", "runbook_url", "dashboard_url"):
            assert rule["annotations"][key]
        for key in ("severity", "service", "slo", "team"):
            assert rule["labels"][key]


def test_short_window_is_one_twelfth_of_long_window():
    for severity in ("page", "ticket"):
        for w in SPEC["defaults"]["alerting"][severity]["windows"]:
            assert slo_gen.duration_seconds(w["long"]) == 12 * slo_gen.duration_seconds(w["short"])


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda s: s.__setitem__("apiVersion", "v2"), "apiVersion"),
        (lambda s: s["slos"][0].__setitem__("objective", 100), "objective"),
        (lambda s: s["slos"][0].__setitem__("name", "Bad Name"), "kebab"),
        (lambda s: s["slos"].append(copy.deepcopy(s["slos"][0])), "duplicate"),
        (lambda s: s["slos"][0]["sli"].__setitem__("errors", "up"), "placeholder"),
        (
            lambda s: s["defaults"]["alerting"]["page"]["windows"][0].__setitem__("short", "2h"),
            "exceed",
        ),
        (
            lambda s: s["defaults"]["alerting"]["page"]["windows"][0].__setitem__("burn_rate", 0),
            "positive",
        ),
    ],
)
def test_spec_validation_rejects_bad_specs(mutate, message):
    spec = copy.deepcopy(SPEC)
    mutate(spec)
    with pytest.raises(ValueError, match=message):
        slo_gen.generate(spec)


def test_check_mode_detects_stale_output(tmp_path):
    out = tmp_path / "rules.yml"
    assert slo_gen.main(["--out", str(out)]) == 0
    assert slo_gen.main(["--out", str(out), "--check"]) == 0
    out.write_text(out.read_text() + "\n# drift\n")
    assert slo_gen.main(["--out", str(out), "--check"]) == 1


def test_error_budget_math_documented_in_deep_dive():
    """99.9% over 30 days is 43.2 minutes of full outage."""
    objective = SPEC["slos"][0]["objective"] / 100
    minutes = 30 * 24 * 60 * (1 - objective)
    assert round(minutes, 1) == 43.2
