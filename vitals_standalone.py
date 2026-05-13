import json
import sys
import os
import argparse
import numpy as np
from scipy import signal

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

# Constants
DEVICE_NISO204  = "NISO204"
DEVICE_CHECKME  = "CHECKME"   
DEVICE_BERRYMED = "BERRYMED"  
DEVICE_LS06     = "LS06"      

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
    """Routes to specific hardware math, then resamples to target 120 Hz."""
    if not raw_pleth:
        return []

    # 1. BERRYMED (NISO 101)
    if device_type == DEVICE_BERRYMED:
        processor = BerryMedProcessor(fs=source_hz, lowcut=0.5, highcut=8.0, order=4)
        clean_signal = processor.process(raw_pleth)
        
    # 2. CHECKME (NISO 103)
    elif device_type == DEVICE_CHECKME:
        processor = CheckmeProcessor(fs=source_hz, target_fs=source_hz)
        clean_signal = processor.process_data(raw_pleth)[0] 
        
    # 3. STANDARD (NISO 204)
    elif device_type == DEVICE_NISO204:
        processor = NISO204Processor(kernel_size=5)
        clean_signal = processor.process(raw_pleth)
        
    else:
        clean_signal = np.array(raw_pleth, dtype=float)

    # FINAL STEP FOR ALL DEVICES: Resample to exact target_hz (120 Hz)
    if source_hz != target_hz:
        target_length = int(len(clean_signal) * (target_hz / source_hz))
        return list(signal.resample(clean_signal, target_length))
        
    return list(clean_signal)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Main Processing Entry Point 
# ─────────────────────────────────────────────────────────────────────────────
def process_vitals(json_data):
    """Takes JSON, identifies device, routes to DSP, and returns AI predictions."""
    device_type = _detect_device(json_data)

    # --- PATHWAY 1: THE BP CUFF (No AI) ---
    if device_type == DEVICE_LS06:
        bp_block = json_data.get("bp", {})
        sys_val = bp_block.get("bpSystolic", 0)
        dia_val = bp_block.get("bpDiastolic", 0)
        cuff_error = bp_block.get("bpErrorMsg", "")

        return {
            "status": "success",
            "device_type": "LS06_CUFF",
            "blood_pressure": f"{sys_val}/{dia_val}",
            "cuff_error": cuff_error if cuff_error else "None",
            "message": "Ground truth BP extracted directly from cuff. AI bypassed."
        }

    # --- PATHWAY 2: CONTINUOUS MONITORS (For AI) ---
    raw_pleth = []
    actual_hz = 120 
    
    if device_type == DEVICE_NISO204:
        raw_pleth = json_data.get("Pleth", [])
        actual_hz = json_data.get("FS", 200) 
        
    elif device_type in [DEVICE_CHECKME, DEVICE_BERRYMED]:
        raw_pleth = json_data.get("pleth", {}).get("plethWave", [])
        actual_hz = 125 if device_type == DEVICE_CHECKME else 200
    else:
        return {"status": "error", "message": "Unknown device format."}

    # 1. Clean and enforce 120Hz
    model_ready_pleth = _preprocess_signal(raw_pleth, actual_hz, 120, device_type)

    if len(model_ready_pleth) < 120:
        return {"status": "error", "message": "Signal too short for AI inference."}

    # 2. Extract demographics for the AI
    age = json_data.get("Age", 35)
    gender = json_data.get("Gender", "Male")
    bmi = json_data.get("BMI", 24)

    # 3. Call the AI Engine
    try:
        ai_results = ai_engine.analyze(
            pleth_array=model_ready_pleth, 
            fs=120, 
            age=age,
            gender=gender,
            bmi=bmi
        )

        return {
            "status": "success",
            "device_type": device_type,
            "blood_pressure": ai_results.get("bp", "Unknown"),
            "category": ai_results.get("category", "Unknown"),
            "hemoglobin": ai_results.get("hb", "Unknown"),
            "glucose": ai_results.get("glucose", "Unknown"),
            "message": "AI successfully calculated vitals from 120Hz waveform."
        }
    except Exception as e:
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