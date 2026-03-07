from __future__ import annotations
import os, json, time, hashlib
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, Iterable

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

BASE = Path(r"C:\Users\chanw\ai_inv_adv")
RAW  = BASE / "data" / "raw"
PRO  = BASE / "data" / "processed"
FEAT = BASE / "data" / "features"
BT   = BASE / "data" / "backtest"
LOGS = BASE / "logs"

def ensure_dirs():
    for p in [RAW, PRO, FEAT, BT, LOGS]:
        p.mkdir(parents=True, exist_ok=True)

def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def log_path(name: str, asof: str) -> Path:
    ensure_dirs()
    return LOGS / f"{name}__asof={asof}__ts={now_ts()}.log"

def write_log(msg: str, fp: Path):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")

def parquet_path(kind: str, asof: str, src: str, extra: str = "", v: int = 1, root: Optional[Path]=None) -> Path:
    root = root or RAW
    extra_part = f"__{extra}" if extra else ""
    return root / f"{kind}__asof={asof}__src={src}{extra_part}__v={v}.parquet"

def save_parquet_atomic(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)

def load_parquet_if_exists(path: Path) -> Optional[pd.DataFrame]:
    return pd.read_parquet(path) if path.exists() else None

def checkpoint_path(task: str, asof: str) -> Path:
    ensure_dirs()
    return PRO / f"_checkpoint__{task}__asof={asof}.json"

def load_checkpoint(task: str, asof: str) -> Dict[str, Any]:
    p = checkpoint_path(task, asof)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}

def save_checkpoint(task: str, asof: str, data: Dict[str, Any]):
    p = checkpoint_path(task, asof)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def winsorize_series(s: pd.Series, p: float = 0.02) -> pd.Series:
    lo = s.quantile(p)
    hi = s.quantile(1 - p)
    return s.clip(lower=lo, upper=hi)

def robust_z(s: pd.Series) -> pd.Series:
    # z = (x - median) / (IQR/1.349)
    med = s.median()
    q1 = s.quantile(0.25)
    q3 = s.quantile(0.75)
    iqr = q3 - q1
    denom = (iqr / 1.349) if iqr and iqr != 0 else s.std(ddof=0)
    if denom == 0 or pd.isna(denom):
        return pd.Series([0.0] * len(s), index=s.index)
    return (s - med) / denom

@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type(Exception),
)
def retryable(fn, *args, **kwargs):
    return fn(*args, **kwargs)
