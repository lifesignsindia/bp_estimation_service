# LifeSigns — NISO204 Device Guide
**Version:** 1.0
**Date:** 2026-05-14
**Scope:** NISO204 payload format, DSP pipeline, SQI, and integration behaviour

---

## 1. What the NISO204 Is

The NISO204 is the primary reference PPG monitor. It is a high-fidelity device, and the AI model was natively trained on NISO204 signal morphology. Because of this, the DSP pipeline for NISO204 is intentionally minimal — only a median despike filter is applied. No heavy filtering is done, which preserves the exact vascular features (such as the dicrotic notch) that the AI model relies on.

---

## 2. Device Detection

A packet is detected as NISO204 if both conditions are true:

```python
str(json_data.get("DeviceName", "")).upper() == "NISO204"
"Pleth" in json_data
```

If either condition is false, the packet is not routed to the NISO204 pipeline. The `DeviceName` check distinguishes NISO204 from CHECKME and BERRYMED devices, which never have `DeviceName="NISO204"`.

---

## 3. Payload Format

A NISO204 pleth packet looks like:

```json
{
  "admissionId": "ADM001",
  "DeviceName":  "NISO204",
  "Pleth":       [512, 518, 525, 530, ...],
  "FS":          200,
  "Age":         45,
  "Gender":      "Male",
  "BMI":         24.5,
  "epochTime":   1747152000
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `admissionId` | Recommended | Patient session identifier. Falls back to `PatId → deviceID → BLEDeviceID → "UNKNOWN_PATIENT"` |
| `DeviceName` | Yes | Must be `"NISO204"` (case-sensitive after `.upper()`) |
| `Pleth` | Yes | Raw ADC pleth waveform array. Also accepted as `PlethWave` |
| `FS` | No | Sampling rate in Hz. Defaults to 200 if absent |
| `Age` | No | Patient age. Defaults to 35 |
| `Gender` | No | `"Male"` or `"Female"`. Defaults to `"Male"` |
| `BMI` | No | Body mass index. Defaults to 24 |
| `epochTime` | No | Unix timestamp — stored with reference BP if cuff type |

**Note:** In production NISO204 packets, there are no `BPSystolic`/`BPDiastolic` fields. The built-in BP measurement from NISO204 hardware is not sent by the BLE gateway and is not used by the pipeline.

---

## 4. DSP Pipeline

The NISO204 uses `NISO204Processor` from `processors/niso204_processor.py`.

### Step 1 — Median filter (despike)

```python
despiked = medfilt(arr, kernel_size=5)
```

A median filter with kernel size 5 removes single-point hardware spikes caused by Bluetooth packet errors or ADC glitches. Kernel size 5 is tuned for 200 Hz data — it removes 1–2 sample spikes without blurring the heartbeat edges.

### Step 2 — SQI (Signal Quality Index)

SQI is computed on the despiked signal **before** normalization, using the raw ADC values for the saturation check:

| Check | Condition | Score | Flag |
|-------|-----------|-------|------|
| Saturation | 99.5th percentile > 98% of max value | 0.2 | `SATURATED` |
| Poor contact | Amplitude (p98 − p2) < 0.05 | 0.0 | `POOR_CONTACT` |
| Motion | Max step in smoothed signal > 50% of amplitude | 0.5 | `MOTION_DETECTED` |
| Good | All checks pass | 1.0 | `GOOD` |

The SQI result is a dict: `{"score": 1.0, "valid": True, "flag": "GOOD"}`. It is included in `success` and `alert` output payloads as the `"sqi"` field.

**SATURATED:** The signal is railing — the ADC has hit its maximum value. Typically happens when the sensor is pressed too hard or there is too much ambient light. The device needs to be repositioned.

**POOR_CONTACT:** The pulsatile swing is negligible — the sensor is likely not in contact with the skin. Reading is invalid.

**MOTION_DETECTED:** The baseline is jumping, indicating the patient moved significantly during the measurement window.

### Step 3 — Normalization

```python
p2, p98 = np.percentile(signal, [2, 98])
clean = np.clip((signal - p2) / (p98 - p2), 0, 1)
```

Percentile-based normalization scales the signal to [0, 1]. This removes ADC scale differences across units and patient populations without affecting the waveform shape.

### Step 4 — Resample to 120 Hz

```python
target_length = int(len(clean_signal) * (120 / source_hz))
model_ready = signal.resample(clean_signal, target_length)
```

The AI model was trained on 120 Hz data. All NISO204 data (default 200 Hz) is resampled to 120 Hz. If the device reports a different native rate via the `FS` field, that value is used for the resample ratio.

---

## 5. Minimum Signal Length

After resampling, the pipeline checks:

```python
if len(model_ready_pleth) < 120:
    return {"status": "error", "message": "Signal too short for AI inference."}
```

120 samples at 120 Hz = 1 second. In practice, a 30-second NISO204 capture at 200 Hz produces 6000 raw samples → 3600 samples after resampling. This is well above the minimum.

---

## 6. Processor Initialization

The NISO204 processor is initialized globally at module load time:

```python
PROCESSORS = {
    "NISO204": NISO204Processor(kernel_size=5),
    ...
}
```

It is stateless (no filter state carried between packets), so a single global instance is safe.

---

## 7. Integration in vitals_standalone.py

```python
elif device_type == DEVICE_NISO204:
    raw_pleth = json_data.get("Pleth", []) or json_data.get("PlethWave", [])
    actual_hz = json_data.get("FS", 200)
```

The signal field is `Pleth` (primary) or `PlethWave` (fallback). The sampling rate is read from the `FS` field (default 200 Hz).

```python
clean_signal, sqi_info = PROCESSORS["NISO204"].process(raw_pleth)
```

The processor returns the normalized, despiked signal and the SQI dict. The resampling to 120 Hz happens afterward in `_preprocess_signal`.

---

## 8. SQI in Output Payload

The SQI dict is included in every `success` and `alert` payload:

```json
{
  "status": "success",
  "admissionId": "ADM001",
  "sqi": {
    "score": 1.0,
    "valid": true,
    "flag": "GOOD"
  },
  "bp": { ... }
}
```

`valid=false` does not by itself block the output. The SQI is informational — the AI still runs and the result is still forwarded. However, if `bp_valid=False` (AI returned no BP), the output will be `status=poor_signal` regardless of the SQI flag.

---

## 9. Differences from CHECKME and BERRYMED

| Aspect | NISO204 | CHECKME (NISO103) | BERRYMED (NISO101) |
|--------|---------|------------------|-------------------|
| Native Hz | 200 (from `FS`) | 125 (fixed) | 200 (fixed) |
| Signal field | `Pleth` / `PlethWave` | `pleth.plethWave` | `pleth.plethWave` |
| DSP | Median despike only | Full PlethProcessor | Butterworth bandpass |
| SQI source | compute_sqi_204() → `process()` return value | processor `results[6]` | Default GOOD |
| Normalization | Percentile 2nd/98th | Done inside processor | Percentile 2nd/98th |
| Why minimal DSP? | Model trained on this device — preserve morphology | Different device, heavier cleaning needed | Different ADC, heavier cleaning needed |
