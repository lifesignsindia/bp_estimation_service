"""v7 BP engine for the Kafka pipeline.

This is the production port of ``ebp_dashboard/v7_runtime.py`` (the isolation-dashboard
prototype). The scoring logic, gate, anchor, 15-minute windows and alert latch are the same;
what changed is where the state lives and how the result is consumed:

    dashboard prototype                     pipeline (this file)
    ----------------------------------      ------------------------------------------------
    per-process dict + SQLite autosave      Redis, one JSON blob per admission (``v7:<adm>``)
    snapshot drawn on every epoch           the pipeline emits ONLY when a 15-minute window
                                            closes; per-epoch results are logged, never published
    poor/flat epochs shown as a state       poor/flat epochs are DROPPED from the window

CONTRACT
    eng = V7Engine(store)                           # store: redis-like (get / setex / delete)
    res = eng.score_epoch(adm, samples, fs, ts, ref_sbp, ref_dbp, ref_ts, extras=(hb, glu))

    ``res["window"]`` is None for every epoch except the first epoch of a NEW 15-minute slot,
    when it carries the closed slot's median (if the slot had >= V7_MIN_EPOCHS_WINDOW good
    epochs) — that is the one moment the pipeline publishes.

MODEL
    v7 is a DELTA model: median(features in slot) - median(features right after the cuff)
    -> +-25 mmHg change on top of the live cuff. Without a cuff there is no estimate. Only
    GOOD epochs (beats >= 10, template corr >= 0.90, dicrotic notch found) build the anchor or
    enter a window. Trained on MIMIC (19 patients, 464 pairs); day-5 blind run on CIMS caught
    5 of 8 real moves 1.5-5.2 h early at 50% precision. See docs/V7_PIPELINE.md.

Everything here is best-effort against Redis: a store failure degrades to "no state", never an
exception into process_vitals.
"""
import os
import sys
import json
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import bpv4_features as V   # noqa: E402  (same DSP as the validated prototype)

MODEL_DIR     = os.getenv("V7_MODEL_DIR", os.path.join(_HERE, "models", "v7"))
WINDOW_SEC    = int(os.getenv("V7_WINDOW_SEC", "900"))        # wall-clock slot length
MIN_EP_WINDOW = int(os.getenv("V7_MIN_EPOCHS_WINDOW", "2"))   # good epochs a slot needs to count
N_ANCHOR      = int(os.getenv("V7_MIN_EPOCHS_ANCHOR", "6"))   # good epochs to build the anchor
ALERT_SBP     = float(os.getenv("V7_ALERT_SBP", "15"))
ALERT_DBP     = float(os.getenv("V7_ALERT_DBP", "10"))
ALERT_PERSIST = int(os.getenv("V7_ALERT_PERSIST", "2"))       # consecutive breaching slots
CAP           = float(os.getenv("V7_CAP_MMHG", "25"))
GATE_BEATS    = int(os.getenv("V7_GATE_BEATS", "10"))
GATE_CORR     = float(os.getenv("V7_GATE_CORR", "0.90"))
STALE_SEC     = int(os.getenv("V7_STALE_SEC", "1800"))        # gap that discards an open slot
STATE_TTL     = int(os.getenv("V7_STATE_TTL", "86400"))       # Redis TTL, same as the cuff ref
MIN_SAMPLES   = int(os.getenv("V7_MIN_SAMPLES", "1200"))
TREND_HIST    = 5                                             # slots kept for the trend field

_CORE = ["aix", "ri", "ipa", "dvp_time", "stiffness_idx"]     # all-NaN together <=> no notch


def _fresh_state():
    return dict(
        anchor_key=None, anchor_ts=None, anchor_f=None, anchor_s=None, anchor_d=None,
        buf=[],                       # good-epoch feature vectors collected for the anchor
        win_key=None, win=[],         # open slot: [sbp, dbp, hb, glu] per good epoch
        win_epochs=0,                 # every epoch seen in the open slot, good or not
        win_first_ts=None,
        run=0, n_windows=0,           # consecutive counted slots; total counted slots
        hot_run=0, alert="", alert_since=None, alert_win=None,
        last_ref_id=None,
        last_epoch_ts=None, last_value=None, last_value_ts=None,
        hist=[],                      # last TREND_HIST slot medians [sbp, dbp]
    )


class V7Engine(object):
    def __init__(self, store, model_dir=MODEL_DIR):
        """``store`` must offer get(key)->str|None, setex(key, ttl, str), delete(key)."""
        import joblib
        self._store = store
        self.ok = False
        self.err = ""
        try:
            man = json.load(open(os.path.join(model_dir, "v7_manifest.json")))
            self.version = man.get("version", "v7")
            self._nm = list(man["features"])
            self._cols = [V.NAMES.index(n) for n in self._nm]
            self._core_idx = [self._nm.index(c) for c in _CORE]
            med = json.load(open(os.path.join(model_dir, "feature_medians.json")))["medians"]
            self._feat_med = np.asarray([float(med[n]) for n in self._nm], float)
            self._ms = joblib.load(os.path.join(model_dir, "v7_sbp.pkl"))
            self._md = joblib.load(os.path.join(model_dir, "v7_dbp.pkl"))
            self._ss = joblib.load(os.path.join(model_dir, "v7_scaler_sbp.pkl"))
            self._sd = joblib.load(os.path.join(model_dir, "v7_scaler_dbp.pkl"))
            self.ok = True
            print("[V7] engine ready | %s | %s | window %ds | anchor %d epochs | gate beats>=%d corr>=%.2f "
                  "| alert %.0f/%.0f x%d | cap %.0f" % (self.version, model_dir, WINDOW_SEC, N_ANCHOR,
                                                        GATE_BEATS, GATE_CORR, ALERT_SBP, ALERT_DBP,
                                                        ALERT_PERSIST, CAP))
            sys.stdout.flush()
        except Exception as e:
            self.err = "%s: %s" % (type(e).__name__, e)
            raise

    # ------------------------------------------------------------------ state I/O
    @staticmethod
    def _key(adm):
        return "v7:%s" % adm

    def load_state(self, adm):
        s = _fresh_state()
        try:
            raw = self._store.get(self._key(adm))
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                s.update(json.loads(raw))
        except Exception as e:
            print("[V7] state load failed for %s: %s: %s" % (adm, type(e).__name__, e))
            sys.stdout.flush()
        return s

    def save_state(self, adm, s):
        try:
            self._store.setex(self._key(adm), STATE_TTL, json.dumps(s))
        except Exception as e:
            print("[V7] state save failed for %s: %s: %s" % (adm, type(e).__name__, e))
            sys.stdout.flush()

    def reset(self, adm):
        try:
            self._store.delete(self._key(adm))
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _ref_key(ref_ts, ref_sbp, ref_dbp):
        """Identity of a cuff. Its own timestamp when we have one, else its value."""
        if isinstance(ref_ts, (int, float)) and ref_ts > 0:
            return str(int(ref_ts))
        return "v:%s:%s" % (ref_sbp, ref_dbp)

    def _finalise_window(self, s):
        """Close the open slot. Returns the published window dict, or None if the slot is dropped."""
        vals = [v for v in s["win"] if v and v[0] is not None]
        n_epochs = s["win_epochs"]
        s["win"], s["win_epochs"] = [], 0
        if len(vals) < MIN_EP_WINDOW:
            # DECAY, don't reset (prototype FIX 2): one quiet slot must not undo an established
            # patient. Two consecutive quiet slots still drop them back to LOW confidence.
            s["run"] = max(0, s["run"] - 1)
            return None
        sbp = float(np.median([v[0] for v in vals]))
        dbp = float(np.median([v[1] for v in vals]))
        hb  = [v[2] for v in vals if isinstance(v[2], (int, float))]
        glu = [v[3] for v in vals if isinstance(v[3], (int, float))]
        s["run"] += 1
        s["n_windows"] += 1
        s["hist"] = (s["hist"] + [[round(sbp, 1), round(dbp, 1)]])[-TREND_HIST:]
        established = s["run"] >= 2
        hot_s = abs(sbp - s["anchor_s"]) >= ALERT_SBP
        hot_d = abs(dbp - s["anchor_d"]) >= ALERT_DBP
        hot = hot_s or hot_d
        s["hot_run"] = s["hot_run"] + 1 if hot else 0
        alert_new = False
        # THE ALERT LATCHES UNTIL A NEW CUFF (prototype behaviour, confirmed on day 5). It needs
        # ALERT_PERSIST consecutive breaching slots from an ESTABLISHED patient, so a one-slot
        # blip never fires.
        if established and s["hot_run"] >= ALERT_PERSIST and not s["alert"]:
            which = []
            if hot_s:
                which.append("SBP")
            if hot_d:
                which.append("DBP")
            s["alert"] = "+".join(which) or "SBP"
            s["alert_since"] = time.time()
            s["alert_win"] = s["win_key"]
            alert_new = True
        return dict(
            sbp=round(sbp, 1), dbp=round(dbp, 1),
            hb=(round(float(np.mean(hb)), 1) if hb else None),
            glucose=(int(round(float(np.mean(glu)))) if glu else None),
            n_good=len(vals), n_epochs=n_epochs,
            key=s["win_key"], start=s["win_key"] * WINDOW_SEC, end=(s["win_key"] + 1) * WINDOW_SEC,
            established=established, hot=hot, hot_sbp=hot_s, hot_dbp=hot_d,
            alert=s["alert"], alert_new=alert_new, run=s["run"],
        )

    @staticmethod
    def trend(s):
        """Same shape as the legacy engine's trend block, computed over the last slots."""
        arr = [h[0] for h in s.get("hist", [])]
        n = len(arr)
        if n < 3:
            return {"trend": "Stable ->", "slope": 0.0, "readings": n}
        slope = float(np.polyfit(np.arange(n), np.asarray(arr, float), 1)[0])
        label = "Rising ^" if slope > 1.0 else ("Falling v" if slope < -1.0 else "Stable ->")
        return {"trend": label, "slope": round(slope, 2), "readings": n}

    # ------------------------------------------------------------------ main entry
    def score_epoch(self, adm, samples, fs, ts, ref_sbp, ref_dbp, ref_ts, extras=None):
        """One pleth epoch. Loads, updates and saves this admission's state.

        Returns a dict:
            quality      SHORT | FLAT | NO_FEATURES | POOR | FAIR | GOOD
            good         bool — this epoch entered the window / anchor
            state        no reference | calibrating | accumulating | 15 min average | flat signal | poor signal
            epoch_value  (sbp, dbp) raw per-epoch estimate, or None. LOG ONLY, never publish.
            window       closed-slot dict (see _finalise_window) or None
            anchor       dict(sbp, dbp, key, ts) or None
            calibrating  "k/N" while the anchor is being built
            alert        latched alert string ("" if none)
            trend        legacy-shaped trend block
        """
        s = self.load_state(adm)
        out = dict(quality="", good=False, state="", epoch_value=None, window=None,
                   n_beats=None, template_corr=None, anchor=None, calibrating="",
                   alert=s["alert"], trend=self.trend(s), run=s["run"])
        hb, glu = (extras or (None, None))[:2] if extras else (None, None)

        # ---- reference bookkeeping (prototype FIX 5 / FIX 7b) ------------------------------
        have_ref = isinstance(ref_sbp, (int, float)) and ref_sbp > 0
        new_ts = ref_ts if (isinstance(ref_ts, (int, float)) and ref_ts > 0) else None
        stale = (have_ref and new_ts is not None and s["anchor_ts"] is not None
                 and new_ts < s["anchor_ts"])
        if stale:
            print("[V7] stale reference REJECTED for %s: %s/%s @%d older than anchor %s/%s @%d"
                  % (adm, ref_sbp, ref_dbp, new_ts, s["anchor_s"], s["anchor_d"], s["anchor_ts"]))
            sys.stdout.flush()
        if have_ref and not stale:
            rid = self._ref_key(new_ts, ref_sbp, ref_dbp)
            if rid != s["last_ref_id"]:
                # any genuinely NEW cuff (even an identical repeat) retires the alert
                s["alert"], s["alert_since"], s["alert_win"] = "", None, None
                s["last_ref_id"] = rid
            if rid != s["anchor_key"]:
                # a new cuff: rebuild the model anchor from scratch, drop the open slot — it
                # belonged to the old anchor (prototype FIX 3)
                s.update(anchor_key=rid, anchor_ts=new_ts,
                         anchor_s=float(ref_sbp), anchor_d=float(ref_dbp or 0),
                         anchor_f=None, buf=[], run=0, hot_run=0,
                         win=[], win_key=None, win_epochs=0, win_first_ts=None,
                         last_value=None, hist=[])
                print("[V7] new anchor cuff for %s: %s/%s (ref id %s)" % (adm, ref_sbp, ref_dbp, rid))
                sys.stdout.flush()
        out["alert"] = s["alert"]
        if s["anchor_key"] is not None:
            out["anchor"] = dict(sbp=s["anchor_s"], dbp=s["anchor_d"], key=s["anchor_key"], ts=s["anchor_ts"])

        # ---- a long silence discards the open slot instead of publishing it late ----------
        if s["last_epoch_ts"] is not None and ts - s["last_epoch_ts"] >= STALE_SEC and s["win_key"] is not None:
            print("[V7] %s silent for %ds: open slot discarded" % (adm, int(ts - s["last_epoch_ts"])))
            sys.stdout.flush()
            s["win"], s["win_key"], s["win_epochs"], s["win_first_ts"] = [], None, 0, None
            s["run"] = max(0, s["run"] - 1)
        s["last_epoch_ts"] = ts

        # ---- stage A: flat / short ---------------------------------------------------------
        x = np.asarray([v for v in (samples or [])
                        if isinstance(v, (int, float, np.integer, np.floating))], float)
        if len(x) < MIN_SAMPLES or float(np.std(x)) < 1e-6:
            out["quality"] = "FLAT" if len(x) >= MIN_SAMPLES else "SHORT"
            out["state"] = "flat signal"
            return self._finish(adm, s, out, ts)

        # ---- stage B: morphology gate ------------------------------------------------------
        f, q = V.features_from_epoch(x, fs_in=float(fs or 200.0))
        if f is None:
            out["quality"], out["state"] = "NO_FEATURES", "flat signal"
            return self._finish(adm, s, out, ts)
        fv = np.asarray(f, float)[self._cols]
        nb, tc = float(q[0]), float(q[1])
        notch = not bool(np.isnan(fv[self._core_idx]).any())
        good = (nb >= GATE_BEATS) and (tc >= GATE_CORR) and notch
        out["quality"] = "GOOD" if good else ("FAIR" if (nb >= 6 and tc >= 0.80) else "POOR")
        out["n_beats"], out["template_corr"], out["good"] = nb, round(tc, 3), good
        fvi = np.where(np.isnan(fv), self._feat_med, fv)

        if s["anchor_key"] is None:
            out["state"] = "no reference"
            return self._finish(adm, s, out, ts)

        if s["anchor_f"] is None:
            if good:
                s["buf"].append([float(v) for v in fvi])
                if len(s["buf"]) >= N_ANCHOR:
                    s["anchor_f"] = [float(v) for v in np.median(np.asarray(s["buf"], float), 0)]
                    s["buf"] = []
                    print("[V7] anchor built for %s from %d good epochs" % (adm, N_ANCHOR))
                    sys.stdout.flush()
            out["state"] = "calibrating"
            out["calibrating"] = "%d/%d" % (len(s["buf"]), N_ANCHOR) if s["anchor_f"] is None else "%d/%d" % (N_ANCHOR, N_ANCHOR)
            return self._finish(adm, s, out, ts)

        # ---- score against the anchor -------------------------------------------------------
        dd = fvi - np.asarray(s["anchor_f"], float)
        d_s = float(np.clip(self._ms.predict(self._ss.transform([dd]))[0], -CAP, CAP))
        d_d = float(np.clip(self._md.predict(self._sd.transform([dd]))[0], -CAP, CAP))
        sbp, dbp = s["anchor_s"] + d_s, s["anchor_d"] + d_d
        out["epoch_value"] = (round(sbp, 1), round(dbp, 1))
        s["last_value"], s["last_value_ts"] = [round(sbp, 1), round(dbp, 1)], ts

        # ---- wall-clock 15-minute slot -----------------------------------------------------
        key = int(ts) // WINDOW_SEC
        if s["win_key"] is None:
            s["win_key"], s["win_first_ts"] = key, ts
        elif key != s["win_key"]:
            out["window"] = self._finalise_window(s)
            s["win_key"], s["win_first_ts"] = key, ts
        s["win_epochs"] += 1
        if good:
            s["win"].append([round(sbp, 1), round(dbp, 1), hb, glu])
        out["alert"] = s["alert"]
        out["trend"] = self.trend(s)
        out["run"] = s["run"]
        if out["quality"] == "POOR":
            out["state"] = "poor signal"
        elif s["run"] >= 2:
            out["state"] = "15 min average"
        else:
            out["state"] = "accumulating"
        return self._finish(adm, s, out, ts)

    def _finish(self, adm, s, out, ts):
        out["open_window"] = dict(key=s["win_key"], n_good=len(s["win"]), n_epochs=s["win_epochs"],
                                  since=s["win_first_ts"])
        self.save_state(adm, s)
        return out
