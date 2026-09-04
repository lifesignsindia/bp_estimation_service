# NISO103 poor-quality pleth (Isolation-dashboard rules) — RAW vs PROCESSED

Classified by the DASHBOARD's own logic (ws_client.py): message 'flat'->FLATLINE, 'poor signal'/'waiting'->POOR_SIGNAL, sqi VALID==False->INVALID_SQI.

Each PNG = ONE 30s epoch: top raw as received, bottom processed (unsigned-fix->bandpass->normalised, beats marked).

Dashboard-flagged epochs seen: {'FLATLINE': 35, 'POOR_SIGNAL': 68}

> SQI flag/score is shown per title: flatline epochs correctly read **FLAT_SIGNAL/0.0**, poor-signal vary. The critical gap is the OTHER folder — the **wraparound-corrupted epochs pass SQI as GOOD/1.0** yet feed wrong BP into the estimate.

### FLATLINE
Dashboard status = FLATLINE (pipeline message contains 'flat'): no waveform / sensor off finger. Processed trace has no pulse -> reading discarded.

### POOR_SIGNAL
Dashboard status = POOR SIGNAL (message 'poor signal'/'waiting'): pulse too weak/broken to stabilise the 15-min window -> no trustworthy estimate.

### INVALID_SQI
Dashboard marked signal-quality INVALID: processed pulse lacks clean repeatable morphology.

## Plots

| file | mode | admission | time | beats/30s | std(fixed) | SQI flag | SQI score |
|---|---|---|---|---|---|---|---|
| 01_FLATLINE_ADM1048721328_1783733987.png | FLATLINE | ADM1048721328 | 2026-07-11 07:09 | 1 | 1.6 | FLAT_SIGNAL | 0.0 |
| 02_FLATLINE_ADM1048721328_1783735931.png | FLATLINE | ADM1048721328 | 2026-07-11 07:42 | 6 | 0.2 | FLAT_LINE | 0.0 |
| 03_FLATLINE_ADM1048721328_1783736023.png | FLATLINE | ADM1048721328 | 2026-07-11 07:43 | 0 | 0.0 | FLAT_LINE | 0.0 |
| 04_FLATLINE_ADM739702737_1783878252.png | FLATLINE | ADM739702737 | 2026-07-12 23:14 | 2 | 3.6 | FLAT_SIGNAL | 0.0 |
| 05_FLATLINE_ADM739702737_1783878463.png | FLATLINE | ADM739702737 | 2026-07-12 23:17 | 0 | 0.0 | FLAT_LINE | 0.0 |
| 06_FLATLINE_ADM739702737_1783878674.png | FLATLINE | ADM739702737 | 2026-07-12 23:21 | 0 | 0.0 | FLAT_LINE | 0.0 |
| 07_POOR_SIGNAL_ADM739754714_1782889955.png | POOR_SIGNAL | ADM739754714 | 2026-07-01 12:42 | 13 | 24.4 | GOOD | 1.0 |
| 08_POOR_SIGNAL_ADM1078287630_1782907119.png | POOR_SIGNAL | ADM1078287630 | 2026-07-01 17:28 | 28 | 18.3 | GOOD | 1.0 |
| 09_POOR_SIGNAL_ADM1310428923_1782973004.png | POOR_SIGNAL | ADM1310428923 | 2026-07-02 11:46 | 11 | 26.0 | GOOD | 1.0 |
| 10_POOR_SIGNAL_ADM1433992336_1782976694.png | POOR_SIGNAL | ADM1433992336 | 2026-07-02 12:48 | 36 | 17.7 | GOOD | 1.0 |
| 11_POOR_SIGNAL_ADM1542407721_1783140595.png | POOR_SIGNAL | ADM1542407721 | 2026-07-04 10:19 | 4 | 38.7 | GOOD | 1.0 |
| 12_POOR_SIGNAL_ADM833350599_1783146368.png | POOR_SIGNAL | ADM833350599 | 2026-07-04 11:56 | 30 | 19.2 | GOOD | 1.0 |
| 13_POOR_SIGNAL_ADM1891059755_1783361616.png | POOR_SIGNAL | ADM1891059755 | 2026-07-06 23:43 | 32 | 20.0 | GOOD | 1.0 |
| 14_POOR_SIGNAL_ADM1284802565_1783395624.png | POOR_SIGNAL | ADM1284802565 | 2026-07-07 09:10 | 18 | 19.6 | GOOD | 1.0 |
| 15_POOR_SIGNAL_ADM1891059755_1783425740.png | POOR_SIGNAL | ADM1891059755 | 2026-07-07 17:32 | 38 | 18.7 | GOOD | 1.0 |
| 16_POOR_SIGNAL_ADM1891059755_1783427024.png | POOR_SIGNAL | ADM1891059755 | 2026-07-07 17:53 | 44 | 19.1 | GOOD | 1.0 |
| 17_POOR_SIGNAL_ADM1507573892_1783427092.png | POOR_SIGNAL | ADM1507573892 | 2026-07-07 17:54 | 38 | 18.2 | GOOD | 1.0 |
| 18_POOR_SIGNAL_ADM1507573892_1783435421.png | POOR_SIGNAL | ADM1507573892 | 2026-07-07 20:13 | 43 | 17.3 | GOOD | 1.0 |
| 19_POOR_SIGNAL_ADM658710055_1783493611.png | POOR_SIGNAL | ADM658710055 | 2026-07-08 12:23 | 20 | 38.6 | SATURATED | 0.3 |
| 20_POOR_SIGNAL_ADM1507573892_1783497480.png | POOR_SIGNAL | ADM1507573892 | 2026-07-08 13:28 | 30 | 17.2 | GOOD | 1.0 |
