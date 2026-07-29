"""
PlethGuard — SHADOW service for NISO206 SpO2.

WHAT IT IS
  A read-only "guard" that runs ALONGSIDE the BP-estimation pipeline. It receives the SAME input
  the pipeline receives (Kafka topic `vitals.raw`), but:
    * processes ONLY NISO206 packets (every other device is ignored),
    * decides DISPLAY vs SUPPRESS for the SpO2 (false-alarm suppression + apnea screen),
    * writes its verdict to its OWN sink (a local JSONL file + optional separate topic),
    * NEVER produces to `vitals.clinical` and NEVER changes the main pipeline's offsets/output.

  It uses a SEPARATE consumer group ('plethguard-shadow'), so it reads its own copy of the stream
  independently. The BP pipeline is completely unaffected — for NISO206 the pipeline already emits
  nothing; PlethGuard just adds an informational "alert suppressed / display" verdict on the side.

USAGE
  python plethguard/plethguard_service.py --kafka                 # live shadow off vitals.raw
  python plethguard/plethguard_service.py --file packet.json      # one payload (local test)
  python plethguard/plethguard_service.py --folder ADM878207965   # replay saved per-hour jsons
Env: KAFKA_BROKERS, KAFKA_INPUT_TOPIC (default vitals.raw), PLETHGUARD_TOPIC (optional),
     PLETHGUARD_OUT (verdict jsonl, default plethguard/plethguard_verdicts.jsonl)
"""
import os, sys, json, glob, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import plethguard_core as ph                          # self-contained core (bundled in this package)

INPUT_TOPIC = os.getenv("KAFKA_INPUT_TOPIC", "vitals.raw")
BROKERS     = os.getenv("KAFKA_BROKERS", "localhost:9092")
GROUP_ID    = os.getenv("PLETHGUARD_GROUP", "plethguard-shadow")     # SEPARATE group -> isolated
VERDICT_TOPIC = os.getenv("PLETHGUARD_TOPIC", "")                    # optional separate output topic
OUT_FILE    = os.getenv("PLETHGUARD_OUT", os.path.join(HERE, "plethguard_verdicts.jsonl"))

# ── OPTIONAL MONGO SINK — writes each verdict to a NEW, DEDICATED collection ───────────────
# Isolated from the pipeline: its own collection, never touches existing collections or
# vitals.clinical. Enable by setting PLETHGUARD_MONGO (empty = disabled -> file/stdout only).
MONGO_URI  = os.getenv("PLETHGUARD_MONGO", "")
MONGO_DB   = os.getenv("PLETHGUARD_MONGO_DB", "local")
MONGO_COLL = os.getenv("PLETHGUARD_MONGO_COLL", "plethguard_spo2_verdicts")
_mongo = [None]

def _mongo_coll():
    if not MONGO_URI:
        return None
    if _mongo[0] is None:
        from pymongo import MongoClient
        _mongo[0] = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000,
                                socketTimeoutMS=60000)[MONGO_DB][MONGO_COLL]
    return _mongo[0]

SHOWN = {"ok", "ok_real_low", "ok_confirmed_low", "ok_severe_alarm"}

# FACILITY GATE — guard ONLY these facility IDs. Defaults to the two activated facilities so the
# guard is scoped even if the env var is unset; override with PLETHGUARD_FACILITIES="CF..,CF..".
_DEFAULT_FAC = "CF792698673,CF1398828720"
_ALLOWED_FAC = {f.strip() for f in os.getenv("PLETHGUARD_FACILITIES", _DEFAULT_FAC).split(",") if f.strip()}


def _facility(payload):
    return (payload.get("facilityId") or payload.get("facilityID")
            or (payload.get("device") or {}).get("facilityId") or "")


def facility_allowed(payload):
    return (not _ALLOWED_FAC) or (_facility(payload) in _ALLOWED_FAC)


def is_niso206(payload):
    dev = (payload.get("deviceName") or (payload.get("device") or {}).get("deviceType")
           or payload.get("deviceType") or payload.get("device") or "")
    return "206" in str(dev).upper()


def _normalize(payload):
    """Accept both the live lowercase format and the saved UPPERCASE per-hour format, in place."""
    sp = payload.get("spo2")
    if isinstance(sp, dict):
        pairs = [("spo2", "SPO2"), ("pulseRate", "PR"), ("spo2Error", "SPO2_ERROR"),
                 ("spo2ErrorMsg", "SPO2_ERROR_MSG"), ("cycleDuration", "SPO2_CYCLE_DUR")]
        for lo, up in pairs:
            if sp.get(lo) is None and sp.get(up) is not None:
                sp[lo] = sp.get(up)
    pl = payload.get("pleth")
    if isinstance(pl, dict) and not pl.get("plethWave"):
        for k in ("PLETH", "rawData", "plethwave", "pleth_wave"):
            if pl.get(k):
                pl["plethWave"] = pl[k]; break


def guard(payload):
    """Run PlethGuard on a NISO206 payload. Returns a compact verdict dict, or None if not 206.
    Does NOT touch the main pipeline — only reads the payload and returns its own verdict."""
    if not is_niso206(payload) or not facility_allowed(payload):
        return None                                     # not a 206, or not an allowed facility -> ignore
    _normalize(payload)
    ph.handle_niso206_spo2(payload)                     # stamps spo2 with the guard decision
    sp = payload.get("spo2") or {}
    display = bool(sp.get("displaySpo2", True))
    return {
        "admissionId": payload.get("admissionId"),
        "epochTime": payload.get("epochTime") or (payload.get("utcTimestamp", 0) // 1000 if isinstance(payload.get("utcTimestamp"), int) else None),
        "deviceName": payload.get("deviceName"),
        "spo2": sp.get("spo2"),
        "decision": "DISPLAY" if display else "SUPPRESS",
        "spo2Status": sp.get("spo2Status"),
        "reason": sp.get("spo2Reason"),
        "heldSpo2": sp.get("heldSpo2"),
        "alert": sp.get("spo2Alert"),
        "advice": sp.get("spo2Advice"),
        "pattern": sp.get("spo2Pattern"),
        "sleepApnea": sp.get("sleepApnea"),
    }


def _emit(v, producer=None):
    """Write the verdict to the guard's own sinks only (Mongo collection + file + stdout + topic).
    NEVER writes to the main pipeline output."""
    import time
    v = dict(v); v["source"] = "plethguard-shadow"; v["processedAt"] = time.time()
    line = json.dumps(v)
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    coll = _mongo_coll()
    if coll is not None:
        try:
            coll.insert_one(dict(v))                    # -> NEW dedicated collection only
        except Exception as e:
            print("[GUARD] mongo insert failed: %s" % str(e)[:60])
    tag = "ALERT SUPPRESSED" if v["decision"] == "SUPPRESS" else "display"
    extra = v.get("reason") or v.get("alert") or v.get("pattern") or ""
    print("[GUARD] adm=%s spo2=%s -> %-16s status=%s %s"
          % (v["admissionId"], v["spo2"], tag, v["spo2Status"], ("| " + str(extra)[:60]) if extra else ""))
    if producer is not None and VERDICT_TOPIC:
        producer.produce(VERDICT_TOPIC, key=str(v["admissionId"]).encode(), value=line.encode())
        producer.poll(0)


def run_kafka():
    from confluent_kafka import Consumer, Producer
    consumer = Consumer({"bootstrap.servers": BROKERS, "group.id": GROUP_ID,
                         "auto.offset.reset": "latest", "enable.auto.commit": True})
    producer = Producer({"bootstrap.servers": BROKERS}) if VERDICT_TOPIC else None
    consumer.subscribe([INPUT_TOPIC])
    print("[PLETHGUARD] SHADOW on '%s' group='%s' -> verdicts: %s %s | NEVER writes vitals.clinical"
          % (INPUT_TOPIC, GROUP_ID, OUT_FILE, ("+topic " + VERDICT_TOPIC) if VERDICT_TOPIC else ""))
    n206 = 0
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                payload = json.loads(msg.value().decode())
            except Exception:
                continue
            v = guard(payload)                          # None for non-206 -> ignored
            if v is not None:
                n206 += 1
                _emit(v, producer)
    except KeyboardInterrupt:
        print("\n[PLETHGUARD] stopped | 206 packets guarded: %d" % n206)
    finally:
        consumer.close()
        if producer:
            producer.flush(5)


def run_payloads(payloads):
    """Local test: run a list of payloads (in time order) through the guard."""
    open(OUT_FILE, "w").close()                         # fresh file for a local run
    n = 0
    for p in payloads:
        v = guard(p)
        if v is not None:
            n += 1; _emit(v)
    print("\n[PLETHGUARD] guarded %d NISO206 packets -> %s" % (n, OUT_FILE))


def _load_folder(folder):
    """Replay saved per-hour SPO2_UNFILTERED jsons (dedup by epochTime, time-ordered)."""
    seen = {}
    for f in sorted(glob.glob(os.path.join(folder, "*", "*SPO2*")) + glob.glob(os.path.join(folder, "*SPO2*"))):
        d = json.load(open(f, encoding="utf-8")); d = d[0] if isinstance(d, list) else d
        ts = d.get("epochTime") or (d.get("utcTimestamp", 0) // 1000)
        d.setdefault("deviceName", "NISO206")           # these folders are the 206 device
        seen.setdefault(ts, d)
    return [seen[k] for k in sorted(seen)]


def main():
    a = sys.argv[1:]
    if "--kafka" in a:
        run_kafka()
    elif "--file" in a:
        p = json.load(open(a[a.index("--file") + 1], encoding="utf-8"))
        run_payloads(p if isinstance(p, list) else [p])
    elif "--folder" in a:
        run_payloads(_load_folder(a[a.index("--folder") + 1]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
