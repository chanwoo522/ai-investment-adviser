from __future__ import annotations
import argparse, os
import pandas as pd
from tqdm import tqdm
import OpenDartReader

from _utils import PRO, parquet_path, save_parquet_atomic, load_parquet_if_exists, log_path, write_log, load_checkpoint, save_checkpoint, retryable

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)  # YYYY-MM-DD
    ap.add_argument("--start_year", type=int, default=2012)
    ap.add_argument("--freq", choices=["y"], default="y")
    args = ap.parse_args()
    asof = args.asof
    lp = log_path("collect_dart_financials", asof)

    api_key = os.environ.get("DART_API_KEY")
    if not api_key:
        raise RuntimeError("DART_API_KEY environment variable not set.")

    out = parquet_path("dart_financials", asof, "opendart", extra=f"start={args.start_year}__freq={args.freq}", root=PRO)
    if load_parquet_if_exists(out) is not None:
        write_log(f"[SKIP] exists: {out}", lp)
        return

    dart = OpenDartReader.OpenDartReader(api_key)

    # corp list 캐시(매번 받지 않게)
    corp_cache = parquet_path("dart_corp_list", asof, "opendart", root=PRO)
    corp_df = load_parquet_if_exists(corp_cache)
    if corp_df is None:
        write_log("[INFO] downloading corp list...", lp)
        corp_df = retryable(dart.corp_codes)  # dataframe
        save_parquet_atomic(corp_df, corp_cache)
        write_log(f"[OK] saved corp list: {corp_cache} rows={len(corp_df)}", lp)

    # 수집 대상: 상장사만(종목코드 있는 corp)
    corp_df = corp_df.copy()
    corp_df = corp_df[corp_df["stock_code"].notna() & (corp_df["stock_code"].astype(str).str.len() > 0)]
    corp_df["stock_code"] = corp_df["stock_code"].astype(str).str.zfill(6)

    ck = load_checkpoint("dart_financials", asof)
    done = set(ck.get("done", []))

    rows = []
    years = list(range(args.start_year, int(asof[:4]) + 1))

    # NOTE: OpenDartReader에서 지표(ROIC/FCF)까지 한 방에 주는 게 아니라,
    # 우선 연간 재무제표 raw를 모으고 processed 단계에서 지표를 계산하는 구조로 가자.
    # 여기서는 '재무상태표/손익계산서/현금흐름표'를 연도별로 받아 누적.
    for _, r in tqdm(corp_df.iterrows(), total=len(corp_df), desc="DART corp"):
        ticker = r["stock_code"]
        corp_code = r["corp_code"]
        if ticker in done:
            continue
        try:
            for y in years:
                # 연간 사업보고서(11011) 중심으로 수집 (필요시 반기/분기도 확장 가능)
                # fnlttSinglAcntAll: 단일회사 전체 재무제표
                df = retryable(dart.finstate_all, corp_code, y, reprt_code="11011")
                if df is None or len(df) == 0:
                    continue
                df = df.copy()
                df["ticker"] = ticker
                df["corp_code"] = corp_code
                df["year"] = y
                rows.append(df)
            done.add(ticker)
            ck["done"] = sorted(done)
            save_checkpoint("dart_financials", asof, ck)
        except Exception as e:
            write_log(f"[WARN] ticker={ticker} corp={corp_code} failed: {e}", lp)
            # 실패해도 진행(다음에 재시도 가능)
            continue

    if rows:
        all_df = pd.concat(rows, ignore_index=True)
    else:
        all_df = pd.DataFrame()

    save_parquet_atomic(all_df, out)
    write_log(f"[OK] saved: {out} rows={len(all_df)} corps_done={len(done)}", lp)

if __name__ == "__main__":
    main()
