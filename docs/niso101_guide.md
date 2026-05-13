# NISO 101 (BerryMed) Signal Processing Guide

This document explains the high-fidelity signal conditioning pipeline for the **BerryMed (NISO 101)** PPG sensor.

## 1. Hardware Characteristics
*   **Sampling Rate:** 200 Hz (Auto-sensed based on 30s epoch length).
*   **Resolution:** 32-bit signed integers (High dynamic range).
*   **Data Format:** Raw ADC counts.

## 2. Processing Pipeline (The "Robust" Chain)

### Step 1: Piecewise Linear Detrending (The Baseline Shift)
Because PPG signals are sensitive to breathing and motion, the baseline often "drifts" or slants.
*   **Method:** The 30-second window is split into 3 segments.
*   **Math:** A linear regression is performed on each segment to find the "drift slope," which is then subtracted.
*   **Result:** The waveform is "snapped" back to a flat horizontal line without losing heart rate detail.

### Step 2: 4th-Order Zero-Phase Butterworth Bandpass (0.5 – 40.0 Hz)
*   **0.5 Hz Low-Cut:** Removes slow-wave baseline wander.
*   **40.0 Hz High-Cut:** Removes high-frequency muscle tremors and electrical noise while preserving the full pulse morphology.
*   **Order:** **4th-Order** design (effectively 8th-order due to zero-phase double pass).
*   **Zero-Phase:** Uses `filtfilt` to ensure the wave is not shifted in time, keeping the systolic peak timing 100% accurate.

### Step 3: Percentile-Based Normalization
To ensure the Blood Pressure model sees the same scale for every user:
*   **Method:** Finds the 2nd and 98th percentiles of the wave.
*   **Math:** $Y_{norm} = \frac{Y - P_2}{P_{98} - P_2}$
*   **Result:** A pristine waveform scaled from **0.0 to 1.0**.

## 3. Usage for JSON Payloads
To process a NISO 101 JSON payload:
```python
from processors.niso101_processor import PPGProcessor
proc = PPGProcessor(fs=200.0)
output = proc.process_niso101_payload(json_payload)
# output['pleth'] is the final clean signal
```
