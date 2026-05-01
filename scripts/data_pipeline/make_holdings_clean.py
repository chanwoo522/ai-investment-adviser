from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Iterable

import pandas as pd


NAME_CANDIDATES = ["종목명", "종목 명", "주식명", "종목"]
SHARES_CANDIDATES = ["보유잔고", "보유수량", "수량", "잔고수량", "잔고"]

# 마스터 파일 컬럼 후보
MASTER_NAME_CANDIDATES = ["종목명", "주식종목명", "name", "name_final"]
MASTER_TICKER_CANDIDATES = ["종목코드", "ticker", "단축코드", "티커", "code"]


def _norm_text(x: object) -> str:
    if pd.isna(x):
        return ""
    s = str(x)
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", "", s)
    return s.strip()


def _normalize_name_key(x: object) -> str:
    if pd.isna(x):
        return ""
    s = str(x).strip()
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", "", s)
    return s.upper()


def _normalize_ticker_value(x: object) -> str | None:
    if pd.isna(x):
        return None
    s = re.sub(r"\D", "", str(x))
    if not s:
        return None
    return s.zfill(6)


def _find_first_matching_col(columns: Iterable[object], candidates: list[str]) -> str | None:
    normalized = {str(c): _norm_text(c) for c in columns}
    cand_norms = [_norm_text(c) for c in candidates]
    for orig, norm in normalized.items():
        if any(c == norm or c in norm for c in cand_norms):
            return orig
    return None


def _detect_header_start(raw: pd.DataFrame) -> int:
    max_rows = min(len(raw), 60)
    name_cands = [_norm_text(x) for x in NAME_CANDIDATES]
    shares_cands = [_norm_text(x) for x in SHARES_CANDIDATES]

    for i in range(max_rows):
        row = [_norm_text(v) for v in raw.iloc[i].tolist()]
        rowset = set(v for v in row if v)

        has_name = any(any(c in v for c in name_cands) for v in rowset)
        has_shares = any(any(c in v for c in shares_cands) for v in rowset)

        if has_name and has_shares:
            return i

    raise ValueError("보유내역 헤더 시작점을 찾지 못했습니다. 원본 파일 형식을 확인해 주세요.")


def _build_headers(h1: list[object], h2: list[object]) -> list[str]:
    headers: list[str] = []
    for a, b in zip(h1, h2):
        a2 = _norm_text(a)
        b2 = _norm_text(b)
        if a2 and b2:
            headers.append(f"{a2}_{b2}")
        elif a2:
            headers.append(a2)
        elif b2:
            headers.append(b2)
        else:
            headers.append("")
    return headers


def _load_raw_table(input_path: Path, sheet_name: str | int | None = None) -> pd.DataFrame:
    suffix = input_path.suffix.lower()
    if suffix in {".xls", ".xlsx", ".xlsm"}:
        kwargs = {"header": None}
        if sheet_name is not None:
            kwargs["sheet_name"] = sheet_name
        return pd.read_excel(input_path, **kwargs)
    return pd.read_csv(input_path, header=None, encoding="utf-8-sig")


def _coerce_int_series(s: pd.Series) -> pd.Series:
    s = (
        s.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace("\u3000", "", regex=False)
    )
    s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    return pd.to_numeric(s, errors="coerce").round().astype("Int64")


def _extract_holdings(raw: pd.DataFrame) -> pd.DataFrame:
    start_idx = _detect_header_start(raw)

    if start_idx + 1 >= len(raw):
        raise ValueError("헤더 2행 구조를 읽을 수 없습니다.")

    header1 = raw.iloc[start_idx].tolist()
    header2 = raw.iloc[start_idx + 1].tolist()
    headers = _build_headers(header1, header2)

    data = raw.iloc[start_idx + 2 :].copy().reset_index(drop=True)
    data.columns = headers

    name_col = _find_first_matching_col(data.columns, NAME_CANDIDATES)
    shares_col = _find_first_matching_col(data.columns, SHARES_CANDIDATES)

    if name_col is None:
        raise ValueError(f"종목명 컬럼을 찾지 못했습니다. columns={list(data.columns)}")
    if shares_col is None:
        raise ValueError(f"보유수량 컬럼을 찾지 못했습니다. columns={list(data.columns)}")

    extracted = data[[name_col, shares_col]].copy()
    extracted.columns = ["name", "shares"]

    extracted["name"] = extracted["name"].map(lambda x: str(x).strip() if not pd.isna(x) else "")
    extracted["shares"] = _coerce_int_series(extracted["shares"])

    # 빈값 제거
    extracted = extracted[(extracted["name"] != "") & extracted["shares"].notna()].copy()

    # 숫자만 있는 행 제거
    extracted = extracted[
        ~extracted["name"].str.fullmatch(r"\d+(?:\.\d+)?", na=False)
    ].copy()

    # 보유수량 0 초과만 유지
    extracted = extracted[extracted["shares"] > 0].copy()

    # 동일 종목이 여러 번 있으면 합산
    extracted = extracted.groupby("name", as_index=False)["shares"].sum()

    return extracted


def _load_master(master_path: Path, sheet_name: str | int | None = None) -> pd.DataFrame:
    if not master_path.exists():
        raise FileNotFoundError(f"master not found: {master_path}")

    suffix = master_path.suffix.lower()
    if suffix == ".parquet":
        m = pd.read_parquet(master_path)
    elif suffix in {".xls", ".xlsx", ".xlsm"}:
        kwargs = {}
        if sheet_name is not None:
            kwargs["sheet_name"] = sheet_name
        m = pd.read_excel(master_path, **kwargs)
    else:
        m = pd.read_csv(master_path, encoding="utf-8-sig")

    print(f"[INFO] master columns: {list(m.columns)}")

    name_col = _find_first_matching_col(m.columns, MASTER_NAME_CANDIDATES)
    ticker_col = _find_first_matching_col(m.columns, MASTER_TICKER_CANDIDATES)

    print(f"[INFO] selected master name col: {name_col}")
    print(f"[INFO] selected master ticker col: {ticker_col}")

    if name_col is None:
        raise ValueError(f"master에서 종목명 컬럼을 찾지 못했습니다. columns={list(m.columns)}")
    if ticker_col is None:
        raise ValueError(f"master에서 ticker 컬럼을 찾지 못했습니다. columns={list(m.columns)}")

    out = m[[name_col, ticker_col]].copy()
    out.columns = ["name", "ticker"]

    out["name"] = out["name"].astype(str).str.strip()
    out["ticker"] = out["ticker"].map(_normalize_ticker_value)
    out["name_key"] = out["name"].map(_normalize_name_key)

    out = out.dropna(subset=["ticker"])
    out = out[out["name"] != ""].copy()
    out = out[out["name_key"] != ""].copy()
    out = out.drop_duplicates(subset=["name_key"], keep="first").reset_index(drop=True)

    return out


def _auto_find_master(asof: str | None) -> Path:
    # 기본 fallback. 가능하면 --master를 직접 주는 것을 권장.
    candidates: list[Path] = []
    p1 = Path("reference")
    p2 = Path("data/processed")

    if asof:
        candidates.extend(sorted(p1.glob(f"*{asof.replace('-', '')}*.xlsx"), reverse=True))
        candidates.extend(sorted(p1.glob(f"*{asof}*.xlsx"), reverse=True))
        candidates.extend(sorted(p2.glob(f"krx_master__asof={asof}__src=*__v=*.parquet"), reverse=True))
        candidates.extend(sorted(p2.glob(f"krx_master__asof={asof}__src=*__v=*.csv"), reverse=True))

    candidates.extend(sorted(p1.glob("*.xlsx"), reverse=True))
    candidates.extend(sorted(p2.glob("krx_master__asof=*__src=*__v=*.parquet"), reverse=True))
    candidates.extend(sorted(p2.glob("krx_master__asof=*__src=*__v=*.csv"), reverse=True))

    if not candidates:
        raise FileNotFoundError("reference 또는 data/processed 아래에서 master 파일을 찾지 못했습니다.")
    return candidates[0]


def main() -> None:
    ap = argparse.ArgumentParser(description="증권사 원본 holdings 파일(xls/xlsx/csv)을 actions용 clean csv로 변환")
    ap.add_argument("--input", required=True, help="원본 holdings 파일 경로 (.xls/.xlsx/.csv)")
    ap.add_argument("--output", required=True, help="출력 clean csv 경로")
    ap.add_argument("--master", default=None, help="종목명-티커 매핑 master 파일 경로 (.xlsx/.csv/.parquet)")
    ap.add_argument("--asof", default=None, help="master 자동 탐색 시 우선 사용할 asof")
    ap.add_argument("--sheet_name", default=None, help="input 엑셀 시트명 또는 시트 index")
    ap.add_argument("--master_sheet_name", default=None, help="master 엑셀 시트명 또는 시트 index")
    ap.add_argument("--allow_missing_ticker", action="store_true", help="ticker 누락 종목이 있어도 계속 진행")
    args = ap.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(f"input holdings file not found: {input_path}")

    if args.sheet_name is None:
        sheet_name: str | int | None = None
    else:
        try:
            sheet_name = int(args.sheet_name)
        except ValueError:
            sheet_name = args.sheet_name

    if args.master_sheet_name is None:
        master_sheet_name: str | int | None = None
    else:
        try:
            master_sheet_name = int(args.master_sheet_name)
        except ValueError:
            master_sheet_name = args.master_sheet_name

    master_path = Path(args.master) if args.master else _auto_find_master(args.asof)

    print(f"[INFO] input : {input_path}")
    print(f"[INFO] master: {master_path}")

    raw = _load_raw_table(input_path, sheet_name=sheet_name)
    holdings = _extract_holdings(raw)
    master = _load_master(master_path, sheet_name=master_sheet_name)

    holdings["name_key"] = holdings["name"].map(_normalize_name_key)

    out = holdings.merge(
        master[["name_key", "ticker"]],
        on="name_key",
        how="left",
    ).drop(columns=["name_key"])

    missing = out["ticker"].isna().sum()
    print(f"[INFO] parsed holdings rows: {len(out)}")
    print(f"[INFO] missing ticker rows : {int(missing)}")

    if missing > 0:
        print("[WARN] ticker 매핑 실패 종목:")
        print(out.loc[out["ticker"].isna(), ["name", "shares"]].to_string(index=False))
        if not args.allow_missing_ticker:
            raise ValueError("ticker 매핑 실패 종목이 있습니다. --allow_missing_ticker 없이 계속할 수 없습니다.")

    out = out[["ticker", "name", "shares"]].copy()
    out["shares"] = pd.to_numeric(out["shares"], errors="coerce").round().astype("Int64")
    out = out.dropna(subset=["shares"])
    out = out.sort_values(["ticker", "name"], na_position="last").reset_index(drop=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"[OK] saved: {output_path}")
    print("\n[PREVIEW]")
    print(out.head(30).to_string(index=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        raise
