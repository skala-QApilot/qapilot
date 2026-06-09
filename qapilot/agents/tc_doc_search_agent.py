"""문서 검색 기반 TC 생성 Agent.

TS 정보와 Qdrant 검색 결과를 기반으로 given/when/then 포함 TC를 생성한다.
3단계 분석(제약 추출 → 기법 결정 → TC 작성) 결과를 analysis 필드로 함께 반환한다.
코드베이스는 사용하지 않는다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import ExecuteResult


def _format_requirements(requirements: list[str]) -> str:
    if not requirements:
        return "없음"
    return "\n".join(f"- {r}" for r in requirements)


def _format_retrieved_docs(docs: list[dict]) -> str:
    if not docs:
        return "검색된 문서 없음"
    parts: list[str] = []
    for i, doc in enumerate(docs, start=1):
        content = doc.get("content", "")
        source = doc.get("source", "")
        score = doc.get("score", 0.0)
        parts.append(f"[문서 {i}] 출처: {source} (유사도: {score:.2f})\n{content}")
    return "\n\n---\n\n".join(parts)


def _extract_json(text: str) -> str:
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            text = m.group(1).strip()
    start = text.find("{")
    if start == -1:
        return text
    # string-aware brace matching — 문자열 내부의 { } 는 depth 계산에서 제외
    in_string = False
    escape_next = False
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


class TCFromDocsAgent(BaseAgent):
    """TS 정보 + 검색된 문서 chunk로 제약 분석 후 TC (given/when/then)를 생성한다."""

    agent_name = "tc_doc_search"
    use_deep_model = True

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        ts_item: dict = context.get("ts_item") or {}
        retrieved_docs: list[dict] = context.get("retrieved_docs") or []

        ts_name = ts_item.get("name", "")
        ts_description = ts_item.get("description", "")
        requirements = ts_item.get("requirements") or []

        user_prompt = self.prompts.render(
            ts_name=ts_name,
            ts_description=ts_description,
            requirements=_format_requirements(requirements),
            retrieved_docs=_format_retrieved_docs(retrieved_docs),
        )
        if last_error:
            user_prompt += f"\n\n[이전 시도 오류: {last_error}. JSON 형식을 확인하라.]"

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
            json_mode=True,
        )

        test_cases, analysis, confidence = self._parse(response.content)
        return ExecuteResult(
            result={"test_cases": test_cases, "analysis": analysis},
            confidence=confidence,
        )

    def _parse(self, content: str) -> tuple[list[dict], list[dict], float]:
        try:
            cleaned = _extract_json(content)
            data = json.loads(cleaned)
        except Exception as e:
            self.logger.warning("tc_doc_search_parse_error", error=str(e), raw=content[:500])
            return [], [], 0.3

        raw_tcs = data.get("test_cases") or []
        analysis = data.get("analysis") or []
        confidence = float(data.get("confidence", 0.7))

        valid: list[dict] = []
        for tc in raw_tcs:
            if not isinstance(tc, dict) or not tc.get("name"):
                continue
            sources = tc.get("sources") or []
            valid.append({
                "name": str(tc.get("name", "")),
                "technique": str(tc.get("technique", "")),
                "given": str(tc.get("given", "")),
                "when": str(tc.get("when", "")),
                "then": str(tc.get("then", "")),
                "values": tc.get("values") or [],
                "tags": tc.get("tags") or ["normal"],
                "req_id": tc.get("req_id"),
                "api": tc.get("api"),
                "sources": sources,
                "doc_verified": "codebase" in sources,
                "depends_on": tc.get("depends_on") or [],
            })

        return valid, analysis, confidence
