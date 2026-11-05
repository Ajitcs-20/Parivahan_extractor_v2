from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

import pandas as pd
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright, sync_playwright

PORTAL_URL = "https://analytics.parivahan.gov.in/analytics/vahanpublicreport?lang=en"
DASHBOARD_URL = "https://analytics.parivahan.gov.in/analytics/publicdashboard/vahan?lang=en"
PROFILE_DIR_NAME = "browser_profile"
ResultT = TypeVar("ResultT")

VISIBLE_TABLES_SCRIPT = """() => {
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  return [...document.querySelectorAll("table")]
  .filter(isVisible)
  .map((table, index) => {
    const visibleRows = [...table.querySelectorAll("tr")].filter(isVisible);
    const rows = visibleRows
      .map((row) => [...row.querySelectorAll("th, td")]
        .map((cell) => (cell.innerText || "").trim()));
    const headerRow = visibleRows.findIndex((row) => row.querySelector("th"));
    return {index: index + 1, rows, headerRow};
  })
    .filter((table) => table.rows.length > 0);
}"""

MAKER_ALL_PAGES_SCRIPT = """async () => {
  const form = document.getElementById("vahanPublicForm");
  if (!document.getElementById("makerPagedReportTable") && !document.getElementById("downloadMakerAllCsv")) {
    return null;
  }

  function buildReq(afterMaker) {
    const params = new URLSearchParams(new FormData(form));
    params.delete("captcha");
    const xEl = document.getElementById("xAxis_hidden") || document.getElementById("xAxis");
    const yEl = document.getElementById("yAxis_hidden") || document.getElementById("yAxis");
    params.set("xAxis", xEl ? xEl.value : "");
    params.set("yAxis", yEl ? yEl.value : "");
    params.set("pageSize", "100");
    params.set("afterMaker", afterMaker || "");
    return params;
  }

  let cursor = "";
  let hasMore = true;
  let pages = 0;
  const allRows = {};

  while (hasMore && pages < 400) {
    pages++;
    try {
      const resp = await fetch("/analytics/vahanpublicreport/maker-report-page", {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: buildReq(cursor).toString()
      });
      if (!resp.ok) break;
      const data = await resp.json();
      const rows = data.rows || {};
      Object.assign(allRows, rows);
      hasMore = Boolean(data.hasMore && data.nextAfterMaker);
      cursor = hasMore ? data.nextAfterMaker : "";
    } catch (e) {
      break;
    }
  }

  const makers = Object.keys(allRows);
  if (makers.length === 0) return null;

  const colSet = new Set();
  makers.forEach((m) => Object.keys(allRows[m] || {}).forEach((c) => colSet.add(c)));
  const columns = Array.from(colSet);

  const tableRows = [];
  tableRows.push(["Maker", ...columns, "Total"]);

  makers.forEach((m) => {
    let rTotal = 0;
    const row = [m];
    columns.forEach((c) => {
      const v = Number(allRows[m][c] || 0);
      rTotal += v;
      row.push(String(v));
    });
    row.push(String(rTotal));
    tableRows.push(row);
  });

  return {
    index: 1,
    title: "maker_report_all_pages",
    headerRow: 0,
    rows: tableRows
  };
}"""

VISIBLE_CHARTS_SCRIPT = """() => {
  if (!window.Chart || typeof window.Chart.getChart !== "function") return [];
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  return [...document.querySelectorAll("canvas")]
    .filter(isVisible)
    .map(canvas => window.Chart.getChart(canvas))
    .filter(chart => chart)
    .map((chart, index) => {
      let title = chart.options?.plugins?.title?.text || "";
      if (Array.isArray(title)) title = title.join(" ");
      if (!title) {
        for (let element = chart.canvas.parentElement; element && element !== document.body;
             element = element.parentElement) {
          const heading = element.querySelector("h1, h2, h3, h4, h5, h6");
          if (heading && heading.innerText.trim()) {
            title = heading.innerText.trim();
            break;
          }
        }
      }
      title = title || `chart_${index + 1}`;
      const chartType = chart.config.type || "";
      const rows = [["category", "series", "value", "chart_type"]];
      const labels = chart.data.labels || [];
      for (const [datasetIndex, dataset] of (chart.data.datasets || []).entries()) {
        const values = dataset.data || [];
        const series = dataset.label || `Series ${datasetIndex + 1}`;
        for (let valueIndex = 0; valueIndex < values.length; valueIndex++) {
          rows.push([
            labels[valueIndex] ?? String(valueIndex + 1),
            series,
            values[valueIndex],
            chartType
          ]);
        }
      }
      return {
        index: index + 1,
        title,
        chartType,
        headerRow: 0,
        rows
      };
    })
    .filter(chart => chart.rows.length > 1);
}"""

VISIBLE_SUMMARY_METRICS_SCRIPT = """() => {
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  return [...document.querySelectorAll(".total-count")]
    .filter(isVisible)
    .map(element => ({
      metric: element.querySelector("h1, h2, h3, h4, h5, h6")?.innerText.trim() || "",
      value: element.querySelector(".count")?.innerText.trim() || ""
    }))
    .filter(item => item.metric && item.value);
}"""


class NoTabularDataError(RuntimeError):
    """The portal has not exposed exportable table, chart, or download data."""


@dataclass
class PortalSession:
    executor: ThreadPoolExecutor
    playwright: Playwright
    context: BrowserContext
    page: Page
    profile_dir: Path
    download_dir: Path
    downloads: list[Path] = field(default_factory=list)
    download_errors: list[str] = field(default_factory=list)

    def run(self, action: Callable[..., ResultT], *args: Any) -> ResultT:
        return self.executor.submit(action, self.page, *args).result()


@dataclass(frozen=True)
class ExportResult:
    parquet_files: tuple[Path, ...]
    manifest_path: Path
    row_counts: dict[str, int]


def safe_filename(value: str, fallback: str = "report") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(value).name).strip("._-")
    return cleaned or fallback


def browser_profile_dir(output_dir: Path) -> Path:
    return output_dir / PROFILE_DIR_NAME


def cleanup_browser_locks(profile_dir: Path) -> None:
    """Safely terminate any orphaned Playwright chromium processes and delete lock files."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-Process chrome -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.Path -like '*ms-playwright*' } | "
                    "Stop-Process -Force -ErrorAction SilentlyContinue"
                ],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

    time.sleep(0.3)

    lock_names = ["SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"]
    for lock_name in lock_names:
        lock_path = profile_dir / lock_name
        if lock_path.exists():
            try:
                lock_path.unlink()
            except Exception:
                pass


def is_session_alive(session: PortalSession | None) -> bool:
    """Check if the browser session and its primary page are still alive."""
    if session is None:
        return False
    try:
        return bool(session.run(lambda p: not p.is_closed()))
    except Exception:
        return False


def start_portal(output_dir: Path, dashboard: bool = False) -> PortalSession:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = output_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = browser_profile_dir(output_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    cleanup_browser_locks(profile_dir)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parivahan-playwright")
    try:
        return executor.submit(
            _start_portal_on_worker,
            executor,
            downloads_dir,
            profile_dir,
            dashboard,
        ).result()
    except BaseException:
        executor.shutdown(wait=False)
        raise


def wait_for_manual_captcha_if_present(page: Page) -> None:
    page.wait_for_timeout(1000)


def _start_portal_on_worker(
    executor: ThreadPoolExecutor,
    downloads_dir: Path,
    profile_dir: Path,
    dashboard: bool,
) -> PortalSession:
    playwright = sync_playwright().start()
    context: BrowserContext | None = None
    target_profile_dir = profile_dir

    cleanup_browser_locks(target_profile_dir)

    for attempt in range(3):
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(target_profile_dir),
                headless=False,
                accept_downloads=True,
            )
            break
        except Exception as exc:
            err_str = str(exc)
            if "ProcessSingleton" in err_str or "profile is already in use" in err_str or "0x20" in err_str:
                cleanup_browser_locks(target_profile_dir)
                time.sleep(0.5)
                if attempt >= 1:
                    target_profile_dir = profile_dir.parent / f"{PROFILE_DIR_NAME}_{int(time.time())}"
                    target_profile_dir.mkdir(parents=True, exist_ok=True)
            else:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                try:
                    playwright.stop()
                except Exception:
                    pass
                raise
    else:
        target_profile_dir = profile_dir.parent / f"{PROFILE_DIR_NAME}_{int(time.time())}"
        target_profile_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(target_profile_dir),
            headless=False,
            accept_downloads=True,
        )

    page = context.pages[0] if context.pages else context.new_page()
    session = PortalSession(
        executor=executor,
        playwright=playwright,
        context=context,
        page=page,
        profile_dir=target_profile_dir,
        download_dir=downloads_dir,
    )

    def save_download(download: Any) -> None:
        filename = safe_filename(download.suggested_filename)
        destination = downloads_dir / filename
        if destination.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            destination = downloads_dir / f"{destination.stem}_{timestamp}{destination.suffix}"
        try:
            download.save_as(str(destination))
        except PlaywrightError as error:
            session.download_errors.append(str(error))
            return
        session.downloads.append(destination)

    page.on("download", save_download)
    page.goto(
        DASHBOARD_URL if dashboard else PORTAL_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    return session


def close_portal(session: PortalSession | None) -> None:
    if session is None:
        return

    def close_browser(_page: Page) -> None:
        try:
            session.context.close()
        except Exception:
            pass
        finally:
            try:
                session.playwright.stop()
            except Exception:
                pass

    try:
        session.run(close_browser)
    except Exception:
        pass
    finally:
        try:
            session.executor.shutdown(wait=False)
        except Exception:
            pass

    try:
        cleanup_browser_locks(session.profile_dir)
    except Exception:
        pass


def visible_tables(page: Page, fetch_all_pages: bool = True) -> list[dict[str, Any]]:
    # 1. First check if it's a Maker server-paged report
    if fetch_all_pages:
        try:
            maker_data = page.evaluate(MAKER_ALL_PAGES_SCRIPT)
            if maker_data and maker_data.get("rows"):
                return [maker_data]
        except Exception:
            pass

    # 2. Extract standard visible tables
    tables = page.evaluate(VISIBLE_TABLES_SCRIPT)
    if not tables:
        return []

    # 3. If there is pagination and fetch_all_pages is True, crawl next pages
    if fetch_all_pages and tables:
        try:
            next_loc = page.locator(
                "#makerServerNext, ul.pagination li:not(.disabled) a:text-matches('Next|»|›', 'i'), .paginate_button.next:not(.disabled)"
            ).first
            if next_loc.count() > 0 and next_loc.is_visible() and next_loc.is_enabled():
                first_table_rows = list(tables[0]["rows"])
                seen_signatures = {tuple(r) for r in first_table_rows}
                for _ in range(50):
                    if not (next_loc.is_visible() and next_loc.is_enabled()):
                        break
                    next_loc.click()
                    page.wait_for_timeout(800)
                    new_tables = page.evaluate(VISIBLE_TABLES_SCRIPT)
                    if not new_tables or not new_tables[0]["rows"]:
                        break
                    new_rows = new_tables[0]["rows"][1:]
                    added = 0
                    for r in new_rows:
                        sig = tuple(r)
                        if sig not in seen_signatures:
                            first_table_rows.append(r)
                            seen_signatures.add(sig)
                            added += 1
                    if added == 0:
                        break
                tables[0]["rows"] = first_table_rows
        except Exception:
            pass

    return tables


def visible_charts(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(VISIBLE_CHARTS_SCRIPT)


def visible_summary_metrics(page: Page) -> list[dict[str, str]]:
    return page.evaluate(VISIBLE_SUMMARY_METRICS_SCRIPT)


def table_to_dataframe(table: dict[str, Any]) -> pd.DataFrame:
    rows: list[list[Any]] = table["rows"]
    header_index = table.get("headerRow", -1)
    if header_index < 0 or header_index >= len(rows):
        headers = [f"column_{index + 1}" for index in range(max(map(len, rows), default=0))]
        data_rows = rows
    else:
        headers = rows[header_index]
        data_rows = rows[header_index + 1 :]

    width = max([len(headers), *(len(row) for row in data_rows)], default=0)
    headers = headers + [f"column_{index + 1}" for index in range(len(headers), width)]
    unique_headers: list[str] = []
    seen: dict[str, int] = {}
    for index, header in enumerate(headers):
        cleaned = header.strip() or f"column_{index + 1}"
        occurrence = seen.get(cleaned, 0) + 1
        seen[cleaned] = occurrence
        unique_headers.append(cleaned if occurrence == 1 else f"{cleaned}_{occurrence}")

    normalized_rows = [row + [None] * (width - len(row)) for row in data_rows]
    return pd.DataFrame(normalized_rows, columns=unique_headers)


def read_download(path: Path) -> dict[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return {path.stem: pd.read_csv(path)}
    if suffix in {".xlsx", ".xlsm"}:
        return {
            str(sheet_name): dataframe
            for sheet_name, dataframe in pd.read_excel(path, sheet_name=None).items()
        }
    raise ValueError(f"Unsupported portal download format: {path.name}")


def export_parquet(
    tables: list[dict[str, Any]],
    downloads: list[Path],
    output_dir: Path,
    filter_description: str,
    source_url: str,
    charts: list[dict[str, Any]] | None = None,
    summary_metrics: list[dict[str, str]] | None = None,
    combo_filters: dict[str, str] | None = None,
) -> ExportResult:
    if urlparse(source_url).hostname != "analytics.parivahan.gov.in":
        raise ValueError("Exports are limited to the official Parivahan analytics portal.")

    exports: list[tuple[str, pd.DataFrame]] = []

    for table in tables:
        dataframe = table_to_dataframe(table)
        if not dataframe.empty:
            exports.append((f"visible_table_{table['index']:02d}", dataframe))

    if summary_metrics:
        exports.append(("visible_dashboard_metrics", pd.DataFrame(summary_metrics)))

    for chart in charts or []:
        dataframe = table_to_dataframe(chart)
        if not dataframe.empty:
            chart_name = safe_filename(chart.get("title", "chart"))
            exports.append((f"chart_{chart['index']:02d}_{chart_name}", dataframe))

    for download in downloads:
        for sheet_name, dataframe in read_download(download).items():
            if not dataframe.empty:
                exports.append(
                    (f"{download.stem}_{safe_filename(sheet_name)}", dataframe)
                )

    if not exports:
        raise NoTabularDataError(
            "No visible tables, summary metrics, chart datasets, or non-empty CSV/Excel "
            "downloads were found. Apply dashboard/report filters, wait for results, and use "
            "a portal download if available."
        )

    captured_at = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    parquet_files: list[Path] = []
    row_counts: dict[str, int] = {}
    for name, dataframe in exports:
        filename = safe_filename(name)
        parquet_path = run_dir / f"{filename}.parquet"
        dataframe.to_parquet(parquet_path, index=False, engine="pyarrow")
        parquet_files.append(parquet_path)
        row_counts[parquet_path.name] = len(dataframe)

    manifest = {
        "source_url": source_url,
        "captured_at_utc": captured_at,
        "filter_description": filter_description.strip(),
        "download_files": [path.name for path in downloads],
        "chart_titles": [chart["title"] for chart in charts or []],
        "summary_metrics": [metric["metric"] for metric in summary_metrics or []],
        "parquet_files": [path.name for path in parquet_files],
        "row_counts": row_counts,
    }
    if combo_filters is not None:
        manifest["combo_filters"] = combo_filters
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return ExportResult(tuple(parquet_files), manifest_path, row_counts)


def export_csv_files(
    tables: list[dict[str, Any]],
    charts: list[dict[str, Any]] | None,
    summary_metrics: list[dict[str, str]] | None,
    downloads: list[Path],
    run_dir: Path,
) -> list[Path]:
    """Write all captured data to CSV files inside an already-created *run_dir*.

    Returns a list of written file paths.
    """
    csv_paths: list[Path] = []

    for table in tables:
        dataframe = table_to_dataframe(table)
        if not dataframe.empty:
            path = run_dir / f"visible_table_{table['index']:02d}.csv"
            dataframe.to_csv(path, index=False, encoding="utf-8-sig")
            csv_paths.append(path)

    if summary_metrics:
        path = run_dir / "visible_dashboard_metrics.csv"
        pd.DataFrame(summary_metrics).to_csv(path, index=False, encoding="utf-8-sig")
        csv_paths.append(path)

    for chart in charts or []:
        dataframe = table_to_dataframe(chart)
        if not dataframe.empty:
            chart_name = safe_filename(chart.get("title", "chart"))
            path = run_dir / f"chart_{chart['index']:02d}_{chart_name}.csv"
            dataframe.to_csv(path, index=False, encoding="utf-8-sig")
            csv_paths.append(path)

    for download in downloads:
        try:
            for sheet_name, dataframe in read_download(download).items():
                if not dataframe.empty:
                    name = safe_filename(f"{download.stem}_{sheet_name}")
                    path = run_dir / f"{name}.csv"
                    dataframe.to_csv(path, index=False, encoding="utf-8-sig")
                    csv_paths.append(path)
        except ValueError:
            pass

    return csv_paths


def export_excel_workbook(
    tables: list[dict[str, Any]],
    charts: list[dict[str, Any]] | None,
    summary_metrics: list[dict[str, str]] | None,
    downloads: list[Path],
    run_dir: Path,
    workbook_stem: str = "report",
) -> Path | None:
    """Write all captured data frames to a single Excel workbook in *run_dir*.

    Returns the written path, or None if there was no data.
    """
    sheets: list[tuple[str, pd.DataFrame]] = []

    for table in tables:
        dataframe = table_to_dataframe(table)
        if not dataframe.empty:
            sheets.append((f"Table_{table['index']:02d}", dataframe))

    if summary_metrics:
        sheets.append(("Dashboard_Metrics", pd.DataFrame(summary_metrics)))

    for chart in charts or []:
        dataframe = table_to_dataframe(chart)
        if not dataframe.empty:
            name = safe_filename(chart.get("title", "chart"))[:25]
            sheets.append((f"Chart_{chart['index']:02d}_{name}", dataframe))

    for download in downloads:
        try:
            for sheet_name, dataframe in read_download(download).items():
                if not dataframe.empty:
                    sheets.append((safe_filename(sheet_name)[:31], dataframe))
        except ValueError:
            pass

    if not sheets:
        return None

    stem = safe_filename(workbook_stem)[:40] or "report"
    workbook_path = run_dir / f"{stem}.xlsx"
    with pd.ExcelWriter(workbook_path, engine="openpyxl") as writer:
        for sheet_name, dataframe in sheets:
            dataframe.to_excel(writer, sheet_name=sheet_name[:31], index=False)

    return workbook_path
