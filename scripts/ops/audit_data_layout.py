from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class CheckResult:
    level: str   # OK / WARN / FAIL
    category: str
    message: str


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SCRIPTS_DIR = ROOT / "scripts"


def _glob_sorted(pattern: str, base: Path) -> list[Path]:
    return sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)


def _exists_one_of(paths: Iterable[Path]) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig")
    except Exception:
        return ""


def _scan_script_references() -> list[CheckResult]:
    """
    스크립트 내부의 경로 문자열을 느슨하게 스캔해서
    새 data 구조와 충돌할 만한 참조를 경고한다.
    """
    results: list[CheckResult] = []

    py_ps1_files = list(SCRIPTS_DIR.rglob("*.py")) + list(SCRIPTS_DIR.rglob("*.ps1"))

    suspicious_patterns = [
        r"current_portfolio",
        r"data[\\/]+processed[\\/]+features_live",
        r"data[\\/]+processed[\\/]+live_actions",
        r"data[\\/]+processed[\\/]+rebalance_report",
        r"20260314_holdings_clean_manual\.csv",
        r"latest\.csv",
    ]

    hits: list[tuple[Path, str]] = []
    for fp in py_ps1_files:
        txt = _read_text_safe(fp)
        for pat in suspicious_patterns:
            if re.search(pat, txt, flags=re.IGNORECASE):
                hits.append((fp, pat))

    if hits:
        for fp, pat in hits:
            results.append(CheckResult(
                "WARN",
                "script_ref",
                f"{fp.relative_to(ROOT)} 에 잠재적 구경로/하드코딩 참조 패턴 발견: {pat}"
            ))
    else:
        results.append(CheckResult(
            "OK",
            "script_ref",
            "주요 스크립트에서 명백한 구경로/하드코딩 참조 패턴이 발견되지 않았습니다."
        ))

    return results


def _check_core_dirs() -> list[CheckResult]:
    results: list[CheckResult] = []

    required_dirs = [
        DATA_DIR / "raw",
        DATA_DIR / "interim",
        DATA_DIR / "processed",
        DATA_DIR / "features",
        DATA_DIR / "live",
        DATA_DIR / "backtest",
        DATA_DIR / "archive",
        DATA_DIR / "reference",
        DATA_DIR / "portfolio",
    ]

    for d in required_dirs:
        if d.exists():
            results.append(CheckResult("OK", "dir", f"디렉터리 존재: {d.relative_to(ROOT)}"))
        else:
            results.append(CheckResult("FAIL", "dir", f"필수 디렉터리 누락: {d.relative_to(ROOT)}"))

    # 권장 하위 구조
    preferred_dirs = [
        DATA_DIR / "live" / "actions",
        DATA_DIR / "live" / "candidates",
        DATA_DIR / "live" / "execution",
        DATA_DIR / "live" / "reports",
        DATA_DIR / "live" / "scores",
        DATA_DIR / "portfolio" / "current",
        DATA_DIR / "portfolio" / "history",
        DATA_DIR / "features" / "features_live",
    ]
    for d in preferred_dirs:
        if d.exists():
            results.append(CheckResult("OK", "dir", f"권장 디렉터리 존재: {d.relative_to(ROOT)}"))
        else:
            results.append(CheckResult("WARN", "dir", f"권장 디렉터리 없음: {d.relative_to(ROOT)}"))

    return results


def _check_reference_files() -> list[CheckResult]:
    results: list[CheckResult] = []

    ref_candidates = [
        DATA_DIR / "reference" / "data_3133_20260329.xlsx",
        ROOT / "reference" / "data_3133_20260329.xlsx",
    ]
    ref_file = _exists_one_of(ref_candidates)
    if ref_file:
        results.append(CheckResult("OK", "reference", f"종목명-티커 참조 파일 확인: {ref_file.relative_to(ROOT)}"))
    else:
        results.append(CheckResult(
            "WARN",
            "reference",
            "종목명-티커 참조 xlsx(data_3133_20260329.xlsx)를 찾지 못했습니다. "
            "make_holdings_clean.py 실행 시 --master 경로를 명시해야 합니다."
        ))

    return results


def _check_live_run_chain(asof: str, metric: str, strategy: str, feat_v: str, action_v: str, report_v: str) -> list[CheckResult]:
    """
    특정 실전 실행 체인의 핵심 파일 존재 여부를 점검.
    """
    results: list[CheckResult] = []

    expected = {
        "fundamentals": DATA_DIR / "processed" / f"fundamentals_quarterly__asof={asof}__src=dart__fs=CFS__y=2016-2025__v=1.parquet",
        "features_live_a": DATA_DIR / "features" / "features_live" / f"features_live__asof={asof}__metric={metric}__v={feat_v}.parquet",
        "features_live_b": DATA_DIR / "processed" / f"features_live__asof={asof}__metric={metric}__v={feat_v}.parquet",
        "scores_full": DATA_DIR / "live" / "scores" / f"latest_scores__asof={asof}__metric={metric}__strat={strategy}__featv={feat_v}__target=2026-03-31__full_universe.csv",
        "scores_topk": DATA_DIR / "live" / "scores" / f"latest_scores__asof={asof}__metric={metric}__strat={strategy}__featv={feat_v}__target=2026-03-31__topk.csv",
        "actions": DATA_DIR / "live" / "actions" / f"live_actions__asof={asof}__metric={metric}__strat={strategy}__target=2026-03-31__v={action_v}.csv",
        "execution_live": DATA_DIR / "live" / "execution" / f"execution_plan__total=100000000__v={report_v}.csv",
        "execution_processed": DATA_DIR / "processed" / f"execution_plan__total=100000000__v={report_v}.csv",
        "report_md": DATA_DIR / "live" / "reports" / f"rebalance_report__asof={asof}__target=2026-03-31__metric={metric}__strat={strategy}__v={report_v}.md",
        "report_html": DATA_DIR / "live" / "reports" / f"rebalance_report__asof={asof}__target=2026-03-31__metric={metric}__strat={strategy}__v={report_v}.html",
        "report_detail": DATA_DIR / "live" / "reports" / f"rebalance_report_detail__asof={asof}__target=2026-03-31__metric={metric}__strat={strategy}__v={report_v}.csv",
    }

    for name, path in expected.items():
        # features_live / execution은 fallback 허용
        if name == "features_live_a":
            alt = expected["features_live_b"]
            if path.exists() or alt.exists():
                use = path if path.exists() else alt
                results.append(CheckResult("OK", "run_chain", f"{name} 확인: {use.relative_to(ROOT)}"))
            else:
                results.append(CheckResult("FAIL", "run_chain", f"features_live 누락: {path.relative_to(ROOT)} 또는 {alt.relative_to(ROOT)}"))
            continue

        if name == "execution_live":
            alt = expected["execution_processed"]
            if path.exists() or alt.exists():
                use = path if path.exists() else alt
                results.append(CheckResult("OK", "run_chain", f"{name} 확인: {use.relative_to(ROOT)}"))
            else:
                results.append(CheckResult("FAIL", "run_chain", f"execution_plan 누락: {path.relative_to(ROOT)} 또는 {alt.relative_to(ROOT)}"))
            continue

        if name in {"features_live_b", "execution_processed"}:
            continue

        if path.exists():
            results.append(CheckResult("OK", "run_chain", f"{name} 확인: {path.relative_to(ROOT)}"))
        else:
            results.append(CheckResult("FAIL", "run_chain", f"{name} 누락: {path.relative_to(ROOT)}"))

    return results


def _check_duplicate_risk(asof: str, metric: str, strategy: str) -> list[CheckResult]:
    results: list[CheckResult] = []

    patterns = [
        (DATA_DIR / "processed", f"features_live__asof={asof}__metric={metric}__v=*.parquet", "processed features_live"),
        (DATA_DIR / "features" / "features_live", f"features_live__asof={asof}__metric={metric}__v=*.parquet", "features/features_live"),
        (DATA_DIR / "processed", f"latest_scores__asof={asof}__metric={metric}__strat={strategy}__featv=*__target=*.csv", "processed latest_scores"),
        (DATA_DIR / "live" / "scores", f"latest_scores__asof={asof}__metric={metric}__strat={strategy}__featv=*__target=*.csv", "live scores"),
        (DATA_DIR / "processed", f"live_actions__asof={asof}__metric={metric}__strat={strategy}__target=*__v=*.csv", "processed actions"),
        (DATA_DIR / "live" / "actions", f"live_actions__asof={asof}__metric={metric}__strat={strategy}__target=*__v=*.csv", "live actions"),
    ]

    for base, pat, label in patterns:
        files = _glob_sorted(pat, base)
        if len(files) == 0:
            results.append(CheckResult("WARN", "duplicate_risk", f"{label}: 파일이 없습니다."))
        elif len(files) == 1:
            results.append(CheckResult("OK", "duplicate_risk", f"{label}: 1개"))
        else:
            newest = files[0].relative_to(ROOT)
            results.append(CheckResult(
                "WARN",
                "duplicate_risk",
                f"{label}: {len(files)}개 존재. 최신={newest}. 자동탐색 대신 버전 명시 사용 권장."
            ))

    return results


def _check_holdings_sources() -> list[CheckResult]:
    results: list[CheckResult] = []

    cur_dir = DATA_DIR / "portfolio" / "current"
    hist_dir = DATA_DIR / "portfolio" / "history"

    if cur_dir.exists():
        cur_files = sorted(cur_dir.glob("*holdings_clean*.csv"))
        if cur_files:
            newest = max(cur_files, key=lambda p: p.stat().st_mtime)
            results.append(CheckResult("OK", "holdings", f"current holdings 후보 확인: {newest.relative_to(ROOT)}"))
        else:
            results.append(CheckResult("WARN", "holdings", "data/portfolio/current 아래 holdings_clean csv가 없습니다."))
    else:
        results.append(CheckResult("WARN", "holdings", "data/portfolio/current 디렉터리가 없습니다."))

    if hist_dir.exists():
        raw_files = sorted(hist_dir.glob("*.xlsx"))
        if raw_files:
            newest = max(raw_files, key=lambda p: p.stat().st_mtime)
            results.append(CheckResult("OK", "holdings", f"history 원본 계좌 파일 후보 확인: {newest.relative_to(ROOT)}"))
        else:
            results.append(CheckResult("WARN", "holdings", "data/portfolio/history 아래 원본 xlsx가 없습니다."))
    else:
        results.append(CheckResult("WARN", "holdings", "data/portfolio/history 디렉터리가 없습니다."))

    # 구경로 흔적
    old_dir = ROOT / "current_portfolio"
    if old_dir.exists():
        old_files = list(old_dir.glob("*holdings_clean*.csv"))
        if old_files:
            results.append(CheckResult(
                "WARN",
                "holdings",
                f"구경로 current_portfolio 아래 holdings 파일이 남아 있습니다. "
                f"새 구조(data/portfolio/*)와 혼용되지 않도록 주의하세요."
            ))

    return results


def _summarize(results: list[CheckResult]) -> dict:
    summary = {"OK": 0, "WARN": 0, "FAIL": 0}
    for r in results:
        summary[r.level] += 1
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="data/scrips 경로 및 참조 무결성 점검")
    ap.add_argument("--asof", default="2026-03-29")
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--strategy", default="D_quality_filter_debt_profitaccel_liq")
    ap.add_argument("--feat_v", default="3291")
    ap.add_argument("--action_v", default="3291")
    ap.add_argument("--report_v", default="3291")
    ap.add_argument("--output_json", default=None)
    args = ap.parse_args()

    all_results: list[CheckResult] = []
    all_results.extend(_check_core_dirs())
    all_results.extend(_check_reference_files())
    all_results.extend(_check_holdings_sources())
    all_results.extend(_check_live_run_chain(
        asof=args.asof,
        metric=args.metric,
        strategy=args.strategy,
        feat_v=args.feat_v,
        action_v=args.action_v,
        report_v=args.report_v,
    ))
    all_results.extend(_check_duplicate_risk(
        asof=args.asof,
        metric=args.metric,
        strategy=args.strategy,
    ))
    all_results.extend(_scan_script_references())

    summary = _summarize(all_results)

    print("\n=== DATA LAYOUT AUDIT ===")
    for level in ("FAIL", "WARN", "OK"):
        print(f"\n[{level}]")
        subset = [r for r in all_results if r.level == level]
        if not subset:
            print("  (none)")
            continue
        for r in subset:
            print(f"  - ({r.category}) {r.message}")

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output_json:
        out = {
            "summary": summary,
            "results": [r.__dict__ for r in all_results],
        }
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] saved json: {out_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        raise