"""Docker smoke test for the v7 pipeline against the local compose stack.

    docker compose up -d --build
    python test_pipeline/docker_smoke_v7.py

Produces to vitals.raw (localhost:9092): one cuff, then real NISO101 epochs with epochTime
advancing 180 s per message (sent quickly - the pipeline slots by epochTime). Then reads
vitals.clinical and prints every payload that came out. Expected: nothing for the first ~6
epochs, then exactly one success payload per 15-minute slot boundary crossed.
"""
import os
import sys
import json
import glob
import time
import uuid

import numpy as np
from confluent_kafka import Producer, Consumer, TopicPartition

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import bpv4_features as V   # noqa: E402

BROKER = os.getenv("KAFKA_BROKERS", "localhost:9092")
IN, OUT = "vitals.raw", "vitals.clinical"
FAC = "CF1398828720"
ADM = "ADM_DOCKER_%s" % uuid.uuid4().hex[:6]
N_EPOCHS = int(os.getenv("SMOKE_EPOCHS", "30"))


def good_epochs(n):
    picked = []
    for path in sorted(glob.glob(os.path.join(_REPO, "ebp_dashboard", "pleth_capture", "ADM*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    s = json.loads(line)["pleth"]["rawData"]
                except Exception:
                    continue
                if len(s) < 1200 or np.std(s) < 1:
                    continue
                f, q = V.features_from_epoch(np.asarray(s, float), fs_in=200.0)
                if f is None or q[0] < 10 or q[1] < 0.90:
                    continue
                if np.isnan(np.asarray(f)[[V.NAMES.index(c) for c in ("aix", "ri", "ipa", "dvp_time", "stiffness_idx")]]).any():
                    continue
                picked.append([int(v) for v in s])
                if len(picked) >= n:
                    return picked
    return picked


def main():
    eps = good_epochs(N_EPOCHS)
    assert len(eps) >= 10, "no capture epochs found"
    p = Producer({"bootstrap.servers": BROKER})
    c = Consumer({"bootstrap.servers": BROKER, "group.id": "smoke-%s" % ADM,
                  "auto.offset.reset": "latest", "enable.auto.commit": False})
    # position the reader at the END of the output topic before we produce anything
    md = c.list_topics(OUT, timeout=10).topics[OUT]
    parts = []
    for pid in md.partitions:
        lo, hi = c.get_watermark_offsets(TopicPartition(OUT, pid), timeout=10)
        parts.append(TopicPartition(OUT, pid, hi))
    c.assign(parts)

    now = time.time()
    t = now - (now % 900) + 30
    send = lambda obj: (p.produce(IN, key=ADM.encode(), value=json.dumps(obj).encode()), p.poll(0))
    send({"admissionId": ADM, "facilityId": FAC, "epochTime": int(t * 1000),
          "device": {"deviceName": "NISO206", "deviceType": "BP"},
          "bp": {"BPSYS": 120, "BPDIA": 80, "BP_ERROR": 0}})
    for i in range(N_EPOCHS):
        t += 180
        send({"admissionId": ADM, "facilityId": FAC, "patientId": "P1", "patientName": "Docker Smoke",
              "epochTime": int(t * 1000), "seqNum": i, "seqPart": 1,
              "device": {"deviceName": "NISO101", "deviceType": "BP_SPO2"},
              "spo2": {"SPO2": 98, "PR_ALL": [72] * 10},
              "pleth": {"plethWave": eps[i % len(eps)]}})
    p.flush(30)
    print("[smoke] sent 1 cuff + %d epochs for %s (%.0f min of device time)" % (N_EPOCHS, ADM, N_EPOCHS * 3))

    got, deadline = [], time.time() + 240
    while time.time() < deadline:
        m = c.poll(1.0)
        if m is None or m.error():
            if got and time.time() - got[-1][0] > 20:
                break
            continue
        d = json.loads(m.value())
        if d.get("admissionId") != ADM:
            continue
        got.append((time.time(), d))
        bp = d.get("bp", {})
        print("[out] %-7s slot=%s..%s  bp=%s/%s ref=%s/%s  n=%s conf=%s alert=%r hb=%s glu=%s" % (
            d["status"], d.get("window", {}).get("start"), d.get("window", {}).get("end"),
            bp.get("estimated_sbp"), bp.get("estimated_dbp"), bp.get("reference_sbp"), bp.get("reference_dbp"),
            d.get("reading_count"), d.get("confidence"), d.get("alert"), d.get("hemoglobin"), d.get("glucose")))
    c.close()
    slots = len({int(x) // 900 for x in np.arange(now - (now % 900) + 30 + 180 * 7, t + 1, 180)})
    print("[smoke] %d payloads on %s for ~%d slots touched; statuses=%s"
          % (len(got), OUT, slots, sorted({d["status"] for _, d in got})))
    ok = 1 <= len(got) <= slots and all(d["status"] in ("success", "alert") for _, d in got)
    print("[smoke] " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
