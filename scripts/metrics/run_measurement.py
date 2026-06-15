"""측정 오케스트레이터 — 구성 매트릭스 × N회 → 정답 대조 → 지표.

흐름(측정 설계서 §7-4):
  for config in [clean, RULE-001..004, API-001]:
      SUT 를 해당 결함 구성으로 pristine 기동 (down -v + ENABLED_FAULTS)
      for run in 1..N:
          QApilot 파이프라인 실행 → cross_check_results + root_cause_results 수집
          run 아티팩트 저장
      구성별 채점 + N회 consistency(pass^k)
  전체 집계 → metrics_report.json + 요약 출력

수집(invoke_qapilot)은 어댑터:
- 기본: run_pipeline(staging_url, test_account) 직접 호출 → 결과 state 에서 두 list 추출.
- --score-only: 이미 저장된 run 아티팩트(JSON)만으로 채점(SUT/QApilot 불필요, 빠른 반복).

주의: 라이브 수집은 QApilot 가 SUT 시나리오/코드를 갖춘 상태(generate 선행 또는 등록된
service)를 전제로 한다. 시나리오 소스 배선은 환경별이라, 본 스크립트는 수집 어댑터를
교체 가능하게 두고 채점 로직을 핵심 자산으로 검증한다.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import config
import scoring
from ground_truth import load_faults, load_golden


# ── QApilot 수집 어댑터 ─────────────────────────────────────────────
def invoke_qapilot_live(config_name: str, run_idx: int) -> dict:
    """run_pipeline 직접 호출 → {cross_check_results, root_cause_results, status}.

    service_id/시나리오는 등록된 측정 service 를 사용(환경 의존). 실패 시 빈 결과.
    """
    import asyncio
    import os
    from qapilot.orchestrator.runner import run_pipeline

    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    try:
        state = asyncio.run(run_pipeline(
            options,
            service_id=os.getenv("MEASURE_SERVICE_ID", ""),
            staging_url=config.STAGING_URL,
            test_account=config.TEST_ACCOUNT,
            qapilot_dir=Path.cwd() / ".qapilot",
        ))
    except Exception as e:  # noqa: BLE001
        print(f"[qapilot] 실행 실패 ({config_name} run{run_idx}): {type(e).__name__}: {e}")
        return {"cross_check_results": [], "root_cause_results": [], "status": "error"}
    return {
        "cross_check_results": state.get("cross_check_results") or [],
        "root_cause_results": state.get("root_cause_results") or [],
        "status": state.get("status"),
    }


def _artifact_path(out_dir: Path, config_name: str, run_idx: int) -> Path:
    return out_dir / "runs" / f"{config_name}__run{run_idx}.json"


def load_artifact(out_dir: Path, config_name: str, run_idx: int) -> dict | None:
    p = _artifact_path(out_dir, config_name, run_idx)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def save_artifact(out_dir: Path, config_name: str, run_idx: int, raw: dict) -> None:
    p = _artifact_path(out_dir, config_name, run_idx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 구성별 채점 ─────────────────────────────────────────────────────
def score_config(config_name: str, runs: list[list[scoring.TCOutcome]],
                 faults, clean_runs: list[list[scoring.TCOutcome]] | None) -> dict:
    """한 구성의 N회 결과를 채점."""
    if config_name == config.CLEAN:
        accs = [scoring.test_code_accuracy_clean(r)["accuracy"] for r in runs]
        vvr = [scoring.valid_verdict_rate(r)["rate"] for r in runs]
        fp = [scoring.precision_from_clean(r)["false_positives"] for r in runs]
        return {
            "config": config_name, "kind": "clean",
            "test_code_accuracy": scoring.mean_std(accs),
            "valid_verdict_rate": scoring.mean_std(vvr),
            "false_positives_mean": scoring.mean_std([float(x) for x in fp]),
            "per_run": [scoring.test_code_accuracy_clean(r) for r in runs],
        }

    fault = faults[config_name]
    clean_rep = (clean_runs or [[]])[0] if clean_runs else []
    det = [scoring.detection(r, fault, clean_rep) for r in runs]
    cls = [scoring.classification(r, fault) for r in runs]
    rc = [scoring.root_cause_topn(r, fault) for r in runs]
    return {
        "config": config_name, "kind": "fault",
        "fault": {"category": fault.category, "defect_type": fault.defect_type,
                  "endpoint": fault.endpoint, "fix_file": fault.fix_file},
        "detection": {
            "consistency": scoring.consistency([d["detected"] for d in det]),
            "per_run": det,
        },
        "classification": {
            "type_acc": scoring.mean_std([c["type_acc"] for c in cls if c["type_acc"] is not None]),
            "decision_acc": scoring.mean_std([c["decision_acc"] for c in cls if c["decision_acc"] is not None]),
            "both_acc": scoring.mean_std([c["both_acc"] for c in cls if c["both_acc"] is not None]),
            "per_run": cls,
        },
        "root_cause": {
            "top1": scoring.consistency([r["top1"] for r in rc]),
            "top3": scoring.consistency([r["top3"] for r in rc]),
            "top5": scoring.consistency([r["top5"] for r in rc]),
            "mrr": scoring.mean_std([r["mrr"] for r in rc]),
            "per_run": rc,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="QApilot 성능 측정 — 구성×N → 지표")
    ap.add_argument("--score-only", action="store_true",
                    help="저장된 run 아티팩트만으로 채점 (SUT/QApilot 미실행)")
    ap.add_argument("--no-fresh-volume", action="store_true",
                    help="구성 전환 시 down -v 생략 (디버그용 — seed/state 오염 주의)")
    ap.add_argument("--configs", nargs="*", default=config.CONFIGS,
                    help=f"측정할 구성 (기본: {config.CONFIGS})")
    ap.add_argument("--n", type=int, default=config.N_RUNS)
    args = ap.parse_args()

    out_dir = config.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    faults = load_faults(config.FAULTS_DIR)
    golden = load_golden(config.GOLDEN_DIR)
    print(f"[gt] golden {len(golden)} TC, faults {len(faults)}개 로드")

    if not args.score_only:
        import sut_control

    # 1) 수집 (또는 아티팩트 로드)
    collected: dict[str, list[list[scoring.TCOutcome]]] = {}
    for cfg in args.configs:
        runs: list[list[scoring.TCOutcome]] = []
        if not args.score_only:
            ok = sut_control.bring_up(cfg, fresh_volume=not args.no_fresh_volume)
            if not ok:
                print(f"[skip] {cfg} 기동 실패 → 건너뜀")
                continue
            sanity = sut_control.verify_config(cfg)
            print(f"[sanity] {cfg}: {sanity}")
        for i in range(1, args.n + 1):
            raw = load_artifact(out_dir, cfg, i) if args.score_only else None
            if raw is None and not args.score_only:
                raw = invoke_qapilot_live(cfg, i)
                save_artifact(out_dir, cfg, i, raw)
            if raw is None:
                print(f"[warn] {cfg} run{i} 아티팩트 없음 → 건너뜀")
                continue
            runs.append(scoring.normalize_run(
                raw.get("cross_check_results", []),
                raw.get("root_cause_results", []),
                endpoint_by_tc=raw.get("endpoint_by_tc"),
                req_by_tc=raw.get("req_by_tc"),
            ))
        collected[cfg] = runs

    # 2) 채점
    clean_runs = collected.get(config.CLEAN)
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "n_runs": args.n, "configs": {}}
    for cfg, runs in collected.items():
        if not runs:
            continue
        report["configs"][cfg] = score_config(cfg, runs, faults, clean_runs)

    out_file = out_dir / "metrics_report.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[report] {out_file}")
    _print_summary(report)
    return 0


def _print_summary(report: dict) -> None:
    print("\n=== 측정 요약 ===")
    for cfg, r in report["configs"].items():
        if r["kind"] == "clean":
            print(f"[clean] 테스트코드정확도={r['test_code_accuracy']['mean']} "
                  f"유효판정율={r['valid_verdict_rate']['mean']} "
                  f"오탐={r['false_positives_mean']['mean']}")
        else:
            d = r["detection"]["consistency"]
            print(f"[{cfg}] 검출 pass^k={d['pass_pow_k']}(rate={d['rate']}) "
                  f"분류①∧②={r['classification']['both_acc']['mean']} "
                  f"원인Top5_rate={r['root_cause']['top5']['rate']} "
                  f"MRR={r['root_cause']['mrr']['mean']}")


if __name__ == "__main__":
    raise SystemExit(main())
