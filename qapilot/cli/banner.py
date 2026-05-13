"""qapilot CLI 환영 배너.

쿼카 우주비행사 마스코트 픽셀아트 + 제품 정보를 Rich Panel 로 출력한다.

담당: A
Created: 2026-05-12
"""

from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from qapilot.cli._branding import BRAND_PURPLE, console
from qapilot.cli.utils import render_image_to_text


def print_welcome_banner() -> None:
    """쿼카 우주비행사 마스코트 픽셀아트와 함께 환영 배너를 출력한다."""

    mascot_path = Path(__file__).parent.parent / "assets" / "mascot.png"

    # 40 그리드 + cell_width=2 → 가로 80글자 × 세로 40줄.
    mascot_text = render_image_to_text(mascot_path, grid_size=40, cell_width=2)
    if mascot_text is None:
        mascot_text = Text(
            f"(mascot image missing: {mascot_path})", style="dim red"
        )

    info = (
        f"\n[bold {BRAND_PURPLE}]QApilot — Intelligent AI QA Agent[/bold {BRAND_PURPLE}]\n"
        f"[dim]No People Testing (NPT) — 완전 무인 테스트 운영 시스템[/dim]\n\n"
        f"[{BRAND_PURPLE}]v0.1.0[/{BRAND_PURPLE}]  |  "
        f"[dim]Status:[/dim] [bold green]Ready[/bold green]\n"
        f"[dim]SK Telecom NOVA Project[/dim]"
    )

    body = Group(Align.center(mascot_text), Align.center(info))

    console.print(
        Panel(
            body,
            border_style=BRAND_PURPLE,
            padding=(1, 2),
            subtitle=f"[bold {BRAND_PURPLE}]Welcome to QApilot[/bold {BRAND_PURPLE}]",
            subtitle_align="right",
        )
    )
