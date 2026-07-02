# GeoTestLab — Project Documentation

## 1. Project Overview

**GeoTestLab** is a Streamlit app for planning and evaluating **geo-tests** — regional marketing incrementality experiments where some geographic areas (e.g. UK regions/local authorities, or equivalent international geography levels) receive a media pause, a campaign launch, a regional media change, or another market-level marketing intervention, while other areas act as a control.

The app is built around **UK and international non-US markets**. See §1.1 "Intended market coverage" below.

The app helps a user:

- Select **balanced test and control regions** using structural (demographic/population) matching.
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

1. **Choose the market and geography level** (sidebar). E.g. "UK" at "Region" level, or "Germany" at "Länder" level.
2. **Select or build the test group** — either manually pick regions, pick test regions and let the app find controls, or set inclusion/exclusion rules and let the app search for a test group and matched controls (Region Matching area).
3. **Generate matched control regions** using one of three matching strategies (greedy, hill-climbing, or stochastic search), based on population-weighted structural similarity.
4. **Upload historical KPI data** (an Excel export of weekly/period KPI values by region).
5. **Validate test/control fit in the pre-period** — before drawing any conclusions, check that the test aggregate and candidate controls actually tracked each other historically (Validate Test Design area, or Measure Test Impact area).
6. **Compare control-selection methods** — Structural matching vs. LASSO/Elastic Net "data-optimised" selection vs. user-selected controls — side by side on the same metrics (Method Comparison table).
7. **Evaluate a completed test**, if test-period data is available, by defining pre/test(/post) date windows and reviewing uplift, error metrics, and placebo results (Measure Test Impact area).
8. **Run Bayesian TBR** for a full probabilistic impact estimate with credible/predictive intervals (Bayesian TBR area).
9. **Review diagnostics** (structural balance, rolling-origin errors, Durbin-Watson, placebo results, MCMC convergence) before trusting and reporting the result.

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

---

## 4. Main App Sections / Tabs

The app has four tabs plus a sidebar:

- **Sidebar**: Market and Geography Level selection, plus a Data Quality Check footer.
- **Tab 1 — ⚙️ Region Matching**: build test/control groups.
- **Tab 2 — 🔍 Validate Test Design**: pre-launch validation using historical KPI data (no test period yet).
- **Tab 3 — 📊 Measure Test Impact**: evaluate a completed test using pre/test(/post) KPI data.
- **Tab 4 — 🧠 Bayesian TBR**: Bayesian counterfactual impact estimation.

### A. Geography and Market Selection (sidebar)

- **Market selection**: chooses which workbook sheet (country/market) to load.
- **Geography level selection**: chooses which column to group and aggregate by (e.g. Region vs. a finer level). Options differ **per market** because `get_grouping_columns()` reads the columns actually present in that market's sheet — not every market necessarily has the same geography hierarchy defined. For example, a UK sheet might offer "Region" and "Local Authority" levels, while a Germany sheet might offer "Land" and a finer administrative level instead — the app doesn't assume any single hierarchy, it reflects whatever the workbook provides for that market.
- **Why granularity matters**: a coarser level (e.g. "Region") gives fewer, larger candidate geographies — easier to reach a target population share with few units, but less flexibility in matching. A finer level (e.g. postcode or local-authority level) gives many small units — more matching flexibility, but each unit's KPI is noisier and structural aggregation (population-weighted averaging) becomes more important.
- **Population and structural features**: population weights the aggregation of demographic/structural features up to the chosen geography level, and weights the structural-distance calculation in matching, so that large, representative sub-areas count for more than small, atypical ones.

### B. Matching Strategy (`stochastic_genetic_search`, greedy/hill-climbing code in the Tab 1 matching loop)

Three strategies, selectable per the `strategy_labels` mapping:

| UI label | Internal name | What it does |
|---|---|---|
| Basic (Fast) | Greedy (Nearest Neighbor) | For each candidate control-group size `n`, take the `n` nearest control regions to the test group's structural profile (via `NearestNeighbors`) and stop. |
| Intermediate (Balanced) | Refined Greedy (Hill Climbing) | Starts from a nearest-neighbour candidate group, then repeatedly tries swapping one selected region for one of a small set of nearby unselected regions, keeping any swap that improves Weighted Structural Distance, until no improving swap is found (bounded by `CONFIG["max_hill_climbing_swaps"]` and a small per-step swap budget). |
| Advanced (Thorough) | Stochastic (Genetic Search) | Starts from the same nearest-neighbour candidate group, then performs many random single-region swaps (`stochastic_genetic_search`), accepting any swap that improves the score, over a configurable number of iterations (`genetic_iterations`, default 1,000). Uses a fixed random seed (42) for reproducibility. |

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
- **Missing data and date alignment**: `build_model_matrix` uses an inner join and drops rows with any missing value, so gaps in any single control region's data will remove that date from the *entire* model, not just that region — a control with sporadic missing history can quietly shrink the usable date range for everyone.

### G. Method Comparison

Four candidate methods are compared side by side (`METHOD_STRUCTURAL`, `METHOD_DATA_OPTIMISED`, `METHOD_DATA_OPTIMISED_EXCL`, `METHOD_USER_SELECTED`):

| Method | Controls used | Model fit with |
|---|---|---|
| **Structurally Matched Controls** | The regions chosen by the Tab 1 matching strategy | Elastic Net (regularised, but on a small pre-selected set) |
| **Data-Optimised Controls** | All non-test regions in the market are offered as candidates | LASSO (hard variable selection — picks a subset via zero/non-zero coefficients) |
| **Data-Optimised Controls (Excluding Force-Exclude Regions)** | All non-test regions **except** any force-excluded regions | LASSO |
| **User Selected Test and Control** | Exactly the regions the user picked manually | Elastic Net |

> ⚠️ **Caveat on Data-Optimised Controls**: because the data-optimised methods select controls purely to fit the pre-period KPI series, they can achieve a strong-looking pre-period fit while selecting controls that are **less structurally plausible** than a human or structural match would choose (e.g. geographically or demographically dissimilar regions that simply happened to correlate historically). A good pre-period fit is not, by itself, a reason to prefer data-optimised controls over structurally matched ones. Before trusting or preferring a data-optimised result, check: contamination risk (is the selected control genuinely independent of the test regions and the intervention?), explainability (is there a plausible reason this control should track the test region, or is the relationship coincidental?), rolling-origin stability (does the fit hold up out-of-sample, not just in-sample?), residual diagnostics (is the fit free of concerning autocorrelation?), and overfitting/placebo results (is the apparent uplift distinguishable from noise?).

Results are computed via `run_validation_method()`. The **Method Comparison table** shown in the UI displays **methods as columns and metrics as rows**, grouped into clearly labelled sections:

**A. CONTROL SELECTION**
- **Control Pool Size** — how many candidate controls were available to the method.
- **Controls Selected** — how many controls ended up with a non-zero coefficient (i.e. were actually used by the model). For Elastic Net methods this can be less than the full pool if coefficients shrink to (near) zero.
- **Selected Features** — the count of selected *model features*, not just selected base regions. If lagged controls are enabled, a region's same-week term and its lag-1 term are counted as two separate features if both have a non-zero coefficient (see §H and §E below).

**B. PRE-PERIOD FIT** *(in-sample — indicative only)*
- **Pre-Period Correlation** (`corr`): correlation between actual and model-predicted pre-period KPI. *Higher is better*, but correlation alone can be misleadingly high even for a poor forecasting model — always read alongside error metrics.
- **Pre-Period R²** (`r2`): proportion of pre-period variance explained. *Higher is better*, but a high in-sample R² can mask overfitting or ignore autocorrelated errors — it says nothing about out-of-sample accuracy by itself.
- **Pre-Period sMAPE (%)** (`smape`): symmetric mean absolute percentage error of the pre-period fit. *Lower is better*; scale-free, so useful for comparing across KPI magnitudes.
- **Pre-Period RMSE** (`rmse`): root-mean-squared error of the pre-period fit, in the KPI's own units. *Lower is better*; penalises large errors more heavily than sMAPE.

**C. RESIDUAL DIAGNOSTICS**
- **Durbin-Watson** (`dw_stat`, via `durbin_watson_stat()`): tests whether pre-period residuals are autocorrelated. **~2.0** = little autocorrelation (better); **< 2** suggests positive autocorrelation (errors cluster); **> 2** suggests negative autocorrelation. Strong autocorrelation is a warning sign that the model is missing time-based structure (trend, delayed effects, seasonality) that a simple regression doesn't capture — the standard errors/intervals may then understate true uncertainty.

**D. ROLLING-ORIGIN VALIDATION** *(out-of-sample — the primary signal)*
- **Rolling-Origin sMAPE (%)**: average out-of-sample percentage error, from repeatedly training on an expanding window and forecasting a short horizon ahead (default horizon matches the placebo window length), starting only once `min_training_weeks` of history is available. *Lower is better.* This is a more honest measure of forecast quality than the in-sample pre-period metrics above, because it never lets the model see the data it's being scored on.
- **Rolling-Origin sMAPE — Worst Case (P90)**: the 90th percentile sMAPE across rolling-origin folds — how bad the worst ~10% of forecast windows were. *Lower is better.* A method can have a good average sMAPE but a concerning P90 if it's unstable in certain periods.
- **Rolling-Origin RMSE**: average out-of-sample error in raw KPI units, across rolling-origin folds. *Lower is better.*
- **Rolling-Origin Bias (%)**: the average tendency of the model to over-predict or under-predict the counterfactual, across rolling-origin folds. *Closer to zero is better.* A model that consistently under-predicts will overstate uplift, and vice versa.

Rolling-origin validation is the **primary signal** for whether the counterfactual relationship is likely to generalise to a real test period — pre-period fit (section B) is only an in-sample sanity check and should not be weighted as heavily.

**E. OVERFITTING DIAGNOSTICS**

Overfitting means a method can look strong in the pre-period but fail to generalise to unseen historical windows — a classic sign of fitting coincidental historical patterns rather than a genuine relationship. This is **especially relevant for data-optimised methods**, because they search across many possible controls and can find relationships that happened to correlate historically for no meaningful reason.

- **Overfit Gap sMAPE (pp)** (`overfit_gap_smape`, via `calculate_overfit_gap()`): calculated as **Rolling-Origin sMAPE minus Pre-Period sMAPE**, in percentage points. *Closer to zero (or negative) is better.* A large positive gap means out-of-sample performance is materially worse than in-sample performance — the pre-period fit was flattering and doesn't hold up.
- **Overfit Gap RMSE**: the same idea as the sMAPE gap, but in raw KPI units (Rolling-Origin RMSE minus Pre-Period RMSE). *Closer to zero (or negative) is better.*
- **Selected Features / Pre Weeks** (`feature_density`, via `calculate_feature_density()`): the number of selected model features divided by the number of pre-period weeks available. *Lower is better.* If lagged controls are enabled, same-week and lag-1 terms count as separate selected features (see §H). A high feature density means the model has relatively many parameters for the amount of history available, which increases the risk of fitting noise.
- **Overfitting Risk** (`overfitting_risk`, via `classify_overfitting_risk()`): a combined **Low / Moderate / High / Insufficient data** summary, based on the overfit gap, feature density, and rolling-origin bias together. Treat "High" results with real caution, especially for data-optimised methods.

  **Current risk logic** (see the function docstring for the authoritative version):
  - **"Low"** is only shown when rolling-origin validation produced a usable overfit gap **and** the diagnostics are not concerning — a low feature density alone is not sufficient to call the risk "Low" if the overfit gap itself is unavailable.
  - **"Moderate" / "High"** are shown when the overfit gap, feature density, or rolling bias are concerning (roughly: overfit gap > 3pp is at least Moderate, > 8pp is High; feature density > 0.30 bumps risk up a level, > 0.50 is High; rolling bias > 10% in magnitude bumps risk up a level).
  - **"Insufficient data"** is shown when there isn't enough rolling-origin validation evidence to assess out-of-sample generalisation — specifically, when the overfit gap is missing **and** feature density is missing, or when the overfit gap is missing and feature density is low (≤ 0.30). A low feature density by itself only means the model isn't obviously too complex for the data; it does **not** prove the model generalises out-of-sample, so the app deliberately avoids calling this "Low."
  - If rolling-origin validation is missing **but** feature density is high (> 0.30), the app can still flag **Moderate** or **High** risk from feature density alone, since a high feature density is informative even without an overfit gap.
  - **Treat "Insufficient data" as a caution flag, not a good result** — it means overfitting could not be properly assessed, not that there is no risk of it.

**F. PLACEBO TESTING** *(historical fake-test windows)*
- **Placebo Windows Run**: how many historical fake-test windows were evaluated (capped at ~20 for performance).
- **Typical Placebo Uplift**: the median "fake" uplift across placebo windows — ideally close to zero.
- **Placebo Uplift Range (95%)**: the middle 95% of placebo (fake-test) uplift values, shown as a `lower to upper` range (e.g. `0.1% to 21.0%`). A real observed uplift that falls **inside** this range is not clearly distinguishable from historical noise.
- **Placebo Forecast Error — avg sMAPE / worst case (P95)**: the typical and worst-case forecast error across placebo windows — a secondary check on how stable the model is across different historical periods.

**G. OBSERVED UPLIFT VS PLACEBOS** *(evaluation mode only, when a real test period is defined)*
- **Observed Uplift Percentile vs Placebos**: where the real observed uplift ranks relative to the distribution of historical placebo (fake-test) uplifts. A higher percentile is stronger evidence that the observed uplift is unusual relative to pre-period noise.
- **Observed Uplift p-value**: the proportion of placebo uplifts as extreme as (or more extreme than) the real uplift (two-sided). This is an **approximate, non-parametric extremeness check** — a description of how unusual the real uplift looks against the placebo distribution — not a formal significance test in the classical statistical sense. It offers evidence consistent with an effect, not proof, and should not be reported as formal statistical significance.
- **Observed Uplift z-score**: how many placebo-uplift standard deviations the real uplift is from the placebo mean.

**H. TEST IMPACT** *(evaluation mode only, when a real test period is defined)*
- **Observed Uplift**: actual minus predicted counterfactual, summed over the test period, in raw KPI units.
- **Observed Uplift (%)**: the same uplift expressed as a percentage of the predicted counterfactual total.
- **Test Period Actual (total)** / **Test Period Counterfactual (total)**: the raw totals behind the uplift figure, for reference.

> ℹ️ **Placebo ranges are empirical, historical fake-test ranges — not Bayesian credible intervals or predictive intervals.** They come from repeatedly re-running the model on real historical pre-period data with no actual intervention, not from posterior sampling. Don't confuse a "Placebo Uplift Range (95%)" with the Bayesian TBR tab's 94% HDI / predictive intervals (§K/§L) — they answer related but different questions using different methods.

**Range formatting**: every range shown in the Method Comparison table (and elsewhere in the app, via the shared `format_range()` helper) uses a consistent `lower to upper` style, e.g. `0.1% to 21.0%` or `-3.3% to 16.1%` — never bracket-style formatting like `[0.1%, 21.0%]`. Missing or non-finite values render as `N/A`.

**Interpretation guidance — Validate Test Design mode:**
1. **Start with rolling-origin validation** (section D) — it tests the model on unseen historical windows and is the primary signal for whether the control group is a credible counterfactual.
2. **Use pre-period fit (section B) as a sanity check only** — a model can fit the pre-period well and still fail out-of-sample.
3. **Check overfitting diagnostics (section E) before trusting a method**, especially data-optimised methods — a strong pre-period fit paired with a large overfit gap or high feature density is a warning sign, not a reassurance.
4. **Review placebo testing (section F)** to understand how much apparent uplift can occur historically without any real intervention — this sets expectations for what a credible future result would need to clear.
5. **Treat `Insufficient data` in Overfitting Risk as a caution flag, not a good result** — it means the model's out-of-sample reliability simply hasn't been established yet, which is different from having established that it's low-risk.

**Interpretation guidance — Measure Test Impact mode:**
1. **Before trusting the observed uplift, verify the model can predict the test KPI reliably.** Rolling-Origin sMAPE, Rolling-Origin Bias, Overfitting Risk, and Placebo Uplift Range (95%) are the key trust checks.
2. **If Overfitting Risk is "High," treat the uplift with extra caution** — the model's apparent fit may not reflect genuine predictive ability.
3. **If Overfitting Risk is "Insufficient data," also treat the uplift cautiously** — the model could not be properly tested out-of-sample, so its reliability is unknown rather than confirmed safe.
4. **If the observed uplift sits inside the Placebo Uplift Range (95%), it may not be distinguishable from normal historical noise.**
5. **If the observed uplift is clearly outside the placebo range, that is stronger evidence that the result is unusual relative to historical noise — not proof of causality.** Combine this with the MCMC/Bayesian diagnostics (§M) and the causal-interpretation caveat in §1 before reporting a result as a genuine effect.

### H. Lagged Controls

- **What it does**: an optional checkbox ("Include 1-week lagged controls") that, when enabled, adds each control region's **previous week's** KPI as an additional predictor (`add_lagged_control_features`), alongside its same-week value. Applies to Validate Test Design, Measure Test Impact, and Bayesian TBR.
- **Why it may help**: if the test region's KPI responds to control-region movements with a short delay (e.g. shared seasonality or a slow-moving regional trend that shows up in the test region a week later), a lag-1 feature can capture that relationship better than the same-week value alone.
- **How lagged features are created**: `add_lagged_control_features()` sorts the model matrix by date, and for each control creates a `{control}_lag1` column via `.shift(1)`. In `run_validation_method`, this is done on a **combined pre + test/post matrix built once**, so the very first test-period week can still use the last pre-period week's value as its lag (rather than losing that row).
- **Why enabling lags drops the first available row**: the first row in any continuous date series has no "previous week" to reference, so its lag value is `NaN` and — per `add_lagged_control_features`'s `dropna` step — that row is dropped from the model matrix entirely.
- **Useful when**: there's a plausible, consistent short (1-week) delay in how control-region movements show up in the test region, and there's enough pre-period history to spare a row and still fit reliably.
- **May overcomplicate when**: the pre-period is already short (losing a row matters more), the number of controls is large (doubling the feature count increases overfitting risk for LASSO/Elastic Net), or there's no substantive reason to expect a delayed relationship. Always check rolling-origin validation with and without lags before committing to lagged features.

### I. Bayesian Time-Based Regression (TBR)

Tab 4. Builds a Bayesian linear regression of the test-region aggregate KPI on the selected controls' KPI, fit with **PyMC** (imported lazily inside the tab).

- **Controls used**: Bayesian TBR uses **the selected controls from whichever validation method the user has chosen** (Structurally Matched, Data-Optimised, Data-Optimised excluding force-excluded regions, or User Selected) — not automatically "all non-test regions." For `METHOD_STRUCTURAL` / `METHOD_USER_SELECTED`, this is the raw matched/manually-picked group; for the data-optimised methods, it's `selected_regions` from the LASSO/Elastic Net fit (falling back to `control_list` if `selected_regions` is empty). In practice the validation method is chosen in the Measure Test Impact tab, but the important point is that Bayesian TBR always reads from the currently selected method's result rather than an independent "all regions" default.
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

- **What the option does**: instead of using a flat/weak prior (e.g. `Normal(0, 0.5)`) for every control coefficient, `calculate_structural_prior_sigmas()` computes a prior standard deviation (sigma) per control **based on how structurally similar that control is to the test region**.
- **How it works**: structural distance/similarity between each control and the test group (using the same population-weighted feature machinery as Tab 1 matching) is mapped to a sigma between a `min_sigma` and `max_sigma` bound — more structurally similar controls get a **wider** (less restrictive) prior sigma, less similar controls get a **narrower** (more restrictive) one.
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

> ⚠️ **Implementation check**: confirm in the current code that the pre-period band is genuinely built from fitted-mean posterior samples (no added observation noise) while the test/post bands are built from posterior *predictive* samples (fitted mean + simulated `sigma` noise). If the code instead uses posterior predictive samples (with noise) for **all** periods, or uses fitted-mean-only samples for all periods, the chart's shaded bands, legend labels, and any "HDI" / "predictive interval" wording should be updated so they match whichever calculation is actually being plotted — the label and the underlying calculation must never be mismatched, since the two interval types are not interchangeable and using the wrong one changes how confidently a period's result can be read.

The chart supports interpretation, but **the uplift summary cards remain the primary readout for total impact** (see §L) — the chart is for visually sanity-checking fit and spotting periods of concern, not for reading off a precise number.

### L. Bayesian Summary Cards

- **Posterior mean uplift** (`mean_uplift`): the average uplift across posterior samples of (actual test-period total − simulated counterfactual test-period total).
- **Uplift percentage** (`uplift_pct`): mean uplift as a percentage of the mean predicted counterfactual total.
- **94% credible interval / HDI for uplift** (`uplift_hdi_lower` / `uplift_hdi_upper`): **intended** to be calculated from the fitted counterfactual mean only (no observation noise) — narrower, and representing uncertainty in the *average* effect.
- **94% predictive interval for uplift** (`uplift_pi_lower` / `uplift_pi_upper`): **intended** to be calculated from posterior predictive counterfactual totals (includes observation noise) — intended as the **primary/headline** interval shown on the summary card, since it would be the more honest representation of plausible real-world outcomes.
- **P(Uplift > 0)** (`prob_pos`): the proportion of uplift posterior samples that are positive. Whether these samples are fitted-mean-only or posterior predictive should match the interval definition used elsewhere on the card (see implementation check below).

> ⚠️ **Implementation check**: as with the chart bands (§K), verify that `uplift_hdi_lower`/`uplift_hdi_upper` are genuinely computed from fitted-mean-only samples and `uplift_pi_lower`/`uplift_pi_upper` from posterior-predictive (noise-included) samples in the current code, and that the card labels match whichever calculation each figure actually reflects.

**Important caveats**:
- **P(Uplift > 0) is not a frequentist p-value.** It is a direct Bayesian probability statement ("given the model and data, there's an X% chance the true effect is positive"), not the probability of observing data this extreme under a null hypothesis of no effect. Don't describe it using p-value language.
- **A wide interval means high uncertainty** — a positive mean uplift is not, by itself, strong evidence if the interval is wide.
- **A positive mean uplift with an interval that crosses zero should be treated cautiously** — it's consistent with a real positive effect, but also consistent with no effect (or even a negative one), given the model's uncertainty.

### M. MCMC Diagnostics

Computed by `summarize_mcmc_diagnostics()` from the ArviZ posterior summary:

- **R-hat**: measures whether multiple MCMC chains have converged to the same distribution. The app flags R-hat > 1.01 as a concern (`rhat_ok`).
- **Effective Sample Size (ESS)**: how many "effectively independent" posterior samples were obtained, accounting for autocorrelation within chains. The app flags ESS below `CONFIG["ess_min_threshold"]` (default 500, described in code as a "softer threshold", was 1000) as a concern (`ess_ok`).
- **MCSE / SD ratio**: Monte Carlo Standard Error relative to the posterior standard deviation — a measure of how much sampling noise is affecting the posterior estimate itself. The app flags a ratio ≥ 10% as a concern (`mcse_ok`).
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
- Holdout bias (does the model systematically over- or under-predict in the rolling-origin holdout folds — see `bias_pct` in `rolling_origin_folds`, which is already computed but could be surfaced more prominently as a diagnostic in its own right).

### O. Placebo Testing

(See also §G for the specific metrics.)

- **What placebo windows are**: repeated "fake tests" run entirely within the pre-period, where a window of the same length as the real test (or a user-set `placebo_length_weeks`) is held out, a model is refit on the data before it, and a "fake uplift" is measured — even though nothing actually happened during that window.
- **Why placebo windows should usually match the test-window length**: the goal is to characterise "how much apparent uplift could arise from noise alone, over a period of this length" — a mismatched window length would answer a different (less relevant) question.
- **Why enough pre-period data is needed**: each placebo window needs (a) enough prior history to train a stable model (`min_training_weeks`) and (b) a full window of held-out data afterward — short pre-periods yield few, less-independent placebo windows, so the placebo distribution becomes less reliable as an evidence base.
- **How placebo uplift helps contextualise real uplift**: if the real observed uplift sits comfortably outside the range of "fake" uplifts the model produces on data with no real effect, that's evidence consistent with a real effect — the result is unusual relative to historical noise, not just another noisy fluctuation.
- **Interpreting percentile rank, p-value, z-score**: see §G — a high percentile rank / low p-value / large-magnitude z-score all point the same direction (the real uplift is unusual relative to placebo noise). Treat these as **approximate, non-parametric extremeness checks** that broadly agree with each other — evidence consistent with an effect, not proof of statistical significance — so don't rely on just one, and don't over-interpret a borderline value as definitive.
- **Limitations with too few placebo windows**: with very few windows (the app caps the number of windows evaluated for performance, e.g. to about 20 even when more are technically available), the placebo distribution can be lumpy/unstable, and single-digit sample sizes make any percentile/p-value estimate quite rough — treat placebo evidence from a short pre-period as suggestive, not definitive.

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
4. Does the pre-period actual-vs-predicted fit look good, visually and numerically (as a sanity check only)?
5. **Are Rolling-Origin sMAPE and RMSE acceptable** for this KPI's scale and volatility?
6. **Is Rolling-Origin Bias small** (the model isn't systematically over- or under-predicting)?
7. Is Durbin-Watson close to 2 (residuals not strongly autocorrelated)?
8. **Is Overfitting Risk "Low," or at least not "High"?** A method with a large overfit gap or high feature density relative to pre-period weeks should be treated with extra caution.
9. **If Overfitting Risk is "Insufficient data," is that uncertainty clearly reported** rather than treated as equivalent to "Low"?
10. **Is the Placebo Uplift Range (95%) narrow enough for the expected effect size?** A wide range means the design may lack power to detect a realistic effect.
11. **Does the observed uplift sit outside the Placebo Uplift Range (95%)?**
12. **Are the selected features reasonable relative to the number of pre-period weeks** (Selected Features / Pre Weeks not unusually high)?
13. Do Bayesian MCMC diagnostics look healthy (R-hat, ESS, MCSE/SD all within thresholds)?
14. Is the uplift large relative to its uncertainty (predictive interval doesn't comfortably straddle zero, or if it does, is that being reported honestly)?

If several of these checks fail, treat the resulting uplift estimate as **indicative at best**, and say so explicitly when reporting it.

### R. Known Limitations

- **The app does not prove causality by itself.** It supports a quasi-experimental, evidence-based judgement — not a randomised controlled trial with guaranteed causal identification.
- **Good pre-period fit does not guarantee a valid post-period counterfactual.** The relationship between test and control regions can change for reasons unrelated to the treatment (a competitor's campaign, a local event, a structural market shift) — pre-period validation reduces but does not eliminate this risk.
- **Control regions may be contaminated** by national campaigns, spillover from the test regions, or other simultaneous changes — the app cannot detect contamination it isn't told about; force-exclude is a manual safeguard, not an automatic one.
- **Small KPI volumes create noisy estimates** — regions or metrics with low absolute counts will have proportionally larger random fluctuation, making both structural and time-series matching less reliable.
- **Few pre-period weeks reduce reliability** across the board — fewer degrees of freedom for model fitting, fewer/less-independent rolling-origin folds, fewer/less-independent placebo windows, and less basis for detecting autocorrelation.
- **Too many controls, or lagged controls, can overfit** — especially with a short pre-period; more predictors relative to observations increases the risk that the model fits noise rather than signal, even with LASSO/Elastic Net regularisation. The Overfitting Risk diagnostic (§E) is intended to help surface this, but is itself only as good as the rolling-origin evidence behind it.
- **Structural features may be stale or incomplete** — the underlying demographic workbook is a snapshot and may not reflect current population/demographic reality, especially in fast-changing markets.
- **Bayesian results depend on model assumptions and priors** — a linear model with (approximately) Normal errors and the chosen prior structure; if the true relationship is meaningfully non-linear, or priors are poorly chosen, results will be biased accordingly. Structurally informed priors are a soft nudge, not a guarantee of correctness.
- **Post-period interpretation can be complicated by delayed effects or rebound** — a treatment's effect may not stop cleanly at test end (carryover) or may show artificial "rebound" patterns that are hard to distinguish from genuine post-test dynamics without more sophisticated modelling.
- **Model selection risk**: comparing many control-selection methods, date windows, or settings can lead to overfitting or cherry-picking if users choose the setup that gives the most favourable result after seeing the output. The preferred setup should be chosen based on pre-period diagnostics and business plausibility, not just the largest uplift. In particular:
  - Comparing many methods and then choosing the one with the largest uplift can lead to cherry-picking a result that happens to look best rather than one that is genuinely most reliable.
  - Data-optimised control selection can overfit historical noise if users rely too heavily on in-sample (pre-period) fit rather than rolling-origin and overfitting diagnostics.
  - Insufficient rolling-origin data means the model's out-of-sample reliability is **unknown**, not safe — "Insufficient data" should never be treated as equivalent to "Low risk."
  - Placebo testing is useful but limited when there are few historical windows — a tight-looking placebo range built from very few windows is less trustworthy than one built from many.

---

## 5. Developer Notes

### 5.1 Code structure (top-to-bottom)

The app is a single Streamlit script (no separate modules) organised roughly as:

1. **Imports & app config** — `st.set_page_config`, `CONFIG` dict of tunable constants, `DATA_PATH`, method-name constants (`METHOD_STRUCTURAL`, etc.).
2. **Time-series validation helpers** — KPI loading/reshaping, model matrix building, lag features, Durbin-Watson, error metrics, MCMC diagnostic summary, structural prior sigma calculation, rolling-origin validation, placebo analysis, and the main `run_validation_method()`.
3. **Text/data cleaning helpers** — `repair_text_value`, `clean_dataframe_text`, `normalise_column_names`, `inspect_excel_sheet`.
4. **Excel workbook loading** — `get_workbook_sheet_names`, `load_market_sheet`, population/geography/grouping-column helpers, `prepare_market_dataframe`.
5. **Aggregation helpers** — `weighted_average_vectorized`, `aggregate_market_data`, `impute_missing_features`.
6. **Matching metric helpers** — `weighted_profile`, `fit_structural_stats`, `calculate_metrics` (+ cached variant), `preprocess_data`, `stochastic_genetic_search`.
7. **UI/session-state utility functions** — validation of uploaded data (`validate_data`), state-reset callbacks (`reset_results`, `reset_manual_results`, `cleanup_session_state`), display formatting helpers, `calculate_experiment_population_coverage`.
8. **Guided experiment group search** — `find_guided_test_group`.
9. **Sidebar** — market/geography-level selectors.
10. **Tab 1 (Region Matching)** — test/control group setup UI, the main matching loop (greedy / hill-climbing / stochastic branches), results display (pool-size optimisation chart, convergence chart, feature distribution detail).
11. **`render_time_series_validation(mode)`** — the shared function powering both Tab 2 ("Design") and Tab 3 ("Evaluate"): settings UI (date windows, lag checkbox, placebo/rolling-origin settings), the run button, calls into `run_validation_method()` for each comparison method, results display (per-method control selection details, Method Comparison table with interpretation help), and validation-result staleness handling.
12. **Tab 4 (Bayesian TBR)** — control selection from the chosen validation method, structural/weak prior setup, the PyMC model definition and sampling, posterior/posterior-predictive computation (intended to distinguish HDI vs. predictive intervals — verify against code), summary cards, line chart, uplift histogram, MCMC diagnostics table, and the "how to interpret" text.
13. **Sidebar footer** — data quality check summary.

### 5.2 Key functions (by name)

- **`load_and_reshape_kpi(uploaded_file)`** — reads the uploaded KPI Excel, melts wide date columns into a long `(region_raw, metric_name, date, kpi)` table, coerces types, and drops invalid/missing rows.
- **`build_region_mapping(df_long, test_regions_val, control_regions_val, adobe_to_geo)`** — maps raw uploaded region labels to the app's internal geography names, using the Adobe reference mapping first and falling back to a direct name match against known test/control regions.
- **`apply_geo_aggregation(df_long, geo_col)`** — sums KPI values by `(date, region)`, collapsing any raw regions that map to the same geography.
- **`build_model_matrix(agg_df, control_list, test_regions)`** — builds the wide `date | test_kpi | control_1 | control_2 | ...` matrix used for all time-series model fitting; inner-joins on date and drops rows with any missing value.
- **`add_lagged_control_features(model_df, control_list, lags=(1,))`** — sorts by date, adds `{control}_lag1` columns via `.shift(1)` for each control, drops rows with missing lag values, and returns the expanded feature list plus a same-week/lag-1 feature map.
- **`durbin_watson_stat(residuals)`** — a manual (no-`statsmodels`-dependency) implementation of the Durbin-Watson statistic for residual autocorrelation.
- **`calculate_overfit_gap(pre_smape, rolling_smape)`** — calculates the gap between rolling-origin (out-of-sample) error and pre-period (in-sample) error as `rolling_smape - pre_smape`. Returns `np.nan` if either input is missing, non-finite, or not convertible to a float.
- **`calculate_feature_density(n_selected_features, n_pre_weeks)`** — calculates the number of selected model features divided by the number of pre-period weeks (`n_selected_features / n_pre_weeks`). Returns `np.nan` if inputs are missing or `n_pre_weeks <= 0`.
- **`classify_overfitting_risk(overfit_gap_smape, feature_density, rolling_bias_pct=None)`** — classifies overfitting risk as `"Low"`, `"Moderate"`, `"High"`, or `"Insufficient data"` from the overfit gap, feature density, and optionally rolling-origin bias. Deliberately **avoids labelling risk as `"Low"` when rolling-origin validation is missing** — a missing overfit gap combined with a low feature density returns `"Insufficient data"` rather than `"Low"`, since a simple model isn't proof that it generalises out-of-sample. See §E for the full current logic.
- **`format_range(lower, upper, suffix="", decimals=1)`** — formats a `(lower, upper)` pair consistently as `"{lower}{suffix} to {upper}{suffix}"` (e.g. `"0.1% to 21.0%"`), used throughout the Method Comparison table (and elsewhere) so ranges never mix bracket-style and "to"-style formatting. Returns `"N/A"` for missing or non-finite values.
- **`run_validation_method(agg_df, control_list, test_regions, method_name, pre_start, pre_end, test_start, test_end, use_post, post_start, post_end, compute_uplift, placebo_length_weeks, min_training_weeks, include_lagged_controls)`** — the central validation function. Builds a combined pre+test/post model matrix (once, so lagged features work across period boundaries), fits an Elastic Net or LASSO model (`method_name` is `"enet"` or `"lasso"`) on the pre-period, computes fit metrics and Durbin-Watson, runs rolling-origin validation, computes the overfit gap and feature density/overfitting risk (selected features are counted from the model's actual non-zero-coefficient features, so same-week and lag-1 terms are counted **separately** when lagged controls are enabled — see §H), computes test/post predictions and uplift, runs the placebo loop, and returns a large dict of results including: rolling-origin metrics (mean sMAPE/RMSE, P90 sMAPE, bias), overfit gap sMAPE/RMSE, selected features and feature density, overfitting risk, placebo uplift range and forecast-error metrics, observed-uplift-vs-placebo metrics (percentile rank, p-value, z-score), and test-impact totals when evaluation mode is used. See the function's `return` statement for the authoritative full list of keys — it includes both raw arrays for charting and summary scalars for the comparison table.
- **`rolling_origin_validation(X, y, horizon, min_training_weeks, dates, n_splits, model_type)`** — expanding-window backtest: repeatedly trains on all data up to a point and forecasts the next `horizon` periods, recording sMAPE/RMSE/bias/uplift-error per fold (capped at ~20 folds for performance).
- **`placebo_analysis(uplift_list, real_uplift)`** — given a list of placebo "fake" uplifts and the real uplift, computes median, 95% range, percentile rank, one- and two-sided p-values, and z-score. (Note: the placebo loop inside `run_validation_method` computes these inline rather than always calling this standalone function directly — check both if extending placebo logic, as the standalone function may not be the sole/current source of truth for placebo statistics shown in the UI.)
- **`calculate_structural_prior_sigmas(...)`** — computes a structural-distance-based prior sigma per control region, scaled between a min/max bound, for use as the Bayesian model's coefficient prior widths.
- **Structural matching functions** — `weighted_profile` (population-weighted feature means), `fit_structural_stats` (fits one mean/std basis per run), `calculate_metrics` (computes Weighted Structural Distance and Mean Abs SMD for a candidate test/control pairing), `preprocess_data` (prepares scaled arrays for `NearestNeighbors`-based candidate search, using the same basis as `calculate_metrics` for consistency).
- **`stochastic_genetic_search(pool_df, test_df_run, active_features, weights, n, calculate_metrics_fn, eligible_means, eligible_stds, nn_start_idx, n_iterations, random_state)`** — the Advanced matching strategy: starts from a nearest-neighbour candidate group and performs randomised single-region swaps, keeping any that improve Weighted Structural Distance, tracking the best group found; seeded for reproducibility.
- **Bayesian TBR section** (inline in Tab 4 / the Bayesian TBR workflow area, not a standalone function) — defines the PyMC model (`intercept`, `coeffs` with per-control prior sigma, `sigma`, `y_obs`), samples via `pm.sample(...)`, and derives posterior counterfactual estimates, uplift samples, uncertainty intervals, chart data, summary cards, and diagnostics. The **intended interval convention** is to use fitted-mean posterior samples for the pre-period HDI and posterior predictive samples (fitted mean plus simulated `sigma`-scaled noise) for the test/post predictive intervals and the primary uplift interval, with a secondary fitted-mean-only uplift interval for comparison — but future maintainers should verify that the code's actual interval calculations and labels (`mu_pre_samples`/`mu_test_samples`/`mu_post_samples` vs. any posterior-predictive-with-noise variables, and the corresponding HDI/predictive-interval labels) match this convention before relying on it.

### 5.3 Session state usage

The app relies heavily on `st.session_state` to persist state across Streamlit reruns and between tabs, including (non-exhaustive):

- Matching results and convergence data from Tab 1.
- `st.session_state.include_lagged_controls` — the shared lagged-controls flag, plus the setting is also stored inside the saved `validation_results` dict so Bayesian TBR reads a value consistent with the validation run it's built on.
- `st.session_state.validation_results` — the dict of per-method results from the last `render_time_series_validation` run; cleared (`clear_validation_state`) when settings that would invalidate it change (e.g. toggling the lag checkbox), forcing a re-run before Bayesian TBR can use stale controls.
- `st.session_state.bayesian_results` — the dict of Bayesian TBR outputs (posterior samples, intervals, chart data, diagnostics).
- `st.session_state.eligible_means` / `eligible_stds` — the fixed structural-matching basis for the current run.
- `st.session_state.force_ctrl_exclude` and related force-include/exclude selections.
- Various reset callbacks (`reset_results`, `reset_manual_results`, `cleanup_session_state`) fire on relevant widget `on_change` events to invalidate stale downstream state when upstream inputs change.

### 5.4 Caching

`@st.cache_data(ttl=CONFIG["cache_ttl"])` (default 3600s / 1 hour) is used on:
- `get_workbook_sheet_names`, `load_market_sheet` — avoid re-reading the Excel workbook on every rerun.
- `aggregate_market_data` — avoid recomputing weighted aggregation repeatedly.
- `calculate_metrics_cached`, `preprocess_data` — cache structural-matching computations keyed on (hashable) tuples of inputs.

Note that Streamlit's caching requires hashable arguments — this is why several functions accept `_tuple` versions of dicts/arrays (e.g. `eligible_means_tuple`) rather than the dicts/arrays themselves.

### 5.5 Dependencies

Core: `streamlit`, `pandas`, `numpy`, `scikit-learn` (`StandardScaler`, `NearestNeighbors`, `ElasticNetCV`, `RidgeCV`, `TimeSeriesSplit`, `KFold`, `mean_squared_error`, `r2_score`), `scipy.stats`, `altair`, `plotly.express`, `random`, `warnings`, `unicodedata`, `io`.

Bayesian modelling: `pymc` and `arviz`, imported **lazily inside the Bayesian TBR tab** (not at module load time) — the code comments note this is to avoid segfaults/Numba errors at startup on some Python versions. Any change to how/when PyMC is imported should preserve this lazy-import pattern.

Excel reading uses the `calamine` engine primarily, with a fallback to `openpyxl` if `calamine` fails (`load_market_sheet`).

### 5.6 Expensive computations

- The **Advanced (Stochastic Genetic Search)** matching strategy and, more generally, testing a **range of control-group sizes** in the matching loop, both scale with the number of `calculate_metrics` calls — the most expensive part of Tab 1 for large pools / high iteration counts.
- **Placebo loops** inside `run_validation_method` fit a full Elastic Net/LASSO model per placebo window (capped at ~20 windows), and this runs for **every** comparison method — with 3–4 methods this multiplies the cost.
- **Rolling-origin validation** similarly fits a model per fold (also capped at ~20 folds).
- **Bayesian TBR sampling** (`pm.sample(draws=2000, tune=1000, chains=4, ...)`) is the single most expensive operation in the app and runs once per "Run Bayesian TBR" click — check the exact `draws`/`tune`/`chains` values in the code if tuning for speed vs. quality.

### 5.7 Where future developers should make changes

- **New matching strategies**: add a new branch in the Tab 1 matching loop (parallel to the Greedy/Hill-Climbing/Stochastic branches) and register a new `strategy_labels` entry; keep the same `opt_data` record shape so downstream charts keep working.
- **New validation methods**: extend the `METHOD_*` constants and the calls into `run_validation_method()` inside `render_time_series_validation`; make sure new methods populate the same result dict keys the Method Comparison table and Bayesian TBR tab expect (especially `selected_regions`, `control_list`, `dw_stat`, `model_feature_cols` if lag-aware).
- **New diagnostics** (Ljung-Box, ACF plot, etc.): follow the pattern of `durbin_watson_stat` — a small, dependency-light helper function, computed inside `run_validation_method` from `pre_residuals`, added to the returned dict, and surfaced in the Method Comparison table (`comparison_rows` list + the `get_value` lookup logic).
- **New Bayesian model features** (e.g. a separate lag-specific prior instead of duplicating the same-week sigma): modify the PyMC model block and `calculate_structural_prior_sigmas` usage inside the Bayesian TBR tab; keep the HDI-vs-predictive-interval distinction intact when adding new interval types.
- **Data-loading robustness**: `load_and_reshape_kpi` and `build_region_mapping` are the most likely places to need hardening if new client KPI export formats need to be supported.

---

## 6. Suggested Future Improvements

- **Ljung-Box test** for residual autocorrelation — a more general check than Durbin-Watson (tests multiple lags jointly, not just lag-1).
- **ACF (autocorrelation function) residual plot** — a visual complement to the numeric autocorrelation tests.
- **Residual drift/trend diagnostic** — explicitly check whether pre-period residuals trend up or down over time, which Durbin-Watson alone won't necessarily flag.
- **Clearer interval naming for HDI vs. predictive interval** throughout the UI and any exports — ensure any future feature (PDF export, CSV download, etc.) follows the same careful HDI/predictive-interval distinction intended for the Bayesian TBR tab (see §K/§L implementation checks), rather than reintroducing an ambiguous "94% interval" label.
- **Exportable results report** — a one-click PDF/Word/PowerPoint summary of the match quality, validation metrics, and Bayesian results for sharing with stakeholders who won't use the app directly.
- **Saved experiment configuration** — persist a given test/control/date-window/method setup (e.g. to a file or lightweight database) so a user can return to or share an exact analysis without re-selecting everything.
- **Stronger data validation for uploaded KPI files** — more explicit checks/errors for wrong column order, mixed metrics in one file, duplicate region names, or unmapped regions, surfaced clearly to the user rather than silently dropped.
- **Optional correlation-informed or blended priors** — priors that blend structural similarity (current approach) with pre-period KPI correlation, potentially giving a stronger, more directly relevant signal than structural features alone.
- **Improved speed for Bayesian placebo tests** — currently, only the LASSO/Elastic Net comparison methods run placebo tests; extending genuine placebo-style testing to the Bayesian model itself would be valuable but is currently too slow to run for many windows (each placebo window would need its own MCMC fit) — worth investigating faster approximate-Bayesian or variational approaches for this specific use case.
- **Clearer stakeholder-friendly interpretation labels** — consider adding a simplified, jargon-free summary view (e.g. "Strong evidence of a positive effect" / "Inconclusive" / "No evidence of an effect") derived from the same underlying numbers, for audiences who don't need to read MCSE ratios or Durbin-Watson statistics.

---

*This documentation describes the app as implemented at the time of writing. Where a section notes ambiguity or recommends checking the code, that reflects genuine uncertainty about exact current behaviour (e.g. UI wiring details that may change independently of the core logic) — always verify against the live code before relying on a specific claim for a production decision.*
