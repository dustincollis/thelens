"""Streamlit web UI — the user-facing surface for The Lens.

Single page: URL input + Run button + (after run) link to HTML report,
with a list of recent runs below the form.

The UI is a thin wrapper. It calls the same `run_pipeline()` function the
CLI calls; no business logic lives here.

Note on report links: browsers block `file://` links opened from `http://`
pages, so plain Markdown links to `report.html` silently do nothing. We
use buttons that call `webbrowser.open()` server-side instead, which
launches the OS default browser directly (works because Streamlit's
"server" is the user's own machine in a personal-use app).
"""

from __future__ import annotations

import asyncio
import io
import logging
import webbrowser
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from rich.console import Console

# Load .env before importing any module that reads API keys.
# override=True so .env wins over inherited empty *_API_KEY values.
load_dotenv(override=True)

from thelens.pipeline.run import run_pipeline  # noqa: E402
from thelens.storage import list_recent_runs  # noqa: E402

logging.basicConfig(level=logging.INFO)


def _project_root() -> Path:
    return Path.cwd()


def _runs_dir() -> Path:
    return _project_root() / "runs"


def _db_path() -> Path:
    return _project_root() / "data" / "runs.db"


def _normalize_url(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    return s


def _format_when(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    try:
        return datetime.fromisoformat(dt_str).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return dt_str


def _run_audit_blocking(url: str) -> tuple[str, Path, str]:
    """Run the full pipeline. Returns (run_id, run_dir, console_log).

    Streamlit blocks on this call. For a 2-5 minute pipeline that's fine
    on a single-user local app.
    """
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=120)
    run_id, run_dir = asyncio.run(
        run_pipeline(url, _runs_dir(), _db_path(), console=console)
    )
    return run_id, run_dir, buffer.getvalue()


# ============================================================================
# Page setup
# ============================================================================

st.set_page_config(
    page_title="The Lens",
    page_icon="🔎",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.title("The Lens")
st.caption("Local-first website audit with multi-LLM evaluation.")

# ============================================================================
# Run form
# ============================================================================

with st.form("run_form", clear_on_submit=False):
    url_input = st.text_input(
        "URL to audit",
        placeholder="https://example.com",
        help="Full URL including protocol; we'll prepend https:// if you forget.",
    )
    submitted = st.form_submit_button("Run audit", type="primary")

if submitted:
    url = _normalize_url(url_input)
    if not url:
        st.warning("Enter a URL.")
    else:
        info = st.info(
            f"Running audit on {url}. This takes 2–5 minutes — please leave the tab open."
        )
        try:
            with st.spinner("Auditing — fetch, audit, classify, personas, "
                            "page-aware, page-blind, verification, "
                            "persona reviews, synthesis, report…"):
                run_id, run_dir, log = _run_audit_blocking(url)
        except Exception as exc:
            info.empty()
            st.error(f"Run failed: {exc}")
            st.stop()

        info.empty()
        report_path = run_dir / "report.html"
        st.success(f"Done — {run_id}")

        # Auto-open the fresh report in the user's default browser.
        if report_path.exists():
            webbrowser.open(report_path.as_uri())
            st.caption("The report should have opened in a new browser tab.")
            if st.button("Open report again", key=f"reopen_{run_id}"):
                webbrowser.open(report_path.as_uri())
            st.code(str(report_path), language="text")
        with st.expander("Run log"):
            st.code(log or "(no log)", language="text")

# ============================================================================
# Recent runs
# ============================================================================

st.divider()
st.subheader("Recent runs")

try:
    recent = list_recent_runs(_db_path(), limit=20)
except Exception:
    recent = []

if not recent:
    st.caption("No runs yet. Submit a URL above.")
else:
    for r in recent:
        report = _runs_dir() / r.run_id / "report.html"
        cols = st.columns([1, 4, 2, 2])

        # Composite score or status badge
        if r.status == "complete":
            score = r.composite_score if r.composite_score is not None else "—"
            cols[0].markdown(f"**{score}/100**")
        elif r.status == "failed":
            cols[0].markdown(":red[failed]")
        elif r.status == "running":
            cols[0].markdown(":orange[running]")
        else:
            cols[0].markdown(f":gray[{r.status}]")

        cols[1].markdown(f"**{r.url}**")
        cols[1].caption(r.run_id)

        cols[2].caption(_format_when(r.started_at.isoformat() if r.started_at else None))
        if r.actual_cost_usd:
            cols[2].caption(f"${r.actual_cost_usd:.2f}")

        if report.exists():
            if cols[3].button("Open ↗", key=f"open_{r.run_id}"):
                webbrowser.open(report.as_uri())
        else:
            cols[3].caption("no report")
