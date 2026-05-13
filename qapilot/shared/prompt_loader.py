"""프롬프트 템플릿 로더.

prompts/{agent_name}/system.md + template.md를 로드하고
{{variable}} 바인딩으로 렌더링한다.

Author: A
Created: 2026-05-07
"""

import os
from pathlib import Path

_PROMPTS_DIR = Path(
    os.environ.get("QAPILOT_PROMPTS_DIR", Path(__file__).parent.parent.parent / "prompts")
)


class PromptLoader:
    """Agent별 프롬프트 로더."""

    def __init__(self, agent_name: str):
        """프롬프트 파일을 로드한다.

        Args:
            agent_name: prompts/ 하위 디렉토리명 (예: "scenario_generator").
        """
        agent_dir = _PROMPTS_DIR / agent_name
        self._system = self._read(agent_dir / "system.md")
        self._template = self._read(agent_dir / "template.md")

    def system(self) -> str:
        """system.md 내용을 반환한다."""
        return self._system

    def render(self, **variables: str) -> str:
        """template.md의 {{key}}를 value로 치환하여 반환한다."""
        rendered = self._template
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", value)
        return rendered

    @staticmethod
    def _read(path: Path) -> str:
        """파일을 읽어 반환한다. 없으면 빈 문자열."""
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""
