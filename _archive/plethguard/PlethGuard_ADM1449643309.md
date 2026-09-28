# PlethGuard Case Report — ADM1449643309

**Figure:** `apnea_ADM1449643309.png` · **Window:** 07-23 23:53 → 07-24 13:12 IST · **Device:** NISO206 + ECG

> PlethGuard is a **SpO₂-trust pipeline** first (prevents false SpO₂ alarms); apnea screening is a **downstream suspected/confirmation** layer on the validated readings.

---

## 1. Reading the graph

- **Panel 1 (SpO₂):** a **cyclic sawtooth** — repeated dip-and-recover events (to 83–89%) packed into **00:23 → 05:06 IST**, each a separate pink band, **entirely at night, none by day**. Baseline 93–97%.
- **Panel 2 (PI):** excellent (5–15) throughout → every dip is real.
- **Panel 3 (Pulse):** swings 100 → 135 bpm (surges), but not systematically higher *at* the desats.
- **Panel 4 (ECG-RR):** ~16–18 brpm (sparse — few clean ECG windows).

## 2. SpO₂-trust layer (false-alarm prevention)

| Metric | Value |
|---|---|
| Valid SpO₂ epochs | 152 (lowest **83%**, median 95%) |
| Naive monitor alarms (raw <90) | 23 |
| **Shown as real by PlethGuard** | **19** (10 tagged as fresh hypoxemia alarms) |
| Suppressed as false | **4** (gross-motion pleth, CV over the dynamic bar) |
| No-signal / device-error epochs | 186 → flagged, not shown |
| `change_finger` advisories | 179 epochs (a lot of probe-off time) |

**How the false alarms were caught:** 4 epochs had a grossly irregular pleth (motion) → `signal_bad`, not displayed. The genuine dips (good, regular pleth) were **all shown**.

## 3. Apnea layer (downstream screen — SUSPECTED)

- **SpO₂ signature:** **23 night desats, 0 daytime**, day baseline 96% → **nocturnal-predominant = YES**; morphology is the classic **cyclic** desaturation-resaturation of obstructive apnea.
- **PR/HR signature:** pulse **−1 bpm at desats** → **arousal NOT evident**.
- **Verdict:** `sleep_apnea_suspected`, **confidence MODERATE (SpO₂ pattern only)**.

## 4. Why "apnea", and why only MODERATE confidence
The SpO₂ pattern is the **strongest, most textbook apnea morphology of the four** (repetitive night-only dips on a normal day baseline) — so the flag fires. But the **autonomic arousal signature is absent** (pulse didn't rise at the events), so PlethGuard is honest and grades it **moderate**, not high. This is exactly the value of using more than SpO₂: it *grades certainty* instead of over-claiming.

## Bottom line
PlethGuard **showed all 19 real dips, suppressed 4 motion artifacts, flagged heavy probe-off time**, and raised **sleep-apnea-suspected (MODERATE)** — strong desaturation pattern, but the pulse-arousal corroboration is missing. *Screen, not diagnosis — refer for a sleep study.*
