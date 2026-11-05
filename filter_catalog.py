from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from playwright.sync_api import sync_playwright

import sys
_current_dir = Path(__file__).resolve().parent
_parent_dir = _current_dir.parent
for _p in (str(_parent_dir), str(_current_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from parivahan_extractor.extractor import PORTAL_URL
except ModuleNotFoundError:
    from extractor import PORTAL_URL

FILTER_LABELS = {
    "archivedFlags": "Active/archive type",
    "reportType": "Year type",
    "financialYearSelect": "Financial year",
    "reportYear": "Calendar year",
    "reportMonth": "Month",
    "stateName": "State",
    "rtoCode": "RTO (depends on selected state)",
    "vehicleEmission": "Emission",
    "vehicleMaker": "Maker (options currently loaded)",
    "vehicleCategoryGroup": "Category group",
    "vehicleSubCategory": "Subcategory",
    "vehicleClass": "Vehicle class",
    "vehicleFuel": "Fuel",
    "evType": "EV type",
    "vehicleStatus": "Status",
    "vehicleOwnerType": "Owner type",
    "vehicleType": "Vehicle type",
    "fitnessCheck": "Fitness check",
    "delhiNcr": "Delhi NCR",
    "yAxis": "Y-axis",
    "xAxis": "X-axis",
}

FILTER_OPTIONS_SCRIPT = """() => [...document.querySelectorAll("select")].map((select) => ({
  filter_id: select.id || "",
  filter_name: select.name || "",
  is_multiple: select.multiple,
  options: [...select.options].map((option, index) => ({
    option_position: index,
    option_value: option.value,
    option_label: (option.textContent || "").trim(),
    is_selected: option.selected,
    is_disabled: option.disabled,
    group_label: option.parentElement?.tagName === "OPTGROUP"
      ? option.parentElement.label
      : ""
  }))
}))"""


def collect_filter_options() -> tuple[list[dict[str, Any]], str]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(PORTAL_URL, wait_until="networkidle", timeout=60_000)
            page.locator("select#stateName").wait_for(state="attached", timeout=30_000)
            filters = page.evaluate(FILTER_OPTIONS_SCRIPT)
            source_url = page.url
        finally:
            browser.close()
    return filters, source_url


def build_filter_catalog(
    filters: list[dict[str, Any]],
    captured_at_utc: str | None = None,
) -> pd.DataFrame:
    captured_at = captured_at_utc or datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for filter_data in filters:
        filter_id = filter_data["filter_id"]
        options = filter_data["options"]
        if not options:
            rows.append(
                {
                    "filter_id": filter_id,
                    "filter_name": filter_data["filter_name"],
                    "filter_label": FILTER_LABELS.get(filter_id, filter_id),
                    "is_multiple": filter_data["is_multiple"],
                    "option_position": None,
                    "option_value": None,
                    "option_label": None,
                    "group_label": None,
                    "is_selected": False,
                    "is_disabled": False,
                    "option_count": 0,
                    "captured_at_utc": captured_at,
                    "note": "No options loaded; this filter may depend on another selection.",
                }
            )
            continue

        for option in options:
            rows.append(
                {
                    "filter_id": filter_id,
                    "filter_name": filter_data["filter_name"],
                    "filter_label": FILTER_LABELS.get(filter_id, filter_id),
                    "is_multiple": filter_data["is_multiple"],
                    "option_position": option["option_position"],
                    "option_value": option["option_value"],
                    "option_label": option["option_label"],
                    "group_label": option["group_label"],
                    "is_selected": option["is_selected"],
                    "is_disabled": option["is_disabled"],
                    "option_count": len(options),
                    "captured_at_utc": captured_at,
                    "note": "",
                }
            )
    return pd.DataFrame(rows)


def export_filter_catalog(output_dir: Path) -> tuple[Path, Path, int]:
    filters, source_url = collect_filter_options()
    dataframe = build_filter_catalog(filters)
    if dataframe.empty:
        raise ValueError("The portal did not expose any filter dropdowns.")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = output_dir / "filter_catalog" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    parquet_path = run_dir / "filter_catalog.parquet"
    dataframe.to_parquet(parquet_path, index=False, engine="pyarrow")

    counts = {
        item["filter_id"]: len(item["options"])
        for item in filters
    }
    manifest = {
        "source_url": source_url,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_type": "portal_filter_option_catalog",
        "note": (
            "This is the dropdown option metadata currently loaded by the report form, "
            "not vehicle registration results. Dependent filters may need a parent selection; "
            "RTO options are not loaded until a state is selected. Dynamic/searchable lists "
            "may show only the options loaded at capture time."
        ),
        "filter_count": len(filters),
        "option_counts": counts,
        "option_rows_including_empty_filter_markers": len(dataframe),
        "parquet_file": parquet_path.name,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return parquet_path, manifest_path, len(dataframe)


if __name__ == "__main__":
    project_dir = Path(__file__).resolve().parent
    parquet_file, manifest_file, row_count = export_filter_catalog(project_dir / "output")
    print(f"Filter catalog rows: {row_count}")
    print(f"Parquet: {parquet_file}")
    print(f"Manifest: {manifest_file}")
