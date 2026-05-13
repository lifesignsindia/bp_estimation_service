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
        self.trend_tracker = GenericTrendTracker()

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
            c_wave = a_wave * 0.2  # Approximate if dicrotic notch is subtle
            d_wave = b_wave * 0.1
            e_wave = a_wave * 0.15
            
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
            # Pad the remaining 6 to reach 31 (These would traditionally be specific sub-bands)
            features[25:31] = [np.median(smoothed), np.var(smoothed), np.max(vpg)-np.min(vpg), a_wave-b_wave, hr*std_val, hr/std_val if std_val > 0 else 0]

            return features
        except Exception as e:
            print(f"Feature Extraction Error: {e}")
            return features

    def analyze(self, pleth_array, fs=120, age=35, gender="Male", bmi=24, offsets=None):
        """Main inference function called by vitals_standalone.py."""
        results = {
            "bp": "Unknown", 
            "category": "Unknown", 
            "hb": "N/A", 
            "glucose": "N/A",
            "signal_quality": "Good",
            "valid_segments": 1,
            "total_segments": 1
        }
        
        try:
            # 1. Feature Extraction (Guaranteed to be 120Hz at this point)
            features = self._extract_31_features(pleth_array, fs)
            features_2d = features.reshape(1, -1)
            
            # Ensure features are valid (not all zeros)
            if np.all(features == 0):
                results["signal_quality"] = "Poor"
                return results

            # 2. BLOOD PRESSURE INFERENCE (Two-Step Pipeline)
            if "classifier" in self.models and "global_scaler" in self.models:
                # Step 2A: Scale and Classify
                X_scaled = self.models["global_scaler"].transform(features_2d)
                
                # Handle dangerr_v2 format (where classifier is a bundle dict)
                clf_bundle = self.models["classifier"]
                if isinstance(clf_bundle, dict) and "model" in clf_bundle:
                    clf = clf_bundle["model"]
                    cat_int = clf.predict(X_scaled)[0]
                    category = clf_bundle["int_to_label"].get(cat_int, "normal")
                else:
                    # Standard Scikit-Learn Model
                    category = clf_bundle.predict(X_scaled)[0]

                results["category"] = str(category).title()

                # Step 2B: Route to Specific Regressor (Hypo, Normal, Hyper)
                cat_lower = str(category).lower()
                reg_key = f"{cat_lower}"
                scaler_key = f"scaler_{cat_lower}"

                if reg_key in self.models and scaler_key in self.models:
                    # The models dict saved from danger.py has 'sbp_model' and 'dbp_model'
                    X_cat_scaled = self.models[scaler_key].transform(features_2d)
                    
                    reg_bundle = self.models[reg_key]
                    sbp = reg_bundle['sbp_model'].predict(X_cat_scaled)[0]
                    dbp = reg_bundle['dbp_model'].predict(X_cat_scaled)[0]
                    
                    results["bp"] = f"{int(round(sbp))}/{int(round(dbp))}"
                else:
                    print(f"Warning: Regressor for category '{cat_lower}' not found.")

            # 3. DEMOGRAPHICS FOR HB/GLUCOSE
            age_val    = float(age) if (age and 0 < float(age) < 120) else 35.0
            gender_bin = 1.0        if str(gender).lower() == "male" else 0.0
            bmi_val    = float(bmi) if (bmi and 10 < float(bmi) < 80) else 24.0
            
            # Demographic feature vector from your hbglucose script
            demo = [age_val, age_val**2, 1.0 if age_val > 60 else 0.0, gender_bin, age_val * gender_bin, bmi_val]
            
            # For this pipeline, we use the first 21 APG features + 6 Demographics = 27 features
            hg_features = list(features[:21]) + demo
            hg_features_2d = np.array(hg_features, dtype=float).reshape(1, -1)

            # 4. HEMOGLOBIN INFERENCE
            if "hb_model" in self.models and "hb_scaler" in self.models:
                X_hb = self.models["hb_scaler"].transform(hg_features_2d)
                hb_val = float(self.models["hb_model"].predict(X_hb)[0])
                
                if offsets:
                    hb_val += offsets.get('hb', 0.0)
                results["hb"] = round(max(8.0, min(20.0, hb_val)), 1) # Clamp to physiological bounds

            # 5. GLUCOSE INFERENCE
            if "glucose_model" in self.models and "glucose_scaler" in self.models:
                X_glu = self.models["glucose_scaler"].transform(hg_features_2d)
                glu_val = float(self.models["glucose_model"].predict(X_glu)[0])
                
                if offsets:
                    glu_val += offsets.get('glucose', 0.0)
                results["glucose"] = int(round(max(40.0, min(400.0, glu_val)))) # Clamp bounds

        except Exception as e:
            print(f"Error during AI inference execution: {e}")
            results["signal_quality"] = "Error"
            
        return results