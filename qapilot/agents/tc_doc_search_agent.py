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

_PLACEHOLDER_TOKEN_RE = re.compile(r"\{[^{}]+\}")
_INLINE_API_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/[^\s,\)\]]+)", re.IGNORECASE)
_API_VALUE_RE = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)\s*$", re.IGNORECASE)


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


def _dedupe_api_prefix(path: str) -> str:
    cleaned = path.strip()
    while True:
        prefix_match = re.match(r"^(/api/[^/]+)", cleaned)
        if not prefix_match:
            return cleaned
        prefix = prefix_match.group(1)
        remainder = cleaned[len(prefix):]
        if not remainder.startswith(prefix):
            return cleaned
        cleaned = prefix + remainder[len(prefix):]


def _normalize_api_value(api: Any) -> str | None:
    if api is None:
        return None
    text = str(api).strip()
    if not text or text.lower() == "null":
        return None
    m = _API_VALUE_RE.match(text)
    if not m:
        return text
    return f"{m.group(1).upper()} {_dedupe_api_prefix(m.group(2))}"


def _extract_inline_api(text: str) -> str | None:
    m = _INLINE_API_RE.search(text or "")
    if not m:
        return None
    return f"{m.group(1).upper()} {_dedupe_api_prefix(m.group(2))}"


def _align_tc_api_fields(tc: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(tc)
    inline_api = _extract_inline_api(str(normalized.get("when", "") or ""))
    api = _normalize_api_value(normalized.get("api"))
    normalized["api"] = inline_api or api
    return normalized


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
            tc = _align_tc_api_fields(tc)
            sources = tc.get("sources") or []
            given = str(tc.get("given", ""))
            when = str(tc.get("when", ""))
            then = str(tc.get("then", ""))
            valid.append({
                "name": str(tc.get("name", "")),
                "technique": str(tc.get("technique", "")),
                "given": given,
                "when": when,
                "then": then,
                "values": _normalize_values(tc.get("values") or [], sources),
                "tags": tc.get("tags") or ["normal"],
                "req_id": tc.get("req_id"),
                "api": tc.get("api"),
                "sources": sources,
                "doc_verified": "codebase" in sources,
                "depends_on": tc.get("depends_on") or [],
                **_claim_meta("given", given, sources),
                **_claim_meta("when", when, sources),
                **_claim_meta("then", then, sources),
            })

        return valid, analysis, confidence


def _contains_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_TOKEN_RE.search(text or ""))


def _claim_meta(prefix: str, text: str, sources: list[str]) -> dict[str, Any]:
    unresolved = _contains_placeholder(text)
    status = "unresolved" if unresolved else "grounded_doc"
    evidence_refs = [s for s in sources if isinstance(s, str)]
    return {
        f"{prefix}_status": status,
        f"{prefix}_source": "doc_draft",
        f"{prefix}_evidence_refs": evidence_refs,
        f"{prefix}_unresolved": unresolved,
    }


def _normalize_values(values: list[Any], sources: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    evidence_refs = [s for s in sources if isinstance(s, str)]
    for value in values:
        if not isinstance(value, dict):
            continue
        raw_value = value.get("value")
        value_text = "" if raw_value is None else str(raw_value)
        unresolved = _contains_placeholder(value_text)
        normalized.append({
            "field": str(value.get("field", "")),
            "value": raw_value,
            "type": str(value.get("type", "string")),
            "purpose": str(value.get("purpose", "")),
            "status": "unresolved" if unresolved else "grounded_doc",
            "source": "doc_draft",
            "evidence_refs": evidence_refs,
            "unresolved": unresolved,
        })
    return normalized
