from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


def load_strategy_modules_config(strat_path: str | Path, strategy: str) -> dict[str, Any]:
    p = Path(strat_path)
    if yaml is None or not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}

    strategies = cfg.get("strategies", {})
    if not isinstance(strategies, dict):
        return {}

    s = strategies.get(strategy, {})
    if not isinstance(s, dict):
        return {}

    scoring = s.get("scoring", {}) if isinstance(s.get("scoring"), dict) else {}
    selection = s.get("selection", {}) if isinstance(s.get("selection"), dict) else {}

    expectation = scoring.get("expectation_overlay", {}) if isinstance(scoring.get("expectation_overlay"), dict) else {}
    quality_soft_penalty = scoring.get("quality_soft_penalty", {}) if isinstance(scoring.get("quality_soft_penalty"), dict) else {}
    mcap_grouping = selection.get("mcap_grouping", {}) if isinstance(selection.get("mcap_grouping"), dict) else {}

    return {
        "expectation_overlay": expectation,
        "quality_soft_penalty": quality_soft_penalty,
        "mcap_grouping": mcap_grouping,
    }


def _to_num(s: pd.Series | Any) -> pd.Series:
    if isinstance(s, pd.Series):
        return pd.to_numeric(s, errors="coerce")
    return pd.to_numeric(pd.Series([s]), errors="coerce")


def _safe_z(s: pd.Series) -> pd.Series:
    x = _to_num(s).astype("float64")
    x = x.replace([np.inf, -np.inf], np.nan)
    if x.notna().sum() == 0:
        return pd.Series(0.0, index=x.index, dtype="float64")
    mu = x.mean(skipna=True)
    sd = x.std(skipna=True)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=x.index, dtype="float64")
    z = (x - mu) / sd
    return z.replace([np.inf, -np.inf], np.nan).fillna(0.0).astype("float64")


def _normalize_group_value(v: Any) -> str:
    if pd.isna(v):
        return "unknown"
    s = str(v).strip()
    return s if s else "unknown"


def _ensure_price_history_returns(price_history: pd.DataFrame) -> pd.DataFrame:
    if price_history is None or len(price_history) == 0:
        return pd.DataFrame(columns=["ticker", "date", "close", "ret_63d", "ret_126d", "ret_252d"])

    cols = set(price_history.columns)
    close_col = "close" if "close" in cols else ("current_price" if "current_price" in cols else None)
    date_col = "date" if "date" in cols else ("price_date" if "price_date" in cols else None)
    if close_col is None or date_col is None or "ticker" not in cols:
        return pd.DataFrame(columns=["ticker", "date", "close", "ret_63d", "ret_126d", "ret_252d"])

    px = price_history[["ticker", date_col, close_col]].copy()
    px = px.rename(columns={date_col: "date", close_col: "close"})
    px["ticker"] = px["ticker"].astype(str)
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["close"] = pd.to_numeric(px["close"], errors="coerce")
    px = px.dropna(subset=["ticker", "date", "close"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    if len(px) == 0:
        return px

    for win in [63, 126, 252]:
        col = f"ret_{win}d"
        if col not in px.columns:
            px[col] = px.groupby("ticker")["close"].transform(lambda s: s / s.shift(win) - 1.0)
    return px


def _price_snapshot_asof(px: pd.DataFrame, tickers: list[str], asof_date: pd.Timestamp) -> pd.DataFrame:
    if px is None or len(px) == 0 or len(tickers) == 0:
        return pd.DataFrame({"ticker": tickers})

    right = px.loc[px["ticker"].isin(tickers)].copy()
    right = right.sort_values(["ticker", "date"]).reset_index(drop=True)

    rows: list[pd.DataFrame] = []
    for tk in sorted(set(tickers)):
        p = right.loc[right["ticker"] == tk].copy()
        if len(p) == 0:
            rows.append(pd.DataFrame({"ticker": [tk]}))
            continue
        left = pd.DataFrame({"asof_date": [pd.Timestamp(asof_date)]})
        merged = pd.merge_asof(
            left.sort_values("asof_date"),
            p.sort_values("date"),
            left_on="asof_date",
            right_on="date",
            direction="backward",
            allow_exact_matches=True,
        )
        merged["ticker"] = tk
        rows.append(merged)
    return pd.concat(rows, ignore_index=True)


def _resolve_expectation_source(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            ser = _to_num(df[c])
            if ser.notna().sum() > 0:
                return c
    return None


def apply_expectation_overlay(
    scored: pd.DataFrame,
    expectation_cfg: dict[str, Any] | None,
    *,
    asof_date: pd.Timestamp | None = None,
    price_history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    out = scored.copy()
    out["score_total_pre_expect"] = pd.to_numeric(out.get("score_total", out.get("score", 0.0)), errors="coerce")

    defaults = {
        "enabled": False,
        "weight": 0.15,
        "clip_min": -0.20,
        "clip_max": 0.10,
        "valuation_weight": 0.60,
        "mom6_weight": 0.25,
        "mom12_weight": 0.15,
    }
    cfg = dict(defaults)
    if isinstance(expectation_cfg, dict):
        cfg.update(expectation_cfg)

    enabled = bool(cfg.get("enabled", False))
    out["expectation_overlay_active"] = int(enabled)
    out["expectation_component_count"] = 0
    out["expectation_valuation_z"] = 0.0
    out["expectation_mom6_z"] = 0.0
    out["expectation_mom12_z"] = 0.0
    out["expectation_score"] = 0.0
    out["expectation_penalty"] = 0.0
    out["score_total_post_expect"] = out["score_total_pre_expect"]

    if not enabled or len(out) == 0:
        out["score_total"] = out["score_total_pre_expect"]
        return out

    valuation_candidates = ["ev_ebitda", "EV_EBITDA", "ev_ebit", "EV_EBIT", "psr", "PSR", "per", "PER", "pbr", "PBR"]
    val_col = _resolve_expectation_source(out, valuation_candidates)
    if val_col is not None:
        out["expectation_valuation_z"] = _safe_z(out[val_col])

    tickers = out["ticker"].astype(str).tolist() if "ticker" in out.columns else []
    if price_history is not None and asof_date is not None and len(tickers) > 0:
        px = _ensure_price_history_returns(price_history)
        snap = _price_snapshot_asof(px, tickers, pd.Timestamp(asof_date))
        if len(snap) > 0:
            out = out.merge(
                snap[[c for c in ["ticker", "ret_63d", "ret_126d", "ret_252d"] if c in snap.columns]],
                on="ticker",
                how="left",
                suffixes=("", "__expectpx"),
            )

    mom6_col = _resolve_expectation_source(out, ["ret_126d", "price_mom_6m", "mom_6m"])
    mom12_col = _resolve_expectation_source(out, ["ret_252d", "price_mom_12m", "mom_12m"])
    if mom6_col is not None:
        out["expectation_mom6_z"] = _safe_z(out[mom6_col])
    if mom12_col is not None:
        out["expectation_mom12_z"] = _safe_z(out[mom12_col])

    component_count = 0
    expectation_raw = pd.Series(0.0, index=out.index, dtype="float64")
    total_w = 0.0
    for comp_col, w_key in [
        ("expectation_valuation_z", "valuation_weight"),
        ("expectation_mom6_z", "mom6_weight"),
        ("expectation_mom12_z", "mom12_weight"),
    ]:
        ser = _to_num(out[comp_col]).fillna(0.0)
        if ser.abs().sum() > 0:
            w = float(cfg.get(w_key, 0.0))
            if w != 0:
                expectation_raw += w * ser
                total_w += abs(w)
                component_count += 1

    out["expectation_component_count"] = int(component_count)
    if component_count == 0 or total_w <= 0:
        out["score_total"] = out["score_total_pre_expect"]
        out["score_total_post_expect"] = out["score_total_pre_expect"]
        return out

    out["expectation_score"] = (expectation_raw / total_w).astype("float64")
    weight = float(cfg.get("weight", defaults["weight"]))
    clip_min = float(cfg.get("clip_min", defaults["clip_min"]))
    clip_max = float(cfg.get("clip_max", defaults["clip_max"]))
    penalty = (-weight * out["expectation_score"]).clip(lower=clip_min, upper=clip_max)
    out["expectation_penalty"] = penalty.astype("float64")
    out["score_total_post_expect"] = (out["score_total_pre_expect"] + out["expectation_penalty"]).astype("float64")
    out["score_total"] = out["score_total_post_expect"]
    return out


def apply_quality_soft_penalty(
    scored: pd.DataFrame,
    penalty_cfg: dict[str, Any] | None,
) -> pd.DataFrame:
    out = scored.copy()
    out["score_total_pre_quality"] = pd.to_numeric(out.get("score_total", out.get("score", 0.0)), errors="coerce")

    defaults = {
        "enabled": False,
        "netincome_ttm_nonpositive_penalty": -0.25,
        "netincome_acc2_negative_penalty": -0.15,
        "cfo_warn_penalty": -0.15,
        "cfo_isnull_penalty": -0.10,
    }
    cfg = dict(defaults)
    if isinstance(penalty_cfg, dict):
        cfg.update(penalty_cfg)

    enabled = bool(cfg.get("enabled", False))
    out["quality_soft_penalty_active"] = int(enabled)
    out["quality_penalty_netincome_ttm_nonpositive"] = 0.0
    out["quality_penalty_netincome_acc2_negative"] = 0.0
    out["quality_penalty_cfo_warn"] = 0.0
    out["quality_penalty_cfo_isnull"] = 0.0
    out["quality_penalty_total"] = 0.0
    out["score_total_post_quality"] = out["score_total_pre_quality"]

    if not enabled or len(out) == 0:
        out["score_total"] = out["score_total_pre_quality"]
        return out

    if "NetIncome_ttm" in out.columns:
        ni_ttm = _to_num(out["NetIncome_ttm"])
        mask = ni_ttm.notna() & (ni_ttm <= 0)
        out.loc[mask, "quality_penalty_netincome_ttm_nonpositive"] = float(cfg.get("netincome_ttm_nonpositive_penalty", defaults["netincome_ttm_nonpositive_penalty"]))

    if "NetIncome_acc2" in out.columns:
        ni_acc2 = _to_num(out["NetIncome_acc2"])
        mask = ni_acc2.notna() & (ni_acc2 < 0)
        out.loc[mask, "quality_penalty_netincome_acc2_negative"] = float(cfg.get("netincome_acc2_negative_penalty", defaults["netincome_acc2_negative_penalty"]))

    if "CFO_warn" in out.columns:
        cfo_warn = _to_num(out["CFO_warn"]).fillna(0.0)
        mask = cfo_warn > 0
        out.loc[mask, "quality_penalty_cfo_warn"] = float(cfg.get("cfo_warn_penalty", defaults["cfo_warn_penalty"]))

    if "CFO_isnull" in out.columns:
        cfo_isnull = _to_num(out["CFO_isnull"]).fillna(0.0)
        mask = cfo_isnull > 0
        out.loc[mask, "quality_penalty_cfo_isnull"] = float(cfg.get("cfo_isnull_penalty", defaults["cfo_isnull_penalty"]))

    penalty_cols = [
        "quality_penalty_netincome_ttm_nonpositive",
        "quality_penalty_netincome_acc2_negative",
        "quality_penalty_cfo_warn",
        "quality_penalty_cfo_isnull",
    ]
    out["quality_penalty_total"] = out[penalty_cols].sum(axis=1).astype("float64")
    out["score_total_post_quality"] = (out["score_total_pre_quality"] + out["quality_penalty_total"]).astype("float64")
    out["score_total"] = out["score_total_post_quality"]
    return out


def assign_mcap_group(scored: pd.DataFrame, grouping_cfg: dict[str, Any] | None) -> pd.DataFrame:
    out = scored.copy()
    defaults = {
        "enabled": False,
        "bins": [0.7, 0.9],
        "labels": ["small", "mid", "large"],
        "mcap_col": "mcap",
    }
    cfg = dict(defaults)
    if isinstance(grouping_cfg, dict):
        cfg.update(grouping_cfg)

    out["mcap_grouping_active"] = int(bool(cfg.get("enabled", False)))
    out["mcap_group"] = pd.NA
    out["mcap_rank_pct"] = pd.NA

    if not bool(cfg.get("enabled", False)):
        return out

    mcap_col = str(cfg.get("mcap_col", "mcap"))
    if mcap_col not in out.columns:
        return out

    x = _to_num(out[mcap_col])
    if x.notna().sum() == 0:
        return out

    out["mcap_rank_pct"] = x.rank(method="average", pct=True)
    bins = cfg.get("bins", [0.7, 0.9])
    try:
        b1, b2 = float(bins[0]), float(bins[1])
    except Exception:
        b1, b2 = 0.7, 0.9
    b1 = min(max(b1, 0.0), 1.0)
    b2 = min(max(b2, b1), 1.0)

    labels = cfg.get("labels", ["small", "mid", "large"])
    if not isinstance(labels, list) or len(labels) != 3:
        labels = ["small", "mid", "large"]

    grp = pd.cut(out["mcap_rank_pct"], bins=[0.0, b1, b2, 1.0], labels=labels, include_lowest=True)
    out["mcap_group"] = grp.astype("string")
    return out


def _normalize_quota(quota_cfg: dict[str, Any], portfolio_size: int, labels: list[str]) -> dict[str, int]:
    quota: dict[str, int] = {}
    for lab in labels:
        try:
            quota[str(lab)] = int(quota_cfg.get(str(lab), 0))
        except Exception:
            quota[str(lab)] = 0
    s = sum(quota.values())
    if s <= 0:
        base = portfolio_size // max(len(labels), 1)
        quota = {str(l): base for l in labels}
        rem = portfolio_size - sum(quota.values())
        for lab in labels[:rem]:
            quota[str(lab)] += 1
        return quota
    if s == portfolio_size:
        return quota

    scaled = {k: float(v) * portfolio_size / s for k, v in quota.items()}
    base = {k: int(np.floor(v)) for k, v in scaled.items()}
    rem = portfolio_size - sum(base.values())
    order = sorted(scaled.keys(), key=lambda k: (scaled[k] - base[k]), reverse=True)
    for k in order[: max(rem, 0)]:
        base[k] += 1
    return base


def _attach_mcap_columns(selected: pd.DataFrame, source: pd.DataFrame) -> pd.DataFrame:
    out = selected.copy()
    wanted = [c for c in ["ticker", "mcap", "mcap_rank_pct", "mcap_group", "mcap_grouping_active"] if c in source.columns]
    if "ticker" not in wanted or len(wanted) <= 1:
        return out
    src = source[wanted].drop_duplicates(subset=["ticker"], keep="first").copy()
    for c in wanted:
        if c == "ticker":
            continue
        if c in out.columns:
            out = out.drop(columns=[c])
    out = out.merge(src, on="ticker", how="left")
    return out


def select_target_portfolio_with_mcap_groups(
    scored_sorted: pd.DataFrame,
    *,
    portfolio_size: int,
    keep_current_top_n: int,
    effective_group_col: Optional[str],
    max_per_group: int,
    current_tickers: set[str] | None,
    base_selector: Callable[..., pd.DataFrame],
    grouping_cfg: dict[str, Any] | None,
) -> pd.DataFrame:
    defaults = {
        "enabled": False,
        "labels": ["small", "mid", "large"],
        "quota": {"large": 3, "mid": 4, "small": 3},
    }
    cfg = dict(defaults)
    if isinstance(grouping_cfg, dict):
        cfg.update(grouping_cfg)

    if not bool(cfg.get("enabled", False)):
        return base_selector(
            scored_sorted,
            portfolio_size=portfolio_size,
            keep_current_top_n=keep_current_top_n,
            effective_group_col=effective_group_col,
            max_per_group=max_per_group,
            current_tickers=current_tickers,
        )

    current_tickers = set(current_tickers or set())
    work = assign_mcap_group(scored_sorted, cfg)
    if "mcap_group" not in work.columns or work["mcap_group"].isna().all():
        return _attach_mcap_columns(
            base_selector(
                work,
                portfolio_size=portfolio_size,
                keep_current_top_n=keep_current_top_n,
                effective_group_col=effective_group_col,
                max_per_group=max_per_group,
                current_tickers=current_tickers,
            ),
            work,
        )

    labels = [str(x) for x in (cfg.get("labels", ["small", "mid", "large"]))]
    quota_cfg = cfg.get("quota", {}) if isinstance(cfg.get("quota"), dict) else {}
    quota = _normalize_quota(quota_cfg, int(portfolio_size), labels)

    current_scored = work[work["ticker"].astype(str).isin(current_tickers)].copy()
    reserved = base_selector(
        current_scored,
        portfolio_size=max(0, min(int(keep_current_top_n), int(portfolio_size))),
        keep_current_top_n=0,
        effective_group_col=effective_group_col,
        max_per_group=int(max_per_group),
        current_tickers=current_tickers,
    ).copy()
    reserved = _attach_mcap_columns(reserved, work)
    reserved_set = set(reserved["ticker"].astype(str).tolist())

    if len(reserved) > 0:
        reserved["selection_bucket"] = "keep_current_top_n"
        reserved["kept_from_previous"] = 1
        reserved["mcap_group_selection_reason"] = reserved["mcap_group"].map(lambda x: f"reserved_current::{_normalize_group_value(x)}")

    picked_parts: list[pd.DataFrame] = [reserved]
    rest_pool = work[~work["ticker"].astype(str).isin(reserved_set)].copy()

    reserved_group_counts = {
        g: int((reserved.get("mcap_group", pd.Series(dtype="object")).astype("string") == g).sum())
        for g in labels
    }

    selected_tickers = set(reserved_set)
    for g in labels:
        group_target = max(0, int(quota.get(g, 0)) - int(reserved_group_counts.get(g, 0)))
        if group_target <= 0:
            continue
        pool_g = rest_pool[(rest_pool["mcap_group"].astype("string") == g) & (~rest_pool["ticker"].astype(str).isin(selected_tickers))].copy()
        if len(pool_g) == 0:
            continue
        part = base_selector(
            pool_g,
            portfolio_size=group_target,
            keep_current_top_n=0,
            effective_group_col=effective_group_col,
            max_per_group=int(max_per_group),
            current_tickers=current_tickers,
        ).copy()
        part = _attach_mcap_columns(part, work)
        if len(part) == 0:
            continue
        part["selection_bucket"] = part.get("selection_bucket", pd.Series(index=part.index, dtype="object")).fillna("mcap_group_fill")
        part["kept_from_previous"] = part["ticker"].astype(str).isin(current_tickers).astype(int)
        part["mcap_group_selection_reason"] = part["mcap_group"].map(lambda x: f"quota_fill::{_normalize_group_value(x)}")
        picked_parts.append(part)
        selected_tickers |= set(part["ticker"].astype(str).tolist())

    interim = pd.concat(picked_parts, ignore_index=True) if picked_parts else work.head(0).copy()
    interim = interim.drop_duplicates(subset=["ticker"], keep="first")

    remaining_slots = max(0, int(portfolio_size) - len(interim))
    if remaining_slots > 0:
        leftover_pool = work[~work["ticker"].astype(str).isin(interim["ticker"].astype(str))].copy()
        fill = base_selector(
            leftover_pool,
            portfolio_size=remaining_slots,
            keep_current_top_n=0,
            effective_group_col=effective_group_col,
            max_per_group=int(max_per_group),
            current_tickers=current_tickers,
        ).copy()
        fill = _attach_mcap_columns(fill, work)
        if len(fill) > 0:
            fill["selection_bucket"] = fill.get("selection_bucket", pd.Series(index=fill.index, dtype="object")).fillna("mcap_group_fallback_fill")
            fill["kept_from_previous"] = fill["ticker"].astype(str).isin(current_tickers).astype(int)
            fill["mcap_group_selection_reason"] = fill["mcap_group"].map(lambda x: f"fallback_fill::{_normalize_group_value(x)}")
            interim = pd.concat([interim, fill], ignore_index=True)
            interim = interim.drop_duplicates(subset=["ticker"], keep="first")

    interim = _attach_mcap_columns(interim, work)
    interim = interim.sort_values(["score_adj", "score", "ticker"], ascending=[False, False, True], na_position="last").reset_index(drop=True)
    return interim.head(int(portfolio_size)).copy()
