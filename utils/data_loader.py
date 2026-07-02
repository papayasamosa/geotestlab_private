# utils/data_loader.py

import streamlit as st
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple
import unicodedata
from scipy import stats
from utils.config import (
    DATA_PATH, POPULATION_COL_RAW, POPULATION_COL, ADOBE_COL,
    CONFIG, SMD_GOOD_THRESHOLD, SMD_HIGH_THRESHOLD
)

# -----------------------------------------------------------------------------
# Text and column helpers
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Excel workbook loading
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Aggregation helpers
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Data quality validation
# -----------------------------------------------------------------------------
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
                z_scores = np.abs(stats.zscore(clean_data))  # stats imported later in app? We'll import scipy.stats at top of matching? Actually we'll import scipy.stats in data_loader? Better to import in this function. We'll add import scipy.stats at top.
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
        cols = st.session_state.final_controls.columns[: CONFIG["max_display_features"] + 3]
        st.session_state.final_controls = st.session_state.final_controls[cols]
