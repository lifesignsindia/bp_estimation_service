# NISO101 vs NISO103 — why 101 is the better device

| metric (median/epoch) | NISO101 | NISO103 |
|---|---|---|
| % negative samples (wraparound) | 0% | 5% |
| raw/fixed roughness (1.0 = clean) | 1.00 | 2.14 |
| clean beats per 30s (data-bearing epochs) | 24 | 29 |

**NISO103** transmits the pleth as a SIGNED int8, so any true sample >127 wraps to a large negative -> pulse peaks fold into deep troughs (see the red traces / dashed +-127 lines). The AI receives an inverted, jagged waveform and under-reads BP, worst at high BP.

**NISO101** transmits clean unsigned data: ~0%% negatives, smooth repeatable pulse, roughness ~1.0, full beat count -> the model sees the true waveform. That is why NISO101 estimates are trustworthy and NISO103 needs the Fix#1 wraparound correction just to be usable.

Plots: A_waveforms_side_by_side.png, B_metrics_summary.png, C_niso101_clean_*.png
