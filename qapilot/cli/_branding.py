"""CLI 공통 콘솔·브랜드 상수.

main.py / banner.py / commands/* 모두에서 공유하는 Rich Console 인스턴스와
브랜드 컬러를 모아둔다.

담당: A
Created: 2026-05-12
"""

from rich.console import Console

console = Console()
BRAND_PURPLE = "#3617CE"
