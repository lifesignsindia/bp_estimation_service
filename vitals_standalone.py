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

# ─── Legacy AI Engine — Hb / glucose ONLY (BP is v7, see below) ───────────────
print("[AI]   Loading legacy Hb/glucose models into memory...")
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

# Hb recalibration — the Hb model systematically OVER-READS by a roughly constant offset
# (cohort mean estimate ~12.8 vs lab ~9.1 g/dL). Subtracting it recenters the output and cut
# mean error 3.93 -> 2.40 g/dL (~39%) in analysis. This is a cohort-derived LEVEL correction,
# not a per-patient measurement (pleth morphology carries little true Hb signal), so Hb stays
# a screening indicator. Retune if the patient population changes; set 0.0 to disable.
# Glucose is intentionally left uncorrected — no recalibration improved it.
HB_BIAS_G_DL = 3.5
HB_OUTPUT_LIMITS = (3.0, 20.0)

_DEVICE_NAME_MAP = {
    "BERRYMED": "NISO101",
    "CHECKME":  "NISO103",
    "NISO204":  "NISO204",
}

# ─── Redis ────────────────────────────────────────────────────────────────────

def _validate_redis_configuration():
    cloud_redis = isinstance(cfg.REDIS_HOST, str) and (
        cfg.REDIS_HOST.endswith(".cache.amazonaws.com") or
        cfg.REDIS_HOST.endswith(".redis.cache.windows.net") or
        cfg.REDIS_HOST.endswith(".redis.cache.azure.com")
    )
    if not getattr(cfg, "REDIS_URL", None):
        if cfg.REDIS_HOST in {"localhost", "127.0.0.1"}:
            print("[REDIS] WARNING: REDIS_HOST is set to localhost. In container deployment, set REDIS_HOST=redis or REDIS_URL=redis://redis:6379.")
            print("[REDIS] If Redis is external, configure REDIS_HOST/REDIS_PORT or REDIS_URL correctly.")
            sys.stdout.flush()
        elif cloud_redis and not getattr(cfg, "REDIS_TLS", False):
            print("[REDIS] WARNING: Cloud Redis host detected but REDIS_TLS=false. If this is AWS ElastiCache or another managed service, use REDIS_TLS=true or REDIS_URL=rediss://<host>:6379.")
            sys.stdout.flush()


_validate_redis_configuration()
print(f"[REDIS] Connecting to {cfg.REDIS_HOST}:{cfg.REDIS_PORT}... {'(TLS)' if getattr(cfg, 'REDIS_TLS', False) else ''}")
if getattr(cfg, "REDIS_URL", None):
    print(f"[REDIS] Using REDIS_URL={cfg.REDIS_URL}")
if getattr(cfg, "REDIS_TLS", False) and not getattr(cfg, "REDIS_URL", None):
    print("[REDIS] TLS mode enabled via REDIS_TLS=true")
sys.stdout.flush()
try:
    redis_kwargs = {
        "host": cfg.REDIS_HOST,
        "port": cfg.REDIS_PORT,
        "decode_responses": True,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    }
    if getattr(cfg, "REDIS_PASSWORD", None):
        redis_kwargs["password"] = cfg.REDIS_PASSWORD
    if getattr(cfg, "REDIS_TLS", False):
        redis_kwargs["ssl"] = True

    _redis = redis_lib.Redis(**redis_kwargs)
    _redis.ping()
    print("[REDIS] Connected OK.")
    sys.stdout.flush()
except Exception as e:
    print("[REDIS] FATAL: Cannot connect to Redis. Verify REDIS_HOST/REDIS_PORT or REDIS_URL and ensure Redis is reachable from this container.")
    print(f"[REDIS] Current config: REDIS_HOST={cfg.REDIS_HOST}, REDIS_PORT={cfg.REDIS_PORT}, REDIS_URL={getattr(cfg, 'REDIS_URL', None)}, REDIS_TLS={getattr(cfg, 'REDIS_TLS', False)}")
    if isinstance(cfg.REDIS_HOST, str) and cfg.REDIS_HOST.endswith(".cache.amazonaws.com") and not getattr(cfg, "REDIS_TLS", False):
        print("[REDIS] NOTE: AWS ElastiCache endpoints typically require TLS. Set REDIS_TLS=true or REDIS_URL=rediss://<host>:6379.")
    print(f"[REDIS] Error: {type(e).__name__}: {e}")
    if hasattr(e, "__cause__") and e.__cause__:
        print(f"[REDIS] Cause: {type(e.__cause__).__name__}: {e.__cause__}")
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

def _primary_write(adm_id, device_type):
    _redis.setex(f"primary:{adm_id}", cfg.REDIS_REF_TTL, device_type)

def _primary_read(adm_id):
    raw = _redis.get(f"primary:{adm_id}")
    return raw if raw else None

# ─── v7 BP engine ─────────────────────────────────────────────────────────────
# BP is owned by v7 (delta-from-cuff model, 15-minute wall-clock slots, latched alert).
# Its per-admission state lives in Redis next to the cuff reference, so it survives
# restarts and is shared across pods. The legacy engine above is kept ONLY for Hb and
# glucose. See v7_engine.py and docs/V7_PIPELINE.md.
try:
    from v7_engine import V7Engine, WINDOW_SEC as V7_WINDOW_SEC
    v7_engine = V7Engine(_redis)
except Exception as e:
    print(f"[V7]   FATAL: v7 engine failed to load — {e}")
    sys.stdout.flush()
    sys.exit(1)

# ─── Session helpers (Redis-backed, survives restarts & works across pods) ────
SESSION_TTL = 7200  # 2 hours — longer than any possible 15-min session

def _session_read(adm_id):
    """Read session from Redis. Returns None if no session exists."""
    raw = _redis.get(f"session:{adm_id}")
    return json.loads(raw) if raw else None

def _session_write(adm_id, session):
    """Write session to Redis with TTL."""
    _redis.setex(f"session:{adm_id}", SESSION_TTL, json.dumps(session))

def _session_delete(adm_id):
    """Remove session from Redis (e.g. on hard reset)."""
    _redis.delete(f"session:{adm_id}")

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

def _resolve_facility(obj, _depth=0):
    """Find a facility id in ANY form, for the temporary facility gate — top-level or
    nested, any case/separator variant (facilityId / facility_id / FACILITY-ID / …),
    a plain `facility` string, or a nested `facility` object carrying an id. Returns
    the first facility-like value found, else None."""
    if not isinstance(obj, dict) or _depth > 5:
        return None
    for k, v in obj.items():
        kn = str(k).lower().replace("_", "").replace("-", "").replace(" ", "")
        if kn in ("facilityid", "facility", "facilitycode"):
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):                      # e.g. {"facility": {"id": "CF..."}}
                for ik, iv in v.items():
                    ikn = str(ik).lower().replace("_", "").replace("-", "").replace(" ", "")
                    if ikn in ("id", "facilityid", "code", "value") and isinstance(iv, str) and iv.strip():
                        return iv.strip()
    for v in obj.values():                               # recurse into nested blocks
        if isinstance(v, dict):
            r = _resolve_facility(v, _depth + 1)
            if r:
                return r
    return None


def _detect_device(json_data):
    """
    Resolve the device type from either nested or top-level fields.

    The payload format is not fully consistent across vendors, so we accept all of:
      - device.deviceName
      - device.deviceType
      - deviceName
      - deviceType
      - device (when it is a plain string)
    """
    def _clean(value):
        return value.strip() if isinstance(value, str) else None

    candidates = []

    # Nested device block first (common for NISO101 / NISO103 payloads)
    device_block = json_data.get("device")
    if isinstance(device_block, dict):
        candidates.extend([
            device_block.get("deviceName"),
            device_block.get("deviceType"),
        ])
    elif isinstance(device_block, str):
        candidates.append(device_block)

    # Top-level fallbacks
    candidates.extend([
        json_data.get("deviceName"),
        json_data.get("deviceType"),
        json_data.get("device"),
    ])

    # Explicitly accept the common vendor aliases used by the pipeline.
    for raw in candidates:
        value = _clean(raw)
        if not value:
            continue
        if value == "NISO206":
            return DEVICE_LS06
        if value in _DEVICE_INPUT_MAP:
            return _DEVICE_INPUT_MAP[value]
        if value in {DEVICE_BERRYMED, DEVICE_CHECKME, DEVICE_NISO204}:
            return value

    # No recognised device marker — fall back to bp field presence (LS06 cuff)
    if "bp" in json_data:
        return DEVICE_LS06
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
        from scipy.signal import medfilt as _medfilt
        raw_arr  = np.array(raw_pleth, dtype=float)
        despiked = _medfilt(raw_arr, kernel_size=5)
        p2, p98  = np.percentile(despiked, [2, 98])
        if p98 - p2 > 1e-6:
            clean_signal = np.clip((despiked - p2) / (p98 - p2), 0, 1)
        else:
            clean_signal = despiked
        
    # 2. CHECKME (NISO 103)
    elif device_type == DEVICE_CHECKME:
        # CheckmeProcessor returns (normalised, t_axis, denoised, filtered, quality_info).
        # Use the auto-sensed source_hz (not the fixed 125) so the filter matches.
        results = PROCESSORS["CHECKME"].process_data(raw_pleth, override_fs=source_hz)
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
        resampled = signal.resample(np.array(clean_signal, dtype=float), target_length)
        return list(resampled), sqi_info
        
    return list(clean_signal), sqi_info

# ─────────────────────────────────────────────────────────────────────────────
# 4. Helpers for the v7 path
# ─────────────────────────────────────────────────────────────────────────────

def _to_seconds(t):
    """Device / Nexus timestamps arrive in seconds or milliseconds. Normalise to seconds."""
    if not isinstance(t, (int, float)) or t <= 0:
        return None
    return float(t) / 1000.0 if t > 1e11 else float(t)


def _epoch_ts(json_data):
    """Time of this pleth epoch, used for the 15-minute slot key. The device epochTime when
    it is plausible (within a day of now), else the wall clock."""
    t = _to_seconds(json_data.get("epochTime"))
    now = time.time()
    if t is None or abs(t - now) > 86400:
        return now
    return t


def _category(sbp, dbp):
    """Same thresholds the pipeline has always used for the reference category."""
    if sbp < 90 or dbp < 60:
        return "hypo"
    if sbp > 140 or dbp > 90:
        return "hyper"
    return "normal"


def _legacy_hb_glu(model_ready_pleth, age, gender, bmi, adm_id, device_type):
    """Hb and glucose from the legacy engine. Its BP output is discarded — v7 owns BP.
    Returns (hb, glucose), either may be None."""
    try:
        r = ai_engine.analyze(pleth_array=model_ready_pleth, fs=120, age=age, gender=gender,
                              bmi=bmi, adm_id=adm_id, device_type=device_type)
    except Exception as e:
        print(f"[HBGLU] adm={adm_id} | legacy engine failed: {e}")
        sys.stdout.flush()
        return None, None
    hb, glu = r.get("hb"), r.get("glucose")
    # Recenter Hb by the known systematic over-read (see HB_BIAS_G_DL).
    hb = round(float(np.clip(float(hb) - HB_BIAS_G_DL, *HB_OUTPUT_LIMITS)), 1) if isinstance(hb, (int, float)) else None
    glu = int(round(float(glu))) if isinstance(glu, (int, float)) else None
    return hb, glu
# ─────────────────────────────────────────────────────────────────────────────
# 5. Main Processing Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def _niso101_pr_from_pr_all(json_data):
    """NISO101 (BerryMed) ONLY: the incoming `spo2` block carries a per-sample pulse-rate
    array under `PR_ALL`. Set `spo2.PR` = mean of the physiologically-plausible
    (25-220 bpm) samples. `PR_ALL` and everything else in `spo2` (including the SpO2
    samples) are left EXACTLY as received; no other device is affected."""
    sp = json_data.get("spo2")
    if not isinstance(sp, dict):
        return
    arr = sp.get("PR_ALL")
    if isinstance(arr, list):
        vals = [float(x) for x in arr if isinstance(x, (int, float)) and 25 <= float(x) <= 220]
        if vals:
            sp["PR"] = int(round(sum(vals) / len(vals)))


def process_vitals(json_data):
    """Takes JSON, identifies device, routes to DSP, and returns AI predictions."""
    adm_id = json_data.get("admissionId") or json_data.get("PatId") or json_data.get("deviceID") or json_data.get("BLEDeviceID", "UNKNOWN_PATIENT")

    # ── TEMPORARY FACILITY GATE ───────────────────────────────────────────────
    # Work ONLY for the ls.gncl facility (CF1315821527). Any other facility →
    # return None: do nothing and emit no packet at all. Configurable via
    # EBP_ALLOWED_FACILITY (set empty to disable). REMOVE after the trial.
    _allowed_facility = os.getenv("EBP_ALLOWED_FACILITY", "CF1315821527,CF557841749,CF1398828720")
    if _allowed_facility:
        _allowed_set = {f.strip() for f in _allowed_facility.split(",") if f.strip()}
        _fac = _resolve_facility(json_data)
        if _fac not in _allowed_set:
            return None

    device_type = _detect_device(json_data)

    # NISO101 (BerryMed) ONLY: the spo2 block carries a per-sample pulse-rate array
    # under `PR_ALL`. Add `spo2.PR` = mean of the plausible (25-220 bpm) samples;
    # `PR_ALL` and the spo2/SpO2 samples are left untouched. NISO103 and every other
    # device are left exactly as they were before the PR changes.
    if device_type == DEVICE_BERRYMED:
        _niso101_pr_from_pr_all(json_data)

    # --- PATHWAY 1: THE BP CUFF (Update Reference Storage) ---
    if device_type == DEVICE_LS06:
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
        
        # --- CASE 2: REFERENCE HANDLING ---
        now = time.time()
        session = _session_read(adm_id)

        # Read OLD reference before overwriting — needed to detect if value changed.
        prev_ref = _ref_read(adm_id)
        # Did the cuff value actually change vs the stored reference?
        ref_changed = (abs(prev_ref.get("sbp", 0) - sys_val) >= 5 or
                       abs(prev_ref.get("dbp", 0) - dia_val) >= 5)
        # A MANUAL reference (staff-entered) is deliberate; so is any genuinely NEW cuff
        # value. Accept those IMMEDIATELY — do not sit in the 15-min stability cooldown.
        _is_manual = bool(json_data.get("isManual")) or (json_data.get("deviceName") == "MANUAL")

        # The cooldown now ONLY suppresses repeated / IDENTICAL re-sends of the same value
        # (Nexus re-broadcasts the same manual reference); this is what prevents the
        # infinite loop of immediate checks and repeated ALERT outputs. A changed or
        # manual reference is always taken.
        if session and not ref_changed and not _is_manual:
            last_confirm = session.get("last_confirmation_time", 0)
            if (now - last_confirm) < 900:
                return {
                    "status": "ignored",
                    "admissionId": adm_id,
                    "message": f"Duplicate reference ignored (value unchanged, {(now - last_confirm)/60:.1f}m into stability window)."
                }

        # Store this as the ground truth for this patient
        _ref_write(adm_id, sys_val, dia_val, json_data.get("epochTime", 0))
        print(f"[REF]  Reference BP received | admissionId={adm_id} | SBP={sys_val} DBP={dia_val} | manual={_is_manual} changed={ref_changed}")
        sys.stdout.flush()

        if session is None:
            _needs_recal = _recal_read(adm_id)
            session = {
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
        elif ref_changed:
            # New cuff reading from staff → trigger immediate re-check
            print(f"[REF]  Reference changed {prev_ref.get('sbp')}/{prev_ref.get('dbp')} -> {sys_val}/{dia_val} | triggering re-check")
            sys.stdout.flush()
            session["is_first_reading"] = {}
        # else: same reference repeated — keep session state, no re-check needed

        _session_write(adm_id, session)

        return {
            "status": "ignored",
            "admissionId": adm_id,
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

    # --- PRIMARY DEVICE LOCK (NISO101 preferred) ---
    # NISO101 (BERRYMED) is always the main BP-estimation device: whenever a 101 packet
    # arrives it claims/takes over the primary slot. Any other PPG device (204/103) is used
    # for estimation only when 101 does NOT own the admission, so 204 readings never mix
    # into a 101 admission's 15-minute average. Once 101 owns the admission there is no
    # handover back to another device (the lock persists even if 101 goes silent). The
    # normal 1800-sample minimum still applies to whichever device is primary.
    _primary = _primary_read(adm_id)
    if device_type == DEVICE_BERRYMED:
        if _primary != DEVICE_BERRYMED:
            _primary_write(adm_id, DEVICE_BERRYMED)
        _primary = DEVICE_BERRYMED
    elif _primary is None:
        _primary_write(adm_id, device_type)
        _primary = device_type
    elif _primary != device_type:
        print(f"[LOCK] adm={adm_id} | device={device_type} ignored | admission locked to primary={_primary}")
        sys.stdout.flush()
        return {**_meta, "status": "ignored", "admissionId": adm_id,
                "deviceName": _DEVICE_NAME_MAP.get(device_type, device_type),
                "deviceType": "BP_SPO2",
                "message": f"Device {_DEVICE_NAME_MAP.get(device_type, device_type)} ignored. "
                           f"Admission locked to primary device {_DEVICE_NAME_MAP.get(_primary, _primary)}."}

    actual_hz = _DEVICE_HZ_MAP.get(device_type, 120)
    pleth_obj = json_data.get("pleth", {}) or {}
    raw_pleth = (
        pleth_obj.get("plethWave")
        or pleth_obj.get("PLETH")
        or pleth_obj.get("plethwave")
        or pleth_obj.get("pleth_wave")
        or []
    )

    # FIX #1 — NISO103 signed-int8 wraparound. CHECKME sends the pleth as SIGNED int8,
    # but the true signal is UNSIGNED 0-255. Any true value > 127 wraps to a negative
    # (e.g. 130 -> -126), turning pulse PEAKS into deep troughs and making the AI read BP
    # far too low — worst at high BP, where more peaks cross 127. Re-read as unsigned
    # (x + 256 for negatives) so the waveform shape, and thus the estimate, are correct.
    # NISO103/CHECKME only; a no-op on clean epochs and untouched for other devices.
    if device_type == DEVICE_CHECKME and isinstance(raw_pleth, list):
        raw_pleth = [(x + 256 if isinstance(x, (int, float)) and x < 0 else x)
                     for x in raw_pleth]

    # CHECKME (NISO103) epochs are 30s but the real sample rate varies by firmware
    # (~100-120 Hz). Auto-sense it from the sample count instead of assuming 125 Hz,
    # so the bandpass / beat-timing math matches the actual signal.
    if device_type == DEVICE_CHECKME and raw_pleth:
        sensed_hz = round(len(raw_pleth) / 30)
        if 80 <= sensed_hz <= 200:
            actual_hz = sensed_hz

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
        print(f"[SQI]  adm={adm_id} | device={device_type} | samples={len(_arr)} | std={_tail_std:.4f} | amp={_tail_amp:.4f} | peaks={len(_peaks)} | flat={_is_flat}")
        sys.stdout.flush()
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

    # 2. Demographics — only the legacy Hb / glucose engine uses them
    age = json_data.get("Age", 35)
    gender = json_data.get("Gender", "Male")
    bmi = json_data.get("BMI", 24)

    now = time.time()
    dev_name = _DEVICE_NAME_MAP.get(device_type, device_type)
    common = {**_meta, "admissionId": adm_id, "deviceName": dev_name,
              "deviceType": "BP_SPO2", "timestamp": int(now)}

    try:
        # 3. Hb / glucose — legacy engine on the 120 Hz cleaned signal (BP output ignored)
        hb_pred, glu_pred = _legacy_hb_glu(model_ready_pleth, age, gender, bmi, adm_id, device_type)

        # 4. BP — v7 on the RAW samples at the device rate. v7 does its own polarity
        #    correction, band-pass, beat ensemble and quality gate, exactly as validated.
        patient_ref = _ref_read(adm_id)
        ref_s = patient_ref.get("sbp", 0) or 0
        ref_d = patient_ref.get("dbp", 0) or 0
        ref_ts = _to_seconds(patient_ref.get("timestamp"))
        epoch_ts = _epoch_ts(json_data)
        if ref_s > 0:
            print(f"[REF]  Using reference | admissionId={adm_id} | SBP={ref_s} DBP={ref_d}")
            sys.stdout.flush()

        v7_in = [float(v) for v in raw_pleth if isinstance(v, (int, float, np.integer, np.floating))]
        res = v7_engine.score_epoch(adm_id, v7_in, fs=actual_hz, ts=epoch_ts,
                                    ref_sbp=ref_s, ref_dbp=ref_d, ref_ts=ref_ts,
                                    extras=(hb_pred, glu_pred))

        sqi_out = {**(sqi_info if isinstance(sqi_info, dict) else {}),
                   "v7_quality": res["quality"],
                   "n_beats": res["n_beats"],
                   "template_corr": res["template_corr"]}
        ev = res["epoch_value"]
        ow = res.get("open_window") or {}
        anchor = res.get("anchor") or {}
        print(f"[V7]   adm={adm_id} | {res['state']} | q={res['quality']} beats={res['n_beats']} "
              f"corr={res['template_corr']} | epoch={ev[0] if ev else '-'}/{ev[1] if ev else '-'} "
              f"| anchor={anchor.get('sbp', '-')}/{anchor.get('dbp', '-')} {res['calibrating']} "
              f"| slot good={ow.get('n_good', 0)}/{ow.get('n_epochs', 0)} | hb={hb_pred} glu={glu_pred} "
              f"| alert={res['alert'] or '-'}")
        sys.stdout.flush()

        win = res["window"]
        if win is None:
            # ── per-epoch states: LOGGED by the consumer, never published ──────────────
            if res["state"] == "no reference":
                return {**common, "status": "ignored", "sqi": sqi_out,
                        "message": "No reference cuff yet. v7 estimates change from a cuff, so nothing to anchor to."}
            if res["state"] == "calibrating":
                return {**common, "status": "accumulating", "sqi": sqi_out,
                        "elapsed_seconds": 0, "target_seconds": int(V7_WINDOW_SEC), "bp": {},
                        "message": f"Building v7 anchor ({res['calibrating']} good epochs after the cuff)."}
            if not res["good"]:
                return {**common, "status": "poor_signal", "sqi": sqi_out,
                        "message": f"Epoch dropped from the 15-minute slot (v7 quality={res['quality']}, "
                                   f"beats={res['n_beats']}, corr={res['template_corr']})."}
            elapsed = int(epoch_ts - (ow.get("since") or epoch_ts))
            return {**common, "status": "accumulating", "sqi": sqi_out,
                    "elapsed_seconds": elapsed, "target_seconds": int(V7_WINDOW_SEC),
                    "bp": {"estimated_sbp": ev[0], "estimated_dbp": ev[1]} if ev else {},
                    "message": f"15-minute slot accumulating ({ow.get('n_good', 0)} good epochs)."}

        # ── a 15-minute slot closed: the ONE payload per slot that reaches Kafka ─────────
        sbp, dbp = int(round(win["sbp"])), int(round(win["dbp"]))
        ref_sbp_i = int(round(anchor["sbp"])) if anchor.get("sbp") is not None else 0
        ref_dbp_i = int(round(anchor["dbp"])) if anchor.get("dbp") is not None else 0
        trend = res["trend"]
        label = trend.get("trend", "")
        morphology = "rising" if label.startswith("Rising") else ("falling" if label.startswith("Falling") else "stable")

        if win["alert"]:
            final_status = "alert"
            final_msg = (f"v7 alert {win['alert']} {'raised' if win['alert_new'] else 'active'}: "
                         f"15-min median {sbp}/{dbp} vs cuff {ref_sbp_i}/{ref_dbp_i} "
                         f"(latched until a new cuff).")
            print(f"[ALERT] adm={adm_id} | v7_median={sbp}/{dbp} | cuff={ref_sbp_i}/{ref_dbp_i} | {win['alert']} | new={win['alert_new']}")
            sys.stdout.flush()
        else:
            final_status = "success"
            final_msg = (f"15-minute v7 median ({win['n_good']} good of {win['n_epochs']} epochs, "
                         f"{'established' if win['established'] else 'first window'}).")

        final_payload = {**common,
            "status": final_status,
            "reading_count": win["n_good"],
            "confidence": "HIGH" if win["established"] else "LOW",
            "bp": {
                "estimated_sbp": sbp,
                "estimated_dbp": dbp,
                "category": _category(sbp, dbp),
                "trend": trend,
                "reference_sbp": ref_sbp_i,
                "reference_dbp": ref_dbp_i,
                "BP_ERROR": 0
            },
            "alert": win["alert"],
            "sqi": sqi_out,
            "trending": bool(win["hot"]),
            "morphology_change": morphology,
            "window": {"start": int(win["start"]), "end": int(win["end"]),
                       "good_epochs": win["n_good"], "epochs": win["n_epochs"],
                       "established": bool(win["established"])},
            "pleth": {"PLETH": pleth_out},
            "message": final_msg
        }
        if win["hb"] is not None:
            final_payload["hemoglobin"] = win["hb"]
        if win["glucose"] is not None:
            final_payload["glucose"] = win["glucose"]
        print(f"[RT_LOG] Admission: {adm_id} | v7 15-min: {sbp}/{dbp} | Hb: {win['hb']} | Glu: {win['glucose']} | {final_status}")
        sys.stdout.flush()
        return final_payload
    except Exception as e:
        # Skip the bad packet. v7 state was saved by the engine before anything could raise
        # here, so the open slot is preserved.
        print(f"[ERR] Packet processing failed for {adm_id}: {e} -- packet skipped, window preserved.")
        sys.stdout.flush()
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
