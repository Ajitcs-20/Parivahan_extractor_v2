"""auto_crawler.py — Fully automated Parivahan filter × CAPTCHA → export loop.

Flow per filter combination
---------------------------
1. Call ``apply_filter_row`` to set all portal dropdowns / text inputs.
2. Call ``fill_captcha`` to auto-solve (EasyOCR) the CAPTCHA with retries.
3. Click the portal Apply / Generate-Report button.
4. Wait for results to appear (table row count increase or network idle).
5. Capture visible tables, charts, summary metrics and downloaded files.
6. Export captured data to Parquet.
7. On any error: exponential back-off then retry up to ``max_retries`` times.
   After all retries, mark the combo as FAILED and move on.
8. Emit ``CrawlEvent`` objects so Streamlit (or a CLI) can display live progress.

Usage
-----
    from parivahan_extractor.auto_crawler import AutoCrawler, CrawlSummary
    from parivahan_extractor.combo_generator import load_config, generate_combos
    from parivahan_extractor.extractor import start_portal, close_portal

    config = load_config("crawler_config.yaml")
    combos = generate_combos(config)
    session = start_portal(output_dir)
    try:
        crawler = AutoCrawler(session, config, output_dir)
        summary = crawler.run(combos, progress_callback=print)
    finally:
        close_portal(session)
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

# Ensure parent directory and current directory are in sys.path
_current_dir = Path(__file__).resolve().parent
_parent_dir = _current_dir.parent
for _p in (str(_parent_dir), str(_current_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd
from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

try:
    from parivahan_extractor.captcha_solver import fill_captcha, is_captcha_present
    from parivahan_extractor.extractor import (
        ExportResult,
        NoTabularDataError,
        PORTAL_URL,
        export_parquet,
        visible_charts,
        visible_summary_metrics,
        visible_tables,
    )
    from parivahan_extractor.filter_batch import LABEL_COLUMN, apply_filter_row
except ModuleNotFoundError:
    from captcha_solver import fill_captcha, is_captcha_present
    from extractor import (
        ExportResult,
        NoTabularDataError,
        PORTAL_URL,
        export_parquet,
        visible_charts,
        visible_summary_metrics,
        visible_tables,
    )
    from filter_batch import LABEL_COLUMN, apply_filter_row

logger = logging.getLogger(__name__)


def combo_signature(combo: dict[str, str]) -> str:
    """Return a stable signature for a filter combination."""
    filters = {key: value for key, value in combo.items() if key != LABEL_COLUMN}
    return json.dumps(filters, sort_keys=True, separators=(",", ":"))


def find_recorded_combos(output_dir: Path) -> tuple[set[str], set[str]]:
    """Find successful combo signatures and legacy labels in saved manifests."""
    signatures: set[str] = set()
    legacy_labels: set[str] = set()
    for manifest_path in Path(output_dir).glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable crawl manifest %s: %s", manifest_path, exc)
            continue

        parquet_files = manifest.get("parquet_files")
        if not isinstance(parquet_files, list) or not parquet_files:
            continue
        if not all((manifest_path.parent / name).is_file() for name in parquet_files):
            continue

        combo_filters = manifest.get("combo_filters")
        if isinstance(combo_filters, dict):
            signatures.add(
                json.dumps(combo_filters, sort_keys=True, separators=(",", ":"))
            )
        else:
            label = manifest.get("filter_description")
            if isinstance(label, str) and label:
                legacy_labels.add(label)

    return signatures, legacy_labels


def is_combo_recorded(
    combo: dict[str, str],
    signatures: set[str],
    legacy_labels: set[str],
) -> bool:
    """Return whether a successful manifest already records this combo."""
    return (
        combo_signature(combo) in signatures
        or combo.get(LABEL_COLUMN, "") in legacy_labels
    )


def load_failed_combos(output_dir: Path) -> list[dict[str, str]]:
    """Load failed combinations saved for a later retry."""
    path = Path(output_dir) / "failed_combinations.json"
    if not path.exists():
        return []
    try:
        raw_combos = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read failed combinations from {path}: {exc}") from exc
    if not isinstance(raw_combos, list) or any(
        not isinstance(combo, dict)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in combo.items())
        for combo in raw_combos
    ):
        raise ValueError(f"Failed combinations file has an invalid format: {path}")
    return raw_combos


def clear_failed_combos(output_dir: Path) -> None:
    """Clear the retry queue without touching saved report exports."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    failed_path = output_dir / "failed_combinations.json"
    temporary_path = failed_path.with_suffix(".json.tmp")
    try:
        temporary_path.write_text("[]", encoding="utf-8")
        temporary_path.replace(failed_path)
    except OSError as exc:
        raise OSError(f"Could not clear failed combinations in {failed_path}: {exc}") from exc


def update_failed_combos(
    output_dir: Path,
    attempted_combos: list[dict[str, str]],
    summary: "CrawlSummary",
    retain_unattempted: bool = True,
) -> list[dict[str, str]]:
    """Persist failures, optionally discarding entries outside the attempted scope."""
    existing = load_failed_combos(output_dir)
    attempted_signatures = {combo_signature(combo) for combo in attempted_combos}
    failed_signatures = {
        combo_signature(attempted_combos[result.index - 1])
        for result in summary.results
        if result.status == ComboStatus.FAILED
        and 1 <= result.index <= len(attempted_combos)
    }
    updated = (
        [
            combo for combo in existing
            if combo_signature(combo) not in attempted_signatures
        ]
        if retain_unattempted
        else []
    )
    updated.extend(
        dict(combo)
        for combo in attempted_combos
        if combo_signature(combo) in failed_signatures
    )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    failed_path = output_dir / "failed_combinations.json"
    temporary_path = failed_path.with_suffix(".json.tmp")
    try:
        temporary_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        temporary_path.replace(failed_path)
    except OSError as exc:
        raise OSError(f"Could not save failed combinations to {failed_path}: {exc}") from exc
    return updated


def _filters_for_combo(
    combo: dict[str, str],
    config: dict[str, Any],
) -> dict[str, str]:
    """In live axis-discovery mode, leave every non-axis portal filter unchanged."""
    if not config.get("discover_x_axes"):
        return {key: value for key, value in combo.items() if key != LABEL_COLUMN}
    return {
        axis: combo[axis]
        for axis in ("yAxis", "xAxis")
        if axis in combo
    }


# ---------------------------------------------------------------------------
# Selectors for the portal's Apply / Generate-Report button
# ---------------------------------------------------------------------------
_APPLY_BUTTON_SELECTORS = [
    "#applyTrigger",
    "button#applyTrigger",
    "button.applyButton",
    "#applyButton",
    "button[id*='apply' i]",
    "button[id*='generate' i]",
    "button[id*='submit' i]",
    "input[type='submit'][id*='apply' i]",
    "input[type='submit'][id*='generate' i]",
    "input[type='button'][value*='Apply' i]",
    "input[type='button'][value*='Generate' i]",
    "button:text-matches('Apply', 'i')",
    "button:text-matches('Generate Report', 'i')",
    "button:text-matches('Get Report', 'i')",
    "button:text-matches('Submit', 'i')",
    "#btnSubmit",
    "#btnApply",
    "#applyFilter",
    "#generateReport",
]
_REPORT_PAGE_PATH = urlparse(PORTAL_URL).path


# ---------------------------------------------------------------------------
# Event / result types
# ---------------------------------------------------------------------------

class ComboStatus(str, Enum):
    PENDING = "⏳ Pending"
    RUNNING = "🔄 Running"
    SUCCESS = "✅ Success"
    FAILED = "❌ Failed"
    SKIPPED = "⏭ Skipped"


@dataclass
class CrawlEvent:
    """Progress event emitted during a crawl run."""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    combo_index: int = 0
    total_combos: int = 0
    status: ComboStatus = ComboStatus.PENDING
    label: str = ""
    message: str = ""
    export_result: Optional[ExportResult] = None
    error: Optional[str] = None


@dataclass
class ComboResult:
    index: int
    label: str
    status: ComboStatus
    export_result: Optional[ExportResult] = None
    error: Optional[str] = None
    attempts: int = 0


@dataclass
class CrawlSummary:
    total: int
    succeeded: int
    failed: int
    skipped: int
    results: list[ComboResult]
    started_at: str
    finished_at: str

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.total if self.total else 0.0


# ---------------------------------------------------------------------------
# CSV / Excel export helpers
# ---------------------------------------------------------------------------

def export_csv(
    tables: list[dict[str, Any]],
    charts: list[dict[str, Any]],
    summary_metrics: list[dict[str, str]],
    downloads: list[Path],
    run_dir: Path,
) -> list[Path]:
    """Write all captured data frames to CSV files in *run_dir*."""
    try:
        from parivahan_extractor.extractor import table_to_dataframe, read_download, safe_filename  # noqa: PLC0415
    except ModuleNotFoundError:
        from extractor import table_to_dataframe, read_download, safe_filename  # noqa: PLC0415
    files: list[Path] = []

    for table in tables:
        df = table_to_dataframe(table)
        if not df.empty:
            path = run_dir / f"visible_table_{table['index']:02d}.csv"
            df.to_csv(path, index=False, encoding="utf-8-sig")
            files.append(path)

    if summary_metrics:
        path = run_dir / "visible_dashboard_metrics.csv"
        pd.DataFrame(summary_metrics).to_csv(path, index=False, encoding="utf-8-sig")
        files.append(path)

    for chart in charts:
        df = table_to_dataframe(chart)
        if not df.empty:
            name = safe_filename(chart.get("title", "chart"))
            path = run_dir / f"chart_{chart['index']:02d}_{name}.csv"
            df.to_csv(path, index=False, encoding="utf-8-sig")
            files.append(path)

    for dl in downloads:
        try:
            for sheet_name, df in read_download(dl).items():
                if not df.empty:
                    name = safe_filename(f"{dl.stem}_{sheet_name}")
                    path = run_dir / f"{name}.csv"
                    df.to_csv(path, index=False, encoding="utf-8-sig")
                    files.append(path)
        except ValueError:
            pass

    return files


def export_excel(
    tables: list[dict[str, Any]],
    charts: list[dict[str, Any]],
    summary_metrics: list[dict[str, str]],
    downloads: list[Path],
    run_dir: Path,
    label: str,
) -> Optional[Path]:
    """Write all data frames to a single Excel workbook in *run_dir*."""
    try:
        from parivahan_extractor.extractor import table_to_dataframe, read_download, safe_filename  # noqa: PLC0415
    except ModuleNotFoundError:
        from extractor import table_to_dataframe, read_download, safe_filename  # noqa: PLC0415
    sheets: list[tuple[str, pd.DataFrame]] = []

    for table in tables:
        df = table_to_dataframe(table)
        if not df.empty:
            sheets.append((f"Table_{table['index']:02d}", df))

    if summary_metrics:
        sheets.append(("Dashboard_Metrics", pd.DataFrame(summary_metrics)))

    for chart in charts:
        df = table_to_dataframe(chart)
        if not df.empty:
            name = safe_filename(chart.get("title", "chart"))[:25]
            sheets.append((f"Chart_{chart['index']:02d}_{name}", df))

    for dl in downloads:
        try:
            for sheet_name, df in read_download(dl).items():
                if not df.empty:
                    sheets.append((safe_filename(sheet_name)[:31], df))
        except ValueError:
            pass

    if not sheets:
        return None

    wb_name = safe_filename(label or "report")[:40]
    path = run_dir / f"{wb_name}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, df in sheets:
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return path


# ---------------------------------------------------------------------------
# Button clicking
# ---------------------------------------------------------------------------

def _click_apply_button(page: Page) -> bool:
    """Find and click the portal Apply/Generate Report button.

    Returns True if a button was found and clicked.
    """
    try:
        page.locator("#applyTrigger").wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        logger.info(
            "The portal's primary Apply button did not appear; checking fallback selectors."
        )

    for selector in _APPLY_BUTTON_SELECTORS:
        try:
            loc = page.locator(selector)
            count = loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(count):
            try:
                btn = loc.nth(i)
                if not btn.is_visible() or not btn.is_enabled():
                    continue
            except Exception:  # noqa: BLE001
                continue
            btn.scroll_into_view_if_needed()
            btn.click(timeout=5_000, no_wait_after=True)
            logger.info("Clicked apply button: %s (nth %d)", selector, i)
            return True
    logger.warning("Could not find a visible Apply button on the page.")
    return False


class InvalidCaptchaError(ValueError):
    """The portal rejected the CAPTCHA for the current combination."""


class CaptchaManualRequiredError(ValueError):
    """The CAPTCHA was not solved; the portal must not be submitted."""


class ReportResultsTimeoutError(RuntimeError):
    """Fresh report results did not appear before the result timeout."""


class ApplyButtonNotFoundError(ValueError):
    """The report page loaded but no usable Apply button appeared."""


_RESULTS_STATE_SCRIPT = """() => {
  const isVisible = element => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const tables = [...document.querySelectorAll(
    "#reportResultTable, #makerPagedReportTable, table"
  )].filter(isVisible);
  const charts = window.Chart && typeof window.Chart.getChart === "function"
    ? [...document.querySelectorAll("canvas")]
        .filter(isVisible)
        .map(canvas => window.Chart.getChart(canvas))
        .filter(Boolean)
        .map(chart => ({
          labels: chart.data.labels || [],
          datasets: (chart.data.datasets || []).map(dataset => dataset.data || [])
        }))
    : [];
  const metrics = [...document.querySelectorAll(".total-count")]
    .filter(isVisible)
    .map(element => element.innerText.trim());
  const contents = JSON.stringify({
    tables: tables.map(table => table.innerText.trim()),
    charts,
    metrics
  });
  const captchaErrorPattern =
    /(?:captcha|verification).{0,80}(?:invalid|incorrect|wrong|not +valid|failed|try again)|(?:invalid|incorrect|wrong|not +valid|valid).{0,80}(?:captcha|verification)/i;
  const captchaError = [
    document.getElementById("captchaError"),
    ...document.querySelectorAll(
      "[role='alert'], .alert-danger, .text-danger, .invalid-feedback, " +
      "[id*='captcha' i], [class*='captcha' i]"
    )
  ]
    .filter(element => element && isVisible(element))
    .map(element => (element.innerText || element.textContent || "").trim())
    .find(text => captchaErrorPattern.test(text)) || "";
  let hash = 2166136261;
  for (let index = 0; index < contents.length; index++) {
    hash = Math.imul(hash ^ contents.charCodeAt(index), 16777619);
  }
  const ready = tables.some(table => table.querySelector("tbody tr")) ||
    charts.some(chart => chart.datasets.some(dataset => dataset.length)) ||
    metrics.length > 0;
  return {
    fingerprint: `${hash >>> 0}:${contents.length}`,
    ready,
    captcha_error: captchaError,
    axis_error:
      document.getElementById("xAxisError")?.innerText?.trim() ||
      document.getElementById("yAxisError")?.innerText?.trim() || ""
  };
}"""


def _get_results_state(page: Page) -> dict[str, Any]:
    return page.evaluate(_RESULTS_STATE_SCRIPT)


def _ensure_report_page(page: Page) -> None:
    """Navigate a reused browser page to the report and wait for its controls."""
    if urlparse(page.url).path.rstrip("/") != _REPORT_PAGE_PATH.rstrip("/"):
        logger.info("Navigating crawler page to the public report from %s", page.url)
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)

    try:
        page.locator("select#yAxis").wait_for(state="attached", timeout=30_000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"The public report did not finish loading at {page.url}; "
            "the Y-axis selector is missing."
        ) from exc


def _wait_for_results(
    page: Page,
    previous_fingerprint: str,
    timeout_ms: int = 3_000,
    stable_for_ms: int = 1_500,
    settle_timeout_ms: int = 5_000,
) -> None:
    """Wait for fresh, stable report data while failing immediately on CAPTCHA errors."""
    try:
        page.wait_for_function(
            """(previousFingerprint) => {
                const state = (""" + _RESULTS_STATE_SCRIPT + """)();
                return Boolean(
                    state.captcha_error ||
                    state.axis_error ||
                    (state.ready && state.fingerprint !== previousFingerprint)
                );
            }""",
            arg=previous_fingerprint,
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        state = _get_results_state(page)
        if state["captcha_error"]:
            raise InvalidCaptchaError(state["captcha_error"])
        if state["axis_error"]:
            raise ValueError(f"Portal axis validation failed: {state['axis_error']}")
        raise ReportResultsTimeoutError(
            f"No fresh report results: none appeared within {timeout_ms} ms; "
            "the existing results were left unexported."
        )

    state = _get_results_state(page)
    if state["captcha_error"]:
        raise InvalidCaptchaError(state["captcha_error"])
    if state["axis_error"]:
        raise ValueError(f"Portal axis validation failed: {state['axis_error']}")
    if not state["ready"] or state["fingerprint"] == previous_fingerprint:
        raise ReportResultsTimeoutError(
            "No fresh report results: the report did not change after Apply; "
            "existing results were left unexported."
        )

    try:
        page.wait_for_function(
            """({previousFingerprint, stableForMs}) => {
                const state = (""" + _RESULTS_STATE_SCRIPT + """)();
                if (state.captcha_error || state.axis_error) return true;
                if (!state.ready || state.fingerprint === previousFingerprint) return false;

                const tracker = window.__parivahanResultSettle;
                if (!tracker || tracker.fingerprint !== state.fingerprint) {
                    window.__parivahanResultSettle = {
                        fingerprint: state.fingerprint,
                        since: performance.now()
                    };
                    return stableForMs === 0;
                }
                return performance.now() - tracker.since >= stableForMs;
            }""",
            arg={
                "previousFingerprint": previous_fingerprint,
                "stableForMs": stable_for_ms,
            },
            timeout=settle_timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        state = _get_results_state(page)
        if state["captcha_error"]:
            raise InvalidCaptchaError(state["captcha_error"])
        if state["axis_error"]:
            raise ValueError(f"Portal axis validation failed: {state['axis_error']}")
        raise ReportResultsTimeoutError(
            f"No fresh report results: data did not settle for {stable_for_ms} ms "
            f"within {settle_timeout_ms} ms; existing results were left unexported."
        ) from exc

    state = _get_results_state(page)
    if state["captcha_error"]:
        raise InvalidCaptchaError(state["captcha_error"])
    if state["axis_error"]:
        raise ValueError(f"Portal axis validation failed: {state['axis_error']}")
    if not state["ready"] or state["fingerprint"] == previous_fingerprint:
        raise ReportResultsTimeoutError(
            "No fresh report results: stable new data was not available; "
            "existing results were left unexported."
        )


# ---------------------------------------------------------------------------
# AutoCrawler
# ---------------------------------------------------------------------------

class AutoCrawler:
    """Orchestrates the automated crawl loop over all filter combinations.

    Parameters
    ----------
    session:
        An active ``PortalSession`` from ``extractor.start_portal``.
    config:
        Parsed config dict (from ``combo_generator.load_config``).
    output_dir:
        Root output directory (e.g. ``Path("output")``).
    """

    def __init__(self, session, config: dict[str, Any], output_dir: Path) -> None:
        self.session = session
        self.config = config
        self.output_dir = Path(output_dir)
        self._stop_requested = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Request the crawl to stop after the current combo finishes."""
        self._stop_requested = True

    def run(
        self,
        combos: list[dict[str, str]],
        progress_callback: Optional[Callable[[CrawlEvent], None]] = None,
        skip_recorded: Optional[Callable[[dict[str, str]], bool]] = None,
    ) -> CrawlSummary:
        """Run the full crawl over *combos*.

        Parameters
        ----------
        combos:
            List of filter dicts (from ``generate_combos``).
        progress_callback:
            Called with a ``CrawlEvent`` after each significant step.

        Returns
        -------
        CrawlSummary
        """
        self._stop_requested = False
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[ComboResult] = []
        total = len(combos)

        def _emit(event: CrawlEvent) -> None:
            logger.info("[%d/%d] %s — %s", event.combo_index, event.total_combos, event.label, event.message)
            if progress_callback:
                progress_callback(event)

        for idx, combo in enumerate(combos, start=1):
            if self._stop_requested:
                results.append(ComboResult(idx, combo.get(LABEL_COLUMN, str(idx)), ComboStatus.SKIPPED))
                _emit(CrawlEvent(combo_index=idx, total_combos=total, status=ComboStatus.SKIPPED,
                                 label=combo.get(LABEL_COLUMN, ""), message="Stop requested."))
                continue

            label = combo.get(LABEL_COLUMN, f"combo_{idx}")
            if skip_recorded is not None and skip_recorded(combo):
                results.append(
                    ComboResult(idx, label, ComboStatus.SKIPPED, attempts=0)
                )
                _emit(
                    CrawlEvent(
                        combo_index=idx,
                        total_combos=total,
                        status=ComboStatus.SKIPPED,
                        label=label,
                        message="Already recorded successfully; skipping.",
                    )
                )
                continue

            _emit(CrawlEvent(combo_index=idx, total_combos=total, status=ComboStatus.RUNNING,
                             label=label, message="Starting…"))

            result = self._process_combo_with_retry(idx, total, combo, label, _emit)
            results.append(result)

            # Delay between requests — skip wait when combo was instantly skipped
            # (unavailable filter), only wait after real network requests.
            if idx < total and not self._stop_requested and result.status != ComboStatus.SKIPPED:
                delay = float(self.config.get("delay_seconds", 1.5))
                fail_fast = (result.error or "").startswith(
                    (
                        "Invalid CAPTCHA:",
                        "CAPTCHA was not solved:",
                        "No fresh report results:",
                        "Could not find a visible Apply button",
                    )
                )
                if delay > 0 and not fail_fast:
                    _emit(CrawlEvent(combo_index=idx, total_combos=total,
                                     status=result.status, label=label,
                                     message=f"Waiting {delay}s before next combo…"))
                    time.sleep(delay)

        finished_at = datetime.now(timezone.utc).isoformat()
        succeeded = sum(1 for r in results if r.status == ComboStatus.SUCCESS)
        failed = sum(1 for r in results if r.status == ComboStatus.FAILED)
        skipped = sum(1 for r in results if r.status == ComboStatus.SKIPPED)

        return CrawlSummary(
            total=total,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # Patterns that indicate a filter value doesn't exist on the portal — skip immediately.
    _SKIP_PATTERNS = (
        "is not currently available",
        "are not currently available",
        "portal search returned it",
        "not available for",
        "Portal axis validation failed",
    )

    def _process_combo_with_retry(
        self,
        idx: int,
        total: int,
        combo: dict[str, str],
        label: str,
        emit: Callable[[CrawlEvent], None],
    ) -> ComboResult:
        max_retries = int(self.config.get("max_retries", 3))
        last_error: Optional[str] = None

        for attempt in range(1, max_retries + 1):
            backoff = min(2 ** (attempt - 1), 30)
            try:
                export_result = self.session.run(self._process_combo, combo, label, emit, idx, total)
                emit(CrawlEvent(combo_index=idx, total_combos=total,
                                status=ComboStatus.SUCCESS, label=label,
                                message=f"Exported {len(export_result.parquet_files)} file(s).",
                                export_result=export_result))
                return ComboResult(idx, label, ComboStatus.SUCCESS,
                                   export_result=export_result, attempts=attempt)
            except NoTabularDataError as exc:
                last_error = str(exc)
                emit(CrawlEvent(combo_index=idx, total_combos=total,
                                status=ComboStatus.SKIPPED, label=label,
                                message=f"No data (skipping): {exc}", error=last_error))
                # No point retrying if the portal returned no table rows at all
                return ComboResult(idx, label, ComboStatus.SKIPPED, error=last_error, attempts=attempt)
            except InvalidCaptchaError as exc:
                last_error = f"Invalid CAPTCHA: {exc}"
                emit(
                    CrawlEvent(
                        combo_index=idx,
                        total_combos=total,
                        status=ComboStatus.FAILED,
                        label=label,
                        message=f"{last_error}; moving to the next combination without retry.",
                        error=last_error,
                    )
                )
                return ComboResult(
                    idx,
                    label,
                    ComboStatus.FAILED,
                    error=last_error,
                    attempts=attempt,
                )
            except CaptchaManualRequiredError as exc:
                last_error = str(exc)
                self._stop_requested = True
                emit(
                    CrawlEvent(
                        combo_index=idx,
                        total_combos=total,
                        status=ComboStatus.FAILED,
                        label=label,
                        message=(
                            f"{last_error} Solve the CAPTCHA in the browser, then "
                            "rerun this failed combination. The crawl is paused to "
                            "avoid marking remaining combinations failed."
                        ),
                        error=last_error,
                    )
                )
                return ComboResult(
                    idx,
                    label,
                    ComboStatus.FAILED,
                    error=last_error,
                    attempts=attempt,
                )
            except ApplyButtonNotFoundError as exc:
                last_error = str(exc)
                emit(
                    CrawlEvent(
                        combo_index=idx,
                        total_combos=total,
                        status=ComboStatus.FAILED,
                        label=label,
                        message=f"{last_error} Moving to the next combination without retry.",
                        error=last_error,
                    )
                )
                return ComboResult(
                    idx,
                    label,
                    ComboStatus.FAILED,
                    error=last_error,
                    attempts=attempt,
                )
            except ReportResultsTimeoutError as exc:
                last_error = str(exc)
                emit(
                    CrawlEvent(
                        combo_index=idx,
                        total_combos=total,
                        status=ComboStatus.FAILED,
                        label=label,
                        message=f"{last_error} Moving to the next combination without retry.",
                        error=last_error,
                    )
                )
                return ComboResult(
                    idx,
                    label,
                    ComboStatus.FAILED,
                    error=last_error,
                    attempts=attempt,
                )
            except (PlaywrightError, PlaywrightTimeoutError, ValueError, OSError) as exc:
                last_error = str(exc)
                # If the error means the filter value simply doesn't exist on the portal,
                # skip immediately — retrying won't help.
                if any(pat in last_error for pat in self._SKIP_PATTERNS):
                    emit(CrawlEvent(combo_index=idx, total_combos=total,
                                    status=ComboStatus.SKIPPED, label=label,
                                    message=f"Skipped (filter unavailable): {exc}",
                                    error=last_error))
                    return ComboResult(idx, label, ComboStatus.SKIPPED, error=last_error, attempts=attempt)
                if attempt < max_retries:
                    emit(CrawlEvent(combo_index=idx, total_combos=total,
                                    status=ComboStatus.RUNNING, label=label,
                                    message=f"Attempt {attempt} failed ({exc}). Retrying in {backoff}s…"))
                    time.sleep(backoff)
                else:
                    emit(CrawlEvent(combo_index=idx, total_combos=total,
                                    status=ComboStatus.FAILED, label=label,
                                    message=f"All {max_retries} attempts failed: {exc}",
                                    error=last_error))

        return ComboResult(idx, label, ComboStatus.FAILED, error=last_error, attempts=max_retries)

    def _process_combo(
        self,
        page: Page,
        combo: dict[str, str],
        label: str,
        emit: Callable[[CrawlEvent], None],
        idx: int,
        total: int,
    ) -> ExportResult:
        """Run one combo: apply filters → solve CAPTCHA → click Apply → export."""

        # 1. Apply filters
        emit(CrawlEvent(combo_index=idx, total_combos=total,
                        status=ComboStatus.RUNNING, label=label,
                        message="Setting filters…"))
        combo_filters = _filters_for_combo(combo, self.config)
        _ensure_report_page(page)
        apply_filter_row(page, combo_filters)

        # 2. Solve CAPTCHA if present
        if is_captcha_present(page):
            emit(CrawlEvent(combo_index=idx, total_combos=total,
                            status=ComboStatus.RUNNING, label=label,
                            message="Solving CAPTCHA…"))
            captcha_max = int(self.config.get("captcha_max_attempts", 5))

            def _captcha_progress(msg: str) -> None:
                emit(CrawlEvent(combo_index=idx, total_combos=total,
                                status=ComboStatus.RUNNING, label=label,
                                message=f"CAPTCHA: {msg}"))

            ok = fill_captcha(page, max_retries=captcha_max, progress_callback=_captcha_progress)
            if not ok:
                raise CaptchaManualRequiredError(
                    "CAPTCHA was not solved; no report was submitted."
                )

        # 3. Click Apply
        emit(CrawlEvent(combo_index=idx, total_combos=total,
                        status=ComboStatus.RUNNING, label=label,
                        message="Clicking Apply…"))
        previous_fingerprint = _get_results_state(page)["fingerprint"]
        if not _click_apply_button(page):
            raise ApplyButtonNotFoundError(
                "Could not find a visible Apply button on the public report page "
                f"({page.url})."
            )

        # 4. Wait for results
        emit(CrawlEvent(combo_index=idx, total_combos=total,
                        status=ComboStatus.RUNNING, label=label,
                        message="Waiting for results…"))
        _wait_for_results(page, previous_fingerprint)

        # Check for axis error
        try:
            axis_err = page.evaluate(
                "() => document.getElementById('xAxisError')?.innerText?.trim() || document.getElementById('yAxisError')?.innerText?.trim() || ''"
            )
            if axis_err:
                raise ValueError(f"Portal axis validation failed: {axis_err}")
        except Exception as exc:
            if "Portal axis validation failed" in str(exc):
                raise

        # 6. Capture data (including all pages for paged tables)
        emit(CrawlEvent(combo_index=idx, total_combos=total,
                        status=ComboStatus.RUNNING, label=label,
                        message="Capturing results (all pages)…"))
        tables = visible_tables(page, fetch_all_pages=True)
        charts = visible_charts(page)
        summary_metrics = visible_summary_metrics(page)
        source_url = page.url
        downloads = list(self.session.downloads)

        # Parquet is the crawler's only output format.
        export_result = export_parquet(
            tables=tables,
            downloads=downloads,
            output_dir=self.output_dir,
            filter_description=label,
            source_url=source_url,
            charts=charts,
            summary_metrics=summary_metrics,
            combo_filters=combo_filters,
        )
        # Clear download queue for next combo
        self.session.downloads.clear()

        return export_result
