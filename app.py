"""app.py — Streamlit UI for VAHAN Parquet / CSV / Excel Exporter + Auto Crawler."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure parent directory (for `parivahan_extractor`) and current directory are in sys.path
_current_dir = Path(__file__).resolve().parent
_parent_dir = _current_dir.parent
for _p in (str(_parent_dir), str(_current_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hashlib
import io
import queue
import threading
import zipfile

import streamlit as st
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

try:
    from parivahan_extractor.filter_batch import (
        LABEL_COLUMN,
        apply_filter_row,
        discover_axis_combinations,
        format_filter_row,
        parse_filter_batch_csv,
    )
    from parivahan_extractor.extractor import (
        DASHBOARD_URL,
        PORTAL_URL,
        NoTabularDataError,
        PortalSession,
        browser_profile_dir,
        cleanup_browser_locks,
        close_portal,
        export_parquet,
        export_csv_files,
        export_excel_workbook,
        is_session_alive,
        start_portal,
        visible_charts,
        visible_summary_metrics,
        visible_tables,
    )
except ModuleNotFoundError:
    from filter_batch import (
        LABEL_COLUMN,
        apply_filter_row,
        discover_axis_combinations,
        format_filter_row,
        parse_filter_batch_csv,
    )
    from extractor import (
        DASHBOARD_URL,
        PORTAL_URL,
        NoTabularDataError,
        PortalSession,
        browser_profile_dir,
        cleanup_browser_locks,
        close_portal,
        export_parquet,
        export_csv_files,
        export_excel_workbook,
        is_session_alive,
        start_portal,
        visible_charts,
        visible_summary_metrics,
        visible_tables,
    )

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="VAHAN Parquet Exporter",
    page_icon="🚗",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
# Custom CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.stTabs [data-baseweb="tab-list"] {
    gap: 0.35rem;
    border-bottom: 1px solid rgba(128, 128, 128, 0.28);
}
.stTabs [data-baseweb="tab"] {
    font-weight: 500;
}
.block-container {
    padding-top: 1.25rem;
    padding-bottom: 2rem;
}
[data-testid="stVerticalBlockBorderWrapper"] {
    border-color: rgba(128, 128, 128, 0.28);
    border-radius: 0.55rem;
}
[data-testid="stMetric"] {
    padding: 0.35rem 0.6rem;
    border: 1px solid rgba(128, 128, 128, 0.22);
    border-radius: 0.45rem;
}
div.stButton > button {
    min-height: 2.45rem;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────
st.title("🚗 VAHAN Data Extractor")
st.caption("Collect public VAHAN reports and export them as Parquet, CSV, or Excel.")

with st.expander("About browser access and CAPTCHA"):
    st.info(
        "Dashboard summaries may be available without CAPTCHA. The public report may "
        "require CAPTCHA; the crawler attempts local OCR. Browser data is stored in a "
        "persistent profile under the output folder. Use within permitted access rates."
    )

# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
if "portal_session" not in st.session_state:
    st.session_state.portal_session = None
if "filter_batch_rows" not in st.session_state:
    st.session_state.filter_batch_rows = []
    st.session_state.filter_batch_index = 0
    st.session_state.filter_batch_loaded_index = None
    st.session_state.filter_batch_digest = None
# Auto-crawler state
if "crawl_running" not in st.session_state:
    st.session_state.crawl_running = False
if "crawl_events" not in st.session_state:
    st.session_state.crawl_events = []
if "crawl_summary" not in st.session_state:
    st.session_state.crawl_summary = None
if "crawl_results_table" not in st.session_state:
    st.session_state.crawl_results_table = []
if "crawl_total" not in st.session_state:
    st.session_state.crawl_total = 0
if "crawl_run_combos" not in st.session_state:
    st.session_state.crawl_run_combos = []
if "crawl_retry_failed" not in st.session_state:
    st.session_state.crawl_retry_failed = False
if "crawl_worker" not in st.session_state:
    st.session_state.crawl_worker = None
if "crawl_stop_requested" not in st.session_state:
    st.session_state.crawl_stop_requested = False
if "crawl_error" not in st.session_state:
    st.session_state.crawl_error = None
if "crawl_event_queue" not in st.session_state:
    st.session_state.crawl_event_queue = queue.Queue()

if (
    st.session_state.portal_session is not None
    and not isinstance(st.session_state.portal_session, PortalSession)
):
    st.session_state.portal_session = None
    st.info("The app was updated — previous browser connection reset.")

# Check if active session is still genuinely alive
if st.session_state.portal_session is not None:
    if not is_session_alive(st.session_state.portal_session):
        try:
            close_portal(st.session_state.portal_session)
        except Exception:
            pass
        st.session_state.portal_session = None

session: PortalSession | None = st.session_state.portal_session

# ─────────────────────────────────────────────────────────────────────────────
# Browser control bar (always visible)
# ─────────────────────────────────────────────────────────────────────────────
with st.container():
    launch_col, dashboard_col, close_col, reset_col, status_col = st.columns([1.2, 1.2, 1.1, 1.1, 2.4])

    with launch_col:
        if st.button("📋 Open Report", type="primary", width="stretch", key="top_open_report_btn"):
            if session is not None and is_session_alive(session):
                try:
                    session.run(lambda p: p.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000))
                    st.success("Navigated to Report in active browser.")
                except Exception:
                    close_portal(session)
                    session = start_portal(OUTPUT_DIR)
                    st.session_state.portal_session = session
                    st.success("Browser reopened.")
            else:
                if session is not None:
                    close_portal(session)
                try:
                    session = start_portal(OUTPUT_DIR)
                    st.session_state.portal_session = session
                    st.success("Browser open — ready.")
                except Exception:
                    cleanup_browser_locks(browser_profile_dir(OUTPUT_DIR))
                    session = start_portal(OUTPUT_DIR)
                    st.session_state.portal_session = session
                    st.success("Browser opened successfully after clearing locks.")
            st.rerun()

    with dashboard_col:
        if st.button("📊 Open Dashboard", width="stretch", key="top_open_dashboard_btn"):
            if session is not None and is_session_alive(session):
                try:
                    session.run(lambda p: p.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60_000))
                    st.success("Navigated to Dashboard in active browser.")
                except Exception:
                    close_portal(session)
                    session = start_portal(OUTPUT_DIR, dashboard=True)
                    st.session_state.portal_session = session
                    st.success("Dashboard reopened.")
            else:
                if session is not None:
                    close_portal(session)
                try:
                    session = start_portal(OUTPUT_DIR, dashboard=True)
                    st.session_state.portal_session = session
                    st.success("Dashboard open.")
                except Exception:
                    cleanup_browser_locks(browser_profile_dir(OUTPUT_DIR))
                    session = start_portal(OUTPUT_DIR, dashboard=True)
                    st.session_state.portal_session = session
                    st.success("Dashboard opened successfully after clearing locks.")
            st.rerun()

    with close_col:
        if st.button("❌ Close Browser", disabled=session is None, width="stretch", key="top_close_browser_btn"):
            close_portal(session)
            cleanup_browser_locks(browser_profile_dir(OUTPUT_DIR))
            st.session_state.portal_session = None
            st.info("Browser closed.")
            st.rerun()

    with reset_col:
        if st.button("🔄 Reset Browser", width="stretch", key="top_reset_browser_btn"):
            close_portal(session)
            cleanup_browser_locks(browser_profile_dir(OUTPUT_DIR))
            st.session_state.portal_session = None
            st.success("Browser and lock files cleanly reset.")
            st.rerun()

    with status_col:
        if session is not None and is_session_alive(session):
            try:
                url = session.run(lambda page: page.url)
                display_url = url[:50] + "..." if len(url) > 50 else url
                st.markdown(f"🟢 **Active** · `{display_url}`")
            except Exception:
                st.markdown("⚪ **Browser idle**")
        else:
            st.markdown("⚪ **Browser not open**")
session = st.session_state.portal_session

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
tab_auto, tab_manual, tab_batch = st.tabs([
    "🤖 Auto Crawler",
    "🖱️ Manual Capture",
    "📦 Filter Batch",
])

# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — Auto Crawler
# ═════════════════════════════════════════════════════════════════════════════
with tab_auto:
    st.subheader("Auto Crawl")
    st.caption(
        "Run portal-valid report combinations using the current browser filters."
    )

    # ── Load crawler configuration ─────────────────────────────────────
    try:
        from parivahan_extractor.combo_generator import (  # noqa: PLC0415
            DEFAULT_YAML_TEMPLATE,
            Y_AXIS_OPTIONS,
            combo_count,
            generate_combos,
            load_config,
        )
    except ModuleNotFoundError:
        from combo_generator import (  # noqa: PLC0415
            DEFAULT_YAML_TEMPLATE,
            Y_AXIS_OPTIONS,
            combo_count,
            generate_combos,
            load_config,
        )
    try:
        from parivahan_extractor.auto_crawler import (  # noqa: PLC0415
            AutoCrawler,
            CrawlEvent,
            CrawlSummary,
            clear_failed_combos,
            find_recorded_combos,
            is_combo_recorded,
            load_failed_combos,
            update_failed_combos,
        )
    except ModuleNotFoundError:
        from auto_crawler import (  # noqa: PLC0415
            AutoCrawler,
            CrawlEvent,
            CrawlSummary,
            clear_failed_combos,
            find_recorded_combos,
            is_combo_recorded,
            load_failed_combos,
            update_failed_combos,
        )

    try:
        config_path = PROJECT_DIR / "crawler_config.yaml"
        cfg = load_config(
            config_path if config_path.exists() else DEFAULT_YAML_TEMPLATE
        )
        if cfg.get("discover_x_axes"):
            raw_y_axes = cfg["filters"].get("yAxis", [])
            y_axes = raw_y_axes if isinstance(raw_y_axes, list) else [raw_y_axes]
            n_combos = len(y_axes)
            combos_preview = []
            combo_count_label = f"{n_combos} Y-axes · X-axes discovered at run time"
        else:
            combos_preview = generate_combos(cfg)
            n_combos = combo_count(cfg)
            combo_count_label = f"{n_combos} report combinations"
        config_valid = True
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load crawler configuration: {exc}")
        config_valid = False
        cfg = {}
        combos_preview = []
        n_combos = 0
        combo_count_label = "Configuration unavailable"

    filter_config = cfg.get("filters", {})
    configured_from = filter_config.get("fromYear")
    configured_to = filter_config.get("toYear")
    if configured_from is not None and configured_to is not None:
        date_range_label = f"{configured_from} – {configured_to}"
    elif configured_from is not None or configured_to is not None:
        date_range_label = str(configured_from or configured_to)
    else:
        date_range_label = "Portal selection"

    # Main run settings are read-only summaries of the existing YAML config.
    with st.container(border=True):
        date_col, format_col, combo_col = st.columns([1.2, 1, 1.8])
        with date_col:
            st.caption("Configured date range")
            st.markdown(f"**{date_range_label}**")
        with format_col:
            st.caption("Output format")
            st.markdown("**Parquet**")
        with combo_col:
            st.caption("Run scope")
            st.markdown(f"**{combo_count_label}**")

    # ── Interactive Axis Assistant ─────────────────────────────────────
    with st.expander("Advanced settings · axes, EV filters & combinations", expanded=False):
        ax_col1, ax_col2 = st.columns(2)
        with ax_col1:
            y_keys = list(Y_AXIS_OPTIONS.keys())
            selected_y = st.selectbox(
                "Select Y-Axis",
                options=y_keys,
                format_func=lambda k: f"{Y_AXIS_OPTIONS.get(k, k)} ({k})",
                key="axis_helper_y",
            )
        with ax_col2:
            scanned_y = st.session_state.get("axis_helper_scanned_y")
            discovered_x_axes = (
                st.session_state.get("axis_helper_x_options", [])
                if scanned_y == selected_y
                else []
            )
            selected_x_list = st.multiselect(
                f"Portal X-Axes for '{Y_AXIS_OPTIONS.get(selected_y, selected_y)}'",
                options=discovered_x_axes,
                key="axis_helper_x",
                disabled=not discovered_x_axes,
            )
            if scanned_y != selected_y:
                selected_x_list = []

        btn_c1, btn_c2 = st.columns([1, 1])
        with btn_c1:
            if st.button("🔎 Scan portal X-Axes", key="btn_scan_axes"):
                try:
                    if session is None or not is_session_alive(session):
                        session = start_portal(OUTPUT_DIR)
                        st.session_state.portal_session = session
                    with st.spinner(f"Scanning X-Axes for {selected_y}..."):
                        session.run(
                            lambda page: (
                                page.goto(
                                    PORTAL_URL,
                                    wait_until="domcontentloaded",
                                    timeout=60_000,
                                ),
                                page.locator("select#yAxis").wait_for(
                                    state="attached",
                                    timeout=30_000,
                                ),
                            )
                        )
                        discovered_pairs = session.run(
                            discover_axis_combinations,
                            [selected_y],
                        )
                    options = [
                        x_axis
                        for y_axis, x_axis in discovered_pairs
                        if y_axis == selected_y
                    ]
                    st.session_state.axis_helper_scanned_y = selected_y
                    st.session_state.axis_helper_x_options = options
                    st.session_state.axis_helper_x = []
                    if options:
                        st.success(f"Found {len(options)} portal X-Axes.")
                    else:
                        st.warning(f"The portal returned no X-Axes for {selected_y}.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not scan portal X-Axes: {exc}")

            if st.button(
                "Save selected axes",
                key="btn_insert_axes",
                disabled=not selected_x_list,
            ):
                config_path = PROJECT_DIR / "crawler_config.yaml"
                current_text = (
                    config_path.read_text(encoding="utf-8")
                    if config_path.exists()
                    else DEFAULT_YAML_TEMPLATE
                )
                import yaml
                try:
                    data = yaml.safe_load(current_text) or {}
                    if "filters" not in data:
                        data["filters"] = {}
                    data.pop("axis_combinations", None)
                    data["filters"]["yAxis"] = [selected_y]
                    data["filters"]["xAxis"] = selected_x_list
                    data["discover_x_axes"] = True
                    data["export_formats"] = ["parquet"]
                    new_yaml = yaml.dump(data, sort_keys=False)
                    config_path.write_text(new_yaml, encoding="utf-8")
                    st.success("Axis settings saved.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not update YAML: {e}")

        st.divider()
        st.markdown("**EV filters and report combinations**")
        ev_filters = {
            key: value
            for key, value in filter_config.items()
            if key not in {"fromYear", "toYear", "reportYear", "yAxis", "xAxis"}
        }
        if ev_filters:
            st.dataframe(
                [
                    {
                        "Filter": key,
                        "Configured value": (
                            ", ".join(map(str, value))
                            if isinstance(value, list)
                            else str(value)
                        ),
                    }
                    for key, value in ev_filters.items()
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("No additional EV filters are configured.")

        configured_y_axes = filter_config.get("yAxis", [])
        configured_y_axes = (
            configured_y_axes
            if isinstance(configured_y_axes, list)
            else [configured_y_axes]
        )
        if cfg.get("discover_x_axes"):
            st.caption(
                "X-axis choices are discovered from the portal when a crawl starts."
            )
            combo_rows = [
                {"Y-axis": str(y_axis), "X-axis": "Discovered at run time"}
                for y_axis in configured_y_axes
            ]
        else:
            combo_rows = [
                {"Y-axis": combo.get("yAxis", ""), "X-axis": combo.get("xAxis", "")}
                for combo in combos_preview
            ]
        with st.expander("Configured report combinations", expanded=False):
            if combo_rows:
                st.dataframe(combo_rows, hide_index=True, width="stretch")
            else:
                st.caption(
                    "No fixed combinations are listed; the portal supplies valid "
                    "X-axis choices when the crawl starts."
                )

    st.divider()

    # ── Crawl controls ─────────────────────────────────────────────────
    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([2.4, 1.4, 1.4])

    crawl_is_running = st.session_state.crawl_running
    try:
        failed_combos = load_failed_combos(OUTPUT_DIR)
        failed_combos_error = None
    except (OSError, ValueError) as exc:
        failed_combos = []
        failed_combos_error = str(exc)

    with ctrl_col1:
        start_disabled = (
            not config_valid
            or n_combos == 0
            or crawl_is_running
        )
        start_requested = st.button(
            "🚀  Start Auto Crawl",
            type="primary",
            disabled=start_disabled,
            width="stretch",
            key="start_auto_crawl_btn",
        )
        retry_failed_requested = st.button(
            f"🔁  Rerun Failed Only ({len(failed_combos)})",
            disabled=not config_valid or crawl_is_running or not failed_combos,
            width="stretch",
            key="retry_failed_combos_btn",
        )
        if failed_combos_error:
            st.error(f"Failed-combination history error: {failed_combos_error}")
        elif failed_combos:
            st.caption(
                "Unresolved failures stay here after a failed-only retry. "
                "A full crawl refreshes this list to its current combination scope."
            )
            with st.expander(f"Failed combinations ({len(failed_combos)})"):
                st.dataframe(
                    [
                        {
                            "#": index,
                            "Combination": combo.get(
                                LABEL_COLUMN,
                                ", ".join(
                                    f"{key}={value}"
                                    for key, value in combo.items()
                                    if key != LABEL_COLUMN
                                ),
                            ),
                        }
                        for index, combo in enumerate(failed_combos, start=1)
                    ],
                    hide_index=True,
                    width="stretch",
                )
                if st.button(
                    "Clear failed history",
                    key="clear_failed_history_btn",
                    help="Removes retry-queue entries only; existing exported files are not changed.",
                ):
                    try:
                        clear_failed_combos(OUTPUT_DIR)
                    except OSError as exc:
                        st.error(str(exc))
                    else:
                        st.success("Failed-combination retry list cleared.")
                        st.rerun()

        if start_requested or retry_failed_requested:
            if session is None or not is_session_alive(session):
                with st.spinner("Starting browser for automated crawl..."):
                    if session is not None:
                        close_portal(session)
                    try:
                        session = start_portal(OUTPUT_DIR)
                    except Exception:
                        cleanup_browser_locks(browser_profile_dir(OUTPUT_DIR))
                        session = start_portal(OUTPUT_DIR)
                    st.session_state.portal_session = session

            run_combos = failed_combos if retry_failed_requested else combos_preview
            if cfg.get("discover_x_axes") and not retry_failed_requested:
                raw_y_axes = cfg["filters"].get("yAxis", [])
                y_axes = (
                    [str(value).strip() for value in raw_y_axes]
                    if isinstance(raw_y_axes, list)
                    else [str(raw_y_axes).strip()]
                )
                y_axes = [value for value in y_axes if value]
                try:
                    with st.spinner("Reading valid X-axis choices from the portal..."):
                        session.run(
                            lambda page: (
                                page.goto(
                                    PORTAL_URL,
                                    wait_until="domcontentloaded",
                                    timeout=60_000,
                                ),
                                page.locator("select#yAxis").wait_for(
                                    state="attached",
                                    timeout=30_000,
                                ),
                            )
                        )
                        axis_combinations = session.run(
                            discover_axis_combinations,
                            y_axes,
                        )
                    raw_x_axes = cfg["filters"].get("xAxis")
                    x_axes_are_auto = (
                        isinstance(raw_x_axes, str)
                        and raw_x_axes.strip().lower() in {"auto", "all_valid", "*"}
                    ) or (
                        isinstance(raw_x_axes, list)
                        and len(raw_x_axes) == 1
                        and str(raw_x_axes[0]).strip().lower()
                        in {"auto", "all_valid", "*"}
                    )
                    if raw_x_axes is not None and not x_axes_are_auto:
                        requested_x_axes = (
                            raw_x_axes if isinstance(raw_x_axes, list) else [raw_x_axes]
                        )
                        requested_x_axes = {
                            str(value).strip() for value in requested_x_axes
                        }
                        axis_combinations = [
                            pair
                            for pair in axis_combinations
                            if pair[1] in requested_x_axes
                        ]
                    run_combos = generate_combos(
                        {"filters": {}},
                        discovered_axis_combinations=axis_combinations,
                    )
                    if not run_combos:
                        raise ValueError(
                            "The portal did not provide any combinations to run."
                        )
                    st.info(
                        f"Portal scan found {len(axis_combinations)} valid Y/X pairs "
                        f"for {len(y_axes)} configured Y-axes."
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not discover portal axis combinations: {exc}")
                    st.stop()

            st.session_state.crawl_running = True
            st.session_state.crawl_events = []
            st.session_state.crawl_summary = None
            st.session_state.crawl_error = None
            st.session_state.crawl_stop_requested = False
            st.session_state.crawl_total = len(run_combos)
            st.session_state.crawl_run_combos = run_combos
            st.session_state.crawl_retry_failed = retry_failed_requested
            recorded_signatures, recorded_labels = find_recorded_combos(OUTPUT_DIR)
            st.session_state.crawl_results_table = [
                {
                    "#": index,
                    "Filters": combo.get(LABEL_COLUMN, f"combo_{index}"),
                    "Status": (
                        "⏭ Already Recorded"
                        if is_combo_recorded(combo, recorded_signatures, recorded_labels)
                        else "⏳ Pending"
                    ),
                    "Message": (
                        "A successful export already exists."
                        if is_combo_recorded(combo, recorded_signatures, recorded_labels)
                        else "Waiting to start."
                    ),
                }
                for index, combo in enumerate(run_combos, start=1)
            ]
            # Fresh queue
            st.session_state.crawl_event_queue = queue.Queue()
            crawler = AutoCrawler(session, cfg, OUTPUT_DIR)
            st.session_state.crawl_worker = crawler

            def _run_crawl(
                q: queue.Queue,
                active_crawler: AutoCrawler,
                combos: list[dict[str, str]],
                saved_signatures: set[str],
                saved_labels: set[str],
            ) -> None:
                try:
                    summary = active_crawler.run(
                        combos,
                        progress_callback=q.put,
                        skip_recorded=lambda combo: is_combo_recorded(
                            combo,
                            saved_signatures,
                            saved_labels,
                        ),
                    )
                    q.put(summary)
                except Exception as exc:  # noqa: BLE001
                    q.put(exc)

            thread = threading.Thread(
                target=_run_crawl,
                args=(
                    st.session_state.crawl_event_queue,
                    crawler,
                    run_combos,
                    recorded_signatures,
                    recorded_labels,
                ),
                daemon=True,
            )
            thread.start()
            st.rerun()

    with ctrl_col2:
        if st.button(
            "⏹ Stop Crawl",
            key="stop_crawl_btn",
            disabled=not crawl_is_running or st.session_state.crawl_stop_requested,
            width="stretch",
        ):
            worker = st.session_state.crawl_worker
            if worker is not None:
                worker.stop()
                st.session_state.crawl_stop_requested = True
                st.warning("Stop requested — the current combination will finish first.")

    with ctrl_col3:
        if st.button("🗑 Clear Results", key="clear_results_btn", disabled=crawl_is_running, width="stretch"):
            st.session_state.crawl_events = []
            st.session_state.crawl_summary = None
            st.session_state.crawl_results_table = []
            st.session_state.crawl_total = 0
            st.session_state.crawl_error = None
            st.rerun()

    @st.fragment(run_every="1s" if st.session_state.crawl_running else None)
    def _render_crawl_progress() -> None:
        events = st.session_state.crawl_events
        results = st.session_state.crawl_results_table
        should_refresh_app = False

        while True:
            try:
                item = st.session_state.crawl_event_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(item, CrawlEvent):
                events.append(item)
                row = {
                    "#": item.combo_index,
                    "Filters": item.label,
                    "Status": item.status.value,
                    "Message": item.message,
                }
                row_index = item.combo_index - 1
                if 0 <= row_index < len(results):
                    results[row_index].update(row)
                else:
                    results.append(row)
            elif isinstance(item, CrawlSummary):
                st.session_state.crawl_summary = item
                try:
                    failed_combos = update_failed_combos(
                        OUTPUT_DIR,
                        st.session_state.crawl_run_combos,
                        item,
                        retain_unattempted=st.session_state.crawl_retry_failed,
                    )
                    st.session_state.failed_combos = failed_combos
                except (OSError, ValueError) as exc:
                    st.session_state.crawl_error = (
                        f"Crawl finished, but failed combinations could not be saved: {exc}"
                    )
                st.session_state.crawl_running = False
                st.session_state.crawl_worker = None
                st.session_state.crawl_stop_requested = False
                should_refresh_app = True
            elif isinstance(item, Exception):
                st.session_state.crawl_error = str(item)
                st.session_state.crawl_running = False
                st.session_state.crawl_worker = None
                st.session_state.crawl_stop_requested = False
                should_refresh_app = True

        if len(events) > 100:
            del events[:-100]

        if st.session_state.crawl_running or results:
            total_combos = st.session_state.crawl_total or len(results) or n_combos
            statuses = [row.get("Status") for row in results]
            succeeded = statuses.count("✅ Success")

            if total_combos:
                st.progress(
                    min(succeeded / total_combos, 1.0),
                    text=f"Successful {succeeded} of {total_combos}",
                )

            running_rows = [row for row in results if row.get("Status") == "🔄 Running"]
            if running_rows:
                current = running_rows[-1]
                st.info(
                    f"**Running [{current['#']}/{total_combos}]:** "
                    f"{current['Filters']}  \n{current['Message']}"
                )
            elif st.session_state.crawl_stop_requested:
                st.warning("Stop requested — waiting for the current combination to finish.")
            elif st.session_state.crawl_running:
                st.info("Waiting for the next combination to start.")

            if st.session_state.crawl_error:
                st.error(f"Crawl error: {st.session_state.crawl_error}")

        recent_events = events[-8:]
        if recent_events:
            st.caption("Latest crawl events")
            st.dataframe(
                [
                    {
                        "Time": event.timestamp[11:19],
                        "Status": event.status.value,
                        "Combination": event.label,
                        "Activity": event.message,
                    }
                    for event in reversed(recent_events)
                ],
                hide_index=True,
                width="stretch",
            )

        if should_refresh_app:
            st.rerun()

    st.subheader("Recent activity")
    with st.container(border=True):
        _render_crawl_progress()
        st.caption(f"Output folder: `{OUTPUT_DIR}`")
        st.caption(f"Browser profile: `{browser_profile_dir(OUTPUT_DIR)}`")

    # ── Summary ────────────────────────────────────────────────────────
    summary = st.session_state.crawl_summary
    if summary is not None:
        st.success(f"✅ Crawl complete — {summary.succeeded}/{summary.total} combos succeeded.")

        # Download ZIP of all output files
        if st.button("📥 Download all results as ZIP", key="download_zip_btn"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for result in summary.results:
                    if result.export_result:
                        run_dir = result.export_result.manifest_path.parent
                        for f in run_dir.iterdir():
                            zf.write(f, arcname=f"{run_dir.name}/{f.name}")
            zip_buf.seek(0)
            st.download_button(
                "⬇️ Download ZIP",
                data=zip_buf,
                file_name="vahan_crawl_results.zip",
                mime="application/zip",
            )


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — Manual Capture
# ═════════════════════════════════════════════════════════════════════════════
with tab_manual:
    st.subheader("🖱️ Manual Capture")
    st.caption(
        "Set your filters manually in the browser window, solve CAPTCHA yourself, "
        "apply the report, then capture the results here."
    )

    if session is None or not is_session_alive(session):
        st.info("Browser is not currently open.")
        if st.button("📋 Open Report Browser Now", key="open_manual_btn", type="primary"):
            try:
                session = start_portal(OUTPUT_DIR)
            except Exception:
                cleanup_browser_locks(browser_profile_dir(OUTPUT_DIR))
                session = start_portal(OUTPUT_DIR)
            st.session_state.portal_session = session
            st.rerun()
    else:
        source_url = session.run(lambda page: page.url)
        is_dashboard_page = "/analytics/publicdashboard/vahan" in source_url

        if is_dashboard_page:
            st.info(
                "Dashboard mode: set filters and click Apply Filters in the browser. "
                "No CAPTCHA typically required for the dashboard."
            )
        else:
            st.info(
                "Report mode: select filters in the browser, solve CAPTCHA manually, "
                "apply the report. Then capture below. "
                "If the portal offers CSV/Excel export, use it first."
            )
        st.caption(f"Browser profile: `{session.profile_dir}`")

        filter_description = st.text_input(
            "Filter description (saved in the manifest)",
            placeholder="Example: Calendar year 2025, all states, Y-axis registrations",
            key="manual_filter_description",
        )

        export_col1, export_col2, export_col3 = st.columns(3)
        with export_col1:
            do_parquet = st.checkbox("Export Parquet", value=True, key="manual_do_parquet")
        with export_col2:
            do_csv = st.checkbox("Export CSV", value=True, key="manual_do_csv")
        with export_col3:
            do_excel = st.checkbox("Export Excel", value=False, key="manual_do_excel")

        if st.button("📸 Capture visible data", type="primary", key="manual_capture_btn"):
            try:
                tables, charts, summary_metrics, src_url = session.run(
                    lambda page: (
                        visible_tables(page),
                        visible_charts(page),
                        visible_summary_metrics(page),
                        page.url,
                    )
                )
                result = export_parquet(
                    tables=tables,
                    downloads=session.downloads,
                    output_dir=OUTPUT_DIR,
                    filter_description=filter_description,
                    source_url=src_url,
                    charts=charts,
                    summary_metrics=summary_metrics,
                )
                run_dir = result.manifest_path.parent

                if do_csv:
                    csv_paths = export_csv_files(tables, charts, summary_metrics, session.downloads, run_dir)
                    st.info(f"CSV: wrote {len(csv_paths)} file(s).")

                if do_excel:
                    wb = export_excel_workbook(tables, charts, summary_metrics, session.downloads, run_dir,
                                               workbook_stem=filter_description or "report")
                    if wb:
                        st.info(f"Excel: wrote `{wb.name}`.")

            except NoTabularDataError as exc:
                st.error(str(exc))
            except (OSError, PlaywrightError, ValueError, ImportError) as exc:
                st.error(f"Export failed: {exc}")
            else:
                session.downloads.clear()
                st.success(f"Exported {len(result.parquet_files)} Parquet file(s).")
                st.dataframe(
                    [{"File": p.name, "Rows": result.row_counts[p.name]} for p in result.parquet_files],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(f"Manifest: `{result.manifest_path}`")

        if session.download_errors:
            st.error("One or more portal downloads could not be saved:")
            for err in session.download_errors:
                st.write(err)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — Filter Batch (original functionality preserved)
# ═════════════════════════════════════════════════════════════════════════════
with tab_batch:
    st.subheader("📦 Filter Batch Assistant")
    st.caption(
        "Upload a CSV whose headers are portal filter IDs. For multi-select values, "
        "separate exact option values with `|`. Blank or `*` clears select filters; "
        "blank text filters are left unchanged."
    )

    if session is None or not is_session_alive(session):
        st.info("Browser is not currently open.")
        if st.button("📋 Open Report Browser Now", key="open_batch_tab_btn", type="primary"):
            try:
                session = start_portal(OUTPUT_DIR)
            except Exception:
                cleanup_browser_locks(browser_profile_dir(OUTPUT_DIR))
                session = start_portal(OUTPUT_DIR)
            st.session_state.portal_session = session
            st.rerun()
    else:
        source_url = session.run(lambda page: page.url)
        is_dashboard_page = "/analytics/publicdashboard/vahan" in source_url

        st.download_button(
            "⬇️ Download filter batch CSV template",
            data=(
                "label,reportYear,fromYear,toYear,stateName,vehicleCategoryGroup,"
                "vehicleFuel,yAxis\n"
                "Example 1,2025,2025,2025,DL|GA,Two Wheeler,DIESEL,stateCode\n"
                "Example 2,2024,2024,2024,MH,Two Wheeler,PETROL,stateCode\n"
            ),
            file_name="filter_batch_template.csv",
            mime="text/csv",
        )
        batch_file = st.file_uploader(
            "Filter combinations CSV",
            type=["csv"],
            key="filter_batch_csv",
            disabled=is_dashboard_page,
        )
        if batch_file is not None and not is_dashboard_page:
            batch_bytes = batch_file.getvalue()
            batch_digest = hashlib.sha256(batch_bytes).hexdigest()
            if batch_digest != st.session_state.filter_batch_digest:
                st.session_state.filter_batch_rows = []
                st.session_state.filter_batch_index = 0
                st.session_state.filter_batch_loaded_index = None
                st.session_state.filter_batch_digest = batch_digest
                try:
                    parsed_rows = parse_filter_batch_csv(batch_bytes.decode("utf-8-sig"))
                except (UnicodeDecodeError, ValueError) as error:
                    st.error(f"Invalid filter batch CSV: {error}")
                else:
                    st.session_state.filter_batch_rows = parsed_rows
                    st.session_state.filter_batch_index = 0
                    st.session_state.filter_batch_loaded_index = None
                    st.success(f"Loaded {len(parsed_rows)} filter row(s).")

        batch_rows = [] if is_dashboard_page else st.session_state.filter_batch_rows
        batch_index = 0 if is_dashboard_page else st.session_state.filter_batch_index

        if batch_rows and batch_index < len(batch_rows):
            current_row = batch_rows[batch_index]
            row_label = current_row.get(LABEL_COLUMN, "").strip() or format_filter_row(current_row)
            st.write(f"Next row: **{batch_index + 1} of {len(batch_rows)}** — {row_label}")

            if st.button(
                "▶️ Load this row's filters",
                key="load_filter_batch_row",
                disabled=is_dashboard_page,
            ):
                try:
                    session.run(apply_filter_row, current_row)
                except (PlaywrightError, ValueError) as error:
                    st.error(f"Could not set this row's filters: {error}")
                else:
                    st.session_state.filter_batch_loaded_index = batch_index
                    st.success(
                        "Filters are set. Solve CAPTCHA and click Apply manually; "
                        "then capture below."
                    )
            if st.session_state.filter_batch_loaded_index == batch_index:
                st.info("Current row is loaded. CAPTCHA and Apply remain manual.")
        elif batch_rows:
            st.success(f"Filter batch complete: {len(batch_rows)} row(s) captured.")

        filter_description = st.text_input(
            "Filter description",
            placeholder="Example: Calendar year 2025, selected states",
            key="batch_filter_description",
        )
        current_batch_row = (
            batch_rows[batch_index]
            if batch_rows and batch_index < len(batch_rows)
            else None
        )
        is_batch_loaded = (
            current_batch_row is not None
            and st.session_state.filter_batch_loaded_index == batch_index
        )
        capture_label = (
            "📸 Capture and load next row"
            if is_batch_loaded and batch_index + 1 < len(batch_rows)
            else "📸 Capture and finish batch row"
            if is_batch_loaded
            else "📸 Capture visible data"
        )
        if st.button(
            capture_label,
            type="primary",
            disabled=bool(batch_rows and batch_index < len(batch_rows) and not is_batch_loaded),
            key="batch_capture_btn",
        ):
            description = filter_description
            if current_batch_row is not None:
                row_description = (
                    current_batch_row.get(LABEL_COLUMN, "").strip()
                    or format_filter_row(current_batch_row)
                )
                description = f"{description} | {row_description}".strip(" |")
            try:
                tables, charts, summary_metrics, src_url = session.run(
                    lambda page: (
                        visible_tables(page),
                        visible_charts(page),
                        visible_summary_metrics(page),
                        page.url,
                    )
                )
                result = export_parquet(
                    tables=tables,
                    downloads=session.downloads,
                    output_dir=OUTPUT_DIR,
                    filter_description=description,
                    source_url=src_url,
                    charts=charts,
                    summary_metrics=summary_metrics,
                )
            except NoTabularDataError as error:
                st.error(str(error))
            except (OSError, PlaywrightError, ValueError, ImportError) as error:
                st.error(f"Export failed: {error}")
            else:
                session.downloads.clear()
                st.success(f"Exported {len(result.parquet_files)} Parquet file(s).")
                st.dataframe(
                    [{"File": p.name, "Rows": result.row_counts[p.name]} for p in result.parquet_files],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(f"Manifest: `{result.manifest_path}`")
                if is_batch_loaded:
                    st.session_state.filter_batch_index += 1
                    st.session_state.filter_batch_loaded_index = None
                    next_index = st.session_state.filter_batch_index
                    if next_index < len(batch_rows):
                        try:
                            session.run(apply_filter_row, batch_rows[next_index])
                        except (PlaywrightError, ValueError) as error:
                            st.error(f"Report saved, but could not load next batch row: {error}")
                        else:
                            st.session_state.filter_batch_loaded_index = next_index
                            st.info(
                                f"Next filters loaded ({next_index + 1} of {len(batch_rows)}). "
                                "Solve CAPTCHA and click Apply manually."
                            )
                    else:
                        st.success("All batch rows have been captured.")

        if session.download_errors:
            st.error("One or more portal downloads could not be saved:")
            for download_error in session.download_errors:
                st.write(download_error)
