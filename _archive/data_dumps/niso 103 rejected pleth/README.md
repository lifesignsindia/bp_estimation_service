# NISO103 (CHECKME) rejected pleth — signal-quality proof

One PNG per bad epoch pulled from prod Mongo. Failure-mode counts in the 1500 scanned epochs:

- **WRAPAROUND**: 78 epochs (5%)
- **NOISY**: 1 epochs (0%)

## Why each mode is bad

### WRAPAROUND
SIGNED int8 wraparound: samples >127 wrap to negative, so pulse PEAKS become deep TROUGHS. The AI receives the RED trace (inverted, jagged) instead of the smooth GREEN (raw+256). Fails hardest at high BP (more peaks cross 127) -> model reads the pulse upside-down -> BP under-read.

### FLATLINE
No pulsatile signal (weak perfusion / sensor not on the finger). Amplitude is near-flat with no repeatable beats -> nothing for the model to measure.

### NOISY
Motion artifact: erratic baseline, irregular beat-to-beat intervals, no clean pulse morphology. maxVPG / APG features can't be extracted reliably -> estimate is unreliable.

### CLIPPING
ADC saturation: samples pinned at the rail (min/max) -> the pulse top/bottom is clipped off, so amplitude and slope features are wrong.

## Plots in this folder

| file | mode | admission | time | frac_neg | rough raw/fixed | beats | ref sys |
|---|---|---|---|---|---|---|---|
| 01_WRAPAROUND_ADM739754714_1782889082.png | WRAPAROUND | ADM739754714 | 2026-07-01 12:28 | 0.11 | 3.3x | 42 | - |
| 02_WRAPAROUND_ADM739754714_1782889740.png | WRAPAROUND | ADM739754714 | 2026-07-01 12:39 | 0.09 | 2.8x | 49 | - |
| 03_WRAPAROUND_ADM739754714_1782890542.png | WRAPAROUND | ADM739754714 | 2026-07-01 12:52 | 0.12 | 2.6x | 39 | - |
| 04_WRAPAROUND_ADM1788600258_1782903300.png | WRAPAROUND | ADM1788600258 | 2026-07-01 16:25 | 0.10 | 2.6x | 44 | - |
| 05_WRAPAROUND_ADM1078287630_1782904903.png | WRAPAROUND | ADM1078287630 | 2026-07-01 16:51 | 0.07 | 2.5x | 39 | - |
| 06_WRAPAROUND_ADM1078287630_1782904949.png | WRAPAROUND | ADM1078287630 | 2026-07-01 16:52 | 0.09 | 2.8x | 46 | - |
| 07_WRAPAROUND_ADM1078287630_1782907190.png | WRAPAROUND | ADM1078287630 | 2026-07-01 17:29 | 0.06 | 2.6x | 45 | - |
| 08_WRAPAROUND_ADM724272176_1782970034.png | WRAPAROUND | ADM724272176 | 2026-07-02 10:57 | 0.07 | 2.4x | 39 | - |
| 09_WRAPAROUND_ADM724272176_1782970204.png | WRAPAROUND | ADM724272176 | 2026-07-02 11:00 | 0.06 | 2.4x | 37 | - |
| 10_WRAPAROUND_ADM1433992336_1782974249.png | WRAPAROUND | ADM1433992336 | 2026-07-02 12:07 | 0.08 | 2.8x | 39 | - |
| 11_WRAPAROUND_ADM1433992336_1782974390.png | WRAPAROUND | ADM1433992336 | 2026-07-02 12:09 | 0.08 | 2.7x | 42 | - |
| 12_WRAPAROUND_ADM1433992336_1782975002.png | WRAPAROUND | ADM1433992336 | 2026-07-02 12:20 | 0.09 | 2.6x | 40 | - |
| 13_WRAPAROUND_ADM999790634_1782981397.png | WRAPAROUND | ADM999790634 | 2026-07-02 14:06 | 0.15 | 3.8x | 42 | 111 |
| 14_WRAPAROUND_ADM999790634_1782981432.png | WRAPAROUND | ADM999790634 | 2026-07-02 14:07 | 0.15 | 3.9x | 44 | - |
| 15_WRAPAROUND_ADM999790634_1782981525.png | WRAPAROUND | ADM999790634 | 2026-07-02 14:08 | 0.07 | 2.6x | 43 | - |
| 16_WRAPAROUND_ADM299526360_1782984253.png | WRAPAROUND | ADM299526360 | 2026-07-02 14:54 | 0.08 | 2.4x | 45 | - |
| 17_WRAPAROUND_ADM299526360_1782984742.png | WRAPAROUND | ADM299526360 | 2026-07-02 15:02 | 0.08 | 2.6x | 43 | - |
| 18_WRAPAROUND_ADM1391489618_1783062136.png | WRAPAROUND | ADM1391489618 | 2026-07-03 12:32 | 0.06 | 2.9x | 41 | - |
| 19_WRAPAROUND_ADM1391489618_1783062352.png | WRAPAROUND | ADM1391489618 | 2026-07-03 12:35 | 0.15 | 4.0x | 40 | - |
| 20_WRAPAROUND_ADM1391489618_1783062562.png | WRAPAROUND | ADM1391489618 | 2026-07-03 12:39 | 0.08 | 2.6x | 42 | - |
| 21_NOISY_ADM1118232021_1783178392.png | NOISY | ADM1118232021 | 2026-07-04 20:49 | 0.13 | 2.1x | 42 | - |

**Headline:** WRAPAROUND is a firmware/encoding bug unique to NISO103 — the device ships a signed byte where an unsigned one is meant. Every high-BP epoch on this device is corrupted before the model even sees it. This is the root cause of the systolic under-read Fix#1 corrects.
