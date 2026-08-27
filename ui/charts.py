"""Chart construction for the Streamlit UI.

Kept free of Streamlit imports so it can be unit-tested in CI without a
browser session or a running server.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px


def prepare_frame(rows: list[list[Any]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def build_figure(df: pd.DataFrame, chart: dict[str, Any]):
    """Return a plotly figure for the chart spec, or None to fall back to a table.

    Returning None (rather than raising) is deliberate: a chart is a nicety and
    must never take down an answer that the pipeline already validated.
    """
    ctype = (chart or {}).get("type", "table")
    x, y = (chart or {}).get("x"), (chart or {}).get("y")

    if ctype not in ("bar", "line"):
        return None
    if not x or not y or x not in df.columns or y not in df.columns:
        return None
    if df.empty:
        return None

    plot_df = df.copy()
    if x == "period":
        plot_df[x] = pd.to_datetime(plot_df[x], errors="coerce")
        plot_df = plot_df.sort_values(x)

    title = (chart or {}).get("title", "")
    if ctype == "line":
        fig = px.line(plot_df, x=x, y=y, title=title, markers=True)
    else:
        fig = px.bar(plot_df, x=x, y=y, title=title)

    fig.update_layout(margin={"l": 10, "r": 10, "t": 40, "b": 10}, height=430)
    return fig
