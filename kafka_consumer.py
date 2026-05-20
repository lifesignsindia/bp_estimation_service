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
import signal as os_signal
from confluent_kafka import Consumer, Producer, KafkaException
from dotenv import load_dotenv

load_dotenv()

import config as cfg
from vitals_standalone import process_vitals

# ─── Forward statuses (downstream clinical output only) ──────────────────────
FORWARD_STATUSES = {"success", "alert"}

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


# ─── Debug Publisher (only active if KAFKA_DEBUG_TOPIC is set) ────────────────
def _debug(event, detail="", adm_id="-", status="-"):
    if not cfg.KAFKA_DEBUG_TOPIC:
        return
    msg = json.dumps({
        "timestamp": int(time.time()),
        "event":     event,
        "admissionId": adm_id,
        "status":    status,
        "detail":    detail,
        "input_topic":  cfg.KAFKA_INPUT_TOPIC,
        "output_topic": cfg.KAFKA_OUTPUT_TOPIC,
        "group_id":     cfg.KAFKA_GROUP_ID,
    })
    try:
        producer.produce(
            cfg.KAFKA_DEBUG_TOPIC,
            key=adm_id.encode("utf-8"),
            value=msg.encode("utf-8"),
        )
        producer.poll(0)
    except Exception as e:
        print(f"[DBG]  Publish failed: {e}")



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
    _debug("PIPELINE_STARTED", f"Subscribed to {cfg.KAFKA_INPUT_TOPIC}, waiting for input")

    while _running:
        msg = consumer.poll(timeout=1.0)

        if msg is None:
            continue

        if msg.error():
            print(f"[KAFKA] Consumer error: {msg.error()}")
            _debug("CONSUMER_ERROR", str(msg.error()))
            continue

        try:
            payload = json.loads(msg.value().decode("utf-8"))
        except Exception as e:
            print(f"[KAFKA] Decode error: {e}")
            _debug("DECODE_ERROR", str(e))
            continue

        try:
            result = process_vitals(payload)
        except Exception as e:
            print(f"[KAFKA] process_vitals error: {e}")
            _debug("INFERENCE_ERROR", str(e))
            continue

        status = result.get("status")
        adm_id = result.get("admissionId", "UNKNOWN")

        if status in FORWARD_STATUSES:
            out = result
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
            dev  = result.get("deviceType", "-")
            bp   = result.get("bp", {})
            sbp  = bp.get("bpSystolic",  "-")
            dbp  = bp.get("bpDiastolic", "-")
            print(f"[OUT]  {status.upper():<8} | adm={adm_id} | dev={dev} | BP={sbp}/{dbp} | {result.get('message','')}")
            _debug("OUTPUT_FORWARDED", f"dev={dev} BP={sbp}/{dbp}", adm_id, status)

        elif status == "accumulating":
            bp      = result.get("bp", {})
            elapsed = result.get("elapsed_seconds", "-")
            target  = result.get("target_seconds",  "-")
            print(f"[ACC]  {elapsed:>4}s/{target}s | adm={adm_id} | BP={bp.get('bpSystolic','-')}/{bp.get('bpDiastolic','-')} | trending={result.get('trending',False)}")
            _debug("ACCUMULATING", f"{elapsed}/{target}s", adm_id, status)

        elif status == "poor_signal":
            sqi  = result.get("sqi", {})
            flag = sqi.get("flag", "-")
            print(f"[SIG]  POOR_SIGNAL  | adm={adm_id} | dev={result.get('deviceType','-')} | flag={flag}")
            _debug("POOR_SIGNAL", f"flag={flag}", adm_id, status)

        elif status == "ignored":
            print(f"[IGN]  IGNORED      | adm={adm_id} | {result.get('message','')}")
            _debug("IGNORED", result.get("message", ""), adm_id, status)

        elif status == "error":
            print(f"[ERR]  ERROR        | adm={adm_id} | {result.get('message','')}")
            _debug("PIPELINE_ERROR", result.get("message", ""), adm_id, status)

        else:
            print(f"[???]  {status:<12} | adm={adm_id} | {result.get('message','')}")
            _debug("UNKNOWN_STATUS", result.get("message", ""), adm_id, status)

    producer.flush()
    consumer.close()
    print("[KAFKA] Shutdown complete.")
    _debug("PIPELINE_STOPPED", "Graceful shutdown complete")


if __name__ == "__main__":
    run()
