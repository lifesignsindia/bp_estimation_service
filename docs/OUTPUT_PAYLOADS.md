# Output Payload Reference

All responses from `process_vitals()` are JSON objects. The `status` field is always present and is the primary routing key for the frontend.

---

## Status Values

| status | Trigger |
|---|---|
| `success` | Clean AI output (immediate or 15-min average) |
| `alert` | Calibration mismatch detected (immediate or averaged) |
| `accumulating` | Within 15-min window, readings being collected |
| `poor_signal` | AI returned no valid segments; no clinical data available |
| `ignored` | Cuff received during 15-min cooldown after confirmed calibration |
| `error` | Signal too short, unknown device, or AI exception |

---

## 1. Reference Update (Cuff / LEPU / Manual BP received)

```json
{
    "status": "success",
    "device_type": "REFERENCE_UPDATE",
    "admissionId": "PAT001",
    "bp": {
        "bpSystolic": 130,
        "bpDiastolic": 85,
        "bpErrorMsg": "None"
    },
    "message": "Reference BP for PAT001 updated to 130/85. AI will use this for calibration."
}
```

---

## 2. Cuff Ignored (Within 15-min Cooldown)

```json
{
    "status": "ignored",
    "admissionId": "PAT001",
    "message": "Reference ignored. System is in 15-minute stability cooldown (7.3m elapsed)."
}
```

---

## 3. Immediate Calibration Mismatch Alert

Fired on the first pleth packet after a cuff reading, or on any packet when `needs_recalibration=True`, if `|cuff - AI| > 15 mmHg`.

`hemoglobin` and `glucose` are omitted if the AI could not compute them.

```json
{
    "status": "alert",
    "admissionId": "PAT001",
    "device_type": "CHECKME",
    "timestamp": 1700000060,
    "message": "Initial Calibration Mismatch: Cuff=130/85, AI=108/70.",
    "bp": {
        "bpSystolic": 108,
        "bpDiastolic": 70,
        "estimated_sbp": 108,
        "estimated_dbp": 70,
        "category": "Hypo",
        "trend": { "trend": "Stable ->", "slope": 0.0, "readings": 1 },
        "reference_sbp": 130,
        "reference_dbp": 85
    },
    "hemoglobin": 13.5,
    "glucose": 95,
    "sqi": { "score": 1.0, "valid": true, "flag": "GOOD" }
}
```

---

## 4. Accumulating (Within 15-min Window)

```json
{
    "status": "accumulating",
    "admissionId": "PAT001",
    "elapsed_seconds": 240,
    "target_seconds": 900,
    "message": "Stability period in progress (240/900s)."
}
```

`target_seconds` is 900 (normal) or 1200 (recovery after mismatch).

---

## 5. Success — 15-min Averaged Clinical Output

`hemoglobin` and `glucose` are omitted if no valid Hb/Glucose readings were collected in the window.

```json
{
    "status": "success",
    "admissionId": "PAT001",
    "device_type": "CHECKME",
    "timestamp": 1700000900,
    "reading_count": 28,
    "bp": {
        "bpSystolic": 128,
        "bpDiastolic": 83,
        "estimated_sbp": 128,
        "estimated_dbp": 83,
        "category": "Normal",
        "trend": { "trend": "Stable ->", "slope": 0.2, "readings": 5 },
        "bpErrorMsg": "None"
    },
    "hemoglobin": 13.8,
    "glucose": 97,
    "sqi": { "score": 1.0, "valid": true, "flag": "GOOD" },
    "message": "15-minute averaged clinical payload."
}
```

`reading_count` is the number of valid AI predictions that contributed to the average (excludes poor-signal packets).

---

## 6. Alert — 15-min Averaged Mismatch

Same shape as the success payload but `status: "alert"`, `bpErrorMsg` is absent from the bp block, and `message` describes the mismatch. `needs_recalibration` is set True internally — the system enters 1200s recovery interval and the cooldown timer is NOT started, allowing a new cuff reading immediately.

```json
{
    "status": "alert",
    "admissionId": "PAT001",
    "device_type": "NISO204",
    "timestamp": 1700001800,
    "reading_count": 25,
    "bp": {
        "bpSystolic": 110,
        "bpDiastolic": 72,
        "estimated_sbp": 110,
        "estimated_dbp": 72,
        "category": "Normal",
        "trend": { "trend": "Falling \\/", "slope": -1.5, "readings": 5 },
        "bpErrorMsg": "None"
    },
    "hemoglobin": 13.2,
    "glucose": 94,
    "sqi": { "score": 1.0, "valid": true, "flag": "GOOD" },
    "message": "Averaged Calibration Mismatch: Cuff=130/85, AI_Avg=110/72."
}
```

---

## 7. Poor Signal

Returned when `bp_valid=False` on an `is_immediate` packet, or when the 15-min window expires with zero valid readings.

```json
{
    "status": "poor_signal",
    "admissionId": "PAT001",
    "device_type": "BERRYMED",
    "timestamp": 1700000030,
    "sqi": { "score": 0.5, "valid": false, "flag": "MOTION_DETECTED" },
    "message": "No valid signal in window. All packets had poor signal quality."
}
```

---

## 8. Error

```json
{
    "status": "error",
    "message": "Signal too short for AI inference."
}
```

```json
{
    "status": "error",
    "admissionId": "PAT001",
    "message": "Device error sentinel received (404/200). Cuff reading ignored."
}
```

```json
{
    "status": "error",
    "message": "AI Inference Failed: <exception detail>"
}
```

---

## SQI Flag Values

| flag | Meaning |
|---|---|
| `GOOD` | Clean signal |
| `MOTION_DETECTED` | Baseline step detected — patient movement |
| `SATURATED` | ADC railing — sensor pressed too hard or gain issue |
| `POOR_CONTACT` | Near-flat signal — finger off or loose contact |
| `INSUFFICIENT_DATA` | Too few samples to assess |

---

## Trend Object

Present in `bp.trend` on all outputs that include a `bp` block.

```json
{ "trend": "Stable ->", "slope": 0.2, "readings": 5 }
```

| trend | Meaning |
|---|---|
| `Stable ->` | SBP slope within ±1.0 mmHg per reading |
| `Rising /\\` | SBP increasing > 1.0 mmHg per reading |
| `Falling \\/` | SBP decreasing > 1.0 mmHg per reading |

`readings` is the number of SBP values in the trend tracker history (max 5).

---

## Category Values

`bp.category` reflects the XGBoost classifier output for the current window.

| category | Approx SBP range |
|---|---|
| `Hypo` | < 90 mmHg |
| `Normal` | 90–129 mmHg |
| `Hyper` | ≥ 130 mmHg |
