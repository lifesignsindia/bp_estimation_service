# NISO 103 (Checkme) Signal Processing Guide

This document explains the "Hardened" signal processing pipeline for the **Checkme O2 (NISO 103)** device, specifically designed for high-fidelity Blood Pressure estimation.

## 1. Hardware Challenges
*   **Sampling Rate:** 125 Hz (Auto-sensing).
*   **Resolution:** 8-bit integers (0 - 255). 
*   **Artifacts:** Prone to "Vertical Cliffs" (Sentinels) and "Flat Tops" (ADC Saturation).

## 2. Hardening Pipeline (The "Hardened" Chain)

### Step 1: Sentinel Cleaning
The device uses `154-160` as hardware error codes (finger off). 
*   **Method:** These values are detected and bridged using linear interpolation.
*   **Result:** Prevents massive filter "ringing" oscillations.

### Step 2: Median Filtering
*   **Method:** A small-window median filter is applied to the raw data.
*   **Result:** Removes impulsive "salt and pepper" glitches before bandpassing.

### Step 3: Piecewise Linear Detrending
Handles the "Baseline Shift" caused by breathing.
*   **Method:** Splits the 30s window into 3 segments and removes individual slopes.

### Step 4: 4th-Order Zero-Phase Bandpass (0.4 – 4.0 Hz)
*   **Filter:** **4th-Order** Butterworth (0.4 Hz high-pass).
*   **Result:** A perfectly stable, centered pulse wave optimized for Viatom/Checkme hardware.

### Step 5: Percentile Normalization [0, 1]
*   **Method:** Percentile mapping [2, 98] to [0, 1].
*   **Result:** Standardizes the output for AI models.

## 3. Usage for JSON Payloads
```python
from processors.niso103_processor import PlethProcessor
proc = PlethProcessor(fs=125.0)
output = proc.process_niso103_payload(json_payload)
# output['pleth'] contains the reconstructed, clinical waveform
```
