"""Streamlit front-end for the AI Data Analyst.

Talks to the FastAPI service over HTTP only - it has no database access and no
LLM key of its own, which keeps the trust boundary in one place.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

# streamlit run puts the script's own directory on sys.path, plain imports
# put the project root there; support both so `make ui` and Docker agree.
try:
    from ui.api_client import ApiClient, ApiError
    from ui.charts import build_figure
except ImportError:  # pragma: no cover
    from api_client import ApiClient, ApiError
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


client = ApiClient(API_URL)


@st.cache_data(ttl=30, show_spinner=False)
def cached_get(path: str) -> dict:
    """Returns {"ok": bool, "data"|"error": ...} so callers must handle failure."""
    try:
        return {"ok": True, "data": ApiClient(API_URL).get(path)}
    except ApiError as exc:
        return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("📊 AI Data Analyst")
    st.caption("Natural language → governed SQL → chart + explanation")

    st.caption(f"API: `{API_URL}`")
    api_ok = False
    health: dict = {}
    try:
        health = client.health()
        api_ok = True
        st.success("API healthy")
        llm_name = health["llm"]
        st.markdown(
            f"**Warehouse** `{health['warehouse']}`  \n"
            f"**Model** `{llm_name}`  \n"
            f"{health['entities']} entities · {health['metrics']} certified metrics"
        )
    except ApiError as exc:
        st.error(str(exc))

    llm_configured = api_ok and health.get("llm") != "offline-rules"
    if llm_configured:
        use_llm = st.toggle("Use LLM", value=True,
                            help="Off = deterministic planner only (no API calls).")
    else:
        use_llm = False
        st.toggle("Use LLM", value=False, disabled=True,
                  help="No OPENAI_API_KEY configured on the API, so the "
                       "deterministic planner handles every question. Set the key "
                       "and restart the API to enable the LLM path.")
        st.caption("Running keyless on the deterministic planner.")

    st.divider()
    st.subheader("Guardrails")
    st.markdown(
        "- LLM sees **only** the semantic layer, never the database\n"
        "- Generated SQL is parsed and checked against an allow-list\n"
        "- Execution is on a **read-only** connection with row + time limits\n"
        "- Results are sanity-checked before they are narrated"
    )

    sem_resp = cached_get("/semantic-layer") if api_ok else {"ok": False}
    if sem_resp.get("ok") and sem_resp["data"].get("metrics"):
        sem = sem_resp["data"]
        with st.expander("Certified metrics"):
            for m in sem["metrics"]:
                st.markdown(f"**{m['label']}** — {m['description']}")
                st.code(m["expression"], language="sql")

# ----------------------------------------------------------------- main
st.header("Ask a question about the business")

if not api_ok:
    st.error("The API is not reachable, so questions cannot be answered. "
             "See the sidebar for details.")
    st.stop()

ex_resp = cached_get("/examples")
examples = ex_resp["data"].get("questions", []) if ex_resp.get("ok") else []
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
            res = client.ask(question, use_llm)
        except ApiError as exc:
            st.error(str(exc))
            st.stop()

    status = res.get("status")
    if status == "refused":
        st.warning(res.get("answer", "Refused"))
        for issue in res.get("issues", []):
            st.caption(f"• {issue}")
    elif status == "error":
        st.error(res.get("answer"))
    else:
        st.success(res.get("answer") or "(no answer returned)")
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
