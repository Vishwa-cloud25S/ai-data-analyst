"""UI chart tests.

The bar/line crash that shipped happened because the chart layer was never
exercised anywhere. These tests drive build_figure with the chart specs the
pipeline actually emits, including real end-to-end output.
"""
import pandas as pd
import pytest

from ui.charts import build_figure, prepare_frame


def test_bar_chart_builds():
    df = pd.DataFrame({"product_name": ["A", "B"], "total_revenue": [10.0, 5.0]})
    fig = build_figure(df, {"type": "bar", "x": "product_name",
                            "y": "total_revenue", "title": "t"})
    assert fig is not None
    assert fig.data[0].type == "bar"


def test_line_chart_builds_with_markers():
    df = pd.DataFrame({"period": ["2026-01-01T00:00:00", "2026-02-01T00:00:00"],
                       "total_revenue": [10.0, 12.0]})
    fig = build_figure(df, {"type": "line", "x": "period", "y": "total_revenue"})
    assert fig is not None
    assert fig.data[0].type == "scatter"
    assert fig.data[0].mode == "lines+markers"


def test_period_is_parsed_and_sorted():
    df = pd.DataFrame({"period": ["2026-03-01T00:00:00", "2026-01-01T00:00:00"],
                       "total_revenue": [3.0, 1.0]})
    fig = build_figure(df, {"type": "line", "x": "period", "y": "total_revenue"})
    xs = list(fig.data[0].x)
    assert xs == sorted(xs)


@pytest.mark.parametrize("chart", [
    {"type": "table", "x": None, "y": None},
    {"type": "bar", "x": "missing_col", "y": "total_revenue"},
    {"type": "bar", "x": "product_name", "y": None},
    {},
])
def test_unchartable_specs_return_none_instead_of_raising(chart):
    df = pd.DataFrame({"product_name": ["A"], "total_revenue": [1.0]})
    assert build_figure(df, chart) is None


def test_empty_frame_returns_none():
    assert build_figure(pd.DataFrame({"a": [], "b": []}),
                        {"type": "bar", "x": "a", "y": "b"}) is None


@pytest.mark.parametrize("question", [
    "What were our highest revenue products last quarter?",   # bar
    "Show me revenue by month for the last 12 months",        # line
    "What is the average order value by channel?",            # bar, ratio metric
    "Top 5 categories by units sold",                         # bar
])
def test_real_pipeline_output_is_always_chartable(analyst, question):
    """Every chart spec the pipeline emits must build without raising."""
    res = analyst.ask(question)
    assert res.status == "answered"
    df = prepare_frame(res.rows, res.columns)
    fig = build_figure(df, res.chart)
    assert fig is not None, f"{question!r} produced an unrenderable chart: {res.chart}"


# ---------------------------------------------------------------- formatting
def test_numpy_integers_are_formatted_not_stringified():
    """numpy.int64 is not an int, so a naive isinstance check printed 506985.

    Integer-typed money columns are the common case in a warehouse, so this
    silently un-formatted exactly the numbers people care most about.
    """
    import numpy as np

    from ui.charts import fmt_value

    assert fmt_value(np.int64(506985), "total_revenue") == "$506,985"
    assert fmt_value(np.float64(1_330_000), "revenue") == "$1.33M"
    assert fmt_value(9403, "units_sold") == "9,403"
    assert fmt_value(True, "flag") == "True"       # bools are not measurements
    assert fmt_value("Computing", "category") == "Computing"


def test_total_label_is_not_doubled():
    """'total_revenue' must not render as 'Total Total Revenue'."""
    from ui.charts import headline_stats

    df = pd.DataFrame({"product": ["a", "b"], "total_revenue": [10.0, 5.0]})
    labels = [s["label"] for s in headline_stats(
        df, {"x": "product", "y": "total_revenue", "type": "bar"})]
    assert "Total Revenue" in labels
    assert not any("Total Total" in lbl for lbl in labels)


def test_headline_stats_for_a_ranking():
    from ui.charts import headline_stats

    df = pd.DataFrame({"product": ["a", "b", "c"], "total_revenue": [60.0, 30.0, 10.0]})
    stats = headline_stats(df, {"x": "product", "y": "total_revenue", "type": "bar"})
    assert stats[0]["value"] == "a"
    assert "60" in stats[0]["delta"]
    assert "60%" in stats[1]["delta"]      # leader's share of the total


def test_headline_stats_for_a_trend():
    from ui.charts import headline_stats

    df = pd.DataFrame({"period": ["2026-01-01", "2026-02-01", "2026-03-01"],
                       "total_revenue": [100.0, 150.0, 200.0]})
    stats = headline_stats(df, {"x": "period", "y": "total_revenue", "type": "line"})
    assert stats[0]["label"] == "Latest"
    assert "+100.0%" in stats[0]["delta"]
    assert stats[2]["label"] == "Peak"


def test_headline_stats_never_raises_on_odd_shapes():
    from ui.charts import headline_stats

    assert headline_stats(pd.DataFrame(), {"x": "a", "y": "b"}) == []
    assert headline_stats(pd.DataFrame({"a": ["x"]}), {"x": "a", "y": "missing"}) == []
    assert headline_stats(pd.DataFrame({"a": ["x"], "b": ["not a number"]}),
                          {"x": "a", "y": "b"}) == []


def test_rankings_are_horizontal_for_label_legibility():
    df = pd.DataFrame({"product_name": ["A very long product name indeed", "B"],
                       "total_revenue": [10.0, 5.0]})
    fig = build_figure(df, {"type": "bar", "x": "product_name", "y": "total_revenue"})
    assert fig.data[0].orientation == "h"
