# VAHAN Public Report Parquet / CSV / Excel Exporter + Auto Crawler

A Streamlit app that exports public Parivahan VAHAN dashboard and report data
to **Parquet, CSV, and Excel**. It includes an **advanced auto-crawler** that
automatically solves image CAPTCHAs using **EasyOCR** (free, local, no API key)
and iterates over all configured filter combinations without manual input.

---

## Features

| Feature | Details |
|---|---|
| 🤖 Auto CAPTCHA solving | EasyOCR (local CPU inference, ~70–85 % accuracy on distorted text) |
| 🔁 Auto filter iteration | Cartesian product of filter values from a YAML config; optional live X-axis discovery |
| 📸 Auto Apply click | Finds and clicks the portal Apply / Generate Report button automatically |
| 📦 Auto-crawler export | Parquet only; manual capture retains CSV and Excel options |
| ♻️ Retry with back-off | Configurable retries per combo; exponential back-off on errors |
| 📊 Live progress UI | Success-only progress bar and current-combination status |
| 🛡️ Fresh-result check | Invalid CAPTCHA exits immediately; new result data must stay stable for 1.5 seconds before export |
| ⬇️ ZIP download | Download all crawl results as a single ZIP from the UI |
| 🖱️ Manual mode | Original manual capture still available in the Manual Capture tab |
| 📦 Filter batch | Original CSV batch assistant preserved in the Filter Batch tab |

---

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

> **First run note:** EasyOCR downloads its model weights (~100 MB) on the
> first CAPTCHA solve attempt. Ensure internet access for the first run.

---

## Run

```powershell
.\.venv\Scripts\python.exe -m streamlit run .\parivahan_extractor\app.py --server.address 127.0.0.1
```

Or double-click `run_app.bat` (Windows).

---

## Auto Crawler

### 1. Configure

Open the **🤖 Auto Crawler** tab in the app. The app reads
`crawler_config.yaml`; edit that file when changing non-axis crawler settings.

For EV reports with portal-verified axes, use live X-axis discovery:

```yaml
filters:
  fromYear: 2026
  toYear: 2026
  stateName:
    - DL
    - MH
    - GA
  vehicleFuel:
    - ELECTRIC
  yAxis:
    - financialYear
    - vehicleClass
  xAxis: auto

discover_x_axes: true
```

At crawl start, the app selects each configured Y-axis and reads its enabled
X-axis choices from the portal, then generates only those axis combinations.
During this live-discovery crawl, it changes only Y-Axis and X-Axis; the portal's
current date range and all other selected filters are left untouched. Use a
specific `xAxis` list instead of `auto` to limit the discovered choices. Valid
successful axis combinations are saved in each export manifest and skipped on
subsequent runs. Failed combinations are listed in the Auto Crawler controls and
saved to `output/failed_combinations.json`; use **Rerun Failed Only** to retry
just that saved list. A combination is removed from the list after it succeeds
or is otherwise completed in a later run.

The Auto Crawler writes Parquet outputs only. Its compact UI reads
`crawler_config.yaml` without displaying the YAML editor or a combination
preview; use the **Axis settings** expander to change selected axes.

For a fixed, manually specified Y-axis/X-axis list, add `axis_combinations` at
the top level and do not set `yAxis` or `xAxis` under `filters`:

```yaml
filters:
  vehicleFuel:
    - ELECTRIC
axis_combinations:
  - [financialYear, Total Consolidated]
  - [vehicleClass, financialYear]
```

Each pair is combined with the other filter values. When `vehicleFuel` is
`ELECTRIC`, every generated report is filtered to electric vehicles.

### Multi-select joint values

To select multiple values **together** in one combo (multi-select filter),
nest them as a list:

```yaml
stateName:
  - [DL, GA]    # DL and GA selected together
  - MH          # MH alone
```

This generates 2 combos for `stateName`, not 3.

### 2. Start

Click **🚀 Start Auto Crawl**. The crawler will:

1. Open the portal filters in the browser.
2. Detect and screenshot the CAPTCHA image.
3. Pre-process (grayscale → adaptive threshold → denoise → 2× upscale).
4. Run EasyOCR to extract the CAPTCHA text.
5. Type the code into the CAPTCHA input.
6. Click **Apply / Generate Report**.
7. Wait for results to load.
8. Capture all visible tables, charts, summary cards, and downloads.
9. Export to Parquet + CSV + Excel in `output/<timestamp>/`.
10. Repeat for the next combo after `delay_seconds`.

When `discover_x_axes: true` is enabled, the crawler first selects each Y-axis
listed in `filters.yAxis` and reads the X-axis values populated by the portal.
Only those live Y/X pairs are crawled. Successful combinations recorded in
output manifests are skipped on subsequent runs; older manifests are matched
by their saved filter description.

### 3. Monitor

The UI shows a live progress bar, per-combo status table, and a scrolling log.

### 4. Download

Click **📥 Download all results as ZIP** when the crawl finishes.

---

## Manual Capture

Open the **🖱️ Manual Capture** tab:

1. Click **📋 Open Report** or **📊 Open Dashboard**.
2. Set filters, solve CAPTCHA yourself, click Apply.
3. Choose export formats (Parquet / CSV / Excel).
4. Click **📸 Capture visible data**.

---

## Filter Batch Assistant

Open the **📦 Filter Batch** tab. Upload a CSV with one report setup per row.

Supported headers: portal select IDs (`stateName`, `vehicleFuel`, `reportYear`,
`yAxis`, etc.) plus text inputs `fromYear`, `toYear`, `fromDate`, `toDate`; an
optional `label` column is kept as the row description. Multi-select values use
`|` between exact portal option values: `DL|GA`.

Download the CSV template from the tab for the exact format.

---

## Supported Filter IDs

```
reportYear    reportMonth   fromYear      toYear
fromDate      toDate        stateName     rtoCode
vehicleEmission vehicleMaker vehicleCategoryGroup vehicleSubCategory
vehicleClass  vehicleFuel   evType        vehicleStatus
vehicleOwnerType vehicleType fitnessCheck  delhiNcr
yAxis         xAxis         archivedFlags reportType
financialYearSelect
```

---

## Output Structure

```
output/
  20250124_143021_123456/       ← one folder per run/combo
    visible_table_01.parquet
    visible_table_01.csv
    chart_01_registrations.parquet
    chart_01_registrations.csv
    report.xlsx                 ← all sheets in one workbook
    manifest.json               ← metadata: URL, filters, timestamps, row counts
  browser_profile/              ← persistent Chromium profile (keep private)
  downloads/                    ← portal CSV/Excel downloads
```

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s parivahan_extractor\tests -v
```

Tests cover:
- CAPTCHA solver preprocessing pipeline (`test_captcha_solver.py`)
- Filter combo generation and YAML config loading (`test_combo_generator.py`)
- Original extractor and filter batch logic

---

## CAPTCHA Accuracy & Tips

| Situation | Recommendation |
|---|---|
| OCR keeps failing | Increase `captcha_max_attempts` |
| Portal blocks IP | Increase `delay_seconds`, reduce combos per run |
| CAPTCHA is reCAPTCHA v2/v3 | Switch to a paid solver service (2captcha etc.) |
| Results are wrong | Use a portal CSV/Excel export and let the crawler capture the download |

---

## Legal / Terms of Use

Automating CAPTCHA solving may violate the portal's Terms of Service. Use this
tool only for data you are authorised to access, at access rates the portal
permits. The `delay_seconds` config key is provided for this purpose. CAPTCHA
images are never stored; only the recognised text is used temporarily.
