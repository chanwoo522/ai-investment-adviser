import pandas as pd
from pathlib import Path
import argparse
import re


REPRT_TO_QUARTER = {
    "11013": 1,  # 1Q
    "11012": 2,  # 반기
    "11014": 3,  # 3Q
    "11011": 4,  # 사업보고서
}


def parse_meta_from_filename(fp: Path):
    name = fp.name
    out = {}
    for key in ["corp", "year", "reprt", "fs", "asof"]:
        m = re.search(rf"{key}=([^_\.]+)", name)
        if m:
            out[key] = m.group(1)
    return out


def pick_col(cols, candidates):
    for c in candidates:
        if c in cols:
            return c
    return None


def normalize_account_name(x: str) -> str | None:
    s = str(x).strip().replace(" ", "")

    # revenue
    if ("매출액" in s) or ("영업수익" in s) or (s == "매출"):
        return "revenue"

    # operating income
    if "영업이익" in s:
        return "op_income"

    # net income
    if "당기순이익" in s:
        return "net_income"

    # balance sheet
    if "자산총계" in s:
        return "assets"
    if "자본총계" in s:
        return "equity"
    if "부채총계" in s:
        return "liabilities"

    # cash flow
    if ("영업활동" in s and "현금흐름" in s) or ("영업활동으로창출된현금" in s):
        return "cfo"

    # capex proxy
    if ("유형자산" in s and "취득" in s):
        return "capex"

    return None


def normalize_one_file(fp: Path):
    meta = parse_meta_from_filename(fp)

    try:
        df = pd.read_parquet(fp)
    except Exception as e:
        print(f"[WARN] failed to read {fp.name}: {e}")
        return None

    if df.empty:
        return None

    cols = df.columns.tolist()

    account_col = pick_col(cols, ["account_nm", "account_nm_x", "account"])
    corp_col = pick_col(cols, ["corp_code", "corp"])
    year_col = pick_col(cols, ["bsns_year", "year"])
    reprt_col = pick_col(cols, ["reprt_code", "reprt"])

    value_col = pick_col(
        cols,
        [
            "thstrm_amount",
            "frmtrm_amount",
            "amount",
            "value",
            "thstrm_add_amount",
        ],
    )

    if account_col is None or value_col is None:
        return None

    out = pd.DataFrame()
    out["account"] = df[account_col].astype(str).str.strip()

    if corp_col is not None:
        out["corp_code"] = df[corp_col].astype(str)
    else:
        out["corp_code"] = meta.get("corp")

    if year_col is not None:
        out["year"] = df[year_col].astype(str)
    else:
        out["year"] = meta.get("year")

    if reprt_col is not None:
        out["reprt_code"] = df[reprt_col].astype(str)
    else:
        out["reprt_code"] = meta.get("reprt")

    out["fs"] = meta.get("fs")
    out["asof"] = meta.get("asof")
    out["source_file"] = fp.name

    val = (
        df[value_col]
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.replace(" ", "", regex=False)
        .replace({"": None, "nan": None, "None": None, "-": None})
    )
    out["value"] = pd.to_numeric(val, errors="coerce")

    out["account_std"] = out["account"].map(normalize_account_name)
    out = out[out["account_std"].notnull()].copy()

    # 값 없는 행 제거
    out = out[out["value"].notnull()].copy()

    if out.empty:
        return None

    return out


def build_panel(df_long: pd.DataFrame):
    panel = (
        df_long.pivot_table(
            index=["corp_code", "year", "reprt_code", "fs", "asof"],
            columns="account_std",
            values="value",
            aggfunc="first",
        )
        .reset_index()
    )

    panel["quarter"] = panel["reprt_code"].map(REPRT_TO_QUARTER)
    panel["quarter_key"] = panel["year"].astype(str) + "Q" + panel["quarter"].astype("Int64").astype(str)

    ordered_cols = [
        "corp_code",
        "year",
        "quarter",
        "quarter_key",
        "reprt_code",
        "fs",
        "revenue",
        "op_income",
        "net_income",
        "assets",
        "equity",
        "liabilities",
        "cfo",
        "capex",
        "asof",
    ]

    existing = [c for c in ordered_cols if c in panel.columns]
    rest = [c for c in panel.columns if c not in existing]
    panel = panel[existing + rest]

    return panel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", required=True)
    parser.add_argument("--out_v", type=int, default=1)
    args = parser.parse_args()

    raw_dir = Path("data/raw/dart")
    files = list(raw_dir.rglob("*.parquet"))

    print(f"[INFO] total parquet files: {len(files)}")

    all_rows = []
    n_ok = 0

    for i, fp in enumerate(files, start=1):
        one = normalize_one_file(fp)
        if one is not None and not one.empty:
            all_rows.append(one)
            n_ok += 1

        if i % 2000 == 0 or i == len(files):
            print(f"[PROGRESS] {i}/{len(files)} files scanned, usable={n_ok}")

    if not all_rows:
        raise ValueError("No valid parquet data parsed from data/raw/dart")

    df_long = pd.concat(all_rows, ignore_index=True)

    print("[INFO] long rows:", len(df_long))
    print("[INFO] mapped accounts:")
    print(df_long["account_std"].value_counts(dropna=False).to_string())

    panel = build_panel(df_long)

    out_dir = Path("data/intermediate/fundamentals_panel")
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / f"fundamentals_panel__asof={args.asof}__src=dart_raw__v={args.out_v}.parquet"
    panel.to_parquet(out_path, index=False)

    print(f"[OK] saved: {out_path}")
    print(f"[INFO] rows: {len(panel)}")
    print(f"[INFO] corp_codes: {panel['corp_code'].nunique(dropna=True)}")
    if len(panel) > 0:
        print(f"[INFO] years: {panel['year'].min()} -> {panel['year'].max()}")
        print(f"[INFO] quarters: {sorted(panel['quarter'].dropna().unique().tolist())}")


if __name__ == "__main__":
    main()