# engine/orders.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from .io import ensure_dir, save_df


@dataclass(frozen=True)
class OrdersConfig:
    out_dir: Path
    asof: str
    metric: str
    strategy: str
    k: int
    out_v: int = 1

    def orders_name(self) -> str:
        return (
            f"orders__asof={self.asof}"
            f"__metric={self.metric}"
            f"__strategy={self.strategy}"
            f"__k={self.k}"
            f"__v={self.out_v}.parquet"
        )

    def summary_name(self) -> str:
        return (
            f"summary__asof={self.asof}"
            f"__metric={self.metric}"
            f"__strategy={self.strategy}"
            f"__k={self.k}"
            f"__v={self.out_v}.md"
        )

    def orders_path(self) -> Path:
        return Path(self.out_dir) / self.orders_name()

    def summary_path(self) -> Path:
        return Path(self.out_dir) / self.summary_name()


def _md_escape(x) -> str:
    if x is None:
        return ""
    s = str(x)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def df_to_markdown_table(df: pd.DataFrame, cols: list[str], max_rows: int = 50) -> str:
    """
    Minimal markdown table generator (no tabulate dependency).
    """
    if df is None or len(df) == 0:
        return "_(empty)_"

    use = df.copy()
    for c in cols:
        if c not in use.columns:
            use[c] = ""
    use = use[cols].head(max_rows)

    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [header, sep]

    for _, row in use.iterrows():
        vals = [_md_escape(row[c]) for c in cols]
        lines.append("| " + " | ".join(vals) + " |")

    return "\n".join(lines)


def build_orders(
    holdings_now: pd.DataFrame,
    prev_holdings: Optional[pd.DataFrame] = None,
    reasons: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, dict]:
    """
    Build target orders:
      - target holdings: holdings_now[ticker, weight]
      - if prev_holdings provided -> compute delta
    Returns:
      (orders_df, summary_dict)
    """
    now = holdings_now.copy()
    if "ticker" not in now.columns:
        raise ValueError("holdings_now must include 'ticker'")
    if "weight" not in now.columns:
        now["weight"] = 1.0 / max(1, len(now))

    now["ticker"] = now["ticker"].astype(str).str.zfill(6)

    if reasons is not None and "ticker" in reasons.columns:
        r = reasons.copy()
        r["ticker"] = r["ticker"].astype(str).str.zfill(6)
        now = now.merge(r, on="ticker", how="left")

    prev = None
    if prev_holdings is not None and len(prev_holdings) > 0 and "ticker" in prev_holdings.columns:
        prev = prev_holdings.copy()
        prev["ticker"] = prev["ticker"].astype(str).str.zfill(6)
        if "weight" not in prev.columns:
            prev["weight"] = 0.0
        prev = prev[["ticker", "weight"]].rename(columns={"weight": "prev_weight"})

    if prev is None:
        out = now.rename(columns={"weight": "target_weight"}).copy()
        out["prev_weight"] = 0.0
        out["delta_weight"] = out["target_weight"]
    else:
        out = now.rename(columns={"weight": "target_weight"}).merge(prev, on="ticker", how="left")
        out["prev_weight"] = out["prev_weight"].fillna(0.0)
        out["delta_weight"] = out["target_weight"] - out["prev_weight"]

    out["action"] = out["delta_weight"].apply(lambda x: "BUY" if x > 0 else ("SELL" if x < 0 else "HOLD"))

    summary = {
        "n_target": int(len(out)),
        "gross_target_weight": float(out["target_weight"].sum()),
        "gross_prev_weight": float(out["prev_weight"].sum()),
        "gross_delta": float(out["delta_weight"].sum()),
        "n_buy": int((out["action"] == "BUY").sum()),
        "n_sell": int((out["action"] == "SELL").sum()),
    }
    return out, summary


def save_orders(orders: pd.DataFrame, cfg: OrdersConfig) -> Path:
    p = cfg.orders_path()
    save_df(orders, p)
    print(f"[OK] saved: {p}")
    return p


def save_summary_md(holdings: pd.DataFrame, orders: pd.DataFrame, cfg: OrdersConfig) -> Path:
    p = cfg.summary_path()
    ensure_dir(p.parent)

    lines = []
    lines.append("# Strategy Summary\n")
    lines.append(f"- asof: `{cfg.asof}`")
    lines.append(f"- metric: `{cfg.metric}`")
    lines.append(f"- strategy: `{cfg.strategy}`")
    lines.append(f"- k: `{cfg.k}`")
    lines.append(f"- v: `{cfg.out_v}`\n")

    h = holdings.copy()
    if "ticker" in h.columns:
        h["ticker"] = h["ticker"].astype(str).str.zfill(6)

    cols_h = [c for c in ["ticker", "name", "weight", "score_total", "score_core", "score_pattern"] if c in h.columns]
    if not cols_h:
        cols_h = list(h.columns[:6])

    lines.append("## Top Holdings\n")
    lines.append(df_to_markdown_table(h.sort_values("weight", ascending=False), cols_h, max_rows=20))
    lines.append("")

    o = orders.copy()
    if "ticker" in o.columns:
        o["ticker"] = o["ticker"].astype(str).str.zfill(6)
    cols_o = [c for c in ["ticker", "action", "prev_weight", "target_weight", "delta_weight", "reason_top_features"] if c in o.columns]
    if not cols_o:
        cols_o = list(o.columns[:6])

    lines.append("## Orders\n")
    lines.append(df_to_markdown_table(o, cols_o, max_rows=50))
    lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] saved: {p}")
    return p