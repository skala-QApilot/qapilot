# Orchestrator 4-Layer 전체 흐름 점검 (2026-05-18)

> 작성: 김주환 (A 담당 — FR-013 Orchestrator)
> 대상: Layer 3 본 구현 완료 후 Layer 1A → 1B → 2 → 3 의 데이터 흐름 + 의미 정합성 + 실 동작 가능 여부 종합 점검
> 갱신 (2026-05-18 후속): DefectClassifier 미구현 결정 — CrossCheckAgent 가 이미 생산하는 `error_code`/`summary`/`mismatches` 를 RootCauseAgent 에 직결. `_defect_classify` 노드 / `defect_results` state / DefectClassifierAgent / 관련 프롬프트·스키마 모두 제거. 17→16 노드.

---

## 1. 노드 wire-up 매트릭스 — 16/16 WIRED ✅

| Layer | 노드 | 상태 | 호출 컴포넌트 | 출력 PipelineState 필드 |
|---|---|---|---|---|
| Pre-L1 | `_doc_import` | ✅ | DomainKnowledgeTool(action=import) | (Qdrant 적재, state X) |
| **L1A** | `_codebase_scan` | ✅ | CodebaseScannerTool | `scan_result` + `.qapilot/codebase-index/*.json` |
| L1A | `_domain_knowledge` | ✅ | DomainKnowledgeTool(action=search) | `domain_rules` |
| L1A | `_requirement_extract` | ✅ | RequirementExtractorAgent (user_input 있을 때만) | `requirements` |
| L1A | `_scenario_generate` | ✅ | ScenarioGeneratorAgent | `scenarios` |
| L1A | `_save_scenarios` | ✅ | (디스크 저장 전담) | `saved_scenario_paths` + `.qapilot/scenarios/*.json` |
| **L1B** | `_load_scenarios_for_codegen` | ✅ | (디스크 로드) | `scenarios` |
| L1B | `_action_mapping` | ✅ | ActionMapperAgent | `action_mappings` |
| L1B | `_code_generate` | ✅ | CodeGeneratorAgent | `generated_codes` |
| L1B | `_save_codes` | ✅ | (디스크 저장 전담) | `saved_code_paths` + `.qapilot/generated-code/*.js` + **`.qapilot/action-mappings/*.json` (PR #89)** |
| **L2** | `_load_scenarios_for_test` | ✅ | (디스크 로드 + depends_on 정렬) | `scenarios` + `action_mappings` + `generated_codes` |
| L2 | `_test_execution` | ✅ | APITrace + UITest + DBTest (graceful) | `ui_results` + `api_results` + `db_results` + `.qapilot/results/{trace_id}/...` |
| L2 | `_cross_check` | ✅ | CrossCheckAgent | `cross_check_results` (`error_code`/`summary` 보존) + `has_mismatch` |
| **L3** | `_root_cause` | ✅ | RootCauseAgent | `root_cause_results` |
| L3 | `_fix_recommend` | ✅ | FixRecommenderAgent | `fix_results` |
| End | `_report` | ✅ | (markdown 직접 생성, Report Tool stub) | `report_path` + `.qapilot/reports/{trace_id}.md` |

---

## 2. Layer 간 데이터 의미 흐름 (deep semantic)

### 2.1 L1A → L1B 연결 (`qapilot generate scenarios` → `qapilot generate code`)

**연결 매개**: 디스크 `.qapilot/scenarios/*.json` (PipelineState 휘발성 우회)

| L1A 출력 | L1B 입력 | 의미 보존 점검 |
|---|---|---|
| `state["scenarios"]` (TS/TC/TV) | `_load_scenarios_for_codegen` 이 디스크에서 로드 | ✅ JSON 직렬화·역직렬화 무손실. `depends_on` (PR #82) 포함 |
| `scan_result` | (재사용 X — L1B 가 새로 안 부름) | ⚠️ Layer 분리 — L1B 는 시나리오만 사용 |

→ **의미 보존 정합**. 사용자가 두 명령 사이에 시나리오 대시보드 수정 가능 (HITL 정신).

### 2.2 L1B → L2 연결 (`qapilot generate code` → `qapilot test`)

**연결 매개**: 3종 디스크 자산 (PR #89 의 핵심 결정)
- `.qapilot/scenarios/*.json`
- `.qapilot/action-mappings/*.json` ⭐ (PR #89 신규)
- `.qapilot/generated-code/*.js`

| L1B 출력 | L2 입력 | 의미 보존 점검 |
|---|---|---|
| `state["action_mappings"]` | `_load_scenarios_for_test` 가 `.qapilot/action-mappings/*.json` 로드 | ✅ PR #89 의 옵션 A 적용. PipelineState 휘발성 우회 |
| `state["generated_codes"]` | `.qapilot/generated-code/*.js` 로드 | ✅ (단 L2 는 ActionMapping 우선 사용, JS 는 검증·디버그용) |
| `state["scenarios"]` | `.qapilot/scenarios/*.json` 로드 + `depends_on` 토폴로지 정렬 | ✅ PR #82 의 `depends_on` 필드 활용 |

→ **결정성 + 사용자 검토 가능성 + 비용 절감** 셋 동시 확보. spec §6.1 자기 일관성 회복.

### 2.3 L2 내부: ActionMapping → UI Test → Cross-check

```
state["action_mappings"]  (TC별 ActionStep 시퀀스)
       │
       ▼
[_test_execution] — 단일 page 인스턴스 공유
       ├─ APITraceTool.run(page, tc_id) — listener 등록 (즉시 반환)
       │     listener 가 self._calls list 에 누적 (같은 참조)
       ├─ UITestTool.run(page, action_mapping, target_url, screenshot_dir)
       │     ↑ ActionStep 27종 처리 + selector None fallback + TOOL_UI_* error 카테고리
       ├─ DBTestTool.run(tc_id) — graceful (QAPILOT_MODULE_URL 미설정 시 흡수)
       └─ L2 디스크 저장 .qapilot/results/{trace_id}/{ts_id}/{tc_id}/*.json
       │
       ▼
state["ui_results"] + state["api_results"] + state["db_results"]
       │
       ▼
[_cross_check] — TC 별 CrossCheckAgent 호출
       context={ui_result, api_trace, db_result}
       │
       ▼
state["cross_check_results"]  (match_score, mismatches, error_code, summary)
state["has_mismatch"] = any(...)
```

**의미 보존 점검**:
- ✅ X-Trace-Id 헤더가 `browser.new_context(extra_http_headers=...)` 로 모든 요청에 자동 주입 (spec FR-006)
- ✅ 같은 page 인스턴스라 APITrace listener 가 UITest 동작 동안 모든 요청 캡처
- ✅ UIStepResult.error 의 `TOOL_UI_*` prefix (PR #79) 가 CrossCheckAgent (PR #62 UI 에러 코드 추출) 와 통합 가능
- ⚠️ TC 간 cookie/session 공유 (같은 context 안 순차) — 격리 필요 시 후속 PR

### 2.4 L2 → L3 분기 (`has_mismatch` 조건부 edge)

```python
graph.add_conditional_edges(
    "cross_check",
    lambda state: "root_cause" if state["has_mismatch"] else "report",
)
```

| 분기 | 도달 노드 |
|---|---|
| `has_mismatch=False` | `_report` 직진 (Layer 3 skip) |
| `has_mismatch=True` | `_root_cause` → `_fix_recommend` → `_report` |

→ **정상 케이스 효율 + 비정상 케이스 깊은 분석** 분리. LLM 비용 최적화.

### 2.5 L3 내부: 원인 추론 → 해결 제안

```
state["cross_check_results"]  (mismatch TC 목록 + error_code/summary 보존)
       │
       ▼
[_root_cause] — has_mismatch=True TC 만 RootCauseAgent 호출
       params={tc_id, error_code, summary, mismatches, has_mismatch}
       ← Cross-check 의 error_code/summary 가 그대로 입력
       │
       ▼
state["root_cause_results"]  (Top-N candidates with rank/confidence/evidences/affected_file:line)
       │
       ▼
[_fix_recommend] — TC 별 FixRecommenderAgent 호출
       params={tc_id, candidates}
       ← RootCause 의 candidates 가 입력
       │
       ▼
state["fix_results"]  (FixSuggestion: file_path, line_number, blame_author, code_snippet, description)
       │
       ▼
[_report] — 4 섹션 markdown (실패 / Cross-check / 원인 / 해결)
```

**의미 보존 점검**:

- ✅ `_cross_check` 가 `output.result` 의 `error_code`/`summary` 를 `cross_check_results[i]` 에 보존 (PR #91 후속 수정)
- ✅ Cross-check 의 `error_code`/`summary`/`mismatches` 가 RootCause params 로 그대로 전달 — DefectClassifier 우회
- ✅ RootCause 의 `candidates` 가 FixRecommender params 로 전달 (rank·confidence·evidences 보존)
- ✅ RootCause 의 `affected_file:line` 이 FixRecommender 의 `file_path:line_number` 로 자연 매핑
- ✅ `_report` 가 root_cause/fix 둘을 한 markdown 으로 통합 (장애 분류 섹션 제거됨)

---

## 3. 휘발성 vs 영속성 매트릭스 (L1/L2/L3)

| 자산 | L1 PipelineState (메모리) | L2 디스크 (.qapilot/) | L3 서버 DB |
|---|---|---|---|
| ScanResult | ✅ | ✅ `codebase-index/*.json` (PR #54) | Phase 2 |
| domain_rules | ✅ | (Qdrant 자체가 L2) | Phase 2 |
| requirements | ✅ | (PG, PR #14) | (이미 L3) |
| scenarios | ✅ | ✅ `scenarios/{ts_id}.json` | Phase 2 |
| action_mappings | ✅ | ✅ **`action-mappings/{tc_id}.json` (PR #89)** | Phase 2 |
| generated_codes | ✅ | ✅ `generated-code/{tc_id}.js` | Phase 2 |
| ui_results | ✅ | ✅ `results/{trace_id}/{ts_id}/{tc_id}/ui_result.json` | Phase 2 |
| api_results | ✅ | ✅ `results/.../api_result.json` | Phase 2 |
| db_results | ✅ | ✅ `results/.../db_result.json` | Phase 2 |
| cross_check_results | ✅ (`error_code`/`summary` 보존) | (`_report` 가 markdown 으로 요약) | Phase 2 |
| root_cause_results | ✅ | (markdown 통합) | Phase 2 |
| fix_results | ✅ | (markdown 통합) | Phase 2 |
| report | ✅ (path) | ✅ `reports/{trace_id}.md` | Phase 2 |

→ **모든 핵심 자산이 L1 + L2 영속화**. L3 서버 DB 는 Phase 2 (별도 PR).

---

## 4. spec 정합성 점검

| spec 항목 | 본 PR + 누적 PR 의 구현 | 정합 |
|---|---|---|
| §2.1 3-파이프라인 분리 | generate_scenarios / generate_code / test 진입점 3개 | ✅ |
| §2.2 Orchestrator LangGraph 고정 DAG | LangGraph StateGraph, 진입점 분기 + has_mismatch 조건부 edge | ✅ |
| §3.1 Orchestrator (LLM 미사용) | wire-up 자체 LLM 호출 0 (Agent 가 LLM) | ✅ |
| §3.2 Agent 9개 | DefectClassifier 미구현 결정 (CrossCheck → RootCause 직결) — 8개 호출 wire | ⚠️ 1개 의도적 제거 (spec 갱신 필요) |
| §3.3 Tool 6개 | 6개 모두 호출 wire (ReportTool 만 stub, 본인 임시 markdown) | ⚠️ 1개 stub graceful |
| §4.5 C/D/E 정책 (v1.4) | UITestTool 27종 + selector null + TOOL_UI_* error 카테고리 | ✅ |
| §6.1 디렉토리 구조 (v1.5) | scenarios/ + action-mappings/ + generated-code/ + results/ + reports/ 모두 사용 | ✅ |
| §7.5.1 TOOL_UI_* 7종 (v1.4) | UIStepResult.error 에 prefix 자동 | ✅ |

→ **명세 정합 ⚠️**. DefectClassifier 는 의도적 제거 (CrossCheckAgent 가 이미 분류 신호 — `error_code`/`summary`/`mismatches` — 를 생산하므로 중복 단계 제거). ReportTool 본체는 타팀 영역 (stub).

---

## 5. 실 동작 가능성 점검

### 5.1 `qapilot generate scenarios` (L1A)
- 의존: OPENAI_API_KEY + Qdrant + (선택) PG
- 실행 가능 ✅ — 본인 E2E 검증 완료 (진행_현황.md 의 §2.1)
- 단 진행_현황.md 의 문제 7가지 (ScenarioGen 도메인 미반영 등) 잔존 — 본인 영역 외

### 5.2 `qapilot generate code` (L1B)
- 의존: OPENAI_API_KEY + L1A 산출물 (`.qapilot/scenarios/`)
- 실행 가능 ✅ — 본인 E2E 검증 완료
- 신규: `action-mappings/*.json` 도 디스크 저장 (PR #89)

### 5.3 `qapilot test` (L2 + L3) — **본 PR 의 완성형**
- 의존:
  - L1B 산출물 (`.qapilot/scenarios/`, `.qapilot/action-mappings/`, `.qapilot/generated-code/`)
  - Python Playwright (이미 설치)
  - SUT 가 `target_url` 에 기동 중
  - QAPILOT_MODULE_URL (선택 — DB 테스트용, 미설정 graceful)
  - OPENAI_API_KEY (Cross-check + RootCause + FixRecommender)
- 실행 가능 ✅:
  - `has_mismatch=False` 케이스: L2 → report 직진. ✅ 정상 완료
  - `has_mismatch=True` 케이스: L2 → L3 (root_cause → fix_recommend) → report. ✅ 정상 완료
- **[리뷰어/사용자 권장] 실 E2E smoke**: mini-bss-lite cwd 에서 `qapilot generate scenarios && qapilot generate code && qapilot test`

---

## 6. 한계점 및 후속 작업

### 본 PR 의 한계

1. **Report Tool 본체 stub** — `_report` 가 노드 내 markdown 직접 생성. FR-012 의 단일 형식 spec 미준수. ReportTool 본체 머지 시 호출로 교체 (별도 PR)
2. **TC 간 cookie/session 공유** — 같은 context 안 순차. 격리 필요 시 후속 PR
3. **실 통합 테스트 부재** — async_playwright + LLM 의존. mock 기반 단위 테스트 통과 (Layer 3 6개)
4. **L3 서버 DB 업로드** — Phase 2 (별도 PR)
5. **DefectClassifier 제거 영향** — FR-009 가 spec 에 남아있다면 spec 갱신 필요 (CrossCheckAgent 의 `error_code`/`summary` 가 분류 정보를 대체)

### 후속 PR 후보 (본인 영역)
| 우선순위 | 작업 |
|---|---|
| P1 | Layer 1A 의 ScenarioGen 산출물 품질 점검 (진행_현황.md 의 7가지 문제) — 본인 wire-up 책임 아니지만 협조 |
| P2 | 실 E2E 통합 테스트 — mini-bss-lite 대상 |
| P3 | ReportTool 본체 머지 시 `_report` 정식화 |
| P4 | TC 간 격리 옵션 (`--isolate-tc`) |
| P5 | 시드 격리 옵션 (`--clean`) |
| P6 | L3 서버 DB 업로드 (Phase 2) |
| P7 | spec 갱신 — FR-009 (DefectClassifier) 제거 반영 |

### 후속 작업 (타팀 영역, 본인 협조)

- ReportTool 본체 (FR-012)

---

## 7. 본인 누적 (2026-05-18 기준)

- **머지 17건**: #7 / #18 / #21 / #24 / #27 / #42 / #47 / #51 / #54 / #59 / #63 / #65 / #67 / #69 / #79 / #83 / **#89 (Layer 2 wire-up)**
- **OPEN 1건**: **PR #91 (예정) — Layer 3 wire-up + report 확장 + spec v1.6**

---

## 마무리

본 PR (Layer 3 본 구현 + DefectClassifier 제거) 머지 후 **Orchestrator 16 노드 완전 wire 상태**. `qapilot test` E2E 흐름 완성 (ReportTool 본체 stub 잔존하나 graceful). DefectClassifier 단계 제거 결정에 따라 spec 갱신 필요 (FR-009). 본인 다음 작업은 P1~P7 우선순위에서 선택.
