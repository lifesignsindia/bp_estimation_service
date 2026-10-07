# NISO206 — noisy pleth @ low SpO2 (extracted samples)

Real NISO206 epochs where the device reported a **low SpO2** while the **pleth was noisy**.
Pulled from `plethguard_dashboard/capture/mongo_20260819_to_20260819_CF1910904817.jsonl`
(facility CF1910904817, 2026-08-19, 1483 NISO206 epochs).

Selection: every epoch with SpO2 <= 92 and a non-flat pleth (326 of them) was scored on
beat regularity (`pr_handling.spo2_pulse_ok`), out-of-band spectral power (outside 0.7–4 Hz),
sample-to-sample jitter and clipping fraction. These are the top 3 after dropping one
truncated 6-sample packet.

## Files

Each `ADM*.json` is a list of 9 epochs — the pick plus 4 context epochs either side of the
same admission — with `spo2`/`pleth`/`device` parsed to real dicts and `"_isPick": true`
marking the target. `index.json` is the summary table; `picks_overview.png` plots all three
(pick in red, context in grey).

| file | admission | UTC | SpO2 | pi | PR | pleth | why it's noisy | guard verdict\* |
|---|---|---|---|---|---|---|---|---|
| `ADM441319045_2026-08-19_105943_spo280.json` | ADM441319045 | 10:59:43 | **80** | 2.0 | 130 | 150 @ 50 Hz | two full dropouts to 0, flat-then-lurching baseline; only 3 beats detected | `ok_severe_alarm` + `hypoxemia` |
| `ADM367842169_2026-08-19_142834_spo284.json` | ADM367842169 | 14:28:34 | **84** | 12.0 | 80 | 150 @ 50 Hz | heaviest waveform corruption — pulse shape destroyed by baseline wander, 70% of power out of band, 2 beats | `ok_severe_alarm` + `hypoxemia` |
| `ADM441319045_2026-08-19_103639_spo288.json` | ADM441319045 | 10:36:39 | **88** | 7.0 | none | 150 @ 50 Hz | pulse almost obliterated (near-flat + dropout spikes), 1 beat; sits inside a run of `SPO2_COMPUTE_ERROR` / `NO_FINGER_IN_SENSOR` epochs | `hold_transient` (held 98) |

\* From `pr_handling.handle_niso206_spo2` replayed over the **full** admission stream (256 and
506 epochs) so the per-patient baseline is real. Replaying only the 9-epoch window gives `ok`
for all three — the `SPO2_HIST_K=4` baseline never fills. Use the full stream when testing.

All three picks are isolated dips between normal neighbours (97→80→97, 91→84→93, err→88→97)
with `spo2Error=0`, which is exactly the transient-vs-real case PlethGuard exists to judge.
Note the two <85 picks bypass confirm-before-alarm via the `SPO2_SEVERE_ALARM` exemption, so
they are shown immediately despite the bad pleth.

## Regenerate / widen

Scanner and extractor: see the session scratchpad (`find_noisy_low_spo2.py`,
`extract_sample.py`, `plot_picks.py`). `LOW=90 python find_noisy_low_spo2.py` tightens the
SpO2 cutoff. The two big archives `ADM1013075949_SPO2.json` and `ADM1566400397_SPO2.json`
were also scanned — neither contains any epoch at or below SpO2 92.
