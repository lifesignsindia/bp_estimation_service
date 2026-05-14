# LifeSigns — Kafka & Backend Implementation
**Version:** 2.0
**Date:** 2026-05-14
**Scope:** Kafka topic design, consumer/producer wiring, Docker deployment, and startup instructions

---

## 1. Overview

The LifeSigns pipeline uses Kafka as its message bus. All BLE devices (NISO204, CHECKME, BERRYMED, LS06) publish their JSON packets to one input topic. The pipeline consumes from that topic, runs `process_vitals()`, and publishes only clinical outputs (`success` and `alert`) to one output topic. All other statuses (`accumulating`, `poor_signal`, `ignored`, `error`) are written to stdout only.

**Partition key = `admissionId` on both topics.** This guarantees all packets for one patient land on the same partition and consumer instance, keeping SESSION_STORAGE consistent in memory.

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
**Retention:** default (7 days)  
**Partition key:** `admissionId`  

All device types publish here. The pipeline inspects the payload to determine device type and routing. No separate reference BP topic — the LS06 cuff packet arrives on this same topic and is handled by the cuff pathway inside `process_vitals()`.

---

### `vitals.clinical` — Outbound
**Direction:** pipeline → downstream consumers  
**Partitions:** 10  
**Retention:** default (7 days)  
**Partition key:** `admissionId`  

Only two statuses are published here:

| Status | Meaning |
|--------|---------|
| `success` | 15-minute averaged reading, calibration confirmed |
| `alert` | Immediate mismatch or averaged mismatch vs reference BP |

Everything else — `accumulating`, `poor_signal`, `ignored`, `error` — goes to stdout only. Downstream consumers only see actionable clinical data.

---

## 3. Consumer Configuration

```python
Consumer({
    "bootstrap.servers":  KAFKA_BROKERS,
    "group.id":           "vitals-pipeline",
    "auto.offset.reset":  "latest",
    "enable.auto.commit": True,
})
```

**Group ID:** `vitals-pipeline` — all pipeline instances share this group; Kafka distributes partitions among them.

**Auto offset reset:** `latest` — on startup, the pipeline picks up from the current tail. Historical packets are not reprocessed. This avoids stale data flooding a fresh deployment.

**Auto commit:** `True` — offsets are committed after each poll cycle. In the rare case of a crash mid-processing, the current packet may be reprocessed once.

---

## 4. Producer Configuration

```python
Producer({
    "bootstrap.servers": KAFKA_BROKERS,
})
```

`producer.flush()` is called immediately after each message to ensure delivery before moving to the next packet.

Partition key = `admissionId` (UTF-8 encoded). This keeps all packets for one patient on the same partition in both input and output topics.

---

## 5. Message Flow

```
  Poll vitals.raw (timeout=1.0s)
          |
          | JSON decode
          v
  process_vitals(payload)
          |
          |─── status in {success, alert}?
          |         |
          |         | produce to vitals.clinical
          |         | key=admissionId
          |         | flush()
          |         | log: [KAFKA] Forwarded | adm=... | status=...
          |
          |─── status NOT in {success, alert}?
                    |
                    | log: [KAFKA] Suppressed | status=... | msg=...
                    | (stdout only, nothing sent downstream)
```

---

## 6. Environment Variables

All configuration is read from environment variables (with defaults for local development):

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BROKERS` | `localhost:9092` | Kafka bootstrap server address |
| `KAFKA_INPUT_TOPIC` | `vitals.raw` | Topic to consume from |
| `KAFKA_OUTPUT_TOPIC` | `vitals.clinical` | Topic to produce to |
| `REDIS_HOST` | `localhost` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | _(none)_ | Redis password (optional) |

In Docker, these are injected via the `environment` block in `docker-compose.yml`.

---

## 7. Docker Deployment

The full stack runs in Docker Compose with four services: Zookeeper, Kafka, Redis, and the pipeline container.

### docker-compose.yml summary

| Service | Image | Role |
|---------|-------|------|
| `zookeeper` | confluentinc/cp-zookeeper:7.5.0 | Kafka metadata coordination |
| `kafka` | confluentinc/cp-kafka:7.5.0 | Message broker |
| `kafka-init` | confluentinc/cp-kafka:7.5.0 | One-shot topic creation on startup |
| `redis` | redis:7-alpine | Reference BP persistence |
| `pipeline` | (built from Dockerfile) | Kafka consumer + inference engine |

### Startup order

```
  zookeeper starts
       |  (healthcheck: nc -z localhost 2181)
       v
  kafka starts
       |  (healthcheck: kafka-broker-api-versions --bootstrap-server localhost:9092)
       v
  kafka-init runs  →  creates vitals.raw and vitals.clinical (10 partitions each)
  redis starts     →  (healthcheck: redis-cli ping)
       |
       v
  pipeline starts  →  loads AI models, pings Redis, subscribes to vitals.raw
```

The `pipeline` service declares `depends_on` for both `kafka` (service_healthy) and `redis` (service_healthy). It will not start until both pass their healthchecks. `restart: on-failure` handles transient startup failures.

---

## 8. Running the Stack

### First time

```bash
# Start everything
docker compose up --build -d

# Watch pipeline logs
docker compose logs -f pipeline
```

### Stopping

```bash
docker compose down
```

### Checking Kafka topics

```bash
docker compose exec kafka kafka-topics --list --bootstrap-server localhost:9092
```

### Sending a test packet (from host)

```bash
# Produce a test message to vitals.raw
docker compose exec kafka kafka-console-producer \
  --broker-list localhost:9092 \
  --topic vitals.raw \
  --property "parse.key=true" \
  --property "key.separator=:"
# Type: ADM001:{"admissionId":"ADM001","DeviceName":"NISO204","Pleth":[...]}
```

### Consuming from vitals.clinical

```bash
docker compose exec kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic vitals.clinical \
  --from-beginning
```

---

## 9. Graceful Shutdown

The pipeline handles `SIGINT` and `SIGTERM`:

```
Signal received
       |
  _running = False
       |
  Poll loop exits cleanly
       |
  consumer.close()
       |
  [KAFKA] Consumer closed.
```

Docker Compose sends `SIGTERM` on `docker compose down`. The pipeline drains its current poll cycle and exits within 1–2 seconds.

---

## 10. Scaling

**Single instance (current design):**
All 10 partitions are consumed by one pipeline instance. This supports up to ~50 simultaneous patients on a single server.

**Multi-instance:**
Add more `pipeline` containers sharing the same `group.id`. Kafka distributes partitions automatically. Because SESSION_STORAGE is in-memory, each instance must consistently own the same partitions (which Kafka guarantees by partition assignment). Reference BP in Redis is accessible by any instance.

**Do not exceed one pipeline instance per partition.** Extra instances in the same group will be idle (Kafka assigns at most one consumer per partition per group).

---

## 11. Error Handling

| Scenario | Behaviour |
|----------|-----------|
| Kafka broker unreachable on startup | confluent-kafka retries connection; pipeline retries until Kafka healthcheck passes |
| JSON decode failure | Log `[KAFKA] Failed to decode message`, skip packet, continue |
| `process_vitals()` raises exception | Log `[KAFKA] process_vitals exception`, skip packet, continue |
| Redis unreachable on startup | `_redis.ping()` raises — pipeline exits immediately (fail-fast) |
| Redis unreachable during operation | `_ref_read` / `_ref_write` will raise; caught by the outer `except` block in `process_vitals`, returns `status=error` |
| Pipeline crash mid-packet | Auto-commit means offset may already be committed; packet is not reprocessed |
