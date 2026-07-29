# PlethGuard — NISO206 SpO₂ shadow guard

A **standalone, read-only side-car** that reduces false SpO₂ alarms for NISO206 finger sensors.
It runs **alongside** the BP-estimation pipeline and **does not touch it**.

## What it does
- Reads the **same input** the pipeline reads (Kafka `vitals.raw`) using a **separate consumer
  group** (`plethguard-shadow`) — so it never affects the pipeline's offsets or output.
- Processes **only** packets that are `deviceName = NISO206` **and** from an allow-listed facility.
- Decides, per epoch, **DISPLAY** vs **ALERT SUPPRESSED** for the SpO₂ (device-flag + pleth/PI
  signal quality + confirm-before-alarm + apnea/hypoxemia screen).
- Writes each verdict to **its own Mongo collection** (and/or a JSONL file). It **never** produces
  to `vitals.clinical` and **never** writes any existing collection.

## Isolation guarantees
- Separate Kafka consumer group → independent read, no offset interference.
- Output only to its own Mongo collection / file → main pipeline & backend unaffected.
- For NISO206 the pipeline already emits nothing, so PlethGuard is purely additive.

## Files
- `plethguard_core.py` — the decision logic (self-contained; validated offline + against a
  nurse-confirmed false alert).
- `plethguard_service.py` — the shadow runner (Kafka / file / folder modes).

## Run
```bash
# live shadow, for chosen facilities, output to a dedicated Mongo DB:
PLETHGUARD_FACILITIES="CF...,CF..." \
PLETHGUARD_MONGO="mongodb://<host>:27017" \
PLETHGUARD_MONGO_DB=plethguard \
PLETHGUARD_MONGO_COLL=spo2_verdicts \
python plethguard/plethguard_service.py --kafka

# local test (no Kafka):
python plethguard/plethguard_service.py --folder <admission_folder>
python plethguard/plethguard_service.py --file  <packet.json>
```

## Env
| var | default | meaning |
|---|---|---|
| `KAFKA_INPUT_TOPIC` | `vitals.raw` | input topic (same as pipeline) |
| `KAFKA_BROKERS` | `localhost:9092` | brokers |
| `PLETHGUARD_GROUP` | `plethguard-shadow` | consumer group (keep separate) |
| `PLETHGUARD_FACILITIES` | *(empty = all)* | comma-separated facility allow-list |
| `PLETHGUARD_MONGO` | *(empty = off)* | Mongo URI for the verdict collection |
| `PLETHGUARD_MONGO_DB` / `_COLL` | `local` / `plethguard_spo2_verdicts` | target DB / collection |
| `PLETHGUARD_OUT` | `plethguard_verdicts.jsonl` | JSONL fallback sink |
| `PLETHGUARD_TOPIC` | *(empty = off)* | optional separate verdict topic |

## Status
Shadow / observe-only. Verdicts are informational and do not drive the UI until an alarm/display
layer is wired to consume them (a deliberate, validated later step). It is a **screen**, not a
diagnosis — apnea flags mean "suspected, refer for a sleep study."
