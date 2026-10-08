"""End-to-end test of the v7 pipeline through process_vitals, with no Kafka and no Redis server.

    python test_pipeline/test_v7_pipeline.py

Redis is replaced by fakeredis BEFORE vitals_standalone is imported, so the real module-level
Redis connect succeeds against an in-memory store. Real NISO101 epochs from the dashboard's
pleth capture are used so the v7 quality gate sees genuine morphology.

Checks
  1. a cuff, then epochs every 180 s -> no model value published until the anchor is built
  1b. EARLY first value: once the anchor is built and the open slot has 2 good scored epochs, ONE
      value is published at once (window.early=true, LOW, no alert), before the slot ends; the
      slot still publishes its own value at its end, and run / alert counting is unchanged
  2. exactly one success/alert payload per closed slot, none in between
  7. CALIBRATING: from the first epoch after a cuff, the cuff itself is published (confidence
     CALIBRATING) at most once per slot, and never again once the first 15-min value is out;
     a new cuff restarts it with the new value; V7_CAL_PUBLISH=0 switches it off
  8. display: success/alert carry display=true and bp.estimated_sbp/dbp; every other status
     (accumulating, poor_signal, ignored, error) carries display=false and its BP only as
     bp.Estimated_sbp/Estimated_dbp, so the backend can store it without showing it
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
import v7_engine as E7                 # noqa: E402

CAL_DEFAULT = E7.CAL_PUBLISH           # what the image ships with (V7_CAL_PUBLISH unset -> off)
E7.CAL_PUBLISH = True                  # sections 1-7 exercise CALIBRATING; 7b checks it switched off

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
    if "V7_CAL_PUBLISH" not in os.environ:
        check(CAL_DEFAULT is False, "CALIBRATING is OFF by default (V7_CAL_PUBLISH unset)")
    assert len(eps) >= 24, "need real NISO101 capture files under ebp_dashboard/pleth_capture"

    VS.v7_engine.reset(ADM)
    VS._redis.flushall()
    # epochTime must be within a day of the wall clock or the pipeline falls back to time.time()
    import time as _time
    _now = _time.time()
    t0 = _now - (_now % 900) + 30                            # 30 s into the current slot
    t = t0
    published, statuses, cals, allr = [], [], [], []
    first_real_t = None

    # ---- 1. cuff, then epochs every 180 s -------------------------------------------------
    r = VS.process_vitals(cuff(120, 80, t))
    check(r["status"] == "ignored", "cuff packet is ignored (stored as reference)")
    allr.append(r)
    for i in range(30):                                        # 90 minutes of epochs
        t += 180
        r = VS.process_vitals(epoch(eps[i % len(eps)], t))
        statuses.append(r.get("confidence") or r["status"])
        allr.append(r)
        if r.get("confidence") == "CALIBRATING":
            cals.append((t, r))
        elif r["status"] in ("success", "alert"):
            published.append(r)
            if first_real_t is None:
                first_real_t = t
    print("  statuses:", statuses)
    check(all(st in ("accumulating", "CALIBRATING") for st in statuses[:6])
          and VS.v7_engine.load_state(ADM)["anchor_f"] is not None,
          "first 6 good epochs build the anchor and publish no model value")

    # ---- 7. CALIBRATING = the cuff, once per slot, until the first 15-min value -----------
    c0 = cals[0][1] if cals else {}
    check(statuses[0] == "CALIBRATING" and c0.get("status") == "success",
          "the FIRST epoch after the cuff publishes a CALIBRATING success payload")
    check(c0.get("bp", {}).get("estimated_sbp") == 120 and c0.get("bp", {}).get("estimated_dbp") == 80
          and c0["bp"].get("reference_sbp") == 120 and c0.get("alert") == "" and c0.get("reading_count") == 0,
          "CALIBRATING value is the cuff 120/80, no alert, reading_count 0")
    cal_slots = [int(tt) // 900 for tt, _ in cals]
    check(len(cal_slots) == len(set(cal_slots)), f"at most one CALIBRATING payload per slot ({len(cals)} in {len(set(cal_slots))} slots)")
    check(all(tt < first_real_t for tt, _ in cals), "no CALIBRATING payload after the first 15-min value")
    check(published and published[0]["confidence"] == "LOW", "the first model value after CALIBRATING is LOW")
    for k in ("status", "admissionId", "deviceName", "deviceType", "timestamp", "reading_count", "confidence",
              "bp", "alert", "sqi", "trending", "morphology_change", "window", "pleth", "message", "patientId"):
        check(k in c0, f"CALIBRATING payload has '{k}'")
    n_slots = len({int(s) // 900 for s in np.arange(t0 + 180 * 7, t + 1, 180)})
    early = [x for x in published if x.get("window", {}).get("early")]
    slots_pub = [x for x in published if not x.get("window", {}).get("early")]
    check(len(early) == 1 and published[0] is early[0], "exactly one EARLY value per cuff, and it is the first value")
    e0 = early[0] if early else {}
    check(e0.get("confidence") == "LOW" and e0.get("alert") == "" and e0.get("window", {}).get("good_epochs") == 2
          and "early" in e0.get("message", ""), "early value: LOW, no alert, from exactly 2 good epochs")
    check(bool(slots_pub) and slots_pub[0]["window"]["start"] == e0["window"]["start"]
          and slots_pub[0]["confidence"] == "LOW" and slots_pub[0]["window"]["good_epochs"] >= 2,
          "the same slot still publishes its full value at its end, still LOW (early did not count as a slot)")
    check(len(slots_pub) >= 2 and slots_pub[1]["confidence"] == "HIGH", "the next slot is HIGH, as before")
    check(1 <= len(slots_pub) <= n_slots, f"published {len(slots_pub)} slot payloads for ~{n_slots} slots touched (one per closed slot)")
    check(all(st in ("accumulating", "CALIBRATING", "LOW", "HIGH", "poor_signal") for st in statuses),
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

    # ---- 9. NISO101 120 Hz variant (LEPU-made, fw "RI…", 2160 samples per 18 s) -----------
    from scipy import signal as _sig
    lepu = [int(v) for v in _sig.resample(np.asarray(eps[2], float), 2160)]      # same 18 s at 120 Hz
    for fw, label in (("RI0.0.3e", "fw RI"), ("Unknown", "fw Unknown, 2160 samples")):
        pk = epoch(lepu, t + 1)
        pk["admissionId"] = "ADM_TEST_LEPU"
        pk["device"] = {"deviceName": "NISO101", "deviceType": "NISO101", "fwVersion": fw}
        rq = VS.process_vitals(pk)
        check((rq.get("sqi") or {}).get("v7_quality") == "GOOD",
              f"120 Hz NISO101 ({label}) is read at 120 Hz and passes v7 (q={(rq.get('sqi') or {}).get('v7_quality')})")
    # a cut-short BerryMed (200 Hz) packet that happens to be 2160 long stays at 200 Hz
    cut = list(eps[3][:2160])
    pk = epoch(cut, t + 2); pk["admissionId"] = "ADM_TEST_BERRY_CUT"
    pk["device"] = {"deviceName": "NISO101", "deviceType": "NISO101", "fwVersion": "Unknown", "macAddress": "00:A0:50:39:7A:02"}
    rq = VS.process_vitals(pk)
    check((rq.get("sqi") or {}).get("v7_quality") == "GOOD",
          f"cut-short BerryMed packet of 2160 samples is still read at 200 Hz (q={(rq.get('sqi') or {}).get('v7_quality')})")

    # ---- 8. display flag + Estimated_* on everything that must not be shown ----------------
    allr += [r_flat, r_noise]
    check(all(isinstance(x.get("display"), bool) for x in allr), "every result carries a display flag")
    check(all(x["display"] == (x["status"] in ("success", "alert")) for x in allr),
          "display=true exactly for success/alert (CALIBRATING included), false for everything else")
    shown = [x for x in allr if x["display"]]
    hidden = [x for x in allr if not x["display"]]
    check(all("estimated_sbp" in x["bp"] and "Estimated_sbp" not in x["bp"] for x in shown),
          "displayed payloads keep bp.estimated_sbp/dbp")
    check(not any("estimated_sbp" in (x.get("bp") or {}) or "estimated_dbp" in (x.get("bp") or {}) for x in hidden),
          "no hidden payload carries bp.estimated_sbp/dbp")
    acc_vals = [x for x in hidden if x["status"] == "accumulating" and (x.get("bp") or {}).get("Estimated_sbp") is not None]
    check(len(acc_vals) > 0 and all(isinstance(x["bp"]["Estimated_dbp"], (int, float)) for x in acc_vals),
          f"per-epoch accumulating values arrive as bp.Estimated_sbp/dbp ({len(acc_vals)} seen)")
    check({"ignored", "poor_signal", "accumulating"} <= {x["status"] for x in hidden},
          "ignored, poor_signal and accumulating results are all tagged display=false")
    print("  sample hidden payload:", json.dumps({k: v for k, v in acc_vals[0].items() if k not in ("pleth", "spo2")})[:400])

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
        first_after = None
        for i in range(40):                                    # 2 hours
            t += 180
            r = VS.process_vitals(epoch(eps[(i + 7) % len(eps)], t))
            first_after = first_after or r
            seq.append((r["status"], r.get("alert", ""), r.get("confidence", ""),
                        bool((r.get("window") or {}).get("early"))))
        st = VS.v7_engine.load_state(ADM)
        check(st["anchor_s"] == 110.0 and st["anchor_f"] is not None, "new cuff rebuilt the anchor at 110/70")
        check(first_after.get("confidence") == "CALIBRATING" and first_after["bp"]["estimated_sbp"] == 110
              and first_after["bp"]["estimated_dbp"] == 70, "a new cuff restarts CALIBRATING with the new cuff 110/70")
        early_after = [s for s in seq if s[3]]
        check(len(early_after) == 1 and early_after[0][0] == "success" and early_after[0][1] == "",
              "new cuff: one early value, no alert even with a +20 mmHg delta")
        pubs = [s[:3] for s in seq if s[0] in ("success", "alert") and s[2] != "CALIBRATING" and not s[3]]
        print("  published after new cuff:", pubs)
        first_alert = next((i for i, s in enumerate(pubs) if s[0] == "alert"), None)
        check(first_alert is not None, "a sustained +20 mmHg delta raises an alert")
        # prototype rule: the alert needs run >= 2 (established) AND two consecutive breaching
        # slots, so the earliest it can fire is the SECOND counted slot; the first is LOW, no alert
        check(first_alert == 1 and pubs[0][0] == "success" and pubs[0][2] == "LOW",
              "first slot LOW with no alert; alert fires on the second consecutive breaching slot")
        check(first_alert is not None and all(s[0] == "alert" for s in pubs[first_alert:]),
              "alert stays latched on every following slot")
        # ---- 5a. a device re-send of the SAME value (new epochTime) is not a new cuff ------
        anchor_key_before = VS.v7_engine.load_state(ADM)["anchor_key"]
        t += 60
        r_dup = VS.process_vitals(cuff(111, 71, t))           # within 5 mmHg of 110/70
        t += 60
        r_zero = VS.process_vitals(cuff(0, 0, t))             # "no reading" from the monitor
        t += 60
        VS.process_vitals(epoch(eps[1], t))
        st = VS.v7_engine.load_state(ADM)
        check(r_dup["status"] == "ignored" and "Duplicate" in r_dup["message"] and r_zero["status"] == "ignored"
              and VS._ref_read(ADM)["sbp"] == 110 and st["anchor_key"] == anchor_key_before
              and st["anchor_f"] is not None and st["alert"],
              "same-value re-send and 0/0 keep the reference, the anchor and the alert")
        # ---- 5. a new cuff clears the latch ---------------------------------------------
        t += 180
        VS.process_vitals(cuff(125, 82, t))
        t += 180
        r = VS.process_vitals(epoch(eps[0], t))
        st = VS.v7_engine.load_state(ADM)
        check(st["alert"] == "" and st["anchor_f"] is None and r.get("confidence") == "CALIBRATING"
              and r.get("alert") == "" and r["bp"]["estimated_sbp"] == 125,
              "new cuff clears the alert and starts re-calibration (CALIBRATING 125, no alert)")
    finally:
        VS.v7_engine._ms, VS.v7_engine._md = real_ms, real_md

    # ---- 7b. V7_CAL_PUBLISH=0 -> the old behaviour, nothing until the first 15-min value ----
    E7.CAL_PUBLISH = False
    try:
        t += 180
        VS.process_vitals(cuff(125, 82, t))
        offs = []
        for i in range(8):
            t += 180
            offs.append(VS.process_vitals(epoch(eps[i], t)))
        check(not any(o.get("confidence") == "CALIBRATING" for o in offs)
              and offs[0]["status"] == "accumulating", "V7_CAL_PUBLISH=0: no CALIBRATING payloads")
    finally:
        E7.CAL_PUBLISH = True
    E7.CAL_PUBLISH = CAL_DEFAULT

    print("\n%d checks failed" % len(fails) if fails else "\nALL CHECKS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
