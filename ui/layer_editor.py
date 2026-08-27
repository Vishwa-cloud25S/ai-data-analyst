"""Semantic-layer editor panel for the Streamlit UI.

Two modes, because two different people need this:

  * **Metrics** - a form. An analyst changes what "revenue" means without
    touching YAML, which is the point of the whole feature.
  * **Advanced** - the raw file, for someone comfortable with it.

Both go through the same server-side validation, so the UI cannot save
something the API would reject.
"""
from __future__ import annotations

import streamlit as st
import yaml

FORMATS = ["currency", "number", "percent"]


def _validate(client, text: str):
    return client.post_json("/semantic-layer/validate", {"yaml": text})


def render(client, api_error) -> None:
    """Render the editor. `client` is an ApiClient; admin role required."""
    try:
        raw = client.get("/semantic-layer/raw")
    except api_error as exc:
        if "403" in str(exc) or "Not permitted" in str(exc):
            return  # not an admin: the editor simply does not appear
        st.error(str(exc))
        return

    st.divider()
    with st.expander("✏️ Edit the semantic layer — what the model is allowed to see"):
        st.caption(
            "Whoever edits this decides which tables and columns exist as far as "
            "the assistant is concerned. Every change is validated against the "
            "warehouse, backed up, and written to the audit log."
        )
        try:
            doc = yaml.safe_load(raw["yaml"]) or {}
        except yaml.YAMLError as exc:
            st.error(f"The current layer is not valid YAML: {exc}")
            return

        tab_metrics, tab_raw, tab_history = st.tabs(["Metrics", "Advanced (YAML)", "History"])

        # ---------------------------------------------------------- metrics
        with tab_metrics:
            metrics = doc.get("metrics") or []
            names = [m.get("name", "?") for m in metrics]
            choice = st.selectbox("Metric", ["➕ New metric", *names])

            if choice == "➕ New metric":
                current: dict = {"name": "", "label": "", "description": "",
                                 "entity": "", "expression": "", "filters": [],
                                 "format": "number"}
            else:
                current = dict(next(m for m in metrics if m.get("name") == choice))

            entities = [e.get("name") for e in (doc.get("entities") or [])]
            c1, c2 = st.columns(2)
            name = c1.text_input("Name (used in SQL aliases)", current.get("name", ""))
            label = c2.text_input("Label (shown to users)", current.get("label", ""))
            description = st.text_area(
                "Description", current.get("description", ""), height=70,
                help="This is what question retrieval matches against. Vague "
                     "descriptions produce vague answers.",
            )
            c3, c4 = st.columns(2)
            entity = c3.selectbox(
                "Entity", entities,
                index=entities.index(current["entity"]) if current.get("entity") in entities else 0,
            ) if entities else c3.text_input("Entity", current.get("entity", ""))
            fmt = c4.selectbox(
                "Format", FORMATS,
                index=FORMATS.index(current.get("format", "number"))
                if current.get("format") in FORMATS else 1,
            )
            expression = st.text_input(
                "Expression", current.get("expression", ""),
                help="e.g. SUM(fct_orders.net_revenue)",
            )
            filters_text = st.text_area(
                "Mandatory filters (one per line)",
                "\n".join(current.get("filters") or []), height=70,
                help="Applied to every question using this metric. This is how "
                     "'revenue' stays consistent with the dashboard.",
            )

            proposed = dict(doc)
            new_metric = {
                "name": name, "label": label or name, "description": description,
                "entity": entity, "expression": expression,
                "filters": [f.strip() for f in filters_text.splitlines() if f.strip()],
                "format": fmt,
            }
            others = [m for m in metrics if m.get("name") != choice]
            proposed["metrics"] = [*others, new_metric] if name else others

            b1, b2, b3 = st.columns([1, 1, 3])
            if b1.button("Validate", key="val_metric"):
                _show_report(_validate(client, yaml.safe_dump(proposed, sort_keys=False)))
            if b2.button("Save", key="save_metric", type="primary", disabled=not name):
                _save(client, api_error, yaml.safe_dump(proposed, sort_keys=False),
                      f"metric '{name}' updated via UI")
            if choice != "➕ New metric" and b3.button(f"Delete '{choice}'"):
                deleted = dict(doc)
                deleted["metrics"] = others
                _save(client, api_error, yaml.safe_dump(deleted, sort_keys=False),
                      f"metric '{choice}' deleted via UI")

        # ---------------------------------------------------------- raw yaml
        with tab_raw:
            edited = st.text_area("semantic_layer.yml", raw["yaml"], height=460,
                                  label_visibility="collapsed")
            message = st.text_input("Change note (recorded in the audit log)")
            c1, c2 = st.columns([1, 4])
            if c1.button("Validate", key="val_raw"):
                _show_report(_validate(client, edited))
            if c2.button("Save", key="save_raw", type="primary"):
                _save(client, api_error, edited, message)

        # ---------------------------------------------------------- history
        with tab_history:
            versions = client.get("/semantic-layer/versions")["versions"]
            if not versions:
                st.caption("No previous versions yet — a backup is written on each save.")
            for v in versions[:15]:
                cols = st.columns([3, 2, 1])
                cols[0].code(v["id"], language=None)
                cols[1].caption(f"{v['author']} · {v['size_bytes']} bytes")
                if cols[2].button("Restore", key=f"r_{v['id']}"):
                    try:
                        client.post_json(f"/semantic-layer/restore/{v['id']}", {})
                        st.success(f"Restored {v['id']}")
                        st.rerun()
                    except api_error as exc:
                        st.error(str(exc))


def _show_report(report: dict) -> None:
    if report.get("ok"):
        st.success("Valid — every entity and metric executes against the warehouse.")
    else:
        st.error("Rejected. Nothing was saved.")
        for e in report.get("errors", []):
            st.write(f"• {e}")
    for w in report.get("warnings", []):
        st.warning(w)
    diff = report.get("diff") or {}
    changes = {k: v for k, v in diff.items() if v}
    if changes:
        st.caption("Changes: " + "; ".join(
            f"{k.replace('_', ' ')}: {', '.join(v)}" for k, v in changes.items()
        ))
        if diff.get("entities_added") or diff.get("columns_added"):
            st.warning("This change **exposes new data** to the assistant.")


def _save(client, api_error, text: str, message: str) -> None:
    try:
        result = client.put_json("/semantic-layer/raw", {"yaml": text, "message": message})
    except api_error as exc:
        st.error(str(exc))
        return
    st.success("Saved. The change is live immediately — no restart needed.")
    _show_report(result)
    st.rerun()
