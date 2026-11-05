from __future__ import annotations

import csv
import io
from typing import Any

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

FILTER_ORDER = (
    "archivedFlags",
    "reportType",
    "financialYearSelect",
    "reportYear",
    "reportMonth",
    "fromYear",
    "toYear",
    "fromDate",
    "toDate",
    "stateName",
    "rtoCode",
    "vehicleEmission",
    "vehicleMaker",
    "vehicleCategoryGroup",
    "vehicleSubCategory",
    "vehicleClass",
    "vehicleFuel",
    "evType",
    "vehicleStatus",
    "vehicleOwnerType",
    "vehicleType",
    "fitnessCheck",
    "delhiNcr",
    "yAxis",
    "xAxis",
)
SELECT_FILTER_IDS = frozenset(
    {
        "archivedFlags",
        "reportType",
        "financialYearSelect",
        "reportYear",
        "reportMonth",
        "stateName",
        "rtoCode",
        "vehicleEmission",
        "vehicleMaker",
        "vehicleCategoryGroup",
        "vehicleSubCategory",
        "vehicleClass",
        "vehicleFuel",
        "evType",
        "vehicleStatus",
        "vehicleOwnerType",
        "vehicleType",
        "fitnessCheck",
        "delhiNcr",
        "yAxis",
        "xAxis",
    }
)
TEXT_FILTER_IDS = frozenset({"fromYear", "toYear", "fromDate", "toDate"})
FILTER_IDS = SELECT_FILTER_IDS | TEXT_FILTER_IDS
LABEL_COLUMN = "label"

# --- CSV parsing ----------------------------------------------------------
def parse_filter_batch_csv(csv_content: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_content.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("The filter batch CSV must include a header row.")

    headers = [header.strip() for header in reader.fieldnames]
    if len(headers) != len(set(headers)):
        raise ValueError("The filter batch CSV contains duplicate column names.")

    unsupported = set(headers) - FILTER_IDS - {LABEL_COLUMN}
    if unsupported:
        raise ValueError(
            "Unsupported filter column(s): "
            + ", ".join(sorted(unsupported))
            + ". Use portal select IDs as CSV headers."
        )

    if not set(headers) & FILTER_IDS:
        raise ValueError("Add at least one portal filter ID as a CSV column.")

    rows: list[dict[str, str]] = []
    for line_number, source_row in enumerate(reader, start=2):
        if None in source_row:
            raise ValueError(f"CSV row {line_number} has more fields than the header.")
        row = {
            key.strip(): (value or "").strip()
            for key, value in source_row.items()
            if key is not None
        }
        if not any(row.get(filter_id, "") not in {"", "*"} for filter_id in FILTER_IDS):
            raise ValueError(
                f"CSV row {line_number} does not select any filter value. "
                "Use * only to clear a filter when another filter is selected."
            )
        rows.append(row)

    if not rows:
        raise ValueError("The filter batch CSV contains no data rows.")
    return rows


# --- Select / multiselect helpers ----------------------------------------
def _selected_option_data(locator: Any) -> list[dict[str, str]]:
    return locator.evaluate(
        """element => [...element.selectedOptions].map(option => ({
          value: option.value,
          label: (option.textContent || "").trim()
        }))"""
    )


def _set_single_select(locator: Any, value: str) -> None:
    selected = locator.evaluate(
        """(element, value) => {
          if (![...element.options].some(option => option.value === value)) return false;
          element.value = value;
          element.dispatchEvent(new Event("input", {bubbles: true}));
          element.dispatchEvent(new Event("change", {bubbles: true}));
          return element.value === value;
        }""",
        value,
    )
    if not selected:
        raise ValueError(f"Could not select value {value!r} in '{locator.get_attribute('id')}'.")


def _set_y_axis(page: Page, locator: Any, value: str) -> None:
    _set_single_select(locator, value)
    locator.dispatch_event("click")
    page.evaluate(
        """(val) => {
            const h = document.getElementById("yAxis_hidden");
            if (h) h.value = val;
            if (typeof updateXAxisOptions === "function") {
                try { updateXAxisOptions(); } catch(e) {}
            }
        }""",
        value,
    )
    if value:
        try:
            page.wait_for_function(
                "() => { const x = document.getElementById('xAxis'); return x && x.options.length > 1; }",
                timeout=5_000,
            )
        except Exception:
            pass


def discover_axis_combinations(
    page: Page,
    y_axes: list[str],
) -> list[tuple[str, str]]:
    """Discover valid Y/X pairs from the portal's dependent axis dropdowns."""
    if not y_axes:
        raise ValueError("Select at least one Y-axis to discover its X-axis options.")

    y_locator = page.locator("select#yAxis")
    x_locator = page.locator("select#xAxis")
    if y_locator.count() != 1 or x_locator.count() != 1:
        raise ValueError("The report page must expose exactly one Y-axis and X-axis dropdown.")

    combinations: list[tuple[str, str]] = []
    for y_axis in y_axes:
        _set_y_axis(page, y_locator, y_axis)
        page.wait_for_function(
            """(value) => {
              const y = document.getElementById("yAxis");
              const x = document.getElementById("xAxis");
              return y && y.value === value && x &&
                [...x.options].some(option => option.value && !option.disabled);
            }""",
            arg=y_axis,
            timeout=10_000,
        )
        x_values = x_locator.evaluate(
            """element => [...element.options]
              .filter(option => option.value && !option.disabled)
              .map(option => option.value)"""
        )
        if not x_values:
            raise ValueError(f"The portal exposed no valid X-axis options for Y-axis {y_axis!r}.")
        combinations.extend((y_axis, x_axis) for x_axis in x_values)

    return list(dict.fromkeys(combinations))


def _set_x_axis(page: Page, locator: Any, value: str) -> None:
    # Ensure options are populated if yAxis was recently changed
    try:
        page.wait_for_function(
            "() => { const x = document.getElementById('xAxis'); return x && x.options.length > 1; }",
            timeout=3_000,
        )
    except Exception:
        # Re-trigger updateXAxisOptions if needed
        page.evaluate(
            """() => {
                const y = document.getElementById("yAxis");
                if (y) {
                    y.dispatchEvent(new Event("click", {bubbles: true}));
                    y.dispatchEvent(new Event("change", {bubbles: true}));
                }
                if (typeof updateXAxisOptions === "function") {
                    try { updateXAxisOptions(); } catch(e) {}
                }
            }"""
        )
        try:
            page.wait_for_function(
                "() => { const x = document.getElementById('xAxis'); return x && x.options.length > 1; }",
                timeout=3_000,
            )
        except Exception:
            pass

    _set_single_select(locator, value)
    page.evaluate(
        """(val) => {
            const h = document.getElementById("xAxis_hidden");
            if (h) h.value = val;
            const err = document.getElementById("xAxisError");
            if (err) err.innerText = "";
        }""",
        value,
    )


def _set_multiselect_filter(page: Page, filter_id: str, requested_values: list[str]) -> None:
    select = page.locator(f"select#{filter_id}")
    widget = page.locator(f"select#{filter_id} + .multiselect-dropdown")
    if widget.count() != 1:
        raise ValueError(f"Portal multi-select widget '{filter_id}' is not available.")

    initial_options = select.evaluate(
        """element => [...element.options].map(option => ({
          value: option.value,
          label: (option.textContent || "").trim()
        }))"""
    )
    unavailable = [
        value for value in requested_values
        if value not in {option["value"] for option in initial_options}
    ]
    if filter_id == "rtoCode" and unavailable:
        try:
            page.wait_for_function(
                """({id, values}) => {
                  const select = document.getElementById(id);
                  if (!select) return false;
                  const options = new Set([...select.options].map(option => option.value));
                  return values.every(value => options.has(value));
                }""",
                arg={"id": filter_id, "values": unavailable},
                timeout=10_000,
            )
        except PlaywrightTimeoutError as error:
            raise ValueError(
                "RTO options are not loaded for the selected state(s), or the requested "
                f"RTO value(s) are unavailable: {unavailable!r}."
            ) from error

    wrapper = widget.locator(".multiselect-dropdown-list-wrapper")
    if wrapper.evaluate("element => getComputedStyle(element).display === 'none'"):
        widget.click()
    search = widget.locator("input.multiselect-dropdown-search")

    selected_values = {
        option["value"] for option in _selected_option_data(select)
    }
    wanted_values = set(requested_values)
    current_options = select.evaluate(
        """element => [...element.options].map(option => ({
          value: option.value,
          label: (option.textContent || "").trim()
        }))"""
    )
    for option in current_options:
        if option["value"] not in selected_values or option["value"] in wanted_values:
            continue
        search.fill(option["label"])
        label = widget.get_by_text(option["label"], exact=True)
        if label.count() != 1:
            raise ValueError(
                f"Could not find selected option {option['label']!r} in '{filter_id}'."
            )
        checkbox = label.locator("xpath=preceding-sibling::input[@type='checkbox']")
        if checkbox.is_checked():
            label.click()

    for value in requested_values:
        selected_values = {
            option["value"] for option in _selected_option_data(select)
        }
        if value in selected_values:
            continue

        option_data = select.evaluate(
            """(element, value) => {
              const option = [...element.options].find(candidate => candidate.value === value);
              return option ? {
                value: option.value,
                label: (option.textContent || "").trim()
              } : null;
            }""",
            value,
        )
        if option_data is None:
            search.fill(value)
            try:
                page.wait_for_function(
                    """({id, value}) => {
                      const select = document.getElementById(id);
                      return select && [...select.options].some(option => option.value === value);
                    }""",
                    arg={"id": filter_id, "value": value},
                    timeout=10_000,
                )
            except PlaywrightTimeoutError as error:
                raise ValueError(
                    f"Value {value!r} is not currently available for '{filter_id}'. "
                    "Check spelling, dependencies, and whether the portal search returned it."
                ) from error
            option_data = select.evaluate(
                """(element, value) => {
                  const option = [...element.options].find(candidate => candidate.value === value);
                  return option ? {
                    value: option.value,
                    label: (option.textContent || "").trim()
                  } : null;
                }""",
                value,
            )
        search.fill(option_data["label"])
        label = widget.get_by_text(option_data["label"], exact=True)
        if label.count() != 1:
            raise ValueError(
                f"Could not find option {option_data['label']!r} in '{filter_id}'."
            )
        label.click()

    search.fill("")
    if wrapper.evaluate("element => getComputedStyle(element).display !== 'none'"):
        page.mouse.click(5, 5)
    actual_values = {
        option["value"] for option in _selected_option_data(select)
    }
    if actual_values != wanted_values:
        raise ValueError(
            f"Could not set '{filter_id}' to {requested_values!r}; "
            f"portal currently has {sorted(actual_values)!r} selected."
        )


def apply_filter_row(
    page: Page,
    row: dict[str, str],
) -> None:
    """Set the selected portal filters without interacting with CAPTCHA."""
    unknown_filters = set(row) - FILTER_IDS - {LABEL_COLUMN}
    if unknown_filters:
        raise ValueError("Unknown filter ID(s): " + ", ".join(sorted(unknown_filters)))

    for filter_id in FILTER_ORDER:
        if filter_id not in row:
            continue

        raw_value = row[filter_id].strip()
        if filter_id in TEXT_FILTER_IDS:
            if raw_value and raw_value != "*":
                locator = page.locator(f"input#{filter_id}")
                if locator.count() != 1:
                    raise ValueError(f"Portal filter '{filter_id}' is not available on this page.")
                locator.fill(raw_value)
            continue

        locator = page.locator(f"select#{filter_id}")
        if locator.count() != 1:
            raise ValueError(f"Portal filter '{filter_id}' is not available on this page.")

        field_info = locator.evaluate(
            """element => ({
              multiple: element.multiple,
              options: [...element.options].map(option => option.value)
            })"""
        )
        if not raw_value or raw_value == "*":
            if field_info["multiple"]:
                _set_multiselect_filter(page, filter_id, [])
            else:
                first_value = field_info["options"][0] if field_info["options"] else ""
                if filter_id == "yAxis":
                    _set_y_axis(page, locator, first_value)
                elif filter_id == "xAxis":
                    _set_x_axis(page, locator, first_value)
                else:
                    _set_single_select(locator, first_value)
            continue

        requested_values = [value.strip() for value in raw_value.split("|")]
        if any(not value for value in requested_values):
            raise ValueError(
                f"Filter '{filter_id}' has an empty value between '|' separators."
            )
        if not field_info["multiple"] and len(requested_values) > 1:
            raise ValueError(
                f"Filter '{filter_id}' is single-select; provide one value, not a '|' list."
            )

        if field_info["multiple"]:
            _set_multiselect_filter(page, filter_id, requested_values)
            continue

        # If setting xAxis, wait for options to be populated dynamically if needed
        if filter_id == "xAxis":
            try:
                page.wait_for_function(
                    "() => { const x = document.getElementById('xAxis'); return x && x.options.length > 1; }",
                    timeout=4_000,
                )
            except Exception:
                pass
            field_info["options"] = locator.evaluate("el => [...el.options].map(o => o.value)")

        missing_values = [value for value in requested_values if value not in field_info["options"]]
        if missing_values:
            try:
                page.wait_for_function(
                    """({id, values}) => {
                      const select = document.getElementById(id);
                      if (!select) return false;
                      const options = new Set([...select.options].map(option => option.value));
                      return values.every(value => options.has(value));
                    }""",
                    arg={"id": filter_id, "values": missing_values},
                    timeout=10_000,
                )
            except PlaywrightTimeoutError as error:
                raise ValueError(
                    f"Value(s) {missing_values!r} are not currently available for "
                    f"'{filter_id}'. Check the value spelling and any parent filters."
                ) from error

        if filter_id == "yAxis":
            _set_y_axis(page, locator, requested_values[0])
        elif filter_id == "xAxis":
            _set_x_axis(page, locator, requested_values[0])
        else:
            _set_single_select(locator, requested_values[0])

    # If yAxis was set but xAxis was omitted in row, auto-select a valid default xAxis (Total Consolidated)
    if "yAxis" in row and "xAxis" not in row:
        x_locator = page.locator("select#xAxis")
        if x_locator.count() == 1:
            try:
                _set_x_axis(page, x_locator, "Total Consolidated")
            except Exception:
                opts = x_locator.evaluate("el => [...el.options].map(o => o.value).filter(Boolean)")
                if opts:
                    _set_x_axis(page, x_locator, opts[0])

    # Keep maker CSV and hidden axis synchronized
    page.evaluate(
        """() => {
            const x = document.getElementById("xAxis");
            const xh = document.getElementById("xAxis_hidden");
            if (x && xh && x.value) xh.value = x.value;

            const y = document.getElementById("yAxis");
            const yh = document.getElementById("yAxis_hidden");
            if (y && yh && y.value) yh.value = y.value;

            const vm = document.getElementById("vehicleMaker");
            const sm = document.getElementById("selectedMakers");
            if (vm && sm) {
                const vals = [...vm.selectedOptions].map(o => o.value).filter(Boolean);
                sm.value = vals.join(",");
            }
            // Clear stale error messages
            const errs = ["#xAxisError", "#yAxisError", "#dateError", "#captchaError", "#makerError"];
            errs.forEach(s => {
                const el = document.querySelector(s);
                if (el) el.innerText = "";
            });
        }"""
    )


def format_filter_row(row: dict[str, Any]) -> str:
    return ", ".join(
        f"{key}={value}"
        for key, value in row.items()
        if key != LABEL_COLUMN and str(value).strip()
    )