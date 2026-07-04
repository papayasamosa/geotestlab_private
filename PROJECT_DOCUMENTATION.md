# GeoTestLab — Project Documentation

> *Last reconciled against the app code: `geomatchapp - working v11`. The app's on-screen title currently carries a temporary "TEST " prefix ("TEST GeoTestLab") — remove before any production release.*

## 1. Project Overview

**GeoTestLab** is a Streamlit app for planning and evaluating **geo-tests** — regional marketing incrementality experiments where some geographic areas (e.g. UK regions/local authorities, or equivalent international geography levels) receive a media pause, a campaign launch, a regional media change, or another market-level marketing intervention, while other areas act as a control.

The app is built around **UK and international non-US markets**. See §1.1 "Intended market coverage" below.

The app helps a user:

- Select **balanced test and control regions**, using either **structural (demographic/population) matching** against the app's built-in geography workbook, or **KPI Pattern matching**, which matches regions on the shape of their own historical KPI trend when demographic data for the regions isn't available (e.g. custom TV/zip-code-derived regions with no built-in demographic profile).
- **Validate** whether a set of control regions is actually suitable for a geo-test, using historical KPI (Key Performance Indicator) data.
- **Compare** different control-selection approaches — structural matching vs. data-driven (LASSO / Elastic Net) selection — side by side.
- Support **two different moments in the test lifecycle**: designing a future test (before it runs) and evaluating a completed test (after it has run).
- **Estimate the impact** of a completed test using **Bayesian Time-Based Regression (TBR)**, a Bayesian counterfactual model.
- Provide **diagnostics** (structural balance, time-series fit, placebo tests, MCMC convergence, residual autocorrelation) so the user can judge whether the counterfactual — and therefore the uplift estimate — can be trusted.

### 1.1 Intended Market Coverage

GeoTestLab is designed for **UK and international non-US markets**. Which specific markets are available in a given deployment depends entirely on which market sheets exist in the built-in geography/demographic workbook (see §3.1) — the app itself is market-agnostic and will offer whatever sheets are present.

Typical geographies this kind of tool is built to support include, for example:

- **UK**: regions, local authorities, or other standard UK geography levels.
- **Germany**: Länder or other regional divisions.
- **Ireland**: counties or ITL (International Territorial Level) areas.
- **Sweden**: NUTS (Nomenclature of Territorial Units for Statistics) areas.
- **Italy**: regions.
- **Australia**: states/territories, or postcode-derived areas.
- **New Zealand**: regions.
- **Canada**: provinces.
- **Mexico**: regions.

**US markets are not part of the current intended coverage.** If US support (e.g. DMA-level geography) were ever added, it would be a distinct future extension requiring its own market sheet and geography-level definitions in the workbook — not something the current app or documentation should be read as already supporting.

### The business problem

Marketers and analysts often want to know: *"Did this media pause, campaign launch, or regional media change actually move the needle?"* The cleanest way to answer that with regional data is a **test vs. control** design: apply the change in some geographies, leave others alone, and compare what happened.

The core difficulty is that you never observe what the test region *would have done* without the intervention — you only observe the control regions. So the entire exercise hinges on **whether the chosen control regions are a credible stand-in ("counterfactual") for the test region**. If the control regions were already trending differently from the test region, or if their historical KPI didn't track the test region's KPI well, any uplift number you calculate is unreliable.

GeoTestLab exists to make that credibility check systematic:

1. It helps build **structurally similar** test/control groups (similar population, demographics, etc.).
2. It checks whether those groups are **statistically similar over time** using real KPI history (correlation, error metrics, autocorrelation, placebo tests).
3. It estimates uplift using a model (**Bayesian TBR**) that produces a full posterior distribution, not just a single number, so the user can see how uncertain the estimate is.
4. It surfaces enough diagnostics that a careful user can decide **not** to trust a result if the underlying match or model fit is weak.

The app does not claim to *prove* causality — it is a toolkit to make an evidence-based, transparent judgement call. **Causal interpretation of an uplift estimate is only supportable when the test design is credible, treatment/control contamination has been avoided, and the pre-period relationship between test and control regions is likely to have continued through the test period had the intervention not happened.** If any of those conditions is doubtful, the resulting uplift should be reported as indicative, not causal.

---

## 2. High-Level Workflow

The current app is organised into four main workflow areas: **Region Matching**, **Validate Test Design**, **Measure Test Impact**, and **Bayesian TBR** (at the time of writing these correspond to Tabs 1–4 in the UI, but the workflow below is described by area rather than by tab number so it stays accurate even if the tab layout changes).

The intended end-to-end flow, in plain English:

1. **Choose the matching method** (sidebar): **Structural** (match on the built-in demographic/population workbook) or **KPI Pattern** (match on the shape of each region's own historical KPI trend, using an uploaded aggregated KPI file instead of the workbook).
2. **Choose the market and geography level** (Structural mode, sidebar). E.g. "UK" at "Region" level, or "Germany" at "Länder" level. In KPI Pattern mode, the "geography level" is instead whichever aggregation-level column the user picks from their uploaded file (see §3.7), and there is no separate market selector.
3. **Select or build the test group** — either manually pick regions, pick test regions and let the app find controls, or set inclusion/exclusion rules and let the app search for a test group and matched controls (Region Matching area).
4. **Generate matched control regions** using one of three matching strategies (greedy, hill-climbing, or stochastic search), based on population-weighted structural similarity (Structural mode) or weekly-KPI-shape similarity (KPI Pattern mode) — the same three strategies and the same underlying distance/optimisation machinery are used in both modes.
5. **Upload historical KPI data** (an Excel export of weekly or daily KPI values by region) for time-series validation, and confirm the **time-series frequency** (Weekly or Daily — see §F2); the app infers the likely frequency from the date gaps and warns (and blocks the run until acknowledged) if the selection doesn't match the data. In KPI Pattern mode, if the uploaded file's columns match the ones already chosen in step 1, the aggregation-level and metric columns are carried over automatically rather than asked for again.
6. **Validate test/control fit in the pre-period** — before drawing any conclusions, check that the test aggregate and candidate controls actually tracked each other historically (Validate Test Design area, or Measure Test Impact area).
7. **Compare control-selection methods** — Structural matching vs. LASSO/Elastic Net "data-optimised" selection vs. user-selected controls — side by side on the same metrics (Method Comparison table).
8. **Evaluate a completed test**, if test-period data is available, by defining pre/test(/post) date windows and reviewing uplift, error metrics, and placebo results (Measure Test Impact area).
9. **Run Bayesian TBR** for a full probabilistic impact estimate with credible/predictive intervals (Bayesian TBR area).
10. **Review diagnostics** (structural balance, rolling-origin errors, Durbin-Watson, placebo results, MCMC convergence) before trusting and reporting the result.

### Why the order matters

- **Structural matching first** narrows down *plausible* controls based on things you'd expect to matter (population, demographics) — but similarity in these static features doesn't guarantee the regions actually move together over time.
- **Time-series validation second** checks the thing that actually matters for a counterfactual: did the candidate controls historically track the test region's KPI? A structurally similar region can still have unrelated week-to-week KPI movements (different local dynamics, media mix, seasonality).
- **Bayesian TBR third** turns the validated relationship into an actual counterfactual estimate and quantifies uncertainty.
- **Diagnostics last (and throughout)** stop the user from over-interpreting a result that looks impressive but rests on a shaky fit — good pre-period fit doesn't guarantee a good post-period counterfactual, and an apparent uplift can still be noise if the model or match is weak.

---

## 3. Data Inputs

### 3.1 Built-in geography/demographic workbook

Path: `DATA_PATH = "data/Population Stats for Geo Tests - Master Sheet Only v2 (Standardised).xlsx"`

- The workbook contains **one sheet per market** (e.g. "UK", "Ireland", "Germany"). Sheet names are read with `get_workbook_sheet_names()` and offered as the **Market** selector.
- Each market sheet is loaded with `load_market_sheet()`, which:
  - Reads all columns as strings first (for robust cleaning), replaces common Excel error strings (`#N/A`, `#DIV/0!`, etc.) with missing values, strips/cleans text, and drops fully-empty rows/columns.
  - Adds a `Market` column tagging every row with the sheet name.

> ℹ️ **The list of markets in the UI is not hardcoded.** It is read directly from the workbook's sheet names at runtime, so it will change if sheets are added, removed, or renamed. Documentation examples (including the illustrative geographies listed in §1.1) should not be treated as an exhaustive or guaranteed list of currently supported markets — always check the live Market dropdown, or the workbook itself, for the actual current list.

### 3.2 Geography columns and grouping

- `get_base_geography_column()` — the first non-`Market` column is assumed to be the finest-grain geography identifier.
- `get_grouping_columns()` — all columns *before* the population column (excluding `Market` and any Adobe-reference-style column) are treated as candidate **geography levels** the user can group by (e.g. "Postcode", "Region", "Local Authority" — whichever hierarchy the workbook encodes). These become the **Geography Level** dropdown.
- `ADOBE_COL = "Adobe Reference List"` — an optional column that maps the base geography to a reference name used in uploaded KPI files (see §3.5).

### 3.3 Population column

- `POPULATION_COL_RAW = "Total Population"`, standardised internally to `POPULATION_COL = "Population"` (`get_population_column()`, `standardise_population_column()`).
- Population is used to:
  - Weight structural feature averages when aggregating to a chosen geography level (`weighted_average_vectorized`, `aggregate_market_data`).
  - Weight test/control structural profiles in matching (`weighted_profile`).
  - Compute "experiment population coverage" (% of total market population in the test group).
  - Rows with missing or non-positive population are dropped (`prepare_market_dataframe`).

### 3.4 Numeric matching features

- `get_numeric_metric_columns()` auto-detects columns that are (a) not the geography/population/Adobe columns, (b) not obviously categorical (name/keyword heuristics like "region", "county", "code"), and (c) numeric or mostly-numeric. These become the pool of **structural matching features** (demographics, density, etc.) available for weighting in Tab 1.

### 3.5 Adobe reference mapping

- Some organisations' KPI exports use a different naming convention than the geography workbook (e.g. an "Adobe Analytics" region code). The `Adobe Reference List` column, where present, is used to build a mapping (`adobe_to_geo`) from the raw KPI region label to the app's internal geography name, via `build_region_mapping()`.
- If a raw region label isn't in the Adobe mapping but does directly match a known test/control region name, it is used as-is; otherwise it is dropped (mapped to `None`).

### 3.6 Uploaded KPI Excel file

Handled by `load_and_reshape_kpi()`. Expected shape (wide format):

| Region / Adobe Reference | Metric | 2024-01-01 | 2024-01-08 | ... |
|---|---|---|---|---|
| London | Sales | 1234 | 1301 | ... |
| Manchester | Sales | 980 | 1010 | ... |

- **First column**: region name or Adobe reference (whatever identifier the export uses).
- **Second column**: metric/KPI name (e.g. "Sales", "Visits").
- **Remaining columns**: one column per date/period, parsed as dates (`pd.to_datetime(..., errors="coerce")`).
- **Cell values**: the KPI observation for that region/metric/date.

The app reshapes this from wide to long format (`melt`), coerces KPI values to numeric, and **drops rows with an invalid date or missing/non-numeric KPI** (it does not fill missing values with 0 — missing data is dropped, not treated as zero activity). Region names are then mapped to the selected geography level and the KPI values are aggregated by summing across any raw regions that map to the same geography (`apply_geo_aggregation`).

> ⚠️ **Watch for**: if the "Metric" column contains multiple different KPIs mixed together, the user is expected to have already filtered to a single metric before upload, or to select the metric of interest downstream — check the current UI (`selected_metric` in the validation tabs) for how metric selection is actually exposed at the point you're reading this, as the code may filter after upload rather than requiring pre-filtering.

`load_and_reshape_kpi()` also supports a second, **aggregated** file layout, auto-detected by column count: if the file has more than two non-date leading columns, column 0 is treated as a raw key that isn't itself used for matching (e.g. postcode), one or more middle columns are candidate **aggregation-level** columns (e.g. "TV Market", "TV Region"), and one column is the metric name. In this case the caller must resolve which column is the region and which is the metric (`agg_col` / `metric_col` — normally chosen via a selectbox pair in the UI) and pass them in; `load_and_reshape_kpi()` raises `ValueError` if this case is detected but the columns weren't supplied. This is the same aggregated layout used by the KPI Pattern sidebar upload (§3.7) — see §F for how the Validate Test Design tab carries over the aggregation-level/metric-column choice automatically when the app is in KPI Pattern mode.

### 3.7 KPI Pattern mode: sidebar setup file

When **KPI Pattern** is selected as the matching method (see §4.A2), the sidebar's "1. Data Source" section replaces the Market/Geography Level selectors from Structural mode with a single file upload, parsed by `read_kpi_pattern_excel()` (cached on the file's raw bytes, so the workbook is only parsed once per upload regardless of how many times Streamlit reruns the script) — not `load_and_reshape_kpi()`. Expected shape is the same **aggregated** wide format described in §3.6 (raw key column, one or more aggregation-level columns, a `Metric` column, then date columns), but here it is used to build the region-level dataset that KPI Pattern matching runs on, not to validate an already-matched pair:

- The user picks which column is the **aggregation level** (this becomes `geo_col`, replacing "Geography Level") and which value in the `Metric` column to use.
- The user picks a **pre-period date range** via two side-by-side Start date / End date dropdowns (`dd mmm yy` labels) — this should be the historical window used for matching, excluding any dates inside the planned test period.
- Rows with a blank aggregation-level cell are dropped **before** aggregating, so unmapped/unclassified raw keys never silently inflate another region's total.
- Values are summed by aggregation level and date, then **each region is indexed to its own mean over the selected range = 100** — this is what makes "distance between regions" comparable for regions with very different raw KPI volume; matching compares the *shape* of each region's trend, not its absolute size.
- The resulting per-region weekly features are named `wk_YYYYMMDD` (one per date in range) and become `active_features` for matching, exactly as demographic columns would in Structural mode.
- `POPULATION_COL` ("Population") is **repurposed** in this mode to hold each region's **total (un-indexed) KPI volume** over the selected range, not a real population figure — this is what "Test/Control Population Share" measures throughout the matching UI in KPI Pattern mode. User-facing labels are adjusted automatically wherever they'd otherwise say "population" (`kpi_share_label()`), and table columns that would otherwise show `wk_YYYYMMDD` values or a "Population" header are instead displayed as `dd mmm yy` dates and the chosen metric label respectively (`kpi_feature_date_label()`, `kpi_pattern_display_rename_map()`) — see §4.A2.

---

## 4. Main App Sections / Tabs

The app has four tabs plus a sidebar:

- **Sidebar**: Matching Method toggle (Structural vs. KPI Pattern, §A2), then Market and Geography Level selection (Structural) or KPI Pattern file upload/selections (§3.7), plus a Data Quality Check footer.
- **Tab 1 — ⚙️ Region Matching**: build test/control groups.
- **Tab 2 — 🔍 Validate Test Design**: pre-launch validation using historical KPI data (no test period yet).
- **Tab 3 — 📊 Measure Test Impact**: evaluate a completed test using pre/test(/post) KPI data.
- **Tab 4 — 🧠 Bayesian TBR**: Bayesian counterfactual impact estimation.

### A. Geography and Market Selection (sidebar)

- **Market selection**: chooses which workbook sheet (country/market) to load.
- **Geography level selection**: chooses which column to group and aggregate by (e.g. Region vs. a finer level). Options differ **per market** because `get_grouping_columns()` reads the columns actually present in that market's sheet — not every market necessarily has the same geography hierarchy defined. For example, a UK sheet might offer "Region" and "Local Authority" levels, while a Germany sheet might offer "Land" and a finer administrative level instead — the app doesn't assume any single hierarchy, it reflects whatever the workbook provides for that market.
- **Why granularity matters**: a coarser level (e.g. "Region") gives fewer, larger candidate geographies — easier to reach a target population share with few units, but less flexibility in matching. A finer level (e.g. postcode or local-authority level) gives many small units — more matching flexibility, but each unit's KPI is noisier and structural aggregation (population-weighted averaging) becomes more important.
- **Population and structural features**: population weights the aggregation of demographic/structural features up to the chosen geography level, and weights the structural-distance calculation in matching, so that large, representative sub-areas count for more than small, atypical ones.

> These three points describe **Structural** mode. §A2 below covers what changes in **KPI Pattern** mode.

### A2. Matching Method: Structural vs. KPI Pattern

A sidebar radio ("Matching Method", `matching_method_sidebar`) chooses between the two matching approaches; the choice is stored in `st.session_state["kpi_pattern_mode"]` and is read throughout Tab 1 (and by the Validate Test Design carry-over logic in §F) to decide which code path and which UI elements to show.

| | **Structural** | **KPI Pattern** |
|---|---|---|
| Matches on | Demographic/population profile from the built-in geography workbook (§3.1) | The shape of each region's own historical KPI trend, from an uploaded aggregated KPI file (§3.7) |
| Sidebar "1." section | Market + Geography Level selectors | Data Source file upload + aggregation-level, metric, and pre-period date-range selectors |
| Matching features (`active_features`) | Numeric demographic columns (`get_numeric_metric_columns()`) | One feature per date in the selected range (`wk_YYYYMMDD`), each region indexed to its own mean = 100 |
| "4. Matching Feature Importance" section | Shown — per-feature weight sliders, "Reset All Weights to 1" / "Reset Slider Positions" buttons | **Hidden.** Every week is weighted equally (`weights = {f: 1 for f in active_features}`) — there's no meaningful notion of "important weeks" the way there is for demographic weighting, so no slider UI is shown |
| `POPULATION_COL` meaning | Actual population | Repurposed to hold each region's total (un-indexed) KPI volume over the selected range (§3.7) — UI labels are adjusted accordingly (`kpi_share_label()`) |
| Preview data / Feature Comparison tables | Raw feature names and values | `wk_YYYYMMDD` columns displayed as `dd mmm yy` dates; the `POPULATION_COL` header is displayed as the chosen metric's label instead of "Population" (`kpi_pattern_display_rename_map()`, `kpi_feature_date_label()`) |
| Export to Excel | `Market`, `Adobe Reference List`, aggregation-level column, Test/Control Geography — built from the workbook's Adobe reference sheet | Aggregation-level column, Test Geography, Control Geography — built directly from the uploaded file's aggregation-level values (there is no Adobe reference sheet in this mode) |

Matching strategies and diagnostics are otherwise identical between the two modes: the same three strategies (§B) and the same `calculate_metrics()` / Weighted Structural Distance / Mean Abs SMD machinery (§E) apply in both — only the feature set and the weighting UI differ.

**Why this mode exists**: some clients define custom geographies (e.g. TV-market or postcode-derived areas) that have no corresponding row in the built-in demographic workbook, so structural matching isn't possible. KPI Pattern mode sidesteps that by matching purely on historical KPI behaviour — if two regions' KPI has moved together in the past, they're treated as good candidates for a test/control pairing, independent of whether any demographic data exists for them.

**Caveat**: because there is no independent structural check in this mode, a good KPI-shape match is the *only* screening step before time-series validation (§F) — there's no "coarse plausibility filter" the way structural balance acts as one in Structural mode. Time-series validation (pre-period fit, rolling-origin, placebo) is therefore even more important to check carefully before trusting a KPI Pattern-selected control group.

### B. Matching Strategy (`stochastic_genetic_search`, greedy/hill-climbing code in the Tab 1 matching loop)

Three strategies, selectable per the `strategy_labels` mapping:

| UI label | Internal name | What it does |
|---|---|---|
| Basic (Fast) | Greedy (Nearest Neighbor) | For each candidate control-group size `n`, take the `n` nearest control regions to the test group's structural profile (via `NearestNeighbors`) and stop. |
| Intermediate (Balanced) | Refined Greedy (Hill Climbing) | Starts from a nearest-neighbour candidate group, then repeatedly tries swapping one selected region for one of a small set of nearby unselected regions, keeping any swap that improves Weighted Structural Distance, until no improving swap is found (bounded by `CONFIG["max_hill_climbing_swaps"]` and a small per-step swap budget). |
| Advanced (Thorough) | Stochastic (Genetic Search) | Starts from the same nearest-neighbour candidate group, then performs many random single-region swaps (`stochastic_genetic_search`), accepting any swap that improves the score, over a configurable number of iterations (`genetic_iterations`, default 1,000). Uses a fixed random seed (42) for reproducibility. |

- **Strategy Parameters (sidebar "3.")**: a **Force 1-to-1 Match Ratio** checkbox (when enabled, the control group size is fixed to the number of test regions and the pool-size search is skipped), and — when 1-to-1 is off — a **control group pool size range slider** (the algorithm tests every size in the range and keeps the best-balanced one; the searchable pool is capped at `CONFIG["max_control_pool_size"]`, currently 50). For the Advanced strategy, a **Search iterations** slider (min/max/default from `CONFIG["genetic_iterations"]`: 100 / 5,000 / 1,000).
- **Results views**: matching results are shown behind a "Select View" radio with three panes — **📊 Summary**, **📈 Diagnostics** (Pool Size Optimization chart, Search Convergence chart, Feature Distribution Detail), and **💾 Export** (the Export to Excel flow described in §A2).
- **Staleness handling**: `matching_setup_changed_since_last_run()` compares the current market/geography level/strategy/test regions/weights against a snapshot taken when results were last generated; if anything changed, results are flagged as stale (`st.session_state.match_results_stale`) and the user is warned rather than the app silently recomputing or showing mismatched results. Downstream Bayesian results are also invalidated when the region set changes (see `reset_results` / `reset_manual_results`).
- **When useful**: Basic is fastest and fine for a quick look or when the pool is small. Intermediate is a good default — bounded, fast local refinement. Advanced explores more of the combination space and can escape local optima that hill climbing gets stuck in, at the cost of more computation.
- **Trade-off**: more search time does not always translate into a meaningfully better real-world match — Weighted Structural Distance can plateau once "good enough" balance is reached, and searching harder on structural balance alone says nothing about historical KPI tracking (which is checked separately in Tabs 2/3). Treat Advanced as a way to be thorough, not as a guarantee of a categorically better result.

### C. Test Region Selection

Three setup modes (`setup_mode` radio in Tab 1):

1. **Manual Selection (Pick Both)** — user picks both test and control regions directly by name; no automated matching or search runs.
2. **Pick Test, Auto-Match Controls** — user picks test regions manually; the app searches for the best-matched control group using the chosen matching strategy.
3. **Set Rules & Auto-Build Groups** — user specifies force-include/force-exclude regions and a **target test population share** (with a tolerance in percentage points); the app runs a **guided search** (`find_guided_test_group`) to find a test group that hits the population target while respecting the include/exclude rules, then matches controls to it.

- **Force-include / force-exclude**: in "Set Rules" mode, `force_exp_include` / `force_exp_exclude` constrain which regions can or cannot be part of the *test* group. There is a parallel concept for the *control* pool (`force_ctrl_include` / `force_ctrl_exclude`) used elsewhere in the app (see §D).
- **Experiment population coverage**: `calculate_experiment_population_coverage()` reports what % of the total market population the selected test regions represent. This is shown live as regions are picked.
- **Why size/representativeness matter**: a very small test group is cheap in terms of "regions used up" but its aggregate KPI will be noisier and less representative of the whole market; a very large test group leaves fewer regions available to serve as controls and may be harder to match well.

### D. Control Region Selection

- **Control pool**: for auto-matching modes, the control pool is every geography **not** in the test group (optionally minus force-excluded regions).
- **Force-excluded regions** (`force_ctrl_exclude`, surfaced later as `force_excluded_regions` in the validation tabs): regions the user knows are unsuitable as controls — e.g. because they are contaminated by a national campaign, are structurally unusual, or are otherwise off-limits. These can be excluded both from the structural matching pool and from the "Data-Optimised Controls (Excluding Force-Exclude Regions)" comparison method (§G).
- **User-selected vs. algorithm-selected controls**: in Manual Selection mode, controls are exactly what the user picked, with no optimisation. In auto-match modes, controls are chosen by the matching strategy to minimise Weighted Structural Distance. The difference matters when interpreting the Method Comparison table (§G) — "User Selected Test and Control" reflects pure human judgement, while "Structurally Matched Controls" and the data-optimised methods reflect an algorithm's choice under different objectives.
- **Why control pool size matters**: a larger pool gives the matching algorithm more options and generally allows a closer structural match, but also increases the risk of including a poorly-tracking or contaminated region if searched purely on structural grounds — hence the need for the time-series validation step.
- **Contamination**: a control region should not itself be receiving the treatment, a related treatment, or a national/cross-region effect that also touches the test regions — otherwise it's not a valid counterfactual.

### E. Structural Matching Diagnostics

Computed by `calculate_metrics()` on a fixed "eligible pool" basis (`fit_structural_stats`, computed once per run over test + full candidate pool, not refit per candidate group — this is what makes scores comparable across different candidate group sizes):

> The same function and formulas below are used in **KPI Pattern mode** too, just applied to weekly-indexed KPI features (`wk_YYYYMMDD`, all weights fixed at 1) instead of demographic features with user-set weights — "Weighted Structural Distance" and "Mean Abs SMD" mean the same thing structurally in both modes, they're just computed over a different feature set.

- **Weighted Structural Distance** (`weighted_structural_distance`): the **primary optimisation objective**. For each feature, the test group's population-weighted mean and the control group's population-weighted mean are each standardised (z-scored) against the eligible pool's mean/std, squared-differenced, multiplied by the user's slider weight for that feature, summed, and square-rooted. **Lower is better** — it is the Euclidean distance between test and control in weighted, standardised feature space.
- **Mean Abs SMD** (`mean_abs_smd`, "Standardised Mean Difference"): a diagnostic, **unweighted** balance metric — the mean of `|test mean − control mean| / eligible-pool std` across features. Not used for optimisation, but useful as a simple, weight-independent balance check. **Lower is better**; the app uses `SMD_GOOD_THRESHOLD = 0.20` and `SMD_HIGH_THRESHOLD = 0.50` as rough traffic-light thresholds (below 0.20 = good balance, above 0.50 = poor balance) — check the UI/thresholds code near `smd_thresholds` for exactly how these are displayed.
- **Feature balance / feature weights**: the user assigns a weight per matching feature (a slider per feature); a weight of 0 removes a feature from the objective entirely. Feature weights only affect Weighted Structural Distance, not Mean Abs SMD.
- **Population weighting**: both test and control structural profiles are population-weighted averages (`weighted_profile`), so large sub-geographies dominate the profile more than small ones (falls back to an unweighted mean if population data is missing or invalid).

**Why structural balance alone is not enough**: two regions can look similar on this year's population and demographic snapshot but have completely different underlying KPI dynamics (different competitive environment, different seasonality, different exposure to other marketing). Structural balance is a *necessary* screening step, not sufficient evidence of a valid counterfactual — that's what the time-series validation in Tabs 2/3 checks.

### F. KPI Upload and Time-Series Validation

- **Why historical KPI data is needed**: structural similarity is about static characteristics; the counterfactual argument depends on the test and control KPI series actually **moving together historically**. This can only be checked with real KPI history.
- **Building the model matrix** (`build_model_matrix`): sums the KPI across all test regions into a single `test_kpi` series by date, pivots the control regions' KPI into one column per region, and inner-joins on date (dropping any date with missing data in any control), producing one row per date with `test_kpi` as the target and each control region as a predictor column.
- **Pre-period validation**: fits a regularised linear model (Elastic Net or LASSO — see §G) on the pre-period only, and reports fit quality metrics plus rolling-origin and placebo diagnostics, *before* looking at any test-period uplift.
- **Design mode vs. Evaluation mode** (`render_time_series_validation(mode)`): the same underlying validation logic is shared by Tab 2 ("Design") and Tab 3 ("Evaluate"). In Design mode there is no real test period yet — the user is checking pre-period fit only, to decide whether a *future* test would be well-supported. In Evaluate mode, the user defines pre/test(/post) date windows for a test that has actually happened and reviews uplift alongside fit diagnostics.
- **Pre / test / post date windows**: pre-period = the historical window used to fit the model; test period = the window during which the treatment was live; post period = optional window after the test to check for lingering or rebound effects.
- **Selected KPI handling**: the user picks which uploaded metric (`selected_metric`) to validate/model.
- **Aggregation-level/metric column carry-over in KPI Pattern mode**: when the app is in KPI Pattern mode (§A2) and the file uploaded here has the same aggregation-level and metric column names already chosen in the Region Matching sidebar (§3.7), those two selections are carried over automatically instead of asking the user to pick them again — only the KPI-to-analyse selector is still shown, since that's a genuinely separate choice (the file may contain other metrics beyond the one used for matching). If the uploaded file's columns don't match what was used for matching, the app falls back to showing the normal column-selection UI with a warning. This also matters for correctness, not just convenience: previously requiring users to re-pick the aggregation column independently in this tab made it easy to pick a different granularity than was used for matching, which silently broke region-name matching downstream (`build_region_mapping`) and could surface as a `KeyError` when building the model matrix.
- **Missing data and date alignment**: `build_model_matrix` uses an inner join and drops rows with any missing value, so gaps in any single control region's data will remove that date from the *entire* model, not just that region — a control with sporadic missing history can quietly shrink the usable date range for everyone. `_warn_on_row_loss()` surfaces a warning when the built matrix has lost a meaningful share of dates this way, rather than letting the shrinkage happen silently.
- **Results display extras**: alongside the Method Comparison table, the validation results include a per-region **role table** (colour-coded Test / Control / Unused Candidate Region, per method) and a **"KPI Performance by Geography"** summary (per-region KPI totals and shares over the pre-period, with the period count and date range shown in frequency-aware units). The main actual-vs-predicted chart has a **⬇ Download chart data** button (`build_chart_data_xlsx`) exporting the plotted series to Excel.

### F2. Time-Series Frequency (Weekly vs. Daily)

The validation tabs (and everything downstream of them) are **frequency-aware**: a "Time series frequency" radio (Weekly / Daily, `time_series_frequency`, persisted in session state and clearing validation results on change) tells the app how to interpret the uploaded dates. `get_frequency_config()` centralises all frequency-dependent settings:

| | Weekly | Daily |
|---|---|---|
| Lag length for lagged controls (§H) | 1 period ("1-week") | 7 periods ("7-day" — compares the same day of week) |
| Default minimum training window | 13 periods | 84 periods |
| Default rolling-origin / placebo horizon | 4 periods | 28 periods |
| Period labels throughout the UI | "week(s)" | "day(s)" |

- **Frequency inference and mismatch guard**: `infer_time_series_frequency()` inspects the median gap between the uploaded dates and classifies the data as "daily", "weekly", or "unknown". It never silently overrides the user's selection — instead, if the inferred frequency contradicts the selected one, a warning is shown and the user must tick an **acknowledgment checkbox** before the validation run button is enabled. While unacknowledged, `st.session_state.frequency_mismatch_blocked` is set, which also **disables the Run button in the Bayesian TBR tab**, since a wrong frequency corrupts lag lengths and window sizing everywhere downstream.
- **Naming convention**: internally, window/period arguments are now named `*_periods` (`min_training_periods`, `placebo_length_periods`, etc.); the older `*_weeks` names (`min_training_weeks`, `placebo_length_weeks`) are retained as backward-compatible aliases (treated as period counts in the selected frequency) both as function arguments and in the returned result dicts.

### G. Method Comparison

Four candidate methods are compared side by side (`METHOD_STRUCTURAL`, `METHOD_DATA_OPTIMISED`, `METHOD_DATA_OPTIMISED_EXCL`, `METHOD_USER_SELECTED`):

| Method | Controls used | Model fit with |
|---|---|---|
| **Structurally Matched Controls** | The regions chosen by the Tab 1 matching strategy | Elastic Net (regularised, but on a small pre-selected set) |
| **Data-Optimised Controls** | All non-test regions in the market are offered as candidates | LASSO (hard variable selection — picks a subset via zero/non-zero coefficients) |
| **Data-Optimised Controls (Excluding Force-Exclude Regions)** | All non-test regions **except** any force-excluded regions | LASSO |
| **User Selected Test and Control** | Exactly the regions the user picked manually | Elastic Net |

> ⚠️ **Caveat on Data-Optimised Controls**: because the data-optimised methods select controls purely to fit the pre-period KPI series, they can achieve a strong-looking pre-period fit while selecting controls that are **less structurally plausible** than a human or structural match would choose (e.g. geographically or demographically dissimilar regions that simply happened to correlate historically). A good pre-period fit is not, by itself, a reason to prefer data-optimised controls over structurally matched ones. Before trusting or preferring a data-optimised result, check: contamination risk (is the selected control genuinely independent of the test regions and the intervention?), explainability (is there a plausible reason this control should track the test region, or is the relationship coincidental?), rolling-origin stability (does the fit hold up out-of-sample, not just in-sample?), residual diagnostics (is the fit free of concerning autocorrelation?), and placebo results (is the apparent uplift distinguishable from noise?).

Run via `run_validation_method()`. Key outputs and how to read them:

> ℹ️ **How the regularised models are built (`build_regularized_model` / `safe_tscv`)**: whenever there is enough pre-period history for safe, leakage-free time-series cross-validation, hyperparameters are chosen via **`ElasticNetCV` with `TimeSeriesSplit`**. If there isn't, the code deliberately does **not** fall back to regular K-fold CV (which leaks future information into past-hyperparameter choices for time-series data). Instead it fits a **fixed-alpha ElasticNet explicitly labelled as exploratory** — and those exploratory fits are **excluded** from the rolling-origin means and from Counterfactual Confidence (see §G2), with a visible warning (`_warn_on_cv_fallback`, `classify_validation_method`). A fixed alpha is an arbitrary modelling choice, not a substitute for CV, so exploratory results should never be compared like-for-like against cross-validated ones.

- **Control pool size / Controls selected**: how many candidate controls were available vs. how many ended up with a non-zero coefficient (i.e. were actually used by the model). For Elastic Net methods this can still be less than the full pool if coefficients shrink to (near) zero.
- **Pre-Period Correlation** (`corr`): correlation between actual and model-predicted pre-period KPI. *Higher is generally better*, but correlation alone can be misleadingly high even for a poor forecasting model — always read alongside error metrics.
- **Pre-Period R²** (`r2`): proportion of pre-period variance explained. *Higher is better*, but a high in-sample R² can mask overfitting or ignore autocorrelated errors — it is an in-sample, indicative metric, not a guarantee of out-of-sample accuracy.
- **Pre-Period sMAPE** (`smape`) / **Pre-Period RMSE** (`rmse`): symmetric mean absolute percentage error and root-mean-squared error of the pre-period fit. *Lower is better* for both; sMAPE is scale-free (useful for comparing across KPI magnitudes), RMSE is in the KPI's own units and penalises large errors more heavily.
- **Pre-Period Durbin-Watson** (`dw_stat`, via `durbin_watson_stat()`): tests whether pre-period residuals are autocorrelated. **~2.0** = little autocorrelation; **< 2** suggests positive autocorrelation (errors cluster); **> 2** suggests negative autocorrelation. Strong autocorrelation is a warning sign that the model is missing time-based structure (trend, delayed effects, seasonality) that a simple regression doesn't capture — the standard errors/intervals may then understate true uncertainty.
- **Rolling-origin / holdout sMAPE and RMSE** (`rolling_smape_mean`, `rolling_rmse_mean`, and the underlying `rolling_origin_folds` table from `rolling_origin_validation()`): out-of-sample accuracy, estimated by repeatedly training on an expanding window and forecasting a short horizon ahead (default horizon matches the placebo window length), starting only once `min_training_periods` of history is available (default 13 weekly / 84 daily periods; the legacy `min_training_weeks` name is kept as an alias). *Lower is better.* This is a more honest measure of forecast quality than the in-sample pre-period metrics above, because it never lets the model see the data it's being scored on.
- **Placebo windows** (`_run_placebo_windows()` + `_summarize_placebo_results()`, called from `run_validation_method`): the pre-period is repeatedly split into a "fake pre-period" and a "fake test window" of the same length as the real test window (or `placebo_length_periods` if set), a model is refit on the fake pre-period, and the "uplift" for the fake window is recorded — even though there was no real treatment. Doing this many times builds a distribution of uplift values that could occur purely from noise.
  - **Median placebo uplift**: the typical "fake" uplift — ideally close to zero.
  - **95% placebo uplift range**: the middle 95% of fake-uplift values; a real observed uplift that falls **inside** this range is not clearly distinguishable from noise.
  - **Placebo percentile rank**: where the real uplift falls within the distribution of placebo (fake) uplifts.
  - **Placebo p-value** (one-sided and two-sided, `p_one_sided` / `p_two_sided`): the proportion of placebo uplifts as extreme as (or more extreme than) the real uplift. This is an **approximate, non-parametric extremeness check** — a description of how unusual the real uplift looks against the placebo distribution — not a formal significance test in the classical statistical sense. It offers evidence consistent with an effect, not proof, and should not be reported as formal statistical significance.
  - **Placebo z-score**: how many placebo-uplift standard deviations the real uplift is from the placebo mean.
  - **Average placebo sMAPE / RMSE** (`median_placebo_smape` etc., from `_summarize_placebo_results`): forecast error inside the fake windows themselves — how accurately the model predicts held-out history when *nothing* happened. High placebo error means even the "no effect" baseline is noisy.
  - **Limitations**: with only a short pre-period, there may be too few placebo windows to form a reliable distribution. The code subsamples to at most **40 evenly-spaced windows** (`_run_placebo_windows(max_windows=40)`) to keep runtime bounded, which caps the resolution of any empirical p-value at roughly 1/40 (the smallest nonzero one-sided p-value achievable is ~0.025) — treat placebo p-values from small window counts with this precision limit in mind.

**Simple interpretation guide**:
- High correlation is useful but not sufficient on its own — always check error metrics and Durbin-Watson too.
- High R² suggests good in-sample fit but can hide time-series issues (autocorrelation, overfitting); it says nothing about forecasting accuracy.
- Lower sMAPE/RMSE (in either the pre-period or rolling-origin numbers) means lower prediction error — prefer rolling-origin numbers when judging real forecasting usefulness.
- Durbin-Watson near 2 suggests residuals are not strongly autocorrelated; values far from 2 are a flag to investigate, not necessarily a disqualifier.
- Placebo results show whether the observed uplift is unusual relative to the kind of "uplift" you'd see from pre-period noise alone — a small placebo range and a real uplift clearly outside it is stronger evidence that the observed uplift is unusual relative to pre-period noise.

### G2. Method Comparison Table Layout & Counterfactual Confidence

The Method Comparison table (rendered by `render_method_comparison_table()`, extracted as a self-contained function) is organised into lettered sections, one column per method:

- **A. Control Selection** — Control Pool Size, Controls Selected, Predictors Selected.
- **B. Pre-Period Fit** — Correlation, R², sMAPE.
- **C1. Rolling-Origin Validation – Error** — Validation sMAPE (%) plus a traffic-light **Validation Error Risk** rating.
- **C2. Rolling-Origin Validation – Bias** — Average Bias (%) plus a **Bias Risk** rating.
- **D. Overfitting Check** — the **Overfitting Gap** (rolling-origin sMAPE minus pre-period sMAPE, in percentage points, `calculate_overfit_gap`) plus an **Overfitting Risk** rating.
- **E. Residual Diagnostics** — Durbin-Watson plus an **Autocorrelation Risk** rating.
- **F. Placebo Testing** — window count, average placebo sMAPE, median placebo uplift, 95% placebo uplift range.
- **G. Counterfactual Confidence** — the overall rating (below).
- **Evaluate mode only**: **H. Observed Uplift vs Placebos** (percentile rank, two-sided p-value, z-score) and **I. Test Impact** (observed uplift, uplift %, test-group actual total, counterfactual total).

**Traffic-light classifiers** (all thresholds live in `CONFIG["reliability_thresholds"]`, the single source of truth):

- `classify_rolling_validation_error()` — 🟢 rolling sMAPE ≤ 10%, 🟡 ≤ 15%, 🔴 above; ⚪ if unavailable.
- `classify_rolling_bias_risk()` — 🟢 |bias| ≤ 5%, 🟡 ≤ 10%, 🔴 above.
- `classify_overfitting_risk()` — based only on the Overfitting Gap: 🟢 ≤ 3pp, 🟡 ≤ 5pp, 🔴 above.
- `classify_autocorrelation_risk()` — practical Durbin-Watson bands (not formal critical-value tests): 🟢 1.5–2.5, 🟡 1.2–<1.5 or >2.5–2.8, 🔴 outside those.

**Overall Counterfactual Confidence** (`combine_reliability_ratings()`) is a **priority-ordered cascade, not a worst-of-four vote**. Rolling Validation Error is the primary check and acts as a gate: if it's 🔴 the overall rating is "🔴 Low confidence" regardless of the rest; if it's ⚪ the overall rating is "⚪ Insufficient data"; if it's 🟡 confidence is capped at "🟡 Moderate confidence". Only when it's 🟢 do the secondary checks (Overfitting, Autocorrelation Risk, Rolling Bias — in that priority order) come into play, and a flagged secondary check can hold confidence back to moderate but can never force it all the way to low. `get_reliability_drivers()` produces the accompanying human-readable explanation (listing *all* flagged issues, not just the deciding one). Exploratory fixed-alpha fallback fits (see the CV note above) are excluded from this rating entirely.

> Internal names still say "reliability" for backward compatibility; all user-facing labels say "Counterfactual Confidence". Similarly, internal "overfit_gap" names surface as "Overfitting Gap".

### H. Lagged Controls

- **What it does**: an optional checkbox (labelled "Include 1-week lagged controls" for weekly data, "Include 7-day lagged controls" for daily — the lag length is frequency-aware, see §F2) that, when enabled, adds each control region's **lagged** KPI as an additional predictor (`add_lagged_control_features`), alongside its same-period value. The lag is 1 period for weekly data and 7 periods for daily data (so the daily lag compares the same day of week). Applies to Validate Test Design, Measure Test Impact, and Bayesian TBR (which inherits the frequency/lag setup from the validation run it's built on).
- **Why it may help**: if the test region's KPI responds to control-region movements with a short delay (e.g. shared seasonality or a slow-moving regional trend that shows up in the test region a week later), a lag-1 feature can capture that relationship better than the same-week value alone.
- **How lagged features are created**: `add_lagged_control_features()` sorts the model matrix by date, and for each control creates a `{control}_lag{n}` column (e.g. `_lag1` weekly, `_lag7` daily) via `.shift(n)`. It accepts a `frequency_config` or `time_series_frequency` argument to resolve the lag length. In `run_validation_method`, lagging is done on a **combined pre + test/post matrix built once**, so the very first test-period row can still use the immediately preceding pre-period value as its lag (rather than losing that row).
- **Why enabling lags drops the first available row(s)**: the first row(s) in any continuous date series have no earlier period to reference, so their lag value is `NaN` and — per `add_lagged_control_features`'s `dropna` step — those rows are dropped from the model matrix entirely (1 row for weekly, 7 for daily).
- **Useful when**: there's a plausible, consistent short (1-week for weekly data, 1-week/7-day for daily) delay in how control-region movements show up in the test region, and there's enough pre-period history to spare the dropped row(s) and still fit reliably.
- **May overcomplicate when**: the pre-period is already short (losing a row matters more), the number of controls is large (doubling the feature count increases overfitting risk for LASSO/Elastic Net), or there's no substantive reason to expect a delayed relationship. Always check rolling-origin validation with and without lags before committing to lagged features.

### I. Bayesian Time-Based Regression (TBR)

Tab 4. Builds a Bayesian linear regression of the test-region aggregate KPI on the selected controls' KPI, fit with **PyMC** (imported lazily inside the tab).

- **Prerequisite**: the tab requires a completed **Evaluate-mode** validation run (from the Measure Test Impact tab) — if `validation_results` is missing or was produced in Design mode, the tab shows an info message and stops. It also stays disabled while an unacknowledged frequency mismatch warning is active in the validation setup (`frequency_mismatch_blocked`, §F2).
- **Controls used**: Bayesian TBR uses **the selected controls from whichever validation method the user picks in the tab's method selector** (Structurally Matched, Data-Optimised, Data-Optimised excluding force-excluded regions, or User Selected) — not automatically "all non-test regions." For `METHOD_STRUCTURAL` / `METHOD_USER_SELECTED`, this is the raw matched/manually-picked group (with a warning + stop if it's empty). For the data-optimised methods, it's `selected_regions` from the LASSO/Elastic Net fit — and if the model retained **zero** controls, the tab deliberately warns and stops rather than falling back to the full candidate pool, since a silent fallback would change what Bayesian TBR is actually testing. An expander lists the exact base control regions (and, when lagging is enabled, the full model feature terms including lagged terms) before running.
- **Frequency and lag setup are inherited**: Bayesian TBR always uses the same time-series frequency and lagged-controls setting as the validation run it's built on (stored inside `validation_results`), keeping the lag length (1-week vs. 7-day) consistent rather than offering an independent toggle.
- **Sampling settings**: fixed in code at `pm.sample(draws=2000, tune=1000, chains=4, target_accept=0.95, random_seed=42)` — `target_accept` is set high specifically to suppress divergent transitions (see §M).
- **The model** estimates, for the test period (and optional post period), what the test region's KPI *would have been* absent the treatment — the **counterfactual** — using the fitted relationship to the control regions.
- **Uplift = actual − counterfactual**, summed over the test period.
- **Posterior uncertainty**: because this is a Bayesian model, every quantity (coefficients, counterfactual predictions, uplift) is represented as a distribution of posterior samples, not a single point estimate — this is what lets the app report credible/predictive intervals rather than just a single uplift figure.
- **MCMC diagnostics** (`summarize_mcmc_diagnostics`) are shown so the user can judge whether the sampler actually converged before trusting any of the above (see §M).

**Model components in plain English**:
- **Intercept**: a baseline level for the test KPI, independent of the controls.
- **Control coefficients**: how strongly (and in which direction) each control region's KPI is associated with the test region's KPI, learned from the pre-period.
- **Sigma / residual noise**: the model's estimate of week-to-week randomness in the test KPI that isn't explained by the controls — this is what gets added back in to produce a *predictive* (rather than just a *fitted-mean*) interval.
- **Priors**: the model's assumptions about plausible coefficient values *before* seeing the pre-period data (see §J for how these are set).
- **Posterior samples**: the MCMC sampler's draws of parameter values (intercept, coefficients, sigma) that are consistent with both the priors and the observed pre-period data.
- **Posterior predictive samples**: simulated *outcomes* (not just parameters) generated by combining posterior parameter draws with simulated observation noise (using `sigma`) — these represent "what an actual observation could plausibly look like," not just "what the average relationship predicts."

### J. Structurally Informed Priors

- **What the option does**: a checkbox ("Use structurally informed coefficient priors", **off by default**). When off, every control coefficient gets a fixed weak prior of `Normal(0, σ=0.50)`. When on, `calculate_structural_prior_sigmas()` computes a prior standard deviation (sigma) per control **based on how structurally similar that control is to the test region**.
- **How it works**: structural distance/similarity between each control and the test group (using the same population-weighted feature machinery as Tab 1 matching; similarity = 1/(1+distance)) is min-max scaled to a sigma between a `min_sigma` and `max_sigma` bound — more structurally similar controls get a **wider** (less restrictive) prior sigma, less similar controls get a **narrower** (more restrictive) one. Various edge cases (no valid features, a single control, all similarities identical, all distances missing) fall back to the uniform σ=0.5 weak prior, labelled "Standard weak prior" in the per-control prior table shown in the UI.
- **Sigma bounds are data-driven**: the `min_sigma`/`max_sigma` bounds passed in by the Bayesian tab are derived from the **median absolute pre-period correlation** between the controls and the test KPI — roughly `min_sigma = clip(0.4 × median_corr, 0.10, 0.40)` and `max_sigma = clip(1.2 × median_corr, 0.30, 0.90)`, falling back to (0.25, 0.70) on error. Better-tracking control sets therefore get a higher sigma ceiling overall; weakly-tracking sets get tighter shrinkage. (This partially implements the "correlation-informed priors" idea previously listed only as a future improvement — structural similarity still drives the *per-control* ordering, while correlation sets the overall *scale*.)
- **Lagged terms share their base region's sigma**: when lagged controls are enabled, each `{control}_lag{n}` term reuses the same prior sigma as its base region — there is no separate lag-specific prior (still listed as a possible future refinement in §5.7).
- **Why the coefficient mean stays at zero**: the prior *mean* for every control coefficient remains 0 regardless of structural similarity — structural information only changes how much the model is *allowed to move away from zero* (the prior width), not which direction it's nudged. This keeps the approach agnostic about the sign/strength of any particular control's relationship; the data (pre-period fit) still has to do the work of pulling a coefficient away from zero.
- **Why similar controls get wider priors**: a structurally similar control is more plausible as a genuine driver/proxy of the test region's behaviour, so the model is allowed more flexibility to assign it a meaningful coefficient.
- **Why weak controls are shrunk more strongly toward zero**: a structurally dissimilar control is less plausible as a genuine relationship, so the model is more strongly regularised to keep its coefficient near zero unless the pre-period data provides very strong evidence otherwise.
- **How this differs from selecting controls directly**: LASSO/Elastic Net perform **hard selection** — a coefficient can be exactly zero, dropping a control entirely. Structurally informed priors are a **soft, continuous nudge** within the Bayesian model — every control can still receive a non-zero coefficient if the pre-period data supports it; structural similarity just makes that easier or harder. This is a soft influence, not a hard inclusion/exclusion rule.

### K. Bayesian Chart Interpretation

The Bayesian TBR line chart shows:

- **Actual**: the real observed test-region KPI, across pre, test, and (if present) post periods.
- **Counterfactual (mean)**: the fitted posterior mean prediction for what the test region's KPI would be, across all periods (pre, test, post).
- **Pre-period interval, test-period interval, and optional post-period interval**: shaded uncertainty bands around the counterfactual.
- **Test start / test end markers**: vertical reference lines marking the treatment window.
- **Indexed vs. raw KPI view**: a toggle to display the chart either in raw KPI units or indexed to the pre-period average (= 100), useful for comparing shape/magnitude of movement independent of the KPI's absolute scale.

**Recommended approach for the shaded intervals** (this is the standard the app's Bayesian TBR chart should follow):

- **Pre-period**: a **94% HDI / credible interval around the fitted counterfactual mean** — this reflects uncertainty in the *average relationship* only, and deliberately does **not** include observation-level noise, because during the pre-period we are checking model fit, not forecasting an unseen outcome.
- **Test period**: a **94% posterior predictive interval** — because the test period is out-of-sample (we're predicting what *would have happened*), this interval includes normal observation-level noise (via posterior `sigma`), making it wider and more representative of the plausible range of actual counterfactual outcomes.
- **Optional post-period**: also a **94% posterior predictive interval**, for the same out-of-sample reasoning as the test period.

**Why the interval type should change between periods**: a credible interval around a *mean* only answers "how sure are we about the average relationship?" A predictive interval answers "what range of actual values could we plausibly see?" — which is the more relevant question once you're comparing a *specific observed value* (the actual test-period KPI) against a *specific counterfactual claim*. Using the (narrower) mean-only interval for the test period would systematically overstate confidence in the comparison.

> ✅ **Implementation status (verified against the current code)**: the code follows this convention. The pre-period band is built from **fitted-mean posterior samples** (`mu_pre_samples` = intercept + coefficients·X, no observation noise; 3rd–97th percentiles → `pre_lower_mean_hdi`/`pre_upper_mean_hdi`), while the test/post bands are built from **posterior predictive samples** (`mu_test_samples`/`mu_post_samples` plus simulated `Normal(0, sigma)` noise). The chart legend labels the bands "94% HDI (pre-period, fitted mean)" vs. "94% predictive interval (test/post)" accordingly, and the interpretation caption explains the distinction. If the interval calculations are ever changed, keep the labels and the calculation in lockstep — the two interval types are not interchangeable, and a mismatch changes how confidently a period's result can be read. Note the "94%" bounds are computed as 3rd/97th percentiles of the samples (an equal-tailed interval) rather than a formal ArviZ HDI — for near-symmetric posteriors these are practically equivalent, but keep the wording in mind if the model ever produces strongly skewed posteriors.
>
> A **⬇ Download chart data** button (`build_chart_data_xlsx`) next to the chart exports the exact plotted series (raw and indexed sheets) to Excel, since Plotly's own toolbar only exports a PNG.

The chart supports interpretation, but **the uplift summary cards remain the primary readout for total impact** (see §L) — the chart is for visually sanity-checking fit and spotting periods of concern, not for reading off a precise number.

### L. Bayesian Summary Cards

- **Posterior mean uplift** (`mean_uplift`): the average uplift across posterior samples of (actual test-period total − simulated counterfactual test-period total).
- **Uplift percentage** (`uplift_pct`): mean uplift as a percentage of the mean predicted counterfactual total.
- **94% credible interval / HDI for uplift** (`uplift_hdi_lower` / `uplift_hdi_upper`): calculated from the fitted counterfactual mean only (no observation noise) — narrower, representing uncertainty in the *average* effect; shown as the secondary interval.
- **94% predictive interval for uplift** (`uplift_pi_lower` / `uplift_pi_upper`): calculated from posterior predictive counterfactual totals (includes observation noise) — the **primary/headline** interval on the summary card, since it's the more honest representation of plausible real-world outcomes.
- **P(Uplift > 0)** (`prob_pos`): the proportion of uplift samples that are positive, computed from the **posterior predictive** uplift samples — consistent with the primary predictive interval.

> ✅ **Implementation status (verified against the current code)**: as with the chart bands (§K), the code matches this convention — `uplift_pi_*` comes from posterior-predictive (noise-included) counterfactual totals and is the primary readout; `uplift_hdi_*` comes from fitted-mean-only totals; the uplift histogram is drawn from the posterior predictive samples with the 94% predictive bounds marked. As in §K, the 94% bounds are 3rd/97th percentiles (equal-tailed), and card labels must be kept in lockstep with any future calculation changes. The uplift histogram also has a **⬇ Download chart data** Excel export.

**Important caveats**:
- **P(Uplift > 0) is not a frequentist p-value.** It is a direct Bayesian probability statement ("given the model and data, there's an X% chance the true effect is positive"), not the probability of observing data this extreme under a null hypothesis of no effect. Don't describe it using p-value language.
- **A wide interval means high uncertainty** — a positive mean uplift is not, by itself, strong evidence if the interval is wide.
- **A positive mean uplift with an interval that crosses zero should be treated cautiously** — it's consistent with a real positive effect, but also consistent with no effect (or even a negative one), given the model's uncertainty.

### M. MCMC Diagnostics

Computed by `summarize_mcmc_diagnostics()` from the ArviZ posterior summary:

- **R-hat**: measures whether multiple MCMC chains have converged to the same distribution. The app flags R-hat > 1.01 as a concern (`rhat_ok`).
- **Effective Sample Size (ESS)**: how many "effectively independent" posterior samples were obtained, accounting for autocorrelation within chains. The app flags ESS below `CONFIG["ess_min_threshold"]` (default 500, described in code as a "softer threshold", was 1000) as a concern (`ess_ok`).
- **MCSE / SD ratio**: Monte Carlo Standard Error relative to the posterior standard deviation — a measure of how much sampling noise is affecting the posterior estimate itself. The app flags a ratio ≥ 10% as a concern (`mcse_ok`).
- **Divergent transitions** (`n_divergences`, `divergence_rate`, `divergence_ok`): the count of NUTS divergences across all chains (from `trace.sample_stats["diverging"]`), with a rate shown relative to total post-tuning draws. Unlike R-hat/ESS/MCSE — which mostly flag sampling *noise* — a divergence flags a region of the posterior the sampler failed to explore, which can **bias** point estimates rather than just add noise. For that reason **even a single divergence is treated as a hard fail** (no tolerance band), and the message suggests raising `target_accept`, adding tuning steps, or reparameterising. The sampler already runs with `target_accept=0.95` specifically to suppress divergences.
- **Why convergence diagnostics matter**: if the sampler hasn't converged (poor R-hat), or hasn't produced enough effectively-independent samples (low ESS), or the sampling noise itself is large relative to the estimate (high MCSE/SD), then the posterior summary — including the credible/predictive intervals and the uplift estimate — cannot be trusted as an accurate reflection of the true posterior.
- **What users should do if diagnostics are poor**: don't report the result as-is. Consider re-running with more draws/tuning steps, simplifying the model (fewer controls, e.g. by disabling lagged controls or force-excluding weak controls), checking for data issues (outliers, structural breaks in the pre-period), or reconsidering the prior specification.
- **Why results should not be trusted if the sampler has not converged**: an unconverged sampler's output isn't a valid sample from the actual posterior distribution — the reported mean, intervals, and probabilities could be systematically wrong, not just "a bit noisy."

### N. Residual Diagnostics

- **Residuals**: actual minus predicted (fitted) KPI in the pre-period (`pre_residuals = y_pre - y_pred_pre`, computed in `run_validation_method`).
- **Durbin-Watson** (`durbin_watson_stat`): see §G — tests for first-order autocorrelation in the residual series.
- **Residual autocorrelation**: when residuals are correlated with their own recent past, it usually means the model is missing some time-based structure — a trend, seasonality, or a lagged relationship (see §H) — rather than the "noise" being genuinely random.
- **Why residual patterns are a warning sign**: linear models (including the ones used here) generally assume errors are independent; when they're not, standard errors and intervals can understate the true uncertainty, and any apparent "significant" result may be partly an artefact of unmodelled structure rather than a real effect.
- **Why residual diagnostics help assess missing time-based structure**: a clean pre-period fit (good R², low RMSE) can still have systematically patterned residuals — Durbin-Watson (and any future diagnostics, see below) exist precisely to catch that failure mode, which the headline fit metrics can miss.

**Suggested future diagnostics** (not currently implemented; see §T):
- Ljung-Box test (a more general autocorrelation test across multiple lags, not just lag-1).
- ACF (autocorrelation function) plot for visual inspection of residual structure.
- Residual trend check (is there a systematic drift in residuals over the pre-period?).
- Rolling error stability (does forecast error grow or shrink as more data is added?).

**Previously-suggested diagnostics now implemented**: holdout bias is now surfaced prominently as the "Average Bias (%)" / "Bias Risk" rows (section C2 of the Method Comparison table), and the in-sample-vs-out-of-sample gap is surfaced as the "Overfitting Gap" / "Overfitting Risk" rows (section D) — both feed the overall Counterfactual Confidence rating (§G2).

### O. Placebo Testing

(See also §G for the specific metrics.)

- **What placebo windows are**: repeated "fake tests" run entirely within the pre-period, where a window of the same length as the real test (or a user-set `placebo_length_periods`) is held out, a model is refit on the data before it, and a "fake uplift" is measured — even though nothing actually happened during that window.
- **Why placebo windows should usually match the test-window length**: the goal is to characterise "how much apparent uplift could arise from noise alone, over a period of this length" — a mismatched window length would answer a different (less relevant) question.
- **Why enough pre-period data is needed**: each placebo window needs (a) enough prior history to train a stable model (`min_training_periods` — 13 weeks or 84 days by default, see §F2) and (b) a full window of held-out data afterward — short pre-periods yield few, less-independent placebo windows, so the placebo distribution becomes less reliable as an evidence base.
- **How placebo uplift helps contextualise real uplift**: if the real observed uplift sits comfortably outside the range of "fake" uplifts the model produces on data with no real effect, that's strong non-parametric evidence the real result isn't just noise.
- **Interpreting percentile rank, p-value, z-score**: see §G — a high percentile rank / low p-value / large-magnitude z-score all point the same direction (the real uplift is unusual relative to placebo noise). Treat these as **approximate, non-parametric extremeness checks** that broadly agree with each other — evidence consistent with an effect, not proof of statistical significance — so don't rely on just one, and don't over-interpret a borderline value as definitive.
- **Limitations with too few placebo windows**: with very few windows (the app subsamples to at most 40 evenly-spaced windows for performance — see §G), the placebo distribution can be lumpy/unstable, and small window counts cap the resolution of any percentile/p-value estimate (~1/40 at best) — treat placebo evidence from a short pre-period as suggestive, not definitive.

### P. Design Mode vs. Evaluation Mode

**Designing a future test** (Tab 2 — Validate Test Design):
- Focus on **pre-period fit** — does the candidate control set track the test group's historical KPI well?
- Focus on **rolling-origin validation** — is the fit stable and genuinely predictive out-of-sample, not just a good in-sample fit?
- Focus on **placebo stability** — is the placebo uplift distribution tight and centred near zero?
- **Do not over-optimise on one metric** — a control set chosen purely to maximise, say, pre-period R² can be overfit; look across correlation, error metrics, Durbin-Watson, and placebo results together.
- Choose controls that are **both** structurally plausible **and** historically predictive — neither alone is sufficient.

**Evaluating a completed test** (Tab 3 — Measure Test Impact, Tab 4 — Bayesian TBR):
- **Define pre, test, and optional post windows carefully**, ideally matching what was planned before the test ran.
- **Avoid changing windows after seeing results** — adjusting date ranges to chase a more favourable uplift number undermines the credibility of the whole analysis (a form of data dredging / p-hacking).
- **Assess fit before interpreting uplift** — check the same pre-period diagnostics as in Design mode first; a strong-looking uplift built on a weak fit is not trustworthy.
- **Use Bayesian TBR and placebo results together** — they answer complementary questions (a probabilistic effect size with uncertainty, vs. a non-parametric "is this unusual" check).
- **Treat the post-period as supplementary unless it was pre-defined** — post-hoc post-period analysis is useful for spotting rebound/decay effects, but shouldn't be used to retroactively justify or reinterpret the primary test-period result.

### Q. Recommended User Decision Process

A practical checklist before trusting and reporting a result:

1. Are the test geos sensible and uncontaminated (not already affected by something else)?
2. Are control geos plausible and uncontaminated (not receiving the treatment, or a related effect)?
3. Is structural balance acceptable (Weighted Structural Distance / Mean Abs SMD within reasonable bounds)?
4. Does the pre-period actual-vs-predicted fit look good, visually and numerically?
5. Are sMAPE/RMSE (pre-period and rolling-origin) acceptable for this KPI's scale and volatility?
6. Is Durbin-Watson close to 2 (residuals not strongly autocorrelated)?
7. Are rolling-origin errors stable across folds (not degrading badly over time)?
8. Are placebo results reasonable (real uplift clearly outside, or at least near the edge of, the placebo noise range)?
9. Is the overall Counterfactual Confidence rating (§G2) acceptable — and if it's moderate/low, do the listed drivers have a plausible explanation?
10. Do Bayesian MCMC diagnostics look healthy (R-hat, ESS, MCSE/SD within thresholds, and zero divergent transitions)?
11. Is the uplift large relative to its uncertainty (predictive interval doesn't comfortably straddle zero, or if it does, is that being reported honestly)?

If several of these checks fail, treat the resulting uplift estimate as **indicative at best**, and say so explicitly when reporting it.

### R. Known Limitations

- **The app does not prove causality by itself.** It supports a quasi-experimental, evidence-based judgement — not a randomised controlled trial with guaranteed causal identification.
- **Good pre-period fit does not guarantee a valid post-period counterfactual.** The relationship between test and control regions can change for reasons unrelated to the treatment (a competitor's campaign, a local event, a structural market shift) — pre-period validation reduces but does not eliminate this risk.
- **Control regions may be contaminated** by national campaigns, spillover from the test regions, or other simultaneous changes — the app cannot detect contamination it isn't told about; force-exclude is a manual safeguard, not an automatic one.
- **Small KPI volumes create noisy estimates** — regions or metrics with low absolute counts will have proportionally larger random fluctuation, making both structural and time-series matching less reliable.
- **Few pre-period weeks reduce reliability** across the board — fewer degrees of freedom for model fitting, fewer/less-independent rolling-origin folds, fewer/less-independent placebo windows, and less basis for detecting autocorrelation.
- **Too many controls, or lagged controls, can overfit** — especially with a short pre-period; more predictors relative to observations increases the risk that the model fits noise rather than signal, even with LASSO/Elastic Net regularisation.
- **Structural features may be stale or incomplete** — the underlying demographic workbook is a snapshot and may not reflect current population/demographic reality, especially in fast-changing markets.
- **Bayesian results depend on model assumptions and priors** — a linear model with (approximately) Normal errors and the chosen prior structure; if the true relationship is meaningfully non-linear, or priors are poorly chosen, results will be biased accordingly. Structurally informed priors are a soft nudge, not a guarantee of correctness.
- **Post-period interpretation can be complicated by delayed effects or rebound** — a treatment's effect may not stop cleanly at test end (carryover) or may show artificial "rebound" patterns that are hard to distinguish from genuine post-test dynamics without more sophisticated modelling.
- **Model selection risk**: comparing many control-selection methods, date windows, or settings can lead to overfitting or cherry-picking if users choose the setup that gives the most favourable result after seeing the output. The preferred setup should be chosen based on pre-period diagnostics and business plausibility, not just the largest uplift.

---

## 5. Developer Notes

### 5.1 Code structure (top-to-bottom)

The app is a single Streamlit script (no separate modules) organised roughly as:

1. **Imports & app config** — `st.set_page_config` (page title currently "TEST GeoTestLab" — note the temporary "TEST " prefix in both the page title and `st.title` if preparing a production release), `load_css()` (loads an optional external `styles.css`, plus inline CSS scoped to `st.download_button` sizing), `CONFIG` dict of tunable constants (including the `reliability_thresholds` traffic-light bands — the single source of truth for §G2's classifiers), `DATA_PATH`, method-name constants (`METHOD_STRUCTURAL`, etc.).
2. **Time-series validation helpers** — `detect_date_columns`, `detect_metric_column`, KPI loading/reshaping (`load_and_reshape_kpi`, supporting both the simple and aggregated file layouts — see §3.6), model matrix building, lag features (frequency-aware, §F2), Durbin-Watson, the Counterfactual Confidence classifiers (`classify_autocorrelation_risk`, `calculate_overfit_gap`, `classify_overfitting_risk`, `classify_rolling_validation_error`, `classify_rolling_bias_risk`, `combine_reliability_ratings`, `get_reliability_drivers` — §G2), frequency helpers (`get_frequency_config`, `infer_time_series_frequency` — §F2), display/export helpers (`format_range`, `build_chart_data_xlsx`), error metrics, MCMC diagnostic summary (incl. divergences), structural prior sigma calculation, model construction (`safe_tscv`, `build_regularized_model`, `classify_validation_method`, `_warn_on_row_loss`, `_warn_on_cv_fallback`), rolling-origin validation (+ `_summarize_rolling_origin_folds`), the placebo helpers (`_run_placebo_windows`, `_summarize_placebo_results`), and the main `run_validation_method()`.
3. **Text/data cleaning helpers** — `repair_text_value`, `clean_dataframe_text`, `normalise_column_names`, `inspect_excel_sheet`.
4. **Excel workbook loading** — `get_workbook_sheet_names`, `load_market_sheet`, population/geography/grouping-column helpers, `prepare_market_dataframe`, `read_kpi_pattern_excel` (cached loader for the KPI Pattern sidebar upload — see §3.7).
5. **Aggregation helpers** — `weighted_average_vectorized`, `aggregate_market_data`, `impute_missing_features`.
6. **Matching metric helpers** — `weighted_profile`, `fit_structural_stats`, `calculate_metrics` (+ cached variant), `make_fast_metrics_fn` (vectorised per-run scorer used by all three matching strategies — see §5.6), `preprocess_data`, `stochastic_genetic_search`.
7. **UI/session-state utility functions** — validation of uploaded data (`validate_data`), state-reset callbacks (`reset_results`, `reset_manual_results`, `cleanup_session_state`), display formatting helpers (including the KPI Pattern-specific `kpi_share_label`, `kpi_feature_date_label`, `kpi_pattern_display_rename_map` — see §3.7/§A2), `calculate_experiment_population_coverage`.
8. **Guided experiment group search** — `find_guided_test_group`.
9. **Sidebar** — Matching Method toggle (Structural vs. KPI Pattern, §A2), then either Market/Geography-Level selectors (Structural) or the KPI Pattern Data Source upload + aggregation-level/metric/date-range selectors (§3.7), followed by the shared Matching Strategy selector.
10. **Tab 1 (Region Matching)** — implemented as `render_structural_matching_tab()`: test/control group setup UI, Strategy Parameters (Force 1-to-1, pool-size range, genetic iterations slider), the main matching loop (greedy / hill-climbing / stochastic branches, shared by both matching methods and built around a single `make_fast_metrics_fn` scorer per run), results display behind a Summary / Diagnostics / Export view radio (pool-size optimisation chart, convergence chart, feature distribution detail), staleness detection (`matching_setup_changed_since_last_run`), and Export to Excel (branches on `kpi_pattern_mode` — see §A2).
11. **`render_method_comparison_table(results, mode, test_start, control_regions_val)`** — a self-contained rendering step for the sectioned Method Comparison table (§G2), its captions, and the "How to interpret these results" expander; reads only from the per-method results dict.
12. **`render_time_series_validation(mode)`** — the shared function powering both Tab 2 ("Design") and Tab 3 ("Evaluate"): settings UI (date windows, frequency radio + mismatch acknowledgment (§F2), lag checkbox, placebo/rolling-origin settings), the KPI file upload with aggregation-level/metric-column carry-over from KPI Pattern setup when applicable (§F), the run button, calls into `run_validation_method()` for each comparison method, results display (per-method control selection details, region role table, KPI Performance by Geography, chart with Excel data download, the Method Comparison table via `render_method_comparison_table()`), and validation-result staleness handling (`clear_validation_state`).
13. **Tab 4 (Bayesian TBR)** — Evaluate-mode prerequisite gate, method/control selection from the chosen validation method (no silent fallback for empty data-optimised selections), structural/weak prior setup (data-driven sigma bounds — §J), the PyMC model definition and sampling (draws=2000, tune=1000, chains=4, target_accept=0.95), posterior/posterior-predictive computation (fitted-mean HDI for pre-period, predictive intervals for test/post — verified, §K/§L), summary cards, line chart and uplift histogram (both with Excel data downloads), MCMC diagnostics table (incl. divergences), and the "how to interpret" text.
14. **Sidebar footer** — data quality check summary ("5. Data Quality Check").

### 5.2 Key functions (by name)

- **`load_and_reshape_kpi(uploaded_file, agg_col=None, metric_col=None)`** — reads the uploaded KPI Excel, melts wide date columns into a long `(region_raw, metric_name, date, kpi)` table, coerces types, and drops invalid/missing rows. Auto-detects the simple 2-column layout vs. the aggregated multi-level layout (§3.6); for the latter, `agg_col`/`metric_col` must be supplied (raises `ValueError` otherwise).
- **`read_kpi_pattern_excel(file_bytes)`** — `st.cache_data`-cached loader for the KPI Pattern sidebar upload (§3.7), keyed on the file's raw bytes. Parses with the `calamine` engine, falling back to `openpyxl`. Introduced to stop the file being re-parsed twice per Streamlit rerun (once for the sidebar peek, once for the main tab).
- **`detect_date_columns(df_raw)`** — returns the columns of a raw uploaded KPI DataFrame that are real datetime column headers, in original order; used to separate date/value columns from leading identifier columns in both KPI file layouts.
- **`detect_metric_column(non_date_cols)`** — best-guesses the metric-name column by header text (`"Metric"`, case-insensitive); returns `None` if no match, so callers can fall back to asking the user.
- **`build_region_mapping(df_long, valid_regions, adobe_to_geo)`** — maps raw uploaded region labels to the app's internal geography names, using the Adobe reference mapping first and falling back to a direct name match against `valid_regions`. `valid_regions` must be the **full** candidate geography universe (e.g. `agg_df[geo_col].unique()`), not just the already-selected test+control regions — passing only the selected regions was a historical bug that silently capped every downstream method's candidate pool to whatever Region Matching had already picked, defeating the point of "Data-Optimised Controls" (which is meant to search *all* non-test regions).
- **`apply_geo_aggregation(df_long, geo_col)`** — sums KPI values by `(date, region)`, collapsing any raw regions that map to the same geography.
- **`build_model_matrix(agg_df, control_list, test_regions)`** — builds the wide `date | test_kpi | control_1 | control_2 | ...` matrix used for all time-series model fitting; inner-joins on date and drops rows with any missing value.
- **`add_lagged_control_features(model_df, control_list, lags=(1,), frequency_config=None, time_series_frequency=None)`** — sorts by date, adds `{control}_lag{n}` columns via `.shift(n)` for each control (lag length resolved from the frequency config: 1 weekly, 7 daily), drops rows with missing lag values, and returns the expanded feature list plus a same-period/lag feature map.
- **`durbin_watson_stat(residuals)`** — a manual (no-`statsmodels`-dependency) implementation of the Durbin-Watson statistic for residual autocorrelation.
- **Counterfactual Confidence helpers** — `classify_autocorrelation_risk`, `calculate_overfit_gap`, `classify_overfitting_risk`, `classify_rolling_validation_error`, `classify_rolling_bias_risk`, `combine_reliability_ratings`, `get_reliability_drivers` (see §G2; thresholds in `CONFIG["reliability_thresholds"]`; `_is_valid_number()` is the shared missing/non-finite guard).
- **`run_validation_method(agg_df, control_list, test_regions, method_name, pre_start, pre_end, test_start=None, test_end=None, use_post=False, post_start=None, post_end=None, compute_uplift=True, placebo_length_weeks=None, min_training_weeks=13, include_lagged_controls=False, time_series_frequency="weekly", placebo_length_periods=None, min_training_periods=None, frequency_config=None)`** — the central validation function. Resolves frequency settings (the `*_periods` arguments take precedence over the legacy `*_weeks` aliases), builds a combined pre+test/post model matrix (once, so lagged features work across period boundaries), fits an Elastic Net or LASSO model (`method_name` is `"enet"` or `"lasso"`) via `build_regularized_model` (TimeSeriesSplit CV, or a clearly-labelled exploratory fixed-alpha fallback — see §G), computes fit metrics and Durbin-Watson, runs rolling-origin validation, computes test/post predictions and uplift, runs the placebo loop, derives the traffic-light risk ratings and the Counterfactual Confidence rating (§G2), and returns a large dict of results (see the function's `return` statement for the authoritative full list of keys — it includes both raw arrays for charting and summary scalars for the comparison table, plus backward-compatible alias keys like `min_training_weeks`). Includes a defensive check that raises a clear `st.error` (rather than an unhandled `KeyError`) if any control region in `control_list` has no matching column in the built model matrix — most often caused by an aggregation-level mismatch between the Region Matching and validation-upload steps (see §F).
- **`safe_tscv(n_splits, n_periods)` / `build_regularized_model(method_name, n_periods, n_splits_pref=5, fixed_alpha=1.0)`** — model construction with time-series-safe CV and the exploratory fixed-alpha fallback described in §G; returns `(model, cv_status, used_cv)`. `classify_validation_method()`, `_warn_on_cv_fallback()`, and `_warn_on_row_loss()` surface the corresponding user-facing warnings.
- **`rolling_origin_validation(X, y, horizon=4, min_training_periods=13, dates=None, n_splits=5, model_type="enet", min_training_weeks=None)`** — `st.cache_data`-cached expanding-window backtest: repeatedly trains on all data up to a point and forecasts the next `horizon` periods, recording sMAPE/RMSE/bias/uplift-error per fold. Fold starts are subsampled to at most 20 evenly-spaced folds for performance; `n_splits` is accepted for backwards compatibility but ignored (all valid folds are used, up to the cap). Folds fit with the exploratory fallback are recorded but excluded from `rolling_smape_mean`/`rolling_rmse_mean` (and hence from Counterfactual Confidence).
- **`_run_placebo_windows(model_pre, model_feature_cols, dates_pre, min_training_periods, placebo_len, method_name, max_windows=40)`** — runs the placebo loop on the already-lagged pre-period matrix (preserving lag features across window boundaries), subsampling to at most 40 evenly-spaced windows; returns parallel lists of placebo uplifts, uplift %s, sMAPEs, and RMSEs. **`_summarize_placebo_results(...)`** turns those lists (plus the real uplift) into the median, 95% range, percentile rank, one-/two-sided p-values, z-score, and placebo error metrics shown in the table. (The previous standalone `placebo_analysis()` function no longer exists — these two helpers are the single source of truth for placebo statistics.)
- **Frequency helpers** — `get_frequency_config(time_series_frequency)` (all frequency-dependent settings in one dict) and `infer_time_series_frequency(dates)` (median-gap-based "daily"/"weekly"/"unknown" classification, used only to warn — never to silently override the user's selection). See §F2.
- **Display/export helpers** — `format_range(lower, upper, suffix, decimals)` (consistent "x to y" range formatting, "N/A" for missing/non-finite values, used throughout the Method Comparison table) and `build_chart_data_xlsx(sheets)` (in-memory .xlsx bytes for `st.download_button` chart-data exports; skips `None` sheets and always emits valid bytes).
- **`calculate_structural_prior_sigmas(agg_df, test_regions, control_regions, geo_col, feature_cols, weight_dict=None, population_col="Population", min_sigma=0.25, max_sigma=0.70)`** — computes a structural-distance-based prior sigma per control region, min-max scaled between the sigma bounds; the Bayesian tab passes in data-driven bounds derived from the median absolute pre-period correlation (§J). Falls back to a uniform σ=0.5 "Standard weak prior" for the edge cases listed in §J, and returns both the sigma array and a per-control explanation DataFrame shown in the UI.
- **Structural matching functions** — `weighted_profile` (population-weighted feature means), `fit_structural_stats` (fits one mean/std basis per run), `calculate_metrics` (computes Weighted Structural Distance and Mean Abs SMD for a candidate test/control pairing — the authoritative, non-vectorised reference implementation), `preprocess_data` (prepares scaled arrays for `NearestNeighbors`-based candidate search, using the same basis as `calculate_metrics` for consistency).
- **`make_fast_metrics_fn(pool_df, test_df_run, features, weights_dict, eligible_means, eligible_stds, population_col=POPULATION_COL)`** — builds a vectorised scorer, closed over one run's fixed pool/test data, that all three matching strategies call hundreds to thousands of times to score candidate control groups. Returns a `fast_metrics(idx_list)` function that produces the same result dict as `calculate_metrics()`, but computed with a handful of NumPy array operations on precomputed matrices instead of re-copying dataframes and re-imputing/re-deriving arrays on every call. See §5.6 for why this exists and §5.7 for how to extend it safely.
- **`stochastic_genetic_search(pool_df, test_df_run, active_features, weights, n, calculate_metrics_fn, eligible_means, eligible_stds, nn_start_idx, n_iterations, random_state, fast_metrics_fn=None)`** — the Advanced matching strategy: starts from a nearest-neighbour candidate group and performs randomised single-region swaps, keeping any that improve Weighted Structural Distance, tracking the best group found; seeded for reproducibility. When `fast_metrics_fn` is supplied (the normal case — see Tab 1's matching loop) it's used in place of `calculate_metrics_fn` for per-swap scoring; the two are numerically equivalent, `fast_metrics_fn` is just faster.
- **Bayesian TBR section** (inline in Tab 4 / the Bayesian TBR workflow area, not a standalone function) — defines the PyMC model (`intercept ~ Normal(0,1)`, `coeffs ~ Normal(0, prior_sigmas)` with per-control prior sigma, `sigma ~ HalfNormal(1)`, `y_obs`), samples via `pm.sample(draws=2000, tune=1000, chains=4, target_accept=0.95, random_seed=42)`, and derives posterior counterfactual estimates, uplift samples, uncertainty intervals, chart data, summary cards, and diagnostics. The interval convention — fitted-mean posterior samples (`mu_pre_samples` etc.) for the pre-period HDI, posterior predictive samples (fitted mean plus simulated `sigma`-scaled noise) for the test/post predictive intervals and the primary uplift interval, with a secondary fitted-mean-only uplift interval — is **implemented and verified** as of this version (§K/§L); keep the labels and calculations in lockstep if changing either.

### 5.3 Session state usage

The app relies heavily on `st.session_state` to persist state across Streamlit reruns and between tabs, including (non-exhaustive):

- `st.session_state["kpi_pattern_mode"]` — whether the sidebar's Matching Method toggle is set to KPI Pattern (§A2); read throughout Tab 1's UI/matching-loop branches and by the Validate Test Design carry-over logic (§F).
- `st.session_state["kpi_pattern_agg_col_sidebar"]`, `st.session_state["kpi_pattern_metric_col"]`, `st.session_state["kpi_pattern_metric_value"]`, `st.session_state["kpi_pattern_wide_raw"]`, `st.session_state["kpi_pattern_dates_in_range"]` — the KPI Pattern setup selections and derived data (§3.7), read back by the Preview/Feature Comparison table formatting helpers and the Validate Test Design carry-over.
- Matching results and convergence data from Tab 1, plus **staleness flags**: `st.session_state.match_results_stale` and the run snapshot compared by `matching_setup_changed_since_last_run()` (market, geography level, strategy, test regions, weights) so changed inputs flag existing results as stale instead of silently recomputing.
- `st.session_state.time_series_frequency` — the Weekly/Daily selection (§F2), plus `st.session_state.frequency_mismatch_blocked` — set while a detected frequency mismatch is unacknowledged; blocks both the validation run button and the Bayesian TBR run button.
- `st.session_state.include_lagged_controls` — the shared lagged-controls flag, plus the setting is also stored inside the saved `validation_results` dict so Bayesian TBR reads a value consistent with the validation run it's built on.
- `st.session_state.validation_results` — the dict of per-method results from the last `render_time_series_validation` run, including the mode ("Design"/"Evaluate"), date windows, selected metric, and the frequency/lag setup (`time_series_frequency`, `frequency_config`, `include_lagged_controls`) that Bayesian TBR inherits; cleared (`clear_validation_state`, defined inside `render_time_series_validation`) when settings that would invalidate it change (e.g. toggling the lag checkbox or the frequency radio, or re-uploading the KPI file), forcing a re-run before Bayesian TBR can use stale controls.
- `st.session_state.bayesian_results` — the dict of Bayesian TBR outputs (posterior samples, intervals, chart data, diagnostics).
- `st.session_state.eligible_means` / `eligible_stds` — the fixed structural-matching basis for the current run.
- `st.session_state.force_ctrl_exclude` and related force-include/exclude selections.
- Various reset callbacks (`reset_results`, `reset_manual_results`, `cleanup_session_state`) fire on relevant widget `on_change` events to invalidate stale downstream state when upstream inputs change.

### 5.4 Caching

`@st.cache_data(ttl=CONFIG["cache_ttl"])` (default 3600s / 1 hour) is used on:
- `get_workbook_sheet_names`, `load_market_sheet` — avoid re-reading the Excel workbook on every rerun.
- `aggregate_market_data` — avoid recomputing weighted aggregation repeatedly.
- `calculate_metrics_cached`, `preprocess_data` — cache structural-matching computations keyed on (hashable) tuples of inputs.
- `rolling_origin_validation` — cached (with `show_spinner=False`) because it fits up to ~20 models per call and is re-invoked on every Streamlit rerun once results are displayed.
- `read_kpi_pattern_excel` — avoid re-parsing the KPI Pattern sidebar upload (§3.7) on every Streamlit rerun; keyed on the file's raw bytes rather than the `UploadedFile` object itself, since the latter isn't a stable cache key across reruns.

Note that Streamlit's caching requires hashable arguments — this is why several functions accept `_tuple` versions of dicts/arrays (e.g. `eligible_means_tuple`) rather than the dicts/arrays themselves.

### 5.5 Dependencies

Core: `streamlit`, `pandas`, `numpy`, `scikit-learn` (`StandardScaler`, `NearestNeighbors`, `ElasticNetCV`, `RidgeCV`, `ElasticNet`, `TimeSeriesSplit`, `mean_squared_error`, `r2_score`), `scipy.stats`, `altair`, `plotly.express`, `random`, `warnings`, `unicodedata`, `io`, `re`.

Bayesian modelling: `pymc`, `arviz`, and `pytensor`, imported **lazily inside the Bayesian TBR tab** (not at module load time) — the code comments note this is to avoid segfaults/Numba errors at startup on some Python versions (specifically 3.14 at the time of writing). Any change to how/when PyMC is imported should preserve this lazy-import pattern; the import is wrapped in a try/except that shows an `st.error` and stops the tab if PyMC is unavailable.

An optional `styles.css` file is loaded by `load_css()` if present (missing file is silently ignored).

Excel reading uses the `calamine` engine primarily, with a fallback to `openpyxl` if `calamine` fails (`load_market_sheet`).

### 5.6 Expensive computations

- **Matching loop cost (all three strategies, both matching methods)**: scales with the number of candidate-group scoring calls — Greedy scores one group per pool size tested, Hill Climbing scores up to `min(len(curr_idx), 5) x min(len(pot_swaps), 10)` per improvement step, and Advanced (Stochastic Genetic Search) scores one group per iteration (`genetic_iterations`, default 1,000) per pool size. All three now score candidates via `make_fast_metrics_fn()`'s vectorised `fast_metrics()` closure rather than calling `calculate_metrics()` directly per candidate — `calculate_metrics()` pays for two dataframe copies, per-feature median re-imputation (a no-op on already-imputed data), and Python-level per-feature loops on every call, none of which needs to be redone once per candidate when the pool and test data are fixed for the whole run. `make_fast_metrics_fn()` hoists the feature matrix, population weights, eligible-basis arrays, and the (constant) test profile out once per run, so each candidate is scored with a handful of NumPy array operations instead. This makes testing a **range of control-group sizes**, and Advanced mode in particular, dramatically cheaper than before — a like-for-like search that previously took low-single-digit seconds now completes in a small fraction of a second (measured on a 120-region/40-feature synthetic pool; actual timing scales with pool size, feature count, and `genetic_iterations`). `calculate_metrics()` itself is retained as the reference implementation and is still used for the one-off final-result snapshot after a run completes.
- **Placebo loops** inside `run_validation_method` fit a full Elastic Net/LASSO model per placebo window (subsampled to at most 40 evenly-spaced windows), and this runs for **every** comparison method — with 3–4 methods this multiplies the cost. This loop is unrelated to the matching-loop optimisation above (different models, different data shape) and has not been vectorised — see §6 for a possible future direction here.
- **Rolling-origin validation** similarly fits a model per fold (subsampled to at most 20 evenly-spaced folds), and is likewise unaffected by the matching-loop optimisation above — though the whole function is now `st.cache_data`-cached, so the cost is paid once per unique input rather than on every rerun.
- **Bayesian TBR sampling** (`pm.sample(draws=2000, tune=1000, chains=4, target_accept=0.95, random_seed=42)`) is the single most expensive operation in the app and runs once per "Run Bayesian TBR" click. The relatively high `target_accept` trades some sampling speed for fewer divergent transitions.

### 5.7 Where future developers should make changes

- **New matching strategies**: add a new branch in the Tab 1 matching loop (parallel to the Greedy/Hill-Climbing/Stochastic branches) and register a new `strategy_labels` entry; keep the same `opt_data` record shape so downstream charts keep working. Use `make_fast_metrics_fn()`'s `fast_metrics()` closure (already built once per run in the matching loop) to score candidates rather than calling `calculate_metrics()` directly — see §5.6.
- **Changes to the matching score/objective itself** (e.g. a different distance formula): `calculate_metrics()` is the reference implementation; `make_fast_metrics_fn()`'s vectorised path must be kept numerically equivalent to it, or the two will silently diverge for different matching strategies (which use the fast path) vs. the final-result snapshot (which uses `calculate_metrics()` directly). If changing the scoring math, update both, or fall back to always calling `calculate_metrics()` until the fast path is re-verified.
- **New validation methods**: extend the `METHOD_*` constants and the calls into `run_validation_method()` inside `render_time_series_validation`; make sure new methods populate the same result dict keys the Method Comparison table and Bayesian TBR tab expect (especially `selected_regions`, `control_list`, `dw_stat`, `model_feature_cols` if lag-aware).
- **New diagnostics** (Ljung-Box, ACF plot, etc.): follow the pattern of `durbin_watson_stat` — a small, dependency-light helper function, computed inside `run_validation_method` from `pre_residuals`, added to the returned dict, and surfaced in the Method Comparison table (`comparison_rows` list + the `get_value` lookup logic). If the new diagnostic should have a traffic-light rating, follow the `classify_*` pattern: a small classifier reading its thresholds from `CONFIG["reliability_thresholds"]`, and — if it should influence the overall rating — a deliberate decision about where it sits in `combine_reliability_ratings()`'s priority cascade (§G2), not an automatic worst-of vote.
- **New reliability thresholds**: change them only in `CONFIG["reliability_thresholds"]` (the single source of truth) — the classifier functions read from it at call time.
- **New frequencies** (e.g. monthly): extend `get_frequency_config()` and `infer_time_series_frequency()`; everything downstream (lags, defaults, labels) reads from the config dict rather than hardcoding "week".
- **New Bayesian model features** (e.g. a separate lag-specific prior instead of duplicating the same-period sigma onto lagged terms): modify the PyMC model block and `calculate_structural_prior_sigmas` usage inside the Bayesian TBR tab; keep the HDI-vs-predictive-interval distinction intact when adding new interval types.
- **Data-loading robustness**: `load_and_reshape_kpi` and `build_region_mapping` are the most likely places to need hardening if new client KPI export formats need to be supported.
- **KPI Pattern mode display formatting**: new tables shown in KPI Pattern mode (§A2) should use `kpi_pattern_display_rename_map()` (renames `wk_YYYYMMDD` columns to `dd mmm yy` and `POPULATION_COL` to the metric label) and/or `kpi_feature_date_label()` (single-value date formatting, e.g. for chart axis labels) rather than reimplementing the same renaming logic inline, so all tables stay consistent if the underlying feature-naming convention (`wk_YYYYMMDD`) ever changes.

---

## 6. Suggested Future Improvements

- **Ljung-Box test** for residual autocorrelation — a more general check than Durbin-Watson (tests multiple lags jointly, not just lag-1).
- **ACF (autocorrelation function) residual plot** — a visual complement to the numeric autocorrelation tests.
- **Residual drift/trend diagnostic** — explicitly check whether pre-period residuals trend up or down over time, which Durbin-Watson alone won't necessarily flag.
- **Clearer interval naming for HDI vs. predictive interval** throughout the UI and any exports — ensure any future feature (PDF export, CSV download, etc.) follows the same careful HDI/predictive-interval distinction intended for the Bayesian TBR tab (see §K/§L implementation checks), rather than reintroducing an ambiguous "94% interval" label.
- **Exportable results report** — a one-click PDF/Word/PowerPoint summary of the match quality, validation metrics, and Bayesian results for sharing with stakeholders who won't use the app directly.
- **Saved experiment configuration** — persist a given test/control/date-window/method setup (e.g. to a file or lightweight database) so a user can return to or share an exact analysis without re-selecting everything.
- **Stronger data validation for uploaded KPI files** — more explicit checks/errors for wrong column order, mixed metrics in one file, duplicate region names, or unmapped regions, surfaced clearly to the user rather than silently dropped.
- **Optional blended priors** — the current structural priors already scale their sigma bounds by the median pre-period KPI correlation (§J), partially delivering the previously-suggested "correlation-informed priors". A further step would blend structural similarity with *per-control* correlation for the per-control ordering itself, and/or a separate lag-specific prior for lagged terms.
- **Improved speed for Bayesian placebo tests** — currently, only the LASSO/Elastic Net comparison methods run placebo tests; extending genuine placebo-style testing to the Bayesian model itself would be valuable but is currently too slow to run for many windows (each placebo window would need its own MCMC fit) — worth investigating faster approximate-Bayesian or variational approaches for this specific use case.
- **Apply the same vectorisation approach used for the matching loop (§5.6) to the placebo and rolling-origin loops** — those still fit a fresh Elastic Net/LASSO model per window/fold via scikit-learn, which is a different (and harder to vectorise, since it involves actual model fitting rather than a fixed-formula distance calculation) kind of cost than the matching loop's closed-form structural distance, but may still have room for speedup (e.g. warm-starting, reducing redundant refits across methods that share a pre-period).
- **KPI Pattern mode beyond region matching** — KPI Pattern currently only replaces the *matching* step (§A2); the structurally-informed Bayesian priors (§J) and the "Data-Optimised Controls" comparison methods still rely on demographic-style structural distance internally where relevant. Worth reviewing whether any of those should also have a KPI-pattern-aware code path for full consistency when the app is run entirely in KPI Pattern mode.
- **Clearer stakeholder-friendly interpretation labels** — the Counterfactual Confidence rating (§G2) now provides a simplified 🟢/🟡/🔴 summary of *model trustworthiness*; a remaining gap is an equivalent jargon-free summary of the *result itself* (e.g. "Strong evidence of a positive effect" / "Inconclusive" / "No evidence of an effect") derived from the uplift, its predictive interval, and the placebo comparison, for audiences who don't need to read MCSE ratios or Durbin-Watson statistics.
- **Configurable MCMC settings** — draws/tune/chains/target_accept are currently fixed in code (§5.6); exposing them (with sensible guardrails) would help users trade speed vs. quality, especially when re-running after a divergence warning.

---

*This documentation describes the app as implemented at the time of writing. Where a section notes ambiguity or recommends checking the code, that reflects genuine uncertainty about exact current behaviour (e.g. UI wiring details that may change independently of the core logic) — always verify against the live code before relying on a specific claim for a production decision.*
