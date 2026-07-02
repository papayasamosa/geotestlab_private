# utils/exports.py

import pandas as pd
import io
from utils.config import POPULATION_COL

def create_excel_export(export_summary, export_features, final_controls, test_df,
                        geo_col, active_features, validation_issues, recommendations):
    """Create an Excel file with multiple sheets."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_summary.to_excel(writer, sheet_name="Summary", index=False)
        export_features.to_excel(writer, sheet_name="Feature_Comparison", index=False)

        control_cols = [geo_col, POPULATION_COL] + active_features[: min(5, len(active_features))]
        control_cols = [c for c in control_cols if c in final_controls.columns]
        control_details = final_controls[control_cols].copy()
        control_details.to_excel(writer, sheet_name="Control_Group", index=False)

        test_details = test_df[control_cols].copy()
        test_details.to_excel(writer, sheet_name="Experiment_Group", index=False)

        if validation_issues:
            max_len = max(len(validation_issues), len(recommendations))
            validation_df = pd.DataFrame({
                "Issue": validation_issues + [""] * (max_len - len(validation_issues)),
                "Recommendation": recommendations + [""] * (max_len - len(recommendations)),
            })
            validation_df.to_excel(writer, sheet_name="Data_Quality_Report", index=False)
    return output.getvalue()

def create_csv_export(comp_df):
    """Create CSV from feature comparison."""
    return comp_df.to_csv(index=False)
