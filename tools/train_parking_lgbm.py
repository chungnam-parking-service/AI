"""음식점의 건물 주차 구성을 매장 정보로 예측한다(기본 천안·아산, 인자로 충남 등 지역 변경).

정답은 건축물대장 주차정보(지번 단위)이고 입력은 상가정보에서 얻을 수 있는 값만 쓴다.
같은 건물 매장이 train/test 에 섞이지 않도록 지번 단위(GroupKFold)로 나누고,
더 어려운 조건으로 법정동 단위 분할도 함께 잰다. baseline 두 개를 같은 fold 에서 비교한다.

출력
  out/cv_results.json      과제 x 분할방식 x 시드 x 모델 지표
  out/pred_unmatched.csv   건축물대장에 없는 음식점에 대한 최종 예측(전체 매칭 데이터로 학습)
"""
from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, balanced_accuracy_score, f1_score, roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.neighbors import BallTree

EARTH_M = 6_371_000.0
SIGUNGU = ("44131", "44133", "44200")
TASKS = {
    "indoor_self": "binary",  # 옥내 자주식 주차 있음
    "mechanical": "binary",   # 기계식 주차 있음
    "spaces_bin": "multiclass",  # 면수 0-5 / 6-20 / 21+
}


def load_data(data_dir: Path, sangga: str, buildings: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    with gzip.open(data_dir / sangga, "rt", encoding="utf-8") as f:
        s = pd.read_csv(f, dtype=str).fillna("")
    s = s[s["상권업종대분류명"] == "음식"].copy()
    for c in ("지번본번지", "지번부번지"):
        s[c] = pd.to_numeric(s[c], errors="coerce").fillna(0).astype(int).astype(str)
    s["key"] = s["법정동코드"] + "-" + s["지번본번지"] + "-" + s["지번부번지"]
    s["lat"] = pd.to_numeric(s["위도"], errors="coerce")
    s["lon"] = pd.to_numeric(s["경도"], errors="coerce")
    s = s.dropna(subset=["lat", "lon"]).reset_index(drop=True)

    b = pd.read_csv(data_dir / buildings, dtype=str).fillna("0")
    b["key"] = (b["주소 코드"] + "-" + pd.to_numeric(b["지번(본번)"]).astype(int).astype(str)
                + "-" + pd.to_numeric(b["지번(부번)"]).astype(int).astype(str))
    num = [c for c in b.columns if c not in ("주소 코드", "지번(본번)", "지번(부번)", "key")]
    b[num] = b[num].apply(pd.to_numeric, errors="coerce").fillna(0)
    b = b.groupby("key", as_index=False)[num].sum()
    mech = b["기계식주차면수"] + b["옥내_기계식_주차대수"] + b["옥외_기계식_주차대수"]
    b["indoor_self"] = (b["옥내_자주식_주차대수"] > 0).astype(int)
    b["mechanical"] = (mech > 0).astype(int)
    b["spaces_bin"] = np.select([b["주차면수"] <= 5, b["주차면수"] <= 20], [0, 1], 2)
    return s, b[["key", *TASKS]]


def lot_points(data_dir: Path, lot_files: list[str], holiday_region: str) -> np.ndarray:
    """주차장 좌표 모음. lot_files 는 위도·경도 열이 있는 CSV, 명절 개방 주차장은 주소 정규식으로 거른다."""
    pts = []
    for name in lot_files:
        pub = pd.read_csv(data_dir / name, dtype=str)
        pts.append(pub[["위도", "경도"]].apply(pd.to_numeric, errors="coerce").dropna().to_numpy())
    hol = pd.read_csv(data_dir / "holiday_open_lots.csv", dtype=str)
    hol = hol[hol["주소"].str.contains(holiday_region, na=False)]
    pts.append(hol[["위도", "경도"]].apply(pd.to_numeric, errors="coerce").dropna().to_numpy())
    return np.vstack(pts)


def floor_code(v: str) -> str:
    v = v.strip()
    if not v:
        return "none"
    if v.startswith(("B", "지하", "-")):
        return "basement"
    if v in ("1", "1층"):
        return "1"
    return "2plus"


def build_features(s: pd.DataFrame, all_food: pd.DataFrame, lots: np.ndarray) -> pd.DataFrame:
    """정답(건축물대장)을 보지 않는 특성만 만든다."""
    rad = np.radians(s[["lat", "lon"]].to_numpy())
    tree = BallTree(np.radians(all_food[["lat", "lon"]].to_numpy()), metric="haversine")
    f = pd.DataFrame(index=s.index)
    for r in (50, 150, 400):
        f[f"food_within_{r}m"] = tree.query_radius(rad, r / EARTH_M, count_only=True)
    lot_tree = BallTree(np.radians(lots), metric="haversine")
    d, _ = lot_tree.query(rad, k=1)
    f["nearest_open_lot_m"] = d[:, 0] * EARTH_M
    f["lots_within_500m"] = lot_tree.query_radius(rad, 500 / EARTH_M, count_only=True)
    f["stores_same_lot"] = s.groupby("key")["key"].transform("size")
    f["has_building_name"] = (s["건물명"] != "").astype(int)
    f["has_ho"] = (s["호정보"] != "").astype(int)
    f["is_mountain_lot"] = (s["대지구분명"] != "대지").astype(int)
    f["is_eupmyeon"] = s["행정동명"].str.endswith(("읍", "면")).astype(int)
    f["has_bubeon"] = (s["지번부번지"] != "0").astype(int)
    f["lat"], f["lon"] = s["lat"], s["lon"]
    cats = {
        "floor": s["층정보"].map(floor_code),
        "sigungu": s["시군구코드"],
        "dong": s["법정동코드"],
        "cat_mid": s["상권업종중분류명"],
        "cat_small": s["상권업종소분류명"],
    }
    for k, v in cats.items():
        f[k] = v.astype("category")
    return f


def metrics(task: str, y: np.ndarray, p: np.ndarray) -> dict:
    if TASKS[task] == "binary":
        out = {"bal_acc": balanced_accuracy_score(y, p >= 0.5)}
        if len(np.unique(y)) > 1:
            out["auc"] = roc_auc_score(y, p)
            out["pr_auc"] = average_precision_score(y, p)
        out["pos_rate"] = float(y.mean())
        return out
    pred = p.argmax(1)
    return {"macro_f1": f1_score(y, pred, average="macro"), "acc": float((pred == y).mean()),
            "bal_acc": balanced_accuracy_score(y, pred)}


def as_proba(task: str, labels: np.ndarray, n_class: int = 3) -> np.ndarray:
    labels = labels.astype(int)
    if TASKS[task] == "binary":
        return labels.astype(float)
    out = np.zeros((len(labels), n_class))
    out[np.arange(len(labels)), labels] = 1.0
    return out


def baselines(task: str, tr: pd.DataFrame, te: pd.DataFrame) -> dict[str, np.ndarray]:
    y = tr[task].to_numpy()
    if TASKS[task] == "binary":
        prior = np.full(len(te), y.mean())
        dong_rate = tr.groupby("법정동코드")[task].mean()
        dong = te["법정동코드"].map(dong_rate).fillna(y.mean()).to_numpy()
    else:
        counts = np.bincount(y, minlength=3) / len(y)
        prior = np.tile(counts, (len(te), 1))
        dist = tr.groupby("법정동코드")[task].apply(lambda v: np.bincount(v, minlength=3) / len(v))
        dong = np.vstack([dist.get(d, counts) for d in te["법정동코드"]])
    # 가장 가까운 train 건물(지번) 5곳의 다수결. 같은 지번은 fold 분할상 train 에 없다.
    lots = tr.drop_duplicates("key")
    tree = BallTree(np.radians(lots[["lat", "lon"]].to_numpy()), metric="haversine")
    _, idx = tree.query(np.radians(te[["lat", "lon"]].to_numpy()), k=5)
    nb = lots[task].to_numpy()[idx]
    if TASKS[task] == "binary":
        knn = nb.mean(1)
    else:
        knn = np.stack([(nb == c).mean(1) for c in range(3)], 1)
    return {"B_prior": prior, "B_dong": dong, "B_knn5": knn}


def lgb_params(task: str, seed: int, threads: int) -> dict:
    p = dict(learning_rate=0.03, num_leaves=31, min_data_in_leaf=40, feature_fraction=0.8,
             bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, max_cat_to_onehot=8,
             cat_smooth=20, seed=seed, deterministic=True, force_row_wise=True,
             num_threads=threads, verbose=-1)
    if TASKS[task] == "binary":
        p.update(objective="binary", metric="auc", is_unbalance=True)
    else:
        p.update(objective="multiclass", num_class=3, metric="multi_logloss")
    return p


def fit_predict(task, Xtr, ytr, Xte, seed, threads, rounds):
    model = lgb.train(lgb_params(task, seed, threads), lgb.Dataset(Xtr, ytr), num_boost_round=rounds)
    return model.predict(Xte), model


def knn_feature(tr: pd.DataFrame, te: pd.DataFrame, task_cols: list[str], exclude_self: bool) -> pd.DataFrame:
    """주변 train 건물의 정답 평균. train 행은 자기 지번을 빼고 계산해 누수를 막는다."""
    lots = tr.drop_duplicates("key").reset_index(drop=True)
    tree = BallTree(np.radians(lots[["lat", "lon"]].to_numpy()), metric="haversine")
    k = 6 if exclude_self else 5
    _, idx = tree.query(np.radians(te[["lat", "lon"]].to_numpy()), k=k)
    out = {}
    keys = lots["key"].to_numpy()
    for col in task_cols:
        vals = lots[col].to_numpy().astype(float)
        if col == "spaces_bin":
            vals = (vals >= 1).astype(float)  # 6면 이상 비율
        nb_vals = vals[idx]
        if exclude_self:
            same = keys[idx] == te["key"].to_numpy()[:, None]
            nb_vals = np.where(same, np.nan, nb_vals)
            nb_vals = np.array([row[~np.isnan(row)][:5] for row in nb_vals], dtype=object)
            out[f"knn5_{col}"] = [float(np.mean(r)) for r in nb_vals]
        else:
            out[f"knn5_{col}"] = nb_vals.mean(1)
    return pd.DataFrame(out, index=te.index)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=400)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--sangga", default="sangga_cheonan_asan.csv.gz")
    ap.add_argument("--buildings", default="building_parking_ca.csv")
    ap.add_argument("--lots", nargs="+", default=["public_lots_cheonan.csv"])
    ap.add_argument("--holiday-region", default="천안|아산", help="명절 개방 주차장 주소 정규식")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    s, b = load_data(args.data, args.sangga, args.buildings)
    lots = lot_points(args.data, args.lots, args.holiday_region)
    print(f"parking lots for features {len(lots)}", flush=True)
    feats = build_features(s, s, lots)
    df = s.join(feats[[c for c in feats.columns if c not in ("lat", "lon")]]).merge(b, on="key", how="left")
    matched = df[df["indoor_self"].notna()].reset_index(drop=True)
    unmatched = df[df["indoor_self"].isna()].reset_index(drop=True)
    for t in TASKS:
        matched[t] = matched[t].astype(int)
    feat_cols = list(feats.columns)
    print(f"food stores {len(df)} matched {len(matched)} ({len(matched)/len(df):.1%}) "
          f"unique lots {matched['key'].nunique()} unmatched {len(unmatched)}", flush=True)

    results = []
    splits = {"group_lot": matched["key"], "group_dong": matched["법정동코드"]}
    for split_name, groups in splits.items():
        for seed in args.seeds:
            # GroupKFold 는 시드가 없어서 그룹 순서를 섞어 시드마다 다른 분할을 만든다.
            rng = np.random.default_rng(seed)
            uniq = groups.unique()
            perm = dict(zip(uniq, rng.permutation(len(uniq))))
            g = groups.map(perm).to_numpy()
            for fold, (tri, tei) in enumerate(GroupKFold(args.folds).split(matched, groups=g)):
                tr, te = matched.iloc[tri], matched.iloc[tei]
                assert not set(tr["key"]) & set(te["key"]), "같은 지번이 train/test 에 함께 있음"
                knn_tr = knn_feature(tr, tr, list(TASKS), exclude_self=True)
                knn_te = knn_feature(tr, te, list(TASKS), exclude_self=False)
                for task in TASKS:
                    yte = te[task].to_numpy()
                    preds = baselines(task, tr, te)
                    preds["LGBM"], _ = fit_predict(task, tr[feat_cols], tr[task], te[feat_cols],
                                                   seed, args.threads, args.rounds)
                    preds["LGBM+knn"], _ = fit_predict(task, tr[feat_cols].join(knn_tr), tr[task],
                                                       te[feat_cols].join(knn_te), seed, args.threads, args.rounds)
                    for name, p in preds.items():
                        results.append({"split": split_name, "seed": seed, "fold": fold, "task": task,
                                        "model": name, "n_test": len(te), **metrics(task, yte, p)})
            print(f"[{time.time()-t0:6.0f}s] {split_name} seed{seed} done", flush=True)

    res = pd.DataFrame(results)
    res.to_csv(args.out / "cv_rows.csv", index=False)
    key_metric = {"indoor_self": "auc", "mechanical": "pr_auc", "spaces_bin": "macro_f1"}
    summary = {}
    for (split, task, model), g in res.groupby(["split", "task", "model"]):
        m = key_metric[task]
        per_seed = g.groupby("seed")[m].mean()
        summary.setdefault(split, {}).setdefault(task, {})[model] = {
            "metric": m, "mean": round(float(per_seed.mean()), 4), "std_over_seeds": round(float(per_seed.std(ddof=0)), 4),
            "bal_acc": round(float(g.groupby("seed")["bal_acc"].mean().mean()), 4)}
    meta = {"n_food": len(df), "n_matched": len(matched), "n_unique_lots": int(matched["key"].nunique()),
            "n_unmatched": len(unmatched), "folds": args.folds, "seeds": args.seeds, "rounds": args.rounds,
            "threads": args.threads, "lightgbm": lgb.__version__, "features": feat_cols,
            "label_rates": {t: matched[t].value_counts(normalize=True).sort_index().round(4).to_dict() for t in TASKS},
            "elapsed_s": round(time.time() - t0, 1)}
    (args.out / "cv_results.json").write_text(json.dumps({"meta": meta, "summary": summary}, ensure_ascii=False, indent=1))

    # 최종 모델: 매칭 전체로 학습해 미매칭 음식점을 예측한다(시드 평균).
    knn_all = knn_feature(matched, matched, list(TASKS), exclude_self=True)
    knn_un = knn_feature(matched, unmatched, list(TASKS), exclude_self=False)
    out = unmatched[["상가업소번호", "상호명", "시군구명", "법정동명", "지번주소", "lat", "lon"]].copy()
    for task in TASKS:
        ps = [fit_predict(task, matched[feat_cols].join(knn_all), matched[task], unmatched[feat_cols].join(knn_un),
                          seed, args.threads, args.rounds)[0] for seed in args.seeds]
        p = np.mean(ps, 0)
        if TASKS[task] == "binary":
            out[f"p_{task}"] = p.round(4)
        else:
            for c, name in enumerate(("0-5", "6-20", "21+")):
                out[f"p_spaces_{name}"] = p[:, c].round(4)
    out.to_csv(args.out / "pred_unmatched.csv", index=False, encoding="utf-8-sig")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"done in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
