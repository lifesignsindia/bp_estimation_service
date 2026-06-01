"""
config.py - Central configuration for the Vitals Inference Pipeline.
"""

import os

# ─── Processor Settings ──────────────────────────────────────────────────────
class SETTINGS:
    filter2_lowcut = 0.5
    filter2_highcut = 8.0
    filter2_order = 4
    block_duration_sec = 30.0
    block_filter_type = "butter"
    block_fir_numtaps = 101
    block_filter_lowcut = 0.5
    block_filter_highcut = 8.0

# ─── Model & System Constraints ──────────────────────────────────────────────
# The Blood Pressure AI was trained on 120Hz data.
# Do NOT change this. All incoming data will be resampled to this frequency.
MODEL_SAMPLING_RATE_HZ = 120
SAMPLING_RATE_HZ       = 120   # default source rate (pipeline always delivers 120 Hz)

# Physiological clamps applied after base+delta regression
BP_SBP_LIMITS = (60, 250)
BP_DBP_LIMITS = (30, 150)

# ─── BP Inference Tuning ─────────────────────────────────────────────────────
# Max ±delta the regression model can shift from the category base (mmHg)
BP_DELTA_CLIP = 15.0

# Category base BPs — (SBP, DBP) centre for each classifier label
BP_CATEGORY_BASES = {
    "hypo":   (90.0,  60.0),
    "normal": (118.0, 76.0),
    "hyper":  (142.0, 90.0),
}

# If the classifier's top-category probability is below this, fall back to
# "normal" base regardless of device type
BP_CONFIDENCE_THRESHOLD = 0.50

# BerryMed preprocessing was fixed to match NISO204 (median despike + normalize only,
# no IIR filter). Confidence threshold is now the same as other devices.
BP_BERRYMED_CONFIDENCE_THRESHOLD = 0.50

# ─── Hardware Sentinels (Invalid Values) ─────────────────────────────────────
BERRY_SPO2_INVALID      = 127
BERRY_HR_INVALID        = 255
BERRY_PI_INVALID        = 0
CHECKME_FINGER_OFF_LOW  = 154
CHECKME_FINGER_OFF_HIGH = 160

# ─── Model Directory Paths ───────────────────────────────────────────────────
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

BP_MODEL_CONFIG = {
    "classifier":    os.path.join(MODEL_DIR, "classifier.pkl"),
    "global_scaler": os.path.join(MODEL_DIR, "global_feature_scaler.pkl"),
    "hypo":          os.path.join(MODEL_DIR, "hypo_models.pkl"),
    "normal":        os.path.join(MODEL_DIR, "normal_models.pkl"),
    "hyper":         os.path.join(MODEL_DIR, "hyper_models.pkl"),
    "scaler_hypo":   os.path.join(MODEL_DIR, "scaler_hypo.pkl"),
    "scaler_normal": os.path.join(MODEL_DIR, "scaler_normal.pkl"),
    "scaler_hyper":  os.path.join(MODEL_DIR, "scaler_hyper.pkl"),
}

HB_GLU_MODEL_CONFIG = {
    "hb_scaler":      os.path.join(MODEL_DIR, "scaler_hb.pkl"),
    "hb_model":       os.path.join(MODEL_DIR, "hb_regressor.pkl"),
    "glucose_scaler": os.path.join(MODEL_DIR, "scaler_glucose.pkl"),
    "glucose_model":  os.path.join(MODEL_DIR, "glucose_regressor.pkl"),
}

# ─── Redis — Reference BP Persistence ────────────────────────────────────────
REDIS_HOST     = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT     = int(os.getenv("REDIS_PORT", "6379"))

REDIS_REF_TTL  = 86400  # 24 hours per clinical session

# ─── Kafka ────────────────────────────────────────────────────────────────────
KAFKA_BROKERS      = os.getenv("KAFKA_BROKERS",      "localhost:9092")
KAFKA_INPUT_TOPIC  = os.getenv("KAFKA_INPUT_TOPIC",  "vitals.raw")
KAFKA_OUTPUT_TOPIC = os.getenv("KAFKA_OUTPUT_TOPIC", "vitals.clinical")
KAFKA_GROUP_ID     = os.getenv("KAFKA_GROUP_ID",     "vitals-pipeline")
KAFKA_DEBUG_TOPIC  = os.getenv("KAFKA_DEBUG_TOPIC",  "")   # optional — leave empty to disable
