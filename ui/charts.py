"""Chart construction for the Streamlit UI.

Kept free of Streamlit imports so it can be unit-tested in CI without a
browser session or a running server.

Styling choices here are about legibility, not decoration: rankings read better
horizontally because category labels stay horizontal, money is formatted as
money, and gridlines are quiet so the bars carry the signal.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

INK = "#0f1720"
MUTE = "#6b7a8c"
GRID = "#eef2f6"
ACCENT = "#0b64d0"
ACCENT_SOFT = "#7fb3f0"

CURRENCY_HINTS = ("revenue", "margin", "value", "amount", "price", "cost",
                  "discount", "sales", "profit", "spend")


def prepare_frame(rows: list[list[Any]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def is_currency(column: str) -> bool:
    return any(h in (column or "").lower() for h in CURRENCY_HINTS)


def fmt_value(value: Any, column: str = "") -> str:
    """Format a number the way a person would write it.

    Accepts numpy scalars too: numpy.int64 is not an instance of int, so a
    plain isinstance check silently returned the raw digits for every
    integer-typed column - which is exactly the money column in most
    warehouses.
    """
    import numbers

    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return str(value)
    value = float(value)
    prefix = "$" if is_currency(column) else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{prefix}{value / 1_000_000:,.2f}M"
    if magnitude >= 1_000:
        return f"{prefix}{value:,.0f}"
    if magnitude < 1 and magnitude > 0 and not prefix:
        return f"{value:.1%}" if magnitude < 1 else f"{value:,.2f}"
    return f"{prefix}{value:,.2f}"


def _style(fig: go.Figure, title: str = "") -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=INK)) if title else None,
        margin={"l": 8, "r": 12, "t": 44 if title else 14, "b": 8},
        height=430,
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="-apple-system, Segoe UI, Roboto, sans-serif",
                  size=13, color=MUTE),
        showlegend=False,
        hoverlabel=dict(bgcolor="white", font_size=13, bordercolor=GRID),
    )
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor=GRID,
                     tickfont=dict(color=MUTE))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor="rgba(0,0,0,0)",
                     tickfont=dict(color=MUTE))
    return fig


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
    money = is_currency(y)
    tick_prefix = "$" if money else ""

    if ctype == "line":
        plot_df[x] = pd.to_datetime(plot_df[x], errors="coerce")
        plot_df = plot_df.sort_values(x)
        fig = px.line(plot_df, x=x, y=y, markers=True)
        fig.update_traces(
            line=dict(color=ACCENT, width=2.5),
            marker=dict(size=6, color=ACCENT),
            hovertemplate=f"%{{x|%b %Y}}<br><b>{tick_prefix}%{{y:,.0f}}</b><extra></extra>",
        )
        fig = _style(fig)
        fig.update_yaxes(tickprefix=tick_prefix, tickformat="~s")
        return fig

    # Rankings read better horizontally: category labels stay horizontal and
    # legible however long they are, and the eye compares bar lengths easily.
    plot_df = plot_df.sort_values(y, ascending=True).tail(20)
    shades = [ACCENT if i == len(plot_df) - 1 else ACCENT_SOFT
              for i in range(len(plot_df))]
    fig = go.Figure(go.Bar(
        x=plot_df[y], y=plot_df[x].astype(str), orientation="h",
        marker=dict(color=shades, line=dict(width=0)),
        text=[fmt_value(v, y) for v in plot_df[y]],
        textposition="outside",
        textfont=dict(size=12, color=MUTE),
        hovertemplate=f"%{{y}}<br><b>{tick_prefix}%{{x:,.2f}}</b><extra></extra>",
        cliponaxis=False,
    ))
    fig = _style(fig)
    fig.update_xaxes(showticklabels=False, showgrid=False,
                     range=[0, float(plot_df[y].max()) * 1.18])
    fig.update_yaxes(gridcolor="rgba(0,0,0,0)",
                     tickfont=dict(color=INK, size=13))
    fig.update_layout(height=max(320, 46 * len(plot_df) + 60))
    return fig


def headline_stats(df: pd.DataFrame, chart: dict[str, Any]) -> list[dict[str, str]]:
    """Two or three numbers worth reading before the chart.

    A wall of rows makes the reader do the work of finding the answer; the
    answer usually is the leader, the total and the spread.
    """
    x, y = (chart or {}).get("x"), (chart or {}).get("y")
    if not y or y not in df.columns or df.empty:
        return []
    series = pd.to_numeric(df[y], errors="coerce").dropna()
    if series.empty:
        return []

    stats: list[dict[str, str]] = []
    label = y.replace("_", " ").title()
    # "total_revenue" would otherwise render as "Total Total Revenue".
    total_label = label if label.lower().startswith("total") else f"Total {label}"

    if x and x in df.columns and len(df) > 1 and x != "period":
        top_idx = series.idxmax()
        stats.append({"label": f"Top {x.replace('_', ' ')}",
                      "value": str(df.loc[top_idx, x]),
                      "delta": fmt_value(series.loc[top_idx], y)})
        total = series.sum()
        share = series.loc[top_idx] / total * 100 if total else 0
        stats.append({"label": total_label, "value": fmt_value(total, y),
                      "delta": f"leader is {share:.0f}% of it"})
    elif x == "period" and len(series) > 1:
        first, last = series.iloc[0], series.iloc[-1]
        change = ((last - first) / first * 100) if first else 0
        stats.append({"label": "Latest", "value": fmt_value(last, y),
                      "delta": f"{change:+.1f}% vs first period"})
        stats.append({"label": total_label, "value": fmt_value(series.sum(), y),
                      "delta": f"across {len(series)} periods"})
        stats.append({"label": "Peak", "value": fmt_value(series.max(), y),
                      "delta": str(df.loc[series.idxmax(), x])[:10]})
    else:
        stats.append({"label": label, "value": fmt_value(series.iloc[0], y),
                      "delta": ""})
    return stats
