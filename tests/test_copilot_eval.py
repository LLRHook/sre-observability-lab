"""Offline evaluation set: recorded incident fixtures with expected classifications.

Each fixture is a snapshot of what the copilot sees (alert + gathered context)
plus what a reviewer decided the right classification and first action are. The
rules engine must get every one right; the same fixtures are the regression set
for any prompt or model change in the LLM path (run with a real key manually).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from copilot.models import AmAlert
from copilot.summarizer import RulesSummarizer

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "incidents").glob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_rules_engine_matches_expected_classification(path: Path):
    case = load(path)
    alert = AmAlert.model_validate(case["alert"])
    events = []
    note = RulesSummarizer().summarize(alert, case["context"], lambda s, d: events.append((s, d)))
    assert note.classification.value == case["expected"]["classification"], note.summary
    assert note.proposed_actions[0].action == case["expected"]["first_action"]
    assert note.confidence >= case["expected"]["min_confidence"]
    assert all(
        a.action in {"reset_chaos", "acknowledge", "open_ticket"} for a in note.proposed_actions
    )


def test_eval_set_covers_every_classification_we_claim_to_handle():
    seen = {load(p)["expected"]["classification"] for p in FIXTURES}
    assert {
        "injected_fault",
        "dependency_failure",
        "latency_degradation",
        "error_rate_spike",
        "target_unreachable",
        "unknown",
    } <= seen


def test_eval_report_summary():
    """Prints a per-fixture table; useful when comparing summarizers by hand."""
    rows = []
    for path in FIXTURES:
        case = load(path)
        note = RulesSummarizer().summarize(
            AmAlert.model_validate(case["alert"]), case["context"], lambda s, d: None
        )
        ok = note.classification.value == case["expected"]["classification"]
        rows.append((path.stem, case["expected"]["classification"], note.classification.value, ok))
    correct = sum(1 for r in rows if r[3])
    print(f"\nrules engine: {correct}/{len(rows)} correct")
    for name, expected, got, ok in rows:
        print(f"  {'ok ' if ok else 'BAD'} {name:<40} expected={expected:<22} got={got}")
    assert correct == len(rows)
