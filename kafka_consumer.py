import json
import sys
import time
import traceback
import signal as os_signal
from confluent_kafka import Consumer, Producer
from dotenv import load_dotenv

load_dotenv()

import config as cfg

print("[CFG]  ==================== PIPELINE STARTING ====================")
print("[CFG]  KAFKA_BROKERS      =", cfg.KAFKA_BROKERS)
print("[CFG]  KAFKA_INPUT_TOPIC  =", cfg.KAFKA_INPUT_TOPIC)
print("[CFG]  KAFKA_OUTPUT_TOPIC =", cfg.KAFKA_OUTPUT_TOPIC)
print("[CFG]  KAFKA_GROUP_ID     =", cfg.KAFKA_GROUP_ID)
print("[CFG]  KAFKA_DEBUG_TOPIC  =", cfg.KAFKA_DEBUG_TOPIC or "(disabled)")
print("[CFG]  REDIS_HOST         =", cfg.REDIS_HOST)
print("[CFG]  REDIS_PORT         =", cfg.REDIS_PORT)
print("[CFG]  REDIS_URL          =", cfg.REDIS_URL or "(none)")
print("[CFG]  REDIS_TLS          =", cfg.REDIS_TLS)
print("[CFG]  ============================================================")
sys.stdout.flush()

print("[IMPORT] Loading vitals_standalone...")
sys.stdout.flush()
try:
    from vitals_standalone import process_vitals
    print("[IMPORT] vitals_standalone loaded OK")
    sys.stdout.flush()
except Exception as e:
    print(f"[IMPORT] FATAL: {e}")
    traceback.print_exc()
    sys.exit(1)

FORWARD_STATUSES = {"success", "alert"}

consumer = Consumer({
    "bootstrap.servers":    cfg.KAFKA_BROKERS,
    "group.id":             cfg.KAFKA_GROUP_ID,
    "auto.offset.reset":    "latest",
    "enable.auto.commit":   True,
    "session.timeout.ms":   30000,
    "max.poll.interval.ms": 300000,
})

producer = Producer({
    "bootstrap.servers":   cfg.KAFKA_BROKERS,
    "linger.ms":           20,
    "batch.num.messages":  500,
    "compression.type":    "lz4",
    "acks":                "1",
    "delivery.timeout.ms": 10000,
})

print("[INIT] Consumer and producer created OK")
sys.stdout.flush()

_flush_counter  = 0
_FLUSH_EVERY    = 50
_msg_count      = 0
_last_heartbeat = time.time()
_running        = True


def _delivery_cb(err, msg):
    if err:
        print(f"[KAFKA] Delivery FAILED | topic={msg.topic()} | err={err}")
    else:
        print(f"[KAFKA] Delivery OK | topic={msg.topic()} | partition={msg.partition()} | offset={msg.offset()}")
    sys.stdout.flush()


def _debug(event, detail="", adm_id="-", status="-"):
    if not cfg.KAFKA_DEBUG_TOPIC:
        return
    try:
        producer.produce(
            cfg.KAFKA_DEBUG_TOPIC,
            key=adm_id.encode(),
            value=json.dumps({
                "timestamp":    int(time.time()),
                "event":        event,
                "admissionId":  adm_id,
                "status":       status,
                "detail":       detail,
                "input_topic":  cfg.KAFKA_INPUT_TOPIC,
                "output_topic": cfg.KAFKA_OUTPUT_TOPIC,
                "group_id":     cfg.KAFKA_GROUP_ID,
            }).encode(),
        )
        producer.poll(0)
    except Exception as e:
        print(f"[DBG] Publish failed: {e}")
        sys.stdout.flush()


def _shutdown(signum, frame):
    global _running
    print("\n[KAFKA] Shutdown signal received...")
    sys.stdout.flush()
    _running = False


os_signal.signal(os_signal.SIGINT,  _shutdown)
os_signal.signal(os_signal.SIGTERM, _shutdown)


def run():
    global _flush_counter, _msg_count, _last_heartbeat

    consumer.subscribe([cfg.KAFKA_INPUT_TOPIC])
    print(f"[KAFKA] Subscribed → '{cfg.KAFKA_INPUT_TOPIC}' | Output → '{cfg.KAFKA_OUTPUT_TOPIC}' | Group → '{cfg.KAFKA_GROUP_ID}'")
    sys.stdout.flush()
    _debug("PIPELINE_STARTED", f"Subscribed to {cfg.KAFKA_INPUT_TOPIC}")

    while _running:
        now = time.time()
        if now - _last_heartbeat >= 30:
            print(f"[HEARTBEAT] alive | processed={_msg_count} | topic='{cfg.KAFKA_INPUT_TOPIC}'")
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

        raw   = msg.value()
        key   = msg.key().decode() if msg.key() else "-"
        print(f"[MSG] Received | partition={msg.partition()} | offset={msg.offset()} | size={len(raw)}B | key={key}")
        sys.stdout.flush()

        try:
            payload = json.loads(raw.decode())
        except Exception as e:
            print(f"[KAFKA] Decode error: {e} | preview={raw[:100]}")
            sys.stdout.flush()
            _debug("DECODE_ERROR", str(e))
            continue

        adm_id  = payload.get("admissionId", "UNKNOWN")
        _dev_block = payload.get("device", {})
        device  = (_dev_block.get("deviceName") if isinstance(_dev_block, dict) else _dev_block) or "UNKNOWN"
        _pleth_block = payload.get("pleth", {}) or {}
        pleth_n = len(_pleth_block.get("plethWave") or _pleth_block.get("PLETH") or [])
        print(f"[MSG] Parsed | admissionId={adm_id} | device={device} | pleth_samples={pleth_n}")
        sys.stdout.flush()

        try:
            result = process_vitals(payload)
        except Exception as e:
            print(f"[KAFKA] Inference error: {e}")
            traceback.print_exc()
            sys.stdout.flush()
            _debug("INFERENCE_ERROR", str(e), adm_id)
            continue

        _msg_count += 1
        status = result.get("status", "unknown")
        adm_id = result.get("admissionId", adm_id)
        print(f"[RESULT] status={status} | admissionId={adm_id} | total={_msg_count}")
        sys.stdout.flush()

        if status in FORWARD_STATUSES:
            try:
                out = {**result, "pleth": payload.get("pleth", {})}
                producer.produce(
                    cfg.KAFKA_OUTPUT_TOPIC,
                    key=adm_id.encode(),
                    value=json.dumps(out).encode(),
                    callback=_delivery_cb,
                )
                _flush_counter += 1
                if _flush_counter >= _FLUSH_EVERY:
                    producer.flush(timeout=5)
                    _flush_counter = 0
            except Exception as e:
                print(f"[KAFKA] Produce error: {e}")
                sys.stdout.flush()
            bp = result.get("bp", {})
            print(f"[OUT] {status.upper()} | adm={adm_id} | EBP={bp.get('estimated_sbp','-')}/{bp.get('estimated_dbp','-')}")
            sys.stdout.flush()
            _debug("OUTPUT_FORWARDED", f"EBP={bp.get('estimated_sbp','-')}/{bp.get('estimated_dbp','-')}", adm_id, status)

        elif status == "accumulating":
            bp      = result.get("bp", {})
            elapsed = result.get("elapsed_seconds", "-")
            target  = result.get("target_seconds",  "-")
            print(f"[ACC] {elapsed}s/{target}s | adm={adm_id} | EBP={bp.get('estimated_sbp','-')}/{bp.get('estimated_dbp','-')}")
            sys.stdout.flush()
            _debug("ACCUMULATING", f"{elapsed}/{target}s", adm_id, status)

        elif status == "poor_signal":
            sqi = result.get("sqi", {})
            print(f"[SIG] POOR_SIGNAL | adm={adm_id} | flag={sqi.get('flag','-')} | sqi={sqi}")
            sys.stdout.flush()
            _debug("POOR_SIGNAL", f"flag={sqi.get('flag','-')}", adm_id, status)

        elif status == "ignored":
            print(f"[IGN] IGNORED | adm={adm_id} | {result.get('message','')}")
            sys.stdout.flush()
            _debug("IGNORED", result.get("message", ""), adm_id, status)

        elif status == "error":
            print(f"[ERR] ERROR | adm={adm_id} | {result.get('message','')}")
            sys.stdout.flush()
            _debug("PIPELINE_ERROR", result.get("message", ""), adm_id, status)

        else:
            print(f"[???] {status} | adm={adm_id} | {result.get('message','')}")
            sys.stdout.flush()
            _debug("UNKNOWN_STATUS", result.get("message", ""), adm_id, status)

    print("[KAFKA] Flushing pending messages...")
    sys.stdout.flush()
    producer.flush(timeout=10)
    _debug("PIPELINE_STOPPED", "Graceful shutdown")
    producer.flush(timeout=5)
    consumer.close()
    print("[KAFKA] Shutdown complete.")
    sys.stdout.flush()


if __name__ == "__main__":
    run()
