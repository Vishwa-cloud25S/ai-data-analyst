"""Streamlit front-end for the AI Data Analyst.

Talks to the FastAPI service over HTTP only - it has no database access and no
LLM key of its own, which keeps the trust boundary in one place.

Presentation principle: the reader wants the answer, then the evidence. So the
headline numbers come first, the chart second, and the SQL and pipeline trace
are one click away rather than hidden - they are the reason to trust the number.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

# streamlit run puts the script's own directory on sys.path, plain imports
# put the project root there; support both so `make ui` and Docker agree.
try:
    from ui.api_client import ApiClient, ApiError
    from ui.charts import build_figure, headline_stats
    from ui.layer_editor import render as render_layer_editor
except ImportError:  # pragma: no cover
    from api_client import ApiClient, ApiError
    from charts import build_figure, headline_stats
    from layer_editor import render as render_layer_editor

API_URL = os.getenv("API_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY") or None

st.set_page_config(page_title="AI Data Analyst", page_icon="📊", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
  :root { --ink:#0f1720; --mute:#6b7a8c; --line:#e6ebf1; --accent:#0b64d0;
          --good:#0a7c53; --warn:#b3261e; --soft:#f5f7fa; }
  .block-container { padding-top: 2.6rem; padding-bottom: 4rem; max-width: 1180px; }
  h1,h2,h3 { letter-spacing:-.02em; color:var(--ink); }
  #MainMenu, footer, [data-testid="stDecoration"] { visibility:hidden; }

  .ada-hero { border-bottom:1px solid var(--line); padding-bottom:1rem; margin-bottom:1.5rem; }
  .ada-eyebrow { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.7rem;
                 letter-spacing:.16em; text-transform:uppercase; color:var(--accent);
                 margin-bottom:.4rem; }
  .ada-title { font-size:2.05rem; font-weight:750; line-height:1.12; letter-spacing:-.03em;
               color:var(--ink); margin:0 0 .3rem; }
  .ada-sub { color:var(--mute); font-size:.95rem; margin:0; }

  .ada-answer { background:linear-gradient(180deg,#f8fbff,#f2f7fd); border:1px solid #d9e8f9;
                border-left:4px solid var(--accent); border-radius:10px; padding:1.1rem 1.25rem;
                font-size:1.08rem; line-height:1.62; color:var(--ink); }
  .ada-refused { background:#fff8f7; border:1px solid #f4d6d2; border-left:4px solid var(--warn);
                 border-radius:10px; padding:1.1rem 1.25rem; color:#7d2018; font-size:1rem;
                 line-height:1.6; }

  .ada-kpis { display:flex; gap:14px; flex-wrap:wrap; margin:1rem 0 .4rem; }
  .ada-kpi { flex:1; min-width:170px; border:1px solid var(--line); border-radius:10px;
             padding:.8rem .95rem; background:#fff; }
  .ada-kpi .k-label { font-size:.72rem; letter-spacing:.09em; text-transform:uppercase;
                      color:var(--mute); font-family:ui-monospace,Menlo,monospace; }
  .ada-kpi .k-value { font-size:1.42rem; font-weight:700; color:var(--ink);
                      letter-spacing:-.02em; line-height:1.25; margin-top:.15rem;
                      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .ada-kpi .k-delta { font-size:.8rem; color:var(--mute); margin-top:.1rem; }

  .ada-chip { display:inline-block; font-size:.74rem; font-weight:600; padding:2px 9px;
              border-radius:20px; margin-left:.4rem; vertical-align:middle; }
  .chip-high { background:#e7f5ee; color:var(--good); }
  .chip-mid  { background:#fdf3e2; color:#8a5a06; }
  .chip-low  { background:#fdeceb; color:var(--warn); }

  section[data-testid="stSidebar"] { background:var(--soft); border-right:1px solid var(--line); }
  section[data-testid="stSidebar"] .block-container { padding-top:1.3rem; }
  .ada-status { display:flex; align-items:center; gap:.5rem; font-weight:600;
                color:var(--good); font-size:.92rem; }
  .ada-status.bad { color:var(--warn); }
  .ada-dot { width:8px; height:8px; border-radius:50%; background:var(--good); }
  .ada-dot.bad { background:var(--warn); }
  .ada-facts { font-size:.87rem; color:var(--mute); line-height:1.75; margin-top:.5rem; }
  .ada-facts b { color:var(--ink); }

  .stButton > button { border-radius:8px; font-weight:550; border:1px solid var(--line);
                       transition:all .14s ease; }
  .stButton > button:hover { border-color:var(--accent); color:var(--accent);
                             transform:translateY(-1px); }
  .stButton > button[kind="primary"] { background:var(--accent); border-color:var(--accent); }
  .stTextInput input { border-radius:8px; font-size:1rem; padding:.68rem .85rem; }
  .stTabs [data-baseweb="tab"] { font-weight:560; }
  div[data-testid="stExpander"] { border:1px solid var(--line); border-radius:9px; }
  code { font-size:.84em; }
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

def _client() -> ApiClient:
    """One client per session, reusing any endpoint already resolved.

    Streamlit re-runs this script on every interaction, so without this the
    fallback probe - which can take a minute against a sleeping service - would
    run again on every click.
    """
    resolved = st.session_state.get("resolved_api_url")
    return ApiClient(resolved or API_URL, api_key=API_KEY)


client = _client()


@st.cache_data(ttl=30, show_spinner=False)
def cached_get(path: str) -> dict:
    """Returns {"ok": bool, "data"|"error": ...} so callers must handle failure."""
    try:
        base = st.session_state.get("resolved_api_url") or API_URL
        return {"ok": True, "data": ApiClient(base, api_key=API_KEY).get(path)}
    except ApiError as exc:
        return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### 📊 AI Data Analyst")
    st.caption("Natural language → governed SQL → chart + explanation")

    api_ok = False
    health: dict = {}
    try:
        health = client.health()
        api_ok = True
        st.session_state["resolved_api_url"] = client.base_url
        st.markdown('<div class="ada-status"><span class="ada-dot"></span>'
                    'Connected</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="ada-facts">'
            f'Warehouse <b>{health["warehouse"]}</b><br>'
            f'Model <b>{health["llm"]}</b><br>'
            f'<b>{health["entities"]}</b> entities · '
            f'<b>{health["metrics"]}</b> certified metrics</div>',
            unsafe_allow_html=True,
        )
        if health.get("auth_enabled"):
            who = cached_get("/whoami")
            if who.get("ok"):
                st.caption(f"Signed in as **{who['data']['name']}** "
                           f"(`{who['data']['role']}`)")
        else:
            st.caption(f"No sign-in required · anonymous "
                       f"`{health.get('anonymous_role', 'analyst')}`")
    except ApiError as exc:
        st.markdown('<div class="ada-status bad"><span class="ada-dot bad"></span>'
                    'Not connected</div>', unsafe_allow_html=True)
        st.error(str(exc))
        if st.button("Retry connection", use_container_width=True):
            st.session_state.pop("resolved_api_url", None)
            st.cache_data.clear()
            st.rerun()

    st.divider()

    # ------------------------------------------------- data: which + upload
    st.markdown("### 📁 Your data")
    if api_ok:
        note = st.session_state.pop("dataset_note", None)
        if note:
            st.success(note)
        ds = cached_get("/datasets")
        if ds.get("ok"):
            data = ds["data"]
            up = data.get("upload", {})
            if up.get("supported"):
                up_file = st.file_uploader(
                    "Upload a CSV to analyse", type=["csv"],
                    help="Becomes a new table with auto-generated metrics. "
                         "Columns that look like personal data are hidden "
                         "from the model.")
                if up_file and up_file.name != st.session_state.get("last_upload"):
                    with st.spinner("Loading CSV and generating certified metrics…"):
                        try:
                            res = client.upload_file(
                                "/datasets/upload", up_file.name, up_file.getvalue())
                        except ApiError as exc:
                            st.error(str(exc))
                        else:
                            msg = (f"Loaded **{res['table']}** — {res['rows']:,} rows, "
                                   f"{res['n_columns']} columns.")
                            if res.get("hidden_columns"):
                                msg += (" Hidden from the model (looked like personal "
                                        "data): " + ", ".join(res["hidden_columns"]))
                            if res.get("joins_added"):
                                msg += " Linked to: " + ", ".join(res["joins_added"])
                            st.session_state["last_upload"] = up_file.name
                            st.session_state["dataset_note"] = msg
                            st.cache_data.clear()
                            st.rerun()
            elif up.get("reason"):
                st.caption(f"Uploads need a DuckDB warehouse: {up['reason']}")

            for d in data.get("datasets", []):
                kind = " · yours" if d["source"] == "upload" else " · built-in"
                exposed = "" if d.get("in_layer") else " — not exposed to the model"
                label = f"**{d['name']}**{kind} — {d['rows']:,} rows{exposed}"
                if d["source"] == "upload":
                    c1, c2 = st.columns([5, 1])
                    c1.markdown(label)
                    if c2.button("Remove", key=f"rm_{d['name']}",
                                 help="Delete this uploaded dataset"):
                        try:
                            client.delete(f"/datasets/{d['name']}")
                        except ApiError as exc:
                            st.error(str(exc))
                        else:
                            st.session_state.pop("dataset_note", None)
                            st.cache_data.clear()
                            st.rerun()
                else:
                    st.markdown(label)
            if data.get("datasets"):
                with st.expander("Columns"):
                    for d in data["datasets"]:
                        cols = ", ".join(
                            f"{c['name']} ({c['type']})" for c in d["columns"][:12])
                        more = (f" +{len(d['columns']) - 12} more"
                                if len(d["columns"]) > 12 else "")
                        st.caption(f"`{d['name']}` — {cols}{more}")
            st.caption(
                "Built-in: synthetic electronics-retail demo "
                "(orders · products · customers). Uploaded data is temporary: "
                "it resets when the service redeploys.")
        else:
            st.caption(f"Could not list the warehouse: {ds.get('error', 'unknown error')}")

    llm_configured = api_ok and health.get("llm") != "offline-rules"
    if llm_configured:
        use_llm = st.toggle("Use LLM", value=True,
                            help="Off = deterministic planner only (no API calls).")
    else:
        use_llm = False
        st.toggle("Use LLM", value=False, disabled=True,
                  help="No OPENAI_API_KEY configured on the API, so the deterministic "
                       "planner handles every question. Set the key and restart the "
                       "API to enable the LLM path.")
        st.caption("Keyless · deterministic planner")

    with st.expander("How answers are kept honest"):
        st.markdown(
            "- The model sees **only** the semantic layer, never the database\n"
            "- Generated SQL is parsed and checked against an allow-list\n"
            "- Execution is **read-only**, with row and time limits\n"
            "- Results are sanity-checked before they are narrated\n"
            "- Out-of-scope questions are **refused**, not guessed"
        )

    sem_resp = cached_get("/semantic-layer") if api_ok else {"ok": False}
    if sem_resp.get("ok") and sem_resp["data"].get("metrics"):
        with st.expander("Certified metrics"):
            for m in sem_resp["data"]["metrics"]:
                st.markdown(f"**{m['label']}** — {m['description']}")
                st.code(m["expression"], language="sql")

    # Operator detail, deliberately last and quiet: useful when something is
    # misconfigured, noise for everyone else.
    if api_ok:
        effective = client.base_url
        with st.expander("Connection"):
            st.caption(f"In use: `{effective}`")
            if effective.rstrip("/") != (API_URL or "").rstrip("/"):
                st.caption(f"⚠️ `API_URL` is `{API_URL}`, which is unreachable. "
                           f"Set it to `{effective}` to remove the fallback.")

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
    if cols[i].button(ex, use_container_width=True, key=f"ex{i}"):
        st.session_state.question = ex

question = st.text_input("Question", key="question", label_visibility="collapsed",
                         placeholder="e.g. what were our highest revenue products last quarter?")
run = st.button("Analyse", type="primary")

if run and question.strip():
    with st.spinner("Running the governed pipeline…"):
        try:
            res = client.ask(question, use_llm)
        except ApiError as exc:
            st.error(str(exc))
            st.stop()

    status = res.get("status")
    conf = res.get("confidence", 0)

    if status == "refused":
        st.markdown(f'<div class="ada-refused"><b>Refused.</b> '
                    f'{res.get("answer", "")}</div>', unsafe_allow_html=True)
        for issue in res.get("issues", []):
            st.caption(f"• {issue}")
    elif status == "error":
        st.error(res.get("answer"))
    else:
        chip = ("chip-high" if conf >= 0.8 else "chip-mid" if conf >= 0.5 else "chip-low")
        st.markdown(
            f'<div class="ada-answer">{res.get("answer") or "(no answer)"}'
            f'<span class="ada-chip {chip}">{conf:.0%} confidence</span></div>',
            unsafe_allow_html=True,
        )

        rows, columns = res.get("rows", []), res.get("columns", [])
        chart = res.get("chart", {})
        if rows:
            df = pd.DataFrame(rows, columns=columns)

            stats = headline_stats(df, chart)
            if stats:
                st.markdown(
                    '<div class="ada-kpis">' + "".join(
                        f'<div class="ada-kpi"><div class="k-label">{s["label"]}</div>'
                        f'<div class="k-value">{s["value"]}</div>'
                        f'<div class="k-delta">{s["delta"]}</div></div>'
                        for s in stats
                    ) + "</div>",
                    unsafe_allow_html=True,
                )

            t_chart, t_data, t_sql, t_trace = st.tabs(
                ["Chart", f"Data ({len(df)})", "SQL", "How it got here"]
            )
            with t_chart:
                fig = build_figure(df, chart)
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True,
                                    config={"displayModeBar": False})
                else:
                    st.dataframe(df, use_container_width=True, hide_index=True)
            with t_data:
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.download_button("Download CSV", df.to_csv(index=False),
                                   file_name="result.csv", mime="text/csv")
            with t_sql:
                st.caption("Validated against the semantic layer before execution, "
                           "then run on a read-only connection.")
                st.code(res.get("sql") or "", language="sql")
            with t_trace:
                for stage in res.get("trace", []):
                    icon = STATUS_ICON.get(stage["status"], "•")
                    label = STAGE_LABELS.get(stage["name"], stage["name"])
                    st.markdown(f"{icon} **{label}** · {stage['duration_ms']} ms")
                    st.json(stage.get("detail", {}), expanded=False)
        elif res.get("sql"):
            with st.expander("Generated SQL"):
                st.code(res["sql"], language="sql")

    if status != "answered" and res.get("trace"):
        with st.expander("How it got here", expanded=True):
            for stage in res.get("trace", []):
                icon = STATUS_ICON.get(stage["status"], "•")
                label = STAGE_LABELS.get(stage["name"], stage["name"])
                st.markdown(f"{icon} **{label}** · {stage['duration_ms']} ms")
                st.json(stage.get("detail", {}), expanded=False)

    for w in res.get("warnings", []):
        st.caption(f"⚠️ {w}")
    st.caption(f"request_id `{res.get('request_id', '-')}`")
else:
    st.caption("Pick an example above or type your own question, then press **Analyse**.")

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
