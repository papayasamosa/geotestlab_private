# utils/validation.py

import streamlit as st
import pandas as pd
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import ElasticNetCV, RidgeCV
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_squared_error, r2_score
from typing import List, Dict, Tuple
from utils.config import POPULATION_COL, ADOBE_COL, CONFIG
from utils.data_loader import load_market_sheet

# -----------------------------------------------------------------------------
# Helper functions for validation
# -----------------------------------------------------------------------------
def load_and_reshape_kpi(uploaded_file):
    df_raw = pd.read_excel(uploaded_file, engine="calamine", header=0)
    region_col = df_raw.columns[0]
    metric_col = df_raw.columns[1]
    df_long = df_raw.melt(id_vars=[region_col, metric_col], var_name="date", value_name="kpi")
    df_long = df_long.rename(columns={region_col: "region_raw", metric_col: "metric_name"})
    df_long["date"] = pd.to_datetime(df_long["date"], errors="coerce")
    df_long = df_long.dropna(subset=["date", "kpi"])
    df_long["kpi"] = pd.to_numeric(df_long["kpi"], errors="coerce").fillna(0)
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
    test_agg = agg_df[agg_df["region"].isin(test_regions)].groupby("date")["kpi"].sum().reset_index().rename(columns={"kpi": "test_kpi"})
    control_wide = agg_df[agg_df["region"].isin(control_list)].pivot(index="date", columns="region", values="kpi").reset_index()
    model = test_agg.merge(control_wide, on="date", how="inner").sort_values("date").dropna().reset_index(drop=True)
    return model

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

def safe_tscv(n_splits, n_weeks):
    if n_weeks < 6:
        return None
    n = min(n_splits, n_weeks // 3)
    return TimeSeriesSplit(n_splits=max(2, n))

def rolling_origin_validation(X, y, horizon=4, n_splits=5, model_type="enet"):
    smapes = []
    rmses = []
    n = len(y)
    if n < horizon + 6:
        return smapes, rmses
    splits = np.linspace(0, n - horizon, min(n_splits, n - horizon), dtype=int)
    for train_size in splits:
        if train_size < 6:
            continue
        train_X, train_y = X[:train_size], y[:train_size]
        test_X, test_y = X[train_size:train_size+horizon], y[train_size:train_size+horizon]
        if len(test_y) < horizon:
            continue
        scaler = StandardScaler()
        train_X_scaled = scaler.fit_transform(train_X)
        test_X_scaled = scaler.transform(test_X)
        if model_type == "enet":
            model = ElasticNetCV(l1_ratio=[.1,.5,.7,.9,.95,1], alphas=np.logspace(-4,4,50), cv=safe_tscv(3, len(train_y)), max_iter=10000, random_state=42)
        elif model_type == "lasso":
            model = ElasticNetCV(l1_ratio=1, alphas=np.logspace(-4,4,100), cv=safe_tscv(3, len(train_y)), max_iter=10000, random_state=42)
        else:
            return smapes, rmses
        model.fit(train_X_scaled, train_y)
        pred = model.predict(test_X_scaled)
        smapes.append(smape(test_y, pred))
        rmses.append(np.sqrt(mean_squared_error(test_y, pred)))
    return smapes, rmses

def placebo_analysis(uplift_list, real_uplift):
    uplift_arr = np.array(uplift_list)
    if len(uplift_arr) == 0:
        return np.nan, np.nan, np.nan, np.nan, np.nan, np.nan
    median_uplift = np.median(uplift_arr)
    p2_5, p97_5 = np.percentile(uplift_arr, [2.5, 97.5])
    percentile_rank = np.mean(uplift_arr < real_uplift) * 100 if real_uplift is not None else np.nan
    p_one_sided = np.mean(uplift_arr >= real_uplift) if real_uplift is not None else np.nan
    z_score = (real_uplift - np.mean(uplift_arr)) / np.std(uplift_arr) if np.std(uplift_arr) > 0 and real_uplift is not None else np.nan
    return median_uplift, p2_5, p97_5, percentile_rank, p_one_sided, z_score

# -----------------------------------------------------------------------------
# Main validation method runner
# -----------------------------------------------------------------------------
def run_validation_method(agg_df, control_list, test_regions, method_name,
                          pre_start, pre_end, test_start=None, test_end=None,
                          use_post=False, post_start=None, post_end=None,
                          compute_uplift=True):
    """
    Run a single validation method (ElasticNet or LASSO).
    If compute_uplift is False (Design mode), test period is ignored.
    """
    # Convert date inputs to pandas Timestamps
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

    # Pre‑period
    pre_mask = (agg_df["date"] >= pre_start) & (agg_df["date"] <= pre_end)
    model_pre = build_model_matrix(agg_df[pre_mask], control_list, test_regions)
    if len(model_pre) < 6:
        return None
    X_pre = model_pre[control_list].values
    y_pre = model_pre["test_kpi"].values
    dates_pre = model_pre["date"].tolist()
    scaler = StandardScaler()
    X_pre_scaled = scaler.fit_transform(X_pre)

    # Fit model
    if method_name == "enet":
        model = ElasticNetCV(l1_ratio=[.1,.5,.7,.9,.95,1], alphas=np.logspace(-4,4,50), cv=safe_tscv(5, len(y_pre)), max_iter=10000, random_state=42)
    else:  # lasso
        model = ElasticNetCV(l1_ratio=1, alphas=np.logspace(-4,4,100), cv=safe_tscv(5, len(y_pre)), max_iter=10000, random_state=42)
    model.fit(X_pre_scaled, y_pre)
    y_pred_pre = model.predict(X_pre_scaled)
    corr, r2, s, rmse = compute_metrics(y_pre, y_pred_pre)

    # Rolling origin validation
    smapes_ro, rmses_ro = rolling_origin_validation(X_pre, y_pre, horizon=4, n_splits=5, model_type=method_name)
    holdout_smape_mean = np.mean(smapes_ro) if smapes_ro else np.nan
    holdout_rmse_mean = np.mean(rmses_ro) if rmses_ro else np.nan

    # Test period (only if compute_uplift and dates provided)
    if compute_uplift and test_start is not None and test_end is not None:
        test_mask = (agg_df["date"] >= test_start) & (agg_df["date"] <= test_end)
        model_test = build_model_matrix(agg_df[test_mask], control_list, test_regions)
        if not model_test.empty:
            X_test = model_test[control_list].values
            X_test_scaled = scaler.transform(X_test)
            y_test_actual = model_test["test_kpi"].values
            y_pred_test = model.predict(X_test_scaled)
            uplift = y_test_actual.sum() - y_pred_test.sum()
            uplift_pct = (uplift / y_pred_test.sum()) * 100 if y_pred_test.sum() != 0 else np.nan
            dates_test = model_test["date"].tolist()
            test_len = len(y_test_actual)
        else:
            uplift = uplift_pct = None
            y_test_actual = y_pred_test = None
            dates_test = []
            test_len = 0
    else:
        uplift = uplift_pct = None
        y_test_actual = y_pred_test = None
        dates_test = []
        test_len = 0

    # Post period
    if use_post and post_start is not None and post_end is not None:
        post_mask = (agg_df["date"] >= post_start) & (agg_df["date"] <= post_end)
        model_post = build_model_matrix(agg_df[post_mask], control_list, test_regions)
        if not model_post.empty:
            X_post = model_post[control_list].values
            X_post_scaled = scaler.transform(X_post)
            y_post_pred = model.predict(X_post_scaled)
            y_post_actual = model_post["test_kpi"].values
            dates_post = model_post["date"].tolist()
        else:
            y_post_pred = y_post_actual = dates_post = None
    else:
        y_post_pred = y_post_actual = dates_post = None

    # Negative predictions
    neg_pre = any(y_pred_pre < 0)
    neg_test = any(y_pred_test < 0) if y_pred_test is not None else False
    neg_post = any(y_post_pred < 0) if y_post_pred is not None else False

    # Rolling placebo (only if test period exists and uplift computed)
    placebos = []
    placebo_smapes = []
    if compute_uplift and test_len > 0:
        current_end = test_start - pd.Timedelta(weeks=1)
        while current_end - pd.Timedelta(weeks=test_len-1) >= pre_start:
            p_start = current_end - pd.Timedelta(weeks=test_len-1)
            p_end = current_end
            if p_end <= pre_end:
                train_mask = (agg_df["date"] >= pre_start) & (agg_df["date"] < p_start)
                test_placebo_mask = (agg_df["date"] >= p_start) & (agg_df["date"] <= p_end)
                if train_mask.sum() >= 6:
                    df_train = agg_df[train_mask].copy()
                    df_test_p = agg_df[test_placebo_mask].copy()
                    m_train = build_model_matrix(df_train, control_list, test_regions)
                    m_test_p = build_model_matrix(df_test_p, control_list, test_regions)
                    if len(m_train) >= 6 and not m_test_p.empty:
                        X_tr = m_train[control_list].values
                        y_tr = m_train["test_kpi"].values
                        X_te = m_test_p[control_list].values
                        y_te = m_test_p["test_kpi"].values
                        scaler_p = StandardScaler()
                        X_tr_scaled = scaler_p.fit_transform(X_tr)
                        ridge = RidgeCV(alphas=np.logspace(-4,4,50), cv=safe_tscv(3, len(y_tr)))
                        ridge.fit(X_tr_scaled, y_tr)
                        pred_p = ridge.predict(scaler_p.transform(X_te))
                        uplift_p = y_te.sum() - pred_p.sum()
                        placebos.append(uplift_p)
                        placebo_smapes.append(smape(y_te, pred_p))
            current_end -= pd.Timedelta(weeks=1)

    # Placebo statistics
    if placebos and uplift is not None:
        median_uplift, p2_5, p97_5, percentile_rank, p_one_sided, z_score = placebo_analysis(placebos, uplift)
        median_placebo_smape = np.median(placebo_smapes) if placebo_smapes else np.nan
        p95_placebo_smape = np.percentile(placebo_smapes, 95) if placebo_smapes else np.nan
    else:
        median_uplift = p2_5 = p97_5 = percentile_rank = p_one_sided = z_score = median_placebo_smape = p95_placebo_smape = np.nan

    # LASSO-specific outputs
    if method_name == "lasso":
        coefs = model.coef_
        selected = [control_list[i] for i, c in enumerate(coefs) if abs(c) > 1e-6]
        coeff_dict = dict(zip(control_list, coefs))
        selected_df = pd.DataFrame([(r, coeff_dict[r]) for r in selected], columns=["Region", "Coefficient"])
        removed = [r for r in control_list if r not in selected]
        n_candidates = len(control_list)
        n_selected = len(selected)
        n_removed = n_candidates - n_selected
        alpha = model.alpha_
    else:
        selected_df = pd.DataFrame()
        n_candidates = len(control_list)
        n_selected = n_candidates
        n_removed = 0
        alpha = np.nan
        selected = control_list

    return {
        "dates_pre": dates_pre,
        "y_pre": y_pre,
        "y_pred_pre": y_pred_pre,
        "corr": corr,
        "r2": r2,
        "smape": s,
        "rmse": rmse,
        "holdout_smape_mean": holdout_smape_mean,
        "holdout_rmse_mean": holdout_rmse_mean,
        "uplift": uplift,
        "uplift_pct": uplift_pct,
        "dates_test": dates_test,
        "y_test_actual": y_test_actual,
        "y_pred_test": y_pred_test,
        "dates_post": dates_post,
        "y_post_actual": y_post_actual,
        "y_post_pred": y_post_pred,
        "placebos": placebos,
        "placebo_smapes": placebo_smapes,
        "median_placebo_uplift": median_uplift,
        "placebo_range_lower": p2_5,
        "placebo_range_upper": p97_5,
        "placebo_percentile_rank": percentile_rank,
        "placebo_p_value": p_one_sided,
        "placebo_z_score": z_score,
        "median_placebo_smape": median_placebo_smape,
        "p95_placebo_smape": p95_placebo_smape,
        "neg_pre": neg_pre,
        "neg_test": neg_test,
        "neg_post": neg_post,
        "selected_regions": selected,
        "selected_df": selected_df,
        "n_candidates": n_candidates,
        "n_selected": n_selected,
        "n_removed": n_removed,
        "alpha": alpha,
        "control_list": control_list,
        "scaler": scaler,
        "model": model
    }
