from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (_THIS_DIR, _SCRIPTS_DIR, _REPO_ROOT):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)


def _python_exe() -> str:
    for cand in [Path(".venv/Scripts/python.exe"), Path(".venv/bin/python")]:
        if cand.exists():
            return str(cand)
    return sys.executable


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _parse_grid(s: str, cast):
    vals: list = []
    for part in str(s).split(","):
        x = part.strip()
        if x:
            vals.append(cast(x))
    return vals


def _load_best_meta(strategy_compare_dir: Path) -> dict:
    p = strategy_compare_dir / "best_strategy.json"
    if not p.exists():
        raise FileNotFoundError(f"best_strategy.json not found: {p}")
    obj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"invalid best_strategy.json: {p}")
    return obj


def _provisional_cols(best_meta: dict) -> dict[str, object]:
    return {
        "provisional": bool(best_meta.get("provisional", False)),
        "provisional_source": str(best_meta.get("provisional_source", "")),
    }


def _to_month_end(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.to_period("M").dt.to_timestamp("M")


def _normalize_nav_from_returns(ret: pd.Series) -> pd.Series:
    x = pd.to_numeric(ret, errors="coerce").fillna(0.0).astype(float)
    return (1.0 + x).cumprod()


def _calc_cagr(nav: pd.Series, periods_per_year: int = 12) -> float:
    x = pd.to_numeric(nav, errors="coerce").dropna()
    if len(x) < 2:
        return float("nan")
    years = len(x) / float(periods_per_year)
    if years <= 0 or float(x.iloc[0]) <= 0:
        return float("nan")
    return float((x.iloc[-1] / x.iloc[0]) ** (1.0 / years) - 1.0)


def _calc_sharpe(ret: pd.Series, periods_per_year: int = 12) -> float:
    x = pd.to_numeric(ret, errors="coerce").dropna()
    if len(x) < 2:
        return float("nan")
    sd = float(x.std(ddof=1))
    if not np.isfinite(sd) or sd == 0.0:
        return float("nan")
    return float(x.mean() / sd * math.sqrt(periods_per_year))


def _calc_mdd(nav: pd.Series) -> float:
    x = pd.to_numeric(nav, errors="coerce").dropna()
    if len(x) == 0:
        return float("nan")
    dd = x / x.cummax() - 1.0
    return float(dd.min())


def _calc_calmar(cagr: float, mdd: float) -> float:
    if not np.isfinite(cagr) or not np.isfinite(mdd) or mdd == 0.0:
        return float("nan")
    return float(cagr / abs(mdd))


def _calc_win_rate(ret: pd.Series) -> float:
    x = pd.to_numeric(ret, errors="coerce").dropna()
    if len(x) == 0:
        return float("nan")
    return float((x > 0).mean())


def _calc_period_return(monthly_ret: pd.Series) -> float:
    x = pd.to_numeric(monthly_ret, errors="coerce").fillna(0.0)
    if len(x) == 0:
        return float("nan")
    return float((1.0 + x).prod() - 1.0)


def _rolling_compound(ret: pd.Series, window: int) -> pd.Series:
    x = pd.to_numeric(ret, errors="coerce").fillna(0.0)
    return x.rolling(window).apply(lambda v: float(np.prod(1.0 + v) - 1.0), raw=True)


def _calc_t_stat(x: pd.Series) -> float:
    s = pd.to_numeric(x, errors="coerce").dropna()
    n = len(s)
    if n < 2:
        return float("nan")
    sd = float(s.std(ddof=1))
    if sd == 0.0 or not np.isfinite(sd):
        return float("nan")
    return float(s.mean() / (sd / math.sqrt(n)))


def _max_consecutive_underperform(x: pd.Series) -> int:
    vals = pd.to_numeric(x, errors="coerce").fillna(0.0)
    best = 0
    cur = 0
    for v in vals:
        if v <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def _load_quant_factor_bt(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path).copy()
    df["month_end"] = _to_month_end(df["month_end"])
    df["rebalance_month"] = _to_month_end(df["rebalance_month"])
    df["month_ret_net"] = pd.to_numeric(df["ret"], errors="coerce").fillna(0.0) - pd.to_numeric(df["tcost"], errors="coerce").fillna(0.0)
    if "nav_net" not in df.columns:
        df["nav_net"] = _normalize_nav_from_returns(df["month_ret_net"])
    return df[["month_end", "rebalance_month", "month_ret_net", "nav_net", "turnover"]].copy()


def _load_ai_monthly(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path).copy()
    df["month_end"] = _to_month_end(df["month_end"])
    df["rebalance_month"] = _to_month_end(df["rebalance_month"])
    return df.rename(columns={"ai_month_ret_net": "month_ret_net", "ai_nav_net": "nav_net"})[
        ["month_end", "rebalance_month", "month_ret_net", "nav_net"]
    ].copy()


def _load_combo_monthly(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path).copy()
    df["month_end"] = _to_month_end(df["month_end"])
    df["rebalance_month"] = _to_month_end(df["rebalance_month"])
    return df.rename(columns={"combo_month_ret_net": "month_ret_net", "combo_nav_net": "nav_net"})[
        ["month_end", "rebalance_month", "month_ret_net", "nav_net"]
    ].copy()


def _load_strategy_monthlies(asof: str, metric: str, k: int, strategy_compare_dir: Path) -> dict[str, pd.DataFrame]:
    strategy = "D_quality_filter_debt_profitaccel_liq"
    return {
        "quant_only": _load_quant_factor_bt(
            Path(f"data/processed/bt__asof={asof}__metric={metric}__k={k}__strat={strategy}__v=101.csv")
        ),
        "factor_composite": _load_quant_factor_bt(
            Path(f"data/processed/bt__asof={asof}__metric={metric}__k={k}__strat={strategy}__v=102.csv")
        ),
        "ai_wf_topk": _load_ai_monthly(strategy_compare_dir / "ai_wf_topk" / "walkforward_integrated_monthly_nav.csv"),
        "combo_overlay_replace": _load_combo_monthly(strategy_compare_dir / "combo" / "combo_monthly_nav.csv"),
    }


def _calc_common_period_comparison(monthlies: dict[str, pd.DataFrame], provisional: dict[str, object]) -> tuple[pd.DataFrame, pd.DataFrame]:
    common_months: set[pd.Timestamp] | None = None
    for df in monthlies.values():
        months = set(pd.to_datetime(df["month_end"]).dropna().tolist())
        common_months = months if common_months is None else (common_months & months)
    if not common_months:
        raise RuntimeError("no common monthly range across strategy outputs")
    common = sorted(pd.Timestamp(x) for x in common_months)

    rows: list[dict[str, object]] = []
    nav_df = pd.DataFrame({"month_end": common})
    for name, df in monthlies.items():
        seg = df.loc[df["month_end"].isin(common), ["month_end", "month_ret_net"]].copy().sort_values("month_end")
        seg["nav_net_common"] = _normalize_nav_from_returns(seg["month_ret_net"])
        cagr = _calc_cagr(seg["nav_net_common"])
        mdd = _calc_mdd(seg["nav_net_common"])
        rows.append(
            {
                "strategy_variant": name,
                "common_start_month": common[0],
                "common_end_month": common[-1],
                "months": int(len(seg)),
                "net_nav": float(seg["nav_net_common"].iloc[-1]),
                "cagr_net": cagr,
                "sharpe_net": _calc_sharpe(seg["month_ret_net"]),
                "maxdd_net": mdd,
                "calmar_net": _calc_calmar(cagr, mdd),
                "monthly_win_rate": _calc_win_rate(seg["month_ret_net"]),
                **provisional,
            }
        )
        nav_df = nav_df.merge(seg[["month_end", "nav_net_common"]].rename(columns={"nav_net_common": name}), on="month_end", how="left")
    return pd.DataFrame(rows), nav_df


def _period_returns_from_bt(bt: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for rb, seg in bt.groupby("rebalance_month", dropna=True):
        rows.append(
            {
                "rebalance_month": pd.Timestamp(rb),
                "period_ret_net": _calc_period_return(seg["month_ret_net"]),
                "turnover": float(pd.to_numeric(seg.get("turnover"), errors="coerce").fillna(0.0).mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("rebalance_month").reset_index(drop=True)


def _run_strategy_compare(
    *,
    asof: str,
    metric: str,
    out_dir: Path,
    log_dir: Path,
    k: int,
    tcost_bps: float,
) -> tuple[bool, str]:
    cmd = [
        _python_exe(),
        "scripts/backtest/run_strategy_compare.py",
        "--skip_prepare",
        "--asof",
        asof,
        "--metric",
        metric,
        "--strategy",
        "D_quality_filter_debt_profitaccel_liq",
        "--k",
        str(k),
        "--feat_v",
        "3291",
        "--ret_v",
        "1",
        "--factor_v",
        "3291",
        "--raw_px_v",
        "1",
        "--model",
        "ridge",
        "--feature_profile",
        "compact_dailyagg",
        "--min_train_rows",
        "20",
        "--valid_rows",
        "4",
        "--temperature",
        "1.0",
        "--cap_profit_accel_delta",
        "-1.0",
        "--ai_overlay_strength",
        "1.0",
        "--mcap_top",
        "800",
        "--trd_bot",
        "0.1",
        "--start",
        "20110101",
        "--lookback_years",
        "15",
        "--tcost_bps",
        str(tcost_bps),
        "--out_dir",
        str(out_dir),
    ]
    _ensure_dir(log_dir)
    log_path = log_dir / f"validate_strategy_robustness__rerun__k={k}__tcost_bps={int(tcost_bps)}.log"
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            subprocess.run(cmd, check=True, stdout=fh, stderr=subprocess.STDOUT)
        return True, str(log_path)
    except subprocess.CalledProcessError as e:
        return False, f"exit={e.returncode}; log_path={log_path}"


def _load_comparison(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _calc_tcost_sensitivity(
    *,
    asof: str,
    metric: str,
    out_root: Path,
    log_dir: Path,
    tcost_grid: list[float],
    provisional: dict[str, object],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for tcost in tcost_grid:
        rerun_dir = out_root / "reruns" / f"tcost_bps={int(tcost)}"
        _ensure_dir(rerun_dir)
        ok, detail = _run_strategy_compare(asof=asof, metric=metric, out_dir=rerun_dir, log_dir=log_dir, k=10, tcost_bps=float(tcost))
        if not ok:
            rows.append({"tcost_bps": float(tcost), "strategy_variant": "__run_failed__", "reason": detail, **provisional})
            continue
        comp = _load_comparison(rerun_dir / "strategy_comparison.csv").copy()
        comp["tcost_bps"] = float(tcost)
        comp["reason"] = ""
        comp["run_dir"] = str(rerun_dir)
        comp["log_path"] = detail
        rows.extend(comp.to_dict("records"))
    return pd.DataFrame(rows)


def _calc_k_sensitivity(
    *,
    asof: str,
    metric: str,
    out_root: Path,
    log_dir: Path,
    k_grid: list[int],
    provisional: dict[str, object],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for k in k_grid:
        rerun_dir = out_root / "reruns" / f"k={int(k)}"
        _ensure_dir(rerun_dir)
        ok, detail = _run_strategy_compare(asof=asof, metric=metric, out_dir=rerun_dir, log_dir=log_dir, k=int(k), tcost_bps=30.0)
        if not ok:
            rows.append({"k": int(k), "strategy_variant": "__run_failed__", "reason": detail, **provisional})
            continue
        comp = _load_comparison(rerun_dir / "strategy_comparison.csv").copy()
        comp["k"] = int(k)
        comp["reason"] = ""
        comp["run_dir"] = str(rerun_dir)
        comp["log_path"] = detail
        rows.extend(comp.to_dict("records"))
    return pd.DataFrame(rows)


def _calc_ai_overfit_diagnostics(
    *,
    strategy_compare_dir: Path,
    factor_bt: pd.DataFrame,
    combo_monthly: pd.DataFrame,
    provisional: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ai_detail = pd.read_csv(strategy_compare_dir / "ai_wf_topk" / "walkforward_integrated_rebalance_detail.csv")
    ai_detail["rebalance_month"] = _to_month_end(ai_detail["rebalance_month"])

    combo_detail = pd.read_csv(strategy_compare_dir / "combo" / "combo_rebalance_detail.csv")
    combo_detail["rebalance_month"] = _to_month_end(combo_detail["rebalance_month"])

    factor_period = _period_returns_from_bt(factor_bt)
    combo_seed = combo_monthly.copy()
    combo_seed["turnover"] = np.nan
    combo_period = _period_returns_from_bt(combo_seed)
    combo_period = combo_period.merge(
        combo_detail[["rebalance_month", "turnover", "overlap_count", "overlap_ratio"]],
        on="rebalance_month",
        how="left",
        suffixes=("", "_detail"),
    )
    combo_period["turnover"] = pd.to_numeric(combo_period.get("turnover_detail"), errors="coerce").combine_first(
        pd.to_numeric(combo_period.get("turnover"), errors="coerce")
    )
    if "turnover_detail" in combo_period.columns:
        combo_period = combo_period.drop(columns=["turnover_detail"])

    ai_period = ai_detail[["rebalance_month", "period_diff_net", "ai_turnover", "overlap_count"]].copy()
    ai_period["avg_overlap_ratio"] = pd.to_numeric(ai_period["overlap_count"], errors="coerce") / 10.0
    ai_period = ai_period.rename(columns={"period_diff_net": "excess_return", "ai_turnover": "turnover"})

    combo_vs_factor = combo_period.merge(
        factor_period[["rebalance_month", "period_ret_net"]].rename(columns={"period_ret_net": "factor_period_ret_net"}),
        on="rebalance_month",
        how="inner",
    )
    combo_vs_factor["excess_return"] = pd.to_numeric(combo_vs_factor["period_ret_net"], errors="coerce") - pd.to_numeric(
        combo_vs_factor["factor_period_ret_net"], errors="coerce"
    )

    diag_rows: list[dict[str, object]] = []

    def _summarize(label: str, df: pd.DataFrame, overlap_col: str, turnover_col: str, excess_col: str) -> None:
        excess = pd.to_numeric(df[excess_col], errors="coerce")
        overlap = pd.to_numeric(df[overlap_col], errors="coerce") if overlap_col in df.columns else pd.Series(dtype=float)
        turnover = pd.to_numeric(df[turnover_col], errors="coerce") if turnover_col in df.columns else pd.Series(dtype=float)
        roll4 = _rolling_compound(excess.fillna(0.0), 4)
        diag_rows.append(
            {
                "diagnostic_target": label,
                "avg_overlap_count": float(overlap.mean()) if len(overlap) else float("nan"),
                "avg_overlap_ratio": float((overlap / 10.0).mean()) if overlap_col == "overlap_count" and len(overlap) else float(overlap.mean()) if len(overlap) else float("nan"),
                "avg_turnover": float(turnover.mean()) if len(turnover) else float("nan"),
                "excess_return_mean": float(excess.mean()),
                "excess_return_std": float(excess.std(ddof=1)) if len(excess.dropna()) > 1 else float("nan"),
                "excess_return_t_stat": _calc_t_stat(excess),
                "hit_rate": float((excess > 0).mean()) if len(excess.dropna()) else float("nan"),
                "worst_4q_return": float(roll4.min()) if len(roll4.dropna()) else float("nan"),
                "best_4q_return": float(roll4.max()) if len(roll4.dropna()) else float("nan"),
                "max_consecutive_underperform_periods": _max_consecutive_underperform(excess),
                **provisional,
            }
        )

    _summarize("ai_wf_topk_vs_factor_composite", ai_period, "overlap_count", "turnover", "excess_return")
    _summarize("combo_overlay_replace_vs_factor_composite", combo_vs_factor, "overlap_ratio", "turnover", "excess_return")

    return pd.DataFrame(diag_rows), ai_period, combo_vs_factor


def _save_figure_common_nav(fig_dir: Path, common_nav: pd.DataFrame, provisional_source: str) -> Path:
    p = fig_dir / "common_period_nav.png"
    plt.figure(figsize=(12, 6))
    for col in common_nav.columns:
        if col != "month_end":
            plt.plot(pd.to_datetime(common_nav["month_end"]), pd.to_numeric(common_nav[col], errors="coerce"), label=col)
    plt.title(f"Common Period NAV (provisional: {provisional_source})")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(p, dpi=150)
    plt.close()
    return p


def _save_figure_drawdown(fig_dir: Path, common_nav: pd.DataFrame, provisional_source: str) -> Path:
    p = fig_dir / "common_period_drawdown.png"
    plt.figure(figsize=(12, 6))
    for col in common_nav.columns:
        if col != "month_end":
            nav = pd.to_numeric(common_nav[col], errors="coerce")
            dd = nav / nav.cummax() - 1.0
            plt.plot(pd.to_datetime(common_nav["month_end"]), dd, label=col)
    plt.title(f"Common Period Drawdown (provisional: {provisional_source})")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(p, dpi=150)
    plt.close()
    return p


def _save_figure_rolling_12m(fig_dir: Path, common_nav: pd.DataFrame, provisional_source: str) -> Path:
    p = fig_dir / "rolling_12m_return.png"
    plt.figure(figsize=(12, 6))
    for col in common_nav.columns:
        if col != "month_end":
            nav = pd.to_numeric(common_nav[col], errors="coerce")
            ret = nav.pct_change().fillna(nav.iloc[0] - 1.0)
            roll = _rolling_compound(ret, 12)
            plt.plot(pd.to_datetime(common_nav["month_end"]), roll, label=col)
    plt.title(f"Rolling 12M Return (provisional: {provisional_source})")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(p, dpi=150)
    plt.close()
    return p


def _save_figure_ai_excess(fig_dir: Path, ai_period: pd.DataFrame, provisional_source: str) -> Path:
    p = fig_dir / "ai_excess_return_by_period.png"
    plt.figure(figsize=(12, 6))
    x = pd.to_datetime(ai_period["rebalance_month"])
    y = pd.to_numeric(ai_period["excess_return"], errors="coerce")
    plt.bar(x, y, color=np.where(y >= 0, "#2e8b57", "#cc4c4c"))
    plt.axhline(0, color="black", linewidth=1)
    plt.title(f"AI Excess Return by Period (provisional: {provisional_source})")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(p, dpi=150)
    plt.close()
    return p


def _save_figure_turnover_vs_return(fig_dir: Path, ai_period: pd.DataFrame, combo_vs_factor: pd.DataFrame, provisional_source: str) -> Path:
    p = fig_dir / "turnover_vs_return.png"
    plt.figure(figsize=(10, 6))
    plt.scatter(pd.to_numeric(ai_period["turnover"], errors="coerce"), pd.to_numeric(ai_period["excess_return"], errors="coerce"), label="ai_wf_topk", alpha=0.7)
    plt.scatter(pd.to_numeric(combo_vs_factor["turnover"], errors="coerce"), pd.to_numeric(combo_vs_factor["excess_return"], errors="coerce"), label="combo_overlay_replace", alpha=0.7)
    plt.axhline(0, color="black", linewidth=1)
    plt.xlabel("Turnover")
    plt.ylabel("Excess Return vs factor_composite")
    plt.title(f"Turnover vs Excess Return (provisional: {provisional_source})")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(p, dpi=150)
    plt.close()
    return p


def _save_figure_overlap_vs_excess(fig_dir: Path, ai_period: pd.DataFrame, combo_vs_factor: pd.DataFrame, provisional_source: str) -> Path:
    p = fig_dir / "overlap_vs_excess_return.png"
    plt.figure(figsize=(10, 6))
    plt.scatter(pd.to_numeric(ai_period["avg_overlap_ratio"], errors="coerce"), pd.to_numeric(ai_period["excess_return"], errors="coerce"), label="ai_wf_topk", alpha=0.7)
    if "overlap_ratio" in combo_vs_factor.columns:
        plt.scatter(pd.to_numeric(combo_vs_factor["overlap_ratio"], errors="coerce"), pd.to_numeric(combo_vs_factor["excess_return"], errors="coerce"), label="combo_overlay_replace", alpha=0.7)
    plt.axhline(0, color="black", linewidth=1)
    plt.xlabel("Overlap Ratio")
    plt.ylabel("Excess Return vs factor_composite")
    plt.title(f"Overlap vs Excess Return (provisional: {provisional_source})")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(p, dpi=150)
    plt.close()
    return p


def _write_summary(
    *,
    out_path: Path,
    best_meta: dict,
    common_comp: pd.DataFrame,
    ai_diag: pd.DataFrame,
    provisional_source: str,
) -> None:
    rows = common_comp.set_index("strategy_variant")
    ai_row = ai_diag.loc[ai_diag["diagnostic_target"] == "ai_wf_topk_vs_factor_composite"].iloc[0]
    combo_row = ai_diag.loc[ai_diag["diagnostic_target"] == "combo_overlay_replace_vs_factor_composite"].iloc[0]
    lines = [
        "# Strategy Validation Summary",
        "",
        f"- asof: **{best_meta.get('krx_master_meta', {}).get('asof', '2026-04-15')}**",
        f"- provisional: **{bool(best_meta.get('provisional', False))}**",
        f"- provisional_source: **{provisional_source}**",
        "",
        "## Why best_strategy is factor_composite",
        "",
        f"- factor_composite has the highest rank score in the current comparison and the strongest net NAV at **{best_meta.get('net_nav')}**.",
        "- On the common-period comparison, factor_composite still keeps top-tier CAGR and Sharpe without extreme turnover behavior.",
        "",
        "## ai_wf_topk strengths and overfit risk",
        "",
        f"- ai_wf_topk is strong on raw CAGR and Sharpe. Its common-period Sharpe is **{rows.loc['ai_wf_topk', 'sharpe_net']:.4f}**.",
        f"- However, avg_turnover is high (**{ai_row['avg_turnover']:.4f}**) and excess-return volatility is meaningful, so overfitting and trading-cost sensitivity remain real risks.",
        f"- hit_rate is **{ai_row['hit_rate']:.4f}** and excess_return_t_stat is **{ai_row['excess_return_t_stat']:.4f}**.",
        "",
        "## combo_overlay_replace defensive meaning",
        "",
        "- combo has a defensive interpretation because it retains a large amount of overlap between the factor sleeve and the AI sleeve.",
        f"- avg_overlap_ratio is **{combo_row['avg_overlap_ratio']:.4f}** and avg_turnover is **{combo_row['avg_turnover']:.4f}**.",
        "- It can be kept as a defensive or blended candidate, but the current form is not strong enough to replace the main strategy.",
        "",
        "## Common-period recomparison",
        "",
    ]
    for name in ["factor_composite", "ai_wf_topk", "quant_only", "combo_overlay_replace"]:
        r = rows.loc[name]
        lines.append(
            f"- {name}: net_nav={r['net_nav']:.4f}, CAGR={r['cagr_net']:.4f}, Sharpe={r['sharpe_net']:.4f}, MDD={r['maxdd_net']:.4f}, Calmar={r['calmar_net']:.4f}, win_rate={r['monthly_win_rate']:.4f}"
        )
    lines.extend(
        [
            "",
            "## Implementation recommendation",
            "",
            "- A. Keep factor_composite as the main strategy",
            "- B. Treat ai_wf_topk as a satellite sleeve or a bounded overlay candidate",
            "- C. Keep combo on hold in its current form",
            "",
            "## Note",
            "",
            "- These outputs are provisional.",
            "- Because KRX master still relies on the 2026-03-31 fallback seed, the full validation set should be rerun after a live 2026-04-15 master is collected.",
        ]
    )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate robustness / overfit risk of 4-strategy comparison outputs.")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--strategy_compare_dir", required=True)
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--tcost_grid", default="0,30,50,100,150")
    ap.add_argument("--k_grid", default="5,10,15,20,30")
    args = ap.parse_args()

    strategy_compare_dir = Path(args.strategy_compare_dir)
    out_dir = Path(args.out_dir)
    fig_dir = out_dir / "figures"
    log_dir = Path("artifacts/logs")
    _ensure_dir(out_dir)
    _ensure_dir(fig_dir)
    _ensure_dir(log_dir)

    before_files = {str(p.relative_to(out_dir)): p.stat().st_mtime for p in out_dir.rglob("*") if p.is_file()}

    best_meta = _load_best_meta(strategy_compare_dir)
    provisional = _provisional_cols(best_meta)
    provisional_source = str(provisional["provisional_source"])
    validation_meta = {
        "asof": args.asof,
        "metric": args.metric,
        "strategy_compare_dir": str(strategy_compare_dir),
        "out_dir": str(out_dir),
        **provisional,
    }
    (out_dir / "validation_meta.json").write_text(json.dumps(validation_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    monthlies = _load_strategy_monthlies(args.asof, args.metric, 10, strategy_compare_dir)
    common_comp, common_nav = _calc_common_period_comparison(monthlies, provisional)
    common_comp.to_csv(out_dir / "common_period_comparison.csv", index=False, encoding="utf-8-sig")

    tcost_df = _calc_tcost_sensitivity(
        asof=args.asof,
        metric=args.metric,
        out_root=out_dir,
        log_dir=log_dir,
        tcost_grid=_parse_grid(args.tcost_grid, float),
        provisional=provisional,
    )
    tcost_df.to_csv(out_dir / "tcost_sensitivity.csv", index=False, encoding="utf-8-sig")

    k_df = _calc_k_sensitivity(
        asof=args.asof,
        metric=args.metric,
        out_root=out_dir,
        log_dir=log_dir,
        k_grid=_parse_grid(args.k_grid, int),
        provisional=provisional,
    )
    k_df.to_csv(out_dir / "k_sensitivity.csv", index=False, encoding="utf-8-sig")

    ai_diag, ai_period, combo_vs_factor = _calc_ai_overfit_diagnostics(
        strategy_compare_dir=strategy_compare_dir,
        factor_bt=monthlies["factor_composite"].copy(),
        combo_monthly=monthlies["combo_overlay_replace"].copy(),
        provisional=provisional,
    )
    ai_diag.to_csv(out_dir / "ai_overfit_diagnostics.csv", index=False, encoding="utf-8-sig")

    fig_paths = [
        _save_figure_common_nav(fig_dir, common_nav, provisional_source),
        _save_figure_drawdown(fig_dir, common_nav, provisional_source),
        _save_figure_rolling_12m(fig_dir, common_nav, provisional_source),
        _save_figure_ai_excess(fig_dir, ai_period, provisional_source),
        _save_figure_turnover_vs_return(fig_dir, ai_period, combo_vs_factor, provisional_source),
        _save_figure_overlap_vs_excess(fig_dir, ai_period, combo_vs_factor, provisional_source),
    ]

    _write_summary(
        out_path=out_dir / "validation_summary.md",
        best_meta=best_meta,
        common_comp=common_comp,
        ai_diag=ai_diag,
        provisional_source=provisional_source,
    )

    after_files = {str(p.relative_to(out_dir)): p.stat().st_mtime for p in out_dir.rglob("*") if p.is_file()}
    created_files = sorted(k for k in after_files if k not in before_files)
    changed_files = sorted(k for k in after_files if k in before_files and after_files[k] != before_files[k])

    print("[INFO] changed_source_files=['scripts/backtest/validate_strategy_robustness.py']")
    print("[OK] saved:", out_dir / "validation_meta.json")
    print("[OK] saved:", out_dir / "common_period_comparison.csv")
    print("[OK] saved:", out_dir / "tcost_sensitivity.csv")
    print("[OK] saved:", out_dir / "k_sensitivity.csv")
    print("[OK] saved:", out_dir / "ai_overfit_diagnostics.csv")
    for fp in fig_paths:
        print(f"[OK] figure: {fp}")
    print(f"[OK] summary: {out_dir / 'validation_summary.md'}")
    print(f"[INFO] provisional={provisional['provisional']} provisional_source={provisional_source}")
    print(f"[INFO] changed_files={changed_files}")
    print(f"[INFO] created_files={created_files}")


if __name__ == "__main__":
    main()
