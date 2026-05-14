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
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
REDIS_REF_TTL  = 86400  # 24 hours per clinical session

# ─── Kafka ────────────────────────────────────────────────────────────────────
KAFKA_BROKERS      = os.getenv("KAFKA_BROKERS",      "localhost:9092")
KAFKA_INPUT_TOPIC  = os.getenv("KAFKA_INPUT_TOPIC",  "vitals.raw")
KAFKA_OUTPUT_TOPIC = os.getenv("KAFKA_OUTPUT_TOPIC", "vitals.clinical")
