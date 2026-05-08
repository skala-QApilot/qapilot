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

    if not scenarios_dir.exists():
        console.print("[yellow]동기화할 시나리오가 없습니다.[/yellow]")
        return True

    # 시나리오 파일 목록 수집 (*.json)
    scenario_files = list(scenarios_dir.glob("*.json"))
    if not scenario_files:
        console.print("[yellow]동기화할 시나리오 파일이 없습니다.[/yellow]")
        return True

    console.print(f"[bold green]{len(scenario_files)}개의 시나리오를 동기화합니다...[/bold green]")

    success_count = 0
    with Progress() as progress:
        task = progress.add_task("[cyan]Uploading scenarios...", total=len(scenario_files))
        
        for sf in scenario_files:
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # TODO: 시나리오에 해당하는 코드 파일(.js)이 있으면 함께 포함하거나 별도 업로드
                # 현재는 시나리오만 전송
                res = await api_client.upload_scenario(data)
                if res:
                    success_count += 1
            except Exception as e:
                console.print(f"[red]파일 {sf.name} 업로드 중 오류 발생: {e}[/red]")
            
            progress.update(task, advance=1)

    console.print(f"[bold blue]동기화 완료: {success_count}/{len(scenario_files)} 성공[/bold blue]")
    return success_count == len(scenario_files)
