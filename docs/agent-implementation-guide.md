# Agent 구현 가이드

> QApilot Agent/Tool 구현 시 참조 문서  
> 대상: 팀원 B/C/D/E/F (Agent/Tool 담당자)  
> 최종 수정일: 2026-05-08

---

## 핵심 규칙

팀원은 `_execute()` 메서드 하나만 구현한다. 나머지(검증, 재시도, 로깅, 가드레일, 메타데이터, 토큰 예산)는 BaseAgent/BaseTool 하네스가 자동 처리한다.

---

## 1. 사용 가능한 인스턴스 속성/메서드

`BaseAgent`를 상속하면 다음을 자동으로 사용할 수 있다.

| 속성/메서드 | 용도 |
|---|---|
| `self.llm` | LLM 호출 (재시도/캐싱/예산차단 자동) |
| `self.prompts` | `prompts/{agent}/system.md + template.md` 로드 |
| `self.with_correction_hint(prompt, last_error)` | 자가 수정 힌트 첨부 |
| `self.use_tool(name, params)` | 화이트리스트 검증된 Tool 호출 |
| `self.logger` | trace_id 자동 포함 구조화 로깅 |
| `self.trace_id` | 현재 실행 추적 ID |

---

## 2. 표준 구현 패턴

```python
import json

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import ExecuteResult


class MyAgent(BaseAgent):
    """역할: ..."""

    # 사용 가능 Tool 화이트리스트 (없으면 빈 리스트)
    allowed_tools = ["codebase_scanner", "domain_knowledge"]

    async def _execute(
        self,
        context: dict,
        params: dict,
        last_error: str | None = None,
    ) -> ExecuteResult:
        # 1. (선택) Tool 호출하여 컨텍스트 보강
        scan = await self.use_tool("codebase_scanner", {"path": "/repo"})

        # 2. 프롬프트 렌더링 + 자가 수정 힌트 첨부
        system_prompt = self.prompts.system()
        user_prompt = self.with_correction_hint(
            self.prompts.render(
                context=json.dumps(scan, ensure_ascii=False),
                input_data=json.dumps(params, ensure_ascii=False),
            ),
            last_error,
        )

        # 3. LLM 호출
        response = await self.llm.chat(system_prompt, user_prompt)

        # 4. 응답 파싱
        parsed = json.loads(response.content)

        # 5. ExecuteResult 반환
        return ExecuteResult(
            result={"items": parsed["items"]},
            confidence=parsed.get("confidence", 0.5),
        )
```

---

## 3. 케이스별 예시

### 케이스 1: Tool 없이 LLM만 사용 (자연어 해석)

```python
class NaturalLanguageAgent(BaseAgent):
    allowed_tools = []  # Tool 사용 안 함

    async def _execute(self, context, params, last_error=None) -> ExecuteResult:
        user_prompt = self.with_correction_hint(
            self.prompts.render(natural_text=params["text"]),
            last_error,
        )
        response = await self.llm.chat(self.prompts.system(), user_prompt)
        parsed = json.loads(response.content)
        return ExecuteResult(
            result={
                "given": parsed["given"],
                "when": parsed["when"],
                "then": parsed["then"],
            },
            confidence=parsed["confidence"],
        )
```

### 케이스 2: Tool 호출 → LLM 추론 (시나리오 생성)

```python
class ScenarioGeneratorAgent(BaseAgent):
    allowed_tools = ["codebase_scanner", "domain_knowledge"]

    async def _execute(self, context, params, last_error=None) -> ExecuteResult:
        # 코드 스캔 + 도메인 검색
        scan = await self.use_tool("codebase_scanner", {"path": params["repo_path"]})
        rules = await self.use_tool("domain_knowledge", {"query": params["feature"]})

        # LLM 호출 (Agent별 config로 model 오버라이드 가능)
        user_prompt = self.with_correction_hint(
            self.prompts.render(
                code_context=json.dumps(scan, ensure_ascii=False),
                domain_rules=json.dumps(rules, ensure_ascii=False),
                git_diff=params.get("git_diff", ""),
            ),
            last_error,
        )
        response = await self.llm.chat(
            self.prompts.system(), user_prompt, model="gpt-4o"
        )
        parsed = json.loads(response.content)

        return ExecuteResult(
            result={"scenarios": parsed["scenarios"]},
            confidence=parsed["confidence"],
        )
```

### 케이스 3: 규칙 1차 + LLM 2차 (장애 분류)

```python
class DefectClassifierAgent(BaseAgent):
    allowed_tools = []

    async def _execute(self, context, params, last_error=None) -> ExecuteResult:
        defect = params["defect"]

        # 규칙 기반 1차 분류 (LLM 미사용)
        rule_based = self._classify_by_rules(defect)
        if rule_based is not None:
            return ExecuteResult(
                result={"defect_type": rule_based, "rule_based": True},
                confidence=1.0,  # 규칙 기반이므로 확정
            )

        # 규칙 미분류 → LLM 보조 분류
        user_prompt = self.with_correction_hint(
            self.prompts.render(defect=json.dumps(defect)),
            last_error,
        )
        response = await self.llm.chat(self.prompts.system(), user_prompt)
        parsed = json.loads(response.content)
        return ExecuteResult(
            result={"defect_type": parsed["type"], "rule_based": False},
            confidence=parsed["confidence"],
        )

    def _classify_by_rules(self, defect: dict) -> str | None:
        """LLM 호출 없는 결정적 규칙."""
        if defect.get("status_code", 0) >= 500:
            return "api"
        if "selector not found" in defect.get("error", "").lower():
            return "ui"
        return None
```

---

## 4. 팀원이 신경쓰지 않아도 되는 것

`_execute()` 안에서 아래는 전부 자동이다.

- `try / except` 블록 (하네스가 처리)
- 재시도 (LLMClient 3회 + run() max_retry회 자동)
- 타임아웃 처리 (run()이 `asyncio.wait_for`로 감쌈)
- trace_id 전파 (`self.llm`, `self.use_tool`이 자동 주입)
- 토큰 카운팅 (`self.llm.total_tokens` 자동 누적)
- 토큰 예산 차단 (한도 초과 시 자동 실패)
- 메타데이터 구성 (run()이 BaseMetadata 자동 생성)
- 입출력 가드레일 (run() 앞뒤에서 자동 호출)
- Pydantic 검증 (AgentInput/AgentOutput 자동)
- 로그에 trace_id 추가 (`self.logger` 자동)

---

## 5. 프롬프트 작성

`prompts/{agent_name}/system.md`와 `template.md`를 작성한다.

`agent_name`은 클래스명에서 자동 파생된다. `ScenarioGeneratorAgent` → `scenario_generator`.

### system.md (역할/규칙/출력 형식)

```markdown
# 시나리오 생성 Agent

## 역할
코드베이스 분석 결과와 도메인 규칙을 기반으로 테스트 시나리오를 생성한다.

## 규칙
- 출력은 반드시 JSON으로만 응답한다.
- confidence는 0.0~1.0 사이의 float로 반환한다.
- 근거 없는 시나리오는 생성하지 않는다.

## 출력 형식
{
  "scenarios": [
    {"ts_id": "TS-001", "name": "...", "test_cases": [...]}
  ],
  "confidence": 0.85
}
```

### template.md (변수 바인딩, `{{key}}` 형식)

```markdown
## 코드 컨텍스트
{{code_context}}

## 도메인 규칙
{{domain_rules}}

## Git 변경
{{git_diff}}

위를 종합하여 테스트 시나리오를 JSON으로 생성하라.
```

---

## 6. Agent별 설정 (`qapilot.config.yaml`)

```yaml
agent:
  timeout_sec: 60       # 글로벌 기본
  max_retry: 3
  overrides:
    scenario_generator:
      timeout_sec: 120
      model: "gpt-4o"
      allowed_tools: ["codebase_scanner", "domain_knowledge"]
    cross_check:
      timeout_sec: 60
      model: "gpt-4o-mini"
      allowed_tools: []
```

`allowed_tools`는 클래스 속성으로도 정의할 수 있고 config로도 오버라이드 가능하다.

---

## 7. Tool 구현

Tool은 결정적(LLM 미사용)이므로 더 단순하다.

```python
from qapilot.tools.base_tool import BaseTool


class CodebaseScannerTool(BaseTool):
    """코드베이스 스캔 Tool."""

    async def _execute(self, params: dict) -> dict:
        path = params["path"]
        # AST 파싱, 엔드포인트 추출 등
        return {
            "files": [...],
            "endpoints": [...],
            "callgraph": {...},
        }
```

Tool에는 다음이 없다:

- `confidence` (결정적이므로 불필요)
- 재시도 (실패하면 진짜 실패)
- LLM 호출
- 가드레일 (LLM 미사용이므로)
- `last_error` (자가 수정 불필요)

Tool도 타임아웃, 에러 래핑, 로깅, 메타데이터는 자동 처리된다.

---

## 8. ExecuteResult 반환 규칙

```python
ExecuteResult(
    result={...},      # 다음 Agent에게 전달할 데이터 (dict)
    confidence=0.85,   # 0.0~1.0 사이 float
)
```

- `result`는 다음 Agent의 입력으로 그대로 전달되므로, 후속 Agent가 기대하는 형식을 따라야 한다 (`shared/schemas.py`의 TypedDict 참조).
- `confidence`는 LLM 자가 평가 + 매칭 기반 + 근거 기반을 조합하여 산출한다.
  - 시나리오 생성·자연어 해석 Agent: 대시보드에서 사용자에게 함께 표시됨 (검토 참고용)
  - 그 외 Agent: 로그/리포트에만 기록됨

---

## 9. 흐름 요약

```text
1. 팀원이 _execute() 호출됨
   ├─ 입력 가드레일은 이미 통과됨 (run()이 자동 처리)
   └─ context, params, last_error를 받음

2. _execute() 내부 (팀원 영역):
   ├─ self.use_tool() 로 Tool 호출
   ├─ self.prompts.render() + self.with_correction_hint() 로 프롬프트 구성
   ├─ self.llm.chat() 로 LLM 호출
   └─ ExecuteResult 반환

3. ExecuteResult 반환 후 (run() 자동):
   ├─ 출력 가드레일 검증
   ├─ 메타데이터 수집 (tokens, duration, retry_count)
   ├─ 구조화 로깅
   └─ AgentOutput 반환
```

---

## 10. 자주 하는 실수

### ❌ try/except로 직접 처리

```python
async def _execute(self, ...):
    try:
        response = await self.llm.chat(...)
    except Exception as e:
        return ExecuteResult(result={}, confidence=0.0)  # 잘못됨!
```

→ 하네스가 재시도/로깅을 처리하므로 그냥 raise하면 된다. 예외를 삼키면 자가 수정 루프가 작동하지 않는다.

### ❌ trace_id 직접 처리

```python
async def _execute(self, ...):
    logger.info(f"trace_id={self.trace_id} 시작")  # 잘못됨!
```

→ `self.logger`를 그대로 쓰면 trace_id가 자동 포함된다.

### ❌ `confidence` 누락

```python
return ExecuteResult(result={"items": [...]}, confidence=None)  # 에러!
```

→ `confidence`는 `0.0~1.0` 필수. LLM이 자가 평가하지 못하면 매칭/근거 기반으로 결정적 산출.

### ❌ Tool을 직접 import해서 호출

```python
from qapilot.tools.codebase_scanner_tool import CodebaseScannerTool

async def _execute(self, ...):
    tool = CodebaseScannerTool()
    result = await tool.run(...)  # 잘못됨!
```

→ `self.use_tool()`을 사용해야 화이트리스트 검증이 적용된다.
