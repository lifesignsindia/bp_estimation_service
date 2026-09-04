# NISO101 signal-quality gate in v7 — "flat signal" and "poor signal"

What decides whether the v7 dashboard shows a BP value or one of the two rejection
strings. Everything below is the deployed path, not a proposal.

Source of truth:
- `ebp_dashboard/v7_runtime.py` — the gate and the display mapping
- `ebp_dashboard/bpv4_features.py` — the DSP that produces `n_beats` / `template_corr`
- `ebp_dashboard/v7_quality_isolated.py` — the same chain lifted out standalone, runnable

There are **two** independent quality gates for NISO101 in this repo. v7 uses only one of
them. That is deliberate to know about, because it explains a class of misreported states
(see [The gap](#the-gap)).

---

## 1. Where the decision is made

`V7Runtime.on_epoch()` is called once per pleth epoch. It assigns
`s["quality"]` ∈ {`SHORT`, `FLAT`, `NO_FEATURES`, `POOR`, `FAIR`, `GOOD`}, and `_snap()`
turns that into the string the ward actually sees.

| `quality` | Display state | Value shown | Counts toward window/anchor |
|---|---|---|---|
| `SHORT` | flat signal | no | no |
| `FLAT` | flat signal | no | no |
| `NO_FEATURES` | flat signal | no | no |
| `POOR` | poor signal | no | no |
| `FAIR` | accumulating / 15 min average | yes | **no** |
| `GOOD` | accumulating / 15 min average | yes | yes |

`FAIR` is the subtle one: the number stays on screen, but the epoch is *not* appended to
the 15-minute window (`v7_runtime.py`, `if good: s["win"].append(...)` else
`counts["gate_fail"] += 1`). A patient can sit at FAIR indefinitely, showing a value that
never advances to `15 min average` and therefore can never alert.

Two more paths reach **"poor signal"** without any signal defect at all:

- `anchor_key is None` — no cuff reference has arrived yet
- `anchor_f is None` — a cuff exists but the 6-epoch model anchor is still being built

So **"poor signal" on the dashboard is not a statement about the waveform.** It means
"no value available", which includes "still calibrating".

---

## 2. Stage A — flat / short (no DSP)

```python
x = np.asarray([v for v in (samples or []) if isinstance(v, (int, float))], float)
if len(x) < 1200 or float(np.std(x)) < 1e-6:
    s["quality"] = "FLAT" if len(x) >= 1200 else "SHORT"
```

- `len(x) < 1200` → `SHORT`. At the usual 200 Hz that is 6 s of stream.
- `std(x) < 1e-6` → `FLAT`. A bit-exact constant trace only.

No filtering, no despiking, nothing. This is the entire pre-DSP check.

A third flat-class label appears later: if the feature extractor cannot build a beat it
returns `None` and the epoch becomes `NO_FEATURES`, which also displays as "flat signal".

---

## 3. Stage B — the morphology gate (the real signal processing)

```python
f, q = features_from_epoch(x, fs_in=fs)
nb, tc = float(q[0]), float(q[1])            # n_beats, template_corr
notch  = not bool(np.isnan(fv[core_idx]).any())
good   = (nb >= GATE_BEATS) and (tc >= GATE_CORR) and notch
quality = "GOOD" if good else ("FAIR" if (nb >= 6 and tc >= 0.80) else "POOR")
```

Thresholds, all env-overridable:

| Constant | Env var | Default |
|---|---|---|
| `GATE_BEATS` | `V7_GATE_BEATS` | 10 |
| `GATE_CORR` | `V7_GATE_CORR` | 0.90 |
| FAIR beats | — (hardcoded) | 6 |
| FAIR corr | — (hardcoded) | 0.80 |

`notch` is a proxy test: the five core features `aix`, `ri`, `ipa`, `dvp_time`,
`stiffness_idx` are all NaN together exactly when the dicrotic notch (and hence the
diastolic point) could not be located. If any one of them is present, the notch was found.

### The DSP chain behind `n_beats` and `template_corr`

`bpv4_features.features_from_epoch` → `auto_polarity` → `ensemble_beat`:

1. **Resample to 120 Hz** (`FS`). Reject the epoch if less than 10 s remains.
2. **Auto-polarity.** Build the ensemble both as-is and inverted; keep the orientation
   whose ensemble peak sits *earliest* in the beat. NISO101 reports the opposite polarity
   to the training distribution (light absorption vs perfusion) — measured, NISO101 puts
   its peak at 68% of the beat as-is and 32% inverted. A real pulse rises fast and decays
   slowly, so early-peak is the correct orientation. Uncorrected, every morphology feature
   is computed upside-down.
3. **Band-pass** 0.5–12 Hz, Butterworth order 3, zero-phase `filtfilt`.
4. **Percentile normalise** on p2/p98.
5. **Systolic peak detection**: `find_peaks(distance=0.4*fs, prominence=0.15)`.
   Need `≥ MIN_BEATS+1` = 6 peaks, else `None`.
6. **Foot detection = minimum between consecutive peaks.** *Not* `find_peaks(-x)` —
   that locks onto a prominent dicrotic notch on NISO101, which truncated 28% of epochs
   (vs 0% on MIMIC) and put the systolic peak at 48% of the segment instead of ~20%.
7. **Segment foot → next foot.** Keep only segments of `0.3 s ≤ len ≤ 1.8 s` and range
   `> 1e-6`. Per-beat min-max scale, cubic-spline resample to 250 samples.
   Need `≥ 5` survivors, else `None`.
8. **Template rejection.** Median beat as template, Pearson correlation per beat, keep
   `cc ≥ 0.80` (`CORR_MIN`). If that leaves fewer than 5, fall back to the top 50% by
   correlation — a salvage path, so a low `n_beats` with a mediocre `template_corr` can
   still emerge from a bad epoch.
9. Return `n_beats = keep.sum()`, `template_corr = mean(cc[keep])`.

That is it. **Every "poor signal" verdict reduces to those two numbers plus the notch
flag.**

---

## 4. The other gate — `_compute_sqi_berry`, which v7 does not call

`processors/niso101_processor.py`. Runs after a `medfilt(kernel_size=5)` despike and
before normalisation, on the ingest side:

| Check | Test | Returns |
|---|---|---|
| Saturation | `>2%` of raw samples equal `max(raw)` | `0.2, False, "SATURATED"` |
| Poor contact | `p98 − p2 < 0.05 · mean(|x|)` | `0.0, False, "POOR_CONTACT"` |
| Motion | max step of an `n/20` moving average `> 0.5 · amplitude` | `0.5, False, "MOTION_DETECTED"` |
| — | otherwise | `1.0, True, "GOOD"` |

This is the probe-off / saturation / motion detector. `V7Runtime.on_epoch` takes raw
`samples` and applies its own `std < 1e-6` test instead, so none of these three flags ever
reach the v7 display state.

---

## 5. Measured behaviour

From the self-test in `v7_quality_isolated.py`:

| Input | quality | state | n_beats | template_corr |
|---|---|---|---|---|
| synthetic pulse | FAIR | ok | 13 | 0.894 |
| constant trace | FLAT | flat signal | — | — |
| 800 samples | SHORT | flat signal | — | — |
| pure Gaussian noise | POOR | poor signal | 22 | 0.595 |

Two conclusions worth carrying:

**`template_corr` is doing all the work of rejecting garbage.** Pure noise produced
`n_beats = 22` and a "found" notch — it cleared the beat-count gate and the notch test
outright. Only the correlation caught it. `GATE_BEATS` rejects *short or weak* signals,
not *noisy* ones. Loosening `V7_GATE_CORR` below ~0.80 lets noise score.

<a name="the-gap"></a>
**`std < 1e-6` is far too narrow to be the flat-line test.** It catches only a bit-exact
constant trace. A probe-off trace with a few LSB of noise, or a saturated rail with dither,
passes Stage A and is rejected downstream as `POOR` or `NO_FEATURES` — so the ward sees
**"poor signal" on a probe that is plainly off**, and cannot distinguish it from a genuine
morphology failure or from ordinary calibration. `_compute_sqi_berry`'s `SATURATED` and
`POOR_CONTACT` checks would catch exactly these cases. Wiring them into `on_epoch` (mapping
both to the flat-signal class) is the fix if that misreport is being seen in the ward.

---

## 6. Running the isolated extract

```python
from v7_quality_isolated import classify

classify(samples, fs_in=200)
# {'quality': 'GOOD', 'state': 'ok', 'n_beats': 34, 'template_corr': 0.96, 'notch': True}
```

`samples` must be a **list**, matching what `on_epoch` receives — the
`isinstance(v, (int, float))` filter is kept verbatim from production and will raise on a
bare numpy array.

`python v7_quality_isolated.py` runs the four-case self-test above.
