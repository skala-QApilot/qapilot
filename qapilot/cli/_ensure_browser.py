"""Playwright Chromium 브라우저 자동 셋업 헬퍼.

`pip install playwright` 는 Python wrapper 만 설치하고 브라우저 바이너리는 별도
(`python -m playwright install chromium`). 이 헬퍼가 부재 시 자동 다운로드한다.

호출 위치:
- `qapilot init` 마법사 마지막 (사용자 prompt 후) — 사전 셋업
- `qapilot test` 의 `_test_execution` 노드 진입 시 — lazy 자동 셋업

담당: A
Created: 2026-05-18
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

_console = Console()


def is_chromium_installed() -> bool:
    """Playwright Chromium 바이너리 존재 여부 확인.

    `executable_path` 접근은 launch 안 함 — 빠른 stat. async_playwright 가 사용 가능해야
    하므로 sync 컨텍스트에서 호출 시 asyncio.run 으로 감쌈.
    """
    try:
        return asyncio.run(_check_chromium_path())
    except RuntimeError:
        # 이미 이벤트 루프 안 (잘 안 나오는 경로) — sync 폴백
        return _check_chromium_path_sync()


async def _check_chromium_path() -> bool:
    """async_playwright 컨텍스트로 executable_path 확인."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            exe_path = pw.chromium.executable_path
            return bool(exe_path) and Path(exe_path).exists()
    except Exception:
        return False


def _check_chromium_path_sync() -> bool:
    """sync_playwright 폴백 — 거의 사용 안 됨."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe_path = pw.chromium.executable_path
            return bool(exe_path) and Path(exe_path).exists()
    except Exception:
        return False


def ensure_chromium(*, prompt: bool = False, console: Console | None = None) -> bool:
    """Playwright Chromium 부재 시 자동 다운로드.

    Args:
        prompt: True 면 사용자 동의 후 다운로드. False 면 즉시 다운로드.
        console: Rich Console (테스트·외부 주입용). None 이면 모듈 console 사용.

    Returns:
        True — 설치 완료 (또는 이미 있음). False — 사용자 거절 또는 실패.
    """
    out = console or _console

    if is_chromium_installed():
        return True

    if prompt:
        out.print(
            "[yellow]Playwright 브라우저 (Chromium, 약 100MB) 가 설치되지 않았습니다.[/yellow]"
        )
        if not Confirm.ask("지금 설치하시겠습니까?", default=True):
            out.print("[dim]건너뜁니다. 추후 `playwright install chromium` 수동 실행 가능.[/dim]")
            return False

    return _run_playwright_install(out)


def _run_playwright_install(console: Console) -> bool:
    """`python -m playwright install chromium` 실행. stdout 라이브 표시."""
    console.print("[bold cyan]Playwright Chromium 설치 중...[/bold cyan]")

    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]

    try:
        # 진행률 표시: subprocess stdout 라이브 출력
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                console.print(f"  [dim]{line}[/dim]")
        return_code = process.wait()
    except FileNotFoundError:
        console.print(
            "[red]Playwright CLI 를 찾을 수 없습니다. "
            "`pip install playwright` 가 완료되었는지 확인하세요.[/red]"
        )
        return False
    except Exception as e:
        console.print(f"[red]설치 중 오류: {type(e).__name__}: {e}[/red]")
        return False

    if return_code == 0:
        console.print("[bold green]✅ Chromium 설치 완료.[/bold green]")
        return True

    console.print(
        f"[red]Chromium 설치 실패 (exit={return_code}). "
        f"네트워크/권한 문제일 수 있습니다. 수동: `{' '.join(cmd)}`[/red]"
    )
    return False


def ensure_chromium_for_test() -> bool:
    """`qapilot test` 의 _test_execution 노드용 — prompt 없이 자동 진행.

    사용자 의도는 이미 `qapilot test` 입력으로 명확. 부재 시 자동 다운로드 + 안내.
    """
    return ensure_chromium(prompt=False)


# CLI 의 init 마법사 호출용 (prompt=True)
__all__ = [
    "is_chromium_installed",
    "ensure_chromium",
    "ensure_chromium_for_test",
]


# Reachability hint — shutil.which 미사용 표기 회피
_ = shutil  # noqa: F401 (향후 PATH 검색 폴백 도입 여지)
