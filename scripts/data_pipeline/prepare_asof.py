#!/usr/bin/env python
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def _python_exe() -> Path:
    py = Path('.venv/Scripts/python.exe')
    if py.exists():
        return py
    py = Path('.venv/bin/python')
    if py.exists():
        return py
    return Path('python')


def _find_script(script_name: str) -> Path:
    candidates = [
        Path('scripts/data_pipeline') / script_name,
        Path('scripts/live') / script_name,
        Path('scripts/ops') / script_name,
        Path('scripts') / script_name,
    ]
    for p in candidates:
        if p.exists():
            return p

    matches = sorted(Path('scripts').rglob(script_name))
    if matches:
        return matches[0]

    raise FileNotFoundError(f"script not found under scripts/: {script_name}")


def _run(script_name: str, args: list[str], *, optional: bool = False) -> None:
    py = _python_exe()
    script = _find_script(script_name)
    cmd = [str(py), str(script)] + args
    print('[RUN]', ' '.join(cmd))
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        msg = f"Command failed ({r.returncode}): {' '.join(cmd)}"
        if optional:
            print(f"[WARN] {msg} -> continue")
            return
        raise RuntimeError(msg)


def _phase1_universe_path(asof: str, mcap_top: int, trd_bot: float, universe_v: int = 1) -> Path:
    return Path(f'data/processed/universe__asof={asof}__src=phase1__mcap_top={mcap_top}__trd_bot={trd_bot}__v={universe_v}.parquet')


def _load_table(path: Path):
    import pandas as pd

    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() in {".csv", ".txt"}:
        return pd.read_csv(path)
    raise ValueError(f"unsupported table type: {path}")


def _parse_asof_from_name(path: Path) -> str | None:
    import re

    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", path.name)
    return m.group(1) if m else None


def _fundamentals_canonical_path(asof: str, fs_div: str, fund_v: int) -> Path:
    return Path(
        f"data/processed/fundamentals_quarterly__asof={asof}"
        f"__src=dart__fs={fs_div}__y=2016-2025__v={fund_v}.parquet"
    )


def _fundamentals_candidates_for_asof(asof: str, fs_div: str) -> list[Path]:
    roots = [Path("data/processed"), Path("data/archive"), Path("data")]
    seen: set[str] = set()
    found: list[Path] = []

    patterns = [
        f"fundamentals_quarterly__asof={asof}__src=dart__fs={fs_div}__*.parquet",
        f"fundamentals_quarterly__asof={asof}__src=dart_merged__fs={fs_div}__*.parquet",
        f"fundamentals_quarterly__asof={asof}__*__fs={fs_div}__*.parquet",
    ]

    for root in roots:
        if not root.exists():
            continue
        for pat in patterns:
            for p in sorted(root.rglob(pat)):
                key = str(p.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append(p)
    return found


def _is_valid_fundamentals(path: Path) -> tuple[bool, str]:
    required = {"ticker", "year", "quarter"}
    try:
        df = _load_table(path)
    except Exception as e:
        return False, f"load_failed: {e}"

    cols = set(map(str, df.columns))
    missing = sorted(required - cols)
    if missing:
        return False, f"missing_columns={missing}"

    if len(df) == 0:
        return False, "rows=0"

    return True, f"rows={len(df)}"


def _find_fundamentals_fallback(asof: str, fs_div: str) -> Path | None:
    roots = [Path("data/processed"), Path("data/archive"), Path("data")]
    seen: set[str] = set()
    candidates: list[tuple[str, float, Path]] = []

    patterns = [
        f"fundamentals_quarterly__asof=*__src=dart__fs={fs_div}__*.parquet",
        f"fundamentals_quarterly__asof=*__src=dart_merged__fs={fs_div}__*.parquet",
        f"fundamentals_quarterly__asof=*__*__fs={fs_div}__*.parquet",
    ]

    for root in roots:
        if not root.exists():
            continue
        for pat in patterns:
            for p in root.rglob(pat):
                key = str(p.resolve())
                if key in seen:
                    continue
                seen.add(key)
                paf = _parse_asof_from_name(p)
                if paf == asof:
                    continue
                ok, _ = _is_valid_fundamentals(p)
                if not ok:
                    continue
                asof_key = paf or "0000-00-00"
                candidates.append((asof_key, p.stat().st_mtime, p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[-1][2]


def _ensure_fundamentals_ready(asof: str, fs_div: str, fund_v: int, allow_fallback: bool = False) -> Path:
    current_candidates = _fundamentals_candidates_for_asof(asof, fs_div)
    checked: list[str] = []

    canonical = _fundamentals_canonical_path(asof, fs_div, fund_v)
    if canonical.exists():
        ok, reason = _is_valid_fundamentals(canonical)
        checked.append(f"{canonical} -> {reason}")
        if ok:
            print(f"[OK] fundamentals validated: {canonical} ({reason})")
            return canonical

    for p in current_candidates:
        ok, reason = _is_valid_fundamentals(p)
        checked.append(f"{p} -> {reason}")
        if ok:
            print(f"[OK] fundamentals validated: {p} ({reason})")
            if p.resolve() != canonical.resolve():
                canonical.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, canonical)
                print(f"[OK] fundamentals canonicalized: {p} -> {canonical}")
                return canonical
            return p

    if not allow_fallback:
        details = "\n".join(checked) if checked else "(no current-asof fundamentals candidates found)"
        raise RuntimeError(
            "Current-asof fundamentals are invalid and fallback is disabled.\n"
            f"asof={asof}, fs_div={fs_div}, fund_v={fund_v}\n"
            f"checked:\n{details}"
        )

    fb = _find_fundamentals_fallback(asof, fs_div)
    if fb is None:
        details = "\n".join(checked) if checked else "(no current-asof fundamentals candidates found)"
        raise RuntimeError(
            "No valid fundamentals snapshot available for current asof and no fallback found.\n"
            f"asof={asof}, fs_div={fs_div}, fund_v={fund_v}\n"
            f"checked:\n{details}"
        )

    canonical.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fb, canonical)
    print(f"[FALLBACK] fundamentals replaced with latest valid snapshot: {fb} -> {canonical}")
    ok, reason = _is_valid_fundamentals(canonical)
    if not ok:
        raise RuntimeError(f"fallback fundamentals copied but still invalid: {canonical} ({reason})")
    print(f"[OK] fallback fundamentals validated: {canonical} ({reason})")
    return canonical


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--asof', required=True)
    ap.add_argument('--metric', default='revenue_op')

    ap.add_argument('--universe_v', type=int, default=1)
    ap.add_argument('--mcap_top', type=int, default=800)
    ap.add_argument('--trd_bot', type=float, default=0.1)

    ap.add_argument('--px_v', type=int, default=1)
    ap.add_argument('--ret_v', type=int, default=1)
    ap.add_argument('--lookback_years', type=int, default=15)
    ap.add_argument('--start', default='20160101')

    ap.add_argument('--fund_v', type=int, default=1)
    ap.add_argument('--factor_in_v', type=int, default=1)
    ap.add_argument('--factor_out_v', type=int, default=2)

    ap.add_argument('--fs_div', default='CFS', choices=['CFS', 'OFS'])
    ap.add_argument('--with_shares_industry', action='store_true')
    ap.add_argument('--shares_out_v', type=int, default=1)
    ap.add_argument('--allow_fallback', action='store_true')

    # smart mode 연동용: 기본 동작은 기존과 동일하게 universe 유지
    ap.add_argument('--fund_mode', default='universe', choices=['universe', 'union', 'missing_only'])

    args = ap.parse_args()

    asof = args.asof
    metric = args.metric

    _run('collect_krx_master.py', ['--asof', asof])

    _run(
        'collect_prices.py',
        ['--asof', asof, '--freq', 'd', '--lookback_years', str(args.lookback_years)],
        optional=True,
    )

    _run(
        'make_prices_daily.py',
        [
            '--asof', asof,
            '--metric', metric,
            '--start', args.start,
            '--universe_v', str(args.universe_v),
            '--out_v', str(args.px_v),
        ],
    )

    _run(
        'make_returns_monthly.py',
        [
            '--asof', asof,
            '--metric', metric,
            '--px_v', str(args.px_v),
            '--out_v', str(args.ret_v),
        ],
    )

    _run(
        'collect_krx_marketdata.py',
        [
            '--asof', asof,
            '--metric', metric,
            '--out_v', '1',
            '--soft_fail',
            '--synth_mcap',
            '--prices_v', str(args.px_v),
        ],
    )

    _run('build_universe.py', ['--asof', asof, '--mcap_top', str(args.mcap_top), '--trd_bot', str(args.trd_bot)])

    exp = Path('scripts/ops/export_universe_csv.py')
    if exp.exists():
        _run('export_universe_csv.py', ['--asof', asof, '--metric', metric, '--out_v', str(args.universe_v)])

    if args.with_shares_industry:
        _run(
            'collect_dart_shares_industry.py',
            [
                '--asof', asof,
                '--metric', metric,
                '--out_v', str(args.shares_out_v),
                '--with_industry',
            ],
        )
        _run(
            'collect_krx_marketdata.py',
            [
                '--asof', asof,
                '--metric', metric,
                '--out_v', '1',
                '--soft_fail',
                '--synth_mcap',
                '--prices_v', str(args.px_v),
            ],
        )

    uni = _phase1_universe_path(asof, args.mcap_top, args.trd_bot, args.universe_v)
    if not uni.exists():
        alt_csv = Path(f'data/processed/universe__asof={asof}__metric={metric}__v={args.universe_v}.csv')
        alt_pq = Path(f'data/processed/universe__asof={asof}__metric={metric}__v={args.universe_v}.parquet')
        if alt_csv.exists():
            uni = alt_csv
        elif alt_pq.exists():
            uni = alt_pq

    fund_args = [
        '--asof', asof,
        '--universe', str(uni),
        '--fs_div', args.fs_div,
        '--mode', args.fund_mode,
    ]

    # missing_only는 기존 결과와 합쳐야 안전하므로 자동 보장
    if args.fund_mode == 'missing_only':
        fund_args.append('--merge_existing')

    _run('collect_fundamentals_quarterly.py', fund_args)

    validated_fund = _ensure_fundamentals_ready(
        asof, args.fs_div, args.fund_v, allow_fallback=args.allow_fallback
    )
    print(f"[OK] fundamentals ready for downstream: {validated_fund}")

    _run('build_features_phase1.py', ['--asof', asof])

    _run(
        'build_factors_ttm_acc2.py',
        [
            '--asof', asof,
            '--metric', metric,
            '--in_v', str(args.factor_in_v),
            '--out_v', str(args.factor_out_v),
        ],
    )

    print('[OK] prepare_asof done:', asof)


if __name__ == '__main__':
    main()