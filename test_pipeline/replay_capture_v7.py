"""Replay REAL captured patients through the Docker/Kafka v7 pipeline and score the outputs.

    docker compose up -d --build          # pipeline must run with EBP_EPOCH_TS_MAX_SKEW_SEC large
    python test_pipeline/replay_capture_v7.py --glob "ebp_dashboard/pleth_capture/2026-08-1[5-6]/*.jsonl*"

Each capture line carries the waveform AND the cuff that was live at that moment (ref_sbp/ref_dbp)
plus the OLD pipeline's estimate (est_sbp/est_dbp). Per admission, in time order:
  * a NISO206 cuff packet is produced whenever (ref_sbp, ref_dbp) changes
  * every de-duplicated epoch is produced as a NISO101 packet with its real epochTime
Then vitals.clinical is read back and each published 15-minute slot is written to a CSV next to
the cuff live at the time, the NEXT cuff, and the old pipeline's mean estimate in the same slot.
A transition table scores every cuff change by the user's rule: was v7's last value before the
new cuff within +-15 of it, and was an alert already up?
"""
import os
import sys
import csv
import json
import glob
import gzip
import time
import argparse
import subprocess
import collections
from datetime import datetime, timedelta, timezone

import numpy as np
from confluent_kafka import Producer, Consumer, TopicPartition

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROKER = os.getenv("KAFKA_BROKERS", "localhost:9092")
IN, OUT = "vitals.raw", "vitals.clinical"
FAC = os.getenv("REPLAY_FACILITY", "CF1398828720")
IST = timezone(timedelta(hours=5, minutes=30))


def ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%m-%d %H:%M") if ts else ""


def load(path):
    op = gzip.open if path.endswith(".gz") else open
    rows, seen = [], set()
    with op(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("epochTime")
            if not isinstance(t, (int, float)) or t <= 0:
                continue
            t = t / 1000.0 if t > 1e11 else float(t)
            if t in seen:
                continue
            seen.add(t)
            s = (r.get("pleth") or {}).get("rawData") or (r.get("pleth") or {}).get("plethWave") or []
            if len(s) < 1200:
                continue
            rows.append(dict(ts=t, adm=r.get("admissionId"), pleth=s, ref=(r.get("ref_sbp"), r.get("ref_dbp")),
                             est=(r.get("est_sbp"), r.get("est_dbp"))))
    rows.sort(key=lambda r: r["ts"])
    return rows


def clear_redis(adms):
    keys = []
    for a in adms:
        keys += ["v7:%s" % a, "ref:%s" % a, "session:%s" % a, "primary:%s" % a, "recal:%s" % a]
    try:
        subprocess.run(["docker", "exec", "new_esti-redis-1", "redis-cli", "del"] + keys,
                       check=False, capture_output=True, timeout=30)
    except Exception as e:
        print("[replay] redis clear skipped: %s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="ebp_dashboard/pleth_capture/ADM*.jsonl")
    ap.add_argument("--min-cuffs", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="max patients (0 = all)")
    ap.add_argument("--out", default="test_pipeline/out")
    ap.add_argument("--quiet-sec", type=int, default=90, help="stop reading after this long with no output")
    ap.add_argument("--collect-only", action="store_true",
                    help="do not produce; read the WHOLE output topic from the beginning and build the report "
                         "for the admissions in --glob (use after a replay whose reader was cut short)")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(_REPO, a.glob)))
    patients = {}
    for p in files:
        rows = load(p)
        if not rows:
            continue
        adm = rows[0]["adm"] or os.path.basename(p).split(".")[0]
        cuffs = []
        last = None
        for r in rows:
            if r["ref"][0] is not None and r["ref"] != last:
                cuffs.append((r["ts"], r["ref"]))
                last = r["ref"]
        if len(cuffs) < a.min_cuffs:
            continue
        patients.setdefault(adm, []).extend(rows)
    for adm in patients:
        patients[adm].sort(key=lambda r: r["ts"])
    adms = sorted(patients, key=lambda k: -len(patients[k]))
    if a.limit:
        adms = adms[:a.limit]
    n_ep = sum(len(patients[k]) for k in adms)
    print("[replay] %d patients, %d de-duplicated epochs, from %d files" % (len(adms), n_ep, len(files)))
    if not adms:
        return 1

    c = Consumer({"bootstrap.servers": BROKER, "group.id": "replay-%d" % int(time.time()),
                  "auto.offset.reset": "earliest", "enable.auto.commit": False})
    md = c.list_topics(OUT, timeout=10).topics[OUT]
    ends = {pid: c.get_watermark_offsets(TopicPartition(OUT, pid), timeout=10) for pid in md.partitions}
    if a.collect_only:
        c.assign([TopicPartition(OUT, pid, lo) for pid, (lo, hi) in ends.items()])
        pending = {pid: hi for pid, (lo, hi) in ends.items() if hi > lo}
        print("[replay] collect-only: reading %d messages already on %s" % (sum(hi - lo for lo, hi in ends.values()), OUT))
    else:
        c.assign([TopicPartition(OUT, pid, hi) for pid, (lo, hi) in ends.items()])
        pending = {}
        clear_redis(adms)

    p = Producer({"bootstrap.servers": BROKER, "linger.ms": 5})
    sent = 0
    t_start = time.time()
    for adm in ([] if a.collect_only else adms):
        last = None
        for i, r in enumerate(patients[adm]):
            if r["ref"][0] is not None and r["ref"] != last:
                last = r["ref"]
                p.produce(IN, key=adm.encode(), value=json.dumps({
                    "admissionId": adm, "facilityId": FAC, "epochTime": int((r["ts"] - 1) * 1000),
                    "device": {"deviceName": "NISO206", "deviceType": "BP"},
                    "bp": {"BPSYS": int(r["ref"][0]), "BPDIA": int(r["ref"][1] or 0), "BP_ERROR": 0}}).encode())
            p.produce(IN, key=adm.encode(), value=json.dumps({
                "admissionId": adm, "facilityId": FAC, "patientId": adm, "epochTime": int(r["ts"] * 1000),
                "seqNum": i, "seqPart": 1,
                "device": {"deviceName": "NISO101", "deviceType": "BP_SPO2"},
                "pleth": {"plethWave": [int(v) for v in r["pleth"]]}}).encode())
            sent += 1
            p.poll(0)
            if sent % 500 == 0:
                p.flush(30)
                print("[replay] produced %d/%d epochs" % (sent, n_ep))
    p.flush(60)
    print("[replay] all produced in %.0fs; reading %s ..." % (time.time() - t_start, OUT))

    want = set(adms)
    got, last_rx = [], time.time()
    while time.time() - last_rx < a.quiet_sec:
        if a.collect_only and not pending:
            break                                   # reached the end of every partition
        m = c.poll(1.0)
        if m is None or m.error():
            continue
        if a.collect_only:
            last_rx = time.time()
            if m.offset() + 1 >= pending.get(m.partition(), 0):
                pending.pop(m.partition(), None)
        try:
            d = json.loads(m.value())
        except Exception:
            continue
        if d.get("admissionId") not in want:
            continue
        got.append(d)
        last_rx = time.time()
        if len(got) % 25 == 0:
            print("[replay] %d payloads so far" % len(got))
    c.close()
    print("[replay] %d payloads received for %d patients" % (len(got), len({d['admissionId'] for d in got})))

    # ---------------------------------------------------------------- report
    os.makedirs(os.path.join(_REPO, a.out), exist_ok=True)
    tag = time.strftime("%Y%m%d_%H%M")
    rows_csv = os.path.join(_REPO, a.out, "v7_replay_windows_%s.csv" % tag)
    trans_csv = os.path.join(_REPO, a.out, "v7_replay_transitions_%s.csv" % tag)

    by_adm = collections.defaultdict(list)
    for d in got:
        by_adm[d["admissionId"]].append(d)
    for adm in by_adm:
        by_adm[adm].sort(key=lambda d: d.get("window", {}).get("end", 0))

    with open(rows_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["admission", "slot_start_ist", "slot_end_ist", "status", "confidence", "alert",
                    "v7_sbp", "v7_dbp", "cuff_sbp", "cuff_dbp", "d_sbp_vs_cuff", "d_dbp_vs_cuff",
                    "old_est_sbp_mean", "old_est_dbp_mean", "good_epochs", "epochs", "hb", "glucose",
                    "next_cuff_sbp", "next_cuff_dbp", "next_cuff_in_min", "v7_within15_next", "old_within15_next"])
        for adm in adms:
            recs = patients[adm]
            cuffs = []
            last = None
            for r in recs:
                if r["ref"][0] is not None and r["ref"] != last:
                    cuffs.append((r["ts"], r["ref"]))
                    last = r["ref"]
            for d in by_adm.get(adm, []):
                win = d.get("window", {})
                s0, s1 = win.get("start", 0), win.get("end", 0)
                bp = d.get("bp", {})
                est = [r["est"] for r in recs if s0 <= r["ts"] < s1 and r["est"][0] is not None]
                oe_s = round(float(np.mean([e[0] for e in est])), 1) if est else ""
                oe_d = round(float(np.mean([e[1] for e in est])), 1) if est else ""
                nxt = next(((t, v) for t, v in cuffs if t >= s1), None)
                v7s, v7d = bp.get("estimated_sbp"), bp.get("estimated_dbp")
                rs, rd = bp.get("reference_sbp"), bp.get("reference_dbp")
                w.writerow([adm, ist(s0), ist(s1), d["status"], d.get("confidence"), d.get("alert"),
                            v7s, v7d, rs, rd,
                            (v7s - rs) if (v7s is not None and rs) else "", (v7d - rd) if (v7d is not None and rd) else "",
                            oe_s, oe_d, win.get("good_epochs"), win.get("epochs"), d.get("hemoglobin", ""), d.get("glucose", ""),
                            nxt[1][0] if nxt else "", nxt[1][1] if nxt else "",
                            round((nxt[0] - s1) / 60) if nxt else "",
                            (abs(v7s - nxt[1][0]) <= 15) if (nxt and v7s is not None) else "",
                            (abs(oe_s - nxt[1][0]) <= 15) if (nxt and oe_s != "") else ""])

    # transitions: every cuff change, scored by the last v7 value published before it
    trans = []
    with open(trans_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["admission", "cuff_time_ist", "prev_cuff", "new_cuff", "d_sbp", "d_dbp", "real_move",
                    "v7_last_before", "v7_age_min", "v7_within15_new", "v7_moved_toward", "alert_before",
                    "old_last_before", "old_within15_new", "hold_within15_new"])
        for adm in adms:
            recs = patients[adm]
            cuffs, last = [], None
            for r in recs:
                if r["ref"][0] is not None and r["ref"] != last:
                    cuffs.append((r["ts"], r["ref"]))
                    last = r["ref"]
            outs = by_adm.get(adm, [])
            for (t0, c0), (t1, c1) in zip(cuffs[:-1], cuffs[1:]):
                ds, dd = c1[0] - c0[0], (c1[1] or 0) - (c0[1] or 0)
                real = abs(ds) >= 15 or abs(dd) >= 10
                prior = [d for d in outs if t0 <= d.get("window", {}).get("end", 0) <= t1 + 60]
                lastv = prior[-1] if prior else None
                est_prior = [r["est"] for r in recs if t0 <= r["ts"] < t1 and r["est"][0] is not None]
                old_last = est_prior[-1][0] if est_prior else None
                v7s = lastv["bp"]["estimated_sbp"] if lastv else None
                row = [adm, ist(t1), "%s/%s" % c0, "%s/%s" % c1, ds, dd, real,
                       ("%s/%s" % (v7s, lastv["bp"]["estimated_dbp"])) if lastv else "",
                       round((t1 - lastv["window"]["end"]) / 60) if lastv else "",
                       (abs(v7s - c1[0]) <= 15) if lastv else "",
                       (np.sign(v7s - c0[0]) == np.sign(ds)) if (lastv and ds) else "",
                       bool(lastv.get("alert")) if lastv else "",
                       old_last if old_last is not None else "",
                       (abs(old_last - c1[0]) <= 15) if old_last is not None else "",
                       abs(c0[0] - c1[0]) <= 15]
                trans.append(row)
                w.writerow(row)

    print("\n[replay] windows  -> %s" % os.path.relpath(rows_csv, _REPO))
    print("[replay] transit. -> %s" % os.path.relpath(trans_csv, _REPO))
    real = [r for r in trans if r[6]]
    scored = [r for r in real if r[9] != ""]
    print("[replay] cuff transitions: %d | real moves (>=15 sys or >=10 dia): %d | with a v7 value before: %d"
          % (len(trans), len(real), len(scored)))
    if scored:
        print("[replay]   v7 within 15 of the NEW cuff: %d/%d | HOLD within 15: %d/%d | old pipeline within 15: %d/%d"
              % (sum(1 for r in scored if r[9] is True), len(scored),
                 sum(1 for r in scored if r[14]), len(scored),
                 sum(1 for r in scored if r[13] is True), sum(1 for r in scored if r[13] != "")))
        print("[replay]   v7 moved toward the new cuff: %d/%d | alert already up: %d/%d"
              % (sum(1 for r in scored if r[10] is True), len(scored), sum(1 for r in scored if r[11] is True), len(scored)))
    stat = collections.Counter(d["status"] for d in got)
    conf = collections.Counter(d.get("confidence") for d in got)
    print("[replay] statuses %s | confidence %s" % (dict(stat), dict(conf)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
