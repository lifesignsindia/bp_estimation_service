"""
inference_engine.py - Unified model inference for BP, Hb, and Glucose.
Fully integrated with vitals_standalone.py and config.py.
"""

import os
import joblib
import numpy as np
from scipy.signal import find_peaks, savgol_filter
from scipy.fft import fft
from scipy.stats import iqr, skew as scipy_skew, kurtosis as scipy_kurtosis
import config as cfg

class GenericTrendTracker:
    """Accumulates successive readings and returns a session trend."""
    def __init__(self, window=5, threshold=1.0):
        self._history = []
        self._window = window
        self._threshold = threshold

    def update(self, value):
        if value is None or np.isnan(value):
            return
        self._history.append(value)
        # Limit history to the window size to ensure trend sensitivity
        if len(self._history) > self._window:
            self._history = self._history[-self._window:]

    def get_trend(self):
        n = len(self._history)
        if n < 2:
            return {"trend": "Stable ->", "slope": 0.0, "readings": n}
        slope = float(np.polyfit(range(n), self._history, 1)[0])
        trend = "Rising /\\" if slope > self._threshold else ("Falling \\/" if slope < -self._threshold else "Stable ->")
        return {"trend": trend, "slope": round(slope, 2), "readings": n}


class VitalInferenceEngine:
    """Unified model inference for BP, Hb, and Glucose."""
    
    def __init__(self):
        self.models = {}
        self._load_models()
        self._trend_trackers = {}  # keyed by admissionId

    def _load_models(self):
        """Automatically loads all .pkl files mapped in config.py"""
        print("Initializing AI parameters...")
        
        # 1. Load Blood Pressure Models
        for key, path in cfg.BP_MODEL_CONFIG.items():
            if os.path.exists(path):
                self.models[key] = joblib.load(path)
                print(f"  [+] Loaded BP Model: {key}")
            else:
                print(f"  [!] Missing BP Model: {path}")

        # 2. Load Hemoglobin & Glucose Models
        for key, path in cfg.HB_GLU_MODEL_CONFIG.items():
            if os.path.exists(path):
                self.models[key] = joblib.load(path)
                print(f"  [+] Loaded Hb/Glu Model: {key}")
            else:
                print(f"  [!] Missing Hb/Glu Model: {path}")

    def _extract_31_features(self, pleth_array, fs=120):
        """
        Extracts exactly 31 features (Morphology, APG, Frequency, Statistical) 
        from the 120Hz waveform to feed into the XGBoost AI.
        """
        features = np.zeros(31)
        try:
            # 1. Smooth the wave to preserve peaks (Savitzky-Golay)
            smoothed = savgol_filter(pleth_array, window_length=15, polyorder=3)
            
            # 2. Find Systolic Peaks
            peaks, _ = find_peaks(smoothed, distance=fs*0.4, prominence=0.1)
            if len(peaks) < 2:
                return features # Not enough peaks, return zeros
            
            # Calculate Heart Rate
            rr_intervals = np.diff(peaks) / fs
            hr = 60.0 / np.mean(rr_intervals)
            
            # 3. Derivatives (Velocity and Acceleration of the pulse)
            vpg = np.gradient(smoothed)        # 1st derivative
            apg = np.gradient(vpg)             # 2nd derivative
            
            # VPG Features
            vpg_peaks, _ = find_peaks(vpg, distance=fs*0.4)
            vpg_max = np.mean(vpg[vpg_peaks]) if len(vpg_peaks) > 0 else 0
            vpg_min = np.min(vpg)
            
            # APG Features (a, b, c, d, e waves)
            a_wave = np.max(apg)
            b_wave = np.min(apg)
            # Genuine signal-derived APG distributions instead of hardcoded synthetic fractions.
            # Using percentiles of the APG to capture secondary wave morphology organically.
            pos_apg = apg[apg > 0]
            neg_apg = apg[apg < 0]
            c_wave = np.percentile(pos_apg, 75) if len(pos_apg) > 0 else 0
            d_wave = -np.percentile(np.abs(neg_apg), 75) if len(neg_apg) > 0 else 0
            e_wave = np.percentile(pos_apg, 25) if len(pos_apg) > 0 else 0
            
            # APG Ratios (Vascular Aging Indices)
            b_a_ratio = b_wave / a_wave if a_wave != 0 else 0
            c_a_ratio = c_wave / a_wave if a_wave != 0 else 0
            d_a_ratio = d_wave / a_wave if a_wave != 0 else 0
            e_a_ratio = e_wave / a_wave if a_wave != 0 else 0
            aging_index = (b_wave - c_wave - d_wave - e_wave) / a_wave if a_wave != 0 else 0
            
            # 4. Statistical Moments
            mean_val = np.mean(smoothed)
            std_val = np.std(smoothed)
            skew_val = scipy_skew(smoothed)
            kurt_val = scipy_kurtosis(smoothed)
            iqr_val = iqr(smoothed)
            
            # 5. Frequency Domain (FFT)
            fft_vals = np.abs(fft(smoothed))
            freqs = np.fft.fftfreq(len(smoothed), 1/fs)
            pos_freqs = freqs[freqs > 0]
            pos_fft = fft_vals[freqs > 0]
            
            peak_freq = pos_freqs[np.argmax(pos_fft)]
            power_total = np.sum(pos_fft**2)
            spectral_centroid = np.sum(pos_freqs * pos_fft) / np.sum(pos_fft) if np.sum(pos_fft) > 0 else 0
            
            # Assign exactly 31 features
            features[0] = hr
            features[1] = np.mean(smoothed[peaks]) # Systolic Amplitude
            features[2] = np.min(smoothed)         # Diastolic Amplitude
            features[3] = np.mean(rr_intervals)    # Peak to Peak Time
            features[4] = hr / 60.0                # Cycle duration
            features[5] = vpg_max
            features[6] = vpg_min
            features[7] = a_wave
            features[8] = b_wave
            features[9] = c_wave
            features[10] = d_wave
            features[11] = e_wave
            features[12] = b_a_ratio
            features[13] = c_a_ratio
            features[14] = d_a_ratio
            features[15] = e_a_ratio
            features[16] = aging_index
            features[17] = mean_val
            features[18] = std_val
            features[19] = skew_val
            features[20] = kurt_val
            features[21] = iqr_val
            features[22] = peak_freq
            features[23] = power_total
            features[24] = spectral_centroid
            # Genuine Frequency Sub-Bands (Power in specific physiological Hz ranges)
            band1 = np.sum(fft_vals[(freqs > 0) & (freqs <= 0.5)]**2)    # VLF
            band2 = np.sum(fft_vals[(freqs > 0.5) & (freqs <= 1.5)]**2)  # LF (Heart rate range)
            band3 = np.sum(fft_vals[(freqs > 1.5) & (freqs <= 3.0)]**2)  # HF
            band4 = np.sum(fft_vals[(freqs > 3.0) & (freqs <= 8.0)]**2)  # VHF
            band5 = np.sum(fft_vals[(freqs > 8.0) & (freqs <= 20.0)]**2) # High1
            band6 = np.sum(fft_vals[(freqs > 20.0)]**2)                  # High2
            features[25:31] = [band1, band2, band3, band4, band5, band6]

            return features
        except Exception as e:
            print(f"Feature Extraction Error: {e}")
            return features

    def _is_noisy(self, segment, fs=120):
        """
        The noise gate from danger.py.
        Checks for flat lines, saturation, and physiological feasibility.
        """
        if len(segment) < fs * 2: 
            return True
        
        # 1. Flat line check
        if np.max(segment) - np.min(segment) < 0.05: 
            return True
        
        # 2. Saturation/Clipping check
        # If more than 20% of the signal is "stuck" at the rails (0 or 1)
        if np.mean(segment > 0.95) > 0.2 or np.mean(segment < 0.05) > 0.2: 
            return True
        
        # 3. Frequency check (FFT)
        # Ensure the power is primarily in the human heart-rate range (0.5 - 4.0 Hz)
        fft_vals = np.abs(fft(segment))
        freqs = np.fft.fftfreq(len(segment), 1/fs)
        hr_mask = (freqs >= 0.5) & (freqs <= 4.0)
        hr_power = np.sum(fft_vals[hr_mask])
        total_power = np.sum(fft_vals[freqs > 0])
        
        if total_power == 0 or (hr_power / total_power) < 0.3:
            return True
            
        return False

    def _run_bp_logic(self, features_2d):
        """Internal BP pipeline: Scale -> Classify -> Specific Regression."""
        if "classifier" not in self.models or "global_scaler" not in self.models:
            return None, None, "Unknown"

        # 1. Scale and Classify
        X_scaled = self.models["global_scaler"].transform(features_2d)
        clf_bundle = self.models["classifier"]
        
        if isinstance(clf_bundle, dict) and "model" in clf_bundle:
            cat_int = clf_bundle["model"].predict(X_scaled)[0]
            category = clf_bundle["int_to_label"].get(cat_int, "normal")
        else:
            category = clf_bundle.predict(X_scaled)[0]

        # 2. Regression
        cat_lower = str(category).lower()
        reg_key = f"{cat_lower}"
        scaler_key = f"scaler_{cat_lower}"

        if reg_key in self.models and scaler_key in self.models:
            X_cat_scaled = self.models[scaler_key].transform(features_2d)
            reg_bundle = self.models[reg_key]
            sbp = reg_bundle['sbp_model'].predict(X_cat_scaled)[0]
            dbp = reg_bundle['dbp_model'].predict(X_cat_scaled)[0]
            return sbp, dbp, str(category).title()
        
        return None, None, str(category).title()

    def _run_hb_glu_logic(self, features, age, gender, bmi, offsets=None):
        """Internal Hb/Glucose pipeline using demographics."""
        age_val    = float(age) if (age and 0 < float(age) < 120) else 35.0
        gender_bin = 1.0        if str(gender).lower() == "male" else 0.0
        bmi_val    = float(bmi) if (bmi and 10 < float(bmi) < 80) else 24.0
        
        demo = [age_val, age_val**2, 1.0 if age_val > 60 else 0.0, gender_bin, age_val * gender_bin, bmi_val]
        hg_features = list(features[:21]) + demo
        hg_features_2d = np.array(hg_features, dtype=float).reshape(1, -1)

        hb_res, glu_res = None, None

        if "hb_model" in self.models and "hb_scaler" in self.models:
            X_hb = self.models["hb_scaler"].transform(hg_features_2d)
            hb_val = float(self.models["hb_model"].predict(X_hb)[0])
            if offsets: hb_val += offsets.get('hb', 0.0)
            hb_res = round(max(8.0, min(20.0, hb_val)), 1)

        if "glucose_model" in self.models and "glucose_scaler" in self.models:
            X_glu = self.models["glucose_scaler"].transform(hg_features_2d)
            glu_val = float(self.models["glucose_model"].predict(X_glu)[0])
            if offsets: glu_val += offsets.get('glucose', 0.0)
            glu_res = int(round(max(40.0, min(400.0, glu_val))))

        return hb_res, glu_res

    def analyze(self, pleth_array, fs=120, age=35, gender="Male", bmi=24, offsets=None, adm_id="UNKNOWN"):
        """
        Segmented inference: splits 30s into 5s windows to match training.
        Aggregates results using Median for robustness.
        """
        if adm_id not in self._trend_trackers:
            self._trend_trackers[adm_id] = GenericTrendTracker()
        trend_tracker = self._trend_trackers[adm_id]

        results = {
            "bp": "Unknown", "category": "Unknown", "hb": "N/A", "glucose": "N/A",
            "signal_quality": "Good", "trend": trend_tracker.get_trend(),
            "valid_segments": 0, "total_segments": 0
        }

        seg_len = 5 * fs  # 5 seconds per segment (600 samples)
        total_segments = len(pleth_array) // seg_len
        results["total_segments"] = total_segments

        sbp_list, dbp_list, hb_list, glu_list, cat_list = [], [], [], [], []

        for i in range(total_segments):
            segment = pleth_array[i*seg_len : (i+1)*seg_len]
            
            # 1. Noise Gating
            if self._is_noisy(segment, fs):
                continue

            # 2. Feature Extraction
            features = self._extract_31_features(segment, fs)
            if np.all(features == 0): 
                continue
            
            # 3. Model Execution
            sbp, dbp, category = self._run_bp_logic(features.reshape(1, -1))
            hb, glu = self._run_hb_glu_logic(features, age, gender, bmi, offsets)

            if sbp is not None:
                sbp_list.append(sbp)
                dbp_list.append(dbp)
                cat_list.append(category)
            if hb is not None: 
                hb_list.append(hb)
            if glu is not None: 
                glu_list.append(glu)

        # 4. Aggregation (Median)
        valid_count = len(sbp_list)
        results["valid_segments"] = valid_count

        if valid_count == 0:
            results["signal_quality"] = "Poor"
            return results

        final_sbp = np.median(sbp_list)
        final_dbp = np.median(dbp_list)
        
        trend_tracker.update(final_sbp)

        results.update({
            "bp": f"{int(round(final_sbp))}/{int(round(final_dbp))}",
            "sbp": final_sbp,
            "dbp": final_dbp,
            "category": max(set(cat_list), key=cat_list.count) if cat_list else "Unknown",
            "hb": round(np.median(hb_list), 1) if hb_list else "N/A",
            "glucose": int(np.median(glu_list)) if glu_list else "N/A",
            "signal_quality": "Good" if valid_count >= (total_segments // 2) else "Fair",
            "trend": trend_tracker.get_trend()
        })

        return results