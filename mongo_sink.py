"""Mirror the Kafka output payload into a separate MongoDB database.

WHY A SEPARATE MODULE
    The pipeline's only job is to publish to Kafka. This sink is an ADDITIONAL, best-effort
    copy of exactly what was published, written to its own database so nothing it does can
    touch `local.estimated_bp_unfiltered_data` or any other production collection.

SAFETY RULES THIS FILE OBEYS
    * OFF unless MONGO_SINK_ENABLED=1. Import alone changes nothing.
    * Never raises into the caller. A Mongo outage, a slow write or a bad document must never
      stop a Kafka publish. Every failure is swallowed and counted, and logged once per minute.
    * Never blocks for long: short server-selection and socket timeouts.
    * Writes to its own database (MONGO_SINK_DB, default `ebp_shadow`), never to `local`.
    * Fire-and-forget writes (w=0) so the pipeline is not waiting on disk.

CONFIG (environment)
    MONGO_SINK_ENABLED    "1" to turn the sink on                     (default off)
    MONGO_SINK_URI        connection string                           (default localhost:27018 tunnel)
    MONGO_SINK_DB         database name                               (default ebp_shadow)
    MONGO_SINK_COLLECTION collection name                             (default kafka_output)
    MONGO_SINK_DROP_PLETH "1" to strip the pleth array before writing (default 0 = keep it)
"""
import os
import sys
import time

_ENABLED   = os.getenv("MONGO_SINK_ENABLED", "0") == "1"
_URI       = os.getenv("MONGO_SINK_URI", "mongodb://localhost:27018/?directConnection=true")
_DB        = os.getenv("MONGO_SINK_DB", "ebp_shadow")
_COLL      = os.getenv("MONGO_SINK_COLLECTION", "kafka_output")
_DROP_PLETH = os.getenv("MONGO_SINK_DROP_PLETH", "0") == "1"

_col = None
_init_done = False
_ok = 0
_fail = 0
_last_log = 0.0


def _log(msg):
    print(f"[MSINK] {msg}")
    sys.stdout.flush()


def _init():
    """Connect once, lazily. Any failure disables the sink for this process."""
    global _col, _init_done
    _init_done = True
    if not _ENABLED:
        return
    try:
        from pymongo import MongoClient
        client = MongoClient(
            _URI,
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=3000,
            w=0,                     # fire and forget - do not wait for an ack
        )
        client.admin.command("ping")
        _col = client[_DB][_COLL]
        _log(f"enabled -> {_DB}.{_COLL}")
    except Exception as e:
        _col = None
        _log(f"DISABLED - could not connect: {type(e).__name__}: {str(e)[:160]}")


def write(payload, status=None, adm_id=None, topic=None):
    """Best-effort copy of one published payload. Never raises, never blocks meaningfully."""
    global _ok, _fail, _last_log
    if not _ENABLED:
        return False
    if not _init_done:
        _init()
    if _col is None:
        return False
    try:
        doc = dict(payload)
        if _DROP_PLETH:
            doc.pop("pleth", None)
        doc["_sink"] = {
            "written_at": time.time(),
            "status": status if status is not None else doc.get("status"),
            "admissionId": adm_id if adm_id is not None else doc.get("admissionId"),
            "kafka_topic": topic,
        }
        _col.insert_one(doc)
        _ok += 1
    except Exception as e:
        _fail += 1
        now = time.time()
        if now - _last_log > 60:      # at most one line a minute, whatever goes wrong
            _last_log = now
            _log(f"write failed ({_fail} total): {type(e).__name__}: {str(e)[:160]}")
        return False
    return True


def stats():
    return {"enabled": _ENABLED, "connected": _col is not None, "written": _ok, "failed": _fail,
            "db": _DB, "collection": _COLL}
