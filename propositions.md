# Pipeline Improvement Propositions

---

## Proposition 1: Gap Detection

### What it does
When a patient removes the device and puts it back on, the pipeline currently treats the first new packet as a continuation of the old session. `is_first_reading` is already `False`, so the immediate-check mechanism never fires. High BP readings post-activity just silently accumulate into the window and get averaged with pre-removal normal readings.

Gap detection tracks the timestamp of the last received packet. If the gap between the last packet and the new incoming packet exceeds a threshold (e.g. 60 seconds), `is_first_reading` is reset to `True` and pre-gap accumulated readings are cleared.

### What changes
- Re-arms the existing immediate-check on the first packet after the gap
- Clears stale pre-gap readings so they don't dilute the new state
- 15-minute average output continues as normal after the check

### Example
- Patient baseline cuff = 110/70 (normal)
- Patient wears device, stable readings accumulate, `is_first_reading = False`
- Patient removes device, goes for a run (10 min gap)
- Patient puts device back on, BP is now 148/92 (hyper)

| Step | Current Behaviour | With Gap Detection |
|---|---|---|
| Device removed | Session keeps running | Session keeps running |
| Device put back on | Continues accumulating silently | Gap detected, `is_first_reading` reset to `True` |
| First post-run packet | Mixed into 15-min average | Immediate check: `\|148 - 110\| = 38 ≥ 15` → **alert fires** |
| Alert timing | Maybe at 15-min mark | Within 30 seconds of first packet |

The 15-minute average continues accumulating after the alert. Nothing changes about the formal output structure.

---

## Proposition 2: Per-Packet Reference-Anchored Correction

### What it does
Currently:
- **Same category** (model and reference agree): raw model output is used, no correction. Model is population-anchored, not patient-anchored.
- **Different category** (mismatch): remapped to population reference category base + delta.

Proposed — split into two clean cases:

**Same category → personal offset correction**
```
corrected = model_raw + (ref_s - category_base)
```
Shifts the model's population-anchored output to be centred on the patient's actual cuff value.

**Different category → trust the model's raw output**
```
corrected = model_raw
```
The model has detected a genuine category shift. Trying to anchor a hyper prediction to a normal patient baseline produces misleading values. Instead, report the model's prediction directly and use the reference only for alert triggering.

### Why not anchor cross-category to reference?
Patient ref = 110 (normal, base 118), model predicts 138 (hyper, base 145):
- delta from hyper base = `138 - 145 = -7`
- If anchored to ref: `110 + (-7) = 103` → undershoots badly, misleading

Trust the model instead: output = `138`, then `|138 - 110| = 28 ≥ 15` → alert.

### Examples

**Same category (personal offset):**
- Patient cuff = 110/70, normal base = 118/76
- Personal offset SBP = `110 - 118 = -8`, DBP = `70 - 76 = -6`
- Model predicts 122/78 (normal)
- Current output: `122/78`
- Proposed: `122 + (-8) = 114` / `78 + (-6) = 72` → **114/72**
- Actual BP = 115/73 → proposed is much closer

**Different category (trust model):**
- Patient cuff = 110/70 (normal)
- Model predicts 138/88 (hyper) → category mismatch
- Current: `145 (hyper base) + (138 - 145) = 138` ... remapped using population base
- Proposed: output = `138/88` directly
- Reference 110 vs 138 → `|138 - 110| = 28 ≥ 15` → **alert fires**
- No misleading undershooting

---

## Proposition 3: Mid-Window Drift Alert

### What it does
The main alert is always the 15-minute average vs the reference. But if a patient's BP is steadily climbing within the window, you currently won't know until minute 15.

Proposition 3 computes the average deviation of the last N per-packet corrected outputs from the reference. If that average exceeds 15 mmHg SBP or 10 mmHg DBP, an alert fires mid-window without waiting for the full 15 minutes.

```
avg(r1 - R, r2 - R, r3 - R, ...) > 15  →  alert
```

Where R = reference cuff value, r1/r2/r3 = per-packet corrected outputs (from Proposition 2).

Proposition 2 is the foundation for this — without patient-anchored per-packet outputs, comparing raw model values to R would be unreliable due to population-level bias.

### Example
- Patient cuff R = 120/80
- Corrected per-packet outputs accumulating in window:

| Packet | Output | Deviation from R (SBP) | Running Avg Deviation |
|---|---|---|---|
| r1 | 122/81 | +2 | 2.0 |
| r2 | 128/84 | +8 | 5.0 |
| r3 | 135/88 | +15 | 8.3 |
| r4 | 138/90 | +18 | 10.8 |
| r5 | 142/91 | +22 | 13.0 |
| r6 | 148/93 | +28 | **15.5 → alert fires** |

Alert fires at r6, mid-window, without waiting for the 15-minute average. The formal 15-minute output still fires at minute 15 as usual.

### Threshold
- SBP: average deviation > 15 mmHg
- DBP: average deviation > 10 mmHg
- Either condition triggers the alert

---

## Summary

| Proposition | What it solves | When it fires |
|---|---|---|
| Gap Detection | Device removed and re-worn, state change not caught | First packet after signal gap > 60s |
| Per-Packet Correction | Model output not anchored to patient's personal baseline | Every packet (same category: offset, different category: raw) |
| Mid-Window Drift Alert | Sustained BP rise within window caught only at minute 15 | When avg deviation from reference > 15/10 mmHg mid-window |

Main 15-minute average vs reference alert remains unchanged as the primary formal output.
