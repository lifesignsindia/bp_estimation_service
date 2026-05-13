import numpy as np
from scipy import signal
from typing import Optional, Tuple
from config import SETTINGS

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
        if self.zi is None:
            # Steady-state continuous state vector initialized at x[0] level
            self.zi = signal.sosfilt_zi(self.sos) * x[0]

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
