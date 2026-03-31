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
import math
from pathlib import Path
import yaml
import pandas as pd
from common.industry_map import attach_industry


def normalize_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def pick_first_existing(cols: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def load_yaml(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_prices_from_csv(prices_csv: str) -> pd.DataFrame:
    p = Path(prices_csv)
    if not p.exists():
        raise FileNotFoundError(f"prices_csv not found: {p}")

    px = pd.read_csv(p)
    if "ticker" not in px.columns or "price" not in px.columns:
        raise ValueError("prices_csv must contain 'ticker' and 'price'")

    px["ticker"] = normalize_ticker(px["ticker"])
    px["price"] = pd.to_numeric(px["price"], errors="coerce")
    if "adv20_value" in px.columns:
        px["adv20_value"] = pd.to_numeric(px["adv20_value"], errors="coerce")
    else:
        px["adv20_value"] = pd.NA

    px = px.dropna(subset=["ticker", "price"]).copy()
    return px[["ticker", "price", "adv20_value"]].drop_duplicates("ticker", keep="last")


def load_prices_from_processed(asof: str, metric: str, ret_v: int, price_date: str | None) -> pd.DataFrame:
    p = Path(
        f"data/processed/prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v={ret_v}.parquet"
    )
    if not p.exists():
        candidates = sorted(
            Path("data/processed").glob(
                f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v=*.parquet"
            )
        )
        if not candidates:
            raise FileNotFoundError(f"processed prices parquet not found: {p}")

        def _extract_v(path: Path) -> int:
            name = path.stem
            token = name.split("__v=")[-1]
            try:
                return int(token)
            except Exception:
                return -1

        p = sorted(candidates, key=_extract_v)[-1]
        print(f"[WARN] requested ret_v={ret_v} not found; using latest available prices parquet: {p}")

    df = pd.read_parquet(p)
    cols = df.columns.tolist()

    ticker_col = pick_first_existing(cols, ["ticker"])
    date_col = pick_first_existing(cols, ["date", "Date", "dt", "ymd", "trd_date"])
    price_col = pick_first_existing(cols, ["close", "Close", "adj_close", "price"])
    volume_col = pick_first_existing(cols, ["Volume", "volume", "vol"])

    if ticker_col is None or date_col is None or price_col is None:
        raise ValueError(f"Could not detect ticker/date/price columns. columns={cols}")

    keep_cols = [ticker_col, date_col, price_col]
    if volume_col is not None:
        keep_cols.append(volume_col)

    px = df[keep_cols].copy()

    rename_map = {
        ticker_col: "ticker",
        date_col: "date",
        price_col: "price",
    }
    if volume_col is not None:
        rename_map[volume_col] = "volume"

    px = px.rename(columns=rename_map)
    px["ticker"] = normalize_ticker(px["ticker"])
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["price"] = pd.to_numeric(px["price"], errors="coerce")

    if "volume" in px.columns:
        px["volume"] = pd.to_numeric(px["volume"], errors="coerce")
        px["daily_traded_value"] = px["price"] * px["volume"]
    else:
        px["volume"] = pd.NA
        px["daily_traded_value"] = pd.NA

    px = px.dropna(subset=["ticker", "date", "price"]).copy()

    if price_date is not None:
        cutoff = pd.to_datetime(price_date)
        px = px[px["date"] <= cutoff].copy()

    px = px.sort_values(["ticker", "date"]).copy()

    # 최근 20거래일 평균 거래대금 proxy
    px["adv20_value"] = (
        px.groupby("ticker")["daily_traded_value"]
        .transform(lambda s: s.rolling(20, min_periods=1).mean())
    )

    # ticker별 마지막 row만 사용
    px = px.drop_duplicates("ticker", keep="last").copy()

    return px[["ticker", "price", "adv20_value", "date"]]


def calc_slippage_bps(order_value: float, adv20_value: float | None, base_bps: float, impact_coef: float) -> float:
    if adv20_value is None or pd.isna(adv20_value) or adv20_value <= 0:
        return base_bps
    return base_bps + impact_coef * (order_value / adv20_value)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions_csv", required=True)
    ap.add_argument("--total_capital", required=True, type=float)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out_v", required=True, type=int)

    ap.add_argument("--prices_csv", default=None)
    ap.add_argument("--asof", default=None)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--ret_v", type=int, default=2)
    ap.add_argument("--price_date", default=None)

    args = ap.parse_args()

    cfg = load_yaml(args.config)
    exec_cfg = cfg["execution"]

    split_ratio_buy = exec_cfg.get("split_ratio_buy", [0.4, 0.3, 0.3])
    split_ratio_sell = exec_cfg.get("split_ratio_sell", [0.7, 0.3, 0.0])
    if len(split_ratio_buy) != 3 or len(split_ratio_sell) != 3:
        raise ValueError(
            f"execution split ratios must each have length 3. got buy={split_ratio_buy}, sell={split_ratio_sell}"
        )
    max_adv_ratio = float(exec_cfg.get("max_adv_ratio", 0.15))
    base_bps = float(exec_cfg.get("slippage", {}).get("base_bps", 5))
    impact_coef = float(exec_cfg.get("slippage", {}).get("adv_impact_coef", 10))

    actions_path = Path(args.actions_csv)
    if not actions_path.exists():
        raise FileNotFoundError(f"actions_csv not found: {actions_path}")

    act = pd.read_csv(actions_path)
    if "ticker" not in act.columns:
        raise ValueError("actions_csv must contain 'ticker'")

    act["ticker"] = normalize_ticker(act["ticker"])

    if "shares" not in act.columns:
        act["shares"] = 0
    act["shares"] = pd.to_numeric(act["shares"], errors="coerce").fillna(0)

    if args.prices_csv:
        px = load_prices_from_csv(args.prices_csv)
        print(f"[INFO] loaded manual prices: {len(px)}")
    else:
        if args.asof is None:
            raise ValueError("When --prices_csv is omitted, --asof is required")
        px = load_prices_from_processed(
            asof=args.asof,
            metric=args.metric,
            ret_v=args.ret_v,
            price_date=args.price_date,
        )
        print(f"[INFO] loaded auto prices from processed parquet: {len(px)}")

    df = act.merge(px, on="ticker", how="left")
    try:
        df = attach_industry(df, prefer_reference=True)
    except Exception as e:
        print(f"[WARN] attach_industry failed: {e}")

    n_target = int((pd.to_numeric(df.get("is_target"), errors="coerce").fillna(0) == 1).sum())
    if n_target <= 0:
        raise ValueError("No target holdings found in actions file")

    target_amount_each = args.total_capital / n_target

    df["current_value"] = df["shares"] * df["price"]
    df["target_value"] = 0.0
    df.loc[pd.to_numeric(df["is_target"], errors="coerce").fillna(0) == 1, "target_value"] = target_amount_each
    df["trade_value"] = df["target_value"] - df["current_value"]

    if "adv20_value" not in df.columns:
        df["adv20_value"] = pd.NA
    df["max_day_value"] = pd.to_numeric(df["adv20_value"], errors="coerce") * max_adv_ratio

    def order_side_from_row(row):
        action = str(row.get("action", ""))
        v = row.get("trade_value")
        if action == "HOLD_REVIEW":
            return "REVIEW"
        if pd.isna(v):
            return "CHECK"
        if v > 0:
            return "BUY"
        if v < 0:
            return "SELL"
        return "HOLD"

    df["order_side"] = df.apply(order_side_from_row, axis=1)

    def pick_ratio_vec(trade_value: float, buy_vec: list[float], sell_vec: list[float]) -> list[float]:
        if pd.isna(trade_value):
            return [0.0, 0.0, 0.0]
        return buy_vec if trade_value > 0 else sell_vec if trade_value < 0 else [0.0, 0.0, 0.0]

    for i in range(1, 4):
        df[f"day{i}_qty"] = 0
        df[f"day{i}_order_value"] = 0.0

    for idx, row in df.iterrows():
        ratio_vec = pick_ratio_vec(row["trade_value"], split_ratio_buy, split_ratio_sell)
        price = row["price"]
        adv20 = row["adv20_value"]
        cap = row["max_day_value"]
        abs_trade_value = abs(row["trade_value"]) if pd.notna(row["trade_value"]) else 0.0

        for i, r in enumerate(ratio_vec, start=1):
            raw_value = abs_trade_value * float(r)

            if pd.notna(cap) and cap > 0:
                use_value = min(raw_value, cap)
            else:
                use_value = raw_value

            if pd.isna(price) or price <= 0 or use_value <= 0:
                qty = 0
            else:
                qty = math.floor(use_value / price)

            df.at[idx, f"day{i}_qty"] = qty
            df.at[idx, f"day{i}_order_value"] = qty * price if qty > 0 else 0.0

    day_qty_cols = [f"day{i}_qty" for i in range(1, len(split_ratio_buy) + 1)]
    day_val_cols = [f"day{i}_order_value" for i in range(1, len(split_ratio_buy) + 1)]

    df["planned_total_qty"] = df[day_qty_cols].sum(axis=1)
    df["planned_total_value"] = df[day_val_cols].sum(axis=1)
    df["unplanned_value"] = df["trade_value"].abs() - df["planned_total_value"]

    df["est_slippage_bps"] = df.apply(
        lambda row: calc_slippage_bps(
            order_value=row["planned_total_value"],
            adv20_value=row["adv20_value"],
            base_bps=base_bps,
            impact_coef=impact_coef,
        ),
        axis=1,
    )

    out_cols = [
        "ticker",
        "name_final",
        "action",
        "reason",
        "shares",
        "price",
        "adv20_value",
        "current_value",
        "target_value",
        "trade_value",
        "order_side",
        "max_day_value",
        "day1_qty",
        "day1_order_value",
        "day2_qty",
        "day2_order_value",
        "day3_qty",
        "day3_order_value",
        "planned_total_qty",
        "planned_total_value",
        "unplanned_value",
        "est_slippage_bps",
        "score",
        "score_rank",
        "industry_code",
        "industry_name",
        "industry4",
    ]
    out_cols = [c for c in out_cols if c in df.columns]
    out = df[out_cols].copy()

    order_priority = {"BUY": 0, "SELL": 1, "REVIEW": 2, "HOLD": 3, "CHECK": 9}
    out["_ord"] = out["order_side"].map(order_priority).fillna(9)
    if "score_rank" in out.columns:
        out = out.sort_values(["_ord", "score_rank"], ascending=[True, True], na_position="last")
    else:
        out = out.sort_values(["_ord", "ticker"], ascending=[True, True], na_position="last")
    out = out.drop(columns=["_ord"]).reset_index(drop=True)

    out_path = Path(f"data/processed/execution_plan__total={int(args.total_capital)}__v={args.out_v}.csv")
    out.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"[OK] saved: {out_path}")
    print("\n[ORDER SIDE COUNTS]")
    print(out["order_side"].value_counts(dropna=False).to_string())

    missing_price = out["price"].isnull().sum() if "price" in out.columns else len(out)
    print(f"\n[INFO] missing price rows: {missing_price}")

    print("\n[PREVIEW]")
    print(out.head(30).to_string(index=False))


if __name__ == "__main__":
    main()