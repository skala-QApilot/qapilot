"""CLI 유틸리티 함수.

픽셀 아트 PNG를 터미널 컬러 텍스트로 렌더링한다.

담당: A
Created: 2026-05-08
Updated: 2026-05-11 — pixel-grid 렌더링으로 변경(BOX 다운샘플 + █ 블록 출력)
"""

from pathlib import Path
from typing import Optional

from PIL import Image
from rich.style import Style
from rich.text import Text


def _is_background(
    r: int, g: int, b: int, a: int, bg: Optional[tuple[int, int, int]]
) -> bool:
    """픽셀이 배경(투명 또는 배경색 근사)인지 판정."""
    if a < 64:
        return True
    if bg is None:
        return False
    return abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) < 40


def render_image_to_text(
    image_path: Path,
    grid_size: int = 32,
    cell_width: int = 2,
) -> Optional[Text]:
    """픽셀 아트 PNG를 터미널 컬러 텍스트로 변환한다.

    원본을 grid_size × grid_size 로 BOX 리샘플링하고, 각 셀을 cell_width 개의
    █ 블록 글자로 출력한다. anti-alias 없이 픽셀 아트의 블록 격자감을 보존한다.
    모노스페이스 폰트 글자비가 1:2 일 때 cell_width=2 가 시각적 정사각형이다.

    Args:
        image_path: 픽셀 아트 PNG 경로 (RGBA 권장)
        grid_size: 다운샘플 그리드 크기(정사각). 32~40 권장.
        cell_width: 픽셀 1개를 가로 몇 글자로 표현할지. 2 → 시각적 정사각형.

    Returns:
        Rich Text 객체. 실패 시 None.
    """
    try:
        if not image_path.exists():
            return None

        img = Image.open(image_path).convert("RGBA")
        small = img.resize((grid_size, grid_size), Image.Resampling.BOX)

        # 배경색 추정: 4 모서리 셀의 평균. 모두 투명이면 None.
        corners = [
            small.getpixel((0, 0)),
            small.getpixel((grid_size - 1, 0)),
            small.getpixel((0, grid_size - 1)),
            small.getpixel((grid_size - 1, grid_size - 1)),
        ]
        opaque = [c for c in corners if c[3] > 128]
        if opaque:
            bg: Optional[tuple[int, int, int]] = (
                sum(c[0] for c in opaque) // len(opaque),
                sum(c[1] for c in opaque) // len(opaque),
                sum(c[2] for c in opaque) // len(opaque),
            )
        else:
            bg = None

        result = Text()
        spaces = " " * cell_width

        for y in range(grid_size):
            for x in range(grid_size):
                r, g, b, a = small.getpixel((x, y))
                if _is_background(r, g, b, a, bg):
                    result.append(spaces)
                else:
                    result.append(
                        "█" * cell_width,
                        style=Style(color=f"rgb({r},{g},{b})"),
                    )
            if y < grid_size - 1:
                result.append("\n")

        return result
    except Exception:
        return None
