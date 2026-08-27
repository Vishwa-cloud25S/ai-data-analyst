"""Streamlit front-end for the AI Data Analyst.

Talks to the FastAPI service over HTTP only - it has no database access and no
LLM key of its own, which keeps the trust boundary in one place.
"""
from __future__ import annotations

import os

import pandas as pd
import requests
import streamlit as st

# streamlit run puts the script's own directory on sys.path, plain imports
# put the project root there; support both so `make ui` and Docker agree.
try:
    from ui.charts import build_figure
except ImportError:  # pragma: no cover
    from charts import build_figure

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(page_title="AI Data Analyst", page_icon="📊", layout="wide")

STAGE_LABELS = {
    "intent_detection": "1 · Intent detection",
    "schema_retrieval": "2 · Schema retrieval (RAG)",
    "sql_generation": "3 · SQL generation",
    "sql_validation": "4 · SQL validation",
    "execution": "5 · Read-only execution",
    "result_validation": "6 · Result validation",
    "explanation": "7 · Explanation",
    "metadata_answer": "Metadata answer",
}
STATUS_ICON = {"ok": "✅", "blocked": "🛑", "error": "❌", "skipped": "⏭️"}


@st.cache_data(ttl=30)
def get_json(path: str):
    try:
        return requests.get(f"{API_URL}{path}", timeout=10).json()
    except Exception as exc:
        return {"error": str(exc)}


def ask(question: str, use_llm: bool):
    return requests.post(f"{API_URL}/ask",
                         json={"question": question, "use_llm": use_llm},
                         timeout=120).json()


# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("📊 AI Data Analyst")
    st.caption("Natural language → governed SQL → chart + explanation")

    health = get_json("/health")
    if "error" in health:
        st.error(f"API unreachable at {API_URL}\n\n{health['error']}")
    else:
        st.success("API healthy")
        c1, c2 = st.columns(2)
        c1.metric("Warehouse", health.get("warehouse", "?"))
        c2.metric("Model", health.get("llm", "?"))
        st.caption(f"{health.get('entities', 0)} entities · "
                   f"{health.get('metrics', 0)} certified metrics")

    use_llm = st.toggle("Use LLM", value=True,
                        help="Off = deterministic planner only (no API calls).")

    st.divider()
    st.subheader("Guardrails")
    st.markdown(
        "- LLM sees **only** the semantic layer, never the database\n"
        "- Generated SQL is parsed and checked against an allow-list\n"
        "- Execution is on a **read-only** connection with row + time limits\n"
        "- Results are sanity-checked before they are narrated"
    )

    sem = get_json("/semantic-layer")
    if "metrics" in sem:
        with st.expander("Certified metrics"):
            for m in sem["metrics"]:
                st.markdown(f"**{m['label']}** — {m['description']}")
                st.code(m["expression"], language="sql")

# ----------------------------------------------------------------- main
st.header("Ask a question about the business")

examples = get_json("/examples").get("questions", [])
if "question" not in st.session_state:
    st.session_state.question = examples[0] if examples else ""

cols = st.columns(min(3, max(1, len(examples[:3]))))
for i, ex in enumerate(examples[:3]):
    if cols[i].button(ex, use_container_width=True):
        st.session_state.question = ex

question = st.text_input("Question", key="question",
                         placeholder="What were our highest revenue products last quarter?")
run = st.button("Analyse", type="primary")

if run and question.strip():
    with st.spinner("Running the governed pipeline…"):
        try:
            res = ask(question, use_llm)
        except Exception as exc:
            st.error(f"Request failed: {exc}")
            st.stop()

    status = res.get("status")
    if status == "refused":
        st.warning(res.get("answer", "Refused"))
        for issue in res.get("issues", []):
            st.caption(f"• {issue}")
    elif status == "error":
        st.error(res.get("answer"))
    else:
        st.success(res.get("answer"))
        conf = res.get("confidence", 0)
        st.progress(min(max(conf, 0.0), 1.0), text=f"Confidence {conf:.0%}")

        rows, columns = res.get("rows", []), res.get("columns", [])
        if rows:
            df = pd.DataFrame(rows, columns=columns)
            chart = res.get("chart", {})

            tab_chart, tab_data = st.tabs(["Chart", f"Data ({len(df)} rows)"])
            with tab_chart:
                fig = build_figure(df, chart)
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.caption("No chart for this shape of result - showing the data.")
                    st.dataframe(df, use_container_width=True)
            with tab_data:
                st.dataframe(df, use_container_width=True)
                st.download_button("Download CSV", df.to_csv(index=False),
                                   file_name="result.csv", mime="text/csv")

    if res.get("sql"):
        with st.expander("Generated SQL (validated before execution)", expanded=False):
            st.code(res["sql"], language="sql")

    for w in res.get("warnings", []):
        st.caption(f"⚠️ {w}")

    with st.expander("Pipeline trace", expanded=(status != "answered")):
        for stage in res.get("trace", []):
            icon = STATUS_ICON.get(stage["status"], "•")
            label = STAGE_LABELS.get(stage["name"], stage["name"])
            st.markdown(f"{icon} **{label}** · {stage['duration_ms']} ms")
            st.json(stage.get("detail", {}), expanded=False)

    st.caption(f"request_id `{res.get('request_id', '-')}`")
else:
    st.info("Pick an example above or type your own question, then press **Analyse**.")
