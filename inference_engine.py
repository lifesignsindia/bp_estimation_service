"""
inference_engine.py - Unified model inference for BP, Hb, and Glucose.
Feature vectors matched exactly to training scripts:
  BP      (31 features) -> danger_v2.py   extract_features()
  Hb/Glu  (27 features) -> hbglucose.py  extract_cycle_features()  + 6 demographics = 33
"""

import os
import joblib
import numpy as np
from scipy.signal import find_peaks, butter, filtfilt, resample, savgol_filter
from scipy.fft import fft
from scipy.stats import skew as scipy_skew, kurtosis as scipy_kurtosis
from collections import Counter
import config as cfg

_DEVICE_NISO204  = "NISO204"
_DEVICE_CHECKME  = "CHECKME"
_DEVICE_BERRYMED = "BERRYMED"


# ── Trend Trackers ────────────────────────────────────────────────────────────

class GenericTrendTracker:
    def __init__(self, window=5, threshold=1.0):
        self._history  = []
        self._window   = window
        self._threshold = threshold

    def update(self, value):
        if value is None or np.isnan(value):
            return
        self._history.append(value)

    def get_trend(self):
        n = len(self._history)
        if n < 2:
            return {"trend": "Stable ->", "slope": 0.0, "readings": n}
        arr   = np.array(self._history[-self._window:])
        slope = float(np.polyfit(np.arange(len(arr)), arr, 1)[0])
        if slope > self._threshold:
            trend = "Rising ^"
        elif slope < -self._threshold:
            trend = "Falling v"
        else:
            trend = "Stable ->"
        return {"trend": trend, "slope": round(slope, 2),
                "latest": self._history[-1], "readings": n}


class BPTrendTracker:
    def __init__(self, window=5):
        self._sbp_history = []
        self._dbp_history = []
        self._cat_history = []
        self._window = window

    def update(self, sbp, dbp, category=None):
        if sbp is None or dbp is None or np.isnan(sbp) or np.isnan(dbp):
            return
        self._sbp_history.append(sbp)
        self._dbp_history.append(dbp)
        self._cat_history.append(category or "unknown")

    def get_trend(self):
        n = len(self._sbp_history)
        if n < 2:
            return {"trend": "Stable ->", "slope": 0.0, "readings": n}
        arr   = np.array(self._sbp_history[-self._window:])
        slope = float(np.polyfit(np.arange(len(arr)), arr, 1)[0])
        trend = "Rising ^" if slope > 1.0 else ("Falling v" if slope < -1.0 else "Stable ->")
        cats  = [c for c in self._cat_history[-self._window:] if c]
        cat   = Counter(cats).most_common(1)[0][0] if cats else "Normal"
        return {"trend": trend, "slope": round(slope, 2), "readings": n}


# ── Main Engine ───────────────────────────────────────────────────────────────

class VitalInferenceEngine:
    def __init__(self):
        self.fs     = cfg.MODEL_SAMPLING_RATE_HZ  # 120 Hz
        self.models = self._load_all_models()
        self._bp_trackers  = {}   # per-patient BPTrendTracker
        self._gen_trackers = {}   # per-patient GenericTrendTracker (Hb/Glu)

    # ── Model Loading ────────────────────────────────────────────────────────

    def _load_all_models(self):
        models = {}
        print("Initializing AI parameters...")
        try:
            # BP classifier (saved as bundle dict by danger_v2.py)
            clf_path = cfg.BP_MODEL_CONFIG["classifier"]
            if os.path.exists(clf_path):
                bundle = joblib.load(clf_path)
                if isinstance(bundle, dict):
                    models["bp_classifier"]   = bundle["model"]
                    models["bp_int_to_label"] = bundle.get("int_to_label",
                                                            {0:"hypo",1:"normal",2:"hyper"})
                    models["bp_label_to_int"] = bundle.get("label_to_int",
                                                            {"hypo":0,"normal":1,"hyper":2})
                else:
                    models["bp_classifier"]   = bundle
                    models["bp_int_to_label"] = {0:"hypo",1:"normal",2:"hyper"}
                    models["bp_label_to_int"] = {"hypo":0,"normal":1,"hyper":2}
                # sklearn >= 1.5 removed multi_class attribute
                clf = models["bp_classifier"]
                if not hasattr(clf, "multi_class"):
                    clf.multi_class = "auto"
                print(f"  [+] Loaded BP classifier")

            scaler_path = cfg.BP_MODEL_CONFIG["global_scaler"]
            if os.path.exists(scaler_path):
                models["bp_global_scaler"] = joblib.load(scaler_path)
                print(f"  [+] Loaded BP global scaler")

            for group in ["hypo", "normal", "hyper"]:
                path     = cfg.BP_MODEL_CONFIG[group]
                scl_path = cfg.BP_MODEL_CONFIG.get(f"scaler_{group}")
                if not os.path.exists(path):
                    print(f"  [!] Missing BP model: {path}")
                    continue
                g = joblib.load(path)
                models[f"bp_{group}_sbp"]      = g["sbp_model"]
                models[f"bp_{group}_dbp"]      = g["dbp_model"]
                models[f"bp_{group}_sbp_meta"] = g.get("sbp_meta")
                models[f"bp_{group}_dbp_meta"] = g.get("dbp_meta")
                models[f"bp_scaler_{group}"]   = (joblib.load(scl_path)
                                                   if scl_path and os.path.exists(scl_path)
                                                   else models.get("bp_global_scaler"))
                print(f"  [+] Loaded BP {group} models")

            # Hb / Glucose
            for key, cfg_key in [("hb_scaler",      "hb_scaler"),
                                   ("hb_model",       "hb_model"),
                                   ("glucose_scaler", "glucose_scaler"),
                                   ("glucose_model",  "glucose_model")]:
                path = cfg.HB_GLU_MODEL_CONFIG[cfg_key]
                if os.path.exists(path):
                    models[key] = joblib.load(path)
                    print(f"  [+] Loaded {key}")
                else:
                    print(f"  [!] Missing {key}: {path}")

        except Exception as e:
            print(f"  [ERROR] Model loading: {e}")
        return models

    # ── Signal Helpers ───────────────────────────────────────────────────────

    def _highpass(self, sig):
        nyq  = 0.5 * self.fs
        b, a = butter(3, 0.4 / nyq, btype="high")
        return filtfilt(b, a, sig)

    def _preprocess(self, sig):
        """0.4 Hz highpass + Savgol smoothing — matches danger_v2.py preprocessing."""
        hp = self._highpass(sig)
        return savgol_filter(hp, window_length=11, polyorder=3)

    def _interpolate_peak(self, sig, idx):
        if idx <= 0 or idx >= len(sig) - 1:
            return float(idx)
        y0, y1, y2 = sig[idx-1], sig[idx], sig[idx+1]
        denom = 2.0 * (2*y1 - y0 - y2)
        if abs(denom) < 1e-10:
            return float(idx)
        return idx + (y2 - y0) / denom

    def _compute_global_hrv(self, ppg_full, pr_all_data):
        """HRV from full 30s window with parabolic interpolation for sub-sample timing."""
        sig = np.array(ppg_full, dtype=float)
        hp  = self._highpass(sig)
        rng = hp.max() - hp.min()
        if rng < 1e-6:
            return None
        norm = (hp - hp.min()) / rng
        raw_peaks, _ = find_peaks(norm, distance=int(self.fs * 0.4))
        if len(raw_peaks) < 4:
            return None
        locs   = np.array([self._interpolate_peak(norm, p) for p in raw_peaks])
        ibi_ms = np.diff(locs) / self.fs * 1000.0
        ibi_ms = ibi_ms[(ibi_ms > 300) & (ibi_ms < 2000)]
        if len(ibi_ms) < 3:
            return None
        rmssd   = float(np.sqrt(np.mean(np.diff(ibi_ms) ** 2)))
        pnn50   = float(np.sum(np.abs(np.diff(ibi_ms)) > 50) / max(len(ibi_ms)-1, 1))
        hrv_std = float(np.std(ibi_ms))
        if pr_all_data and len(pr_all_data) >= 6:
            pr_arr = np.array(pr_all_data, dtype=float)
            pr_arr[(pr_arr == 0) | (pr_arr > 250)] = np.nan
            pr_mean = float(np.nanmean(pr_arr))
            pr_std  = float(np.nanstd(pr_arr)) if np.nanstd(pr_arr) > 0.1 else 2.0
        else:
            pr_mean = float(60000.0 / np.mean(ibi_ms))
            pr_std  = float(np.std(60000.0 / ibi_ms)) if len(ibi_ms) > 1 else 2.0
        return {"hrv": hrv_std, "rmssd": rmssd, "pnn50": pnn50,
                "pr_mean": pr_mean, "pr_std": pr_std}

    # ── Noise Gate ───────────────────────────────────────────────────────────

    def _is_noisy(self, seg_raw, seg_norm, device_type=None):
        """7-gate noise detector using both raw and normalised signal."""
        raw  = np.asarray(seg_raw,  dtype=float)
        norm = np.asarray(seg_norm, dtype=float)

        # Gate 1 — flat signal
        if np.std(norm) < 1e-4 or (norm.max() - norm.min()) < 0.01:
            return True
        # Gate 2 — clipping
        vmin, vmax = norm.min(), norm.max()
        if np.sum((norm == vmin) | (norm == vmax)) / len(norm) > 0.25:
            return True
        # Gate 3 — CHECKME finger-off sentinel (value 156)
        if device_type == _DEVICE_CHECKME and np.mean(raw == 156) > 0.50:
            return True
        # Gate 4 — BerryMed ADC not settled
        if device_type == _DEVICE_BERRYMED and np.mean(raw == 0) > 0.20:
            return True
        # Gate 5 — HF motion artifact (>8 Hz power > 60% of total)
        pwr   = np.abs(np.fft.rfft(norm)) ** 2
        freqs = np.fft.rfftfreq(len(norm), d=1.0 / self.fs)
        tot   = pwr.sum()
        if tot > 0 and pwr[freqs > 8].sum() / tot > 0.60:
            return True
        # Gate 6 — extreme skewness
        if not (-2.0 <= float(scipy_skew(norm)) <= 5.0):
            return True
        # Gate 7 — extreme kurtosis (spike noise)
        if not (-1.5 <= float(scipy_kurtosis(norm)) <= 15.0):
            return True
        return False

    def _iqr_filter(self, values, factor=1.5):
        if len(values) < 4:
            return np.ones(len(values), dtype=bool)
        q1, q3 = np.percentile(values, 25), np.percentile(values, 75)
        iq = q3 - q1
        return (np.array(values) >= q1 - factor*iq) & (np.array(values) <= q3 + factor*iq)

    # ── BP Feature Extraction (matches danger_v2.py exactly) ────────────────

    def _morph_feats_single(self, cyc):
        """16 morphological features from one beat cycle."""
        t   = np.linspace(0, len(cyc) / self.fs, len(cyc))
        d1  = np.gradient(cyc)
        d2  = np.gradient(d1)
        ttp = t[np.argmax(cyc)]
        tdp = t[-1] - ttp if t[-1] - ttp != 0 else 1e-6
        auc = float(np.trapezoid(cyc, t))
        a_w = float(np.max(d2))
        b_w = float(np.min(d2))
        peak_idx  = np.argmax(cyc)
        post_peak = cyc[peak_idx:]
        notch_mins, _ = find_peaks(-post_peak)
        if len(notch_mins) > 0:
            ri  = float(post_peak[notch_mins[0]] / (np.max(cyc) + 1e-9))
            aix = float((post_peak[notch_mins[0]] - np.max(cyc)) / (np.max(cyc) + 1e-9))
        else:
            ri, aix = 0.0, 0.0
        si          = float(0.1 / (ttp + 1e-9))
        above_half  = np.where(cyc >= np.max(cyc) * 0.5)[0]
        pw50        = float(len(above_half) / self.fs) if len(above_half) > 0 else 0.0
        return [
            float(np.max(cyc)), float(t[-1]), float(ttp), float(ttp / tdp),
            float(np.max(d1)), float(np.min(d1)), a_w, b_w,
            a_w, b_w, float(b_w / (a_w + 1e-9)),   # features 8-10: APG a, b, b/a
            auc, ri, aix, si, pw50,
        ]

    def _bp_features(self, ppg_seg, pr_seg=None, hrv_override=None, device_type=None):
        """
        31-feature vector matching danger_v2.py extract_features().
        Beat averaging across all valid beats (within 2σ amplitude).
        Returns None if segment is unusable.
        """
        if len(ppg_seg) < self.fs:
            return None

        ppg_filt = self._preprocess(np.array(ppg_seg, dtype=float))
        rng      = ppg_filt.max() - ppg_filt.min()
        if rng < 1e-6:
            return None
        norm = (ppg_filt - ppg_filt.min()) / rng

        if self._is_noisy(ppg_seg, norm, device_type):
            return None

        peaks, _ = find_peaks(norm, distance=int(self.fs * 0.4))
        mins,  _ = find_peaks(-norm, distance=int(self.fs * 0.3))
        if len(peaks) < 2 or len(mins) < 2:
            return None

        cycles = [norm[mins[mins < p][-1]: mins[mins > p][0]]
                  for p in peaks
                  if len(mins[mins < p]) > 0 and len(mins[mins > p]) > 0]
        if not cycles:
            return None

        # Beat averaging: all beats within 2σ of mean amplitude
        peak_amps    = np.array([np.max(c) for c in cycles])
        valid_cycles = [c for c, a in zip(cycles, peak_amps)
                        if abs(a - peak_amps.mean()) <= 2.0 * peak_amps.std()]
        if not valid_cycles:
            valid_cycles = cycles

        morph_avg = np.mean([self._morph_feats_single(c) for c in valid_cycles], axis=0)

        # FFT from median-amplitude representative beat
        c_rep    = min(cycles, key=lambda c: abs(np.max(c) - np.median(peak_amps)))
        fft_vals = np.abs(fft(c_rep)[: len(c_rep) // 2])
        freqs    = np.fft.fftfreq(len(c_rep), 1/self.fs)[: len(c_rep) // 2]
        pks, _   = find_peaks(fft_vals, distance=5)
        top_idx  = np.argsort(fft_vals[pks])[-3:] if len(pks) >= 3 else np.arange(len(pks))
        f_top    = list(freqs[pks][top_idx])    if len(top_idx) >= 3 else [0.0]*3
        m_top    = list(fft_vals[pks][top_idx]) if len(top_idx) >= 3 else [0.0]*3

        # HRV + PR: prefer global 30s override, fall back to per-segment
        if hrv_override:
            hrv     = hrv_override["hrv"]
            rmssd   = hrv_override["rmssd"]
            pnn50   = hrv_override["pnn50"]
            pr_mean = hrv_override["pr_mean"]
            pr_std  = hrv_override["pr_std"]
        else:
            ibi   = np.diff(peaks) / self.fs if len(peaks) > 1 else np.array([0.0])
            hrv   = float(np.std(ibi))
            rmssd = float(np.sqrt(np.mean(np.diff(ibi)**2))) if len(ibi) > 1 else 0.0
            pnn50 = float(np.sum(np.abs(np.diff(ibi)) > 0.05) / max(len(ibi)-1,1)) if len(ibi) > 1 else 0.0
            if pr_seg is not None and len(pr_seg) > 0 and not np.all(np.isnan(pr_seg)):
                pr_mean = float(np.nanmean(pr_seg))
                pr_std  = float(np.nanstd(pr_seg)) if np.nanstd(pr_seg) > 0.1 else 2.0
            else:
                pr_mean = float(60.0 / np.mean(ibi)) if np.mean(ibi) > 0 else 70.0
                pr_std  = float(np.std(60.0 / ibi))  if len(ibi) > 1 else 2.0

        return [
            float(morph_avg[0]),   # 0  peak amplitude
            float(morph_avg[1]),   # 1  pulse width
            float(morph_avg[2]),   # 2  time-to-peak
            float(morph_avg[3]),   # 3  ttp/tdp ratio
            float(morph_avg[4]),   # 4  max VPG
            float(morph_avg[5]),   # 5  min VPG
            float(morph_avg[6]),   # 6  max APG (d2)
            float(morph_avg[7]),   # 7  min APG (d2)
            float(morph_avg[8]),   # 8  APG a-wave
            float(morph_avg[9]),   # 9  APG b-wave
            float(morph_avg[10]),  # 10 APG b/a ratio
            float(morph_avg[11]),  # 11 AUC
            *f_top,                # 12-14 FFT top freqs
            *m_top,                # 15-17 FFT top mags
            float(hrv),            # 18 HRV std
            float(np.mean(norm)),  # 19 normalised signal mean
            float(np.std(norm)),   # 20 normalised signal std
            float(np.max(norm)),   # 21 normalised signal max
            float(np.min(norm)),   # 22 normalised signal min
            float(pr_mean),        # 23 PR mean
            float(pr_std),         # 24 PR std
            float(morph_avg[12]),  # 25 Reflection Index
            float(morph_avg[13]),  # 26 Augmentation Index
            float(morph_avg[14]),  # 27 Stiffness Index
            float(rmssd),          # 28 RMSSD
            float(pnn50),          # 29 pNN50
            float(morph_avg[15]),  # 30 Pulse Width at 50%
        ]

    # ── Hb/Glu Feature Extraction (matches hbglucose.py exactly) ────────────

    def _hb_glu_features(self, ppg_seg, device_type=None):
        """
        20 PPG + 7 optical features (27 total).
        Caller appends 6 demographics → 33 features for the scaler.
        """
        if len(ppg_seg) < self.fs:
            return None

        ppg_filt = self._preprocess(np.array(ppg_seg, dtype=float))
        rng      = ppg_filt.max() - ppg_filt.min()
        if rng < 1e-6:
            return None
        norm = (ppg_filt - ppg_filt.min()) / rng

        if self._is_noisy(ppg_seg, norm, device_type):
            return None

        peaks, _ = find_peaks(norm, distance=int(self.fs * 0.4))
        mins,  _ = find_peaks(-norm, distance=int(self.fs * 0.3))
        cycles   = [norm[mins[mins < p][-1]: mins[mins > p][0]]
                    for p in peaks
                    if len(mins[mins < p]) > 0 and len(mins[mins > p]) > 0]
        if not cycles:
            return None

        peak_amps = [np.max(c) for c in cycles]
        cycle     = min(cycles, key=lambda c: abs(np.max(c) - np.median(peak_amps)))
        t   = np.linspace(0, len(cycle) / self.fs, len(cycle))
        d1  = np.gradient(cycle)
        d2  = np.gradient(d1)

        auc   = float(np.trapezoid(cycle, t))
        pw    = t[-1]
        ttp   = t[np.argmax(cycle)]
        tdp   = pw - ttp if pw - ttp != 0 else 1e-6
        ratio = ttp / tdp

        fft_vals = np.abs(fft(cycle)[: len(cycle) // 2])
        freqs    = np.fft.fftfreq(len(cycle), 1/self.fs)[: len(cycle) // 2]
        pks, _   = find_peaks(fft_vals, distance=5)
        if len(pks) < 3:
            top_freqs = [0.0]*3; top_mags = [0.0]*3
        else:
            si = np.argsort(fft_vals[pks])[-3:]
            top_freqs = list(freqs[pks][si]); top_mags = list(fft_vals[pks][si])

        ibi_list = np.diff(peaks) / self.fs if len(peaks) > 1 else [0.0]
        hrv_std  = float(np.std(ibi_list)) if len(ibi_list) > 1 else 0.0

        # Normalised signal stats (sensor-agnostic, matches hbglucose.py)
        pf_n = (ppg_filt - ppg_filt.min()) / (ppg_filt.max() - ppg_filt.min() + 1e-6)

        features = [
            float(np.max(cycle)), float(pw), float(ttp), float(ratio),
            float(np.max(d1)), float(np.min(d1)), float(np.max(d2)), float(np.min(d2)),
            auc,
            float(top_freqs[0]), float(top_mags[0]),
            float(top_freqs[1]), float(top_mags[1]),
            float(top_freqs[2]), float(top_mags[2]),
            hrv_std,
            float(np.mean(pf_n)), float(np.std(pf_n)),
            float(np.max(pf_n)),  float(np.min(pf_n)),
        ]

        # Optical / shape features (indices 20–26)
        ac_dc     = (ppg_filt.max() - ppg_filt.min()) / (np.mean(np.abs(ppg_filt)) + 1e-9)
        sig_e     = ppg_filt - ppg_filt.min()
        sig_e     = sig_e / (sig_e.sum() + 1e-9)
        entropy   = float(-np.sum(sig_e * np.log(sig_e + 1e-9)))
        sig_skew  = float(scipy_skew(ppg_filt))
        perf_idx  = (ppg_filt.max() - ppg_filt.min()) / (np.mean(ppg_filt) + 1e-9)
        sqi       = float(np.max(cycle) / (np.std(ppg_filt) + 1e-6))
        cyc_skew  = float(scipy_skew(cycle))
        cyc_kurt  = float(scipy_kurtosis(cycle))
        features += [ac_dc, entropy, sig_skew, perf_idx, sqi, cyc_skew, cyc_kurt]

        # log1p transforms applied at training time (same indices as hbglucose.py)
        for idx in [0, 8, 16, 17, 18, 19, 20, 23]:
            features[idx] = float(np.log1p(abs(features[idx])))

        return features  # 27 features

    # ── BP Inference ────────────────────────────────────────────────────────

    def _run_bp(self, ppg_segment, pr_all_data=None, device_type=None):
        """
        Segment-wise BP inference with soft-voting across categories.
        Returns dict with sbp/dbp/bp_category/valid_segments or None.
        """
        from scipy.signal import medfilt as _medfilt

        FS      = self.fs
        SEG_LEN = FS * 5
        SEGS    = len(ppg_segment) // SEG_LEN

        if len(ppg_segment) < FS * 15 or "bp_classifier" not in self.models:
            return None

        # Resolve PR array
        pr_arr = None
        if pr_all_data and len(pr_all_data) > 0:
            pr_arr = np.array(pr_all_data, dtype=float)
            pr_arr[(pr_arr == 0) | (pr_arr == 127) | (pr_arr == 255)] = np.nan
        if pr_arr is None or np.count_nonzero(~np.isnan(pr_arr)) < 6:
            pr_arr = np.full(6, 75.0)

        ppg_med    = _medfilt(ppg_segment, kernel_size=3)
        global_hrv = self._compute_global_hrv(ppg_med, pr_all_data)

        seg_preds = []
        for i in range(SEGS):
            seg    = ppg_med[i*SEG_LEN : (i+1)*SEG_LEN]
            pr_seg = pr_arr[i*5 : (i+1)*5] if len(pr_arr) >= 30 else pr_arr
            feat   = self._bp_features(seg, pr_seg, hrv_override=global_hrv,
                                        device_type=device_type)
            if feat is None:
                continue

            X_raw = np.array(feat, dtype=float).reshape(1, -1)
            X_cls = self.models["bp_global_scaler"].transform(X_raw)
            probs   = self.models["bp_classifier"].predict_proba(X_cls)[0]
            classes = self.models["bp_classifier"].classes_
            int_to_label = self.models.get("bp_int_to_label", {0:"hypo",1:"normal",2:"hyper"})

            # Soft voting: weighted sum across all categories
            w_sbp = w_dbp = 0.0
            for idx, cls_raw in enumerate(classes):
                p        = probs[idx]
                cls_name = int_to_label.get(cls_raw, str(cls_raw))
                scl_key  = f"bp_scaler_{cls_name}"
                sbp_key  = f"bp_{cls_name}_sbp"
                dbp_key  = f"bp_{cls_name}_dbp"
                if scl_key in self.models and sbp_key in self.models:
                    X_reg  = self.models[scl_key].transform(X_raw)
                    s_pred = float(self.models[sbp_key].predict(X_reg)[0])
                    d_pred = float(self.models[dbp_key].predict(X_reg)[0])
                    # Apply stacking meta-model if available
                    sbp_meta = self.models.get(f"bp_{cls_name}_sbp_meta")
                    dbp_meta = self.models.get(f"bp_{cls_name}_dbp_meta")
                    if sbp_meta:
                        s_pred = float(sbp_meta.predict([[s_pred]])[0])
                        d_pred = float(dbp_meta.predict([[d_pred]])[0])
                    w_sbp += p * s_pred
                    w_dbp += p * d_pred

            top_raw = classes[np.argmax(probs)]
            label   = int_to_label.get(top_raw, str(top_raw))
            seg_preds.append({"sbp": w_sbp, "dbp": w_dbp, "label": label,
                               "conf": float(np.max(probs)), "proba": list(probs)})

        if len(seg_preds) < 3:
            return None

        # IQR outlier removal
        sbp_list = [r["sbp"] for r in seg_preds]
        dbp_list = [r["dbp"] for r in seg_preds]
        mask     = self._iqr_filter(sbp_list) & self._iqr_filter(dbp_list)
        if mask.sum() < 2:
            mask = np.ones(len(seg_preds), dtype=bool)

        clean_sbp  = [sbp_list[i] for i,m in enumerate(mask) if m]
        clean_dbp  = [dbp_list[i] for i,m in enumerate(mask) if m]
        clean_lbls = [seg_preds[i]["label"] for i,m in enumerate(mask) if m]
        category   = Counter(clean_lbls).most_common(1)[0][0]

        # Confidence fallback: all devices — if classifier is uncertain, use "normal" base
        all_probas = np.array([r["proba"] for r in seg_preds])
        mean_proba = np.mean(all_probas, axis=0)
        if category in ("hypo", "hyper") and float(mean_proba.max()) < cfg.BP_CONFIDENCE_THRESHOLD:
            category = "normal"

        # Base + clipped delta → final BP (values from config)
        base_sbp, base_dbp = cfg.BP_CATEGORY_BASES.get(category, (118.0, 76.0))
        reg_sbp = float(np.clip(np.mean(clean_sbp), -cfg.BP_DELTA_CLIP, cfg.BP_DELTA_CLIP))
        reg_dbp = float(np.clip(np.mean(clean_dbp), -cfg.BP_DELTA_CLIP, cfg.BP_DELTA_CLIP))
        f_sbp   = float(np.clip(base_sbp + reg_sbp, *cfg.BP_SBP_LIMITS))
        f_dbp   = float(np.clip(base_dbp + reg_dbp, *cfg.BP_DBP_LIMITS))

        return {"sbp": round(f_sbp, 1), "dbp": round(f_dbp, 1),
                "bp_category": category,
                "valid_segments": len(seg_preds), "total_segments": SEGS}

    # ── Hb / Glu Inference ──────────────────────────────────────────────────

    def _run_hb_glu(self, ppg_segment, age, gender, bmi, offsets, device_type):
        """27 PPG features + 6 demographics = 33 → Hb and Glucose."""
        hg_raw = self._hb_glu_features(ppg_segment, device_type=device_type)
        if hg_raw is None:
            return None, None

        age_val    = float(age)    if (age    and 0  < float(age)    < 120) else 35.0
        gender_bin = 1.0           if str(gender).lower() == "male"         else 0.0
        bmi_val    = float(bmi)    if (bmi    and 10 < float(bmi)    < 80)  else -1.0
        demo       = [age_val, age_val**2, 1.0 if age_val > 60 else 0.0,
                      gender_bin, age_val * gender_bin, bmi_val]
        X = np.array(hg_raw + demo, dtype=float).reshape(1, -1)

        hb_res = glu_res = None
        try:
            if "hb_model" in self.models:
                raw = float(self.models["hb_model"].predict(
                    self.models["hb_scaler"].transform(X))[0])
                if offsets: raw += offsets.get("hb", 0.0)
                hb_res = round(raw, 2)
        except Exception as e:
            print(f"  [Hb error] {e}")
        try:
            if "glucose_model" in self.models:
                raw = float(self.models["glucose_model"].predict(
                    self.models["glucose_scaler"].transform(X))[0])
                if offsets: raw += offsets.get("glucose", 0.0)
                glu_res = round(max(40.0, min(400.0, raw)), 1)
        except Exception as e:
            print(f"  [Glu error] {e}")
        return hb_res, glu_res

    # ── Public API ───────────────────────────────────────────────────────────

    def analyze(self, pleth_array, fs=120, age=35, gender="Male", bmi=24,
                offsets=None, adm_id="UNKNOWN", device_type=None):
        """
        Main entry point called by vitals_standalone and test_pipeline.
        Returns a dict compatible with the existing pipeline result parsing:
          { sbp, dbp, category, hb, glucose, trend, valid_segments, total_segments }
        Returns empty dict (no "sbp" key) when signal is insufficient.
        """
        ppg = np.asarray(pleth_array, dtype=float)

        # Resample to model rate if needed (usually a no-op — pipeline sends 120 Hz)
        src_rate = fs if fs else cfg.SAMPLING_RATE_HZ
        if src_rate != cfg.MODEL_SAMPLING_RATE_HZ and len(ppg) > 0:
            n_target = int(round(len(ppg) * cfg.MODEL_SAMPLING_RATE_HZ / src_rate))
            ppg = resample(ppg, n_target)

        # Per-patient trend tracker
        if adm_id not in self._bp_trackers:
            self._bp_trackers[adm_id] = BPTrendTracker()
        tracker = self._bp_trackers[adm_id]

        # BP
        bp_result = None
        try:
            bp_result = self._run_bp(ppg, device_type=device_type)
        except Exception as e:
            print(f"  [BP error] {e}")

        if bp_result is None:
            return {}   # no "sbp" key → pipeline treats as poor signal

        tracker.update(bp_result["sbp"], bp_result["dbp"], bp_result["bp_category"])

        if offsets:
            bp_result["sbp"] = float(np.clip(
                bp_result["sbp"] + offsets.get("sbp", 0.0), *cfg.BP_SBP_LIMITS))
            bp_result["dbp"] = float(np.clip(
                bp_result["dbp"] + offsets.get("dbp", 0.0), *cfg.BP_DBP_LIMITS))

        # Hb / Glucose
        hb_res = glu_res = None
        try:
            hb_res, glu_res = self._run_hb_glu(ppg, age, gender, bmi, offsets, device_type)
        except Exception as e:
            print(f"  [Hb/Glu error] {e}")

        result = {
            "sbp":            bp_result["sbp"],
            "dbp":            bp_result["dbp"],
            "category":       bp_result["bp_category"],
            "trend":          tracker.get_trend(),
            "valid_segments": bp_result["valid_segments"],
            "total_segments": bp_result["total_segments"],
        }
        if hb_res  is not None: result["hb"]      = hb_res
        if glu_res is not None: result["glucose"]  = glu_res
        return result
