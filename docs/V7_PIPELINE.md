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
                                                    anchor < 6 GOOD → "calibrating"                  (nothing)
                                                    GOOD epoch      → joins the open 15-min slot
                                                    first epoch of a NEW slot closes the old one → PUBLISH
```

The device already sends one 18-second epoch (3600 samples at 200 Hz) every 180 s per
admission, so the pipeline processes every arrival. No throttling is needed.

## Timing after a cuff

| step | needs | earliest |
|---|---|---|
| anchor | 6 GOOD epochs | ~18 min |
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

`status: alert` is emitted on **every** slot while the latch is on. A new cuff (even an identical
repeat) clears it and rebuilds the anchor.

Per-epoch results (`accumulating`, `poor_signal`, `ignored`, `error`) are still returned by
`process_vitals` for the consumer's logs and the Mongo shadow sink, but `FORWARD_STATUSES` is
back to `{"success", "alert"}` so they never reach `vitals.clinical`.

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

## Decisions taken (2026-09-03)

- v7 owns BP for all three PPG devices. It is validated on NISO101 only; NISO103 (auto-sensed
  Hz, int8 wraparound fix kept) and NISO204 run unvalidated.
- Hb/glucose: legacy engine, `HB_BIAS_G_DL` still applied.
- Poor/flat epochs inside a slot are dropped, not published. Flat-line handling for the ward
  is the backend's job.
- No estimate without a cuff. First alert ~1 h after the cuff. Alerts latch until a new cuff.
- Kept as-is, still temporary: the facility gate (`EBP_ALLOWED_FACILITY`, default
  `CF1315821527` only since 2026-09-05; comma-separate to add, empty to disable) and the Mongo
  shadow sink (`MONGO_SINK_ENABLED`).
- The cuff path (dedupe of identical re-sends within 15 min) is unchanged. v7 keys the cuff by
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
