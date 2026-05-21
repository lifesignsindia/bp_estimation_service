import json
import sys
import os
import argparse
import time
import numpy as np
import redis as redis_lib
from scipy import signal
import config as cfg

# Add current directory to path to ensure absolute imports work
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.append(script_dir)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Imports (Processors and AI)
# ─────────────────────────────────────────────────────────────────────────────
try:
    # Hardware Processors
    from processors.niso101_processor import PPGFilter as BerryMedProcessor
    from processors.niso103_processor import PlethProcessor as CheckmeProcessor
    from processors.niso204_processor import NISO204Processor
    
    # The AI Inference Engine
    from inference_engine import VitalInferenceEngine
except ImportError as e:
    print(f"CRITICAL IMPORT ERROR: {e}")
    print("Ensure your 'processors' folder has an __init__.py and the AI files are present.")
    sys.exit(1)

# ─── AI Engine ───────────────────────────────────────────────────────────────
print("[AI]   Loading models into memory...")
sys.stdout.flush()
try:
    ai_engine = VitalInferenceEngine()
    print("[AI]   AI Engine Ready.")
    sys.stdout.flush()
except Exception as e:
    print(f"[AI]   FATAL: AI Engine failed to load — {e}")
    sys.stdout.flush()
    sys.exit(1)

# ─── Processors ──────────────────────────────────────────────────────────────
print("[AI]   Initializing device processors...")
sys.stdout.flush()
PROCESSORS = {
    "BERRYMED": BerryMedProcessor(fs=200, lowcut=0.5, highcut=8.0, order=4),
    "CHECKME":  CheckmeProcessor(fs=125),
    "NISO204":  NISO204Processor(kernel_size=5)
}
print("[AI]   Processors ready.")
sys.stdout.flush()

# Constants
DEVICE_NISO204  = "NISO204"
DEVICE_CHECKME  = "CHECKME"
DEVICE_BERRYMED = "BERRYMED"
DEVICE_LS06     = "LS06"

_DEVICE_NAME_MAP = {
    "BERRYMED": "NISO101",
    "CHECKME":  "NISO103",
    "NISO204":  "NISO204",
}

# ─── Redis ────────────────────────────────────────────────────────────────────
print(f"[REDIS] Connecting to {cfg.REDIS_HOST}:{cfg.REDIS_PORT}...")
sys.stdout.flush()
try:
    _redis = redis_lib.Redis(
        host=cfg.REDIS_HOST,
        port=cfg.REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    _redis.ping()
    print("[REDIS] Connected OK.")
    sys.stdout.flush()
except Exception as e:
    print(f"[REDIS] FATAL: Cannot connect — {e}")
    sys.stdout.flush()
    sys.exit(1)

def _ref_write(adm_id, sbp, dbp, timestamp):
    _redis.setex(
        f"ref:{adm_id}",
        cfg.REDIS_REF_TTL,
        json.dumps({"sbp": sbp, "dbp": dbp, "timestamp": timestamp})
    )

def _ref_read(adm_id):
    raw = _redis.get(f"ref:{adm_id}")
    return json.loads(raw) if raw else {"sbp": 0, "dbp": 0}

def _recal_write(adm_id, value):
    _redis.setex(f"recal:{adm_id}", cfg.REDIS_REF_TTL, "1" if value else "0")

def _recal_read(adm_id):
    raw = _redis.get(f"recal:{adm_id}")
    return raw == "1" if raw else False

# Session Storage to accumulate readings for 15-minute averaging
# Structure: { admissionId: {
#    "start_time": timestamp,
#    "readings": [],
#    "is_first_reading": True,
#    "needs_recalibration": False,
#    "current_interval": 900,
#    "last_confirmation_time": 0
# } }
SESSION_STORAGE = {}

# ─────────────────────────────────────────────────────────────────────────────
# 2. Smart Device Detection
# ─────────────────────────────────────────────────────────────────────────────
_DEVICE_INPUT_MAP = {
    "NISO101": DEVICE_BERRYMED,
    "NISO103": DEVICE_CHECKME,
    "NISO204": DEVICE_NISO204,
}

_DEVICE_HZ_MAP = {
    DEVICE_NISO204:  200,
    DEVICE_CHECKME:  125,
    DEVICE_BERRYMED: 200,
}

def _detect_device(json_data):
    if "bp" in json_data:
        return DEVICE_LS06
    return _DEVICE_INPUT_MAP.get(json_data.get("deviceName", ""), "UNKNOWN")

# ─────────────────────────────────────────────────────────────────────────────
# 3. The DSP Janitor (Routing & Strict 120Hz Resampling)
# ─────────────────────────────────────────────────────────────────────────────
def _preprocess_signal(raw_pleth, source_hz, target_hz, device_type):
    """Routes to specific hardware math, returns (clean_signal, sqi_info)."""
    if not raw_pleth:
        return [], {}

    sqi_info = {"score": 1.0, "valid": True, "flag": "GOOD"} # Default

    # 1. BERRYMED (NISO 101)
    if device_type == DEVICE_BERRYMED:
        filtered = PROCESSORS["BERRYMED"].process(raw_pleth)
        # Normalize to 0-1 to match CHECKME and NISO204 output scale
        filtered_arr = np.array(filtered, dtype=float)
        p2, p98 = np.percentile(filtered_arr, [2, 98])
        if p98 - p2 > 1e-6:
            clean_signal = np.clip((filtered_arr - p2) / (p98 - p2), 0, 1)
        else:
            clean_signal = filtered_arr
        
    # 2. CHECKME (NISO 103)
    elif device_type == DEVICE_CHECKME:
        # CheckmeProcessor returns (normalised, t_axis, denoised, filtered, quality_info)
        results = PROCESSORS["CHECKME"].process_data(raw_pleth)
        clean_signal = results[0]
        sqi_info = results[4]
        
    # 3. STANDARD (NISO 204)
    elif device_type == DEVICE_NISO204:
        clean_signal, sqi_info = PROCESSORS["NISO204"].process(raw_pleth)
        
    else:
        clean_signal = np.array(raw_pleth, dtype=float)

    # FINAL STEP FOR ALL DEVICES: Resample to exact target_hz (120 Hz)
    if source_hz != target_hz and len(clean_signal) > 0:
        target_length = int(len(clean_signal) * (target_hz / source_hz))
        return list(signal.resample(clean_signal, target_length)), sqi_info
        
    return list(clean_signal), sqi_info

# ─────────────────────────────────────────────────────────────────────────────
# 4. Signal Analysis Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_aix(ppg, fs=120):
    """SDPTG b/a ratio as Augmentation Index proxy. Returns None if signal insufficient."""
    ppg = np.array(ppg, dtype=float)
    if len(ppg) < fs * 3:
        return None
    rng = ppg.max() - ppg.min()
    if rng < 1e-6:
        return None
    ppg_n = (ppg - ppg.min()) / rng
    wl = max(5, min(15, (len(ppg_n) // 20) * 2 + 1))
    ppg_s = signal.savgol_filter(ppg_n, window_length=wl, polyorder=3)
    peaks, _ = signal.find_peaks(ppg_s, distance=int(fs * 0.4), height=0.4)
    if len(peaks) < 3:
        return None
    d2 = np.diff(ppg_s, n=2)
    ratios = []
    for pk in peaks[:-1]:
        s = max(0, pk - int(fs * 0.05))
        e = min(len(d2), pk + int(fs * 0.4))
        seg = d2[s:e]
        if len(seg) < 8:
            continue
        h = len(seg) // 2
        a_val = float(seg[:h].min())
        b_val = float(seg[max(0, h // 4): h + h // 4 + 1].max())
        if abs(a_val) > 1e-9:
            ratios.append(b_val / a_val)
    return float(np.median(ratios)) if len(ratios) >= 2 else None


def _apply_bp_offset(sbp, dbp):
    """Empirical bias correction for no-reference mode via smooth interpolation."""
    offset_s = float(np.interp(sbp, [85.0, 100.0, 110.0], [25.0, 15.0, 0.0]))
    offset_d = float(np.interp(dbp, [45.0, 60.0], [15.0, 0.0]))
    return (int(round(float(np.clip(sbp + offset_s, *cfg.BP_SBP_LIMITS)))),
            int(round(float(np.clip(dbp + offset_d, *cfg.BP_DBP_LIMITS)))))


def _compute_trends(session):
    """Returns (trending, morphology_change) from delta history and AIx history."""
    trending   = False
    morphology = "stable"
    deltas   = session.get("deltas", [])
    baseline = session.get("baseline_delta")
    if baseline and len(deltas) >= 3:
        recent_mean = float(np.mean([d[0] for d in deltas[-5:]]))
        if abs(recent_mean - baseline[0]) >= 10.0:
            trending = True
    aix_history = session.get("aix_history", [])
    aix_values  = session.get("aix_values",  [])
    if aix_history and len(aix_values) >= 3:
        current_aix = float(np.mean(aix_values[-5:]))
        shift = current_aix - aix_history[0]
        if shift > 0.15:
            morphology = "rising"
        elif shift < -0.15:
            morphology = "falling"
    return trending, morphology


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main Processing Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def process_vitals(json_data):
    """Takes JSON, identifies device, routes to DSP, and returns AI predictions."""
    adm_id = json_data.get("admissionId") or json_data.get("PatId") or json_data.get("deviceID") or json_data.get("BLEDeviceID", "UNKNOWN_PATIENT")
    device_type = _detect_device(json_data)

    # --- PATHWAY 1: THE BP CUFF (Update Reference Storage) ---
    if device_type == DEVICE_LS06 or "bp" in json_data:
        bp_block = json_data.get("bp", {}) or json_data
        sys_val    = int(bp_block.get("BPSYS",  bp_block.get("bpSystolic",  bp_block.get("BPSystolic",  0))))
        dia_val    = int(bp_block.get("BPDIA",  bp_block.get("bpDiastolic", bp_block.get("BPDiastolic", 0))))
        cuff_error = int(bp_block.get("BP_ERROR", 0))

        if cuff_error != 0:
            return {
                "status": "error",
                "admissionId": adm_id,
                "message": f"Cuff hardware error (BP_ERROR={cuff_error}). Reading {sys_val}/{dia_val} rejected."
            }

        if sys_val >= 400 or dia_val >= 200:
            return {
                "status": "error",
                "admissionId": adm_id,
                "message": f"Device error sentinel received ({sys_val}/{dia_val}). Cuff reading ignored."
            }
        
        # --- CASE 2: REFERENCE COOLDOWN (Error Protection) ---
        # If we successfully confirmed a calibration recently, ignore random/mistaken cuff readings
        now = time.time()
        if adm_id in SESSION_STORAGE:
            session = SESSION_STORAGE[adm_id]
            last_confirm = session.get("last_confirmation_time", 0)
            if (now - last_confirm) < 900:
                return {
                    "status": "ignored",
                    "admissionId": adm_id,
                    "message": f"Reference ignored. System is in 15-minute stability cooldown ({(now - last_confirm)/60:.1f}m elapsed)."
                }

        # Store this as the ground truth for this patient
        _ref_write(adm_id, sys_val, dia_val, json_data.get("epochTime", 0))
        
        # Every time a new reference comes, we want to trigger an immediate 
        # AI confirmation on the next pleth packet, bypassing the 15-min timer.
        if adm_id not in SESSION_STORAGE:
            _needs_recal = _recal_read(adm_id)
            SESSION_STORAGE[adm_id] = {
                "start_time": time.time(),
                "readings": [],
                "is_first_reading": {},
                "needs_recalibration": _needs_recal,
                "current_interval": 1200 if _needs_recal else 900,
                "last_confirmation_time": 0,
                "deltas": [],
                "baseline_delta": None,
                "aix_values": [],
                "aix_history": [],
            }
        else:
            SESSION_STORAGE[adm_id]["is_first_reading"] = {}

        return {
            "status": "success",
            "deviceType": "REFERENCE_UPDATE",
            "admissionId": adm_id,
            "bp": {
                "bpSystolic": sys_val,
                "bpDiastolic": dia_val,
                "BP_ERROR": cuff_error
            },
            "message": f"Reference BP for {adm_id} updated to {sys_val}/{dia_val}. AI will use this for calibration."
        }

    # --- PATHWAY 2: CONTINUOUS MONITORS (For AI) ---
    _meta = {k: json_data[k] for k in [
        "category", "patientName", "assignedDoctor", "deviceId",
        "epochTime", "seqNum", "seqPart", "spo2", "device",
        "cgroup", "pgroup", "facilityId", "patientId",
    ] if k in json_data}

    if device_type == "UNKNOWN":
        return {**_meta, "status": "error", "admissionId": adm_id,
                "message": f"Unknown deviceName: {json_data.get('deviceName')}. Expected NISO101/NISO103/NISO204."}

    actual_hz = _DEVICE_HZ_MAP.get(device_type, 120)
    raw_pleth = json_data.get("pleth", {}).get("PLETH", [])

    # 1. Clean and enforce 120Hz
    model_ready_pleth, sqi_info = _preprocess_signal(raw_pleth, actual_hz, 120, device_type)

    if isinstance(sqi_info, dict) and not sqi_info.get("valid", True):
        sqi_flag = sqi_info.get("flag", "INVALID")
        return {**_meta,
            "status": "poor_signal",
            "admissionId": adm_id,
            "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
            "deviceType": "BP_SPO2",
            "timestamp": int(time.time()),
            "sqi": sqi_info,
            "message": f"Poor signal quality ({sqi_flag}). Skipping inference."
        }

    if len(model_ready_pleth) > 0:
        _arr = np.array(model_ready_pleth)
        _tail = _arr[len(_arr) // 3:]
        _tail_std = float(_tail.std())
        _tail_amp = float(_tail.max() - _tail.min())
        _peaks, _ = signal.find_peaks(_arr, distance=36, height=float(_arr.mean()))
        _is_flat = (_tail_std < 0.01 or _tail_amp < 0.05) or len(_peaks) < 5
        if _is_flat:
            _flat_sqi = {"score": 0.0, "valid": False, "flag": "FLAT_SIGNAL"}
            return {**_meta,
                "status": "poor_signal",
                "admissionId": adm_id,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
                "deviceType": "BP_SPO2",
                "timestamp": int(time.time()),
                "sqi": _flat_sqi,
                "message": "Flat signal detected. No physiological waveform present."
            }

    if len(model_ready_pleth) < 120:
        return {**_meta, "status": "error", "admissionId": adm_id, "message": "Signal too short for AI inference."}

    pleth_out = [round(float(x), 6) for x in model_ready_pleth]

    # 2. Extract demographics for the AI
    age = json_data.get("Age", 35)
    gender = json_data.get("Gender", "Male")
    bmi = json_data.get("BMI", 24)

    # 3. Call the AI Engine
    try:
        # Retrieve the latest reference BP for this patient from Redis
        patient_ref = _ref_read(adm_id)

        ai_results = ai_engine.analyze(
            pleth_array=model_ready_pleth,
            fs=120,
            age=age,
            gender=gender,
            bmi=bmi,
            adm_id=adm_id,
            device_type=device_type
        )

        # 4. CONSOLE LOGGING (STDOUT) - Always visible, not the formal payload
        bp_valid = "sbp" in ai_results
        sbp_pred = int(round(float(ai_results.get("sbp", 120)))) if bp_valid else 120
        dbp_pred = int(round(float(ai_results.get("dbp", 80)))) if bp_valid else 80
        hb_pred  = ai_results.get("hb", "N/A") if bp_valid else "N/A"
        glu_pred = ai_results.get("glucose", "N/A") if bp_valid else "N/A"

        # Raw model delta for trend tracking (always computed before any correction)
        model_cat_raw  = ai_results.get("category", "normal")
        base_s_raw, base_d_raw = cfg.BP_CATEGORY_BASES.get(model_cat_raw, (118.0, 76.0))
        pkt_delta_s = (float(ai_results["sbp"]) - base_s_raw) if bp_valid else None
        pkt_delta_d = (float(ai_results["dbp"]) - base_d_raw) if bp_valid else None

        # AIx from this packet
        aix = _compute_aix(model_ready_pleth, fs=120)

        # Two-path BP correction
        ref_s = patient_ref.get("sbp", 0)
        ref_d = patient_ref.get("dbp", 0)
        if bp_valid:
            if ref_s > 0 and ref_d > 0:
                if ref_s < 90 or ref_d < 60:
                    ref_cat = "hypo"
                elif ref_s > 130 or ref_d > 80:
                    ref_cat = "hyper"
                else:
                    ref_cat = "normal"
                if model_cat_raw != ref_cat:
                    m_base_s, m_base_d = cfg.BP_CATEGORY_BASES.get(model_cat_raw, (118.0, 76.0))
                    r_base_s, r_base_d = cfg.BP_CATEGORY_BASES.get(ref_cat, (118.0, 76.0))
                    sbp_pred = int(round(float(np.clip(r_base_s + pkt_delta_s, *cfg.BP_SBP_LIMITS))))
                    dbp_pred = int(round(float(np.clip(r_base_d + pkt_delta_d, *cfg.BP_DBP_LIMITS))))
            else:
                sbp_pred, dbp_pred = _apply_bp_offset(sbp_pred, dbp_pred)

        print(f"[RT_LOG] Admission: {adm_id} | AI Estimate: {sbp_pred}/{dbp_pred} | Hb: {hb_pred} | Glu: {glu_pred}")

        # 5. INITIALIZE SESSION STATE
        now = time.time()
        
        if adm_id not in SESSION_STORAGE:
            _needs_recal = _recal_read(adm_id)
            SESSION_STORAGE[adm_id] = {
                "start_time": now,
                "readings": [],
                "is_first_reading": {},
                "needs_recalibration": _needs_recal,
                "current_interval": 1200 if _needs_recal else 900,
                "last_confirmation_time": 0,
                "deltas": [],
                "baseline_delta": None,
                "aix_values": [],
                "aix_history": [],
            }

        session = SESSION_STORAGE[adm_id]

        # 6. SMART CALIBRATION: first packet per device → immediate check, rest → 15-min window
        has_reference = patient_ref.get("sbp", 0) > 0 or patient_ref.get("dbp", 0) > 0
        is_immediate  = has_reference and session["is_first_reading"].get(device_type, True)

        if is_immediate and not bp_valid:
            return {**_meta,
                "status": "poor_signal",
                "admissionId": adm_id,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
                "deviceType": "BP_SPO2",
                "timestamp": int(now),
                "sqi": sqi_info,
                "message": "Poor signal on first packet. Waiting for clean signal before calibration check."
            }

        if is_immediate:
            ref_sbp = patient_ref.get("sbp", 0)
            ref_dbp = patient_ref.get("dbp", 0)

            if ref_sbp > 0 or ref_dbp > 0:
                sbp_mismatch = (ref_sbp > 0 and abs(ref_sbp - sbp_pred) >= 15)
                dbp_mismatch = (ref_dbp > 0 and abs(ref_dbp - dbp_pred) >= 15)
            else:
                sbp_mismatch = (sbp_pred > 140 or sbp_pred < 90)
                dbp_mismatch = (dbp_pred > 90  or dbp_pred < 60)

            if sbp_mismatch or dbp_mismatch:
                session["needs_recalibration"] = True
                session["is_first_reading"][device_type] = False
                _recal_write(adm_id, True)
                alert_msg = (
                    f"Physiological Alert: AI={sbp_pred}/{dbp_pred} outside normal range."
                    if ref_sbp == 0 and ref_dbp == 0
                    else f"Initial Calibration Mismatch: Cuff={ref_sbp}/{ref_dbp}, AI={sbp_pred}/{dbp_pred}."
                )
                alert_payload = {**_meta,
                    "status": "alert",
                    "admissionId": adm_id,
                    "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
                    "deviceType": "BP_SPO2",
                    "timestamp": int(now),
                    "message": alert_msg,
                    "bp": {
                        "bpSystolic": sbp_pred, "bpDiastolic": dbp_pred,
                        "estimated_sbp": sbp_pred, "estimated_dbp": dbp_pred,
                        "category": ai_results.get("category", "Unknown"),
                        "trend": ai_results.get("trend", {"trend": "Stable ->", "slope": 0.0, "readings": 0}),
                        "reference_sbp": ref_sbp,
                        "reference_dbp": ref_dbp
                    },
                    "sqi": sqi_info,
                    "pleth": {"PLETH": pleth_out},
                }
                if hb_pred != "N/A":
                    alert_payload["hemoglobin"] = hb_pred
                if glu_pred != "N/A":
                    alert_payload["glucose"] = glu_pred
                return alert_payload

        # First reading matched — subsequent packets from this device go to the window
        if is_immediate:
            session["is_first_reading"][device_type] = False

        # 7. OUTPUT LOGIC (15-min window accumulation)
        if bp_valid:
            session["readings"].append((sbp_pred, dbp_pred, hb_pred, glu_pred))
            if pkt_delta_s is not None:
                session["deltas"].append((pkt_delta_s, pkt_delta_d))
        if aix is not None:
            session["aix_values"].append(aix)

        target_interval = session.get("current_interval", 900)
        elapsed = now - session["start_time"]

        if not is_immediate and elapsed < target_interval:
            trending, morphology = _compute_trends(session)
            if not bp_valid:
                return {**_meta,
                    "status": "poor_signal",
                    "admissionId": adm_id,
                    "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
                    "deviceType": "BP_SPO2",
                    "timestamp": int(now),
                    "sqi": sqi_info,
                    "trending": trending,
                    "morphology_change": morphology,
                    "pleth": {"PLETH": pleth_out},
                    "message": "Poor signal during accumulation window. Waiting for valid packet."
                }
            acc = {**_meta,
                "status": "accumulating",
                "admissionId": adm_id,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
                "deviceType": "BP_SPO2",
                "elapsed_seconds": int(elapsed),
                "target_seconds": int(target_interval),
                "bp": {
                    "bpSystolic": sbp_pred,
                    "bpDiastolic": dbp_pred,
                    "estimated_sbp": sbp_pred,
                    "estimated_dbp": dbp_pred,
                    "category": ai_results.get("category", "Unknown"),
                },
                "sqi": sqi_info,
                "trending": trending,
                "morphology_change": morphology,
                "pleth": {"PLETH": pleth_out},
                "message": f"Stability period in progress ({int(elapsed)}/{int(target_interval)}s)."
            }
            if hb_pred != "N/A":
                acc["hemoglobin"] = hb_pred
            if glu_pred != "N/A":
                acc["glucose"] = glu_pred
            return acc

        # Timer Expired -> Finalise window trends, then calculate averages
        trending, morphology = _compute_trends(session)

        aix_vals = session.get("aix_values", [])
        if aix_vals:
            session["aix_history"].append(float(np.mean(aix_vals)))
        session["aix_values"] = []

        window_deltas = session.get("deltas", [])
        if window_deltas and session.get("baseline_delta") is None:
            session["baseline_delta"] = (
                float(np.mean([d[0] for d in window_deltas])),
                float(np.mean([d[1] for d in window_deltas])),
            )
        session["deltas"] = []

        readings = session["readings"]

        if not readings:
            session["start_time"] = now
            return {**_meta,
                "status": "poor_signal",
                "admissionId": adm_id,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
                "deviceType": "BP_SPO2",
                "timestamp": int(now),
                "sqi": sqi_info,
                "pleth": {"PLETH": pleth_out},
                "message": "No valid signal in window. All packets had poor signal quality."
            }

        avg_sbp = int(np.mean([r[0] for r in readings]))
        avg_dbp = int(np.mean([r[1] for r in readings]))

        if not has_reference and aix_vals and window_deltas:
            mean_aix     = float(np.mean(aix_vals))
            mean_delta_s = float(np.mean([d[0] for d in window_deltas]))
            mean_delta_d = float(np.mean([d[1] for d in window_deltas]))
            aix_cat = "hyper" if mean_aix > 0.0 else ("hypo" if mean_aix < -0.3 else "normal")
            base_s, base_d = cfg.BP_CATEGORY_BASES[aix_cat]
            avg_sbp = int(round(float(np.clip(base_s + mean_delta_s, *cfg.BP_SBP_LIMITS))))
            avg_dbp = int(round(float(np.clip(base_d + mean_delta_d, *cfg.BP_DBP_LIMITS))))
        
        valid_hb = [r[2] for r in readings if isinstance(r[2], (int, float)) and r[2] != "N/A"]
        valid_glu = [r[3] for r in readings if isinstance(r[3], (int, float)) and r[3] != "N/A"]
        avg_hb  = round(float(np.mean(valid_hb)), 1) if valid_hb else "N/A"
        avg_glu = int(np.mean(valid_glu)) if valid_glu else "N/A"

        # Final Check: Mismatch on the 15-minute average (Both SBP and DBP)
        ref_sbp = patient_ref.get("sbp", 0)
        ref_dbp = patient_ref.get("dbp", 0)
        final_status = "success"
        final_msg = "Immediate initial reading confirmed." if is_immediate else "15-minute averaged clinical payload."
        
        if ref_sbp > 0 or ref_dbp > 0:
            sbp_avg_mismatch = (ref_sbp > 0 and abs(ref_sbp - avg_sbp) >= 15)
            dbp_avg_mismatch = (ref_dbp > 0 and abs(ref_dbp - avg_dbp) >= 15)
        else:
            sbp_avg_mismatch = (avg_sbp > 140 or avg_sbp < 90)
            dbp_avg_mismatch = (avg_dbp > 90  or avg_dbp < 60)
        
        if sbp_avg_mismatch or dbp_avg_mismatch:
            final_status = "alert"
            final_msg = f"Averaged Calibration Mismatch: Cuff={ref_sbp}/{ref_dbp}, AI_Avg={avg_sbp}/{avg_dbp}."
            session["needs_recalibration"] = True
            _recal_write(adm_id, True)
        else:
            session["needs_recalibration"] = False
            _recal_write(adm_id, False)

        # Reset Session State
        # Only start the 15-minute cooldown timer if there is NO mismatch (successful calibration).
        # We do NOT start it during a mismatch, so clinical staff can take a new cuff reading immediately.
        if not session["needs_recalibration"]:
            session["last_confirmation_time"] = now

        if session["needs_recalibration"]:
            session["current_interval"] = 1200
        else:
            session["current_interval"] = 900

        session["start_time"] = now
        session["readings"] = []

        # 8. Construct Final FORMAL JSON
        final_payload = {**_meta,
            "status": final_status,
            "admissionId": adm_id,
            "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
            "deviceType": "BP_SPO2",
            "timestamp": int(now),
            "reading_count": len(readings),
            "bp": {
                "bpSystolic": avg_sbp,
                "bpDiastolic": avg_dbp,
                "estimated_sbp": avg_sbp,
                "estimated_dbp": avg_dbp,
                "category": ai_results.get("category", "Unknown"),
                "trend": ai_results.get("trend", {"trend": "Stable ->", "slope": 0.0, "readings": 0}),
                "BP_ERROR": 0
            },
            "sqi": sqi_info,
            "trending": trending,
            "morphology_change": morphology,
            "pleth": {"PLETH": pleth_out},
            "message": final_msg
        }
        if avg_hb != "N/A":
            final_payload["hemoglobin"] = avg_hb
        if avg_glu != "N/A":
            final_payload["glucose"] = avg_glu

        return final_payload
    except Exception as e:
        if adm_id in SESSION_STORAGE:
            SESSION_STORAGE[adm_id]["readings"] = []
            SESSION_STORAGE[adm_id]["start_time"] = time.time()
        return {"status": "error", "admissionId": adm_id, "message": f"AI Inference Failed: {str(e)}"}

# ─────────────────────────────────────────────────────────────────────────────
# 5. CLI Test Block
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Vitals Standalone Pipeline")
    parser.add_argument("input_file", help="Path to JSON file")
    args = parser.parse_args()

    if os.path.exists(args.input_file):
        with open(args.input_file, 'r') as f:
            data = json.load(f)
        result = process_vitals(data)
        print(json.dumps(result, indent=4))