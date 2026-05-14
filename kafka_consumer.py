"""
kafka_consumer.py - Kafka consumer/producer for the Vitals Inference Pipeline.

Consumes device packets from the input topic, calls process_vitals(),
and forwards only clinical outputs (success / alert) to the output topic.
All other statuses (accumulating, poor_signal, ignored, error) are logged
to stdout only and not forwarded downstream.

Partition key = admissionId on both input and output topics.
This guarantees all packets for a patient land on the same partition and
consumer instance, keeping SESSION_STORAGE consistent in memory.
"""

import json
import sys
import signal as os_signal
from confluent_kafka import Consumer, Producer, KafkaException

import config as cfg
from vitals_standalone import process_vitals

# ─── Kafka Consumer ───────────────────────────────────────────────────────────
consumer = Consumer({
    "bootstrap.servers":  cfg.KAFKA_BROKERS,
    "group.id":           "vitals-pipeline",
    "auto.offset.reset":  "latest",
    "enable.auto.commit": True,
})

# ─── Kafka Producer ───────────────────────────────────────────────────────────
producer = Producer({
    "bootstrap.servers": cfg.KAFKA_BROKERS,
})

# ─── Statuses that go downstream to the clinical output topic ─────────────────
FORWARD_STATUSES = {"success", "alert"}

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
    consumer.subscribe([cfg.KAFKA_INPUT_TOPIC])
    print(f"[KAFKA] Subscribed to '{cfg.KAFKA_INPUT_TOPIC}'. Forwarding to '{cfg.KAFKA_OUTPUT_TOPIC}'.")

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
            print(f"[KAFKA] Failed to decode message: {e}")
            continue

        try:
            result = process_vitals(payload)
        except Exception as e:
            print(f"[KAFKA] process_vitals exception: {e}")
            continue

        status = result.get("status")

        if status in FORWARD_STATUSES:
            adm_id = result.get("admissionId", "UNKNOWN")
            producer.produce(
                cfg.KAFKA_OUTPUT_TOPIC,
                key=adm_id.encode("utf-8"),
                value=json.dumps(result).encode("utf-8"),
            )
            producer.flush()
            print(f"[KAFKA] Forwarded | adm={adm_id} | status={status}")
        else:
            print(f"[KAFKA] Suppressed | status={status} | msg={result.get('message', '')}")

    consumer.close()
    print("[KAFKA] Consumer closed.")


if __name__ == "__main__":
    run()
