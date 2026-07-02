# utils/session.py

import streamlit as st

def init_session_state():
    """Initialise all session state variables."""
    if "current_step" not in st.session_state:
        st.session_state.current_step = 1
        
def reset_design_results():
    """Reset only the design step results (keeps market/geography selection)."""
    st.session_state.design["final_controls"] = None
    st.session_state.design["test_df"] = None
    st.session_state.design["opt_results"] = {}
    st.session_state.design["match_mode_res"] = None
    st.session_state.design["best_n"] = None
    st.session_state.design["w_reset"] = st.session_state.design.get("w_reset", 0) + 1
    st.session_state.design["guided_share_info"] = None
    st.session_state.design["selected_experiment_regions"] = []
    st.session_state.design["comp_df"] = None
    st.session_state.design["run_clicked"] = False
    
    # Design step
    if "design" not in st.session_state:
        st.session_state.design = {
            "market": None,
            "geography_level": None,
            "setup_mode": "Select test geographies yourself",
            "test_geos": [],
            "force_exp_include": [],
            "force_exp_exclude": [],
            "force_ctrl_include": [],
            "force_ctrl_exclude": [],
            "target_test_share": 25,
            "target_tolerance_pp": 5,
            "guided_iterations": 2000,
            "weights": {},
            "force_1to1": False,
            "min_p": 0,
            "max_p": 0,
            "run_clicked": False,
            "final_controls": None,
            "test_df": None,
            "opt_results": {},
            "match_mode_res": None,
            "best_n": None,
            "w_reset": 0,
            "guided_share_info": None,
            "selected_experiment_regions": [],
            "agg_df": None,
            "active_features": [],
            "proportion_cols": set(),
            "comp_df": None,
            "avg_smd": None,
            "weighted_dist": None,
            "smd_list": None,
            "e_m": None,
            "c_m": None,
            "experiment_pop": 0,
            "control_pop": 0,
            "eligible_market_pop": 0,
            "validation_issues": [],
            "recommendations": [],
            "issue_severity": "🟢 None",
        }

    # Validation step
    if "validation" not in st.session_state:
        st.session_state.validation = {
            "triggered": False,
            "results": {},
            "mode": "Design",
            "test_start": None,
            "test_end": None,
            "pre_start": None,
            "pre_end": None,
            "use_post": False,
            "post_start": None,
            "post_end": None,
            "agg_df_val": None,
            "selected_metric": "",
            "compute_uplift": False,
            "test_regions_val": [],
            "control_regions_val": [],
            "force_excluded_regions": [],
            "all_non_test": [],
            "uploaded_file": None,
        }

    # Evaluation step
    if "evaluation" not in st.session_state:
        st.session_state.evaluation = {}

def reset_design_results():
    """Reset only the design step results (keeps market/geography selection)."""
    st.session_state.design["final_controls"] = None
    st.session_state.design["test_df"] = None
    st.session_state.design["opt_results"] = {}
    st.session_state.design["match_mode_res"] = None
    st.session_state.design["best_n"] = None
    st.session_state.design["w_reset"] = st.session_state.design.get("w_reset", 0) + 1
    st.session_state.design["guided_share_info"] = None
    st.session_state.design["selected_experiment_regions"] = []
    st.session_state.design["comp_df"] = None
    st.session_state.design["run_clicked"] = False
