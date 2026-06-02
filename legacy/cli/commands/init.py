"""qapilot init 명령 핸들러.

프로젝트 초기화 + 인터랙티브 설정 마법사 + 코드베이스 스캔 + 서버 등록.

담당: A
Created: 2026-05-12
"""

import asyncio
import re
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

# 이슈 #123 — frontend dev server URL 자동 추론용 휴리스틱.
_FE_DIR_CANDIDATES = ("frontend", "web", "client", "ui", "app")
_VITE_CONFIG_NAMES = ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs")
_VITE_PORT_PATTERN = re.compile(r"\bport\s*:\s*(\d+)")
_PKG_DEV_PORT_PATTERN = re.compile(r'"(?:dev|start)"\s*:\s*"[^"]*?(?:--port|-p)\s+(\d+)')
_COMPOSE_FE_PORTS_PATTERN = re.compile(
    r"(?:frontend|web|client|ui)[\s\S]{0,300}?ports:[\s\S]{0,200}?[\"']?(\d+):\d+"
)


def _build_project_identity(project_root: Path) -> tuple[str, str]:
    """Derive the project slug and display name from the current directory."""
    display_name = project_root.name or "project"
    return project_slug_from_path(project_root), display_name


def _infer_target_url(project_root: Path) -> tuple[str | None, str | None]:
    """frontend dev server URL 자동 추론 (이슈 #123).

    탐색 순서 (적중 시 즉시 반환):
    1. `<frontend_dir>/vite.config.{js,ts,mjs,cjs}` 의 `server.port: NNNN`
    2. `<frontend_dir>/next.config.{js,mjs}` 존재 → Next.js default 3000
    3. `<frontend_dir>/package.json` 의 `scripts.dev` 또는 `scripts.start` 의
       `--port NNNN` 또는 `-p NNNN`
    4. `docker-compose.{yml,yaml}` 또는 `compose.yml` 의 frontend/web/client/ui
       service 의 `ports: "NNNN:..."`

    `<frontend_dir>` 후보: `frontend`, `web`, `client`, `ui`, `app`.

    Returns:
        (url, source_file_label) 또는 (None, None) 추론 실패 시.
        url 은 `http://localhost:PORT` 형식. source_file_label 은 사용자에게
        보여줄 상대 경로.
    """
    # 1~3) frontend dir 후보 순회
    for fe_name in _FE_DIR_CANDIDATES:
        fe_root = project_root / fe_name
        if not fe_root.is_dir():
            continue
        # 1) vite.config 파싱
        for cfg_name in _VITE_CONFIG_NAMES:
            cfg = fe_root / cfg_name
            if cfg.is_file():
                try:
                    text = cfg.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                m = _VITE_PORT_PATTERN.search(text)
                if m:
                    return f"http://localhost:{m.group(1)}", f"{fe_name}/{cfg_name}"
        # 2) Next.js default 3000
        if (fe_root / "next.config.js").is_file() or (fe_root / "next.config.mjs").is_file():
            return "http://localhost:3000", f"{fe_name}/next.config (Next.js default 3000)"
        # 3) package.json dev/start script
        pkg = fe_root / "package.json"
        if pkg.is_file():
            try:
                text = pkg.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _PKG_DEV_PORT_PATTERN.search(text)
            if m:
                return f"http://localhost:{m.group(1)}", f"{fe_name}/package.json"
    # 4) docker-compose 의 frontend service ports
    for compose_name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml"):
        compose = project_root / compose_name
        if compose.is_file():
            try:
                text = compose.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _COMPOSE_FE_PORTS_PATTERN.search(text)
            if m:
                return f"http://localhost:{m.group(1)}", compose_name
    return None, None


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

    # [Step 1.5] 테스트 대상 SUT URL — 이슈 #123 (target_url UX 강화).
    # 자동 추론 (frontend dev server config) 우선 → 실패 시 사용자 입력.
    # PR #122 의 auto-navigate (옵션 C) 가 작동하려면 target_url 필수.
    console.print(
        f"\n[bold {BRAND_PURPLE}]Step 1-2. 테스트 대상 (SUT) URL[/bold {BRAND_PURPLE}]"
    )
    inferred_url, inferred_src = _infer_target_url(project_root)
    if inferred_url:
        console.print(
            f"[dim]'{inferred_src}' 에서 dev server URL 을 감지했습니다.[/dim]"
        )
        target_url = Prompt.ask("SUT URL", default=inferred_url)
    else:
        console.print(
            "[dim]frontend dev server URL 을 자동 추론하지 못했습니다.\n"
            "QApilot 이 브라우저로 접속할 대상 서비스의 base URL 을 입력해주세요.\n"
            "예: http://localhost:3000 (로컬 dev) / https://staging.example.com (스테이징)[/dim]"
        )
        target_url = Prompt.ask("SUT URL", default="http://localhost:3000")

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
            "target_url": target_url,
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
    console.print(f" - SUT URL: {target_url}")
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

        # 이슈 #127: frontend DOM 정적 인덱싱 — ActionMapper LLM 호출 시 selector
        # 추측 대신 실제 DOM 정보 참조. 상세 배경: docs/frontend-dom-scan-gap.md
        from qapilot.tools.frontend_dom_scanner import (
            scan_frontend_directory,
            write_frontend_index,
        )
        fe_elements = scan_frontend_directory(project_root)
        if fe_elements:
            fe_index_path = index_dir / "frontend.json"
            write_frontend_index(fe_elements, fe_index_path)
            console.print(
                f"[dim]frontend DOM 인덱싱 완료 (element: {len(fe_elements)}개 → {fe_index_path.relative_to(project_root)})[/dim]"
            )
        else:
            console.print(
                "[dim]frontend 디렉토리에서 스캔 가능한 .vue/.tsx/.jsx 파일이 발견되지 않았습니다 (생략).[/dim]"
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
