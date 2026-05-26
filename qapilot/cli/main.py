"""Typer CLI 메인.

qapilot init / generate / test / explain / spec import / rescan / rule add / ui / sync 명령어를 제공한다.

핸들러는 점진적으로 `cli/commands/` 하위로 분리됨. 본 파일은 Typer 앱 정의 +
서브커맨드 등록 + 아직 분리되지 않은 명령(generate/test/explain/rescan/sync/ui/spec/rule)
의 핸들러를 담는다.

담당: A
Created: 2026-05-07
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Optional

import typer

from qapilot.cli._branding import BRAND_PURPLE, console
from qapilot.cli.api_client import ApiClient
from qapilot.cli.commands.init import init
from qapilot.cli.sync import sync_local_to_server
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.logger import setup_logger
from qapilot.shared.schemas import RunOptions

app = typer.Typer(help="QApilot — AI 기반 QA 자동화 시스템")

@app.callback(invoke_without_command=True)
def _init_cli(ctx: typer.Context) -> None:
    """모든 CLI 명령 실행 전에 로깅을 초기화한다.

    인자 없이 실행 시 인터랙티브 셸 (REPL) 진입.
    """
    setup_logger()
    if ctx.invoked_subcommand is None:
        from qapilot.cli.repl import run_repl

        run_repl(app)
        raise typer.Exit()


# `qapilot init` — 핸들러는 cli/commands/init.py 에 정의됨.
app.command()(init)


generate_app = typer.Typer(help="시나리오 및 테스트 코드 자동 생성 (Layer 1)")
app.add_typer(generate_app, name="generate")

@generate_app.command("scenarios")
def generate_scenarios(
    affected: bool = typer.Option(False, "--affected", help="Git 변경분만 생성"),
    sync: bool = typer.Option(True, "--sync/--no-sync", help="완료 후 서버와 동기화"),
) -> None:
    """시나리오 자동 생성. 생성 후 사용자가 대시보드에서 검토·수정한다."""
    options: RunOptions = {
        "command": "generate_scenarios",
        "trigger": "code_change" if affected else "init",
        "user_input": None,
        "scenario_ids": None,
        "filter": None,
        "tags": None,
    }
    console.print(f"[bold {BRAND_PURPLE}]시나리오 생성을 시작합니다 (Layer 1)...[/bold {BRAND_PURPLE}]")
    result = asyncio.run(run_pipeline(options, Path.cwd() / ".qapilot"))
    console.print(f"[bold blue]시나리오 생성 완료! 대시보드(qapilot ui)에서 검토 후 'qapilot generate code'를 실행하세요. (상태: {result['status']})[/bold blue]")
    
    if sync:
        asyncio.run(sync_local_to_server())
    else:
        console.print("생성된 시나리오가 서버로 동기화될 준비가 되었습니다.")


@generate_app.command("code")
def generate_code(
    case: Optional[str] = typer.Option(None, "--case", help="특정 시나리오 ID"),
    sync: bool = typer.Option(True, "--sync/--no-sync", help="완료 후 서버와 동기화"),
) -> None:
    """저장된 시나리오를 기반으로 테스트 코드 자동 생성."""
    options: RunOptions = {
        "command": "generate_code",
        "trigger": None,
        "user_input": None,
        "scenario_ids": [case] if case else None,
        "filter": None,
        "tags": None,
    }
    target = f"시나리오 {case}" if case else "저장된 전체 시나리오"
    console.print(f"[bold {BRAND_PURPLE}]{target} 기반 테스트 코드 생성을 시작합니다...[/bold {BRAND_PURPLE}]")
    result = asyncio.run(run_pipeline(options, Path.cwd() / ".qapilot"))
    console.print(f"[bold blue]테스트 코드 생성 완료! (상태: {result['status']})[/bold blue]")
    
    if sync:
        asyncio.run(sync_local_to_server())
    else:
        console.print("생성된 코드가 서버로 동기화될 준비가 되었습니다.")


@app.command()
def test(
    case: Optional[str] = typer.Option(None, "--case", help="특정 시나리오 ID"),
    failed: bool = typer.Option(False, "--failed", help="이전 실패 건만"),
    affected: bool = typer.Option(False, "--affected", help="Git 변경분 영향만"),
    tag: Optional[str] = typer.Option(None, "--tag", help="태그 필터"),
) -> None:
    """테스트 실행 (Layer 2~3)."""
    if failed:
        filter_opt = "failed"
    elif affected:
        filter_opt = "affected"
    else:
        filter_opt = "all"

    options: RunOptions = {
        "command": "test",
        "trigger": None,
        "user_input": None,
        "scenario_ids": [case] if case else None,
        "filter": filter_opt,
        "tags": [tag] if tag else None,
    }
    console.print(f"[bold {BRAND_PURPLE}]로컬 테스트를 실행합니다 (필터: {filter_opt})...[/bold {BRAND_PURPLE}]")
    result = asyncio.run(run_pipeline(options, Path.cwd() / ".qapilot"))
    console.print(f"[bold blue]테스트 실행 완료! (상태: {result['status']})[/bold blue]")


@app.command()
def explain(defect_id: str) -> None:
    """결함 원인 분석."""
    from qapilot.shared.schemas import AgentInput
    from qapilot.agents.root_cause_agent import RootCauseAgent

    console.print(f"[bold {BRAND_PURPLE}]결함({defect_id}) 원인 분석을 시작합니다...[/bold {BRAND_PURPLE}]")
    
    agent = RootCauseAgent()
    try:
        result = asyncio.run(
            agent.run(
                AgentInput(
                    trace_id=str(uuid.uuid4()),
                    context={},
                    params={"tc_id": defect_id},
                )
            )
        )
        candidates = result.result.get("root_causes", [])
        if candidates:
            for c in candidates:
                console.print(f"- [bold yellow]{c.get('cause', '알 수 없음')}[/bold yellow] (신뢰도: {c.get('confidence', 0)})")
        else:
            console.print("[yellow]원인 후보를 찾지 못했습니다.[/yellow]")
            
        console.print("[bold blue]분석 완료![/bold blue]")
    except Exception as e:
        console.print(f"[red]분석 중 오류 발생: {e}[/red]")


@app.command()
def rescan() -> None:
    """코드 인덱스 재생성."""
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.codebase_scanner_tool import CodebaseScannerTool

    console.print(f"[bold {BRAND_PURPLE}]로컬 코드 인덱스 재생성을 시작합니다...[/bold {BRAND_PURPLE}]")
    
    tool = CodebaseScannerTool()
    try:
        result = asyncio.run(
            tool.run(
                ToolInput(
                    trace_id=str(uuid.uuid4()),
                    params={"trigger": "code_change"},
                )
            )
        )
        r = result.result.get("scan_result", {})
        files_cnt = len(r.get('files', []))
        endpoint_cnt = r.get('endpoint_count', 0)
        framework = r.get('framework', 'unknown')
        console.print(f"[bold blue]재생성 완료! (파일: {files_cnt}개, 엔드포인트: {endpoint_cnt}개, 프레임워크: {framework})[/bold blue]")
    except Exception as e:
        console.print(f"[red]재생성 중 오류 발생: {e}[/red]")


@app.command()
def sync() -> None:
    """로컬 산출물을 서버와 동기화."""
    console.print(f"[bold {BRAND_PURPLE}]서버 동기화를 시작합니다...[/bold {BRAND_PURPLE}]")
    asyncio.run(sync_local_to_server())


@app.command()
def ui(
    port: int = typer.Option(7860, help="웹 대시보드 포트"),
    host: str = typer.Option("127.0.0.1", help="바인딩 호스트"),
) -> None:
    """웹 대시보드 기동 (FastAPI)."""
    import uvicorn
    console.print(f"[bold {BRAND_PURPLE}]QApilot 로컬 대시보드를 시작합니다 (http://{host}:{port})...[/bold {BRAND_PURPLE}]")
    uvicorn.run("qapilot.api.main:app", host=host, port=port, reload=True)


# spec 서브커맨드
spec_app = typer.Typer(help="도메인 문서 관리")
app.add_typer(spec_app, name="spec")


@spec_app.command("import")
def spec_import(file_path: str) -> None:
    """도메인 문서를 파싱·청킹·임베딩하여 Qdrant 에 적재한다."""
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.domain_knowledge import DomainKnowledgeTool

    console.print(
        f"[bold {BRAND_PURPLE}]도메인 문서({file_path}) 벡터 임베딩을 시작합니다...[/bold {BRAND_PURPLE}]"
    )

    tool = DomainKnowledgeTool()
    result = asyncio.run(
        tool.run(
            ToolInput(
                trace_id=str(uuid.uuid4()),
                params={"action": "import", "file_path": file_path},
            )
        )
    )
    r = result.result
    typer.echo("임베딩 완료")
    typer.echo(f"  file          : {r['file']}")
    typer.echo(f"  chunks_total  : {r['chunks_total']}")
    typer.echo(f"  chunks_stored : {r['chunks_stored']}")
    typer.echo(f"  chunks_failed : {r['chunks_failed']}")


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
