# backtest_quarterly_rebalance_v2.py
# - quarterly rebalance backtest (factor score -> pick top-k -> monthly NAV)
# - supports returns_monthly schema: month OR month_end
# - supports strategies yaml shapes: weights dict OR legacy components list
# - optional audit dumps: holdings_snapshot, inputs_long
# - supports per-group cap in final selection
# - writes rebalance audit with selected names/metrics/score/price/forward returns

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


def _load_yaml_unique(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("pyyaml not installed. Run: pip install pyyaml")

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"Duplicate YAML key detected: {key!r} in {path}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f, Loader=UniqueKeyLoader)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML root type in {path}: {type(data)}")
    return data


# -----------------------------
# Utils
# -----------------------------

def zscore_safe(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce").astype("float64")
    x = x.replace([np.inf, -np.inf], np.nan)
    mu = x.mean(skipna=True)
    sd = x.std(skipna=True)
    if not np.isfinite(sd) or sd == 0.0:
        return pd.Series(np.zeros(len(x)), index=x.index, dtype="float64")
    z = (x - mu) / sd
    z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return z.astype("float64")


def robust_zscore_safe(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce").astype("float64")
    x = x.replace([np.inf, -np.inf], np.nan)
    med = x.median(skipna=True)
    mad = (x - med).abs().median(skipna=True)
    if not np.isfinite(mad) or mad == 0.0:
        return zscore_safe(x)
    z = 0.6744897501960817 * (x - med) / mad
    z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return z.astype("float64")


def apply_piecewise_clip(z: pd.Series, clip_z: float | None = None, clip_tiers: list[dict] | None = None) -> pd.Series:
    z = pd.to_numeric(z, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0).astype("float64")
    if not clip_tiers:
        if clip_z is not None and np.isfinite(float(clip_z)) and float(clip_z) >= 0.0:
            return z.clip(lower=-float(clip_z), upper=float(clip_z)).astype("float64")
        return z.astype("float64")

    tiers: list[tuple[float, float]] = []
    for item in clip_tiers:
        if not isinstance(item, dict):
            continue
        try:
            upto = float(item.get("upto"))
            slope = float(item.get("slope", 1.0))
        except Exception:
            continue
        if not np.isfinite(upto) or upto <= 0:
            continue
        slope = max(0.0, min(1.0, slope))
        tiers.append((upto, slope))
    tiers = sorted(tiers, key=lambda x: x[0])
    if not tiers:
        if clip_z is not None and np.isfinite(float(clip_z)) and float(clip_z) >= 0.0:
            return z.clip(lower=-float(clip_z), upper=float(clip_z)).astype("float64")
        return z.astype("float64")

    cap = float(clip_z) if clip_z is not None and np.isfinite(float(clip_z)) and float(clip_z) > 0 else tiers[-1][0]

    def _compress_one(v: float) -> float:
        sign = -1.0 if v < 0 else 1.0
        a = abs(v)
        out = 0.0
        prev = 0.0
        for upto, slope in tiers:
            hi = min(a, upto)
            if hi > prev:
                out += (hi - prev) * slope
                prev = hi
            if a <= upto:
                break
        if a > prev:
            tail_slope = tiers[-1][1]
            hi = min(a, cap)
            if hi > prev:
                out += (hi - prev) * tail_slope
        return sign * out

    return z.map(_compress_one).astype("float64")


def standardize_factor(s: pd.Series, use_robust_z: bool = False, clip_z: float | None = None, clip_tiers: list[dict] | None = None) -> pd.Series:
    z = robust_zscore_safe(s) if use_robust_z else zscore_safe(s)
    return apply_piecewise_clip(z, clip_z=clip_z, clip_tiers=clip_tiers)


def safe_fill_for_z(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    if s.notna().any():
        med = s.median(skipna=True)
        return s.fillna(med)
    return s.fillna(0.0)


def ym_to_month_end(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.to_period("M").to_timestamp("M")


def compute_rebalance_month_from_yq(year_s: pd.Series, quarter_s: pd.Series) -> pd.Series:
    """
    Rebalance convention:
      - Q1 end + 45 days
      - Q2 end + 45 days
      - Q3 end + 45 days
      - Q4 end + 90 days
    Then snap to month-end.
    """
    year = pd.to_numeric(year_s, errors="raise").astype(int)
    quarter = pd.to_numeric(quarter_s, errors="raise").astype(int)

    q_end_month = quarter.map({1: 3, 2: 6, 3: 9, 4: 12}).astype(int)
    lag_days = quarter.map({1: 45, 2: 45, 3: 45, 4: 90}).astype(int)

    q_end = pd.to_datetime(
        year.astype(str) + "-" + q_end_month.astype(str).str.zfill(2) + "-01"
    ).dt.to_period("M").dt.to_timestamp("M")

    rb = q_end + pd.to_timedelta(lag_days, unit="D")
    return rb.dt.to_period("M").dt.to_timestamp("M")


def ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)


def normalize_ticker_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _pick_first_existing(cols, candidates):
    cols = set(cols)
    for c in candidates:
        if c in cols:
            return c
    return None


def _read_any_table(p: Path) -> pd.DataFrame:
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    if p.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(p)
    return pd.read_parquet(p)


def _name_map_from_df(df: pd.DataFrame) -> pd.DataFrame:
    tcol = _pick_first_existing(df.columns, ["ticker", "code", "종목코드", "symbol"])
    ncol = _pick_first_existing(df.columns, ["name", "종목명", "company_name", "corp_name", "short_name", "한글종목명"])
    if tcol is None or ncol is None:
        return pd.DataFrame(columns=["ticker", "name"])
    out = df[[tcol, ncol]].copy()
    out["ticker"] = normalize_ticker_series(out[tcol])
    out["name"] = out[ncol].astype("string")
    out["name"] = out["name"].replace({"None": pd.NA, "nan": pd.NA, "NaN": pd.NA, "": pd.NA})
    out = out[["ticker", "name"]].dropna(subset=["ticker", "name"]).drop_duplicates("ticker")
    return out


def _load_name_map(asof: str, feat: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    root = Path("data/processed")
    candidates = []

    explicit = [
        root / f"krx_master__asof={asof}__src=pykrx__v=1.parquet",
        root / f"krx_master__asof={asof}__src=pykrx__v=1.csv",
        root / f"master__src=pykrx__asof={asof}__v=1.parquet",
        root / f"master__src=pykrx__asof={asof}__v=1.csv",
        root / f"universe__asof={asof}__metric=revenue_op__v=1.parquet",
        root / f"universe__asof={asof}__metric=revenue_op__v=1.csv",
        root / f"features_phase1__asof={asof}__src=phase1__v=1.parquet",
        root / f"features_phase1__asof={asof}__src=phase1__v=1.csv",
        root / f"features_live__asof={asof}__metric=revenue_op__v=2.parquet",
        root / f"features_live__asof={asof}__metric=revenue_op__v=3.parquet",
    ]
    candidates.extend([p for p in explicit if p.exists()])

    for pat in [
        f"krx_master__asof={asof}__src=pykrx__v=*.*",
        f"*master*asof={asof}*.*",
        f"universe__asof={asof}__*.csv",
        f"universe__asof={asof}__*.parquet",
    ]:
        candidates.extend(sorted(root.glob(pat)))

    seen = set()
    for p in candidates:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        try:
            df = _read_any_table(p)
            out = _name_map_from_df(df)
            if len(out) > 0:
                print(f"[OK] name map loaded: {p} rows={len(out)}")
                return out
        except Exception as e:
            print(f"[WARN] failed reading name source {p}: {e}")
            continue

    if feat is not None and "ticker" in feat.columns:
        try:
            from pykrx import stock  # type: ignore

            ticks = sorted(set(normalize_ticker_series(feat["ticker"]).dropna().tolist()))
            rows = []
            for t in ticks:
                try:
                    nm = stock.get_market_ticker_name(t)
                except Exception:
                    nm = None
                if nm and str(nm).strip() and str(nm) != "None":
                    rows.append((t, str(nm)))
            out = pd.DataFrame(rows, columns=["ticker", "name"]).drop_duplicates("ticker")
            if len(out) > 0:
                print(f"[OK] name map loaded via pykrx fallback rows={len(out)}")
                return out
        except Exception as e:
            print(f"[WARN] pykrx name fallback failed: {e}")

    print("[WARN] name map not found; names may remain missing")
    return pd.DataFrame(columns=["ticker", "name"])


def attach_names(df: pd.DataFrame, name_map: pd.DataFrame) -> pd.DataFrame:
    if "ticker" not in df.columns or name_map is None or name_map.empty:
        return df
    x = df.copy()
    x["ticker"] = normalize_ticker_series(x["ticker"])
    if "name" not in x.columns:
        x = x.merge(name_map, on="ticker", how="left")
    else:
        x = x.merge(name_map, on="ticker", how="left", suffixes=("", "_map"))
        x["name"] = x["name"].where(x["name"].notna(), x["name_map"])
        if "name_map" in x.columns:
            x = x.drop(columns=["name_map"])
    return x


def load_strategy(p_yaml: Path, name: str) -> Tuple[Dict[str, float], Dict[str, Any], str]:
    if yaml is None:
        raise RuntimeError("pyyaml not installed. Run: pip install pyyaml")

    cfg = _load_yaml_unique(p_yaml)
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
    if isinstance(s.get("weights", None), dict):
        weights = {str(k): float(v) for k, v in s["weights"].items()}

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

    if weights is None:
        maybe = {k: v for k, v in s.items() if k not in {"filters", "desc", "weights", "components"}}
        if maybe and all(isinstance(v, (int, float)) for v in maybe.values()):
            weights = {str(k): float(v) for k, v in maybe.items()}

    if not weights or not isinstance(weights, dict):
        raise ValueError("strategy config has no usable 'weights' (or legacy 'components').")

    weights = {k: float(v) for k, v in weights.items() if float(v) != 0.0}
    if not weights:
        raise ValueError("strategy weights are empty (all zeros).")

    return weights, filters, desc


def load_strategy_runtime(p_yaml: Path, name: str) -> Dict[str, Any]:
    if yaml is None:
        return {}
    cfg = _load_yaml_unique(p_yaml)
    strategies = cfg.get("strategies", {}) if isinstance(cfg, dict) else {}
    s = strategies.get(name, {}) if isinstance(strategies, dict) else {}
    scoring = s.get("scoring", {}) if isinstance(s.get("scoring", {}), dict) else {}
    selection = s.get("selection", {}) if isinstance(s.get("selection", {}), dict) else {}
    out: Dict[str, Any] = {}
    if "clip_z" in scoring:
        try:
            out["clip_z"] = float(scoring["clip_z"])
        except Exception:
            pass
    if "clip_tiers" in scoring and isinstance(scoring.get("clip_tiers"), list):
        out["clip_tiers"] = list(scoring.get("clip_tiers") or [])
    if "hold_bonus" in scoring:
        try:
            out["hold_bonus"] = float(scoring["hold_bonus"])
        except Exception:
            pass
    if "use_robust_z" in scoring:
        try:
            out["use_robust_z"] = bool(scoring["use_robust_z"])
        except Exception:
            pass
    if "raw_factors" in scoring and isinstance(scoring.get("raw_factors"), list):
        out["raw_factors"] = list(scoring.get("raw_factors") or [])
    if "portfolio_size" in selection:
        try:
            out["portfolio_size"] = int(selection["portfolio_size"])
        except Exception:
            pass
    if "keep_current_top_n" in selection:
        try:
            out["keep_current_top_n"] = int(selection["keep_current_top_n"])
        except Exception:
            pass
    return out


def apply_filters(df: pd.DataFrame, filters: Dict[str, Any], *, verbose: bool = False) -> pd.DataFrame:
    """
    Supported filter keys:
      - max_<col>
      - min_<col>
      - maxq_<col>
      - minq_<col>
      - require_notnull_<col>

    Safety rule:
      If a filter would wipe out the whole cohort, skip that filter.
    """
    if not filters:
        return df

    out = df.copy()
    for k, v in filters.items():
        cand = out

        if k.startswith("require_notnull_"):
            col = k[len("require_notnull_"):]
            if col not in out.columns:
                continue
            cand = out[out[col].notna()]

        elif k.startswith("maxq_") or k.startswith("minq_"):
            is_max = k.startswith("maxq_")
            col = k[len("maxq_"):] if is_max else k[len("minq_"):]
            if col not in out.columns:
                continue
            x = pd.to_numeric(out[col], errors="coerce")
            q = float(v)
            q = min(max(q, 0.0), 1.0)
            valid = x.dropna()
            if len(valid) == 0:
                if verbose:
                    print(f"[WARN] skip filter {k}={v}: no valid data in column '{col}'")
                continue
            thr = float(valid.quantile(q))
            cand = out[x.notna() & ((x <= thr) if is_max else (x >= thr))]

        elif k.startswith("max_"):
            col = k[len("max_"):]
            if col not in out.columns:
                continue
            x = pd.to_numeric(out[col], errors="coerce")
            cand = out[x.isna() | (x <= float(v))]

        elif k.startswith("min_"):
            col = k[len("min_"):]
            if col not in out.columns:
                continue
            x = pd.to_numeric(out[col], errors="coerce")
            cand = out[x.isna() | (x >= float(v))]

        else:
            continue

        if len(cand) == 0:
            if verbose:
                print(f"[WARN] skip filter {k}={v}: would empty the rebalance universe")
            continue

        out = cand.copy()

    return out




def select_target_portfolio(
    scored_sorted: pd.DataFrame,
    portfolio_size: int,
    keep_current_top_n: int,
    effective_group_col: Optional[str],
    max_per_group: int,
    current_tickers: set[str] | None = None,
) -> pd.DataFrame:
    current_tickers = set(current_tickers or set())
    portfolio_size = max(0, int(portfolio_size))
    keep_current_top_n = max(0, min(int(keep_current_top_n), portfolio_size))

    if portfolio_size == 0 or len(scored_sorted) == 0:
        return scored_sorted.head(0).copy()

    current_scored = scored_sorted[scored_sorted["ticker"].astype(str).isin(current_tickers)].copy()
    reserved = select_top_k_with_group_cap(current_scored, keep_current_top_n, effective_group_col, int(max_per_group)).copy()
    reserved_set = set(reserved["ticker"].astype(str).tolist())

    remaining_slots = max(0, portfolio_size - len(reserved))
    rest_pool = scored_sorted[~scored_sorted["ticker"].astype(str).isin(reserved_set)].copy()
    fill = select_top_k_with_group_cap(rest_pool, remaining_slots, effective_group_col, int(max_per_group)).copy()

    if len(reserved) > 0:
        reserved["selection_bucket"] = "keep_current_top_n"
        reserved["kept_from_previous"] = 1
    if len(fill) > 0:
        fill["selection_bucket"] = "new_or_rank_fill"
        fill["kept_from_previous"] = fill["ticker"].astype(str).isin(current_tickers).astype(int)

    out = pd.concat([reserved, fill], ignore_index=True)
    out = out.drop_duplicates(subset=["ticker"], keep="first")
    out = out.sort_values(["score_adj", "score", "ticker"], ascending=[False, False, True], na_position="last").reset_index(drop=True)
    return out


def detect_group_col(df: pd.DataFrame) -> Optional[str]:
    return _pick_first_existing(
        df.columns,
        ["industry4", "industry", "sector", "industry_name", "induty_code", "krx_industry", "peer_key", "group"],
    )


def load_group_map(asof: str) -> Tuple[pd.DataFrame, Optional[str], Optional[str]]:
    """
    External group source loader.
    Priority:
      1) krx_marketdata (industry4 / induty_code)
      2) shares_industry
      3) features_phase1 (processed / features)
      4) krx_master
    Returns: (group_map_df, source_name, group_col)
    """

    def _extract_asof_from_name(name: str) -> Optional[str]:
        import re
        m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
        return m.group(1) if m else None

    def _candidate_paths() -> list[tuple[Path, str]]:
        items: list[tuple[Path, str]] = []

        exact = [
            (Path(rf"data/processed/krx_marketdata__asof={asof}__src=pykrx__lookback=365d__v=1.parquet"), "krx_marketdata"),
            (Path(rf"data/processed/shares_industry__asof={asof}__src=dart__y=2025__reprt=11011__v=1.parquet"), "shares_industry"),
            (Path(rf"data/processed/features_phase1__asof={asof}__src=phase1__v=1.parquet"), "features_phase1_processed"),
            (Path(rf"data/features/features_phase1__asof={asof}__src=phase1__v=1.parquet"), "features_phase1_features"),
            (Path(rf"data/processed/krx_master__asof={asof}__src=pykrx__v=1.parquet"), "krx_master"),
        ]
        items.extend(exact)

        # fallback: latest <= asof
        patterns = [
            ("data/processed/krx_marketdata__asof=*__src=pykrx__lookback=365d__v=*.parquet", "krx_marketdata"),
            ("data/processed/shares_industry__asof=*__src=dart__*.parquet", "shares_industry"),
            ("data/processed/features_phase1__asof=*__src=phase1__v=*.parquet", "features_phase1_processed"),
            ("data/features/features_phase1__asof=*__src=phase1__v=*.parquet", "features_phase1_features"),
            ("data/processed/krx_master__asof=*__src=pykrx__v=*.parquet", "krx_master"),
        ]

        fallback: list[tuple[str, Path, str]] = []
        for pat, src_name in patterns:
            for p in Path(".").glob(pat):
                a = _extract_asof_from_name(p.name)
                if a and a <= asof:
                    fallback.append((a, p, src_name))

        fallback.sort(key=lambda x: (x[0], x[1].name), reverse=True)
        items.extend([(p, src_name) for _, p, src_name in fallback])

        return items

    seen = set()
    for path, src_name in _candidate_paths():
        if path in seen or not path.exists():
            continue
        seen.add(path)

        try:
            df = pd.read_parquet(path)
        except Exception as e:
            print(f"[WARN] failed to read group source {path}: {e}")
            continue

        if "ticker" not in df.columns:
            print(f"[WARN] group source has no ticker: {path}")
            continue

        gcol = detect_group_col(df)
        if not gcol:
            print(f"[WARN] no group-like column found in {path}")
            continue

        gm = df[["ticker", gcol]].copy()
        gm["ticker"] = normalize_ticker_series(gm["ticker"])
        gm[gcol] = gm[gcol].astype("string")
        gm = gm.dropna(subset=["ticker", gcol]).drop_duplicates(subset=["ticker"])

        if len(gm) == 0:
            print(f"[WARN] group source has group column but usable rows are zero: {path} ({gcol})")
            continue

        print(f"[OK] group map source selected: {path} rows={len(gm)} group_col={gcol}")
        return gm, src_name, gcol

    return pd.DataFrame(columns=["ticker"]), None, None


def _normalize_group_value(v: Any) -> str:
    if pd.isna(v):
        return "__NA_GROUP__"
    s = str(v).strip()
    return s if s else "__NA_GROUP__"


def select_top_k_with_group_cap(scored: pd.DataFrame, k: int, group_col: Optional[str], max_per_group: int) -> pd.DataFrame:
    if k <= 0:
        return scored.head(0).copy()
    if not group_col or group_col not in scored.columns or max_per_group <= 0:
        return scored.head(k).copy()

    rows = []
    counts: Dict[str, int] = {}
    for _, row in scored.iterrows():
        grp = _normalize_group_value(row[group_col])
        if counts.get(grp, 0) >= max_per_group:
            continue
        rows.append(row)
        counts[grp] = counts.get(grp, 0) + 1
        if len(rows) >= k:
            break

    out = pd.DataFrame(rows)
    if len(out) < k:
        picked_idx = set(out.index.tolist()) if len(out) else set()
        fill = scored.loc[~scored.index.isin(picked_idx)].head(k - len(out))
        out = pd.concat([out, fill], ignore_index=False)

    return out.head(k).copy()


def _find_prices_daily_file(asof: str, metric: str) -> Optional[Path]:
    root = Path("data/processed")
    exact = root / f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v=1.parquet"
    if exact.exists():
        return exact
    cands = sorted(root.glob(f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v=*.parquet"))
    if cands:
        return cands[-1]
    cands = sorted(root.glob(f"prices_daily__src=pykrx__start=*__asof={asof}__metric={metric}__v=*.parquet"))
    if cands:
        return cands[-1]
    return None


def load_price_history(asof: str, metric: str) -> pd.DataFrame:
    p = _find_prices_daily_file(asof, metric)
    if p is None or not p.exists():
        print("[WARN] prices_daily not found; current_price fields will be NaN")
        return pd.DataFrame(columns=["ticker", "date", "close"])

    px = pd.read_parquet(p)
    px["ticker"] = normalize_ticker_series(px["ticker"])
    dcol = _pick_first_existing(px.columns, ["date", "Date"])
    ccol = _pick_first_existing(px.columns, ["Close", "close", "종가"])
    if dcol is None or ccol is None:
        print(f"[WARN] prices_daily missing date/close columns: {list(px.columns)}")
        return pd.DataFrame(columns=["ticker", "date", "close"])

    out = px[["ticker", dcol, ccol]].copy()
    out = out.rename(columns={dcol: "date", ccol: "close"})
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["ticker", "date"]).sort_values(["ticker", "date"])
    print(f"[OK] prices_daily loaded: {p}")
    return out


def lookup_prices_on_or_before(px: pd.DataFrame, tickers: pd.Series, asof_date: pd.Timestamp) -> pd.DataFrame:
    tt = pd.DataFrame({"ticker": normalize_ticker_series(tickers).dropna().unique()})
    if px.empty:
        tt["current_price"] = np.nan
        tt["price_date"] = pd.NaT
        return tt

    sub = px.loc[px["date"] <= pd.Timestamp(asof_date), ["ticker", "date", "close"]].copy()
    if sub.empty:
        tt["current_price"] = np.nan
        tt["price_date"] = pd.NaT
        return tt

    last_px = sub.groupby("ticker", as_index=False).tail(1).rename(
        columns={"date": "price_date", "close": "current_price"}
    )
    return tt.merge(last_px, on="ticker", how="left")


def max_drawdown(nav: pd.Series) -> float:
    x = pd.to_numeric(nav, errors="coerce").astype("float64")
    if len(x) == 0:
        return float("nan")
    peak = x.cummax()
    dd = x / peak - 1.0
    return float(dd.min())


def build_forward_return_maps(
    ret: pd.DataFrame,
    rb_months: list[pd.Timestamp],
    months: list[pd.Timestamp],
    bt: pd.DataFrame,
) -> tuple[dict, dict, dict, dict]:
    ret2 = ret[["ticker", "month_end", "ret_1m"]].copy()
    ret2["ret_1m"] = pd.to_numeric(ret2["ret_1m"], errors="coerce").fillna(0.0)

    stock_fwd_map: dict[tuple[pd.Timestamp, str], float] = {}
    stock_end_map: dict[tuple[pd.Timestamp, str], pd.Timestamp] = {}
    port_gross_map: dict[pd.Timestamp, float] = {}
    port_net_map: dict[pd.Timestamp, float] = {}

    for i, rb in enumerate(rb_months):
        rb = pd.Timestamp(rb)
        next_rb = pd.Timestamp(rb_months[i + 1]) if i + 1 < len(rb_months) else None

        if next_rb is None:
            mask = ret2["month_end"] >= rb
            bt_mask = bt["month_end"] >= rb
            hold_end = pd.Timestamp(months[-1])
        else:
            mask = (ret2["month_end"] >= rb) & (ret2["month_end"] < next_rb)
            bt_mask = (bt["month_end"] >= rb) & (bt["month_end"] < next_rb)
            hold_end = pd.Timestamp(next_rb) - pd.offsets.MonthEnd(1)

        seg = ret2.loc[mask].copy()
        if len(seg):
            stock_cum = seg.groupby("ticker", as_index=False)["ret_1m"].apply(
                lambda x: float(np.prod(1.0 + x.to_numpy()) - 1.0)
            )
            stock_cum = stock_cum.rename(columns={"ret_1m": "stock_forward_ret"})
            for _, r in stock_cum.iterrows():
                stock_fwd_map[(rb, str(r["ticker"]))] = float(r["stock_forward_ret"])
                stock_end_map[(rb, str(r["ticker"]))] = hold_end

        bt_seg = bt.loc[bt_mask].copy()
        if len(bt_seg):
            port_gross_map[rb] = float(
                np.prod(1.0 + pd.to_numeric(bt_seg["ret"], errors="coerce").fillna(0.0).to_numpy()) - 1.0
            )
            port_net_map[rb] = float(
                np.prod(
                    1.0
                    + (
                        pd.to_numeric(bt_seg["ret"], errors="coerce").fillna(0.0)
                        - pd.to_numeric(bt_seg["tcost"], errors="coerce").fillna(0.0)
                    ).to_numpy()
                )
                - 1.0
            )
        else:
            port_gross_map[rb] = 0.0
            port_net_map[rb] = 0.0

    return stock_fwd_map, stock_end_map, port_gross_map, port_net_map


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
    ap.add_argument("--hold_bonus", type=float, default=None)
    ap.add_argument("--entry_gap", type=float, default=0.0)
    ap.add_argument("--max_per_group", type=int, default=0, help="0 means no cap. Example: 2")
    ap.add_argument("--group_col", default="", help="Optional explicit group column. Default: auto-detect industry4/industry/sector...")
    ap.add_argument("--save_holdings", action="store_true")
    ap.add_argument("--save_inputs", action="store_true")
    ap.add_argument("--clip_z", type=float, default=None, help="Clip standardized factor z-scores to +/- this value. Set negative to disable clipping. If omitted, use strategy yaml default when available.")
    ap.add_argument("--use_robust_z", action="store_true", default=None, help="Use median/MAD-based robust z-score instead of mean/std z-score. If omitted, use strategy yaml default when available.")
    ap.add_argument(
        "--filter_fallback",
        choices=["full", "error"],
        default="full",
        help="When filters remove all names in a rebalance bucket: full=fall back to unfiltered set (legacy), error=raise immediately.",
    )
    args = ap.parse_args()

    feat_candidates = [
        Path(rf"data/processed/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet"),
        Path(rf"data/features/features_live/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet"),
        Path(rf"data/features/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet"),
    ]
    p_feat = next((p for p in feat_candidates if p.exists()), feat_candidates[0])
    p_ret = Path(rf"data/processed/returns_monthly__src={args.ret_src}__asof={args.asof}__metric={args.metric}__v={args.ret_v}.parquet")

    out_bt = Path(rf"data/processed/bt__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.csv")
    out_pick = Path(rf"data/processed/picks__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.parquet")
    out_rep = Path(rf"data/processed/report__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.csv")
    out_hold = Path(rf"data/processed/holdings_snapshot__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.csv")
    out_inputs = Path(rf"data/processed/inputs_long__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.parquet")
    out_meta = Path(rf"data/processed/report_meta__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.json")
    out_audit = Path(rf"data/processed/rebalance_audit__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.out_v}.csv")

    for p in [out_bt, out_pick, out_rep, out_hold, out_inputs, out_meta, out_audit]:
        ensure_dir(p)

    if not p_feat.exists():
        raise FileNotFoundError(p_feat)
    if not p_ret.exists():
        raise FileNotFoundError(p_ret)

    feat = pd.read_parquet(p_feat)
    ret = pd.read_parquet(p_ret)
    print(f"[OK] returns loaded: {p_ret}")

    feat["ticker"] = normalize_ticker_series(feat["ticker"])
    ret["ticker"] = normalize_ticker_series(ret["ticker"])

    name_map = _load_name_map(args.asof, feat=feat)
    feat = attach_names(feat, name_map)

    # attach/fill external group classification if features_live does not already contain one
    group_map, group_src, external_group_col = load_group_map(args.asof)
    existing_group_col = detect_group_col(feat)

    if len(group_map) > 0 and external_group_col:
        if existing_group_col and existing_group_col in feat.columns:
            feat = feat.merge(
                group_map.rename(columns={external_group_col: f"{external_group_col}__ext"}),
                on="ticker",
                how="left",
            )
            ext_col = f"{external_group_col}__ext"
            if ext_col in feat.columns:
                feat[existing_group_col] = feat[existing_group_col].where(feat[existing_group_col].notna(), feat[ext_col])
                feat = feat.drop(columns=[ext_col])
            nn = int(feat[existing_group_col].notna().sum())
            print(f"[OK] group map filled from {group_src}: group_col={existing_group_col} non_null={nn}/{len(feat)}")
        else:
            feat = feat.merge(group_map, on="ticker", how="left")
            nn = int(feat[external_group_col].notna().sum()) if external_group_col in feat.columns else 0
            print(f"[OK] group map merged from {group_src}: group_col={external_group_col} non_null={nn}/{len(feat)}")
    elif args.max_per_group > 0:
        print("[WARN] no external group map available; group cap may not be applied")

    price_hist = load_price_history(args.asof, args.metric)

    def _git_hash() -> str:
        try:
            return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
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
        "hold_bonus": float(args.hold_bonus) if args.hold_bonus is not None else 0.0,
        "entry_gap": float(args.entry_gap),
        "max_per_group": int(args.max_per_group),
        "group_col_requested": str(args.group_col),
        "git_hash": _git_hash(),
    }
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] report_meta saved: {out_meta}")

    if "month_end" not in ret.columns and "month" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month"]).dt.to_period("M").dt.to_timestamp("M")
    else:
        ret["month_end"] = pd.to_datetime(ret["month_end"])

    if "name" in feat.columns:
        nm2 = feat[["ticker", "name"]].dropna(subset=["name"]).drop_duplicates("ticker")
        if len(nm2) > 0:
            name_map = nm2

    feat["quarter_key"] = feat["year"].astype(int) * 100 + feat["quarter"].astype(int)
    feat["rebalance_month"] = compute_rebalance_month_from_yq(feat["year"], feat["quarter"])

    group_col = args.group_col.strip() or detect_group_col(feat)
    if group_col and group_col in feat.columns:
        print(f"[OK] group column detected: {group_col}")
    elif args.max_per_group > 0:
        print("[WARN] group cap requested but no group column detected; proceeding without cap")
        group_col = None

    strat_path = Path("configs/strategies.yaml")
    if not strat_path.exists():
        strat_path = Path("strategies.yaml")
    if not strat_path.exists():
        raise FileNotFoundError(strat_path)

    weights, filters, desc = load_strategy(strat_path, args.strategy)
    runtime_cfg = load_strategy_runtime(strat_path, args.strategy)
    resolved_hold_bonus = float(args.hold_bonus) if args.hold_bonus is not None else float(runtime_cfg.get("hold_bonus", 0.0))
    resolved_clip_z = args.clip_z if args.clip_z is not None else runtime_cfg.get("clip_z", 5.0)
    resolved_use_robust_z = bool(args.use_robust_z) if args.use_robust_z is not None else bool(runtime_cfg.get("use_robust_z", False))
    resolved_clip_tiers = list(runtime_cfg.get("clip_tiers", [])) if isinstance(runtime_cfg.get("clip_tiers", []), list) else []
    resolved_raw_factors = list(runtime_cfg.get("raw_factors", [])) if isinstance(runtime_cfg.get("raw_factors", []), list) else []
    if not resolved_raw_factors:
        resolved_raw_factors = [c for c in weights.keys() if c in {"op_growth_streak2", "rev_growth_streak2"}]
    resolved_portfolio_size = int(runtime_cfg.get("portfolio_size", args.k)) if runtime_cfg else int(args.k)
    resolved_keep_current_top_n = int(runtime_cfg.get("keep_current_top_n", 0)) if runtime_cfg else 0
    meta["hold_bonus"] = resolved_hold_bonus
    meta["clip_z"] = resolved_clip_z
    meta["clip_tiers"] = resolved_clip_tiers
    meta["raw_factors"] = resolved_raw_factors
    meta["use_robust_z"] = resolved_use_robust_z
    meta["portfolio_size"] = resolved_portfolio_size
    meta["keep_current_top_n"] = resolved_keep_current_top_n
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] strategies_yaml: {strat_path}")
    if desc:
        print(f"[INFO] strategy desc: {desc}")

    def make_scores(g: pd.DataFrame, rm: pd.Timestamp) -> pd.DataFrame:
        gg = g.copy()
        gg_filtered = apply_filters(gg, filters, verbose=True)

        if "op_qoq" in gg_filtered.columns:
            op_qoq_num = pd.to_numeric(gg_filtered["op_qoq"], errors="coerce")
            strict_qoq = gg_filtered.loc[op_qoq_num.notna() & (op_qoq_num > 0)].copy()
            if len(strict_qoq) == 0:
                print(f"[WARN] skip strict filter op_qoq > 0 for rebalance_month={pd.Timestamp(rm).date()}: would empty the rebalance universe")
            else:
                gg_filtered = strict_qoq

        if len(gg_filtered) == 0:
            msg = f"All rows filtered out for rebalance_month={pd.Timestamp(rm).date()} with filters={filters}"
            if args.filter_fallback == "error":
                raise ValueError(msg)
            print(f"[WARN] {msg}; falling back to unfiltered universe (legacy behavior)")
            gg = g.copy()
        else:
            gg = gg_filtered.copy()

        gg["score_total"] = 0.0
        used_cols = []
        raw_factor_set = set(resolved_raw_factors or [])

        def _mask_mult_for(col: str) -> pd.Series:
            if ("CFO" in col) or ("Quality_CFO" in col) or ("CFO_to_Assets" in col):
                isnull = pd.to_numeric(gg.get("CFO_isnull", 0.0), errors="coerce").fillna(0.0)
                warn = pd.to_numeric(gg.get("CFO_warn", 0.0), errors="coerce").fillna(0.0)
                return (1.0 - 0.7 * isnull - 0.4 * warn).clip(0.0, 1.0).astype("float64")
            return pd.Series(1.0, index=gg.index, dtype="float64")

        for c, ww in weights.items():
            ww = float(ww)
            if ww == 0.0 or c not in gg.columns:
                continue

            used_cols.append(c)
            raw = pd.to_numeric(gg[c], errors="coerce")
            if c in raw_factor_set:
                z = raw.fillna(0.0).astype("float64")
                rk = z.rank(ascending=False, method="min")
                mult = pd.Series(1.0, index=gg.index, dtype="float64")
                contrib = ww * z
            else:
                z_base = standardize_factor(safe_fill_for_z(raw), use_robust_z=resolved_use_robust_z, clip_z=None)
                z = apply_piecewise_clip(z_base, clip_z=None if resolved_clip_z is None or resolved_clip_z < 0 else resolved_clip_z, clip_tiers=resolved_clip_tiers)
                rk = z.rank(ascending=False, method="min")
                mult = _mask_mult_for(c)
                contrib = ww * z * mult

            gg[f"{c}__raw"] = raw
            gg[f"{c}__z"] = z
            gg[f"{c}__rank"] = rk
            gg[f"{c}__mult"] = mult
            gg[f"{c}__contrib"] = contrib
            gg["score_total"] += contrib

        gg["score"] = gg["score_total"]
        gg["score_rank"] = gg["score_total"].rank(ascending=False, method="min")
        gg["rebalance_month"] = rm

        keep = ["ticker", "rebalance_month", "score_total", "score", "score_rank"]
        for c in used_cols:
            for suf in ("__raw", "__z", "__rank", "__mult", "__contrib"):
                col = f"{c}{suf}"
                if col in gg.columns:
                    keep.append(col)

        for c in ["name", "corp_code", "year", "quarter", "CFO_isnull", "CFO_warn", group_col]:
            if c and c in gg.columns and c not in keep:
                keep.insert(1, c)

        out = gg[keep]
        out = attach_names(out, name_map)
        return out

    def apply_hysteresis(scored: pd.DataFrame, prev_hold: Optional[set]) -> pd.DataFrame:
        out = scored.copy()
        out["score_adj"] = out["score"]
        out["hold_bonus_applied"] = 0.0
        if prev_hold and resolved_hold_bonus != 0:
            mask = out["ticker"].isin(prev_hold)
            out.loc[mask, "score_adj"] += float(resolved_hold_bonus)
            out.loc[mask, "hold_bonus_applied"] = float(resolved_hold_bonus)
        return out

    # holdings_rows: always keep for audit
    holdings_rows = []
    inputs_rows = []
    all_picks = []

    prev_hold = None
    prev_weights = None
    turnover_rows = []

    months = [pd.Timestamp(m) for m in sorted(ret["month_end"].dropna().unique())]
    rb_months = [
        pd.Timestamp(m)
        for m in sorted(feat["rebalance_month"].dropna().unique())
        if (m >= months[0]) and (m <= months[-1])
    ]
    if not rb_months:
        raise RuntimeError("No rebalance months overlap between features and returns range.")

    for rm in rb_months:
        g = feat[feat["rebalance_month"] == rm].copy()
        if len(g) == 0:
            continue

        scored = make_scores(g, rm)
        scored["score"] = scored["score_total"]
        scored = apply_hysteresis(scored, prev_hold)
        scored = scored.sort_values(["score_adj", "score", "ticker"], ascending=[False, False, True], na_position="last").reset_index(drop=True)

        effective_group_col = group_col if (group_col and group_col in scored.columns) else detect_group_col(scored)
        picked = select_target_portfolio(
            scored,
            portfolio_size=resolved_portfolio_size,
            keep_current_top_n=resolved_keep_current_top_n,
            effective_group_col=effective_group_col,
            max_per_group=int(args.max_per_group),
            current_tickers=prev_hold or set(),
        )
        picked["rebalance_month_end"] = picked["rebalance_month"]

        if args.entry_gap > 0 and prev_hold:
            keepers = picked[picked["ticker"].isin(prev_hold)].copy()
            newcomers = picked[~picked["ticker"].isin(prev_hold)].copy()
            if len(newcomers) > 0 and len(keepers) > 0:
                cutoff = keepers["score_adj"].min()
                newcomers = newcomers[newcomers["score_adj"] >= (cutoff + float(args.entry_gap))]
                tmp = pd.concat([keepers, newcomers], ignore_index=True).sort_values("score_adj", ascending=False)
                effective_group_col = group_col if (group_col and group_col in tmp.columns) else detect_group_col(tmp)
                picked = select_target_portfolio(tmp, resolved_portfolio_size, resolved_keep_current_top_n, effective_group_col, int(args.max_per_group), prev_hold or set())
            if len(picked) < resolved_portfolio_size:
                effective_group_col = group_col if (group_col and group_col in scored.columns) else detect_group_col(scored)
                picked = select_target_portfolio(scored, resolved_portfolio_size, resolved_keep_current_top_n, effective_group_col, int(args.max_per_group), prev_hold or set())
                picked["rebalance_month_end"] = picked["rebalance_month"]

        n_hold = max(len(picked), 1)
        picked["w"] = 1.0 / n_hold
        picked = attach_names(picked, name_map)

        if group_col and group_col in picked.columns:
            picked["group_value"] = picked[group_col].map(_normalize_group_value)
        else:
            picked["group_value"] = "__NO_GROUP__"

        px_now = lookup_prices_on_or_before(price_hist, picked["ticker"], rm)
        picked = picked.merge(px_now, on="ticker", how="left")

        cur_weights = picked[["ticker", "w"]].copy()
        if prev_weights is None:
            turnover = float(cur_weights["w"].abs().sum())
        else:
            tw = prev_weights.merge(cur_weights, on="ticker", how="outer", suffixes=("_prev", "_cur")).fillna(0.0)
            turnover = float((tw["w_cur"] - tw["w_prev"]).abs().sum() / 2.0)

        turnover_rows.append({"rebalance_month": rm, "turnover": turnover})
        prev_weights = cur_weights.copy()

        if args.save_inputs:
            tmp = picked.copy()
            tmp["selected"] = 1
            tmp["asof"] = args.asof
            tmp["metric"] = args.metric
            tmp["strategy"] = args.strategy
            tmp["k"] = int(args.k)
            if "rebalance_month_end" in tmp.columns:
                tmp = tmp.drop(columns=["rebalance_month_end"])
            tmp["rebalance_month_end"] = tmp["rebalance_month"]
            inputs_rows.append(tmp)

        holdings_rows.append(picked.copy())

        prev_hold = set(picked["ticker"].tolist())
        pick_cols = [c for c in ["ticker", "name", group_col, "group_value", "rebalance_month_end", "w", "score", "score_adj", "current_price", "price_date"] if c and c in picked.columns]
        all_picks.append(
            picked[pick_cols].rename(columns={"rebalance_month_end": "rebalance_month"})
        )

    if not all_picks:
        raise RuntimeError("No picks were generated. Check filters / overlap / input data.")

    picks = pd.concat(all_picks, ignore_index=True)
    picks.to_parquet(out_pick, index=False)
    print(f"[OK] picks saved: {out_pick}")

    if args.save_holdings and holdings_rows:
        hold_df = pd.concat(holdings_rows, ignore_index=True)
        hold_df = attach_names(hold_df, name_map)
        hold_df.to_csv(out_hold, index=False, encoding="utf-8-sig")
        print(f"[OK] holdings snapshot saved: {out_hold}")

    if args.save_inputs and inputs_rows:
        exp_df = pd.concat(inputs_rows, ignore_index=True)
        exp_df = attach_names(exp_df, name_map)
        exp_df = exp_df.merge(
            ret[["ticker", "month_end", "ret_1m"]],
            left_on=["ticker", "rebalance_month_end"],
            right_on=["ticker", "month_end"],
            how="left",
        ).drop(columns=["month_end"])
        exp_df.to_parquet(out_inputs, index=False)
        print(f"[OK] inputs_long saved: {out_inputs}")

    ret2 = ret[["ticker", "month_end", "ret_1m"]].copy()
    ret2["ret_1m"] = pd.to_numeric(ret2["ret_1m"], errors="coerce").fillna(0.0)
    rb_series = pd.Series(sorted(picks["rebalance_month"].unique())).sort_values()
    turnover_map = {pd.Timestamp(r["rebalance_month"]): float(r["turnover"]) for r in turnover_rows}

    def last_rb(m: pd.Timestamp) -> pd.Timestamp:
        idx = rb_series.searchsorted(m, side="right") - 1
        return rb_series.iloc[0] if idx < 0 else rb_series.iloc[int(idx)]

    port_rows = []
    nav_gross = 1.0
    nav_net = 1.0

    for m in months:
        rb = last_rb(m)
        w = picks[picks["rebalance_month"] == rb][["ticker", "w"]].copy()
        r = ret2[ret2["month_end"] == m].merge(w, on="ticker", how="inner")
        port_ret = 0.0 if len(r) == 0 else float((r["w"] * r["ret_1m"]).sum())

        tcost = 0.0
        turnover = 0.0
        if m == rb and args.tcost_bps:
            turnover = turnover_map.get(pd.Timestamp(rb), 0.0)
            tcost = turnover * float(args.tcost_bps) / 10000.0

        nav_gross *= (1.0 + port_ret)
        nav_net *= (1.0 + port_ret - tcost)

        port_rows.append(
            {
                "month_end": m,
                "rebalance_month": rb,
                "ret": port_ret,
                "turnover": turnover,
                "tcost": tcost,
                "nav_gross": nav_gross,
                "nav_net": nav_net,
            }
        )

    bt = pd.DataFrame(port_rows)
    bt.to_csv(out_bt, index=False, encoding="utf-8-sig")
    print(f"[OK] bt saved: {out_bt}")

    stock_fwd_map, stock_end_map, port_gross_map, port_net_map = build_forward_return_maps(ret, rb_months, months, bt)

    audit_parts = []
    for picked in holdings_rows:
        aud = picked.copy()
        aud["hold_start_month"] = aud["rebalance_month_end"]
        aud["stock_forward_ret_to_next_rebalance"] = [
            stock_fwd_map.get((pd.Timestamp(rm), str(tk)), np.nan)
            for rm, tk in zip(aud["rebalance_month_end"], aud["ticker"])
        ]
        aud["hold_end_month"] = [
            stock_end_map.get((pd.Timestamp(rm), str(tk)), pd.NaT)
            for rm, tk in zip(aud["rebalance_month_end"], aud["ticker"])
        ]
        aud["portfolio_gross_ret_to_next_rebalance"] = aud["rebalance_month_end"].map(
            lambda x: port_gross_map.get(pd.Timestamp(x), np.nan)
        )
        aud["portfolio_net_ret_to_next_rebalance"] = aud["rebalance_month_end"].map(
            lambda x: port_net_map.get(pd.Timestamp(x), np.nan)
        )
        audit_parts.append(aud)

    if audit_parts:
        audit_df = pd.concat(audit_parts, ignore_index=True)

        first_metric_cols = []
        for c in weights.keys():
            raw_col = f"{c}__raw"
            contrib_col = f"{c}__contrib"
            if raw_col in audit_df.columns:
                first_metric_cols.append(raw_col)
            if contrib_col in audit_df.columns:
                first_metric_cols.append(contrib_col)

        ordered = [
            c
            for c in [
                "ticker",
                "name",
                group_col,
                "group_value",
                "year",
                "quarter",
                "rebalance_month",
                "rebalance_month_end",
                "hold_start_month",
                "hold_end_month",
                "w",
                "score",
                "score_adj",
                "hold_bonus_applied",
                "score_rank",
                "current_price",
                "price_date",
                "CFO_isnull",
                "CFO_warn",
            ]
            if c and c in audit_df.columns
        ]
        ordered += [c for c in first_metric_cols if c in audit_df.columns and c not in ordered]
        ordered += [
            c
            for c in [
                "stock_forward_ret_to_next_rebalance",
                "portfolio_gross_ret_to_next_rebalance",
                "portfolio_net_ret_to_next_rebalance",
            ]
            if c in audit_df.columns and c not in ordered
        ]
        tail = [c for c in audit_df.columns if c not in ordered]
        audit_df = audit_df[ordered + tail]
        audit_df.to_csv(out_audit, index=False, encoding="utf-8-sig")
        print(f"[OK] rebalance audit saved: {out_audit}")

    n_months = len(bt)
    years = n_months / 12.0 if n_months > 0 else np.nan
    gross_nav = float(bt["nav_gross"].iloc[-1]) if n_months else 1.0
    net_nav = float(bt["nav_net"].iloc[-1]) if n_months else 1.0
    cagr_gross = (gross_nav ** (1 / years) - 1) if years and years > 0 else np.nan
    cagr_net = (net_nav ** (1 / years) - 1) if years and years > 0 else np.nan

    sharpe_net = np.nan
    if n_months > 1:
        ex = bt["ret"] - bt["tcost"]
        mu = ex.mean()
        sd = ex.std(ddof=1)
        if sd > 0:
            sharpe_net = (mu / sd) * np.sqrt(12)

    avg_turnover = (
        float(bt.loc[bt["turnover"] > 0, "turnover"].mean())
        if "turnover" in bt.columns and (bt["turnover"] > 0).any()
        else np.nan
    )
    maxdd_net = max_drawdown(bt["nav_net"]) if "nav_net" in bt.columns else np.nan

    rep = pd.DataFrame(
        [
            {
                "asof": args.asof,
                "metric": args.metric,
                "strategy": args.strategy,
                "k": args.k,
                "tcost_bps": float(args.tcost_bps),
                "max_per_group": int(args.max_per_group),
                "group_col": group_col,
                "gross_nav": gross_nav,
                "net_nav": net_nav,
                "cagr_gross": cagr_gross,
                "cagr_net": cagr_net,
                "sharpe_net": sharpe_net,
                "maxdd_net": maxdd_net,
                "avg_turnover": avg_turnover,
            }
        ]
    )
    rep.to_csv(out_rep, index=False, encoding="utf-8-sig")
    print(f"[OK] report saved: {out_rep}")
    print(
        f"[INFO] gross NAV: {gross_nav} net NAV: {net_nav} "
        f"CAGR gross: {cagr_gross} CAGR net: {cagr_net} Sharpe net: {sharpe_net}"
    )


if __name__ == "__main__":
    main()