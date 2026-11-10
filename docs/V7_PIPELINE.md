# v7 in the Kafka pipeline

Branch `features/v7-pipeline`. BP is now produced by v7; Hb and glucose still come from the
legacy engine. One payload per patient per 15-minute slot reaches Kafka, nothing in between.

## What runs per input message

```
vitals.raw  ─►  kafka_consumer.py  ─►  process_vitals()  ─►  vitals.clinical (success | alert only)
                 facility gate            │
                 (temporary)              ├─ NISO206 / cuff  → Redis ref:{adm}   (unchanged path)
                                          │
                                          └─ NISO101 / 103 / 204 pleth epoch
                                               1. device preprocessing + legacy flat check   (unchanged)
                                               2. legacy engine → Hb, glucose only
                                               3. v7_engine.score_epoch(raw samples @ device Hz)
                                                    stage A  <1200 samples / constant → FLAT/SHORT   (dropped)
                                                    stage B  beats ≥10, template corr ≥0.90, notch → GOOD
                                                             else FAIR / POOR                        (dropped)
                                                    no cuff        → "no reference"                  (nothing)
                                                    anchor < 6 GOOD → "calibrating"                  (CALIBRATING*)
                                                    GOOD epoch      → joins the open 15-min slot
                                                    first epoch of a NEW slot closes the old one → PUBLISH

  * until the first 15-min value after a cuff, the CUFF itself is published once per slot
    (confidence CALIBRATING) — see "CALIBRATING" below
```

The device already sends one 18-second epoch (3600 samples at 200 Hz) every 180 s per
admission, so the pipeline processes every arrival. No throttling is needed.

## Timing after a cuff

| step | needs | earliest |
|---|---|---|
| CALIBRATING (the cuff value) | any non-flat epoch after the cuff | ~3 min, then once per slot |
| anchor | 6 GOOD epochs | ~18 min |
| **early first value** (`window.early: true`, `LOW`) | anchor built + 2 GOOD scored epochs in the open slot | ~21-25 min (published at once, not at the slot end) |
| first published value (`confidence: LOW`) | slot with ≥2 GOOD epochs closes | ~30–35 min |
| established (`confidence: HIGH`) | 2 consecutive counted slots | ~45–50 min |
| alert | established AND 2 consecutive slots ≥15 sys or ≥10 dia off the cuff, so earliest on the 2nd counted slot | ~45–50 min |

Slots are wall-clock aligned (`epochTime // 900`) and close only when the next epoch arrives.
A silence of ≥30 min discards the open slot rather than publishing it late.

## Published payload (same shape as before, additive fields)

```json
{
  "status": "success | alert",
  "admissionId": "...", "deviceName": "NISO101", "deviceType": "BP_SPO2", "timestamp": 1756900000,
  "reading_count": 4,                 // GOOD epochs in the slot
  "confidence": "LOW | HIGH",         // new
  "bp": { "estimated_sbp": 124, "estimated_dbp": 79, "category": "normal",
          "trend": {"trend": "Stable ->", "slope": 0.0, "readings": 3},
          "reference_sbp": 120, "reference_dbp": 80, "BP_ERROR": 0 },
  "alert": "" | "SBP" | "DBP" | "SBP+DBP",   // new; latched until the next cuff
  "sqi": { ...device sqi..., "v7_quality": "GOOD", "n_beats": 18, "template_corr": 0.95 },
  "trending": false,                  // slot median ≥15/10 off the cuff
  "morphology_change": "stable | rising | falling",   // from the slot-to-slot trend
  "window": {"start": ..., "end": ..., "good_epochs": 4, "epochs": 5, "established": true},  // new
  "hemoglobin": 9.8, "glucose": 104,  // legacy engine, mean over the slot's GOOD epochs
  "pleth": {"PLETH": [...]},          // the closing epoch, 120 Hz
  "message": "...",
  ...all input _meta fields passed through (patientId, facilityId, spo2, device, ...)
}
```

### CALIBRATING (added 2026-09-28)

v7 is *cuff + morphology change since the anchor*; right after a cuff that change is ~0, so the
first model value is ~the cuff anyway, just 30 min later. So from the first non-flat epoch after
a cuff, the cuff itself is published as a normal `success` payload with `confidence: "CALIBRATING"`,
`reading_count: 0`, `estimated_* == reference_*` = the cuff, `alert` as latched (a new cuff clears
it), `window` = the current slot. At most one per wall-clock slot, and none once the first 15-min
value (`LOW`) has been published for that cuff. A new cuff restarts it with the new value.
**OFF by default since 2026-10-01** (on QA the repeated cuff value read as a stuck estimate);
`V7_CAL_PUBLISH=1` switches it back on. It is the
measured cuff, not a model output, so it does not touch v7 accuracy.

`status: alert` is emitted on **every** slot while the latch is on. A new cuff (even an identical
repeat) clears it and rebuilds the anchor.

### Early first value (2026-10-08)

After a cuff, the first value no longer waits for its slot to end: as soon as the anchor is built
and the open slot holds `V7_MIN_EPOCHS_WINDOW` (2) GOOD scored epochs, their median is published
once, as a normal `success` with `confidence: "LOW"` and `window.early: true`. It is a real v7
estimate, not the cuff. It does **not** count as a slot: `run`, the alert rule and the trend are
untouched, and the same slot still publishes its full value at its end (still `LOW`, then `HIGH`
on the next slot). One early value per cuff; a new cuff re-arms it. `V7_EARLY_FIRST=0` turns it off.

### Re-anchor rule and smoothing (2026-11-03)

**Re-anchor only on a real change.** A new cuff (device or manual) becomes the anchor only if it
differs from the anchor held by >= 15 systolic or >= 10 diastolic. A cuff inside that range keeps
the anchor, its baseline and the open slot (no re-calibration gap); it still retires a latched
alert. The newest cuff seen is the forward-only limit for replays.

**Smoothed value.** The published `estimated_*` is `anchor + 0.5 * (EMA - anchor)`, EMA = exponential
average (alpha 0.25) of the GOOD per-epoch v7 values since the anchor. One noisy slot no longer
swings the display and only half of v7's change is shown. The alert rule is unchanged: it still
reads the raw slot median (`sbp_raw` / `dbp_raw` in the engine's window), because half of a
capped 25 mmHg change could never reach the 15 mmHg breach.

Replay on the CIMS NISO101 captures (60 cuffs, 23 patients; last value published before each real
cuff vs that cuff):

| | raw slot median (before) | smoothed (now) |
|---|---|---|
| SBP within 15 | 57% | 70% |
| SBP within 15 and DBP within 10 | 47% | 55% |
| MAP within 10 | 62% | 77% |
| consecutive values >= 15/10 apart | 12% | 2% |

Repeating the anchor cuff scores about the same (60% both, MAP 80%): v7's change is mostly noise
on NISO101 (no single feature tracks BP between cuffs on this data), so the gain is from
damping that noise, not from better tracking.

### Change check: "BP may have changed - take a cuff" (2026-11-03, SHADOW until validated)

Not a BP number. With every anchor, the change check learns the patient's own baseline of three
signals from the same GOOD epochs that build the anchor: `aging_index`, `apg_b_a` (pulse-shape,
second derivative) and pleth `hr` (median and robust spread, with a noise floor). Each closed
15-min slot is turned into z-scores against that baseline. The score is the mean of the clipped z
(all three signed "BP up" = up). If |score| >= 2.1 in 2 consecutive slots in the same direction,
the check is raised: `BP may have changed (likely rising / falling) - take a manual cuff`. It stays
latched until the next cuff; any new cuff, even one inside the +-15/+-10 range, retires it. It
also reports a mean-reversion opinion (`reversion`: a reference well above / below the patient's
earlier cuffs, or 125 systolic, tends to come back). That is a second opinion only and never raises
the check.

Every closed-slot payload carries it (additive field; display and estimates unaffected):

    "change_check": {"suspected": true, "direction": "up", "score": 2.4,
                     "signals": {"aging_index": 2.9, "apg_b_a": 2.1, "hr": 2.2},
                     "reversion": "down", "mode": "shadow",
                     "message": "BP may have changed (likely rising) - take a manual cuff."}

`V7_CHANGE_CHECK=shadow` (default): computed, logged (`[CHANGE]`) and stored; the published alert
keeps the +-15/+-10 median rule. `live`: a raised check is published as `status: "alert"`,
`alert: "BP_MAY_HAVE_CHANGED"` with the message above, and replaces the median rule. `off`: not
computed.

**Evidence so far (CIMS NISO101, 67 cuff intervals, 23 patients, 28 real changes; signs and
threshold learned leave-one-patient-out).** With the threshold set for <= 20% false checks: flagged
before 7 of 28 real changes (25%) vs 7 of 39 stable intervals (18%), so it does NOT yet tell
whether BP changed (p = 0.34). When it fired before a real change, the direction was right 7/7
(10/11 at a looser threshold). Too few cases to trust either way.

**Go-live pass mark (fixed 2026-11-03, before any new data is seen).** Score on NISO101 data
collected after this date (new patients only), interval = reference in use -> next cuff, real
change = >= 15 SBP or >= 10 DBP, defaults unchanged:
1. flagged before >= 40% of real changes, AND
2. flagged before <= 20% of stable intervals, AND
3. flag rate real > stable with one-sided Fisher p < 0.05, AND
4. direction right on >= 70% of flagged real changes, with at least 15 flagged real changes.
If it passes, set `V7_CHANGE_CHECK=live`. If not, it stays in shadow; signals or thresholds are
changed only with a new pass mark written before re-testing on data not yet used.

### NISO204 not processed (2026-11-10)

NISO204 packets are dropped right after device detection: no BP estimation, no SpO2 / PR
handling, no primary-device lock, and no output packet (like a facility outside the gate). Its
pleth is ~60 Hz, not the 200 Hz the DSP assumed, so every packet ended as `poor_signal` and the
null rule sent its valid SpO2 / PR as null. NISO204 cuffs were never references (only NISO206 and
manual entries are), so nothing that fed v7 is lost.

### Display flag (2026-09-28)

Every result now reaches `vitals.clinical` (`EBP_FORWARD_STATUSES`, default
`success,alert,accumulating,poor_signal,ignored,error`; set `success,alert` to go back).
Each payload carries `"display": true|false`:

| status | display | BP keys |
|---|---|---|
| `success`, `alert` (incl. `CALIBRATING`) | `true` | `bp.estimated_sbp`, `bp.estimated_dbp`, `bp.estimated_map` (integers) |
| `accumulating` (per-epoch v7 value, ~every 3 min) | `false` | `bp.Estimated_sbp`, `bp.Estimated_dbp`, `bp.Estimated_map` (capital E, integers) |
| `poor_signal`, `ignored`, `error` | `false` | no estimated BP |

The two casings are deliberate: the backend shows only lowercase `estimated_*` (exact,
case-sensitive match) and stores capital-E `Estimated_*` without showing it.

### Output contract (2026-10-20, backend integration review)

- **The output is the input packet plus our fields.** Every input field keeps its name and value
  (facilityId, patientId, the original `pleth` block, a cuff packet's `bp` block, unknown fields).
  `deviceName` / `deviceType` / `timestamp` are only added when the input lacks them. In the `bp`
  block only `estimated_sbp/dbp/map` are ours; any input bp field wins over our other bp fields.
  NISO101 packets still get `spo2.PR` added (mean of `PR_ALL`).
- **Lowercase estimates only on display packets**, so `bp.estimated_sbp > 0` (case-sensitive)
  identifies a shown estimate. Accumulating packets carry `bp.Estimated_*` for storage. All
  values are integers; MAP = `round((sbp + 2*dbp) / 3)`.
- **Range check.** A display estimate outside SBP 40-300 / DBP 20-200, or with SBP <= DBP, becomes
  `status: error`, `error_code: ESTIMATE_OUT_OF_RANGE` (env `EBP_EST_SBP_MIN/MAX`, `EBP_EST_DBP_MIN/MAX`).
- **`error_code`** on every packet: `null` for success, alert, accumulating and an accepted cuff;
  otherwise one of `CUFF_HW_ERROR`, `CUFF_SENTINEL`, `CUFF_EMPTY`, `CUFF_DUPLICATE`,
  `UNKNOWN_DEVICE`, `DEVICE_LOCKED`, `POOR_SIGNAL`, `FLAT_SIGNAL`, `SIGNAL_TOO_SHORT`,
  `NO_REFERENCE_CUFF`, `EPOCH_REJECTED`, `INFERENCE_FAILED`, `ESTIMATE_OUT_OF_RANGE`.
  `message` stays as human-readable detail.

- **Null on error.** On `poor_signal` (incl. flat signal), any `error`, or a device SpO2 / PR error
  in the spo2 block (`spo2Error`/`SPO2_ERROR` non-zero, or `spo2ErrorMsg` / `prErrorMsg` not a
  `*_VALID` message, e.g. `SPO2_UNKNOWN_ERROR`, `PR_INVALID_DATA`), the packet carries SpO2, PR
  (`spo2`/`pulseRate`, or `SPO2`/`PR`) and `bp.estimated_sbp/dbp/map` as JSON `null`, keys present
  even when the device dropped them; accumulating `Estimated_*` are nulled too. Cuff readings are
  never touched and no spo2 block is invented. Such a packet has `display: false`; when it had no
  error_code it gets `SPO2_INVALID`, `PR_INVALID` or `SPO2_PR_INVALID` (this includes a 15-min
  success/alert packet, whose estimate is then null).

All of it is applied in one place, `process_vitals`.

## State

One Redis key per admission, `v7:{admissionId}`, JSON, 24 h TTL: anchor cuff + its 16-feature
anchor vector, the open slot, `run`, `hot_run`, the alert latch, the last cuff identity. It sits
next to `ref:{adm}` so pods and restarts share it.

## Configuration (env, defaults = validated prototype)

| var | default | meaning |
|---|---|---|
| `V7_MODEL_DIR` | `models/v7` | pkls + manifest + `feature_medians.json` |
| `V7_WINDOW_SEC` | 900 | slot length |
| `V7_MIN_EPOCHS_WINDOW` | 2 | GOOD epochs a slot needs to publish |
| `V7_MIN_EPOCHS_ANCHOR` | 6 | GOOD epochs to build the anchor |
| `V7_GATE_BEATS` / `V7_GATE_CORR` | 10 / 0.90 | GOOD gate |
| `V7_ALERT_SBP` / `V7_ALERT_DBP` / `V7_ALERT_PERSIST` | 15 / 10 / 2 | alert rule |
| `V7_CAP_MMHG` | 25 | max delta from the cuff |
| `V7_STALE_SEC` | 1800 | silence that discards the open slot |
| `V7_EARLY_FIRST` | 1 | publish the first value after a cuff as soon as the open slot has 2 GOOD scored epochs (0 = wait for the slot end) |
| `V7_CAL_PUBLISH` | 0 | publish the cuff as `CALIBRATING` until the first 15-min value (1 = on) |
| `V7_REANCHOR_SBP` / `V7_REANCHOR_DBP` | 15 / 10 | a new cuff re-anchors only this far from the anchor (0 / 0 = every cuff) |
| `V7_SMOOTH` | 1 | publish anchor + `V7_SMOOTH_SHRINK` x (EMA - anchor) instead of the raw slot median (0 = raw) |
| `V7_SMOOTH_ALPHA` / `V7_SMOOTH_SHRINK` | 0.25 / 0.5 | EMA weight per GOOD epoch / share of v7's change shown |
| `V7_CHANGE_CHECK` | shadow | change check: `shadow` (report only), `live` (drives the alert), `off` |
| `V7_CHANGE_THRESHOLD` / `V7_CHANGE_PERSIST` | 2.1 / 2 | change score needed / consecutive slots |

## Decisions taken (2026-09-03)

- v7 owns BP for all three PPG devices. It is validated on NISO101 only; NISO103 (auto-sensed
  Hz, int8 wraparound fix kept) and NISO204 run unvalidated.
- Hb/glucose: legacy engine, `HB_BIAS_G_DL` still applied.
- Poor/flat epochs inside a slot are dropped, not published. Flat-line handling for the ward
  is the backend's job.
- No estimate without a cuff. First alert ~1 h after the cuff. (2026-09-28: the cuff itself is
  now shown as `CALIBRATING` from ~3 min after it, until the first 15-min value.) Alerts latch until a new cuff.
- Kept as-is, still temporary: the facility gate (`EBP_ALLOWED_FACILITY`, default
  `CF1315821527` only since 2026-09-05, `CF1315821527,CF199221737` since 2026-09-28, `CF1315821527,CF199221737,CF965742886,CF1094500946` since 2026-10-29; comma-separate to add, empty to disable) and the Mongo
  shadow sink (`MONGO_SINK_ENABLED`).
- 2026-09-28 cuff path: `0/0` (or any zero half) is "no reading" and ignored. A non-manual cuff
  within 5 mmHg of the stored reference is a re-send and keeps the stored reference AND its
  timestamp (the old 15-min cooldown never fired under v7, so monitors that re-broadcast their
  last NIBP, e.g. LEPU, rebuilt the anchor on every packet). Consequence: a same-value DEVICE cuff
  no longer clears an alert; a MANUAL entry, or a changed value, still does.
- The cuff path (dedupe of identical re-sends within 15 min) was otherwise unchanged. v7 keys the cuff by
  its `epochTime`; a repeat that the dedupe lets through still clears the alert.

## Test

```
python test_pipeline/test_v7_pipeline.py
```

Runs `process_vitals` against fakeredis with real NISO101 epochs from
`ebp_dashboard/pleth_capture`: one payload per slot, dropped bad epochs, payload shape, anchor
rebuild, forced-alert latch and release.

## Image

`.dockerignore` now excludes dashboards, analysis, captures, databases, `.pem` keys and docs.
The image needs only: `kafka_consumer.py vitals_standalone.py v7_engine.py bpv4_features.py
inference_engine.py mongo_sink.py config.py processors/ models/ requirements.txt`.
