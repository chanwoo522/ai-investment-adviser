#!/usr/bin/env python
from __future__ import annotations

import argparse
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

    args = ap.parse_args()

    asof = args.asof
    metric = args.metric

    # 1) master 먼저
    _run('collect_krx_master.py', ['--asof', asof])

    # 2) 가격계열 먼저 확보 -> fallback marketdata에서도 latest price / traded_value를 붙일 수 있게
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

    # 3) marketdata는 prices_daily 생성 이후 실행
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

    # 4) universe
    _run('build_universe.py', ['--asof', asof, '--mcap_top', str(args.mcap_top), '--trd_bot', str(args.trd_bot)])

    exp = Path('scripts/ops/export_universe_csv.py')
    if exp.exists():
        _run('export_universe_csv.py', ['--asof', asof, '--metric', metric, '--out_v', str(args.universe_v)])

    # 5) optional shares / industry enrich
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

    # 6) fundamentals
    uni = _phase1_universe_path(asof, args.mcap_top, args.trd_bot, args.universe_v)
    if not uni.exists():
        alt_csv = Path(f'data/processed/universe__asof={asof}__metric={metric}__v={args.universe_v}.csv')
        alt_pq = Path(f'data/processed/universe__asof={asof}__metric={metric}__v={args.universe_v}.parquet')
        if alt_csv.exists():
            uni = alt_csv
        elif alt_pq.exists():
            uni = alt_pq

    _run(
        'collect_fundamentals_quarterly.py',
        [
            '--asof', asof,
            '--universe', str(uni),
            '--fs_div', args.fs_div,
        ],
    )

    # 7) phase1 features
    _run('build_features_phase1.py', ['--asof', asof])

    # 8) factor build (편의상 유지)
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
