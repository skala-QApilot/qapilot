# QApilot 구현 플랜

> **Version**: 1.0  
> **최종 수정일**: 2026-05-07  
> **기반 문서**: 요구사항정의서 v0.4 / 개발표준정의서 v0.4  
> **변경 이력**:  
> - v1.0 (2026-05-07): 최초 작성. Orchestrator 고정 DAG 전환, HITL 범위 축소, 리포트 단일 형식 반영
> - v1.1 (2026-05-07): generate/test 파이프라인 분리 확정. generate=Layer1만, test=Layer2~3만

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 프로젝트명 | QApilot — AI 기반 테스트 시나리오/데이터 생성 및 통합 테스트 자동화 및 오류 분석 시스템 |
| 유형 | Agentic AI (신규 개발) |
| 기간 | 2026-04-17 ~ 2026-06-23 |
| 비전 | NPT(No People Testing) — 완전 무인 테스트 운영. 사람 개입은 시나리오 승인(HITL)으로만 제한 |
| 적용 대상 | 웹 애플리케이션 + REST API + RDB 기반 서비스 |
| 사용자 그룹 | QA 담당자, 개발자, 개발 리더, PM |

### 1.1 기대 효과

- 시나리오 작성 공수 80% 절감
- QA 실행 인력 60~80% 축소
- 배포 주기 2주 → 2~3일 단축
- 결함 분석 시간 90% 단축
- RTM 작성 공수 95% 감소

---

## 2. 시스템 아키텍처

### 2.1 전체 구조 (2개 독립 파이프라인 + 고정 DAG)

generate와 test는 **독립된 파이프라인**으로 분리된다.
generate의 산출물(.qapilot/scenarios/, .qapilot/generated-code/)을 test가 로드하여 실행한다.

```
═══════════════════════════════════════════════════════════════════════
  [qapilot generate] — Layer 1 파이프라인
═══════════════════════════════════════════════════════════════════════

  코드스캔Tool → 도메인지식Tool → 요구사항추출Agent
  → 시나리오생성Agent → [HITL 승인] → 액션매핑Agent → 코드생성Agent
  → .qapilot/scenarios/ + .qapilot/generated-code/ 에 저장

  * 자연어 입력 시 별도 진입점:
    자연어해석Agent → [HITL 승인] → 액션매핑Agent → 코드생성Agent

═══════════════════════════════════════════════════════════════════════
  [qapilot test] — Layer 2~3 파이프라인
═══════════════════════════════════════════════════════════════════════

  저장된 시나리오/코드 로드
  → UI테스트Tool ─┐
    API추적Tool  ─┼─(병렬)─→ Cross-check Agent
    DB테스트Tool ─┘
  → (불일치 시) 장애분류Agent → 원인추론Agent → 해결방안추천Agent
  → 리포트Tool (단일 형식)

═══════════════════════════════════════════════════════════════════════
```

- **generate**: 시나리오와 테스트 코드를 생성하고 HITL 승인까지 완료. 산출물을 로컬에 저장
- **test**: 이미 승인된 시나리오/코드를 로드하여 실행. HITL 미발생. 야간 무인 실행 가능

### 2.2 Orchestrator 설계 결정

| 항목 | 결정 |
|---|---|
| 방식 | **사전 정의된 고정 DAG** (LLM 미사용) |
| 구현체 | LangGraph StateGraph **1개** |
| 분기 | `command`로 진입점 분기 (generate→Layer1, test→Layer2~3) + `has_mismatch`로 Layer 3 실행 여부 |
| 근거 | 파이프라인 흐름이 완전 확정적. LLM 판단이 필요한 지점 없음. 안정성·속도·비용 모두 우위 |

```python
graph = StateGraph(PipelineState)

# ── Layer 1 노드 (generate) ──
graph.add_edge("codebase_scan", "domain_knowledge")
graph.add_edge("domain_knowledge", "requirement_extract")
graph.add_edge("requirement_extract", "scenario_generate")
graph.add_edge("scenario_generate", "hitl_review")        # HITL 필수
graph.add_edge("hitl_review", "action_mapping")
graph.add_edge("action_mapping", "code_generate")
graph.add_edge("code_generate", END)                      # generate 종료

# ── Layer 2~3 노드 (test) ──
graph.add_edge("load_scenarios", "test_execution")        # UI/API/DB 병렬
graph.add_edge("test_execution", "cross_check")
graph.add_conditional_edges(                               # Layer 3 조건부
    "cross_check",
    lambda state: "defect_classify" if state["has_mismatch"] else "report",
)
graph.add_edge("defect_classify", "root_cause")
graph.add_edge("root_cause", "fix_recommend")
graph.add_edge("fix_recommend", "report")
graph.add_edge("report", END)

# ── 진입점 분기 ──
graph.add_conditional_edges(
    START,
    lambda state: "codebase_scan" if state["command"] == "generate"
                  else "load_scenarios",
)
```

### 2.3 모듈 간 호출 규칙

```
호출자 \ 피호출자  │ Agent │ Tool  │ Orchestrator
─────────────────┼───────┼───────┼─────────────
Agent            │ 금지  │ 참조용│ 금지
Tool             │ 금지  │ 금지  │ 금지
Orchestrator     │ 실행  │ 실행  │ —
```

- Agent 간 직접 호출 금지 — 반드시 Orchestrator(고정 DAG) 경유
- Agent는 참조용 Tool(코드 인덱스, 도메인 검색)만 직접 호출 가능

---

## 3. 전체 구성요소 목록

### 3.1 Orchestrator (고정 DAG 파이프라인 러너) — 1개

| FR | 이름 | LLM | 역할 |
|---|---|---|---|
| FR-013 | Orchestrator | X | DAG 실행 관리, trace_id 발급, 순서/병렬화/재시도, 선택적 실행(--case/--failed/--affected/--tag) |

### 3.2 Agent (LLM 사용) — 9개

| FR | 이름 | Layer | 핵심 역할 | HITL |
|---|---|---|---|---|
| FR-024 | 요구사항 추출 Agent | L1 | PRD에서 REQ-XXX 구조화 추출, RTM 행 생성 | X |
| FR-002 | 시나리오 생성 Agent | L1 | 코드+도메인+Git diff 기반 정상/엣지 시나리오+테스트데이터 생성 | **O** |
| FR-003 | 자연어 요구사항 해석 Agent | L1 | 자연어 → Given/When/Then 구조화 | **O** |
| FR-004 | 시나리오-액션 매핑 Agent | L1 | 시나리오 → UI액션+API매핑+검증포인트 분해 | X |
| FR-005 | Playwright 코드 생성 Agent | L1 | 액션 시퀀스 → Playwright JS 코드 | X |
| FR-008 | Cross-check Agent | L2 | UI↔API↔DB 데이터 정합성 검증, 정합성 점수 산출 | X |
| FR-009 | 장애 분류 Agent | L3 | 규칙 1차 + LLM 보조 → 5개 카테고리 분류 | X |
| FR-010 | 원인 추론 Agent | L3 | 로그+코드+Git → Top-N 원인 후보 + 근거 3종 | X |
| FR-011 | 해결 방안 추천 Agent | L3 | 파일경로+담당자+수정 snippet 제시 | X |

### 3.3 Tool (결정적, LLM 미사용) — 6개

| FR | 이름 | 역할 |
|---|---|---|
| FR-000 | 코드베이스 스캔 Tool | AST 파싱, 엔드포인트/모델/종속성/callgraph 추출, 증분 재분석 |
| FR-001 | 도메인 지식 Tool (RAG) | 문서 임베딩, Qdrant 벡터 검색, 용어사전(glossary.json) |
| FR-006 | UI 테스트 Tool | Playwright 코드 실행, 스크린샷 캡처, headed/headless |
| FR-007 | API 추적 Tool | 네트워크 리스너, 요청/응답 수집, trace_id 매칭 |
| FR-020 | DB 테스트 Tool | 전후 스냅샷, SQL 추적, 시드 주입/자동 롤백 |
| FR-012 | 리포트 Tool | 테스트 결과 리포트 생성 (단일 형식) |

### 3.4 Module (인프라/UI/설정) — 9개

| FR | 이름 | 역할 |
|---|---|---|
| FR-014 | HITL 모듈 | 시나리오 생성/자연어 해석 결과의 승인/수정/반려 |
| FR-015 | Trace ID 모듈 | UUID v4 발급, UI→API→DB 전 계층 전파 |
| FR-016 | CLI + 웹 대시보드 | Typer CLI + React 대시보드 (포트 7860) |
| FR-017 | 캐시 모듈 | .qapilot/ 디렉토리, LLM 캐시, Git hash 기반 증분 |
| FR-018 | 스케줄링 모듈 | 자동 회귀 테스트 배치 실행 (완전 무인) |
| FR-019 | 진척률 대시보드 | RTM, 메트릭 시각화, CSV export |
| FR-021 | 증적 캡처-보관 모듈 | 스크린샷/API 본문/로그 trace_id 기준 영구 보관 |
| FR-022 | 요구사항 변경 히스토리 | 버전별 diff, 영향 시나리오 자동 식별 |
| FR-023 | 시나리오 의존성 그래프 | D3.js/Cytoscape.js DAG 시각화 |

---

## 4. 핵심 정책 (요구사항 문서 대비 변경 사항)

### 4.1 HITL 정책

> **요구사항 문서와 달리, 아래 정책으로 확정합니다.**

| 항목 | 정책 |
|---|---|
| 적용 지점 | **시나리오 생성(FR-002)** + **자연어 해석(FR-003)** 이 2곳만 |
| 적용 방식 | confidence 무관하게 **항상** HITL 큐 적재 → 승인 후에만 후속 진행 |
| 나머지 Agent | confidence는 로그/리포트 참조용으로만 기록. 자체 Fallback 처리 |
| 야간 실행(FR-018) | 이미 승인된 시나리오만 실행 → **HITL 미발생, 완전 무인** |

```
[시나리오 생성 / 자연어 해석]
  → 항상 HITL 큐 적재
  → 사용자 승인/수정/반려
  → 승인 후에만 액션 매핑으로 진행

[그 외 Agent: Cross-check, 장애분류, 원인추론, 해결추천 등]
  → HITL 큐에 적재하지 않음
  → confidence < 임계값 → 자체 Fallback (재시도, 규칙 기반 등)
  → confidence는 리포트/로그에 기록
```

### 4.2 리포트 정책

> **요구사항 문서의 3종(임원/QA/개발자) 대신 단일 형식으로 확정합니다.**

**테스트 결과 리포트 — 단일 형식**:
- 실행 요약: 총 건수, 성공/실패/스킵, 소요 시간
- 실패 케이스: 시나리오ID, 실패 스텝, 스크린샷
- 원인 분석: Top-N 원인, 신뢰도, 근거
- 해결 방안: 파일 경로, 담당자, 수정 코드 snippet

### 4.3 Orchestrator 정책

> **요구사항 문서의 "Orchestrator Agent(LLM)" 대신 고정 DAG로 확정합니다.**

| 항목 | 정책 |
|---|---|
| 방식 | LangGraph StateGraph 기반 사전 정의 DAG |
| LLM 사용 | 없음 |
| 분기 | `has_mismatch` boolean 조건만 (Layer 3 실행 여부) |
| 선택적 실행 | CLI 옵션 파싱 기반 (`--case / --failed / --affected / --tag`) |
| 재시도 | 규칙 기반 (최대 3회, exponential backoff) |

### 4.4 파이프라인 분리 정책

> **generate와 test는 독립된 파이프라인으로 실행합니다. 1→2→3 한 번에 실행하는 경로는 없습니다.**

| 항목 | generate | test |
|---|---|---|
| 실행 명령 | `qapilot generate` | `qapilot test [옵션]` |
| 실행 범위 | Layer 1만 | Layer 2~3만 |
| 입력 | 코드베이스 + 도메인 문서 (+ 자연어 입력) | .qapilot/에 저장된 시나리오/코드 |
| 산출물 | .qapilot/scenarios/ + .qapilot/generated-code/ | .qapilot/results/ + 리포트 |
| HITL | 발생 (시나리오 승인) | 미발생 (승인 완료된 시나리오만 실행) |
| 야간 무인 | 불가 (HITL 대기) | **가능** |

```
사용자 워크플로우:

1) qapilot generate              → 시나리오 생성 + HITL 승인
2) qapilot test                  → 승인된 전체 시나리오 테스트
3) qapilot test --failed         → 이전 실패 건만 재실행
4) qapilot test --case TS-001    → 특정 시나리오만 실행
5) qapilot test --affected       → Git 변경분 영향 시나리오만 실행
6) qapilot test --tag payment    → 태그 기반 필터 실행
```

---

## 5. 기술 스택

| 구분 | 기술 | 비고 |
|---|---|---|
| AI 서버 | FastAPI (Python 3.11+) | 비동기 네이티브 |
| 중앙 API | SpringBoot | Gateway, 인증/인가 |
| 프론트엔드 | React 18 + TypeScript | 대시보드 (Vite, shadcn/ui, Tailwind, Recharts) |
| CLI | Typer (Python) | |
| Agent 프레임워크 | LangGraph | StateGraph로 고정 DAG 구성 |
| LLM | OpenAI API | gpt-4o-mini / gpt-4o / o3-mini (3-tier) |
| 테스트 실행 | Playwright (Python) | |
| AST 파싱 | ast (내장) + tree-sitter | |
| 벡터 DB | Qdrant (Docker) | 포트 6333 |
| RDB | PostgreSQL (Docker) | |
| 로깅 | 구조화 JSON 로깅 | |
| 컨테이너 | Docker Compose | |

### 5.1 LLM 비용 Tier

| Tier | 용도 | LLM | 비용 |
|---|---|---|---|
| Tier 1 | 구조 스캔 | 미사용 | ~$0.01 |
| Tier 2 | 타겟 분석 | gpt-4o-mini | ~$0.30/시나리오 |
| Tier 3 | 심층 분석 | gpt-4o | ~$1.00/건 |

---

## 6. 디렉토리 구조

```
qapilot/
├── agents/                          # Agent 모듈 (9개)
│   ├── base_agent.py                # BaseAgent ABC
│   ├── requirement_extractor_agent.py
│   ├── scenario_generator_agent.py
│   ├── natural_language_agent.py
│   ├── action_mapper_agent.py
│   ├── code_generator_agent.py
│   ├── cross_check_agent.py
│   ├── defect_classifier_agent.py
│   ├── root_cause_agent.py
│   └── fix_recommender_agent.py
├── tools/                           # Tool 모듈 (6개)
│   ├── base_tool.py                 # BaseTool ABC
│   ├── codebase_scanner_tool.py
│   ├── domain_knowledge_tool.py
│   ├── ui_test_tool.py
│   ├── api_trace_tool.py
│   ├── db_test_tool.py
│   └── report_tool.py
├── orchestrator/                    # 고정 DAG 파이프라인
│   ├── pipeline.py                  # LangGraph StateGraph 정의 (단일 그래프)
│   ├── state.py                     # PipelineState 정의
│   └── runner.py                    # CLI에서 호출하는 실행 엔트리포인트
├── modules/                         # 인프라 모듈
│   ├── hitl_module.py
│   ├── trace_module.py
│   ├── cache_module.py
│   ├── schedule_module.py
│   └── evidence_module.py
├── cli/                             # Typer CLI
│   └── main.py
├── api/                             # FastAPI 라우터
│   ├── agent_router.py
│   ├── scenario_router.py
│   ├── defect_router.py
│   ├── hitl_router.py
│   └── report_router.py
├── web/dist/                        # React 빌드 결과물 (QApilot-UI)
├── shared/                          # 공통 유틸/타입/인터페이스
│   ├── schemas.py                   # AgentInput/Output, ToolInput/Output
│   ├── errors.py                    # 에러 코드 체계
│   ├── config.py                    # 설정 로드
│   └── logger.py                    # 구조화 JSON 로깅
├── prompts/                         # Agent별 프롬프트 템플릿
│   ├── scenario_generator/
│   │   ├── system.md
│   │   └── template.md
│   ├── action_mapper/
│   ├── cross_check/
│   ├── defect_classifier/
│   ├── root_cause/
│   ├── fix_recommender/
│   ├── natural_language/
│   └── requirement_extractor/
├── pyproject.toml
├── .env.example
└── qapilot.config.yaml
```

### 6.1 로컬 실행 시 생성되는 디렉토리

```
.qapilot/                            # 대상 프로젝트 루트에 생성
├── codebase-index/                  # FR-000 코드 분석 결과 캐시
│   ├── endpoints.json
│   ├── models.json
│   ├── callgraph.json
│   └── manifest.json                # Git commit hash 기준
├── domain/                          # FR-001 도메인 지식 인덱스
├── scenarios/                       # generate 산출물: 시나리오
│   ├── {시나리오ID}.json             # TS/TC/TV 3단계 구조
│   ├── raw/                         # 자연어 원본
│   └── regression/                  # 회귀 테스트 자산
├── generated-code/                  # generate 산출물: Playwright 코드
│   └── {TC-ID}.js                   # TC별 생성 코드
├── results/{trace_id}/              # 테스트 결과
│   └── TS-001/
│       ├── TC-001/
│       │   ├── ui_result.json
│       │   ├── api_result.json
│       │   ├── db_result.json
│       │   └── screenshots/
│       └── summary.json
├── evidence/{trace_id}/             # FR-021 증적 보관
├── reports/                         # 리포트
├── logs/{날짜}/{trace_id}.log       # 구조화 로그
└── cache/                           # LLM 응답 캐시
```

---

## 7. 공통 개발 규칙 요약

### 7.1 입출력 스키마

```python
# Agent 입출력
class AgentInput(BaseModel):
    trace_id: str
    context: dict[str, Any]
    params: dict[str, Any]

class AgentOutput(BaseModel):
    trace_id: str
    result: dict[str, Any]
    confidence: float          # 0.0~1.0 필수
    metadata: BaseMetadata     # model, tokens_used, duration_sec 등

# Tool 입출력
class ToolInput(BaseModel):
    trace_id: str
    params: dict[str, Any]

class ToolOutput(BaseModel):
    trace_id: str
    result: dict[str, Any]
    metadata: dict[str, Any]   # confidence 불필요 (결정적)
```

### 7.2 API 표준

| URI | Method | 설명 |
|---|---|---|
| /api/agent/run | POST | 테스트 실행 요청 |
| /api/scenarios | GET / POST | 시나리오 목록 / 생성 |
| /api/scenarios/{id} | GET / PUT | 시나리오 상세 / 수정 |
| /api/defects | GET | 결함 목록 |
| /api/defects/{id} | GET | 결함 상세 |
| /api/hitl/pending | GET | HITL 대기 목록 |
| /api/hitl/{id}/approve | POST | HITL 승인 |
| /api/hitl/{id}/reject | POST | HITL 반려 |
| /api/reports | GET | 리포트 목록 |
| /api/reports/{trace_id} | GET | 실행별 리포트 |

- 응답 포맷: `{ success: bool, data: {...} }` / 실패: `{ success: false, error: { code, message } }`
- Agent API: trace_id + confidence 필수 포함

### 7.3 네이밍 규칙

| 대상 | 규칙 | 예시 |
|---|---|---|
| 파일 | snake_case | scenario_generator.py |
| 변수/함수 | snake_case | generate_scenario() |
| 클래스 | PascalCase | ScenarioAgent |
| 상수 | UPPER_SNAKE_CASE | MAX_RETRY_COUNT |
| Agent 파일 | {이름}_agent.py | scenario_generator_agent.py |
| Tool 파일 | {이름}_tool.py | codebase_scanner_tool.py |

### 7.4 코드 작성 규칙

- Type Hint 필수 (모든 함수 인자 + 반환값)
- Black (line-length: 100)
- Ruff 린팅
- 함수 최대 50줄, 파일 최대 300줄
- Docstring: Google Style
- import 순서: 표준 → 서드파티 → 로컬 (isort)

### 7.5 에러 처리

- 에러 코드 체계: `AGENT_XXX` / `TOOL_XXX` / `SYSTEM_XXX`
- 모든 에러 로그에 trace_id 필수 포함
- LLM API 실패: 최대 3회 재시도 (exponential backoff)
- Agent 타임아웃: 기본 60초 (config 변경 가능)

### 7.6 가드레일

- 입력: 프롬프트 인젝션 감지, 금지 패턴 필터
- 출력: Pydantic 스키마로 형식 강제
- 행동: Agent별 호출 가능 Tool을 config에 화이트리스트 정의
- 데이터: .env, DB 접속정보, 개인정보, DB 원본 → LLM 전송 금지

---

## 8. CLI 명령어

### 8.1 공통 명령어

| 명령어 | 동작 | 파이프라인 |
|---|---|---|
| `qapilot init` | 프로젝트 초기화 + 코드 스캔 | - |
| `qapilot spec import <파일>` | 도메인 문서 임베딩 | - |
| `qapilot rescan` | 코드 인덱스 재생성 | - |
| `qapilot explain <id>` | 결함 원인 분석 (단건 조회) | - |

### 8.2 generate 명령어 (Layer 1)

| 명령어 | 동작 |
|---|---|
| `qapilot generate` | 시나리오 자동 생성 → HITL 승인 → 코드 생성 → 저장 |
| `qapilot generate --affected` | Git 변경분 영향 범위만 시나리오 생성 |

### 8.3 test 명령어 (Layer 2~3)

| 명령어 | 동작 |
|---|---|
| `qapilot test` | 승인된 전체 시나리오 테스트 실행 |
| `qapilot test --case {id}` | 특정 시나리오만 실행 |
| `qapilot test --failed` | 이전 실패 건만 재실행 |
| `qapilot test --affected` | Git 변경분 영향 시나리오만 실행 |
| `qapilot test --tag {태그}` | 태그 기반 필터 실행 |

---

## 9. 구현 Phase

### Phase 0: 프로젝트 기반 구축 (Week 1)

| # | 작업 | 검증 |
|---|---|---|
| 1 | 디렉토리 구조 + pyproject.toml + .env.example | pip install -e . 성공 |
| 2 | Docker Compose (PostgreSQL + Qdrant) | docker compose up 기동 확인 |
| 3 | shared/ 공통 모듈: 스키마(AgentInput/Output, ToolInput/Output), BaseAgent/BaseTool ABC, trace_id 유틸, 에러 코드, 구조화 로깅, config 로드 | Pydantic 스키마 단위 테스트 통과 |
| 4 | FastAPI 서버 기본 셋업 + API 라우터 골격 | /api/agent/run 엔드포인트 응답 |
| 5 | Orchestrator 고정 DAG 골격 (단일 StateGraph, command 분기) | 빈 노드로 generate/test 양쪽 경로 실행 성공 |

### Phase 1: Layer 1 — 컨텍스트 + 시나리오 (Week 2~3)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-000 | 코드베이스 스캔 Tool: AST 파싱, 엔드포인트/모델/종속성/callgraph 추출, .qapilot/codebase-index/ 캐싱, Git diff 증분 재분석 | 샘플 프로젝트 스캔 → JSON 출력 정합성 |
| 2 | FR-001 | 도메인 지식 Tool: 문서 파서, 임베딩 → Qdrant 적재, glossary.json, 유사도 검색 | qapilot spec import → 벡터DB 적재 확인 |
| 3 | FR-024 | 요구사항 추출 Agent: PRD에서 REQ-XXX 추출, RTM 행 생성 | 샘플 PRD → REQ-XXX 목록 |
| 4 | FR-002 | 시나리오 생성 Agent: 코드컨텍스트+도메인+Git diff → 시나리오+테스트데이터, TS/TC/TV 구조 | 골든셋 대비 Pass@1 60%+ |
| 5 | FR-003 | 자연어 요구사항 해석 Agent: 자연어 → Given/When/Then | 샘플 입력 → 구조화 출력 |
| 6 | FR-014 | HITL 모듈: 승인/수정/반려 큐 (시나리오 생성 + 자연어 해석 전용) | HITL 큐 적재 → 승인 플로우 |
| 7 | FR-004 | 시나리오-액션 매핑 Agent: 시나리오 → UI액션+API매핑+검증포인트, DOM 셀렉터 매칭 | 구조화 시나리오 → Step 시퀀스 출력 |
| 8 | FR-005 | Playwright 코드 생성 Agent: Step 시퀀스 → Playwright JS 코드, trace_id 주입 | 생성 코드 컴파일 성공 |

### Phase 2: Layer 2 — 테스트 실행 + 교차 검증 (Week 3~4)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-006 | UI 테스트 Tool: Playwright 실행, 스텝별 스크린샷, X-Trace-Id 주입 | 샘플 사이트 UI 테스트 성공 |
| 2 | FR-007 | API 추적 Tool: 네트워크 리스너, URL/method/headers/body/status/latency | API 호출 로그 수집 확인 |
| 3 | FR-020 | DB 테스트 Tool: 전후 스냅샷, 시드 주입+자동 롤백, SQL 추적 | 시드 → 테스트 → 롤백 확인 |
| 4 | FR-008 | Cross-check Agent: UI↔API↔DB 필드 매핑, 정합성 점수 산출 | 불일치 삽입 → 탐지 확인 |
| 5 | FR-015 | Trace ID 모듈: UUID v4 전 계층 전파 | 파이프라인 전 구간 연결 확인 |

### Phase 3: Layer 3 — 장애 분석 (Week 4~5)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-009 | 장애 분류 Agent: 규칙 1차 + LLM 보조, 5개 카테고리 | 실패 로그 → 정확한 분류 |
| 2 | FR-010 | 원인 추론 Agent: 로그+코드+Git blame → Top-N + 근거 3종 | Top-3 정확도 60%+ |
| 3 | FR-011 | 해결 방안 추천 Agent: 파일경로+담당자+수정 snippet | 원인 후보 → 해결 가이드 |

### Phase 4: Orchestrator 통합 + 리포트 (Week 5)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-013 | Orchestrator 통합: 단일 StateGraph에 전체 노드 연결, command 분기 + 선택적 실행 옵션 | generate/test 양쪽 경로 실행 성공 |
| 3 | FR-012 | 리포트 Tool: 단일 형식 템플릿, 실행요약+실패케이스+원인분석+해결방안 | 리포트 생성 확인 |
| 4 | FR-021 | 증적 캡처-보관: .qapilot/evidence/{trace_id}/ 저장, 대시보드 연동 | TC별 증적 묶음 확인 |

### Phase 5: CLI + 대시보드 + 부가 모듈 (Week 5~6)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-016 | CLI (Typer): init/generate/test/explain/spec import/rescan | 각 명령어 정상 동작 |
| 2 | FR-019 | 진척률 대시보드 (React): 6탭, RTM, 메트릭, CSV export | 대시보드 로딩 3초 이내 |
| 3 | FR-017 | 캐시 모듈: .qapilot/ 관리, LLM 캐시, TTL, 증분 | 동일 입력 캐시 히트 |
| 4 | FR-018 | 스케줄링 모듈: 자동 배치 실행 (완전 무인, HITL 미발생) | 스케줄 실행 → 리포트 자동 생성 |
| 5 | FR-022 | 요구사항 변경 히스토리: 버전 diff, 영향 시나리오 식별 | v2 업로드 → 영향 식별 |
| 6 | FR-023 | 시나리오 의존성 그래프: D3.js/Cytoscape.js DAG | 그래프 렌더링 + 인터랙션 |

### Phase 6: 통합 테스트 + 품질 (Week 6~7)

| # | 작업 | 검증 |
|---|---|---|
| 1 | E2E 통합 테스트 (generate → test 양쪽 파이프라인) | 시나리오 1개 실행 2분 이내 |
| 2 | 성능 튜닝 | 코드 스캔 diff 10초 이내, 대시보드 로딩 3초 이내 |
| 3 | 보안 점검 | OWASP Top 10, 프롬프트 인젝션 방지, 민감정보 마스킹 |
| 4 | 골든셋 인수 테스트 | 시나리오 정확도 75%+, 장애 분류 85%+, 원인 추론 Top-3 75%+ |
| 5 | 문서 산출물 정리 | CHANGELOG, API 문서 (Swagger) |

---

## 10. 인수 기준

| No | 항목 | 합격 기준 | 검증 방법 |
|---|---|---|---|
| 1 | 시나리오 생성 정확도 | 골든셋 300건 기준 75%+ | 독립 골든셋 측정 |
| 2 | 장애 분류 정확도 | 결함 라벨 데이터 기준 85%+ | 라벨 데이터 측정 |
| 3 | 원인 추론 Top-3 | 결함 이력 200건 기준 75%+ | 이력 데이터 측정 |
| 4 | 대시보드 API 응답 | p95 기준 500ms 이내 | JMeter 부하 테스트 |
| 5 | 에이전트 안전성 | 비인가 Tool 호출 0건, 타임아웃 자동 회복률 95%+ | 시나리오 테스트 |
| 6 | 무인 실행 | 5영업일 연속 성공 + 리포트 자동 생성 | 5일 연속 실행 로그 |
| 7 | CLI + 대시보드 통합성 | 동일 기능이 CLI/웹 양쪽 동작 | 크로스 검증 |
| 8 | 보안 | OWASP Top 10 치명/높음 0건 | 보안 점검 도구 스캔 |

---

## 11. Git 브랜치 전략

| 브랜치 | 용도 |
|---|---|
| main | 배포 가능 상태 |
| develop | 통합 개발 |
| feature/{기능명} | 기능 개발 |
| fix/{이슈번호} | 버그 수정 |
| release/{버전} | 릴리즈 준비 |

### 커밋 메시지 (Conventional Commits)

| 접두사 | 용도 | 예시 |
|---|---|---|
| feat | 새 기능 | feat(scenario): Git diff 기반 시나리오 생성 |
| fix | 버그 수정 | fix(ui-test): Playwright 타임아웃 수정 |
| refactor | 리팩토링 | refactor(shared): 공통 인터페이스 정리 |
| docs | 문서 | docs: 구현 플랜 업데이트 |
| chore | 빌드/설정 | chore: pyproject.toml 업데이트 |
