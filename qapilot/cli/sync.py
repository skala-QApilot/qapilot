"""로컬 산출물 서버 동기화 모듈.

.qapilot/ 디렉토리의 시나리오 및 코드를 중앙 서버로 업로드한다.

담당: A
Created: 2026-05-07
"""

import json
from pathlib import Path
from typing import List

from rich.console import Console
from rich.progress import Progress

from qapilot.cli.api_client import ApiClient

console = Console()
api_client = ApiClient()


async def sync_local_to_server() -> bool:
    """로컬의 모든 시나리오와 생성된 코드를 서버로 동기화한다."""
    base_dir = Path(".qapilot")
    scenarios_dir = base_dir / "scenarios"
    codes_dir = base_dir / "generated-code"

    # 1. 시나리오 동기화
    scenario_files = list(scenarios_dir.glob("*.json"))
    if scenario_files:
        console.print(f"[bold green]{len(scenario_files)}개의 시나리오를 동기화합니다...[/bold green]")
        with Progress() as progress:
            task = progress.add_task("[cyan]Uploading scenarios...", total=len(scenario_files))
            for sf in scenario_files:
                try:
                    with open(sf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    await api_client.upload_scenario(data)
                except Exception as e:
                    console.print(f"[red]시나리오 {sf.name} 업로드 실패: {e}[/red]")
                progress.update(task, advance=1)
    else:
        console.print("[yellow]동기화할 시나리오가 없습니다.[/yellow]")

    # 2. 생성된 코드(.js) 동기화
    code_files = list(codes_dir.glob("*.js"))
    if code_files:
        console.print(f"[bold green]{len(code_files)}개의 생성된 코드를 동기화합니다...[/bold green]")
        with Progress() as progress:
            task = progress.add_task("[cyan]Uploading generated codes...", total=len(code_files))
            for cf in code_files:
                try:
                    code_content = cf.read_text(encoding="utf-8")
                    tc_id = cf.stem  # 파일명이 TC_ID라고 가정
                    await api_client.upload_generated_code(tc_id, code_content)
                except Exception as e:
                    console.print(f"[red]코드 {cf.name} 업로드 실패: {e}[/red]")
                progress.update(task, advance=1)
    else:
        console.print("[yellow]동기화할 생성 코드가 없습니다.[/yellow]")

    console.print("[bold blue]서버 동기화 프로세스가 완료되었습니다.[/bold blue]")
    return True
