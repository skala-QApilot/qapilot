"""ScenarioGeneratorAgent 실행 확인 스크립트.

NOVA Self-Care PRD 기반 요구사항·도메인 규칙·코드베이스 정보를 입력으로
실제 LLM을 호출하여 시나리오를 생성하고 결과를 출력한다.

실행:
    cd qapilot
    python tests/run_scenario_generator.py
"""

import asyncio
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from qapilot.agents.scenario_generator import ScenarioGeneratorAgent
from qapilot.shared.schemas import AgentInput

console = Console()


# ── 입력 데이터 ────────────────────────────────────────────────────────────────

REQUIREMENTS = [
    {
        "req_id": "REQ-001",
        "req_type": "functional",
        "content": "이메일과 비밀번호(8~100자)로 회원가입 가능하며, 이메일은 시스템 전체 고유여야 한다",
        "priority": "high",
        "domain_area": "인증",
    },
    {
        "req_id": "REQ-002",
        "req_type": "functional",
        "content": "만 19세 미만 가입자는 법정대리인 동의(guardian_consent) 플래그가 필수다",
        "priority": "high",
        "domain_area": "인증",
    },
    {
        "req_id": "REQ-003",
        "req_type": "functional",
        "content": "로그인 성공 시 JWT 토큰을 발급하며, 이메일 또는 비밀번호 불일치 시 401을 반환한다",
        "priority": "high",
        "domain_area": "인증",
    },
    {
        "req_id": "REQ-004",
        "req_type": "functional",
        "content": "회선당 활성 요금제는 1건만 보유 가능하다. 이미 활성 회선이 있는 고객의 신규 가입 요청은 거부된다",
        "priority": "high",
        "domain_area": "주문",
    },
    {
        "req_id": "REQ-005",
        "req_type": "functional",
        "content": "만 19세 미만 가입자는 is_minor_only=true인 청소년 안심 요금제만 신청·변경 가능하다",
        "priority": "high",
        "domain_area": "주문",
    },
    {
        "req_id": "REQ-006",
        "req_type": "functional",
        "content": "요금제 변경 예약 시 적용일(scheduled_date)은 반드시 오늘 이후여야 한다",
        "priority": "medium",
        "domain_area": "주문",
    },
    {
        "req_id": "REQ-007",
        "req_type": "functional",
        "content": "타 고객의 회선(오더) 조회 시 존재 누설 방지를 위해 404를 반환한다",
        "priority": "high",
        "domain_area": "인증",
    },
    {
        "req_id": "REQ-008",
        "req_type": "functional",
        "content": "청구 개요에는 현재 월 요금, 다음 결제일, 납부 방법, 납부 현황이 포함된다",
        "priority": "medium",
        "domain_area": "청구",
    },
]

DOMAIN_RULES = [
    {
        "rule_id": "RULE-001",
        "source": "PRD_v3.0.md (FR-AUTH-01)",
        "category": "인증",
        "content": "비밀번호는 평문 저장이 금지된다. 반드시 해시 처리 후 저장해야 한다",
        "similarity_score": 0.97,
    },
    {
        "rule_id": "RULE-002",
        "source": "PRD_v3.0.md (FR-AUTH-01)",
        "category": "인증",
        "content": "이메일은 시스템 전체에서 고유해야 하며, 중복 이메일로 가입 요청 시 409 Conflict를 반환한다",
        "similarity_score": 0.95,
    },
    {
        "rule_id": "RULE-003",
        "source": "PRD_v3.0.md (FR-ORDER-01)",
        "category": "주문",
        "content": "회선당 활성 요금제는 1건만 허용된다. 이미 CONFIRMED 상태의 오더가 있는 고객의 신규 가입은 거부된다",
        "similarity_score": 0.96,
    },
    {
        "rule_id": "RULE-004",
        "source": "PRD_v3.0.md (FR-ORDER-01/02/03)",
        "category": "주문",
        "content": "만 19세 미만 가입자(birth_date 기준)는 is_minor_only=true 요금제만 신청·변경 가능하다. 일반 요금제 요청 시 422를 반환한다",
        "similarity_score": 0.94,
    },
    {
        "rule_id": "RULE-005",
        "source": "PRD_v3.0.md (§2.2)",
        "category": "인증",
        "content": "모든 인증된 API는 서버에서 customer_id == 인증된 본인을 강제하며, 타 고객 자원 요청은 404 Not Found로 응답한다",
        "similarity_score": 0.93,
    },
    {
        "rule_id": "RULE-006",
        "source": "PRD_v3.0.md (FR-ORDER-03)",
        "category": "주문",
        "content": "요금제 변경 예약의 적용일은 오늘 이후여야 한다. 오늘 이하의 날짜는 422로 거부된다",
        "similarity_score": 0.91,
    },
    {
        "rule_id": "RULE-007",
        "source": "요금제정책.md (§5.3)",
        "category": "주문",
        "content": "비활성(is_active=false) 요금제에 대한 신청·변경 요청은 422로 거부된다",
        "similarity_score": 0.90,
    },
]

SCAN_RESULT = {
    "framework": "FastAPI",
    "language": "Python",
    "endpoint_count": 15,
    "files": [
        {
            "path": "app/routers/auth.py",
            "language": "Python",
            "endpoints": [
                {"method": "POST", "path": "/api/auth/signup"},
                {"method": "POST", "path": "/api/auth/login"},
                {"method": "GET", "path": "/api/auth/me"},
            ],
        },
        {
            "path": "app/routers/plans.py",
            "language": "Python",
            "endpoints": [
                {"method": "GET", "path": "/api/plans"},
                {"method": "GET", "path": "/api/plans/{plan_id}"},
            ],
        },
        {
            "path": "app/routers/orders.py",
            "language": "Python",
            "endpoints": [
                {"method": "POST", "path": "/api/orders"},
                {"method": "GET", "path": "/api/orders"},
                {"method": "GET", "path": "/api/orders/{order_id}"},
                {"method": "PATCH", "path": "/api/orders/{order_id}/cancel"},
                {"method": "PATCH", "path": "/api/orders/{order_id}/change-plan"},
                {"method": "PUT", "path": "/api/orders/{order_id}/scheduled-change"},
                {"method": "GET", "path": "/api/orders/{order_id}/benefits"},
                {"method": "POST", "path": "/api/orders/{order_id}/benefits/{benefit_id}/toggle"},
            ],
        },
        {
            "path": "app/routers/billing.py",
            "language": "Python",
            "endpoints": [
                {"method": "GET", "path": "/api/billing/overview"},
                {"method": "GET", "path": "/api/billing/payment-methods"},
                {"method": "PUT", "path": "/api/billing/payment-method"},
            ],
        },
    ],
}


# ── 출력 헬퍼 ───────────────────────────────────────────────────────────────────

def print_scenario(scenario: dict, idx: int) -> None:
    ts_id = scenario["ts_id"]
    console.print(f"\n[bold cyan]{ts_id}[/bold cyan] {scenario['name']}")
    if scenario.get("description"):
        console.print(f"  [dim]{scenario['description']}[/dim]")

    tcs = scenario.get("test_cases", [])
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta", padding=(0, 1))
    table.add_column("TC ID", style="cyan", no_wrap=True)
    table.add_column("이름")
    table.add_column("Given")
    table.add_column("When")
    table.add_column("Then")
    table.add_column("Tags", style="yellow")

    for tc in tcs:
        table.add_row(
            tc["tc_id"],
            tc["name"],
            tc["given"][:50] + "…" if len(tc["given"]) > 50 else tc["given"],
            tc["when"][:50] + "…" if len(tc["when"]) > 50 else tc["when"],
            tc["then"][:60] + "…" if len(tc["then"]) > 60 else tc["then"],
            ", ".join(tc.get("tags", [])),
        )

    console.print(table)


def print_mismatches(mismatches: list[dict]) -> None:
    if not mismatches:
        console.print("[green]PRD-코드 불일치 없음[/green]")
        return
    console.print("[bold red]⚠️  PRD-코드 불일치 탐지[/bold red]")
    for m in mismatches:
        console.print(f"  [{m['req_id']}] {m['note']}")


async def main() -> None:
    console.print(Panel.fit("[bold]ScenarioGeneratorAgent 실행[/bold]\nNOVA Self-Care 기반", style="blue"))

    agent = ScenarioGeneratorAgent(trace_id="run-check-001")

    agent_input = AgentInput(
        trace_id="run-check-001",
        context={
            "requirements": REQUIREMENTS,
            # domain_rules 미제공 → agent가 Qdrant에서 자동 검색
            "scan_result": SCAN_RESULT,
        },
        params={"trigger": "init", "affected_only": False},
    )

    console.print(f"  요구사항 {len(REQUIREMENTS)}개 / 도메인 규칙 Qdrant 자동 검색 / 엔드포인트 {SCAN_RESULT['endpoint_count']}개")
    console.print("  LLM 호출 중…\n")

    output = await agent.run(agent_input)

    scenarios = output.result["scenarios"]
    mismatches = output.result.get("prd_code_mismatches", [])
    tc_count = sum(len(s["test_cases"]) for s in scenarios)

    console.print(Panel.fit(
        f"TS {len(scenarios)}개  /  TC {tc_count}개  /  confidence [bold green]{output.confidence:.2f}[/bold green]\n"
        f"토큰 {output.metadata.tokens_used}  /  소요 {output.metadata.duration_sec}s  /  재시도 {output.metadata.retry_count}회",
        title="결과 요약",
        style="green",
    ))

    for i, scenario in enumerate(scenarios):
        print_scenario(scenario, i)

    console.print()
    print_mismatches(mismatches)

    out_path = Path("tests/scenario_output.json")
    out_path.write_text(json.dumps(output.result, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"\n[dim]전체 JSON → {out_path}[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
