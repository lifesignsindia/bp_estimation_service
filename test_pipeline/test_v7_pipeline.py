"""End-to-end test of the v7 pipeline through process_vitals, with no Kafka and no Redis server.

    python test_pipeline/test_v7_pipeline.py

Redis is replaced by fakeredis BEFORE vitals_standalone is imported, so the real module-level
Redis connect succeeds against an in-memory store. Real NISO101 epochs from the dashboard's
pleth capture are used so the v7 quality gate sees genuine morphology.

Checks
  1. a cuff, then epochs every 180 s -> nothing published until a 15-min slot closes
  2. exactly one success/alert payload per closed slot, none in between
  3. flat and noisy epochs are dropped from the slot (poor_signal, not published, not counted)
  4. the published payload keeps the legacy shape (bp block, sqi, pleth, Hb/glucose, _meta)
  5. a new cuff rebuilds the anchor and clears any alert
  6. a forced +20 mmHg delta raises an alert only after two ESTABLISHED breaching slots, and
     it stays latched until the next cuff
"""
import os
import sys
import json
import glob

os.environ.setdefault("EBP_ALLOWED_FACILITY", "CF1315821527")
os.environ.setdefault("REDIS_HOST", "localhost")

import fakeredis                       # noqa: E402
import redis as _redis_mod             # noqa: E402
_redis_mod.Redis = fakeredis.FakeRedis  # patch BEFORE the pipeline connects

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import numpy as np                     # noqa: E402
import vitals_standalone as VS         # noqa: E402
import bpv4_features as V              # noqa: E402

FAC = "CF1315821527"
ADM = "ADM_TEST_V7"


def good_epochs(n_needed=40):
    """Real NISO101 epochs that pass the v7 GOOD gate, from the capture files."""
    eng = VS.v7_engine
    picked = []
    for path in sorted(glob.glob(os.path.join(_REPO, "ebp_dashboard", "pleth_capture", "ADM*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    s = r["pleth"]["rawData"]
                except Exception:
                    continue
                if len(s) < 1200 or np.std(s) < 1:
                    continue
                f, q = V.features_from_epoch(np.asarray(s, float), fs_in=200.0)
                if f is None:
                    continue
                fv = np.asarray(f, float)[eng._cols]
                if q[0] >= 10 and q[1] >= 0.90 and not np.isnan(fv[eng._core_idx]).any():
                    picked.append([int(v) for v in s])
                    if len(picked) >= n_needed:
                        return picked
    return picked


def cuff(sbp, dbp, ts):
    return {"admissionId": ADM, "facilityId": FAC, "epochTime": int(ts * 1000),
            "device": {"deviceName": "NISO206", "deviceType": "BP"},
            "bp": {"BPSYS": sbp, "BPDIA": dbp, "BP_ERROR": 0}}


def epoch(samples, ts):
    return {"admissionId": ADM, "facilityId": FAC, "patientId": "P1", "patientName": "Test",
            "epochTime": int(ts * 1000), "seqNum": 1, "seqPart": 1,
            "device": {"deviceName": "NISO101", "deviceType": "BP_SPO2"},
            "spo2": {"SPO2": 98, "PR_ALL": [72] * 10},
            "pleth": {"plethWave": list(samples)}}


def run():
    fails = []

    def check(cond, msg):
        print(("  PASS " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    eps = good_epochs(40)
    print(f"[setup] {len(eps)} GOOD real epochs available")
    assert len(eps) >= 24, "need real NISO101 capture files under ebp_dashboard/pleth_capture"

    VS.v7_engine.reset(ADM)
    VS._redis.flushall()
    # epochTime must be within a day of the wall clock or the pipeline falls back to time.time()
    import time as _time
    _now = _time.time()
    t0 = _now - (_now % 900) + 30                            # 30 s into the current slot
    t = t0
    published, statuses = [], []

    # ---- 1. cuff, then epochs every 180 s -------------------------------------------------
    r = VS.process_vitals(cuff(120, 80, t))
    check(r["status"] == "ignored", "cuff packet is ignored (stored as reference)")
    for i in range(30):                                        # 90 minutes of epochs
        t += 180
        r = VS.process_vitals(epoch(eps[i % len(eps)], t))
        statuses.append(r["status"])
        if r["status"] in ("success", "alert"):
            published.append(r)
    print("  statuses:", statuses)
    check(statuses[:6].count("accumulating") == 6 and VS.v7_engine.load_state(ADM)["anchor_f"] is not None,
          "first 6 good epochs build the anchor and publish nothing")
    n_slots = len({int(s) // 900 for s in np.arange(t0 + 180 * 7, t + 1, 180)})
    check(1 <= len(published) <= n_slots, f"published {len(published)} payloads for ~{n_slots} slots touched (one per closed slot)")
    check(all(st in ("accumulating", "success", "alert", "poor_signal") for st in statuses),
          "no unexpected statuses")

    # ---- 4. payload shape -------------------------------------------------------------------
    p = published[0]
    for k in ("status", "admissionId", "deviceName", "deviceType", "timestamp", "reading_count",
              "bp", "sqi", "trending", "morphology_change", "pleth", "message", "patientId", "patientName"):
        check(k in p, f"payload has '{k}'")
    for k in ("estimated_sbp", "estimated_dbp", "category", "trend", "BP_ERROR", "reference_sbp", "reference_dbp"):
        check(k in p["bp"], f"bp block has '{k}'")
    check(p["deviceName"] == "NISO101" and p["pleth"].get("PLETH"), "deviceName mapped and pleth echoed")
    check(p["bp"]["reference_sbp"] == 120 and p["bp"]["reference_dbp"] == 80, "reference in payload is the live cuff")
    check(abs(p["bp"]["estimated_sbp"] - 120) <= 25 and abs(p["bp"]["estimated_dbp"] - 80) <= 25, "estimate is cuff +- cap")
    check(("hemoglobin" in p) and ("glucose" in p), "Hb and glucose came from the legacy engine")
    print("  sample payload:", json.dumps({k: v for k, v in p.items() if k != "pleth"})[:600])

    # ---- 3. flat and noisy epochs are dropped ---------------------------------------------
    st_before = VS.v7_engine.load_state(ADM)
    n_good_before = len(st_before["win"])
    t += 180
    r_flat = VS.process_vitals(epoch([2048] * 3600, t))
    t += 180
    rng = np.random.default_rng(0)
    r_noise = VS.process_vitals(epoch((2048 + 300 * rng.standard_normal(3600)).astype(int), t))
    st_after = VS.v7_engine.load_state(ADM)
    check(r_flat["status"] == "poor_signal", f"flat epoch -> poor_signal ({r_flat['message'][:60]})")
    check(r_noise["status"] == "poor_signal", f"noise epoch -> poor_signal (q={r_noise['sqi'].get('v7_quality')})")
    check(len(st_after["win"]) == n_good_before or st_after["win_key"] != st_before["win_key"],
          "dropped epochs did not enter the slot")

    # ---- 6. forced alert: +20 mmHg delta from a fresh cuff --------------------------------
    class _Plus:
        def __init__(self, v): self.v = v
        def predict(self, X): return np.full(len(X), self.v)
    real_ms, real_md = VS.v7_engine._ms, VS.v7_engine._md
    VS.v7_engine._ms, VS.v7_engine._md = _Plus(20.0), _Plus(2.0)
    try:
        t += 180
        VS.process_vitals(cuff(110, 70, t))                    # new cuff -> anchor rebuild
        st = VS.v7_engine.load_state(ADM)
        # the reference is only seen by v7 on the next pleth epoch
        seq = []
        for i in range(40):                                    # 2 hours
            t += 180
            r = VS.process_vitals(epoch(eps[(i + 7) % len(eps)], t))
            seq.append((r["status"], r.get("alert", ""), r.get("confidence", "")))
        st = VS.v7_engine.load_state(ADM)
        check(st["anchor_s"] == 110.0 and st["anchor_f"] is not None, "new cuff rebuilt the anchor at 110/70")
        pubs = [s for s in seq if s[0] in ("success", "alert")]
        print("  published after new cuff:", pubs)
        first_alert = next((i for i, s in enumerate(pubs) if s[0] == "alert"), None)
        check(first_alert is not None, "a sustained +20 mmHg delta raises an alert")
        # prototype rule: the alert needs run >= 2 (established) AND two consecutive breaching
        # slots, so the earliest it can fire is the SECOND counted slot; the first is LOW, no alert
        check(first_alert == 1 and pubs[0][0] == "success" and pubs[0][2] == "LOW",
              "first slot LOW with no alert; alert fires on the second consecutive breaching slot")
        check(first_alert is not None and all(s[0] == "alert" for s in pubs[first_alert:]),
              "alert stays latched on every following slot")
        # ---- 5. a new cuff clears the latch ---------------------------------------------
        t += 180
        VS.process_vitals(cuff(112, 72, t))
        t += 180
        r = VS.process_vitals(epoch(eps[0], t))
        st = VS.v7_engine.load_state(ADM)
        check(st["alert"] == "" and st["anchor_f"] is None and r["status"] == "accumulating",
              "new cuff clears the alert and starts re-calibration")
    finally:
        VS.v7_engine._ms, VS.v7_engine._md = real_ms, real_md

    print("\n%d checks failed" % len(fails) if fails else "\nALL CHECKS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
