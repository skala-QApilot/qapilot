# QApilot 구현 플랜

> **Version**: 1.9  
> **최종 수정일**: 2026-05-19  
> **기반 문서**: 요구사항정의서 v0.4 / 개발표준정의서 v0.4  
> **변경 이력**:  
> - v1.0 (2026-05-07): 최초 작성. Orchestrator 고정 DAG 전환, HITL 범위 축소, 리포트 단일 형식 반영
> - v1.1 (2026-05-07): generate/test 파이프라인 분리 확정. generate=Layer1만, test=Layer2~3만
> - v1.2 (2026-05-11): HITL 모듈 제거. generate를 generate_scenarios/generate_code 2단계로 추가 분리. 사용자는 두 명령 사이에서 시나리오를 자유롭게 수정·삭제 가능
> - v1.3 (2026-05-13): §8.4 Interactive Shell Mode (REPL) 추가. `qapilot` 단독 실행 시 인터랙티브 셸 진입 (Claude Code 패턴 차용). 명령 히스토리는 휘발성 (Phase 2 에서 opt-in 영속화 검토).
> - v1.4 (2026-05-15): §4.5 ActionMapper ↔ CodeGenerator ↔ UITestTool 공통 정책 명문화. §7.5 ErrorCode 체계에 UI Test 전용 7종 (`TOOL_UI_*`) 부록. 27종 action vocabulary 매핑 표 추가.
> - v1.5 (2026-05-18): §6.1 디렉토리 구조에 `.qapilot/action-mappings/{TC-ID}.json` 추가 — ActionMapping 디스크 영속화. Layer 1B 의 `_save_codes` 노드가 저장, Layer 2 의 `_load_scenarios_for_test` 가 로드. spec 자기 일관성 회복 (scenarios/ + generated-code/ + results/ 와 동일한 디스크 자산 격상). 결정성·HITL 검토 가능성·비용 절감 동시 확보.
> - v1.6 (2026-05-18): Orchestrator Layer 3 wire-up 완료 — `_defect_classify` (DefectClassifier stub graceful) / `_root_cause` (RootCauseAgent FR-010) / `_fix_recommend` (FixRecommenderAgent FR-011) 호출. `_report` 5 섹션 markdown 확장. 4-Layer 17 노드 완전 wire 상태 도달.
> - v1.7 (2026-05-18): **DefectClassifier 단계 제거**. CrossCheckAgent 가 이미 생산하는 `error_code`/`summary`/`mismatches` 가 분류 신호를 제공하므로 별도 분류 단계 불필요. `_cross_check` 노드에서 두 필드 보존 패치 + 그래프에서 `_defect_classify` 노드/엣지 제거 (cross_check → root_cause 직결). `defect_results` state 필드 / DefectClassifierAgent / 관련 프롬프트·스키마 모두 제거. `_report` 4 섹션 (실패 / Cross-check / 원인 / 해결) 로 재조정. FR-009 spec 갱신 별도 필요.
> - v1.8 (2026-05-19): **§4.5.4 UITestTool 측 의미적 chain 도입** (이슈 #111). `1-step fallback` 한정 → `selector_type 별 retry chain` 확장 — ActionMapper LLM 추론과 실제 SUT DOM mismatch 보완 (e2e 첫 완주 `721a4e4f` 의 UI 100% fail 원인 분석 결과). 1차 timeout 10s / 2차+ 5s 분배로 총 시간 폭증 회피. chain 적중 시 `ui_fallback_chain_success` 로그. 근본 해결 (CodebaseScannerTool frontend 스캔) 은 별도 트랙 — 상세 `docs/frontend-dom-scan-gap.md`.
> - v1.8.1 (2026-05-19): **§4.5.4 옵션 B 추가** (이슈 #115). 옵션 A chain 모두 실패 시 런타임 DOM 스캔 + fuzzy match — Python `difflib.SequenceMatcher.ratio()` + 포함관계 가산점 0.2 + 임계값 0.6. 반환 Locator 우선순위 testid > placeholder > text > label > id > name. **이슈 #119 (2026-05-19)**: §4.5.4 옵션 B 본문의 `Levenshtein Distance` 표기를 실제 알고리즘 (`difflib.SequenceMatcher`) 로 정정 + DOM scan 수집 element 와 후보 속성 정확히 명시. 코드 일관성 + docstring + 테스트 강화 동반.
> - v1.8.2 (2026-05-19): **§4.5.4 옵션 C 추가** (이슈 #121). ActionMapping 첫 step 이 DOM action 일 때 `api_endpoint` 힌트로 frontend route 추론 + auto-navigate. e2e trace `e1796b43` 의 73/73 UI fail 원인 분석 결과 (옵션 A/B 가 잘못된 페이지에서 시도되어 적중 0) 보완. SUT vue-router redirect (예: `/` → `/plans`) 인식 안 하던 한계를 휴리스틱 추론으로 우회. 옵션 A/B 효과가 비로소 실측 가능. 상세: `docs/e2e-navigate-gap-analysis.md`.
> - v1.8.3 (2026-05-19): **target_url UX + 옵션 C 견고함 강화** (이슈 #123). e2e trace `4e1d8d49` 의 옵션 C auto-navigate 36/36 fail (invalid URL `full_url=/login`) 원인 — (a) mini-bss-lite/qapilot.config.yaml 의 `target_url` 누락 (b) UITestTool 의 invalid URL 방어 부재 (c) 휴리스틱이 backend `/api/...` path 그대로 사용. 세 가지 fix: (1) `qapilot init` 마법사가 frontend dev server URL 자동 추론 (vite.config / next.config / package.json / docker-compose) + Prompt fallback (2) `_try_auto_navigate` 가 `target_url` 부재/scheme 부재 시 사전 skip (3) `_infer_target_route` v2 — `/api/` prefix 제거 + 1차 segment 만 사용.
> - v1.9 (2026-05-19): **frontend DOM 정적 인덱싱 + ActionMapper LLM 컨텍스트 주입** (이슈 #127). e2e trace `e1796b43`/`4e1d8d49` 의 UI 100% fail 근본 원인 (LLM 환각 — selector="HTTP 401" 같은 API 응답을 UI 텍스트로 추측) 해결. §6.1 codebase-index/ 에 `frontend.json` 추가. `.vue/.tsx/.jsx` 의 의미적 element (input/button/textarea/select/label/a + `[role="button"]`) + 속성 (text/placeholder/aria-label/data-testid/data-test-id/id/name) regex 휴리스틱 추출. `qapilot init` 마지막 + `qapilot rescan` 시 디스크 저장. ActionMapper `_call_batch` 가 디스크 로드 + prompt 의 `{{frontend_dom}}` 변수로 LLM 컨텍스트 주입. LLM 이 추측 대신 실제 DOM 정보 참조 → 옵션 A/B/C 효과 비로소 발현 가능.

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 프로젝트명 | QApilot — AI 기반 테스트 시나리오/데이터 생성 및 통합 테스트 자동화 및 오류 분석 시스템 |
| 유형 | Agentic AI (신규 개발) |
| 기간 | 2026-04-17 ~ 2026-06-23 |
| 비전 | NPT(No People Testing) — 완전 무인 테스트 운영. 사람 개입은 시나리오 검토 단계(generate_scenarios ↔ generate_code 사이)로만 제한 |
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

### 2.1 전체 구조 (3개 독립 파이프라인 + 고정 DAG)

generate_scenarios, generate_code, test는 **독립된 파이프라인**으로 분리된다.
사용자는 generate_scenarios와 generate_code 사이에서 대시보드를 통해
시나리오를 자유롭게 수정·삭제할 수 있다 (HITL 모듈 불필요).

```text
═══════════════════════════════════════════════════════════════════════
  [qapilot generate scenarios] — Layer 1A 파이프라인
═══════════════════════════════════════════════════════════════════════

  코드스캔Tool → 도메인지식Tool → 요구사항추출Agent
  → 시나리오생성Agent → .qapilot/scenarios/ 에 저장

  * 자연어 입력 시 별도 진입점:
    자연어해석Agent → .qapilot/scenarios/ 에 저장

═══════════════════════════════════════════════════════════════════════
  [사용자가 대시보드에서 시나리오 검토/수정/삭제]
       └ GET / PUT / DELETE /api/scenarios/{id}
═══════════════════════════════════════════════════════════════════════
  [qapilot generate code] — Layer 1B 파이프라인
═══════════════════════════════════════════════════════════════════════

  저장된 시나리오 로드 → 액션매핑Agent → 코드생성Agent
  → .qapilot/generated-code/ 에 저장

═══════════════════════════════════════════════════════════════════════
  [qapilot test] — Layer 2~3 파이프라인
═══════════════════════════════════════════════════════════════════════

  저장된 시나리오/코드 로드
  → UI테스트Tool ─┐
    API추적Tool  ─┼─(병렬)─→ Cross-check Agent
    DB테스트Tool ─┘
  → (불일치 시) 원인추론Agent → 해결방안추천Agent
  → 리포트Tool (단일 형식)

═══════════════════════════════════════════════════════════════════════
```

- **generate_scenarios**: 시나리오 + 테스트 데이터만 생성하여 저장. 코드 생성 안 함
- **(사이 시점)**: 사용자가 대시보드에서 시나리오 자유 수정/삭제. 명령 실행 없음
- **generate_code**: 저장된 시나리오를 기반으로 액션 매핑 + Playwright 코드 생성/저장
- **test**: 저장된 시나리오/코드를 로드하여 실행. 야간 무인 실행 가능

### 2.2 Orchestrator 설계 결정

| 항목 | 결정 |
|---|---|
| 방식 | **사전 정의된 고정 DAG** (LLM 미사용) |
| 구현체 | LangGraph StateGraph **1개** |
| 분기 | `command`로 3개 진입점 분기 (generate_scenarios / generate_code / test) + `has_mismatch`로 Layer 3 실행 여부 |
| 근거 | 파이프라인 흐름이 완전 확정적. LLM 판단이 필요한 지점 없음. 안정성·속도·비용 모두 우위 |

```python
graph = StateGraph(PipelineState)

# ── Layer 1A 노드 (generate_scenarios) ──
graph.add_edge("codebase_scan", "domain_knowledge")
graph.add_edge("domain_knowledge", "requirement_extract")
graph.add_edge("requirement_extract", "scenario_generate")
graph.add_edge("scenario_generate", "save_scenarios")
graph.add_edge("save_scenarios", END)

# ── Layer 1B 노드 (generate_code) ──
graph.add_edge("load_scenarios_for_codegen", "action_mapping")
graph.add_edge("action_mapping", "code_generate")
graph.add_edge("code_generate", "save_codes")
graph.add_edge("save_codes", END)

# ── Layer 2~3 노드 (test) ──
graph.add_edge("load_scenarios_for_test", "test_execution")
graph.add_edge("test_execution", "cross_check")
graph.add_conditional_edges(
    "cross_check",
    lambda state: "root_cause" if state["has_mismatch"] else "report",
)
graph.add_edge("root_cause", "fix_recommend")
graph.add_edge("fix_recommend", "report")
graph.add_edge("report", END)

# ── 진입점 분기 (3개 명령) ──
graph.add_conditional_edges(
    START,
    lambda state: {
        "generate_scenarios": "codebase_scan",
        "generate_code": "load_scenarios_for_codegen",
        "test": "load_scenarios_for_test",
    }[state["run_options"]["command"]],
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

### 3.2 Agent (LLM 사용) — 8개

| FR | 이름 | Layer | 핵심 역할 |
|---|---|---|---|
| FR-024 | 요구사항 추출 Agent | L1A | PRD에서 REQ-XXX 구조화 추출, RTM 행 생성 |
| FR-002 | 시나리오 생성 Agent | L1A | 코드+도메인+Git diff 기반 정상/엣지 시나리오+테스트데이터 생성 |
| FR-003 | 자연어 요구사항 해석 Agent | L1A | 자연어 → Given/When/Then 구조화 |
| FR-004 | 시나리오-액션 매핑 Agent | L1B | 시나리오 → UI액션+API매핑+검증포인트 분해 |
| FR-005 | Playwright 코드 생성 Agent | L1B | 액션 시퀀스 → Playwright JS 코드 |
| FR-008 | Cross-check Agent | L2 | UI↔API↔DB 데이터 정합성 검증, 정합성 점수 산출, `error_code`/`summary` 도출 |
| FR-010 | 원인 추론 Agent | L3 | Cross-check `error_code`/`summary`/`mismatches` + 코드 인덱스 → Top-N 원인 후보 + 근거 3종 |
| FR-011 | 해결 방안 추천 Agent | L3 | 파일경로+담당자+수정 snippet 제시 |

> **FR-009 장애 분류 Agent 는 구현하지 않는다.** Cross-check Agent (FR-008) 가 이미 `error_code`/`summary`/`mismatches` 를 생산하여 분류 신호를 제공하므로 별도 분류 단계가 불필요. 그래프는 `cross_check → root_cause` 로 직결.

### 3.3 Tool (결정적, LLM 미사용) — 6개

| FR | 이름 | 역할 |
|---|---|---|
| FR-000 | 코드베이스 스캔 Tool | AST 파싱, 엔드포인트/모델/종속성/callgraph 추출, 증분 재분석 |
| FR-001 | 도메인 지식 Tool (RAG) | 문서 임베딩, Qdrant 벡터 검색, 용어사전(glossary.json) |
| FR-006 | UI 테스트 Tool | Playwright 코드 실행, 스크린샷 캡처, headed/headless |
| FR-007 | API 추적 Tool | 네트워크 리스너, 요청/응답 수집, trace_id 매칭 |
| FR-020 | DB 테스트 Tool | 전후 스냅샷, SQL 추적, 시드 주입/자동 롤백 |
| FR-012 | 리포트 Tool | 테스트 결과 리포트 생성 (단일 형식) |

### 3.4 Module (인프라/UI/설정) — 8개

| FR | 이름 | 역할 |
|---|---|---|
| FR-015 | Trace ID 모듈 | UUID v4 발급, UI→API→DB 전 계층 전파 |
| FR-016 | CLI + 웹 대시보드 | Typer CLI + React 대시보드 (포트 7860) |
| FR-017 | 캐시 모듈 | .qapilot/ 디렉토리, LLM 캐시, Git hash 기반 증분 |
| FR-018 | 스케줄링 모듈 | 자동 회귀 테스트 배치 실행 (완전 무인) |
| FR-019 | 진척률 대시보드 | RTM, 메트릭 시각화, CSV export |
| FR-021 | 증적 캡처-보관 모듈 | 스크린샷/API 본문/로그 trace_id 기준 영구 보관 |
| FR-022 | 요구사항 변경 히스토리 | 버전별 diff, 영향 시나리오 자동 식별 |
| FR-023 | 시나리오 의존성 그래프 | D3.js/Cytoscape.js DAG 시각화 |

> **FR-014 HITL 모듈은 구현하지 않는다.** generate_scenarios / generate_code 명령 분리로
> 사용자가 그 사이에서 시나리오를 자유롭게 검토/수정/삭제할 수 있어 별도 모듈이 불필요하다.
> 시나리오 CRUD는 `api/scenario_router.py`(GET/POST/PUT/DELETE)에서 제공한다.

---

## 4. 핵심 정책 (요구사항 문서 대비 변경 사항)

### 4.1 HITL 정책

> **요구사항 문서의 FR-014 HITL 모듈은 별도 모듈로 구현하지 않습니다.**
> 파이프라인을 generate_scenarios / generate_code / test 3단계로 분리하여
> 사용자 검토 시점이 자연스럽게 보장됩니다.

| 항목 | 정책 |
|---|---|
| HITL 모듈 | **구현하지 않음** (FR-014 미적용) |
| 사용자 검토 시점 | `generate_scenarios` 완료 후 → 사용자가 대시보드에서 시나리오 자유 수정/삭제 → `generate_code` 실행 |
| 검토 방식 | 시나리오 CRUD API (`GET/POST/PUT/DELETE /api/scenarios/{id}`) |
| 나머지 Agent | confidence는 로그/리포트 참조용으로만 기록. 자체 Fallback 처리 |
| 야간 실행(FR-018) | `test` 명령만 스케줄링. 시나리오/코드는 이미 저장되어 있음 → **완전 무인** |

```text
[qapilot generate scenarios]
  → 시나리오 + 테스트 데이터 생성 → .qapilot/scenarios/ 저장 → 끝

[사용자가 대시보드에서 시나리오 검토/수정/삭제]
  → GET/PUT/DELETE /api/scenarios/{id}
  → 명령 실행 없음. 자유 시점

[qapilot generate code]
  → 저장된 시나리오 로드 → 액션 매핑 → 코드 생성 → 저장 → 끝

[그 외 Agent: Cross-check, 원인추론, 해결추천 등]
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
| 분기 | `command` 3개 진입점 분기 + `has_mismatch` boolean 조건 (Layer 3 실행 여부) |
| 선택적 실행 | CLI 옵션 파싱 기반 (`--case / --failed / --affected / --tag`) |
| 재시도 | 규칙 기반 (최대 3회, exponential backoff) |

### 4.4 파이프라인 분리 정책

> **generate_scenarios / generate_code / test 3개 독립 파이프라인.**
> Layer 1→2→3을 한 번에 실행하는 경로는 없습니다.

| 항목 | generate_scenarios | generate_code | test |
|---|---|---|---|
| 실행 명령 | `qapilot generate scenarios` | `qapilot generate code` | `qapilot test [옵션]` |
| 실행 범위 | Layer 1A | Layer 1B | Layer 2~3 |
| 입력 | 코드베이스 + 도메인 문서 (+ 자연어) | .qapilot/scenarios/ | .qapilot/scenarios/ + generated-code/ |
| 산출물 | .qapilot/scenarios/ | .qapilot/generated-code/ | .qapilot/results/ + 리포트 |
| 사용자 검토 | 후속 검토 가능 (대시보드) | 후속 검토 가능 (대시보드) | - |
| 야간 무인 | 불가 (코드 미생성 상태) | 불가 | **가능** |

```text
사용자 워크플로우:

1) qapilot generate scenarios          → 시나리오만 생성
2) [대시보드에서 시나리오 자유 수정/삭제]
3) qapilot generate code               → 액션 매핑 + Playwright 코드 생성
4) qapilot test                        → 전체 시나리오 테스트
5) qapilot test --failed               → 이전 실패 건만 재실행
6) qapilot test --case TS-001          → 특정 시나리오만 실행
7) qapilot test --affected             → Git 변경분 영향 시나리오만 실행
8) qapilot test --tag payment          → 태그 기반 필터 실행
```

### 4.5 ActionMapper ↔ CodeGenerator ↔ UITestTool 정책 (v1.4 신규)

> 본 정책은 PR #73 (ActionMapper 정규화) + PR #79 (CodeGenerator 프롬프트 + UITestTool vocabulary 확장) 으로 정착되었다. **세 컴포넌트의 책임을 계층화** 하여 fail 을 ActionMapping 생성 시점 → 코드 생성 시점 → 실행 시점 으로 점진 분산.

#### 4.5.1 공통 원칙

| 컴포넌트 | 책임 |
|---|---|
| **ActionMapper** | 가능한 한 실패하지 않고 표준화 (정규화 계층). LLM이 비표준 action을 내도 alias dict로 표준 action 변환 |
| **CodeGenerator** | 가능한 한 코드 생성을 멈추지 않음 (관대한 코드 생성 계층). unsupported action은 `test.skip` 또는 주석으로 폴백 |
| **UITestTool** | 실제 유효성 판정자. action 27종 모두 실행 시도 + 1-step fallback + 카테고리화된 에러 기록 |
| **QA 사용자** | selector/action 을 직접 수정하지 않음. 시나리오 검토 (대시보드)와 결과 분석만 |

#### 4.5.2 표준 action vocabulary (27종)

ActionMapper / CodeGenerator / UITestTool 모두 동일 vocabulary 공유.

| 그룹 | action | selector 필요? | value 의미 | expected 의미 |
|---|---|---|---|---|
| **page-level (9)** | `navigate` | X | URL (relative/absolute) | - |
| | `reload` | X | - | - |
| | `go_back` | X | - | - |
| | `go_forward` | X | - | - |
| | `wait` | X | timeout(ms) 숫자 또는 미지정 → networkidle | - |
| | `wait_for_url` | X | URL pattern | - |
| | `wait_for_load_state` | X | "load" \| "domcontentloaded" \| "networkidle" | - |
| | `wait_for_response` | X | URL pattern | - |
| | `assert_url` | X | - | URL pattern |
| **DOM (10)** | `fill` | O | 입력값 | - |
| | `clear` | O | - | - |
| | `click` | O | - | - |
| | `dblclick` | O | - | - |
| | `hover` | O | - | - |
| | `select` | O | option 값 | - |
| | `check` | O | - | - |
| | `uncheck` | O | - | - |
| | `press` | O | 키 이름 (e.g. "Enter") | - |
| | `upload` | O | 파일 경로 | - |
| **assert (8)** | `assert` (=assert_visible 별칭) | O | - | - |
| | `assert_visible` / `assert_hidden` | O | - | - |
| | `assert_enabled` / `assert_disabled` | O | - | - |
| | `assert_text` | O | - | 기대 텍스트 |
| | `assert_value` | O | - | 기대 값 |
| | `assert_count` | O | - | 기대 개수 (숫자, fallback=0) |

#### 4.5.3 selector_type 9종 매핑

| selector_type | Python Playwright (UITestTool) | JS Playwright (CodeGenerator 출력) |
|---|---|---|
| `role` | `page.get_by_role(s)` | `page.getByRole(s)` |
| `label` | `page.get_by_label(s)` | `page.getByLabel(s)` |
| `placeholder` | `page.get_by_placeholder(s)` | `page.getByPlaceholder(s)` |
| `text` | `page.get_by_text(s)` | `page.getByText(s)` |
| `testid` | `page.get_by_test_id(s)` | `page.getByTestId(s)` |
| `alttext` | `page.get_by_alt_text(s)` | `page.getByAltText(s)` |
| `title` | `page.get_by_title(s)` | `page.getByTitle(s)` |
| `css` | `page.locator(s)` | `page.locator(s)` |
| `xpath` | `page.locator(f"xpath={s}")` | `page.locator(s)` |

#### 4.5.4 Fallback 정책

**ActionMapper 측 (1-step)**:
- 비표준 action → alias dict 로 표준 변환 (예: `input` → `fill`, `verify` → `assert`)
- selector 없는데 DOM action → `selector = expected or value or action`, `selector_type = text`, confidence 감점
- 검증 실패는 JSON 구조 위반·필수 필드 누락만

**CodeGenerator 측 (1-step)** (`prompts/code_generator/system.md` §일반 4):
- unsupported action 도착 → `test.skip(true, 'unsupported action: <action>')` 또는 주석 처리
- selector null + DOM action → `page.getByText(expected || value || action)`

**UITestTool 측 (selector_type 별 의미적 chain 및 DOM Scan, 이슈 #111 & #115)** (`qapilot/tools/ui_test_tool.py`):
- `press` value 누락 → "Enter"
- `wait_for_load_state` value 모호 → "networkidle"
- `assert_count` expected 비숫자/None → 0
- selector None + DOM action → `get_by_text` → `get_by_placeholder` → `get_by_label` 3-step chain + `TOOL_UI_FALLBACK_USED` 경고
- DOM action 의 selector_type 별 retry chain (옵션 A) — ActionMapper LLM 추론과 실제 SUT DOM mismatch 보완:

| selector_type | chain order (1차 → ...) | timeout |
|---|---|---|
| `text` | `get_by_text` → `get_by_label` → `get_by_placeholder` → `get_by_test_id` | 1차 10s / 2차+ 5s |
| `label` | `get_by_label` → `get_by_text` → `get_by_placeholder` | 1차 10s / 2차+ 5s |
| `placeholder` | `get_by_placeholder` → `get_by_label` → `get_by_text` | 1차 10s / 2차+ 5s |
| `testid` | `get_by_test_id` → `[data-testid=]` → `[data-test-id=]` | 1차 10s / 2차+ 5s |
| `role` | `get_by_role` → `get_by_text` | 1차 10s / 2차+ 5s |
| `alttext` | `get_by_alt_text` → `get_by_text` | 1차 10s / 2차+ 5s |
| `title` | `get_by_title` → `get_by_text` | 1차 10s / 2차+ 5s |
| `css` / `xpath` | 단일 시도 (정확한 selector 가정) | 1차 10s |

- **런타임 DOM Scan + Fuzzy Match (옵션 B)**: 위 1차/2차 chain(옵션 A)이 모두 실패했을 경우, 런타임 시점의 실제 브라우저 DOM을 스캔(`page.evaluate`)하여 모든 대화형 요소(`input, textarea, select, button, a, label, [role="button"]` — visible 만)를 수집하고 각 요소의 `text / placeholder / aria-label / data-testid \| data-test-id / id / name` 을 후보로 추출. Python `difflib.SequenceMatcher.ratio()` (gestalt pattern matching 변형) 기반 fuzzy match 에 포함관계 가산점 (`target in cand or cand in target` 시 +0.2) 을 적용, 임계값 0.6 이상에서 가장 유사한 요소를 선택. 반환 Locator 우선순위 `testid > placeholder > text > label > id > name` 로 가장 안정적인 entry point 활용. 마지막으로 1회 더 재시도 (fallback timeout 5s).
- chain 적중 시 `ui_fallback_chain_success` (옵션 A) 또는 `ui_fallback_dom_scan_success` (옵션 B) info 로그 기록. 모두 실패 시 마지막 에러 raise → caller 가 `TOOL_UI_LOCATOR_NOT_FOUND` 분류.
- **Auto-navigate 보강 (옵션 C, 이슈 #121 + 강화 #123)**: ActionMapping 의 첫 step 이 navigate 가 아닌 DOM action (`fill`/`click`/`assert` 등) 일 때 SUT 의 잘못된 페이지에서 시작될 가능성 보완. `_run_steps` 진입 시 ActionMapping steps 의 `api_endpoint` 힌트를 검사해 frontend route 추론. **휴리스틱 v2 (이슈 #123)**:
  - HTTP method prefix 제거 (`POST /login` → `/login`)
  - 경로 매개변수 제거 (`GET /plans/{id}` → `/plans`)
  - **`/api/` prefix 제거** (`POST /api/login` → `/login`, `DELETE /api/contracts/1/cancel` → `/contracts`)
  - **1차 segment 만 사용** (frontend route 는 보통 단순 1-segment)
  - 예시: `POST /api/family-group/join` → `/family-group`, `PATCH /api/orders/{id}/status` → `/orders`

  추론 성공 시 `page.goto(target_url + route)` 자동 호출 + `ui_auto_navigate` info. **target_url 안전망 (이슈 #123 P2)**: `target_url` 이 None / empty / scheme 없는 path-only 시 invalid URL fail 방지 — `ui_auto_navigate_skipped` warning 후 기존 동작. api_endpoint 부재 시 `ui_auto_navigate_skipped` debug. goto 실패 시 `ui_auto_navigate_failed` warning + 후속 step 진행. 옵션 A/B 가 올바른 페이지에서 시작되도록 보장. 상세 배경: `docs/e2e-navigate-gap-analysis.md`.

  **`qapilot init` 마법사 (이슈 #123 P1)**: `target_url` 누락 UX 결함 해결 — frontend dev server URL 자동 추론 (`<frontend_dir>/vite.config` → `next.config` → `package.json dev script port` → `docker-compose ports`) + 추론 실패 시 Prompt fallback. 추론 성공 시 사용자가 enter 만 눌러 default 채택 가능.

> **배경**: 본 chain 확장은 ActionMapper / ScenarioGen LLM 의 SUT DOM 추론 한계 보완. 근본 해결 (CodebaseScannerTool 의 frontend 스캔 + ActionMapper 인덱스 주입) 은 별도 트랙 (C 영역). 상세: `docs/frontend-dom-scan-gap.md`.

#### 4.5.5 confidence 감점 요인 (ActionMapper)

- 비표준 action 정규화 발생
- 비표준 selector_type 정규화 발생
- fallback selector 사용
- css/xpath 사용 비율 높음
- api_endpoint 매핑률 낮음

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
├── agents/                          # Agent 모듈 (8개)
│   ├── base_agent.py                # BaseAgent ABC
│   ├── requirement_extractor_agent.py
│   ├── scenario_generator_agent.py
│   ├── natural_language_agent.py
│   ├── action_mapper_agent.py
│   ├── code_generator_agent.py
│   ├── cross_check_agent.py
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
│   ├── trace_module.py
│   ├── cache_module.py
│   ├── schedule_module.py
│   └── evidence_module.py
├── cli/                             # Typer CLI
│   └── main.py
├── api/                             # FastAPI 라우터
│   ├── agent_router.py
│   ├── scenario_router.py           # GET/POST/PUT/DELETE (사용자 시나리오 검토)
│   ├── defect_router.py
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
│   ├── frontend.json                # 이슈 #127: frontend DOM 정적 인덱스 (.vue/.tsx/.jsx)
│   └── manifest.json                # Git commit hash 기준
├── domain/                          # FR-001 도메인 지식 인덱스
├── scenarios/                       # generate 산출물: 시나리오
│   ├── {시나리오ID}.json             # TS/TC/TV 3단계 구조
│   ├── raw/                         # 자연어 원본
│   └── regression/                  # 회귀 테스트 자산
├── action-mappings/                 # generate_code 산출물: ActionMapping (v1.5 신규)
│   └── {TC-ID}.json                 # TC별 ActionStep 시퀀스 (Layer 2 가 로드)
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
| /api/agent/run | POST | 파이프라인 실행 요청 (generate_scenarios / generate_code / test) |
| /api/scenarios | GET / POST | 시나리오 목록 / 생성 |
| /api/scenarios/{id} | GET / PUT / DELETE | 시나리오 상세 / 수정 / 삭제 (사용자 검토 시 사용) |
| /api/defects | GET | 결함 목록 |
| /api/defects/{id} | GET | 결함 상세 |
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

#### 7.5.1 UI Test Tool 전용 에러 코드 (v1.4 신규)

`UIStepResult.error` 필드는 raw string 이 아닌 카테고리 prefix 를 포함한다 (`"<CODE>: <detail>"` 형식). CrossCheckAgent (PR #62) 의 UI 에러 추출 기능과 통합되어 결함 분류 시 활용.

| 코드 | 발생 조건 | 예시 메시지 | 활용 (Cross-check) |
|---|---|---|---|
| `TOOL_UI_LOCATOR_NOT_FOUND` | DOM action 의 selector 가 화면에 없음 (Locator 30s 타임아웃) | `TOOL_UI_LOCATOR_NOT_FOUND: locator("#submit") - 30000ms timeout` | UI 요소 누락 → UI 카테고리 결함 |
| `TOOL_UI_TIMEOUT` | 페이지 로드 / wait 타임아웃 | `TOOL_UI_TIMEOUT: page.goto exceeded 30s` | 네트워크/성능 → INFRA 카테고리 |
| `TOOL_UI_ASSERTION_FAIL` | `expect(...)` 단언 실패 | `TOOL_UI_ASSERTION_FAIL: expected "가입 완료" got "오류 발생"` | 기대 ≠ 실제 → RULE/UI 카테고리 |
| `TOOL_UI_NAVIGATION_FAIL` | navigate URL 접속 실패 (ECONNREFUSED 등) | `TOOL_UI_NAVIGATION_FAIL: net::ERR_CONNECTION_REFUSED` | SUT 미기동 → INFRA |
| `TOOL_UI_UNSUPPORTED_ACTION` | (방어) ActionMapper 정규화 후에도 미지원 action | `TOOL_UI_UNSUPPORTED_ACTION: 지원하지 않는 action: 'drag'` | ActionMapper 정규화 누락 — 본 카테고리 발생 시 ActionMapper alias dict 갱신 신호 |
| `TOOL_UI_FALLBACK_USED` | 1-step fallback 적용 (selector None + DOM action 등) | `TOOL_UI_FALLBACK_USED: get_by_text('가입 완료')` | 로그용 — fail 분류 아님 |
| `TOOL_UI_UNKNOWN` | 분류 외 일반 예외 | `TOOL_UI_UNKNOWN: KeyError: 'foo'` | 추적 후 새 카테고리 도입 검토 |

본 코드들은 `qapilot/shared/errors.py` 의 `ErrorCode` 클래스에 상수로 정의되며, `qapilot/tools/ui_test_tool.py` 가 `_run_steps` 의 except 분기에서 자동 prefix 한다.

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

### 8.2 generate 명령어 (Layer 1A / 1B 분리)

| 명령어 | 동작 |
|---|---|
| `qapilot generate scenarios` | 시나리오 + 테스트 데이터 생성 → .qapilot/scenarios/ 저장 |
| `qapilot generate scenarios --affected` | Git 변경분 영향 범위만 시나리오 생성 |
| `qapilot generate code` | 저장된 시나리오 → 액션 매핑 + Playwright 코드 생성 → .qapilot/generated-code/ 저장 |
| `qapilot generate code --case {id}` | 특정 시나리오의 코드만 생성 |

두 명령 사이에서 사용자는 대시보드(`GET/PUT/DELETE /api/scenarios/{id}`)를 통해 시나리오를 자유롭게 검토·수정·삭제할 수 있다.

### 8.3 test 명령어 (Layer 2~3)

| 명령어 | 동작 |
|---|---|
| `qapilot test` | 저장된 전체 시나리오 테스트 실행 |
| `qapilot test --case {id}` | 특정 시나리오만 실행 |
| `qapilot test --failed` | 이전 실패 건만 재실행 |
| `qapilot test --affected` | Git 변경분 영향 시나리오만 실행 |
| `qapilot test --tag {태그}` | 태그 기반 필터 실행 |

### 8.4 Interactive Shell Mode (REPL)

| 명령어 | 동작 |
|---|---|
| `qapilot` | 인자 없이 실행 시 인터랙티브 셸 (REPL) 진입. 모든 CLI 명령을 연속 입력 가능 |

#### 셸 안 사용법
- 모든 일회성 명령 그대로 사용 가능 (`init`, `generate scenarios`, `generate code`, `test`, ...)
- 슬래시 명령 (Phase 1): `/help`, `/exit`, `/clear`
- `exit`, `quit`, Ctrl-D 로도 종료. Ctrl-C 를 1.5초 안에 두 번 누르면 종료, 한 번만 누르면 현재 명령만 중단
- 명령 히스토리는 **현재 세션 안에서만 휘발** (↑/↓ 으로 동일 세션 내 이전 명령 호출). 사내 도메인·정책·DSN 등 민감 입력의 디스크 평문 저장 위험 회피. Phase 2 에서 `qapilot.config.yaml` 의 `repl.history_persistent: true` opt-in 토글 도입 고려 (마스킹·권한 0600·크기 cap 동반)
- Tab 자동완성 지원

#### 동작 시나리오 예시
```text
$ qapilot
[쿼카 우주비행사 마스코트 배너]
Interactive Shell (REPL) 모드
명령을 연속 입력하세요. /help 로 도움말, /exit (또는 Ctrl-D) 로 종료.

qapilot> spec import docs/policy_v3.md
임베딩 완료
  file          : docs/policy_v3.md
  chunks_total  : 24
  chunks_stored : 24
  chunks_failed : 0
qapilot> generate scenarios
시나리오 생성을 시작합니다 (Layer 1)...
...
qapilot> /exit
bye.
```

#### Phase 2/3 확장 후보 (별도 이슈)
- 슬래시 명령 추가: `/cost`, `/model`, `/memory`, `/sessions`, `/compact`, `/resume`
- 입력 큐 — 작업 실행 중 다음 명령 미리 입력 가능 (FIFO 자동 실행)
- Bracketed paste / heredoc / 백슬래시 라인 연결 — 멀티라인 입력
- 모드별 prompt 변경 (예: `qapilot[generate]>`)
- 자연어 입력 → FR-003 NaturalLanguageAgent 해석 (가장 Claude Code 다움)

---

## 9. 구현 Phase

### Phase 0: 프로젝트 기반 구축 (Week 1)

| # | 작업 | 검증 |
|---|---|---|
| 1 | 디렉토리 구조 + pyproject.toml + .env.example | pip install -e . 성공 |
| 2 | Docker Compose (PostgreSQL + Qdrant) | docker compose up 기동 확인 |
| 3 | shared/ 공통 모듈: 스키마(AgentInput/Output, ToolInput/Output), BaseAgent/BaseTool ABC, trace_id 유틸, 에러 코드, 구조화 로깅, config 로드 | Pydantic 스키마 단위 테스트 통과 |
| 4 | FastAPI 서버 기본 셋업 + API 라우터 골격 | /api/agent/run 엔드포인트 응답 |
| 5 | Orchestrator 고정 DAG 골격 (단일 StateGraph, 3개 진입점 분기) | 빈 노드로 generate_scenarios/generate_code/test 3개 경로 실행 성공 |

### Phase 1A: Layer 1A — 시나리오 생성 파이프라인 (Week 2)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-000 | 코드베이스 스캔 Tool: AST 파싱, 엔드포인트/모델/종속성/callgraph 추출, .qapilot/codebase-index/ 캐싱, Git diff 증분 재분석 | 샘플 프로젝트 스캔 → JSON 출력 정합성 |
| 2 | FR-001 | 도메인 지식 Tool: 문서 파서, 임베딩 → Qdrant 적재, glossary.json, 유사도 검색 | qapilot spec import → 벡터DB 적재 확인 |
| 3 | FR-024 | 요구사항 추출 Agent: PRD에서 REQ-XXX 추출, RTM 행 생성 | 샘플 PRD → REQ-XXX 목록 |
| 4 | FR-002 | 시나리오 생성 Agent: 코드컨텍스트+도메인+Git diff → 시나리오+테스트데이터, TS/TC/TV 구조 | 골든셋 대비 Pass@1 60%+ |
| 5 | FR-003 | 자연어 요구사항 해석 Agent: 자연어 → Given/When/Then | 샘플 입력 → 구조화 출력 |
| 6 | - | save_scenarios 노드: .qapilot/scenarios/{ts_id}.json 저장 | 파일 저장 확인 |
| 7 | - | 시나리오 CRUD API: GET/POST/PUT/DELETE /api/scenarios | 사용자 수정/삭제 플로우 확인 |

### Phase 1B: Layer 1B — 코드 생성 파이프라인 (Week 3)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | - | load_scenarios_for_codegen 노드: .qapilot/scenarios/ 로드 | 저장된 시나리오 로드 확인 |
| 2 | FR-004 | 시나리오-액션 매핑 Agent: 시나리오 → UI액션+API매핑+검증포인트, DOM 셀렉터 매칭 | 구조화 시나리오 → Step 시퀀스 출력 |
| 3 | FR-005 | Playwright 코드 생성 Agent: Step 시퀀스 → Playwright JS 코드, trace_id 주입 | 생성 코드 컴파일 성공 |
| 4 | - | save_codes 노드: .qapilot/generated-code/ 저장 | 파일 저장 확인 |

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
| 1 | FR-010 | 원인 추론 Agent: Cross-check `error_code`/`summary`/`mismatches` + 코드 인덱스 → Top-N + 근거 3종 | Top-3 정확도 60%+ |
| 2 | FR-011 | 해결 방안 추천 Agent: 파일경로+담당자+수정 snippet | 원인 후보 → 해결 가이드 |

> FR-009 (장애 분류 Agent) 는 구현하지 않는다. Cross-check Agent 의 출력이 분류 신호를 대체.

### Phase 4: Orchestrator 통합 + 리포트 (Week 5)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-013 | Orchestrator 통합: 단일 StateGraph에 전체 노드 연결, 3개 진입점 분기 + 선택적 실행 옵션 | generate_scenarios / generate_code / test 3개 경로 실행 성공 |
| 2 | FR-012 | 리포트 Tool: 단일 형식 템플릿, 실행요약+실패케이스+원인분석+해결방안 | 리포트 생성 확인 |
| 3 | FR-021 | 증적 캡처-보관: .qapilot/evidence/{trace_id}/ 저장, 대시보드 연동 | TC별 증적 묶음 확인 |

### Phase 5: CLI + 대시보드 + 부가 모듈 (Week 5~6)

| # | FR | 작업 | 검증 |
|---|---|---|---|
| 1 | FR-016 | CLI (Typer): init / generate scenarios / generate code / test / explain / spec import / rescan | 각 명령어 정상 동작 |
| 2 | FR-019 | 진척률 대시보드 (React): 6탭, RTM, 메트릭, CSV export | 대시보드 로딩 3초 이내 |
| 3 | FR-017 | 캐시 모듈: .qapilot/ 관리, LLM 캐시, TTL, 증분 | 동일 입력 캐시 히트 |
| 4 | FR-018 | 스케줄링 모듈: 자동 배치 실행 (test 명령만 스케줄링) | 스케줄 실행 → 리포트 자동 생성 |
| 5 | FR-022 | 요구사항 변경 히스토리: 버전 diff, 영향 시나리오 식별 | v2 업로드 → 영향 식별 |
| 6 | FR-023 | 시나리오 의존성 그래프: D3.js/Cytoscape.js DAG | 그래프 렌더링 + 인터랙션 |

### Phase 6: 통합 테스트 + 품질 (Week 6~7)

| # | 작업 | 검증 |
|---|---|---|
| 1 | E2E 통합 테스트 (generate scenarios → generate code → test 3단계 파이프라인) | 시나리오 1개 실행 2분 이내 |
| 2 | 성능 튜닝 | 코드 스캔 diff 10초 이내, 대시보드 로딩 3초 이내 |
| 3 | 보안 점검 | OWASP Top 10, 프롬프트 인젝션 방지, 민감정보 마스킹 |
| 4 | 골든셋 인수 테스트 | 시나리오 정확도 75%+, 원인 추론 Top-3 75%+ |
| 5 | 문서 산출물 정리 | CHANGELOG, API 문서 (Swagger) |

---

## 10. 인수 기준

| No | 항목 | 합격 기준 | 검증 방법 |
|---|---|---|---|
| 1 | 시나리오 생성 정확도 | 골든셋 300건 기준 75%+ | 독립 골든셋 측정 |
| 2 | 원인 추론 Top-3 | 결함 이력 200건 기준 75%+ | 이력 데이터 측정 |
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
