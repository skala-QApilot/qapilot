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
    """run_pipeline 직접 호출(command=test, 코드 재생성 없이 기존 generated-code 재사용)
    → {cross_check_results, root_cause_results, status}.

    등록된 측정 service(config.SERVICE_ID)의 시나리오/코드를 DB 에서 로드하고,
    staging_url(게이트웨이 8090)·test_account(demo1)·target_root 를 명시 주입한다.
    결과는 in-memory state 에서 추출 — DB defects 적재(V19) 실패와 무관.
    """
    import asyncio
    from qapilot.orchestrator.runner import run_pipeline

    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    try:
        state = asyncio.run(run_pipeline(
            options,
            service_id=config.SERVICE_ID,
            staging_url=config.STAGING_URL,
            test_account=config.TEST_ACCOUNT,
            target_root=config.TARGET_ROOT,
            qapilot_dir=config.SERVICE_QAPILOT_DIR,
        ))
    except Exception as e:  # noqa: BLE001
        import traceback
        print(f"[qapilot] 실행 실패 ({config_name} run{run_idx}): {type(e).__name__}: {e}")
        traceback.print_exc()
        return {"cross_check_results": [], "root_cause_results": [], "status": "error"}
    # api_results: TC별 관측 HTTP status/endpoint — golden 오라클 대조 + ①장애유형 + 검출 endpoint.
    # 부피 줄이려 calls 의 method/url/status_code 만 슬림 저장.
    api_slim = []
    for ar in (state.get("api_results") or []):
        calls = [{"method": c.get("method"), "url": c.get("url"),
                  "status_code": c.get("status_code")}
                 for c in (ar.get("calls") or []) if isinstance(c, dict)]
        api_slim.append({"tc_id": ar.get("tc_id"), "verdict": ar.get("verdict"),
                         "verify_mode": ar.get("verify_mode"), "calls": calls})
    return {
        "cross_check_results": state.get("cross_check_results") or [],
        "root_cause_results": state.get("root_cause_results") or [],
        # fix_results 에 원인 위치(file_path)가 담긴다 — 원인 추론 Top-N 측정에 필요.
        # (root_cause 후보의 file_path 는 None 인 경우가 많아 fix_results 로 보완)
        "fix_results": state.get("fix_results") or [],
        "api_results": api_slim,
        "status": state.get("status"),
    }


def _build_llm_judge():
    """원인 의미 일치 LLM-judge — (candidate_cause, gt_root_cause) -> bool.

    qapilot LLMClient(seed=42, temp=0) 로 0/1 판정. 실패 시 False.
    """
    import asyncio
    try:
        from qapilot.shared.llm_client import LLMClient
        client = LLMClient()
    except Exception as e:  # noqa: BLE001
        print(f"[judge] LLMClient 사용 불가 → 의미 채점 생략: {e}")
        return None

    SYS = ("너는 결함 원인 판정관이다. 시스템 추론이 정답 원인의 핵심"
           "(어느 함수/규칙/조건이 왜 잘못됐는지)과 의미상 일치하면 YES, 아니면 NO. "
           "한 단어로만 답하라: YES 또는 NO.")

    def judge(cand: str, gt: str) -> bool:
        user = f"[정답 원인]\n{gt}\n\n[시스템 추론]\n{cand}"
        try:
            resp = asyncio.run(client.chat(SYS, user, temperature=0.0))
            text = getattr(resp, "content", None) or getattr(resp, "text", None) or str(resp)
            return "YES" in str(text).upper()
        except Exception:  # noqa: BLE001
            return False
    return judge


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
                 faults, clean_runs: list[list[scoring.TCOutcome]] | None,
                 golden=None, llm_judge=None) -> dict:
    """한 구성의 N회 결과를 채점."""
    if config_name == config.CLEAN:
        tca = [scoring.test_code_accuracy_clean(r) for r in runs]
        vvr = [scoring.valid_verdict_rate(r)["rate"] for r in runs]
        fp = [scoring.precision_from_clean(r)["false_positives"] for r in runs]
        return {
            "config": config_name, "kind": "clean",
            "test_code_accuracy": scoring.mean_std([t["accuracy"] for t in tca]),
            "test_code_accuracy_explicit": scoring.mean_std(
                [t["accuracy_explicit"] for t in tca if t["accuracy_explicit"] is not None]),
            "n_estimated_fail": scoring.mean_std([float(t["n_estimated_fail"]) for t in tca]),
            "valid_verdict_rate": scoring.mean_std(vvr),
            "false_positives_mean": scoring.mean_std([float(x) for x in fp]),
            "golden_oracle": scoring.golden_oracle_accuracy(runs[0], golden) if golden else None,
            "per_run": tca,
        }

    fault = faults[config_name]
    # 검출 기준선 = clean 전 런 합집합(노이즈 제거). 단일 런이 아니라 모든 clean 런.
    clean_all = [o for run in (clean_runs or []) for o in run]
    det = [scoring.detection(r, fault, clean_all) for r in runs]
    cls = [scoring.classification(r, fault, clean_all) for r in runs]
    rc = [scoring.root_cause_topn(r, fault, clean_outcomes=clean_all) for r in runs]
    sem = [scoring.root_cause_semantic(r, fault, llm_judge, clean_all) for r in runs]
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
            "semantic_match": scoring.mean_std(
                [s["match_rate"] for s in sem if s["match_rate"] is not None]),
            "per_run": rc, "semantic_per_run": sem,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="QApilot 성능 측정 — 구성×N → 지표")
    ap.add_argument("--score-only", action="store_true",
                    help="저장된 run 아티팩트만으로 채점 (SUT/QApilot 미실행)")
    ap.add_argument("--no-fresh-volume", action="store_true",
                    help="구성 전환 시 down -v 생략 (디버그용 — seed/state 오염 주의)")
    ap.add_argument("--judge", action="store_true",
                    help="원인 의미(LLM-judge) 채점 활성화 (qapilot LLMClient, seed 고정)")
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
                fix_results=raw.get("fix_results"),
                api_results=raw.get("api_results"),
            ))
        collected[cfg] = runs

    # 2) 채점
    clean_runs = collected.get(config.CLEAN)
    llm_judge = _build_llm_judge() if args.judge else None
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "n_runs": args.n, "configs": {}}
    for cfg, runs in collected.items():
        if not runs:
            continue
        report["configs"][cfg] = score_config(
            cfg, runs, faults, clean_runs, golden=golden, llm_judge=llm_judge)

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
