"""Alert noise report: episode reconstruction and recommendations."""

from __future__ import annotations

import alert_noise_report as anr


def series(name: str, state: str, samples: list[int], step: int = 15, **labels):
    metric = {
        "alertname": name,
        "alertstate": state,
        "severity": "page",
        "service": "ticketing-api",
        **labels,
    }
    return {"metric": metric, "values": [[t * step, "1"] for t in samples]}


def test_episodes_split_on_gaps():
    eps = anr.episodes_from_samples(
        [[0, "1"], [15, "1"], [30, "1"], [300, "1"], [315, "1"]], step=15
    )
    assert [(e.start, e.end) for e in eps] == [(0, 45), (300, 330)]


def test_flapping_and_pending_only_are_counted():
    matrix = [
        # three firing episodes: two short flaps and one long
        series("A", "firing", [0, 1, 2] + [100, 101] + list(range(200, 260))),
        series("A", "pending", list(range(0, 3)) + [500, 501, 502]),  # last one never fired
        series("B", "pending", [10, 11, 12], severity="ticket"),
    ]
    stats = anr.build_stats(matrix, step=15)
    a = next(s for k, s in stats.items() if k.startswith("A{")).summary(flap_seconds=300)
    assert a["firings"] == 3 and a["flaps"] == 2 and a["pending_only"] == 1
    assert a["max_ttr_s"] == 60 * 15
    b = next(s for k, s in stats.items() if k.startswith("B{")).summary(flap_seconds=300)
    assert b["firings"] == 0 and b["pending_only"] == 1
    notes = anr.recommendations([a, b], flap_seconds=300)
    assert any("flapping" in n for n in notes)


def test_table_renders_without_rows():
    assert anr.render_table([]).startswith("alert")
