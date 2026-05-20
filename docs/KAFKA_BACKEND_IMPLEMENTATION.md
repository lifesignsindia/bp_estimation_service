# LifeSigns — Kafka & Backend Implementation
**Version:** 3.0
**Date:** 2026-05-18
**Scope:** Kafka topic design, input/output format, consumer/producer wiring, Docker deployment

---

## 1. Overview

The LifeSigns pipeline uses Kafka as its message bus. All BLE devices (NISO101, NISO103, NISO204) and the reference cuff publish JSON packets to one input topic. The pipeline consumes from that topic, runs `process_vitals()`, and publishes only clinical outputs (`success` and `alert`) to one output topic.

```
  BLE Gateway
       |
       | vitals.raw  (all device types, key=admissionId)
       v
  kafka_consumer.py
       |  process_vitals()
       | vitals.clinical  (success + alert only, key=admissionId)
       v
  Downstream (dashboard, EHR, alert handler)
```

---

## 2. Topic Design

### `vitals.raw` — Inbound
**Direction:** BLE Gateway → pipeline
**Partitions:** 10
**Partition key:** `admissionId`

All device types publish here. Device routing is determined by:
- `bp` key present → cuff pathway
- `deviceName` field → PPG pathway (`NISO101` / `NISO103` / `NISO204`)

---

### `vitals.clinical` — Outbound
**Direction:** pipeline → downstream consumers
**Partitions:** 10
**Partition key:** `admissionId`

Only two statuses published here:

| Status | Meaning |
|--------|---------|
| `success` | 15-minute averaged reading, calibration confirmed |
| `alert` | Mismatch detected between reference BP and AI estimate |

`accumulating`, `poor_signal`, `ignored`, `error` → stdout only, never forwarded.

---

## 3. Input Packet Format

### PPG Device Packet (NISO101 / NISO103 / NISO204)

```json
{
  "admissionId": "ADM454567633",
  "patientId": "ShivaTestkedke",
  "patientName": "Shivam Kumar",
  "assignedDoctor": "Dr. Shivam Kumar",
  "facilityId": "CF199221737",
  "deviceId": "BPLKPO",
  "deviceName": "NISO101",
  "epochTime": 1779089450,
  "seqNum": 1,
  "seqPart": 1,
  "spo2": 98,
  "device": "NISO101",
  "cgroup": "TestingA",
  "pgroup": "5th Floor",
  "Age": 29,
  "Gender": "MALE",
  "BMI": 24.2,
  "pleth": {
    "PLETH": [681473, 681236, 679581, ...]
  }
}
```

**Required fields:** `admissionId`, `deviceName`, `pleth.PLETH`
**Optional fields:** all others are passthrough `_meta` — included in output if present

**deviceName values:** `"NISO101"` | `"NISO103"` | `"NISO204"` — top level, case sensitive
**pleth.PLETH:** minimum 120 samples, real device sends ~3600

---

### Cuff Reference Packet

```json
{
  "admissionId": "ADM454567633",
  "bp": {
    "BPSYS": 120,
    "BPDIA": 80,
    "BP_ERROR": 0
  }
}
```

**Required fields:** `admissionId`, `bp.BPSYS`, `bp.BPDIA`, `bp.BP_ERROR`
**BP_ERROR:** integer — `0` = valid reading, non-zero = hardware error (rejected)
**Send once per patient session** before PPG packets for calibration to work.

---

## 4. Output Packet Format

Both `success` and `alert` share the same structure.

```json
{
  "patientId": "ShivaTestkedke",
  "facilityId": "CF199221737",
  "patientName": "Shivam Kumar",
  "assignedDoctor": "Dr. Shivam Kumar",
  "deviceId": "BPLKPO",
  "epochTime": 1779089450,
  "seqNum": 1,
  "seqPart": 1,
  "spo2": 98,
  "device": "NISO101",
  "cgroup": "TestingA",
  "pgroup": "5th Floor",
  "status": "success",
  "admissionId": "ADM454567633",
  "deviceName": "NISO101",
  "deviceType": "BP_SPO2",
  "timestamp": 1779090350,
  "reading_count": 5,
  "bp": {
    "bpSystolic": 126,
    "bpDiastolic": 81,
    "estimated_sbp": 126,
    "estimated_dbp": 81,
    "category": "normal",
    "trend": { "trend": "Stable ->", "slope": 0.1, "readings": 5 },
    "BP_ERROR": 0
  },
  "sqi": { "score": 0.94, "valid": true, "flag": "GOOD" },
  "trending": false,
  "morphology_change": "stable",
  "hemoglobin": 13.4,
  "glucose": 97,
  "pleth": { "PLETH": [0.123456, 0.456789, "..."] },
  "message": "15-minute averaged clinical payload."
}
```

**Alert payload** — same structure, `status: "alert"` and different message:
```json
{
  "status": "alert",
  "message": "Averaged Calibration Mismatch: Cuff=120/80, AI_Avg=145/98."
}
```

---

## 5. Consumer Configuration

```python
Consumer({
    "bootstrap.servers":  KAFKA_BROKERS,
    "group.id":           "vitals-pipeline",
    "auto.offset.reset":  "latest",
    "enable.auto.commit": True,
    "session.timeout.ms": 30000,
    "max.poll.interval.ms": 300000,
})
```

**Group ID:** `vitals-pipeline` — all instances share this group.
**Auto offset reset:** `latest` — picks up from current tail on startup, no historical reprocessing.

---

## 6. Producer Configuration

```python
Producer({
    "bootstrap.servers":        KAFKA_BROKERS,
    "linger.ms":                20,
    "batch.num.messages":       500,
    "compression.type":         "snappy",
    "acks":                     "1",
    "delivery.timeout.ms":      10000,
})
```

Partition key = `admissionId` (UTF-8). Flush every 50 messages.

---

## 7. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BROKERS` | `localhost:9092` | Kafka bootstrap server |
| `KAFKA_INPUT_TOPIC` | `vitals.raw` | Topic to consume from |
| `KAFKA_OUTPUT_TOPIC` | `vitals.clinical` | Topic to produce to |
| `KAFKA_GROUP_ID` | `vitals-pipeline` | Consumer group ID |
| `REDIS_HOST` | `localhost` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | _(none)_ | Redis password (optional) |

In Docker: injected via `environment` block in `docker-compose.yml`.
Inside Docker network: `KAFKA_BROKERS=kafka:29092`, `REDIS_HOST=redis`.

---

## 8. Docker Deployment

### Services

| Service | Image | Role |
|---------|-------|------|
| `zookeeper` | confluentinc/cp-zookeeper:7.5.0 | Kafka metadata coordination |
| `kafka` | confluentinc/cp-kafka:7.5.0 | Message broker |
| `kafka-init` | confluentinc/cp-kafka:7.5.0 | One-shot topic creation on startup |
| `redis` | redis:7-alpine | Reference BP persistence |
| `pipeline` | built from Dockerfile (python:3.12-slim) | Kafka consumer + AI inference engine |

### Startup order

```
  zookeeper → kafka → kafka-init (creates topics) → redis → pipeline
```

Pipeline waits for both kafka and redis healthchecks before starting. `restart: on-failure` handles transient startup failures.

### Commands

```powershell
# Start full stack
docker compose up -d

# First time or after code changes
docker compose build --no-cache
docker compose up -d

# Watch pipeline logs
docker compose logs -f pipeline

# Stop
docker compose down

# Clean corrupted Docker cache
docker builder prune -f
docker system prune -f
```

---

## 9. Testing Kafka End-to-End

**Terminal 1 — Watch pipeline:**
```powershell
docker compose logs -f pipeline
```

**Terminal 2 — Watch output:**
```powershell
docker exec -it new_esti-kafka-1 kafka-console-consumer --bootstrap-server localhost:9092 --topic vitals.clinical --from-beginning
```

**Terminal 3 — Send reference:**
```powershell
echo '{"admissionId":"ADM001","bp":{"BPSYS":120,"BPDIA":80,"BP_ERROR":0}}' | docker exec -i new_esti-kafka-1 kafka-console-producer --bootstrap-server localhost:9092 --topic vitals.raw
```

**Terminal 3 — Send PPG packet (compact to single line):**
```powershell
python3 -c "import json; f=open('sample_payloads/101.json'); print(json.dumps(json.load(f)))" | docker exec -i new_esti-kafka-1 kafka-console-producer --bootstrap-server localhost:9092 --topic vitals.raw
```

**What to expect:**
- Terminal 1: `[RT_LOG] Admission: ADM001 | AI Estimate: .../...`
- Terminal 1: `[OUT] ALERT` or `[ACC]`
- Terminal 2: Full output JSON if `success` or `alert`

**Note:** JSON must be sent as a single line — kafka-console-producer treats each line as a separate message.

---

## 10. Graceful Shutdown

Pipeline handles `SIGINT` and `SIGTERM`:

```
Signal received → _running = False → poll loop exits → producer.flush() → consumer.close()
```

Docker sends `SIGTERM` on `docker compose down`. Pipeline drains current cycle and exits cleanly.

---

## 11. Error Handling

| Scenario | Behaviour |
|----------|-----------|
| JSON decode failure | Log `[KAFKA] Decode error`, skip packet, continue |
| `process_vitals()` exception | Log `[KAFKA] process_vitals error`, skip packet, continue |
| Redis unreachable on startup | `_redis.ping()` raises — pipeline exits immediately |
| Redis unreachable during operation | `_ref_read`/`_ref_write` raises → caught → `status=error` returned |
| Unknown deviceName | `status=error`, message lists valid device names |
| Signal too short | `status=error`, not forwarded |
| Flat signal | `status=poor_signal`, not forwarded |
| numpy/model version mismatch | Hb/Glucose return N/A, BP still works |

---

## 12. Scaling

**Single instance:** All 10 partitions consumed by one pipeline. Supports ~50 simultaneous patients.

**Multi-instance:** Add more `pipeline` containers sharing `group.id=vitals-pipeline`. Kafka distributes partitions automatically. SESSION_STORAGE is in-memory per instance — Kafka partition assignment guarantees consistency. Redis reference BP is shared across all instances.

Do not exceed one pipeline instance per partition.
