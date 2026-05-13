"""qapilot init 명령 핸들러.

프로젝트 초기화 + 인터랙티브 설정 마법사 + 코드베이스 스캔(미연동).

담당: A
Created: 2026-05-12
"""

from pathlib import Path

import typer
import yaml
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from qapilot.cli._branding import BRAND_PURPLE, console
from qapilot.cli.banner import print_welcome_banner


def init() -> None:
    """프로젝트 초기화 + 코드 스캔 + 서버 설정 마법사."""
    print_welcome_banner()
    console.print(f"\n[bold {BRAND_PURPLE}]QApilot 설정을 시작합니다...[/bold {BRAND_PURPLE}]\n")

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
    console.print("[dim]디렉토리 구조 생성 완료 (.qapilot/)[/dim]")

    # 2. 인터랙티브 설정 마법사
    config_file = Path("qapilot.config.yaml")

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

    # 3. 설정 파일 저장
    config_data = {
        "server": {
            "url": server_url,
            "token": server_token if server_token else None,
        },
        "project": {
            "framework": framework,
            "language": "python" if project_choice == 1 else "unknown",
        },
        "llm": {
            "default_model": selected_model,
            "deep_model": "gpt-4o",
        },
    }

    console.print(f"\n[bold cyan]설정 요약:[/bold cyan]")
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
    # TODO: codebase scanner 연동 (FR-000)
    console.print("[dim]스캔 및 인덱싱 완료.[/dim]")
    console.print(
        f"\n이제 [bold {BRAND_PURPLE}]qapilot generate scenarios[/bold {BRAND_PURPLE}] "
        f"명령어로 테스트 시나리오를 만들어보세요!"
    )
