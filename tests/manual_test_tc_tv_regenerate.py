"""S3의 ts_generation.json을 입력으로 TC 생성 단계만 재실행하는 수동 테스트 스크립트.

prd_only_experiment 파이프라인의 PRD 문서 임포트 / TS 생성 단계는 비용이 크다
(전체 재실행 시 trace 1건당 수 분 / 수 달러). 이미 생성된 ts_generation.json
(services/{service_id}/scenario-runs/{trace_id}/ts_generation.json) 의 ts_list 를
재사용해 _tc_generate_doc_search → _tv_generate_codebase_aware → _save_experiment_scenarios
세 단계만 재실행하여 TC(given/when/then/values/evidence)를 갱신한다.

프롬프트 변경(예: evidence 필드 추가) 후 회귀 확인용.

실행:
    python tests/manual_test_tc_tv_regenerate.py <trace_id> [--service-id ID] \
        [--ts TS-001 TS-002 ...] [--out tests/tc_tv_regenerate_output.json]

trace_id 만 주면 service_id 는 runs 테이블(load_trace)에서 조회한다.
--ts 를 생략하면 ts_generation.json 의 모든 TS 를 대상으로 한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qapilot.orchestrator.pipeline import (
    _save_experiment_scenarios,
    _tc_generate_doc_search,
    _tv_generate_codebase_aware,
)
from qapilot.shared.trace_store import load_trace
from qapilot.storage import s3_client


def _green(s: str) -> str:  return f"\033[92m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[93m{s}\033[0m"
def _cyan(s: str) -> str:   return f"\033[96m{s}\033[0m"
def _bold(s: str) -> str:   return f"\033[1m{s}\033[0m"


def _load_ts_generation(service_id: str, trace_id: str) -> dict:
    key = f"services/{service_id}/scenario-runs/{trace_id}/ts_generation.json"
    raw = s3_client.get_object(key)
    if raw is None:
        raise SystemExit(f"S3 객체를 찾을 수 없음: {key}")
    return json.loads(raw.decode("utf-8"))


def _print_tc(tc: dict) -> None:
    print(f"  {_cyan(tc.get('tc_id', '?'))} {tc.get('name', '')}")
    print(f"    given: {tc.get('given', '')}")
    print(f"    when : {tc.get('when', '')}")
    print(f"    then : {tc.get('then', '')}")
    evidence = tc.get("evidence") or {}
    print(f"    evidence: given={evidence.get('given')!r} "
          f"when={evidence.get('when')!r} then={evidence.get('then')!r}")
    for v in tc.get("values") or []:
        ev = v.get("evidence", "?")
        tag = _yellow("[근거 없음]") if ev == "근거 없음" else _green(f"[{ev}]")
        print(f"      - {v.get('field')}={v.get('value')!r} {tag}")
    cb = tc.get("codebase_ref") or {}
    print(f"    codebase_ref: service_id={cb.get('service_id')} "
          f"commit_sha={cb.get('commit_sha')} files={cb.get('files')} table={cb.get('table')}")


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_id", help="기존 prd_only_experiment 실행의 trace_id")
    parser.add_argument("--service-id", default=None, help="미지정 시 runs 테이블에서 조회")
    parser.add_argument("--ts", nargs="*", default=None,
                         help="대상 TS ID 목록 (예: TS-001 TS-002). 미지정 시 전체")
    parser.add_argument("--out", default="tests/tc_tv_regenerate_output.json",
                         help="갱신된 시나리오 JSON 저장 경로")
    args = parser.parse_args()

    service_id = args.service_id
    if not service_id:
        trace = load_trace(args.trace_id)
        if not trace or not trace.get("service_id"):
            raise SystemExit("--service-id 를 지정하거나 DB에 등록된 trace_id 를 사용하라.")
        service_id = trace["service_id"]

    print(_bold(f"\n=== TC/TV 재생성 (trace_id={args.trace_id}, service_id={service_id}) ==="))

    ts_generation = _load_ts_generation(service_id, args.trace_id)
    ts_list = ts_generation.get("ts_list") or []
    print(f"ts_generation.json 로드 완료 — TS {len(ts_list)}개")

    if args.ts:
        target_indices = []
        for t in args.ts:
            try:
                idx = int(str(t).replace("TS-", "")) - 1
                if 0 <= idx < len(ts_list):
                    target_indices.append(f"TS-{idx + 1:03d}")
            except ValueError:
                pass
        tc_target_ts_ids = target_indices
    else:
        tc_target_ts_ids = []  # 빈 리스트 = 전체 TS 처리

    with tempfile.TemporaryDirectory() as tmp_dir:
        state: dict = {
            "trace_id": args.trace_id,
            "service_id": service_id,
            "qapilot_dir": tmp_dir,
            "run_options": {
                "command": "prd_only_experiment",
                "trigger": "init",
                "tc_target_ts_ids": tc_target_ts_ids,
            },
            "ts_list": ts_list,
            "requirements": [],
            "agent_logs": [],
        }

        print(_cyan("\n[1/3] tc_doc_search — 문서 검색 기반 TC 골격 생성..."))
        state.update(await _tc_generate_doc_search(state))

        print(_cyan("[2/3] tv_codebase_aware — 코드베이스/DB 기반 given/when/then/values 채움..."))
        state.update(await _tv_generate_codebase_aware(state))

        print(_cyan("[3/3] 시나리오 저장..."))
        state.update(await _save_experiment_scenarios(state))

    scenarios = state.get("scenarios") or []
    tc_count = sum(len(s.get("test_cases") or []) for s in scenarios)
    print(_bold(_green(f"\n완료 — TS {len(scenarios)}개 / TC {tc_count}개\n")))

    for ts in scenarios:
        if not ts.get("test_cases"):
            continue
        print(f"{_bold(ts['ts_id'])} {ts['name']}")
        doc_search = ts.get("doc_search") or {}
        print(f"  doc_search.query: {doc_search.get('query', '')}")
        print(f"  doc_search.sources: {doc_search.get('sources', [])}")
        for tc in ts["test_cases"]:
            _print_tc(tc)
        print()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(scenarios, ensure_ascii=False, indent=2), encoding="utf-8")
    print(_cyan(f"전체 JSON → {out_path}"))


if __name__ == "__main__":
    asyncio.run(_main())
