# utils/config.py

CONFIG = {
    "max_hill_climbing_swaps": 15,
    "genetic_iterations": {"min": 100, "max": 5000, "default": 1000},
    "max_control_pool_size": 50,
    "smd_thresholds": {"good": 0.20, "high": 0.50},
    "cache_ttl": 3600,
    "max_display_features": 10,
    "missing_threshold": 20,
    "outlier_std_threshold": 5,
}

SMD_GOOD_THRESHOLD = 0.20
SMD_HIGH_THRESHOLD = 0.50

DATA_PATH = "data/Population Stats for Geo Tests - Master Sheet Only v2 (Standardised).xlsx"
POPULATION_COL_RAW = "Total Population"
POPULATION_COL = "Population"
ADOBE_COL = "Adobe Reference List"
