# >>> ACTIVE_FALLBACK (USED BY prepare_asof - DO NOT REMOVE)
from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd


def _find_universe(asof: str) -> Path:
    processed = Path('data/processed')
    candidates = [
        processed / f'universe__asof={asof}__metric=revenue_op__v=1.parquet',
        processed / f'universe__asof={asof}__metric=revenue_op__v=1.csv',
    ]
    candidates.extend(sorted(processed.glob(f'universe__asof={asof}__src=phase1__mcap_top=*__trd_bot=*__v=*.parquet'), reverse=True))
    candidates.extend(sorted(processed.glob(f'universe__asof={asof}__src=phase1__mcapTop=*__trdBot=*__capQ=*__v=*.parquet'), reverse=True))
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(f'Universe not found for asof={asof}. Run build_universe.py first.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--asof', required=True)
    args = ap.parse_args()
    asof = args.asof

    uni_path = _find_universe(asof)
    uni = pd.read_csv(uni_path) if uni_path.suffix.lower() == '.csv' else pd.read_parquet(uni_path)
    uni = uni.copy()
    uni['ticker'] = uni['ticker'].astype(str).str.replace(r'\.0$', '', regex=True).str.zfill(6)

    if 'industry4' not in uni.columns:
        if 'industry_code' in uni.columns and not uni['industry_code'].isna().all():
            code = uni['industry_code'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip().replace('nan', '')
            uni['industry4'] = code.str.zfill(4).str.slice(0, 4)
            uni.loc[uni['industry4'].isin(['', '0000']), 'industry4'] = None
        else:
            uni['industry4'] = None

    for c in ['name', 'market', 'cap_bucket']:
        if c not in uni.columns:
            uni[c] = None

    out_dir = Path('data/features')
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'features_phase1__asof={asof}__src=phase1__v=1.parquet'
    feats = uni[['ticker', 'name', 'market', 'industry4', 'cap_bucket']].copy()
    feats.to_parquet(out_path, index=False)
    print('[OK] universe:', uni_path)
    print('[OK] saved:', out_path)
    print('[CHECK] rows:', len(feats), 'industry4 null ratio:', float(feats['industry4'].isna().mean()))


if __name__ == '__main__':
    main()

