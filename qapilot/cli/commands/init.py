"""qapilot init 명령 핸들러.

프로젝트 초기화 + 인터랙티브 설정 마법사 + 코드베이스 스캔 + 서버 등록.

담당: A
Created: 2026-05-12
"""

import asyncio
import uuid
import webbrowser
from pathlib import Path

import typer
import yaml
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from qapilot.cli._branding import BRAND_PURPLE, console
from qapilot.cli.api_client import ApiClient
from qapilot.cli.banner import print_welcome_banner
from qapilot.shared.project_slug import project_slug_from_path
from qapilot.shared.schemas import ToolInput
from qapilot.tools.codebase_scanner_tool import CodebaseScannerTool


def _build_project_identity(project_root: Path) -> tuple[str, str]:
    """Derive the project slug and display name from the current directory."""
    display_name = project_root.name or "project"
    return project_slug_from_path(project_root), display_name


def _open_dashboard(url: str) -> None:
    """Open the dashboard URL if possible, but never fail the init flow."""
    try:
        if webbrowser.open(url, new=2):
            console.print(f"[dim]브라우저를 열었습니다: {url}[/dim]")
        else:
            console.print(f"[yellow]브라우저 자동 열기에 실패했습니다. URL: {url}[/yellow]")
    except Exception:
        console.print(f"[yellow]브라우저 자동 열기에 실패했습니다. URL: {url}[/yellow]")


def init() -> None:
    """프로젝트 초기화 + 코드 스캔 + 서버 등록."""
    print_welcome_banner()
    console.print(f"\n[bold {BRAND_PURPLE}]QApilot 설정을 시작합니다...[/bold {BRAND_PURPLE}]\n")

    project_root = Path.cwd().resolve()
    project_slug, display_name = _build_project_identity(project_root)

    # 1. 디렉토리 구조 생성
    base_dir = project_root / ".qapilot"
    index_dir = base_dir / "codebase-index"
    dirs_to_create = [
        index_dir,
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
    console.print("[dim]디렉토리 구조 생성 완료 (.qapilot/)[/dim]")

    # 2. 인터랙티브 설정 마법사
    config_file = project_root / "qapilot.config.yaml"

    # [Step 1] 서버 설정
    console.print(f"\n[bold {BRAND_PURPLE}]Step 1. 중앙 서버 연결 설정[/bold {BRAND_PURPLE}]")
    server_url = Prompt.ask("중앙 서버 URL", default="http://localhost:8080")
    server_token = Prompt.ask("서버 인증 토큰 (옵션)", default="", show_default=False)

    # [Step 2] 프로젝트 유형 선택 (메뉴형)
    console.print(f"\n[bold {BRAND_PURPLE}]Step 2. 대상 프로젝트 유형 선택[/bold {BRAND_PURPLE}]")
    project_table = Table(show_header=False, box=None, padding=(0, 2))
    project_table.add_row(f"[{BRAND_PURPLE}]1.[/{BRAND_PURPLE}]", "Python (FastAPI/Flask)")
    project_table.add_row(f"[{BRAND_PURPLE}]2.[/{BRAND_PURPLE}]", "Java (Spring Boot)")
    project_table.add_row(f"[{BRAND_PURPLE}]3.[/{BRAND_PURPLE}]", "Node.js (Express/Nest)")
    project_table.add_row(f"[{BRAND_PURPLE}]4.[/{BRAND_PURPLE}]", "기타 / 직접 입력")
    console.print(project_table)

    project_choice = IntPrompt.ask("유형을 선택하세요", choices=["1", "2", "3", "4"], default=1)
    project_types = {1: "fastapi", 2: "springboot", 3: "nodejs", 4: "other"}
    framework = project_types[project_choice]
    language = {1: "python", 2: "java", 3: "javascript", 4: "unknown"}[project_choice]

    # [Step 3] AI 모델 선택
    console.print(f"\n[bold {BRAND_PURPLE}]Step 3. 사용할 LLM 모델 선택[/bold {BRAND_PURPLE}]")
    model_table = Table(show_header=False, box=None, padding=(0, 2))
    model_table.add_row(f"[{BRAND_PURPLE}]1.[/{BRAND_PURPLE}]", "[bold]gpt-4o-mini[/bold] (추천: 빠르고 경제적)")
    model_table.add_row(f"[{BRAND_PURPLE}]2.[/{BRAND_PURPLE}]", "[bold]gpt-4o[/bold] (강력한 추론 성능)")
    model_table.add_row(f"[{BRAND_PURPLE}]3.[/{BRAND_PURPLE}]", "[bold]o3-mini[/bold] (최신 추론 특화 모델)")
    console.print(model_table)

    model_choice = IntPrompt.ask("모델을 선택하세요", choices=["1", "2", "3"], default=1)
    models = {1: "gpt-4o-mini", 2: "gpt-4o", 3: "o3-mini"}
    selected_model = models[model_choice]

    # [Step 4] 테스트 환경 사전 셋업 (Playwright Chromium) — 선택형
    # 거절해도 qapilot test 시점에 자동 처리됨 (lazy)
    console.print(
        f"\n[bold {BRAND_PURPLE}]Step 4. 테스트 환경 사전 셋업 (선택)[/bold {BRAND_PURPLE}]"
    )
    from qapilot.cli._ensure_browser import ensure_chromium

    ensure_chromium(prompt=True, console=console)

    config_data = {
        "server": {
            "url": server_url,
            "token": server_token if server_token else None,
        },
        "project": {
            "name": display_name,
            "root": str(project_root),
            "repo_path": str(project_root),
            "framework": framework,
            "language": language,
        },
        "llm": {
            "default_model": selected_model,
            "deep_model": "gpt-4o",
        },
    }

    console.print(f"\n[bold cyan]설정 요약:[/bold cyan]")
    console.print(f" - 프로젝트: {display_name}")
    console.print(f" - slug: {project_slug}")
    console.print(f" - 서버: {server_url}")
    console.print(f" - 유형: {framework}")
    console.print(f" - 모델: {selected_model}")

    if typer.confirm("\n이 설정으로 qapilot.config.yaml 파일을 생성할까요?"):
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)
        console.print(
            f"\n✨ [bold {BRAND_PURPLE}]초기화 완료![/bold {BRAND_PURPLE}] "
            f"설정이 저장되었습니다: {config_file}"
        )
    else:
        console.print("\n[yellow]설정이 저장되지 않았습니다.[/yellow]")

    console.print(f"\n[bold green]코드베이스 스캔을 시작합니다...[/bold green]")
    tool = CodebaseScannerTool()
    dashboard_url = f"{server_url.rstrip('/')}/{project_slug}"
    setup_url = f"{dashboard_url}/setup"
    try:
        result = asyncio.run(
            tool.run(
                ToolInput(
                    trace_id=str(uuid.uuid4()),
                    params={"trigger": "init"},
                )
            )
        )
        scan_result = result.result["scan_result"]
        console.print(
            f"[dim]스캔 및 인덱싱 완료 (파일: {len(scan_result['files'])}개, 엔드포인트: {scan_result['endpoint_count']}개, 프레임워크: {scan_result['framework']})[/dim]"
        )

        payload = {
            "project_slug": project_slug,
            "display_name": display_name,
            "local_path": str(project_root),
            "config_path": str(config_file),
            "index_path": str(index_dir),
            "framework": scan_result.get("framework", framework),
            "language": scan_result.get("language", language),
        }
        api_client = ApiClient(base_url=server_url, token=server_token or None)
        register_result = asyncio.run(api_client.register_project(payload))
        if register_result is None:
            console.print(f"[yellow]프로젝트 등록에 실패했습니다. 셋업 URL만 안내합니다: {setup_url}[/yellow]")
        else:
            dashboard_url = register_result.get("dashboard_url") or dashboard_url
            setup_url = f"{dashboard_url.rstrip('/')}/setup"
            console.print(f"[bold green]프로젝트 등록 완료[/bold green] (status: {register_result.get('status', 'unknown')})")
            console.print(f"[bold cyan]{setup_url}[/bold cyan]")
            _open_dashboard(setup_url)
    except Exception as e:
        console.print(f"[red]스캔 중 오류 발생: {e}[/red]")
        console.print(f"[yellow]셋업 URL: {setup_url}[/yellow]")

    console.print(
        f"\n이제 [bold {BRAND_PURPLE}]qapilot generate scenarios[/bold {BRAND_PURPLE}] "
        f"명령어로 테스트 시나리오를 만들어보세요!"
    )
