"""defect → GitHub issue 본문 템플릿 로드/렌더링.

서비스별 템플릿은 S3 `services/{service_id}/github-issue-template.md` 에 저장한다
(없으면 DEFAULT_TEMPLATE 사용). 템플릿의 첫 줄은 issue title, 이후 줄은 body로 쓴다.

Author: C
Created: 2026-06-15
"""

from __future__ import annotations

from typing import Any

from qapilot.storage import s3_client

DEFAULT_TEMPLATE = """\
[{{category}}] {{tc_id}} - {{root_cause}}

## 원인 분석
- TC: {{tc_id}} (TS: {{ts_id}})
- 분류: {{category}}
- 원인(Top1): {{root_cause}}
- 신뢰도: {{confidence}}
- 위치: {{file_location}}

## 해결 방안
{{solution_guide}}

## 담당자
{{assignee}}

---
run_id: {{run_id}}
"""


def _template_key(service_id: str) -> str:
    return f"services/{service_id}/github-issue-template.md"


def load_template(service_id: str) -> str:
    """서비스별 issue 템플릿을 S3에서 로드한다. 없으면 DEFAULT_TEMPLATE."""
    data = s3_client.get_object(_template_key(service_id))
    if data is None:
        return DEFAULT_TEMPLATE
    return data.decode("utf-8")


def render(template: str, fields: dict[str, Any]) -> tuple[str, str]:
    """템플릿의 `{{key}}` placeholder를 fields 값으로 치환하고 (title, body)로 분리한다.

    첫 줄 = title, 나머지 줄 = body.
    """
    rendered = template
    for key, value in fields.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", "" if value is None else str(value))

    lines = rendered.split("\n")
    title = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    return title, body
