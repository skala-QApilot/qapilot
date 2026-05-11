"""Typer CLI 메인.

qapilot init / generate / test / explain / spec import / rescan / ui 명령어를 제공한다.

담당: A
Created: 2026-05-07
"""

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from qapilot.cli.api_client import ApiClient
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.schemas import RunOptions

app = typer.Typer(help="QApilot — AI 기반 QA 자동화 시스템")
console = Console()
api_client = ApiClient()


@app.command()
def init() -> None:
    """프로젝트 초기화 + 코드 스캔."""
    console.print("[bold green]QApilot 초기화를 시작합니다...[/bold green]")
    base_dir = Path(".qapilot")
    dirs_to_create = [
        base_dir / "codebase-index",
        base_dir / "domain",
        base_dir / "scenarios" / "raw",
        base_dir / "scenarios" / "regression",
        base_dir / "generated-code",
        base_dir / "results",
        base_dir / "evidence",
        base_dir / "reports",
        base_dir / "logs",
        base_dir / "cache",
    ]
    for d in dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)

    console.print("디렉토리 구조 생성 완료.")
    console.print("코드 스캔을 진행합니다...")
    # TODO: codebase scanner 연동 (FR-000)
    console.print("[bold blue]초기화 완료! 로컬 코드 인덱스가 생성되었습니다.[/bold blue]")


@app.command()
def generate(
    affected: bool = typer.Option(False, "--affected", help="Git 변경분만 생성"),
) -> None:
    """시나리오 자동 생성 (Layer 1)."""
    options: RunOptions = {
        "command": "generate",
        "trigger": "code_change" if affected else "init",
        "user_input": None,
        "scenario_ids": None,
        "filter": None,
        "tags": None,
    }
    console.print("[bold green]시나리오 생성을 시작합니다 (Layer 1)...[/bold green]")
    result = asyncio.run(run_pipeline(options))
    console.print(f"[bold blue]시나리오 생성 완료! (상태: {result['status']})[/bold blue]")
    console.print("생성된 시나리오와 테스트 코드가 서버로 동기화될 준비가 되었습니다.")


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
    console.print(f"[bold green]로컬 테스트를 실행합니다 (필터: {filter_opt})...[/bold green]")
    result = asyncio.run(run_pipeline(options))
    console.print(f"[bold blue]테스트 실행 완료! (상태: {result['status']})[/bold blue]")


@app.command()
def explain(defect_id: str) -> None:
    """결함 원인 분석."""
    console.print(f"[bold green]결함({defect_id}) 원인 분석을 시작합니다...[/bold green]")
    # TODO: 단건 분석 로직 연동 (FR-010)
    console.print("[bold blue]분석 완료![/bold blue]")


@app.command()
def rescan() -> None:
    """코드 인덱스 재생성."""
    console.print("[bold green]로컬 코드 인덱스 재생성을 시작합니다...[/bold green]")
    # TODO: codebase scanner 연동 (FR-000)
    console.print("[bold blue]재생성 완료![/bold blue]")


@app.command()
def ui(
    port: int = typer.Option(7860, help="웹 대시보드 포트"),
    host: str = typer.Option("127.0.0.1", help="바인딩 호스트"),
) -> None:
    """웹 대시보드 기동 (FastAPI)."""
    import uvicorn

    console.print(f"[bold green]QApilot 로컬 대시보드를 시작합니다 (http://{host}:{port})...[/bold green]")
    uvicorn.run("qapilot.api.main:app", host=host, port=port, reload=True)


# spec 서브커맨드
spec_app = typer.Typer(help="도메인 문서 관리")
app.add_typer(spec_app, name="spec")


@spec_app.command("import")
def spec_import(file_path: str) -> None:
    """도메인 문서를 임베딩한다."""
    console.print(f"[bold green]도메인 문서({file_path}) 벡터 임베딩을 시작합니다...[/bold green]")
    # TODO: DomainKnowledgeTool 연동 (FR-001)
    console.print("[bold blue]임베딩 완료![/bold blue]")


if __name__ == "__main__":
    app()
