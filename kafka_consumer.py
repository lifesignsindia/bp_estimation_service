"""
kafka_consumer.py - Kafka consumer/producer for the Vitals Inference Pipeline.

Consumes device packets from the input topic, calls process_vitals(),
and forwards clinical outputs to the output topic.

Forward policy:
  success / alert      → forwarded to output topic (final 15-min clinical output)
  accumulating         → stdout only, not forwarded
  poor_signal / error  → stdout only, not forwarded
  ignored              → stdout only, not forwarded

Partition key = admissionId — guarantees all packets for a patient land on
the same partition/consumer instance so SESSION_STORAGE stays consistent.

Pleth array is stripped from the forwarded payload — it is large and only
needed at the edge for waveform display.
"""

import json
import sys
import time
import traceback
import signal as os_signal
from confluent_kafka import Consumer, Producer, KafkaException
from dotenv import load_dotenv

load_dotenv()


import config as cfg
# ─── Startup config dump ─────────────────────────────────────────────────────
print("[CFG]  ==================== PIPELINE STARTING ====================")
print("[CFG]  KAFKA_BROKERS      =", cfg.KAFKA_BROKERS)
print("[CFG]  KAFKA_INPUT_TOPIC  =", cfg.KAFKA_INPUT_TOPIC)
print("[CFG]  KAFKA_OUTPUT_TOPIC =", cfg.KAFKA_OUTPUT_TOPIC)
print("[CFG]  KAFKA_GROUP_ID     =", cfg.KAFKA_GROUP_ID)
print("[CFG]  KAFKA_DEBUG_TOPIC  =", cfg.KAFKA_DEBUG_TOPIC or "(disabled)")
print("[CFG]  REDIS_HOST         =", cfg.REDIS_HOST)
print("[CFG]  REDIS_PORT         =", cfg.REDIS_PORT)
print("[CFG]  ============================================================")
sys.stdout.flush()

from vitals_standalone import process_vitals

# ─── Forward statuses (downstream clinical output only) ──────────────────────
FORWARD_STATUSES = {"success", "alert"}


# ─── Kafka Consumer (with retry) ─────────────────────────────────────────────
consumer = None
for attempt in range(1, 6):
    try:
        print(f"[INIT] Creating Kafka consumer (attempt {attempt}/5)...")
        sys.stdout.flush()
        consumer = Consumer({
            "bootstrap.servers":    cfg.KAFKA_BROKERS,
            "group.id":             cfg.KAFKA_GROUP_ID,
            "auto.offset.reset":    "latest",
            "enable.auto.commit":   True,
            "session.timeout.ms":   30000,
            "max.poll.interval.ms": 300000,
        })
        print("[INIT] Kafka consumer created OK")
        sys.stdout.flush()
        break
    except Exception as e:
        print(f"[INIT] Consumer creation failed (attempt {attempt}): {e}")
        sys.stdout.flush()
        time.sleep(5)

if consumer is None:
    print("[INIT] FATAL: Could not create Kafka consumer after 5 attempts. Exiting.")
    sys.exit(1)

# ─── Kafka Producer (with retry) ─────────────────────────────────────────────
producer = None
for attempt in range(1, 6):
    try:
        print(f"[INIT] Creating Kafka producer (attempt {attempt}/5)...")
        sys.stdout.flush()
        producer = Producer({
            "bootstrap.servers":   cfg.KAFKA_BROKERS,
            "linger.ms":           20,
            "batch.num.messages":  500,
            "compression.type":    "snappy",
            "acks":                "1",
            "delivery.timeout.ms": 10000,
        })
        print("[INIT] Kafka producer created OK")
        sys.stdout.flush()
        break
    except Exception as e:
        print(f"[INIT] Producer creation failed (attempt {attempt}): {e}")
        sys.stdout.flush()
        time.sleep(5)

if producer is None:
    print("[INIT] FATAL: Could not create Kafka producer after 5 attempts. Exiting.")
    sys.exit(1)

_flush_counter  = 0
_FLUSH_EVERY    = 50
_msg_count      = 0
_last_heartbeat = time.time()


def _delivery_cb(err, msg):
    if err:
        print(f"[KAFKA] Delivery FAILED | topic={msg.topic()} | err={err}")
    else:
        print(f"[KAFKA] Delivery OK     | topic={msg.topic()} | partition={msg.partition()} | offset={msg.offset()}")
    sys.stdout.flush()


# ─── Debug Publisher ──────────────────────────────────────────────────────────
def _debug(event, detail="", adm_id="-", status="-"):
    if not cfg.KAFKA_DEBUG_TOPIC:
        return
    try:
        producer.produce(
            cfg.KAFKA_DEBUG_TOPIC,
            key=adm_id.encode("utf-8"),
            value=json.dumps({
                "timestamp":    int(time.time()),
                "event":        event,
                "admissionId":  adm_id,
                "status":       status,
                "detail":       detail,
                "input_topic":  cfg.KAFKA_INPUT_TOPIC,
                "output_topic": cfg.KAFKA_OUTPUT_TOPIC,
                "group_id":     cfg.KAFKA_GROUP_ID,
            }).encode("utf-8"),
        )
        producer.poll(0)
    except Exception as e:
        print(f"[DBG]  Publish failed: {e}")


# ─── Graceful shutdown ────────────────────────────────────────────────────────
_running = True


def _shutdown(signum, frame):
    global _running
    print("\n[KAFKA] Shutdown signal received. Draining and closing...")
    sys.stdout.flush()
    _running = False


os_signal.signal(os_signal.SIGINT,  _shutdown)
os_signal.signal(os_signal.SIGTERM, _shutdown)


# ─── Main Loop ────────────────────────────────────────────────────────────────
def run():
    global _flush_counter, _msg_count, _last_heartbeat

    # Subscribe with retry
    for attempt in range(1, 6):
        try:
            print(f"[KAFKA] Subscribing to '{cfg.KAFKA_INPUT_TOPIC}' (attempt {attempt}/5)...")
            sys.stdout.flush()
            consumer.subscribe([cfg.KAFKA_INPUT_TOPIC])
            print(f"[KAFKA] Subscribed → '{cfg.KAFKA_INPUT_TOPIC}' | Output → '{cfg.KAFKA_OUTPUT_TOPIC}' | Group → '{cfg.KAFKA_GROUP_ID}'")
            sys.stdout.flush()
            break
        except Exception as e:
            print(f"[KAFKA] Subscribe failed (attempt {attempt}): {e}")
            sys.stdout.flush()
            time.sleep(5)
    else:
        print("[KAFKA] FATAL: Could not subscribe after 5 attempts. Exiting.")
        sys.exit(1)

    _debug("PIPELINE_STARTED", f"Subscribed to {cfg.KAFKA_INPUT_TOPIC}, waiting for input")

    while _running:
        # Heartbeat every 30s
        now = time.time()
        if now - _last_heartbeat >= 30:
            print(f"[HEARTBEAT] alive | messages_processed={_msg_count} | listening on '{cfg.KAFKA_INPUT_TOPIC}'")
            sys.stdout.flush()
            _last_heartbeat = now

        try:
            msg = consumer.poll(timeout=1.0)
        except Exception as e:
            print(f"[KAFKA] Poll error: {e}")
            sys.stdout.flush()
            continue

        if msg is None:
            continue

        if msg.error():
            print(f"[KAFKA] Consumer error: {msg.error()}")
            sys.stdout.flush()
            _debug("CONSUMER_ERROR", str(msg.error()))
            continue

        # Raw message metadata
        raw_bytes = msg.value()
        msg_key   = msg.key().decode("utf-8") if msg.key() else "-"
        print(f"[MSG]  Received | topic={msg.topic()} | partition={msg.partition()} | offset={msg.offset()} | size={len(raw_bytes)}B | key={msg_key}")
        sys.stdout.flush()

        # Decode
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except Exception as e:
            print(f"[KAFKA] Decode error: {e} | raw_preview={raw_bytes[:120]}")
            sys.stdout.flush()
            _debug("DECODE_ERROR", str(e))
            continue

        adm_id_raw  = payload.get("admissionId", "UNKNOWN")
        device_name = payload.get("deviceName",  "UNKNOWN")
        pleth_len   = len(payload.get("pleth", {}).get("PLETH", []))
        print(f"[MSG]  Parsed  | admissionId={adm_id_raw} | device={device_name} | pleth_samples={pleth_len}")
        sys.stdout.flush()

        if pleth_len == 0:
            print(f"[WARN] Empty pleth array for admissionId={adm_id_raw} — likely poor_signal")
            sys.stdout.flush()

        # Inference
        try:
            result = process_vitals(payload)
        except Exception as e:
            print(f"[KAFKA] process_vitals error: {e}")
            traceback.print_exc()
            sys.stdout.flush()
            _debug("INFERENCE_ERROR", str(e), adm_id_raw)
            continue

        _msg_count += 1
        status = result.get("status", "unknown")
        adm_id = result.get("admissionId", adm_id_raw)
        print(f"[RESULT] status={status} | admissionId={adm_id} | total_processed={_msg_count}")
        sys.stdout.flush()

        if status in FORWARD_STATUSES:
            try:
                producer.produce(
                    cfg.KAFKA_OUTPUT_TOPIC,
                    key=adm_id.encode("utf-8"),
                    value=json.dumps(result).encode("utf-8"),
                    callback=_delivery_cb,
                )
                _flush_counter += 1
                if _flush_counter >= _FLUSH_EVERY:
                    producer.flush()
                    _flush_counter = 0
            except Exception as e:
                print(f"[KAFKA] Produce error: {e}")
                sys.stdout.flush()
            dev = result.get("deviceType", "-")
            bp  = result.get("bp", {})
            print(f"[OUT]  {status.upper():<8} | adm={adm_id} | dev={dev} | BP={bp.get('bpSystolic','-')}/{bp.get('bpDiastolic','-')} | {result.get('message','')}")
            sys.stdout.flush()
            _debug("OUTPUT_FORWARDED", f"dev={dev} BP={bp.get('bpSystolic','-')}/{bp.get('bpDiastolic','-')}", adm_id, status)

        elif status == "accumulating":
            bp      = result.get("bp", {})
            elapsed = result.get("elapsed_seconds", "-")
            target  = result.get("target_seconds",  "-")
            print(f"[ACC]  {elapsed:>4}s/{target}s | adm={adm_id} | BP={bp.get('bpSystolic','-')}/{bp.get('bpDiastolic','-')} | trending={result.get('trending', False)}")
            sys.stdout.flush()
            _debug("ACCUMULATING", f"{elapsed}/{target}s", adm_id, status)

        elif status == "poor_signal":
            sqi  = result.get("sqi", {})
            flag = sqi.get("flag", "-")
            print(f"[SIG]  POOR_SIGNAL | adm={adm_id} | dev={result.get('deviceType','-')} | flag={flag} | sqi={sqi}")
            sys.stdout.flush()
            _debug("POOR_SIGNAL", f"flag={flag}", adm_id, status)

        elif status == "ignored":
            print(f"[IGN]  IGNORED     | adm={adm_id} | {result.get('message','')}")
            sys.stdout.flush()
            _debug("IGNORED", result.get("message", ""), adm_id, status)

        elif status == "error":
            print(f"[ERR]  ERROR       | adm={adm_id} | {result.get('message','')}")
            sys.stdout.flush()
            _debug("PIPELINE_ERROR", result.get("message", ""), adm_id, status)

        else:
            print(f"[???]  {status:<12} | adm={adm_id} | {result.get('message','')}")
            sys.stdout.flush()
            _debug("UNKNOWN_STATUS", result.get("message", ""), adm_id, status)

    producer.flush()
    consumer.close()
    print("[KAFKA] Shutdown complete.")
    _debug("PIPELINE_STOPPED", "Graceful shutdown complete")


if __name__ == "__main__":
    run()
