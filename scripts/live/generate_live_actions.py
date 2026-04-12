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
import pandas as pd
from common.industry_map import attach_industry, standardize_industry_columns


ACTION_ORDER = {"BUY": 0, "HOLD": 1, "HOLD_REVIEW": 2, "SELL": 3}


def resolve_scores_path(kind: str, asof: str, metric: str, strategy: str, feat_v: int, target_date: str) -> Path:
    candidates = [
        Path(
            f"data/live/scores/latest_scores__asof={asof}__metric={metric}"
            f"__strat={strategy}__featv={feat_v}__target={target_date}__{kind}.csv"
        ),
        Path(
            f"data/processed/latest_scores__asof={asof}__metric={metric}"
            f"__strat={strategy}__featv={feat_v}__target={target_date}__{kind}.csv"
        ),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"Could not find latest_scores file for kind={kind}. tried: " + " / ".join(str(p) for p in candidates)
    )


def normalize_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)

def ensure_industry_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    try:
        out = attach_industry(out, prefer_reference=True)
    except TypeError:
        out = attach_industry(out)
    except Exception:
        pass
    out = standardize_industry_columns(out)
    return out

def pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def compute_rank_from_score(df: pd.DataFrame, score_col: str = "score") -> pd.Series:
    score = pd.to_numeric(df[score_col], errors="coerce")
    return score.rank(method="min", ascending=False)


def _ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "score" not in out.columns:
        out["score"] = pd.NA
    out["score"] = pd.to_numeric(out["score"], errors="coerce")

    if "score_rank" not in out.columns:
        out["score_rank"] = compute_rank_from_score(out)
    out["score_rank"] = pd.to_numeric(out["score_rank"], errors="coerce")

    if "name" not in out.columns:
        out["name"] = pd.NA

    out = ensure_industry_columns(out)
    return out


def load_universe_scores(asof: str, metric: str, strategy: str, feat_v: int, target_date: str) -> pd.DataFrame:
    full_path = resolve_scores_path("full_universe", asof, metric, strategy, feat_v, target_date)
    topk_path = None
    try:
        topk_path = resolve_scores_path("topk", asof, metric, strategy, feat_v, target_date)
    except FileNotFoundError:
        topk_path = None

    if full_path.exists():
        df = pd.read_csv(full_path)
    elif topk_path is not None and topk_path.exists():
        df = pd.read_csv(topk_path)
    else:
        raise FileNotFoundError(
            f"Neither full_universe nor top-k file found. expected one of: {full_path} / {topk_path}"
        )

    if "ticker" not in df.columns:
        raise ValueError("score file must contain 'ticker'")

    df["ticker"] = normalize_ticker(df["ticker"])
    df = _ensure_score_columns(df)

    keep = [c for c in ["ticker", "name", "score", "score_adj", "hold_bonus_applied", "score_rank", "score_adj_rank", "industry_code", "industry_name"] if c in df.columns]
    uni = df[keep].copy()
    uni = uni.dropna(subset=["ticker"]).drop_duplicates("ticker", keep="first")
    uni = uni.rename(
        columns={
            "name": "name_universe",
            "score": "universe_score",
            "score_adj": "universe_score_adj",
            "hold_bonus_applied": "universe_hold_bonus_applied",
            "score_rank": "universe_score_rank",
            "score_adj_rank": "universe_score_adj_rank",
            "industry_code": "universe_industry_code",
            "industry_name": "universe_industry_name",
        }
    )
    return uni


def load_topk(asof: str, metric: str, strategy: str, feat_v: int, target_date: str) -> pd.DataFrame:
    topk_path = resolve_scores_path("topk", asof, metric, strategy, feat_v, target_date)

    topk = pd.read_csv(topk_path)
    if "ticker" not in topk.columns:
        raise ValueError("topk.csv must contain 'ticker'")

    topk["ticker"] = normalize_ticker(topk["ticker"])
    topk = _ensure_score_columns(topk)
    keep = [c for c in ["ticker", "name", "score", "score_adj", "hold_bonus_applied", "score_rank", "score_adj_rank", "industry_code", "industry_name", "industry4", "selection_bucket", "kept_from_previous"] if c in topk.columns]
    topk = topk[keep].copy()
    topk = topk.dropna(subset=["ticker"]).drop_duplicates("ticker", keep="first")
    topk["target_flag"] = 1
    topk = topk.rename(
        columns={
            "name": "name_target",
            "score": "target_score",
            "score_adj": "target_score_adj",
            "hold_bonus_applied": "target_hold_bonus_applied",
            "score_rank": "target_score_rank",
            "score_adj_rank": "target_score_adj_rank",
            "industry_code": "target_industry_code",
            "industry_name": "target_industry_name",
            "selection_bucket": "target_selection_bucket",
            "kept_from_previous": "target_kept_from_previous",
        }
    )
    return topk


def load_current_holdings_scored(asof: str, metric: str, strategy: str, feat_v: int, target_date: str) -> pd.DataFrame:
    try:
        p = resolve_scores_path("current_holdings_scored", asof, metric, strategy, feat_v, target_date)
    except FileNotFoundError:
        return pd.DataFrame(columns=[
            "ticker",
            "cohort_status",
            "score_availability_reason",
            "latest_available_rebalance_month",
            "latest_available_year",
            "latest_available_quarter",
            "filter_status_diag",
            "passed_filters_diag",
            "selected_topk_diag",
            "in_target_topk_diag",
            "current_price_diag",
            "price_date_diag",
            "current_value_diag",
            "current_weight_diag",
        ])

    cur = pd.read_csv(p)
    if "ticker" not in cur.columns:
        return pd.DataFrame(columns=["ticker"])

    cur["ticker"] = normalize_ticker(cur["ticker"])
    keep = [c for c in [
        "ticker",
        "cohort_status",
        "score_availability_reason",
        "latest_available_rebalance_month",
        "latest_available_year",
        "latest_available_quarter",
        "filter_status",
        "passed_filters",
        "selected_topk",
        "in_target_topk",
        "current_price",
        "price_date",
        "current_value",
        "current_weight",
    ] if c in cur.columns]
    cur = cur[keep].drop_duplicates("ticker", keep="last").copy()
    cur = cur.rename(columns={
        "filter_status": "filter_status_diag",
        "passed_filters": "passed_filters_diag",
        "selected_topk": "selected_topk_diag",
        "in_target_topk": "in_target_topk_diag",
        "current_price": "current_price_diag",
        "price_date": "price_date_diag",
        "current_value": "current_value_diag",
        "current_weight": "current_weight_diag",
    })
    return cur


def load_holdings(holdings_csv: str | Path) -> pd.DataFrame:
    holdings_path = Path(holdings_csv)
    if not holdings_path.exists():
        raise FileNotFoundError(f"Holdings file not found: {holdings_path}")

    holdings = pd.read_csv(holdings_path)
    if "ticker" not in holdings.columns:
        raise ValueError("holdings_csv must contain 'ticker'")

    holdings["ticker"] = normalize_ticker(holdings["ticker"])

    hold_name_col = pick_col(holdings, ["name", "종목명"])
    hold_shares_col = pick_col(holdings, ["current_shares", "shares", "qty", "quantity", "보유수량", "수량"])
    hold_weight_col = pick_col(holdings, ["weight", "target_weight", "비중"])

    if hold_name_col is not None and hold_name_col != "name_hold":
        holdings = holdings.rename(columns={hold_name_col: "name_hold"})
    elif hold_name_col is None:
        holdings["name_hold"] = pd.NA

    if hold_shares_col is not None and hold_shares_col != "current_shares":
        holdings = holdings.rename(columns={hold_shares_col: "current_shares"})
    elif hold_shares_col is None:
        holdings["current_shares"] = pd.NA

    if hold_weight_col is not None and hold_weight_col != "current_weight":
        holdings = holdings.rename(columns={hold_weight_col: "current_weight"})
    elif hold_weight_col is None:
        holdings["current_weight"] = pd.NA

    holdings["current_shares"] = pd.to_numeric(holdings["current_shares"], errors="coerce")
    holdings["current_weight"] = pd.to_numeric(holdings["current_weight"], errors="coerce")
    holdings["current_flag"] = 1

    keep = [c for c in ["ticker", "name_hold", "current_shares", "current_weight", "current_flag"] if c in holdings.columns]
    holdings = holdings[keep].copy()
    holdings = holdings.dropna(subset=["ticker"]).drop_duplicates("ticker", keep="first")
    return holdings


def _is_unscored_current(row) -> bool:
    reason = str(row.get("score_availability_reason") or "").strip()
    cohort = str(row.get("cohort_status") or "").strip()
    score = pd.to_numeric(pd.Series([row.get("score")]), errors="coerce").iloc[0]

    if reason and reason != "scored_in_target_cohort":
        return True
    if cohort and cohort != "in_target_cohort":
        return True
    if pd.isna(score):
        return True
    return False


def _latest_available_label(row) -> str | None:
    m = row.get("latest_available_rebalance_month")
    y = row.get("latest_available_year")
    q = row.get("latest_available_quarter")

    if pd.notna(m) and str(m).strip():
        return str(m)
    if pd.notna(y) and pd.notna(q):
        try:
            return f"{int(y)}Q{int(q)}"
        except Exception:
            return f"{y}Q{q}"
    return None


def build_action_union(
    holdings: pd.DataFrame,
    uni: pd.DataFrame,
    topk: pd.DataFrame,
    curdiag: pd.DataFrame | None = None,
    protect_current_unscored: bool = True,
) -> pd.DataFrame:
    action_tickers = pd.Index(pd.concat([holdings["ticker"], topk["ticker"]], ignore_index=True).dropna().unique())

    merged = pd.DataFrame({"ticker": action_tickers})
    merged = merged.merge(holdings, on="ticker", how="left")
    merged = merged.merge(uni, on="ticker", how="left")
    merged = merged.merge(topk, on="ticker", how="left")
    if curdiag is not None and len(curdiag) > 0:
        merged = merged.merge(curdiag, on="ticker", how="left")

    merged["is_current"] = merged.get("current_flag", pd.Series(index=merged.index, dtype="float64")).notna().astype(int)
    merged["is_target"] = merged.get("target_flag", pd.Series(index=merged.index, dtype="float64")).notna().astype(int)

    merged["name_final"] = pd.NA
    for c in ["name_hold", "name_target", "name_universe"]:
        if c in merged.columns:
            merged["name_final"] = merged["name_final"].fillna(merged[c])

    merged["score"] = merged.get("target_score", pd.Series(index=merged.index, dtype="float64"))
    if "universe_score" in merged.columns:
        merged["score"] = merged["score"].fillna(merged["universe_score"])

    merged["score_rank"] = merged.get("target_score_rank", pd.Series(index=merged.index, dtype="float64"))
    if "universe_score_rank" in merged.columns:
        merged["score_rank"] = merged["score_rank"].fillna(merged["universe_score_rank"])

    merged["industry_code"] = merged.get("target_industry_code", pd.Series(index=merged.index, dtype="object"))
    if "universe_industry_code" in merged.columns:
        merged["industry_code"] = merged["industry_code"].fillna(merged["universe_industry_code"])
    merged["industry_name"] = merged.get("target_industry_name", pd.Series(index=merged.index, dtype="object"))
    if "universe_industry_name" in merged.columns:
        merged["industry_name"] = merged["industry_name"].fillna(merged["universe_industry_name"])
    merged["industry4"] = merged["industry_code"]

    merged["current_shares"] = pd.to_numeric(merged.get("current_shares"), errors="coerce")
    merged["current_weight"] = pd.to_numeric(merged.get("current_weight"), errors="coerce")
    if "current_weight_diag" in merged.columns:
        merged["current_weight"] = merged["current_weight"].combine_first(
            pd.to_numeric(merged["current_weight_diag"], errors="coerce")
        )

    merged["score"] = pd.to_numeric(merged.get("score"), errors="coerce")
    merged["score_rank"] = pd.to_numeric(merged.get("score_rank"), errors="coerce")

    merged["shares"] = merged["current_shares"]
    merged["target_shares"] = pd.NA
    merged["target_value"] = pd.NA

    def decide(row) -> str:
        cur = bool(row["is_current"])
        tgt = bool(row["is_target"])

        if cur and tgt:
            return "HOLD"
        if (not cur) and tgt:
            return "BUY"
        if cur and (not tgt):
            if protect_current_unscored and _is_unscored_current(row):
                return "HOLD_REVIEW"
            return "SELL"
        return "CHECK"

    merged["action"] = merged.apply(decide, axis=1)

    def fmt_rank(v) -> str:
        return str(int(v)) if pd.notna(v) else "NA"

    def build_reason(row) -> str:
        action = row["action"]
        latest_lbl = _latest_available_label(row)
        why = str(row.get("score_availability_reason") or "").strip()

        bucket = str(row.get("target_selection_bucket") or "").strip()
        if action == "BUY":
            if bucket == "keep_current_top_n":
                return f"기존 보유 상위 점수 유지 규칙으로 유지 전환 (rank={fmt_rank(row['score_rank'])})"
            return f"전략 신규 편입 (rank={fmt_rank(row['score_rank'])})"

        if action == "HOLD":
            if bucket == "keep_current_top_n":
                return f"기존 보유 상위 점수 유지 (rank={fmt_rank(row['score_rank'])})"
            return f"전략 유지 (rank={fmt_rank(row['score_rank'])})"

        if action == "HOLD_REVIEW":
            base = "평가 보류 유지"
            if why:
                base += f" ({why})"
            if latest_lbl:
                base += f" / latest={latest_lbl}"
            return base

        if action == "SELL":
            if pd.notna(row.get("score_rank")):
                return f"전략 탈락 (universe_rank={fmt_rank(row['score_rank'])})"
            if why:
                return f"전략 탈락 ({why})"
            return "전략 탈락 (유니버스 점수 없음)"

        return "확인 필요"

    merged["reason"] = merged.apply(build_reason, axis=1)
    return merged


def build_candidates_watchlist(holdings: pd.DataFrame, uni: pd.DataFrame, topk: pd.DataFrame) -> pd.DataFrame:
    current_set = set(holdings["ticker"].astype(str).tolist())
    target_set = set(topk["ticker"].astype(str).tolist())

    cand = uni.copy()
    cand["is_current"] = cand["ticker"].isin(current_set).astype(int)
    cand["is_target"] = cand["ticker"].isin(target_set).astype(int)
    cand = cand[(cand["is_current"] == 0) & (cand["is_target"] == 0)].copy()

    if len(cand) == 0:
        return cand

    cand = cand.rename(
        columns={
            "name_universe": "name",
            "universe_score": "score",
            "universe_score_rank": "score_rank",
            "universe_industry_code": "industry_code",
            "universe_industry_name": "industry_name",
        }
    )
    cand["action"] = "CHECK"
    cand["reason"] = "후보군 참고용 (비보유/비선정)"
    keep = [c for c in ["ticker", "name", "score", "score_rank", "industry_code", "industry_name", "is_current", "is_target", "action", "reason"] if c in cand.columns]
    cand = cand[keep].copy()
    cand["score"] = pd.to_numeric(cand["score"], errors="coerce")
    cand["score_rank"] = pd.to_numeric(cand["score_rank"], errors="coerce")
    cand = cand.sort_values(["score_rank", "score"], ascending=[True, False], na_position="last").reset_index(drop=True)
    return cand

def enforce_min_keep_current(actions: pd.DataFrame, min_keep_current: int) -> pd.DataFrame:
    if min_keep_current <= 0:
        return actions

    out = actions.copy()

    # 현재 보유 종목만 대상
    cur = out[out["is_current"] == 1].copy()
    if len(cur) == 0:
        return out

    # 이미 유지되는 종목 수 계산
    keep_like = {"HOLD", "HOLD_REVIEW"}
    kept = cur[cur["action"].isin(keep_like)].copy()
    need = max(0, int(min_keep_current) - len(kept))
    if need == 0:
        return out

    # SELL 중에서 score 있는 종목 우선, score 높은 순으로 유지 전환
    sell_cur = cur[cur["action"] == "SELL"].copy()

    scored_sell = sell_cur[sell_cur["score"].notna()].copy()
    scored_sell = scored_sell.sort_values(
        ["score", "score_rank", "ticker"],
        ascending=[False, True, True]
    )

    promote = scored_sell.head(need).copy()
    promote_tickers = set(promote["ticker"].astype(str).tolist())

    if promote_tickers:
        out.loc[out["ticker"].astype(str).isin(promote_tickers), "action"] = "HOLD"
        out.loc[out["ticker"].astype(str).isin(promote_tickers), "reason"] = (
            "최소 보유 유지 규칙 적용 (min_keep_current)"
        )

    return out


def enforce_total_target_count(actions: pd.DataFrame, target_count: int) -> pd.DataFrame:
    if target_count <= 0:
        return actions

    out = actions.copy()
    keep_like_current = {"HOLD", "HOLD_REVIEW"}
    kept_current = out[(out["is_current"] == 1) & (out["action"].isin(keep_like_current))].copy()
    n_kept_current = len(kept_current)

    allowed_buys = max(0, int(target_count) - n_kept_current)
    buys = out[out["action"] == "BUY"].copy()
    if len(buys) <= allowed_buys:
        return out

    buys = buys.sort_values(["score_rank", "score", "ticker"], ascending=[True, False, True], na_position="last")
    demote = buys.iloc[allowed_buys:].copy()
    if len(demote) == 0:
        return out

    demote_tickers = set(demote["ticker"].astype(str).tolist())
    mask = out["ticker"].astype(str).isin(demote_tickers)
    old_reason = out.loc[mask, "reason"].fillna("").astype(str).str.strip()
    out.loc[mask, "action"] = "CHECK"
    out.loc[mask, "reason"] = old_reason.where(old_reason.ne(""), "전략 신규 편입") + " / 최소 보유 유지 규칙으로 편입 보류"
    return out


def build_candidates_watchlist_from_actions(holdings: pd.DataFrame, uni: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    current_set = set(holdings["ticker"].astype(str).tolist())
    active_set = set(actions.loc[actions["action"].isin(["BUY", "HOLD", "HOLD_REVIEW"]), "ticker"].astype(str).tolist())

    cand = uni.copy()
    cand["is_current"] = cand["ticker"].isin(current_set).astype(int)
    cand["is_selected_final"] = cand["ticker"].isin(active_set).astype(int)
    cand = cand[(cand["is_current"] == 0) & (cand["is_selected_final"] == 0)].copy()

    if len(cand) == 0:
        return cand

    cand = cand.rename(
        columns={
            "name_universe": "name",
            "universe_score": "score",
            "universe_score_rank": "score_rank",
            "universe_industry_code": "industry_code",
            "universe_industry_name": "industry_name",
        }
    )
    cand["action"] = "CHECK"
    cand["reason"] = "후보군 참고용 (비보유/최종 미선정)"
    keep = [c for c in ["ticker", "name", "score", "score_rank", "industry_code", "industry_name", "is_current", "is_selected_final", "action", "reason"] if c in cand.columns]
    cand = cand[keep].copy()
    cand["score"] = pd.to_numeric(cand["score"], errors="coerce")
    cand["score_rank"] = pd.to_numeric(cand["score_rank"], errors="coerce")
    cand = cand.sort_values(["score_rank", "score"], ascending=[True, False], na_position="last").reset_index(drop=True)
    return cand

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--feat_v", required=True, type=int)
    ap.add_argument("--target_date", required=True)
    ap.add_argument("--holdings_csv", required=True)
    ap.add_argument("--out_v", required=True, type=int)
    ap.add_argument("--save_candidates", action="store_true")
    ap.add_argument(
        "--sell_unscored_current",
        action="store_true",
        help="Disable protective HOLD_REVIEW for current holdings with unavailable latest score/cohort.",
    )
    ap.add_argument(
    "--min_keep_current",
    type=int,
    default=0,
    help="Minimum number of currently held positions to keep as HOLD/HOLD_REVIEW before issuing SELL."
    )
    args = ap.parse_args()

    holdings = load_holdings(args.holdings_csv)
    uni = load_universe_scores(args.asof, args.metric, args.strategy, args.feat_v, args.target_date)
    topk = load_topk(args.asof, args.metric, args.strategy, args.feat_v, args.target_date)
    curdiag = load_current_holdings_scored(args.asof, args.metric, args.strategy, args.feat_v, args.target_date)

    merged = build_action_union(
        holdings,
        uni,
        topk,
        curdiag=curdiag,
        protect_current_unscored=(not args.sell_unscored_current),
    )

    if (merged["action"] == "CHECK").any():
        bad = merged.loc[
            merged["action"] == "CHECK",
            [c for c in ["ticker", "name_final", "is_current", "is_target"] if c in merged.columns],
        ]
        raise RuntimeError(f"Unexpected CHECK rows in action universe before policy enforcement.\n{bad.to_string(index=False)}")

    merged = enforce_min_keep_current(merged, args.min_keep_current)
    merged = enforce_total_target_count(merged, target_count=int(topk["ticker"].nunique()))

    final_cols = [
        "ticker",
        "name_final",
        "shares",
        "current_shares",
        "current_weight",
        "target_shares",
        "target_value",
        "score",
        "score_rank",
        "industry_code",
        "industry_name",
        "industry4",
        "target_selection_bucket",
        "target_kept_from_previous",
        "cohort_status",
        "score_availability_reason",
        "latest_available_rebalance_month",
        "latest_available_year",
        "latest_available_quarter",
        "filter_status_diag",
        "passed_filters_diag",
        "selected_topk_diag",
        "in_target_topk_diag",
        "current_price_diag",
        "price_date_diag",
        "current_value_diag",
        "is_current",
        "is_target",
        "action",
        "reason",
    ]
    final_cols = [c for c in final_cols if c in merged.columns]
    out = merged.loc[merged["action"] != "CHECK", final_cols].copy()

    out["_ord"] = out["action"].map(ACTION_ORDER).fillna(9)
    out = out.sort_values(["_ord", "score_rank", "score"], ascending=[True, True, False], na_position="last")
    out = out.drop(columns=["_ord"]).reset_index(drop=True)

    out_path = Path(
        f"data/live/actions/live_actions__asof={args.asof}__metric={args.metric}"
        f"__strat={args.strategy}__target={args.target_date}__v={args.out_v}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False, encoding="utf-8-sig")

    cand = build_candidates_watchlist_from_actions(holdings, uni, merged)
    cand_path = Path(
        f"data/live/candidates/live_candidates__asof={args.asof}__metric={args.metric}"
        f"__strat={args.strategy}__target={args.target_date}__v={args.out_v}.csv"
    )
    cand_path.parent.mkdir(parents=True, exist_ok=True)
    if args.save_candidates or len(cand) > 0:
        cand.to_csv(cand_path, index=False, encoding="utf-8-sig")

    print(f"[OK] saved actions   : {out_path}")
    if args.save_candidates or len(cand) > 0:
        print(f"[OK] saved candidates: {cand_path}")

    print("\n[ACTION COUNTS]")
    print(out["action"].value_counts().to_string())

    print("\n[PREVIEW]")
    print(out.head(30).to_string(index=False))

    if len(cand) > 0:
        print("\n[CANDIDATE PREVIEW]")
        print(cand.head(20).to_string(index=False))


if __name__ == "__main__":
    main()