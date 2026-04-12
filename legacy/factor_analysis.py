import pandas as pd

p = r'.\data\processed\rebalance_audit__asof=2026-02-18__metric=revenue_op__k=10__strat=B_growth_plus_quality__v=3010.csv'
df = pd.read_csv(p)

df['fwd'] = pd.to_numeric(df['stock_forward_ret_to_next_rebalance'], errors='coerce')

factors = [
    'Revenue_ttm_yoy__z',
    'OpIncome_ttm_yoy__z',
    'Revenue_acc2__z',
    'OpIncome_acc2__z',
    'CAPEX_ttm_yoy__z',
    'Debt_to_Equity_log__z',
    'Quality_CFO_to_Assets__z',
    'CFO_isnull__z',
    'score'
]

print("\n=== FACTOR IC TABLE ===")

rows = []

for fac in factors:
    x = pd.to_numeric(df[fac], errors='coerce')
    g = pd.DataFrame({'x': x, 'fwd': df['fwd']}).dropna()

    rows.append({
        'factor': fac,
        'n': len(g),
        'pearson': g['x'].corr(g['fwd'], method='pearson'),
        'spearman': g['x'].corr(g['fwd'], method='spearman'),
        'mean': g['x'].mean(),
        'std': g['x'].std()
    })

res = pd.DataFrame(rows).sort_values('spearman', ascending=False)
print(res.to_string(index=False))


print("\n=== BUCKET TEST ===")

for fac in factors:

    x = pd.to_numeric(df[fac], errors='coerce')
    g = pd.DataFrame({'x': x, 'fwd': df['fwd']}).dropna().copy()

    print("\n", fac)

    try:
        g['bucket'] = pd.qcut(g['x'], 5, labels=False, duplicates='drop')

        print(
            g.groupby('bucket')['fwd']
            .agg(['count','mean','median'])
            .round(4)
        )

    except Exception as e:
        print("bucket error:", e)


print("\n=== FACTOR CORRELATION ===")

cols = factors
X = df[cols].apply(pd.to_numeric, errors='coerce')

print(X.corr(method='spearman').round(3))