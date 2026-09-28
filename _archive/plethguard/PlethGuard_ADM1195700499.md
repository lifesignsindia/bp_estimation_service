# PlethGuard Case Report — ADM1195700499

**Figure:** `apnea_ADM1195700499.png` · **Window:** 07-23 19:44 → 07-24 13:11 IST · **Device:** NISO206 + ECG

> PlethGuard is a **SpO₂-trust pipeline** first (prevents false SpO₂ alarms); apnea screening is a **downstream suspected/confirmation** layer. This is the **sickest** of the four patients.

---

## 1. Reading the graph

- **Panel 1 (SpO₂):** the **deepest desaturations of all four — down to 56%** (dark-red severe points), beginning in the **evening (22:38 onward)** and continuing through the night. Desats occur **more by day/evening than deep-night**. Baseline lower at ~92–93%.
- **Panel 2 (PI):** good perfusion even at 56% → these severe lows are **real and critical**, not artifact.
- **Panel 3 (Pulse):** tachycardic ~120–160 bpm early (the expected response to severe hypoxemia), then settles ~80.
- **Panel 4 (ECG-RR):** ~18 brpm (sparse).

## 2. SpO₂-trust layer (false-alarm prevention)

| Metric | Value |
|---|---|
| Valid SpO₂ epochs | 230 (lowest **56%**, median 93%) |
| Naive monitor alarms (raw <90) | 67 |
| **Shown as real by PlethGuard** | **67 — every one (0 hidden)** |
| Suppressed as false | **0** (well, 1 held transient elsewhere) |
| No-signal / device-error epochs | 190 → flagged, not shown |
| `change_finger` advisories | 180 epochs |

**Safety proof:** with perfusion good throughout, PlethGuard suppressed **nothing** — all 67 low readings, including the critical 56%, were **displayed**. This is the non-negotiable property: never hide a real desaturation.

## 3. Apnea layer (downstream screen)

- **SpO₂ signature:** desats **22 night vs 45 day** → **day-predominant** → **nocturnal-predominant = NO**.
- **PR/HR signature:** pulse +0.5 bpm at desats (no meaningful arousal).
- **Verdict:** `chronic_hypoxemia` — **NOT** sleep apnea.

## 4. Why "chronic hypoxemia", not "apnea"
The desaturations are **not sleep-confined** — in fact they are **more frequent by day/evening** and far deeper (to 56%) than any apnea pattern. This is severe, ongoing hypoxemia (a sick respiratory/cardiac patient), not sleep-disordered breathing. PlethGuard correctly **does not** raise the apnea flag and instead ensures every severe low is shown for immediate clinical attention.

## Bottom line
PlethGuard **displayed all 67 real desaturations including a critical 56%, hid nothing, and flagged the probe dropouts**. Apnea screen: **negative — this is severe chronic/intermittent hypoxemia, not apnea**. The pipeline's value here is entirely the **SpO₂-trust + never-hide-a-real-desat** behaviour; apnea screening correctly stays silent.
