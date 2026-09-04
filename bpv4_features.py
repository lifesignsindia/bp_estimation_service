"""BP v4 -- physiologically-grounded pulse-wave-analysis features, computed correctly.

WHY A REWRITE
    The deployed path computes BP as `category_base + clip(regressor, +-15)` with three discrete
    bases (90/118/142). That is a 3-way classifier with a nudge, not a continuous estimator: the
    reachable outputs are only 75-105, 103-133 or 127-157, and 34% of this trial's references fall
    outside whichever band is selected. It cannot reach 100 or 144 by construction, which is exactly
    the range compression observed. v4 abandons that architecture entirely.

    The feature layer also had two real defects: the dicrotic notch was missed on 99.6% of epochs
    (silently writing aix=ri=0.0), and derivatives were taken with raw np.gradient on a single
    max-amplitude beat, which is dominated by noise.

DESIGN
    * ENSEMBLE BEAT. Segment every beat, resample to a common length, reject by template
      correlation, then average. Noise falls as 1/sqrt(N); with 30-50 beats that is a ~6x SNR gain,
      which is what makes second-derivative (APG) features usable at all.
    * SAVITZKY-GOLAY DERIVATIVES. Local polynomial fits rather than point differences -- the
      standard way to differentiate noisy physiological signals.
    * ROBUST FIDUCIALS. Systolic peak, dicrotic notch and diastolic peak located via the APG and
      amplitude-relative prominence, restricted to physiologically plausible windows, with an
      explicit "not found" instead of a fake zero.

FEATURES (each has a stated physiological rationale for BP -- nothing included just to pad a vector)
    Timing / stiffness
      crest_time      time to systolic peak; falls as arterial stiffness and BP rise
      dvp_time        systolic->diastolic peak interval; the basis of the stiffness index
      stiffness_idx   1/dvp_time (height-normalised SI needs subject height, unavailable here)
      pulse_width_50/25/75  width at fractions of amplitude; narrows with rising vascular tone
    Reflected wave (the classical BP correlates)
      aix             (P_notch - P_sys)/P_sys, augmentation from the reflected wave
      ri              P_notch / P_sys, reflection index
      ipa             area after the notch / area before it; inflection-point-area ratio
    Contractility / ejection
      max_upstroke    max dP/dt on the rise; tracks pulse pressure and contractility
      upstroke_ratio  max_upstroke normalised by amplitude
    APG (second-derivative) indices -- established arterial-stiffness markers
      apg_b_a, apg_c_a, apg_d_a, apg_e_a, aging_index = (b-c-d-e)/a
    Autonomic / rate
      hr, rmssd, pnn50, ibi_cv
    Signal quality (for weighting, not prediction)
      n_beats, template_corr, perfusion_index
"""
import numpy as np
from scipy.signal import butter, filtfilt, find_peaks, savgol_filter
from scipy.interpolate import CubicSpline

FS = 120
BEAT_LEN = 250
MIN_BEATS = 5
CORR_MIN = 0.80

NAMES = ["crest_time", "dvp_time", "stiffness_idx", "si_crest", "pulse_width_50", "pulse_width_25",
         "pulse_width_75", "aix", "ri", "ipa", "max_upstroke", "upstroke_ratio",
         "apg_b_a", "apg_c_a", "apg_d_a", "apg_e_a", "aging_index",
         "hr", "rmssd", "pnn50", "ibi_cv", "auc_norm", "decay_slope"]
QUALITY = ["n_beats", "template_corr", "perfusion_index"]


def _bandpass(x, lo=0.5, hi=12.0, fs=FS, order=3):
    nyq = 0.5 * fs
    b, a = butter(order, [lo / nyq, min(hi, nyq * 0.95) / nyq], btype="band")
    return filtfilt(b, a, x)


def _resample(y, n):
    if len(y) == n:
        return np.asarray(y, float)
    return CubicSpline(np.linspace(0, 1, len(y)), np.asarray(y, float))(np.linspace(0, 1, n))


def _sg(y, deriv, win=11, poly=3):
    w = min(win if win % 2 else win + 1, len(y) - (1 - len(y) % 2))
    if w < poly + 2:
        return np.gradient(y) if deriv == 1 else np.gradient(np.gradient(y))
    return savgol_filter(y, w, poly, deriv=deriv)


def ensemble_beat(sig, fs=FS):
    """High-SNR average beat + quality metrics. Returns (beat, n_beats, template_corr, ibis)."""
    x = _bandpass(sig, fs=fs)
    p2, p98 = np.percentile(x, [2, 98])
    x = (x - p2) / (p98 - p2 + 1e-9)
    peaks, _ = find_peaks(x, distance=int(fs * 0.4), prominence=0.15)
    if len(peaks) < MIN_BEATS + 1:
        return None, 0, 0.0, np.array([])
    # FOOT DETECTION, robust version.
    # `find_peaks(-x)` does not reliably find the end-diastolic foot on NISO101: a prominent
    # dicrotic notch gets picked instead, so beats were cut at the notch (28% of NISO101 epochs
    # truncated, vs 0% on MIMIC) and the systolic peak landed at 48% of the segment instead of the
    # physiological ~20%. Systolic PEAKS are a far more reliable fiducial, so the foot is taken as
    # the MINIMUM BETWEEN CONSECUTIVE PEAKS and each beat runs foot -> next foot.
    beats, ibis = [], np.diff(peaks) / fs
    feet = []
    for a_, b_ in zip(peaks[:-1], peaks[1:]):
        if b_ - a_ < 3:
            continue
        feet.append(a_ + int(np.argmin(x[a_:b_])))
    feet = np.array(feet, dtype=int)
    if len(feet) < MIN_BEATS:
        return None, 0, 0.0, ibis[(ibis > 0.3) & (ibis < 2.0)]
    for f0, f1 in zip(feet[:-1], feet[1:]):
        seg = x[f0:f1]
        if not (int(fs * 0.3) <= len(seg) <= int(fs * 1.8)):
            continue
        rng = seg.max() - seg.min()
        if rng < 1e-6:
            continue
        beats.append(_resample((seg - seg.min()) / rng, BEAT_LEN))
    if len(beats) < MIN_BEATS:
        return None, 0, 0.0, ibis
    B = np.array(beats)
    tmpl = np.median(B, axis=0)
    cc = np.array([np.corrcoef(b, tmpl)[0, 1] for b in B])
    keep = cc >= CORR_MIN
    if keep.sum() < MIN_BEATS:
        keep = cc >= np.percentile(cc, 50)
    return np.mean(B[keep], axis=0), int(keep.sum()), float(np.mean(cc[keep])), ibis[(ibis > 0.3) & (ibis < 2.0)]


def fiducials(beat):
    """(systolic peak, dicrotic notch, diastolic peak) indices; notch/diastolic may be None."""
    sys_i = int(np.argmax(beat))
    post = beat[sys_i:]
    n = len(post)
    if n < 8:
        return sys_i, None, None
    lo, hi = max(2, int(0.10 * n)), max(4, int(0.70 * n))
    if hi <= lo:
        return sys_i, None, None
    seg = post[lo:hi]
    amp = float(np.max(beat) - np.min(beat)) + 1e-9
    notch = None
    w = min(len(seg) if len(seg) % 2 else len(seg) - 1, 9)
    if w >= 5:
        try:
            d2 = savgol_filter(seg, w, 2, deriv=2)
            c, _ = find_peaks(d2)
            if len(c):
                notch = lo + int(c[np.argmax(d2[c])])
        except Exception:
            notch = None
    if notch is None:
        c, pr = find_peaks(-seg, prominence=0.01 * amp)
        if len(c):
            notch = lo + int(c[np.argmax(pr["prominences"])])
    dia = None
    if notch is not None and notch + 3 < n:
        tail = post[notch:]
        c, pr = find_peaks(tail, prominence=0.002 * amp)
        if len(c):
            dia = notch + int(c[np.argmax(pr["prominences"])])
        else:
            # At the finger the diastolic wave is often a SHOULDER, not a true local maximum.
            # Take the point of maximum upward curvature in the tail -- the standard fallback --
            # rather than discarding the feature (which made dvp_time/stiffness_idx 100% NaN).
            if len(tail) >= 7:
                w = min(len(tail) if len(tail) % 2 else len(tail) - 1, 9)
                try:
                    cur = savgol_filter(tail, w, 2, deriv=2)
                    k = int(np.argmax(cur))
                    if 1 <= k < len(tail) - 1:
                        dia = notch + k
                except Exception:
                    dia = None
    return sys_i, (sys_i + notch if notch is not None else None), (sys_i + dia if dia is not None else None)


def width_at(beat, frac):
    thr = beat.min() + frac * (beat.max() - beat.min())
    idx = np.where(beat >= thr)[0]
    return float(len(idx) / len(beat)) if len(idx) else 0.0


def auto_polarity(sig, fs=FS):
    """Return sig oriented so the SYSTOLIC PEAK is the maximum.

    NISO101 reports the opposite polarity to MIMIC (light absorption vs perfusion). Measured on the
    corrected foot-to-foot segmentation: MIMIC puts its peak at 26% of the beat as-is and 75%
    inverted; NISO101 gives 68% as-is and 32% inverted -- exactly mirrored. Left uncorrected, every
    morphology feature is computed upside-down: crest_time measured foot-to-TROUGH (0.50 s vs the
    physiological 0.20 s), max_upstroke measured on the diastolic decay (3.3 vs 9.9), aix/ri taken
    off the wrong fiducial. A real pulse rises fast and decays slowly, so the correct orientation is
    the one whose peak sits EARLY in the cycle.
    """
    x = np.asarray(sig, float)
    best, best_pos = x, None
    for cand in (x, -x):
        b, n, _, _ = ensemble_beat(cand, fs=fs)
        if b is None or n < MIN_BEATS:
            continue
        pos = int(np.argmax(b)) / len(b)
        if best_pos is None or pos < best_pos:
            best, best_pos = cand, pos
    return best


def features(sig, fs=FS, auto_pol=True):
    """-> (feature vector, quality vector) or (None, None)."""
    if auto_pol:
        sig = auto_polarity(sig, fs=fs)
    beat, nb, tc, ibis = ensemble_beat(sig, fs=fs)
    if beat is None:
        return None, None
    dur = float(np.mean(ibis)) if len(ibis) else 0.8
    t = np.linspace(0, dur, len(beat))
    amp = float(beat.max() - beat.min()) + 1e-9

    sys_i, notch_i, dia_i = fiducials(beat)
    crest = float(t[sys_i])
    dvp = float(t[dia_i] - t[sys_i]) if dia_i is not None else np.nan
    stiff = float(1.0 / dvp) if (dia_i is not None and dvp > 1e-6) else np.nan
    peak_v = float(beat[sys_i])
    if notch_i is not None:
        aix = float((beat[notch_i] - peak_v) / (peak_v + 1e-9))
        ri = float(beat[notch_i] / (peak_v + 1e-9))
        a1 = float(np.trapezoid(beat[:notch_i], t[:notch_i])) if notch_i > 1 else np.nan
        a2 = float(np.trapezoid(beat[notch_i:], t[notch_i:])) if notch_i < len(beat) - 1 else np.nan
        ipa = float(a2 / a1) if (a1 and not np.isnan(a1) and a1 > 1e-9) else np.nan
    else:
        aix = ri = ipa = np.nan

    d1 = _sg(beat, 1); d2 = _sg(beat, 2)
    max_up = float(np.max(d1)) / (dur / len(beat))
    up_ratio = float(np.max(d1) / amp)

    # APG a-b-c-d-e waves in the first ~40% of the beat
    # a-b-c-d-e span most of systole plus early diastole; the original 40% window cut off c/d/e,
    # leaving them 100% NaN. Search 70% of the beat with amplitude-relative prominence.
    lim = max(8, int(0.70 * len(beat)))
    seg2 = d2[:lim]
    _pmin = 0.02 * (np.max(np.abs(seg2)) + 1e-12)
    pk, _ = find_peaks(seg2, prominence=_pmin)
    tr, _ = find_peaks(-seg2, prominence=_pmin)
    a_w = float(seg2[pk[0]]) if len(pk) else float(np.max(seg2))
    b_w = float(seg2[tr[0]]) if len(tr) else float(np.min(seg2))
    c_w = float(seg2[pk[1]]) if len(pk) > 1 else np.nan
    d_w = float(seg2[tr[1]]) if len(tr) > 1 else np.nan
    e_w = float(seg2[pk[2]]) if len(pk) > 2 else np.nan
    den = a_w if abs(a_w) > 1e-9 else np.nan
    b_a, c_a, d_a, e_a = (b_w / den, c_w / den, d_w / den, e_w / den) if den == den else (np.nan,) * 4
    aging = ((b_w - (c_w if c_w == c_w else 0) - (d_w if d_w == d_w else 0) - (e_w if e_w == e_w else 0)) / den) if den == den else np.nan

    hr = float(60.0 / dur) if dur > 1e-6 else np.nan
    rmssd = float(np.sqrt(np.mean(np.diff(ibis) ** 2))) if len(ibis) > 2 else np.nan
    pnn50 = float(np.mean(np.abs(np.diff(ibis)) > 0.05)) if len(ibis) > 2 else np.nan
    ibi_cv = float(np.std(ibis) / (np.mean(ibis) + 1e-9)) if len(ibis) > 2 else np.nan

    auc_n = float(np.trapezoid(beat, t) / (amp * dur + 1e-9))
    tail = beat[sys_i:]
    decay = float(np.polyfit(np.arange(len(tail)), tail, 1)[0]) if len(tail) > 3 else np.nan

    si_crest = float(0.1 / (crest + 1e-9))
    f = [crest, dvp, stiff, si_crest, width_at(beat, 0.5), width_at(beat, 0.25), width_at(beat, 0.75),
         aix, ri, ipa, max_up, up_ratio, b_a, c_a, d_a, e_a, aging,
         hr, rmssd, pnn50, ibi_cv, auc_n, decay]
    # PERFUSION INDEX = AC/DC, both measured on the RAW signal.
    # The previous form was  amp / mean(|sig|)  where `amp` came from the ensemble beat, which is
    # already per-beat scaled to [0,1]. That made the numerator ~1 and the denominator the raw ADC
    # offset, so PI collapsed to 1/DC_level: every CIMS epoch landed in 0.0000-0.0001 and the metric
    # carried no perfusion information at all. AC must be the pulsatile amplitude of the RAW trace.
    raw = np.asarray(sig, float)
    dc = float(np.mean(raw))
    ac_sig = _bandpass(raw, fs=fs)
    lo, hi = np.percentile(ac_sig, [2, 98])
    ac = float(hi - lo)
    pi = float(100.0 * ac / abs(dc)) if abs(dc) > 1e-9 else np.nan
    return np.array(f, float), np.array([nb, tc, pi], float)


def features_from_epoch(pleth, fs_in=None, fs=FS):
    x = np.asarray(pleth, float)
    if fs_in and fs_in != fs:
        x = _resample(x, int(round(len(x) * fs / fs_in)))
    if len(x) < fs * 10:
        return None, None
    return features(x, fs=fs)
