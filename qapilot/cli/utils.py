"""CLI 유틸리티 함수.

이미지를 Rich Text 픽셀 아트로 변환하는 기능을 포함한다.

담당: A
Created: 2026-05-08
"""

from pathlib import Path
from typing import Optional

from PIL import Image
from rich.console import Console
from rich.style import Style
from rich.text import Text

def is_transparent(r: int, g: int, b: int, a: int, threshold: int = 15) -> bool:
    """픽셀이 투명하거나 거의 검은색(크로마키)인지 확인한다."""
    if a < 128:
        return True
    # 검은색 배경(rgb 0,0,0 ~ 15,15,15)을 투명하게 처리
    if r <= threshold and g <= threshold and b <= threshold:
        return True
    return False

def render_image_to_text(image_path: Path, width: int = 40) -> Optional[Text]:
    """이미지 파일을 읽어 하프 블록(▀, ▄)을 사용하는 Rich Text 객체로 변환한다.
    
    투명도(Alpha) 및 블랙 배경을 인식하여 터미널 배경과 자연스럽게 어우러지게 출력한다.
    
    Args:
        image_path: 이미지 파일 경로
        width: 터미널에서 표시할 가로 픽셀 수 (너비)
        
    Returns:
        Rich Text 객체 또는 실패 시 None
    """
    try:
        if not image_path.exists():
            return None
            
        # 투명도를 지원하기 위해 RGBA로 변환
        img = Image.open(image_path).convert("RGBA")
        
        # 터미널의 한 문자는 보통 가로보다 세로가 약 2배 김 (1:2).
        # 하지만 사용자마다 폰트 설정(줄간격 등)이 달라 세로가 덜 길게(예 1:1.5) 나올 수 있음.
        # 두 스크린샷 비교 결과: 세로 2배(40줄)는 에일리언처럼 길었고, 1:1 정비율(20줄)은 약간 납작했음.
        # 따라서 터미널 환경에서 가장 완벽한 원/정사각형 비율을 내는 보정 계수(약 1.3)를 적용함.
        aspect_ratio = img.height / img.width
        font_correction = 1.3  # 높이 보정 계수 (1.0 = 납작, 2.0 = 길쭉)
        
        pixel_height = int(width * aspect_ratio * font_correction)
        
        # 하프 블록을 위해 세로 픽셀 수를 짝수로 맞춤
        if pixel_height % 2 != 0:
            pixel_height += 1
            
        img = img.resize((width, pixel_height), Image.Resampling.LANCZOS)
        
        result = Text()
        
        # 터미널 1줄이 2개의 세로 픽셀을 표현하므로, 출력 라인 수는 픽셀 높이의 절반
        lines_count = pixel_height // 2
        
        for y in range(lines_count):
            for x in range(width):
                # 위쪽 픽셀
                r1, g1, b1, a1 = img.getpixel((x, y * 2))
                top_trans = is_transparent(r1, g1, b1, a1)
                
                # 아래쪽 픽셀
                r2, g2, b2, a2 = img.getpixel((x, y * 2 + 1))
                bottom_trans = is_transparent(r2, g2, b2, a2)
                
                # 4가지 투명도 경우의 수 처리
                if top_trans and bottom_trans:
                    # 둘 다 투명: 빈 칸 출력
                    result.append(" ")
                elif top_trans and not bottom_trans:
                    # 상단 투명, 하단 색상: 하단 블록(▄) 출력 (배경색 없음)
                    style = Style(color=f"rgb({r2},{g2},{b2})")
                    result.append("▄", style=style)
                elif not top_trans and bottom_trans:
                    # 상단 색상, 하단 투명: 상단 블록(▀) 출력 (배경색 없음)
                    style = Style(color=f"rgb({r1},{g1},{b1})")
                    result.append("▀", style=style)
                else:
                    # 둘 다 색상: 상단 블록(▀) 출력 (상단:전경색, 하단:배경색)
                    style = Style(color=f"rgb({r1},{g1},{b1})", bgcolor=f"rgb({r2},{g2},{b2})")
                    result.append("▀", style=style)
            
            if y < lines_count - 1:
                result.append("\n")
                
        return result
    except Exception as e:
        # 오류 발생 시 조용히 None 반환
        return None
