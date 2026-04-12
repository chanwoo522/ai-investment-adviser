from __future__ import annotations

from pathlib import Path
from typing import Iterable
import pandas as pd

DEFAULT_REFERENCE_CANDIDATES = [
    Path(r"C:\Users\chanw\ai_inv_adv\data\reference\krx_industry_code_name_by_ticker_from_data_3133_20260329.csv"),
    Path("data/reference/krx_industry_code_name_by_ticker_from_data_3133_20260329.csv"),
    Path("reference/krx_industry_code_name_by_ticker_from_data_3133_20260329.csv"),
    Path("data/reference/krx_industry_code_name_by_ticker.csv"),
    Path("reference/krx_industry_code_name_by_ticker.csv"),
    Path("data/reference/industry_code_name_by_ticker.csv"),
    Path("reference/industry_code_name_by_ticker.csv"),
    Path("data/reference/krx_company_industry_6digit.csv"),
    Path("reference/krx_company_industry_6digit.csv"),
]


def normalize_ticker_series(s: pd.Series) -> pd.Series:
    x = s.astype(str).str.extract(r"(\d+)")[0]
    return x.str.zfill(6)


def normalize_industry_code_series(s: pd.Series) -> pd.Series:
    x = s.astype(str).str.extract(r"(\d+)")[0]
    return x.str.zfill(6)




def _clean_text_series(s: pd.Series) -> pd.Series:
    return s.astype("string").replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA}).str.strip()


def _coalesce(left: pd.Series, right: pd.Series, *, as_string: bool = False) -> pd.Series:
    if as_string:
        l = _clean_text_series(left)
        r = _clean_text_series(right.reindex(left.index))
        return l.where(l.notna(), r)
    l = left.copy()
    r = right.reindex(left.index)
    return l.where(~l.isna(), r)

def resolve_reference_path(reference_csv: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if reference_csv is not None:
        candidates.append(Path(reference_csv))
    candidates.extend(DEFAULT_REFERENCE_CANDIDATES)
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Industry reference CSV not found. expected one of: "
        + " / ".join(str(p) for p in candidates)
    )


def _pick_first_existing(cols: Iterable[str], candidates: list[str]) -> str | None:
    cols = list(cols)
    for c in candidates:
        if c in cols:
            return c
    return None


def standardize_industry_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "ticker" in out.columns:
        out["ticker"] = normalize_ticker_series(out["ticker"])
    code_col = _pick_first_existing(
        out.columns,
        ["industry_code", "업종코드", "industry6", "industry6_code", "industry_code6", "industry4", "induty_code"],
    )
    if code_col is not None:
        out["industry_code"] = normalize_industry_code_series(out[code_col]).astype("string")
        bad = out["industry_code"].notna() & ~out["industry_code"].astype(str).str.len().eq(6)
        if bad.any():
            out.loc[bad, "industry_code"] = pd.NA
    elif "industry_code" not in out.columns:
        out["industry_code"] = pd.NA
    name_col = _pick_first_existing(out.columns, ["industry_name", "업종명", "industry_nm", "krx_industry_name", "sector_name"])
    if name_col is not None:
        out["industry_name"] = out[name_col].astype("string").str.strip()
    elif "industry_name" not in out.columns:
        out["industry_name"] = pd.NA
    out["industry4"] = out["industry_code"]
    return out


def load_industry_reference(reference_csv: str | Path | None = None) -> pd.DataFrame:
    p = resolve_reference_path(reference_csv)
    df = pd.read_csv(p, dtype={"ticker": str, "industry_code": str}, keep_default_na=True)
    df = standardize_industry_columns(df)
    need = {"ticker", "industry_code", "industry_name"}
    if not need.issubset(df.columns):
        raise ValueError(f"Industry reference missing required columns: {need - set(df.columns)} @ {p}")
    out = df[["ticker", "industry_code", "industry_name"]].copy()
    out["ticker"] = normalize_ticker_series(out["ticker"])
    out["industry_code"] = normalize_industry_code_series(out["industry_code"]).astype("string")
    out["industry_name"] = out["industry_name"].astype("string").str.strip()
    out = out.dropna(subset=["ticker", "industry_code"]).drop_duplicates("ticker", keep="last")
    out.loc[out["industry_name"].isin(["", "nan", "None", "<NA>"]), "industry_name"] = pd.NA
    out["industry4"] = out["industry_code"]
    return out.reset_index(drop=True)


def load_industry_code_name_map(reference_csv: str | Path | None = None) -> pd.DataFrame:
    ref = load_industry_reference(reference_csv)
    out = ref[["industry_code", "industry_name"]].dropna(subset=["industry_code"]).drop_duplicates("industry_code", keep="last").copy()
    return out.reset_index(drop=True)


def attach_industry(df: pd.DataFrame, *, reference_csv: str | Path | None = None, how: str = "left", prefer_reference: bool = True) -> pd.DataFrame:
    if "ticker" not in df.columns:
        raise ValueError("ticker column missing")
    ref = load_industry_reference(reference_csv)
    out = df.copy()
    out["ticker"] = normalize_ticker_series(out["ticker"])
    out = standardize_industry_columns(out)
    merged = out.merge(ref, on="ticker", how=how, suffixes=("", "__ref"))
    if prefer_reference:
        merged["industry_code"] = _coalesce(merged["industry_code__ref"], merged["industry_code"], as_string=True).astype("string")
        merged["industry_name"] = _coalesce(merged["industry_name__ref"], merged["industry_name"], as_string=True).astype("string")
    else:
        merged["industry_code"] = _coalesce(merged["industry_code"], merged["industry_code__ref"], as_string=True).astype("string")
        merged["industry_name"] = _coalesce(merged["industry_name"], merged["industry_name__ref"], as_string=True).astype("string")
    merged["industry4"] = merged["industry_code"]
    return merged.drop(columns=["industry_code__ref", "industry_name__ref"], errors="ignore")


def validate_industry(df: pd.DataFrame, *, raise_on_missing: bool = False) -> pd.DataFrame:
    if "ticker" not in df.columns:
        raise ValueError("ticker column missing")
    x = standardize_industry_columns(df)
    bad = x[x["industry_code"].isna()].copy()
    if len(bad) > 0:
        msg = f"missing industry_code rows={len(bad)}"
        if raise_on_missing:
            raise ValueError(msg)
        print(f"[WARN] {msg}")
    return bad
