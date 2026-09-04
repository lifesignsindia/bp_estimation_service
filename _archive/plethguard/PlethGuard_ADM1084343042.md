# PlethGuard Case Report — ADM1084343042

**Figure:** `apnea_ADM1084343042.png` · **Window:** 07-21 22:05 → 07-24 13:10 IST (**3-night record**) · **Device:** NISO206 + ECG

> PlethGuard is a **SpO₂-trust pipeline** first (prevents false SpO₂ alarms); apnea screening is a **downstream suspected/confirmation** layer.

---

## 1. Reading the graph

- **Panel 1 (SpO₂):** the X-axis spans **three nights** (07-21 → 07-24). Desaturations to **72–89%** appear **both at night AND during the day** (e.g. episodes at 23:19, 02:39, but also **09:17 and 16:24**). Baseline ~96–97%.
- **Panel 2 (PI):** good during the dips → the lows are real.
- **Panel 3 (Pulse):** variable, occasional spikes to ~150 bpm.
- **Panel 4 (ECG-RR):** ~15 brpm (first night).

## 2. SpO₂-trust layer (false-alarm prevention)

| Metric | Value |
|---|---|
| Valid SpO₂ epochs | 558 (lowest **72%**, median 97%) |
| Naive monitor alarms (raw <90) | 85 |
| **Shown as real by PlethGuard** | **81** (incl. 4 severe <85 shown at once) |
| Suppressed as false | **4** (2 signal-bad + 2 transient) |
| No-signal / device-error epochs | 988 → flagged, not shown |
| `change_finger` advisories | 947 epochs (very intermittent contact over 3 nights) |

**Severity handling:** the 4 readings <85 were shown **immediately** (`ok_severe_alarm`) — a severe desat is never held.

## 3. Apnea layer (downstream screen)

- **SpO₂ signature:** desats **55 night vs 29 day** → **nocturnal-predominant = NO** (too many daytime desats).
- **PR/HR signature:** pulse +4 bpm at desats (arousal present) — but this alone doesn't make it apnea.
- **Verdict:** `chronic_hypoxemia` — **NOT** sleep apnea; flag correctly withheld.

## 4. Why "chronic hypoxemia", not "apnea"
Apnea/SDB desaturations are **confined to sleep**. This patient desaturates **around the clock** (daytime episodes at 09:17 and 16:24), so it fails the nocturnal-predominance test. Flagging it as apnea would be a **false positive** — PlethGuard withholds it and calls it what it is: recurrent (chronic/intermittent) hypoxemia. It still **displays every real low, including the deep ones to 72%**.

## Bottom line
PlethGuard **showed 81/85 real desats (down to 72%, severe ones instantly), suppressed 4 artifacts, and flagged the pervasive probe-contact problem** across 3 nights. Apnea screen: **negative — recurrent hypoxemia, not sleep-disordered breathing** (desats not sleep-confined). The value here is the **SpO₂-trust + severity + change_finger** behaviour, not an apnea flag.
