# 로깅 & 비용 추적 가이드

QApilot의 구조화 로깅과 LLM 비용 추적 방식. 하네스(`BaseAgent` / `BaseTool` /
`LLMClient`)에 내장돼 있으므로 Agent/Tool 개발자가 따로 신경 쓸 게 없다 —
`_execute()`만 구현하면 로깅·비용 집계가 자동으로 따라온다.

관련 코드: `shared/logger.py`, `shared/pricing.py`, `shared/llm_client.py`,
`agents/base_agent.py`, `tools/base_tool.py`, `orchestrator/runner.py`.

---

## 1. 로그가 쌓이는 곳 — 3-sink

| Sink | 경로 | 포맷 | 회전/보존 | 용도 |
|---|---|---|---|---|
| 콘솔(stdout) | — | 사람이 읽는 텍스트 + 레벨/이벤트 색상 | — | 개발 중 즉시 확인 |
| 일자별 파일 | `.qapilot/logs/qapilot.log` → 자정에 `qapilot-{YYYY-MM-DD}.log` | JSON Lines | 30일 보관 | 누적 운영 로그, 집계 |
| trace별 파일 | `.qapilot/logs/traces/{trace_id}.jsonl` | JSON Lines | 영구 | "이 실행만" 디버깅·재현 |

- `.qapilot/` 전체가 `.gitignore` 대상이라 로그 파일은 커밋되지 않는다.
- trace 파일은 `trace_id`가 바인딩된 이벤트만 기록한다 (Agent/파이프라인 실행).
  `qapilot --help` 같은 trace_id 없는 호출은 콘솔·일자별에만 남는다.
- `setup_logger()`는 CLI 콜백 / FastAPI `create_app()`에서 호출된다. 단일 Agent를
  단독 실행할 때는 `get_logger()`가 미설정 상태를 감지해 자동 초기화한다.

### 콘솔 vs 파일 — 같은 이벤트, 다른 렌더링

```
# 콘솔
2026-05-12T07:33:58Z [error    ] agent_failed   source=dummy trace_id=t_abc retries=2 last_error=RuntimeError: boom

# .qapilot/logs/qapilot.log  /  traces/t_abc.jsonl
{"source":"dummy","trace_id":"t_abc","retries":2,"last_error":"RuntimeError: boom","event":"agent_failed","level":"error","timestamp":"2026-05-12T07:33:58Z"}
```

---

## 2. 로그 레벨 가이드라인

| 레벨 | 의미 | 예 |
|---|---|---|
| `INFO` | 상태 변화 | pipeline/agent/tool 시작·완료 |
| `WARNING` | 일시적 이상 (자동 복구됨) | retry, timeout |
| `ERROR` | 영구 실패 + 보안/통제 차단 사건 | agent_failed, guardrail_blocked, budget_exceeded, tool_denied |
| `DEBUG` | 진단용 (운영 환경 기본 OFF) | 캐시 히트, asyncio 내부 등 |

Agent/Tool 개발자가 도메인 이벤트를 추가로 남길 때도 이 기준을 따른다
(예: `requirement_extractor`의 `requirements_extracted` = INFO).

---

## 3. 이벤트 카탈로그

모든 이벤트는 공통 자동 필드를 갖는다: `timestamp`, `level`, `source`, `event`,
그리고 (바인딩됐다면) `trace_id`.

### Orchestrator (`source=orchestrator`)
| 이벤트 | 레벨 | 페이로드 |
|---|---|---|
| `pipeline_start` | INFO | `command`, `trigger`, `filter` |
| `pipeline_complete` | INFO | `status`, `total_cost_usd`, `trace_id` |
| `pipeline_failed` | ERROR | `error` |
| `node_complete` | INFO | *(미구현 — LangGraph 노드 구현 PR에서 추가 예정)* |

### Agent (`source={agent_name}`)
| 이벤트 | 레벨 | 페이로드 |
|---|---|---|
| `agent_start` | INFO | `params_keys` |
| `agent_complete` | INFO | `confidence`, `duration_sec`, `tokens`, `cost_usd`, `retries` |
| `agent_retry` | WARNING | `attempt`, `error` |
| `agent_timeout` | WARNING | `attempt` |
| `agent_failed` | ERROR | `retries`, `last_error` |
| `input_guardrail_blocked` | ERROR | `code`, `detail` |
| `output_guardrail_blocked` | ERROR | `code`, `detail` |
| `tool_denied` | ERROR | `tool`, `allowed` |

### Tool (`source={ToolClassName}`)
| 이벤트 | 레벨 | 페이로드 |
|---|---|---|
| `tool_complete` | INFO | `duration_sec` |
| `tool_timeout` | ERROR | — |
| `tool_error` | ERROR | `error` |

### LLM (`source=llm_client`)
| 이벤트 | 레벨 | 페이로드 |
|---|---|---|
| `llm_retry` | WARNING | `attempt`, `model`, `error` |
| `llm_cache_hit` | DEBUG | `model` |
| `llm_budget_exceeded` | ERROR | `used`, `limit` |

---

## 4. 비용 추적 — 동작 원리

### 측정 지점은 한 곳: `LLMClient.chat()`

Agent가 OpenAI SDK를 직접 부르면 계측이 새므로, 하네스는 `self.llm`(`LLMClient`)만
노출한다. 모든 LLM 호출이 `chat()` 한 곳을 지나므로 거기서만 계측한다.

```
Agent._execute()  →  self.llm.chat()  →  ChatOpenAI.ainvoke()  →  usage_metadata
                          │
                          ├─ input_tokens / output_tokens 추출 (API 실측값)
                          ├─ pricing.calc_cost(model, in, out) → USD
                          └─ LLMClient 인스턴스에 누적 (total_input_tokens / total_output_tokens / total_cost_usd)
```

- **토큰은 추정이 아니라 실측**: `tiktoken` 사전 추정이 아니라 응답의
  `usage_metadata`(공급자가 청구 기준으로 돌려주는 값)를 쓴다.
- **입력/출력 분리**: GPT-4o는 출력 토큰이 입력의 4배($2.5 vs $10 / 1M)라
  합산 토큰만으로는 비용을 못 맞춘다. `LLMResponse`에 `input_tokens` /
  `output_tokens`를 따로 보관한다.
- **단가는 데이터로 분리** (`shared/pricing.py`의 `MODEL_PRICING`): 가격이 바뀌면
  표만 고친다. 모르는 모델은 0.0으로 처리하되 토큰 수는 그대로 기록 → 표를 채운 뒤
  사후 보정 가능. `gpt-4o-2024-08-06` 같은 날짜 접미사 변형은 접두사 매칭으로 흡수.
- **누적 단위 = "한 번의 실행"**: `LLMClient`는 Agent 1회 실행당 1개 생성된다
  (`BaseAgent.__init__`). 그래서 `llm.total_cost_usd`가 곧 "이 Agent 이번 실행 비용".
  캐시 히트는 비용 0.

### 비용이 노출되는 경로

| 위치 | 무엇 |
|---|---|
| `agent_complete` 로그 | `cost_usd=0.0234` |
| `AgentOutput.metadata.cost_usd` | Pydantic 메타데이터 |
| `PipelineState.total_cost` | `runner`가 `agent_logs[].cost_usd` 합산 (노드가 `agent_logs`를 채우면 자동) |
| `pipeline_complete` 로그 | `total_cost_usd` |

### 비용 통제와 같은 지점

`LLMClient.chat()`은 호출 전 `total_tokens >= max_tokens_per_task`를 검사해
초과 시 `llm_budget_exceeded`를 남기고 `QApilotError(SYSTEM_002)`로 차단한다.
측정 지점 = 통제 지점이라, 예산 초과로 폭주하는 Agent를 래퍼가 끊는다.

### 단가표 관리

`shared/pricing.py`의 `MODEL_PRICING`에 `모델명 → (입력단가, 출력단가)` (USD/1K tokens)
형태로 추가/수정한다. 새 모델을 쓰기 시작하면 여기에 한 줄 추가.

### 알려진 한계

- 단가표 수동 관리 — 가격 변동 반영에 지연 가능
- 미등록 모델은 비용 0 처리 (토큰은 기록되므로 사후 보정 가능)
- OpenAI가 부분 생성 후 연결이 끊기면 `usage_metadata`가 없어 과소 계상될 수 있음
- 임베딩(`sentence-transformers`)은 로컬 모델이라 비용 0 — API 임베딩으로 바꾸면 단가표에 추가 필요

---

## 5. 실전 — `jq`로 조회하기

```bash
# 특정 실행 추적
less .qapilot/logs/traces/t_abc123.jsonl

# 그 실행에 든 총 비용
jq -s '[.[] | select(.event=="agent_complete") | .cost_usd] | add' \
   .qapilot/logs/traces/t_abc123.jsonl

# 오늘 전체 비용 합계
jq -s '[.[] | select(.event=="agent_complete") | .cost_usd] | add' \
   .qapilot/logs/qapilot-$(date +%F).log

# 오늘 실패한 trace_id 목록
jq -r 'select(.event=="agent_failed" or .event=="pipeline_failed") | .trace_id' \
   .qapilot/logs/qapilot-$(date +%F).log | sort -u

# Agent별 비용·토큰 요약 (오늘)
jq -s 'map(select(.event=="agent_complete")) | group_by(.source)
       | map({agent: .[0].source, calls: length,
              cost_usd: (map(.cost_usd) | add), tokens: (map(.tokens) | add)})' \
   .qapilot/logs/qapilot-$(date +%F).log

# 보안 차단 사건 (가드레일/예산)
jq 'select(.event | test("guardrail_blocked|budget_exceeded|tool_denied"))' \
   .qapilot/logs/qapilot-$(date +%F).log
```

단일 Agent를 단독 실행해도 같은 로그가 떨어진다 — 파이프라인을 거치든 안 거치든
`BaseAgent.run()`을 타기 때문. "이 1회 실행에 얼마/몇 토큰 들었나"는 그 trace 파일
하나만 보면 된다.

---

## 6. 범위 외 (별도 이슈)

- 월 예산(`monthly_budget_usd`) 초과 시 파이프라인 차단 — 영속 비용 로그 필요
- 누적 비용 조회 API / 대시보드 표시
- `node_complete` — LangGraph 노드 구현 시 노드 래퍼로 추가
