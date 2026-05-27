"""
test_pipeline.py — Replicated vitals pipeline for the test dashboard.

Identical logic to vitals_standalone.py with three differences:
  1. patient_name used as key instead of admissionId
  2. Reference BP stored in-memory (no Redis)
  3. Returns (result, clean_signal) so the app can plot the filtered waveform
"""

import sys
import os
import time
import numpy as np
from scipy import signal as scipy_signal

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import config as cfg
from processors.niso101_processor import PPGFilter as BerryMedProcessor
from processors.niso103_processor import PlethProcessor as CheckmeProcessor
from processors.niso204_processor import NISO204Processor
from inference_engine import VitalInferenceEngine

print("[TEST] Loading AI models...")
ai_engine = VitalInferenceEngine()
print("[TEST] AI engine ready.")

# Separate processor instances — do not share state with the main pipeline
PROCESSORS = {
    "BERRYMED": BerryMedProcessor(fs=200, lowcut=0.5, highcut=8.0, order=4),
    "CHECKME":  CheckmeProcessor(fs=125),
    "NISO204":  NISO204Processor(kernel_size=5),
}

DEVICE_NISO204  = "NISO204"
DEVICE_CHECKME  = "CHECKME"
DEVICE_BERRYMED = "BERRYMED"
DEVICE_LS06     = "LS06"

_DEVICE_NAME_MAP = {
    "BERRYMED": "NISO101",
    "CHECKME":  "NISO103",
    "NISO204":  "NISO204",
}

# In-memory stores (no Redis)
REFERENCE_STORAGE = {}   # { patient_name: {"sbp": int, "dbp": int, "timestamp": int} }
SESSION_STORAGE   = {}   # same structure as vitals_standalone


# ── Reference helpers ────────────────────────────────────────────────────────

def _ref_write(patient_name, sbp, dbp, timestamp):
    REFERENCE_STORAGE[patient_name] = {"sbp": sbp, "dbp": dbp, "timestamp": timestamp}


def _ref_read(patient_name):
    return REFERENCE_STORAGE.get(patient_name, {"sbp": 0, "dbp": 0})


# ── Reset helpers ─────────────────────────────────────────────────────────────

def reset_patient(patient_name):
    """Full reset — clears reference, session, and all in-memory state."""
    REFERENCE_STORAGE.pop(patient_name, None)
    SESSION_STORAGE.pop(patient_name, None)


def reset_reference(patient_name):
    """Clears reference BP and restarts the session window. History kept."""
    REFERENCE_STORAGE.pop(patient_name, None)
    if patient_name in SESSION_STORAGE:
        SESSION_STORAGE[patient_name].update({
            "is_first_reading": {},
            "needs_recalibration": False,
            "readings": [],
            "start_time": time.time(),
            "last_confirmation_time": 0,
            "deltas": [],
            "baseline_delta": None,
            "aix_values": [],
            "aix_history": [],
            "last_drift_alert_time": 0,
        })


# ── Signal analysis helpers (identical to vitals_standalone) ─────────────────

def _compute_aix(ppg, fs=120):
    ppg = np.array(ppg, dtype=float)
    if len(ppg) < fs * 3:
        return None
    rng = ppg.max() - ppg.min()
    if rng < 1e-6:
        return None
    ppg_n = (ppg - ppg.min()) / rng
    wl = max(5, min(15, (len(ppg_n) // 20) * 2 + 1))
    ppg_s = scipy_signal.savgol_filter(ppg_n, window_length=wl, polyorder=3)
    peaks, _ = scipy_signal.find_peaks(ppg_s, distance=int(fs * 0.4), height=0.4)
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
    offset_s = float(np.interp(sbp, [85.0, 100.0, 110.0], [25.0, 15.0, 0.0]))
    offset_d = float(np.interp(dbp, [45.0, 60.0], [15.0, 0.0]))
    return (int(round(float(np.clip(sbp + offset_s, *cfg.BP_SBP_LIMITS)))),
            int(round(float(np.clip(dbp + offset_d, *cfg.BP_DBP_LIMITS)))))


def _compute_trends(session):
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


# ── Device detection ─────────────────────────────────────────────────────────

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
    # Check deviceName first — NISO101/103/204 are PPG devices, never cuff.
    # Matches vitals_standalone logic exactly: deviceName takes priority over bp field.
    device_name = json_data.get("deviceName", "")
    if device_name in _DEVICE_INPUT_MAP:
        return _DEVICE_INPUT_MAP[device_name]
    # No recognised deviceName — fall back to bp field presence (LS06 cuff)
    if "bp" in json_data:
        return DEVICE_LS06
    return "UNKNOWN"


# ── Signal preprocessing (identical to vitals_standalone) ────────────────────

def _preprocess_signal(raw_pleth, source_hz, target_hz, device_type):
    """Returns (model_ready_signal, sqi_info, clean_signal_for_plot)."""
    if not raw_pleth:
        return [], {}, []

    sqi_info    = {"score": 1.0, "valid": True, "flag": "GOOD"}
    plot_signal = None   # set per-device; falls back to clean_signal below

    if device_type == DEVICE_BERRYMED:
        from scipy.signal import medfilt as _medfilt
        raw_arr  = np.array(raw_pleth, dtype=float)
        despiked = _medfilt(raw_arr, kernel_size=5)
        p2, p98  = np.percentile(despiked, [2, 98])
        if p98 - p2 > 1e-6:
            clean_signal = np.clip((despiked - p2) / (p98 - p2), 0, 1)
        else:
            clean_signal = despiked

    elif device_type == DEVICE_CHECKME:
        results      = PROCESSORS["CHECKME"].process_data(raw_pleth)
        clean_signal = results[0]
        sqi_info     = results[4]

    elif device_type == DEVICE_NISO204:
        clean_signal, sqi_info = PROCESSORS["NISO204"].process(raw_pleth)

    else:
        clean_signal = np.array(raw_pleth, dtype=float)

    if plot_signal is None:
        plot_signal = list(clean_signal)

    if source_hz != target_hz and len(clean_signal) > 0:
        target_length = int(len(clean_signal) * (target_hz / source_hz))
        resampled     = list(scipy_signal.resample(clean_signal, target_length))
        return resampled, sqi_info, plot_signal

    return list(clean_signal), sqi_info, plot_signal


# ── Main entry point ──────────────────────────────────────────────────────────

def process_vitals_test(json_data, patient_name):
    """
    Same logic as vitals_standalone.process_vitals.
    Returns (result_dict, clean_signal_for_plot).
    """
    device_type = _detect_device(json_data)

    # ── CUFF PATHWAY ─────────────────────────────────────────────────────────
    if device_type == DEVICE_LS06:
        bp_block  = json_data.get("bp", {}) or json_data
        sys_val   = int(bp_block.get("BPSYS",  bp_block.get("bpSystolic",  bp_block.get("BPSystolic",  0))))
        dia_val   = int(bp_block.get("BPDIA",  bp_block.get("bpDiastolic", bp_block.get("BPDiastolic", 0))))
        cuff_err  = int(bp_block.get("BP_ERROR", 0))

        if cuff_err != 0:
            return {
                "status": "error",
                "patient": patient_name,
                "deviceType": DEVICE_LS06,
                "message": f"Cuff hardware error (BP_ERROR={cuff_err}). Reading {sys_val}/{dia_val} rejected."
            }, []

        if sys_val >= 400 or dia_val >= 200:
            return {
                "status": "error",
                "patient": patient_name,
                "deviceType": DEVICE_LS06,
                "message": f"Device error sentinel ({sys_val}/{dia_val}). Ignored."
            }, []

        if not (60 <= sys_val <= 250) or not (30 <= dia_val <= 150) or dia_val >= sys_val:
            return {
                "status": "error",
                "patient": patient_name,
                "deviceType": DEVICE_LS06,
                "message": f"Physiologically invalid BP ({sys_val}/{dia_val}). Rejected."
            }, []

        now = time.time()
        if patient_name in SESSION_STORAGE:
            last_confirm = SESSION_STORAGE[patient_name].get("last_confirmation_time", 0)
            if (now - last_confirm) < 900:
                return {
                    "status": "ignored",
                    "patient": patient_name,
                    "deviceType": DEVICE_LS06,
                    "message": f"Cooldown active ({(now - last_confirm)/60:.1f}m elapsed). Reference ignored."
                }, []

        _ref_write(patient_name, sys_val, dia_val, json_data.get("epochTime", 0))

        if patient_name not in SESSION_STORAGE:
            SESSION_STORAGE[patient_name] = {
                "start_time": now, "readings": [], "is_first_reading": {},
                "needs_recalibration": False, "current_interval": 900,
                "last_confirmation_time": 0,
                "deltas": [], "baseline_delta": None,
                "aix_values": [], "aix_history": [],
                "last_packet_time": {}, "last_drift_alert_time": 0,
            }
        else:
            SESSION_STORAGE[patient_name]["is_first_reading"] = {}
            SESSION_STORAGE[patient_name]["last_drift_alert_time"] = 0

        return {
            "status": "success",
            "patient": patient_name,
            "deviceType": "REFERENCE_UPDATE",
            "bp": {"bpSystolic": sys_val, "bpDiastolic": dia_val,
                   "BP_ERROR": cuff_err},
            "message": f"Reference BP updated to {sys_val}/{dia_val}."
        }, []

    # ── PLETH PATHWAY ─────────────────────────────────────────────────────────
    _meta = {k: json_data[k] for k in [
        "category", "patientName", "assignedDoctor", "deviceId",
        "epochTime", "seqNum", "seqPart", "spo2", "device",
        "cgroup", "pgroup", "facilityId", "patientId",
    ] if k in json_data}

    if device_type == "UNKNOWN":
        return {**_meta, "status": "error", "patient": patient_name,
                "message": f"Unknown deviceName: {json_data.get('deviceName')}. Expected NISO101/NISO103/NISO204."}, []

    actual_hz = _DEVICE_HZ_MAP.get(device_type, 120)
    raw_pleth = json_data.get("pleth", {}).get("PLETH", [])

    model_ready, sqi_info, plot_signal = _preprocess_signal(
        raw_pleth, actual_hz, 120, device_type
    )

    if isinstance(sqi_info, dict) and not sqi_info.get("valid", True):
        sqi_flag = sqi_info.get("flag", "INVALID")
        return {**_meta,
            "status": "poor_signal", "patient": patient_name,
            "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2",
            "timestamp": int(time.time()), "sqi": sqi_info,
            "message": f"Poor signal quality ({sqi_flag}). Skipping inference.",
        }, plot_signal

    if len(model_ready) > 0:
        _arr = np.array(model_ready)
        _tail = _arr[len(_arr) // 3:]
        _tail_std = float(_tail.std())
        _tail_amp = float(_tail.max() - _tail.min())
        _peaks, _ = scipy_signal.find_peaks(_arr, distance=36, height=float(_arr.mean()))
        _is_flat = (_tail_std < 0.01 or _tail_amp < 0.05) or len(_peaks) < 5
        if _is_flat:
            _flat_sqi = {"score": 0.0, "valid": False, "flag": "FLAT_SIGNAL"}
            return {**_meta,
                "status": "poor_signal", "patient": patient_name,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2",
                "timestamp": int(time.time()), "sqi": _flat_sqi,
                "message": "Flat signal detected. No physiological waveform present.",
            }, plot_signal

    if len(model_ready) < 120:
        return {**_meta, "status": "error", "patient": patient_name,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2",
                "message": "Signal too short for AI inference."}, plot_signal

    age    = json_data.get("Age", 35)
    gender = json_data.get("Gender", "Male")
    bmi    = json_data.get("BMI", 24)

    try:
        patient_ref = _ref_read(patient_name)

        ai_results = ai_engine.analyze(
            pleth_array=model_ready, fs=120,
            age=age, gender=gender, bmi=bmi,
            adm_id=patient_name, device_type=device_type
        )

        bp_valid = "sbp" in ai_results
        sbp_pred = int(round(float(ai_results.get("sbp", 120)))) if bp_valid else 120
        dbp_pred = int(round(float(ai_results.get("dbp", 80))))  if bp_valid else 80
        hb_pred  = ai_results.get("hb",      "N/A") if bp_valid else "N/A"
        glu_pred = ai_results.get("glucose",  "N/A") if bp_valid else "N/A"

        # Raw delta for trend tracking (before any correction)
        model_cat_raw  = ai_results.get("category", "normal")
        base_s_raw, base_d_raw = cfg.BP_CATEGORY_BASES.get(model_cat_raw, (118.0, 76.0))
        pkt_delta_s = (float(ai_results["sbp"]) - base_s_raw) if bp_valid else None
        pkt_delta_d = (float(ai_results["dbp"]) - base_d_raw) if bp_valid else None

        aix = _compute_aix(model_ready, fs=120)

        # Cross-category correction: if ref category differs from model category,
        # re-root the AI's delta onto the reference category base
        ref_s = patient_ref.get("sbp", 0)
        ref_d = patient_ref.get("dbp", 0)
        ai_sbp_raw = int(round(float(ai_results.get("sbp", 120)))) if bp_valid else sbp_pred
        ai_dbp_raw = int(round(float(ai_results.get("dbp",  80)))) if bp_valid else dbp_pred
        if bp_valid and ref_s > 0 and ref_d > 0:
            if ref_s < 90 or ref_d < 60:
                ref_cat = "hypo"
            elif ref_s > 140 or ref_d > 90:
                ref_cat = "hyper"
            else:
                ref_cat = "normal"
            if model_cat_raw != ref_cat:
                r_base_s, r_base_d = cfg.BP_CATEGORY_BASES.get(ref_cat, (118.0, 76.0))
                sbp_pred = int(round(float(np.clip(r_base_s + pkt_delta_s, *cfg.BP_SBP_LIMITS))))
                dbp_pred = int(round(float(np.clip(r_base_d + pkt_delta_d, *cfg.BP_DBP_LIMITS))))
                print(f"[CORR] Cross-cat | patient={patient_name} | AI_cat={model_cat_raw} ref_cat={ref_cat} | AI={ai_sbp_raw}/{ai_dbp_raw} → corrected={sbp_pred}/{dbp_pred}")
        old_sbp = sbp_pred
        old_dbp = dbp_pred

        now = time.time()

        if patient_name not in SESSION_STORAGE:
            SESSION_STORAGE[patient_name] = {
                "start_time": now, "readings": [], "is_first_reading": {},
                "needs_recalibration": False, "current_interval": 900,
                "last_confirmation_time": 0,
                "deltas": [], "baseline_delta": None,
                "aix_values": [], "aix_history": [],
                "last_packet_time": {}, "last_drift_alert_time": 0,
            }

        session = SESSION_STORAGE[patient_name]

        # Proposition 1: Gap Detection — re-arm first-reading check if gap ≥ 10 minutes
        _last_pkt = session.setdefault("last_packet_time", {}).get(device_type, 0)
        if _last_pkt > 0 and (now - _last_pkt) >= 600:
            session["is_first_reading"][device_type] = True
            session["readings"] = []
            session["last_drift_alert_time"] = 0
            print(f"[GAP] {patient_name} | {device_type} | gap={int(now - _last_pkt)}s ≥ 600s → re-arming first-reading check")
        session["last_packet_time"][device_type] = now

        has_reference = patient_ref.get("sbp", 0) > 0 or patient_ref.get("dbp", 0) > 0
        is_immediate  = has_reference and session["is_first_reading"].get(device_type, True)

        if is_immediate and not bp_valid:
            return {**_meta,
                "status": "poor_signal", "patient": patient_name,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2", "timestamp": int(now),
                "sqi": sqi_info,
                "message": "Poor signal on first packet. Waiting for clean signal."
            }, plot_signal

        if is_immediate:
            ref_sbp = patient_ref.get("sbp", 0)
            ref_dbp = patient_ref.get("dbp", 0)
            if ref_sbp > 0 or ref_dbp > 0:
                sbp_mismatch = ref_sbp > 0 and abs(ref_sbp - sbp_pred) >= 15
                dbp_mismatch = ref_dbp > 0 and abs(ref_dbp - dbp_pred) >= 15
            else:
                sbp_mismatch = (sbp_pred > 140 or sbp_pred < 90)
                dbp_mismatch = (dbp_pred > 90  or dbp_pred < 60)

            if sbp_mismatch or dbp_mismatch:
                session["needs_recalibration"] = True
                session["is_first_reading"][device_type] = False
                alert_msg = (
                    f"Physiological Alert: AI={sbp_pred}/{dbp_pred} outside normal range."
                    if ref_sbp == 0 and ref_dbp == 0
                    else f"Calibration Mismatch: Cuff={ref_sbp}/{ref_dbp}, AI={sbp_pred}/{dbp_pred}."
                )
                alert = {**_meta,
                    "status": "alert", "patient": patient_name,
                    "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2", "timestamp": int(now),
                    "message": alert_msg,
                    "bp": {
                        "bpSystolic": sbp_pred, "bpDiastolic": dbp_pred,
                        "estimated_sbp": sbp_pred, "estimated_dbp": dbp_pred,
                        "category": ai_results.get("category", "Unknown"),
                        "trend": ai_results.get("trend", {}),
                        "reference_sbp": ref_sbp, "reference_dbp": ref_dbp,
                    },
                    "old_method_bp": {"sbp": old_sbp, "dbp": old_dbp},
                    "sqi": sqi_info,
                    "pleth": {"PLETH": [round(float(x), 6) for x in model_ready]},
                }
                if hb_pred  != "N/A": alert["hemoglobin"] = hb_pred
                if glu_pred != "N/A": alert["glucose"]    = glu_pred
                return alert, plot_signal

        if is_immediate:
            session["is_first_reading"][device_type] = False

        if bp_valid:
            session["readings"].append((sbp_pred, dbp_pred, hb_pred, glu_pred))
            if pkt_delta_s is not None:
                session["deltas"].append((pkt_delta_s, pkt_delta_d))
        if aix is not None:
            session["aix_values"].append(aix)

        elapsed         = now - session["start_time"]
        target_interval = session.get("current_interval", 900)

        if not is_immediate and elapsed < target_interval:
            trending, morphology = _compute_trends(session)
            if not bp_valid:
                return {**_meta,
                    "status": "poor_signal", "patient": patient_name,
                    "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2", "timestamp": int(now),
                    "sqi": sqi_info,
                    "trending": trending, "morphology_change": morphology,
                    "message": f"Poor signal during accumulation ({int(elapsed)}/{int(target_interval)}s).",
                }, plot_signal
            # Proposition 3: Mid-window drift alert — 5-min cooldown, ≥3 readings required
            if ref_s > 0 and ref_d > 0 and len(session["readings"]) >= 3:
                _last_drift = session.get("last_drift_alert_time", 0)
                if (now - _last_drift) >= 300:
                    _avg_sbp_dev = float(np.mean([r[0] - ref_s for r in session["readings"]]))
                    _avg_dbp_dev = float(np.mean([r[1] - ref_d for r in session["readings"]]))
                    if abs(_avg_sbp_dev) > 15 or abs(_avg_dbp_dev) > 10:
                        session["last_drift_alert_time"] = now
                        _drift = {**_meta,
                            "status": "alert", "patient": patient_name,
                            "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2",
                            "timestamp": int(now),
                            "bp": {
                                "bpSystolic": sbp_pred, "bpDiastolic": dbp_pred,
                                "estimated_sbp": sbp_pred, "estimated_dbp": dbp_pred,
                                "category": ai_results.get("category", "Unknown"),
                                "reference_sbp": ref_s, "reference_dbp": ref_d,
                            },
                            "sqi": sqi_info,
                            "trending": trending, "morphology_change": morphology,
                            "pleth": {"PLETH": [round(float(x), 6) for x in model_ready]},
                            "message": f"Mid-window drift alert: avg deviation SBP={_avg_sbp_dev:+.1f} DBP={_avg_dbp_dev:+.1f} from reference {ref_s}/{ref_d}."
                        }
                        _drift["raw_model_bp"] = {"sbp": raw_sbp_model, "dbp": raw_dbp_model}
                        if hb_pred  != "N/A": _drift["hemoglobin"] = hb_pred
                        if glu_pred != "N/A": _drift["glucose"]    = glu_pred
                        return _drift, plot_signal

            acc = {**_meta,
                "status": "accumulating", "patient": patient_name,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2",
                "elapsed_seconds": int(elapsed), "target_seconds": int(target_interval),
                "message": f"Accumulating ({int(elapsed)}/{int(target_interval)}s).",
                "sqi": sqi_info,
                "trending": trending, "morphology_change": morphology,
                "pleth": {"PLETH": [round(float(x), 6) for x in model_ready]},
                "bp": {"bpSystolic": sbp_pred, "bpDiastolic": dbp_pred,
                       "estimated_sbp": sbp_pred, "estimated_dbp": dbp_pred},
                "old_method_bp": {"sbp": old_sbp, "dbp": old_dbp},
            }
            if hb_pred  != "N/A": acc["hemoglobin"] = hb_pred
            if glu_pred != "N/A": acc["glucose"]    = glu_pred
            return acc, plot_signal

        # Timer expired — finalise window trends, then compute averages
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
                "status": "poor_signal", "patient": patient_name,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2", "timestamp": int(now),
                "sqi": sqi_info,
                "message": "No valid signal collected in window.",
            }, plot_signal

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
        valid_hb  = [r[2] for r in readings if isinstance(r[2], (int, float))]
        valid_glu = [r[3] for r in readings if isinstance(r[3], (int, float))]
        avg_hb    = round(float(np.mean(valid_hb)),  1) if valid_hb  else "N/A"
        avg_glu   = int(np.mean(valid_glu))              if valid_glu else "N/A"

        ref_sbp = patient_ref.get("sbp", 0)
        ref_dbp = patient_ref.get("dbp", 0)

        if ref_sbp > 0 or ref_dbp > 0:
            sbp_avg_mismatch = ref_sbp > 0 and abs(ref_sbp - avg_sbp) >= 15
            dbp_avg_mismatch = ref_dbp > 0 and abs(ref_dbp - avg_dbp) >= 15
        else:
            sbp_avg_mismatch = (avg_sbp > 140 or avg_sbp < 90)
            dbp_avg_mismatch = (avg_dbp > 90  or avg_dbp < 60)

        if sbp_avg_mismatch or dbp_avg_mismatch:
            final_status = "alert"
            final_msg    = f"Averaged Mismatch: Cuff={ref_sbp}/{ref_dbp}, AI_Avg={avg_sbp}/{avg_dbp}."
            session["needs_recalibration"] = True
        else:
            final_status = "success"
            final_msg    = "15-minute averaged clinical payload."
            session["needs_recalibration"] = False

        if not session["needs_recalibration"]:
            session["last_confirmation_time"] = now

        session["current_interval"]     = 1200 if session["needs_recalibration"] else 900
        session["start_time"]           = now
        session["readings"]             = []
        session["last_drift_alert_time"] = 0

        final = {**_meta,
            "status": final_status, "patient": patient_name,
            "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2", "timestamp": int(now),
            "reading_count": len(readings),
            "bp": {
                "bpSystolic": avg_sbp, "bpDiastolic": avg_dbp,
                "estimated_sbp": avg_sbp, "estimated_dbp": avg_dbp,
                "category": ai_results.get("category", "Unknown"),
                "trend": ai_results.get("trend", {}),
                "BP_ERROR": 0,
            },
            "old_method_bp": {"sbp": old_sbp, "dbp": old_dbp},
            "sqi": sqi_info,
            "trending": trending, "morphology_change": morphology,
            "pleth": {"PLETH": [round(float(x), 6) for x in model_ready]},
            "message": final_msg,
        }
        if avg_hb  != "N/A": final["hemoglobin"] = avg_hb
        if avg_glu != "N/A": final["glucose"]    = avg_glu
        return final, plot_signal

    except Exception as e:
        if patient_name in SESSION_STORAGE:
            SESSION_STORAGE[patient_name]["readings"]   = []
            SESSION_STORAGE[patient_name]["start_time"] = time.time()
        return {"status": "error", "patient": patient_name, "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type), "deviceType": "BP_SPO2",
                "message": f"AI Inference Failed: {e}"}, plot_signal
