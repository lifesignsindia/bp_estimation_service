"""
danger.py — BP model training from NISO204 / Berry PPG data.

Usage:
    python danger.py data/training_data_200hz/
    python danger.py data/training_data_200hz/ data/hyperonly_training_data_200hz/ data/ppgbp_converted/
    python danger.py data/training_data_200hz/ --output_dir modelsss
"""
import os, json, joblib, argparse
import numpy as np
from glob import glob
from scipy.signal import butter, filtfilt, find_peaks, medfilt, resample, resample_poly
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupShuffleSplit, RandomizedSearchCV
from sklearn.metrics import accuracy_score, mean_absolute_error, r2_score
from scipy.stats import skew
import psutil

try:
    from xgboost import XGBRegressor
    USE_XGB = True
except ImportError:
    from sklearn.ensemble import RandomForestRegressor
    USE_XGB = False
    print("WARNING: xgboost not installed — falling back to RandomForest")

# ── Constants ──────────────────────────────────────────────────────────────────
FS          = 120           # PlethWave[32:] is always 120 Hz
SEG_LEN     = FS * 5       # 600 samples per 5-second segment
SEGMENTS    = 6             # 6 × 5s = 30s
MIN_PPG_LEN = FS * 25      # minimum 25s of signal


# Error codes AND physiologically impossible values
INVALID_BP_VALUES = {0.0, -1.0, 1.0, 200.0, 202.0, 400.0, 404.0}
SBP_RANGE = (60, 220)
DBP_RANGE = (30, 130)


def mem():
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024


def bandpass(sig):
    nyq = 0.5 * FS
    b, a = butter(3, [0.4 / nyq, 11 / nyq], btype='band')
    return filtfilt(b, a, sig)


def is_noisy(signal):
    if np.std(signal) < 0.01:
        return True
    if len(find_peaks(signal, distance=FS // 2)[0]) < 2:
        return True
    if skew(signal) < 0:
        return True
    return False


def extract_features(ppg_seg, pr_seg):
    if len(ppg_seg) < FS or is_noisy(ppg_seg):
        return None

    ppg_filt = bandpass(ppg_seg)
    norm = (ppg_filt - np.min(ppg_filt)) / (np.max(ppg_filt) - np.min(ppg_filt) + 1e-6)
    peaks, _ = find_peaks(norm, distance=int(FS * 0.4))
    mins,  _ = find_peaks(-norm, distance=int(FS * 0.3))

    cycles = [norm[mins[mins < p][-1]:mins[mins > p][0]] for p in peaks
              if len(mins[mins < p]) > 0 and len(mins[mins > p]) > 0]
    if not cycles:
        return None

    # Median-closest cycle (avoids noise spikes from max-amplitude selection)
    peak_amps  = [np.max(cyc) for cyc in cycles]
    median_amp = np.median(peak_amps)
    c = min(cycles, key=lambda cyc: abs(np.max(cyc) - median_amp))

    # Per-cycle normalize to 0-1 (removes sensor amplitude differences)
    c_min, c_max = np.min(c), np.max(c)
    c = (c - c_min) / (c_max - c_min + 1e-9)

    # Resample every cycle to exactly 100 points (removes HR/duration differences)
    c = resample(c, 100)
    c = np.clip(c, 0, 1)   # resample can overshoot slightly

    time = np.linspace(0, len(c) / FS, len(c))
    d1   = np.gradient(c)
    d2   = np.gradient(d1)

    auc   = np.trapezoid(c, time)
    ttp   = time[np.argmax(c)]
    tdp   = time[-1] - ttp if (time[-1] - ttp) != 0 else 1e-6
    ratio = ttp / tdp

    # APG features
    apg_a = float(np.max(d2))
    apg_b = float(np.min(d2))
    apg_ba = apg_b / apg_a if apg_a != 0 else 0.0

    fft_vals = np.abs(np.fft.fft(c)[:len(c) // 2])
    freqs    = np.fft.fftfreq(len(c), 1 / FS)[:len(c) // 2]
    pks, _   = find_peaks(fft_vals, distance=5)
    f_top = [0.0] * 3
    m_top = [0.0] * 3
    if len(pks) > 0:
        top_idx = np.argsort(fft_vals[pks])[-3:]
        ft = freqs[pks][top_idx].tolist()
        mt = fft_vals[pks][top_idx].tolist()
        f_top = ft + [0.0] * (3 - len(ft))
        m_top = mt + [0.0] * (3 - len(mt))

    ibi = np.diff(peaks) / FS if len(peaks) > 1 else np.array([0.0])
    hrv = float(np.std(ibi))

    pr_mean = float(np.nanmean(pr_seg)) if len(pr_seg) > 0 else 0.0
    pr_std  = float(np.nanstd(pr_seg))  if len(pr_seg) > 0 else 0.0
    if np.isnan(pr_mean): pr_mean = 0.0
    if np.isnan(pr_std) or pr_std < 0.1: pr_std = 2.0

    # Vascular features
    peak_idx  = np.argmax(c)
    post_peak = c[peak_idx:]
    notch_mins, _ = find_peaks(-post_peak)
    ri  = float(post_peak[notch_mins[0]] / (np.max(c) + 1e-9)) if len(notch_mins) > 0 else 0.0
    aix = float((post_peak[notch_mins[0]] - np.max(c)) / (np.max(c) + 1e-9)) if len(notch_mins) > 0 else 0.0
    large_si = float(0.1 / (ttp + 1e-9))

    rmssd = float(np.sqrt(np.mean(np.diff(ibi) ** 2))) if len(ibi) > 1 else 0.0
    pnn50 = float(np.sum(np.abs(np.diff(ibi)) > 0.05) / max(len(ibi) - 1, 1)) if len(ibi) > 1 else 0.0

    half_max   = np.max(c) * 0.5
    above_half = np.where(c >= half_max)[0]
    pw50 = float(len(above_half) / FS) if len(above_half) > 0 else 0.0

    return [
        float(np.max(c)), float(time[-1]), float(ttp), float(ratio),
        float(np.max(d1)), float(np.min(d1)), float(np.max(d2)), float(np.min(d2)),
        apg_a, apg_b, apg_ba,
        float(auc), *f_top, *m_top, hrv,
        float(np.mean(ppg_filt)), float(np.std(ppg_filt)),
        float(np.max(ppg_filt)), float(np.min(ppg_filt)),
        pr_mean, pr_std,
        ri, aix, large_si, rmssd, pnn50, pw50,
    ]


def get_bp_label(sbp, dbp):
    if sbp < 90 or dbp < 60:
        return "hypo"
    elif sbp >= 130 or dbp >= 80:
        return "hyper"
    else:
        return "normal"


def load_data(folders):
    X, Y_sbp, Y_dbp, labels, pids = [], [], [], [], []
    skipped_bp = 0

    for folder in folders:
        files = glob(os.path.join(folder, "*.json"))
        print(f"\n[{folder}] Found {len(files)} files")

        for path in files:
            try:
                with open(path) as f:
                    d = json.load(f)
            except Exception:
                continue

            # ── BP label validation ────────────────────────────────────────
            sbp = d.get("SBP") or d.get("BPSystolic")
            dbp = d.get("DBP") or d.get("BPDiastolic")

            if sbp is None or dbp is None:
                skipped_bp += 1; continue
            if not isinstance(sbp, (int, float)) or not isinstance(dbp, (int, float)):
                skipped_bp += 1; continue

            # Error codes (404 = no cuff reading, 200/202 = device fault)
            if sbp in INVALID_BP_VALUES or dbp in INVALID_BP_VALUES:
                skipped_bp += 1; continue

            # Physiological range check
            if not (SBP_RANGE[0] <= sbp <= SBP_RANGE[1]):
                skipped_bp += 1; continue
            if not (DBP_RANGE[0] <= dbp <= DBP_RANGE[1]):
                skipped_bp += 1; continue

            # DBP must be less than SBP
            if dbp >= sbp:
                skipped_bp += 1; continue

            # ── Signal extraction ─────────────────────────────────────────
            device = str(d.get("DeviceName", "")).lower()
            pw_raw = d.get("PlethWave", [])
            fs_field = d.get("FS")

            # Berry app JSON: PlethWave is already normalized (0-100), no header
            is_berry_json = "berry" in device or (
                fs_field is not None and len(pw_raw) > 0 and max(pw_raw[:5]) <= 100.0
            )

            if is_berry_json and len(pw_raw) > 0:
                ppg_raw = pw_raw
                file_fs = int(fs_field) if fs_field else 200
            elif len(pw_raw) > 32:
                ppg_raw = pw_raw[32:]   # NISO204: skip 32-byte header
                file_fs = FS            # always 120 Hz
            else:
                ppg_raw = d.get("Pleth", [])
                file_fs = FS

            # Resample to model FS (120 Hz) if source differs
            if file_fs != FS and len(ppg_raw) > 0:
                from math import gcd
                g = gcd(FS, file_fs)
                ppg_raw = resample_poly(
                    np.array(ppg_raw, dtype=float), FS // g, file_fs // g
                ).tolist()

            if len(ppg_raw) < MIN_PPG_LEN:
                continue

            ppg = medfilt(np.array(ppg_raw, dtype=float), kernel_size=3)

            # ── PR data ────────────────────────────────────────────────────
            pr_json = d.get("PRAllData")
            pr_arr  = None
            if isinstance(pr_json, list) and len(pr_json) > 0:
                tmp = np.array(pr_json, dtype=float)
                for v in INVALID_PR_VALUES:
                    tmp[tmp == v] = np.nan
                if np.count_nonzero(~np.isnan(tmp)) >= SEGMENTS:
                    pr_arr = tmp

            if pr_arr is None:
                pr_arr = np.full(SEGMENTS * 5, np.nan)

            # ── Feature extraction: one row per 5s segment ─────────────────
            label = get_bp_label(sbp, dbp)
            pid   = d.get("PatientID") or d.get("Name") or d.get("patientId") or os.path.basename(path)

            for i in range(SEGMENTS):
                seg_ppg = ppg[i * SEG_LEN:(i + 1) * SEG_LEN]
                seg_pr  = pr_arr[i * 5:(i + 1) * 5]
                feat = extract_features(seg_ppg, seg_pr)
                if feat:
                    X.append(feat)
                    Y_sbp.append(float(sbp))
                    Y_dbp.append(float(dbp))
                    labels.append(label)
                    pids.append(str(pid))

    print(f"\nTotal segment rows: {len(X)}")
    print(f"Skipped (bad BP label): {skipped_bp}")
    print(f"Memory: {mem():.1f} MB")
    return np.array(X), np.array(Y_sbp), np.array(Y_dbp), np.array(labels), np.array(pids)


def balance_classes(X, Y_sbp, Y_dbp, labels, pids, max_per_class=6000, seed=42):
    """Cap the dominant class so no group exceeds max_per_class rows."""
    rng = np.random.default_rng(seed)
    keep = []
    for grp in np.unique(labels):
        idx = np.where(labels == grp)[0]
        if len(idx) > max_per_class:
            idx = rng.choice(idx, size=max_per_class, replace=False)
        keep.append(idx)
    keep = np.concatenate(keep)
    rng.shuffle(keep)
    print(f"\nAfter balancing:")
    for grp in np.unique(labels[keep]):
        print(f"  {grp}: {np.sum(labels[keep] == grp)} rows")
    return X[keep], Y_sbp[keep], Y_dbp[keep], labels[keep], pids[keep]


def train_models(X, Y_sbp, Y_dbp, labels, pids, output_dir="modelsss"):
    os.makedirs(output_dir, exist_ok=True)

    # Balance classes before splitting — cap hyper which dominates
    X, Y_sbp, Y_dbp, labels, pids = balance_classes(X, Y_sbp, Y_dbp, labels, pids)

    # Subject-independent split
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, test_idx = next(gss.split(X, labels, groups=pids))
    X_train, X_test = X[train_idx], X[test_idx]
    l_train, l_test = labels[train_idx], labels[test_idx]
    print(f"\nSplit: {len(train_idx)} train / {len(test_idx)} test rows")
    print(f"Unique patients in test: {len(set(pids[test_idx]))}")

    # Global scaler
    scaler = StandardScaler()
    X_tr_sc = scaler.fit_transform(X_train)
    X_te_sc = scaler.transform(X_test)
    joblib.dump(scaler, os.path.join(output_dir, "global_feature_scaler.pkl"))
    print(f"Saved global_feature_scaler.pkl ({X_train.shape[1]} features)")

    # Classifier
    print("\n--- Classifier ---")
    clf = LogisticRegression(max_iter=1000, class_weight='balanced', solver='lbfgs')
    clf.fit(X_tr_sc, l_train)
    acc = accuracy_score(l_test, clf.predict(X_te_sc))
    print(f"Accuracy (subject-independent): {acc:.4f}")
    joblib.dump(clf, os.path.join(output_dir, "classifier.pkl"))

    for lbl, cnt in zip(*np.unique(labels, return_counts=True)):
        print(f"  {lbl}: {cnt} rows")

    # XGBoost hyperparameter space
    if USE_XGB:
        base_model  = XGBRegressor(random_state=42, n_jobs=-1, verbosity=0)
        param_space = {
            'n_estimators':     [100, 200, 300],
            'max_depth':        [3, 4, 5, 6],
            'learning_rate':    [0.01, 0.05, 0.1],
            'subsample':        [0.7, 0.8, 0.9],
            'colsample_bytree': [0.7, 0.8, 1.0],
            'reg_alpha':        [0, 0.1, 0.5],
            'reg_lambda':       [1, 2, 5],
        }
    else:
        from sklearn.ensemble import RandomForestRegressor
        base_model  = RandomForestRegressor(random_state=42)
        param_space = {'n_estimators': [50, 100, 200], 'max_depth': [None, 10, 20]}

    # Group regressors
    print("\n--- Group Regressors ---")
    for group in ["hypo", "normal", "hyper"]:
        g_train = train_idx[l_train == group]
        g_test  = test_idx[l_test  == group]

        if len(g_train) < 10:
            print(f"  {group}: only {len(g_train)} train rows — skipping")
            continue

        print(f"\n  {group.upper()} — {len(g_train)} train / {len(g_test)} test rows")

        Xg_tr = scaler.transform(X[g_train])
        Xg_te = scaler.transform(X[g_test]) if len(g_test) > 0 else np.empty((0, X.shape[1]))

        sbp_search = RandomizedSearchCV(
            base_model, param_space, n_iter=30, cv=3,
            scoring='neg_mean_absolute_error', n_jobs=-1, random_state=42
        )
        sbp_search.fit(Xg_tr, Y_sbp[g_train])

        dbp_search = RandomizedSearchCV(
            base_model, param_space, n_iter=30, cv=3,
            scoring='neg_mean_absolute_error', n_jobs=-1, random_state=42
        )
        dbp_search.fit(Xg_tr, Y_dbp[g_train])

        if len(Xg_te) > 0:
            mae_s = mean_absolute_error(Y_sbp[g_test], sbp_search.predict(Xg_te))
            mae_d = mean_absolute_error(Y_dbp[g_test], dbp_search.predict(Xg_te))
            r2_s  = r2_score(Y_sbp[g_test], sbp_search.predict(Xg_te))
            r2_d  = r2_score(Y_dbp[g_test], dbp_search.predict(Xg_te))
            print(f"  SBP MAE={mae_s:.2f} R2={r2_s:.2f} | DBP MAE={mae_d:.2f} R2={r2_d:.2f}")

        out = os.path.join(output_dir, f"{group}_models.pkl")
        joblib.dump({'sbp_model': sbp_search.best_estimator_,
                     'dbp_model': dbp_search.best_estimator_}, out)
        print(f"  Saved {group}_models.pkl ({os.path.getsize(out)/1024:.1f} KB)")

    print(f"\nDone. Models saved to: {os.path.abspath(output_dir)}")
    print(f"Memory: {mem():.1f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data_folders", nargs="+", help="One or more folders with JSON files")
    parser.add_argument("--output_dir", default="modelsss")
    args = parser.parse_args()

    X, Y_sbp, Y_dbp, labels, pids = load_data(args.data_folders)

    if len(X) == 0:
        print("No valid data loaded.")
    else:
        train_models(X, Y_sbp, Y_dbp, labels, pids, args.output_dir)
