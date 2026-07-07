"""Train a LightGBM regression model for E[K | features, pays_off].

Target: fill_fraction = contracts_filled / quantity ∈ [0, 1]
Loss: cross-entropy
Output: E[K] = quantity × predicted_fill_fraction

Validation:
  - Rolling blocked CV across days in the 7-day depth era
  - Final temporal hold-out on day 7

Spec: docs/design/fill-expected-fills-spec.md
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "work" / "training_data_v2.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "work" / "fill_model"


# --- Feature engineering ---


NUMERIC_FEATURES = [
    "quantity",
    "log1p_quantity",
    "same_price_depth",
    "log1p_same_price_depth",
    "better_price_depth",
    "log1p_better_price_depth",
    "gap_to_nearest_populated",
    "distance_to_touch",
    "qty_over_same_depth",
    "qty_over_total_at_or_better",
    "yes_bid", "yes_ask", "spread",
    "volume", "log1p_volume",
    "open_interest", "log1p_open_interest",
    "snapshot_age_seconds",
    "vol_5m_before", "log1p_vol_5m_before",
    "vol_30m_before", "log1p_vol_30m_before",
    "vol_2h_before", "log1p_vol_2h_before",
    "time_since_trade_at_level_seconds",
    "hours_to_close",
    "lifecycle_fraction",
    "hour_of_day",
    "day_of_week",
]
BOOL_FEATURES = [
    "is_level_populated",
    "is_new_best_price",
    "is_at_touch",
    "pays_off",
]
CAT_FEATURES = [
    "side", "generating_process", "topic", "payoff_type",
]


def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    """Derive features from the raw virtual-order CSV."""
    df = df.copy()

    # Parse timestamps
    df["placement_time"] = pd.to_datetime(df["placement_time"], utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True)

    # Target
    df["fill_fraction"] = df["contracts_filled"] / df["quantity"]
    df["fill_fraction"] = df["fill_fraction"].clip(0.0, 1.0)

    # Log-scale versions of skewed features
    for col in ["quantity", "same_price_depth", "better_price_depth",
                "volume", "open_interest",
                "vol_5m_before", "vol_30m_before", "vol_2h_before"]:
        df[f"log1p_{col}"] = np.log1p(df[col].fillna(0).clip(lower=0))

    # Queue-relative size ratios
    df["qty_over_same_depth"] = (
        df["quantity"] / (1.0 + df["same_price_depth"].fillna(0))
    )
    df["qty_over_total_at_or_better"] = (
        df["quantity"] / (
            1.0 + df["same_price_depth"].fillna(0)
            + df["better_price_depth"].fillna(0)
        )
    )

    # Derived time features
    df["hours_to_close"] = (
        (df["close_time"] - df["placement_time"]).dt.total_seconds() / 3600.0
    )
    df["hour_of_day"] = df["placement_time"].dt.hour
    df["day_of_week"] = df["placement_time"].dt.dayofweek

    # Lifecycle fraction: how far through the sampled lifecycle we are.
    # Approximated per-ticker as (placement_time - min_placement) /
    # (close_time - min_placement). With 3 placements at 0.25/0.5/0.75, this
    # is informative; computed from what we have in the CSV.
    g = df.groupby("ticker", observed=False)
    earliest = g["placement_time"].transform("min")
    total = (df["close_time"] - earliest).dt.total_seconds()
    elapsed = (df["placement_time"] - earliest).dt.total_seconds()
    df["lifecycle_fraction"] = np.where(total > 0, elapsed / total, 0.0)

    # Derived booleans
    df["is_at_touch"] = (df["distance_to_touch"].fillna(0) == 0)

    # Categorical dtype
    for c in CAT_FEATURES:
        df[c] = df[c].astype("category")

    return df


def feature_columns() -> tuple[list[str], list[str]]:
    """Return (all_feature_columns, categorical_feature_columns)."""
    all_feats = NUMERIC_FEATURES + BOOL_FEATURES + CAT_FEATURES
    return all_feats, CAT_FEATURES


# --- Data loading and split ---


@dataclass
class Fold:
    train_days: list[int]       # days of the month (1-based index into 04-12..04-18)
    val_day: int
    train_idx: np.ndarray
    val_idx: np.ndarray


def day_index(ts: pd.Timestamp, era_start: datetime) -> int:
    """1-based day-of-era index."""
    return (ts.date() - era_start.date()).days + 1


def make_folds(df: pd.DataFrame, era_start: datetime) -> list[Fold]:
    """Rolling blocked CV folds on the 7-day depth era."""
    day = df["placement_time"].apply(lambda ts: day_index(ts, era_start)).values
    folds = []
    for val_day in (4, 5, 6):
        train_days = list(range(1, val_day))
        train_mask = np.isin(day, train_days)
        val_mask = day == val_day
        folds.append(Fold(
            train_days=train_days, val_day=val_day,
            train_idx=np.where(train_mask)[0],
            val_idx=np.where(val_mask)[0],
        ))
    return folds


# --- Metrics ---


def calibration_stratified(df_eval: pd.DataFrame, pred_col: str,
                           stratum_cols: list[str]) -> pd.DataFrame:
    """Per-stratum predicted vs observed E[K], with support and error."""
    df_eval = df_eval.copy()
    df_eval["pred_K"] = df_eval[pred_col] * df_eval["quantity"]
    g = df_eval.groupby(stratum_cols, observed=True)
    out = g.agg(
        n=("quantity", "size"),
        obs_K=("contracts_filled", "mean"),
        pred_K=("pred_K", "mean"),
    )
    out["abs_err"] = (out["pred_K"] - out["obs_K"]).abs()
    out["rel_err"] = out["abs_err"] / out["obs_K"].clip(lower=1e-6)
    return out


def baseline_mae(df: pd.DataFrame) -> float:
    """Baseline MAE: predict per-quantity mean fill_fraction."""
    means = df.groupby("quantity", observed=False)["fill_fraction"].transform("mean")
    return (df["fill_fraction"] - means).abs().mean()


# --- Training ---


def train_one(df_train: pd.DataFrame, df_val: pd.DataFrame,
              feats: list[str], cat_feats: list[str],
              params: dict, num_boost_round: int = 1000,
              early_stopping: int = 50) -> lgb.Booster:
    """Train a single LightGBM booster on fill_fraction."""
    dtrain = lgb.Dataset(df_train[feats], label=df_train["fill_fraction"],
                         categorical_feature=cat_feats)
    dval = lgb.Dataset(df_val[feats], label=df_val["fill_fraction"],
                       categorical_feature=cat_feats, reference=dtrain)
    booster = lgb.train(
        params, dtrain, num_boost_round=num_boost_round,
        valid_sets=[dtrain, dval], valid_names=["train", "val"],
        callbacks=[lgb.early_stopping(early_stopping),
                   lgb.log_evaluation(100)],
    )
    return booster


# --- Main ---


def run(input_path: Path, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading {input_path}...")
    df = pd.read_csv(input_path)
    print(f"  {len(df):,} rows")

    # Filter: maker orders only
    df = df[~df["would_cross_spread"]].copy()
    print(f"  After excluding crossing orders: {len(df):,}")

    df = prepare_features(df)
    feats, cat_feats = feature_columns()

    # Depth era starts 2026-04-12
    era_start = datetime(2026, 4, 12, tzinfo=timezone.utc)
    df["day_idx"] = df["placement_time"].apply(
        lambda ts: day_index(ts, era_start))
    day_counts = df["day_idx"].value_counts().sort_index()
    print("\nRows per day:")
    for d, n in day_counts.items():
        print(f"  day {d} ({(era_start + timedelta(days=d-1)).date()}): {n:,}")

    # Test hold-out
    test_mask = df["day_idx"] == 7
    dev_mask = df["day_idx"] <= 6
    df_dev = df[dev_mask].copy()
    df_test = df[test_mask].copy()
    print(f"\nDev set (days 1-6): {len(df_dev):,}")
    print(f"Test set (day 7):    {len(df_test):,}")

    # Rolling CV
    params = dict(
        objective="cross_entropy",
        metric=["cross_entropy", "mae"],
        num_leaves=63,
        learning_rate=0.05,
        min_data_in_leaf=50,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=5,
        verbose=-1,
    )

    print("\n=== Rolling CV ===")
    folds = make_folds(df_dev, era_start)
    fold_stats = []
    for i, fold in enumerate(folds):
        print(f"\nFold {i+1}: train days {fold.train_days} val day {fold.val_day}")
        df_tr = df_dev.iloc[fold.train_idx]
        df_va = df_dev.iloc[fold.val_idx]
        print(f"  train={len(df_tr):,}, val={len(df_va):,}")
        booster = train_one(df_tr, df_va, feats, cat_feats, params)
        preds_va = booster.predict(df_va[feats], num_iteration=booster.best_iteration)
        mae = np.abs(preds_va - df_va["fill_fraction"].values).mean()
        base = baseline_mae(df_va)
        improvement = (base - mae) / base * 100 if base > 0 else 0
        print(f"  MAE={mae:.4f} (baseline {base:.4f}, improvement {improvement:.1f}%)")
        fold_stats.append({
            "fold": i+1, "val_day": fold.val_day,
            "n_train": len(df_tr), "n_val": len(df_va),
            "mae": mae, "baseline_mae": base, "improvement_pct": improvement,
            "best_iter": booster.best_iteration,
        })

    print("\n=== Rolling CV summary ===")
    print(pd.DataFrame(fold_stats).to_string(index=False))

    # Final training on days 1-6
    print("\n=== Final training on days 1-6, test on day 7 ===")
    # Use average best iteration from folds
    best_iter_avg = int(np.mean([s["best_iter"] for s in fold_stats]))
    print(f"Using {best_iter_avg} iterations (avg from folds)")
    # Simple: train on dev, use last fold's val as validation sentinel
    last_fold = folds[-1]
    df_tr_final = df_dev.iloc[last_fold.train_idx]
    df_va_final = df_dev.iloc[last_fold.val_idx]
    booster = train_one(df_tr_final, df_va_final, feats, cat_feats, params,
                        num_boost_round=best_iter_avg, early_stopping=100)

    # Test evaluation
    preds_test = booster.predict(df_test[feats], num_iteration=booster.best_iteration)
    df_test = df_test.copy()
    df_test["pred_fill_fraction"] = preds_test
    df_test["pred_K"] = df_test["pred_fill_fraction"] * df_test["quantity"]

    mae_test = np.abs(preds_test - df_test["fill_fraction"].values).mean()
    base_test = baseline_mae(df_test)
    print("\n=== Test (day 7) metrics ===")
    print(f"MAE: {mae_test:.4f}  (baseline {base_test:.4f}, "
          f"improvement {100*(base_test-mae_test)/base_test:.1f}%)")

    # Outcome-conditional gap
    gap_yes = (
        df_test[df_test["side"] == "yes"].groupby("pays_off", observed=False)["pred_K"].mean()
    )
    gap_no = (
        df_test[df_test["side"] == "no"].groupby("pays_off", observed=False)["pred_K"].mean()
    )
    obs_gap_yes = (
        df_test[df_test["side"] == "yes"].groupby("pays_off", observed=False)["contracts_filled"].mean()
    )
    obs_gap_no = (
        df_test[df_test["side"] == "no"].groupby("pays_off", observed=False)["contracts_filled"].mean()
    )
    print("\n=== Outcome-conditional E[K] (test) ===")
    print(f"YES side  predicted: {dict(gap_yes.round(2))}")
    print(f"YES side  observed:  {dict(obs_gap_yes.round(2))}")
    print(f"NO  side  predicted: {dict(gap_no.round(2))}")
    print(f"NO  side  observed:  {dict(obs_gap_no.round(2))}")

    # Stratified calibration
    print("\n=== Calibration by (side × pays_off × quantity) ===")
    cal = calibration_stratified(df_test, "pred_fill_fraction",
                                 ["side", "pays_off", "quantity"])
    cal = cal[cal["n"] >= 50]   # minimum support
    print(cal.round(3).to_string())

    # Feature importance
    print("\n=== Top 20 features by importance ===")
    imp = pd.DataFrame({
        "feature": booster.feature_name(),
        "gain": booster.feature_importance(importance_type="gain"),
    }).sort_values("gain", ascending=False)
    print(imp.head(20).to_string(index=False))

    # Save
    model_path = output_dir / "fill_fraction_model.txt"
    booster.save_model(str(model_path))
    print(f"\nSaved model to {model_path}")

    metadata = {
        "spec": "docs/design/fill-expected-fills-spec.md",
        "target": "fill_fraction",
        "features": feats,
        "categorical_features": cat_feats,
        "hyperparameters": params,
        "training_data": str(input_path),
        "n_train": int(len(df_tr_final)),
        "n_test": int(len(df_test)),
        "best_iteration": int(booster.best_iteration),
        "test_mae": float(mae_test),
        "baseline_mae": float(base_test),
        "improvement_pct": float(100 * (base_test - mae_test) / base_test),
        "fold_stats": fold_stats,
        "trained_at": datetime.utcnow().isoformat() + "Z",
    }
    meta_path = output_dir / "fill_fraction_model.metadata.json"
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"Saved metadata to {meta_path}")

    return booster, df_test


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    run(args.input, args.output_dir)
