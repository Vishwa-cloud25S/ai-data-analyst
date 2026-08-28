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
    from ui.layer_editor import render as render_layer_editor
except ImportError:  # pragma: no cover
    from api_client import ApiClient, ApiError
    from charts import build_figure
    from layer_editor import render as render_layer_editor

API_URL = os.getenv("API_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY") or None

st.set_page_config(page_title="AI Data Analyst", page_icon="📊", layout="wide",
                   initial_sidebar_state="expanded")

# Streamlit's defaults read as "prototype". A buyer judges the screen in front
# of them, so this tightens typography, spacing and the result surfaces without
# turning the tool into a brochure - numbers stay the loudest thing on screen.
st.markdown("""
<style>
  :root { --ink:#0f1720; --mute:#6b7a8c; --line:#e3e8ee; --accent:#0b64d0;
          --good:#0a7c53; --warn:#b3261e; --soft:#f5f7fa; }
  .block-container { padding-top: 3rem; padding-bottom: 3rem; max-width: 1180px; }
  h1, h2, h3 { letter-spacing: -0.02em; color: var(--ink); }
  #MainMenu, footer { visibility: hidden; }

  /* hero */
  .ada-hero { border-bottom: 1px solid var(--line); padding-bottom: 1.1rem;
              margin-bottom: 1.4rem; }
  .ada-eyebrow { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                 font-size: .72rem; letter-spacing: .16em; text-transform: uppercase;
                 color: var(--accent); margin-bottom: .35rem; }
  .ada-title { font-size: 2.1rem; font-weight: 750; line-height: 1.12;
               letter-spacing: -.03em; color: var(--ink); margin: 0 0 .35rem; }
  .ada-sub { color: var(--mute); font-size: .96rem; margin: 0; }

  /* answer surface */
  .ada-answer { background: linear-gradient(180deg,#f7fbff,#f2f7fd);
                border: 1px solid #d6e6f8; border-left: 4px solid var(--accent);
                border-radius: 10px; padding: 1.05rem 1.2rem; font-size: 1.06rem;
                line-height: 1.6; color: var(--ink); }
  .ada-refused { background: #fff7f6; border: 1px solid #f3d3cf;
                 border-left: 4px solid var(--warn); border-radius: 10px;
                 padding: 1.05rem 1.2rem; color: #7d2018; font-size: 1rem; }

  /* sidebar */
  section[data-testid="stSidebar"] { background: var(--soft);
                                     border-right: 1px solid var(--line); }
  section[data-testid="stSidebar"] .block-container { padding-top: 1.4rem; }

  /* controls */
  .stButton > button { border-radius: 8px; font-weight: 550; border: 1px solid var(--line);
                       transition: all .14s ease; }
  .stButton > button:hover { border-color: var(--accent); color: var(--accent);
                             transform: translateY(-1px); }
  .stButton > button[kind="primary"] { background: var(--accent); border-color: var(--accent); }
  .stTextInput input { border-radius: 8px; font-size: 1rem; padding: .65rem .8rem; }
  .stTabs [data-baseweb="tab"] { font-weight: 560; }
  div[data-testid="stExpander"] { border: 1px solid var(--line); border-radius: 9px; }
  code { font-size: .84em; }
</style>
""", unsafe_allow_html=True)

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


client = ApiClient(API_URL, api_key=API_KEY)


@st.cache_data(ttl=30, show_spinner=False)
def cached_get(path: str) -> dict:
    """Returns {"ok": bool, "data"|"error": ...} so callers must handle failure."""
    try:
        return {"ok": True, "data": ApiClient(API_URL, api_key=API_KEY).get(path)}
    except ApiError as exc:
        return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("📊 AI Data Analyst")
    st.caption("Natural language → governed SQL → chart + explanation")

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
        # Show the endpoint actually in use. After a fallback the configured
        # value is not the one serving traffic, and displaying the stale one
        # sends people to debug a URL the app already stopped using.
        effective = client.base_url
        if effective.rstrip("/") != (API_URL or "").rstrip("/"):
            st.caption(f"API: `{effective}`")
            st.info(f"`API_URL` is set to `{API_URL}`, which is unreachable. "
                    f"Fell back to the public endpoint. Set `API_URL` to "
                    f"`{effective}` to remove the guesswork.", icon="⚠️")
        else:
            st.caption(f"API: `{effective}`")

        if health.get("auth_enabled"):
            who = cached_get("/whoami")
            if who.get("ok"):
                st.caption(f"Signed in as **{who['data']['name']}** "
                           f"(`{who['data']['role']}`)")
            else:
                st.warning("Authentication is required by this API. "
                           "Set API_KEY in the UI environment.")
        else:
            st.caption(f"Auth disabled — callers are anonymous "
                       f"(`{health.get('anonymous_role', 'analyst')}` role)")
    except ApiError as exc:
        st.caption(f"API: `{API_URL}`")
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
st.markdown(
    '<div class="ada-hero">'
    '<div class="ada-eyebrow">Governed natural-language analytics</div>'
    '<div class="ada-title">Ask a question about the business</div>'
    '<p class="ada-sub">Every answer is produced by validated SQL against certified '
    'metrics — and shown with the query that produced it.</p>'
    "</div>",
    unsafe_allow_html=True,
)

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
        st.markdown(f'<div class="ada-refused"><b>Refused.</b> '
                    f'{res.get("answer", "")}</div>', unsafe_allow_html=True)
        for issue in res.get("issues", []):
            st.caption(f"• {issue}")
    elif status == "error":
        st.error(res.get("answer"))
    else:
        st.markdown(f'<div class="ada-answer">{res.get("answer") or "(no answer)"}</div>',
                    unsafe_allow_html=True)
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

# ------------------------------------------- semantic layer editor (admin only)
render_layer_editor(client, ApiError)

# ------------------------------------------------------- audit (admin only)
stats_resp = cached_get("/audit/stats")
if stats_resp.get("ok"):
    st.divider()
    with st.expander("🔎 Audit log — every question asked, answered or refused"):
        stats = stats_resp["data"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Questions", stats.get("total_questions", 0))
        c2.metric("Refused", stats.get("by_status", {}).get("refused", 0))
        c3.metric("Refusal rate", f"{stats.get('refusal_rate', 0) * 100:.0f}%")

        by_stage = stats.get("refusals_by_stage") or {}
        if by_stage:
            st.caption("Refusals by blocking stage: " +
                       ", ".join(f"{k} ({v})" for k, v in by_stage.items()))

        ev = cached_get("/audit?limit=50")
        if ev.get("ok") and ev["data"]["events"]:
            rows = [
                {
                    "when": e["ts"][:19].replace("T", " "),
                    "who": e["principal"],
                    "question": e["question"][:60],
                    "status": e["status"],
                    "blocked at": e["blocked_stage"] or "-",
                    "rows": e["row_count"],
                    "ms": round(e["duration_ms"]),
                }
                for e in ev["data"]["events"]
            ]
            audit_df = pd.DataFrame(rows)
            st.dataframe(audit_df, use_container_width=True, hide_index=True)
            st.download_button("Download audit CSV", audit_df.to_csv(index=False),
                               file_name="audit.csv", mime="text/csv")
        else:
            st.caption("No audit events yet.")
