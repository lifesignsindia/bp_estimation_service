import numpy as np
from scipy.signal import medfilt

def compute_sqi_204(raw, clean):
    """
    Multi-factor signal quality assessment for NISO 204.
    'raw' is the original ADC array.
    'clean' is the despiked (but NOT yet normalized) array.
    """
    raw_arr = np.array(raw, dtype=float)
    
    # 1. Saturation check on RAW (pre-normalization) values
    # True digital clipping: >2% of samples stuck at the exact ADC maximum
    if len(raw_arr) > 0 and np.max(raw_arr) > 0 and np.mean(raw_arr == np.max(raw_arr)) > 0.02:
        return 0.2, False, "SATURATED"
        
    # 2. Amplitude check on the despiked signal
    # If the pulsatile swing is negligible, it's likely poor contact.
    amplitude = float(np.percentile(clean, 98) - np.percentile(clean, 2))
    if amplitude < 0.05:  # Flat line or extremely low signal
        return 0.0, False, "POOR_CONTACT"
        
    # 3. Baseline step check (Motion detection)
    window = max(10, len(clean) // 20)
    smoothed = np.convolve(clean, np.ones(window) / window, mode='same')
    step = float(np.max(np.abs(np.diff(smoothed))))
    if step > amplitude * 0.5:
        return 0.5, False, "MOTION_DETECTED"
        
    return 1.0, True, "GOOD"


class NISO204Processor:
    """
    Lightweight processor for the high-fidelity NISO 204 reference monitor.
    Because the downstream AI was natively trained on this device's morphology,
    we only apply a median filter to remove 1-point Bluetooth/hardware spikes,
    preserving the exact vascular features (like the dicrotic notch).
    """
    def __init__(self, kernel_size=5):
        # A kernel of 5 is perfect for 200Hz data. It wipes out sudden 
        # spikes without blurring the sharp edges of the heartbeat.
        self.kernel_size = kernel_size

    def normalize(self, signal):
        """
        Percentile-based normalization (2nd and 98th percentiles).
        Protects the scale from being ruined by random outlier spikes.
        """
        p2, p98 = np.percentile(signal, [2, 98])
        if p98 - p2 == 0:
            return signal
        return np.clip((signal - p2) / (p98 - p2), 0, 1)

    def process(self, raw_pleth):
        """Advanced Pipeline with Quality Awareness for NISO 204."""
        if not raw_pleth or len(raw_pleth) < self.kernel_size:
            return [], {"score": 0.0, "valid": False, "flag": "INSUFFICIENT_DATA"}
            
        arr = np.array(raw_pleth, dtype=float)
        
        # 1. Median filter to despike (preserving features)
        despiked = medfilt(arr, kernel_size=self.kernel_size)
        
        # 2. RUN SQI BEFORE NORMALIZATION
        # We pass the raw array and the despiked version
        sqi_score, sqi_valid, sqi_flag = compute_sqi_204(arr, despiked)
        sqi_info = {"score": sqi_score, "valid": sqi_valid, "flag": sqi_flag}
        
        # 3. Normalize for AI
        clean_signal = self.normalize(despiked)
        
        return clean_signal, sqi_info