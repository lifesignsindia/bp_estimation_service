import numpy as np
from scipy import signal
from scipy.signal import medfilt
from typing import Optional, Tuple
from config import SETTINGS


def _compute_sqi_berry(raw, despiked):
    """
    Signal quality check for BerryMed (NISO101) — same logic as NISO204.
    'raw'     : original ADC array (pre-despike)
    'despiked': after median filter, before detrend/normalize
    """
    raw_arr = np.array(raw, dtype=float)

    # 1. Saturation: >2% of samples stuck at exact ADC maximum
    if len(raw_arr) > 0 and np.max(raw_arr) > 0:
        if np.mean(raw_arr == np.max(raw_arr)) > 0.02:
            return 0.2, False, "SATURATED"

    # 2. Amplitude: negligible pulsatile swing → probe off / poor contact
    amplitude = float(np.percentile(despiked, 98) - np.percentile(despiked, 2))
    if amplitude < 0.05 * np.mean(np.abs(despiked)) + 1e-6:
        return 0.0, False, "POOR_CONTACT"

    # 3. Motion: sudden baseline step mid-window
    window = max(10, len(despiked) // 20)
    smoothed = np.convolve(despiked, np.ones(window) / window, mode='same')
    step = float(np.max(np.abs(np.diff(smoothed))))
    if amplitude > 1e-6 and step > amplitude * 0.5:
        return 0.5, False, "MOTION_DETECTED"

    return 1.0, True, "GOOD"


class BerryMed204Processor:
    """
    Clean block processor for BerryMed (NISO101) finger probe.

    Matches NISO204 preprocessing so the AI model sees familiar morphology
    (model was trained exclusively on NISO204 data).

    Pipeline:
        1. Median despike  (kernel=5) — removes hardware/BT spike artifacts
        2. SQI check       — catches flat line, saturation, motion BEFORE detrend
        3. Piecewise detrend (3 segments) — removes initial DC offset + baseline wander
        4. Percentile normalize [0, 1] — matches NISO204 output scale

    No IIR/FIR bandpass — NISO204 training data had none; applying one shifts
    BerryMed morphology away from the training distribution.
    """

    def __init__(self, kernel_size: int = 5):
        self.kernel_size = kernel_size

    def process(self, raw_pleth: list) -> tuple:
        """
        Returns (clean_signal: list[float], sqi_info: dict)
        Same interface as NISO204Processor.process().
        """
        if not raw_pleth or len(raw_pleth) < self.kernel_size:
            return [], {"score": 0.0, "valid": False, "flag": "INSUFFICIENT_DATA"}

        arr = np.array(raw_pleth, dtype=float)

        # 1. Median despike
        despiked = medfilt(arr, kernel_size=self.kernel_size)

        # 2. SQI on raw/despiked (before normalize — catches probe-off / motion)
        sqi_score, sqi_valid, sqi_flag = _compute_sqi_berry(arr, despiked)
        sqi_info = {"score": sqi_score, "valid": sqi_valid, "flag": sqi_flag}

        # 3. Percentile normalize to [0, 1]
        #    p2/p98 clipping handles DC offset naturally — even if the first
        #    few seconds are unsettled, the bulk of the 30s window drives the
        #    percentiles to the correct range. No detrend needed: it removes
        #    respiratory modulation which the model uses for feature extraction.
        p2, p98 = np.percentile(despiked, [2, 98])
        if p98 - p2 > 1e-6:
            clean = np.clip((despiked - p2) / (p98 - p2), 0, 1)
        else:
            clean = despiked

        return list(clean), sqi_info

class PPGFilter:
    """
    High-precision, real-time stateful PPG filter using Second-Order Sections (SOS).
    Provides absolute biquad numerical stability for IIR designs.
    """
    def __init__(self, fs: float = 200.0, lowcut: float = 0.5, highcut: float = 8.0, order: int = 2):
        self.fs = fs
        self.lowcut = lowcut
        self.highcut = highcut
        self.order = order

        # Design the filter using Second-Order Sections (SOS) for optimal numerical precision
        nyq = 0.5 * fs
        low = lowcut / nyq
        high = highcut / nyq
        
        self.sos = signal.butter(order, [low, high], btype='bandpass', output='sos')

        # Will be initialized to steady-state using the first sample to avoid startup transients
        self.zi = None

    def process(self, samples: list[int]) -> list[float]:
        """
        Processes a block of samples through the SOS filter cascade, maintaining 
        continuous state to eliminate packet boundary artifacts.
        """
        if not samples:
            return []

        x = np.array(samples, dtype=float)

        # Median pre-filter: removes single-sample spikes before they corrupt
        # the IIR filter state (which is initialised from x[0])
        from scipy.signal import medfilt as _medfilt
        x = _medfilt(x, kernel_size=15)

        # Remove DC offset BEFORE filtering — bandpass steady-state for DC is 0,
        # so subtracting the mean first means zi=0 is the correct initial condition
        # and avoids startup transients regardless of raw ADC magnitude (~574K or ~1.2M).
        x = x - np.mean(x)

        if self.zi is None:
            self.zi = signal.sosfilt_zi(self.sos) * 0.0

        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        return y.tolist()


class PPGFIRFilter:
    """
    Genuine Linear-Phase FIR (Finite Impulse Response) Bandpass Filter.
    Designed using windowing via scipy.signal.firwin.
    """
    def __init__(self, fs: float = 200.0, lowcut: float = 0.5, highcut: float = 40.0, numtaps: int = 101):
        self.fs = fs
        self.lowcut = lowcut
        self.highcut = highcut
        self.numtaps = numtaps if numtaps % 2 != 0 else numtaps + 1  # Must be odd for bandpass

        # Design FIR bandpass coefficients (only b, no feedback a)
        self.b = signal.firwin(self.numtaps, [lowcut, highcut], pass_zero=False, fs=fs)
        self.a = np.array([1.0], dtype=float)

        # Will be initialized to steady-state on first sample
        self.zi = None

    def process(self, samples: list[float]) -> list[float]:
        if not samples:
            return []
        x = np.array(samples, dtype=float)
        if self.zi is None:
            # Initialize to steady-state at x[0]
            self.zi = signal.lfilter_zi(self.b, self.a) * x[0]
        y, self.zi = signal.lfilter(self.b, self.a, x, zi=self.zi)
        return y.tolist()


class BlockProcessor:
    """
    Maintains a rolling buffer representing a fixed historical duration (e.g., 30 seconds).
    Once full, it filters the entire block of raw data as a single continuous block 
    using offline/zero-phase methods.
    """
    def __init__(self, duration_sec: float = 30.0, fs: float = 200.0):
        self.fs = fs
        self.capacity = int(duration_sec * fs)
        self.raw_buffer = []

    def feed(self, samples: list[int]) -> Tuple[list[float], Optional[list[float]]]:
        """
        Feed new samples. Returns:
          (rolling_raw_samples, filtered_block_samples_or_None)
        """
        self.raw_buffer.extend(samples)
        if len(self.raw_buffer) > self.capacity:
            self.raw_buffer = self.raw_buffer[-self.capacity:]

        # If we don't have enough data yet, return raw buffer so far and None for filtered
        if len(self.raw_buffer) < self.capacity:
            return list(map(float, self.raw_buffer)), None

        # Filter the full block using configured filter
        raw_arr = np.array(self.raw_buffer, dtype=float)
        filtered = None

        # ─── THE ROBUST BASELINE SHIFT ───
        # Piecewise linear detrend to snap the wave back to center
        # Removes the linear slope from 3 segments of the 30s window
        n = len(raw_arr)
        breakpoints = [n // 3, 2 * n // 3]
        processed_input = signal.detrend(raw_arr, type='linear', bp=breakpoints)
        
        # Restore mean for visualization consistency
        processed_input = processed_input + np.mean(raw_arr)

        if SETTINGS.block_filter_type == "fir":
            # True linear-phase FIR filtering via zero-phase filtfilt with FIR coefficients
            numtaps = SETTINGS.block_fir_numtaps
            if numtaps % 2 == 0:
                numtaps += 1
            b = signal.firwin(numtaps, [SETTINGS.block_filter_lowcut, SETTINGS.block_filter_highcut], pass_zero=False, fs=self.fs)
            filtered = signal.filtfilt(b, [1.0], processed_input).tolist()
        else:
            # Butterworth zero-phase filtfilt filtering
            nyq = 0.5 * self.fs
            low = SETTINGS.block_filter_lowcut / nyq
            high = SETTINGS.block_filter_highcut / nyq
            b, a = signal.butter(SETTINGS.filter2_order, [low, high], btype='bandpass')
            filtered = signal.filtfilt(b, a, processed_input).tolist()

        return list(map(float, self.raw_buffer)), filtered


class PPGProcessor:
    """
    Unified real-time PPG Signal Processor.
    Encapsulates real-time filters and 30-second block processors.
    """
    def __init__(self, fs: float = 200.0):
        # Main Filter: Real-time 4th-order Butterworth Bandpass (Customizable)
        self.filter = PPGFilter(
            fs=fs, 
            lowcut=SETTINGS.filter2_lowcut, 
            highcut=SETTINGS.filter2_highcut, 
            order=SETTINGS.filter2_order
        )

        # Block processor (Default 30 seconds)
        self.block_proc = BlockProcessor(
            duration_sec=SETTINGS.block_duration_sec, 
            fs=fs
        )

    def process(self, samples: list[int]) -> dict:
        """
        Processes raw samples.
        Returns a dict of results for WebSocket and storage.
        """
        f_out = self.filter.process(samples)
        
        # Feed the block processor
        raw_block, filt_block = self.block_proc.feed(samples)

        return {
            "filtered": f_out,
            "raw_block": raw_block,
            "filt_block": filt_block, # Only non-None when block is fully populated
        }

    def process_niso101_payload(self, payload: dict) -> dict:
        """
        Entry point for NISO 101 (BerryMed) JSON payloads with Auto-Sensing FS.
        Extracts waveform, applies robust baseline shift, and normalizes.
        """
        # 1. Extract waveform from nested 'pleth' or 'raw' keys
        raw_samples = payload.get('pleth', {}).get('plethWave') or payload.get('samples')
        if not raw_samples:
            return {"error": "No waveform data found"}

        # AUTO-SENSE SAMPLING RATE:
        # Assuming most payloads are 30-second epochs.
        num_samples = len(raw_samples)
        detected_fs = round(num_samples / 30) # e.g. 3000 -> 100, 6000 -> 200
        
        raw_arr = np.array(raw_samples, dtype=float)
        
        # --- THE ROBUST BASELINE SHIFT ---
        n = len(raw_arr)
        breakpoints = [n // 3, 2 * n // 3]
        detrended = signal.detrend(raw_arr, type='linear', bp=breakpoints)
        detrended = detrended + np.mean(raw_arr)

        # --- BANDPASS AT DETECTED FS ---
        nyq = 0.5 * detected_fs
        low = SETTINGS.block_filter_lowcut / nyq
        high = SETTINGS.block_filter_highcut / nyq
        b, a = signal.butter(SETTINGS.filter2_order, [low, high], btype='band')
        filtered = signal.filtfilt(b, a, detrended)

        # --- PERCENTILE NORMALIZATION [0, 1] ---
        f_min, f_max = np.percentile(filtered, [2, 98])
        if f_max - f_min > 1e-6:
            normalised = np.clip((filtered - f_min) / (f_max - f_min), 0, 1)
        else:
            normalised = np.full_like(filtered, 0.5)

        return {
            "deviceID": payload.get('deviceID', 'BERRY_UNKNOWN'),
            "detected_fs": detected_fs,
            "hr_bpm": np.mean(payload.get('spo2', {}).get('pulseRate', [0])),
            "spo2_pct": np.mean(payload.get('spo2', {}).get('spo2', [0])),
            "pleth": normalised.tolist()
        }
