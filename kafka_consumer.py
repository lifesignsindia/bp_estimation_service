"""
kafka_consumer.py - Kafka consumer/producer for the Vitals Inference Pipeline.

Consumes device packets from the input topic, calls process_vitals(),
and forwards clinical outputs to the output topic.

Forward policy:
  success / alert      → forwarded (final 15-min clinical output)
  accumulating         → forwarded (intermediate BP visible downstream)
  poor_signal / error  → stdout only, not forwarded
  ignored              → stdout only, not forwarded

Partition key = admissionId — guarantees all packets for a patient land on
the same partition/consumer instance so SESSION_STORAGE stays consistent.

Pleth array is stripped from the forwarded payload — it is large and only
needed at the edge for waveform display.
"""

import json
import sys
import signal as os_signal
from confluent_kafka import Consumer, Producer, KafkaException
from dotenv import load_dotenv

load_dotenv()

import config as cfg
from vitals_standalone import process_vitals

# ─── Forward statuses ────────────────────────────────────────────────────────
FORWARD_STATUSES = {"success", "alert", "accumulating"}

# ─── Kafka Consumer ───────────────────────────────────────────────────────────
consumer = Consumer({
    "bootstrap.servers":  cfg.KAFKA_BROKERS,
    "group.id":           cfg.KAFKA_GROUP_ID,
    "auto.offset.reset":  "latest",
    "enable.auto.commit": True,
    "session.timeout.ms": 30000,
    "max.poll.interval.ms": 300000,
})

# ─── Kafka Producer ───────────────────────────────────────────────────────────
producer = Producer({
    "bootstrap.servers":        cfg.KAFKA_BROKERS,
    "linger.ms":                20,
    "batch.num.messages":       500,
    "compression.type":         "snappy",
    "acks":                     "1",
    "delivery.timeout.ms":      10000,
})

_flush_counter = 0
_FLUSH_EVERY   = 50


def _delivery_cb(err, msg):
    if err:
        print(f"[KAFKA] Delivery FAILED | topic={msg.topic()} | err={err}")


def _strip_pleth(result: dict) -> dict:
    """Remove pleth array before sending over Kafka — too large for message bus."""
    out = dict(result)
    out.pop("pleth", None)
    return out


# ─── Graceful shutdown ────────────────────────────────────────────────────────
_running = True


def _shutdown(signum, frame):
    global _running
    print("\n[KAFKA] Shutdown signal received. Draining and closing...")
    _running = False


os_signal.signal(os_signal.SIGINT,  _shutdown)
os_signal.signal(os_signal.SIGTERM, _shutdown)


# ─── Main Loop ────────────────────────────────────────────────────────────────
def run():
    global _flush_counter
    consumer.subscribe([cfg.KAFKA_INPUT_TOPIC])
    print(f"[KAFKA] Subscribed → '{cfg.KAFKA_INPUT_TOPIC}' | Output → '{cfg.KAFKA_OUTPUT_TOPIC}' | Group → '{cfg.KAFKA_GROUP_ID}'")

    while _running:
        msg = consumer.poll(timeout=1.0)

        if msg is None:
            continue

        if msg.error():
            print(f"[KAFKA] Consumer error: {msg.error()}")
            continue

        try:
            payload = json.loads(msg.value().decode("utf-8"))
        except Exception as e:
            print(f"[KAFKA] Decode error: {e}")
            continue

        try:
            result = process_vitals(payload)
        except Exception as e:
            print(f"[KAFKA] process_vitals error: {e}")
            continue

        status = result.get("status")
        adm_id = result.get("admissionId", "UNKNOWN")

        if status in FORWARD_STATUSES:
            out = _strip_pleth(result)
            producer.produce(
                cfg.KAFKA_OUTPUT_TOPIC,
                key=adm_id.encode("utf-8"),
                value=json.dumps(out).encode("utf-8"),
                callback=_delivery_cb,
            )
            _flush_counter += 1
            if _flush_counter >= _FLUSH_EVERY:
                producer.flush()
                _flush_counter = 0
            print(f"[KAFKA] Forwarded  | adm={adm_id} | status={status}")
        else:
            print(f"[KAFKA] Suppressed | adm={adm_id} | status={status} | {result.get('message', '')}")

    producer.flush()
    consumer.close()
    print("[KAFKA] Shutdown complete.")


if __name__ == "__main__":
    run()
