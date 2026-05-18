# LifeSigns — Calibration and Processing Cases
**Version:** 3.0
**Date:** 2026-05-18
**Scope:** All scenarios the pipeline handles — cuff pathway, pleth pathway, edge cases

---

## Architecture Summary

```
  vitals.raw → process_vitals(json_data)
                    |
               Device detection (deviceName field)
                    |
         ┌──────────┴──────────┐
         │                     │
      Cuff pathway          Pleth pathway
      (bp key present)      (NISO101/NISO103/NISO204)
         │                     │
      Write Redis           DSP → AI → session
         │                     │
      status=success        status=success/alert/accumulating/poor_signal/error
```

**Partition key = admissionId** — all packets for one patient land on the same consumer, keeping SESSION_STORAGE consistent in memory.

**Reference BP storage:** Redis, key = `ref:{admissionId}`, TTL = 86400s.

**Device detection:** via `deviceName` field (top level, not nested):
- `NISO101` → BERRYMED (200Hz)
- `NISO103` → CHECKME (125Hz)
- `NISO204` → NISO204 (200Hz)

**Pleth input field:** `pleth.PLETH` for all 3 devices.

**SESSION_STORAGE:** In-memory dict per admissionId. Contains:
- `start_time` — when the current 15-minute window started
- `readings` — list of (sbp, dbp, hb, glu) tuples accumulated this window
- `is_first_reading` — **per-device dict** `{}` — each device gets its own independent first-packet flag
- `needs_recalibration` — True when a mismatch was detected; extends window to 1200s
- `current_interval` — 900s (normal) or 1200s (recovery)
- `last_confirmation_time` — timestamp of last successful calibration match

**Mismatch threshold:** ±15 mmHg on SBP OR DBP.

**Forward policy (vitals.clinical):** Only `success` and `alert` are forwarded. `accumulating`, `poor_signal`, `error` are stdout only.

---

## How Output is Given

Output is sent to `vitals.clinical` in two scenarios only:

1. **First packet per device** — immediate check after reference is set
2. **Every 15 minutes** — window average calculated and sent

---

## Group C — Cuff Pathway

A packet is routed to the cuff pathway if the payload contains a top-level `"bp"` key.

**Required cuff fields:**
```json
{
  "admissionId": "ADM001",
  "bp": {
    "BPSYS": 120,
    "BPDIA": 80,
    "BP_ERROR": 0
  }
}
```
`BP_ERROR` is an integer — `0` = valid, non-zero = rejected.

---

### C1: Valid cuff reading, no session yet

```
Packet: {"admissionId": "ADM001", "bp": {"BPSYS": 128, "BPDIA": 82, "BP_ERROR": 0}}

sys_val = 128  (< 400 → not a sentinel)
dia_val = 82   (< 200 → not a sentinel)
BP_ERROR = 0   (valid)
admissionId not in SESSION_STORAGE

→ _ref_write("ADM001", 128, 82, epochTime)
→ SESSION_STORAGE["ADM001"] created with is_first_reading={}
→ Return: {status: "success", deviceType: "REFERENCE_UPDATE",
           bp: {bpSystolic: 128, bpDiastolic: 82, BP_ERROR: 0},
           message: "Reference BP for ADM001 updated to 128/82."}
```

---

### C2: Valid cuff reading, session within 15-minute cooldown

```
now - last_confirmation_time = 400s  (< 900s cooldown)

→ Return: {status: "ignored",
           message: "Reference ignored. System is in 15-minute stability cooldown (6.7m elapsed)."}
```

---

### C3: Valid cuff reading, session past cooldown

```
now - last_confirmation_time = 1100s  (> 900s)

→ _ref_write("ADM001", 132, 84, epochTime)
→ SESSION_STORAGE["ADM001"]["is_first_reading"] = {}
→ Return: {status: "success", deviceType: "REFERENCE_UPDATE", ...}
```

---

### C4: BP_ERROR non-zero

```
Packet: {"admissionId": "ADM001", "bp": {"BPSYS": 120, "BPDIA": 80, "BP_ERROR": 3}}

BP_ERROR = 3 (non-zero → hardware error)

→ Return: {status: "error",
           message: "Cuff hardware error (BP_ERROR=3). Reading 120/80 rejected."}

Redis and SESSION_STORAGE are untouched.
```

---

### C5: Hardware sentinel values

```
Packet: {"admissionId": "ADM001", "bp": {"BPSYS": 404, "BPDIA": 200, "BP_ERROR": 0}}

sys_val = 404  (≥ 400 → sentinel)

→ Return: {status: "error",
           message: "Device error sentinel received (404/200). Cuff reading ignored."}
```

---

## Group P — Pleth Pathway (NISO101 / NISO103 / NISO204)

**Required pleth fields:**
```json
{
  "admissionId": "ADM001",
  "deviceName": "NISO101",
  "pleth": {
    "PLETH": [681473, 681236, ...]
  }
}
```

All three devices use `pleth.PLETH` — exact key, case sensitive. Minimum 120 samples required.

---

### P1: First pleth packet per device, no reference stored

```
is_first_reading.get("BERRYMED", True) → True
ref_sbp = 0, ref_dbp = 0 (no reference)

has_reference = False → is_immediate = False
→ Packet goes into 15-min window accumulation
→ Return: {status: "accumulating", ...}
```

---

### P2: First pleth per device after cuff, reference matches AI

```
Redis: ref:ADM001 = {"sbp": 128, "dbp": 82}
is_first_reading.get("BERRYMED", True) → True
is_immediate = True

AI produces: sbp_pred=126, dbp_pred=81
|128 − 126| = 2 ≤ 15 → no SBP mismatch
|82  − 81|  = 1 ≤ 15 → no DBP mismatch

→ is_first_reading["BERRYMED"] = False
→ Return: {status: "success", reading_count: 1,
           bp: {bpSystolic: 126, bpDiastolic: 81, BP_ERROR: 0}, ...}
→ Cooldown starts
```

---

### P3: First pleth per device after cuff, mismatch detected

```
Redis: ref:ADM001 = {"sbp": 128, "dbp": 82}
is_first_reading.get("BERRYMED", True) → True
is_immediate = True

AI produces: sbp_pred=150, dbp_pred=98
|128 − 150| = 22 > 15 → SBP mismatch

→ session["needs_recalibration"] = True
→ is_first_reading["BERRYMED"] = False
→ Return: {status: "alert",
           message: "Initial Calibration Mismatch: Cuff=128/82, AI=150/98.",
           bp: {bpSystolic: 150, bpDiastolic: 98,
                reference_sbp: 128, reference_dbp: 82}, ...}

Forwarded to vitals.clinical.
```

---

### P4: Per-device independence

```
BERRYMED (NISO101) packet arrives → is_first_reading.get("BERRYMED", True) → True → immediate check
CHECKME  (NISO103) packet arrives → is_first_reading.get("CHECKME",  True) → True → immediate check
NISO204  packet arrives           → is_first_reading.get("NISO204",  True) → True → immediate check

Each device gets its own independent first-packet calibration check.
After first packet: is_first_reading["BERRYMED"] = False (others unchanged)
```

---

### P5: Accumulating (mid-window, timer not elapsed)

```
is_first_reading["BERRYMED"] = False
elapsed = 450s (< 900s)

AI produces valid reading → appended to session["readings"]

→ Return: {status: "accumulating",
           elapsed_seconds: 450, target_seconds: 900, ...}

Not forwarded to vitals.clinical.
```

---

### P6: Timer expires, 15-minute average mismatch

```
Redis: ref:ADM001 = {"sbp": 128, "dbp": 82}
session["readings"] = [(148, 96, ...), (150, 97, ...), (147, 95, ...)]
elapsed = 920s (> 900s)

avg_sbp = 148, avg_dbp = 96
|128 − 148| = 20 > 15 → alert

→ final_status = "alert"
→ session["needs_recalibration"] = True
→ session["current_interval"] = 1200
→ Return: {status: "alert", reading_count: 3,
           message: "Averaged Calibration Mismatch: Cuff=128/82, AI_Avg=148/96.", ...}

Forwarded to vitals.clinical. Next window = 1200s.
```

---

### P7: Timer expires, 15-minute average matches reference

```
avg_sbp = 126, avg_dbp = 81
|128 − 126| = 2 ≤ 15 → success

→ final_status = "success"
→ session["needs_recalibration"] = False
→ session["current_interval"] = 900
→ session["last_confirmation_time"] = now
→ Return: {status: "success", reading_count: 3,
           message: "15-minute averaged clinical payload.", ...}

Forwarded to vitals.clinical. Cooldown starts.
```

---

### P8: Poor signal / flat signal

```
After DSP processing:
- tail std < 0.01 OR tail amplitude < 0.05 → flat signal
- peak count < 5 → flat signal
- SQI valid=False from processor → poor signal

→ Return: {status: "poor_signal", sqi: {...}, message: "Flat signal detected."}

Not forwarded. is_first_reading unchanged.
```

---

### P9: Signal too short

```
len(model_ready_pleth) < 120

→ Return: {status: "error", message: "Signal too short for AI inference."}
```

Minimum 120 samples required after resampling to 120Hz.

---

## Group E — Edge Cases

### E1: Repeated alerts (intentional)

When `needs_recalibration=True`, system alerts every 1200s window until:
1. New cuff reading arrives
2. AI matches new reference

No suppression — intentional so clinical staff are kept informed.

---

### E2: Unknown deviceName

```
Packet: {"deviceName": "NISO999", ...}

→ Return: {status: "error",
           message: "Unknown deviceName: NISO999. Expected NISO101/NISO103/NISO204."}
```

---

### E3: AI inference exception

```
→ Session readings cleared, timer resets
→ Return: {status: "error", message: "AI Inference Failed: ..."}

Not forwarded to vitals.clinical.
```

---

## Summary Tables

### Cuff Cases

| Case | Trigger | Result |
|------|---------|--------|
| C1 | Valid cuff, no session | Write Redis, is_first_reading={}, REFERENCE_UPDATE |
| C2 | Valid cuff, within cooldown | ignored |
| C3 | Valid cuff, past cooldown | Write Redis, is_first_reading={}, REFERENCE_UPDATE |
| C4 | BP_ERROR != 0 | error, Redis untouched |
| C5 | sys≥400 or dia≥200 sentinel | error, Redis untouched |

### Pleth Cases

| Case | Trigger | Result |
|------|---------|--------|
| P1 | First pleth, no reference | accumulating |
| P2 | First pleth, reference matches | success, cooldown starts |
| P3 | First pleth, mismatch | alert, needs_recalibration=True |
| P4 | Per-device independence | each device has own first-packet flag |
| P5 | Mid-window accumulation | accumulating (not forwarded) |
| P6 | 15-min average mismatch | alert, interval=1200 |
| P7 | 15-min average matches | success, interval=900 |
| P8 | Flat/poor signal | poor_signal (not forwarded) |
| P9 | Signal too short (<120 samples) | error (not forwarded) |

### Edge Cases

| Case | Trigger | Result |
|------|---------|--------|
| E1 | needs_recalibration=True | Keeps alerting every 1200s — intentional |
| E2 | Unknown deviceName | error |
| E3 | AI exception | error, session reset |

---

## Forwarding Policy

| Status | Forwarded to vitals.clinical |
|--------|------------------------------|
| success | Yes |
| alert | Yes |
| accumulating | No |
| poor_signal | No |
| error | No |
| ignored | No |
