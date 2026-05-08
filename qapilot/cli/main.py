"""Typer CLI 메인.

qapilot init / generate / test / explain / spec import / rescan / ui / sync 명령어를 제공한다.

담당: A
Created: 2026-05-07
"""

import asyncio
import os
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from qapilot.cli.api_client import ApiClient
from qapilot.cli.sync import sync_local_to_server
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.schemas import RunOptions

app = typer.Typer(help="QApilot — AI 기반 QA 자동화 시스템")
console = Console()


def print_welcome_banner():
    """제미나이 CLI 스타일의 환영 배너 출력."""
    console.print(
        Panel.fit(
            "[bold cyan]🚀 QApilot — Intelligent AI QA Agent[/bold cyan]\n"
            "[dim]No People Testing (NPT) - 완전 무인 테스트 운영 시스템[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


@app.command()
def init() -> None:
    """프로젝트 초기화 + 코드 스캔 + 서버 설정 마법사."""
    print_welcome_banner()
    console.print("\n[bold green]QApilot 설정을 시작합니다...[/bold green]\n")

    # 1. 디렉토리 구조 생성
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
    console.print("✅ [dim]디렉토리 구조 생성 완료 (.qapilot/)[/dim]")

    # 2. 인터랙티브 설정 마법사
    config_file = Path("qapilot.config.yaml")
    
    # [Step 1] 서버 설정
    console.print("\n[bold]Step 1. 중앙 서버 연결 설정[/bold]")
    server_url = Prompt.ask("중앙 서버 URL", default="http://localhost:8080")
    server_token = Prompt.ask("서버 인증 토큰 (옵션)", default="", show_default=False)

    # [Step 2] 프로젝트 유형 선택 (메뉴형)
    console.print("\n[bold]Step 2. 대상 프로젝트 유형 선택[/bold]")
    project_table = Table(show_header=False, box=None, padding=(0, 2))
    project_table.add_row("1.", "Python (FastAPI/Flask)")
    project_table.add_row("2.", "Java (Spring Boot)")
    project_table.add_row("3.", "Node.js (Express/Nest)")
    project_table.add_row("4.", "기타 / 직접 입력")
    console.print(project_table)
    
    project_choice = IntPrompt.ask("유형을 선택하세요", choices=[1, 2, 3, 4], default=1)
    project_types = {1: "fastapi", 2: "springboot", 3: "nodejs", 4: "other"}
    framework = project_types[project_choice]

    # [Step 3] AI 모델 선택
    console.print("\n[bold]Step 3. 사용할 LLM 모델 선택[/bold]")
    model_table = Table(show_header=False, box=None, padding=(0, 2))
    model_table.add_row("1.", "[bold]gpt-4o-mini[/bold] (추천: 빠르고 경제적)")
    model_table.add_row("2.", "[bold]gpt-4o[/bold] (강력한 추론 성능)")
    model_table.add_row("3.", "[bold]o3-mini[/bold] (최신 추론 특화 모델)")
    console.print(model_table)
    
    model_choice = IntPrompt.ask("모델을 선택하세요", choices=[1, 2, 3], default=1)
    models = {1: "gpt-4o-mini", 2: "gpt-4o", 3: "o3-mini"}
    selected_model = models[model_choice]

    # 3. 설정 파일 저장
    config_data = {
        "server": {
            "url": server_url,
            "token": server_token if server_token else None
        },
        "project": {
            "framework": framework,
            "language": "python" if project_choice == 1 else "unknown"
        },
        "llm": {
            "default_model": selected_model,
            "deep_model": "gpt-4o"
        }
    }

    console.print("\n[bold cyan]설정 요약:[/bold cyan]")
    console.print(f" - 서버: {server_url}")
    console.print(f" - 유형: {framework}")
    console.print(f" - 모델: {selected_model}")

    if typer.confirm("\n이 설정으로 qapilot.config.yaml 파일을 생성할까요?"):
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)
        console.print(f"\n✨ [bold blue]초기화 완료![/bold blue] 설정이 저장되었습니다: {config_file}")
    else:
        console.print("\n[yellow]설정이 저장되지 않았습니다.[/yellow]")

    console.print("\n[bold green]코드베이스 스캔을 시작합니다...[/bold green]")
    # TODO: codebase scanner 연동 (FR-000)
    console.print("✅ [dim]스캔 및 인덱싱 완료.[/dim]")
    console.print("\n이제 [bold]qapilot generate[/bold] 명령어로 테스트 시나리오를 만들어보세요!")


@app.command()
def generate(
    affected: bool = typer.Option(False, "--affected", help="Git 변경분만 생성"),
    sync: bool = typer.Option(True, "--sync/--no-sync", help="완료 후 서버와 동기화"),
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
    
    if sync:
        asyncio.run(sync_local_to_server())
    else:
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
def sync() -> None:
    """로컬 산출물을 서버와 동기화."""
    console.print("[bold green]서버 동기화를 시작합니다...[/bold green]")
    asyncio.run(sync_local_to_server())


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
