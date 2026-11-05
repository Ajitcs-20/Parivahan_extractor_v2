"""combo_generator.py — Filter combination generator for the auto-crawler.

Reads a YAML (or dict) config that lists filter IDs and their allowed values,
then generates the Cartesian product of all combinations as a list of dicts
that match the ``apply_filter_row`` API.

Config format example (YAML)
-----------------------------
delay_seconds: 3          # seconds to wait between crawl requests
max_retries: 4            # max CAPTCHA solve attempts per combo
captcha_max_attempts: 5   # max OCR retries before manual fallback
export_formats:           # which formats to write (any subset)
  - parquet
  - csv
  - excel
filters:
  reportYear:   [2023, 2024, 2025]
  stateName:    [DL, MH, GA, KA]
  vehicleFuel:  [PETROL, DIESEL, ELECTRIC]
  yAxis:        [stateCode]               # single value → treated as constant
  vehicleCategoryGroup: ["Two Wheeler"]  # values with spaces must be quoted

Only keys listed under ``filters`` are passed to the portal. All values are
converted to strings. Multi-select values for one filter iteration are joined
with ``|`` (the ``filter_batch`` separator).

You can also supply a list of lists to mean "these values together as one
multi-select choice":

  stateName:
    - [DL, GA]          # → "DL|GA"  (both selected together in one combo)
    - MH                # → "MH"

This generates 2 combos for stateName, not 2 separate single-state combos.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import Any

# Ensure parent directory and current directory are in sys.path
_current_dir = Path(__file__).resolve().parent
_parent_dir = _current_dir.parent
for _p in (str(_parent_dir), str(_current_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml

try:
    from parivahan_extractor.filter_batch import FILTER_IDS, LABEL_COLUMN
except ModuleNotFoundError:
    from filter_batch import FILTER_IDS, LABEL_COLUMN

# ---------------------------------------------------------------------------
# Default config values
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict[str, Any] = {
    "delay_seconds": 3,
    "max_retries": 4,
    "captcha_max_attempts": 5,
    "export_formats": ["parquet", "csv"],
    "headless": False,
    "filters": {},
}

SUPPORTED_EXPORT_FORMATS = {"parquet", "csv", "excel"}

Y_AXIS_OPTIONS: dict[str, str] = {
    "vehicleCategoryDescription": "Vehicle Category",
    "vehicleCategoryGroup": "Vehicle Category Group",
    "vehicleClass": "Vehicle Class",
    "vehicleType": "Vehicle Type",
    "vehiclePollutionNorm": "Norms",
    "vehicleFuel": "Fuel",
    "vehicleMakerName": "Maker",
    "stateCode": "State Wise",
    "rtoName": "RTO Wise",
    "vehicleManufecturingYear": "Manufacturing Year",
    "financialYear": "Financial Year",
    "calendarYear": "Calendar Year",
    "monthWise": "Month Wise",
}

VALID_X_AXES_BY_Y: dict[str, list[str]] = {
    "vehicleCategoryDescription": [
        "Total Consolidated", "vehicleClass", "vehicleType", "vehiclePollutionNorm",
        "vehicleFuel", "vehicleOwnerType", "stateCode", "rtoName",
        "vehicleManufecturingYear", "financialYear", "calendarYear", "monthWise",
    ],
    "vehicleCategoryGroup": [
        "Total Consolidated", "vehicleClass", "vehicleType", "vehiclePollutionNorm",
        "vehicleFuel", "vehicleOwnerType", "stateCode", "rtoName",
        "vehicleManufecturingYear", "financialYear", "calendarYear", "monthWise",
    ],
    "vehicleClass": [
        "Total Consolidated", "vehicleCategoryDescription", "vehiclePollutionNorm",
        "vehicleType", "vehicleFuel", "vehicleOwnerType", "vehicleCategoryGroup",
        "stateCode", "rtoName", "vehicleManufecturingYear", "financialYear",
        "calendarYear", "monthWise",
    ],
    "vehicleType": [
        "Total Consolidated", "vehicleCategoryDescription", "vehiclePollutionNorm",
        "vehicleClass", "vehicleFuel", "vehicleOwnerType", "vehicleCategoryGroup",
        "stateCode", "rtoName", "vehicleManufecturingYear", "financialYear",
        "calendarYear", "monthWise",
    ],
    "vehiclePollutionNorm": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehicleFuel", "vehicleOwnerType", "vehicleCategoryGroup",
        "stateCode", "rtoName", "vehicleManufecturingYear", "financialYear",
        "calendarYear", "monthWise",
    ],
    "vehicleFuel": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehicleOwnerType", "vehiclePollutionNorm",
        "vehicleCategoryGroup", "stateCode", "rtoName",
        "vehicleManufecturingYear", "financialYear", "calendarYear", "monthWise",
    ],
    "vehicleMakerName": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehiclePollutionNorm", "vehicleFuel", "vehicleOwnerType",
        "vehicleCategoryGroup", "stateCode", "rtoName",
        "vehicleManufecturingYear", "financialYear", "calendarYear", "monthWise",
    ],
    "stateCode": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehiclePollutionNorm", "vehicleFuel", "vehicleOwnerType",
        "vehicleCategoryGroup", "rtoName", "vehicleManufecturingYear",
        "financialYear", "calendarYear", "monthWise",
    ],
    "rtoName": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehiclePollutionNorm", "vehicleFuel", "vehicleOwnerType",
        "vehicleCategoryGroup", "stateCode", "vehicleManufecturingYear",
        "financialYear", "calendarYear", "monthWise",
    ],
    "vehicleManufecturingYear": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehiclePollutionNorm", "vehicleFuel", "vehicleOwnerType",
        "vehicleCategoryGroup", "stateCode", "rtoName", "financialYear",
        "calendarYear", "monthWise",
    ],
    "financialYear": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleCategoryGroup",
        "vehicleClass", "vehicleType", "vehicleFuel", "stateCode", "monthWise",
    ],
    "calendarYear": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehiclePollutionNorm", "vehicleFuel", "vehicleOwnerType",
        "vehicleCategoryGroup", "stateCode", "rtoName",
        "vehicleManufecturingYear", "financialYear", "monthWise",
    ],
    "monthWise": [
        "Total Consolidated", "vehicleCategoryDescription", "vehicleClass",
        "vehicleType", "vehiclePollutionNorm", "vehicleFuel", "vehicleOwnerType",
        "vehicleCategoryGroup", "stateCode", "rtoName",
        "vehicleManufecturingYear", "financialYear", "calendarYear",
    ],
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _normalise_value(value: Any) -> str:
    """Convert a single scalar or a list to the portal ``|``-separated string."""
    if isinstance(value, list):
        return "|".join(str(v).strip() for v in value)
    return str(value).strip()


def _normalise_filter_values(raw: Any) -> list[str]:
    """Convert a filter's raw YAML value to a list of combo-iteration values.

    Scalars → [str]
    Lists where each element is a scalar → [str, str, …]      (each is one combo value)
    Lists where an element is itself a list → handled as multi-select joint choice
    """
    if not isinstance(raw, list):
        return [_normalise_value(raw)]

    result: list[str] = []
    for item in raw:
        result.append(_normalise_value(item))
    return result


def _validate_filter_ids(filters: dict[str, Any]) -> None:
    unknown = set(filters) - FILTER_IDS
    if unknown:
        raise ValueError(
            "Unknown filter ID(s) in config: "
            + ", ".join(sorted(unknown))
            + f"\nSupported IDs: {sorted(FILTER_IDS)}"
        )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(source: str | Path | dict) -> dict[str, Any]:
    """Load and validate the crawler config.

    Parameters
    ----------
    source:
        Path to a YAML file, a raw YAML string, or an already-parsed dict.

    Returns
    -------
    dict
        Merged config (defaults + user values).
    """
    if isinstance(source, dict):
        raw = source
    elif isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and Path(source).suffix in {".yaml", ".yml"}):
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    else:
        raw = yaml.safe_load(source) or {}

    config = {**DEFAULT_CONFIG, **raw}

    # Validate export formats
    bad_formats = set(config.get("export_formats", [])) - SUPPORTED_EXPORT_FORMATS
    if bad_formats:
        raise ValueError(
            f"Unsupported export_formats: {bad_formats}. "
            f"Choose from: {SUPPORTED_EXPORT_FORMATS}"
        )

    # Validate filter IDs
    filters = config.get("filters", {})
    if not isinstance(filters, dict):
        raise ValueError("'filters' must be a YAML mapping of filter_id → values.")
    _validate_filter_ids(filters)
    discover_x_axes = config.get("discover_x_axes", False)
    if not isinstance(discover_x_axes, bool):
        raise ValueError("'discover_x_axes' must be true or false.")
    if discover_x_axes and "yAxis" not in filters:
        raise ValueError("'filters.yAxis' is required when 'discover_x_axes' is enabled.")
    if "axis_combinations" in config:
        if "yAxis" in filters or "xAxis" in filters:
            raise ValueError(
                "'axis_combinations' cannot be combined with yAxis or xAxis filters."
            )
        combinations = config["axis_combinations"]
        if not isinstance(combinations, list) or any(
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(axis, str) for axis in pair)
            for pair in combinations
        ):
            raise ValueError("'axis_combinations' must be a list of [yAxis, xAxis] pairs.")
        config["axis_combinations"] = [tuple(pair) for pair in combinations]
        validate_combinations(config["axis_combinations"], VALID_X_AXES_BY_Y)

    config["filters"] = filters
    return config



def validate_combinations(
    combinations: list[tuple[str, str]],
    valid_axes: dict[str, list[str]],
) -> None:
    for y_axis, x_axis in combinations:
        if y_axis not in valid_axes:
            raise ValueError(f"Unknown Y-axis: {y_axis}")
        if x_axis not in valid_axes[y_axis]:
            raise ValueError(
                f"Invalid X-axis {x_axis!r} for Y-axis {y_axis!r}"
            )

def get_valid_x_axes(y_axis: str, state_count: int = 1) -> list[str]:
    """Return the list of valid X-axis options for a given Y-axis."""
    axes = list(VALID_X_AXES_BY_Y.get(y_axis, []))
    if state_count != 1 and "rtoName" in axes:
        # On the portal, RTO-Wise is only active when exactly one State is selected
        axes.remove("rtoName")
    return axes


def is_valid_axis_combination(y_axis: str, x_axis: str, state_count: int = 1) -> bool:
    """Return True if (y_axis, x_axis) is an allowed combination on the portal."""
    if not y_axis or not x_axis:
        return True
    valid_x = get_valid_x_axes(y_axis, state_count=state_count)
    return x_axis in valid_x


# ---------------------------------------------------------------------------
# Combo generation
# ---------------------------------------------------------------------------

def generate_combos(
    config: dict[str, Any],
    *,
    discovered_axis_combinations: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Generate all filter combination dicts from *config*.

    Ensures that when both ``yAxis`` and ``xAxis`` are provided, X-axis
    options strictly depend on the chosen Y-axis (invalid combinations are
    filtered out). Also supports ``xAxis: auto`` or ``xAxis: all_valid`` to
    automatically generate all valid X-axes for each selected Y-axis.

    Returns a list of dicts that can be passed directly to
    ``apply_filter_row(page, combo)``.  Each dict also has a
    ``"label"`` key describing the combination.
    """
    filters: dict[str, Any] = config.get("filters", {})
    axis_combinations = (
        discovered_axis_combinations
        if discovered_axis_combinations is not None
        else config.get("axis_combinations")
    )
    if not filters and not axis_combinations:
        return []

    if axis_combinations is not None:
        if "yAxis" in filters or "xAxis" in filters:
            raise ValueError(
                "'axis_combinations' cannot be combined with yAxis or xAxis filters."
            )
        if discovered_axis_combinations is None:
            validate_combinations(axis_combinations, VALID_X_AXES_BY_Y)
        other_filter_ids = list(filters)
        other_values = [_normalise_filter_values(filters[fid]) for fid in other_filter_ids]
        combinations = list(axis_combinations)
        combos: list[dict[str, str]] = []
        for y_axis, x_axis in combinations:
            other_products = itertools.product(*other_values) if other_values else [()]
            for other_values_for_combo in other_products:
                row = dict(zip(other_filter_ids, other_values_for_combo))
                row["yAxis"] = y_axis
                row["xAxis"] = x_axis
                row[LABEL_COLUMN] = ", ".join(
                    f"{key}={value}" for key, value in sorted(row.items())
                )
                combos.append(row)
        return combos

    has_y = "yAxis" in filters
    has_x = "xAxis" in filters
    raw_x = filters.get("xAxis")
    x_is_auto = (
        isinstance(raw_x, str) and raw_x.strip().lower() in {"auto", "all_valid", "*"}
    ) or (
        isinstance(raw_x, list)
        and len(raw_x) == 1
        and str(raw_x[0]).strip().lower() in {"auto", "all_valid", "*"}
    )

    if has_y and x_is_auto:
        # Dynamically generate combinations per Y-axis
        other_filter_ids = [fid for fid in filters if fid not in {"yAxis", "xAxis"}]
        other_values = [_normalise_filter_values(filters[fid]) for fid in other_filter_ids]
        y_values = _normalise_filter_values(filters["yAxis"])

        combos: list[dict[str, str]] = []
        if other_filter_ids:
            other_products = list(itertools.product(*other_values))
        else:
            other_products = [()]

        for y in y_values:
            valid_x_list = get_valid_x_axes(y)
            for x in valid_x_list:
                for o_tuple in other_products:
                    row: dict[str, str] = dict(zip(other_filter_ids, o_tuple))
                    row["yAxis"] = y
                    row["xAxis"] = x
                    # Build human-readable label
                    row[LABEL_COLUMN] = ", ".join(f"{k}={v}" for k, v in sorted(row.items()))
                    combos.append(row)
        return combos

    filter_ids = list(filters.keys())
    value_lists: list[list[str]] = [
        _normalise_filter_values(filters[fid]) for fid in filter_ids
    ]

    combos = []
    for combo_values in itertools.product(*value_lists):
        row = dict(zip(filter_ids, combo_values))

        # Check Y / X axis compatibility if both are present
        if has_y and has_x:
            y_val = row.get("yAxis", "")
            x_val = row.get("xAxis", "")
            if y_val and x_val and not is_valid_axis_combination(y_val, x_val):
                continue  # Filter out incompatible X-for-Y pairs

        # Build a human-readable label
        row[LABEL_COLUMN] = ", ".join(
            f"{fid}={val}" for fid, val in zip(filter_ids, combo_values)
        )
        combos.append(row)

    return combos


def combo_count(config: dict[str, Any]) -> int:
    """Return the number of combinations."""
    filters = config.get("filters", {})
    if config.get("axis_combinations") is not None:
        return len(generate_combos(config))
    if not filters:
        return 1

    has_y = "yAxis" in filters
    has_x = "xAxis" in filters
    raw_x = filters.get("xAxis")
    x_is_auto = (
        isinstance(raw_x, str) and raw_x.strip().lower() in {"auto", "all_valid", "*"}
    ) or (
        isinstance(raw_x, list)
        and len(raw_x) == 1
        and str(raw_x[0]).strip().lower() in {"auto", "all_valid", "*"}
    )

    # When X depends on Y, compute the exact valid count
    if has_y and (has_x or x_is_auto):
        return len(generate_combos(config))

    count = 1
    for raw in filters.values():
        count *= len(_normalise_filter_values(raw))
    return count


# ---------------------------------------------------------------------------
# Default YAML config template
# ---------------------------------------------------------------------------

DEFAULT_YAML_TEMPLATE = """\
# Parivahan Auto-Crawler Config
# ─────────────────────────────
# delay_seconds: seconds to wait between consecutive crawl requests
# max_retries:   retries on portal errors per combo
# captcha_max_attempts: max OCR retries before manual fallback
# export_formats: subset of [parquet, csv, excel]
# headless: false  → visible browser (default); true → headless (no window)
#
# filters: map of portal filter IDs to lists of values.
#   Use a list of scalars for multiple choices (Cartesian product).
#   Use a nested list for a joint multi-select choice, e.g.:
#     stateName:
#       - [DL, GA]   # DL and GA selected together
#       - MH         # MH alone

delay_seconds: 3
max_retries: 4
captcha_max_attempts: 5
export_formats:
  - parquet
  - csv

filters:
  reportYear:
    - 2024
    - 2025
  stateName:
    - DL
    - MH
    - GA
  vehicleFuel:
    - PETROL
    - DIESEL
    - ELECTRIC
  yAxis:
    - stateCode
  xAxis:
    - Total Consolidated
"""
