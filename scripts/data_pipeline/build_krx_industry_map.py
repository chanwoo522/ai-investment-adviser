import pandas as pd
from pykrx import stock

# 기준일
DATE = "20260329"

# 1. 전체 종목
tickers = stock.get_market_ticker_list(market="ALL")

rows = []

for t in tickers:
    try:
        name = stock.get_market_ticker_name(t)
        sector = stock.get_market_ticker_industry(t)  # 업종명
        rows.append((t, name, sector))
    except:
        pass

df = pd.DataFrame(rows, columns=["ticker","name","industry_name"])

print("[INFO] pykrx industry collected:", len(df))

# 2. 업종명 → 6자리 코드 매핑 (여기 핵심)
# 👉 이건 직접 매핑 테이블 만들어야 함
map_df = pd.read_csv("data/reference/krx_industry_code_map.csv")

out = df.merge(map_df, on="industry_name", how="left")

out = out[["ticker","industry_code","industry_name"]]

out.to_csv("data/reference/krx_industry_code_name_by_ticker.csv", index=False, encoding="utf-8-sig")

print("[OK] saved krx_industry_code_name_by_ticker.csv")