# LifeSigns — System Overview
**Version:** 8.0
**Date:** 2026-05-18
**Audience:** Product, clinical, and non-engineering stakeholders
**Scope:** What the system does, who the actors are, how data flows

---

## 1. What the System Does

LifeSigns is a continuous, non-invasive vitals monitoring service. Patients wear a PPG wearable device. Approximately every 3 minutes the device captures a window of pulse waveform (pleth) data and sends it to the LifeSigns server via Kafka. The server analyses the signal using an AI model and produces blood pressure (BP), haemoglobin (Hb), and blood glucose estimates. Results accumulate over a 15-minute window and an averaged clinical reading is forwarded downstream.

The system also accepts manual BP cuff readings as a reference ground truth. When a cuff reading is received, the AI's first output per device is compared against it immediately. A significant mismatch triggers an alert so clinical staff can investigate.

Each patient is completely independent — different patients can wear different device models simultaneously with no cross-patient dependency.

---

## 2. Actors and Devices

### The Patient
Wears one PPG wearable continuously. The device records pulse waveform data passively.

### The Clinical Staff
Monitors BP readings and alerts on the dashboard. Takes a manual cuff reading when requested by the system.

### Supported Device Models

All devices send their data on one Kafka topic (`vitals.raw`).

| Hardware | deviceName field | Native sampling rate | Pleth field | Built-in BP used? |
|----------|-----------------|---------------------|-------------|-------------------|
| NISO204 | `"NISO204"` | 200 Hz | `pleth.PLETH` | No |
| CHECKME O2 / NISO103 | `"NISO103"` | 125 Hz | `pleth.PLETH` | No |
| BerryMed Watch / NISO101 | `"NISO101"` | 200 Hz | `pleth.PLETH` | No |
| Reference cuff | _(has `bp` key)_ | — | N/A | Yes — `BPSYS`/`BPDIA`/`BP_ERROR` |

**Device detection** is done via the `deviceName` field at the top level of the JSON packet — not nested, not inferred from other fields.

**NISO204, NISO103, NISO101** — all three run AI inference on the pleth signal to estimate BP, Hb, and glucose.

**Cuff** — reference BP only. Fields `BPSYS`, `BPDIA`, `BP_ERROR` (integer, 0=valid). Stored in Redis and used to validate AI output. No inference done from cuff.

### The LifeSigns Server
Consumes all device packets from `vitals.raw`, routes them by device type, runs AI inference on pleth packets, manages per-device calibration state, aggregates session readings over 15 minutes, and forwards clinical outputs to `vitals.clinical`.

### Redis
Stores the latest reference BP reading per patient, keyed by `admissionId`. TTL = 24 hours. The pipeline fails fast on startup if Redis is unreachable.

### Downstream Consumers
Consume from `vitals.clinical`. Receive only `success` and `alert` payloads — all intermediate states are stdout only.

---

## 3. End-to-End Data Flow

```
  Wearable (NISO101 / NISO103 / NISO204)  or  Cuff (bp key present)
           |
           | BLE
           v
  BLE Gateway  [parses raw BLE frames into JSON]
           |
           | Kafka Topic: vitals.raw
           |   (partition key = admissionId)
           v
  ┌─────────────────────────────────────────────────────────┐
  │              kafka_consumer.py                          │
  │                                                         │
  │  Poll vitals.raw → decode JSON → call process_vitals()  │
  │                                                         │
  │  ┌─────────────────────────────────────────────────┐   │
  │  │              vitals_standalone.py               │   │
  │  │                                                 │   │
  │  │  1. Resolve admissionId                        │   │
  │  │     admissionId → PatId → deviceID →           │   │
  │  │     BLEDeviceID → "UNKNOWN_PATIENT"             │   │
  │  │                                                 │   │
  │  │  2. Device detection                            │   │
  │  │     "bp" key present → cuff pathway             │   │
  │  │     deviceName="NISO101" → BERRYMED (200Hz)     │   │
  │  │     deviceName="NISO103" → CHECKME  (125Hz)     │   │
  │  │     deviceName="NISO204" → NISO204  (200Hz)     │   │
  │  │     anything else → error (unknown deviceName)  │   │
  │  │                    |                            │   │
  │  │     ┌──── Cuff? ───────────────────────────┐   │   │
  │  │     │ Extract BPSYS / BPDIA                │   │   │
  │  │     │ Reject if BP_ERROR != 0              │   │   │
  │  │     │ Reject sentinel (sys≥400 or dia≥200) │   │   │
  │  │     │ Check cooldown (15 min since last    │   │   │
  │  │     │   confirmation → ignore if active)   │   │   │
  │  │     │ Write reference to Redis             │   │   │
  │  │     │ Reset is_first_reading={}            │   │   │
  │  │     │ Return status=success/REFERENCE_UPDATE│  │   │
  │  │     └──────────────────────────────────────┘   │   │
  │  │                    |  (pleth pathway)           │   │
  │  │  3. Extract _meta fields from input            │   │
  │  │     (patientId, facilityId, patientName,       │   │
  │  │      epochTime, seqNum, seqPart, spo2, etc.)   │   │
  │  │     Passed through to every output payload     │   │
  │  │                                                │   │
  │  │  4. Signal preprocessing (device-specific DSP) │   │
  │  │     BERRYMED: bandpass filter → normalize      │   │
  │  │     CHECKME:  PlethProcessor → SQI             │   │
  │  │     NISO204:  median despike → normalize       │   │
  │  │     All: resample to 120 Hz                    │   │
  │  │     Flat signal check (tail std / peak count)  │   │
  │  │     Reject if < 120 samples after resample     │   │
  │  │                    |                           │   │
  │  │  5. AI Inference                               │   │
  │  │     VitalInferenceEngine.analyze()             │   │
  │  │     → SBP, DBP, BP category, Hb, Glucose      │   │
  │  │                    |                           │   │
  │  │  6. Immediate calibration check (per device)  │   │
  │  │     Runs when: is_first_reading.get(device,   │   │
  │  │                True) = True AND has_reference  │   │
  │  │     If |ref_sbp − pred_sbp| ≥ 15 OR           │   │
  │  │        |ref_dbp − pred_dbp| ≥ 15:             │   │
  │  │        → alert immediately                     │   │
  │  │     is_first_reading[device] = False after     │   │
  │  │                    |                           │   │
  │  │  7. Session accumulation                       │   │
  │  │     Append (sbp, dbp, hb, glu) to readings    │   │
  │  │     Wait until 15 min elapsed                 │   │
  │  │     → accumulating (not forwarded)             │   │
  │  │                    |                           │   │
  │  │  8. Timer expiry → compute averages            │   │
  │  │     If no readings → poor_signal, reset timer  │   │
  │  │     Mismatch check (SBP OR DBP ≥ 15 mmHg)     │   │
  │  │     Match:    status=success, interval=900     │   │
  │  │     Mismatch: status=alert,   interval=1200    │   │
  │  │                                                │   │
  │  └─────────────────────────────────────────────────┘   │
  │                                                         │
  │  status=success or alert → produce to vitals.clinical  │
  │  all other statuses     → stdout only                  │
  └─────────────────────────────────────────────────────────┘
           |
           | Kafka Topic: vitals.clinical
           | (partition key = admissionId)
           v
  Downstream consumers (dashboard, EHR, alert handler)
```

---

## 4. Signal Processing — How Device Differences Are Handled

Each device has its own DSP pipeline before the signal reaches the AI model. All devices use `pleth.PLETH` as the input field.

| Step | BERRYMED (NISO101) | CHECKME (NISO103) | NISO204 |
|------|-------------------|------------------|---------|
| Input field | `pleth.PLETH` | `pleth.PLETH` | `pleth.PLETH` |
| Native Hz | 200 Hz | 125 Hz | 200 Hz |
| Filtering | Butterworth bandpass 0.5–8 Hz, order 4 | PlethProcessor (internal DSP) | Median filter (kernel=5) |
| SQI | Default GOOD | Quality dict from processor | Multi-factor: SATURATED / POOR_CONTACT / MOTION_DETECTED / GOOD |
| Normalisation | Percentile 2–98, clip [0,1] | Already normalised | Percentile 2–98, clip [0,1] |
| Resampled to | 120 Hz | 120 Hz | 120 Hz |

**Why 120 Hz?** The BP AI model was trained on 120 Hz data. All devices are resampled to this exact rate so the model sees uniform input regardless of hardware.

**Flat signal detection:** After resampling, the pipeline checks if the signal tail has std < 0.01 or amplitude < 0.05, or fewer than 5 peaks. Flat signals are rejected as `poor_signal` before reaching the AI.

**Minimum signal length:** 120 samples after resampling. Packets shorter than this return `status=error`.

---

## 5. Session Aggregation — 15-Minute Averaging

```
  Pleth packets arrive every ~3 minutes

  t= 0s   Packet 1 → AI estimate → immediate check → appended to session
  t= 3m   Packet 2 → AI estimate → appended to session
  t= 6m   Packet 3 → AI estimate → appended to session
  ...
  t=15m   Timer expires → compute average → output success/alert
```

**Standard interval:** 900 seconds (15 minutes).
**Recovery interval:** 1200 seconds (20 minutes) when `needs_recalibration=True`.
**reading_count** field in output shows how many valid readings contributed to the average.

---

## 6. Calibration and Mismatch Detection

**Per-device first-packet check:**
- `is_first_reading` is a dict keyed by device type — each device gets its own independent flag
- First packet per device after reference is set → immediate check
- NISO101, NISO103, NISO204 each get their own first-packet check independently
- Threshold: ±15 mmHg on SBP OR DBP → immediate `alert`

**15-minute average check:**
- End of every session window
- Threshold: ±15 mmHg on SBP OR DBP
- Match → `success`, cooldown starts (new cuff readings ignored for 15 min)
- Mismatch → `alert`, interval extended to 1200s

**No reference:** If no cuff reading stored (`ref_sbp=0`), mismatch checks skipped. Output forwarded as `success`.

**Worst case alert delay:** 15 minutes (if spike happens at start of window). First-packet check catches initial mismatches immediately.

---

## 7. Output Statuses

| Status | Forwarded to vitals.clinical? | Meaning |
|--------|------------------------------|---------|
| `success` | **Yes** | 15-min averaged reading, calibration confirmed |
| `alert` | **Yes** | Mismatch detected between reference and AI estimate |
| `accumulating` | No | Session in progress, timer not elapsed |
| `poor_signal` | No | Bad signal quality or flat signal |
| `ignored` | No | Cuff reading within 15-min cooldown |
| `error` | No | Hardware sentinel, signal too short, AI exception |

---

## 8. Output Payload Fields

Every `success` and `alert` payload contains:

**Passthrough from input (_meta):**
`patientId`, `facilityId`, `patientName`, `assignedDoctor`, `deviceId`, `epochTime`, `seqNum`, `seqPart`, `spo2`, `device`, `cgroup`, `pgroup`

**Computed by pipeline:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `success` or `alert` |
| `admissionId` | string | Patient session key |
| `deviceName` | string | `NISO101` / `NISO103` / `NISO204` |
| `deviceType` | string | `BP_SPO2` |
| `timestamp` | int | Unix epoch of output |
| `reading_count` | int | Number of valid readings averaged |
| `bp.bpSystolic` | int | Averaged SBP estimate (mmHg) |
| `bp.bpDiastolic` | int | Averaged DBP estimate (mmHg) |
| `bp.estimated_sbp` | int | Same as bpSystolic |
| `bp.estimated_dbp` | int | Same as bpDiastolic |
| `bp.category` | string | `hypo` / `normal` / `hyper` |
| `bp.trend` | object | Trend direction and slope |
| `bp.BP_ERROR` | int | Always 0 in output |
| `sqi` | object | `{score, valid, flag}` |
| `trending` | bool | True if BP trending significantly |
| `morphology_change` | string | `stable` / `rising` / `falling` |
| `hemoglobin` | float | Hb estimate (g/dL) — present if valid |
| `glucose` | int | Glucose estimate (mg/dL) — present if valid |
| `pleth.PLETH` | array | Processed 120Hz signal array |
| `message` | string | Human-readable description |

---

## 9. Reference BP Storage (Redis)

- **Key:** `ref:{admissionId}`
- **Value:** `{"sbp": 120, "dbp": 80, "timestamp": 1234567890}`
- **TTL:** 86400 seconds (24 hours)

Pipeline fails fast on startup if Redis is unreachable.

---

## 10. What the System Does NOT Do

- **Does not replace clinical diagnosis.** Alerts prompt human review, not treatment decisions.
- **Does not handle Bluetooth directly.** BLE connection handled by the gateway.
- **Does not use NISO101/NISO103/NISO204 built-in BP fields.** Only cuff readings and pleth-based AI estimates are used.
- **Does not store session state across restarts.** SESSION_STORAGE is in-memory. On restart, current window is lost and restarts cleanly.
- **Does not mix data across patients.** Each admissionId is processed independently.
- **Does not suppress repeated alerts.** Keeps alerting every cycle until a nurse responds with a new cuff reading.
