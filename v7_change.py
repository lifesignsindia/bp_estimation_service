"""v7 change check: "BP may have changed - take a cuff".

Not a BP number. After every anchor cuff, the change check learns the patient's own baseline of a
few pulse-shape / rate signals from the same GOOD epochs that build the v7 anchor (median and
noise). Every closed 15-min slot is then expressed as z-scores against that baseline; a slot whose
signed, combined score passes CHG_THRESHOLD in CHG_PERSIST consecutive slots in the same direction
raises the check, latched until the next cuff (a cuff inside the +-15/+-10 range retires it too).

Signals (direction sign = BP up when the signal moves this way; candidates from the 2026-11-03
CIMS NISO101 check, NOT yet validated - see docs/V7_PIPELINE.md):
    aging_index, apg_b_a     pulse-shape (second derivative) indices
    hr                       heart rate from the pleth
The direction label also reports mean reversion (a reference far above / below the patient's usual
level tends to come back), as a second opinion only - it never raises the check on its own.

V7_CHANGE_CHECK = shadow (default)  computed, logged and stored in the output (change_check block,
                                    display unaffected); the BP alert keeps its current rule
                  live              the check also drives status "alert" (BP_MAY_HAVE_CHANGED)
                  off               not computed
"""
import os

import numpy as np

MODE          = os.getenv("V7_CHANGE_CHECK", "shadow").strip().lower()
CHG_THRESHOLD = float(os.getenv("V7_CHANGE_THRESHOLD", "2.1"))
CHG_PERSIST   = int(os.getenv("V7_CHANGE_PERSIST", "2"))
CHG_POP_SBP   = float(os.getenv("V7_CHANGE_POP_SBP", "125"))

SIGNALS = ("aging_index", "apg_b_a", "hr")
SIGN    = {"aging_index": 1.0, "apg_b_a": 1.0, "hr": 1.0}
# noise floor per signal (feature units): a 6-epoch baseline can look unrealistically quiet, so its
# spread is never taken below this
FLOOR   = {"aging_index": 0.05, "apg_b_a": 0.05, "hr": 2.0}
ZCLIP   = 3.0


def enabled():
    return MODE in ("shadow", "live")


def live():
    return MODE == "live"


def pick(f, names):
    """The change signals of one epoch from the full bpv4_features vector, NaN when missing."""
    out = []
    for n in SIGNALS:
        try:
            v = float(f[names.index(n)])
        except Exception:
            v = float("nan")
        out.append(v if np.isfinite(v) else None)
    return out


def baseline(rows):
    """Median and robust spread of each signal over the anchor epochs."""
    med, sd = [], []
    for k, n in enumerate(SIGNALS):
        x = np.asarray([r[k] for r in rows if r and r[k] is not None], float)
        if len(x) < 3:
            med.append(None)
            sd.append(None)
            continue
        m = float(np.median(x))
        med.append(m)
        sd.append(max(1.4826 * float(np.median(np.abs(x - m))), FLOOR[n]))
    return dict(med=med, sd=sd)


def slot_z(rows, base):
    """z of the slot median of each signal against the anchor baseline (None when unavailable)."""
    z = []
    for k in range(len(SIGNALS)):
        x = [r[k] for r in rows if r and r[k] is not None]
        if not x or base is None or base["med"][k] is None:
            z.append(None)
            continue
        z.append(float((np.median(x) - base["med"][k]) / base["sd"][k]))
    return z


def score(z):
    """Signed combined score: mean of the sign-corrected, clipped z of the available signals.
    Positive = the signals moved the way BP rising moves them."""
    v = [SIGN[n] * float(np.clip(zz, -ZCLIP, ZCLIP)) for n, zz in zip(SIGNALS, z) if zz is not None]
    return float(np.mean(v)) if len(v) >= 2 else None


def reversion(anchor_sbp, prev_cuffs):
    """Mean-reversion opinion: +1 BP likely to rise, -1 to fall, 0 none. Uses the patient's earlier
    cuffs when there are any, else the population level."""
    if anchor_sbp is None:
        return 0
    ref = float(np.mean(prev_cuffs)) if prev_cuffs else CHG_POP_SBP
    d = ref - float(anchor_sbp)
    return 0 if abs(d) < 10 else int(np.sign(d))
