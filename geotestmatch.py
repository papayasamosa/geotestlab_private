import streamlit as st
import pandas as pd
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import NearestNeighbors
from scipy import stats
import altair as alt
import plotly.express as px
import random
from typing import Tuple, List, Dict
import warnings
import unicodedata
import io
import re
# pymc and arviz imported lazily inside the Bayesian tab to avoid
# segfaults and Numba errors at startup on Python 3.14


# New imports for validation module
from sklearn.linear_model import ElasticNetCV, RidgeCV, ElasticNet
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_squared_error, r2_score

warnings.filterwarnings("ignore")

# ------------------------------------------------------------
# App configuration
# ------------------------------------------------------------

st.set_page_config(page_title="TEST GeoTestLab", layout="wide")

def load_css(path: str = "styles.css") -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except FileNotFoundError:
        pass

load_css()

st.title("TEST GeoTestLab")
st.caption("Build statistically balanced test and control groups for geo-testing — no coding required.")

# ------------------------------------------------------------
# Configuration constants
# ------------------------------------------------------------

CONFIG = {
    "max_hill_climbing_swaps": 15,
    "genetic_iterations": {"min": 100, "max": 5000, "default": 1000},
    "max_control_pool_size": 50,
    "smd_thresholds": {"good": 0.20, "high": 0.50},
    "cache_ttl": 3600,
    "max_display_features": 10,
    "missing_threshold": 20,          # % missing above which we warn
    "outlier_std_threshold": 5,
    "ess_min_threshold": 500,         # softer threshold for ESS (was 1000)
    # ---- Method comparison / Counterfactual Confidence traffic-light bands ----
    # Single source of truth for the classify_* helper functions below. Durbin-Watson
    # bands are practical interpretation bands, not formal critical-value tests — see
    # classify_autocorrelation_risk().
    "reliability_thresholds": {
        "durbin_watson_low_band": (1.5, 2.5),          # 🟢 Low autocorrelation risk
        "durbin_watson_moderate_low_band": (1.2, 1.5),  # 🟡 Moderate (positive autocorrelation side)
        "durbin_watson_moderate_high_band": (2.5, 2.8), # 🟡 Moderate (negative autocorrelation side)
        "overfitting_gap_pp": {"low_max": 3, "moderate_max": 5},
        "rolling_smape_pct": {"low_max": 10, "moderate_max": 15},
        "rolling_bias_pct": {"low_max": 5, "moderate_max": 10},
    },
}

# Single source of truth for SMD thresholds: CONFIG["smd_thresholds"] is canonical;
# these module-level names exist for readability at the (many) call sites.
SMD_GOOD_THRESHOLD = CONFIG["smd_thresholds"]["good"]
SMD_HIGH_THRESHOLD = CONFIG["smd_thresholds"]["high"]

DATA_PATH = "data/Population Stats for Geo Tests - Master Sheet Only v2 (Standardised).xlsx"
POPULATION_COL_RAW = "Total Population"
POPULATION_COL = "Population"
ADOBE_COL = "Adobe Reference List"

# ------------------------------------------------------------
# Time-Series Validation helpers
# ------------------------------------------------------------

METHOD_STRUCTURAL = "Structurally Matched Controls"
METHOD_DATA_OPTIMISED = "Data-Optimised Controls"
METHOD_DATA_OPTIMISED_EXCL = "Data-Optimised Controls (Excluding Force-Exclude Regions)"
METHOD_USER_SELECTED = "User Selected Test and Control"

def load_and_reshape_kpi(uploaded_file):
    """Load KPI Excel, melt to long format, keep missing values as NaN."""
    try:
        df_raw = pd.read_excel(uploaded_file, engine="calamine", header=0)
    except Exception:
        try:
            uploaded_file.seek(0)
            df_raw = pd.read_excel(uploaded_file, engine="openpyxl", header=0)
        except Exception as e:
            st.error(
                "The KPI file could not be read with either the calamine or openpyxl engine. "
                "Please confirm this is a valid .xlsx file and try again. "
                f"(Details: {e})"
            )
            st.stop()
    region_col = df_raw.columns[0]
    metric_col = df_raw.columns[1]
    df_long = df_raw.melt(id_vars=[region_col, metric_col], var_name="date", value_name="kpi")
    df_long = df_long.rename(columns={region_col: "region_raw", metric_col: "metric_name"})
    df_long["date"] = pd.to_datetime(df_long["date"], errors="coerce")
    # Drop rows with invalid date or missing KPI (do NOT fill with 0)
    df_long = df_long.dropna(subset=["date", "kpi"])
    # Convert KPI to numeric, coercing errors -> NaN, then drop those rows
    df_long["kpi"] = pd.to_numeric(df_long["kpi"], errors="coerce")
    df_long = df_long.dropna(subset=["kpi"])
    return df_long

def build_region_mapping(df_long, test_regions_val, control_regions_val, adobe_to_geo):
    all_geomatch_regions = set(test_regions_val + control_regions_val)
    df_long["region_clean"] = df_long["region_raw"].astype(str).str.strip()
    df_long["mapped_geo"] = df_long["region_clean"].map(adobe_to_geo)
    def final_region_name(row):
        if pd.notna(row["mapped_geo"]):
            return row["mapped_geo"]
        elif row["region_clean"] in all_geomatch_regions:
            return row["region_clean"]
        else:
            return None
    df_long["region"] = df_long.apply(final_region_name, axis=1)
    return df_long

def apply_geo_aggregation(df_long, geo_col):
    agg_df = df_long.groupby(["date", "region"])["kpi"].sum().reset_index()
    return agg_df

def build_model_matrix(agg_df, control_list, test_regions):
    """
    Build the model matrix (test KPI + one column per control) for a given control list.

    Returns (model, matrix_diagnostics). matrix_diagnostics reports how many rows were
    lost to the dropna() step (e.g. because a selected control had missing KPI values for
    some dates) so callers can warn the user rather than silently losing data. Diagnostics
    are computed after the merge but before dropna().

    NOTE: this changed from returning `model` alone to returning `(model, matrix_diagnostics)`.
    All callers in this file have been updated accordingly.
    """
    test_agg = agg_df[agg_df["region"].isin(test_regions)].groupby("date")["kpi"].sum().reset_index().rename(columns={"kpi": "test_kpi"})
    control_wide = agg_df[agg_df["region"].isin(control_list)].pivot(index="date", columns="region", values="kpi").reset_index()
    merged = test_agg.merge(control_wide, on="date", how="inner").sort_values("date").reset_index(drop=True)

    rows_before_dropna = len(merged)
    # Which control columns actually have missing values (only meaningful control columns,
    # not "date"/"test_kpi").
    control_cols_present = [c for c in control_list if c in merged.columns]
    control_columns_with_missing = [c for c in control_cols_present if merged[c].isna().any()]

    model = merged.dropna().reset_index(drop=True)
    rows_after_dropna = len(model)
    rows_dropped = rows_before_dropna - rows_after_dropna
    pct_rows_dropped = (rows_dropped / rows_before_dropna * 100.0) if rows_before_dropna > 0 else 0.0

    matrix_diagnostics = {
        "rows_before_dropna": rows_before_dropna,
        "rows_after_dropna": rows_after_dropna,
        "rows_dropped": rows_dropped,
        "pct_rows_dropped": pct_rows_dropped,
        "control_columns_with_missing": control_columns_with_missing,
    }
    return model, matrix_diagnostics

def add_lagged_control_features(model_df, control_list, lags=(1,), frequency_config=None, time_series_frequency=None):
    """
    Add lagged versions of each control KPI column.

    Each control gets a `{control}_lag{lag}` feature containing that control's KPI value
    `lag` periods earlier. The target remains the current period's `test_kpi`. Rows with
    missing lagged values are dropped.

    Lag mechanics depend on frequency (pass `frequency_config` from get_frequency_config(),
    or `time_series_frequency` — "weekly"/"daily" — and it will be resolved internally;
    defaults to weekly if neither is given, preserving legacy row-shift behaviour for old
    callers):

    - Weekly (or any non-daily) mode: uses a row-based `.shift(lag)`. This is safe as long
      as the weekly series has regular, evenly-spaced rows (the normal case for this app).
    - Daily mode: uses a true *calendar-day* lag — each row is matched to the control's value
      from exactly `date - lag days` via a date-based merge, not simply "N rows earlier".
      If the exact `date - lag days` row is missing for a control (e.g. a gap in the daily
      series), the lag value is left missing and the row is dropped by the dropna step below,
      rather than silently borrowing a nearby date's value.

    Args:
        model_df: DataFrame with a "date" column, a "test_kpi" column, and one column per
                   control in control_list (as produced by build_model_matrix).
        control_list: base control region names (same-period columns already present in model_df).
        lags: iterable of integer lags (in periods — weeks or days depending on frequency) to
              add. Default (1,) adds a 1-period lag only.
        frequency_config: optional dict from get_frequency_config(); if omitted, resolved from
              time_series_frequency (defaulting to "weekly").
        time_series_frequency: optional "weekly"/"daily" string, used only if frequency_config
              is not supplied.

    Returns:
        model_df_lagged: model_df sorted by date, with additional `{control}_lag{lag}` columns,
                          and rows with missing lag values dropped.
        model_feature_cols: list of feature columns to use as model predictors — the original
                             same-period control columns followed by the lagged columns.
        lagged_feature_map: dict mapping each base control region to its same-period and lagged
                             feature column names, e.g.
                             {"Region A": {"current": "Region A", "lag1": "Region A_lag1"}, ...}
                             or, for a 7-day lag, {"lag7": "Region A_lag7"}.
        lag_drop_metadata: dict with rows_before_lag_drop, rows_after_lag_drop,
                             rows_dropped_due_to_lag, and lag_drop_pct — lets callers warn when
                             a meaningful share of rows were lost because exact lag dates were
                             missing (most relevant for the daily calendar-day lag).
    """
    if frequency_config is None:
        frequency_config = get_frequency_config(time_series_frequency if time_series_frequency is not None else "weekly")
    use_date_based_lag = frequency_config.get("frequency") == "daily"

    model_df_lagged = model_df.sort_values("date").reset_index(drop=True).copy()
    model_df_lagged["date"] = pd.to_datetime(model_df_lagged["date"])
    rows_before_lag_drop = len(model_df_lagged)
    lagged_feature_map = {}
    lag_cols_all = []

    if use_date_based_lag:
        # True calendar-day lag: match each row to the control value from date - lag days,
        # via a date-keyed merge rather than a row-position shift. Missing lag dates produce
        # NaN (dropped below), never a borrowed/nearest value.
        for c in control_list:
            lagged_feature_map[c] = {"current": c}
            for lag in lags:
                lag_col = f"{c}_lag{lag}"
                lookup = model_df_lagged[["date", c]].copy()
                lookup["date"] = lookup["date"] + pd.Timedelta(days=int(lag))
                lookup = lookup.rename(columns={c: lag_col})
                model_df_lagged = model_df_lagged.merge(lookup, on="date", how="left")
                lagged_feature_map[c][f"lag{lag}"] = lag_col
                lag_cols_all.append(lag_col)
    else:
        # Row-based shift (weekly / regular-interval default): preserves existing behaviour.
        for c in control_list:
            lagged_feature_map[c] = {"current": c}
            for lag in lags:
                lag_col = f"{c}_lag{lag}"
                model_df_lagged[lag_col] = model_df_lagged[c].shift(lag)
                lagged_feature_map[c][f"lag{lag}"] = lag_col
                lag_cols_all.append(lag_col)

    model_df_lagged = model_df_lagged.dropna(subset=lag_cols_all).reset_index(drop=True)
    rows_after_lag_drop = len(model_df_lagged)
    rows_dropped_due_to_lag = rows_before_lag_drop - rows_after_lag_drop
    lag_drop_pct = (rows_dropped_due_to_lag / rows_before_lag_drop * 100.0) if rows_before_lag_drop > 0 else 0.0

    model_feature_cols = list(control_list) + lag_cols_all
    lag_drop_metadata = {
        "rows_before_lag_drop": rows_before_lag_drop,
        "rows_after_lag_drop": rows_after_lag_drop,
        "rows_dropped_due_to_lag": rows_dropped_due_to_lag,
        "lag_drop_pct": lag_drop_pct,
    }
    return model_df_lagged, model_feature_cols, lagged_feature_map, lag_drop_metadata

def durbin_watson_stat(residuals):
    """
    Durbin-Watson statistic for residual autocorrelation.
    ~2.0 = little autocorrelation; <2 suggests positive autocorrelation; >2 suggests negative autocorrelation.
    Implemented manually (no statsmodels dependency).
    """
    residuals = np.asarray(residuals, dtype=float)
    residuals = residuals[np.isfinite(residuals)]
    if len(residuals) < 3:
        return np.nan
    denom = np.sum(residuals ** 2)
    if denom == 0:
        return np.nan
    return np.sum(np.diff(residuals) ** 2) / denom

def _is_valid_number(v):
    """
    Shared validity check used by the classify_* traffic-light helpers below.
    Returns False for None, NaN, pd.NA, and +/-inf; True for any other finite number.
    """
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        return False
    try:
        if not np.isfinite(float(v)):
            return False
    except (TypeError, ValueError):
        return False
    return True

def classify_autocorrelation_risk(dw_stat):
    """
    Short traffic-light interpretation of the Durbin-Watson statistic, for the method
    comparison table row "Autocorrelation Risk".

    Durbin-Watson is an established statistic for first-order residual autocorrelation.
    These red/amber/green bands are practical interpretation bands for this app, not
    formal critical-value tests.

    Durbin-Watson is approximately:
    - 2.0 = little/no first-order autocorrelation
    - below 2.0 = positive autocorrelation
    - above 2.0 = negative autocorrelation

    Practical diagnostic bands used here (not formal critical-value tests):
    - 🟢 Low autocorrelation risk: 1.5 to 2.5
    - 🟡 Moderate autocorrelation risk: 1.2 to <1.5, or >2.5 to 2.8
    - 🔴 High autocorrelation risk: <1.2 or >2.8
    """
    if not _is_valid_number(dw_stat):
        return "⚪ Insufficient data"

    dw = float(dw_stat)
    _t = CONFIG["reliability_thresholds"]
    _low_lo, _low_hi = _t["durbin_watson_low_band"]
    _mod_lo_lo, _mod_lo_hi = _t["durbin_watson_moderate_low_band"]
    _mod_hi_lo, _mod_hi_hi = _t["durbin_watson_moderate_high_band"]

    if _low_lo <= dw <= _low_hi:
        return "🟢 Low"
    elif (_mod_lo_lo <= dw < _mod_lo_hi) or (_mod_hi_lo < dw <= _mod_hi_hi):
        return "🟡 Moderate"
    else:
        return "🔴 High"

def calculate_overfit_gap(pre_smape, rolling_smape):
    """
    Overfitting Gap: how much worse the model performs out-of-sample (rolling-origin)
    versus in-sample (pre-period fit). A large positive gap means the model looks good
    on the data it was fitted on, but performs worse when predicting unseen historical
    periods. This is a validation diagnostic, not a formal statistical test. Returns
    np.nan if either input is missing, not finite (covers None, np.nan, pd.NA, and
    +/-inf), or not convertible to a float.

    (Internal variable/function names still use "overfit_gap" for backward
    compatibility; all user-facing labels use "Overfitting Gap".)
    """
    if not _is_valid_number(pre_smape) or not _is_valid_number(rolling_smape):
        return np.nan
    return float(rolling_smape) - float(pre_smape)

def classify_overfitting_risk(overfit_gap_smape):
    """
    Short traffic-light rating for the method comparison table row "Overfitting Risk".

    Uses ONLY the Overfitting Gap (rolling-origin sMAPE minus pre-period sMAPE) — it
    does not mix in autocorrelation, bias, rolling sMAPE, or placebo results, which
    each have their own dedicated row. Answers only: does the model look meaningfully
    worse on held-out historical validation than on the fitted pre-period?
    """
    if not _is_valid_number(overfit_gap_smape):
        return "⚪ Insufficient data"
    _t = CONFIG["reliability_thresholds"]["overfitting_gap_pp"]
    if overfit_gap_smape <= _t["low_max"]:
        return "🟢 Low"
    if overfit_gap_smape <= _t["moderate_max"]:
        return "🟡 Moderate"
    return "🔴 High"

def classify_rolling_validation_error(rolling_smape_mean):
    """
    Short traffic-light rating for the method comparison table row "Rolling Validation
    Error", based ONLY on rolling_smape_mean (absolute out-of-sample sMAPE).

    High rolling validation error means the model is inaccurate out-of-sample — this is
    not the same thing as overfitting (a model can be inaccurate everywhere, in-sample
    and out-of-sample alike, without a large gap between the two).
    """
    if not _is_valid_number(rolling_smape_mean):
        return "⚪ Insufficient data"
    _t = CONFIG["reliability_thresholds"]["rolling_smape_pct"]
    if rolling_smape_mean <= _t["low_max"]:
        return "🟢 Low"
    if rolling_smape_mean <= _t["moderate_max"]:
        return "🟡 Moderate"
    return "🔴 High"

def classify_rolling_bias_risk(rolling_bias_pct):
    """
    Short traffic-light rating for the method comparison table row "Rolling Bias Risk",
    based ONLY on rolling_bias_pct — whether the model systematically over- or
    under-predicts in held-out historical periods.
    """
    if not _is_valid_number(rolling_bias_pct):
        return "⚪ Insufficient data"
    _t = CONFIG["reliability_thresholds"]["rolling_bias_pct"]
    if abs(rolling_bias_pct) <= _t["low_max"]:
        return "🟢 Low"
    if abs(rolling_bias_pct) <= _t["moderate_max"]:
        return "🟡 Moderate"
    return "🔴 High"

def combine_reliability_ratings(component_ratings):
    """
    Derives the overall "Counterfactual Confidence" rating (🟢 High confidence /
    🟡 Moderate confidence / 🔴 Low confidence / ⚪ Insufficient data) from a dict of
    component traffic-light ratings, e.g.:
        {
            "rolling validation error": "🟢 Low",
            "overfitting gap": "🟡 Moderate",
            "rolling bias": "🟢 Low",
            "autocorrelation risk": "⚪ Insufficient data",
        }

    This is a PRIORITY-ORDERED CASCADE, not a flat "worst of four" vote. The four
    checks are not equally important: Rolling Validation Error is the primary
    model-quality check (can this model predict unseen historical data at all?), so it
    is evaluated first and acts as a gate. Overfitting, Autocorrelation Risk, and
    Rolling Bias are evaluated next, in that priority order, as secondary checks that
    can still hold confidence back but cannot single-handedly force it all the way down
    to low the way Rolling Validation Error can.

    Rule (first match wins):
    1. Rolling Validation Error 🔴  -> "🔴 Low confidence", regardless of the other
       three checks. A model that can't predict held-out history is not rescued by
       good residual diagnostics or low bias.
    2. Rolling Validation Error ⚪ (unavailable) -> "⚪ Insufficient data", regardless
       of the other three. The primary check must be available to make any judgement.
    3. Rolling Validation Error 🟡  -> "🟡 Moderate confidence". A moderate primary
       check caps confidence at moderate; it cannot reach high, and the secondary
       checks cannot pull it below moderate either.
    4. Rolling Validation Error 🟢, but Overfitting, Autocorrelation Risk, or Rolling
       Bias is 🔴 or 🟡 -> "🟡 Moderate confidence". A secondary check that is flagged
       is not ignored, but on its own it caps confidence at moderate rather than
       forcing it to low.
    5. Rolling Validation Error 🟢 and all available secondary checks are 🟢
       -> "🟢 High confidence".

    (Internal variable/function names still use "reliability" for backward
    compatibility; all user-facing labels use "Counterfactual Confidence".)
    """
    def _sym(key):
        v = component_ratings.get(key)
        return v.split(" ", 1)[0] if v else "⚪"

    validation_error_sym = _sym("rolling validation error")
    secondary_syms = [
        _sym("overfitting gap"),
        _sym("autocorrelation risk"),
        _sym("rolling bias"),
    ]

    # ---- 1-2: Rolling Validation Error is the primary gate. ----
    if validation_error_sym == "🔴":
        return "🔴 Low confidence"
    if validation_error_sym == "⚪":
        return "⚪ Insufficient data"

    # ---- 3: A moderate primary check caps confidence at moderate. ----
    if validation_error_sym == "🟡":
        return "🟡 Moderate confidence"

    # ---- 4-5: Rolling Validation Error is 🟢 — secondary checks can only hold
    # confidence back to moderate, never force it down to low. ----
    available_secondary = [s for s in secondary_syms if s != "⚪"]
    if any(s in ("🔴", "🟡") for s in available_secondary):
        return "🟡 Moderate confidence"
    return "🟢 High confidence"

def get_reliability_drivers(component_ratings):
    """
    Produces a short, human-readable explanation of what drove the Counterfactual
    Confidence rating, e.g. "Moderate overfitting gap" or
    "High rolling validation error + moderate rolling bias".

    component_ratings: dict of {short driver label: traffic-light string}, using the
    same short driver labels as combine_reliability_ratings() (e.g. "rolling
    validation error", "overfitting gap", "rolling bias", "autocorrelation risk").

    Lists ALL flagged issues, not just the one(s) that determined the overall rating —
    e.g. if Rolling Validation Error is high (which alone forces low confidence) and
    Rolling Bias is also moderate, both are shown. Issues are listed in the same
    priority order used by combine_reliability_ratings(): rolling validation error,
    overfitting gap, autocorrelation risk, then rolling bias.
    """
    overall = combine_reliability_ratings(component_ratings)
    symbols = {k: v.split(" ", 1)[0] for k, v in component_ratings.items() if v}

    if overall == "🟢 High confidence":
        return "Validation checks passed"
    if overall == "⚪ Insufficient data":
        return "Insufficient validation data to assess confidence"

    priority_order = [
        "rolling validation error",
        "overfitting gap",
        "autocorrelation risk",
        "rolling bias",
    ]
    reds = [f"high {k}" for k in priority_order if symbols.get(k) == "🔴"]
    yellows = [f"moderate {k}" for k in priority_order if symbols.get(k) == "🟡"]
    drivers = reds + yellows
    fallback = "validation checks failed" if overall == "🔴 Low confidence" else "elevated validation risk"
    detail = " + ".join(drivers) if drivers else fallback
    return f"{detail[:1].upper()}{detail[1:]}"

# ------------------------------------------------------------
# Frequency-awareness helpers (weekly vs daily time series)
# ------------------------------------------------------------
def get_frequency_config(time_series_frequency):
    """
    Return a dict of frequency-aware settings for the given time series frequency.

    Args:
        time_series_frequency: "weekly" or "daily". Anything else falls back to "weekly".

    Returns:
        dict with keys:
            frequency: "weekly" or "daily"
            period_label_singular: "week" or "day"
            period_label_plural: "weeks" or "days"
            lag_periods: 1 for weekly, 7 for daily
            lag_label: "1-week" or "7-day"
            default_min_training_periods: sensible default minimum training window
            default_validation_horizon_periods: sensible default rolling-origin / placebo horizon
            default_placebo_length_periods: sensible default placebo/test window length
    """
    if time_series_frequency == "daily":
        return {
            "frequency": "daily",
            "period_label_singular": "day",
            "period_label_plural": "days",
            "lag_periods": 7,
            "lag_label": "7-day",
            "default_min_training_periods": 84,
            "default_validation_horizon_periods": 28,
            "default_placebo_length_periods": 28,
        }
    # Default / fallback: weekly (preserves existing behaviour)
    return {
        "frequency": "weekly",
        "period_label_singular": "week",
        "period_label_plural": "weeks",
        "lag_periods": 1,
        "lag_label": "1-week",
        "default_min_training_periods": 13,
        "default_validation_horizon_periods": 4,
        "default_placebo_length_periods": 4,
    }

def infer_time_series_frequency(dates):
    """
    Infer whether a collection of dates looks daily or weekly, based on the median
    difference between sorted unique dates. This is a suggestion/warning helper only
    and should never be used to silently override a user's explicit selection.

    Args:
        dates: iterable of date-like values.

    Returns:
        "daily" if the median gap is close to 1 day, "weekly" if close to 7 days,
        otherwise "unknown". Returns "unknown" if fewer than 2 unique dates are given.
    """
    try:
        unique_dates = sorted(pd.to_datetime(pd.Series(list(dates))).dropna().unique())
    except Exception:
        return "unknown"
    if len(unique_dates) < 2:
        return "unknown"
    diffs = np.diff(np.array(unique_dates)).astype("timedelta64[D]").astype(int)
    if len(diffs) == 0:
        return "unknown"
    median_diff = float(np.median(diffs))
    if 0.5 <= median_diff <= 1.5:
        return "daily"
    elif 5.5 <= median_diff <= 8.5:
        return "weekly"
    else:
        return "unknown"

def format_range(lower, upper, suffix="", decimals=1):
    """
    Consistently formats a (lower, upper) range as "{lower}{suffix} to {upper}{suffix}",
    e.g. "0.1% to 21.0%" or "-50 to 120". Returns "N/A" if either value is missing or
    not finite (covers None, np.nan, pd.NA, and +/-inf).
    Used everywhere range values are shown in the Method Comparison table so ranges
    never mix bracket-style formatting (e.g. "[0.1%, 21.0%]") with "to"-style formatting.
    """
    if lower is None or upper is None:
        return "N/A"
    try:
        if pd.isna(lower) or pd.isna(upper):
            return "N/A"
    except (TypeError, ValueError):
        return "N/A"
    try:
        if not np.isfinite(float(lower)) or not np.isfinite(float(upper)):
            return "N/A"
    except (TypeError, ValueError):
        return "N/A"
    fmt = f"{{:.{decimals}f}}"
    return f"{fmt.format(lower)}{suffix} to {fmt.format(upper)}{suffix}"

def smape(actual, pred):
    denom = (np.abs(actual) + np.abs(pred)) / 2
    denom = np.where(denom == 0, 1e-8, denom)
    return np.mean(np.abs(actual - pred) / denom) * 100

def compute_metrics(actual, pred):
    corr = np.corrcoef(actual, pred)[0,1]
    r2 = r2_score(actual, pred)
    s = smape(actual, pred)
    rmse = np.sqrt(mean_squared_error(actual, pred))
    return corr, r2, s, rmse

def summarize_mcmc_diagnostics(summary_df, n_divergences=None, n_total_draws=None):
    """
    Compute high‑level MCMC diagnostics from ArviZ summary DataFrame.
    Returns a dict with keys: max_rhat, min_ess, max_mcse_sd_ratio, n_divergences,
    divergence_rate, status, messages.

    n_divergences: total count of divergent transitions across all chains (from
    trace.sample_stats["diverging"].sum()), if available. n_total_draws: total
    post-tuning draws across all chains (chains * draws), used only to compute a
    display divergence rate. If n_divergences is None (not passed), the divergence
    check is skipped and treated as passing — callers should always pass it when
    available.

    Divergences are checked alongside R-hat/ESS/MCSE, not as a replacement for them:
    unlike those three (which mostly flag sampling *noise*), a divergence flags a
    specific region of the posterior the sampler failed to explore, which can bias
    point estimates rather than just add noise — so even a single divergence is
    treated as a hard fail here, unlike the other three which use tolerance bands.
    """
    summary_df = summary_df.astype(float)
    
    max_rhat = summary_df['r_hat'].max()
    min_ess = min(summary_df['ess_bulk'].min(), summary_df['ess_tail'].min())
    max_mcse_sd = (summary_df['mcse_mean'] / summary_df['sd']).max()
    
    rhat_ok = max_rhat <= 1.01
    ess_ok = min_ess >= CONFIG["ess_min_threshold"]   # softer threshold
    mcse_ok = max_mcse_sd < 0.10
    divergence_ok = (n_divergences is None) or (n_divergences == 0)
    divergence_rate = (
        (n_divergences / n_total_draws) if (n_divergences is not None and n_total_draws) else None
    )
    
    overall_ok = rhat_ok and ess_ok and mcse_ok and divergence_ok
    status = "✅ Good" if overall_ok else "⚠️ Review needed"
    
    messages = []
    if not rhat_ok:
        messages.append(f"R‑hat > 1.01 (max = {max_rhat:.3f}) – chains may not have converged.")
    if not ess_ok:
        messages.append(f"Effective sample size < {CONFIG['ess_min_threshold']} (min = {min_ess:.0f}) – try increasing draws/tune.")
    if not mcse_ok:
        messages.append(f"MCSE/SD > 10% (max = {max_mcse_sd:.1%}) – sampling error may be high.")
    if not divergence_ok:
        _rate_str = f" ({divergence_rate:.1%} of draws)" if divergence_rate is not None else ""
        messages.append(
            f"{n_divergences} divergent transition(s){_rate_str} – posterior estimates may be biased "
            "in the region the sampler avoided, not just noisier. Try a higher target_accept, more "
            "tuning steps, or reparameterizing the model."
        )
    
    return {
        'max_rhat': max_rhat,
        'min_ess': min_ess,
        'max_mcse_sd_ratio': max_mcse_sd,
        'n_divergences': n_divergences,
        'divergence_rate': divergence_rate,
        'rhat_ok': rhat_ok,
        'ess_ok': ess_ok,
        'mcse_ok': mcse_ok,
        'divergence_ok': divergence_ok,
        'overall_ok': overall_ok,
        'status': status,
        'messages': messages
    }
    
def calculate_structural_prior_sigmas(
    agg_df,
    test_regions,
    control_regions,
    geo_col,
    feature_cols,
    weight_dict=None,
    population_col="Population",
    min_sigma=0.25,
    max_sigma=0.70,
):
    """
    Compute per-control coefficient prior sigmas based on structural similarity
    to the population-weighted test-group profile.

    Returns
    -------
    prior_sigmas : np.ndarray  shape (len(control_regions),)
    structural_prior_df : pd.DataFrame
    """
    # 1. Keep only features present in agg_df and numeric
    valid_features = [
        f for f in feature_cols
        if f in agg_df.columns and pd.api.types.is_numeric_dtype(agg_df[f])
    ]

    # Edge case: no valid features
    if not valid_features:
        prior_sigmas = np.repeat(0.5, len(control_regions))
        df_out = pd.DataFrame({
            "Control Region": control_regions,
            "Structural Distance": np.nan,
            "Structural Similarity": np.nan,
            "Prior Sigma": prior_sigmas,
            "Prior Type": "Standard weak prior",
        })
        return prior_sigmas, df_out

    # 2. Impute missing values if helper is available
    try:
        agg_df = impute_missing_features(agg_df, valid_features)
    except Exception:
        pass

    # 3. Standardise across all available regions
    all_regions = list(test_regions) + list(control_regions)
    region_df = agg_df[agg_df[geo_col].isin(all_regions)].copy()
    region_df = region_df.drop_duplicates(subset=[geo_col])

    scaler = StandardScaler()
    try:
        region_df[valid_features] = scaler.fit_transform(region_df[valid_features].fillna(0))
    except Exception:
        prior_sigmas = np.repeat(0.5, len(control_regions))
        df_out = pd.DataFrame({
            "Control Region": control_regions,
            "Structural Distance": np.nan,
            "Structural Similarity": np.nan,
            "Prior Sigma": prior_sigmas,
            "Prior Type": "Standard weak prior",
        })
        return prior_sigmas, df_out

    # 4. Population-weighted test-group profile
    test_rows = region_df[region_df[geo_col].isin(test_regions)]
    if population_col in test_rows.columns:
        pop_weights = pd.to_numeric(test_rows[population_col], errors="coerce").fillna(1.0).values
        if pop_weights.sum() <= 0 or np.isnan(pop_weights).all():
            pop_weights = np.ones(len(test_rows))
    else:
        pop_weights = np.ones(len(test_rows))

    test_features = test_rows[valid_features].values
    if len(test_features) == 0:
        prior_sigmas = np.repeat(0.5, len(control_regions))
        df_out = pd.DataFrame({
            "Control Region": control_regions,
            "Structural Distance": np.nan,
            "Structural Similarity": np.nan,
            "Prior Sigma": prior_sigmas,
            "Prior Type": "Standard weak prior",
        })
        return prior_sigmas, df_out

    test_profile_z = np.average(test_features, axis=0, weights=pop_weights)

    # 5. Feature weights
    if weight_dict is not None:
        feature_weights = np.array([weight_dict.get(f, 1.0) for f in valid_features], dtype=float)
        feature_weights = np.where(feature_weights <= 0, 1.0, feature_weights)
    else:
        feature_weights = np.ones(len(valid_features))
    feature_weights = feature_weights / feature_weights.sum()

    # 6. Distance and similarity for each control
    distances = []
    for ctrl in control_regions:
        ctrl_row = region_df[region_df[geo_col] == ctrl]
        if ctrl_row.empty:
            distances.append(np.nan)
        else:
            ctrl_z = ctrl_row[valid_features].values[0]
            dist = np.sqrt(np.average((ctrl_z - test_profile_z) ** 2, weights=feature_weights))
            distances.append(dist)

    distances = np.array(distances, dtype=float)
    # Replace NaN distances with max observed distance (worst case)
    finite_mask = np.isfinite(distances)
    if finite_mask.any():
        distances = np.where(finite_mask, distances, np.nanmax(distances))
    else:
        # All NaN — fall back to uniform
        prior_sigmas = np.repeat(0.5, len(control_regions))
        df_out = pd.DataFrame({
            "Control Region": control_regions,
            "Structural Distance": np.nan,
            "Structural Similarity": np.nan,
            "Prior Sigma": prior_sigmas,
            "Prior Type": "Standard weak prior",
        })
        return prior_sigmas, df_out

    similarities = 1.0 / (1.0 + distances)

    # 7. Edge case: single control or all similarities identical
    if len(control_regions) == 1 or (similarities.max() - similarities.min()) < 1e-8:
        prior_sigmas = np.repeat(0.5, len(control_regions))
        df_out = pd.DataFrame({
            "Control Region": control_regions,
            "Structural Distance": np.round(distances, 3),
            "Structural Similarity": np.round(similarities, 3),
            "Prior Sigma": np.round(prior_sigmas, 3),
            "Prior Type": "Standard weak prior",
        })
        return prior_sigmas, df_out

    # 8. Continuous scaling to [min_sigma, max_sigma]
    similarity_scaled = (similarities - similarities.min()) / (similarities.max() - similarities.min() + 1e-8)
    prior_sigmas = min_sigma + similarity_scaled * (max_sigma - min_sigma)
    prior_sigmas = np.clip(prior_sigmas, min_sigma, max_sigma)

    prior_types = [
        "Structurally informed" for _ in control_regions
    ]

    df_out = pd.DataFrame({
        "Control Region": control_regions,
        "Structural Distance": np.round(distances, 3),
        "Structural Similarity": np.round(similarities, 3),
        "Prior Sigma": np.round(prior_sigmas, 3),
        "Prior Type": prior_types,
    })

    return prior_sigmas.astype(float), df_out


def safe_tscv(n_splits, n_periods):
    """Return TimeSeriesSplit only if enough periods (weeks or days), else None (caller must handle)."""
    if n_periods < 6:
        return None
    n = min(n_splits, n_periods // 3)
    return TimeSeriesSplit(n_splits=max(2, n))

def build_regularized_model(method_name, n_periods, n_splits_pref=5, fixed_alpha=1.0):
    """
    Builds an ElasticNet-family model for the given method_name ("enet" or "lasso").

    Uses TimeSeriesSplit-based ElasticNetCV whenever there are enough pre-period
    observations to support safe, leakage-free time-series cross-validation.

    If there are too few periods for TimeSeriesSplit, this does NOT fall back to
    regular K-fold CV — standard K-fold (even with shuffle=False) can still let
    later time points influence hyperparameter choices for earlier ones, which is
    a form of leakage for time-series data. It also does NOT treat a fixed-alpha
    fallback as equivalent to cross-validated model selection: alpha=1.0 is an
    arbitrary modelling choice, not a statistically defensible substitute for CV.
    Instead, it returns a fixed-alpha ElasticNet explicitly labelled as exploratory
    — callers must exclude exploratory-fallback results from Counterfactual
    Reliability and from rolling-origin validation metrics used for method
    comparison.

    Returns: (model, cv_status, used_cv) where cv_status is a short human-readable
    string describing whether TimeSeriesSplit CV or the exploratory fallback was
    used, and used_cv is True only when TimeSeriesSplit-based ElasticNetCV was used.
    """
    tscv = safe_tscv(n_splits_pref, n_periods)
    if tscv is not None:
        if method_name == "enet":
            model = ElasticNetCV(l1_ratio=[.1, .3, .5, .7, .9, .95], alphas=np.logspace(-4, 4, 50),
                                  cv=tscv, max_iter=10000, random_state=42)
        else:  # lasso
            model = ElasticNetCV(l1_ratio=1, alphas=np.logspace(-4, 4, 100),
                                  cv=tscv, max_iter=10000, random_state=42)
        return model, "TimeSeriesSplit cross-validation used to select regularisation strength.", True
    # Too few pre-period observations for safe, leakage-free time-series CV. This
    # fixed-alpha fit is exploratory only: it is NOT statistically equivalent to
    # cross-validated model selection and must not feed Counterfactual Confidence
    # or rolling-origin validation metrics used for method comparison.
    l1_ratio = 1.0 if method_name == "lasso" else 0.5
    model = ElasticNet(alpha=fixed_alpha, l1_ratio=l1_ratio, max_iter=10000, random_state=42)
    cv_status = (
        f"Insufficient history for TimeSeriesSplit; exploratory fixed-alpha ElasticNet "
        f"fit (alpha={fixed_alpha}, l1_ratio={l1_ratio}) used instead — NOT cross-validated "
        f"and excluded from Counterfactual Confidence."
    )
    return model, cv_status, False

@st.cache_data(ttl=CONFIG["cache_ttl"], show_spinner=False)
def rolling_origin_validation(X, y, horizon=4, min_training_periods=13, dates=None, n_splits=5, model_type="enet",
                               min_training_weeks=None):
    """
    Expanding-window rolling origin validation.
    Trains on rows 0:start_idx, tests on rows start_idx:start_idx+horizon.
    Starts at min_training_periods, continues while a full horizon window is available.
    horizon and min_training_periods are row counts (periods), matching the granularity of the
    underlying data — weeks for weekly data, days for daily data.

    Uses TimeSeriesSplit-based cross-validation to tune model regularisation whenever a fold's
    training window has enough periods. Never falls back to regular K-fold CV, since that can leak
    future information into hyperparameter tuning for time-series data. Folds with too little
    training history for TimeSeriesSplit instead fit an exploratory fixed-alpha model (see
    build_regularized_model()) — those folds are excluded from rolling_smape_mean and
    rolling_rmse_mean, since a fixed-alpha fit is not statistically equivalent to a
    cross-validated result and should not be presented as such.

    Returns: fold_df (DataFrame, includes a "used_cv_fallback" column per fold), rolling_smape_mean
    (float, computed only from TimeSeriesSplit-CV folds — np.nan if none are available),
    rolling_rmse_mean (float, same basis), cv_status (str describing CV vs. fallback usage across
    folds). For backwards compatibility, also accepts n_splits (ignored — all valid folds are used)
    and min_training_weeks as an alias for min_training_periods.

    Cached via @st.cache_data: this is pure and deterministic (all models use a fixed
    random_state) with no Streamlit UI calls inside, and it's the most expensive step in
    validation (fits up to ~20 folds). It's re-invoked on every Streamlit rerun once
    validation_triggered is set, including reruns caused by unrelated widget changes
    elsewhere on the page, so caching avoids redundant model fitting on identical inputs.
    """
    if min_training_weeks is not None:
        min_training_periods = min_training_weeks
    n = len(y)
    empty_df = pd.DataFrame(columns=[
        "fold_number", "training_periods", "forecast_horizon_periods",
        "training_weeks", "forecast_horizon_weeks",
        "smape", "rmse", "bias", "bias_pct", "uplift_error", "uplift_error_pct",
        "train_start_date", "train_end_date", "test_start_date", "test_end_date",
        "used_cv_fallback"
    ])
    if n < min_training_periods + horizon:
        return empty_df, np.nan, np.nan, "No folds: insufficient pre-period history for rolling-origin validation."

    folds = []
    fold_num = 0
    _all_starts = list(range(min_training_periods, n - horizon + 1))
    if len(_all_starts) > 20:
        _step = len(_all_starts) // 20
        _all_starts = _all_starts[::_step][:20]
    for start_idx in _all_starts:
        train_X, train_y = X[:start_idx], y[:start_idx]
        test_X, test_y = X[start_idx:start_idx + horizon], y[start_idx:start_idx + horizon]
        if len(test_y) < horizon:
            continue

        scaler = StandardScaler()
        train_X_scaled = scaler.fit_transform(train_X)
        test_X_scaled = scaler.transform(test_X)

        if model_type not in ("enet", "lasso"):
            return empty_df, np.nan, np.nan, "Unsupported model_type"

        model, fold_cv_status, fold_used_cv = build_regularized_model(model_type, len(train_y), n_splits_pref=3)
        used_cv_fallback = not fold_used_cv

        model.fit(train_X_scaled, train_y)
        pred = model.predict(test_X_scaled)

        fold_smape = smape(test_y, pred)
        fold_rmse = np.sqrt(mean_squared_error(test_y, pred))
        bias = float(np.mean(pred - test_y))
        mean_actual = float(np.mean(test_y))
        bias_pct = bias / mean_actual * 100 if mean_actual != 0 else np.nan
        uplift_error = float(test_y.sum() - pred.sum())
        pred_sum = float(pred.sum())
        uplift_error_pct = uplift_error / pred_sum * 100 if pred_sum != 0 else np.nan

        # Date labels (optional)
        if dates is not None and len(dates) == n:
            train_start_date = dates[0]
            train_end_date = dates[start_idx - 1]
            test_start_date = dates[start_idx]
            test_end_date = dates[min(start_idx + horizon - 1, n - 1)]
        else:
            train_start_date = train_end_date = test_start_date = test_end_date = None

        fold_num += 1
        folds.append({
            "fold_number": fold_num,
            "training_periods": start_idx,
            "forecast_horizon_periods": horizon,
            # Backward-compatible aliases
            "training_weeks": start_idx,
            "forecast_horizon_weeks": horizon,
            "smape": fold_smape,
            "rmse": fold_rmse,
            "bias": bias,
            "bias_pct": bias_pct,
            "uplift_error": uplift_error,
            "uplift_error_pct": uplift_error_pct,
            "train_start_date": train_start_date,
            "train_end_date": train_end_date,
            "test_start_date": test_start_date,
            "test_end_date": test_end_date,
            "used_cv_fallback": used_cv_fallback,
        })

    if not folds:
        return empty_df, np.nan, np.nan, "No folds: insufficient pre-period history for rolling-origin validation."

    fold_df = pd.DataFrame(folds)
    n_fallback_folds = int(fold_df["used_cv_fallback"].sum())
    n_cv_folds = len(fold_df) - n_fallback_folds

    # Exploratory fixed-alpha folds are NOT statistically equivalent to cross-validated
    # results and must not feed the headline rolling-origin metrics used for method
    # comparison or Counterfactual Confidence. Only TimeSeriesSplit-CV folds count here.
    cv_fold_df = fold_df[~fold_df["used_cv_fallback"]]
    if n_cv_folds > 0:
        rolling_smape_mean = float(cv_fold_df["smape"].mean())
        rolling_rmse_mean = float(cv_fold_df["rmse"].mean())
    else:
        rolling_smape_mean = np.nan
        rolling_rmse_mean = np.nan

    if n_fallback_folds == 0:
        cv_status = "TimeSeriesSplit cross-validation used to select regularisation strength in all folds."
    elif n_cv_folds == 0:
        cv_status = (
            "Insufficient history for TimeSeriesSplit in all folds; only exploratory fixed-alpha "
            "fits were available, so rolling-origin validation metrics are Insufficient data "
            "rather than being based on a non-cross-validated fallback."
        )
    else:
        cv_status = (
            f"Insufficient history for TimeSeriesSplit in {n_fallback_folds} of {len(fold_df)} folds; "
            f"those folds were exploratory fixed-alpha fits and are excluded from rolling-origin "
            f"validation metrics (based on the remaining {n_cv_folds} TimeSeriesSplit-CV fold(s))."
        )
    return fold_df, rolling_smape_mean, rolling_rmse_mean, cv_status

def classify_validation_method(fold_df, main_model_used_cv_fallback):
    """
    Short, stakeholder-facing summary of whether rolling-origin validation for a
    method used proper leakage-free TimeSeriesSplit cross-validation, only partially
    did so, or wasn't possible at all due to insufficient pre-period history.

    - 🟢 "Rolling-origin validation": every rolling-origin fold used TimeSeriesSplit CV.
    - 🟡 "Partial rolling-origin validation": some folds were excluded because they
      didn't have enough training history for TimeSeriesSplit (those folds used the
      exploratory fixed-alpha fallback and are excluded from the headline metrics).
    - ⚪ "Insufficient validation history": no valid TimeSeriesSplit-CV fold is
      available at all (including when the main pre-period model itself couldn't run
      TimeSeriesSplit).

    Full technical detail (exact fold counts, fallback settings) is available
    separately via the "cv_status" string, shown in the "Technical validation
    details" expander rather than in the headline table.
    """
    if main_model_used_cv_fallback or fold_df is None or fold_df.empty or "used_cv_fallback" not in fold_df.columns:
        return "⚪ Insufficient validation history"
    n_fallback_folds = int(fold_df["used_cv_fallback"].sum())
    if n_fallback_folds == 0:
        return "🟢 Rolling-origin validation"
    elif n_fallback_folds < len(fold_df):
        return "🟡 Partial rolling-origin validation"
    else:
        return "⚪ Insufficient validation history"

def _warn_on_row_loss(matrix_diagnostics):
    """
    Row-loss diagnostics: warns the user when a meaningful share of rows were dropped
    from the model matrix because the test series or a selected control had missing KPI
    values for some dates. Extracted from run_validation_method() so the row-loss check
    reads as a single, named step rather than being interleaved with matrix construction.
    """
    pct_dropped = matrix_diagnostics.get("pct_rows_dropped", 0.0)
    rows_dropped = matrix_diagnostics.get("rows_dropped", 0)
    rows_before = matrix_diagnostics.get("rows_before_dropna", 0)
    if rows_dropped > 0 and pct_dropped > 20:
        st.error(
            f"{rows_dropped} of {rows_before} rows ({pct_dropped:.1f}%) were removed because "
            "the test series or at least one selected control had missing KPI values. "
            "This is a large share of the data and the validation result may be unreliable. "
            f"Controls with missing values: {', '.join(matrix_diagnostics.get('control_columns_with_missing', [])) or 'none'}."
        )
    elif rows_dropped > 0 and pct_dropped > 10:
        st.warning(
            f"{rows_dropped} of {rows_before} rows ({pct_dropped:.1f}%) were removed because "
            "the test series or at least one selected control had missing KPI values. "
            "This can affect validation reliability. "
            f"Controls with missing values: {', '.join(matrix_diagnostics.get('control_columns_with_missing', [])) or 'none'}."
        )

def _warn_on_cv_fallback(method_name, main_model_used_cv_fallback, fold_df):
    """
    Surfaces a warning whenever TimeSeriesSplit cross-validation couldn't be used —
    either for the main pre-period model (no confidence rating at all) or for some
    rolling-origin folds (those folds are excluded from the headline validation metrics
    and Counterfactual Confidence). See build_regularized_model() and
    rolling_origin_validation() for why this app never falls back to regular KFold.
    """
    if main_model_used_cv_fallback:
        st.warning(
            f"⚠️ There is insufficient pre-period history to run leakage-free TimeSeriesSplit "
            f"cross-validation for **{method_name}**. This method has not been given a "
            "confidence rating. Add more pre-period data or reduce the validation window."
        )
    elif not fold_df.empty and bool(fold_df["used_cv_fallback"].any()):
        n_fallback_folds = int(fold_df["used_cv_fallback"].sum())
        st.warning(
            f"⚠️ {n_fallback_folds} of {len(fold_df)} rolling-origin folds for **{method_name}** "
            "did not have enough training history for leakage-free TimeSeriesSplit cross-validation. "
            "Those folds were fit exploratorily with a fixed regularisation strength and are excluded "
            "from the rolling-origin validation metrics and Counterfactual Confidence shown here."
        )

def _summarize_rolling_origin_folds(fold_df):
    """
    Additional rolling-origin summary stats (P90 sMAPE, mean bias, uplift-error interval)
    computed only from TimeSeriesSplit-CV folds — exploratory fixed-alpha fallback folds
    are excluded, since Rolling-Origin Bias (%) directly feeds Counterfactual Confidence
    and should not be contaminated by a non-cross-validated fit.

    Returns a dict with keys: rolling_smape_p90, rolling_bias_pct_mean,
    rolling_uplift_error_pct_median, rolling_uplift_error_pct_lower,
    rolling_uplift_error_pct_upper. All np.nan if no CV folds are available.
    """
    if not fold_df.empty:
        cv_fold_df = fold_df[~fold_df["used_cv_fallback"]]
    else:
        cv_fold_df = fold_df

    if cv_fold_df.empty:
        return {
            "rolling_smape_p90": np.nan,
            "rolling_bias_pct_mean": np.nan,
            "rolling_uplift_error_pct_median": np.nan,
            "rolling_uplift_error_pct_lower": np.nan,
            "rolling_uplift_error_pct_upper": np.nan,
        }

    valid_uplift_errs = cv_fold_df["uplift_error_pct"].dropna()
    if len(valid_uplift_errs) >= 2:
        lower, upper = np.percentile(valid_uplift_errs, [2.5, 97.5])
        lower, upper = float(lower), float(upper)
    else:
        lower = upper = np.nan

    return {
        "rolling_smape_p90": float(np.percentile(cv_fold_df["smape"], 90)),
        "rolling_bias_pct_mean": float(cv_fold_df["bias_pct"].mean()),
        "rolling_uplift_error_pct_median": float(np.median(valid_uplift_errs)) if len(valid_uplift_errs) else np.nan,
        "rolling_uplift_error_pct_lower": lower,
        "rolling_uplift_error_pct_upper": upper,
    }

def _run_placebo_windows(model_pre, model_feature_cols, dates_pre, min_training_periods, placebo_len, method_name,
                          max_windows=40):
    """
    Simulates a fake intervention across all available historical pre-period windows
    ("placebo testing"): repeatedly trains on an expanding window and evaluates on the
    next placebo_len periods, using the same model type as the main fit. Never falls
    back to regular KFold for time-series data — see build_regularized_model().

    Subsamples to at most `max_windows` evenly-spaced windows when more are available,
    to keep runtime bounded (each window fits a fresh model). This caps the resolution
    of any empirical p-value / percentile rank derived from the result at roughly
    1/max_windows — e.g. with the default of 40, the smallest nonzero one-sided p-value
    achievable is ~0.025, not smaller. Callers that report a p-value alongside the
    "Placebo Windows" count should treat "p < 0.05" claims from very small window counts
    with this precision limit in mind.

    Returns four parallel lists (placebos, placebo_uplift_pcts, placebo_smapes,
    placebo_rmses), one entry per placebo window. All empty if placebo_len is missing/
    non-positive or there isn't enough pre-period history for even one window.
    """
    placebos, placebo_uplift_pcts, placebo_smapes, placebo_rmses = [], [], [], []

    if placebo_len is None or placebo_len <= 0:
        return placebos, placebo_uplift_pcts, placebo_smapes, placebo_rmses

    n_pre = len(dates_pre)
    if n_pre < placebo_len + min_training_periods:
        return placebos, placebo_uplift_pcts, placebo_smapes, placebo_rmses

    all_starts = list(range(min_training_periods, n_pre - placebo_len + 1))
    if len(all_starts) > max_windows:
        step = len(all_starts) // max_windows
        all_starts = all_starts[::step][:max_windows]

    for start_idx in all_starts:
        train_dates = dates_pre[:start_idx]
        test_dates = dates_pre[start_idx:start_idx + placebo_len]
        # Slice from the already-lagged pre-period matrix — this preserves lagged
        # features computed from the full continuous series rather than recomputing
        # (and losing the first row of) each placebo window independently.
        m_train = model_pre[(model_pre["date"] >= train_dates[0]) & (model_pre["date"] <= train_dates[-1])]
        m_test = model_pre[(model_pre["date"] >= test_dates[0]) & (model_pre["date"] <= test_dates[-1])]
        if len(m_train) < min_training_periods or m_test.empty:
            continue

        X_tr = m_train[model_feature_cols].values
        y_tr = m_train["test_kpi"].values
        X_te = m_test[model_feature_cols].values
        y_te = m_test["test_kpi"].values
        scaler_p = StandardScaler()
        X_tr_scaled = scaler_p.fit_transform(X_tr)
        model_p, _placebo_cv_status, _placebo_used_cv = build_regularized_model(method_name, len(y_tr), n_splits_pref=3)
        model_p.fit(X_tr_scaled, y_tr)
        pred_p = model_p.predict(scaler_p.transform(X_te))

        uplift_p = y_te.sum() - pred_p.sum()
        placebos.append(uplift_p)
        pred_sum = pred_p.sum()
        placebo_uplift_pcts.append((uplift_p / pred_sum) * 100 if pred_sum != 0 else np.nan)
        placebo_smapes.append(smape(y_te, pred_p))
        placebo_rmses.append(np.sqrt(mean_squared_error(y_te, pred_p)))

    return placebos, placebo_uplift_pcts, placebo_smapes, placebo_rmses

def _summarize_placebo_results(placebos, placebo_uplift_pcts, placebo_smapes, placebo_rmses, uplift):
    """
    Summarizes the raw per-window placebo lists from _run_placebo_windows() into the
    metrics shown in the "Placebo Testing" and "Observed Uplift vs Placebos" table
    sections: the median/95% range of placebo uplift, placebo forecast error, and (if
    an observed uplift is available) how extreme that observed uplift is relative to
    the placebo distribution (percentile rank, one/two-sided p-values, z-score).

    Returns a dict; all values are np.nan if there are no placebo windows.
    """
    if not placebos:
        return {
            "median_uplift": np.nan, "p2_5": np.nan, "p97_5": np.nan,
            "median_placebo_smape": np.nan, "p95_placebo_smape": np.nan,
            "median_placebo_rmse": np.nan, "p95_placebo_rmse": np.nan,
            "median_placebo_uplift_pct": np.nan, "p2_5_pct": np.nan, "p97_5_pct": np.nan,
            "percentile_rank": np.nan, "p_one_sided": np.nan, "p_two_sided": np.nan, "z_score": np.nan,
        }

    median_uplift = np.median(placebos)
    p2_5, p97_5 = np.percentile(placebos, [2.5, 97.5])
    median_placebo_smape = np.median(placebo_smapes) if placebo_smapes else np.nan
    p95_placebo_smape = np.percentile(placebo_smapes, 95) if placebo_smapes else np.nan
    median_placebo_rmse = np.median(placebo_rmses) if placebo_rmses else np.nan
    p95_placebo_rmse = np.percentile(placebo_rmses, 95) if placebo_rmses else np.nan
    median_placebo_uplift_pct = np.median(placebo_uplift_pcts) if placebo_uplift_pcts else np.nan
    p2_5_pct, p97_5_pct = np.percentile(placebo_uplift_pcts, [2.5, 97.5]) if placebo_uplift_pcts else (np.nan, np.nan)

    if uplift is not None:
        percentile_rank = np.mean(np.array(placebos) < uplift) * 100
        p_one_sided = np.mean(np.array(placebos) >= uplift)
        mean_placebo = np.mean(placebos)
        p_two_sided = np.mean(np.abs(np.array(placebos) - mean_placebo) >= np.abs(uplift - mean_placebo))
        z_score = (uplift - mean_placebo) / (np.std(placebos) + 1e-12)
    else:
        percentile_rank = p_one_sided = p_two_sided = z_score = np.nan

    return {
        "median_uplift": median_uplift, "p2_5": p2_5, "p97_5": p97_5,
        "median_placebo_smape": median_placebo_smape, "p95_placebo_smape": p95_placebo_smape,
        "median_placebo_rmse": median_placebo_rmse, "p95_placebo_rmse": p95_placebo_rmse,
        "median_placebo_uplift_pct": median_placebo_uplift_pct, "p2_5_pct": p2_5_pct, "p97_5_pct": p97_5_pct,
        "percentile_rank": percentile_rank, "p_one_sided": p_one_sided, "p_two_sided": p_two_sided, "z_score": z_score,
    }

def run_validation_method(agg_df, control_list, test_regions, method_name,
                          pre_start, pre_end, test_start=None, test_end=None,
                          use_post=False, post_start=None, post_end=None,
                          compute_uplift=True, placebo_length_weeks=None,
                          min_training_weeks=13, include_lagged_controls=False,
                          time_series_frequency="weekly", placebo_length_periods=None,
                          min_training_periods=None, frequency_config=None):
    """
    Run a single validation method (ElasticNet or LASSO).
    Returns a dict with metrics, predictions, placebo results, etc.

    If include_lagged_controls is True, each control also gets a lagged feature
    (`{control}_lag{lag_periods}`), and the model is fit on the expanded feature set. A
    combined pre + test/post model matrix is built first and lags are applied once, so the
    first test/post period can still use the immediately preceding period's control KPI as
    its lag. lag_periods is 1 for weekly data (a 1-week lag) and 7 for daily data (a 7-day
    lag, chosen so the lag compares the same day of week).

    time_series_frequency ("weekly" or "daily") — or an explicit frequency_config dict from
    get_frequency_config() — determines the lag length, and (together with
    placebo_length_periods / min_training_periods, which take precedence over the legacy
    placebo_length_weeks / min_training_weeks arguments) the rolling-origin and placebo
    window sizing. placebo_length_weeks and min_training_weeks are retained as backward-
    compatible aliases and are treated as period counts matching the selected frequency.
    """
    if frequency_config is None:
        frequency_config = get_frequency_config(time_series_frequency)
    lag_periods = frequency_config["lag_periods"]

    # Resolve period-based args, preferring the new *_periods names but falling back to the
    # legacy *_weeks names so existing callers keep working unchanged.
    if placebo_length_periods is None:
        placebo_length_periods = placebo_length_weeks
    if min_training_periods is None:
        min_training_periods = min_training_weeks if min_training_weeks is not None else 13

    pre_start = pd.to_datetime(pre_start)
    pre_end = pd.to_datetime(pre_end)
    if test_start is not None:
        test_start = pd.to_datetime(test_start)
    if test_end is not None:
        test_end = pd.to_datetime(test_end)
    if use_post and post_start is not None:
        post_start = pd.to_datetime(post_start)
    if use_post and post_end is not None:
        post_end = pd.to_datetime(post_end)

    # ---- Build a combined pre + test/post model matrix so lagged features apply once,
    # across the full continuous date range, before splitting back out by period. ----
    combined_end_candidates = [pre_end]
    if test_end is not None:
        combined_end_candidates.append(test_end)
    if use_post and post_end is not None:
        combined_end_candidates.append(post_end)
    combined_end = max(combined_end_candidates)

    full_mask = (agg_df["date"] >= pre_start) & (agg_df["date"] <= combined_end)
    model_full, matrix_diagnostics = build_model_matrix(agg_df[full_mask], control_list, test_regions)

    # ---- Row-loss diagnostics: warn when a meaningful share of rows were dropped because
    # the test series or a selected control had missing KPI values for some dates. ----
    _warn_on_row_loss(matrix_diagnostics)

    if include_lagged_controls:
        model_full, model_feature_cols, lagged_feature_map, lag_drop_metadata = add_lagged_control_features(
            model_full, control_list, lags=(lag_periods,), frequency_config=frequency_config
        )
    else:
        model_feature_cols = list(control_list)
        lagged_feature_map = {}
        lag_drop_metadata = None

    # Pre-period data (sliced from the combined, already-lagged matrix)
    pre_mask = (model_full["date"] >= pre_start) & (model_full["date"] <= pre_end)
    model_pre = model_full[pre_mask].sort_values("date").reset_index(drop=True)
    if len(model_pre) < 6:
        return None
    X_pre = model_pre[model_feature_cols].values
    y_pre = model_pre["test_kpi"].values
    dates_pre = model_pre["date"].tolist()
    scaler = StandardScaler()
    X_pre_scaled = scaler.fit_transform(X_pre)

    # Determine model type from method_name
    # method_name is either "enet" or "lasso"
    model, main_model_cv_status, main_model_used_cv = build_regularized_model(method_name, len(y_pre), n_splits_pref=5)
    main_model_used_cv_fallback = not main_model_used_cv
    model.fit(X_pre_scaled, y_pre)
    y_pred_pre = model.predict(X_pre_scaled)
    corr, r2, s, rmse = compute_metrics(y_pre, y_pred_pre)

    # ---- Durbin-Watson statistic on pre-period residuals (autocorrelation diagnostic) ----
    pre_residuals = y_pre - y_pred_pre
    dw_stat = durbin_watson_stat(pre_residuals)

    # Rolling-origin validation (using the same model type)
    # horizon matches placebo_length_periods so both use the same window length
    cv_horizon = placebo_length_periods if placebo_length_periods is not None else frequency_config["default_validation_horizon_periods"]
    fold_df, rolling_smape_mean, rolling_rmse_mean, rolling_cv_status = rolling_origin_validation(
        X_pre, y_pre,
        horizon=cv_horizon,
        min_training_periods=min_training_periods,
        dates=dates_pre,
        model_type=method_name
    )
    # Backwards-compat aliases
    holdout_smape_mean = rolling_smape_mean
    holdout_rmse_mean = rolling_rmse_mean

    # ---- CV status (item 6): never falls back to regular KFold for time-series data, and
    # never treats a fixed-alpha fallback as equivalent to cross-validated model selection.
    # If there isn't enough pre-period history for TimeSeriesSplit at all, rolling-origin
    # folds are all exploratory fixed-alpha fits too (a fold's training window can never be
    # longer than the full pre-period), so rolling_smape_mean/rolling_rmse_mean above are
    # already np.nan in that case, and Counterfactual Confidence naturally reports
    # "Insufficient data" rather than a misleading rating based on an arbitrary alpha.
    cv_status = f"Main model: {main_model_cv_status} Rolling-origin folds: {rolling_cv_status}"
    _warn_on_cv_fallback(method_name, main_model_used_cv_fallback, fold_df)

    # Additional rolling-origin summary stats. These also exclude exploratory fixed-alpha
    # folds, since Rolling-Origin Bias (%) directly feeds Counterfactual Confidence and
    # should not be contaminated by a non-cross-validated fit.
    _rolling_summary = _summarize_rolling_origin_folds(fold_df)
    rolling_smape_p90 = _rolling_summary["rolling_smape_p90"]
    rolling_bias_pct_mean = _rolling_summary["rolling_bias_pct_mean"]
    rolling_uplift_error_pct_median = _rolling_summary["rolling_uplift_error_pct_median"]
    rolling_uplift_error_pct_lower = _rolling_summary["rolling_uplift_error_pct_lower"]
    rolling_uplift_error_pct_upper = _rolling_summary["rolling_uplift_error_pct_upper"]

    # ---- Overfitting Gap (part 1): compare pre-period (in-sample) fit against
    # rolling-origin (out-of-sample) accuracy. A large gap means the model looks good
    # in-sample but doesn't hold up out-of-sample when predicting held-out historical
    # periods. This is a validation diagnostic, not a formal statistical test.
    # n_pre_periods is used for pre-period observation counts; the reliability
    # classification is finalised further below, once rolling-origin validation
    # and residual diagnostics are known. ----
    n_pre_periods = len(y_pre)
    overfit_gap_smape = calculate_overfit_gap(s, rolling_smape_mean)
    overfit_gap_rmse = calculate_overfit_gap(rmse, rolling_rmse_mean)

    # Test period predictions (if uplift required)
    model_test = None
    if compute_uplift and test_start is not None and test_end is not None:
        test_mask = (model_full["date"] >= test_start) & (model_full["date"] <= test_end)
        model_test = model_full[test_mask].sort_values("date").reset_index(drop=True)
        if not model_test.empty:
            X_test = model_test[model_feature_cols].values
            X_test_scaled = scaler.transform(X_test)
            y_test_actual = model_test["test_kpi"].values
            y_pred_test = model.predict(X_test_scaled)
            uplift = y_test_actual.sum() - y_pred_test.sum()
            uplift_pct = (uplift / y_pred_test.sum()) * 100 if y_pred_test.sum() != 0 else np.nan
            dates_test = model_test["date"].tolist()
        else:
            uplift = uplift_pct = None
            y_test_actual = y_pred_test = None
            dates_test = []
    else:
        uplift = uplift_pct = None
        y_test_actual = y_pred_test = None
        dates_test = []

    # Post-period (if any)
    if use_post and post_start is not None and post_end is not None:
        post_mask = (model_full["date"] >= post_start) & (model_full["date"] <= post_end)
        model_post = model_full[post_mask].sort_values("date").reset_index(drop=True)
        if not model_post.empty:
            X_post = model_post[model_feature_cols].values
            X_post_scaled = scaler.transform(X_post)
            y_post_pred = model.predict(X_post_scaled)
            y_post_actual = model_post["test_kpi"].values
            dates_post = model_post["date"].tolist()
        else:
            y_post_pred = y_post_actual = dates_post = None
    else:
        y_post_pred = y_post_actual = dates_post = None

    # Negative predictions flags
    neg_pre = any(y_pred_pre < 0)
    neg_test = any(y_pred_test < 0) if y_pred_test is not None else False
    neg_post = any(y_post_pred < 0) if y_post_pred is not None else False

    # ---------- Placebo generation (using the same model type) ----------
    if compute_uplift:
        if placebo_length_periods is not None:
            placebo_len = placebo_length_periods
        elif model_test is not None and not model_test.empty:
            # Prefer actual observed rows in the test period over a calendar-based guess —
            # this is robust to missing dates and correct for both weekly and daily data.
            placebo_len = len(model_test)
        elif test_start is not None and test_end is not None:
            if frequency_config["frequency"] == "daily":
                placebo_len = max(1, (test_end - test_start).days + 1)
            else:
                placebo_len = max(1, (test_end - test_start).days // 7 + 1)
        else:
            placebo_len = None
    else:
        placebo_len = None

    placebos, placebo_uplift_pcts, placebo_smapes, placebo_rmses = _run_placebo_windows(
        model_pre, model_feature_cols, dates_pre, min_training_periods, placebo_len, method_name
    )

    # Placebo summary statistics (use the same functions)
    _placebo_summary = _summarize_placebo_results(placebos, placebo_uplift_pcts, placebo_smapes, placebo_rmses, uplift)
    median_uplift = _placebo_summary["median_uplift"]
    p2_5 = _placebo_summary["p2_5"]
    p97_5 = _placebo_summary["p97_5"]
    median_placebo_smape = _placebo_summary["median_placebo_smape"]
    p95_placebo_smape = _placebo_summary["p95_placebo_smape"]
    median_placebo_rmse = _placebo_summary["median_placebo_rmse"]
    p95_placebo_rmse = _placebo_summary["p95_placebo_rmse"]
    median_placebo_uplift_pct = _placebo_summary["median_placebo_uplift_pct"]
    p2_5_pct = _placebo_summary["p2_5_pct"]
    p97_5_pct = _placebo_summary["p97_5_pct"]
    percentile_rank = _placebo_summary["percentile_rank"]
    p_one_sided = _placebo_summary["p_one_sided"]
    p_two_sided = _placebo_summary["p_two_sided"]
    z_score = _placebo_summary["z_score"]

    # Report selected (non-zero coefficient) features for BOTH LASSO and Elastic Net.
    # Elastic Net can also shrink coefficients to ~0 depending on l1_ratio, so it should
    # not be reported as "all candidates selected" by default.
    # With lagged controls, coefficients are over model_feature_cols (same-period + lag
    # terms), so each feature is mapped back to its base region and term type. The lag
    # recogniser is dynamic (any `{control}_lag{N}` feature, not just `_lag1`) so it works
    # for both the weekly 1-period lag and the daily 7-period lag.
    coefs = model.coef_
    coeff_threshold = 1e-6
    coeff_dict = dict(zip(model_feature_cols, coefs))
    same_period_label = "Same day" if frequency_config["frequency"] == "daily" else "Same week"

    def _feature_to_region_and_term(feat):
        for c in control_list:
            if feat == c:
                return c, same_period_label
            lag_match = re.match(rf"^{re.escape(c)}_lag(\d+)$", feat)
            if lag_match:
                lag_n = int(lag_match.group(1))
                period_word = "day" if frequency_config["frequency"] == "daily" else "week"
                period_word_plural = period_word + "s" if lag_n != 1 else period_word
                return c, f"Lag {lag_n} {period_word_plural}"
        return feat, same_period_label

    selected_df_rows = []
    for feat in model_feature_cols:
        base_region, term_type = _feature_to_region_and_term(feat)
        coeff_val = float(coeff_dict[feat])
        selected_df_rows.append({
            "Feature": feat,
            "Base Region": base_region,
            "Term Type": term_type,
            "Coefficient": round(coeff_val, 4),
            "Non-zero Coefficient": abs(coeff_val) > coeff_threshold,
        })
    selected_df = pd.DataFrame(
        selected_df_rows,
        columns=["Feature", "Base Region", "Term Type", "Coefficient", "Non-zero Coefficient"]
    )

    selected_features = [row["Feature"] for row in selected_df_rows if row["Non-zero Coefficient"]]
    # selected_regions stays a clean list of base regions used (a region counts as
    # selected if either its same-period or lagged term has a non-zero coefficient).
    selected = sorted({row["Base Region"] for row in selected_df_rows if row["Non-zero Coefficient"]},
                       key=lambda r: control_list.index(r) if r in control_list else 0)
    n_candidates = len(control_list)
    n_selected = len(selected)
    n_removed = n_candidates - n_selected
    alpha = getattr(model, "alpha_", np.nan)

    # ---- Selected feature count, kept for transparency in the selected-controls
    # table only. It is NOT used as a reliability diagnostic — reliability is based
    # solely on the four component checks below (rolling validation error,
    # overfitting gap, rolling bias, autocorrelation risk). ----
    n_selected_features = len(selected_features)

    # ---- Component traffic-light ratings. Each is based on exactly one diagnostic —
    # see classify_rolling_validation_error(), classify_overfitting_risk(),
    # classify_rolling_bias_risk(), and classify_autocorrelation_risk(). ----
    rolling_validation_error_risk = classify_rolling_validation_error(rolling_smape_mean)
    overfitting_risk = classify_overfitting_risk(overfit_gap_smape)
    rolling_bias_risk = classify_rolling_bias_risk(rolling_bias_pct_mean)
    autocorrelation_risk = classify_autocorrelation_risk(dw_stat)
    validation_method_label = classify_validation_method(fold_df, main_model_used_cv_fallback)

    # ---- Counterfactual Confidence: a priority-ordered cascade led by Rolling
    # Validation Error, with a short explanation of every check that contributed. See
    # combine_reliability_ratings() for the full cascade logic. ----
    reliability_components = {
        "rolling validation error": rolling_validation_error_risk,
        "overfitting gap": overfitting_risk,
        "autocorrelation risk": autocorrelation_risk,
        "rolling bias": rolling_bias_risk,
    }
    counterfactual_reliability = combine_reliability_ratings(reliability_components)
    reliability_drivers = get_reliability_drivers(reliability_components)

    return {
        "dates_pre": dates_pre,
        "y_pre": y_pre,
        "y_pred_pre": y_pred_pre,
        "corr": corr,
        "r2": r2,
        "smape": s,
        "rmse": rmse,
        "dw_stat": dw_stat,
        "autocorrelation_risk": autocorrelation_risk,
        "pre_residuals": pre_residuals,
        "holdout_smape_mean": holdout_smape_mean,
        "holdout_rmse_mean": holdout_rmse_mean,
        "rolling_origin_folds": fold_df,
        "rolling_smape_mean": rolling_smape_mean,
        "rolling_rmse_mean": rolling_rmse_mean,
        "rolling_smape_p90": rolling_smape_p90,
        "rolling_bias_pct_mean": rolling_bias_pct_mean,
        "rolling_validation_error_risk": rolling_validation_error_risk,
        "rolling_bias_risk": rolling_bias_risk,
        "rolling_uplift_error_pct_median": rolling_uplift_error_pct_median,
        "rolling_uplift_error_pct_lower": rolling_uplift_error_pct_lower,
        "rolling_uplift_error_pct_upper": rolling_uplift_error_pct_upper,
        "overfit_gap_smape": overfit_gap_smape,
        "overfit_gap_rmse": overfit_gap_rmse,
        "overfitting_risk": overfitting_risk,
        "validation_method_label": validation_method_label,
        "cv_status": cv_status,
        "used_cv_fallback": main_model_used_cv_fallback or (not fold_df.empty and bool(fold_df["used_cv_fallback"].any())),
        "main_model_used_cv_fallback": main_model_used_cv_fallback,
        "n_selected_features": n_selected_features,
        "n_pre_periods": n_pre_periods,
        "n_pre_weeks": n_pre_periods,  # backward-compatible alias
        "counterfactual_reliability": counterfactual_reliability,
        "reliability_drivers": reliability_drivers,
        "min_training_periods": min_training_periods,
        "min_training_weeks": min_training_periods,  # backward-compatible alias
        "validation_window_periods": cv_horizon,
        "validation_window_weeks": cv_horizon,  # backward-compatible alias
        "time_series_frequency": frequency_config["frequency"],
        "frequency_config": frequency_config,
        "lag_periods": lag_periods,
        "lag_label": frequency_config["lag_label"],
        "lag_drop_metadata": lag_drop_metadata,
        "matrix_diagnostics": matrix_diagnostics,
        "placebo_length_periods": placebo_len,
        "uplift": uplift,
        "uplift_pct": uplift_pct,
        "dates_test": dates_test,
        "y_test_actual": y_test_actual,
        "y_pred_test": y_pred_test,
        "dates_post": dates_post,
        "y_post_actual": y_post_actual,
        "y_post_pred": y_post_pred,
        "placebos": placebos,
        "placebo_uplift_pcts": placebo_uplift_pcts,
        "placebo_smapes": placebo_smapes,
        "placebo_rmses": placebo_rmses,
        "median_placebo_uplift": median_uplift,
        "placebo_range_lower": p2_5,
        "placebo_range_upper": p97_5,
        "median_placebo_uplift_pct": median_placebo_uplift_pct,
        "placebo_range_lower_pct": p2_5_pct,
        "placebo_range_upper_pct": p97_5_pct,
        "placebo_percentile_rank": percentile_rank,
        "placebo_p_value_one_sided": p_one_sided,
        "placebo_p_value_two_sided": p_two_sided,
        "placebo_z_score": z_score,
        "median_placebo_smape": median_placebo_smape,
        "p95_placebo_smape": p95_placebo_smape,
        "median_placebo_rmse": median_placebo_rmse,
        "p95_placebo_rmse": p95_placebo_rmse,
        "neg_pre": neg_pre,
        "neg_test": neg_test,
        "neg_post": neg_post,
        "selected_regions": selected,
        "selected_features": selected_features,
        "selected_df": selected_df,
        "n_candidates": n_candidates,
        "n_selected": n_selected,
        "n_removed": n_removed,
        "alpha": alpha,
        "control_list": control_list,
        "base_control_list": control_list,
        "include_lagged_controls": include_lagged_controls,
        "model_feature_cols": model_feature_cols,
        "lagged_feature_map": lagged_feature_map,
        "scaler": scaler,
        "model": model
    }


# ------------------------------------------------------------
# Text and column helpers
# ------------------------------------------------------------
def repair_text_value(v):
    if not isinstance(v, str):
        return v
    s = v.strip()
    try:
        repaired = s.encode("latin1").decode("utf-8")
        s = repaired
    except Exception:
        pass
    s = s.replace("--", "–")
    return unicodedata.normalize("NFC", s)

def clean_dataframe_text(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    obj_cols = df.select_dtypes(include=["object"]).columns
    for c in obj_cols:
        df[c] = df[c].map(repair_text_value)
    return df

def normalise_column_names(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, ~df.columns.str.contains("^Unnamed", case=False, na=False)]
    return df

def inspect_excel_sheet(path: str, sheet_name: str) -> Dict:
    try:
        df_raw = pd.read_excel(
            path,
            sheet_name=sheet_name,
            engine="calamine",
            header=None,
            dtype=str,
            keep_default_na=False
        )
        issues = []
        for row_idx, row in df_raw.iterrows():
            for col_idx, val in enumerate(row):
                if val and str(val).startswith('#'):
                    issues.append({'row': row_idx, 'col': col_idx, 'value': val})
        return {'has_issues': len(issues) > 0, 'issues': issues[:10], 'total_issues': len(issues)}
    except Exception as e:
        return {'has_issues': True, 'error': str(e)}

# ------------------------------------------------------------
# Excel workbook loading
# ------------------------------------------------------------
@st.cache_data(ttl=CONFIG["cache_ttl"])
def get_workbook_sheet_names(path: str) -> List[str]:
    xl = pd.ExcelFile(path, engine="calamine")
    return xl.sheet_names

@st.cache_data(ttl=CONFIG["cache_ttl"])
def load_market_sheet(path: str, sheet_name: str) -> pd.DataFrame:
    try:
        df = pd.read_excel(path, sheet_name=sheet_name, engine="calamine", dtype=str)
    except Exception as e:
        try:
            df = pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl", dtype=str)
        except Exception as e2:
            st.error(f"Failed to load sheet with both engines: {e2}")
            raise
    df = normalise_column_names(df)
    error_patterns = ['#N/A', '#DIV/0!', '#VALUE!', '#REF!', '#NAME?', '#NUM!', '#NULL!']
    df = df.replace(error_patterns, pd.NA)
    df = clean_dataframe_text(df)
    df = df.dropna(axis=1, how="all")
    df = df.dropna(axis=0, how="all")
    df["Market"] = sheet_name
    return df

def get_population_column(df: pd.DataFrame) -> str:
    if POPULATION_COL_RAW in df.columns:
        return POPULATION_COL_RAW
    if POPULATION_COL in df.columns:
        return POPULATION_COL
    candidates = [c for c in df.columns if c.strip().lower() in ["total population", "population"]]
    if candidates:
        return candidates[0]
    raise ValueError("Could not find a population column. Expected 'Total Population' or 'Population'.")

def get_base_geography_column(df: pd.DataFrame) -> str:
    non_market_cols = [c for c in df.columns if c != "Market"]
    if not non_market_cols:
        raise ValueError("Could not identify a base geography column.")
    return non_market_cols[0]

def get_grouping_columns(df: pd.DataFrame) -> List[str]:
    pop_col = get_population_column(df)
    pop_idx = list(df.columns).index(pop_col)
    pre_population_cols = list(df.columns[:pop_idx])
    grouping_cols = [c for c in pre_population_cols if c not in ["Market", ADOBE_COL]]
    grouping_cols = [c for c in grouping_cols if not c.lower().startswith("adobe")]
    if not grouping_cols:
        grouping_cols = [get_base_geography_column(df)]
    return grouping_cols

def standardise_population_column(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    pop_col = get_population_column(df)
    if pop_col != POPULATION_COL:
        df = df.rename(columns={pop_col: POPULATION_COL})
    return df

def get_numeric_metric_columns(df: pd.DataFrame, grouping_cols: List[str]) -> List[str]:
    categorical_keywords = ['area', 'region', 'county', 'city', 'district', 'borough',
                            'territory', 'province', 'state', 'country', 'name', 'code']
    excluded = set(grouping_cols + ["Market", ADOBE_COL, POPULATION_COL, POPULATION_COL_RAW])
    numeric_cols = []
    for c in df.columns:
        if c in excluded:
            continue
        if any(keyword in c.lower() for keyword in categorical_keywords):
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            numeric_cols.append(c)
        else:
            numeric_attempt = pd.to_numeric(df[c], errors='coerce')
            if numeric_attempt.notna().sum() > len(df[c]) * 0.5:
                numeric_cols.append(c)
    return numeric_cols

def prepare_market_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = standardise_population_column(df)
    if POPULATION_COL not in df.columns:
        raise ValueError("Population column not found after standardisation.")
    df[POPULATION_COL] = pd.to_numeric(df[POPULATION_COL], errors="coerce")
    grouping_cols = get_grouping_columns(df)
    excluded_cols = set(grouping_cols + ["Market", ADOBE_COL, POPULATION_COL])
    for c in df.columns:
        if c not in excluded_cols and c != POPULATION_COL:
            sample = df[c].dropna().head(10)
            if len(sample) > 0:
                sample_str = sample.astype(str)
                looks_numeric = sample_str.str.match(r'^[\d\-\.\,]+$').all()
                if looks_numeric:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[POPULATION_COL])
    df = df[df[POPULATION_COL] > 0]
    return df

# ------------------------------------------------------------
# Aggregation helpers
# ------------------------------------------------------------
def weighted_average_vectorized(df: pd.DataFrame, value_cols: List[str], weight_col: str) -> pd.Series:
    result_dict = {}
    if not value_cols:
        result_dict[weight_col] = df[weight_col].sum()
        return pd.Series(result_dict)
    weights = df[weight_col].values.reshape(-1, 1)
    values = df[value_cols].values
    valid_mask = pd.notna(df[value_cols]).values
    weighted_sums = np.where(valid_mask, values * weights, 0).sum(axis=0)
    weight_sums = np.where(valid_mask, weights, 0).sum(axis=0)
    results = np.divide(weighted_sums, weight_sums, out=np.full(weighted_sums.shape, np.nan, dtype=float), where=weight_sums != 0)
    result_dict.update(dict(zip(value_cols, results)))
    result_dict[weight_col] = df[weight_col].sum()
    return pd.Series(result_dict)

@st.cache_data(ttl=CONFIG["cache_ttl"])
def aggregate_market_data(market_df: pd.DataFrame, grouping_col: str, numeric_metric_cols: List[str]) -> pd.DataFrame:
    keep_cols = ["Market", grouping_col, POPULATION_COL] + numeric_metric_cols
    keep_cols = [c for c in keep_cols if c in market_df.columns]
    df = market_df[keep_cols].copy()
    df = df.dropna(subset=[grouping_col, POPULATION_COL])
    agg_df = df.groupby(grouping_col, dropna=True).apply(lambda x: weighted_average_vectorized(x, numeric_metric_cols, POPULATION_COL)).reset_index()
    agg_df["Market"] = market_df["Market"].iloc[0]
    ordered_cols = ["Market", grouping_col, POPULATION_COL] + numeric_metric_cols
    ordered_cols = [c for c in ordered_cols if c in agg_df.columns]
    return agg_df[ordered_cols]

def impute_missing_features(df: pd.DataFrame, feature_cols: List[str]) -> pd.DataFrame:
    df = df.copy()
    for c in feature_cols:
        if c in df.columns:
            median_val = df[c].median()
            if pd.isna(median_val):
                median_val = 0
            df[c] = df[c].fillna(median_val)
    return df

# ------------------------------------------------------------
# Matching metric helpers
# ------------------------------------------------------------
def weighted_profile(df: pd.DataFrame, features: List[str], population_col: str = POPULATION_COL) -> pd.Series:
    """Population-weighted feature means. Falls back to equal-weighted means
    if the population column is missing, all-NaN, or sums to zero/negative."""
    if population_col in df.columns:
        w = pd.to_numeric(df[population_col], errors="coerce")
        if w.notna().any() and w.fillna(0).sum() > 0:
            w = w.fillna(0).values
            return pd.Series({f: np.average(df[f].values, weights=w) for f in features}, index=features)
    return df[features].mean()

def fit_structural_stats(eligible_df: pd.DataFrame, features: List[str]):
    """Fit ONE structural mean/std basis on the eligible region universe
    (selected test regions + full control candidate pool) for this run.
    Using the same basis for every candidate group is what makes
    Weighted Structural Distance comparable across candidates of different sizes."""
    means = eligible_df[features].mean()
    stds = eligible_df[features].std(ddof=0)
    return means, stds

def calculate_metrics(test_df, control_df, features, weights_dict, eligible_means, eligible_stds, population_col=POPULATION_COL):
    """
    eligible_means / eligible_stds: dict-like {feature: value}, fitted ONCE per run
    via fit_structural_stats() on the eligible region universe — NOT refit per candidate.
    Returns a dict (not an ambiguous tuple).
    """
    empty = {
        "mean_abs_smd": 0.0, "weighted_structural_distance": 0.0, "smd_list": [],
        "test_means": np.array([]), "control_means": np.array([]),
        "raw_diffs": np.array([]), "weighted_contributions": np.array([]),
    }
    if not features:
        return empty
    test_df = impute_missing_features(test_df, features)
    control_df = impute_missing_features(control_df, features)
    # Guard: only use features present in both dataframes
    features = [f for f in features if f in test_df.columns and f in control_df.columns]
    if not features:
        return empty

    # Population-weighted structural profiles (falls back to equal-weighted mean internally)
    test_profile = weighted_profile(test_df, features, population_col)
    control_profile = weighted_profile(control_df, features, population_col)

    means_arr = np.array([eligible_means[f] for f in features], dtype=float)
    stds_arr = np.array([eligible_stds[f] for f in features], dtype=float)
    # Safe denominator for z-scoring only (never divide by 0); SMD uses the raw stds_arr below.
    z_scale = np.where((stds_arr > 0) & np.isfinite(stds_arr), stds_arr, 1.0)

    z_test = (test_profile.values - means_arr) / z_scale
    z_control = (control_profile.values - means_arr) / z_scale

    w_vector = np.array([weights_dict.get(f, 1.0) for f in features])
    sq_diff = (z_test - z_control) ** 2
    # weighted_structural_distance is the slider-weighted optimisation metric, scored on the
    # fixed eligible-pool basis so it is comparable across candidate groups of different sizes.
    weighted_contributions = w_vector * sq_diff
    weighted_structural_distance = float(np.sqrt(np.sum(weighted_contributions)))

    raw_diffs = test_profile.values - control_profile.values

    # mean_abs_smd is the unweighted diagnostic balance metric (no slider weights),
    # using the FULL eligible pool's std as a stable denominator — not the selected
    # group's own std, which can be 0 (or near-0) with a single test/control region.
    smd_list = []
    for i, f in enumerate(features):
        feature_scale = stds_arr[i]
        if feature_scale > 0 and np.isfinite(feature_scale):
            smd_list.append(abs(raw_diffs[i] / feature_scale))
        else:
            smd_list.append(np.nan)  # flagged: feature has zero/invalid variance across the eligible pool
    mean_abs_smd = float(np.nanmean(smd_list)) if smd_list else 0.0

    return {
        "mean_abs_smd": mean_abs_smd,
        "weighted_structural_distance": weighted_structural_distance,
        "smd_list": smd_list,
        "test_means": test_profile.values,
        "control_means": control_profile.values,
        "raw_diffs": raw_diffs,
        "weighted_contributions": weighted_contributions,
    }

@st.cache_data(ttl=CONFIG["cache_ttl"])
def calculate_metrics_cached(test_df, control_df, features_tuple, weights_tuple, eligible_means_tuple, eligible_stds_tuple):
    features = list(features_tuple)
    weights_dict = dict(zip(features, weights_tuple))
    eligible_means = dict(zip(features, eligible_means_tuple))
    eligible_stds = dict(zip(features, eligible_stds_tuple))
    return calculate_metrics(test_df, control_df, features, weights_dict, eligible_means, eligible_stds)

@st.cache_data(ttl=CONFIG["cache_ttl"])
def preprocess_data(pool_df, test_df_run, active_features, weights, eligible_means_tuple, eligible_stds_tuple):
    """Nearest-neighbour candidate search uses the SAME fixed eligible-pool basis
    (eligible_means/eligible_stds) as calculate_metrics(), so the NN ranking is
    consistent with the Weighted Structural Distance objective."""
    pool_df = impute_missing_features(pool_df, active_features)
    test_df_run = impute_missing_features(test_df_run, active_features)
    means_arr = np.array(eligible_means_tuple, dtype=float)
    stds_arr = np.array(eligible_stds_tuple, dtype=float)
    z_scale = np.where((stds_arr > 0) & np.isfinite(stds_arr), stds_arr, 1.0)
    w_vec = np.array([np.sqrt(weights.get(f, 1.0)) for f in active_features])
    p_scaled = ((pool_df[active_features].values - means_arr) / z_scale) * w_vec
    t_profile = weighted_profile(test_df_run, active_features, POPULATION_COL).values
    t_cent = (((t_profile - means_arr) / z_scale) * w_vec).reshape(1, -1)
    return w_vec, p_scaled, t_cent

def stochastic_genetic_search(
    pool_df,
    test_df_run,
    active_features,
    weights,
    n,
    calculate_metrics_fn,
    eligible_means,
    eligible_stds,
    nn_start_idx,
    n_iterations=1000,
    random_state=42,
):
    """
    Stochastic (Genetic Search) — the "Advanced (Thorough)" matching strategy.

    Starts from a good nearest-neighbour candidate group, then repeatedly swaps one
    selected control for one unselected control at random. Candidate groups are scored
    on Weighted Structural Distance (optimisation objective; Mean Abs SMD is diagnostic
    only). Swaps that improve the score are kept; the best group found is tracked and
    returned. Reproducible via a fixed random seed.

    Returns:
        best_idx: list of selected control indices for this n
        best_metrics: the metrics dict for best_idx (from calculate_metrics_fn)
        evaluated_count: number of candidate groups scored during the search
        convergence: list of best Weighted Structural Distance values over the search
    """
    pool_indices = list(pool_df.index)
    evaluated_count = 0
    convergence = []
    rng = np.random.default_rng(random_state)

    if n <= 0 or n > len(pool_indices):
        empty_metrics = calculate_metrics_fn(test_df_run, pool_df.loc[[]], active_features, weights, eligible_means, eligible_stds)
        return [], empty_metrics, 0, convergence

    def score(idx_list):
        nonlocal evaluated_count
        metrics = calculate_metrics_fn(test_df_run, pool_df.loc[idx_list], active_features, weights, eligible_means, eligible_stds)
        evaluated_count += 1
        return metrics["weighted_structural_distance"], metrics

    # ---- Start from a good nearest-neighbour candidate group ----
    current_idx = list(nn_start_idx)
    current_score, current_metrics = score(current_idx)
    convergence.append(current_score)

    best_idx = list(current_idx)
    best_score = current_score
    best_metrics = current_metrics

    for _iteration in range(n_iterations):
        available = [idx for idx in pool_indices if idx not in current_idx]
        if not available or not current_idx:
            break
        remove_idx = current_idx[rng.integers(0, len(current_idx))]
        add_idx = available[rng.integers(0, len(available))]
        candidate_idx = [idx for idx in current_idx if idx != remove_idx] + [add_idx]
        cand_score, cand_metrics = score(candidate_idx)
        # Keep swaps that improve the score (Weighted Structural Distance).
        if cand_score < current_score:
            current_idx, current_score, current_metrics = candidate_idx, cand_score, cand_metrics
            if current_score < best_score:
                best_score = current_score
                best_idx = list(current_idx)
                best_metrics = current_metrics
            convergence.append(current_score)

    return best_idx, best_metrics, evaluated_count, convergence

# ------------------------------------------------------------
# Validation and display helpers
# ------------------------------------------------------------
def validate_data(df, required_cols, geo_col=None, market=None, level=None):
    issues = []
    recommendations = []
    if len(df) == 0:
        issues.append("❌ No data available for the selected filters")
        recommendations.append("💡 Try a different market or geography grouping")
        return issues, recommendations
    if not required_cols:
        issues.append("⚠️ No numeric matching features detected")
        recommendations.append("💡 Check that demographic columns are numeric")
        return issues, recommendations
    missing_pct = df[required_cols].isnull().mean() * 100
    high_missing = missing_pct[missing_pct > CONFIG["missing_threshold"]]
    if len(high_missing) > 0:
        issues.append(f"📊 High missing values (> {CONFIG['missing_threshold']}%): {dict(high_missing)}")
        recommendations.append(f"💡 Consider removing from matching: {', '.join(high_missing.index[:3])}")
    constant_cols = []
    for col in required_cols:
        if df[col].nunique(dropna=False) <= 1:
            constant_cols.append(col)
    if constant_cols:
        issues.append(f"⚠️ Constant features detected: {constant_cols[:5]}")
        recommendations.append(f"💡 Remove these features because they do not help matching: {', '.join(constant_cols[:3])}")
    outlier_dict = {}
    for col in required_cols:
        if df[col].count() > 10:
            clean_data = df[col].dropna()
            if len(clean_data) > 0 and clean_data.std() > 0:
                z_scores = np.abs(stats.zscore(clean_data))
                outlier_mask = z_scores > CONFIG["outlier_std_threshold"]
                if outlier_mask.any():
                    outlier_indices = clean_data.index[outlier_mask]
                    if geo_col and len(outlier_indices) > 0:
                        outlier_regions = df.loc[outlier_indices, geo_col].tolist()
                    else:
                        outlier_regions = ["Unknown"]
                    outlier_dict[col] = outlier_regions[:3]
    if outlier_dict:
        issues.append(f"🔴 Extreme outliers detected (> {CONFIG['outlier_std_threshold']} std dev)")
        for col, regions in list(outlier_dict.items())[:3]:
            issues.append(f"   • {col}: {', '.join(str(r) for r in regions)}")
        recommendations.append("💡 Investigate outlier regions for data errors or consider excluding them")
    if len(df) < 3:
        issues.append(f"⚠️ Very small sample size: {len(df)} geographies")
        recommendations.append("💡 Try a more granular geography grouping, if available")
    return issues, recommendations

def reset_results():
    st.session_state.final_controls = None
    st.session_state.test_df = None
    st.session_state.opt_results = {}
    st.session_state.match_mode_res = None
    st.session_state.best_n = None
    st.session_state.w_reset = st.session_state.get("w_reset", 0) + 1
    st.session_state.guided_share_info = None
    st.session_state.selected_experiment_regions = []
    st.session_state.user_selected_mode = False
    st.session_state.user_control_geos = []
    st.session_state.match_run_snapshot = None
    st.session_state.match_run_metrics = None
    st.session_state.match_results_stale = False
    # Test/control regions are changing — any downstream time-series validation and
    # Bayesian TBR results were computed against the old region set and are now stale.
    st.session_state.validation_results = None
    st.session_state.validation_triggered = False
    st.session_state.bayesian_results = None
    st.session_state.bayesian_interpretation_visible = False

def reset_manual_results():
    """Clear matching results but keep manual selections (test/control geos)."""
    st.session_state.final_controls = None
    st.session_state.test_df = None
    st.session_state.opt_results = {}
    st.session_state.match_mode_res = None
    st.session_state.best_n = None
    st.session_state.guided_share_info = None
    st.session_state.selected_experiment_regions = []
    st.session_state.match_run_snapshot = None
    st.session_state.match_run_metrics = None
    st.session_state.match_results_stale = False
    # Do NOT reset user_control_geos or user_selected_mode
    # Test/control regions are changing — any downstream time-series validation and
    # Bayesian TBR results were computed against the old region set and are now stale.
    st.session_state.validation_results = None
    st.session_state.validation_triggered = False
    st.session_state.bayesian_results = None
    st.session_state.bayesian_interpretation_visible = False

def matching_setup_changed_since_last_run(run_snapshot, market, geography_level, match_mode, test_geos, weights):
    """
    Compare the CURRENT live setup against the frozen snapshot saved at the time of the
    last completed Run Match Analysis click. Returns True if anything that would affect
    the displayed results (market, geography level, strategy, test regions, or slider
    weights) has changed since that run, so the UI can warn the user that the cards below
    are stale rather than silently recomputing them from live widget state.
    """
    if not run_snapshot:
        return False
    if market != run_snapshot.get("market"):
        return True
    if geography_level != run_snapshot.get("geography_level"):
        return True
    if match_mode != run_snapshot.get("match_mode"):
        return True
    if set(test_geos) != set(run_snapshot.get("test_geos", [])):
        return True
    run_weights = run_snapshot.get("weights", {}) or {}
    if dict(weights) != dict(run_weights):
        return True
    return False

def is_proportion_series(series):
    s = series.dropna()
    if s.empty:
        return False
    return (s.min() >= 0) and (s.max() <= 1)

def format_numeric_value(col_name, val, proportion_cols):
    if pd.isna(val):
        return ""
    if col_name == "Population Density":
        return f"{val:,.1f}"
    if col_name in proportion_cols:
        return f"{val * 100:.1f}%"
    if abs(val) >= 1000:
        return f"{val:,.1f}"
    if abs(val) >= 10:
        return f"{val:.2f}"
    return f"{val:.3f}"

def format_display_df(df, proportion_cols):
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_numeric_dtype(out[c]):
            out[c] = out[c].apply(lambda v: format_numeric_value(c, v, proportion_cols))
    return out

def standardize_column_order(df, geo_col, active_features):
    base_order = ["Market", geo_col, POPULATION_COL]
    if "Population Density" in df.columns:
        base_order.append("Population Density")
    remaining = [c for c in df.columns if c not in base_order]
    ordered = [c for c in base_order if c in df.columns] + remaining
    return df[ordered]

def calculate_experiment_population_coverage(test_regions, agg_df, geo_col, total_market_pop):
    if total_market_pop <= 0 or not test_regions:
        return 0.0
    test_pop = agg_df[agg_df[geo_col].isin(test_regions)][POPULATION_COL].sum()
    return (test_pop / total_market_pop) * 100

def cleanup_session_state():
    if st.session_state.get("final_controls") is not None and len(st.session_state.final_controls) > 100:
        df = st.session_state.final_controls
        # Always keep geo_col and POPULATION_COL; fill remaining slots with other feature cols
        must_keep = [c for c in [geo_col, POPULATION_COL] if c in df.columns]
        other_cols = [c for c in df.columns if c not in must_keep]
        keep = must_keep + list(other_cols[: CONFIG["max_display_features"]])
        st.session_state.final_controls = df[keep]

# ------------------------------------------------------------
# Guided experiment group search
# ------------------------------------------------------------
def find_guided_test_group(agg_df, geo_col, total_market_pop,
                           force_exp_include, force_exp_exclude,
                           force_ctrl_include, force_ctrl_exclude,
                           target_share, tolerance_pp, search_iterations=2000):
    all_geos = set(agg_df[geo_col].unique())
    forced_test = set(force_exp_include)
    forbidden_test = set(force_exp_exclude) | set(force_ctrl_include)
    candidate = list(all_geos - forced_test - forbidden_test - set(force_ctrl_exclude))
    pop_map = agg_df.set_index(geo_col)[POPULATION_COL].to_dict()
    if any(g not in all_geos for g in forced_test):
        return [], 0, False
    forced_pop = sum(pop_map.get(g, 0) for g in forced_test)
    low = max(0, target_share - tolerance_pp) / 100
    high = min(100, target_share + tolerance_pp) / 100
    best_set = list(forced_test)
    best_share = (forced_pop / total_market_pop) if total_market_pop > 0 else 0
    best_dist = min(abs(best_share - low), abs(best_share - high)) if not (low <= best_share <= high) else 0
    met = low <= best_share <= high
    if len(candidate) <= 16:
        from itertools import combinations
        for r in range(len(candidate) + 1):
            for comb in combinations(candidate, r):
                trial = list(forced_test | set(comb))
                share = agg_df[agg_df[geo_col].isin(trial)][POPULATION_COL].sum() / total_market_pop if total_market_pop > 0 else 0
                d = 0 if (low <= share <= high) else min(abs(share - low), abs(share - high))
                if (d < best_dist) or (d == best_dist and abs(share - (target_share / 100)) < abs(best_share - (target_share / 100))):
                    best_set, best_share, best_dist = trial, share, d
                    met = low <= share <= high
    else:
        for _ in range(search_iterations):
            k = random.randint(0, len(candidate))
            sampled = random.sample(candidate, k)
            trial = list(forced_test | set(sampled))
            share = agg_df[agg_df[geo_col].isin(trial)][POPULATION_COL].sum() / total_market_pop if total_market_pop > 0 else 0
            d = 0 if (low <= share <= high) else min(abs(share - low), abs(share - high))
            if (d < best_dist) or (d == best_dist and abs(share - (target_share / 100)) < abs(best_share - (target_share / 100))):
                best_set, best_share, best_dist = trial, share, d
                met = low <= share <= high
    return best_set, best_share, met

# ------------------------------------------------------------
# Session state initialisation
# ------------------------------------------------------------
if "final_controls" not in st.session_state:
    st.session_state.final_controls = None
if "test_df" not in st.session_state:
    st.session_state.test_df = None
if "opt_results" not in st.session_state:
    st.session_state.opt_results = {}
if "match_mode_res" not in st.session_state:
    st.session_state.match_mode_res = None
if "best_n" not in st.session_state:
    st.session_state.best_n = None
if "w_reset" not in st.session_state:
    st.session_state.w_reset = 0
if "guided_share_info" not in st.session_state:
    st.session_state.guided_share_info = None
if "selected_experiment_regions" not in st.session_state:
    st.session_state.selected_experiment_regions = []
if "user_selected_mode" not in st.session_state:
    st.session_state.user_selected_mode = False
if "user_control_geos" not in st.session_state:
    st.session_state.user_control_geos = []
if "match_run_snapshot" not in st.session_state:
    st.session_state.match_run_snapshot = None
if "match_run_metrics" not in st.session_state:
    st.session_state.match_run_metrics = None
if "match_results_stale" not in st.session_state:
    st.session_state.match_results_stale = False

# ------------------------------------------------------------
# Load workbook and market
# ------------------------------------------------------------
try:
    available_markets = sorted(get_workbook_sheet_names(DATA_PATH))
except Exception as e:
    st.error("We couldn't load the geography/population data file this app relies on. Please check that the data file is present and correctly formatted.")
    with st.expander("Technical details"):
        st.code(f"{type(e).__name__}: {e}")
    st.stop()

_default_market_index = available_markets.index("UK") if "UK" in available_markets else 0

with st.sidebar:
    st.header("1. Geography")
    market = st.selectbox("Market", available_markets, index=_default_market_index, on_change=reset_results,
                          help="Select the market whose regions you want to use for geo-testing.")

try:
    market_df_raw = load_market_sheet(DATA_PATH, market)
    market_df = prepare_market_dataframe(market_df_raw)
    grouping_options = get_grouping_columns(market_df)
except Exception as e:
    st.error(f"We couldn't prepare the data for market '{market}'. Please check that this market's sheet is formatted correctly.")
    with st.expander("Technical details"):
        st.code(f"{type(e).__name__}: {e}")
    st.stop()

with st.sidebar:
    geography_level = st.selectbox("Geography Level", grouping_options, on_change=reset_results,
                                   help="The geographic unit to match on — e.g. region, state, or city.")
    st.write("---")
    st.header("2. Matching Strategy")
    strategy_labels = {
        "Basic (Fast)": "Greedy (Nearest Neighbor)",
        "Intermediate (Balanced)": "Refined Greedy (Hill Climbing)",
        "Advanced (Thorough)": "Stochastic (Genetic Search)",
    }
    strategy_choice = st.radio("Strategy", list(strategy_labels.keys()), index=0, on_change=reset_results,
                               help="Controls how thoroughly GeoMatch searches for the best control group.\n\n"
                                    "**Basic** uses nearest-neighbour matching — fast but may miss better combinations.\n\n"
                                    "**Intermediate** refines the nearest-neighbour result by trying local swaps.\n\n"
                                    "**Advanced** uses stochastic swap search across many candidate combinations. It is slower than Intermediate, but explores more possible control groups without exhaustively testing every combination.")
    match_mode = strategy_labels[strategy_choice]

# ------------------------------------------------------------
# Aggregate selected market
# ------------------------------------------------------------
geo_col = geography_level
active_features = get_numeric_metric_columns(market_df, grouping_options)

agg_df = aggregate_market_data(market_df=market_df, grouping_col=geo_col, numeric_metric_cols=active_features)
agg_df = impute_missing_features(agg_df, active_features)
agg_df = agg_df.dropna(subset=[geo_col, POPULATION_COL])
agg_df = agg_df[agg_df[POPULATION_COL] > 0]

total_market_pop = agg_df[POPULATION_COL].sum()

# Data quality check – also warn about high missingness in features
validation_issues, recommendations = validate_data(agg_df, active_features, geo_col=geo_col, market=market, level=geography_level)
issue_severity = "🔴 High" if len(validation_issues) > 3 else "🟡 Medium" if len(validation_issues) > 0 else "🟢 None"

# =============================================================================
# Main app – Tabs
# =============================================================================
tab1, tab2, tab3, tab4 = st.tabs([
    "⚙️ Region Matching",
    "🔍 Validate Test Design",
    "📊 Measure Test Impact",
    "🧠 Bayesian TBR"
])

# =============================================================================
# TAB 1: MATCHING SETUP
# =============================================================================
with tab1:
    # ------------------------------------------------------------
    # Preview data
    # ------------------------------------------------------------
    with st.expander(f"Preview data: {market} ({geography_level})", expanded=False):
        proportion_cols = {c for c in active_features if c in agg_df.columns and is_proportion_series(agg_df[c])}
        preview_df = standardize_column_order(agg_df, geo_col, active_features)
        st.dataframe(format_display_df(preview_df, proportion_cols), width='stretch', height=240)

    # ------------------------------------------------------------
    # Matching setup
    # ------------------------------------------------------------
    st.markdown('<div class="custom-divider"></div>', unsafe_allow_html=True)
    st.subheader("🧩 MATCHING SETUP")
    setup_mode = st.radio(
        "Setup Mode",
        [
            "Manual Selection (Pick Both)",
            "Pick Test, Auto‑Match Controls",
            "Set Rules & Auto‑Build Groups"
        ],
        horizontal=True,
        help="Choose how to define your test and control groups.\n\n"
             "**Manual Selection** — you pick both groups directly, no automated matching.\n\n"
             "**Pick Test, Auto‑Match Controls** — you choose the test regions and the app finds the best-matched controls.\n\n"
             "**Set Rules & Auto‑Build Groups** — define inclusion/exclusion rules and the app builds both groups."
    )
    st.markdown("<div style='margin: 0.6rem 0;'></div>", unsafe_allow_html=True)
    st.markdown('<div class="custom-divider"></div>', unsafe_allow_html=True)
    st.markdown("<div style='margin: 0.4rem 0;'></div>", unsafe_allow_html=True)

    # ----------------------------------------------------------------------
    # Three-mode UI
    # ----------------------------------------------------------------------
    sel_col1, sel_col2 = st.columns(2, gap="large")
    all_geo_values = sorted(agg_df[geo_col].dropna().unique())

    # ----------------------------------------------------------------------
    # COLUMN 1 – Test Group
    # ----------------------------------------------------------------------
    with sel_col1:
        st.subheader("A. Test Group")

        total_pop = agg_df[POPULATION_COL].sum()
        geo_options_with_pop = []
        for geo in all_geo_values:
            geo_pop = agg_df[agg_df[geo_col] == geo][POPULATION_COL].sum()
            pop_pct = (geo_pop / total_pop) * 100
            geo_options_with_pop.append(f"{geo} ({pop_pct:.1f}%)")
        label_to_geo = {label: geo for label, geo in zip(geo_options_with_pop, all_geo_values)}

        if setup_mode == "Manual Selection (Pick Both)":
            st.markdown("Select geographies to <span style='color:#15803d;font-weight:600'>include</span> in test group:", unsafe_allow_html=True)
            selected_test_labels = st.multiselect(
                "test_geos_manual",
                geo_options_with_pop,
                on_change=reset_manual_results,
                help="Population percentage of total market shown in brackets. These will be the test regions.",
                label_visibility="collapsed"
            )
            test_geos = [label_to_geo[label] for label in selected_test_labels]
            if test_geos:
                test_pop_pct = calculate_experiment_population_coverage(test_geos, agg_df, geo_col, total_market_pop)
                st.metric(
                    "Test group market population included",
                    f"{test_pop_pct:.1f}%",
                    help="Percentage of the total market population covered by the selected test regions."
                )
            force_exp_include = []
            force_exp_exclude = []
            force_ctrl_include = []
            force_ctrl_exclude = []
            target_test_share = 25
            target_tolerance_pp = 5
            guided_iterations = 2000

        elif setup_mode == "Pick Test, Auto‑Match Controls":
            st.markdown("Select geographies to <span style='color:#15803d;font-weight:600'>include</span> in test group:", unsafe_allow_html=True)
            selected_labels = st.multiselect(
                "select_geographies",
                geo_options_with_pop,
                on_change=reset_results,
                help="Population percentage of total market shown in brackets",
                label_visibility="collapsed"
            )
            test_geos = [label_to_geo[label] for label in selected_labels]
            if test_geos:
                test_pop_pct = calculate_experiment_population_coverage(test_geos, agg_df, geo_col, total_market_pop)
                st.metric(
                    "Test group market population included",
                    f"{test_pop_pct:.1f}%",
                    help="Percentage of the total market population covered by the selected test geographies. Larger test groups are typically more representative of the overall market, but leave fewer regions available for control selection."
                )
            force_exp_include = []
            force_exp_exclude = []
            force_ctrl_include = []
            force_ctrl_exclude = []
            target_test_share = 25
            target_tolerance_pp = 5
            guided_iterations = 2000

        else:  # "Set Rules & Auto‑Build Groups"
            st.markdown("Test geographies to force <span style='color:#15803d;font-weight:600'>include:</span>", unsafe_allow_html=True)
            selected_include_labels = st.multiselect(
                "exp_include",
                geo_options_with_pop,
                label_visibility="collapsed",
                key="exp_include_select"
            )
            force_exp_include = [label_to_geo[label] for label in selected_include_labels]
            exclude_options = [label for label in geo_options_with_pop if label_to_geo[label] not in force_exp_include]
            st.markdown("Test geographies to force <span style='color:#dc2626;font-weight:600'>exclude:</span>", unsafe_allow_html=True)
            selected_exclude_labels = st.multiselect(
                "exp_exclude",
                exclude_options,
                label_visibility="collapsed",
                key="exp_exclude_select"
            )
            force_exp_exclude = [label_to_geo[label] for label in selected_exclude_labels]
            force_ctrl_include = []
            force_ctrl_exclude = []
            target_test_share = st.slider(
                "Target test population share",
                5, 80, 25, 1,
                help="Desired percentage of the total market population to include in the test group. A larger test group is more representative but leaves fewer regions available as controls.",
                key="target_share_slider"
            )
            target_tolerance_pp = st.slider(
                "Population share tolerance (± pp)",
                1, 30, 5, 1,
                help="Acceptable deviation from the target population share, in percentage points.",
                key="tolerance_slider"
            )
            guided_iterations = st.slider(
                "Search intensity",
                500, 10000, 2000, 500,
                help="Number of candidate test groups evaluated. Higher values increase the chance of finding a better group but take longer to run.",
                key="guided_iterations_slider"
            )
            test_geos = []  # filled later

    # ----------------------------------------------------------------------
    # COLUMN 2 – Control Group
    # ----------------------------------------------------------------------
    with sel_col2:
        st.subheader("B. Control Group")

        if setup_mode == "Manual Selection (Pick Both)":
            pot_pool = [g for g in all_geo_values if g not in test_geos]
            pot_pool_with_pop = []
            label_to_geo_pool = {}
            for geo in pot_pool:
                geo_pop = agg_df[agg_df[geo_col] == geo][POPULATION_COL].sum()
                pop_pct = (geo_pop / total_pop) * 100
                pot_pool_with_pop.append(f"{geo} ({pop_pct:.1f}%)")
                label_to_geo_pool[f"{geo} ({pop_pct:.1f}%)"] = geo
            st.markdown("Select geographies to <span style='color:#15803d;font-weight:600'>include</span> in control group:", unsafe_allow_html=True)
            selected_control_labels = st.multiselect(
                "control_geos_manual",
                pot_pool_with_pop,
                on_change=reset_manual_results,
                help="Population percentage of total market shown in brackets. Only geographies not already in the test group are shown.",
                label_visibility="collapsed"
            )
            control_geos = [label_to_geo_pool[label] for label in selected_control_labels]
            st.session_state.user_control_geos = control_geos
            st.session_state.user_selected_mode = True
            force_ctrl_exclude = []
            force_ctrl_include = []
            control_pool_geos = []

        elif setup_mode == "Pick Test, Auto‑Match Controls":
            total_pop = agg_df[POPULATION_COL].sum()
            pot_pool = [g for g in all_geo_values if g not in test_geos]
            pot_pool_with_pop = []
            label_to_geo_pool = {}
            for geo in pot_pool:
                geo_pop = agg_df[agg_df[geo_col] == geo][POPULATION_COL].sum()
                pop_pct = (geo_pop / total_pop) * 100
                pot_pool_with_pop.append(f"{geo} ({pop_pct:.1f}%)")
                label_to_geo_pool[f"{geo} ({pop_pct:.1f}%)"] = geo
            st.markdown("Select geographies to <span style='color:#dc2626;font-weight:600'>exclude</span> from control pool:", unsafe_allow_html=True)
            excluded_labels = st.multiselect(
                "exclude_geographies",
                pot_pool_with_pop,
                on_change=reset_results,
                key="exclude_geos_select",
                label_visibility="collapsed"
            )
            excluded_geos = [label_to_geo_pool[label] for label in excluded_labels]
            st.session_state.force_ctrl_exclude = excluded_geos
            control_pool_geos = [g for g in pot_pool if g not in excluded_geos]
            force_ctrl_include = []
            force_ctrl_exclude = []

        else:  # "Set Rules & Auto‑Build Groups"
            total_pop = agg_df[POPULATION_COL].sum()
            force_ctrl_exclude = st.session_state.get("force_ctrl_exclude", [])
            eligible_for_control = [g for g in all_geo_values if g not in force_exp_include and g not in force_ctrl_exclude]
            ctrl_options_with_pop = []
            label_to_ctrl = {}
            for geo in eligible_for_control:
                geo_pop = agg_df[agg_df[geo_col] == geo][POPULATION_COL].sum()
                pop_pct = (geo_pop / total_pop) * 100
                ctrl_options_with_pop.append(f"{geo} ({pop_pct:.1f}%)")
                label_to_ctrl[f"{geo} ({pop_pct:.1f}%)"] = geo
            st.markdown("Control geographies to force <span style='color:#15803d;font-weight:600'>include:</span>", unsafe_allow_html=True)
            selected_ctrl_include_labels = st.multiselect(
                "ctrl_include",
                ctrl_options_with_pop,
                label_visibility="collapsed",
                key="ctrl_include_select"
            )
            force_ctrl_include = [label_to_ctrl[label] for label in selected_ctrl_include_labels]
            exclude_ctrl_options = [label for label in ctrl_options_with_pop if label_to_ctrl[label] not in force_ctrl_include]
            st.markdown("Control geographies to force <span style='color:#dc2626;font-weight:600'>exclude:</span>", unsafe_allow_html=True)
            selected_ctrl_exclude_labels = st.multiselect(
                "ctrl_exclude",
                exclude_ctrl_options,
                label_visibility="collapsed",
                help="These geographies cannot be used in control selection.",
                key="ctrl_exclude_select"
            )
            force_ctrl_exclude = [label_to_ctrl[label] for label in selected_ctrl_exclude_labels]
            st.session_state.force_ctrl_exclude = force_ctrl_exclude
            eligible_for_control = [g for g in all_geo_values if g not in force_exp_include and g not in force_ctrl_exclude]
            control_pool_geos = eligible_for_control

    if "force_ctrl_exclude" not in st.session_state:
        st.session_state.force_ctrl_exclude = []

    # ------------------------------------------------------------
    # Sidebar strategy parameters (keep in sidebar — do NOT move)
    # ------------------------------------------------------------
    with st.sidebar:
        st.write("---")
        st.header("3. Strategy Parameters")
        force_1to1 = st.checkbox("Force 1-to-1 Match Ratio", value=False)

        if setup_mode == "Manual Selection (Pick Both)":
            max_possible_controls = 0
            min_p, max_p = 0, 0
            st.info("In Manual Selection mode, you select both test and control groups directly. The matching algorithm is bypassed.")
        else:
            max_possible_controls = min(len(control_pool_geos), CONFIG["max_control_pool_size"])
            min_p, max_p = 0, 0
            if not force_1to1:
                if max_possible_controls < 2:
                    st.warning("Not enough control geographies available for a pool search.")
                else:
                    default_lower = max(2, int(np.ceil(max_possible_controls / 2)))
                    default_upper = max_possible_controls
                    if default_lower > default_upper:
                        default_lower = default_upper
                    pool_range = st.slider(
                        "Select control group pool size range:",
                        min_value=2,
                        max_value=max_possible_controls,
                        value=(default_lower, default_upper),
                        key=f"pool_slider_{max_possible_controls}",
                        help="The algorithm tests every control group size in this range and selects the one with the best pre-period balance. A wider range is more thorough but slower."
                    )
                    min_p, max_p = pool_range

        if match_mode == "Stochastic (Genetic Search)":
            genetic_iterations = st.slider(
                "Search iterations",
                min_value=CONFIG["genetic_iterations"]["min"],
                max_value=CONFIG["genetic_iterations"]["max"],
                value=CONFIG["genetic_iterations"]["default"],
                step=100,
                help="Number of random single-swap trials the stochastic search runs per control-group size. Higher values search more combinations but take longer."
            )
        else:
            genetic_iterations = CONFIG["genetic_iterations"]["default"]

        st.write("---")
        st.header("4. Matching Feature Importance")
        st.caption(f"📊 **{len(active_features)} numeric features** available for weighting")
        if "current_weights" not in st.session_state:
            st.session_state.current_weights = {f: 1 for f in active_features}
        preset_col1, preset_col2 = st.columns(2)
        with preset_col1:
            if st.button("🗑️ Reset All Weights to 1", width='stretch', key="reset_all_weights"):
                for f in active_features:
                    st.session_state.current_weights[f] = 1
                st.session_state.w_reset += 1
                st.rerun()
        with preset_col2:
            if st.button("👴 Older Pop. Focus (50+)", width='stretch', key="senior_focus"):
                for f in active_features:
                    st.session_state.current_weights[f] = 1
                for f in active_features:
                    if "50-64" in f or "65+" in f or "65 plus" in f.lower():
                        st.session_state.current_weights[f] = 8
                st.session_state.w_reset += 1
                st.rerun()
        if st.button("Reset Slider Positions", width='stretch', key="reset_sliders"):
            st.session_state.w_reset += 1
            st.rerun()
        weights = {}
        with st.expander("Demographic Importance Weights", expanded=False):
            search_term = st.text_input("🔍 Filter features", placeholder="Type to search...", key=f"weight_search_{st.session_state.w_reset}")
            ordered_features = active_features.copy()
            if search_term:
                ordered_features = [f for f in ordered_features if search_term.lower() in f.lower()]
                st.caption(f"Showing {len(ordered_features)} of {len(active_features)} features")
            container_height = min(500, max(200, len(ordered_features) * 35))
            with st.container(height=container_height):
                num_columns = 2 if len(ordered_features) > 15 else 1
                cols = st.columns(num_columns)
                for idx, f in enumerate(ordered_features):
                    col_idx = idx % num_columns
                    with cols[col_idx]:
                        current_val = st.session_state.current_weights.get(f, 1)
                        display_name = f.replace('_', ' ').title() if '_' in f else f
                        if current_val != 1:
                            display_name = f"⭐ {display_name}"
                        weight_val = st.slider(display_name, 1, 10, current_val, 1,
                                               key=f"w_{market}_{geography_level}_{f}_{st.session_state.w_reset}",
                                               help=f"Weight for {f} (higher = more important for matching)")
                        st.session_state.current_weights[f] = weight_val
                        weights[f] = weight_val
        for f in active_features:
            if f not in weights:
                weights[f] = st.session_state.current_weights.get(f, 1)
        non_default_weights = {k: v for k, v in weights.items() if v != 1}
        if non_default_weights:
            with st.expander(f"⚡ Active Overrides ({len(non_default_weights)} features)", expanded=False):
                for feature, weight in list(non_default_weights.items())[:10]:
                    st.caption(f"**{feature}**: weight = {weight}")
                if len(non_default_weights) > 10:
                    st.caption(f"... and {len(non_default_weights) - 10} more")

    # ------------------------------------------------------------
    # Run matching
    # ------------------------------------------------------------
    st.markdown("<p class='small-muted'>Tip: start with equal weights, then increase business-critical features if needed.</p>", unsafe_allow_html=True)
    run_clicked = st.button("▶ Run Match Analysis", width='stretch', type="primary")

    if run_clicked:
        if not active_features:
            st.error("No numeric matching features were found for this market and geography level.")
            st.stop()

        if setup_mode == "Manual Selection (Pick Both)":
            control_geos = st.session_state.get("user_control_geos", [])
            if len(test_geos) == 0:
                st.error("Please select at least one test geography.")
                st.stop()
            if len(control_geos) == 0:
                st.error("Please select at least one control geography.")
                st.stop()
            overlap = set(test_geos) & set(control_geos)
            if overlap:
                st.error(f"Overlapping geographies: {overlap}. Test and control groups must be disjoint.")
                st.stop()

            st.session_state.selected_experiment_regions = list(test_geos)
            st.session_state.test_df = agg_df[agg_df[geo_col].isin(test_geos)].copy()
            st.session_state.final_controls = agg_df[agg_df[geo_col].isin(control_geos)].copy()
            st.session_state.match_mode_res = "User Selected"
            st.session_state.best_n = len(control_geos)
            st.session_state.opt_results = {}
            st.session_state.user_selected_mode = True
            _eligible_df = pd.concat([st.session_state.test_df, st.session_state.final_controls], axis=0)
            _eligible_df = impute_missing_features(_eligible_df, active_features)
            _elig_means, _elig_stds = fit_structural_stats(_eligible_df, active_features)
            st.session_state.eligible_means = {f: float(_elig_means[f]) for f in active_features}
            st.session_state.eligible_stds = {f: float(_elig_stds[f]) for f in active_features}

            # ---- Freeze a snapshot of the inputs/outputs used for this run ----
            # so slider changes afterwards don't silently change the displayed results.
            _final_metrics = calculate_metrics(
                st.session_state.test_df, st.session_state.final_controls,
                active_features, weights, st.session_state.eligible_means, st.session_state.eligible_stds
            )
            _eligible_market_pop = agg_df[POPULATION_COL].sum()
            _experiment_pop = agg_df[agg_df[geo_col].isin(test_geos)][POPULATION_COL].sum()
            _control_pop = agg_df[agg_df[geo_col].isin(control_geos)][POPULATION_COL].sum()
            _test_pop_pct = (_experiment_pop / _eligible_market_pop) * 100 if _eligible_market_pop > 0 else 0
            _control_pop_pct = (_control_pop / _eligible_market_pop) * 100 if _eligible_market_pop > 0 else 0
            st.session_state.match_run_snapshot = {
                "market": market,
                "geography_level": geography_level,
                "geo_col": geo_col,
                "setup_mode": setup_mode,
                "match_mode": "User Selected",
                "test_geos": list(test_geos),
                "control_pool_geos": [],
                "force_ctrl_exclude": list(st.session_state.get("force_ctrl_exclude", [])),
                "active_features": list(active_features),
                "weights": dict(weights),
                "eligible_means": tuple(st.session_state.eligible_means.get(f, np.nan) for f in active_features),
                "eligible_stds": tuple(st.session_state.eligible_stds.get(f, np.nan) for f in active_features),
                "best_n": len(control_geos),
            }
            st.session_state.match_run_metrics = {
                "weighted_structural_distance": _final_metrics["weighted_structural_distance"],
                "mean_abs_smd": _final_metrics["mean_abs_smd"],
                "smd_list": _final_metrics["smd_list"],
                "test_means": _final_metrics["test_means"],
                "control_means": _final_metrics["control_means"],
                "raw_diffs": _final_metrics.get("raw_diffs"),
                "weighted_contributions": _final_metrics.get("weighted_contributions"),
                "test_population_share": _test_pop_pct,
                "control_population_share": _control_pop_pct,
                "control_group_size": len(control_geos),
            }
            st.session_state.match_results_stale = False

            cleanup_session_state()
            st.success(f"Groups set. Test: {len(test_geos)} regions, Control: {len(control_geos)} regions.")

        else:
            if setup_mode == "Set Rules & Auto‑Build Groups":
                conflicts = (set(force_exp_include) & set(force_exp_exclude)) | (set(force_ctrl_include) & set(force_ctrl_exclude)) | (set(force_exp_include) & set(force_ctrl_include))
                if conflicts:
                    st.error(f"Invalid constraints. These geographies have conflicting assignments: {sorted(conflicts)}")
                    st.stop()
                test_geos, achieved_share, target_met = find_guided_test_group(agg_df, geo_col, total_market_pop,
                                                                                force_exp_include, force_exp_exclude,
                                                                                force_ctrl_include, force_ctrl_exclude,
                                                                                target_test_share, target_tolerance_pp, guided_iterations)
                if len(test_geos) == 0:
                    st.error("Could not construct a valid test group with the provided constraints.")
                    st.stop()
                if not target_met:
                    st.warning(f"Target population share range was not met. Closest achieved: {achieved_share * 100:.1f}% (target {target_test_share}%, ±{target_tolerance_pp}pp).")
                st.session_state.guided_share_info = {"achieved": achieved_share * 100, "target": target_test_share, "tolerance": target_tolerance_pp, "met": target_met}
                all_geos = set(agg_df[geo_col].unique())
                control_pool_geos = list((all_geos - set(test_geos) - set(force_ctrl_exclude)) | set(force_ctrl_include))
            else:
                st.session_state.guided_share_info = None
                if 'control_pool_geos' not in locals():
                    control_pool_geos = [g for g in all_geo_values if g not in test_geos and g not in st.session_state.get("force_ctrl_exclude", [])]

            if len(test_geos) == 0:
                st.error("No test regions selected. Please select at least one test region before running.")
                st.stop()
            st.session_state.selected_experiment_regions = list(test_geos)
            test_df_run = agg_df[agg_df[geo_col].isin(test_geos)].copy()
            pool_df = agg_df[agg_df[geo_col].isin(control_pool_geos)].copy()
            test_df_run = impute_missing_features(test_df_run, active_features)
            pool_df = impute_missing_features(pool_df, active_features)

            if force_1to1:
                s_min = s_max = len(test_geos)
            else:
                s_min, s_max = min_p, max_p
                if s_min <= 0 or s_max <= 0:
                    st.error("Invalid control pool size range. Please ensure min size >= 2 and max size > 0.")
                    st.stop()
            if len(pool_df) < s_max:
                st.error(f"Insufficient controls available. Need {s_max}, have {len(pool_df)}.")
                st.stop()

            eligible_df = pd.concat([test_df_run, pool_df], axis=0)
            eligible_df = impute_missing_features(eligible_df, active_features)
            eligible_means, eligible_stds = fit_structural_stats(eligible_df, active_features)
            eligible_means_tuple = tuple(float(eligible_means[f]) for f in active_features)
            eligible_stds_tuple = tuple(float(eligible_stds[f]) for f in active_features)
            st.session_state.eligible_means = dict(zip(active_features, eligible_means_tuple))
            st.session_state.eligible_stds = dict(zip(active_features, eligible_stds_tuple))

            w_vec, p_scaled, t_cent = preprocess_data(pool_df, test_df_run, active_features, weights, eligible_means_tuple, eligible_stds_tuple)
            opt_data = []
            best_score = float("inf")
            best_idx = None
            global_conv = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            size_range = [len(test_geos)] if force_1to1 else range(s_min, s_max + 1)
            total_iterations = len(size_range)

            for i, n in enumerate(size_range):
                status_text.text(f"Testing {n} controls..." if not force_1to1 else "Finding best 1-to-1 match...")
                if match_mode == "Greedy (Nearest Neighbor)":
                    nn = NearestNeighbors(n_neighbors=min(n, len(pool_df))).fit(p_scaled)
                    _, ind = nn.kneighbors(t_cent)
                    c_idx = [pool_df.index[j] for j in ind[0][:n]]
                    metrics = calculate_metrics(test_df_run, agg_df.loc[c_idx], active_features, weights, eligible_means, eligible_stds)
                    mean_abs_smd = metrics["mean_abs_smd"]
                    # Use weighted structural distance as the optimisation objective so slider weights affect control selection.
                    # Mean Abs SMD is retained as an unweighted diagnostic balance metric.
                    optimisation_score = metrics["weighted_structural_distance"]
                    opt_data.append({
                        "Num_Controls": n,
                        "Weighted_Structural_Distance": metrics["weighted_structural_distance"],
                        "Mean_Abs_SMD": mean_abs_smd,
                        "Optimisation_Score": optimisation_score,
                        "Indices": c_idx
                    })
                    if optimisation_score < best_score:
                        best_score, best_idx = optimisation_score, c_idx
                elif match_mode == "Refined Greedy (Hill Climbing)":
                    nn_w = NearestNeighbors(n_neighbors=min(len(pool_df), n + 5)).fit(p_scaled)
                    _, ind_w = nn_w.kneighbors(t_cent)
                    curr_idx = [pool_df.index[j] for j in ind_w[0][:n]]
                    pot_swaps = [pool_df.index[j] for j in ind_w[0] if pool_df.index[j] not in curr_idx][:CONFIG["max_hill_climbing_swaps"]]
                    curr_metrics = calculate_metrics(test_df_run, agg_df.loc[curr_idx], active_features, weights, eligible_means, eligible_stds)
                    curr_score = curr_metrics["weighted_structural_distance"]
                    curr_mean_abs_smd = curr_metrics["mean_abs_smd"]
                    conv = [curr_score]
                    improved = True
                    while improved:
                        improved = False
                        best_improvement = 0
                        best_swap_tuple = None
                        for j in range(min(len(curr_idx), 5)):
                            for swap_in in pot_swaps[:10]:
                                temp = curr_idx.copy()
                                temp[j] = swap_in
                                new_metrics = calculate_metrics(test_df_run, agg_df.loc[temp], active_features, weights, eligible_means, eligible_stds)
                                new_score = new_metrics["weighted_structural_distance"]
                                # Accept/reject swaps based on Weighted Structural Distance, not Mean Abs SMD.
                                improvement = curr_score - new_score
                                if improvement > best_improvement:
                                    best_improvement = improvement
                                    best_swap_tuple = (temp, swap_in, new_score, new_metrics["mean_abs_smd"])
                        if best_improvement > 0 and best_swap_tuple:
                            curr_idx, swap_in, curr_score, curr_mean_abs_smd = best_swap_tuple
                            if swap_in in pot_swaps:
                                pot_swaps.remove(swap_in)
                            conv.append(curr_score)
                            improved = True
                    optimisation_score = curr_score
                    opt_data.append({
                        "Num_Controls": n,
                        "Weighted_Structural_Distance": curr_score,
                        "Mean_Abs_SMD": curr_mean_abs_smd,
                        "Optimisation_Score": optimisation_score,
                        "Indices": curr_idx
                    })
                    if optimisation_score < best_score:
                        best_score, best_idx, global_conv = optimisation_score, curr_idx, conv
                elif match_mode == "Stochastic (Genetic Search)":
                    # Start from a good nearest-neighbour candidate group, then randomly swap one
                    # selected control for one unselected control, keeping improving swaps.
                    # Weighted Structural Distance is the optimisation objective; Mean Abs SMD is diagnostic only.
                    nn_start = NearestNeighbors(n_neighbors=min(n, len(pool_df))).fit(p_scaled)
                    _, ind_start = nn_start.kneighbors(t_cent)
                    nn_start_idx = [pool_df.index[j] for j in ind_start[0][:n]]
                    best_idx_for_n, best_metrics_for_n, evaluated_count, conv = stochastic_genetic_search(
                        pool_df, test_df_run, active_features, weights, n,
                        calculate_metrics, eligible_means, eligible_stds,
                        nn_start_idx=nn_start_idx,
                        n_iterations=genetic_iterations,
                        random_state=42,
                    )
                    optimisation_score = best_metrics_for_n["weighted_structural_distance"]
                    opt_data.append({
                        "Num_Controls": n,
                        "Weighted_Structural_Distance": best_metrics_for_n["weighted_structural_distance"],
                        "Mean_Abs_SMD": best_metrics_for_n["mean_abs_smd"],
                        "Optimisation_Score": optimisation_score,
                        "Indices": best_idx_for_n,
                        "Candidates_Evaluated": evaluated_count,
                    })
                    if optimisation_score < best_score:
                        best_score = optimisation_score
                        best_idx = best_idx_for_n
                        global_conv = conv
                progress_bar.progress((i + 1) / total_iterations)

            progress_bar.empty()
            status_text.empty()
            st.session_state.final_controls = agg_df.loc[best_idx].copy()
            st.session_state.opt_results = {"size_df": pd.DataFrame(opt_data), "convergence": global_conv}
            st.session_state.best_n = len(best_idx)
            st.session_state.test_df = test_df_run.copy()
            st.session_state.match_mode_res = match_mode

            # ---- Freeze a snapshot of the inputs/outputs used for this run ----
            # so slider changes afterwards don't silently change the displayed results.
            final_metrics = calculate_metrics(
                test_df_run, agg_df.loc[best_idx], active_features, weights, eligible_means, eligible_stds
            )
            _eligible_market_pop = agg_df[POPULATION_COL].sum()
            _experiment_pop = agg_df[agg_df[geo_col].isin(test_geos)][POPULATION_COL].sum()
            _control_pop = agg_df[agg_df[geo_col].isin(st.session_state.final_controls[geo_col].tolist())][POPULATION_COL].sum()
            _test_pop_pct = (_experiment_pop / _eligible_market_pop) * 100 if _eligible_market_pop > 0 else 0
            _control_pop_pct = (_control_pop / _eligible_market_pop) * 100 if _eligible_market_pop > 0 else 0
            st.session_state.match_run_snapshot = {
                "market": market,
                "geography_level": geography_level,
                "geo_col": geo_col,
                "setup_mode": setup_mode,
                "match_mode": match_mode,
                "test_geos": list(test_geos),
                "control_pool_geos": list(control_pool_geos) if "control_pool_geos" in locals() else [],
                "force_ctrl_exclude": list(st.session_state.get("force_ctrl_exclude", [])),
                "active_features": list(active_features),
                "weights": dict(weights),
                "eligible_means": tuple(eligible_means_tuple) if "eligible_means_tuple" in locals() else None,
                "eligible_stds": tuple(eligible_stds_tuple) if "eligible_stds_tuple" in locals() else None,
                "best_n": len(best_idx) if best_idx is not None else None,
            }
            st.session_state.match_run_metrics = {
                "weighted_structural_distance": final_metrics["weighted_structural_distance"],
                "mean_abs_smd": final_metrics["mean_abs_smd"],
                "smd_list": final_metrics["smd_list"],
                "test_means": final_metrics["test_means"],
                "control_means": final_metrics["control_means"],
                "raw_diffs": final_metrics.get("raw_diffs"),
                "weighted_contributions": final_metrics.get("weighted_contributions"),
                "test_population_share": _test_pop_pct,
                "control_population_share": _control_pop_pct,
                "control_group_size": len(best_idx),
            }
            st.session_state.match_results_stale = False

            cleanup_session_state()
            st.success(
                f"Match completed. Selected {len(best_idx)} controls with "
                f"Weighted Structural Distance = {best_score:.4f}."
            )

    # ------------------------------------------------------------
    # Results display (Summary, Diagnostics, Export)
    # ------------------------------------------------------------
    if run_clicked and len(test_geos) == 0:
        st.warning("Select at least one test region before running analysis.")

    if st.session_state.final_controls is not None:
        # ---- Read from the FROZEN snapshot of the last completed run ----
        # Do not recalculate display metrics from the current live slider weights here;
        # the cards/table/chart below must only change when Run Match Analysis is clicked again.
        run_metrics = st.session_state.get("match_run_metrics", {})
        run_snapshot = st.session_state.get("match_run_snapshot", {})
        run_weights = run_snapshot.get("weights", {})
        run_features = run_snapshot.get("active_features", active_features)

        if not run_metrics or not run_snapshot:
            # Safety net for any legacy session state saved before this snapshot pattern existed.
            if not st.session_state.get("eligible_means") or not st.session_state.get("eligible_stds"):
                _fallback_df = pd.concat([st.session_state.test_df, st.session_state.final_controls], axis=0)
                _fallback_df = impute_missing_features(_fallback_df, active_features)
                _fb_means, _fb_stds = fit_structural_stats(_fallback_df, active_features)
                st.session_state.eligible_means = {f: float(_fb_means[f]) for f in active_features}
                st.session_state.eligible_stds = {f: float(_fb_stds[f]) for f in active_features}
            _em_tuple = tuple(st.session_state.eligible_means.get(f, np.nan) for f in active_features)
            _es_tuple = tuple(st.session_state.eligible_stds.get(f, np.nan) for f in active_features)
            _fallback_metrics = calculate_metrics_cached(
                st.session_state.test_df, st.session_state.final_controls,
                tuple(active_features), tuple(weights.get(f, 1.0) for f in active_features),
                _em_tuple, _es_tuple
            )
            run_metrics = {
                "weighted_structural_distance": _fallback_metrics["weighted_structural_distance"],
                "mean_abs_smd": _fallback_metrics["mean_abs_smd"],
                "smd_list": _fallback_metrics["smd_list"],
                "test_means": _fallback_metrics["test_means"],
                "control_means": _fallback_metrics["control_means"],
                "raw_diffs": _fallback_metrics.get("raw_diffs"),
                "weighted_contributions": _fallback_metrics.get("weighted_contributions"),
                "control_group_size": len(st.session_state.final_controls),
            }
            run_weights = weights
            run_features = active_features

        mean_abs_smd = run_metrics["mean_abs_smd"]
        weighted_structural_distance = run_metrics["weighted_structural_distance"]
        smd_list = run_metrics["smd_list"]
        e_m = run_metrics["test_means"]
        c_m = run_metrics["control_means"]
        weighted_contributions = run_metrics["weighted_contributions"]
        st.subheader("🔍 MATCHING RESULTS")

        setup_changed = matching_setup_changed_since_last_run(run_snapshot, market, geography_level, match_mode, test_geos, weights)
        if setup_changed:
            st.info(
                "You have changed the matching setup since the last run. "
                "The results below still show the last completed match. Click Run Match Analysis to update them."
            )

        raw_diffs = run_metrics.get("raw_diffs")
        if raw_diffs is None:
            raw_diffs = [round(e - c, 4) for e, c in zip(e_m, c_m)]
        comp_df = pd.DataFrame({
            "Feature": run_features[:len(smd_list)],
            "Weight": [run_weights.get(f, 1.0) for f in run_features[:len(smd_list)]],
            "Test Mean": [round(x, 4) for x in e_m[:len(smd_list)]],
            "Ctrl Mean": [round(x, 4) for x in c_m[:len(smd_list)]],
            "Raw Diff": [round(x, 4) for x in raw_diffs[:len(smd_list)]],
            "Abs SMD": [round(x, 4) if np.isfinite(x) else np.nan for x in smd_list],
            "Weighted Contribution": [round(x, 4) for x in weighted_contributions[:len(smd_list)]]
        }).sort_values("Abs SMD", ascending=False)

        tab_choice = st.radio("Select View", ["📊 Summary", "📈 Diagnostics", "💾 Export"], horizontal=True, key="tab_selector_main", label_visibility="collapsed")

        if tab_choice == "📊 Summary":
            experiment_pop_pct = run_metrics.get("test_population_share")
            control_pop_pct = run_metrics.get("control_population_share")
            if experiment_pop_pct is None or control_pop_pct is None:
                # Safety net: derive from the frozen test_df/final_controls (not live weights)
                eligible_market_pop = agg_df[POPULATION_COL].sum()
                selected_experiment_regions = st.session_state.selected_experiment_regions or st.session_state.test_df[geo_col].tolist()
                selected_control_regions = st.session_state.final_controls[geo_col].tolist()
                experiment_pop = agg_df[agg_df[geo_col].isin(selected_experiment_regions)][POPULATION_COL].sum()
                control_pop = agg_df[agg_df[geo_col].isin(selected_control_regions)][POPULATION_COL].sum()
                experiment_pop_pct = (experiment_pop / eligible_market_pop) * 100 if eligible_market_pop > 0 else 0
                control_pop_pct = (control_pop / eligible_market_pop) * 100 if eligible_market_pop > 0 else 0
            control_group_size = run_metrics.get("control_group_size", len(st.session_state.final_controls))

            ck1, ck2, ck3, ck4, ck5 = st.columns(5)
            with ck1:
                st.metric("Weighted Structural Distance", round(weighted_structural_distance, 4), help="Weighted Euclidean distance between standardised test and control feature means, using the slider weights at the time of the last run. This is the optimisation objective. Lower is better — 0 means identical means across all features.")
            with ck2:
                smd_color = "🟢" if mean_abs_smd < SMD_GOOD_THRESHOLD else "🟡" if mean_abs_smd < SMD_HIGH_THRESHOLD else "🔴"
                st.metric("Mean Abs SMD", f"{smd_color} {round(mean_abs_smd, 4)}", help=f"Average absolute Standardised Mean Difference across all features (unweighted, diagnostic only). 🟢 < {SMD_GOOD_THRESHOLD:.2f} = good balance, 🟡 {SMD_GOOD_THRESHOLD:.2f}–{SMD_HIGH_THRESHOLD:.2f} = moderate imbalance, 🔴 ≥ {SMD_HIGH_THRESHOLD:.2f} = high imbalance.")
            with ck3:
                st.metric("Control Group Size", control_group_size, help="Number of control regions selected in the last completed run.")
            with ck4:
                st.metric("Test Population Share", f"{experiment_pop_pct:.1f}%", help="Percentage of total market population covered by the test regions used in the last completed run.")
                if st.session_state.guided_share_info:
                    st.caption(f"Target: {st.session_state.guided_share_info['target']}%")
            with ck5:
                st.metric("Control Population Share", f"{control_pop_pct:.1f}%", help="Percentage of total market population covered by the control regions selected in the last completed run.")
            st.caption("Weighted Structural Distance is the optimisation objective and uses the slider weights from the last completed run. Mean Abs SMD is an unweighted diagnostic balance check. These results are frozen until you click Run Match Analysis again.")

            with st.expander("View Selected Groups", expanded=True):
                c1, c2 = st.columns(2)
                with c1:
                    st.write("**Test Geographies**")
                    st.table(pd.DataFrame({"Test Geography": st.session_state.test_df[geo_col].values}))
                with c2:
                    st.write("**Control Geographies**")
                    st.table(pd.DataFrame({"Control Geography": st.session_state.final_controls[geo_col].values}))

            st.markdown('<div class="custom-divider"></div>', unsafe_allow_html=True)
            cl, cr = st.columns([1.5, 1])
            with cl:
                st.write("**Feature Comparison Table**")
                def color_smd(val):
                    if pd.isna(val):
                        return "background-color: #e5e7eb"
                    if val < SMD_GOOD_THRESHOLD:
                        return "background-color: #c6efce"
                    elif val < SMD_HIGH_THRESHOLD:
                        return "background-color: #ffeb9c"
                    else:
                        return "background-color: #ffc7ce"
                display_comp = comp_df.copy()
                for c in ["Test Mean", "Ctrl Mean", "Raw Diff"]:
                    display_comp[c] = display_comp[c].astype(object)
                prop_features = set(proportion_cols)
                for idx, row in display_comp.iterrows():
                    feat = row["Feature"]
                    if feat in prop_features:
                        display_comp.at[idx, "Test Mean"] = f"{row['Test Mean'] * 100:.1f}%"
                        display_comp.at[idx, "Ctrl Mean"] = f"{row['Ctrl Mean'] * 100:.1f}%"
                        display_comp.at[idx, "Raw Diff"] = f"{row['Raw Diff'] * 100:.1f}%"
                    elif feat in [POPULATION_COL, "Population Density"]:
                        display_comp.at[idx, "Test Mean"] = f"{row['Test Mean']:,.1f}"
                        display_comp.at[idx, "Ctrl Mean"] = f"{row['Ctrl Mean']:,.1f}"
                        display_comp.at[idx, "Raw Diff"] = f"{row['Raw Diff']:,.1f}"
                    else:
                        display_comp.at[idx, "Test Mean"] = f"{row['Test Mean']:.4f}"
                        display_comp.at[idx, "Ctrl Mean"] = f"{row['Ctrl Mean']:.4f}"
                        display_comp.at[idx, "Raw Diff"] = f"{row['Raw Diff']:.4f}"
                styled_comp = display_comp.style.map(color_smd, subset=["Abs SMD"]).format({"Weight": "{:.0f}", "Abs SMD": "{:.4f}"}, na_rep="")
                st.dataframe(styled_comp, width='stretch', hide_index=False, height=400)
                st.caption("Abs SMD is unweighted and shows the actual balance for each feature. The slider weight changes how much that feature influences control selection (via Weighted Contribution), but it does not change the SMD formula itself.")
            with cr:
                st.write("**Balance (Love Plot)**")
                pdf = comp_df.sort_values("Abs SMD")
                fig = px.scatter(pdf, x="Abs SMD", y="Feature", color="Abs SMD", color_continuous_scale=["#CCFBF1", "#0F766E"], title="Feature Balance Plot", labels={"Abs SMD": "Absolute SMD"})
                fig.add_vline(x=SMD_GOOD_THRESHOLD, line_dash="dash", line_color="#0F766E")
                fig.add_vline(x=SMD_HIGH_THRESHOLD, line_dash="dash", line_color="#F59E0B")
                fig.update_layout(height=500, margin=dict(l=10, r=10, t=50, b=10), paper_bgcolor="white", plot_bgcolor="white")
                st.plotly_chart(fig, width='stretch')

        elif tab_choice == "📈 Diagnostics":
            st.markdown('<div class="custom-divider"></div>', unsafe_allow_html=True)
            if not force_1to1 and len(st.session_state.opt_results.get("size_df", pd.DataFrame())) > 0:
                st.subheader("Pool Size Optimization", help="This chart tests different control group sizes to find the best balance. The dashed line marks the size that resulted in the lowest SMD.")
                size_df = st.session_state.opt_results["size_df"]
                required_cols = ["Num_Controls", "Weighted_Structural_Distance", "Mean_Abs_SMD"]
                missing_cols = [c for c in required_cols if c not in size_df.columns]
                if missing_cols:
                    st.error(f"Pool size results are missing expected columns: {missing_cols}. Check that all match-mode branches write the same opt_data keys.")
                else:
                    chart_df = size_df[required_cols]
                    rule_df = pd.DataFrame({"best_n": [st.session_state.best_n]})
                    base = alt.Chart(chart_df).mark_line(point=True, color="#7C3AED").encode(x=alt.X("Num_Controls:Q", title="Number of Controls"), y=alt.Y("Weighted_Structural_Distance:Q", title="Weighted Structural Distance"), tooltip=["Num_Controls", "Weighted_Structural_Distance", "Mean_Abs_SMD"])
                    marker = alt.Chart(rule_df).mark_rule(color="#0F766E", strokeDash=[6, 4]).encode(x="best_n:Q")
                    st.altair_chart((base + marker).properties(height=280), width='stretch')
                    st.caption("Lower is better. This is the slider-weighted objective used to select the control group. Mean Abs SMD is retained as an unweighted balance diagnostic.")
            if st.session_state.match_mode_res != "Greedy (Nearest Neighbor)" and st.session_state.opt_results.get("convergence"):
                st.subheader("Search Convergence", help="This shows whether the search improved as it tried alternative control combinations.")
                conv_df = pd.DataFrame({"step": list(range(len(st.session_state.opt_results["convergence"]))), "Weighted_Structural_Distance": st.session_state.opt_results["convergence"]})
                conv_chart = alt.Chart(conv_df).mark_line(color="#0F766E").encode(x=alt.X("step:Q", title="Improvement Steps"), y=alt.Y("Weighted_Structural_Distance:Q", title="Weighted Structural Distance"), tooltip=["step", "Weighted_Structural_Distance"]).properties(height=280, title=f"Optimization Path for N={st.session_state.best_n}")
                st.altair_chart(conv_chart, width='stretch')
            st.markdown('<div class="custom-divider"></div>', unsafe_allow_html=True)
            st.subheader("Feature Distribution Detail", help="Compare spread, median, and outliers of selected feature values for Test vs Control.")
            display_features_for_viz = active_features[: min(len(active_features), 20)]
            if display_features_for_viz:
                if "selected_viz_feature" not in st.session_state:
                    st.session_state.selected_viz_feature = display_features_for_viz[0]
                viz_f = st.selectbox("Select feature to view density distribution:", display_features_for_viz, index=display_features_for_viz.index(st.session_state.selected_viz_feature) if st.session_state.selected_viz_feature in display_features_for_viz else 0, key="feature_distribution_select")
                st.session_state.selected_viz_feature = viz_f
                if viz_f in st.session_state.test_df.columns and viz_f in st.session_state.final_controls.columns:
                    test_data = st.session_state.test_df[viz_f].dropna()
                    control_data = st.session_state.final_controls[viz_f].dropna()
                    if len(test_data) > 1 and len(control_data) > 1:
                        density_df = pd.concat([pd.DataFrame({"value": test_data, "Group": "Test"}), pd.DataFrame({"value": control_data, "Group": "Control"})], ignore_index=True)
                        fig_dist = px.violin(density_df, x="Group", y="value", color="Group", box=True, points="all", color_discrete_map={"Test": "#7C3AED", "Control": "#0F766E"}, labels={"value": viz_f})
                        fig_dist.update_layout(title=f"Distribution Comparison: {viz_f}", yaxis_title=viz_f, xaxis_title="Group", showlegend=False, height=420, margin=dict(l=10, r=10, t=50, b=10))
                        st.plotly_chart(fig_dist, width='stretch')
                    else:
                        st.warning("Insufficient data points for distribution plot. Need at least 2 points per group.")
            else:
                st.info("No numeric features available for diagnostics.")

        elif tab_choice == "💾 Export":
            st.markdown('<div class="custom-divider"></div>', unsafe_allow_html=True)
            with st.expander("📋 What will be exported?", expanded=True):
                st.markdown(f"""
                The export lists every Adobe geography with its **{geo_col}** grouping and test/control assignment:

                - **Market** — {market}
                - **Adobe Reference List** — every geography as it appears in Adobe
                - **{geo_col}** — the aggregation level used for matching
                - **Test Geography** — Yes if assigned to the test group
                - **Control Geography** — Yes if assigned to the control group
                """)
            col1, col2 = st.columns(2)
            with col1:
                if st.button("📥 Export to Excel", width='stretch', type="primary"):
                    try:
                        _raw = market_df_raw.copy()

                        # Pull just Adobe reference + selected geo_col
                        _keep = [c for c in [ADOBE_COL, geo_col] if c in _raw.columns]
                        _lookup = _raw[_keep].drop_duplicates().copy()
                        for c in _keep:
                            _lookup[c] = _lookup[c].astype(str).str.strip()

                        _test_geos = set(st.session_state.test_df[geo_col].astype(str).str.strip().tolist())
                        _ctrl_geos = set(st.session_state.final_controls[geo_col].astype(str).str.strip().tolist())

                        _lookup.insert(0, "Market", market)
                        _lookup["Test Geography"] = _lookup[geo_col].apply(
                            lambda g: "Yes" if g in _test_geos else ""
                        )
                        _lookup["Control Geography"] = _lookup[geo_col].apply(
                            lambda g: "Yes" if g in _ctrl_geos else ""
                        )

                        if ADOBE_COL in _lookup.columns:
                            _lookup = _lookup.sort_values(ADOBE_COL).reset_index(drop=True)

                        output = io.BytesIO()
                        with pd.ExcelWriter(output, engine="openpyxl") as writer:
                            _lookup.to_excel(writer, sheet_name="Geo_Assignments", index=False)
                            ws = writer.sheets["Geo_Assignments"]
                            for col_cells in ws.columns:
                                max_len = max((len(str(c.value)) for c in col_cells if c.value), default=10)
                                ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 60)

                        _n_test = _lookup["Test Geography"].eq("Yes").sum()
                        _n_ctrl = _lookup["Control Geography"].eq("Yes").sum()
                        st.download_button(
                            label="Download Excel",
                            data=output.getvalue(),
                            file_name=f"geo_assignments_{market}_{geography_level}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                        st.success(f"✅ Export ready — {len(_lookup)} geographies, {_n_test} test, {_n_ctrl} control.")
                    except Exception as e:
                        st.error("We couldn't create the Excel export. Please try again, and check that a valid control group has been selected.")
                        with st.expander("Technical details"):
                            st.code(f"{type(e).__name__}: {e}")
            with col2:
                if st.button("📋 Copy Summary to Clipboard", width='stretch'):

                    summary_text = f"""GEO-MATCH RESULTS SUMMARY\n=========================\nMarket: {market}\nGeography Level: {geography_level}\nStrategy: {match_mode}\n----------------------------------------\nMean Abs SMD (unweighted diagnostic): {mean_abs_smd:.4f}\nWeighted Structural Distance (optimisation objective): {weighted_structural_distance:.4f}\nControl Group Size: {len(st.session_state.final_controls)}\nTest Group Size: {len(st.session_state.test_df)}\nTest Population Share: {(experiment_pop / eligible_market_pop * 100):.1f}%\nControl Population Share: {(control_pop / eligible_market_pop * 100):.1f}%"""
                    st.code(summary_text, language="text")
                    st.caption("Copy the text above manually")

# =============================================================================
# TAB 2: DESIGN FUTURE GEO TEST
# TAB 3: EVALUATE COMPLETED GEO TEST
# =============================================================================

def render_method_comparison_table(results, mode, test_start, control_regions_val):
    """
    Renders the Method Comparison table (traffic-light diagnostics per method),
    its captions, and the "How to interpret these results" expander.

    Extracted from render_time_series_validation() as a self-contained rendering
    step: it only reads from `results` (the dict of per-method result dicts built by
    run_validation_method()), `mode` ("Design" or "Evaluate"), `test_start`, and
    `control_regions_val` — it does not depend on any other local state from the
    caller. METHOD_STRUCTURAL / METHOD_USER_SELECTED are module-level constants.
    """
    # ---- Method Comparison table ----
    st.subheader("Method Comparison")

    # combine_reliability_ratings() already returns the full user-facing label
    # (e.g. "🟢 High confidence"), so this is a passthrough map with a safe fallback
    # for any unexpected/legacy value.
    RELIABILITY_LABELS = {
        "🟢 High confidence": "🟢 High confidence",
        "🟡 Moderate confidence": "🟡 Moderate confidence",
        "🔴 Low confidence": "🔴 Low confidence",
        "⚪ Insufficient data": "⚪ Insufficient data",
    }

    comparison_rows = [
        {"Metric": "A. CONTROL SELECTION", "is_section": True},
        {"Metric": "Control Pool Size", "key": "control_pool_size"},
        {"Metric": "Controls Selected", "key": "controls_selected"},
        {"Metric": "Predictors Selected", "key": "n_selected_features"},

        {"Metric": "B. PRE-PERIOD FIT", "is_section": True},
        {"Metric": "Pre-Period Correlation", "key": "pre_corr"},
        {"Metric": "Pre-Period R²", "key": "pre_r2"},
        {"Metric": "Pre-Period sMAPE (%)", "key": "pre_smape"},

        {"Metric": "C1. ROLLING-ORIGIN VALIDATION - ERROR", "is_section": True},
        {"Metric": "Validation sMAPE (%)", "key": "holdout_smape"},
        {"Metric": "Validation Error Risk", "key": "rolling_validation_error_risk"},
        
        {"Metric": "C2. ROLLING-ORIGIN VALIDATION - BIAS", "is_section": True},
        {"Metric": "Average Bias (%)", "key": "rolling_bias_pct_mean"},
        {"Metric": "Bias Risk", "key": "rolling_bias_risk"},

        {"Metric": "D. OVERFITTING CHECK", "is_section": True},
        {"Metric": "Pre-Period vs Validation sMAPE Difference (pp)", "key": "overfit_gap_smape"},
        {"Metric": "Overfitting Risk", "key": "overfitting_risk"},

        {"Metric": "E. RESIDUAL DIAGNOSTICS", "is_section": True},
        {"Metric": "Durbin-Watson", "key": "dw_stat"},
        {"Metric": "Autocorrelation Risk", "key": "autocorrelation_risk"},

        {"Metric": "F. PLACEBO TESTING", "is_section": True},
        {"Metric": "Placebo Windows", "key": "placebo_windows"},
        {"Metric": "Average Placebo sMAPE (%)", "key": "median_placebo_smape"},
        {"Metric": "Median Placebo Uplift", "key": "median_placebo_uplift_pct"},
        {"Metric": "95% Placebo Uplift Range", "key": "placebo_range_pct"},

        {"Metric": "G. COUNTERFACTUAL CONFIDENCE", "is_section": True},
        {"Metric": "Overall Counterfactual Confidence", "key": "counterfactual_reliability"},
        {"Metric": "Key Issues", "key": "reliability_drivers"},

    ]
    show_test_impact = (mode == "Evaluate" and test_start is not None)
    if show_test_impact:
        comparison_rows += [
            {"Metric": "H. OBSERVED UPLIFT VS PLACEBOS", "is_section": True},
            {"Metric": "Uplift Percentile vs Placebos", "key": "placebo_percentile_rank"},
            {"Metric": "Uplift p-value", "key": "placebo_p_two_sided"},
            {"Metric": "Uplift z-score", "key": "placebo_z_score"},
            
            {"Metric": "I. TEST IMPACT", "is_section": True},
            {"Metric": "Observed Uplift", "key": "observed_uplift"},
            {"Metric": "Observed Uplift (%)", "key": "observed_uplift_pct"},
            {"Metric": "Test Group Actual Total", "key": "test_period_actual"},
            {"Metric": "Expected Total Without Test (Counterfactual)", "key": "test_period_counterfactual"},
        ]

    def _fmt_pct(v, decimals=1):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "N/A"
        return f"{v:.{decimals}f}%"

    def _fmt_num(v, decimals=1):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "N/A"
        return f"{v:.{decimals}f}"

    def get_value(key, res, method_name):
        if key == "control_pool_size":
            if method_name in [METHOD_STRUCTURAL, METHOD_USER_SELECTED]:
                return str(len(control_regions_val))
            else:
                return str(res['n_candidates'])
        elif key == "controls_selected":
            if method_name in [METHOD_STRUCTURAL, METHOD_USER_SELECTED]:
                return str(len(control_regions_val))
            else:
                return str(res['n_selected'])
        elif key == "n_selected_features":
            v = res.get("n_selected_features", None)
            return str(v) if v is not None else "N/A"
        elif key == "validation_method_label":
            return res.get("validation_method_label", "⚪ Insufficient validation history")
        elif key == "pre_corr":
            return _fmt_num(res.get('corr', np.nan), decimals=3)
        elif key == "pre_r2":
            return _fmt_num(res.get('r2', np.nan), decimals=3)
        elif key == "pre_smape":
            return _fmt_pct(res.get('smape', np.nan))
        elif key == "pre_rmse":
            return _fmt_num(res.get('rmse', np.nan))
        elif key == "dw_stat":
            dw = res.get("dw_stat", np.nan)
            if dw is None or (isinstance(dw, float) and np.isnan(dw)):
                return "N/A"
            return f"{dw:.2f}"
        elif key == "autocorrelation_risk":
            return res.get("autocorrelation_risk", "⚪ Insufficient data")
        elif key == "holdout_smape":
            return _fmt_pct(res.get('holdout_smape_mean', np.nan))
        elif key == "rolling_validation_error_risk":
            return res.get("rolling_validation_error_risk", "⚪ Insufficient data")
        elif key == "holdout_rmse":
            return _fmt_num(res.get('holdout_rmse_mean', np.nan))
        elif key == "rolling_smape_p90":
            return _fmt_pct(res.get("rolling_smape_p90", np.nan))
        elif key == "rolling_bias_pct_mean":
            return _fmt_pct(res.get("rolling_bias_pct_mean", np.nan))
        elif key == "rolling_bias_risk":
            return res.get("rolling_bias_risk", "⚪ Insufficient data")
        elif key == "overfit_gap_smape":
            v = res.get("overfit_gap_smape", np.nan)
            return f"{v:.1f} pp" if not (v is None or (isinstance(v, float) and np.isnan(v))) else "N/A"
        elif key == "overfit_gap_rmse":
            return _fmt_num(res.get("overfit_gap_rmse", np.nan))
        elif key == "overfitting_risk":
            return res.get("overfitting_risk", "⚪ Insufficient data")
        elif key == "reliability_drivers":
            return res.get("reliability_drivers", "Insufficient validation data to assess confidence")
        elif key == "counterfactual_reliability":
            reliability = res.get("counterfactual_reliability", None)
            return RELIABILITY_LABELS.get(reliability, "⚪ Insufficient data")
        elif key == "placebo_windows":
            return str(len(res['placebos']))
        elif key == "median_placebo_uplift_pct":
            return _fmt_pct(res.get('median_placebo_uplift_pct', np.nan))
        elif key == "placebo_range_pct":
            return format_range(res.get('placebo_range_lower_pct', np.nan), res.get('placebo_range_upper_pct', np.nan), suffix="%", decimals=1)
        elif key == "median_placebo_smape":
            return _fmt_pct(res.get('median_placebo_smape', np.nan))
        elif key == "p95_placebo_smape":
            return _fmt_pct(res.get('p95_placebo_smape', np.nan))
        elif key == "placebo_percentile_rank":
            return _fmt_pct(res.get('placebo_percentile_rank', np.nan))
        elif key == "placebo_p_two_sided":
            return _fmt_num(res.get('placebo_p_value_two_sided', np.nan), decimals=3)
        elif key == "placebo_z_score":
            return _fmt_num(res.get('placebo_z_score', np.nan), decimals=2)
        elif key == "observed_uplift":
            return _fmt_num(res.get('uplift', np.nan))
        elif key == "observed_uplift_pct":
            return _fmt_pct(res.get('uplift_pct', np.nan))
        elif key == "test_period_actual":
            y_test_actual = res.get('y_test_actual', None)
            if y_test_actual is None or len(y_test_actual) == 0:
                return "N/A"
            return _fmt_num(float(np.sum(y_test_actual)))
        elif key == "test_period_counterfactual":
            y_pred_test = res.get('y_pred_test', None)
            if y_pred_test is None or len(y_pred_test) == 0:
                return "N/A"
            return _fmt_num(float(np.sum(y_pred_test)))
        else:
            return "N/A"

    table_data = []
    method_names = list(results.keys())
    for row in comparison_rows:
        if row.get("is_section", False):
            new_row = {"Metric": row["Metric"]}
            for m in method_names:
                new_row[m] = ""
            table_data.append(new_row)
        else:
            new_row = {"Metric": row["Metric"]}
            for m in method_names:
                new_row[m] = get_value(row["key"], results[m], m)
            table_data.append(new_row)

    comp_df_val = pd.DataFrame(table_data)
    def style_section_rows(row):
        if row["Metric"] in [r["Metric"] for r in comparison_rows if r.get("is_section", False)]:
            return ["font-weight: bold; background-color: #f0f2f6"] * len(row)
        return [""] * len(row)
    styled_comp = comp_df_val.style.apply(style_section_rows, axis=1)
    st.dataframe(styled_comp, width='stretch', hide_index=False)


    st.caption(
        "**Rolling-Origin Validation Error** shows whether the model can predict held-out historical periods. "
        "Lower is better."
    )
    st.caption(
        "**Rolling-Origin Validation Bias** checks whether the model systematically over- or under-predicts in held-out "
        "historical periods."
    )
    st.caption(
        "**Pre-Period vs Validation sMAPE Difference** compares the model's in-sample pre-period error with its held-out rolling "
        "validation error. A large positive gap means the model looks good on the data it was fitted on, "
        "but performs worse when predicting unseen historical periods."
    )
    st.caption(
        "**Durbin-Watson** checks whether residuals are autocorrelated. Values near 2 are good. Values far "
        "below or above 2 suggest the model is missing time patterns."
    )
    st.caption(
        "**95% Placebo Uplift Range** is based on historical fake-test windows. They show how much apparent "
        "uplift could occur when no real intervention happened. If the observed test uplift sits inside "
        "this range, it may not be distinguishable from normal historical noise."
    )
    st.caption(
        "**Uplift Percentile vs Placebos** and **Uplift p-value** are empirical, derived directly from the "
        "**Placebo Windows** count shown above — their resolution is limited to roughly 1 / (that count). "
        "With few placebo windows, a p-value can only take a few discrete values (e.g. 10 windows means "
        "p can only land on multiples of 0.1), so treat a borderline result with a low placebo-window count "
        "with extra caution."
    )
    st.caption(
        "**Overall Counterfactual Confidence** is a priority-ordered summary, not a simple worst-of-four "
        "vote. Rolling Validation Error is the primary check and acts as a gate: a high-risk validation "
        "error alone makes confidence low. Overfitting, Autocorrelation Risk, and Rolling Bias are "
        "evaluated next in that order — a flag on any of them holds confidence at moderate, but only "
        "Rolling Validation Error can push it all the way down to low."
    )
    st.caption(
        "**Key Issues** lists all the high- and moderate-risk checks that drove the confidence rating, "
        "not just the single worst one. "
        "Traffic-light bands are interpretation aids based on validation diagnostics — they are not "
        "standalone hypothesis tests."
    )

    # ---- Interpretation help ----
    with st.expander("How to interpret these results", expanded=False):
        if mode == "Design":
            st.markdown(f"""
**Validate Test Design — How to read this**

The goal here is to assess whether your control group can reliably predict what would have happened to your test regions without any intervention. If it can, you can have more confidence in a future uplift estimate.

We recommend interpreting the checks in this order: rolling-origin validation error, then overfitting, then residual diagnostics, then rolling bias, then Overall Counterfactual Confidence as the final summary.

---

**Step 1 — Start with Rolling-Origin Validation Error**

This is the main check for whether the model can predict unseen historical data, and should be treated as the primary model-quality check. It is far more trustworthy than pre-period fit alone.

- **Rolling-Origin sMAPE (%)** — Typical percentage error when predicting the test KPI from controls. Lower validation sMAPE means the counterfactual is likely to be more trustworthy. 🟢 Low: 10% or below. 🟡 Moderate: above 10% up to 15%. 🔴 High: above 15%.
- **Rolling-Origin sMAPE — Worst Case (P90)** — The error in the weakest 10% of forecast windows. Even if the average looks fine, a high P90 means the model breaks down in certain periods.

---

**Step 2 — Then check Overfitting**

Compare pre-period fit against rolling-origin validation. A large gap means the model may look good in-sample but perform poorly on unseen data.

- **Overfitting Gap, sMAPE percentage points** — Rolling-origin validation sMAPE minus pre-period sMAPE. 🟢 Low: up to 3 percentage points. 🟡 Moderate: above 3 up to 5 percentage points. 🔴 High: above 5 percentage points. This is a validation diagnostic, not a formal statistical test.

---

**Step 3 — Then check Residual Diagnostics**

Use Durbin-Watson / autocorrelation risk to assess whether residuals are independent enough. Strong autocorrelation means the model may be missing time structure.

- **Durbin-Watson** / **Autocorrelation Risk** — Durbin-Watson is an established statistic for first-order residual autocorrelation. Values near 2 suggest little autocorrelation. 🟢 Low autocorrelation risk: 1.5 to 2.5. 🟡 Moderate autocorrelation risk: 1.2 to just under 1.5, or above 2.5 up to 2.8. 🔴 High autocorrelation risk: below 1.2 or above 2.8. These are practical diagnostic bands, not formal critical-value tests.

---

**Step 4 — Then check Rolling Bias**

Bias tells you whether the model systematically over- or under-predicts in validation windows. Rolling bias feeds into Overall Counterfactual Confidence — if bias is moderate or high, be more cautious about interpreting the uplift.

- **Rolling-Origin Bias (%)** — Whether the model consistently overshoots or undershoots. 🟢 Low: absolute bias 5% or below. 🟡 Moderate: above 5% up to 10%. 🔴 High: above 10%.

---

**Step 5 — Use Overall Counterfactual Confidence as the final summary**

- **Overall Counterfactual Confidence** — Not a simple worst-of-four vote. Rolling Validation Error is the primary check and acts as a gate: a high-risk validation error alone is enough to make confidence low. Overfitting Risk, Autocorrelation Risk, and Rolling Bias Risk are evaluated next in that priority order — a flag on any of them holds confidence at moderate, but only Rolling Validation Error can push it all the way down to low. **Key Issues** lists every high- and moderate-risk check that contributed, not just the single worst one.
    - 🟢 **High confidence** — Suitable to proceed, assuming the business context also makes sense.
    - 🟡 **Moderate confidence** — Usable, but interpret uplift cautiously and check the Key Issues.
    - 🔴 **Low confidence** — Don't rely on the counterfactual without improving the model, controls, or time window.
    - ⚪ **Insufficient data** — Not enough validation evidence to make a reliable judgement. This is a data-availability gap, not evidence that confidence is high.

Traffic-light bands are interpretation aids based on validation diagnostics. They are not standalone hypothesis tests.

---

**Step 6 — Review Placebo Testing**

Placebo tests simulate running a fake intervention across all available historical windows. A well-behaved model produces placebo uplifts clustered near zero.

- **Median Placebo Uplift** — Should be close to 0%. Large values mean the model consistently finds phantom effects.
- **95% Placebo Uplift Range** — The full spread of placebo (fake-test) uplifts. **This is your rough minimum detectable effect:** if your target uplift is smaller than this range, the design may lack power to distinguish a real signal from historical noise. A wide range also means the model is volatile — your real test uplift will need to sit clearly outside it to be credible.

---

**Step 7 — Use Pre-Period Fit as a sanity check only**

Pre-period Correlation is shown for reference but can be misleadingly high — a model can fit the pre-period well and still fail out-of-sample. Always weight the rolling-origin metrics more heavily.

---

**Rule of thumb:** Low rolling-origin sMAPE + High/Moderate Overall Counterfactual Confidence + a narrow, tight placebo distribution = a reliable test design ready to run.
            """)
        else:
            st.markdown("""
**Measure Test Impact — How to read this**

Before trusting the uplift estimate, verify the model can reliably predict the test KPI. An unreliable model produces an unreliable uplift number.

We recommend interpreting the checks in this order: rolling-origin validation error, then overfitting, then residual diagnostics, then rolling bias, then Overall Counterfactual Confidence as the final summary.

---

**Step 1 — Start with Rolling-Origin Validation Error**

This is the main check for whether the model can predict unseen historical data, and should be treated as the primary model-quality check.

- **Rolling-Origin sMAPE (%)** — Typical out-of-sample prediction error. Lower validation sMAPE means the counterfactual is likely to be more trustworthy. 🟢 Low: 10% or below. 🟡 Moderate: above 10% up to 15%. 🔴 High: above 15% — treat the uplift estimate with caution, since the counterfactual baseline is uncertain.

---

**Step 2 — Then check Overfitting**

Compare pre-period fit against rolling-origin validation. A large gap means the model may look good in-sample but perform poorly on unseen data.

- **Overfitting Gap, sMAPE percentage points** — Rolling-origin validation sMAPE minus pre-period sMAPE. 🟢 Low: up to 3 percentage points. 🟡 Moderate: above 3 up to 5 percentage points. 🔴 High: above 5 percentage points.

---

**Step 3 — Then check Residual Diagnostics**

Use Durbin-Watson / autocorrelation risk to assess whether residuals are independent enough. Strong autocorrelation means the model may be missing time structure.

- **Durbin-Watson** / **Autocorrelation Risk** — 🟢 Low autocorrelation risk: 1.5 to 2.5. 🟡 Moderate autocorrelation risk: 1.2 to just under 1.5, or above 2.5 up to 2.8. 🔴 High autocorrelation risk: below 1.2 or above 2.8. These are practical diagnostic bands, not formal critical-value tests.

---

**Step 4 — Then check Rolling Bias**

Bias tells you whether the model systematically over- or under-predicts in validation windows. A model that consistently undershoots will overstate uplift, and vice versa. If bias is moderate or high, be more cautious about interpreting the uplift.

- **Rolling-Origin Bias (%)** — 🟢 Low: absolute bias 5% or below. 🟡 Moderate: above 5% up to 10%. 🔴 High: above 10%.

---

**Step 5 — Use Overall Counterfactual Confidence as the final summary**

- **Overall Counterfactual Confidence** — Not a simple worst-of-four vote. Rolling Validation Error is the primary check and acts as a gate: a high-risk validation error alone is enough to make confidence low, particularly for data-optimised methods. Overfitting Risk, Autocorrelation Risk, and Rolling Bias Risk are evaluated next in that priority order — a flag on any of them holds confidence at moderate, but only Rolling Validation Error can push it all the way down to low. **Key Issues** lists every high- and moderate-risk check that contributed, not just the single worst one.
    - 🟢 **High confidence** — Suitable to proceed, assuming the business context also makes sense.
    - 🟡 **Moderate confidence** — Usable, but interpret uplift cautiously and check the Key Issues, particularly for data-optimised methods.
    - 🔴 **Low confidence** — Don't rely on the counterfactual without improving the model, controls, or time window.
    - ⚪ **Insufficient data** — Not enough rolling-origin history to assess this at all; treat the uplift with the same caution you would give a low-confidence result.
- **95% Placebo Uplift Range** — The range of apparent uplifts the model detects in historical periods with no intervention. Your observed uplift needs to sit clearly outside this range.

Traffic-light bands are interpretation aids based on validation diagnostics. They are not standalone hypothesis tests.

---

**Step 6 — Assess the uplift result**

- **Observed Uplift Percentile vs Placebos** — Where your observed uplift ranks relative to the distribution of historical placebo (fake-test) uplifts. 95th percentile or above is stronger evidence that the observed uplift is unusual relative to pre-period noise.
- **Observed Uplift p-value** — The placebo p-value is an empirical extremeness check: it shows how unusual the observed uplift is relative to historical fake-test windows. It is not proof of causality and should be interpreted alongside model fit, rolling-origin validation, and business context. Below 0.05 is the conventional (approximate) threshold analysts use as a rule of thumb.
- **A precision note:** both of these are only as fine-grained as the number of **Placebo Windows** available (shown in section F). With, say, 10 placebo windows, the p-value can only land on multiples of 0.1 — it's not possible to observe a "real" 0.02. Check the Placebo Windows count before leaning heavily on a borderline p-value or percentile.

---

**Step 7 — Compare methods**

If you ran multiple methods (Structural, Data-Optimised), look for agreement. When both methods produce similar uplift estimates, both show good out-of-sample fit, and both show High/Moderate Overall Counterfactual Confidence, confidence in the result is higher.

---

**Rule of thumb:** Low rolling-origin sMAPE + High/Moderate Overall Counterfactual Confidence + observed uplift outside the placebo range + a small p-value = a result with stronger evidence behind it, though still not proof of causality on its own.
            """)


def render_time_series_validation(mode: str):
    """
    Shared UI for Design (Tab 2) and Evaluate (Tab 3).
    mode is either "Design" or "Evaluate".
    Pass the literal string used in the existing working-file logic.
    """
    if st.session_state.final_controls is None:
        st.info("Complete the Matching Setup in the **Region Matching** tab first.")
        return

    # -------------------------------------------------------------------------
    # Session state for validation (persist across reruns)
    # -------------------------------------------------------------------------
    if "validation_results" not in st.session_state:
        st.session_state.validation_results = None
    if "validation_triggered" not in st.session_state:
        st.session_state.validation_triggered = False
    if "kpi_long_df" not in st.session_state:
        st.session_state.kpi_long_df = None
    if "kpi_available_dates" not in st.session_state:
        st.session_state.kpi_available_dates = []
    if "kpi_metric_options" not in st.session_state:
        st.session_state.kpi_metric_options = []
    if "file_upload_key" not in st.session_state:
        st.session_state.file_upload_key = 0
    if "bayesian_results" not in st.session_state:
        st.session_state.bayesian_results = None
    if "bayesian_interpretation_visible" not in st.session_state:
        st.session_state.bayesian_interpretation_visible = False

    # -------------------------------------------------------------------------
    # Helper to clear previous validation results
    # -------------------------------------------------------------------------
    def clear_validation_state():
        st.session_state.validation_results = None
        st.session_state.validation_triggered = False
        st.session_state.bayesian_results = None
        st.session_state.bayesian_interpretation_visible = False

    def clear_uploaded_kpi_state():
        """Clear validation/Bayesian results AND the previously parsed KPI file, so a newly
        uploaded file can't leave stale parsed data (dates, metric list, long-format df) behind."""
        clear_validation_state()
        st.session_state.kpi_long_df = None
        st.session_state.kpi_available_dates = []
        st.session_state.kpi_metric_options = []

    # -------------------------------------------------------------------------
    # 1. Data Source
    # -------------------------------------------------------------------------
    st.markdown("### Data Source")
    st.caption("Upload your historical KPI data and select the metric to model.")

    # Use mode-prefixed uploader key so Design and Evaluate get independent file widgets
    mode_prefix = "design" if mode == "Design" else "evaluate"
    uploaded_file = st.file_uploader(
        "Upload historical KPI Excel file",
        type=["xlsx"],
        key=f"kpi_uploader_{mode_prefix}_{st.session_state.file_upload_key}",
        help="Expected format: first column = region name, second column = metric name, then date columns across the top (weekly or daily).",
        on_change=clear_uploaded_kpi_state
    )

    if uploaded_file is None:
        st.info("📂 Please upload a historical KPI Excel file to begin.")
        return

    if st.session_state.kpi_long_df is None:
        with st.spinner("Reading KPI file..."):
            df_long = load_and_reshape_kpi(uploaded_file)
            st.session_state.kpi_long_df = df_long
            st.session_state.kpi_available_dates = sorted(df_long["date"].dt.date.unique())
            st.session_state.kpi_metric_options = sorted(df_long["metric_name"].unique())

    if st.session_state.kpi_long_df is None:
        st.error("Failed to read the KPI file.")
        st.stop()

    df_long = st.session_state.kpi_long_df
    available_dates = st.session_state.kpi_available_dates
    metric_options = st.session_state.kpi_metric_options

    if not metric_options:
        st.error("No metric names found in second column of the KPI file.")
        st.stop()

    with st.expander("Summary of Uploaded Data", expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Regions detected", df_long["region_raw"].nunique())
        col2.metric("KPIs found", len(metric_options))
        col3.metric("Date range", f"{available_dates[0].strftime('%d %b %y')} – {available_dates[-1].strftime('%d %b %y')}")
        col4.metric("Observed date points", len(available_dates), help="Number of distinct dates found in the uploaded file, independent of the selected time series frequency.")

    st.markdown("""
<div style="background:#E6F7F5; border-left:4px solid #0F766E; border-radius:6px; padding:0.75rem 1rem 0.25rem 1rem; margin-bottom:0.75rem;">
<span style="font-weight:600; color:#0F766E; font-size:1rem;">📊 Select KPI</span><br>
<span style="color:#4B5563; font-size:0.875rem;">Choose the metric you want to model. This drives all validation, placebo, and uplift results.</span>
</div>
""", unsafe_allow_html=True)
    selected_metric = st.selectbox(
        "KPI to analyse",
        metric_options,
        key=f"{mode_prefix}_selected_metric",
        help="The metric used to assess how well the control regions track the test regions over time. Choose the KPI you plan to measure in your geo test.",
        on_change=clear_validation_state,
        label_visibility="collapsed"
    )

    # -------------------------------------------------------------------------
    # 1b. Observed dates for the selected KPI — computed from the actual dates present
    # for the selected metric, so frequency inference, period counts, and slider defaults
    # all reflect real data coverage for THIS metric rather than the whole file or a
    # calendar assumption (robust to missing/irregular dates, and to metrics with
    # different date coverage than each other).
    # -------------------------------------------------------------------------
    _metric_dates_series = df_long.loc[df_long["metric_name"] == selected_metric, "date"]
    if not _metric_dates_series.empty:
        _metric_dates_all = pd.to_datetime(_metric_dates_series).dt.normalize()
    else:
        _metric_dates_all = pd.to_datetime(pd.Series(available_dates))

    def _observed_period_count(start_val, end_val):
        if start_val is None or end_val is None:
            return None
        start_ts = pd.Timestamp(start_val)
        end_ts = pd.Timestamp(end_val)
        mask = (_metric_dates_all >= start_ts) & (_metric_dates_all <= end_ts)
        return int(_metric_dates_all[mask].nunique())

    # -------------------------------------------------------------------------
    # 1c. Time series frequency (shared by Validate Test Design and Measure Test
    # Impact, and passed through to Bayesian TBR via validation_results)
    # -------------------------------------------------------------------------
    st.markdown("**Time series frequency**")
    time_series_frequency = st.radio(
        "Time series frequency",
        options=["weekly", "daily"],
        format_func=lambda v: "Weekly" if v == "weekly" else "Daily",
        index=0 if st.session_state.get("time_series_frequency", "weekly") == "weekly" else 1,
        key=f"{mode_prefix}_time_series_frequency",
        horizontal=True,
        help="Weekly data uses a 1-week lag and week-based windows. Daily data uses a 7-day lag (same day-of-week comparison) and day-based windows.",
        on_change=clear_validation_state,
        label_visibility="collapsed"
    )
    st.session_state.time_series_frequency = time_series_frequency
    freq_config = get_frequency_config(time_series_frequency)

    if time_series_frequency == "daily":
        st.info(
            "ℹ️ Daily data can be noisier than weekly data and often contains day-of-week effects. "
            "Use longer pre-periods, check rolling-origin validation carefully, and prefer the 7-day "
            "lag option if lagged controls are enabled.\n\n"
            "Daily analysis is useful when you need more granular monitoring, but weekly aggregation "
            "is usually more stable for final geo-test readouts. Daily results should be judged "
            "carefully using rolling-origin validation, placebo ranges and model validation diagnostics."
        )

    # Frequency inference uses the SELECTED METRIC's own dates (not the whole file), since
    # different metrics can have different date coverage. A confirmed daily-vs-weekly
    # mismatch is treated as a hard blocker (not just a soft suggestion) because it can
    # silently change how lags behave and make results misleading — see the checkbox below.
    _inferred_freq = infer_time_series_frequency(_metric_dates_all)
    frequency_mismatch_detected = False
    if _inferred_freq == "daily" and time_series_frequency == "weekly":
        frequency_mismatch_detected = True
        st.error(
            "The uploaded KPI dates look daily, but weekly mode is selected. "
            "Weekly mode expects already weekly-aggregated data and does not aggregate daily rows automatically. "
            "In this mode, a 1-week lag is implemented as a 1-row lag, which would behave like a 1-day lag on daily data."
        )
    elif _inferred_freq == "weekly" and time_series_frequency == "daily":
        frequency_mismatch_detected = True
        st.error(
            "The uploaded KPI dates look weekly, but daily mode is selected. "
            "Daily mode expects daily rows and uses a true calendar 7-day lag. "
            "Please switch to weekly mode unless the file genuinely contains daily data."
        )
    elif _inferred_freq != "unknown" and _inferred_freq != time_series_frequency:
        # Covers any other inferred/selected mismatch not caught by the two explicit cases above.
        frequency_mismatch_detected = True
        st.error(
            f"The uploaded data looks like it may be **{_inferred_freq}** data (based on the typical "
            f"gap between dates), but **{'Weekly' if time_series_frequency == 'weekly' else 'Daily'}** "
            f"is currently selected. This mismatch can make lag behaviour and validation results misleading."
        )

    frequency_mismatch_acknowledged = True
    if frequency_mismatch_detected:
        frequency_mismatch_acknowledged = st.checkbox(
            "I understand the frequency mismatch and want to continue",
            value=False,
            key=f"{mode_prefix}_frequency_mismatch_ack",
        )
        if not frequency_mismatch_acknowledged:
            st.info("Validation and Bayesian TBR are disabled until the frequency mismatch above is acknowledged or resolved.")
    st.session_state.frequency_mismatch_blocked = frequency_mismatch_detected and not frequency_mismatch_acknowledged

    # -------------------------------------------------------------------------
    # 2. Analysis Type header (static — driven by the tab the user is in)
    # -------------------------------------------------------------------------
    if mode == "Design":
        selected_label = "Design a future geo test"
    else:
        selected_label = "Evaluate a completed geo test"

    date_options = {d.strftime("%d %b %y"): d for d in available_dates}
    date_list = list(date_options.keys())

    # -------------------------------------------------------------------------
    # 3. Configuration (depends on mode) — EXACTLY as in working file
    # -------------------------------------------------------------------------
    insufficient_pre_period = False
    if mode == "Design":
        st.markdown("---")
        st.markdown("### Historical Period")
        st.caption("Define the historical date range used to assess whether test and control regions move together.")

        col_start, col_end = st.columns(2)
        with col_start:
            design_start_label = st.selectbox(
                "Historical period start",
                date_list,
                index=0,
                key=f"{mode_prefix}_design_start",
                on_change=clear_validation_state
            )
            design_start = date_options[design_start_label]
        with col_end:
            design_end_label = st.selectbox(
                "Historical period end",
                date_list,
                index=len(date_list)-1,
                key=f"{mode_prefix}_design_end",
                on_change=clear_validation_state
            )
            design_end = date_options[design_end_label]

        if design_start >= design_end:
            st.error("Start date must be before end date.")
            st.stop()

        pre_start = pd.Timestamp(design_start)
        pre_end = pd.Timestamp(design_end)
        test_start = None
        test_end = None
        use_post = False
        post_start = None
        post_end = None
        compute_uplift = True
        summary_label = "Design period"

        st.markdown("---")
        st.markdown("### Validation & Placebo Settings")
        _period_divisor = 1 if freq_config["frequency"] == "daily" else 7
        pre_periods_design = _observed_period_count(design_start, design_end)
        if not pre_periods_design:
            # Fallback to a calendar-span estimate if observed dates couldn't be computed
            pre_periods_design = (pd.Timestamp(design_end) - pd.Timestamp(design_start)).days // _period_divisor + 1
        default_placebo_len = freq_config["default_validation_horizon_periods"]
        _min_training_floor = 6 if freq_config["frequency"] == "weekly" else 14
        _placebo_slider_min = 2 if freq_config["frequency"] == "weekly" else 7
        _placebo_slider_max = 12 if freq_config["frequency"] == "weekly" else 90

        _slider_col1, _slider_col2 = st.columns(2)
        with _slider_col1:
            _max_min_training = max(_min_training_floor, pre_periods_design - default_placebo_len)
            _default_min_training = min(freq_config["default_min_training_periods"], _max_min_training)
            min_training_periods = st.slider(
                f"Minimum training period ({freq_config['period_label_plural']})",
                min_value=_min_training_floor,
                max_value=_max_min_training,
                value=_default_min_training,
                step=1,
                key=f"{mode_prefix}_min_training_slider",
                help=f"Minimum {freq_config['period_label_plural']} of history required before each validation or placebo window. Higher = stricter and more realistic, but fewer windows are generated.",
                on_change=clear_validation_state
            )
        with _slider_col2:
            _placebo_default_value = min(max(default_placebo_len, _placebo_slider_min), _placebo_slider_max)
            placebo_length_periods = st.slider(
                f"Test & placebo window length ({freq_config['period_label_plural']})",
                min_value=_placebo_slider_min,
                max_value=_placebo_slider_max,
                value=_placebo_default_value,
                step=1,
                key=f"{mode_prefix}_placebo_slider",
                help="Length of each simulated test window used for placebo testing and rolling-origin validation. Set this to match your planned test duration.",
                on_change=clear_validation_state
            )

        # ---- Definitive pre-period sufficiency check, using the ACTUAL selected slider
        # values (not just their floors) — this is what run_validation_method will use to
        # build at least one rolling-origin / placebo window. ----
        insufficient_pre_period = pre_periods_design < (min_training_periods + placebo_length_periods)
        if insufficient_pre_period:
            st.warning(
                "⚠️ Not enough pre-period observations for the selected minimum training period and "
                "validation window. Choose a longer pre-period, shorter validation window, or switch to "
                "weekly aggregation."
            )

    else:  # Evaluate
        st.markdown("---")
        st.markdown("### Define Test Periods")
        st.caption("Set the pre‑test, test, and (optionally) post‑test periods.")

        st.markdown("**Pre‑test period**")
        col_pre1, col_pre2 = st.columns(2)
        with col_pre1:
            pre_start_label = st.selectbox(
                "Start",
                date_list,
                index=0,
                key=f"{mode_prefix}_pre_start",
                on_change=clear_validation_state
            )
            pre_start = date_options[pre_start_label]
        with col_pre2:
            pre_end_idx = min(len(date_list)-1, int(len(date_list)*0.75))
            pre_end_label = st.selectbox(
                "End",
                date_list,
                index=pre_end_idx,
                key=f"{mode_prefix}_pre_end",
                on_change=clear_validation_state
            )
            pre_end = date_options[pre_end_label]

        st.markdown("**Test period**")
        col_test1, col_test2 = st.columns(2)
        with col_test1:
            test_start_idx = min(len(date_list)-1, pre_end_idx + 5)
            test_start_label = st.selectbox(
                "Start",
                date_list,
                index=test_start_idx,
                key=f"{mode_prefix}_test_start",
                on_change=clear_validation_state
            )
            test_start = date_options[test_start_label]
        with col_test2:
            test_end_idx = min(len(date_list)-1, test_start_idx + 5)
            test_end_label = st.selectbox(
                "End",
                date_list,
                index=test_end_idx,
                key=f"{mode_prefix}_test_end",
                on_change=clear_validation_state
            )
            test_end = date_options[test_end_label]

        use_post = st.checkbox(
            "Include post‑test period",
            value=False,
            key=f"{mode_prefix}_use_post",
            on_change=clear_validation_state
        )
        if use_post:
            st.markdown("**Post‑test period**")
            col_post1, col_post2 = st.columns(2)
            with col_post1:
                post_start_idx = min(len(date_list)-1, test_end_idx + 2)
                post_start_label = st.selectbox(
                    "Start",
                    date_list,
                    index=post_start_idx,
                    key=f"{mode_prefix}_post_start",
                    on_change=clear_validation_state
                )
                post_start = date_options[post_start_label]
            with col_post2:
                post_end_label = st.selectbox(
                    "End",
                    date_list,
                    index=len(date_list)-1,
                    key=f"{mode_prefix}_post_end",
                    on_change=clear_validation_state
                )
                post_end = date_options[post_end_label]
        else:
            post_start = post_end = None

        # ---- Validation window settings ----
        st.markdown("---")
        st.markdown("### Validation & Placebo Settings")
        _period_divisor = 1 if freq_config["frequency"] == "daily" else 7
        if test_start is not None and test_end is not None:
            default_placebo_len = _observed_period_count(test_start, test_end)
            if not default_placebo_len:
                # Fallback to a calendar-span estimate if observed dates couldn't be computed
                if freq_config["frequency"] == "daily":
                    default_placebo_len = max(2, (test_end - test_start).days + 1)
                else:
                    default_placebo_len = max(2, (test_end - test_start).days // 7 + 1)
            default_placebo_len = max(2, default_placebo_len)
        else:
            default_placebo_len = freq_config["default_validation_horizon_periods"]
        pre_periods_eval = _observed_period_count(pre_start, pre_end)
        if not pre_periods_eval:
            pre_periods_eval = (pd.Timestamp(pre_end) - pd.Timestamp(pre_start)).days // _period_divisor + 1
        _min_training_floor = 6 if freq_config["frequency"] == "weekly" else 14
        # Note: unlike Design mode, there's no _placebo_slider_min/_placebo_slider_max here —
        # placebo_length_periods is locked to default_placebo_len below, not user-adjustable.

        _slider_col1, _slider_col2 = st.columns(2)
        with _slider_col1:
            _max_min_training = max(_min_training_floor, pre_periods_eval - default_placebo_len)
            _default_min_training = min(freq_config["default_min_training_periods"], _max_min_training)
            min_training_periods = st.slider(
                f"Minimum training period ({freq_config['period_label_plural']})",
                min_value=_min_training_floor,
                max_value=_max_min_training,
                value=_default_min_training,
                step=1,
                key=f"{mode_prefix}_min_training_slider",
                help=f"Minimum {freq_config['period_label_plural']} of history required before each validation or placebo window. Higher = stricter and more realistic, but fewer windows are generated.",
                on_change=clear_validation_state
            )
        with _slider_col2:
            # ---- LOCKED, not a slider, in Evaluate mode. ----
            # The observed uplift is always computed over the actual test_start..test_end
            # dates (see run_validation_method()), so every placebo window used to build
            # the comparison distribution — and the rolling-origin validation horizon,
            # which is deliberately kept equal to it (see cv_horizon in
            # run_validation_method()) — MUST use that same window length. If this were
            # independently adjustable, the observed uplift (summed over N_test periods)
            # could be compared against placebo uplifts summed over a different number of
            # periods, silently invalidating the percentile rank, p-value, and z-score in
            # the "Observed Uplift vs Placebos" section — cumulative uplift and its
            # variance both scale with window length, so the two would no longer be on
            # the same scale.
            placebo_length_periods = default_placebo_len
            st.metric(
                f"Test & placebo window length ({freq_config['period_label_plural']})",
                placebo_length_periods,
            )
            st.caption(
                "Locked to your observed test period length so placebo windows and "
                "rolling-origin validation folds stay directly comparable to your actual "
                "test."
            )

        # ---- Definitive pre-period sufficiency check, using the ACTUAL selected slider
        # values (not just their floors) — this is what run_validation_method will use to
        # build at least one rolling-origin / placebo window. ----
        insufficient_pre_period = pre_periods_eval < (min_training_periods + placebo_length_periods)
        if insufficient_pre_period:
            st.warning(
                "⚠️ Not enough pre-period observations for the selected minimum training period and "
                "validation window. Choose a longer pre-period, shorter validation window, or switch to "
                "weekly aggregation."
            )

        # Convert to Timestamps and validate
        pre_start = pd.Timestamp(pre_start)
        pre_end = pd.Timestamp(pre_end)
        test_start = pd.Timestamp(test_start)
        test_end = pd.Timestamp(test_end)
        if use_post and post_start is not None:
            post_start = pd.Timestamp(post_start)
            post_end = pd.Timestamp(post_end)

        if pre_start >= pre_end:
            st.error("Pre‑test period start must be before end.")
            st.stop()
        if test_start >= test_end:
            st.error("Test period start must be before end.")
            st.stop()
        if test_start <= pre_end:
            st.warning("Test period starts before pre‑test period ends. Consider adjusting.")
        compute_uplift = True
        summary_label = "Pre-test period"

    # -------------------------------------------------------------------------
    # 3c. Lagged Controls Option (applies to Validate Test Design, Measure Test
    # Impact, and — via the shared session_state flag — Bayesian TBR)
    # -------------------------------------------------------------------------
    st.markdown("---")
    include_lagged_controls = st.checkbox(
        f"Include {freq_config['lag_label']} lagged controls",
        value=st.session_state.get("include_lagged_controls", False),
        key=f"{mode_prefix}_include_lagged_controls",
        help=(
            f"Adds each control region\u2019s KPI from {freq_config['lag_periods']} "
            f"{freq_config['period_label_singular'] if freq_config['lag_periods'] == 1 else freq_config['period_label_plural']} "
            "earlier as an additional predictor. This can help when the test region follows control-region "
            "movements with a short delay, but it increases the number of predictors and should be judged "
            "using rolling-origin validation."
            + (" For daily data, the 7-day lag compares the same day of week to avoid confusing day-of-week "
               "seasonality with a true lagged relationship." if freq_config["frequency"] == "daily" else "")
        ),
        on_change=clear_validation_state
    )
    st.session_state.include_lagged_controls = include_lagged_controls

    if freq_config["frequency"] == "daily" and not include_lagged_controls:
        st.info(
            "ℹ️ Daily data often has strong day-of-week effects. The 7-day lag option can help compare the "
            "same day of week, but it also increases feature count and model reliability risk. Use rolling-origin "
            "validation to decide whether it improves the model."
        )

    # -------------------------------------------------------------------------
    # 4. Validation Summary (compact card before the run button)
    # -------------------------------------------------------------------------
    st.markdown("---")
    st.markdown("### Validation Summary")

    test_regions = st.session_state.selected_experiment_regions
    control_regions = st.session_state.final_controls[geo_col].tolist()
    n_test = len(test_regions)
    n_control = len(control_regions)

    # ---- Observed period counts (preferred over calendar-span estimates) ----
    # Reuses the _observed_period_count() helper defined earlier in this function (section 1c),
    # which is based on the actual dates present for the selected KPI.
    if mode == "Design":
        hist_periods = _observed_period_count(pre_start, pre_end)
        test_length = None
        placebo_len = placebo_length_periods
    else:
        hist_periods = _observed_period_count(pre_start, pre_end)
        test_length = _observed_period_count(test_start, test_end)
        placebo_len = None

    with st.container(border=True):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("KPI", selected_metric)
        col2.metric("Test regions", n_test)
        col3.metric("Control regions", n_control)
        col4.metric("Analysis type", selected_label)

        col5, col6, col7 = st.columns(3)
        col5.metric(f"Historical period (observed {freq_config['period_label_plural']})", hist_periods)
        if mode == "Design":
            col6.metric(f"Simulated test length ({freq_config['period_label_plural']})", placebo_len)
            col7.empty()
        else:
            col6.metric(f"Test period (observed {freq_config['period_label_plural']})", test_length)
            col7.metric("Post‑test included", "Yes" if use_post else "No")

    # ---- Daily short-history caution (does not block the user) ----
    if freq_config["frequency"] == "daily" and hist_periods is not None:
        _horizon_for_check = placebo_length_periods if placebo_length_periods is not None else freq_config["default_validation_horizon_periods"]
        _est_folds = hist_periods - min_training_periods - _horizon_for_check + 1
        if hist_periods < 84:
            st.warning(
                "⚠️ Daily data with fewer than around 12 weeks of pre-period history may produce unstable "
                "validation and placebo results. Treat model reliability risk, rolling-origin metrics and placebo "
                "ranges with caution."
            )
        elif _est_folds < 5:
            st.warning(
                "⚠️ With the current minimum training period and window length, rolling-origin validation is "
                "likely to produce very few folds. Treat model reliability risk, rolling-origin metrics and placebo "
                "ranges with caution, or consider a longer pre-period / shorter window."
            )

    # -------------------------------------------------------------------------
    # 5. Run button
    # -------------------------------------------------------------------------
    run_label = "Assess Region Alignment" if mode == "Design" else "Evaluate Test Impact"
    _freq_mismatch_blocked = st.session_state.get("frequency_mismatch_blocked", False)
    _run_disabled = insufficient_pre_period or _freq_mismatch_blocked
    if _freq_mismatch_blocked:
        _run_help = "Resolve or acknowledge the frequency mismatch warning above before running."
    elif insufficient_pre_period:
        _run_help = "Resolve the pre-period data warning above before running."
    else:
        _run_help = None
    validate_clicked = st.button(
        run_label,
        width='stretch',
        type="primary",
        key=f"{mode_prefix}_run_button",
        disabled=_run_disabled,
        help=_run_help
    )

    if validate_clicked:
        st.session_state.validation_triggered = True

    # -------------------------------------------------------------------------
    # Process validation if triggered — IDENTICAL to working file
    # -------------------------------------------------------------------------
    if st.session_state.validation_triggered:
        if uploaded_file is None or st.session_state.kpi_long_df is None:
            st.error("KPI file not available. Please upload a file first.")
            st.session_state.validation_triggered = False
            st.stop()
        if st.session_state.get("frequency_mismatch_blocked", False):
            st.error("Validation cannot run while there is an unacknowledged frequency mismatch. Please resolve or acknowledge it above.")
            st.session_state.validation_triggered = False
            st.stop()

        if insufficient_pre_period:
            st.error(
                "Not enough pre-period observations for the selected minimum training period and "
                "validation window. Choose a longer pre-period, shorter validation window, or switch to "
                "weekly aggregation."
            )
            st.session_state.validation_triggered = False
            st.stop()

        with st.spinner("Running validation models..."):
            try:
                master_df = load_market_sheet(DATA_PATH, market)
                adobe_to_geo = dict(zip(
                    master_df[ADOBE_COL].astype(str).str.strip(),
                    master_df[geo_col].astype(str).str.strip()
                ))
            except Exception as e:
                st.error(f"Failed to load region mapping: {e}")
                st.stop()

            test_regions_val = st.session_state.selected_experiment_regions
            control_regions_val = st.session_state.final_controls[geo_col].tolist()
            force_excluded_regions = st.session_state.get("force_ctrl_exclude", [])
            if not isinstance(force_excluded_regions, list):
                force_excluded_regions = []

            df_long_metric = df_long[df_long["metric_name"] == selected_metric].copy()
            if df_long_metric.empty:
                st.error(f"No data for selected metric: {selected_metric}")
                st.stop()

            df_long_raw = df_long_metric.copy()
            df_long_mapped = build_region_mapping(df_long_raw, test_regions_val, control_regions_val, adobe_to_geo)
            matched = df_long_mapped[df_long_mapped["region"].notna()]
            if matched.empty:
                st.error("No regions matched. Check mapping table.")
                st.stop()

            agg_df_val = apply_geo_aggregation(matched, geo_col)
            if agg_df_val.empty:
                st.error("Aggregation resulted in empty dataset.")
                st.stop()

            # Region Mapping Diagnostics (unchanged)
            with st.expander("Region Mapping Diagnostics"):
                raw_count = len(df_long_raw["region_raw"].unique())
                matched_count = len(matched["region"].unique())
                unmatched_count = len(df_long_raw[~df_long_raw["region_raw"].isin(matched["region_raw"].unique())]["region_raw"].unique())
                agg_count = len(agg_df_val["region"].unique())
                unmatched_names = ", ".join(df_long_raw[~df_long_raw["region_raw"].isin(matched["region_raw"].unique())]["region_raw"].unique().tolist()) if unmatched_count > 0 else "None"
                diag_data = {
                    "Metric": [
                        "Raw geographies in KPI file",
                        "Matched to aggregation level",
                        "Unmatched",
                        "Aggregated geographies",
                        "Unmatched names"
                    ],
                    "Value": [
                        str(raw_count),
                        str(matched_count),
                        str(unmatched_count),
                        str(agg_count),
                        unmatched_names
                    ]
                }
                st.dataframe(pd.DataFrame(diag_data), width='stretch', hide_index=True)

                all_regions = sorted(agg_df_val["region"].unique())
                role_rows = []
                for region in all_regions:
                    if region in test_regions_val:
                        m1_role = "Test Region"
                    elif region in force_excluded_regions:
                        m1_role = "Force-Excluded Region"
                    elif region in control_regions_val:
                        m1_role = "Matched Control Region"
                    else:
                        m1_role = "Unused Candidate Region"

                    if region in test_regions_val:
                        m2_role = "Test Region"
                    else:
                        m2_role = "Control Candidate Region"

                    if force_excluded_regions:
                        if region in test_regions_val:
                            m3_role = "Test Region"
                        elif region in force_excluded_regions:
                            m3_role = "Force-Excluded Region"
                        else:
                            m3_role = "Control Candidate Region"
                    else:
                        m3_role = None

                    row = {
                        "Region": region,
                        "Method 1 (Structurally Matched)": m1_role,
                        "Method 2 (Data-Optimised)": m2_role,
                    }
                    if force_excluded_regions:
                        row["Method 3 (Data-Optimised Excl.)"] = m3_role
                    role_rows.append(row)

                role_df = pd.DataFrame(role_rows)
                def color_roles(val):
                    if val == "Test Region":
                        return "background-color: #90EE90"
                    elif val == "Matched Control Region":
                        return "background-color: #FFFACD"
                    elif val == "Control Candidate Region":
                        return "background-color: #ADD8E6"
                    elif val == "Force-Excluded Region":
                        return "background-color: #FFCCCB"
                    elif val == "Unused Candidate Region":
                        return "background-color: #D3D3D3"
                    else:
                        return ""
                role_cols = [c for c in role_df.columns if c != "Region"]
                styled_role = role_df.style.map(color_roles, subset=role_cols)
                st.dataframe(styled_role, width='stretch', hide_index=True)

            # Regional KPI Summary (unchanged)
            st.subheader("KPI Performance by Geography")
            summary_start = pd.Timestamp(pre_start)
            summary_end = pd.Timestamp(pre_end)
            summary_df = agg_df_val[
                (agg_df_val["date"] >= summary_start) &
                (agg_df_val["date"] <= summary_end)
            ].copy()
            n_periods = summary_df["date"].nunique()
            st.caption(
                f"{summary_label}:  "
                f"⏱️ {n_periods} {freq_config['period_label_plural']}  |  "
                f"📅 {summary_start:%d %b %Y} – {summary_end:%d %b %Y}"
            )
            if n_periods == 0:
                st.warning("No data available in the selected date range.")
            else:
                region_stats = []
                total_kpi = summary_df["kpi"].sum()
                kpi_name = selected_metric
                for region in sorted(summary_df["region"].unique()):
                    region_data = summary_df[summary_df["region"] == region]
                    total = region_data["kpi"].sum()
                    avg = region_data["kpi"].mean()
                    std = region_data["kpi"].std()
                    cv = std / avg if avg != 0 else np.nan
                    if cv < 0.2:
                        vol_flag = "Low"
                    elif cv < 0.5:
                        vol_flag = "Medium"
                    else:
                        vol_flag = "High"
                    status = "Test Region" if region in test_regions_val else ("Matched Control Region" if region in control_regions_val else ("Force-Excluded Region" if region in force_excluded_regions else "Unused Candidate Region"))
                    region_stats.append({
                        "Region": region,
                        "Status": status,
                        f"Total {kpi_name}": total,
                        "Share (%)": (total / total_kpi) * 100 if total_kpi > 0 else 0,
                        f"Avg. {kpi_name} per {freq_config['period_label_singular']}": avg,
                        "Std dev": std,
                        "Coefficient of Variation": cv,
                        "Volatility": vol_flag
                    })
                desc_df = pd.DataFrame(region_stats)
                def color_status(val):
                    if val == "Test Region":
                        return "background-color: #90EE90"
                    elif val == "Matched Control Region":
                        return "background-color: #FFFACD"
                    elif val == "Force-Excluded Region":
                        return "background-color: #FFCCCB"
                    else:
                        return "background-color: #D3D3D3"
                styled_desc = desc_df.style.format({
                    f"Total {kpi_name}": "{:,.0f}",
                    "Share (%)": "{:.1f}%",
                    f"Avg. {kpi_name} per {freq_config['period_label_singular']}": "{:.1f}",
                    "Std dev": lambda x: f"±{x:.1f}",
                    "Coefficient of Variation": "{:.3f}"
                }).map(color_status, subset=["Status"])
                st.dataframe(styled_desc, width='stretch')

            # -------------------------------------------------------------
            # 7. Run validation methods — IDENTICAL to working file
            # -------------------------------------------------------------
            st.subheader("Validation Results")
            results = {}

            method1_key = METHOD_USER_SELECTED if st.session_state.get("user_selected_mode", False) else METHOD_STRUCTURAL

            with st.spinner(f"Running {method1_key}..."):
                res1 = run_validation_method(
                    agg_df_val, control_regions_val, test_regions_val, "enet",
                    pre_start, pre_end, test_start, test_end,
                    use_post, post_start, post_end,
                    compute_uplift=compute_uplift,
                    placebo_length_periods=placebo_length_periods,
                    min_training_periods=min_training_periods,
                    include_lagged_controls=st.session_state.get("include_lagged_controls", False),
                    frequency_config=freq_config
                )
                if res1 is None:
                    st.error(f"{method1_key} failed: insufficient pre‑period data.")
                else:
                    results[method1_key] = res1

            all_non_test = sorted([r for r in agg_df_val["region"].unique() if r not in test_regions_val])
            if len(all_non_test) < 2:
                st.warning("Not enough non‑test regions for Data-Optimised Controls. Method 2 skipped.")
            else:
                with st.spinner("Running Data-Optimised Controls..."):
                    res2 = run_validation_method(
                        agg_df_val, all_non_test, test_regions_val, "lasso",
                        pre_start, pre_end, test_start, test_end,
                        use_post, post_start, post_end,
                        compute_uplift=compute_uplift,
                        placebo_length_periods=placebo_length_periods,
                        min_training_periods=min_training_periods,
                        include_lagged_controls=st.session_state.get("include_lagged_controls", False),
                        frequency_config=freq_config
                    )
                    if res2 is not None:
                        results[METHOD_DATA_OPTIMISED] = res2

            force_excluded_in_agg = [r for r in force_excluded_regions if r in agg_df_val["region"].unique()]
            if force_excluded_regions and force_excluded_in_agg:
                candidate_controls = [r for r in all_non_test if r not in force_excluded_in_agg]
                if len(candidate_controls) < 2:
                    st.warning("Not enough non‑test regions after Excluding Force-Exclude Regions. Method 3 skipped.")
                else:
                    with st.spinner("Running Data-Optimised Controls (Excluding Force-Exclude Regions)..."):
                        res3 = run_validation_method(
                            agg_df_val, candidate_controls, test_regions_val, "lasso",
                            pre_start, pre_end, test_start, test_end,
                            use_post, post_start, post_end,
                            compute_uplift=compute_uplift,
                            placebo_length_periods=placebo_length_periods,
                            min_training_periods=min_training_periods,
                            include_lagged_controls=st.session_state.get("include_lagged_controls", False),
                            frequency_config=freq_config
                        )
                        if res3 is not None:
                            results[METHOD_DATA_OPTIMISED_EXCL] = res3
            elif force_excluded_regions and not force_excluded_in_agg:
                st.warning("Force‑excluded regions were defined but none appear in the aggregated dataset. Check region names. Skipping Method 3.")
            else:
                st.info("No force‑excluded regions were defined, so Method 3 (excluding them) was not run.")

            st.session_state.validation_results = {
                "results": results,
                "agg_df": agg_df_val,
                "test_regions": test_regions_val,
                "control_regions": control_regions_val,
                "force_excluded": force_excluded_regions,
                "mode": mode,
                "pre_start": pre_start,
                "pre_end": pre_end,
                "test_start": test_start,
                "test_end": test_end,
                "use_post": use_post,
                "post_start": post_start,
                "post_end": post_end,
                "selected_metric": selected_metric,
                "placebo_length_periods": placebo_length_periods,
                "placebo_length_weeks": placebo_length_periods,  # backward-compatible alias
                "min_training_periods": min_training_periods,
                "min_training_weeks": min_training_periods,  # backward-compatible alias
                "include_lagged_controls": include_lagged_controls,
                "time_series_frequency": time_series_frequency,
                "frequency_config": freq_config,
            }

            st.session_state.validation_triggered = False

    # -------------------------------------------------------------------------
    # Display results if they exist — IDENTICAL to working file
    # -------------------------------------------------------------------------
    if st.session_state.validation_results is not None:
        vres = st.session_state.validation_results
        # Only show results if they match the current mode
        if vres.get("mode") != mode:
            st.info("Results from a previous run are shown. Re-run to update for the current mode.")
            return
        results = vres["results"]
        agg_df_val = vres["agg_df"]
        test_regions_val = vres["test_regions"]
        control_regions_val = vres["control_regions"]
        force_excluded_regions = vres["force_excluded"]
        pre_start = vres["pre_start"]
        pre_end = vres["pre_end"]
        test_start = vres["test_start"]
        test_end = vres["test_end"]
        use_post = vres["use_post"]
        post_start = vres["post_start"]
        post_end = vres["post_end"]
        selected_metric = vres["selected_metric"]
        placebo_length_periods = vres.get("placebo_length_periods", vres.get("placebo_length_weeks"))
        min_training_periods = vres.get("min_training_periods", vres.get("min_training_weeks", 13))
        include_lagged_controls_val = vres.get("include_lagged_controls", False)
        vres_time_series_frequency = vres.get("time_series_frequency", "weekly")
        vres_freq_config = vres.get("frequency_config") or get_frequency_config(vres_time_series_frequency)
        all_non_test = sorted([r for r in agg_df_val["region"].unique() if r not in test_regions_val])

        if include_lagged_controls_val:
            _same_period_word = "day" if vres_freq_config["frequency"] == "daily" else "week"
            st.caption(
                f"⏱️ {vres_freq_config['lag_label']} lagged controls are **enabled** — models were fit on "
                f"same-{_same_period_word} and lagged control features."
            )

        # ---- Display per‑method results ----
        for method_name, res in results.items():
            st.markdown(f"#### {method_name}")
            if method_name == METHOD_STRUCTURAL:
                st.caption(
                    "Structurally Matched Controls uses the GeoMatch-selected control pool, then fits an "
                    "Elastic Net model to estimate the counterfactual. Some structurally selected controls "
                    "may be shrunk to zero by the model."
                )

            with st.expander("Control Selection Details", expanded=False):
                if method_name in [METHOD_STRUCTURAL, METHOD_USER_SELECTED]:
                    st.write(f"**Candidate controls:** {res['n_candidates']}")
                    st.write(f"**Selected controls:** {res['n_selected']}")
                    st.write(f"**Removed controls:** {res['n_removed']}")
                    if res.get('include_lagged_controls'):
                        st.caption(f"Model features used ({len(res.get('model_feature_cols', []))}): includes same-period and lagged control terms.")
                    if not res['selected_df'].empty:
                        st.dataframe(res['selected_df'], width='stretch')
                    else:
                        st.write("**Control regions:**", ", ".join(control_regions_val))
                else:
                    st.write(f"**Candidate controls:** {res['n_candidates']}")
                    st.write(f"**Selected controls:** {res['n_selected']}")
                    st.write(f"**Removed controls:** {res['n_removed']}")
                    if res.get('include_lagged_controls'):
                        st.caption(f"Model features used ({len(res.get('model_feature_cols', []))}): includes same-period and lagged control terms.")
                    if res['n_selected'] > 0:
                        st.dataframe(res['selected_df'])
                    else:
                        st.warning("LASSO selected zero controls.")
                    st.caption(f"Model regularisation strength (alpha): {res['alpha']:.6f}")

            _lag_drop_meta = res.get("lag_drop_metadata")
            if res.get("include_lagged_controls") and res.get("time_series_frequency") == "daily" and _lag_drop_meta:
                if _lag_drop_meta.get("lag_drop_pct", 0) > 20:
                    st.warning(
                        f"⚠️ Daily 7-day lagged controls require matching dates exactly 7 calendar days earlier. "
                        f"{_lag_drop_meta['rows_dropped_due_to_lag']} of {_lag_drop_meta['rows_before_lag_drop']} rows "
                        f"({_lag_drop_meta['lag_drop_pct']:.1f}%) were dropped because those lag dates were missing. "
                        f"Check whether your daily data has gaps."
                    )

            # ---- High validation error, even when the overfitting gap is small (item 13) ----
            _rolling_smape_mean = res.get("rolling_smape_mean", res.get("holdout_smape_mean", np.nan))
            if _rolling_smape_mean is not None and not (isinstance(_rolling_smape_mean, float) and np.isnan(_rolling_smape_mean)):
                if _rolling_smape_mean > 30:
                    st.error(
                        f"High validation error: rolling-origin sMAPE is {_rolling_smape_mean:.1f}%. "
                        "Even if the Overfitting Gap is small, the model is not predicting the test group accurately enough to support a reliable uplift estimate."
                    )
                elif _rolling_smape_mean > 20:
                    st.warning(
                        f"Elevated validation error: rolling-origin sMAPE is {_rolling_smape_mean:.1f}%. "
                        "Review the fit chart, residual diagnostics, and placebo results before relying on this method."
                    )

            col1, col2, col3 = st.columns(3)
            col1.metric(
                "Pre-Period Correlation",
                f"{res['corr']:.3f}",
                help="How closely the counterfactual fits the actual test KPI during the pre‑period. This is an in‑sample measure."
            )
            col2.metric(
                "Pre-Period R²",
                f"{res['r2']:.3f}",
                help="Proportion of variation in the test KPI explained by the controls. This is an in‑sample measure."
            )
            col3.metric(
                "Pre-Period sMAPE",
                f"{res['smape']:.1f}%",
                help="Average percentage error in pre‑period predictions (in‑sample). Lower is better."
            )

            if compute_uplift and test_start is not None and test_end is not None and res['uplift'] is not None:
                st.metric(
                    "Observed Uplift",
                    f"{res['uplift']:.0f} ({res['uplift_pct']:.1f}%)",
                    help="Absolute uplift = Actual sum − Predicted baseline sum. Percentage = uplift / baseline."
                )

            with st.expander("Rolling Cross-Validation", expanded=False):
                _res_freq_config = res.get("frequency_config") or vres_freq_config
                _period_word = _res_freq_config["period_label_singular"]
                _period_word_plural = _res_freq_config["period_label_plural"]
                _vw = res.get("validation_window_periods", res.get("validation_window_weeks", placebo_length_periods))
                _mt = res.get("min_training_periods", res.get("min_training_weeks", min_training_periods))
                st.caption(
                    f"Rolling-origin validation used a **{_vw}-{_period_word}** forecast horizon "
                    f"and required at least **{_mt} {_period_word_plural}** of training history before each validation window."
                )
                _rcv_col1 = st.columns(1)[0]
                _rcv_col1.metric(
                    "Average Out-of-Sample sMAPE",
                    f"{res['holdout_smape_mean']:.1f}%" if not np.isnan(res['holdout_smape_mean']) else "-",
                    help="Average sMAPE across all rolling-origin validation windows. Out-of-sample — more reliable than pre-period fit."
                )
                _fold_df = res.get("rolling_origin_folds", pd.DataFrame())
                if not _fold_df.empty:
                    _display_cols = [
                        "fold_number", "training_periods", "forecast_horizon_periods",
                        "test_start_date", "test_end_date",
                        "smape", "rmse", "bias_pct", "uplift_error_pct"
                    ]
                    _display_cols = [c for c in _display_cols if c in _fold_df.columns]
                    _fold_display = _fold_df[_display_cols].copy()
                    # Format dates as DD/MM/YYYY
                    for _date_col in ["test_start_date", "test_end_date"]:
                        if _date_col in _fold_display.columns:
                            _fold_display[_date_col] = pd.to_datetime(
                                _fold_display[_date_col]
                            ).dt.strftime("%d/%m/%Y")
                    # Format percentage columns
                    for _pct_col in ["smape", "bias_pct", "uplift_error_pct"]:
                        if _pct_col in _fold_display.columns:
                            _fold_display[_pct_col] = _fold_display[_pct_col].apply(
                                lambda v: f"{v:.1f}%" if pd.notna(v) else "-"
                            )
                    if "rmse" in _fold_display.columns:
                        _fold_display["rmse"] = _fold_display["rmse"].apply(
                            lambda v: f"{v:.0f}" if pd.notna(v) else "-"
                        )
                    # Rename to human-readable labels
                    _training_periods_label = f"Training {_period_word_plural.capitalize()}"
                    _horizon_label = "Horizon (Days)" if _res_freq_config["frequency"] == "daily" else "Horizon (Wks)"
                    _fold_display.rename(columns={
                        "fold_number": "Fold",
                        "training_periods": _training_periods_label,
                        "forecast_horizon_periods": _horizon_label,
                        "test_start_date": "Forecast Start",
                        "test_end_date": "Forecast End",
                        "smape": "sMAPE",
                        "rmse": "RMSE",
                        "bias_pct": "Bias %",
                        "uplift_error_pct": "Uplift Error %",
                    }, inplace=True)
                    st.dataframe(_fold_display, width='stretch', hide_index=True)
                else:
                    st.info("No rolling-origin folds were generated — the historical period may be too short for the selected training and window settings.")


            plot_type = st.radio(
                "Display plot:",
                ["Actual", "Indexed (pre‑period avg = 100)"],
                horizontal=True,
                key=f"plot_toggle_{mode_prefix}_{method_name}"
            )

            all_dates = res['dates_pre'] + (res['dates_test'] if res['dates_test'] else []) + (res['dates_post'] if res['dates_post'] else [])
            all_actual = list(res['y_pre']) + (list(res['y_test_actual']) if res['y_test_actual'] is not None else []) + (list(res['y_post_actual']) if res['y_post_actual'] is not None else [])
            all_pred = list(res['y_pred_pre']) + (list(res['y_pred_test']) if res['y_pred_test'] is not None else []) + (list(res['y_post_pred']) if res['y_post_pred'] is not None else [])

            if plot_type == "Actual":
                y_actual = all_actual
                y_pred = all_pred
                y_label = selected_metric
                title_suffix = "Actual"
            else:
                pre_mean = np.mean(res['y_pre'])
                if pre_mean > 0:
                    y_actual = np.array(all_actual) / pre_mean * 100
                    y_pred = np.array(all_pred) / pre_mean * 100
                    y_label = f"{selected_metric} (Indexed)"
                    title_suffix = "Indexed (pre‑period avg=100)"
                else:
                    st.warning("Pre‑period average zero, cannot index. Showing Actual.")
                    y_actual = all_actual
                    y_pred = all_pred
                    y_label = selected_metric
                    title_suffix = "Actual"

            plot_df = pd.DataFrame({
                "Date": all_dates,
                "Actual": y_actual,
                "Predicted / Counterfactual": y_pred
            }).melt(id_vars="Date", var_name="Series", value_name="Value")

            fig = px.line(
                plot_df,
                x="Date",
                y="Value",
                color="Series",
                title=f"{title_suffix} – {method_name}",
                labels={"Value": y_label, "Date": "Date"}
            )

            def add_vline_with_annotation(fig, x_val, color, label, position="top left"):
                if x_val is None:
                    return
                fig.add_vline(
                    x=x_val,
                    line_dash="dash",
                    line_color=color,
                    annotation_text=label,
                    annotation_position=position
                )

            if compute_uplift and test_start is not None:
                add_vline_with_annotation(fig, test_start, "red", "Test start", position="top left")
                add_vline_with_annotation(fig, test_end, "orange", "Test end", position="top right")

            fig.update_layout(yaxis_title=y_label)
            st.plotly_chart(fig, width='stretch')

        # ---- Method Comparison table (traffic-light diagnostics), captions, and
        # interpretation help — rendered by a standalone, independently testable function. ----
        render_method_comparison_table(results, mode, test_start, control_regions_val)


with tab2:
    st.subheader("🔍 Validate Test Design")
    st.caption("Validate whether your selected control regions can reliably predict the test regions before running a live geo-test.")
    render_time_series_validation("Design")

with tab3:
    st.subheader("📊 Measure Test Impact")
    st.caption("Estimate the uplift from your completed geo test and compare results against expected historical variation.")
    render_time_series_validation("Evaluate")

# =============================================================================
# TAB 4: BAYESIAN TIME-BASED REGRESSION
# =============================================================================
with tab4:
    st.subheader("🧠 Bayesian Time-Based Regression (TBR)")
    st.caption("Run a Bayesian time-based regression on the results from the Measure Test Impact tab.")

    if st.session_state.get("bayesian_results") is None and (
        st.session_state.get("validation_results") is None
        or st.session_state.get("validation_results", {}).get("mode") != "Evaluate"
    ):
        st.info("Run an evaluation in the **Measure Test Impact** tab first. The Bayesian model uses those validation results.")
    else:
        # Retrieve validation state needed by Bayesian
        if st.session_state.get("validation_results") is not None and st.session_state.get("validation_results", {}).get("mode") == "Evaluate":
            vres = st.session_state.validation_results
            results = vres["results"]
            agg_df_bayes = vres["agg_df"]
            test_regions_val = vres["test_regions"]
            control_regions_val = vres["control_regions"]
            pre_start = vres["pre_start"]
            pre_end = vres["pre_end"]
            test_start = vres["test_start"]
            test_end = vres["test_end"]
            use_post = vres["use_post"]
            post_start = vres["post_start"]
            post_end = vres["post_end"]
            selected_metric = vres["selected_metric"]
            bayes_time_series_frequency = vres.get("time_series_frequency", "weekly")
            bayes_freq_config = vres.get("frequency_config") or get_frequency_config(bayes_time_series_frequency)
            all_non_test = sorted([r for r in agg_df_bayes["region"].unique() if r not in test_regions_val])

            available_methods = list(results.keys())
            if available_methods:
                selected_bayes_method = st.selectbox(
                    "Select method for Bayesian TBR evaluation",
                    available_methods,
                    help="The selected method's control list will be used for Bayesian uplift estimation.",
                    key="bayes_method_select"
                )
                if selected_bayes_method:
                    res = results.get(selected_bayes_method)
                    if selected_bayes_method in (METHOD_STRUCTURAL, METHOD_USER_SELECTED):
                        # User Selected Test and Control (and the structural pool itself)
                        # uses the user-selected controls directly, but must not be empty.
                        bayes_control_list = control_regions_val
                        if not bayes_control_list:
                            st.warning(
                                "No control regions are selected, so Bayesian TBR cannot be run. "
                                "Choose another method or select at least one control region."
                            )
                            st.stop()
                    else:
                        # Data-Optimised / LASSO / Elastic Net methods: only use the
                        # model-selected base control regions. Do NOT fall back to the
                        # full candidate control pool if the model selected zero controls —
                        # that would silently change what Bayesian TBR is actually testing.
                        bayes_control_list = res.get("selected_regions", []) if res else []
                        if not bayes_control_list:
                            st.warning(
                                "The selected method did not retain any controls, so Bayesian TBR cannot be run for this method. "
                                "Choose another method, adjust exclusions, increase the control pool, or disable overly "
                                "restrictive validation settings."
                            )
                            st.stop()

                    bayes_base_control_list = list(bayes_control_list)
                    # Bayesian TBR always uses the same frequency and lag setup as the selected
                    # validation method (from validation_results), not an independently chosen one —
                    # this keeps the lag length (1-week vs 7-day) consistent with how the underlying
                    # validation run was configured.
                    bayes_include_lag = vres.get("include_lagged_controls", False)
                    bayes_lag_periods = bayes_freq_config["lag_periods"]
                    # Bayesian TBR always uses every base control (with coefficient shrinkage via
                    # priors, not hard LASSO/Elastic-Net selection), so the expected feature set is
                    # simply the base controls plus their lagged terms when lagging is enabled —
                    # not the upstream validation method's own selected_features.
                    bayes_feature_preview = (
                        bayes_base_control_list + [f"{c}_lag{bayes_lag_periods}" for c in bayes_base_control_list]
                        if bayes_include_lag else list(bayes_base_control_list)
                    )

                    st.caption(f"⏱️ {bayes_freq_config['lag_label']} lagged controls: {'**enabled**' if bayes_include_lag else '**disabled**'} (using the {bayes_time_series_frequency} frequency and lag setup from the Measure Test Impact validation run)")
                    with st.expander("Controls used by Bayesian TBR", expanded=False):
                        st.write(f"**Base control regions ({len(bayes_base_control_list)}):**")
                        st.write(", ".join(bayes_base_control_list) if bayes_base_control_list else "_None_")
                        if bayes_include_lag:
                            st.write(f"**Number of model features:** {len(bayes_feature_preview)}")
                            st.write("**Model feature terms (including lagged terms):**")
                            st.write(", ".join(bayes_feature_preview) if bayes_feature_preview else "_None_")

                    # ---- Structural prior settings ----
                    use_structural_priors = st.checkbox(
                        "Use structurally informed coefficient priors",
                        value=False,
                        key="use_structural_priors",
                        help=(
                            "Controls how the Bayesian model's coefficient priors are set.\n\n"
                            "OFF (default): every control gets a fixed prior of Normal(0, σ=0.50). "
                            "All controls are treated equally.\n\n"
                            "ON: each control is scored by its structural distance to the "
                            "population-weighted test-group profile, using the same features and "
                            "weights as GeoMatch matching. Sigma bounds are then derived from the "
                            "median pre-period correlation between controls and the test KPI, so "
                            "the scale reflects how predictive your controls actually are.\n\n"
                            "Better structural match → wider sigma (more flexibility).\n"
                            "Weaker structural match → narrower sigma (shrunk toward zero).\n\n"
                            "The prior mean stays zero regardless. Only the width changes."
                        ),
                    )

                    _bayes_freq_blocked = st.session_state.get("frequency_mismatch_blocked", False)
                    if _bayes_freq_blocked:
                        st.info("Bayesian TBR is disabled until the frequency mismatch warning above (in the validation setup) is acknowledged or resolved.")
                    if st.button("Run Bayesian Time-Based Regression (TBR)", width='stretch', type="primary", key="run_bayes_tab4", disabled=_bayes_freq_blocked):
                        with st.spinner(f"Running Bayesian TBR using {selected_bayes_method}..."):
                            pre_start_ts = pd.Timestamp(pre_start)
                            pre_end_ts = pd.Timestamp(pre_end)
                            test_start_ts = pd.Timestamp(test_start) if test_start is not None else None
                            test_end_ts = pd.Timestamp(test_end) if test_end is not None else None
                            post_start_ts = pd.Timestamp(post_start) if use_post and post_start is not None else None
                            post_end_ts = pd.Timestamp(post_end) if use_post and post_end is not None else None

                            post_dates = None
                            y_post_actual = None
                            y_pred_post_mean = None
                            post_lower_pi = None
                            post_upper_pi = None
                            X_post_scaled = None

                            # ---- Build a combined pre + test/post model matrix so frequency-aware lagged
                            # control features (if enabled) apply once across the full continuous
                            # date range, then split back out by period. ----
                            _combined_end_candidates = [pre_end_ts]
                            if test_end_ts is not None:
                                _combined_end_candidates.append(test_end_ts)
                            if use_post and post_end_ts is not None:
                                _combined_end_candidates.append(post_end_ts)
                            combined_end_ts = max(_combined_end_candidates)

                            full_mask = (agg_df_bayes["date"] >= pre_start_ts) & (agg_df_bayes["date"] <= combined_end_ts)
                            model_full_bayes, bayes_matrix_diagnostics = build_model_matrix(agg_df_bayes[full_mask], bayes_control_list, test_regions_val)

                            _bayes_pct_dropped = bayes_matrix_diagnostics.get("pct_rows_dropped", 0.0)
                            _bayes_rows_dropped = bayes_matrix_diagnostics.get("rows_dropped", 0)
                            _bayes_rows_before = bayes_matrix_diagnostics.get("rows_before_dropna", 0)
                            if _bayes_rows_dropped > 0 and _bayes_pct_dropped > 20:
                                st.error(
                                    f"{_bayes_rows_dropped} of {_bayes_rows_before} rows ({_bayes_pct_dropped:.1f}%) were removed because "
                                    "the test series or at least one selected control had missing KPI values. "
                                    "This is a large share of the data and the Bayesian TBR result may be unreliable. "
                                    f"Controls with missing values: {', '.join(bayes_matrix_diagnostics.get('control_columns_with_missing', [])) or 'none'}."
                                )
                            elif _bayes_rows_dropped > 0 and _bayes_pct_dropped > 10:
                                st.warning(
                                    f"{_bayes_rows_dropped} of {_bayes_rows_before} rows ({_bayes_pct_dropped:.1f}%) were removed because "
                                    "the test series or at least one selected control had missing KPI values. "
                                    "This can affect Bayesian TBR reliability. "
                                    f"Controls with missing values: {', '.join(bayes_matrix_diagnostics.get('control_columns_with_missing', [])) or 'none'}."
                                )

                            if bayes_include_lag:
                                model_full_bayes, bayes_model_feature_cols, bayes_lagged_feature_map, bayes_lag_drop_metadata = add_lagged_control_features(
                                    model_full_bayes, bayes_control_list, lags=(bayes_lag_periods,), frequency_config=bayes_freq_config
                                )
                            else:
                                bayes_model_feature_cols = list(bayes_control_list)
                                bayes_lagged_feature_map = {}
                                bayes_lag_drop_metadata = None

                            pre_mask = (model_full_bayes["date"] >= pre_start_ts) & (model_full_bayes["date"] <= pre_end_ts)
                            model_pre = model_full_bayes[pre_mask].sort_values("date").reset_index(drop=True)
                            if len(model_pre) < 6:
                                st.error("Not enough pre‑period data for Bayesian model.")
                            else:
                                X_pre = model_pre[bayes_model_feature_cols].values
                                y_pre = model_pre["test_kpi"].values
                                pre_dates = model_pre["date"].values
                                scaler_X = StandardScaler()
                                X_pre_scaled = scaler_X.fit_transform(X_pre)
                                scaler_y = StandardScaler()
                                y_pre_scaled = scaler_y.fit_transform(y_pre.reshape(-1,1)).flatten()

                                test_mask = (model_full_bayes["date"] >= test_start_ts) & (model_full_bayes["date"] <= test_end_ts)
                                model_test = model_full_bayes[test_mask].sort_values("date").reset_index(drop=True)
                                if model_test.empty:
                                    st.error("No test period data available.")
                                else:
                                    X_test = model_test[bayes_model_feature_cols].values
                                    X_test_scaled = scaler_X.transform(X_test)
                                    y_test_actual = model_test["test_kpi"].values
                                    test_dates = model_test["date"].values

                                    if use_post and post_start_ts is not None and post_end_ts is not None:
                                        post_mask = (model_full_bayes["date"] >= post_start_ts) & (model_full_bayes["date"] <= post_end_ts)
                                        model_post = model_full_bayes[post_mask].sort_values("date").reset_index(drop=True)
                                        if not model_post.empty:
                                            X_post = model_post[bayes_model_feature_cols].values
                                            X_post_scaled = scaler_X.transform(X_post)
                                            y_post_actual = model_post["test_kpi"].values
                                            post_dates = model_post["date"].values
                                        else:
                                            X_post_scaled = None
                                            y_post_actual = None
                                            post_dates = None
                                    else:
                                        X_post_scaled = None
                                        y_post_actual = None
                                        post_dates = None

                                    # ---- Compute coefficient prior sigmas ----
                                    _use_structural = st.session_state.get("use_structural_priors", False)
                                    if _use_structural:
                                        # Data-driven sigma bounds from pre-period KPI correlations
                                        # corr[i] = how well control i tracks the test KPI historically
                                        try:
                                            pre_corrs = np.array([
                                                np.corrcoef(X_pre[:, i], y_pre)[0, 1]
                                                if np.std(X_pre[:, i]) > 0 else 0.0
                                                for i in range(X_pre.shape[1])
                                            ])
                                            pre_corrs = np.nan_to_num(pre_corrs, nan=0.0)
                                            abs_corrs = np.abs(pre_corrs)
                                            # Median absolute correlation anchors the midpoint
                                            median_corr = float(np.median(abs_corrs))
                                            median_corr = np.clip(median_corr, 0.1, 0.95)
                                            # Bounds scale with the data: better-tracking controls
                                            # → higher sigma ceiling; weaker → tighter floor
                                            _min_sigma = round(float(np.clip(median_corr * 0.4, 0.10, 0.40)), 3)
                                            _max_sigma = round(float(np.clip(median_corr * 1.2, 0.30, 0.90)), 3)
                                        except Exception:
                                            _min_sigma, _max_sigma = 0.25, 0.70

                                        prior_sigmas_base, structural_prior_df_base = calculate_structural_prior_sigmas(
                                            agg_df=agg_df,
                                            test_regions=test_regions_val,
                                            control_regions=bayes_control_list,
                                            geo_col=geo_col,
                                            feature_cols=active_features,
                                            weight_dict=st.session_state.get("current_weights", None),
                                            population_col=POPULATION_COL,
                                            min_sigma=_min_sigma,
                                            max_sigma=_max_sigma,
                                        )
                                        if bayes_include_lag:
                                            # Duplicate/map each base region's structural prior sigma to its
                                            # lagged term as well, since we don't implement a separate lag prior.
                                            _sigma_map = dict(zip(bayes_control_list, prior_sigmas_base))
                                            prior_sigmas = np.array([_sigma_map[c] for c in bayes_control_list] +
                                                                     [_sigma_map[c] for c in bayes_control_list])
                                            _same_period_label = "Same day" if bayes_freq_config["frequency"] == "daily" else "Same week"
                                            _lag_term_label = f"Lag {bayes_lag_periods} " + (
                                                bayes_freq_config["period_label_singular"] if bayes_lag_periods == 1
                                                else bayes_freq_config["period_label_plural"]
                                            )
                                            _base_df = structural_prior_df_base.copy()
                                            _base_df.insert(0, "Feature", _base_df["Control Region"])
                                            _base_df.insert(2, "Term Type", _same_period_label)
                                            _lag_df = structural_prior_df_base.copy()
                                            _lag_df["Feature"] = _lag_df["Control Region"].apply(lambda c: f"{c}_lag{bayes_lag_periods}")
                                            _lag_df.insert(2, "Term Type", _lag_term_label)
                                            _lag_df = _lag_df[["Feature", "Control Region", "Term Type", "Structural Distance", "Structural Similarity", "Prior Sigma", "Prior Type"]]
                                            _base_df = _base_df[["Feature", "Control Region", "Term Type", "Structural Distance", "Structural Similarity", "Prior Sigma", "Prior Type"]]
                                            structural_prior_df = pd.concat([_base_df, _lag_df], ignore_index=True)
                                        else:
                                            prior_sigmas = prior_sigmas_base
                                            structural_prior_df = structural_prior_df_base
                                    else:
                                        prior_sigmas = np.repeat(0.5, len(bayes_model_feature_cols))
                                        if bayes_include_lag:
                                            _same_period_label = "Same day" if bayes_freq_config["frequency"] == "daily" else "Same week"
                                            _lag_term_label = f"Lag {bayes_lag_periods} " + (
                                                bayes_freq_config["period_label_singular"] if bayes_lag_periods == 1
                                                else bayes_freq_config["period_label_plural"]
                                            )
                                            _feature_rows = []
                                            for c in bayes_control_list:
                                                _feature_rows.append({"Feature": c, "Control Region": c, "Term Type": _same_period_label,
                                                                       "Structural Distance": np.nan, "Structural Similarity": np.nan,
                                                                       "Prior Sigma": 0.5, "Prior Type": "Standard weak prior"})
                                            for c in bayes_control_list:
                                                _feature_rows.append({"Feature": f"{c}_lag{bayes_lag_periods}", "Control Region": c, "Term Type": _lag_term_label,
                                                                       "Structural Distance": np.nan, "Structural Similarity": np.nan,
                                                                       "Prior Sigma": 0.5, "Prior Type": "Standard weak prior"})
                                            structural_prior_df = pd.DataFrame(_feature_rows)
                                        else:
                                            structural_prior_df = pd.DataFrame({
                                                "Control Region": bayes_control_list,
                                                "Structural Distance": np.nan,
                                                "Structural Similarity": np.nan,
                                                "Prior Sigma": prior_sigmas,
                                                "Prior Type": "Standard weak prior",
                                            })

                                    try:
                                        import pymc as pm
                                        import arviz as az
                                        import pytensor
                                    except ImportError as _e:
                                        st.error(
                                            f"**PyMC could not be imported:** {_e}"
                                        )
                                        st.stop()

                                    with pm.Model() as bmodel:
                                        intercept = pm.Normal("intercept", mu=0, sigma=1)
                                        coeffs = pm.Normal(
                                            "coeffs",
                                            mu=0,
                                            sigma=prior_sigmas,
                                            shape=X_pre_scaled.shape[1],
                                        )
                                        sigma = pm.HalfNormal("sigma", sigma=1)
                                        mu = intercept + pm.math.dot(X_pre_scaled, coeffs)
                                        pm.Normal("y_obs", mu=mu, sigma=sigma, observed=y_pre_scaled)
                                        _mcmc_n_draws = 2000
                                        _mcmc_n_tune = 1000
                                        _mcmc_n_chains = 4
                                        _mcmc_target_accept = 0.95
                                        trace = pm.sample(
                                            draws=_mcmc_n_draws, tune=_mcmc_n_tune, chains=_mcmc_n_chains,
                                            target_accept=_mcmc_target_accept, progressbar=False, random_seed=42
                                        )
                                        # Divergent transitions are often the single most informative NUTS
                                        # diagnostic — unlike R-hat/ESS/MCSE (which mostly flag noise), a
                                        # divergence flags a region of the posterior the sampler failed to
                                        # explore, which can bias point estimates rather than just add noise.
                                        # target_accept=0.95 above is set high specifically to suppress these,
                                        # but that doesn't guarantee zero, so we count and surface them.
                                        _mcmc_n_divergences = int(trace.sample_stats["diverging"].sum())

                                    post_int = trace.posterior["intercept"].values.flatten()
                                    post_coeff = trace.posterior["coeffs"].values.reshape(-1, X_pre_scaled.shape[1])
                                    post_sigma = trace.posterior["sigma"].values.flatten()

                                    # ---- Enrich structural prior df with posterior coefficients ----
                                    posterior_coeff_means = post_coeff.mean(axis=0)
                                    structural_prior_df["Posterior Coefficient Mean"] = np.round(posterior_coeff_means, 3)
                                    structural_prior_df["Posterior Coefficient 3%"] = np.round(
                                        np.percentile(post_coeff, 3, axis=0), 3
                                    )
                                    structural_prior_df["Posterior Coefficient 97%"] = np.round(
                                        np.percentile(post_coeff, 97, axis=0), 3
                                    )

                                    # ---- Posterior fitted-mean samples (no observation noise) ----
                                    # Used for the "Counterfactual (mean)" line everywhere, and for the
                                    # pre-period 94% HDI / credible interval band.
                                    mu_pre_samples = post_int[:, None] + np.dot(post_coeff, X_pre_scaled.T)
                                    mu_pre_original = scaler_y.inverse_transform(mu_pre_samples.T).T
                                    y_pred_pre_mean = mu_pre_original.mean(axis=0)
                                    # 94% HDI / credible interval around the fitted counterfactual mean —
                                    # deliberately excludes observation-level noise.
                                    pre_lower_mean_hdi = np.percentile(mu_pre_original, 3, axis=0)
                                    pre_upper_mean_hdi = np.percentile(mu_pre_original, 97, axis=0)

                                    mu_test_samples = post_int[:, None] + np.dot(post_coeff, X_test_scaled.T)
                                    mu_test_original = scaler_y.inverse_transform(mu_test_samples.T).T
                                    y_pred_test_mean = mu_test_original.mean(axis=0)

                                    # ---- Posterior predictive samples (with observation noise) ----
                                    # Used for the test/post 94% predictive interval band — the plausible
                                    # range of actual counterfactual *observations*, not just the mean.
                                    noise_test = np.random.normal(0, post_sigma[:, None], size=mu_test_samples.shape)
                                    y_pred_test_samples = mu_test_samples + noise_test
                                    y_pred_test_predictive_original = scaler_y.inverse_transform(y_pred_test_samples.T).T
                                    test_lower_pi = np.percentile(y_pred_test_predictive_original, 3, axis=0)
                                    test_upper_pi = np.percentile(y_pred_test_predictive_original, 97, axis=0)

                                    if X_post_scaled is not None:
                                        mu_post_samples = post_int[:, None] + np.dot(post_coeff, X_post_scaled.T)
                                        mu_post_original = scaler_y.inverse_transform(mu_post_samples.T).T
                                        y_pred_post_mean = mu_post_original.mean(axis=0)

                                        noise_post = np.random.normal(0, post_sigma[:, None], size=mu_post_samples.shape)
                                        y_pred_post_samples = mu_post_samples + noise_post
                                        y_pred_post_predictive_original = scaler_y.inverse_transform(y_pred_post_samples.T).T
                                        post_lower_pi = np.percentile(y_pred_post_predictive_original, 3, axis=0)
                                        post_upper_pi = np.percentile(y_pred_post_predictive_original, 97, axis=0)
                                    else:
                                        y_pred_post_mean = None
                                        post_lower_pi = None
                                        post_upper_pi = None

                                    # ---- Uplift intervals ----
                                    # Primary readout: 94% posterior predictive interval for uplift (includes
                                    # observation-level noise on the counterfactual test-period total).
                                    total_actual = y_test_actual.sum()
                                    total_pred_samples = y_pred_test_predictive_original.sum(axis=1)
                                    uplift_samples = total_actual - total_pred_samples
                                    uplift_pi_lower = np.percentile(uplift_samples, 3)
                                    uplift_pi_upper = np.percentile(uplift_samples, 97)
                                    prob_pos = (uplift_samples > 0).mean()
                                    mean_uplift = uplift_samples.mean()
                                    uplift_pct = (mean_uplift / total_pred_samples.mean()) * 100 if total_pred_samples.mean() != 0 else np.nan

                                    # Secondary readout: 94% credible interval / HDI for uplift, based on the
                                    # fitted counterfactual mean only (no observation noise). Narrower than the
                                    # predictive interval above — shows uncertainty in the *average* effect.
                                    total_pred_mean_samples = mu_test_original.sum(axis=1)
                                    uplift_mean_samples = total_actual - total_pred_mean_samples
                                    uplift_hdi_lower = np.percentile(uplift_mean_samples, 3)
                                    uplift_hdi_upper = np.percentile(uplift_mean_samples, 97)

                                    corr_b, r2_b, smape_b, rmse_b = compute_metrics(y_pre, y_pred_pre_mean)

                                    st.session_state.bayesian_results = {
                                        "pre_dates": pre_dates,
                                        "y_pre": y_pre,
                                        "y_pred_pre_mean": y_pred_pre_mean,
                                        "pre_lower_mean_hdi": pre_lower_mean_hdi,
                                        "pre_upper_mean_hdi": pre_upper_mean_hdi,
                                        "test_dates": test_dates,
                                        "y_test_actual": y_test_actual,
                                        "y_pred_test_mean": y_pred_test_mean,
                                        "test_lower_pi": test_lower_pi,
                                        "test_upper_pi": test_upper_pi,
                                        "post_dates": post_dates,
                                        "y_post_actual": y_post_actual,
                                        "y_pred_post_mean": y_pred_post_mean,
                                        "post_lower_pi": post_lower_pi,
                                        "post_upper_pi": post_upper_pi,
                                        "uplift_samples": uplift_samples,
                                        "uplift_pi_lower": uplift_pi_lower,
                                        "uplift_pi_upper": uplift_pi_upper,
                                        "uplift_hdi_lower": uplift_hdi_lower,
                                        "uplift_hdi_upper": uplift_hdi_upper,
                                        "prob_pos": prob_pos,
                                        "mean_uplift": mean_uplift,
                                        "uplift_pct": uplift_pct,
                                        "corr": corr_b,
                                        "r2": r2_b,
                                        "smape": smape_b,
                                        "rmse": rmse_b,
                                        "trace": trace,
                                        "n_divergences": _mcmc_n_divergences,
                                        "n_chains": _mcmc_n_chains,
                                        "n_draws": _mcmc_n_draws,
                                        "n_tune": _mcmc_n_tune,
                                        "target_accept": _mcmc_target_accept,
                                        "selected_metric": selected_metric,
                                        "test_start_ts": test_start_ts,
                                        "test_end_ts": test_end_ts,
                                        "prior_style": "Structurally informed" if _use_structural else "Standard weak prior",
                                        "prior_sigmas": prior_sigmas,
                                        "structural_prior_df": structural_prior_df,
                                        "min_prior_sigma": _min_sigma if _use_structural else 0.25,
                                        "max_prior_sigma": _max_sigma if _use_structural else 0.70,
                                        "control_list": bayes_control_list,
                                        "base_control_list": bayes_base_control_list,
                                        "include_lagged_controls": bayes_include_lag,
                                        "model_feature_cols": bayes_model_feature_cols,
                                        "lagged_feature_map": bayes_lagged_feature_map,
                                        "time_series_frequency": bayes_time_series_frequency,
                                        "frequency_config": bayes_freq_config,
                                        "lag_periods": bayes_lag_periods,
                                        "lag_label": bayes_freq_config["lag_label"],
                                        "lag_drop_metadata": bayes_lag_drop_metadata,
                                        "lag_drop_pct": bayes_lag_drop_metadata["lag_drop_pct"] if bayes_lag_drop_metadata else None,
                                        "rows_dropped_due_to_lag": bayes_lag_drop_metadata["rows_dropped_due_to_lag"] if bayes_lag_drop_metadata else None,
                                    }
                                    st.session_state.bayesian_interpretation_visible = True

        # ---- Bayesian results display — IDENTICAL to working file ----
        if st.session_state.bayesian_results is not None:
            bayes = st.session_state.bayesian_results

            # Row 1: Pre-period fit metrics
            col1, col2, col3 = st.columns(3)
            col1.metric("Pre-Period Correlation", f"{bayes['corr']:.3f}",
                help="How closely the Bayesian counterfactual fits the actual pre-period KPI.")
            col2.metric("Pre-Period R²", f"{bayes['r2']:.3f}",
                help="Proportion of variation in the test KPI explained by the controls (pre-period).")
            col3.metric("Pre-Period sMAPE", f"{bayes['smape']:.1f}%",
                help="Average percentage error of the Bayesian model in the pre-period.")

            # Row 2: Uplift results
            uplift_label = f"{bayes['mean_uplift']:.0f}"
            if not np.isnan(bayes['uplift_pct']):
                uplift_label += f"  ({bayes['uplift_pct']:.1f}%)"
            col5, col6, col7 = st.columns(3)
            col5.metric(
                "Estimated Incremental Uplift",
                uplift_label,
                help="Posterior mean incremental uplift during the test period, with percentage of predicted baseline."
            )
            col6.metric(
                "P(Uplift > 0)",
                f"{bayes['prob_pos']:.1%}",
                help="Probability that the intervention had a positive impact."
            )
            col7.metric(
                "94% Predictive Interval for Uplift",
                format_range(bayes['uplift_pi_lower'], bayes['uplift_pi_upper'], decimals=0),
                help="The interval within which the true uplift is expected to lie with 94% probability, including observation-level noise in the counterfactual. This is the primary readout for total impact."
            )

            # ---- Line chart ----
            # Pre-period: 94% HDI / credible interval around the fitted counterfactual mean (no observation noise).
            # Test/post period: 94% posterior predictive interval (includes observation-level noise) — the
            # plausible range of actual counterfactual observations under the no-test scenario.
            all_dates_b = list(bayes['pre_dates']) + list(bayes['test_dates'])
            all_actual_b = list(bayes['y_pre']) + list(bayes['y_test_actual'])
            all_pred_b = list(bayes['y_pred_pre_mean']) + list(bayes['y_pred_test_mean'])
            n_pre_pts = len(bayes['pre_dates'])
            n_test_pts = len(bayes['test_dates'])

            # Fitted-mean HDI band — populated for pre-period rows only, NaN elsewhere.
            all_mean_hdi_lower_b = list(bayes['pre_lower_mean_hdi']) + [np.nan] * n_test_pts
            all_mean_hdi_upper_b = list(bayes['pre_upper_mean_hdi']) + [np.nan] * n_test_pts
            # Predictive interval band — populated for test-period rows only, NaN elsewhere.
            all_pi_lower_b = [np.nan] * n_pre_pts + list(bayes['test_lower_pi'])
            all_pi_upper_b = [np.nan] * n_pre_pts + list(bayes['test_upper_pi'])
            interval_type_b = ["94% fitted mean interval (pre-period)"] * n_pre_pts + \
                               ["94% predictive interval (test/post)"] * n_test_pts

            if bayes['post_dates'] is not None:
                n_post_pts = len(bayes['post_dates'])
                all_dates_b += list(bayes['post_dates'])
                all_actual_b += list(bayes['y_post_actual'])
                all_pred_b += list(bayes['y_pred_post_mean'])
                all_mean_hdi_lower_b += [np.nan] * n_post_pts
                all_mean_hdi_upper_b += [np.nan] * n_post_pts
                all_pi_lower_b += list(bayes['post_lower_pi'])
                all_pi_upper_b += list(bayes['post_upper_pi'])
                interval_type_b += ["94% predictive interval (test/post)"] * n_post_pts

            plot_df = pd.DataFrame({
                "Date": all_dates_b,
                "Actual": all_actual_b,
                "Counterfactual (mean)": all_pred_b,
                "Lower 94% Fitted Mean Interval": all_mean_hdi_lower_b,
                "Upper 94% Fitted Mean Interval": all_mean_hdi_upper_b,
                "Lower 94% Predictive Interval": all_pi_lower_b,
                "Upper 94% Predictive Interval": all_pi_upper_b,
                "Interval Type": interval_type_b,
            })

            bayes_plot_type = st.radio(
                "Display plot:",
                ["Actual", "Indexed (pre‑period avg = 100)"],
                horizontal=True,
                key="bayes_plot_toggle"
            )

            interval_cols = [
                "Actual", "Counterfactual (mean)",
                "Lower 94% Fitted Mean Interval", "Upper 94% Fitted Mean Interval",
                "Lower 94% Predictive Interval", "Upper 94% Predictive Interval",
            ]

            if bayes_plot_type == "Indexed (pre‑period avg = 100)":
                pre_mean_b = np.mean(bayes['y_pre'])
                if pre_mean_b > 0:
                    for col in interval_cols:
                        plot_df[col] = plot_df[col] / pre_mean_b * 100
                    y_label = f"{bayes['selected_metric']} (Indexed)"
                    title_suffix = "Indexed"
                else:
                    y_label = bayes['selected_metric']
                    title_suffix = "Actual"
            else:
                y_label = bayes['selected_metric']
                title_suffix = "Actual"

            fig_line = px.line(
                plot_df,
                x="Date",
                y=["Actual", "Counterfactual (mean)"],
                labels={"value": y_label, "Date": "Date"},
                title=f"Bayesian TBR: {title_suffix}"
            )
            # Pre-period 94% fitted mean interval band
            fig_line.add_scatter(
                x=plot_df["Date"],
                y=plot_df["Upper 94% Fitted Mean Interval"],
                mode='lines',
                line=dict(width=0),
                showlegend=False,
                connectgaps=False
            )
            fig_line.add_scatter(
                x=plot_df["Date"],
                y=plot_df["Lower 94% Fitted Mean Interval"],
                mode='lines',
                line=dict(width=0),
                fill='tonexty',
                fillcolor='rgba(0,100,200,0.15)',
                showlegend=True,
                name='94% fitted mean interval (pre-period)',
                connectgaps=False
            )
            # Test/post-period 94% predictive interval band
            fig_line.add_scatter(
                x=plot_df["Date"],
                y=plot_df["Upper 94% Predictive Interval"],
                mode='lines',
                line=dict(width=0),
                showlegend=False,
                connectgaps=False
            )
            fig_line.add_scatter(
                x=plot_df["Date"],
                y=plot_df["Lower 94% Predictive Interval"],
                mode='lines',
                line=dict(width=0),
                fill='tonexty',
                fillcolor='rgba(0,150,80,0.2)',
                showlegend=True,
                name='94% predictive interval (test/post)',
                connectgaps=False
            )
            if bayes['test_start_ts'] is not None:
                fig_line.add_vline(x=bayes['test_start_ts'], line_dash="dash", line_color="red", annotation_text="Test start", annotation_position="top left")
            if bayes['test_end_ts'] is not None:
                fig_line.add_vline(x=bayes['test_end_ts'], line_dash="dash", line_color="orange", annotation_text="Test end", annotation_position="top right")
            fig_line.update_layout(yaxis_title=y_label)
            st.plotly_chart(fig_line, width='stretch')
            st.caption(
                "**Blue** (pre-period) = uncertainty in the average fitted relationship, no noise added. "
                "**Green** (test/post) = the plausible range of actual outcomes if there had been no test, "
                f"including normal period-to-period ({bayes.get('frequency_config', {}).get('period_label_singular', 'week')}-to-{bayes.get('frequency_config', {}).get('period_label_singular', 'week')}) noise. "
                "Compare actuals to the counterfactual line and green band "
                "to judge the test period — the uplift cards above are the main readout for total impact."
            )

            # ---- Posterior uplift distribution histogram ----
            fig_b = px.histogram(
                pd.DataFrame({"uplift": bayes['uplift_samples']}),
                x="uplift",
                nbins=50,
                title="Posterior Uplift Distribution"
            )
            fig_b.update_yaxes(title_text="Frequency")
            fig_b.update_xaxes(title_text="Incremental Uplift")
            fig_b.add_vline(x=0, line_dash="dash", line_color="red")
            fig_b.add_vline(x=bayes['uplift_pi_lower'], line_dash="dot", line_color="green", annotation_text="94% lower (predictive)", annotation_position="top")
            fig_b.add_vline(x=bayes['uplift_pi_upper'], line_dash="dot", line_color="green", annotation_text="94% upper (predictive)", annotation_position="top")
            fig_b.add_vline(x=bayes['mean_uplift'], line_dash="solid", line_color="blue", annotation_text=f"Mean = {bayes['mean_uplift']:.0f}", annotation_position="top")
            st.plotly_chart(fig_b, width='stretch')
            st.caption("The histogram shows the distribution of possible uplift values, drawn from the posterior predictive counterfactual totals. The blue line marks the mean estimate, red is zero (no effect), and the green dashed lines show the 94% predictive interval.")

            _bayes_lag_drop_meta = bayes.get("lag_drop_metadata")
            if bayes.get("include_lagged_controls") and bayes.get("time_series_frequency") == "daily" and _bayes_lag_drop_meta:
                if _bayes_lag_drop_meta.get("lag_drop_pct", 0) > 20:
                    st.warning(
                        f"⚠️ Daily 7-day lagged controls require matching dates exactly 7 calendar days earlier. "
                        f"{_bayes_lag_drop_meta['rows_dropped_due_to_lag']} of {_bayes_lag_drop_meta['rows_before_lag_drop']} rows "
                        f"({_bayes_lag_drop_meta['lag_drop_pct']:.1f}%) were dropped because those lag dates were missing. "
                        f"Check whether your daily data has gaps."
                    )

            # ---- Coefficient priors used ----
            with st.expander("Coefficient priors used in Bayesian TBR", expanded=False):
                _bayes_lag_label = bayes.get("lag_label", "1-week")
                st.write(f"**Prior style:** {bayes['prior_style']}")
                st.write(f"**{_bayes_lag_label} lagged controls:** {'Enabled' if bayes.get('include_lagged_controls') else 'Disabled'}")
                st.write(f"**Base control regions:** {', '.join(bayes.get('base_control_list', bayes.get('control_list', []))) or '_None_'}")
                st.write(f"**Number of model features:** {len(bayes.get('model_feature_cols', bayes.get('control_list', [])))}")
                if bayes.get('include_lagged_controls'):
                    st.write(f"**Model features used:** {', '.join(bayes.get('model_feature_cols', []))}")
                if bayes['prior_style'] == "Structurally informed":
                    st.caption(
                        f"**Sigma bounds (data-driven):** "
                        f"min = {bayes['min_prior_sigma']:.3f}, "
                        f"max = {bayes['max_prior_sigma']:.3f}. "
                        f"Bounds are derived from the median absolute pre-period correlation between "
                        f"control and test KPIs — higher tracking quality raises the ceiling."
                    )
                st.dataframe(bayes["structural_prior_df"], width='stretch')
                st.caption(
                    "The prior mean remains zero for every control. Structural similarity only changes the prior width: "
                    "better structural matches are allowed more coefficient flexibility, while weaker structural matches "
                    f"are shrunk more strongly toward zero. If {_bayes_lag_label} lagged controls are enabled, each region's lagged "
                    "term uses the same structural prior sigma as its same-period term."
                )

            # ---- MCMC Diagnostics ----
            import arviz as az
            summary = az.summary(bayes['trace'], var_names=["intercept", "coeffs", "sigma"], hdi_prob=0.94)
            _mcmc_n_chains = bayes.get("n_chains")
            _mcmc_n_draws = bayes.get("n_draws")
            _mcmc_n_tune = bayes.get("n_tune")
            _mcmc_target_accept = bayes.get("target_accept")
            _mcmc_n_total_draws = (
                _mcmc_n_chains * _mcmc_n_draws if _mcmc_n_chains and _mcmc_n_draws else None
            )
            diag = summarize_mcmc_diagnostics(
                summary,
                n_divergences=bayes.get("n_divergences"),
                n_total_draws=_mcmc_n_total_draws,
            )

            with st.expander("MCMC Diagnostics", expanded=True):
                st.markdown("**Diagnostic summary**")
                if _mcmc_n_chains is not None:
                    st.caption(
                        f"Sampled {_mcmc_n_chains} chains × {_mcmc_n_draws} draws "
                        f"({_mcmc_n_tune} tuning steps), target_accept={_mcmc_target_accept}."
                    )
                col1, col2, col3, col4, col5 = st.columns(5)
                col1.metric(
                    "Chain convergence",
                    f"{'✅ Pass' if diag['rhat_ok'] else '⚠️ Warning'}",
                    help=(
                        f"R-hat measures whether the sampling chains converged on the same distribution. "
                        f"Values close to 1.0 mean convergence. Above 1.01 suggests the chains disagreed — "
                        f"results may be unreliable.\n\nYour max R-hat: {diag['max_rhat']:.3f} (pass = ≤1.01)."
                    )
                )
                col2.metric(
                    "Effective sample size",
                    f"{'✅ Pass' if diag['ess_ok'] else '⚠️ Warning'}",
                    help=(
                        f"ESS estimates how many independent samples your chains are equivalent to, "
                        f"after accounting for autocorrelation. Higher is better. Low ESS means the "
                        f"sampler got 'stuck' and posterior estimates may be noisy.\n\n"
                        f"Your min ESS: {diag['min_ess']:.0f} (guidance = ≥{CONFIG['ess_min_threshold']})."
                    )
                )
                col3.metric(
                    "Sampling error",
                    f"{'✅ Pass' if diag['mcse_ok'] else '⚠️ Warning'}",
                    help=(
                        f"MCSE (Monte Carlo Standard Error) measures numerical noise in the posterior mean "
                        f"estimates relative to the posterior SD. Below 10% means the sampling error is "
                        f"small compared to genuine uncertainty in the model.\n\n"
                        f"Your max MCSE/SD: {diag['max_mcse_sd_ratio']:.1%} (pass = <10%)."
                    )
                )
                _divergence_help = (
                    "Divergent transitions mean the sampler failed to explore a specific region of the "
                    "posterior. Unlike the other three checks, this can bias point estimates rather than "
                    "just add noise, so even one divergence is treated as a fail here.\n\n"
                    f"Your divergences: {diag['n_divergences'] if diag['n_divergences'] is not None else 'N/A'}"
                )
                if diag['divergence_rate'] is not None:
                    _divergence_help += f" ({diag['divergence_rate']:.1%} of draws)."
                col4.metric(
                    "Divergences",
                    f"{'✅ Pass' if diag['divergence_ok'] else '⚠️ Warning'}",
                    help=_divergence_help
                )
                col5.metric(
                    "Overall status",
                    diag['status'],
                    help=(
                        "All four diagnostics must pass for an overall Good status. "
                        "A warning on any one of them means you should interpret results cautiously — "
                        "try increasing draws, tuning steps, or target_accept if issues persist."
                    )
                )
                if diag['messages']:
                    for msg in diag['messages']:
                        st.warning(msg)

            # Kept as a sibling expander, not nested inside "MCMC Diagnostics" above —
            # Streamlit does not allow expanders to be nested inside other expanders.
            with st.expander("View full MCMC diagnostics table", expanded=False):
                rename_map = {
                    'mean': 'Mean',
                    'sd': 'SD',
                    'hdi_3%': '94% lower',
                    'hdi_97%': '94% upper',
                    'mcse_mean': 'MCSE mean',
                    'mcse_sd': 'MCSE SD',
                    'ess_bulk': 'ESS bulk',
                    'ess_tail': 'ESS tail',
                    'r_hat': 'R-hat'
                }
                existing_cols = [col for col in rename_map.keys() if col in summary.columns]
                display_summary = summary[existing_cols].rename(columns=rename_map).astype(float)
                for col in display_summary.columns:
                    if col in ['ESS bulk', 'ESS tail']:
                        display_summary[col] = display_summary[col].round(0)
                    else:
                        display_summary[col] = display_summary[col].round(3)
                # Replace coeffs[n] index labels with control region / lagged feature names
                coeff_feature_list = bayes.get("model_feature_cols") or bayes.get("control_list", [])
                new_index = []
                for idx in display_summary.index:
                    if idx.startswith("coeffs[") and idx.endswith("]"):
                        try:
                            n = int(idx[7:-1])
                            new_index.append(coeff_feature_list[n] if n < len(coeff_feature_list) else idx)
                        except (ValueError, IndexError):
                            new_index.append(idx)
                    else:
                        new_index.append(idx)
                display_summary.index = new_index

                # ---- Row-level highlighting: flag which specific parameter(s) are driving
                # a "Review needed" status, rather than making the user scan manually. ----
                def _flag_bad_diagnostic_row(row):
                    rhat = row.get('R-hat', np.nan)
                    ess_bulk = row.get('ESS bulk', np.nan)
                    ess_tail = row.get('ESS tail', np.nan)
                    sd = row.get('SD', np.nan)
                    mcse_mean = row.get('MCSE mean', np.nan)
                    mcse_sd_ratio = (mcse_mean / sd) if (pd.notna(sd) and sd != 0 and pd.notna(mcse_mean)) else np.nan
                    is_bad = (
                        (pd.notna(rhat) and rhat > 1.01)
                        or (pd.notna(ess_bulk) and ess_bulk < CONFIG["ess_min_threshold"])
                        or (pd.notna(ess_tail) and ess_tail < CONFIG["ess_min_threshold"])
                        or (pd.notna(mcse_sd_ratio) and mcse_sd_ratio >= 0.10)
                    )
                    return ["background-color: #FEE2E2; color: #7F1D1D"] * len(row) if is_bad else [""] * len(row)

                styled_summary = display_summary.style.apply(_flag_bad_diagnostic_row, axis=1)
                st.dataframe(styled_summary, width='stretch')
                if diag['n_divergences']:
                    st.caption(
                        f"⚠️ {diag['n_divergences']} divergent transition(s) occurred during sampling. "
                        "Divergences aren't tied to a specific parameter row the way R-hat/ESS/MCSE are, "
                        "so they aren't reflected in the highlighting above — see the Divergences card and "
                        "warning above the table instead."
                    )
                st.caption(
                    "Rows highlighted in red fail at least one of: R-hat > 1.01, ESS bulk or tail "
                    f"< {CONFIG['ess_min_threshold']}, or MCSE/SD ≥ 10%."
                )

        # ---- Bayesian interpretation ----
        if st.session_state.get("bayesian_interpretation_visible", False):
            with st.expander("How to interpret Bayesian TBR results", expanded=False):
                st.markdown("""
                **Bayesian TBR – Assessing Test Impact**

                Focus on these three measures:

                **Estimated Incremental Uplift** (posterior mean)
                - The model's best estimate of the intervention's impact.
                - Positive values suggest the test increased the KPI.

                **94% Predictive Interval**
                - The interval within which the future uplift is expected to lie with 94% probability.
                - If entirely above zero, there is strong evidence of a positive effect.
                - If it crosses zero, the result is uncertain.

                **P(Uplift > 0)**
                - The probability that the intervention had a positive impact.
                - High values (e.g., >0.95) indicate strong confidence.

                **Rule of thumb**
                - Positive uplift + interval above zero + high probability = strong evidence.

                **Reading the chart**
                - The blue band (pre-period) is the 94% HDI / credible interval around the *fitted counterfactual mean* — it does not include observation-level noise, so it is narrower.
                - The green band (test/post-period) is the 94% posterior predictive interval — the plausible range of *actual counterfactual observations* under the no-test scenario, including observation-level noise. This is what you should compare the actuals against.
                """)

# ------------------------------------------------------------
# Sidebar data quality footer
# ------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.markdown("### 5. Data Quality Check")
st.sidebar.caption(f"**{market}** - **{geography_level}**")
if validation_issues:
    for issue in validation_issues[:5]:
        st.sidebar.caption(issue)
    if recommendations:
        with st.sidebar.expander("💡 Recommendations", expanded=False):
            for rec in recommendations[:5]:
                st.caption(rec)
    st.sidebar.metric("Data Quality", issue_severity, help=f"Found {len(validation_issues)} potential issues")
else:
    st.sidebar.success(f"✅ Data quality check passed for {market} ({geography_level})")
