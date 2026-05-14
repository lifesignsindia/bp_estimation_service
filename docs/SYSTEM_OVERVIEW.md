# LifeSigns — System Overview
**Version:** 7.0
**Date:** 2026-05-14
**Audience:** Product, clinical, and non-engineering stakeholders
**Scope:** What the system does, who the actors are, how data flows

---

## 1. What the System Does

LifeSigns is a continuous, non-invasive vitals monitoring service. Patients wear a PPG wearable device. Approximately every 3 minutes the device captures a window of pulse waveform (pleth) data and sends it to the LifeSigns server via Kafka. The server analyses the signal using an AI model and produces blood pressure (BP), haemoglobin (Hb), and blood glucose estimates. Results accumulate over a 15-minute window and an averaged clinical reading is forwarded downstream.

The system also accepts manual BP cuff readings (LS06/LEPU device) as a reference ground truth. When a cuff reading is received, the AI's immediate output is compared against it. A significant mismatch triggers an alert so clinical staff can investigate.

Each patient is completely independent — different patients can wear different device models simultaneously with no cross-patient dependency.

---

## 2. Actors and Devices

### The Patient
Wears one PPG wearable continuously. The device records pulse waveform data passively.

### The Clinical Staff
Monitors BP readings and alerts on the dashboard. Takes a manual cuff reading when requested by the system.

### Supported Device Models

All devices send their data on one Kafka topic (`vitals.raw`). The cuff device and the wearable devices share the same topic.

| Hardware | Detected as | Native sampling rate | Signal field | Built-in BP in payload? |
|----------|------------|---------------------|-------------|------------------------|
| NISO204 | NISO204 | 200 Hz (configurable via `FS` field) | `Pleth` or `PlethWave` | No — never in production packets |
| CHECKME O2 / NISO103 | CHECKME | 125 Hz | `pleth.plethWave` | Ignored — only pleth used |
| BerryMed Watch / NISO101 | BERRYMED | 200 Hz | `pleth.plethWave` | Ignored — only pleth used |
| LS06 / LEPU (reference cuff) | LS06 | — (no inference) | N/A | Yes — `bpSystolic`/`bpDiastolic` only field used |

**NISO204, CHECKME, and BERRYMED** — all three run AI inference on the pleth signal to estimate BP, Hb, and glucose.

**LS06** — dedicated reference BP cuff. Its `bpSystolic`/`bpDiastolic` is stored in Redis and used to validate AI output. No pleth inference is done from the cuff.

### The LifeSigns Server
Consumes all device packets from `vitals.raw`, routes them by device type, runs AI inference on pleth packets, manages calibration validation state, aggregates session readings over 15 minutes, and forwards clinical outputs to `vitals.clinical`.

### Redis
Stores the latest reference BP reading per patient, keyed by `admissionId`. TTL = 24 hours. The pipeline fails fast on startup if Redis is unreachable.

### Downstream Consumers
Consume from `vitals.clinical`. Receive only `success` and `alert` payloads — all intermediate states are stdout only.

---

## 3. End-to-End Data Flow

```
  Wearable (NISO204 / CHECKME / BERRYMED)  or  Cuff (LS06)
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
  │  │     LS06 / LEPU / "bp" key → cuff pathway      │   │
  │  │     DeviceName="NISO204" + "Pleth" → NISO204   │   │
  │  │     deviceType CHECKME/NISO103 → CHECKME        │   │
  │  │     deviceType BERRY/NISO101  → BERRYMED        │   │
  │  │                    |                            │   │
  │  │     ┌──── Cuff? ───────────────────────────┐   │   │
  │  │     │ Extract bpSystolic / bpDiastolic     │   │   │
  │  │     │ Reject sentinel (sys≥400 or dia≥200) │   │   │
  │  │     │ Check cooldown (15 min since last    │   │   │
  │  │     │   confirmation → ignore if active)   │   │   │
  │  │     │ Write reference to Redis             │   │   │
  │  │     │ Set is_first_reading=True            │   │   │
  │  │     │ Return status=success/REFERENCE_UPDATE│  │   │
  │  │     └──────────────────────────────────────┘   │   │
  │  │                    |  (pleth pathway)           │   │
  │  │  3. Signal preprocessing (device-specific DSP) │   │
  │  │     BERRYMED: bandpass filter → normalize      │   │
  │  │     CHECKME:  PlethProcessor → SQI from [6]    │   │
  │  │     NISO204:  median despike → SQI → normalize │   │
  │  │     All: resample to 120 Hz (model requirement)│   │
  │  │     Reject if < 120 samples after resample      │   │
  │  │                    |                            │   │
  │  │  4. AI Inference                                │   │
  │  │     VitalInferenceEngine.analyze()              │   │
  │  │     → SBP, DBP, BP category, Hb, Glucose       │   │
  │  │     → If AI produces no SBP/DBP: bp_valid=False│   │
  │  │                    |                            │   │
  │  │  5. Immediate calibration check                 │   │
  │  │     Runs when: is_first_reading OR              │   │
  │  │                needs_recalibration=True         │   │
  │  │     If bp_valid=False → poor_signal             │   │
  │  │     If |ref_sbp − pred_sbp| > 15 OR            │   │
  │  │        |ref_dbp − pred_dbp| > 15:              │   │
  │  │        → alert, needs_recalibration=True        │   │
  │  │                    |                            │   │
  │  │  6. Session accumulation                        │   │
  │  │     Append (sbp, dbp, hb, glu) if bp_valid      │   │
  │  │     Wait until 15 min elapsed                   │   │
  │  │     → accumulating status (not forwarded)       │   │
  │  │                    |                            │   │
  │  │  7. Timer expiry → compute averages             │   │
  │  │     If no readings → poor_signal, reset timer   │   │
  │  │     Final mismatch check (BOTH SBP and DBP)     │   │
  │  │     Match: status=success, interval=900         │   │
  │  │     Mismatch: status=alert,  interval=1200      │   │
  │  │                                                 │   │
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

Each device has its own DSP pipeline before the signal reaches the AI model.

| Step | BERRYMED (NISO101) | CHECKME (NISO103) | NISO204 |
|------|-------------------|------------------|---------|
| Filtering | Butterworth bandpass 0.5–8 Hz, order 4 | PlethProcessor (internal DSP, 125 Hz) | Median filter (kernel=5) to remove hardware spikes |
| SQI computation | Default GOOD (no dedicated SQI) | Quality dict from processor results[6] | Multi-factor: SATURATED / POOR_CONTACT / MOTION_DETECTED / GOOD |
| Amplitude normalisation | Percentile 2nd–98th, clip to [0,1] | Output already normalised by processor | Percentile 2nd–98th, clip to [0,1] |
| Resampling to 120 Hz | 200 Hz → 120 Hz via scipy.signal.resample | 125 Hz → 120 Hz | From FS field (default 200 Hz) → 120 Hz |

**Why 120 Hz?** The BP AI model was trained on 120 Hz data. All devices are resampled to this exact rate so the model sees a uniform input regardless of hardware.

**Minimum signal length:** 120 samples after resampling (equivalent to 1 second at 120 Hz). Packets shorter than this are rejected with `status=error`.

---

## 5. Session Aggregation — 15-Minute Averaging

A single 30-second pleth capture has measurement variance. LifeSigns accumulates readings over a 15-minute window and outputs one averaged clinical reading per window.

```
  Pleth packets arrive every ~3 minutes

  t= 0s   Packet 1 → AI estimate → appended to session
  t= 3m   Packet 2 → AI estimate → appended to session
  t= 6m   Packet 3 → AI estimate → appended to session
  ...
  t=15m   Timer expires → compute average → output success/alert

  Average: mean of all valid SBP, DBP, Hb, Glucose readings in window
  Reading count included in payload ("reading_count" field)
```

**Standard interval:** 900 seconds (15 minutes) after a successful calibration confirmation.

**Recovery interval:** 1200 seconds (20 minutes) when `needs_recalibration=True` — the system gives extra time to collect more data during a mismatch recovery period.

**Immediate output (bypasses timer):** The very first pleth packet after a cuff reading (`is_first_reading=True`) or during recalibration (`needs_recalibration=True`) produces an output immediately without waiting for the timer. This ensures the calibration check happens right away.

---

## 6. Calibration and Mismatch Detection

The system uses the LS06 cuff reading as reference ground truth, stored per patient in Redis. The AI's pleth-based BP estimate is validated against this reference.

**Threshold:** 15 mmHg on SBP or DBP (either one triggers an alert).

**Immediate check (first packet or recalibration):**
- Runs on the first pleth packet after a cuff reading, or on every packet when `needs_recalibration=True`
- If `|ref_sbp − ai_sbp| > 15` OR `|ref_dbp − ai_dbp| > 15` → `status=alert`
- `needs_recalibration=True` is set and the recovery interval (1200s) is used for the next window

**15-minute average check:**
- Runs at the end of every session window
- Both SBP AND DBP must mismatch to trigger an alert (stricter than immediate check)
- If mismatch → `status=alert`, `needs_recalibration=True`, interval extended to 1200s
- If match → `status=success`, 15-minute cooldown starts during which new cuff readings are ignored

**No reference:** If no cuff reading has ever been stored in Redis (`ref_sbp=0`), mismatch checks are silently skipped. The AI output is forwarded as `success` without a calibration check.

---

## 7. Output Statuses

| Status | Forwarded to vitals.clinical? | Meaning |
|--------|------------------------------|---------|
| `success` | **Yes** | 15-minute averaged clinical reading, calibration confirmed |
| `alert` | **Yes** | Mismatch detected between reference BP and AI estimate |
| `accumulating` | No — stdout only | Session in progress, timer not yet elapsed |
| `poor_signal` | No — stdout only | AI could not produce a valid BP (bad signal quality) |
| `ignored` | No — stdout only | Cuff reading arrived within the 15-minute cooldown |
| `error` | No — stdout only | Hardware sentinel, signal too short, or AI inference exception |

---

## 8. Reference BP Storage (Redis)

The cuff reading for each patient is stored in Redis:

- **Key:** `ref:{admissionId}`
- **Value:** `{"sbp": 120, "dbp": 80, "timestamp": 1234567890}`
- **TTL:** 86400 seconds (24 hours)

If Redis is unreachable on startup, the pipeline exits immediately (fail-fast). There is no in-memory fallback — Redis is the single source of truth for reference BP.

---

## 9. Hb and Glucose Reporting

Hb (haemoglobin) and glucose are estimated by the AI model from the same PPG signal alongside BP. They are included in `success` and `alert` payloads when the AI produces valid values. If the AI does not return valid Hb or glucose, those fields are omitted from the payload (not null — simply absent).

There is no separate calibration requirement for Hb or glucose.

---

## 10. What the System Does NOT Do

- **Does not replace a clinical diagnosis.** Alerts prompt human review, not treatment decisions.
- **Does not handle Bluetooth directly.** BLE connection and frame parsing is handled by the gateway.
- **Does not use CHECKME or BERRYMED built-in BP fields.** Only LS06 cuff readings and pleth-based AI estimates are used.
- **Does not store session state across restarts.** SESSION_STORAGE is in-memory. Redis persists reference BP but not session accumulation. On restart, the current 15-minute window is lost and restarts cleanly.
- **Does not mix data across patients.** Each admissionId is processed independently.
- **Does not suppress repeated alerts.** If the AI continues to mismatch the reference, it keeps firing alerts every cycle until a new cuff reading confirms calibration. This is intentional — it keeps alerting until a nurse responds.
