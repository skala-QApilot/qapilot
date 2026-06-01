"""NaturalLanguageAgent 수동 통합 테스트 (#182).

터미널에서 자연어 쿼리를 입력하면 query_status / requirements / feedback을 출력한다.
세션 시뮬레이션: insufficient 응답 후 follow-up 입력까지 이어서 확인 가능.

실행:
    python tests/manual_test_natural_lang_182.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qapilot.agents.natural_language_agent import NaturalLanguageAgent
from qapilot.orchestrator.pipeline import _resolve_scenario_targets
from qapilot.shared.config import load_config
from qapilot.shared.schemas import AgentInput
from qapilot.shared.session_store import (
    get_last_exchange,
    new_session_id,
    save_exchange,
)

# ── 색상 출력 헬퍼 ──────────────────────────────────────────────────────────────

def _green(s: str) -> str:  return f"\033[92m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[93m{s}\033[0m"
def _red(s: str) -> str:    return f"\033[91m{s}\033[0m"
def _cyan(s: str) -> str:   return f"\033[96m{s}\033[0m"
def _bold(s: str) -> str:   return f"\033[1m{s}\033[0m"

# ── 더미 컨텍스트 ───────────────────────────────────────────────────────────────

_MOCK_SCAN_RESULT = {
    "framework": "fastapi",
    "language": "python",
    "endpoint_count": 5,
    "files": [
        {"path": "api/auth.py", "endpoints": [
            {"method": "POST", "path": "/api/auth/login"},
            {"method": "POST", "path": "/api/auth/register"},
        ]},
        {"path": "api/payment.py", "endpoints": [
            {"method": "POST", "path": "/api/payment/charge"},
            {"method": "POST", "path": "/api/payment/cancel"},
        ]},
        {"path": "api/order.py", "endpoints": [
            {"method": "GET",  "path": "/api/orders"},
        ]},
    ],
}

_MOCK_EXISTING_SCENARIOS = [
    {
        "ts_id": "TS-001",
        "title": "로그인 정상 케이스",
        "test_cases": [
            {"tc_id": "TC-001", "title": "이메일/비밀번호 정상 로그인"},
            {"tc_id": "TC-002", "title": "잘못된 비밀번호 오류"},
        ],
    },
    {
        "ts_id": "TS-002",
        "title": "회원가입 플로우",
        "test_cases": [
            {"tc_id": "TC-003", "title": "정상 회원가입"},
            {"tc_id": "TC-004", "title": "중복 이메일 오류"},
        ],
    },
    {
        "ts_id": "TS-003",
        "title": "결제 처리",
        "test_cases": [
            {"tc_id": "TC-005", "title": "카드 정상 결제"},
            {"tc_id": "TC-006", "title": "결제 취소"},
        ],
    },
]


def _print_result(result: dict) -> None:
    status = result.get("query_status", "sufficient")

    print()
    if status == "sufficient":
        print(_bold(_green("✅ sufficient — 시나리오 생성 가능")))
        reqs = result.get("requirements", [])
        print(f"   요구사항 {len(reqs)}개 추출됨\n")
        for r in reqs:
            action = r.get("action_type", "create")
            level  = r.get("target_level", "ts")
            ts_id  = r.get("target_ts_id") or "-"
            tc_id  = r.get("target_tc_id") or "-"
            print(f"   {_cyan(r['req_id'])} [{r['priority']}] {r['content']}")
            print(f"         domain: {r['domain_area']} | action: {action} | level: {level}"
                  f" | target_ts: {ts_id} | target_tc: {tc_id}")

    elif status == "insufficient":
        print(_bold(_yellow("⚠️  insufficient — 추가 정보 필요")))
        print(f"   피드백: {result.get('query_feedback', '')}")

    else:
        print(_bold(_red("❌ rejected — QA와 무관한 질의")))
        print(f"   안내: {result.get('query_feedback', '')}")

    print()


async def _run_query(
    agent: NaturalLanguageAgent,
    user_input: str,
    session_id: str,
    qapilot_dir: Path,
) -> str:
    """쿼리를 실행하고 query_status를 반환한다."""
    last_exchange = get_last_exchange(str(qapilot_dir), session_id)
    history = [last_exchange] if last_exchange else []

    output = await agent.run(
        AgentInput(
            trace_id="manual-test",
            context={
                "scan_result": _MOCK_SCAN_RESULT,
                "domain_rules": [],
                "conversation_history": history,
                "existing_scenarios": _MOCK_EXISTING_SCENARIOS,
            },
            params={"trigger": "natural_lang", "user_input": user_input},
        )
    )

    query_status = output.result.get("query_status", "sufficient")
    query_feedback = output.result.get("query_feedback")

    # 임베딩 매칭 레이어: action_type: update인 항목에 target_ts_id / target_tc_id 주입
    result = dict(output.result)
    if query_status == "sufficient":
        try:
            matched = await _resolve_scenario_targets(
                result.get("requirements", []),
                _MOCK_EXISTING_SCENARIOS,
            )
            result["requirements"] = matched
        except Exception as e:
            print(_yellow(f"  [매칭 레이어 오류: {e}]"))

    _print_result(result)
    save_exchange(str(qapilot_dir), session_id, user_input, query_status, query_feedback)
    return query_status


async def _main() -> None:
    print(_bold("\n=== NaturalLanguageAgent 수동 테스트 (#182) ==="))
    print("코드베이스: FastAPI (로그인/회원가입/결제/주문)")
    print("기존 시나리오: TS-001(로그인) / TS-002(회원가입) / TS-003(결제)")
    print("종료: Ctrl+C 또는 빈 입력 후 Enter\n")

    agent = NaturalLanguageAgent(config=load_config(), trace_id="manual-test")
    session_id = new_session_id()
    print(f"세션 ID: {_cyan(session_id)}\n")

    with tempfile.TemporaryDirectory() as tmp_dir:
        qapilot_dir = Path(tmp_dir)

        while True:
            try:
                user_input = input(_bold("쿼리 입력 > ")).strip()
            except (KeyboardInterrupt, EOFError):
                print("\n종료합니다.")
                break

            if not user_input:
                print("종료합니다.")
                break

            if len(user_input) < 2:
                print(_red("  → 2자 이상 입력해 주세요."))
                continue

            print(_cyan(f"\n  [세션: {session_id}] 처리 중..."))
            try:
                await _run_query(agent, user_input, session_id, qapilot_dir)
            except Exception as e:
                print(_red(f"  오류 발생: {e}\n"))


if __name__ == "__main__":
    asyncio.run(_main())
