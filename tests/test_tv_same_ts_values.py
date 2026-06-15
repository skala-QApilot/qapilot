"""격차 2 — TV agent 의 same-TS 값 중복 방지 컨텍스트 검증.

배경: TV agent 가 TC 1개씩 호출되며 같은 TS 의 이전 TC values 를 못 봐
`test@test.com`, `newuser_2026@test.com` 이 TS 안에서 중복 생성 →
SUT unique constraint 위반 → positive 의도 TC 가 api fail.

해결: pipeline `_tv_generate_codebase_aware` 가 TS 별 누적 values 를
`context["same_ts_values"]` 로 전달, agent 가 prompt 에 렌더.
"""
from __future__ import annotations

from qapilot.agents.tv_codebase_aware_agent import _format_same_ts_values
from qapilot.shared.prompt_loader import PromptLoader


class TestFormatSameTsValues:
    def test_empty(self):
        assert "첫 TC" in _format_same_ts_values([])

    def test_entries_rendered(self):
        entries = [
            {"tc_id": "TS-001-TC-01", "field": "email", "value": "a@test.com"},
            {"tc_id": "TS-001-TC-02", "field": "email", "value": "b@test.com"},
        ]
        text = _format_same_ts_values(entries)
        assert "TS-001-TC-01" in text
        assert "a@test.com" in text
        assert "b@test.com" in text

    def test_truncation(self):
        entries = [
            {"tc_id": f"TS-001-TC-{i:02d}", "field": "email", "value": f"user{i}@example.com"}
            for i in range(100)
        ]
        text = _format_same_ts_values(entries)
        assert len(text) < 1700
        assert "truncated" in text


class TestPromptTemplateHasPlaceholder:
    def test_template_renders_same_ts_values(self):
        pack = PromptLoader("tv_codebase_aware")
        rendered = pack.render(
            tc_name="n", tc_api="a", tc_req_id="r", tc_tags="t",
            tc_given="g", tc_when="w", tc_then="t",
            existing_values="(없음)", schemas="{}", selectors="{}",
            patterns="{}", db_snapshot="(없음)", source_snippets="(없음)",
            same_ts_values="- [TS-001-TC-01] email = a@test.com",
            validation_feedback="",
        )
        assert "{{same_ts_values}}" not in rendered
        assert "a@test.com" in rendered
