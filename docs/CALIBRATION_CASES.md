# LifeSigns — Calibration and Processing Cases
**Version:** 2.0
**Date:** 2026-05-14
**Scope:** All scenarios the pipeline handles — cuff pathway, pleth pathway, edge cases

---

## Architecture Summary

```
  vitals.raw → process_vitals(json_data)
                    |
               Device detection
                    |
         ┌──────────┴──────────┐
         │                     │
      Cuff pathway          Pleth pathway
      (LS06/LEPU)           (NISO204/CHECKME/BERRYMED)
         │                     │
      Write Redis           DSP → AI → session
         │                     │
      status=success        status=success/alert/accumulating/poor_signal/error
```

**Partition key = admissionId** — all packets for one patient land on the same consumer, keeping SESSION_STORAGE consistent in memory.

**Reference BP storage:** Redis, key = `ref:{admissionId}`, TTL = 86400s. Retrieved fresh for every pleth inference call.

**SESSION_STORAGE:** In-memory dict per admissionId. Contains:
- `start_time` — when the current 15-minute window started
- `readings` — list of (sbp, dbp, hb, glu) tuples accumulated this window
- `is_first_reading` — True when waiting for the first pleth after a cuff reading
- `needs_recalibration` — True when a mismatch was detected; extends window to 1200s
- `current_interval` — 900s (normal) or 1200s (recovery)
- `last_confirmation_time` — timestamp of last successful calibration match

**Mismatch threshold:** 15 mmHg on SBP OR DBP (immediate check); BOTH SBP AND DBP (15-min average check).

---

## Group C — Cuff Pathway (LS06 / LEPU)

A packet is routed to the cuff pathway if `deviceType` contains "LS06" or "LEPU", or if the payload contains a top-level `"bp"` key.

### C1: Valid cuff reading, no session yet

```
Packet: {"admissionId": "ADM001", "bp": {"bpSystolic": 128, "bpDiastolic": 82}}

sys_val = 128  (< 400 → not a sentinel)
dia_val = 82   (< 200 → not a sentinel)
admissionId not in SESSION_STORAGE

→ _ref_write("ADM001", 128, 82, epochTime)
  → Redis: ref:ADM001 = {"sbp":128, "dbp":82, ...}  TTL=86400s
→ SESSION_STORAGE["ADM001"] created with is_first_reading=True
→ Return: {status: "success", device_type: "REFERENCE_UPDATE",
           bp: {bpSystolic: 128, bpDiastolic: 82},
           message: "Reference BP for ADM001 updated to 128/82."}
```

The next pleth packet for ADM001 will run an immediate calibration check.

---

### C2: Valid cuff reading, session within 15-minute cooldown

```
Packet: {"admissionId": "ADM001", "bp": {"bpSystolic": 130, "bpDiastolic": 83}}

SESSION_STORAGE["ADM001"]["last_confirmation_time"] = T
now - T = 400s  (< 900s cooldown)

→ Return: {status: "ignored",
           message: "Reference ignored. System is in 15-minute stability cooldown (6.7m elapsed)."}
```

This prevents a random or accidental cuff reading from resetting a freshly confirmed calibration. The cooldown only starts after a successful match — during a mismatch recovery there is no cooldown.

---

### C3: Valid cuff reading, session past cooldown

```
Packet: {"admissionId": "ADM001", "bp": {"bpSystolic": 132, "bpDiastolic": 84}}

SESSION_STORAGE["ADM001"]["last_confirmation_time"] = T
now - T = 1100s  (> 900s cooldown)

→ _ref_write("ADM001", 132, 84, epochTime)
→ SESSION_STORAGE["ADM001"]["is_first_reading"] = True
→ Return: {status: "success", device_type: "REFERENCE_UPDATE", ...}
```

The next pleth packet will run an immediate calibration check against the new reference.

---

### C4: Cuff reading during active mismatch recovery

```
STATE: SESSION_STORAGE["ADM001"]["needs_recalibration"] = True
       SESSION_STORAGE["ADM001"]["last_confirmation_time"] = 0
       (no cooldown active — mismatch recovery has no cooldown)

Packet: {"admissionId": "ADM001", "bp": {"bpSystolic": 135, "bpDiastolic": 86}}

now - last_confirmation_time = very large (> 900s) → cooldown not active

→ _ref_write("ADM001", 135, 86, epochTime)
→ SESSION_STORAGE["ADM001"]["is_first_reading"] = True
→ Return: {status: "success", device_type: "REFERENCE_UPDATE", ...}
```

Because `needs_recalibration=True` is still set, the next pleth packet will run an immediate check against the new reference value.

---

### C5: Hardware error sentinel

```
Packet: {"admissionId": "ADM001", "bp": {"bpSystolic": 404, "bpDiastolic": 200}}

sys_val = 404  (≥ 400 → sentinel)
dia_val = 200  (≥ 200 → sentinel)

→ Return: {status: "error",
           message: "Device error sentinel received (404/200). Cuff reading ignored."}

Redis and SESSION_STORAGE are untouched.
```

Values like `404/200` mean the cuff device did not complete a measurement this cycle. They are not real BP values.

---

### C6: NISO204-style BP fields (top-level, not under "bp" key)

```
Packet: {"admissionId": "ADM001", "BPSystolic": 126, "BPDiastolic": 80}
(No "bp" sub-object, but top-level bp fields — rare edge case)

_detect_device() returns DEVICE_LS06 because "bp" key not present but the
cuff pathway checks bp_block = json_data.get("bp", {}) or json_data

If "bp" key is absent: bp_block falls back to json_data itself
sys_val = json_data.get("BPSystolic", 0) → 126
dia_val = json_data.get("BPDiastolic", 0) → 80

→ Proceeds normally as a valid cuff reading
```

---

## Group P — Pleth Pathway (NISO204 / CHECKME / BERRYMED)

All three wearable devices go through:
1. Device-specific DSP (filter, despike, SQI)
2. Resample to 120 Hz
3. AI inference (VitalInferenceEngine.analyze)
4. Calibration check or session accumulation

### P1: First pleth packet, no reference ever stored

```
admissionId: "ADM001"
Redis: ref:ADM001 → (empty)
SESSION_STORAGE["ADM001"]: is_first_reading=True (just created by C1)
                           OR does not exist yet

_ref_read("ADM001") → {"sbp": 0, "dbp": 0}

AI produces: sbp_pred=122, dbp_pred=79, bp_valid=True

is_immediate = True (is_first_reading=True)
ref_sbp = 0 → sbp_mismatch = False (ref_sbp=0 skips check)
ref_dbp = 0 → dbp_mismatch = False

→ No mismatch triggered
→ Append (122, 79, hb, glu) to session["readings"]
→ is_immediate=True → timer bypassed → compute average immediately
→ average: sbp=122, dbp=79
→ Final mismatch check: ref_sbp=0 → no mismatch
→ Return: {status: "success", bp: {bpSystolic: 122, bpDiastolic: 79}, ...}
→ session["is_first_reading"] = False
→ session["last_confirmation_time"] = now
→ session["current_interval"] = 900
```

No reference means no calibration check. The AI output goes out as `success` regardless.

---

### P2: First pleth after cuff, reference matches AI

```
admissionId: "ADM001"
Redis: ref:ADM001 = {"sbp": 128, "dbp": 82}
session["is_first_reading"] = True

AI produces: sbp_pred=126, dbp_pred=81, bp_valid=True

is_immediate = True
|128 − 126| = 2 ≤ 15 → no SBP mismatch
|82  − 81|  = 1 ≤ 15 → no DBP mismatch

→ Append (126, 81, ...) to session["readings"]
→ Timer bypassed (is_immediate), average now
→ Final check: ref_sbp=128, |128−126|=2 ≤ 15 → success
→ Return: {status: "success", reading_count: 1, bp: {bpSystolic: 126, ...}, ...}
→ is_first_reading=False, needs_recalibration=False, cooldown starts
```

---

### P3: First pleth after cuff, mismatch detected

```
admissionId: "ADM001"
Redis: ref:ADM001 = {"sbp": 128, "dbp": 82}
session["is_first_reading"] = True

AI produces: sbp_pred=150, dbp_pred=98, bp_valid=True

is_immediate = True
|128 − 150| = 22 > 15 → SBP mismatch

→ session["needs_recalibration"] = True
→ Return: {status: "alert",
           message: "Initial Calibration Mismatch: Cuff=128/82, AI=150/98.",
           bp: {bpSystolic: 150, bpDiastolic: 98,
                reference_sbp: 128, reference_dbp: 82}, ...}

SESSION_STORAGE NOT reset yet (is_first_reading stays True for next packet).
No reading appended (we returned before the append step).
```

The `alert` is forwarded to `vitals.clinical`. The system keeps alerting every incoming pleth packet until a new cuff reading is provided and the AI matches it.

---

### P4: Accumulating (mid-window, timer not elapsed)

```
admissionId: "ADM001"
session["is_first_reading"] = False
session["needs_recalibration"] = False
session["current_interval"] = 900
elapsed = 450s (< 900s)

AI produces valid reading → appended to session["readings"]

→ Return: {status: "accumulating",
           elapsed_seconds: 450, target_seconds: 900,
           message: "Stability period in progress (450/900s)."}

Not forwarded to vitals.clinical.
```

---

### P5: Poor signal on immediate check packet

```
admissionId: "ADM001"
session["is_first_reading"] = True  (or needs_recalibration=True)

DSP pipeline runs, AI called
AI produces no valid BP: "sbp" not in ai_results → bp_valid=False

is_immediate = True, bp_valid = False

→ Return: {status: "poor_signal",
           message: "Poor signal on first/recalibration packet. Waiting for clean signal."}

Session is not reset. is_first_reading stays True.
Next pleth packet will try the immediate check again.
```

This handles the case where the patient's finger is lifting off or the sensor is poorly seated. The system waits for a clean signal rather than triggering a spurious alert.

---

### P6: Poor signal during accumulation window

```
admissionId: "ADM001"
session["is_first_reading"] = False, elapsed < 900s

AI produces no valid BP: bp_valid=False

bp_valid=False → nothing appended to session["readings"]
Timer not elapsed → accumulating

→ Return: {status: "accumulating", ...}

The packet is effectively skipped. The window continues.
```

---

### P7: Timer expires, no valid readings collected

```
admissionId: "ADM001"
session["is_first_reading"] = False
elapsed = 960s (> 900s)
session["readings"] = []  (all packets in the window had poor signal)

→ session["start_time"] = now  (reset timer, try again)
→ Return: {status: "poor_signal",
           message: "No valid signal in window. All packets had poor signal quality."}

Not forwarded to vitals.clinical.
```

The window resets and the system waits for the next packet. The `needs_recalibration` state is preserved.

---

### P8: Timer expires, 15-minute average mismatch

```
admissionId: "ADM001"
Redis: ref:ADM001 = {"sbp": 128, "dbp": 82}
session["readings"] = [(148, 96, 14.2, 102), (150, 97, 13.8, 98), (147, 95, 14.0, 100)]
elapsed = 920s (> 900s)

avg_sbp = 148, avg_dbp = 96
ref_sbp = 128, ref_dbp = 82

|128 − 148| = 20 > 15 → SBP mismatch (average check requires BOTH to mismatch to alert)
|82  − 96|  = 14 ≤ 15 → DBP within threshold

Wait — the final check uses OR logic:
sbp_avg_mismatch = (ref_sbp > 0 and abs(ref_sbp - avg_sbp) > 15) → True
dbp_avg_mismatch = (ref_dbp > 0 and abs(ref_dbp - avg_dbp) > 15) → False (14 ≤ 15)

if sbp_avg_mismatch or dbp_avg_mismatch → alert

→ final_status = "alert"
→ session["needs_recalibration"] = True
→ session["current_interval"] = 1200
→ Return: {status: "alert", reading_count: 3,
           message: "Averaged Calibration Mismatch: Cuff=128/82, AI_Avg=148/96.",
           bp: {bpSystolic: 148, bpDiastolic: 96,
                reference_sbp: 128, reference_dbp: 82}, ...}

Forwarded to vitals.clinical. Session resets. Next window = 1200s.
No cooldown started (needs_recalibration=True, so last_confirmation_time stays unchanged).
```

---

### P9: Timer expires, 15-minute average matches reference

```
admissionId: "ADM001"
Redis: ref:ADM001 = {"sbp": 128, "dbp": 82}
session["readings"] = [(126, 81, ...), (124, 80, ...), (127, 82, ...)]
elapsed = 910s (> 900s)

avg_sbp = 126, avg_dbp = 81
|128 − 126| = 2 ≤ 15 → no SBP mismatch
|82  − 81|  = 1 ≤ 15 → no DBP mismatch

→ final_status = "success"
→ session["needs_recalibration"] = False
→ session["last_confirmation_time"] = now  (cooldown starts)
→ session["current_interval"] = 900
→ Return: {status: "success", reading_count: 3,
           message: "15-minute averaged clinical payload.",
           bp: {bpSystolic: 126, bpDiastolic: 81, ...}, ...}

Forwarded to vitals.clinical. Next cuff reading within 900s will be ignored (C2).
```

---

## Group E — Edge Cases

### E1: Repeated alerts (intentional behaviour)

When `needs_recalibration=True`, the system fires an alert on every incoming pleth packet without any suppression. This is intentional. The system keeps alerting until clinical staff take a new cuff reading and the AI matches it.

```
Cycle 1: alert → forwarded to vitals.clinical
Cycle 2: alert → forwarded to vitals.clinical
Cycle 3: (nurse takes cuff reading → C3) → is_first_reading=True, needs_recalibration still True
Cycle 4: immediate check → if AI matches new cuff → success, needs_recalibration=False
```

There is no `alert_sent` flag. The dashboard should display the most recent alert; each new alert replaces or confirms the prior one.

---

### E2: AI inference exception

```
try:
    ai_results = ai_engine.analyze(...)
    ...
except Exception as e:
    SESSION_STORAGE["ADM001"]["readings"] = []
    SESSION_STORAGE["ADM001"]["start_time"] = time.time()
    return {"status": "error", "message": f"AI Inference Failed: {str(e)}"}
```

On exception: session readings are cleared, timer resets, `status=error` returned. The error is not forwarded to `vitals.clinical`. The pipeline continues to the next packet.

---

### E3: reading_count in output payloads

Every `success` and `alert` payload that comes from the 15-minute average path includes:

```json
{"reading_count": 3}
```

This is the number of valid AI readings that contributed to the average. Useful for the dashboard to indicate confidence — a reading_count of 1 is less reliable than 5.

---

### E4: CHECKME SQI index

The CHECKME `PlethProcessor.process_data()` returns a tuple of 7 items:

```
results[0] = normalised signal (array)
results[1] = raw filtered signal
results[2] = peak indices
results[3] = trough indices
results[4] = display array
results[5] = additional metrics
results[6] = quality dict {"score": ..., "valid": ..., "flag": ...}
```

The pipeline uses `sqi_info = results[6]` (not `results[4]`). `results[4]` is a display array, not a quality dict — using it would break the SQI field in the output payload.

---

## Summary Table

### Cuff Cases

| Case | Trigger | Action |
|------|---------|--------|
| C1 | Valid cuff, no session | Write Redis, create session, is_first_reading=True, status=success/REFERENCE_UPDATE |
| C2 | Valid cuff, within 15-min cooldown | Ignored — status=ignored |
| C3 | Valid cuff, past cooldown | Write Redis, is_first_reading=True, status=success/REFERENCE_UPDATE |
| C4 | Valid cuff during mismatch recovery | Cooldown not active → write Redis, is_first_reading=True (immediate check on next pleth) |
| C5 | sys≥400 or dia≥200 sentinel | status=error, Redis/session untouched |
| C6 | Top-level bp fields (no "bp" sub-key) | bp_block fallback to json_data, proceeds normally |

### Pleth Cases

| Case | Trigger | Action |
|------|---------|--------|
| P1 | First pleth, ref_sbp=0 | No mismatch check, status=success |
| P2 | First pleth, reference matches | status=success, cooldown starts |
| P3 | First pleth, mismatch | status=alert, needs_recalibration=True |
| P4 | Mid-window, timer not elapsed | status=accumulating |
| P5 | Poor signal on immediate packet | status=poor_signal, is_first_reading stays True |
| P6 | Poor signal mid-window | Nothing appended, accumulating continues |
| P7 | Timer expires, no readings | status=poor_signal, timer reset |
| P8 | Timer expires, average mismatch | status=alert, interval=1200, needs_recalibration=True |
| P9 | Timer expires, average matches | status=success, interval=900, cooldown starts |

### Edge Cases

| Case | Trigger | Action |
|------|---------|--------|
| E1 | needs_recalibration=True | Keeps alerting every cycle — intentional, no suppression |
| E2 | AI inference exception | Session cleared, timer reset, status=error (not forwarded) |
| E3 | 15-min output | reading_count field in payload |
| E4 | CHECKME SQI | sqi_info = results[6] (quality dict), not results[4] (display array) |
