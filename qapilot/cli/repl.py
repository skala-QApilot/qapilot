"""qapilot 인터랙티브 셸 (REPL).

`qapilot` 명령을 인자 없이 실행하면 진입한다. Claude Code 의 REPL UX 를 차용:
- SIGINT 2-step: 첫 Ctrl-C 는 현재 명령 abort, 1.5초 내 두 번째 Ctrl-C 는 셸 종료
- 슬래시 명령: /help, /exit, /clear  (Phase 2 에서 /cost, /model, /memory 추가 예정)
- Banner 는 REPL 진입 시 1회만 출력
- 명령 히스토리는 **현재 세션 안에서만 휘발 (InMemoryHistory)** — 사내 도메인·정책·DSN 등
  민감 입력이 디스크에 평문 보존되는 위험 회피. Phase 2 에서 opt-in 영속화 옵션 검토.

담당: A
Created: 2026-05-13
"""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory

from qapilot.cli._branding import BRAND_PURPLE, console
from qapilot.cli.banner import print_welcome_banner

if TYPE_CHECKING:
    import typer


# Tab 자동완성 후보 (Phase 1)
_COMPLETIONS = [
    "init",
    "generate", "generate scenarios", "generate code",
    "test",
    "explain",
    "rescan",
    "sync",
    "ui",
    "spec", "spec import",
    "rule", "rule add",
    "/help", "/exit", "/clear",
    "exit", "quit",
    "--help", "--case", "--failed", "--affected", "--tag", "--sync", "--no-sync",
]

# SIGINT 2-step 윈도우 (밀리초)
_SIGINT_DOUBLE_TAP_MS = 1500


def _print_repl_intro() -> None:
    """REPL 진입 시 안내 메시지."""
    console.print(
        f"\n[bold {BRAND_PURPLE}]Interactive Shell (REPL) 모드[/bold {BRAND_PURPLE}]\n"
        f"[dim]명령을 연속 입력하세요. [bold]/help[/bold] 로 도움말, "
        f"[bold]/exit[/bold] (또는 Ctrl-D) 로 종료.[/dim]\n"
    )


def _print_help() -> None:
    """슬래시 명령 + 일반 명령 도움말 출력."""
    rows = [
        ("/help", "이 도움말 출력"),
        ("/exit, /quit", "셸 종료 (또는 exit, quit, Ctrl-D, Ctrl-C 2회)"),
        ("/clear", "화면 지우기"),
        ("", ""),
        ("init", "프로젝트 초기화 + 코드 스캔"),
        ("generate scenarios", "시나리오 생성 (Layer 1A)"),
        ("generate code [--case ID]", "테스트 코드 생성 (Layer 1B)"),
        ("test [--case|--failed|--affected]", "테스트 실행 (Layer 2~3)"),
        ("explain <defect_id>", "결함 원인 분석"),
        ("rescan", "코드 인덱스 재생성"),
        ("spec import <file>", "도메인 문서 임베딩"),
        ("rule add <text>", "도메인 규칙 추가"),
        ("sync", "서버 동기화"),
        ("ui", "웹 대시보드 기동"),
    ]
    console.print(f"\n[bold {BRAND_PURPLE}]사용 가능한 명령[/bold {BRAND_PURPLE}]")
    for cmd, desc in rows:
        if not cmd:
            console.print()
            continue
        console.print(f"  [bold]{cmd:<36}[/bold] [dim]{desc}[/dim]")
    console.print(
        f"\n[dim]명령 뒤에 [bold]--help[/bold] 를 붙이면 상세 옵션. "
        f"기존 [bold]qapilot <cmd>[/bold] (셸 외부 일회성) 도 그대로 사용 가능.[/dim]\n"
    )


def _dispatch_typer(app: typer.Typer, text: str) -> None:
    """입력 라인을 Typer 앱으로 위임 실행. REPL 컨텍스트에 맞게 에러 흡수."""
    try:
        args = shlex.split(text)
    except ValueError as e:
        console.print(f"[red]입력 파싱 오류:[/red] {e}")
        return

    try:
        app(args=args, standalone_mode=False, prog_name="qapilot")
    except click.exceptions.Exit:
        # Typer.Exit() — 정상 종료 신호. REPL 에서는 흡수
        pass
    except click.exceptions.UsageError as e:
        console.print(f"[red]사용법 오류:[/red] {e.format_message()}")
        if args:
            console.print(f"[dim]힌트: [bold]{args[0]} --help[/bold] 로 옵션 확인[/dim]")
    except click.exceptions.ClickException as e:
        console.print(f"[red]오류:[/red] {e.format_message()}")
    except KeyboardInterrupt:
        console.print("\n[yellow](명령 중단됨)[/yellow]")
    except Exception as e:
        console.print(f"[red]예상치 못한 오류:[/red] {e}")


def run_repl(app: typer.Typer) -> None:
    """qapilot 인터랙티브 셸을 실행한다.

    Args:
        app: Typer 애플리케이션 인스턴스. REPL 안에서 명령 실행 시 위임 대상.
    """
    print_welcome_banner()
    _print_repl_intro()

    # 휘발성 히스토리 — 셸 종료 시 사라짐. 민감 입력(rule add, spec import 경로, 향후 DSN 등) 디스크 평문 저장 회피.
    session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        completer=WordCompleter(_COMPLETIONS, ignore_case=True, sentence=True),
        complete_while_typing=False,
    )

    last_sigint_at = 0.0
    while True:
        try:
            text = session.prompt("qapilot> ").strip()
        except KeyboardInterrupt:
            # SIGINT 2-step (Claude Code 패턴)
            now = time.time() * 1000
            if now - last_sigint_at < _SIGINT_DOUBLE_TAP_MS:
                console.print("\n[dim]bye.[/dim]")
                return
            last_sigint_at = now
            console.print("[dim](다시 Ctrl-C 또는 /exit 으로 종료)[/dim]")
            continue
        except EOFError:
            # Ctrl-D
            console.print("\n[dim]bye.[/dim]")
            return

        if not text:
            continue

        if text in ("/exit", "/quit", "exit", "quit", ":q"):
            console.print("[dim]bye.[/dim]")
            return

        if text == "/help":
            _print_help()
            continue
        if text == "/clear":
            console.clear()
            continue

        _dispatch_typer(app, text)
