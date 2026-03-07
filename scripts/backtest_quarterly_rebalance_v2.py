# backtest_quarterly_rebalance_v2.py
# - quarterly rebalance backtest (factor score -> pick top-k -> monthly NAV)
# - supports returns_monthly schema: month OR month_end
# - supports strategies yaml shapes: weights dict OR legacy components list
# - optional audit dumps: holdings_snapshot, inputs_long

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple, Any, Optional

import numpy as np
import pandas as pd

import json
import subprocess

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

# -----------------------------
# Utils
# -----------------------------

def zscore_safe(s: pd.Series) -> pd.Series:
    """
    Debug/robustness oriented zscore:
      - numeric coercion
      - inf -> NaN
      - sd==0 or all-NaN -> zeros
      - final NaN/inf -> 0
    기존 기능(스코어링) 유지하면서 RuntimeWarning/inf 전파를 차단.
    """
    x = pd.to_numeric(s, errors="coerce").astype("float64")
    x = x.replace([np.inf, -np.inf], np.nan)

    mu = x.mean(skipna=True)
    sd = x.std(skipna=True)

    if not np.isfinite(sd) or sd == 0.0:
        return pd.Series(np.zeros(len(x)), index=x.index, dtype="float64")

    z = (x - mu) / sd
    z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return z.astype("float64")

def safe_fill_for_z(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    if s.notna().any():
        med = s.median(skipna=True)
        return s.fillna(med)
    return s.fillna(0.0)

def ym_to_month_end(ts: pd.Timestamp) -> pd.Timestamp:
    # force month-end timestamp
    p = ts.to_period("M")
    return p.to_timestamp("M")

def ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Strategy loading
# -----------------------------

def load_strategy(p_yaml: Path, name: str) -> Tuple[Dict[str, float], Dict[str, Any], str]:
    """
    Supported YAML shapes:
      - strategies: { NAME: {weights: {...}, filters: {...}, desc: "..."} }
      - strategies: { NAME: {components: [...], filters: {...}} }  # legacy
      - strategies: { NAME: {...weights directly...} }              # minimal
    Returns: (weights, filters, desc)
    """
    if yaml is None:
        raise RuntimeError("pyyaml not installed. Run: pip install pyyaml")
    cfg = yaml.safe_load(p_yaml.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or "strategies" not in cfg:
        raise ValueError(f"Invalid strategies yaml: missing top-level 'strategies'. file={p_yaml}")

    if name not in cfg["strategies"]:
        avail = list(cfg["strategies"].keys())
        raise KeyError(f"Strategy '{name}' not found in {p_yaml}. Available (first 30): {avail[:30]}")

    s = cfg["strategies"][name] or {}
    if not isinstance(s, dict):
        raise ValueError(f"Strategy '{name}' must be a dict in yaml. Got: {type(s)}")

    filters = s.get("filters", {}) if isinstance(s.get("filters", {}), dict) else {}
    desc = s.get("desc", "") if isinstance(s.get("desc", ""), str) else ""

    weights: Optional[Dict[str, float]] = None

    # Preferred: weights dict
    if isinstance(s.get("weights", None), dict):
        weights = {str(k): float(v) for k, v in s["weights"].items()}

    # Legacy: components list -> convert
    if weights is None and isinstance(s.get("components", None), list):
        weights = {}
        for comp in s["components"]:
            if not isinstance(comp, dict):
                continue
            col = comp.get("col") or comp.get("feature") or comp.get("name")
            w = comp.get("w") if "w" in comp else comp.get("weight", 0.0)
            if col is None:
                continue
            try:
                weights[str(col)] = float(w) if w is not None else 0.0
            except Exception:
                weights[str(col)] = 0.0

    # Minimal: dict of weights directly
    if weights is None:
        maybe = {k: v for k, v in s.items() if k not in {"filters", "desc", "weights", "components"}}
        if maybe and all(isinstance(v, (int, float)) for v in maybe.values()):
            weights = {str(k): float(v) for k, v in maybe.items()}

    if not weights or not isinstance(weights, dict):
        raise ValueError("strategy config has no usable 'weights' (or legacy 'components').")

    # drop zeros
    weights = {k: float(v) for k, v in weights.items() if float(v) != 0.0}
    if not weights:
        raise ValueError("strategy weights are empty (all zeros).")

    return weights, filters, desc

# -----------------------------
# Filters
# -----------------------------

def apply_filters(df: pd.DataFrame, filters: Dict[str, Any]) -> pd.DataFrame:
    if not filters:
        return df

    out = df
    for k, v in filters.items():
        # common patterns:
        #   max_Debt_to_Equity: 2.0
        #   min_Revenue_ttm: 0
        #   max_Debt_to_Equity_log: 1.5
        #   allow_if_missing: [some cols]
        if k.startswith("max_"):
            col = k[len("max_"):]
            if col in out.columns:
                out = out[out[col].isna() | (out[col] <= float(v))]
        elif k.startswith("min_"):
            col = k[len("min_"):]
            if col in out.columns:
                out = out[out[col].isna() | (out[col] >= float(v))]
        # ignore unknown keys safely
    return out

# -----------------------------
# Main
# -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--ret_v", type=int, required=True)
    ap.add_argument("--ret_src", default="pykrx", help="returns_monthly src in filename (pykrx|fdr).")
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--out_v", type=int, default=1)

    ap.add_argument("--tcost_bps", type=float, default=30.0)
    ap.add_argument("--hold_bonus", type=float, default=0.0)
    ap.add_argument("--entry_gap", type=float, default=0.0)

    ap.add_argument("--save_holdings", action="store_true")
    ap.add_argument("--save_inputs", action="store_true")

    args = ap.parse_args()

    p_feat = Path(rf"data/processed/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet")
    p_ret  = Path(rf"data/processed/returns_monthly__src={args.ret_src}__asof={args.asof}__metric={args.metric}__v={args.ret_v}.parquet")

    out_bt   = Path(rf"data/processed/bt__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.csv")
    out_pick = Path(rf"data/processed/picks__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.parquet")
    out_rep  = Path(rf"data/processed/report__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.csv")
    out_hold = Path(rf"data/processed/holdings_snapshot__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.csv")
    out_inputs = Path(rf"data/processed/inputs_long__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.parquet")

    ensure_dir(out_bt)
    ensure_dir(out_pick)
    ensure_dir(out_rep)
    out_meta = Path(rf"data/processed/report_meta__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.json")
    ensure_dir(out_meta)

    if not p_feat.exists():
        raise FileNotFoundError(p_feat)
    if not p_ret.exists():
        raise FileNotFoundError(p_ret)

    feat = pd.read_parquet(p_feat)
    ret = pd.read_parquet(p_ret)

    print(f"[OK] returns loaded: {p_ret}")

    def _git_hash() -> str:
        try:
            h = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
            return h
        except Exception:
            return "NA"

    meta = {
        "asof": args.asof,
        "metric": args.metric,
        "strategy": args.strategy,
        "k": int(args.k),
        "feat_v": int(args.feat_v),
        "ret_v": int(args.ret_v),
        "ret_src": str(args.ret_src),
        "tcost_bps": float(args.tcost_bps),
        "hold_bonus": float(args.hold_bonus),
        "entry_gap": float(args.entry_gap),
        "features_path": p_feat.as_posix(),
        "returns_path": p_ret.as_posix(),
        "git_hash": _git_hash(),
        "features_cols": list(map(str, feat.columns)),
        "returns_cols": list(map(str, ret.columns)),
        "features_rows": int(len(feat)),
        "returns_rows": int(len(ret)),
    }
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] report_meta saved: {out_meta}")

    # --- prep returns (canonicalize to month_end + ret_1m) ---
    if "month_end" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month_end"]).dt.normalize()
    elif "month" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month"]).dt.normalize()
    else:
        raise ValueError(f"returns_monthly missing month column. Need month_end (preferred) or month. Have: {list(ret.columns)}")

    if "ret_1m" not in ret.columns:
        if "ret" in ret.columns:
            ret["ret_1m"] = pd.to_numeric(ret["ret"], errors="coerce")
        else:
            raise ValueError(f"returns_monthly missing ret column. Need ret_1m (preferred) or ret. Have: {list(ret.columns)}")

    ret["ticker"] = ret["ticker"].astype(str)
    ret["ret_1m"] = pd.to_numeric(ret["ret_1m"], errors="coerce")

    # keep canonical cols only for stability
    ret = ret[["ticker", "month_end", "ret_1m"]].dropna(subset=["month_end"]).copy()
    ret = ret.sort_values(["ticker", "month_end"]).reset_index(drop=True)

    # --- prep features ---
    # name mapping (audit)
    name_map = None
    if "name" in feat.columns:
        name_map = feat[["ticker", "name"]].drop_duplicates("ticker")

    feat["quarter_key"] = feat["year"].astype(int) * 100 + feat["quarter"].astype(int)

    # rebalance month = quarter end + 45 days, then month-end
    # quarter end months: 3,6,9,12
    q_end_month = feat["quarter"].map({1: 3, 2: 6, 3: 9, 4: 12}).astype(int)
    q_end = pd.to_datetime(
        feat["year"].astype(str) + "-" + q_end_month.astype(str).str.zfill(2) + "-01"
    ).dt.to_period("M").dt.to_timestamp("M")
    q_end_series = pd.Series(q_end, index=feat.index)
    rebalance = (q_end_series.apply(lambda x: x + pd.Timedelta(days=45))).apply(ym_to_month_end)
    feat["rebalance_month"] = rebalance

    # strategy yaml path: prefer configs/strategies.yaml, fallback strategies.yaml
    strat_path = Path("configs/strategies.yaml")
    if not strat_path.exists():
        strat_path = Path("strategies.yaml")
    if not strat_path.exists():
        raise FileNotFoundError(strat_path)

    weights, filters, desc = load_strategy(strat_path, args.strategy)
    print(f"[OK] strategies_yaml: {strat_path}")
    if desc:
        print(f"[INFO] strategy desc: {desc}")

    # ---- scoring per rebalance month ----
    def make_scores(g: pd.DataFrame, rm: pd.Timestamp) -> pd.DataFrame:
        gg = g.copy()
        gg = apply_filters(gg, filters)
        if len(gg) == 0:
            gg = g.copy()  # fallback to avoid empty portfolio

        gg["score_total"] = 0.0
        used_cols = []

        # (optional) 마스킹 기반 가중치 조절: CFO 관련 컬럼이면 CFO_isnull/CFO_warn 반영
        def _mask_mult_for(col: str) -> pd.Series:
            if ("CFO" in col) or ("Quality_CFO" in col) or ("CFO_to_Assets" in col):
                if "CFO_isnull" in gg.columns:
                    isnull = pd.to_numeric(gg["CFO_isnull"], errors="coerce").fillna(0.0)
                else:
                    isnull = 0.0
                if "CFO_warn" in gg.columns:
                    warn = pd.to_numeric(gg["CFO_warn"], errors="coerce").fillna(0.0)
                else:
                    warn = 0.0
                mult = (1.0 - 0.7 * isnull - 0.4 * warn).clip(0.0, 1.0)
                return mult.astype("float64")
            return pd.Series(1.0, index=gg.index, dtype="float64")

        for c, ww in weights.items():
            ww = float(ww)
            if ww == 0.0:
                continue
            if c not in gg.columns:
                continue

            used_cols.append(c)

            raw = pd.to_numeric(gg[c], errors="coerce")
            filled = safe_fill_for_z(raw)
            z = zscore_safe(filled)
            rk = z.rank(ascending=False, method="min")  # z 기준 rank

            mult = _mask_mult_for(c)
            contrib = ww * z * mult

            # 근거 컬럼 남기기
            gg[f"{c}__raw"] = raw
            gg[f"{c}__z"] = z
            gg[f"{c}__rank"] = rk
            gg[f"{c}__mult"] = mult
            gg[f"{c}__contrib"] = contrib

            gg["score_total"] += contrib

        gg["score_rank"] = gg["score_total"].rank(ascending=False, method="min")
        gg["rebalance_month"] = rm

        keep = ["ticker", "rebalance_month", "score_total", "score_rank"]
        # audit: factor evidence
        for c in used_cols:
            for suf in ("__raw", "__z", "__rank", "__mult", "__contrib"):
                col = f"{c}{suf}"
                if col in gg.columns:
                    keep.append(col)

        # metadata cols (if present)
        for c in ["name", "corp_code", "year", "quarter", "CFO_isnull", "CFO_warn"]:
            if c in gg.columns and c not in keep:
                keep.insert(1, c)

        return gg[keep]

    # ---- hysteresis bonus / entry gap ----
    def apply_hysteresis(scored: pd.DataFrame, prev_hold: Optional[set]) -> pd.DataFrame:
        out = scored.copy()
        out["score_adj"] = out["score"]
        if prev_hold and args.hold_bonus != 0:
            out.loc[out["ticker"].isin(prev_hold), "score_adj"] += float(args.hold_bonus)
        return out

    holdings_rows = []
    inputs_rows = []

    all_picks = []
    port_rows = []
    prev_hold = None

    months = sorted(ret["month_end"].dropna().unique())
    months = [pd.Timestamp(m) for m in months]

    # rebalance months in returns range
    rb_months = sorted(feat["rebalance_month"].dropna().unique())
    rb_months = [pd.Timestamp(m) for m in rb_months if (m >= months[0]) and (m <= months[-1])]
    if not rb_months:
        raise RuntimeError("No rebalance months overlap between features and returns range.")

    for rm in rb_months:
        g = feat[feat["rebalance_month"] == rm].copy()
        if len(g) == 0:
            continue

        scored = make_scores(g, rm)  # has ticker, score_total + evidence cols

        # hysteresis uses score_total as base
        scored = scored.copy()
        scored["score"] = scored["score_total"]  # for compatibility with existing hysteresis logic

        scored = apply_hysteresis(scored, prev_hold)

        # entry gap: require new names to beat worst-held by gap (simple)
        scored = scored.sort_values("score_adj", ascending=False).reset_index(drop=True)
        picked = scored.head(args.k).copy()

        # alias for downstream compatibility (picks/holdings 등)
        picked["rebalance_month_end"] = picked["rebalance_month"]

        # ---- inputs_long (audit) : save ONLY selected tickers with full evidence ----
        if args.save_inputs:
            tmp = picked.copy()
            tmp["selected"] = 1
            tmp["asof"] = args.asof
            tmp["metric"] = args.metric
            tmp["strategy"] = args.strategy
            tmp["k"] = int(args.k)

            # rebalance_month_end 컬럼을 "유일하게" 보장 (중복 라벨 방지)
            if "rebalance_month_end" in tmp.columns:
                tmp = tmp.drop(columns=["rebalance_month_end"])
            tmp["rebalance_month_end"] = tmp["rebalance_month"]

            if name_map is not None and "name" not in tmp.columns:
                tmp = tmp.merge(name_map, on="ticker", how="left")

            inputs_rows.append(tmp)

        if args.entry_gap > 0 and prev_hold:
            # ensure continuity: if too many newcomers, enforce gap vs last held threshold
            keepers = picked[picked["ticker"].isin(prev_hold)].copy()
            newcomers = picked[~picked["ticker"].isin(prev_hold)].copy()

            if len(newcomers) > 0 and len(keepers) > 0:
                cutoff = keepers["score_adj"].min()
                newcomers = newcomers[newcomers["score_adj"] >= (cutoff + float(args.entry_gap))]
                picked = pd.concat([keepers, newcomers], ignore_index=True).sort_values("score_adj", ascending=False).head(args.k)

            # if still short, refill by top score_adj
            if len(picked) < args.k:
                picked = scored.head(args.k).copy()
                picked["rebalance_month_end"] = picked["rebalance_month"]

        picked["w"] = 1.0 / args.k

        if args.save_holdings:
            h = picked.copy()
            if name_map is not None and "name" not in h.columns:
                h = h.merge(name_map, on="ticker", how="left")
            holdings_rows.append(h)

        prev_hold = set(picked["ticker"].tolist())

        # store picks
        all_picks.append(
            picked[["ticker", "rebalance_month_end", "w", "score", "score_adj"]]
            .rename(columns={"rebalance_month_end": "rebalance_month"})
        )

    picks = pd.concat(all_picks, ignore_index=True)
    picks.to_parquet(out_pick, index=False)
    print(f"[OK] picks saved: {out_pick}")

    # optional audit dumps
    if args.save_holdings and holdings_rows:
        hold_df = pd.concat(holdings_rows, ignore_index=True)
        hold_df.to_csv(out_hold, index=False, encoding="utf-8-sig")
        print(f"[OK] holdings snapshot saved: {out_hold}")
    if args.save_inputs and inputs_rows:
        exp_df = pd.concat(inputs_rows, ignore_index=True)

        # attach realized monthly return at rebalance_month_end
        exp_df = exp_df.merge(
            ret[["ticker", "month_end", "ret_1m"]],
            left_on=["ticker", "rebalance_month_end"],
            right_on=["ticker", "month_end"],
            how="left",
        ).drop(columns=["month_end"])

        exp_df.to_parquet(out_inputs, index=False)
        print(f"[OK] inputs_long saved: {out_inputs}")

    # ---- backtest NAV ----
    # monthly portfolio return = sum(w_i * ret_1m_i)
    ret2 = ret[["ticker", "month_end", "ret_1m"]].copy()
    ret2["ret_1m"] = pd.to_numeric(ret2["ret_1m"], errors="coerce").fillna(0.0)

    # align: for each month, use the latest rebalance <= month
    rb_series = pd.Series(sorted(picks["rebalance_month"].unique()))
    rb_series = rb_series.sort_values()

    def last_rb(m: pd.Timestamp) -> pd.Timestamp:
        idx = rb_series.searchsorted(m, side="right") - 1
        if idx < 0:
            return rb_series.iloc[0]
        return rb_series.iloc[int(idx)]

    port_rows = []
    nav_gross = 1.0
    nav_net = 1.0

    for m in months:
        rb = last_rb(m)
        w = picks[picks["rebalance_month"] == rb][["ticker", "w"]].copy()
        if len(w) == 0:
            continue

        r = ret2[ret2["month_end"] == m].merge(w, on="ticker", how="inner")
        if len(r) == 0:
            port_ret = 0.0
        else:
            port_ret = float((r["w"] * r["ret_1m"]).sum())

        # turnover cost at rebalance month only
        tcost = 0.0
        if args.tcost_bps and m == rb:
            tcost = float(args.tcost_bps) / 10000.0

        nav_gross *= (1.0 + port_ret)
        nav_net *= (1.0 + port_ret - tcost)

        port_rows.append({
            "month_end": m,
            "rebalance_month": rb,
            "ret": port_ret,
            "tcost": tcost,
            "nav_gross": nav_gross,
            "nav_net": nav_net
        })

    bt = pd.DataFrame(port_rows)
    bt["month"] = bt["month_end"]  # legacy compatibility
    bt.to_csv(out_bt, index=False, encoding="utf-8-sig")
    print(f"[OK] bt saved: {out_bt}")

    # ---- report ----
    if len(bt) > 0:
        n_months = len(bt)
        years = n_months / 12.0
        cagr_g = (bt["nav_gross"].iloc[-1] ** (1.0 / years) - 1.0) if years > 0 else np.nan
        cagr_n = (bt["nav_net"].iloc[-1] ** (1.0 / years) - 1.0) if years > 0 else np.nan

        rets = bt["ret"].astype(float)
        mu = rets.mean()
        sd = rets.std()
        sharpe = (mu / sd * np.sqrt(12.0)) if (sd and np.isfinite(sd) and sd > 0) else np.nan

        rep = pd.DataFrame([{
            "asof": args.asof,
            "metric": args.metric,
            "strategy": args.strategy,
            "k": args.k,
            "tcost_bps": args.tcost_bps,
            "gross_nav": float(bt["nav_gross"].iloc[-1]),
            "net_nav": float(bt["nav_net"].iloc[-1]),
            "cagr_gross": float(cagr_g),
            "cagr_net": float(cagr_n),
            "sharpe_net": float(sharpe),
        }])
    else:
        rep = pd.DataFrame([{
            "asof": args.asof,
            "metric": args.metric,
            "strategy": args.strategy,
            "k": args.k,
            "tcost_bps": args.tcost_bps,
            "gross_nav": np.nan,
            "net_nav": np.nan,
            "cagr_gross": np.nan,
            "cagr_net": np.nan,
            "sharpe_net": np.nan,
        }])

    rep.to_csv(out_rep, index=False, encoding="utf-8-sig")
    print(f"[OK] report saved: {out_rep}")

    if len(bt) > 0:
        print(
            f"[INFO] gross NAV: {bt['nav_gross'].iloc[-1]} "
            f"net NAV: {bt['nav_net'].iloc[-1]} "
            f"CAGR gross: {rep['cagr_gross'].iloc[0]} "
            f"CAGR net: {rep['cagr_net'].iloc[0]} "
            f"Sharpe net: {rep['sharpe_net'].iloc[0]}"
        )

if __name__ == "__main__":
    main()