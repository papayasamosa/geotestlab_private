# utils/matching.py

import streamlit as st
import pandas as pd
import numpy as np
import random
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import NearestNeighbors
from typing import Tuple, List, Dict
from utils.config import CONFIG, POPULATION_COL
from utils.data_loader import impute_missing_features

# -----------------------------------------------------------------------------
# Matching metric helpers
# -----------------------------------------------------------------------------
@st.cache_data(ttl=CONFIG["cache_ttl"])
def calculate_metrics_cached(test_df, control_df, features_tuple, weights_tuple):
    features = list(features_tuple)
    weights_dict = dict(zip(features, weights_tuple))
    return calculate_metrics(test_df, control_df, features, weights_dict)

def calculate_metrics(test_df, control_df, features, weights_dict):
    if not features:
        return 0.0, 0.0, [], np.array([]), np.array([])
    test_df = impute_missing_features(test_df, features)
    control_df = impute_missing_features(control_df, features)
    e_means = test_df[features].mean()
    c_means = control_df[features].mean()
    scaler = StandardScaler()
    combined = pd.concat([test_df[features], control_df[features]], axis=0)
    scaler.fit(combined)
    z_test = scaler.transform(test_df[features]).mean(axis=0)
    z_control = scaler.transform(control_df[features]).mean(axis=0)
    w_vector = np.array([weights_dict.get(f, 1.0) for f in features])
    sq_diff = (z_test - z_control) ** 2
    weighted_dist = np.sqrt(np.sum(w_vector * sq_diff))
    smd_list = []
    for f in features:
        e_m = e_means[f]
        c_m = c_means[f]
        e_s = test_df[f].std(ddof=0)
        c_s = control_df[f].std(ddof=0)
        pooled_std = np.sqrt((e_s ** 2 + c_s ** 2) / 2)
        if pooled_std > 0 and np.isfinite(pooled_std):
            smd = abs((e_m - c_m) / pooled_std)
        else:
            smd = 0
        smd_list.append(smd)
    return np.mean(smd_list), weighted_dist, smd_list, e_means.values, c_means.values

@st.cache_data(ttl=CONFIG["cache_ttl"])
def preprocess_data(pool_df, test_df_run, active_features, weights):
    pool_df = impute_missing_features(pool_df, active_features)
    test_df_run = impute_missing_features(test_df_run, active_features)
    scaler = StandardScaler()
    w_vec = np.array([np.sqrt(weights.get(f, 1.0)) for f in active_features])
    p_scaled = scaler.fit_transform(pool_df[active_features]) * w_vec
    t_cent = scaler.transform(test_df_run[active_features].mean().values.reshape(1, -1)) * w_vec
    return scaler, w_vec, p_scaled, t_cent

# -----------------------------------------------------------------------------
# Guided experiment group search
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Run matching (wrapper that encapsulates the whole matching process)
# -----------------------------------------------------------------------------
def run_matching(agg_df, geo_col, active_features, weights,
                 test_geos, control_pool_geos, match_mode,
                 force_1to1, min_p, max_p,
                 setup_mode, force_exp_include, force_exp_exclude,
                 force_ctrl_include, force_ctrl_exclude,
                 target_test_share, target_tolerance_pp, guided_iterations,
                 total_market_pop):
    """
    Execute the full matching pipeline based on the provided parameters.
    Returns (final_controls, test_df, best_n, opt_results, match_mode_res, best_smd, guided_share_info)
    """
    if setup_mode == "Set test/control constraints":
        conflicts = (set(force_exp_include) & set(force_exp_exclude)) | (set(force_ctrl_include) & set(force_ctrl_exclude)) | (set(force_exp_include) & set(force_ctrl_include))
        if conflicts:
            raise ValueError(f"Invalid constraints. Conflicting assignments: {sorted(conflicts)}")
        test_geos, achieved_share, target_met = find_guided_test_group(
            agg_df, geo_col, total_market_pop,
            force_exp_include, force_exp_exclude,
            force_ctrl_include, force_ctrl_exclude,
            target_test_share, target_tolerance_pp, guided_iterations
        )
        if len(test_geos) == 0:
            raise ValueError("Could not construct a valid experiment group with the provided constraints.")
        guided_share_info = {"achieved": achieved_share * 100, "target": target_test_share, "tolerance": target_tolerance_pp, "met": target_met}
        # Recalculate control pool based on guided test groups
        all_geos = set(agg_df[geo_col].unique())
        control_pool_geos = list((all_geos - set(test_geos) - set(force_ctrl_exclude)) | set(force_ctrl_include))
    else:
        guided_share_info = None

    if len(test_geos) == 0:
        raise ValueError("No test geographies selected.")

    test_df_run = agg_df[agg_df[geo_col].isin(test_geos)].copy()
    pool_df = agg_df[agg_df[geo_col].isin(control_pool_geos)].copy()
    test_df_run = impute_missing_features(test_df_run, active_features)
    pool_df = impute_missing_features(pool_df, active_features)

    if len(test_df_run) == 0:
        raise ValueError("No test geographies selected.")
    if len(pool_df) == 0:
        raise ValueError("No control geographies available.")

    if force_1to1:
        s_min = s_max = len(test_geos)
    else:
        s_min, s_max = min_p, max_p
        if s_min <= 0 or s_max <= 0:
            raise ValueError("Invalid control pool size range.")

    if len(pool_df) < s_max:
        raise ValueError(f"Insufficient controls available. Need {s_max}, have {len(pool_df)}.")

    scaler, w_vec, p_scaled, t_cent = preprocess_data(pool_df, test_df_run, active_features, weights)
    opt_data = []
    best_smd = float("inf")
    best_idx = None
    global_conv = []
    size_range = [len(test_geos)] if force_1to1 else range(s_min, s_max + 1)

    # Determine matching algorithm based on match_mode
    # match_mode is one of: "Greedy (Nearest Neighbor)", "Refined Greedy (Hill Climbing)", "Stochastic (Genetic Search)"
    # But we need to know the iterations for genetic search. We'll pass it as a parameter.
    # We'll add iterations as argument, default from CONFIG.
    iterations = CONFIG["genetic_iterations"]["default"] if match_mode == "Stochastic (Genetic Search)" else 0
    # We'll also need CONFIG["max_hill_climbing_swaps"].
    max_swaps = CONFIG["max_hill_climbing_swaps"]

    for n in size_range:
        if match_mode == "Greedy (Nearest Neighbor)":
            nn = NearestNeighbors(n_neighbors=min(n, len(pool_df))).fit(p_scaled)
            _, ind = nn.kneighbors(t_cent)
            c_idx = [pool_df.index[j] for j in ind[0][:n]]
            m, _, _, _, _ = calculate_metrics(test_df_run, agg_df.loc[c_idx], active_features, weights)
            opt_data.append({"Num_Controls": n, "Mean_Abs_SMD": m, "Indices": c_idx})
            if m < best_smd:
                best_smd, best_idx = m, c_idx
        elif match_mode == "Refined Greedy (Hill Climbing)":
            nn_w = NearestNeighbors(n_neighbors=min(len(pool_df), n + 5)).fit(p_scaled)
            _, ind_w = nn_w.kneighbors(t_cent)
            curr_idx = [pool_df.index[j] for j in ind_w[0][:n]]
            pot_swaps = [pool_df.index[j] for j in ind_w[0] if pool_df.index[j] not in curr_idx][:max_swaps]
            curr_smd, _, _, _, _ = calculate_metrics(test_df_run, agg_df.loc[curr_idx], active_features, weights)
            conv = [curr_smd]
            improved = True
            while improved:
                improved = False
                best_improvement = 0
                best_swap_tuple = None
                for j in range(min(len(curr_idx), 5)):
                    for swap_in in pot_swaps[:10]:
                        temp = curr_idx.copy()
                        temp[j] = swap_in
                        n_smd, _, _, _, _ = calculate_metrics(test_df_run, agg_df.loc[temp], active_features, weights)
                        improvement = curr_smd - n_smd
                        if improvement > best_improvement:
                            best_improvement = improvement
                            best_swap_tuple = (temp, swap_in, n_smd)
                if best_improvement > 0 and best_swap_tuple:
                    curr_idx, swap_in, curr_smd = best_swap_tuple
                    if swap_in in pot_swaps:
                        pot_swaps.remove(swap_in)
                    conv.append(curr_smd)
                    improved = True
            opt_data.append({"Num_Controls": n, "Mean_Abs_SMD": curr_smd, "Indices": curr_idx})
            if curr_smd < best_smd:
                best_smd, best_idx, global_conv = curr_smd, curr_idx, conv
        elif match_mode == "Stochastic (Genetic Search)":
            nn = NearestNeighbors(n_neighbors=min(n, len(pool_df))).fit(p_scaled)
            _, ind = nn.kneighbors(t_cent)
            curr_idx = list(pool_df.index[ind[0]])
            curr_smd, _, _, _, _ = calculate_metrics(test_df_run, agg_df.loc[curr_idx], active_features, weights)
            conv = [curr_smd]
            actual_iterations = min(iterations, n * 100)
            for _ in range(actual_iterations):
                avail = [idx for idx in pool_df.index if idx not in curr_idx]
                if not avail:
                    break
                cand = curr_idx.copy()
                cand[random.randint(0, n - 1)] = random.choice(avail)
                n_smd, _, _, _, _ = calculate_metrics(test_df_run, agg_df.loc[cand], active_features, weights)
                if n_smd < curr_smd:
                    curr_smd, curr_idx = n_smd, cand
                conv.append(curr_smd)
            opt_data.append({"Num_Controls": n, "Mean_Abs_SMD": curr_smd, "Indices": curr_idx})
            if curr_smd < best_smd:
                best_smd, best_idx, global_conv = curr_smd, curr_idx, conv

    final_controls = agg_df.loc[best_idx].copy()
    test_df = test_df_run.copy()
    best_n = len(best_idx)
    opt_results = {"size_df": pd.DataFrame(opt_data), "convergence": global_conv}
    return final_controls, test_df, best_n, opt_results, match_mode, best_smd, guided_share_info
