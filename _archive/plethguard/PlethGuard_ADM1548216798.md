# PlethGuard Case Report — ADM1548216798

**Figure:** `apnea_ADM1548216798.png` · **Window:** 07-23 22:39 → 07-24 13:09 IST · **Device:** NISO206 (finger SpO₂ + pleth) + ECG

> PlethGuard is a **SpO₂-trust pipeline**: its first job is to decide, epoch-by-epoch, whether each SpO₂ reading is real and should be displayed/alarmed — i.e. to **prevent false SpO₂ alarms**. Apnea screening is a **downstream, suspected/confirmation** layer that runs on the *validated* readings. This report explains the graph, then both layers.

---

## 1. Reading the graph

- **Panel 1 (SpO₂):** normal (~95–99%) all evening, one **sustained dip 00:30 → 02:39 IST down to 83%** (pink/red points, pink episode band), then a clean recovery to 97–99%. Blue band = night 00:00–07:00; the desat sits squarely inside it.
- **Panel 2 (PI, perfusion):** strong (3–8) throughout the dip → the sensor had a genuine pulse, so the low is **real physiology**, not a dropout.
- **Panel 3 (Pulse):** ~100 bpm, and it **rises at the desats** (autonomic arousal).
- **Panel 4 (ECG-RR, experimental):** ~14 brpm, **drops to ~11–12 during the dip** — respiration slowed exactly when SpO₂ fell.

## 2. SpO₂-trust layer (the primary job — false-alarm prevention)

| Metric | Value |
|---|---|
| Valid SpO₂ epochs | 229 (lowest **83%**, median 96%) |
| Naive monitor alarms (raw <90) | 50 |
| **Shown as real by PlethGuard** | **49** (0 real desats hidden) |
| Suppressed as false | **1** (transient PI collapse, 00:18) |
| No-signal / device-error epochs | 101 → flagged, not shown |
| `change_finger` advisories | 96 epochs |

**How the false alarm was caught:** at 00:18 a single SpO₂=89 coincided with PI collapsing to 1.3 (< ½ its baseline) — a transient artifact, held one epoch, then confirmed. Every other low was on good perfusion → **displayed**.

## 3. Apnea layer (downstream screen — SUSPECTED, not diagnosis)

- **SpO₂ signature:** desats **49 at night vs 1 by day**, day baseline 96% → **nocturnal-predominant = YES**.
- **PR/HR signature:** pulse **+3 bpm at desats** → autonomic **arousal present**.
- **Verdict:** `sleep_apnea_suspected`, **confidence HIGH (SpO₂ + PR arousal)**.

## 4. Why "apnea", not "chronic hypoxemia"
The desaturation is **confined to sleep on a normal daytime baseline** and is accompanied by the **arousal pulse-rise** — the sleep-disordered-breathing pattern. A chronic hypoxemic would desaturate by day too; this patient does not.

## Bottom line
PlethGuard **displayed every real desaturation (to 83%), suppressed the one artifact, flagged the probe issues**, and — as a downstream screen — raised **sleep-apnea-suspected at HIGH confidence** because all three signals (SpO₂ desat + PR arousal + ECG-RR dip) agree. *Screen, not diagnosis — refer for a sleep study.*
