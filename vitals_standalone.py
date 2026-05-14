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

# Initialize the AI Engine globally so it only loads into memory ONCE
print("Loading AI Models into memory...")
ai_engine = VitalInferenceEngine()
print("AI Engine Ready.")

# Initialize Processors globally to maintain filter state (zi) across data packets
# This eliminates "startup transients" or "spikes" at the beginning of each 30s block.
PROCESSORS = {
    "BERRYMED": BerryMedProcessor(fs=200, lowcut=0.5, highcut=8.0, order=4),
    "CHECKME":  CheckmeProcessor(fs=125),
    "NISO204":  NISO204Processor(kernel_size=5)
}

# Constants
DEVICE_NISO204  = "NISO204"
DEVICE_CHECKME  = "CHECKME"   
DEVICE_BERRYMED = "BERRYMED"  
DEVICE_LS06     = "LS06"      

# ─── Redis — Reference BP store (keyed by admissionId) ──────────────────────
_redis = redis_lib.Redis(
    host=cfg.REDIS_HOST,
    port=cfg.REDIS_PORT,
    password=cfg.REDIS_PASSWORD,
    decode_responses=True,
    socket_connect_timeout=5
)
_redis.ping()  # fail fast on startup if Redis is unreachable

def _ref_write(adm_id, sbp, dbp, timestamp):
    _redis.setex(
        f"ref:{adm_id}",
        cfg.REDIS_REF_TTL,
        json.dumps({"sbp": sbp, "dbp": dbp, "timestamp": timestamp})
    )

def _ref_read(adm_id):
    raw = _redis.get(f"ref:{adm_id}")
    return json.loads(raw) if raw else {"sbp": 0, "dbp": 0}

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
def _detect_device(json_data):
    """Safely distinguishes between continuous monitors and spot-check cuffs."""
    device_block = json_data.get("device", {}) or {}
    dtype = str(device_block.get("deviceType", "")).upper()
    dev_name = str(json_data.get("DeviceName", "")).upper()

    # ROUTE A: THE CUFF (Spot-Check Ground Truth)
    if "LS06" in dtype or "LEPU" in dtype or "bp" in json_data:
        return DEVICE_LS06

    # ROUTE B: THE CONTINUOUS MONITORS
    if dev_name == "NISO204" and "Pleth" in json_data:
        return DEVICE_NISO204

    if "CHECKME" in dtype or "NISO103" in dtype:
        return DEVICE_CHECKME
        
    if "BERRY" in dtype or "NISO101" in dtype:
        return DEVICE_BERRYMED

    return "UNKNOWN"

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
# 4. Main Processing Entry Point 
# ─────────────────────────────────────────────────────────────────────────────
def process_vitals(json_data):
    """Takes JSON, identifies device, routes to DSP, and returns AI predictions."""
    adm_id = json_data.get("admissionId") or json_data.get("PatId") or json_data.get("deviceID") or json_data.get("BLEDeviceID", "UNKNOWN_PATIENT")
    device_type = _detect_device(json_data)

    # --- PATHWAY 1: THE BP CUFF (Update Reference Storage) ---
    if device_type == DEVICE_LS06 or "bp" in json_data:
        bp_block = json_data.get("bp", {}) or json_data # Handle different structures
        sys_val = int(bp_block.get("bpSystolic", bp_block.get("BPSystolic", 0)))
        dia_val = int(bp_block.get("bpDiastolic", bp_block.get("BPDiastolic", 0)))
        cuff_error = bp_block.get("bpErrorMsg", "")

        # Reject hardware error sentinels (e.g., 404/200)
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
            SESSION_STORAGE[adm_id] = {
                "start_time": time.time(),
                "readings": [],
                "is_first_reading": True,
                "needs_recalibration": False,
                "current_interval": 900,
                "last_confirmation_time": 0
            }
        else:
            SESSION_STORAGE[adm_id]["is_first_reading"] = True

        return {
            "status": "success",
            "device_type": "REFERENCE_UPDATE",
            "admissionId": adm_id,
            "bp": {
                "bpSystolic": sys_val,
                "bpDiastolic": dia_val,
                "bpErrorMsg": cuff_error if cuff_error else "None"
            },
            "message": f"Reference BP for {adm_id} updated to {sys_val}/{dia_val}. AI will use this for calibration."
        }

    # --- PATHWAY 2: CONTINUOUS MONITORS (For AI) ---
    raw_pleth = []
    actual_hz = 120 
    
    if device_type == DEVICE_NISO204:
        raw_pleth = json_data.get("Pleth", []) or json_data.get("PlethWave", [])
        actual_hz = json_data.get("FS", 200) 
        
    elif device_type in [DEVICE_CHECKME, DEVICE_BERRYMED]:
        raw_pleth = json_data.get("pleth", {}).get("plethWave", [])
        actual_hz = 125 if device_type == DEVICE_CHECKME else 200
    else:
        return {"status": "error", "message": f"Unknown device format for {device_type}."}

    # 1. Clean and enforce 120Hz
    model_ready_pleth, sqi_info = _preprocess_signal(raw_pleth, actual_hz, 120, device_type)

    if len(model_ready_pleth) < 120:
        return {"status": "error", "message": "Signal too short for AI inference."}

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
            adm_id=adm_id
        )

        # 4. CONSOLE LOGGING (STDOUT) - Always visible, not the formal payload
        bp_valid = "sbp" in ai_results
        sbp_pred = int(round(float(ai_results.get("sbp", 120)))) if bp_valid else 120
        dbp_pred = int(round(float(ai_results.get("dbp", 80)))) if bp_valid else 80
        hb_pred  = ai_results.get("hb", "N/A") if bp_valid else "N/A"
        glu_pred = ai_results.get("glucose", "N/A") if bp_valid else "N/A"

        print(f"[RT_LOG] Admission: {adm_id} | AI Estimate: {sbp_pred}/{dbp_pred} | Hb: {hb_pred} | Glu: {glu_pred}")

        # 5. INITIALIZE SESSION STATE
        now = time.time()
        
        if adm_id not in SESSION_STORAGE:
            SESSION_STORAGE[adm_id] = {
                "start_time": now,
                "readings": [],
                "is_first_reading": True,
                "needs_recalibration": False,
                "current_interval": 900,
                "last_confirmation_time": 0
            }
        
        session = SESSION_STORAGE[adm_id]

        # 6. SMART CALIBRATION: Mismatch Detection
        # Rule: Alerts are only immediate for the FIRST reading.
        # Otherwise, they wait for the 15-minute average to confirm stability.
        # Skip calibration entirely if the AI had no valid signal — fallback values must never trigger alerts.
        is_immediate = session["is_first_reading"] or session["needs_recalibration"]

        if is_immediate and not bp_valid:
            return {
                "status": "poor_signal",
                "admissionId": adm_id,
                "device_type": device_type,
                "timestamp": int(now),
                "sqi": sqi_info,
                "message": "Poor signal on first/recalibration packet. Waiting for clean signal before calibration check."
            }

        if is_immediate:
            ref_sbp = patient_ref.get("sbp", 0)
            ref_dbp = patient_ref.get("dbp", 0)
            
            # Check if either SBP or DBP is off by more than 15 mmHg
            sbp_mismatch = (ref_sbp > 0 and abs(ref_sbp - sbp_pred) > 15)
            dbp_mismatch = (ref_dbp > 0 and abs(ref_dbp - dbp_pred) > 15)
            
            if sbp_mismatch or dbp_mismatch:
                session["needs_recalibration"] = True
                alert_payload = {
                    "status": "alert",
                    "admissionId": adm_id,
                    "device_type": device_type,
                    "timestamp": int(now),
                    "message": f"Initial Calibration Mismatch: Cuff={ref_sbp}/{ref_dbp}, AI={sbp_pred}/{dbp_pred}.",
                    "bp": {
                        "bpSystolic": sbp_pred, "bpDiastolic": dbp_pred,
                        "estimated_sbp": sbp_pred, "estimated_dbp": dbp_pred,
                        "category": ai_results.get("category", "Unknown"),
                        "trend": ai_results.get("trend", {"trend": "Stable ->", "slope": 0.0, "readings": 0}),
                        "reference_sbp": ref_sbp,
                        "reference_dbp": ref_dbp
                    },
                    "sqi": sqi_info
                }
                if hb_pred != "N/A":
                    alert_payload["hemoglobin"] = hb_pred
                if glu_pred != "N/A":
                    alert_payload["glucose"] = glu_pred
                return alert_payload

        # 7. OUTPUT LOGIC (Immediate for First, 15-min Average for rest)
        # Only accumulate real AI predictions — never store fallback values
        if bp_valid:
            session["readings"].append((sbp_pred, dbp_pred, hb_pred, glu_pred))
        
        # Check if current window interval has elapsed (Standard=900, Recovery=1200)
        target_interval = session.get("current_interval", 900)
        elapsed = now - session["start_time"]
        
        if not is_immediate and elapsed < target_interval:
            return {
                "status": "accumulating", 
                "admissionId": adm_id, 
                "elapsed_seconds": int(elapsed), 
                "target_seconds": int(target_interval),
                "message": f"Stability period in progress ({int(elapsed)}/{int(target_interval)}s)."
             }

        # Timer Expired -> Calculate Averages
        readings = session["readings"]

        if not readings:
            session["start_time"] = now
            return {
                "status": "poor_signal",
                "admissionId": adm_id,
                "device_type": device_type,
                "timestamp": int(now),
                "sqi": sqi_info,
                "message": "No valid signal in window. All packets had poor signal quality."
            }

        avg_sbp = int(np.mean([r[0] for r in readings]))
        avg_dbp = int(np.mean([r[1] for r in readings]))
        
        valid_hb = [r[2] for r in readings if isinstance(r[2], (int, float)) and r[2] != "N/A"]
        valid_glu = [r[3] for r in readings if isinstance(r[3], (int, float)) and r[3] != "N/A"]
        avg_hb  = round(float(np.mean(valid_hb)), 1) if valid_hb else "N/A"
        avg_glu = int(np.mean(valid_glu)) if valid_glu else "N/A"

        # Final Check: Mismatch on the 15-minute average (Both SBP and DBP)
        ref_sbp = patient_ref.get("sbp", 0)
        ref_dbp = patient_ref.get("dbp", 0)
        final_status = "success"
        final_msg = "Immediate initial reading confirmed." if is_immediate else "15-minute averaged clinical payload."
        
        sbp_avg_mismatch = (ref_sbp > 0 and abs(ref_sbp - avg_sbp) > 15)
        dbp_avg_mismatch = (ref_dbp > 0 and abs(ref_dbp - avg_dbp) > 15)
        
        if sbp_avg_mismatch or dbp_avg_mismatch:
            final_status = "alert"
            final_msg = f"Averaged Calibration Mismatch: Cuff={ref_sbp}/{ref_dbp}, AI_Avg={avg_sbp}/{avg_dbp}."
            session["needs_recalibration"] = True
        else:
            session["needs_recalibration"] = False

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
        session["is_first_reading"] = False
        # deliberately leaving needs_recalibration untouched so the alert state persists

        # 8. Construct Final FORMAL JSON
        final_payload = {
            "status": final_status,
            "admissionId": adm_id,
            "device_type": device_type,
            "timestamp": int(now),
            "reading_count": len(readings),
            "bp": {
                "bpSystolic": avg_sbp,
                "bpDiastolic": avg_dbp,
                "estimated_sbp": avg_sbp,
                "estimated_dbp": avg_dbp,
                "category": ai_results.get("category", "Unknown"),
                "trend": ai_results.get("trend", {"trend": "Stable ->", "slope": 0.0, "readings": 0}),
                "bpErrorMsg": "None"
            },
            "sqi": sqi_info,
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
        return {"status": "error", "message": f"AI Inference Failed: {str(e)}"}

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