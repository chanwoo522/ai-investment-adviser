
from __future__ import annotations

# ---- path bootstrap ----
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ------------------------

import argparse
from pathlib import Path
from typing import Iterable
import html
import math
import re
import pandas as pd
import numpy as np
from common.industry_map import attach_industry, load_industry_code_name_map, standardize_industry_columns

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


# ----------------------------
# basic utils
# ----------------------------
def _first_series(df: pd.DataFrame, col: str) -> pd.Series:
    x = df[col]
    if isinstance(x, pd.DataFrame):
        return x.iloc[:, 0]
    return x

def _dedupe_columns_keep_first(df: pd.DataFrame) -> pd.DataFrame:
    return df.loc[:, ~df.columns.duplicated(keep="first")].copy()


def _normalize_missing_series(s: pd.Series, *, stringy: bool = False) -> pd.Series:
    if stringy:
        return s.astype("string").replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA}).str.strip()
    return pd.to_numeric(s, errors="coerce")


def coalesce_prefer(primary: pd.Series, fallback: pd.Series, *, stringy: bool = False) -> pd.Series:
    p = _normalize_missing_series(primary, stringy=stringy)
    f = _normalize_missing_series(fallback.reindex(primary.index), stringy=stringy)
    return p.where(p.notna(), f)

def ensure_industry_columns(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    out = df.copy()
    try:
        attached = attach_industry(out, prefer_reference=True)
        if attached is not None:
            out = attached
    except TypeError:
        try:
            attached = attach_industry(out)
            if attached is not None:
                out = attached
        except Exception:
            pass
    except Exception:
        pass

    try:
        standardized = standardize_industry_columns(out)
        if standardized is not None:
            out = standardized
    except Exception:
        pass
    return out

def normalize_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def pick_first(cols: Iterable[str], candidates: list[str]) -> str | None:
    cols = list(cols)
    for c in candidates:
        if c in cols:
            return c
    return None


def safe_num(x):
    try:
        if pd.isna(x):
            return None
        return float(x)
    except Exception:
        return None


def fmt_num(x, digits: int = 0) -> str:
    v = safe_num(x)
    if v is None:
        return "-"
    return f"{v:,.{digits}f}"


def fmt_ratio(x, digits: int = 2) -> str:
    v = safe_num(x)
    if v is None:
        return "-"
    return f"{v:,.{digits}f}"


def fmt_large_krw(x) -> str:
    v = safe_num(x)
    if v is None:
        return "-"
    abs_v = abs(v)
    if abs_v >= 1e12:
        return f"{v/1e12:,.2f}조원"
    if abs_v >= 1e8:
        return f"{v/1e8:,.0f}억원"
    if abs_v >= 1e6:
        return f"{v/1e6:,.0f}백만원"
    return f"{v:,.0f}원"


def fmt_int_plain(x) -> str:
    v = safe_num(x)
    if v is None:
        return "-"
    return str(int(v))


def text_or_dash(x) -> str:
    if pd.isna(x):
        return "-"
    s = str(x).strip()
    if s in {"", "<NA>", "nan", "None"}:
        return "-"
    return s


def coalesce_series(left: pd.Series, right: pd.Series) -> pd.Series:
    left_aligned = left.copy()
    right_aligned = right.reindex(left_aligned.index)
    return left_aligned.where(~left_aligned.isna(), right_aligned)



def load_any_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {p}")
    if p.suffix.lower() == ".parquet":
        return pd.read_parquet(p)
    if p.suffix.lower() in [".csv", ".txt"]:
        return pd.read_csv(p)
    raise ValueError(f"unsupported file type: {p.suffix}")


def write_text(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_output_file_path(
    base_path: str | Path,
    *,
    asof: str,
    target_date: str,
    metric: str | None,
    strategy: str,
    version_tag: str | None = None,
    default_basename: str | None = None,
    default_suffix: str | None = None,
) -> Path:
    """
    Output path normalization rules.

    1) If caller passes a full filename with a known suffix (.md/.csv/.html),
       trust it and return as-is.
    2) If caller passes an existing directory path, create a default filename under it.
    3) If caller passes a suffix-less path, treat it like a directory-like destination
       and create a default filename under it.

    This preserves backward compatibility while preventing duplicated
    __asof/__target/__metric/__strat/__v suffixes when the caller already
    passed a fully materialized filename.
    """
    p = Path(base_path)

    # Case 1: explicit filename already provided
    if p.suffix.lower() in {".md", ".csv", ".html"}:
        return p

    if not default_basename:
        raise ValueError("default_basename is required when output path is not a full filename")
    if not default_suffix:
        raise ValueError("default_suffix is required when output path is not a full filename")

    parts = [f"__asof={asof}", f"__target={target_date}"]
    if metric:
        parts.append(f"__metric={metric}")
    parts.append(f"__strat={strategy}")
    if version_tag:
        parts.append(f"__v={version_tag}")

    filename = default_basename + "".join(parts) + default_suffix

    # Case 2: explicit existing directory
    if p.exists() and p.is_dir():
        return p / filename

    # Case 3: suffix-less path; current CLI historically used directory-like outputs
    if p.suffix == "":
        return p / filename

    # Fallback: respect caller path
    return p


def auto_detect_marketdata_path(asof: str) -> Path | None:
    search_roots = [Path("data/interim/marketdata"), Path("data/processed")]
    for root in search_roots:
        cands = sorted(root.glob(f"krx_marketdata__asof={asof}__src=pykrx__lookback=*__v=*.parquet"))
        if cands:
            return cands[-1]
    for root in search_roots:
        cands = sorted(root.glob("krx_marketdata__asof=*__src=pykrx__lookback=*__v=*.parquet"))
        if cands:
            return cands[-1]
    return None


def load_industry_name_map(reference_csv: str | Path | None = None) -> pd.DataFrame:
    try:
        return load_industry_code_name_map(reference_csv)
    except Exception:
        return pd.DataFrame(columns=["industry_code", "industry_name"])


def merge_industry_reference(detail: pd.DataFrame, asof: str | None = None, reference_csv: str | Path | None = None) -> pd.DataFrame:
    out = detail.copy()
    try:
        standardized = standardize_industry_columns(out)
        if standardized is not None:
            out = standardized
    except Exception:
        pass

    try:
        attached = attach_industry(out, reference_csv=reference_csv, prefer_reference=True)
        if attached is not None:
            out = attached
    except Exception as e:
        print(f"[WARN] attach_industry in report merge failed: {e}")

    # 1) ticker -> industry4 from features_phase1
    feat_paths = []
    if asof:
        feat_paths.append(Path(f"data/features/features_phase1__asof={asof}__src=phase1__v=1.parquet"))
    for p in feat_paths:
        if p.exists():
            try:
                df = pd.read_parquet(p)
                if {"ticker", "industry_code"}.issubset(df.columns):
                    mp = df[["ticker", "industry_code"]].copy()
                    mp["ticker"] = normalize_ticker(mp["ticker"])
                    mp["industry_code"] = pd.to_numeric(mp["industry_code"], errors="coerce")
                    mp = mp.dropna(subset=["ticker"]).drop_duplicates("ticker", keep="last")
                    out = out.merge(mp.rename(columns={"industry_code": "industry_code__phase1"}), on="ticker", how="left")
                    if "industry_code" in out.columns:
                        out["industry_code"] = pd.to_numeric(out["industry_code"], errors="coerce")
                        out["industry_code"] = coalesce_series(out["industry_code"], out["industry_code__phase1"])
                    else:
                        out["industry_code"] = out["industry_code__phase1"]
                    out = out.drop(columns=["industry_code__phase1"], errors="ignore")
                    break
            except Exception as e:
                print(f"[WARN] failed phase1 industry attach: {e}")

    # 2) fallback ticker -> industry4 from marketdata
    if asof:
        md = auto_detect_marketdata_path(asof)
        if md and md.exists():
            try:
                df = pd.read_parquet(md)
                if {"ticker", "industry_code"}.issubset(df.columns):
                    mp = df[["ticker", "industry_code"]].copy()
                    mp["ticker"] = normalize_ticker(mp["ticker"])
                    mp["industry_code"] = pd.to_numeric(mp["industry_code"], errors="coerce")
                    mp = mp.dropna(subset=["ticker"]).drop_duplicates("ticker", keep="last")
                    out = out.merge(mp.rename(columns={"industry_code": "industry_code__md"}), on="ticker", how="left")
                    out["industry_code"] = pd.to_numeric(out.get("industry_code"), errors="coerce")
                    out["industry_code"] = coalesce_series(out["industry_code"], out["industry_code__md"])
                    out = out.drop(columns=["industry_code__md"], errors="ignore")
            except Exception as e:
                print(f"[WARN] failed marketdata industry attach: {e}")

    # 3) industry_code -> industry_name from reference csv
    ref = load_industry_name_map(reference_csv)
    if len(ref) > 0 and "industry_code" in out.columns:
        out["industry_code"] = (
            pd.to_numeric(out["industry_code"], errors="coerce")
            .astype("Int64")
            .astype(str)
            .replace("<NA>", pd.NA)
        )

        ref = ref.copy()
        ref["industry_code"] = (
            pd.to_numeric(ref["industry_code"], errors="coerce")
            .astype("Int64")
            .astype(str)
            .replace("<NA>", pd.NA)
        )

        ref = ref.dropna(subset=["industry_code"]).drop_duplicates("industry_code", keep="last")

        out = out.merge(
            ref.rename(columns={"industry_name": "industry_name__ref"}),
            on="industry_code",
            how="left",
        )
        if "report_industry_name" not in out.columns:
            out["report_industry_name"] = pd.NA
        out["industry_name__ref"] = out["industry_name__ref"].astype("string").str.strip().replace("", pd.NA)
        out["report_industry_name"] = out["report_industry_name"].astype("string").str.strip().replace("", pd.NA)
        out["report_industry_name"] = coalesce_prefer(
            out["report_industry_name"],
            out["industry_name__ref"],
            stringy=True,
        )
        out = out.drop(columns=["industry_name__ref"], errors="ignore")

    return out



def enforce_industry_name_from_code(detail: pd.DataFrame, reference_csv: str | Path | None = None) -> pd.DataFrame:
    out = detail.copy()
    if "industry_code" not in out.columns:
        return out

    ref = load_industry_name_map(reference_csv)
    if ref is None or len(ref) == 0:
        return out
    if not {"industry_code", "industry_name"}.issubset(ref.columns):
        return out

    ref = ref.copy()
    ref["industry_code"] = ref["industry_code"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    ref["industry_name"] = (
        ref["industry_name"]
        .astype("string")
        .str.strip()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    )
    ref = ref.dropna(subset=["industry_code"]).drop_duplicates("industry_code", keep="last")

    code_to_name = ref.set_index("industry_code")["industry_name"]

    out["industry_code"] = out["industry_code"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    mapped_name = out["industry_code"].map(code_to_name)

    if "report_industry_name" not in out.columns:
        out["report_industry_name"] = pd.NA

    # ?? C: ?? ??? ???? industry_code ???? ??
    out["report_industry_name"] = coalesce_prefer(
        mapped_name,
        out["report_industry_name"],
        stringy=True,
    )

    # summary/detail?? ?? ?? industry_name? ???? ??
    out["industry_name"] = out["report_industry_name"]
    out["industry4"] = out["industry_code"]

    return out


def extract_tag(path_like, tag: str):
    from pathlib import Path
    import re

    s = str(path_like)
    name = Path(s).name

    # 가장 안전한 방식: 파일명 기준으로 __tag=value__ 패턴 찾기
    m = re.search(rf"__{re.escape(tag)}=([^_][^/]*)__", name)
    if m:
        return m.group(1)

    # 파일명 끝에서 끝나는 경우도 허용
    m = re.search(rf"__{re.escape(tag)}=([^/\\]+?)(?:\.[^.]+)?$", name)
    if m:
        return m.group(1)

    return None


def infer_metric_from_path(path_like):
    return extract_tag(path_like, "metric")

def infer_asof_from_path(path_like):
    return extract_tag(path_like, "asof")

def parse_feat_v_from_features_path(path_like) -> int | None:
    """Parse feat version from features_live path like ...__v=206.parquet."""
    v = extract_tag(path_like, "v")
    if v is None:
        return None
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else None

def load_strategy_config(strategy: str, strategies_yaml: str | Path = "configs/strategies.yaml") -> dict | None:
    p = Path(strategies_yaml)
    if not p.exists() or yaml is None:
        return None
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        return None
    strategies = cfg.get("strategies", {})
    if not isinstance(strategies, dict):
        return None
    obj = strategies.get(strategy)
    return obj if isinstance(obj, dict) else None


def auto_detect_scores_csv(
    *,
    asof: str,
    metric: str | None,
    strategy: str,
    target_date: str,
    features_live: str | Path,
) -> Path | None:
    feat_v = parse_feat_v_from_features_path(features_live)
    if feat_v is None or not metric:
        return None
    candidates = [
        Path(
            f"data/live/scores/latest_scores__asof={asof}__metric={metric}"
            f"__strat={strategy}__featv={feat_v}__target={target_date}__full_universe.csv"
        ),
        Path(
            f"data/processed/latest_scores__asof={asof}__metric={metric}"
            f"__strat={strategy}__featv={feat_v}__target={target_date}__full_universe.csv"
        ),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def merge_exact_scores(detail: pd.DataFrame, scores_csv: str | Path) -> pd.DataFrame:
    sc = pd.read_csv(scores_csv, dtype={"ticker": str})
    sc["ticker"] = sc["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)

    out = detail.copy()
    out["ticker"] = out["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)

    # exact score source에서 가져올 후보 컬럼
    fill_pairs = [
        ("score", "score"),
        ("score_adj", "score_adj"),
        ("score_base", "score_base"),
        ("hold_bonus_applied", "hold_bonus_applied"),
        ("score_rank", "score_rank"),
        ("score_adj_rank", "score_adj_rank"),
        ("filter_status", "filter_status"),
        ("passed_filters", "passed_filters"),
        ("selected_topk", "selected_topk"),
        ("industry4", "industry4"),
        ("industry_code", "industry_code"),
        ("industry_name", "industry_name"),
        ("selection_bucket", "selection_bucket"),
        ("kept_from_previous", "kept_from_previous"),
        ("mcap_group", "mcap_group"),
        ("mcap_rank_pct", "mcap_rank_pct"),
        ("expectation_overlay_active", "expectation_overlay_active"),
        ("expectation_score", "expectation_score"),
        ("expectation_penalty", "expectation_penalty"),
        ("quality_soft_penalty_active", "quality_soft_penalty_active"),
        ("quality_penalty_total", "quality_penalty_total"),
        ("quality_penalty_netincome_ttm_nonpositive", "quality_penalty_netincome_ttm_nonpositive"),
        ("quality_penalty_netincome_acc2_negative", "quality_penalty_netincome_acc2_negative"),
        ("quality_penalty_cfo_warn", "quality_penalty_cfo_warn"),
        ("quality_penalty_cfo_isnull", "quality_penalty_cfo_isnull"),
    ]

    alias_map = {
        "OpIncome_acc2__contrib": "contrib_op",
        "OpIncome_acc2_log1p__contrib": "contrib_op_log",
        "op_growth_streak2__contrib": "contrib_op_streak",
        "Revenue_acc2__contrib": "contrib_rev",
        "Revenue_acc2_log1p__contrib": "contrib_rev_log",
        "rev_growth_streak2__contrib": "contrib_rev_streak",
        "Debt_to_Equity_log__contrib": "contrib_debt",
        "Quality_CFO_to_Assets__contrib": "contrib_cfo",
        "CFO_isnull__contrib": "contrib_missing",
        "OpIncome_acc2__raw": "op_acc2",
        "OpIncome_acc2_log1p__raw": "op_acc2_log1p",
        "op_growth_streak2__raw": "op_growth_streak2",
        "Revenue_acc2__raw": "rev_acc2",
        "Revenue_acc2_log1p__raw": "rev_acc2_log1p",
        "rev_growth_streak2__raw": "rev_growth_streak2",
        "Debt_to_Equity_log__raw": "debt_log",
        "Quality_CFO_to_Assets__raw": "cfo_to_assets",
        "CFO_isnull__raw": "cfo_isnull",
    }
    present_alias_src = [c for c in alias_map.keys() if c in sc.columns]
    contrib_cols = [alias_map[c] for c in present_alias_src if c.endswith("__contrib")]
    raw_cols = [alias_map[c] for c in present_alias_src if c.endswith("__raw")]
    sc = sc.rename(columns={c: alias_map[c] for c in present_alias_src})
    keep_cols = ["ticker"] + [r for _, r in fill_pairs if r in sc.columns] + contrib_cols + raw_cols
    keep_cols = list(dict.fromkeys([c for c in keep_cols if c in sc.columns]))

    sc_small = sc[keep_cols].copy()
    sc_small = _dedupe_columns_keep_first(sc_small)

    # suffix를 강제로 붙여서 충돌 방지
    rename_map = {c: f"{c}__exact" for c in sc_small.columns if c != "ticker"}
    sc_small = sc_small.rename(columns=rename_map)

    out = out.merge(sc_small, on="ticker", how="left")
    out = _dedupe_columns_keep_first(out)

    string_like = {
        "filter_status",
        "industry4",
        "industry_code",
        "industry_name",
        "selection_bucket",
        "kept_from_previous",
    }

    bool_like = {
        "passed_filters",
        "selected_topk",
    }

    for left, right in fill_pairs:
        right_exact = f"{right}__exact"
        if right_exact not in out.columns:
            continue

        rser = _first_series(out, right_exact)

        if left not in out.columns:
            out[left] = pd.NA

        lser = _first_series(out, left)

        if left in string_like:
            out[left] = coalesce_prefer(lser, rser, stringy=True)
        elif left in bool_like:
            out[left] = coalesce_prefer(rser, lser, stringy=False)
        else:
            out[left] = coalesce_prefer(rser, lser, stringy=False)

    # contrib / raw 컬럼도 exact source 우선 반영
    for c in contrib_cols + raw_cols:
        ce = f"{c}__exact"
        if ce not in out.columns:
            continue
        rser = _first_series(out, ce)
        if c not in out.columns:
            out[c] = pd.NA
        lser = _first_series(out, c)
        out[c] = coalesce_prefer(rser, lser, stringy=False)

    # exact 임시 컬럼 제거
    drop_cols = [c for c in out.columns if c.endswith("__exact")]
    out = out.drop(columns=drop_cols, errors="ignore")
    out = _dedupe_columns_keep_first(out)

    return out




# ----------------------------
# feature / supp mapping
# ----------------------------
def dedup_features_latest(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "ticker" not in out.columns:
        raise ValueError("ticker column missing in dataframe")

    if "quarter_key" in out.columns:
        out["_sort_q"] = out["quarter_key"].astype(str)
        out = out.sort_values(["ticker", "_sort_q"])
        out = out.drop_duplicates("ticker", keep="last").copy()
        return out.drop(columns=["_sort_q"])

    if "year" in out.columns and "quarter" in out.columns:
        out["_year"] = pd.to_numeric(out["year"], errors="coerce")
        out["_q"] = out["quarter"].astype(str).str.extract(r"(\d+)")[0]
        out["_q"] = pd.to_numeric(out["_q"], errors="coerce")
        out = out.sort_values(["ticker", "_year", "_q"])
        out = out.drop_duplicates("ticker", keep="last").copy()
        return out.drop(columns=["_year", "_q"])

    if "date" in out.columns:
        out["_date"] = pd.to_datetime(out["date"], errors="coerce")
        out = out.sort_values(["ticker", "_date"])
        out = out.drop_duplicates("ticker", keep="last").copy()
        return out.drop(columns=["_date"])

    return out.drop_duplicates("ticker", keep="last").copy()



def build_target_period_map(feat: pd.DataFrame, target_date: str | None) -> pd.DataFrame:
    cols = set(feat.columns)
    need = {"ticker", "year", "quarter"}
    if not need.issubset(cols):
        return pd.DataFrame(columns=["ticker", "target_year", "target_quarter"])

    x = feat.copy()
    x["ticker"] = normalize_ticker(x["ticker"])
    x["year"] = pd.to_numeric(x["year"], errors="coerce")
    x["_qnum"] = _quarter_num(x["quarter"])

    if target_date and "rebalance_month" in x.columns:
        rb = pd.to_datetime(x["rebalance_month"], errors="coerce")
        x = x.loc[rb == pd.Timestamp(target_date)].copy()

    x = x.dropna(subset=["ticker", "year", "_qnum"])
    if len(x) == 0:
        return pd.DataFrame(columns=["ticker", "target_year", "target_quarter"])

    x = x.sort_values(["ticker", "year", "_qnum"]).drop_duplicates("ticker", keep="last").copy()
    x["target_year"] = x["year"].astype(int)
    x["target_quarter"] = x["_qnum"].astype(int)
    return x[["ticker", "target_year", "target_quarter"]].copy()


def align_to_target_period(raw: pd.DataFrame, target_period_map: pd.DataFrame | None) -> pd.DataFrame:
    if target_period_map is None or len(target_period_map) == 0:
        return raw
    if "ticker" not in raw.columns or "year" not in raw.columns or "quarter" not in raw.columns:
        return raw

    x = raw.copy()
    x["ticker"] = normalize_ticker(x["ticker"])
    x["year"] = pd.to_numeric(x["year"], errors="coerce")
    x["_qnum"] = _quarter_num(x["quarter"])

    pm = target_period_map.copy()
    pm["ticker"] = normalize_ticker(pm["ticker"])
    pm["target_year"] = pd.to_numeric(pm["target_year"], errors="coerce")
    pm["target_quarter"] = pd.to_numeric(pm["target_quarter"], errors="coerce")

    x = x.merge(pm, on="ticker", how="inner")
    x = x.loc[
        x["year"].eq(x["target_year"]) &
        x["_qnum"].eq(x["target_quarter"])
    ].copy()

    if len(x) == 0:
        return raw
    return x.drop(columns=["target_year", "target_quarter", "_qnum"], errors="ignore")


def pick_feature_columns(df: pd.DataFrame) -> dict:
    cols = df.columns.tolist()
    return {
        # keys/basic
        "ticker": pick_first(cols, ["ticker"]),
        "name": pick_first(cols, ["name", "name_final", "corp_name", "corp_nm", "종목명"]),
        "market": pick_first(cols, ["market", "market_type", "시장구분"]),
        "industry_code": pick_first(cols, ["industry_code", "induty_code", "industry", "sector"]),
        "industry_name": pick_first(cols, ["industry_name", "industry_nm", "sector_name", "sector", "업종명", "krx_industry_name"]),

        # market data
        "price": pick_first(cols, ["price", "close", "Close", "종가"]),
        "mcap": pick_first(cols, ["mcap", "market_cap", "marketcap", "시가총액"]),

        # period
        "year": pick_first(cols, ["year"]),
        "quarter": pick_first(cols, ["quarter"]),

        # financials
        "revenue": pick_first(cols, ["Revenue_ttm", "revenue_ttm", "revenue", "sales_ttm", "sales", "매출액"]),
        "op_income": pick_first(cols, ["OpIncome_ttm", "op_income_ttm", "op_income", "operating_income_ttm", "operating_income", "op", "영업이익"]),
        "net_income": pick_first(cols, ["NetIncome_ttm", "net_income_ttm", "net_income", "ni_ttm", "ni", "당기순이익"]),
        "assets": pick_first(cols, ["Assets", "assets", "total_assets", "자산총계"]),
        "equity": pick_first(cols, ["Equity", "equity", "total_equity", "자본총계"]),
        "liabilities": pick_first(cols, ["Liabilities", "liabilities", "total_liabilities", "부채총계"]),
        "cfo": pick_first(cols, ["CFO_ttm", "cfo_ttm", "cfo", "cashflow_from_operations_ttm", "영업활동현금흐름"]),
        "ebitda": pick_first(cols, ["EBITDA_ttm", "ebitda_ttm", "ebitda", "EBITDA"]),

        # factor raw
        "op_acc2": pick_first(cols, ["OpIncome_acc2", "opincome_acc2", "op_income_acc2"]),
        "rev_acc2": pick_first(cols, ["Revenue_acc2", "revenue_acc2"]),
        "debt_log": pick_first(cols, ["Debt_to_Equity_log", "debt_to_equity_log"]),
        "cfo_to_assets": pick_first(cols, ["Quality_CFO_to_Assets", "CFO_to_Assets_ttm", "cfo_to_assets", "quality_cfo_to_assets"]),
        "cfo_isnull": pick_first(cols, ["CFO_isnull", "cfo_isnull"]),

        # factor z
        "op_acc2_z": pick_first(cols, ["OpIncome_acc2__z", "opincome_acc2__z", "op_income_acc2__z"]),
        "rev_acc2_z": pick_first(cols, ["Revenue_acc2__z", "revenue_acc2__z"]),
        "debt_log_z": pick_first(cols, ["Debt_to_Equity_log__z", "debt_to_equity_log__z"]),
        "cfo_to_assets_z": pick_first(cols, ["Quality_CFO_to_Assets__z", "quality_cfo_to_assets__z", "CFO_to_Assets_ttm__z"]),

        # multiples
        "per": pick_first(cols, ["per", "PER"]),
        "pbr": pick_first(cols, ["pbr", "PBR"]),
        "psr": pick_first(cols, ["psr", "PSR"]),
        "ev_ebit": pick_first(cols, ["ev_ebit", "EV_EBIT", "ev_to_ebit"]),
        "ev_ebitda": pick_first(cols, ["ev_ebitda", "EV_EBITDA", "ev_to_ebitda"]),

        # yoy
        "rev_yoy": pick_first(cols, ["Revenue_ttm_yoy", "revenue_yoy", "sales_yoy", "rev_yoy"]),
        "op_yoy": pick_first(cols, ["OpIncome_ttm_yoy", "op_income_yoy", "operating_income_yoy", "op_yoy"]),

        # quarter snapshot
        "revenue_prev_q": pick_first(cols, ["revenue_prev_q"]),
        "revenue_cur_q": pick_first(cols, ["revenue_cur_q"]),
        "revenue_qoq": pick_first(cols, ["revenue_qoq"]),
        "op_prev_q": pick_first(cols, ["op_prev_q"]),
        "op_cur_q": pick_first(cols, ["op_cur_q"]),
        "op_qoq": pick_first(cols, ["op_qoq"]),
    }


def reduce_to_mapped(df: pd.DataFrame) -> pd.DataFrame:
    if "ticker" not in df.columns:
        raise ValueError("ticker missing")

    out = df.copy()
    out["ticker"] = normalize_ticker(out["ticker"])
    out = dedup_features_latest(out)

    fmap = pick_feature_columns(out)
    keep_cols = [c for c in fmap.values() if c is not None]
    keep_cols = list(dict.fromkeys(keep_cols))
    small = out[keep_cols].copy()

    rename = {}
    for k, v in fmap.items():
        if v is not None and v != "ticker":
            rename[v] = k
    return ensure_industry_columns(small.rename(columns=rename))


def merge_code_name_map(detail: pd.DataFrame, supp_raw: pd.DataFrame) -> pd.DataFrame:
    """
    If a supplemental file contains code->name mapping (industry4 + industry_name)
    without ticker-level rows, merge by industry_code.
    """
    cols = supp_raw.columns.tolist()
    code_col = pick_first(cols, ["industry_code", "induty_code", "industry", "sector"])
    name_col = pick_first(cols, ["industry_name", "industry_nm", "sector_name", "업종명", "krx_industry_name", "sector"])
    if code_col is None or name_col is None:
        return detail

    mp = supp_raw[[code_col, name_col]].copy()
    mp.columns = ["industry_code", "industry_name_map"]
    mp["industry_code"] = pd.to_numeric(mp["industry_code"], errors="coerce")
    mp = mp.dropna(subset=["industry_code", "industry_name_map"]).drop_duplicates("industry_code", keep="last")

    out = detail.copy()
    if "industry_code" not in out.columns:
        return out
    out["industry_code"] = pd.to_numeric(out["industry_code"], errors="coerce")
    out = out.merge(mp, on="industry_code", how="left")
    if "report_industry_name" not in out.columns:
        out["report_industry_name"] = pd.NA
    out["report_industry_name"] = coalesce_series(out["report_industry_name"], out["industry_name_map"])
    return out.drop(columns=["industry_name_map"], errors="ignore")


def _quarter_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.extract(r"(\d+)")[0], errors="coerce")


def _quarter_flow_from_cum(df: pd.DataFrame, value_col: str) -> pd.Series:
    vals = pd.to_numeric(df[value_col], errors="coerce")
    qn = _quarter_num(df["quarter"])
    prev = vals.groupby(df["ticker"]).shift(1)
    return vals.where(qn.eq(1), vals - prev)


def build_quarter_snapshot(raw: pd.DataFrame) -> pd.DataFrame | None:
    cols = raw.columns.tolist()
    if "ticker" not in cols:
        return None

    direct_cols = ["revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq"]
    if any(c in cols for c in direct_cols):
        keep = [c for c in ["ticker", "year", "quarter", *direct_cols] if c in cols]
        df = raw[keep].copy()
        df["ticker"] = normalize_ticker(df["ticker"])
        return dedup_features_latest(df)

    year_col = pick_first(cols, ["year"])
    quarter_col = pick_first(cols, ["quarter"])
    rev_col = pick_first(cols, ["Revenue", "revenue", "매출액", "Revenue_ttm", "revenue_ttm"])
    op_col = pick_first(cols, ["OpIncome", "op", "operating_income", "영업이익", "OpIncome_ttm", "op_income_ttm"])
    if year_col is None or quarter_col is None or (rev_col is None and op_col is None):
        return None

    df = raw.copy()
    df["ticker"] = normalize_ticker(df["ticker"])
    df["year"] = pd.to_numeric(df[year_col], errors="coerce")
    df["quarter"] = df[quarter_col]
    df["quarter_num"] = _quarter_num(df["quarter"])
    df = df.dropna(subset=["ticker", "year", "quarter_num"]).sort_values(["ticker", "year", "quarter_num"]).copy()

    def _derive_from_value(value_col: str, prefix: str) -> None:
        vals = pd.to_numeric(df[value_col], errors="coerce")
        is_ttm_like = "ttm" in str(value_col).lower()
        if is_ttm_like:
            prev_base = vals.groupby(df["ticker"]).shift(1)
            qn = _quarter_num(df["quarter"])
            cur_q = vals.where(qn.eq(1), vals - prev_base)
        else:
            cur_q = vals
        prev_q = cur_q.groupby(df["ticker"]).shift(1)
        denom = prev_q.abs().replace(0.0, np.nan)
        qoq = (cur_q - prev_q) / denom
        df[f"{prefix}_cur_q"] = cur_q
        df[f"{prefix}_prev_q"] = prev_q
        df[f"{prefix}_qoq"] = qoq

    if rev_col is not None:
        _derive_from_value(rev_col, "revenue")
    if op_col is not None:
        _derive_from_value(op_col, "op")

    keep = ["ticker", "year", "quarter"]
    for c in ["revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq"]:
        if c in df.columns:
            keep.append(c)
    return df[keep].drop_duplicates("ticker", keep="last").copy()


def build_detail(actions: pd.DataFrame, feat: pd.DataFrame, target_period_map: pd.DataFrame | None = None) -> pd.DataFrame:
    actions = actions.copy()
    actions["ticker"] = normalize_ticker(actions["ticker"])

    feat_base = align_to_target_period(feat, target_period_map)
    feat_small = reduce_to_mapped(feat_base)
    df = actions.merge(feat_small, on="ticker", how="left")
    qsnap = build_quarter_snapshot(feat_base)
    if qsnap is not None:
        df = df.merge(qsnap, on="ticker", how="left", suffixes=("", "__featq"))
        for c in ["year", "quarter", "revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq"]:
            rc = f"{c}__featq"
            if rc in df.columns:
                if c in df.columns:
                    df[c] = coalesce_series(df[c], df[rc])
                else:
                    df[c] = df[rc]
        df = df.drop(columns=[c for c in df.columns if c.endswith("__featq")], errors="ignore")

    if "name_final" not in df.columns:
        df["name_final"] = pd.NA
    if "name" in df.columns:
        df["name_final"] = coalesce_series(df["name_final"], df["name"])

    if "industry_code" not in df.columns and "industry_code" in feat_small.columns:
        df["industry_code"] = feat_small["industry_code"]

    df["report_name"] = df["name_final"]
    df["report_market"] = df["market"] if "market" in df.columns else pd.NA
    df["report_industry_name"] = df["industry_name"] if "industry_name" in df.columns else pd.NA
    df["report_price"] = df["price"] if "price" in df.columns else pd.NA
    df["report_mcap"] = df["mcap"] if "mcap" in df.columns else pd.NA
    df["industry4"] = df["industry_code"] if "industry_code" in df.columns else pd.NA
    for c in ["year", "quarter", "revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq"]:
        if c not in df.columns:
            df[c] = pd.NA

    return df


def merge_execution_plan(detail: pd.DataFrame, execution_plan_path: str | None) -> pd.DataFrame:
    if not execution_plan_path:
        return detail
    p = Path(execution_plan_path)
    if not p.exists():
        print(f"[WARN] execution_plan not found: {p}")
        return detail

    ex = pd.read_csv(p)
    if "ticker" not in ex.columns:
        print("[WARN] execution_plan has no ticker col; skip")
        return detail

    ex["ticker"] = normalize_ticker(ex["ticker"])
    keep = [
        "ticker", "price", "current_value", "target_value", "trade_value",
        "planned_total_qty", "planned_total_value",
        "day1_qty", "day1_order_value",
        "day2_qty", "day2_order_value",
        "day3_qty", "day3_order_value",
        "est_slippage_bps",
    ]
    keep = [c for c in keep if c in ex.columns]
    ex = ex[keep].drop_duplicates("ticker", keep="last").copy()
    rename = {c: f"exec__{c}" for c in keep if c != "ticker"}
    ex = ex.rename(columns=rename)

    out = detail.merge(ex, on="ticker", how="left")
    if "exec__price" in out.columns:
        out["report_price"] = coalesce_series(out["exec__price"], out["report_price"])
    return out


def merge_supplemental(detail: pd.DataFrame, supp_paths: list[str], asof: str | None = None, target_period_map: pd.DataFrame | None = None) -> pd.DataFrame:
    out = detail.copy()
    paths = list(supp_paths or [])
    if asof:
        md = auto_detect_marketdata_path(asof)
        if md is not None and str(md) not in [str(Path(p)) for p in paths]:
            paths.append(str(md))
    for sp in paths:
        p = Path(sp)
        if not p.exists():
            print(f"[WARN] supp_file not found, skip: {p}")
            continue
        try:
            raw = load_any_table(p)
        except Exception as e:
            print(f"[WARN] failed to load supp_file={p}: {e}")
            continue

        raw_base = align_to_target_period(raw, target_period_map)

        # first try code->industry name map
        out = merge_code_name_map(out, raw)

        qsnap = build_quarter_snapshot(raw_base)
        if qsnap is not None:
            out = out.merge(qsnap, on="ticker", how="left", suffixes=("", "__qraw"))
            for c in ["year", "quarter", "revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq"]:
                rc = f"{c}__qraw"
                if rc in out.columns:
                    if c in out.columns:
                        out[c] = out[c].combine_first(out[rc])
                    else:
                        out[c] = out[rc]
            out = out.drop(columns=[c for c in out.columns if c.endswith("__qraw")], errors="ignore")

        if "ticker" not in raw.columns:
            continue

        supp = reduce_to_mapped(raw_base)
        supp = ensure_industry_columns(supp)
        supp = supp.drop_duplicates("ticker", keep="last").copy()
        merge_cols = [
            "ticker", "name", "market", "industry_code", "industry4", "industry_name",
        "cohort_status", "score_availability_reason", "latest_available_rebalance_month", "latest_available_year", "latest_available_quarter",
            "price", "mcap",
            "revenue", "op_income", "net_income", "assets", "equity", "liabilities", "cfo", "ebitda",
            "per", "pbr", "psr", "ev_ebit", "ev_ebitda"
        ]
        merge_cols = [c for c in merge_cols if c in supp.columns]
        supp = supp[merge_cols].copy()
        rename = {c: f"supp__{c}" for c in merge_cols if c != "ticker"}
        supp = supp.rename(columns=rename)

        out = out.merge(supp, on="ticker", how="left")

        fill_pairs = [
            ("report_name", "supp__name"),
            ("report_market", "supp__market"),
            ("report_industry_name", "supp__industry_name"),
            ("report_price", "supp__price"),
            ("report_mcap", "supp__mcap"),
            ("revenue", "supp__revenue"),
            ("op_income", "supp__op_income"),
            ("net_income", "supp__net_income"),
            ("assets", "supp__assets"),
            ("equity", "supp__equity"),
            ("liabilities", "supp__liabilities"),
            ("cfo", "supp__cfo"),
            ("ebitda", "supp__ebitda"),
            ("per", "supp__per"),
            ("pbr", "supp__pbr"),
            ("psr", "supp__psr"),
            ("ev_ebit", "supp__ev_ebit"),
            ("ev_ebitda", "supp__ev_ebitda"),
        ]
        for left, right in fill_pairs:
            if right in out.columns:
                if left in out.columns:
                    out[left] = coalesce_series(out[left], out[right])
                else:
                    out[left] = out[right]

        if "supp__industry4" in out.columns:
            supp_industry4 = pd.to_numeric(out["supp__industry4"], errors="coerce")
            if "industry_code" in out.columns:
                base_industry4 = pd.to_numeric(out["industry_code"], errors="coerce")
                if not isinstance(base_industry4, pd.Series):
                    base_industry4 = pd.Series([base_industry4] * len(out), index=out.index)
                out["industry_code"] = coalesce_series(base_industry4, supp_industry4)
            else:
                out["industry_code"] = supp_industry4
    return out


def add_derived_valuations(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        raise RuntimeError("add_derived_valuations received None")
    out = df.copy()
    for c in ["report_mcap", "revenue", "net_income", "equity", "liabilities", "op_income", "ebitda"]:
        if c not in out.columns:
            out[c] = pd.NA
        out[c] = pd.to_numeric(out[c], errors="coerce")

    def safe_div(n, d):
        return n / d if (pd.notna(n) and pd.notna(d) and d not in [0, 0.0]) else pd.NA

    if "per" not in out.columns:
        out["per"] = pd.NA
    if "pbr" not in out.columns:
        out["pbr"] = pd.NA
    if "psr" not in out.columns:
        out["psr"] = pd.NA
    if "ev_ebit" not in out.columns:
        out["ev_ebit"] = pd.NA
    if "ev_ebitda" not in out.columns:
        out["ev_ebitda"] = pd.NA

    for idx, row in out.iterrows():
        mcap = row["report_mcap"]
        rev = row["revenue"]
        ni = row["net_income"]
        eq = row["equity"]
        liab = row["liabilities"]
        op = row["op_income"]
        ebitda = row["ebitda"]

        if pd.isna(row["per"]) and pd.notna(mcap) and pd.notna(ni) and ni > 0:
            out.at[idx, "per"] = safe_div(mcap, ni)
        if pd.isna(row["pbr"]) and pd.notna(mcap) and pd.notna(eq) and eq > 0:
            out.at[idx, "pbr"] = safe_div(mcap, eq)
        if pd.isna(row["psr"]) and pd.notna(mcap) and pd.notna(rev) and rev > 0:
            out.at[idx, "psr"] = safe_div(mcap, rev)

        # approximate EV using market cap + liabilities (cash unavailable)
        if pd.notna(mcap) and pd.notna(liab):
            ev_proxy = mcap + liab
            if pd.isna(row["ev_ebit"]) and pd.notna(op) and op > 0:
                out.at[idx, "ev_ebit"] = safe_div(ev_proxy, op)
            if pd.isna(row["ev_ebitda"]) and pd.notna(ebitda) and ebitda > 0:
                out.at[idx, "ev_ebitda"] = safe_div(ev_proxy, ebitda)

    return out


# ----------------------------
# score breakdown
# ----------------------------
def _human_factor_name(col: str) -> str:
    return {
        "OpIncome_acc2": "OpIncome_acc2",
        "OpIncome_acc2_log1p": "OpIncome_acc2_log1p",
        "Revenue_acc2": "Revenue_acc2",
        "Revenue_acc2_log1p": "Revenue_acc2_log1p",
        "op_growth_streak2": "op_growth_streak2",
        "rev_growth_streak2": "rev_growth_streak2",
        "Debt_to_Equity_log": "Debt_to_Equity_log",
        "Quality_CFO_to_Assets": "Quality_CFO_to_Assets",
        "CFO_isnull": "CFO_isnull",
    }.get(col, col)



def _extract_scoring_subcfg(strategy_cfg: dict | None, key: str) -> dict | None:
    if not isinstance(strategy_cfg, dict):
        return None
    scoring = strategy_cfg.get("scoring")
    if isinstance(scoring, dict):
        v = scoring.get(key)
        if isinstance(v, dict):
            return v
    return None


def _extract_ai_weighting_cfg(strategy_cfg: dict | None) -> dict | None:
    if not isinstance(strategy_cfg, dict):
        return None

    candidate_keys = [
        "ai_weighting",
        "ml_weighting",
        "dynamic_weighting",
        "weight_adjustment_model",
        "ai_ml_weighting",
    ]
    for key in candidate_keys:
        v = strategy_cfg.get(key)
        if isinstance(v, dict):
            return v

    for key in candidate_keys:
        v = _extract_scoring_subcfg(strategy_cfg, key)
        if isinstance(v, dict):
            return v

    return None


def _build_quality_penalty_note(strategy_cfg: dict | None) -> dict:
    cfg = _extract_scoring_subcfg(strategy_cfg, "quality_soft_penalty")
    if not cfg:
        return {"present": False, "enabled": False, "lines": []}

    enabled = bool(cfg.get("enabled", False))
    lines = [
        f"품질 소프트 패널티 모듈은 {'활성화' if enabled else '비활성화'} 상태입니다.",
        "기본 factor score 산출 이후 순이익·영업현금흐름 관련 품질 경고 신호에 대해 추가 감점을 적용할 수 있습니다.",
    ]
    mapping = [
        ("netincome_ttm_nonpositive_penalty", "TTM 순이익 0 이하"),
        ("netincome_acc2_negative_penalty", "순이익 가속도 음수"),
        ("cfo_warn_penalty", "CFO 경고"),
        ("cfo_isnull_penalty", "CFO 결측"),
    ]
    detail_lines = []
    for key, label in mapping:
        if key in cfg:
            detail_lines.append(f"- {label}: **{cfg.get(key)}**")
    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)
    return {"present": True, "enabled": enabled, "lines": lines}


def _build_expectation_overlay_note(strategy_cfg: dict | None) -> dict:
    cfg = _extract_scoring_subcfg(strategy_cfg, "expectation_overlay")
    if not cfg:
        top = strategy_cfg.get("expectation_overlay") if isinstance(strategy_cfg, dict) else None
        cfg = top if isinstance(top, dict) else None
    if not cfg:
        return {"present": False, "enabled": False, "lines": []}

    enabled = bool(cfg.get("enabled", False))
    lines = [
        f"Expectation overlay 모듈은 {'활성화' if enabled else '비활성화'} 상태입니다.",
        "이 모듈은 기본 factor score 이후 추가 기대점수(expectation_score) 또는 기대 패널티(expectation_penalty)를 반영하는 후처리 계층입니다.",
    ]
    source = cfg.get("source") or cfg.get("model_name") or cfg.get("description")
    if source:
        lines.append(f"- 기대반영 입력/출처: **{source}**")
    if not enabled:
        lines.append("- 현재 설정 기준으로는 최종 선별 점수에 expectation overlay가 반영되지 않습니다.")
    return {"present": True, "enabled": enabled, "lines": lines}


def _build_ai_weighting_note(strategy_cfg: dict | None) -> dict:
    """
    Returns:
        {
            "present": bool,
            "section_enabled": bool,
            "enabled": bool,
            "applied_to_score": bool,
            "lines": list[str],
            "model_name": str | None,
            "apply_mode": str | None,
        }
    """
    ai_cfg = _extract_ai_weighting_cfg(strategy_cfg)
    if not ai_cfg:
        return {
            "present": False,
            "section_enabled": False,
            "enabled": False,
            "applied_to_score": False,
            "lines": [],
            "model_name": None,
            "apply_mode": None,
        }

    enabled = bool(ai_cfg.get("enabled", False))
    report_enabled = bool(ai_cfg.get("report_enabled", False))
    smoke_test = bool(ai_cfg.get("smoke_test", False))
    reference_only = bool(ai_cfg.get("reference_only", False))
    test_mode = bool(ai_cfg.get("test_mode", False))
    section_enabled = enabled or report_enabled or smoke_test or reference_only or test_mode

    model_name = ai_cfg.get("model_name") or ai_cfg.get("model") or ai_cfg.get("name")
    apply_mode = ai_cfg.get("apply_mode") or ai_cfg.get("mode") or ai_cfg.get("application")
    applied_to_score = bool(ai_cfg.get("active_for_selection", ai_cfg.get("apply_to_score", enabled)))

    feature_list = ai_cfg.get("input_factors") or ai_cfg.get("features") or ai_cfg.get("inputs")
    feature_text = ", ".join(str(x) for x in feature_list if str(x).strip()) if isinstance(feature_list, list) else None
    output_desc = ai_cfg.get("output") or ai_cfg.get("target_output") or ai_cfg.get("description")

    if not section_enabled:
        return {
            "present": True,
            "section_enabled": False,
            "enabled": enabled,
            "applied_to_score": applied_to_score,
            "lines": [],
            "model_name": model_name,
            "apply_mode": apply_mode,
        }

    lines = [
        f"AI/ML 가중치 조정 모듈은 {'설정됨' if section_enabled else '미설정'} 상태이며, 현재 {'활성' if enabled else '비활성'}로 정의되어 있습니다.",
        "기본 rule-based factor score와 별도로, factor별 조정계수(α_i)를 부여하는 실험적/보조적 계층으로 해석합니다.",
        "",
        "```text",
        "Score_base = Σ (w_i × standardized_factor_i)",
        "Score_ai   = Σ (w_i × α_i × standardized_factor_i)",
        "```",
        "",
        "여기서 w_i는 기본 전략 weight이고, α_i는 AI/ML 모듈이 산출한 조정계수입니다.",
    ]

    if model_name:
        lines.append(f"- 모듈명: **{model_name}**")
    if apply_mode:
        lines.append(f"- 적용 방식: **{apply_mode}**")
    if feature_text:
        lines.append(f"- 입력 변수: **{feature_text}**")
    if output_desc:
        lines.append(f"- 모델 출력 설명: **{output_desc}**")

    if applied_to_score:
        lines.append("- 현재 설정상 이 모듈은 최종 score 조정에 반영되는 것으로 정의되어 있습니다.")
    else:
        lines.append("- 현재 설정상 이 모듈은 smoke/reference 목적 설명용이며, 최종 score 조정에는 직접 반영되지 않습니다.")

    return {
        "present": True,
        "section_enabled": True,
        "enabled": enabled,
        "applied_to_score": applied_to_score,
        "lines": lines,
        "model_name": model_name,
        "apply_mode": apply_mode,
    }


def build_module_notes_md_block(spec: dict) -> list[str]:
    module_notes = spec.get("module_notes", {}) or {}
    sections = []
    title_map = {
        "quality_penalty": "품질 소프트 패널티",
        "expectation_overlay": "Expectation Overlay",
        "ai_weighting": "AI/ML 가중치 조정",
    }
    for key in ["quality_penalty", "expectation_overlay", "ai_weighting"]:
        info = module_notes.get(key) or {}
        lines = info.get("lines") or []
        if lines:
            sections.append((title_map[key], lines))

    if not sections:
        return []

    out = ["### 3.3 추가 보정/오버레이 모듈", ""]
    for idx, (title, lines) in enumerate(sections, start=1):
        out.append(f"#### 3.3.{idx} {title}")
        out.append("")
        out.extend(lines)
        out.append("")
    return out


def build_module_notes_html(spec: dict) -> str:
    module_notes = spec.get("module_notes", {}) or {}
    sections = []
    title_map = {
        "quality_penalty": "품질 소프트 패널티",
        "expectation_overlay": "Expectation Overlay",
        "ai_weighting": "AI/ML 가중치 조정",
    }
    for key in ["quality_penalty", "expectation_overlay", "ai_weighting"]:
        info = module_notes.get(key) or {}
        lines = info.get("lines") or []
        if lines:
            sections.append((title_map[key], lines))

    if not sections:
        return ""

    rendered = ['<section><h3>3.3 추가 보정/오버레이 모듈</h3>']
    for idx, (title, lines) in enumerate(sections, start=1):
        rendered.append(f"<h4>3.3.{idx} {html.escape(title)}</h4>")
        html_lines = []
        in_code = False
        for line in lines:
            if line.strip() == "```text":
                in_code = True
                html_lines.append("<pre><code>")
                continue
            if line.strip() == "```":
                in_code = False
                html_lines.append("</code></pre>")
                continue
            if in_code:
                html_lines.append(html.escape(line))
                html_lines.append("<br>")
                continue
            if line.startswith("- "):
                html_lines.append(f"<li>{line[2:]}</li>")
            elif line.strip() == "":
                html_lines.append("<p></p>")
            else:
                html_lines.append(f"<p>{html.escape(line)}</p>")
        body = []
        ul_buf = []
        for chunk in html_lines:
            if chunk.startswith("<li>"):
                ul_buf.append(chunk)
            else:
                if ul_buf:
                    body.append("<ul>" + "".join(ul_buf) + "</ul>")
                    ul_buf = []
                body.append(chunk)
        if ul_buf:
            body.append("<ul>" + "".join(ul_buf) + "</ul>")
        rendered.append("".join(body))
    rendered.append("</section>")
    return "".join(rendered)


def get_score_spec(strategy: str, strategy_cfg: dict | None = None) -> dict:
    known_factor_notes = {
        "OpIncome_acc2": "최근 구간에서 영업이익 증가 속도가 얼마나 가속되었는지를 나타내는 팩터. 값이 높을수록 영업이익 개선 속도가 빨라진 것으로 해석한다.",
        "OpIncome_acc2_log1p": "OpIncome_acc2를 signed log1p로 완화한 팩터. 극단값 영향은 줄이되 이익 가속의 방향성과 크기는 유지한다.",
        "Revenue_acc2": "최근 구간에서 매출 증가 속도가 얼마나 가속되었는지를 나타내는 팩터. 매출 모멘텀의 가속 여부를 본다.",
        "Debt_to_Equity_log": "부채/자본 비율을 로그 변환한 값. 높을수록 재무 레버리지가 큰 상태로 보고 감점 요인으로 사용한다.",
        "op_growth_streak2": "최근 두 구간 연속으로 단분기 영업이익이 개선되고 현재 단분기 영업이익이 양수인 경우 1, 아니면 0인 보너스 팩터.",
        "rev_growth_streak2": "최근 두 구간 연속으로 단분기 매출이 증가한 경우 1, 아니면 0인 보너스 팩터.",
        "Quality_CFO_to_Assets": "영업현금흐름을 자산으로 나눈 품질 팩터. 높을수록 이익의 현금화 품질이 좋다고 해석한다.",
        "CFO_isnull": "영업현금흐름 데이터 결측 여부를 나타내는 패널티 더미(0/1). 결측이면 감점한다.",
    }

    quality_note = _build_quality_penalty_note(strategy_cfg)
    expectation_note = _build_expectation_overlay_note(strategy_cfg)
    ai_note = _build_ai_weighting_note(strategy_cfg)

    default = {
        "title": strategy,
        "formula_lines": ["전략별 score 정의를 별도 확인 필요"],
        "scoring_note": "-",
        "calc_steps": ["전략별 계산 절차를 별도 확인 필요"],
        "factor_notes": {},
        "filters": ["-"],
        "weights": None,
        "module_notes": {
            "quality_penalty": quality_note,
            "expectation_overlay": expectation_note,
            "ai_weighting": ai_note,
        },
    }

    if not strategy_cfg:
        known = {
            "D_quality_filter_debt_profitaccel_liq": {
                "title": "이익가속 + 부채통제 + 유동성 필터 전략",
                "formula_lines": [
                    "Score_raw = 1.00 × z(OpIncome_acc2_log1p)",
                    "          + 0.25 × z(Revenue_acc2)",
                    "          - 0.35 × z(Debt_to_Equity_log)",
                    "          + 0.15 × op_growth_streak2",
                    "          + 0.05 × rev_growth_streak2",
                ],
                "scoring_note": "Cross-sectional z-score 기준 조합",
                "calc_steps": [
                    "1) 리밸런싱 시점 유니버스를 구성한다.",
                    "2) 각 종목의 OpIncome_acc2, Revenue_acc2, Debt_to_Equity_log를 계산한다.",
                    "3) 각 팩터를 동일 시점 유니버스 내 cross-sectional z-score로 표준화한다.",
                    "4) extreme clipping 설정이 있으면 표준화 점수에 상하한을 적용한다.",
                    "5) 가중합으로 Score_raw를 계산한다.",
                    "6) holding bonus 설정이 있고 현재 보유 종목이면 Score_adj에 보너스를 더한다.",
                    "7) 전략 필터를 적용한 뒤 최종 점수 순으로 상위 종목을 편입한다.",
                ],
                "factor_notes": known_factor_notes,
                "filters": [
                    "min_traded_value >= 10억원",
                    "min_mcap >= 1000억원",
                    "industry4 그룹당 최대 2종목",
                    "부채 과다 종목 제한",
                ],
                "weights": {
                    "OpIncome_acc2_log1p": 1.00,
                    "Revenue_acc2": 0.25,
                    "Debt_to_Equity_log": -0.35,
                    "op_growth_streak2": 0.15,
                    "rev_growth_streak2": 0.05,
                },
                "scoring": {
                    "clip_z": 4.0,
                    "hold_bonus": 0.50,
                    "use_robust_z": True,
                    "clip_tiers": [
                        {"upto": 1.0, "slope": 1.0},
                        {"upto": 2.0, "slope": 0.8},
                        {"upto": 3.0, "slope": 0.6},
                        {"upto": 4.0, "slope": 0.4},
                    ],
                },
                "module_notes": {
                    "quality_penalty": quality_note,
                    "expectation_overlay": expectation_note,
                    "ai_weighting": ai_note,
                },
            },
        }
        return known.get(strategy, default)

    weights = strategy_cfg.get("weights", {}) if isinstance(strategy_cfg.get("weights", {}), dict) else {}
    filters_cfg = strategy_cfg.get("filters", {}) if isinstance(strategy_cfg.get("filters", {}), dict) else {}
    scoring_cfg = strategy_cfg.get("scoring", {}) if isinstance(strategy_cfg.get("scoring", {}), dict) else {}
    scoring_method = scoring_cfg.get("method", "zscore")
    raw_factors = set(scoring_cfg.get("raw_factors", []) if isinstance(scoring_cfg.get("raw_factors", []), list) else [])

    if not weights:
        out = default.copy()
        return out

    formula_lines = []
    for i, (col, w) in enumerate(weights.items()):
        w = float(w)
        sign = "+" if w >= 0 else "-"
        lhs = "Score_raw =" if i == 0 else "          "
        rhs = (
            f" {abs(w):.2f} × {_human_factor_name(col)}"
            if col == "CFO_isnull" or col in raw_factors
            else f" {abs(w):.2f} × z({_human_factor_name(col)})"
        )
        formula_lines.append(f"{lhs} {rhs}" if i == 0 and w >= 0 else f"{lhs} {sign}{rhs}")

    calc_steps = [
        "1) 리밸런싱 시점 유니버스를 구성한다.",
        "2) 전략에서 사용하는 팩터를 계산한다.",
        "3) 연속형 팩터는 동일 시점 유니버스 내 cross-sectional z-score로 표준화한다.",
    ]
    if "clip_z" in scoring_cfg:
        calc_steps.append("4) extreme clipping 설정이 있으면 표준화 점수에 상하한을 적용한다.")
        next_step = 5
    else:
        next_step = 4

    calc_steps.append(f"{next_step}) 가중합으로 Score_raw를 계산한다.")
    next_step += 1

    if quality_note.get("enabled"):
        calc_steps.append(f"{next_step}) 품질 소프트 패널티가 활성화된 경우 순이익·CFO 관련 품질 패널티를 추가 반영한다.")
        next_step += 1

    if expectation_note.get("enabled"):
        calc_steps.append(f"{next_step}) expectation overlay가 활성화된 경우 기대점수 또는 기대 패널티를 후처리로 반영한다.")
        next_step += 1

    if ai_note.get("applied_to_score"):
        calc_steps.append(f"{next_step}) AI/ML 가중치 조정 모듈이 활성화된 경우 factor별 조정계수(α_i)를 반영해 보정 점수를 계산한다.")
        next_step += 1

    if "hold_bonus" in scoring_cfg:
        calc_steps.append(f"{next_step}) holding bonus 설정이 있고 현재 보유 종목이면 Score_adj에 보너스를 더한다.")
        next_step += 1

    calc_steps.append(f"{next_step}) 전략 필터를 적용한 뒤 최종 점수 순으로 상위 종목을 편입한다.")

    filter_lines = []
    for k, v in filters_cfg.items():
        if k.startswith("min_"):
            filter_lines.append(f"{k[4:]} >= {v}")
        elif k.startswith("max_"):
            filter_lines.append(f"{k[4:]} <= {v}")
        else:
            filter_lines.append(f"{k} = {v}")
    if strategy == "D_quality_filter_debt_profitaccel_liq" and "op_qoq > 0" not in filter_lines and "min_op_cur_q <= 0" not in filter_lines:
        filter_lines.append("op_qoq > 0")
    if not filter_lines:
        filter_lines = ["-"]

    extra = []
    if "clip_z" in scoring_cfg:
        extra.append(f"clip_z={scoring_cfg.get('clip_z')}")
    if "hold_bonus" in scoring_cfg:
        extra.append(f"holding_bonus={scoring_cfg.get('hold_bonus')}")
    if scoring_cfg.get("use_robust_z"):
        extra.append("robust_z=true")
    if quality_note.get("enabled"):
        extra.append("quality_soft_penalty=on")
    elif quality_note.get("present"):
        extra.append("quality_soft_penalty=off")
    if expectation_note.get("enabled"):
        extra.append("expectation_overlay=on")
    elif expectation_note.get("present"):
        extra.append("expectation_overlay=off")
    if ai_note.get("applied_to_score"):
        extra.append("ai_weighting=on")
    elif ai_note.get("present"):
        extra.append("ai_weighting=defined_not_applied")
    clip_tiers_note = _clip_tiers_note(scoring_cfg)
    if clip_tiers_note:
        extra.append(f"tiered_clip=({clip_tiers_note})")

    scoring_note = f"Cross-sectional {scoring_method} 기준 조합"
    if extra:
        scoring_note += " / " + " / ".join(extra)

    return {
        "title": strategy_cfg.get("desc", strategy),
        "formula_lines": formula_lines,
        "scoring_note": scoring_note,
        "calc_steps": calc_steps,
        "factor_notes": {k: known_factor_notes.get(k, k) for k in weights.keys()},
        "filters": filter_lines,
        "weights": {str(k): float(v) for k, v in weights.items()},
        "scoring": scoring_cfg,
        "module_notes": {
            "quality_penalty": quality_note,
            "expectation_overlay": expectation_note,
            "ai_weighting": ai_note,
        },
    }


def add_score_breakdown(df: pd.DataFrame, strategy: str, strategy_cfg: dict | None = None) -> pd.DataFrame:
    out = df.copy()

    exact_cols = [c for c in ["contrib_op", "contrib_op_log", "contrib_op_streak", "contrib_rev", "contrib_rev_log", "contrib_rev_streak", "contrib_debt", "contrib_cfo", "contrib_missing"] if c in out.columns]
    exact_available = len(exact_cols) > 0
    if exact_available:
        for c in exact_cols:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out["score_rebuilt"] = out[exact_cols].sum(axis=1, min_count=1)
        if "score" in out.columns:
            out["score_diff_vs_reported"] = pd.to_numeric(out["score"], errors="coerce") - pd.to_numeric(out["score_rebuilt"], errors="coerce")
        else:
            out["score_diff_vs_reported"] = pd.NA
        out["score_rebuild_method"] = "exact_from_scores_csv"
        return out

    spec = get_score_spec(strategy, strategy_cfg=strategy_cfg)
    weights = spec.get("weights") or {}
    if not weights:
        out["score_rebuild_method"] = "unavailable"
        return out

    feature_alias = {
        "OpIncome_acc2": ("op_acc2_z", "contrib_op"),
        "OpIncome_acc2_log1p": ("op_acc2_log1p_z", "contrib_op_log"),
        "op_growth_streak2": ("op_growth_streak2", "contrib_op_streak"),
        "Revenue_acc2": ("rev_acc2_z", "contrib_rev"),
        "Revenue_acc2_log1p": ("rev_acc2_log1p_z", "contrib_rev_log"),
        "rev_growth_streak2": ("rev_growth_streak2", "contrib_rev_streak"),
        "Debt_to_Equity_log": ("debt_log_z", "contrib_debt"),
        "Quality_CFO_to_Assets": ("cfo_to_assets_z", "contrib_cfo"),
        "CFO_isnull": ("cfo_isnull", "contrib_missing"),
    }

    rebuilt_cols = []
    for factor, weight in weights.items():
        if factor not in feature_alias:
            continue
        src_col, dst_col = feature_alias[factor]
        if src_col not in out.columns:
            out[src_col] = pd.NA
        out[dst_col] = pd.to_numeric(out[src_col], errors="coerce") * float(weight)
        rebuilt_cols.append(dst_col)

    out["score_rebuilt"] = out[rebuilt_cols].sum(axis=1, min_count=1) if rebuilt_cols else pd.NA
    if "score" in out.columns:
        out["score_diff_vs_reported"] = pd.to_numeric(out["score"], errors="coerce") - pd.to_numeric(out["score_rebuilt"], errors="coerce")
    else:
        out["score_diff_vs_reported"] = pd.NA
    out["score_rebuild_method"] = out.get("score_rebuild_method", "approx_from_features")
    return out


def add_quality_penalty_info(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    numeric_cols = [
        "quality_penalty_total",
        "quality_penalty_netincome_ttm_nonpositive",
        "quality_penalty_netincome_acc2_negative",
        "quality_penalty_cfo_warn",
        "quality_penalty_cfo_isnull",
        "hold_bonus_applied",
        "expectation_penalty",
    ]
    for c in numeric_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    if "quality_penalty_total" not in out.columns:
        out["quality_penalty_total"] = pd.NA

    if "quality_soft_penalty_active" not in out.columns:
        out["quality_soft_penalty_active"] = pd.NA

    if "penalty_flag" not in out.columns:
        out["penalty_flag"] = 0

    qpt = pd.to_numeric(out["quality_penalty_total"], errors="coerce")
    out["penalty_flag"] = ((qpt.notna()) & (qpt < 0)).astype(int)

    def classify_penalty(x):
        if pd.isna(x):
            return "-"
        if x <= -0.40:
            return "HIGH"
        if x <= -0.20:
            return "MID"
        if x < 0:
            return "LOW"
        return "NONE"

    out["penalty_level"] = qpt.apply(classify_penalty)

    def penalty_reason(row) -> str:
        reasons = []
        mapping = [
            ("quality_penalty_netincome_ttm_nonpositive", "NetIncome_ttm<=0"),
            ("quality_penalty_netincome_acc2_negative", "NetIncome_acc2<0"),
            ("quality_penalty_cfo_warn", "CFO_warn"),
            ("quality_penalty_cfo_isnull", "CFO_isnull"),
        ]
        for col, label in mapping:
            if col in row.index:
                v = safe_num(row.get(col))
                if v is not None and v < 0:
                    reasons.append(label)
        if not reasons:
            return "-"
        return ", ".join(reasons)

    def penalty_desc(reason: str) -> str:
        if not isinstance(reason, str):
            return "-"
        reason = reason.strip()
        if reason in {"", "-"}:
            return "-"
        mapping = {
            "NetIncome_ttm<=0": "TTM 순이익이 0 이하라서 품질 패널티 적용",
            "NetIncome_acc2<0": "순이익 가속도가 음수로 전환되어 품질 패널티 적용",
            "CFO_warn": "영업현금흐름 관련 경고 신호로 추가 감점",
            "CFO_isnull": "영업현금흐름 데이터 부재로 신뢰도 감점",
        }
        parts = [p.strip() for p in reason.split(",") if p.strip()]
        if not parts:
            return "-"
        return " / ".join(mapping.get(p, p) for p in parts)

    out["penalty_reason"] = out.apply(penalty_reason, axis=1)
    out["penalty_desc"] = out["penalty_reason"].apply(penalty_desc)

    return out


# ----------------------------
# performance section
# ----------------------------

def load_optional_performance(
    perf_summary_path: str | None,
    perf_daily_path: str | None,
    perf_contrib_path: str | None,
) -> dict | None:
    if not any([perf_summary_path, perf_daily_path, perf_contrib_path]):
        return None

    out: dict[str, object] = {"summary": None, "daily": pd.DataFrame(), "contrib": pd.DataFrame()}
    try:
        if perf_summary_path and Path(perf_summary_path).exists():
            import json
            out["summary"] = json.loads(Path(perf_summary_path).read_text(encoding="utf-8"))
        if perf_daily_path and Path(perf_daily_path).exists():
            out["daily"] = pd.read_csv(perf_daily_path)
        if perf_contrib_path and Path(perf_contrib_path).exists():
            out["contrib"] = pd.read_csv(perf_contrib_path)
    except Exception as e:
        print(f"[WARN] failed to load optional performance section: {e}")
        return None

    if out["summary"] is None and len(out["daily"]) == 0 and len(out["contrib"]) == 0:
        return None
    return out


def auto_build_performance_summary(
    perf_summary_path: str | None,
    perf_daily_path: str | None,
    perf_contrib_path: str | None,
) -> bool:
    if not perf_summary_path:
        return False

    sp = Path(perf_summary_path)
    if sp.exists():
        return False
    if not perf_daily_path or not Path(perf_daily_path).exists():
        return False

    try:
        nav_df = pd.read_csv(perf_daily_path)
        if len(nav_df) == 0:
            return False

        nav_df = nav_df.copy()
        if "date" in nav_df.columns:
            nav_df["date"] = pd.to_datetime(nav_df["date"], errors="coerce")

        if "nav" not in nav_df.columns:
            if "value" in nav_df.columns:
                nav_df["nav"] = pd.to_numeric(nav_df["value"], errors="coerce")
            else:
                raise ValueError("NAV source must contain 'nav' or 'value'")

        if "daily_return" not in nav_df.columns:
            if "return" in nav_df.columns:
                nav_df["daily_return"] = pd.to_numeric(nav_df["return"], errors="coerce")
            else:
                nav_df["daily_return"] = pd.to_numeric(nav_df["nav"], errors="coerce").pct_change()

        if "cum_return" not in nav_df.columns:
            nav_df["cum_return"] = (1 + pd.to_numeric(nav_df["daily_return"], errors="coerce").fillna(0.0)).cumprod()

        if "drawdown" not in nav_df.columns:
            nav_df["drawdown"] = nav_df["cum_return"] / nav_df["cum_return"].cummax() - 1

        num_positions = None
        if perf_contrib_path and Path(perf_contrib_path).exists():
            try:
                cdf = pd.read_csv(perf_contrib_path)
                if "ticker" in cdf.columns:
                    num_positions = int(cdf["ticker"].astype(str).nunique())
            except Exception:
                pass

        summary = {
            "start_date": str(nav_df["date"].iloc[0].date()) if "date" in nav_df.columns and pd.notna(nav_df["date"].iloc[0]) else None,
            "end_date": str(nav_df["date"].iloc[-1].date()) if "date" in nav_df.columns and pd.notna(nav_df["date"].iloc[-1]) else None,
            "initial_nav": safe_num(nav_df["nav"].iloc[0]),
            "last_nav": safe_num(nav_df["nav"].iloc[-1]),
            "cum_return": safe_num(nav_df["cum_return"].iloc[-1] - 1),
            "max_drawdown": safe_num(nav_df["drawdown"].min()),
            "num_positions": num_positions,
        }

        import json
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] auto-generated performance_summary: {sp}")
        return True
    except Exception as e:
        print(f"[WARN] failed to auto-build performance_summary: {e}")
        return False


def _performance_name_map(detail_or_summary: pd.DataFrame | None) -> pd.Series:
    if detail_or_summary is None or len(detail_or_summary) == 0 or "ticker" not in detail_or_summary.columns:
        return pd.Series(dtype="string")
    x = detail_or_summary.copy()
    x["ticker"] = normalize_ticker(x["ticker"])
    name_col = pick_first(x.columns.tolist(), ["report_name", "name", "name_final"])
    if name_col is None:
        return pd.Series(dtype="string")
    mp = (
        x[["ticker", name_col]]
        .dropna(subset=["ticker"])
        .drop_duplicates("ticker", keep="first")
        .set_index("ticker")[name_col]
        .astype("string")
    )
    return mp


def enrich_performance_with_names(perf: dict | None, detail_or_summary: pd.DataFrame | None) -> dict | None:
    if not perf:
        return perf
    mp = _performance_name_map(detail_or_summary)
    if len(mp) == 0:
        return perf

    out = dict(perf)
    contrib = out.get("contrib")
    if isinstance(contrib, pd.DataFrame) and len(contrib) > 0 and "ticker" in contrib.columns:
        c = contrib.copy()
        c["ticker"] = normalize_ticker(c["ticker"])
        if "name" not in c.columns:
            c["name"] = c["ticker"].map(mp)
        else:
            c["name"] = coalesce_prefer(c["name"], c["ticker"].map(mp), stringy=True)
        out["contrib"] = c
    return out


def make_performance_snapshot_tables(perf: dict | None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not perf:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    summary = perf.get("summary") or {}
    daily = perf.get("daily") if isinstance(perf.get("daily"), pd.DataFrame) else pd.DataFrame()
    contrib = perf.get("contrib") if isinstance(perf.get("contrib"), pd.DataFrame) else pd.DataFrame()

    summary_rows = []
    for k, label in [
        ("start_date", "성과 시작일"),
        ("end_date", "평가일"),
        ("initial_nav", "초기 NAV"),
        ("last_nav", "최종 NAV"),
        ("cum_return", "누적 수익률"),
        ("max_drawdown", "최대 낙폭(MDD)"),
        ("benchmark_cum_return", "벤치마크 누적 수익률"),
        ("active_return", "초과 수익률"),
        ("invested_base", "초기 투자금"),
        ("cash_after_rebalance", "리밸런싱 후 현금"),
        ("num_positions", "보유 종목 수"),
    ]:
        if k in summary and summary.get(k) is not None:
            summary_rows.append({"metric": label, "value": summary.get(k)})
    summary_df = pd.DataFrame(summary_rows)

    daily_df = pd.DataFrame()
    if len(daily) > 0:
        daily_df = daily.copy()
        if "date" in daily_df.columns:
            daily_df["date"] = pd.to_datetime(daily_df["date"], errors="coerce").dt.date.astype(str)
        if "nav" not in daily_df.columns and "value" in daily_df.columns:
            daily_df["nav"] = pd.to_numeric(daily_df["value"], errors="coerce")
        if "daily_return" not in daily_df.columns and "return" in daily_df.columns:
            daily_df["daily_return"] = pd.to_numeric(daily_df["return"], errors="coerce")
        if "drawdown" not in daily_df.columns and "cum_return" in daily_df.columns:
            cr = pd.to_numeric(daily_df["cum_return"], errors="coerce")
            daily_df["drawdown"] = cr / cr.cummax() - 1
        keep = [c for c in ["date", "nav", "daily_return", "cum_return", "benchmark_cum_return", "drawdown"] if c in daily_df.columns]
        daily_df = daily_df[keep].tail(10).copy()

    contrib_df = pd.DataFrame()
    if len(contrib) > 0:
        contrib_df = contrib.copy()
        if "ticker" in contrib_df.columns:
            contrib_df["ticker"] = normalize_ticker(contrib_df["ticker"])
        if "contribution_to_total_return" not in contrib_df.columns and "contribution" in contrib_df.columns:
            contrib_df["contribution_to_total_return"] = pd.to_numeric(contrib_df["contribution"], errors="coerce")
        keep = [c for c in ["ticker", "name", "position_value", "pnl", "contribution_to_total_return", "weight_at_last_nav"] if c in contrib_df.columns]
        if keep:
            contrib_df = contrib_df[keep].copy()
            if "name" not in contrib_df.columns:
                contrib_df["name"] = pd.NA
            if "contribution_to_total_return" in contrib_df.columns:
                contrib_df["contribution_to_total_return"] = pd.to_numeric(contrib_df["contribution_to_total_return"], errors="coerce")
                contrib_df = contrib_df.sort_values("contribution_to_total_return", ascending=False)
                top = contrib_df.head(5)
                bottom = contrib_df.tail(5).sort_values("contribution_to_total_return", ascending=True)
                contrib_df = pd.concat([top, bottom], ignore_index=True)
                contrib_df = contrib_df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)

    return summary_df, daily_df, contrib_df


def _format_performance_value(metric_label: str, value):
    if value is None or pd.isna(value):
        return "-"
    metric_label = str(metric_label)
    if metric_label in {"초기 NAV", "최종 NAV", "초기 투자금", "리밸런싱 후 현금"}:
        return fmt_num(value, 0)
    if metric_label in {"누적 수익률", "벤치마크 누적 수익률", "초과 수익률", "최대 낙폭(MDD)"}:
        v = safe_num(value)
        return f"{fmt_ratio(v * 100, 2)}%" if v is not None else "-"
    if metric_label in {"보유 종목 수"}:
        return fmt_int_plain(value)
    return text_or_dash(value)


def _format_performance_df_for_display(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    view = df.copy()
    if {"metric", "value"}.issubset(view.columns):
        view["value"] = [
            _format_performance_value(m, v)
            for m, v in zip(view["metric"], view["value"])
        ]
        return view

    for c in view.columns:
        if c in ["nav", "position_value", "pnl", "value"]:
            view[c] = view[c].map(lambda x: fmt_num(x, 0))
        elif c in ["daily_return", "cum_return", "benchmark_cum_return", "active_return", "drawdown", "contribution_to_total_return", "weight_at_last_nav"]:
            view[c] = view[c].map(lambda x: f"{fmt_ratio(safe_num(x) * 100, 2)}%" if safe_num(x) is not None else "-")
        elif c in ["ticker"]:
            view[c] = view[c].map(text_or_dash)
        elif c in ["name", "date", "metric", "value"]:
            view[c] = view[c].map(text_or_dash)
    return view


def dataframe_to_markdown_performance(df: pd.DataFrame) -> str:
    if df.empty:
        return "_없음_"
    view = _format_performance_df_for_display(df)
    return dataframe_to_markdown(view, max_rows=len(view))


def render_html_performance_table(df: pd.DataFrame, title: str) -> str:
    if df.empty:
        return f"<section><h2>{html.escape(title)}</h2><p>없음</p></section>"

    desc_map = {
        "metric": "지표",
        "value": "값",
        "date": "일자",
        "nav": "포트폴리오 NAV",
        "daily_return": "일간 수익률",
        "cum_return": "누적 수익률",
        "benchmark_cum_return": "벤치마크 누적 수익률",
        "active_return": "초과 수익률",
        "drawdown": "낙폭",
        "ticker": "종목코드",
        "name": "종목명",
        "position_value": "평가금액",
        "pnl": "손익",
        "contribution_to_total_return": "총수익 기여도",
        "weight_at_last_nav": "최종 비중",
    }

    view = _format_performance_df_for_display(df)
    cols = list(view.columns)
    header = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body_rows = []
    for _, row in view.iterrows():
        cells = []
        for c in cols:
            val = row[c]
            s = "-" if pd.isna(val) or str(val) in ["<NA>", "nan", "None"] else str(val)
            cells.append(f"<td>{html.escape(s)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    caption = " | ".join(f"{c}: {desc_map.get(c, c)}" for c in cols)
    return f"""
<section class="table-section">
  <h2>{html.escape(title)}</h2>
  <div class="table-wrap">
    <table class="summary-table">
      <caption>{html.escape(caption)}</caption>
      <thead><tr>{header}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
  </div>
</section>
"""


def build_performance_html(perf: dict | None, title: str | None = None) -> str:
    title = title or "직전 리밸런싱 이후 성과 평가"
    summary_df, daily_df, contrib_df = make_performance_snapshot_tables(perf)
    blocks = [f"<h2>0. {html.escape(title)}</h2>"]
    if summary_df.empty and daily_df.empty and contrib_df.empty:
        blocks.append("<p>성과 평가 데이터가 제공되지 않았습니다.</p>")
        return "\n".join(blocks)
    if not summary_df.empty:
        blocks.append(render_html_performance_table(summary_df, "0.1 요약 성과"))
    else:
        blocks.append("<h3>0.1 요약 성과</h3><p>없음</p>")
    if not daily_df.empty:
        blocks.append(render_html_performance_table(daily_df, "0.2 최근 일별 NAV / 수익률"))
    else:
        blocks.append("<h3>0.2 최근 일별 NAV / 수익률</h3><p>없음</p>")
    if not contrib_df.empty:
        blocks.append(render_html_performance_table(contrib_df, "0.3 종목별 기여도"))
    else:
        blocks.append("<h3>0.3 종목별 기여도</h3><p>없음</p>")
    return "\n".join(blocks)

# ----------------------------
# markdown + html builders
# ----------------------------
def _series_has_meaningful_values(s: pd.Series | pd.DataFrame) -> bool:
    if s is None:
        return False
    if isinstance(s, pd.DataFrame):
        if s.shape[1] == 0:
            return False
        s = s.iloc[:, 0]
    if len(s) == 0:
        return False
    if pd.api.types.is_numeric_dtype(s):
        return bool(s.notna().any())
    x = s.astype("string").replace({"<NA>": "", "nan": "", "None": "", "-": ""}).str.strip()
    return bool(x.ne("").any())


def _visible_summary_columns(df: pd.DataFrame) -> list[str]:
    preferred = [
        "ticker", "name", "action", "reason",
        "industry_code", "industry_name", "mcap_group", "mcap_rank_pct",
        "score", "score_adj", "hold_bonus_applied", "score_rank", "expectation_overlay_active", "expectation_score", "expectation_penalty", "quality_soft_penalty_active", "quality_penalty_total", "penalty_flag", "penalty_level", "penalty_reason", "penalty_desc", "quality_soft_penalty_active", "quality_penalty_total", "penalty_flag", "penalty_level", "penalty_reason", "penalty_desc", 
        "op_acc2", "op_acc2_log1p", "contrib_op", "contrib_op_log", "op_growth_streak2", "contrib_op_streak",
        "rev_acc2", "rev_acc2_log1p", "contrib_rev", "contrib_rev_log", "rev_growth_streak2", "contrib_rev_streak",
        "debt_log", "contrib_debt", "cfo_to_assets", "contrib_cfo", "cfo_isnull", "contrib_missing", "score_rebuilt",
        "price", "mcap", "year", "quarter",
        "revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq",
        "revenue", "op_income", "net_income",
        "per", "pbr", "psr",
        "cohort_status", "score_availability_reason", "latest_available_rebalance_month", "latest_available_year", "latest_available_quarter",
        "planned_qty", "planned_value", "day1_qty", "day1_value", "day2_qty", "day2_value", "day3_qty", "day3_value",
        "est_slippage_bps",
    ]
    preferred = [c for c in preferred if c in df.columns]
    always_keep = {"ticker", "name", "action", "reason"}
    out: list[str] = []
    for c in preferred:
        if c in always_keep or _series_has_meaningful_values(df[c]):
            out.append(c)
    return out




def _ordered_existing_columns(df: pd.DataFrame, ordered_cols: list[str]) -> list[str]:
    return [c for c in ordered_cols if c in df.columns]


def _clip_tiers_note(scoring_cfg: dict) -> str:
    tiers = scoring_cfg.get("clip_tiers", []) if isinstance(scoring_cfg, dict) else []
    if not isinstance(tiers, list) or len(tiers) == 0:
        return ""
    parts = []
    for item in tiers:
        if not isinstance(item, dict):
            continue
        upto = item.get("upto")
        slope = item.get("slope")
        if upto is None or slope is None:
            continue
        parts.append(f"|z|≤{upto}: {slope}배")
    return ", ".join(parts)
def make_summary_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "ticker", "report_name", "action", "reason",
        "industry_code", "report_industry_name", "mcap_group", "mcap_rank_pct",
        "score", "score_adj", "hold_bonus_applied", "score_rank", "expectation_overlay_active", "expectation_score", "expectation_penalty", "quality_soft_penalty_active", "quality_penalty_total", "penalty_flag", "penalty_level", "penalty_reason", "penalty_desc", "quality_soft_penalty_active", "quality_penalty_total", "penalty_flag", "penalty_level", "penalty_reason", "penalty_desc", 
        "op_acc2", "op_acc2_log1p", "contrib_op", "contrib_op_log", "op_growth_streak2", "contrib_op_streak",
        "rev_acc2", "rev_acc2_log1p", "contrib_rev", "contrib_rev_log", "rev_growth_streak2", "contrib_rev_streak",
        "debt_log", "contrib_debt", "cfo_to_assets", "contrib_cfo", "cfo_isnull", "contrib_missing", "score_rebuilt",
        "report_price", "report_mcap", "year", "quarter",
        "revenue_prev_q", "revenue_cur_q", "revenue_qoq",
        "op_prev_q", "op_cur_q", "op_qoq",
        "revenue", "op_income", "net_income",
        "per", "pbr", "psr",
        "cohort_status", "score_availability_reason", "latest_available_rebalance_month", "latest_available_year", "latest_available_quarter",
        "exec__planned_total_qty", "exec__planned_total_value",
        "exec__day1_qty", "exec__day1_order_value",
        "exec__day2_qty", "exec__day2_order_value",
        "exec__day3_qty", "exec__day3_order_value",
        "exec__est_slippage_bps",
    ]
    cols = [c for c in cols if c in df.columns]
    out = df[cols].copy()
    out = _dedupe_columns_keep_first(out)

    rename = {
        "report_name": "name",
        "report_industry_name": "industry_name",
        "report_price": "price",
        "report_mcap": "mcap",
        "exec__planned_total_qty": "planned_qty",
        "exec__planned_total_value": "planned_value",
        "exec__day1_qty": "day1_qty",
        "exec__day1_order_value": "day1_value",
        "exec__day2_qty": "day2_qty",
        "exec__day2_order_value": "day2_value",
        "exec__day3_qty": "day3_qty",
        "exec__day3_order_value": "day3_value",
        "exec__est_slippage_bps": "est_slippage_bps",
    }
    out = out.rename(columns=rename)
    out = _dedupe_columns_keep_first(out)
    visible = _visible_summary_columns(out)
    return out[visible].copy()


def dataframe_to_markdown(df: pd.DataFrame, max_rows: int = 30) -> str:
    if df.empty:
        return "_없음_"
    view = df.head(max_rows).copy()

    for c in view.columns:
        if c in [
            "score", "score_adj", "score_rank", "op_acc2", "op_acc2_log1p", "rev_acc2", "rev_acc2_log1p", "debt_log", "cfo_to_assets", "revenue_qoq", "op_qoq",
            "per", "pbr", "psr", "ev_ebit", "ev_ebitda", "contrib_op", "contrib_op_log", "contrib_op_streak", "contrib_rev", "contrib_rev_log", "contrib_rev_streak", "contrib_debt",
            "contrib_cfo", "contrib_missing", "score_rebuilt", "score_diff_vs_reported", "est_slippage_bps", "quality_penalty_total"
        ]:
            view[c] = view[c].map(lambda x: fmt_ratio(x, 2))
        elif c in ["price", "planned_qty", "day1_qty", "day2_qty", "day3_qty", "cfo_isnull", "year", "quarter"]:
            view[c] = view[c].map(lambda x: fmt_num(x, 0))
        elif c in ["mcap", "revenue", "op_income", "net_income", "planned_value", "day1_value", "day2_value", "day3_value", "revenue_prev_q", "revenue_cur_q", "op_prev_q", "op_cur_q"]:
            view[c] = view[c].map(fmt_large_krw)

    def esc(x):
        if pd.isna(x):
            return "-"
        s = str(x).replace("\n", " ").replace("|", "\\|")
        if s in ["<NA>", "nan", "None"]:
            return "-"
        return s

    cols = list(view.columns)
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    rows = []
    for _, row in view.iterrows():
        rows.append("| " + " | ".join(esc(row[c]) for c in cols) + " |")
    return "\n".join([header, sep] + rows)


def render_html_table(df: pd.DataFrame, title: str) -> str:
    if df.empty:
        return f"<section><h2>{html.escape(title)}</h2><p>없음</p></section>"

    column_desc = {
        "ticker": "종목코드", "name": "종목명", "action": "권고 액션", "reason": "선정/제외 사유", "target_selection_bucket": "선정 버킷", "score_rebuild_method": "점수 재구성 출처",
        "score": "최종 점수", "score_adj": "보유 보너스 반영 점수", "hold_bonus_applied": "보유 보너스 적용값", "score_rank": "기본 점수 순위", "quality_soft_penalty_active": "품질 패널티 사용 여부", "quality_penalty_total": "품질 패널티 총합", "penalty_flag": "패널티 적용 여부", "penalty_level": "패널티 강도", "penalty_reason": "패널티 사유 코드", "penalty_desc": "패널티 해설", "industry_code": "업종 코드", "industry_name": "업종명", "mcap_group": "시가총액 그룹", "mcap_rank_pct": "시가총액 백분위", "expectation_overlay_active": "기대반영 오버레이 사용 여부", "expectation_score": "기대반영 점수", "expectation_penalty": "기대반영 보정치",
        "year": "최근 사용 분기 연도", "quarter": "최근 사용 분기",
        "revenue_prev_q": "직전 단일분기 매출액", "revenue_cur_q": "당기 단일분기 매출액", "revenue_qoq": "단일분기 매출 QoQ",
        "op_prev_q": "직전 단일분기 영업이익", "op_cur_q": "당기 단일분기 영업이익", "op_qoq": "단일분기 영업이익 QoQ",
        "revenue": "TTM 매출액", "op_income": "TTM 영업이익", "net_income": "TTM 순이익",
        "op_acc2": "영업이익 가속도", "op_acc2_log1p": "로그 완화 영업이익 가속도", "contrib_op": "영업이익 가속 기여도", "contrib_op_log": "로그 완화 영업이익 가속 기여도", "op_growth_streak2": "영업이익 2분기 연속 개선 더미", "contrib_op_streak": "영업이익 연속개선 기여도", "rev_acc2": "매출 가속도", "rev_acc2_log1p": "로그 완화 매출 가속도", "contrib_rev": "매출 가속 기여도", "contrib_rev_log": "로그 완화 매출 가속 기여도", "rev_growth_streak2": "매출 2분기 연속 증가 더미", "contrib_rev_streak": "매출 연속증가 기여도", "debt_log": "부채비율 로그값", "contrib_debt": "부채 요인 기여도",
        "cfo_to_assets": "CFO/Assets 품질 팩터", "cfo_isnull": "CFO 결측 더미",
        "price": "현재가", "mcap": "시가총액", "per": "PER", "pbr": "PBR", "psr": "PSR", "ev_ebit": "EV Proxy/EBIT", "ev_ebitda": "EV Proxy/EBITDA", "cohort_status": "타깃 코호트 포함 상태", "score_availability_reason": "점수 가용 사유", "planned_qty": "총 계획 수량", "planned_value": "총 계획 금액",
        "day1_qty": "1일차 수량", "day1_value": "1일차 금액", "day2_qty": "2일차 수량", "day2_value": "2일차 금액",
        "day3_qty": "3일차 수량", "day3_value": "3일차 금액", "est_slippage_bps": "예상 슬리피지(bps)"
    }

    view = df.copy()
    for c in view.columns:
        if c in [
            "score", "score_adj", "score_rank", "op_acc2", "op_acc2_log1p", "rev_acc2", "rev_acc2_log1p", "debt_log", "cfo_to_assets", "revenue_qoq", "op_qoq",
            "per", "pbr", "psr", "ev_ebit", "ev_ebitda", "contrib_op", "contrib_op_log", "contrib_op_streak", "contrib_rev", "contrib_rev_log", "contrib_rev_streak", "contrib_debt",
            "contrib_cfo", "contrib_missing", "score_rebuilt", "score_diff_vs_reported", "est_slippage_bps", "quality_penalty_total"
        ]:
            view[c] = view[c].map(lambda x: fmt_ratio(x, 2))
        elif c in ["price", "planned_qty", "day1_qty", "day2_qty", "day3_qty", "cfo_isnull", "year", "quarter"]:
            view[c] = view[c].map(lambda x: fmt_num(x, 0))
        elif c in ["mcap", "revenue", "op_income", "net_income", "planned_value", "day1_value", "day2_value", "day3_value", "revenue_prev_q", "revenue_cur_q", "op_prev_q", "op_cur_q"]:
            view[c] = view[c].map(fmt_large_krw)

    cols = list(view.columns)
    header = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body_rows = []
    for _, row in view.iterrows():
        cells = []
        for c in cols:
            val = row[c]
            if pd.isna(val) or str(val) in ["<NA>", "nan", "None"]:
                s = "-"
            else:
                s = str(val)
            cells.append(f"<td>{html.escape(s)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    caption = " | ".join(f"{c}: {column_desc.get(c, c)}" for c in cols)
    return f"""
<section class="table-section">
  <h2>{html.escape(title)}</h2>
  <div class="table-wrap">
    <table class="summary-table">
      <caption>{html.escape(caption)}</caption>
      <thead><tr>{header}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
  </div>
</section>
"""


def build_factor_note_html(spec: dict) -> str:
    items = []
    for k, v in spec.get("factor_notes", {}).items():
        items.append(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>")
    if not items:
        return ""
    return f"""
<section>
  <h3>3.3 핵심 팩터 설명</h3>
  <div class="table-wrap">
    <table class="factor-table">
      <thead><tr><th>팩터</th><th>설명</th></tr></thead>
      <tbody>{''.join(items)}</tbody>
    </table>
  </div>
</section>
"""


def _render_value(label: str, value: str) -> str:
    return f"<li><strong>{html.escape(label)}:</strong> {html.escape(value)}</li>"


def _format_multiple_lines(row: pd.Series) -> list[str]:
    out = []
    vals = [
        ("PER", row.get("per")),
        ("PBR", row.get("pbr")),
        ("PSR", row.get("psr")),
    ]
    # EV metrics are only shown if actually available
    if pd.notna(row.get("ev_ebit")):
        vals.append(("EV Proxy/EBIT", row.get("ev_ebit")))
    if pd.notna(row.get("ev_ebitda")):
        vals.append(("EV Proxy/EBITDA", row.get("ev_ebitda")))

    for label, val in vals:
        s = fmt_ratio(val, 2)
        if s != "-":
            out.append(_render_value(label, s))
    return out


def render_detail_card(row: pd.Series) -> str:
    def _maybe(li: list[str], label: str, value: str) -> None:
        if value != "-":
            li.append(_render_value(label, value))

    mult_lines = _format_multiple_lines(row)
    mult_html = "<ul>" + "".join(mult_lines) + "</ul>" if mult_lines else "<p>가용 멀티플 데이터 없음</p>"

    industry_name = row.get("report_industry_name")
    industry_html = ""
    if pd.notna(industry_name) and str(industry_name) not in ["<NA>", "nan", "None", "-"]:
        industry_html = f"<li><strong>업종명:</strong> {html.escape(str(industry_name))}</li>"

    factor_items: list[str] = []
    for label, key, fmt in [
        ("OpIncome_acc2", "op_acc2", lambda v: fmt_ratio(v, 4)),
        ("OpIncome_acc2_log1p", "op_acc2_log1p", lambda v: fmt_ratio(v, 4)),
        ("op_growth_streak2", "op_growth_streak2", lambda v: fmt_ratio(v, 4)),
        ("Revenue_acc2", "rev_acc2", lambda v: fmt_ratio(v, 4)),
        ("Revenue_acc2_log1p", "rev_acc2_log1p", lambda v: fmt_ratio(v, 4)),
        ("rev_growth_streak2", "rev_growth_streak2", lambda v: fmt_ratio(v, 4)),
        ("Debt_to_Equity_log", "debt_log", lambda v: fmt_ratio(v, 4)),
        ("Quality_CFO_to_Assets", "cfo_to_assets", lambda v: fmt_ratio(v, 4)),
        ("CFO_isnull", "cfo_isnull", lambda v: fmt_num(v, 0)),
    ]:
        _maybe(factor_items, label, fmt(row.get(key)))
    factor_html = "<ul>" + "".join(factor_items) + "</ul>" if factor_items else "<p>가용 팩터 데이터 없음</p>"

    contrib_items: list[str] = []
    for label, key in [
        ("contrib_op", "contrib_op"),
        ("contrib_op_log", "contrib_op_log"),
        ("contrib_op_streak", "contrib_op_streak"),
        ("contrib_rev", "contrib_rev"),
        ("contrib_rev_log", "contrib_rev_log"),
        ("contrib_rev_streak", "contrib_rev_streak"),
        ("contrib_debt", "contrib_debt"),
        ("contrib_cfo", "contrib_cfo"),
        ("contrib_missing", "contrib_missing"),
    ]:
        _maybe(contrib_items, label, fmt_ratio(row.get(key), 4))
    contrib_html = "<ul>" + "".join(contrib_items) + "</ul>" if contrib_items else "<p>가용 기여도 데이터 없음</p>"

    return f"""
<article class="card">
  <h3>{html.escape(str(row.get('report_name', '-')))} ({html.escape(str(row.get('ticker', '-')) )})</h3>
  <div class="grid">
    <section>
      <h4>기본</h4>
      <ul>
        {_render_value("액션", str(row.get('action', '-')))}
        {_render_value("사유", text_or_dash(row.get('reason', '-')))}
        {_render_value("보고서 SCORE", f"{fmt_ratio(row.get('score'), 4)} / rank {fmt_int_plain(row.get('score_rank'))}")}
        {_render_value("조정 SCORE", fmt_ratio(row.get('score_adj'), 4))}
        {_render_value("holding bonus", fmt_ratio(row.get('hold_bonus_applied'), 4))}
        {_render_value("Quality penalty", fmt_ratio(row.get('quality_penalty_total'), 4))}
        {_render_value("Penalty level", text_or_dash(row.get('penalty_level')))}
        {_render_value("Penalty reason", text_or_dash(row.get('penalty_reason')))}
        {_render_value("Penalty 설명", text_or_dash(row.get('penalty_desc')))}
        {_render_value("재구성 SCORE", fmt_ratio(row.get('score_rebuilt'), 4))}
        {_render_value("SCORE 차이", fmt_ratio(row.get('score_diff_vs_reported'), 4))}
        {_render_value("industry_code", fmt_int_plain(row.get('industry4')))}
        {_render_value("시가총액 그룹", text_or_dash(row.get('mcap_group', '-')))}
        {_render_value("시가총액 백분위", fmt_ratio(row.get('mcap_rank_pct'), 4))}
        {_render_value("Expectation score", fmt_ratio(row.get('expectation_score'), 4))}
        {_render_value("Expectation penalty", fmt_ratio(row.get('expectation_penalty'), 4))}
        {industry_html}
        {_render_value("cohort_status", text_or_dash(row.get('cohort_status', '-')))}
        {_render_value("score_availability_reason", text_or_dash(row.get('score_availability_reason', '-')))}
        {_render_value("latest_available_rebalance_month", text_or_dash(row.get('latest_available_rebalance_month', '-')))}
        {_render_value("latest_available_year/quarter", f"{fmt_int_plain(row.get('latest_available_year'))} / {fmt_int_plain(row.get('latest_available_quarter'))}")}
        {_render_value("현재가", fmt_num(row.get('report_price'), 0))}
        {_render_value("시가총액", fmt_large_krw(row.get('report_mcap')))}
      </ul>
    </section>
    <section>
      <h4>재무</h4>
      <ul>
        {_render_value("기준연도/분기", f"{fmt_int_plain(row.get('year'))} / {fmt_int_plain(row.get('quarter'))}")}
        {_render_value("매출(TTM)", fmt_large_krw(row.get('revenue')))}
        {_render_value("영업이익(TTM)", fmt_large_krw(row.get('op_income')))}
        {_render_value("순이익(TTM)", fmt_large_krw(row.get('net_income')))}
        {_render_value("직전 단분기 매출", fmt_large_krw(row.get('revenue_prev_q')))}
        {_render_value("당기 단분기 매출", fmt_large_krw(row.get('revenue_cur_q')))}
        {_render_value("단분기 매출 QoQ", fmt_ratio(row.get('revenue_qoq'), 4))}
        {_render_value("직전 단분기 영업이익", fmt_large_krw(row.get('op_prev_q')))}
        {_render_value("당기 단분기 영업이익", fmt_large_krw(row.get('op_cur_q')))}
        {_render_value("단분기 영업이익 QoQ", fmt_ratio(row.get('op_qoq'), 4))}
        {_render_value("영업현금흐름(CFO)", fmt_large_krw(row.get('cfo')))}
      </ul>
    </section>
    <section>
      <h4>팩터</h4>
      {factor_html}
    </section>
    <section>
      <h4>스코어 기여도</h4>
      {contrib_html}
    </section>
    <section>
      <h4>멀티플</h4>
      {mult_html}
    </section>
    <section>
      <h4>실행 계획</h4>
      <ul>
        {_render_value("총 목표 주문수량", f"{fmt_num(row.get('exec__planned_total_qty'), 0)}주")}
        {_render_value("총 목표 주문금액", fmt_large_krw(row.get('exec__planned_total_value')))}
        {_render_value("Day1", f"{fmt_num(row.get('exec__day1_qty'), 0)}주 / {fmt_large_krw(row.get('exec__day1_order_value'))}")}
        {_render_value("Day2", f"{fmt_num(row.get('exec__day2_qty'), 0)}주 / {fmt_large_krw(row.get('exec__day2_order_value'))}")}
        {_render_value("Day3", f"{fmt_num(row.get('exec__day3_qty'), 0)}주 / {fmt_large_krw(row.get('exec__day3_order_value'))}")}
        {_render_value("예상 슬리피지", f"{fmt_ratio(row.get('exec__est_slippage_bps'), 3)} bps")}
      </ul>
    </section>
  </div>
</article>
"""


def build_html_report(spec: dict, asof: str, target_date: str, strategy: str, buy_tab: pd.DataFrame, sell_tab: pd.DataFrame, hold_tab: pd.DataFrame, buy_df: pd.DataFrame, sell_df: pd.DataFrame, hold_df: pd.DataFrame, perf: dict | None = None, performance_title: str | None = None, report_title: str | None = None) -> str:
    factor_table = build_factor_note_html(spec)
    buy_cards = "".join(render_detail_card(r) for _, r in buy_df.iterrows()) if not buy_df.empty else "<p>없음</p>"
    sell_cards = "".join(render_detail_card(r) for _, r in sell_df.iterrows()) if not sell_df.empty else "<p>없음</p>"
    hold_cards = "".join(render_detail_card(r) for _, r in hold_df.iterrows()) if not hold_df.empty else "<p>없음</p>"

    score_formula = "<br>".join(html.escape(x) for x in spec["formula_lines"])
    calc_steps = "".join(f"<li>{html.escape(s)}</li>" for s in spec["calc_steps"])
    filters = "".join(f"<li>{html.escape(f)}</li>" for f in spec["filters"])

    note_diff = """
<p class="note">주: <code>score_rebuilt</code>는 보고서 설명을 위해 현재 보유한 표준화 팩터값으로 재구성한 점수입니다.
실제 포트폴리오 선별에 사용된 최종 점수와는 전처리·클리핑·제약 적용 차이로 인해 다를 수 있습니다.</p>
"""

    report_title = report_title or "리밸런싱 보고서"
    html_text = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{html.escape(report_title)}</title>
<style>
body {{
  font-family: Arial, "Malgun Gothic", sans-serif;
  line-height: 1.6;
  margin: 28px;
  color: #222;
}}
h1, h2, h3, h4 {{ color: #1f2937; }}
code, pre {{ font-family: Consolas, monospace; }}
.note {{
  background: #f8fafc; border-left: 4px solid #64748b; padding: 10px 12px; border-radius: 6px;
}}
.table-wrap {{
  overflow-x: auto; border: 1px solid #e5e7eb; border-radius: 10px; background: #fff;
}}
.summary-table, .factor-table {{
  width: 100%; border-collapse: collapse; font-size: 14px; min-width: 1100px;
}}
.summary-table thead th, .factor-table thead th {{
  position: sticky; top: 0; background: #f3f4f6; z-index: 1;
}}
.summary-table th, .summary-table td, .factor-table th, .factor-table td {{
  border: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; white-space: nowrap;
}}
.summary-table tbody tr:nth-child(even) {{ background: #fafafa; }}
.card {{
  border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin: 16px 0; background: #fff;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}}
.grid {{
  display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 12px 20px;
}}
ul {{ margin-top: 6px; }}
.section-meta {{
  background: #f8fafc; padding: 12px 14px; border-radius: 10px; border: 1px solid #e5e7eb;
}}
</style>
</head>
<body>
<h1>{html.escape(report_title)}</h1>
<p>본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.
개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.</p>
<div class="section-meta">
  <ul>
    <li><strong>보고서 제목:</strong> {html.escape(report_title)}</li>
    <li><strong>기준일(asof):</strong> {html.escape(asof)}</li>
    <li><strong>목표 리밸런싱일(target):</strong> {html.escape(target_date)}</li>
    <li><strong>전략명:</strong> {html.escape(strategy)}</li>
    <li><strong>전략 설명:</strong> {html.escape(spec["title"])}</li>
  </ul>
</div>

{build_performance_html(perf, performance_title)}

<h2>1. 유의사항</h2>
<ul>
  <li>본 전략은 이익 가속도와 재무 건전성 중심의 상대평가 전략입니다.</li>
  <li>대형 우량주라도 해당 시점의 점수 경쟁에서 제외될 수 있습니다.</li>
  <li>과거 부진했던 기업이라도 현재 흑자 전환 및 이익 가속이 확인되면 편입될 수 있습니다.</li>
  <li>BUY/SELL는 절대적 우열이 아니라 이번 분기 기준 상대 점수 재정렬 결과입니다.</li>
</ul>

<h2>2. 요약</h2>
<ul>
  <li>BUY 종목 수: <strong>{len(buy_df)}</strong></li>
  <li>SELL 종목 수: <strong>{len(sell_df)}</strong></li>
  <li>HOLD/REVIEW 종목 수: <strong>{len(hold_df)}</strong></li>
</ul>

<h2>3. SCORE 계산 방식</h2>
<h3>3.1 점수 식</h3>
<pre><code>{score_formula}</code></pre>
<p><strong>스코어링 방식:</strong> {html.escape(spec["scoring_note"])}</p>

<h3>3.2 계산 절차</h3>
<ul>{calc_steps}</ul>

{factor_table}

<h3>3.4 포트폴리오 종목별 SCORE 계산 표시</h3>
<ul>
  <li>각 종목에 대해 contrib_op_log / contrib_op_streak / contrib_rev / contrib_rev_streak / contrib_debt / contrib_cfo / contrib_missing와 quality penalty 해설을 별도로 출력합니다.</li>
  <li>score_rebuilt는 위 기여도를 합산해 재구성한 점수입니다.</li>
  <li>score_diff_vs_reported는 보고서 score와 재구성 score의 차이입니다.</li>
</ul>
{note_diff}

<h3>3.5 필터 / 제약조건</h3>
<ul>{filters}</ul>

{render_html_table(buy_tab, "4. BUY 종목 요약표")}
{render_html_table(sell_tab, "5. SELL 종목 요약표")}
{render_html_table(hold_tab, "6. REVIEW 종목 요약표")}

<section>
  <h2>7. BUY 종목 상세</h2>
  {buy_cards}
</section>

<section>
  <h2>8. SELL 종목 상세</h2>
  {sell_cards}
</section>

<section>
  <h2>9. REVIEW 종목 상세</h2>
  {hold_cards}
</section>
</body>
</html>"""
    return html_text


def row_to_markdown_block(row: pd.Series) -> str:
    lines = []
    lines.append(f"### {row.get('report_name', '-')} ({row['ticker']})")
    lines.append("")
    lines.append(f"- 액션: **{row.get('action', '-')}**")
    lines.append(f"- 사유: {text_or_dash(row.get('reason', '-'))}")
    lines.append(f"- 보고서 SCORE: **{fmt_ratio(row.get('score'), 4)}** / rank {fmt_int_plain(row.get('score_rank'))}")
    lines.append(f"- 조정 SCORE: **{fmt_ratio(row.get('score_adj'), 4)}**")
    lines.append(f"- holding bonus: {fmt_ratio(row.get('hold_bonus_applied'), 4)}")
    lines.append(f"- Quality penalty: {fmt_ratio(row.get('quality_penalty_total'), 4)}")
    lines.append(f"- Penalty level: {text_or_dash(row.get('penalty_level'))}")
    lines.append(f"- Penalty reason: {text_or_dash(row.get('penalty_reason'))}")
    lines.append(f"- Penalty 설명: {text_or_dash(row.get('penalty_desc'))}")
    lines.append(f"- 재구성 SCORE: **{fmt_ratio(row.get('score_rebuilt'), 4)}**")
    lines.append(f"- SCORE 차이(보고서-재구성): {fmt_ratio(row.get('score_diff_vs_reported'), 4)}")
    lines.append(f"- industry_code: {text_or_dash(row.get('industry_code'))}")
    lines.append(f"- 시가총액 그룹: {text_or_dash(row.get('mcap_group'))}")
    lines.append(f"- 시가총액 백분위: {fmt_ratio(row.get('mcap_rank_pct'), 4)}")
    lines.append(f"- Expectation score: {fmt_ratio(row.get('expectation_score'), 4)}")
    lines.append(f"- Expectation penalty: {fmt_ratio(row.get('expectation_penalty'), 4)}")
    if pd.notna(row.get("report_industry_name")) and str(row.get("report_industry_name")) not in ["<NA>", "nan", "None", "-"]:
        lines.append(f"- 업종명: {row.get('report_industry_name')}")
    if pd.notna(row.get("cohort_status")) and str(row.get("cohort_status")) not in ["<NA>", "nan", "None", "-"]:
        lines.append(f"- cohort_status: {row.get('cohort_status')}")
    if pd.notna(row.get("score_availability_reason")) and str(row.get("score_availability_reason")) not in ["<NA>", "nan", "None", "-"]:
        lines.append(f"- score_availability_reason: {row.get('score_availability_reason')}")
    if pd.notna(row.get("latest_available_rebalance_month")) and str(row.get("latest_available_rebalance_month")) not in ["<NA>", "nan", "None", "-"]:
        lines.append(f"- latest_available_rebalance_month: {row.get('latest_available_rebalance_month')}")
    if pd.notna(row.get("latest_available_year")):
        lines.append(f"- latest_available_year/quarter: {fmt_int_plain(row.get('latest_available_year'))} / {fmt_int_plain(row.get('latest_available_quarter'))}")
    lines.append(f"- 현재가: {fmt_num(row.get('report_price'), 0)}")
    lines.append(f"- 시가총액: {fmt_large_krw(row.get('report_mcap'))}")
    lines.append("")
    lines.append("재무:")
    for t in [
        f"- 매출(TTM): {fmt_large_krw(row.get('revenue'))}",
        f"- 영업이익(TTM): {fmt_large_krw(row.get('op_income'))}",
        f"- 순이익(TTM): {fmt_large_krw(row.get('net_income'))}",
        f"- 영업현금흐름(CFO): {fmt_large_krw(row.get('cfo'))}",
    ]:
        if not t.endswith('-'):
            lines.append(t)
    lines.append("")
    lines.append("팩터:")
    for label, key, fmt in [
        ("OpIncome_acc2", 'op_acc2', lambda v: fmt_ratio(v,4)),
        ("OpIncome_acc2_log1p", 'op_acc2_log1p', lambda v: fmt_ratio(v,4)),
        ("op_growth_streak2", 'op_growth_streak2', lambda v: fmt_ratio(v,4)),
        ("Revenue_acc2", 'rev_acc2', lambda v: fmt_ratio(v,4)),
        ("Revenue_acc2_log1p", 'rev_acc2_log1p', lambda v: fmt_ratio(v,4)),
        ("rev_growth_streak2", 'rev_growth_streak2', lambda v: fmt_ratio(v,4)),
        ("Debt_to_Equity_log", 'debt_log', lambda v: fmt_ratio(v,4)),
        ("Quality_CFO_to_Assets", 'cfo_to_assets', lambda v: fmt_ratio(v,4)),
        ("CFO_isnull", 'cfo_isnull', lambda v: fmt_num(v,0)),
    ]:
        s = fmt(row.get(key))
        if s != '-':
            lines.append(f"- {label}: {s}")
    lines.append("")
    lines.append("스코어 기여도:")
    for label, key in [("contrib_op",'contrib_op'),("contrib_op_log",'contrib_op_log'),("contrib_op_streak",'contrib_op_streak'),("contrib_rev",'contrib_rev'),("contrib_rev_log",'contrib_rev_log'),("contrib_rev_streak",'contrib_rev_streak'),("contrib_debt",'contrib_debt'),("contrib_cfo",'contrib_cfo'),("contrib_missing",'contrib_missing')]:
        s = fmt_ratio(row.get(key),4)
        if s != '-':
            lines.append(f"- {label}: {s}")
    lines.append("")
    mults = []
    for lbl, key in [("PER","per"),("PBR","pbr"),("PSR","psr"),("EV/EBIT","ev_ebit"),("EV/EBITDA","ev_ebitda")]:
        s = fmt_ratio(row.get(key), 2)
        if s != "-":
            mults.append(f"- {lbl}: {s}")
    lines.append("멀티플:")
    lines.extend(mults if mults else ["- 가용 멀티플 데이터 없음"])
    lines.append("")
    lines.append("실행 계획:")
    for t in [
        f"- 총 목표 주문수량: {fmt_num(row.get('exec__planned_total_qty'), 0)}주",
        f"- 총 목표 주문금액: {fmt_large_krw(row.get('exec__planned_total_value'))}",
        f"- Day1: {fmt_num(row.get('exec__day1_qty'), 0)}주 / {fmt_large_krw(row.get('exec__day1_order_value'))}",
        f"- Day2: {fmt_num(row.get('exec__day2_qty'), 0)}주 / {fmt_large_krw(row.get('exec__day2_order_value'))}",
        f"- Day3: {fmt_num(row.get('exec__day3_qty'), 0)}주 / {fmt_large_krw(row.get('exec__day3_order_value'))}",
        f"- 예상 슬리피지: {fmt_ratio(row.get('exec__est_slippage_bps'), 3)} bps",
    ]:
        if not t.endswith('-') and not t.endswith('/ -'):
            lines.append(t)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions_csv", required=True)
    ap.add_argument("--features_live", required=True)
    ap.add_argument("--execution_plan", default=None)
    ap.add_argument("--supp_file", action="append", default=[])
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--asof", required=True)
    ap.add_argument("--target_date", required=True)
    ap.add_argument("--output_md", required=True)
    ap.add_argument("--output_csv", required=True)
    ap.add_argument("--output_html", default=None)
    ap.add_argument("--scores_csv", default=None)
    ap.add_argument("--performance_summary", default=None)
    ap.add_argument("--performance_daily", default=None)
    ap.add_argument("--performance_contrib", default=None)
    ap.add_argument("--performance_title", default="직전 리밸런싱 이후 성과 평가")
    ap.add_argument("--report_title", default="리밸런싱 보고서")
    args = ap.parse_args()

    actions = pd.read_csv(args.actions_csv)
    feat = pd.read_parquet(args.features_live)

    if "ticker" not in actions.columns or "ticker" not in feat.columns:
        raise ValueError("actions_csv and features_live must contain 'ticker'")

    actions["ticker"] = normalize_ticker(actions["ticker"])
    feat["ticker"] = normalize_ticker(feat["ticker"])

    target_period_map = build_target_period_map(feat, args.target_date)
    detail = build_detail(actions, feat, target_period_map=target_period_map)

    metric = infer_metric_from_path(args.features_live) or infer_metric_from_path(args.actions_csv)
    scores_csv = args.scores_csv or auto_detect_scores_csv(
        asof=args.asof,
        metric=metric,
        strategy=args.strategy,
        target_date=args.target_date,
        features_live=args.features_live,
    )
    if scores_csv:
        print(f"[INFO] exact scoring source: {scores_csv}")
        detail = merge_exact_scores(detail, scores_csv)
    else:
        print("[WARN] exact scoring source not found; score rebuild will use approximate feature-based reconstruction")
    detail = merge_execution_plan(detail, args.execution_plan)
    detail = merge_supplemental(detail, args.supp_file, asof=args.asof, target_period_map=target_period_map)
    reference_csv = None
    for _cand in [
        Path("reference/krx_industry_code_name_by_ticker_from_data_3133_20260329.csv"),
        Path("data/reference/krx_industry_code_name_by_ticker_from_data_3133_20260329.csv"),
    ]:
        if _cand.exists():
            reference_csv = _cand
            break
    detail = merge_industry_reference(detail, asof=args.asof, reference_csv=reference_csv)
    try:
        detail = attach_industry(detail, reference_csv=reference_csv, prefer_reference=True)
    except Exception as e:
        print(f"[WARN] attach_industry final failed: {e}")
    detail = add_derived_valuations(detail)
    strategy_cfg = load_strategy_config(args.strategy)
    detail = add_score_breakdown(detail, args.strategy, strategy_cfg=strategy_cfg)
    detail = add_quality_penalty_info(detail)

    # ?? C: ?? ??? ???? ??? industry_code -> industry_name ???? ??
    detail = enforce_industry_name_from_code(detail, reference_csv=reference_csv)

    spec = get_score_spec(args.strategy, strategy_cfg=strategy_cfg)

    auto_build_performance_summary(
        args.performance_summary,
        args.performance_daily,
        args.performance_contrib,
    )
    perf = load_optional_performance(args.performance_summary, args.performance_daily, args.performance_contrib)
    perf = enrich_performance_with_names(perf, detail)
    perf_summary_df, perf_daily_df, perf_contrib_df = make_performance_snapshot_tables(perf)

    buy_df = detail[detail["action"] == "BUY"].copy().sort_values(["score_rank", "score"], ascending=[True, False], na_position="last")
    sell_df = detail[detail["action"] == "SELL"].copy().sort_values(["ticker"])
    hold_df = detail[detail["action"].isin(["HOLD", "HOLD_REVIEW", "REVIEW"])].copy().sort_values(["ticker"])

    buy_tab = _dedupe_columns_keep_first(make_summary_table(buy_df))
    sell_tab = _dedupe_columns_keep_first(make_summary_table(sell_df))
    hold_tab = _dedupe_columns_keep_first(make_summary_table(hold_df))

    detail_csv = pd.concat([buy_tab, sell_tab, hold_tab], ignore_index=True)
    if len(detail_csv) > 0:
        keep_cols = [c for c in detail_csv.columns if _series_has_meaningful_values(detail_csv[c]) or c in {"ticker", "name", "action", "reason"}]
        detail_csv = detail_csv[keep_cols].copy()

    version_tag = parse_feat_v_from_features_path(args.features_live)
    version_tag = str(version_tag) if version_tag is not None else None


    output_md_path = build_output_file_path(
        args.output_md,
        asof=args.asof,
        target_date=args.target_date,
        metric=metric,
        strategy=args.strategy,
        version_tag=version_tag,
        default_basename="rebalance_report",
        default_suffix=".md",
    )
    output_csv_path = build_output_file_path(
        args.output_csv,
        asof=args.asof,
        target_date=args.target_date,
        metric=metric,
        strategy=args.strategy,
        version_tag=version_tag,
        default_basename="rebalance_report_detail",
        default_suffix=".csv",
    )
    output_html_path = None
    if args.output_html:
        output_html_path = build_output_file_path(
            args.output_html,
            asof=args.asof,
            target_date=args.target_date,
            metric=metric,
            strategy=args.strategy,
            version_tag=version_tag,
            default_basename="rebalance_report",
            default_suffix=".html",
        )

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    detail_csv.to_csv(output_csv_path, index=False, encoding="utf-8-sig")

    md = []
    md.append(f"# {args.report_title}")
    md.append("")
    md.append("본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.")
    md.append("개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.")
    md.append("본 보고서는 투자판단 보조를 위한 정량 리밸런싱 자료이며, 최종 주문 집행 전 유동성·이벤트·체결 가능성을 추가 점검합니다.")
    md.append("")
    md.append(f"- 보고서 제목: **{args.report_title}**")
    md.append(f"- 기준일(asof): **{args.asof}**")
    md.append(f"- 목표 리밸런싱일(target): **{args.target_date}**")
    md.append(f"- 전략명: **{args.strategy}**")
    md.append(f"- 전략 설명: **{spec['title']}**")
    md.append(f"- 점수 재구성 원칙: **exact score csv 우선, 없으면 feature 기반 근사 재구성**")
    md.append("")
    if perf:
        md.append(f"## 0. {args.performance_title}")
        md.append("")
        md.append("### 0.1 요약 성과")
        md.append("")
        md.append(dataframe_to_markdown_performance(perf_summary_df))
        md.append("")
        if not perf_daily_df.empty:
            md.append("### 0.2 최근 일별 NAV / 수익률")
            md.append("")
            md.append(dataframe_to_markdown_performance(perf_daily_df))
            md.append("")
        if not perf_contrib_df.empty:
            md.append("### 0.3 종목별 기여도")
            md.append("")
            md.append(dataframe_to_markdown_performance(perf_contrib_df))
            md.append("")
    md.append("## 1. 유의사항")
    md.append("")
    md.append("- 본 전략은 이익 가속도와 재무 건전성 중심의 상대평가 전략입니다.")
    md.append("- 대형 우량주라도 해당 시점의 점수 경쟁에서 제외될 수 있습니다.")
    md.append("- 과거 부진했던 기업이라도 현재 흑자 전환 및 이익 가속이 확인되면 편입될 수 있습니다.")
    md.append("- BUY/SELL는 절대적 우열이 아니라 이번 분기 기준 상대 점수 재정렬 결과입니다.")
    md.append("")
    md.append("## 2. 요약")
    md.append("")
    md.append(f"- BUY 종목 수: **{len(buy_df)}**")
    md.append(f"- SELL 종목 수: **{len(sell_df)}**")
    md.append(f"- HOLD/REVIEW 종목 수: **{len(hold_df)}**")
    if "target_selection_bucket" in detail.columns:
        keep_cnt = int((detail.get("target_selection_bucket") == "keep_current_top_n").fillna(False).sum())
        md.append(f"- 기존 보유 상위 점수 유지 종목 수: **{keep_cnt}**")
    md.append("")
    md.append("## 3. SCORE 계산 방식")
    md.append("")
    md.append("### 3.1 점수 식")
    md.append("")
    md.append("```text")
    md.extend(spec["formula_lines"])
    md.append("```")
    md.append("")
    md.append(f"- 스코어링 방식: {spec['scoring_note']}")
    md.append("")
    md.append("### 3.2 계산 절차")
    md.append("")
    for s in spec["calc_steps"]:
        md.append(f"- {s}")
    md.append("")
    md.append("### 3.3 핵심 팩터 설명")
    md.append("")
    for k, v in spec.get("factor_notes", {}).items():
        md.append(f"- **{k}**: {v}")
    md.append("")
    md.append("### 3.4 포트폴리오 종목별 SCORE 계산 표시")
    md.append("")
    md.append("- 각 종목에 대해 contrib_op_log / contrib_op_streak / contrib_rev / contrib_rev_streak / contrib_debt / contrib_cfo / contrib_missing와 quality penalty 해설을 별도로 출력합니다.")
    md.append("- score_rebuilt는 가능한 경우 score_latest_rebalance의 실제 factor contribution을 합산한 값입니다.")
    md.append("- exact scoring source를 찾지 못한 경우에만 feature 기반 근사 재구성을 사용합니다.")
    md.append("")
    md.append("### 3.5 필터 / 제약조건")
    md.append("")
    for f in spec["filters"]:
        md.append(f"- {f}")
    md.append("")
    md.append("## 4. BUY 종목 요약표")
    md.append("")
    md.append(dataframe_to_markdown(buy_tab))
    md.append("")
    md.append("## 5. SELL 종목 요약표")
    md.append("")
    md.append(dataframe_to_markdown(sell_tab))
    md.append("")
    md.append("## 6. REVIEW 종목 요약표")
    md.append("")
    md.append(dataframe_to_markdown(hold_tab))
    md.append("")
    md.append("## 7. BUY 종목 상세")
    md.append("")
    if buy_df.empty:
        md.append("_없음_")
    else:
        for _, row in buy_df.iterrows():
            md.append(row_to_markdown_block(row))
            md.append("")
    md.append("## 8. SELL 종목 상세")
    md.append("")
    if sell_df.empty:
        md.append("_없음_")
    else:
        for _, row in sell_df.iterrows():
            md.append(row_to_markdown_block(row))
            md.append("")

    md.append("## 9. REVIEW 종목 상세")
    md.append("")
    if hold_df.empty:
        md.append("_없음_")
    else:
        for _, row in hold_df.iterrows():
            md.append(row_to_markdown_block(row))
            md.append("")

    md_text = "\n".join(md)
    write_text(output_md_path, md_text)

    if output_html_path is not None:
        html_text = build_html_report(
            spec=spec,
            asof=args.asof,
            target_date=args.target_date,
            strategy=args.strategy,
            buy_tab=buy_tab,
            sell_tab=sell_tab,
            hold_tab=hold_tab,
            buy_df=buy_df,
            sell_df=sell_df,
            hold_df=hold_df,
            perf=perf,
            performance_title=args.performance_title,
            report_title=args.report_title,
        )
        write_text(output_html_path, html_text)

    print(f"[OK] saved md  : {output_md_path}")
    print(f"[OK] saved csv : {output_csv_path}")
    if output_html_path is not None:
        print(f"[OK] saved html: {output_html_path}")


if __name__ == "__main__":
    main()
