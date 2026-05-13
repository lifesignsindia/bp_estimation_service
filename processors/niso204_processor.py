import numpy as np
from scipy.signal import medfilt

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

    def process(self, raw_pleth):
        """Applies median filter to despike the raw NISO 204 wave."""
        if not raw_pleth or len(raw_pleth) < self.kernel_size:
            return []
            
        arr = np.array(raw_pleth, dtype=float)
        
        # Apply the median filter to despike the wave
        clean_signal = medfilt(arr, kernel_size=self.kernel_size)
        
        # We return the despiked array. It will be resampled to 120Hz 
        # in the main router file.
        return clean_signal