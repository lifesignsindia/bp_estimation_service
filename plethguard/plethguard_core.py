"""
pr_handling.py — Standalone robust pulse-rate handling.

Per-device dispatch (apply_pr_handling):
  * NISO101 (and default) — handle_niso101_pr: spike/outlier removal on the PR_ALL array
    (the ORIGINAL behaviour, unchanged).
  * NISO206               — handle_niso206_spo2: VALIDATE the incoming SpO2. The finger
    sensor can report a number even when it is off the finger or the patient is moving — a
    "false" SpO2. We don't trust the number; we check whether the conditions to measure it
    were met, using PERFUSION INDEX (pi) + the PLETH waveform only. If either says the reading
    is untrustworthy we flag the packet so the frontend hides the value (see verify_spo2).

Handles a noisy per-sample pulse-rate array and produces one clean value. Two parts:

  PART 1 — FILTER (clean_pr):     detect & drop noisy samples, then produce one value.
                                  Noise is caught two ways:
                                    (a) out-of-range   — outside 25-220 bpm (0, 255, …)
                                    (b) in-range spikes — outside the Tukey 1.5x IQR fence
                                        around the quartiles (a stray 200 among 78s)
                                  The clean value = MEAN of the surviving inliers.

  PART 2 — PR HANDLING (apply_pr_handling):  read `spo2.PR_ALL` from a payload, set
                                  `spo2.PR` = the clean value. `PR_ALL` and everything
                                  else are left untouched.

Standalone: no external deps, no pipeline imports. Run a demo:  python pr_handling.py
See README.md for details.
"""

PR_LOW = 25            # physiologically-plausible pulse-rate window (bpm)
PR_HIGH = 220
IQR_K = 1.5            # Tukey fence multiplier (1.5 = standard outlier test)


# ── PART 1: FILTER (noise detection → clean value) ───────────────────────────────
def clean_pr(samples, lo=PR_LOW, hi=PR_HIGH, return_stats=False):
    """Return one robust pulse rate from a noisy per-sample array.

    1. Drop out-of-range samples (< lo or > hi) — hard noise / sentinels.
    2. Drop in-range spikes: anything outside [Q1 - k*IQR, Q3 + k*IQR] (Tukey fence).
    3. Return the MEAN of the surviving inliers, rounded to int.

    If `return_stats`, returns (pr, stats) with stats = {n_in, n_range_ok, n_kept,
    n_dropped, noisy}. `pr` is None when nothing usable remains.
    """
    n_in = len(samples)
    vals = sorted(float(x) for x in samples
                  if isinstance(x, (int, float)) and lo <= float(x) <= hi)
    n_range_ok = len(vals)

    inliers = vals
    if n_range_ok >= 4:                                   # need a few points to judge spread
        q1 = vals[n_range_ok // 4]
        q3 = vals[(3 * n_range_ok) // 4]
        iqr = q3 - q1
        if iqr > 0:
            lo_f, hi_f = q1 - IQR_K * iqr, q3 + IQR_K * iqr
            kept = [x for x in vals if lo_f <= x <= hi_f]
            if kept:
                inliers = kept

    pr = int(round(sum(inliers) / len(inliers))) if inliers else None
    if not return_stats:
        return pr
    n_kept = len(inliers) if inliers else 0
    stats = {"n_in": n_in, "n_range_ok": n_range_ok, "n_kept": n_kept,
             "n_dropped": n_in - n_kept, "noisy": n_kept < n_in}
    return pr, stats


# ── PART 1b: independent rate from the PLETH waveform (catches SYSTEMATIC over-counts) ──
# clean_pr only removes outliers WITHIN PR_ALL; if the device over-counts every sample
# (e.g. counting the dicrotic notch), the whole array is inflated and passes the fence.
# The only way to catch that is to re-derive the rate from the raw pleth and cross-check.
PR_DISAGREE = 12          # bpm: device-vs-pleth gap that triggers a correction/flag


# KNOWN native pleth rates per device (do NOT infer from the data — that mis-counted NISO204).
# NISO101 = 200 Hz; NISO204 / NISO103(CHECKME) = 125 Hz; others fall back to cycleDuration.
# The signal is resampled to a fixed 125 Hz (same as the BP path) so the detector runs at one rate.
DET_FS = 125
def native_fs(device_type=None, cycle_duration=None, n_samples=None):
    d = str(device_type or "").upper()
    if "206" in d:            # NISO206 (finger SpO2): 25 Hz x 6 s epoch = 150 samples
        return 25
    if "101" in d:
        return 200
    if "204" in d or "103" in d or "CHECKME" in d:
        return 125
    if cycle_duration and n_samples:
        f = n_samples / float(cycle_duration)
        if 20 <= f <= 300:
            return f
    return 125


def pr_from_pleth(pleth, device_type=None, cycle_duration=None, fs=None):
    """True pulse rate from the raw pleth. Resamples from the KNOWN device rate to 125 Hz, then
    detects beats with DICROTIC-NOTCH rejection (prominence>=0.25) + refractory (>=0.4s, <=150
    bpm), rate = 60/median(beat interval). `fs` overrides the native lookup. Needs numpy+scipy."""
    try:
        import numpy as np
        from scipy.signal import butter, filtfilt, find_peaks, resample
    except Exception:
        return None
    a = np.asarray([x for x in pleth if isinstance(x, (int, float))], float)
    if len(a) < 30 or np.ptp(a) < 1e-6:
        return None
    fs0 = fs or native_fs(device_type, cycle_duration, len(a))
    m = int(round(len(a) * DET_FS / fs0))            # resample to the fixed 125 Hz detection rate
    if m < DET_FS * 2:                                # need >= 2 s of signal
        return None
    a = resample(a, m); fsd = DET_FS
    b, aa = butter(3, [0.5 / (0.5 * fsd), 8 / (0.5 * fsd)], btype="band")
    f = filtfilt(b, aa, a)
    n = (f - f.min()) / (f.max() - f.min() + 1e-9)
    pk, _ = find_peaks(n, distance=int(fsd * 0.4), prominence=0.25)   # dicrotic notch rejected
    if len(pk) < 3:
        return None
    return 60.0 / float(np.median(np.diff(pk)) / fsd)                 # median beat interval (edge-robust)


def robust_pr(pr_all, pleth=None, device_type=None, cycle_duration=None, ecg_hr=None, ecg_clean=False):
    """Best pulse rate + why. 1) clean_pr(PR_ALL) removes spikes. 2) if a clean ECG-HR is
    given and the device disagrees by >PR_DISAGREE, trust the ECG. 3) else re-derive from the
    pleth (KNOWN device rate) and, on disagreement, trust the waveform. Returns (pr, source, flags)."""
    dev = clean_pr(pr_all) if isinstance(pr_all, list) else (pr_all if isinstance(pr_all, (int, float)) else None)
    flags = []
    if dev is None:
        pw = pr_from_pleth(pleth, device_type, cycle_duration) if pleth is not None else None
        return (int(round(pw)) if pw else None, "pleth" if pw else "none", ["no device PR"])
    if ecg_hr and ecg_clean and abs(dev - ecg_hr) > PR_DISAGREE:
        flags.append("device PR %d vs clean ECG-HR %d -> use ECG" % (dev, ecg_hr))
        return int(round(ecg_hr)), "ecg", flags
    if pleth is not None:
        pw = pr_from_pleth(pleth, device_type, cycle_duration)
        if pw and abs(dev - pw) > PR_DISAGREE:
            flags.append("device PR %d vs pleth-derived %d -> over-count, use pleth" % (dev, round(pw)))
            return int(round(pw)), "pleth", flags
    return dev, "device", flags


# ── PART 2: NISO101 handling (ORIGINAL — spike removal on PR_ALL, unchanged) ──────
def handle_niso101_pr(payload):
    """NISO101 (and default): set `spo2.PR` = clean_pr(PR_ALL) — out-of-range + in-range spike
    removal only. This is the original behaviour, left intact."""
    sp = payload.get("spo2")
    if not isinstance(sp, dict):
        return payload
    arr = sp.get("PR_ALL")
    if isinstance(arr, list):
        pr = clean_pr(arr)
        if pr is not None:
            sp["PR"] = pr
    return payload


# ── field helpers (real payloads: pleth under 'plethWave'/'rawData'; PR list under pulseRate) ──
def _get_pleth(payload):
    sp = payload.get("spo2") or {}
    p = payload.get("pleth") if isinstance(payload.get("pleth"), dict) else {}
    return (p.get("plethWave") or p.get("rawData") or payload.get("samples")
            or ((sp.get("pleth") or {}) if isinstance(sp.get("pleth"), dict) else {}).get("rawData"))


def _get_device_pr(sp):
    v = sp.get("pulseRate")                          # real field (usually a list)
    if v is None:
        v = sp.get("PR_ALL")
    return v


# ── PART 3: NISO206 SpO2 VALIDATION (NEW — is the SpO2 trustworthy? uses PI + PLETH only) ──
# The finger SpO2 device can report a plausible-looking number even when the sensor is off the
# finger or the patient is moving — a "false" SpO2. We do NOT judge the number itself; we judge
# whether the conditions to measure it were met, using two independent signals the device sends:
#   1. Perfusion Index (pi) — pi <= PI_MIN means no blood-volume pulse is reaching the sensor,
#      so any SpO2 computed from it is meaningless (sensor not on tissue / no perfusion).
#   2. Pleth waveform       — a real SpO2 needs a genuine, REGULAR pulse. A flat trace (no
#      beats) or a jagged/irregular one (motion artifact) means the SpO2 is not reliable.
# If either fails, the packet is flagged so the frontend hides the value.
PI_MIN          = 0      # perfusion index at/below this = no perfusion (sensor not reading blood)
SPO2_MIN_BEATS  = 3      # fewer detected beats than this on a 6 s / 25 Hz epoch is UNRELIABLE
                         # (detector limitation), NOT proof of no pulse -> defer to perfusion.
SPO2_MAX_IBI_CV = 0.30   # beat-interval CV above this = mildly irregular -> defer to perfusion.
SPO2_IBI_CV_HARD = 0.80  # ... but CV above THIS is grossly irregular = genuine motion -> reject.

# ── Transient PI-collapse detection (rule 1) ──────────────────────────────────────
# A REAL desat keeps good perfusion (verified on prod: 91-93% of true low-SpO2 epochs had
# pi>=2). A false one is a sudden coincident PI + SpO2 collapse that recovers (e.g. pi 5.7->1.6
# as SpO2 98->88->99). We flag ONLY that pattern — never a low PI value on its own — so real
# sustained desaturations are never suppressed. Baseline = median of the last K valid epochs;
# it re-adapts, so a genuinely SUSTAINED drop stops being flagged after ~K epochs (it becomes
# the new normal), while an isolated spike stays flagged.
SPO2_HIST_K       = 4     # rolling baseline = median of the last K valid (pi, spo2) epochs
SPO2_PI_DROP_FRAC = 0.5   # flag if current pi < this fraction of the baseline pi ...
SPO2_DROP_MIN     = 5     # ... AND spo2 is at least this many points below the baseline spo2.
# NOTE: NO absolute pi threshold. The device's `pi` is NOT a calibrated perfusion index — its
# scale varies per device/firmware (observed medians 2.6 .. 78, values 0..200), so an absolute
# cutoff is meaningless. We use pi ONLY self-normalized to the patient's OWN rolling baseline
# (relative drop + coefficient-of-variation), which is scale-independent.

SPO2_CHANGE_FINGER_N = 3  # consecutive bad-signal epochs (no perfusion / motion / device error)
                          # after which we advise repositioning: spo2Advice='change_finger'.
SPO2_CONFIRM_HOLDS = 1    # max CONSECUTIVE transient holds before the low is force-confirmed and
                          # shown. Bounds display delay to 1 epoch so a real sustained low that
                          # happens to start with a pi dip is never hidden for more than one epoch.
SPO2_SEVERE_ALARM = 85    # SpO2 below this = SEVERE. Show immediately, NEVER hold — a severe desat
                          # alarm must not be delayed even one epoch, whatever the pi looks like.
SPO2_PI_CV_ERRATIC = 0.5  # recent pi coefficient-of-variation above this = erratic/jumpy perfusion.
                          # An erratic pi at a SpO2 drop => distrust (artifact); a STABLE pi => trust.

# Per-admission rolling state for the stateful handler (standalone use). In the real pipeline,
# back this with the session store (like BP's _session_read/_write) so it survives across
# processes — pass the history explicitly to the pure is_transient_pi_drop() instead.
_SPO2_STATE = {}          # admissionId -> {"hist": [(pi, spo2), ...], "last_good": int|None}


def _spo2_state_reset():
    """Clear all per-admission SpO2 state (test helper)."""
    _SPO2_STATE.clear()


def spo2_pulse_ok(pleth, device_type=None, cycle_duration=None, fs=None,
                  cv_mild=None, cv_hard=None):
    """Judge whether the PLETH shows a genuine, regular pulse. Resample from the KNOWN device
    rate to 125 Hz, bandpass, detect beats (dicrotic-notch rejection + refractory), measure the
    beat-interval coefficient of variation. A GROSSLY irregular pulse (CV > cv_hard) is motion ->
    reject; a mildly irregular one (CV > cv_mild) or a low beat COUNT is UNEVALUABLE -> defer to
    perfusion (a short 6 s/25 Hz epoch is too little to hard-reject on). `cv_mild`/`cv_hard`
    default to the module constants but the caller passes DYNAMIC, per-patient thresholds.
    Returns (ok, reason, cv): ok is True/False/None; cv is the measured value (or None)."""
    cv_mild = SPO2_MAX_IBI_CV if cv_mild is None else cv_mild
    cv_hard = SPO2_IBI_CV_HARD if cv_hard is None else cv_hard
    try:
        import numpy as np
        from scipy.signal import butter, filtfilt, find_peaks, resample
    except Exception:
        return None, "pleth not evaluable (numpy/scipy unavailable)", None
    a = np.asarray([x for x in (pleth or []) if isinstance(x, (int, float))], float)
    if len(a) < 30 or np.ptp(a) < 1e-6:
        return False, "flat pleth (no pulse: n=%d amp=%.3g)" % (len(a), (float(np.ptp(a)) if len(a) else 0.0)), None
    fs0 = fs or native_fs(device_type, cycle_duration, len(a))
    m = int(round(len(a) * DET_FS / fs0))            # resample to the fixed 125 Hz detection rate
    if m < DET_FS * 2:                               # need >= 2 s of signal to judge
        return None, "pleth too short (<2s) to evaluate", None
    a = resample(a, m); fsd = DET_FS
    b, aa = butter(3, [0.5 / (0.5 * fsd), 8 / (0.5 * fsd)], btype="band")
    f = filtfilt(b, aa, a)
    n = (f - f.min()) / (f.max() - f.min() + 1e-9)
    pk, _ = find_peaks(n, distance=int(fsd * 0.4), prominence=0.25)   # dicrotic notch rejected
    if len(pk) < SPO2_MIN_BEATS:
        # A LOW beat COUNT on a 6 s / 25 Hz epoch is a detector limitation, NOT proof of no pulse
        # (verified on prod: masks real lows when perfusion is fine). Defer to pi.
        return None, "few beats (%d) on short epoch -> defer to perfusion" % len(pk), None
    ibi = np.diff(pk).astype(float)                  # beat-to-beat intervals (samples)
    cv = float(np.std(ibi) / (np.mean(ibi) + 1e-9))
    if cv > cv_hard:
        return False, "grossly irregular pulse (CV %.2f > dyn %.2f: motion artifact)" % (cv, cv_hard), cv
    if cv > cv_mild:
        return None, "mildly irregular pulse (CV %.2f > dyn %.2f) -> defer to perfusion" % (cv, cv_mild), cv
    return True, "regular pulse (%d beats, CV %.2f)" % (len(pk), cv), cv


def verify_spo2(pi, pleth, device_type=None, cycle_duration=None, cv_mild=None, cv_hard=None):
    """Is the device's SpO2 trustworthy? Decided from PERFUSION INDEX + PLETH only — the SpO2
    number itself is never inspected. `cv_mild`/`cv_hard` are the (dynamic, per-patient) pulse-
    irregularity thresholds; the caller passes them. Returns (valid: bool, reason: str, cv):
      1. pi <= PI_MIN            -> invalid (no perfusion, sensor not reading blood volume).
      2. pleth flat / grossly irregular pulse -> invalid (artifact).
      3. pleth not evaluable / few beats / mildly irregular -> accept if there is perfusion."""
    if isinstance(pi, (int, float)) and pi <= PI_MIN:
        return False, "pi=%s -> no perfusion, sensor not reading blood volume" % pi, None
    ok, why, cv = spo2_pulse_ok(pleth, device_type, cycle_duration, cv_mild=cv_mild, cv_hard=cv_hard)
    if ok is False:
        return False, why, cv
    if ok is None:                                   # pleth couldn't be judged -> lean on pi
        if pi is None:
            return True, "pleth not evaluable and no pi; accepted (no evidence of fault)", cv
        return True, "pi=%s (perfusion present); %s" % (pi, why), cv
    return True, "pi=%s; %s" % (pi, why), cv


def _dyn_cv_thresholds(cv_hist):
    """DYNAMIC per-patient pulse-irregularity thresholds. Learns each patient's OWN clean-pulse
    CV distribution and flags only epochs that are outliers FOR THAT PATIENT (median + K*MAD),
    instead of one fixed cutoff for everyone. Bootstraps to the module defaults until there are
    enough clean epochs, and clamps to sane bounds so it never degenerates. Returns (mild, hard)."""
    vals = [c for c in cv_hist if isinstance(c, (int, float))]
    if len(vals) < 6:
        return SPO2_MAX_IBI_CV, SPO2_IBI_CV_HARD          # bootstrap: fixed defaults
    med = _median(vals)
    mad = _median([abs(c - med) for c in vals]) or 0.02   # floor MAD so thresholds aren't zero
    mild = min(max(med + 3.0 * mad, 0.15), 0.60)          # clamp to [0.15, 0.60]
    hard = min(max(med + 6.0 * mad, 0.60), 1.20)          # clamp to [0.60, 1.20]
    return mild, hard


def _median(vals):
    v = sorted(vals)
    m = len(v)
    if not m:
        return None
    return v[m // 2] if m % 2 else 0.5 * (v[m // 2 - 1] + v[m // 2])


def _pi_cv(valid):
    """Coefficient of variation of recent pi (stability/trend). Self-normalizing, so it works
    whatever the device's pi scaling is. None if too few points."""
    pis = [p for p, _ in valid[-SPO2_HIST_K:] if isinstance(p, (int, float)) and p >= 0]
    if len(pis) < 3:
        return None
    m = sum(pis) / len(pis)
    if m <= 0:
        return None
    sd = (sum((p - m) ** 2 for p in pis) / len(pis)) ** 0.5
    return sd / m


def classify_low_spo2(pi, spo2, hist):
    """COMBINED CONFIDENCE (pure). Given current (pi, spo2) and `hist` = recent [(pi, spo2), ...],
    decide whether a downward SpO2 move is a REAL desat or an ARTIFACT (would be a false alarm).
    Everything is self-normalized to the patient's OWN rolling baseline, so the unknown pi scale
    (values ranged 0-40) doesn't matter. Returns (verdict, reason):
        'ok'       -> not a meaningful drop (or no baseline yet) — normal reading.
        'real'     -> stable/good perfusion at the drop -> trust it (display; alarm if low).
        'artifact' -> pi COLLAPSED, or perfusion is ERRATIC while low, at the drop -> distrust.
    Confidence factors: pi LEVEL (absolute floor), pi vs baseline (collapse), and pi STABILITY
    (CV) — a stable pi with a low SpO2 is trusted; an erratic/jumpy pi is not."""
    if not isinstance(pi, (int, float)) or not isinstance(spo2, (int, float)):
        return "ok", ""
    valid = [(p, s) for (p, s) in hist
             if isinstance(p, (int, float)) and isinstance(s, (int, float))]
    if len(valid) < SPO2_HIST_K:
        return "ok", ""                                  # no baseline yet -> treat as normal
    base_pi = _median([p for p, _ in valid[-SPO2_HIST_K:]])
    base_sp = _median([s for _, s in valid[-SPO2_HIST_K:]])
    if spo2 >= base_sp - SPO2_DROP_MIN:
        return "ok", ""                                  # not a meaningful downward move
    cv = _pi_cv(valid)
    # SELF-NORMALIZED only (pi scale is device-dependent, so no absolute cutoff):
    #   collapse -> pi fell well below the patient's OWN recent baseline at the SpO2 drop.
    #   erratic  -> the patient's OWN recent pi is jumpy (high CV) at the SpO2 drop.
    collapse = bool(base_pi and base_pi > 0 and pi < SPO2_PI_DROP_FRAC * base_pi)
    erratic = (cv is not None and cv > SPO2_PI_CV_ERRATIC)
    if collapse:
        return "artifact", ("pi collapse: pi %.1f fell below half its baseline %.1f as SpO2 %d dropped from %d"
                            % (pi, base_pi, spo2, round(base_sp)))
    if erratic:
        return "artifact", ("erratic perfusion: pi CV %.2f (baseline %.1f) at SpO2 %d drop"
                            % (cv, base_pi, spo2))
    return "real", ("stable perfusion: pi %.1f (CV %s, baseline %.1f) -> real SpO2 %d"
                    % (pi, ("%.2f" % cv if cv is not None else "n/a"), base_pi, spo2))


def is_transient_pi_drop(pi, spo2, hist):
    """Back-compat wrapper: True iff classify_low_spo2 says 'artifact'."""
    verdict, why = classify_low_spo2(pi, spo2, hist)
    return (verdict == "artifact"), why


SPO2_PATTERN_MIN = 20     # min displayed SpO2 samples before a session pattern is called
IST_OFFSET_S     = 19800  # UTC->IST (+5:30) for the night (00:00-07:00) window


SPO2_PR_AROUSAL = 3       # PR/HR rise (bpm) at desats vs baseline = autonomic arousal signature


def classify_spo2_pattern(samples, tz_offset_s=IST_OFFSET_S):
    """SESSION-level pattern from MULTIPLE signals, not SpO2 alone. `samples` = [(epoch_s, spo2, pr)..]
    (pr = pulse rate / HR; may be None). Apnea has three physiological signatures and we use all we
    have per epoch:
      1. SpO2  — nocturnal-predominant desaturation on a normal daytime baseline (the desat itself).
      2. PR/HR — the AUTONOMIC AROUSAL response: pulse/heart rate rises at the desats (cyclical
                 variation of heart rate) as the patient arouses to breathe. Corroborates SDB.
      (3. ECG-RR respiratory pause is shown on the graph as an experimental lane.)
    Returns (pattern, detail). SpO2 nocturnal-predominance is NECESSARY; the PR arousal signature
    RAISES confidence ('sleep_apnea_suspected' with support='PR_arousal') and is reported either way.
    NOT diagnostic: ~3-min sampling can't resolve 30-60 s cycles -> a SCREEN, not a diagnosis."""
    vals = []
    for x in samples:                                     # accept (t,s) or (t,s,pr)
        t, s = x[0], x[1]
        pr = x[2] if len(x) > 2 else None
        if isinstance(t, (int, float)) and isinstance(s, (int, float)):
            vals.append((t, s, pr if isinstance(pr, (int, float)) else None))
    if len(vals) < SPO2_PATTERN_MIN:
        return "insufficient", {}
    def hr(t):
        return int(((t + tz_offset_s) // 3600) % 24)
    night = [s for t, s, _ in vals if 0 <= hr(t) < 7]
    day   = [s for t, s, _ in vals if not (0 <= hr(t) < 7)]
    S = [s for _, s, _ in vals]
    lowest, med = min(S), _median(S)
    day_base = _median(day) if day else med
    n_low = sum(1 for s in night if s < 90)
    d_low = sum(1 for s in day if s < 90)

    # PR/HR AUTONOMIC signature: pulse rate during desats vs during normal SpO2
    pr_base = _median([pr for _, s, pr in vals if pr is not None and s >= 94])
    pr_desat = _median([pr for _, s, pr in vals if pr is not None and s < 90])
    arousal = (pr_desat - pr_base) if (pr_base is not None and pr_desat is not None) else None
    pr_support = arousal is not None and arousal >= SPO2_PR_AROUSAL

    if night and lowest < 90 and n_low >= 5 and day_base >= 94 and n_low >= 3 * max(d_low, 1):
        detail = {"lowest": int(lowest), "day_baseline": int(day_base),
                  "night_lows": n_low, "day_lows": d_low,
                  "pr_arousal_bpm": (round(arousal, 1) if arousal is not None else None),
                  "confidence": "high (SpO2 + PR arousal)" if pr_support else "moderate (SpO2 pattern only)",
                  "support": ["nocturnal_desaturation"] + (["PR_arousal"] if pr_support else [])}
        return "sleep_apnea_suspected", detail
    # recurrent hypoxemia: many desats but NOT nocturnal-predominant (day+night) -> not SDB.
    # Don't require a low median — intermittent deep dips on a normal baseline still count.
    if (n_low + d_low) >= 10 or med < 94:
        return "chronic_hypoxemia", {"median": int(med), "lowest": int(lowest),
                                     "night_lows": n_low, "day_lows": d_low,
                                     "pr_arousal_bpm": (round(arousal, 1) if arousal is not None else None)}
    return "normal", {}


def _spo2_device_error(sp):
    """The device's OWN quality flag — cheapest, most authoritative bad-epoch signal.
    The INTEGER `spo2Error` is authoritative: 0 = OK, non-zero = error (verified on prod:
    'SPO2_VALID'->0, 'SPO2_UNKNOWN_ERROR'->0, 'SPO2_COMPUTE_ERROR'->1, 'SPO2_TIMEOUT'->4).
    The message string alone is NOT reliable — 'SPO2_VALID' is a GOOD status, so we must NOT
    treat 'not "no error"' as an error. Returns a reason if the device flags an error, else None."""
    err = sp.get("spo2Error")
    if isinstance(err, (int, float)) and int(err) != 0:
        msg = sp.get("spo2ErrorMsg") or sp.get("sp2ErrorMsg") or ("spo2Error=%d" % int(err))
        return "%s (spo2Error=%d)" % (str(msg).strip(), int(err))
    return None


def _mark_bad(sp, st):
    """Count a consecutive bad-signal epoch; after SPO2_CHANGE_FINGER_N in a row, advise the
    nurse to reposition the probe. Scale-independent (does not use the raw pi number)."""
    st["bad_streak"] = st.get("bad_streak", 0) + 1
    st["hold_streak"] = 0
    if st["bad_streak"] >= SPO2_CHANGE_FINGER_N:
        sp["spo2Advice"] = "change_finger"


def handle_niso206_spo2(payload):
    """NISO206 ONLY. Decide whether the incoming SpO2 should be shown, and stamp the spo2 block:
        spo2Valid    (bool)  — reading is trustworthy this epoch
        displaySpo2  (bool)  — FRONTEND flag: False => do NOT display the live value
        heldSpo2     (int)   — last confirmed-good SpO2 to show (ONLY on a transient hold)
        spo2Status   (str)   — 'ok' | 'ok_real_low' | 'ok_confirmed_low' | 'ok_severe_alarm'
                               | 'spo2_error' | 'signal_bad' | 'hold_transient'
        spo2Alert    (str)   — 'hypoxemia' when a real low (good perfusion, SpO2<90) is displayed
        spo2Advice   (str)   — 'change_finger' after SPO2_CHANGE_FINGER_N consecutive bad epochs
        spo2Pattern  (str)   — session screen: 'sleep_apnea_suspected' | 'chronic_hypoxemia'
        sleepApnea   (str)   — 'SLEEP_APNEA_SUSPECTED' flag when the nocturnal-desat pattern is seen
        spo2Reason   (str)   — why it was rejected (only when not displayed)
    Gates, in order:
      A0. DEVICE error flag (integer spo2Error != 0) -> status 'spo2_error'.
      A1. signal quality (verify_spo2: pi=0 / flat / irregular pleth) -> status 'signal_bad'.
          Gates A do NOT hold a previous value — they FLAG the output as bad so the frontend can
          show an error / '--'. Sustained Gate-A failures raise spo2Advice='change_finger'.
      B.  COMBINED CONFIDENCE (classify_low_spo2, self-normalized pi vs the patient's OWN
          baseline — no absolute pi cutoff, since the device pi scale is uncalibrated). An
          artifact holds the last good value for <= SPO2_CONFIRM_HOLDS epochs, then the low is
          CONFIRMED and shown. A severe low (< SPO2_SEVERE_ALARM) is shown at once, never held.
    The SpO2 value itself is LEFT UNTOUCHED. Never raises; safe no-op if no spo2 block."""
    sp = payload.get("spo2")
    if not isinstance(sp, dict):
        return payload
    adm = payload.get("admissionId") or payload.get("PatId") or "UNKNOWN"
    st = _SPO2_STATE.setdefault(adm, {"hist": [], "last_good": None, "hold_streak": 0,
                                      "bad_streak": 0, "cv_hist": [], "pat_hist": []})
    pi, spo2 = sp.get("pi"), sp.get("spo2")
    epoch_ts = payload.get("epochTime") or payload.get("utcTimestamp")

    # Gate A0 — the device's own error flag (most authoritative). FLAG, do not hold.
    dev_err = _spo2_device_error(sp)
    if dev_err:
        sp["spo2Valid"], sp["displaySpo2"], sp["spo2Status"] = False, False, "spo2_error"
        sp["spo2Reason"] = "device reports bad SpO2 (%s)" % dev_err
        _mark_bad(sp, st)
        return payload                                   # device says bad -> don't touch baseline

    # Gate A1 — signal quality from pi + pleth, with DYNAMIC per-patient irregularity thresholds
    # (learned from this patient's own clean-pulse CV, not a fixed global cutoff). FLAG, no hold.
    cv_mild, cv_hard = _dyn_cv_thresholds(st["cv_hist"])
    valid, reason, cv = verify_spo2(pi, _get_pleth(payload), device_type="NISO206",
                                    cycle_duration=sp.get("cycleDuration"),
                                    cv_mild=cv_mild, cv_hard=cv_hard)
    if not valid:
        sp["spo2Valid"], sp["displaySpo2"], sp["spo2Status"] = False, False, "signal_bad"
        sp["spo2Reason"] = reason
        _mark_bad(sp, st)                                # sustained -> advise change_finger
        return payload                                   # bad signal -> don't touch baseline

    # a clean/near-clean pulse this epoch -> learn its CV into the patient's own baseline
    if isinstance(cv, (int, float)) and cv <= cv_hard:
        st["cv_hist"].append(cv)
        if len(st["cv_hist"]) > 40:
            st["cv_hist"] = st["cv_hist"][-40:]

    # Gate B — COMBINED CONFIDENCE (uses history BEFORE this epoch). Decide real vs artifact,
    # then apply SEVERITY: a severe low (< SPO2_SEVERE_ALARM) is shown at once and never held.
    # Otherwise an artifact is held at most SPO2_CONFIRM_HOLDS epochs, then force-confirmed.
    verdict, why = classify_low_spo2(pi, spo2, st["hist"])
    severe = isinstance(spo2, (int, float)) and spo2 < SPO2_SEVERE_ALARM
    # A fresh DROP (whether pi-suspicious 'artifact' OR a good-perfusion 'real' low) is UNCONFIRMED
    # on its first epoch: an isolated dip that recovers next epoch is a transient FALSE alarm
    # (verified on prod: nurse-confirmed false). So confirm-before-alarm — hold ONE epoch; if the
    # low persists it is shown (confirmed real/hypoxemia), if it recovers it was never displayed.
    # SEVERE (< SPO2_SEVERE_ALARM) is exempt: shown immediately, never delayed.
    needs_confirm = verdict in ("artifact", "real")
    if needs_confirm and not severe and st["hold_streak"] < SPO2_CONFIRM_HOLDS:
        st["hold_streak"] += 1
        sp["spo2Valid"], sp["displaySpo2"], sp["spo2Status"] = False, False, "hold_transient"
        sp["spo2Reason"] = why or ("unconfirmed sudden drop: SpO2 %s -> hold 1 epoch to confirm" % spo2)
        if st["last_good"] is not None:
            sp["heldSpo2"] = st["last_good"]
    else:
        # show it. Label WHY, so downstream can tell a confirmed/real/severe low apart.
        sp["spo2Valid"], sp["displaySpo2"] = True, True
        low = isinstance(spo2, (int, float)) and spo2 < 90
        if needs_confirm and severe:
            sp["spo2Status"] = "ok_severe_alarm"         # too low to delay -> shown at once
            if low: sp["spo2Alert"] = "hypoxemia"
        elif verdict == "artifact":
            sp["spo2Status"] = "ok_confirmed_low"        # was a pi dip, but the low persisted
            if low: sp["spo2Alert"] = "hypoxemia"
        elif verdict == "real" and low:
            sp["spo2Status"] = "ok_real_low"             # good perfusion + persisted low = real desat
            sp["spo2Alert"] = "hypoxemia"
        else:
            sp["spo2Status"] = "ok"
        st["hold_streak"] = 0
        st["bad_streak"] = 0                             # a shown reading -> finger is working
        if isinstance(spo2, (int, float)):
            st["last_good"] = int(spo2)                  # confirmed good -> becomes the held value

    # Always record valid numeric readings so the baseline re-adapts to sustained changes
    if isinstance(pi, (int, float)) and isinstance(spo2, (int, float)):
        st["hist"].append((pi, spo2))
        if len(st["hist"]) > 4 * SPO2_HIST_K:
            st["hist"] = st["hist"][-4 * SPO2_HIST_K:]

    # SESSION PATTERN (Rule 2) — accumulate DISPLAYED SpO2 + PR/HR with time-of-day and screen
    # for sleep-disordered breathing using MULTIPLE signals (SpO2 desat + PR autonomic arousal).
    if isinstance(spo2, (int, float)) and isinstance(epoch_ts, (int, float)):
        pr = sp.get("pulseRate")
        if not isinstance(pr, (int, float)):
            pr = sp.get("PR") if isinstance(sp.get("PR"), (int, float)) else None
        st["pat_hist"].append((float(epoch_ts), float(spo2), pr))
        if len(st["pat_hist"]) > 2000:
            st["pat_hist"] = st["pat_hist"][-2000:]
        pattern, detail = classify_spo2_pattern(st["pat_hist"])
        if pattern not in ("normal", "insufficient"):
            sp["spo2Pattern"] = pattern                  # e.g. 'sleep_apnea_suspected'
            if pattern == "sleep_apnea_suspected":
                sp["sleepApnea"] = "SLEEP_APNEA_SUSPECTED"   # screen flag for the frontend
                sp["sleepApneaConfidence"] = detail.get("confidence")   # SpO2-only vs SpO2+PR
                if detail.get("pr_arousal_bpm") is not None:
                    sp["prArousalBpm"] = detail["pr_arousal_bpm"]
    return payload


# ── DISPATCH: route by device ─────────────────────────────────────────────────────
def apply_pr_handling(payload):
    """Route by device. NISO206 -> SpO2 validity check (pi + pleth) that flags whether the
    frontend should display the value; every other device (incl. NISO101) -> the original
    PR spike-only cleaning (Part 2)."""
    device_type = payload.get("deviceName") or ((payload.get("device") or {}).get("deviceType"))
    if "206" in str(device_type or "").upper():
        return handle_niso206_spo2(payload)
    return handle_niso101_pr(payload)


if __name__ == "__main__":
    import json

    cases = {
        "clean":              [77, 78, 79, 78, 77, 79, 78],
        "out-of-range noise": [77, 78, 79, 250, 0, 78, 77, 79, 78],   # 250 / 0 dropped
        "in-range spike":     [77, 78, 79, 78, 200, 77, 79, 78],      # 200 now caught
        "very noisy":         [78, 5, 79, 240, 77, 0, 78, 200, 79, 78],
    }
    for name, arr in cases.items():
        pr, st = clean_pr(arr, return_stats=True)
        print(f"{name:20s} {arr}\n{'':20s} -> PR={pr}  dropped={st['n_dropped']}/{st['n_in']}  noisy={st['noisy']}\n")

    demo = {"spo2": {"PR_ALL": [77, 78, 79, 78, 200, 77, 79, 78], "SpO2": [99, 98, 99]}}
    apply_pr_handling(demo)
    print("payload spo2.PR =", demo["spo2"]["PR"], "(in-range 200 spike rejected)\n")

    # NISO206 SpO2 validation — no perfusion (pi=0) => flagged do-not-display
    no_perf = {"deviceName": "NISO206", "spo2": {"spo2": 97, "pi": 0}, "pleth": {"plethWave": []}}
    apply_pr_handling(no_perf)
    print("NISO206 pi=0    -> spo2Valid=%s displaySpo2=%s reason=%s"
          % (no_perf["spo2"]["spo2Valid"], no_perf["spo2"]["displaySpo2"], no_perf["spo2"].get("spo2Reason")))
