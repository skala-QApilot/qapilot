"""Typer CLI 메인.

qapilot init / generate / test / explain / spec import / rescan / rule add 명령어를 제공한다.

담당: A
Created: 2026-05-07
"""

import asyncio
import uuid
from typing import Optional

import typer

app = typer.Typer(help="QApilot — AI 기반 QA 자동화 시스템")


@app.command()
def init() -> None:
    """프로젝트 초기화 + 코드 스캔."""
    raise NotImplementedError


@app.command()
def generate(
    affected: bool = typer.Option(False, "--affected", help="Git 변경분만 생성"),
) -> None:
    """시나리오 자동 생성 (Layer 1)."""
    raise NotImplementedError


@app.command()
def test(
    case: Optional[str] = typer.Option(None, "--case", help="특정 시나리오 ID"),
    failed: bool = typer.Option(False, "--failed", help="이전 실패 건만"),
    affected: bool = typer.Option(False, "--affected", help="Git 변경분 영향만"),
    tag: Optional[str] = typer.Option(None, "--tag", help="태그 필터"),
) -> None:
    """테스트 실행 (Layer 2~3)."""
    raise NotImplementedError


@app.command()
def explain(defect_id: str) -> None:
    """결함 원인 분석."""
    raise NotImplementedError


@app.command()
def rescan() -> None:
    """코드 인덱스 재생성."""
    raise NotImplementedError


# spec 서브커맨드
spec_app = typer.Typer(help="도메인 문서 관리")
app.add_typer(spec_app, name="spec")


@spec_app.command("import")
def spec_import(file_path: str) -> None:
    """도메인 문서를 임베딩한다."""
    raise NotImplementedError


# rule 서브커맨드
rule_app = typer.Typer(help="도메인 규칙 관리")
app.add_typer(rule_app, name="rule")


@rule_app.command("add")
def rule_add(
    rule: str = typer.Argument(..., help="추가할 자연어 규칙 문장"),
    category: str = typer.Option("custom_rule", "--category", "-c", help="규칙 카테고리"),
) -> None:
    """자연어 규칙을 도메인 지식 벡터 DB에 추가한다."""
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.domain_knowledge import DomainKnowledgeTool

    tool = DomainKnowledgeTool()
    result = asyncio.run(
        tool.run(
            ToolInput(
                trace_id=str(uuid.uuid4()),
                params={"action": "add_rule", "rule": rule, "category": category},
            )
        )
    )
    r = result.result
    typer.echo(f"규칙 추가 완료")
    typer.echo(f"  rule_id : {r['rule_id']}")
    typer.echo(f"  category: {r['category']}")
    typer.echo(f"  content : {r['content']}")


if __name__ == "__main__":
    app()
