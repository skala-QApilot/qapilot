"""문서 검색 기반 TC 열거 Agent.

TS 정보와 Qdrant 검색 결과를 기반으로 TC 골격(skeleton)만 열거한다.
3단계 분석(제약 추출 → 기법 결정 → TC 열거) 결과를 analysis 필드로 함께 반환한다.

given/when/then/value 는 여기서 만들지 않는다 — 후속 통합 단계
(TVFromCodebaseAgent)가 문서+코드베이스+DB 를 한 컨텍스트에서 보고 함께 생성한다.
본 단계는 "어떤 TC가 있어야 하는가"(name/intent/technique/tags/api/req_id)만 정한다.
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


def _format_available_apis(apis: list[str]) -> str:
    if not apis:
        return "없음 (api는 null로 두어라)"
    return "\n".join(f"- {a}" for a in apis)


def _normalize_api(api: Any, valid_apis: set[str] | None) -> str | None:
    """LLM이 반환한 api 값을 valid_apis 안의 값으로 정규화한다.

    정확히 일치하지 않아도 경로 파라미터(예: /api/orders/123 -> /api/orders/{order_id})를
    채워 넣은 값이면 매칭한다. valid_apis가 주어졌는데 매칭 실패 시 None
    (hallucination 방지 — 존재하지 않는 경로를 그대로 두지 않는다).
    valid_apis가 None이면 grounding 정보 없이 호출된 것으로 보고 원본 값을 그대로 둔다.
    """
    if not isinstance(api, str) or not api.strip() or api == "null":
        return None
    api = api.strip()
    if valid_apis is None:
        return api
    if not valid_apis:
        return None
    if api in valid_apis:
        return api
    if " " not in api:
        return None
    method, path = api.split(" ", 1)
    for valid in valid_apis:
        v_method, v_path = valid.split(" ", 1)
        if v_method != method:
            continue
        pattern = re.sub(r"\{[^}]+\}", r"[^/]+", v_path)
        if re.fullmatch(pattern, path):
            return valid
    return None


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
    """TS 정보 + 검색된 문서 chunk로 제약 분석 후 TC 골격(skeleton)을 열거한다.

    출력 TC 는 name/intent/technique/tags/api/req_id/depends_on 만 가진다.
    given/when/then/value 는 채우지 않는다 (통합 단계가 담당).
    """

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
        available_apis: list[str] = context.get("available_apis") or []

        ts_name = ts_item.get("name", "")
        ts_description = ts_item.get("description", "")
        requirements = ts_item.get("requirements") or []

        user_prompt = self.prompts.render(
            ts_name=ts_name,
            ts_description=ts_description,
            requirements=_format_requirements(requirements),
            retrieved_docs=_format_retrieved_docs(retrieved_docs),
            available_apis=_format_available_apis(available_apis),
        )
        if last_error:
            user_prompt += f"\n\n[이전 시도 오류: {last_error}. JSON 형식을 확인하라.]"

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
            json_mode=True,
        )

        test_cases, analysis, confidence = self._parse(response.content, set(available_apis))
        return ExecuteResult(
            result={"test_cases": test_cases, "analysis": analysis},
            confidence=confidence,
        )

    def _parse(self, content: str, valid_apis: set[str] | None = None) -> tuple[list[dict], list[dict], float]:
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
            # 골격(skeleton)만 — given/when/then/value 는 통합 단계가 채운다.
            valid.append({
                "name": str(tc.get("name", "")),
                "technique": str(tc.get("technique", "")),
                "intent": str(tc.get("intent", "") or "normal"),
                "tags": tc.get("tags") or ["normal"],
                "req_id": tc.get("req_id"),
                "api": _normalize_api(tc.get("api"), valid_apis),
                "sources": sources,
                "doc_verified": "codebase" in sources,
                "depends_on": tc.get("depends_on") or [],
            })

        return valid, analysis, confidence
