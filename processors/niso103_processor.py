import numpy as np
from scipy.signal import butter, filtfilt, medfilt, detrend
from scipy.interpolate import CubicSpline

def clean_checkme_sentinel(raw):
    """
    Enhanced sentinel cleaning for CHECKME devices.
    1. Detects values in the range 154-160 (finger-off).
    2. Interpolates short gaps (<= 1s).
    3. Replaces long gaps with the signal mean to avoid filtering artifacts and scaling issues.
    """
    arr = np.array(raw, dtype=float)
    # Use a range check to account for hardware jitter
    sentinel_mask = (arr >= 154) & (arr <= 160)
    
    # Calculate mean of valid signal for long-run replacement
    valid_data = arr[~sentinel_mask]
    signal_mean = np.mean(valid_data) if len(valid_data) > 0 else 80.0
    
    i = 0
    while i < len(arr):
        if sentinel_mask[i]:
            # find end of run
            j = i
            while j < len(arr) and sentinel_mask[j]:
                j += 1
            run_len = j - i
            
            if run_len <= 120:  # short run (<= 1s at 120Hz) -> interpolate
                left  = arr[i-1] if i > 0 else (arr[j] if j < len(arr) else signal_mean)
                right = arr[j]   if j < len(arr) else left
                for k in range(i, j):
                    t = (k - i + 1) / (run_len + 1)
                    arr[k] = left + t * (right - left)
            else:
                # long run -> force to signal mean to avoid scaling/filtering issues
                arr[i:j] = signal_mean
            i = j
        else:
            i += 1
    return arr


def reconstruct_clipped_peaks(arr):
    """
    Detects flat plateaus at the top of the waveform (Systolic Clipping)
    and reconstructs the missing natural peak using Cubic Spline interpolation.
    """
    arr = np.array(arr, dtype=float)
    mean_val = np.mean(arr)
    
    i = 0
    while i < len(arr) - 1:
        # Detect if the signal is high (above average) AND perfectly flat (difference is near 0)
        if arr[i] > mean_val and abs(arr[i+1] - arr[i]) < 1e-4:
            j = i + 1
            while j < len(arr) and abs(arr[j] - arr[i]) < 1e-4:
                j += 1
            
            run_len = j - i
            
            # If the flat top is short enough to safely rebuild (e.g., 2 to 15 samples at 120Hz)
            if 2 <= run_len <= 15:
                # Grab 6 valid points before the clip and 6 valid points after the clip
                pad = 6
                if i - pad >= 0 and j + pad < len(arr):
                    x_known = list(range(i - pad, i)) + list(range(j, j + pad))
                    y_known = [arr[idx] for idx in x_known]
                    
                    # Fit a cubic spline curve to those surrounding points
                    cs = CubicSpline(x_known, y_known)
                    
                    # Predict what the missing peak should have looked like
                    x_missing = list(range(i, j))
                    arr[i:j] = cs(x_missing)
            i = j
        else:
            i += 1
            
    return arr


def compute_signal_quality(cleaned, filtered, fs=120):
    """
    Multi-factor signal quality assessment.
    
    Returns:
        quality_score (float): 0.0 - 1.0
        is_valid (bool): Whether the epoch should be saved
        quality_flag (str): Human-readable reason
        ac_amplitude (float): Peak-to-trough swing of the filtered signal
    """
    ac_amplitude = float(np.percentile(filtered, 98) - np.percentile(filtered, 2))
    
    # --- Check 1: Flat line (any cause, not just sentinel) ---
    # After bandpass, a flat input becomes near-zero. AC amplitude < 0.5 ADC units
    # means there is no real pulsatile signal.
    if ac_amplitude < 0.5:
        return 0.0, False, "FLAT_LINE", ac_amplitude
    
    # --- Check 2: ADC saturation (pressed too hard or sensor glitch) ---
    # Real saturation = the signal is PINNED at a rail for a large share of samples.
    # A healthy CHECKME PPG (0-168 range) dips near 0 on every beat, so a low trough
    # value is NORMAL — flagging "< 5" rejected every good epoch. Instead require a
    # big fraction of samples stuck at the extreme (genuine clipping).
    cleaned_arr = np.asarray(cleaned, dtype=float)
    frac_low  = float(np.mean(cleaned_arr <= 1))
    frac_high = float(np.mean(cleaned_arr >= 250))
    if frac_low > 0.20 or frac_high > 0.20:
        return 0.3, False, "SATURATED", ac_amplitude
    
    # --- Check 3: Baseline step detection ---
    # Smooth the filtered signal over 1s, then look for sudden jumps
    # that are large relative to the AC amplitude
    window = max(fs, 1)
    smoothed = np.convolve(filtered, np.ones(window) / window, mode='same')
    step_magnitude = float(np.max(np.abs(np.diff(smoothed))))
    if step_magnitude > ac_amplitude * 0.5:
        return 0.5, False, "BASELINE_STEP", ac_amplitude
    
    return 1.0, True, "GOOD", ac_amplitude


class PlethProcessor:
    def __init__(self, fs=120):
        self.fs = fs
        
    def process_data(self, raw_data, override_fs=None):
        """
        Advanced Pipeline with Quality Awareness.
        """
        # Use provided fs, or fall back to class default
        fs = override_fs if override_fs else self.fs
        
        empty = np.array([])
        bad_quality = {"score": 0.0, "valid": False, "flag": "INSUFFICIENT_DATA", "ac_amplitude": 0.0}
        
        if len(raw_data) < fs: # require at least 1s of data
            return empty, empty, empty, empty, bad_quality

        # 1. Enhanced Sentinel clean
        cleaned = clean_checkme_sentinel(raw_data)

        # 2. Reconstruct Clipped Peaks (Fixes the flat tops)
        unclipped = reconstruct_clipped_peaks(cleaned)

        # 3. Spike removal
        denoised = medfilt(unclipped, kernel_size=5)

        # 3. Piecewise linear detrend
        n = len(denoised)
        breakpoints = [n // 3, 2 * n // 3]
        detrended = detrend(denoised, type='linear', bp=breakpoints)
        detrended = detrended + np.mean(denoised)

        # 4. Bandpass at NATIVE fs
        nyq = 0.5 * fs
        low, high = 0.4 / nyq, 4.0 / nyq
        b, a = butter(4, [low, high], btype='band')
        filtered = filtfilt(b, a, detrended)

        # 5. Signal quality assessment
        q_score, q_valid, q_flag, q_ac = compute_signal_quality(cleaned, filtered, fs)
        quality_info = {"score": q_score, "valid": q_valid, "flag": q_flag, "ac_amplitude": q_ac}

        # 6. Robust Normalise
        f_min, f_max = np.percentile(filtered, [2, 98])
        if f_max - f_min > 1e-6:
            normalised = np.clip((filtered - f_min) / (f_max - f_min), 0, 1)
        else:
            normalised = np.full_like(filtered, 0.5)

        t_axis = np.linspace(0, len(normalised) / fs, len(normalised))

        return normalised, t_axis, denoised, filtered, quality_info

    def process_niso103_payload(self, payload):
        """
        Entry point for NISO 103 JSON with Auto-Sensing Sampling Rate.
        """
        pleth_data = payload.get('pleth', {}).get('plethWave')
        if not pleth_data:
            return None, {"error": "Missing 'plethWave'"}

        # AUTO-SENSE SAMPLING RATE:
        # Assuming most NISO103 payloads are 30-second epochs.
        num_samples = len(pleth_data)
        detected_fs = round(num_samples / 30) # e.g. 3000 -> 100, 3600 -> 120
        
        # Log detection
        # print(f"Processing NISO103 JSON: Detected {detected_fs} Hz ({num_samples} samples over 30s)")

        # Run pipeline with the detected frequency
        results = self.process_data(pleth_data, override_fs=detected_fs)
        normalised, t_axis, _, _, quality = results
        
        pr_list = payload.get('spo2', {}).get('pulseRate', [])
        sp_list = payload.get('spo2', {}).get('spo2', [])
        avg_hr = np.mean(pr_list) if pr_list else 0
        avg_sp = np.mean(sp_list) if sp_list else 0

        return {
            "deviceID": payload.get('deviceID'),
            "sequence": f"{payload.get('seqNum')}.{payload.get('seqPart')}",
            "detected_fs": detected_fs,
            "hr_bpm": round(float(avg_hr), 1),
            "spo2_pct": round(float(avg_sp), 1),
            "quality": quality,
            "pleth": normalised.tolist() if len(normalised) > 0 else []
        }