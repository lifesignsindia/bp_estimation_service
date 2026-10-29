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
