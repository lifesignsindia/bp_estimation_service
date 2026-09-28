"""Offline sweep of the v7 anchor size over the REAL NISO101 captures. No Kafka, no Redis, no Docker.

    python test_pipeline/anchor_sweep_v7.py                 # N = 3 4 5 6
    python test_pipeline/anchor_sweep_v7.py --n 2 3 6

Every capture line carries the waveform AND the cuff that was live at that moment (ref_sbp/ref_dbp).
Per admission, in time order, each de-duplicated epoch goes straight through V7Engine.score_epoch
with the live cuff, exactly as process_vitals would hand it over. Only V7_MIN_EPOCHS_ANCHOR changes
between runs, so any difference is the anchor size and nothing else.

Scored by the v7 acceptance rule (+-15 mmHg of the NEXT cuff), for:
  * latency       minutes from a cuff to its first published 15-min value
  * first slot    the first value after each cuff (the one a smaller anchor makes earlier)
  * all slots     every published value
  * transitions   real cuff moves (>=15 sys or >=10 dia): last v7 value before the new cuff within
                  15 of it, and was an alert already up
HOLD (the cuff carried forward, i.e. what the CALIBRATING payload shows) is scored on the same slots.
"""
import os
import sys
import glob
import gzip
import json
import argparse
import collections

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import v7_engine as E                  # noqa: E402

FS = 200.0


class DictStore(object):
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def setex(self, k, ttl, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


def load_patients(pattern):
    """{adm: [row, ...]} de-duplicated on (adm, epochTime), NISO101 only, time-ordered."""
    pats, seen = collections.defaultdict(list), set()
    files = sorted(glob.glob(os.path.join(_REPO, pattern), recursive=True))
    for path in files:
        op = gzip.open if path.endswith(".gz") else open
        with op(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("deviceName") not in (None, "NISO101"):
                    continue
                t = r.get("epochTime")
                adm = r.get("admissionId")
                if not adm or not isinstance(t, (int, float)) or t <= 0:
                    continue
                t = t / 1000.0 if t > 1e11 else float(t)
                if (adm, t) in seen:
                    continue
                s = (r.get("pleth") or {}).get("rawData") or (r.get("pleth") or {}).get("plethWave") or []
                if len(s) < 1200:
                    continue
                seen.add((adm, t))
                ref = (r.get("ref_sbp"), r.get("ref_dbp"))
                pats[adm].append(dict(ts=t, pleth=s, ref=ref if ref[0] else None))
    for adm in pats:
        pats[adm].sort(key=lambda r: r["ts"])
    return pats, len(files)


def cuffs_of(rows):
    """(ts, (sbp, dbp)) every time the live cuff changes; ts = 1 s before the first epoch that sees it."""
    out, last = [], None
    for r in rows:
        if r["ref"] and r["ref"] != last:
            out.append((r["ts"] - 1, r["ref"]))
            last = r["ref"]
    return out


# features are the slow part and do not depend on N: compute once per epoch, reuse for every N
_CACHE, _CUR = {}, [None]
_real_feats = E.V.features_from_epoch


def _cached_feats(x, fs_in=200.0):
    k = _CUR[0]
    if k not in _CACHE:
        _CACHE[k] = _real_feats(x, fs_in=fs_in)
    return _CACHE[k]


E.V.features_from_epoch = _cached_feats


def replay(eng, adm, rows, cuffs):
    """Published windows as dicts with the publish time, the cuff they were anchored on, and alert."""
    eng.reset(adm)
    pubs, ci = [], -1
    for r in rows:
        while ci + 1 < len(cuffs) and cuffs[ci + 1][0] <= r["ts"]:
            ci += 1
        ref_s = ref_d = ref_ts = None
        if ci >= 0:
            ref_ts, (ref_s, ref_d) = cuffs[ci]
        _CUR[0] = (adm, r["ts"])
        res = eng.score_epoch(adm, r["pleth"], fs=FS, ts=r["ts"], ref_sbp=ref_s, ref_dbp=ref_d, ref_ts=ref_ts)
        w = res["window"]
        if w is not None and ci >= 0:
            pubs.append(dict(pub_ts=r["ts"], end=w["end"], sbp=w["sbp"], dbp=w["dbp"], alert=w["alert"],
                             established=w["established"], cuff_i=ci))
    return pubs


def pct(a, b):
    return "%d/%d (%.0f%%)" % (a, b, 100.0 * a / b) if b else "0/0"


def score(pats, n, min_cuffs):
    E.N_ANCHOR = n
    eng = E.V7Engine(DictStore())
    lat, never = [], 0
    first_ok, first_hold, first_n = 0, 0, 0
    all_ok_s, all_ok_d, all_hold, all_n = 0, 0, 0, 0
    tr_n, tr_ok, tr_alert, tr_hold = 0, 0, 0, 0
    for adm, rows in pats.items():
        cuffs = cuffs_of(rows)
        if len(cuffs) < min_cuffs:
            continue
        pubs = replay(eng, adm, rows, cuffs)
        for i, (ct, cv) in enumerate(cuffs):
            nxt_t = cuffs[i + 1][0] if i + 1 < len(cuffs) else float("inf")
            mine = [p for p in pubs if p["cuff_i"] == i and p["pub_ts"] < nxt_t]
            if mine:
                lat.append((mine[0]["pub_ts"] - ct) / 60.0)
            elif i + 1 < len(cuffs):
                never += 1                 # a next cuff came before any value was published
            if i + 1 >= len(cuffs):
                continue
            nv = cuffs[i + 1][1]
            for j, p in enumerate(mine):
                ok = abs(p["sbp"] - nv[0]) <= 15
                hold = abs(cv[0] - nv[0]) <= 15
                all_n += 1
                all_ok_s += ok
                all_ok_d += abs(p["dbp"] - (nv[1] or 0)) <= 15
                all_hold += hold
                if j == 0:
                    first_n += 1
                    first_ok += ok
                    first_hold += hold
            real = abs(nv[0] - cv[0]) >= 15 or abs((nv[1] or 0) - (cv[1] or 0)) >= 10
            if real and mine:
                tr_n += 1
                tr_ok += abs(mine[-1]["sbp"] - nv[0]) <= 15
                tr_alert += bool(mine[-1]["alert"])
                tr_hold += abs(cv[0] - nv[0]) <= 15
    lat = np.asarray(lat) if lat else np.asarray([np.nan])
    return dict(n=n, lat_med=float(np.nanmedian(lat)), lat_p75=float(np.nanpercentile(lat, 75)),
                n_cuffs_valued=int(np.isfinite(lat).sum()), never=never,
                first=pct(first_ok, first_n), first_hold=pct(first_hold, first_n),
                all_s=pct(all_ok_s, all_n), all_d=pct(all_ok_d, all_n), all_hold=pct(all_hold, all_n),
                tr=pct(tr_ok, tr_n), tr_alert=pct(tr_alert, tr_n), tr_hold=pct(tr_hold, tr_n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="ebp_dashboard/pleth_capture/**/*.jsonl*")
    ap.add_argument("--n", type=int, nargs="+", default=[3, 4, 5, 6])
    ap.add_argument("--min-cuffs", type=int, default=1)
    a = ap.parse_args()

    pats, n_files = load_patients(a.glob)
    n_ep = sum(len(v) for v in pats.values())
    n_cuffs = sum(len(cuffs_of(v)) for v in pats.values())
    print("[sweep] %d files -> %d admissions, %d de-duplicated epochs, %d cuffs" % (n_files, len(pats), n_ep, n_cuffs))
    sys.stdout.flush()

    rows = []
    for n in a.n:
        r = score(pats, n, a.min_cuffs)
        rows.append(r)
        print("[sweep] N=%d  latency median %.0f min (p75 %.0f) over %d cuffs | never valued before next cuff: %d"
              % (n, r["lat_med"], r["lat_p75"], r["n_cuffs_valued"], r["never"]))
        print("         first slot SBP +-15 of next cuff: %s   (HOLD %s)" % (r["first"], r["first_hold"]))
        print("         all slots  SBP +-15: %s  DBP +-15: %s   (HOLD SBP %s)" % (r["all_s"], r["all_d"], r["all_hold"]))
        print("         real moves: last v7 within 15 of new cuff %s | alert already up %s   (HOLD %s)"
              % (r["tr"], r["tr_alert"], r["tr_hold"]))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
