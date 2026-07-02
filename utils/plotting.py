# utils/plotting.py

import streamlit as st
import pandas as pd
import numpy as np
import altair as alt
import plotly.express as px
from utils.config import SMD_GOOD_THRESHOLD, SMD_HIGH_THRESHOLD, POPULATION_COL

def create_love_plot(comp_df):
    """Generate the love plot (feature balance plot)."""
    pdf = comp_df.sort_values("Abs SMD")
    fig = px.scatter(
        pdf,
        x="Abs SMD",
        y="Feature",
        color="Abs SMD",
        color_continuous_scale=["#CCFBF1", "#0F766E"],
        title="Feature Balance Plot",
        labels={"Abs SMD": "Absolute SMD"},
    )
    fig.add_vline(x=SMD_GOOD_THRESHOLD, line_dash="dash", line_color="#0F766E")
    fig.add_vline(x=SMD_HIGH_THRESHOLD, line_dash="dash", line_color="#F59E0B")
    fig.update_layout(
        height=500,
        margin=dict(l=10, r=10, t=50, b=10),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig

def create_optimization_plot(opt_results, best_n):
    """Create the pool size optimization plot."""
    size_df = opt_results.get("size_df")
    if size_df is None or size_df.empty:
        return None
    base = alt.Chart(size_df).mark_line(point=True, color="#7C3AED").encode(
        x=alt.X("Num_Controls:Q", title="Number of Controls"),
        y=alt.Y("Mean_Abs_SMD:Q", title="Mean Abs SMD"),
        tooltip=["Num_Controls", "Mean_Abs_SMD"],
    )
    rule_df = pd.DataFrame({"best_n": [best_n]})
    marker = alt.Chart(rule_df).mark_rule(
        color="#0F766E",
        strokeDash=[6, 4],
    ).encode(x="best_n:Q")
    return base + marker

def create_convergence_plot(convergence_data):
    """Create the search convergence plot."""
    if not convergence_data:
        return None
    conv_df = pd.DataFrame({
        "step": list(range(len(convergence_data))),
        "Mean_Abs_SMD": convergence_data,
    })
    return alt.Chart(conv_df).mark_line(color="#0F766E").encode(
        x=alt.X("step:Q", title="Improvement Steps"),
        y=alt.Y("Mean_Abs_SMD:Q", title="Mean Abs SMD"),
        tooltip=["step", "Mean_Abs_SMD"],
    ).properties(height=280)

def create_violin_plot(test_df, control_df, feature):
    """Create a violin plot comparing test and control distributions for a feature."""
    if feature not in test_df.columns or feature not in control_df.columns:
        return None
    test_data = test_df[feature].dropna()
    control_data = control_df[feature].dropna()
    if len(test_data) <= 1 or len(control_data) <= 1:
        return None
    density_df = pd.concat([
        pd.DataFrame({"value": test_data, "Group": "Experimental"}),
        pd.DataFrame({"value": control_data, "Group": "Control Group"}),
    ], ignore_index=True)
    fig = px.violin(
        density_df,
        x="Group",
        y="value",
        color="Group",
        box=True,
        points="all",
        color_discrete_map={"Experimental": "#7C3AED", "Control Group": "#0F766E"},
        labels={"value": feature},
    )
    fig.update_layout(
        title=f"Distribution Comparison: {feature}",
        yaxis_title=feature,
        xaxis_title="Group",
        showlegend=False,
        height=420,
        margin=dict(l=10, r=10, t=50, b=10),
    )
    return fig

def create_validation_plot(results, method_name, test_start, test_end, use_post, post_start, selected_metric):
    """Create actual vs predicted and indexed plots for validation results."""
    res = results
    all_dates = res['dates_pre'] + (res['dates_test'] if res['dates_test'] else []) + (res['dates_post'] if res['dates_post'] else [])
    all_actual = list(res['y_pre']) + (list(res['y_test_actual']) if res['y_test_actual'] is not None else []) + (list(res['y_post_actual']) if res['y_post_actual'] is not None else [])
    all_pred = list(res['y_pred_pre']) + (list(res['y_pred_test']) if res['y_pred_test'] is not None else []) + (list(res['y_post_pred']) if res['y_post_pred'] is not None else [])

    fig1 = px.line(x=all_dates, y=all_actual, title=f"Actual vs Predicted – {method_name}")
    fig1.add_scatter(x=all_dates, y=all_pred, name="Predicted", line=dict(dash="dash"))
    if test_start is not None:
        fig1.add_vline(x=test_start, line_dash="dot", line_color="red", annotation_text="Test start")
    if test_end is not None:
        fig1.add_vline(x=test_end, line_dash="dot", line_color="orange", annotation_text="Test end")
    if use_post and post_start:
        fig1.add_vline(x=post_start, line_dash="dot", line_color="green", annotation_text="Post start")

    # Indexed plot
    pre_mean = np.mean(res['y_pre'])
    if pre_mean > 0:
        idx_actual = np.array(all_actual) / pre_mean * 100
        idx_pred = np.array(all_pred) / pre_mean * 100
        fig2 = px.line(x=all_dates, y=idx_actual, title=f"Indexed (pre‑period avg=100) – {method_name}")
        fig2.add_scatter(x=all_dates, y=idx_pred, name="Predicted", line=dict(dash="dash"))
        if test_start is not None:
            fig2.add_vline(x=test_start, line_dash="dot", line_color="red")
        if test_end is not None:
            fig2.add_vline(x=test_end, line_dash="dot", line_color="orange")
        if use_post and post_start:
            fig2.add_vline(x=post_start, line_dash="dot", line_color="green")
        return fig1, fig2
    else:
        return fig1, None

def create_placebo_histogram(placebos, real_uplift, method_name):
    """Create a histogram of placebo uplift distribution."""
    fig = px.histogram(pd.DataFrame({"placebo": placebos}), x="placebo", nbins=20, title=f"Placebo distribution – {method_name}")
    if real_uplift is not None:
        fig.add_vline(x=real_uplift, line_dash="dash", line_color="red", annotation_text="Real uplift")
    return fig

def create_posterior_plot(uplift_samples):
    """Create posterior distribution plot for Bayesian uplift."""
    fig = px.histogram(pd.DataFrame({"uplift": uplift_samples}), x="uplift", nbins=50, title="Posterior uplift distribution")
    fig.add_vline(x=0, line_dash="dash", line_color="red")
    return fig
