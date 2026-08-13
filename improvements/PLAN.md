# Improving production BP, one measured step at a time

Branch `improve/bp-accuracy`, based on `3066b72` (Jul 6 tree + facility `CF1398828720` +
patient-data ignore rules).

Rule for this branch: **no step lands without a before/after number on real CIMS data.** Every
claim below is measured, and where a number came from someone else's validation note rather than
this branch, it says so.

---

## Measured baseline — what production actually does

Source: `ebp_dashboard/csv/Cims/CIMS_V5_vs_PRODUCTION.csv`, 680 scoreable 15-min windows from the
CIMS trial, `bp.estimatedSbp` exactly as the deployed pipeline emitted it, scored against the
**next** cuff reference (scoring against the current one is circular — production blends with it).

| true BP | n | PROD MAE | within 15 | bias | HOLD MAE |
|---|---|---|---|---|---|
| < 120 | 371 | 13.62 | 68% | **+11.4 over-reads** | 9.17 |
| 120–140 | 244 | **5.63** | **93%** | +1.1 | 10.76 |
| 140–160 | 61 | 32.27 | **0%** | **−32.3 under-reads** | 42.77 |
| ≥ 160 | 4 | 37.65 | 0% | −37.6 | 33.00 |
| **all** | 680 | **12.57** | **71%** | | 12.95 / 83% |

Read that carefully:

1. It is genuinely good in **120–140** and nowhere else.
2. Below 120 it over-reads by ~11 mmHg and is **worse than just showing the last cuff**.
3. Above 140 it collapses — 0% within 15, and it under-reads, which is the dangerous direction
   for a hypertensive patient. Estimates cap out at 171.
4. Overall it loses to HOLD on within-15 (71% vs 83%).

### Root cause

The architecture is `classifier → BP category → category base → ±15 clip → blend with cuff`.
Two things follow:

- The classifier has a **domain shift on NISO101** (~17% accuracy vs ~81% on the device it was
  trained on), so the category base is usually the wrong band.
- The **±15 clip** means a wrong base can never be recovered from. That is what produces the
  saturation at both ends — over-read at the bottom, under-read at the top.

Everything below attacks that chain.

---

## Step 1 — restore the three already-validated fixes ✅ this commit

The Jul 6 revert removed `323560f` "Version -2 fixes", which prod had been running. Restoring
them is the cheapest possible win: they are already validated and already live today, so this is
un-regressing, not experimenting.

| fix | what it does | reported effect |
|---|---|---|
| **FIX #2** high-BP cuff-trust ramp + under-read floor | leans progressively on the verified cuff from 140→180 sys / 90→120 dia (weight → 0.90); floor stops a saturated estimate dragging a high cuff down | high-BP mean sys error **22.6 → 4.6 mmHg** |
| **FIX #1** NISO103 signed-int8 unwrap | CHECKME sends pleth as signed int8 when the true signal is unsigned 0–255; values >127 wrapped negative, turning peaks into troughs | fixes NISO103 waveform; **no effect on NISO101/CIMS** |
| **HB_BIAS_G_DL = 3.5** | Hb over-reads by a near-constant offset (cohort ~12.8 vs lab ~9.1 g/dL) | Hb mean error **3.93 → 2.40 g/dL** |

Honesty note: those three numbers are from the original validation notes in `323560f`, **not
re-measured on this branch**. FIX #2 targets exactly the 140–160 collapse in the baseline table,
which is independent corroboration that it addresses a real defect. Re-measuring FIX #2 against
the CIMS references is Step 2.

Facility gate stays `CF1398828720`. Nothing else from `323560f` (no Plethguard) comes along.

---

## Step 2 — re-measure FIX #2 on CIMS, per BP band

Replay the CIMS windows through the restored blend and rebuild the baseline table. Confirms (or
refutes) the 22.6 → 4.6 claim on our own data, and shows whether the <120 over-read moved at all —
it should not, since `np.interp` returns the base weight below 140.

Deliverable: the same 4-row table, before vs after. If the <120 band degrades, the ramp gets
reverted rather than argued about.

---

## Step 3 — the classifier domain shift (the actual root cause)

The 17% → 81% gap is a **fixable defect, not a data limit**. Fixing the band selection removes
both the over-read and the under-read at once, because the ±15 clip stops being fatal when the
base is right.

Options to measure against each other:
- retrain the classifier on NISO101 epochs instead of NISO204
- widen or drop the ±15 clip once the base is trustworthy
- replace hard category bases with a continuous regression on the anchor

## Step 4 — self-labelled capture (unblocks everything after it)

Save every incoming epoch's raw pleth alongside its active ref SBP/DBP from `kafka_consumer.py`
(that is where the waveform actually arrives — `app.py` never sees it). This is the missing
ingredient: NISO101-native labelled data, so the delta model can be **fine-tuned** rather than
transferred from MIMIC.

Why it matters, measured: the MIMIC-trained delta model gives +0.253 partial corr on CIMS but
**−0.165 on the independent Siddaganga cohort**. Any CIMS-only result stays provisional until
there is NISO101 training data.

## Step 5 — between-cuff tracking, only after Step 4

Currently out of reach and the numbers say so: direction on real moves ≥5 mmHg is **62%** on CIMS
and the estimate covers 38% of a move at best (`v5_moving_report.py`). No tuning knob moves the
62% — a gain scales magnitude, never sign. Do not promise trend until Step 4 data exists.

---

## Reference runs kept for comparison

- `model_lab/v5_frozen/` — strict v5, frozen models + golden output, 8/8 tests pass
  (`test_v5.py`). Flat by design: 31% of windows move <1 mmHg.
- `model_lab/v5_moving_report.py` — the de-shrunk version. 16% hold-like, movement mean 4.0,
  covers 38% of the real move, MAE 10.51 vs HOLD 10.51.
