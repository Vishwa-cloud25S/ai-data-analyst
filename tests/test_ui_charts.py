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
