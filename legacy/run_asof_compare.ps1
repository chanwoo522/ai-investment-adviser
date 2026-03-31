param(
  [string]$Metric = "revenue_op",
  [string]$Strat  = "B_growth_plus_quality",
  [int]$K = 20,
  [int]$Tcost = 30,
  [int]$FeatV = 2,
  [int]$RetV  = 1
)

$asofs = @("2025-05-31","2025-08-31","2025-11-30","2026-02-18")

# 1) Prepare all asofs (includes fundamentals/factors/features)
foreach($a in $asofs){
  python .\scripts\prepare_asof.py --asof $a --metric $Metric
}

# 2) Backtest for each asof (out_v increments so files don't collide)
$i = 1
foreach($a in $asofs){
  python .\scripts\backtest_quarterly_rebalance_v2.py `
    --asof $a `
    --metric $Metric `
    --feat_v $FeatV `
    --ret_v $RetV `
    --k $K `
    --strategy $Strat `
    --tcost_bps $Tcost `
    --out_v $i
  $i++
}

# 3) Summaries
foreach($a in $asofs){
  python .\scripts\ops\summarize_reports.py --asof $a --metric $Metric
}

# 4) Universe overlap (add/drop)
$asofs_json = ($asofs | ConvertTo-Json -Compress)
python -c "import pandas as pd, json
asofs=json.loads(r'$asofs_json')
def load_u(a):
    p=f'data/processed/universe__asof={a}__src=phase1__mcap_top=800__trd_bot=0.1__v=1.parquet'
    df=pd.read_parquet(p)
    return set(df['ticker'].astype(str))
U={a:load_u(a) for a in asofs}
for a in asofs:
    print(a, 'n=', len(U[a]))
for a,b in zip(asofs[:-1], asofs[1:]):
    inter=len(U[a]&U[b]); add=len(U[b]-U[a]); drop=len(U[a]-U[b])
    print(f'{a} -> {b}  inter={inter}  add={add}  drop={drop}')
"
