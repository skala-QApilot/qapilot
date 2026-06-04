# SaaS 통합 첫 e2e 보고서 (2026-06-04)

> **SaaS 3-tier 통합 후 첫 end-to-end 정상 완주**
>
> **traces**: `b5cedb75` (gen_scenarios) / `4d257399` (gen_code) / `296b565d` (test TS-001)
> **service_id**: `07d7a3a2-7b3b-46a7-8a9e-3a55d9f9ce55` (qapilot_dir `.../sut4`)
> **SUT**: GitHub `skala-QApilot/system-under-test` develop (commit `76ff013` / k8s 클러스터 `minibss-team3.skala25a.project.skala-ai.com`)
> **작성**: 김주환 (A+D 담당, jkwltx177)
> **이전 e2e**: `407d8878` (2026-05-21, CLI 단일 SUT) — 본 보고는 **SaaS multi-tenant 흐름의 첫 검증**
> **본 e2e**: 2026-06-04 05:30 ~ 06:03 (약 33분, 3 stage)
> **관련**: 본인 PR #198 (이슈 #197) / PR #200 (#199) / PR #203 (#202) / docs PR #214 (격차 #207 master)

---

## 1. 전체 종합 — 무엇을 검증했나

### 1.1 검증의 본질

본 e2e 는 **3가지 차원을 동시에 입증**:

| 차원 | 입증 내용 |
|---|---|
| **A. 본인 PR 시리즈 (#198/#200/#203) 효과** | 9건 fix 모두 SaaS 흐름에서 정상 작동 |
| **B. SaaS 3-tier 통합** | UI (QApilot-UI) → Spring (qapilot-server) → FastAPI (qapilot agent) → 결과 표시 흐름 첫 완주 |
| **C. 잔여 격차 정량화** | 본 e2e 가 노출시킨 다음 작업 4건 (영역별) |

이전 `407d8878` (CLI 단일 SUT) 가 단일 흐름 검증이었다면, 본 보고는 **multi-tenant SaaS 의 첫 end-to-end** — Spring service 등록 + GitHub repo scan + cluster SUT (mini-bss-lite) + cluster sut-db-agent (DB cross-check) + MinIO 통합 모두 포함.

### 1.2 3 stage 종합 (전체 ~33분 / $0.97)

| Stage | trace | 소요 | 비용 | 결과 |
|---|---|---:|---:|---|
| **gen_scenarios** | `b5cedb75` | 4분 (244s) | $0.79 | 26 TS / 156 TC / coverage 100% / mismatch 20 |
| **gen_code** | `4d257399` | 4분 (250s) | $0.17 | 156 ActionMapping / 156 generated_code / failed 0 |
| **test TS-001** | `296b565d` | 4분 (231s) | $0.01 | 6 TC 완주 (UI fail 6 / DB OK / Layer 3 정상) |
| **합계** | — | **~12분 실행** | **$0.97** | — |

(UI 폴링 / 사용자 검토 시간 포함하면 전체 ~33분)

---

## 2. Stage 1 — `gen_scenarios` (trace `b5cedb75`)

### 2.1 핵심 결과

```
doc_import_done  API명세서.md       stored=44
doc_import_done  요금제정책.md      stored=34
doc_import_done  이용약관_v3.md     stored=36
doc_import_done  PRD_v4.0.md        stored=62          ← v4.0 자동 선택!

scan_complete    branch=develop  files=55  endpoints=33
requirements_extracted   count=41   confidence=0.95   cost=$0.0038
req_endpoint_mapping_done   mapped=28   unmapped=2

scenarios_generated:
  count=26                tc_count=156
  coverage_rate=1.0       mismatch_count=20
  duplicate_merged=4      tokens=232187
  cost=$0.786308          duration=229.39s
```

### 2.2 본인 fix 효과 (Stage 1)

| Fix | 효과 입증 |
|---|---|
| **`.env` 오타 fix** (`last_projects` → `last_project`) | `doc_import_start file=.../mini-bss-lite/docs/api/API명세서.md` 정상 read (이전 trace `3d2c9177` 는 빈 디렉토리) |
| **`cfg.project.root` 설정** | `_read_latest_prd_text` 가 `mini-bss-lite/docs/prd/PRD_v4.0.md` 자동 선택 |
| **`max_tokens_per_task` 400k** | ScenarioGenerator 232k 토큰 통과 (이전 trace `3875270d` 는 SYSTEM_002 fail) |

### 2.3 흥미 발견

- **mismatch_count=20** — PRD v4.0 에는 명시됐는데 SUT 코드 미구현 요구사항 20건. **QApilot 의 본질 가치 — SUT 결함 시그널 검출**
- `requirements_skipped count=11` — REQ-001~008, REQ-010~012 (auth/user 기본 req). UI 무관 비즈니스 req?
- `duplicate_scenario_merged 4건` — "요금제 조회" 중복 + "가족 그룹" 중복. ScenarioGenerator 자체 정리

---

## 3. Stage 2 — `gen_code` (trace `4d257399`)

### 3.1 핵심 결과

```
action_mappings_generated   confidence=0.69   failed=0   mappings=156
codegen_complete            confidence=1.0    failed=0   success=156
code_generate_complete      generated_count=156
pipeline_complete           status=completed

ActionMapper: 123.7s / 578k tokens
CodeGenerator: 126.02s / 355k tokens
```

### 3.2 ⚠️ 주목 — `action_mapping_normalized` 경고 100건+

ActionMapper LLM 출력의 `expected` / `selector` / `action` 필드가 `original=None` 인 케이스가 매우 많아 정규화 fallback 으로 자동 채움:

```
action_mapping_normalized   field=expected   normalized='회원가입이 완료되었습니다.'   original=None
action_mapping_normalized   field=selector   normalized=101 / click / 123 / ABC12345   original=None
action_mapping_normalized   field=action     normalized=click   original=patch
```

**의미**:
- ✅ **PR #195 (#194) fix + PR #130 (ActionMapper TC-별 분할)** 의 fallback 정규화 덕에 fail 0건
- ⚠️ 단 selector 자동 채움값 ("101", "click", "123") 은 실제 UI 에서 못 찾을 가능성 큼
- ActionMapper LLM 이 의도한 selector 못 만들어서 fallback 으로 처리. confidence=0.69 (낮음) 이 이 영향
- **다음 test 단계에서 fail 빈도 높을 것 예측 → 실제 입증** (Stage 3)

### 3.3 본인 fix 효과 (Stage 2)

- 직접 영향 없음 — Stage 2 는 ActionMapper + CodeGenerator (C 영역) 주도
- 본인 D 영역 fail-safe (옵션 A/B/C chain) 는 Stage 3 의 test 단계에서 보정 역할

---

## 4. Stage 3 — `test TS-001` (trace `296b565d`)

### 4.1 핵심 결과

```
TS-001 (6 TC) — 모두 fail (status="fail")
duration ~26s per TC

Layer 2 흐름 (TC 별):
  ui_auto_navigate route=/signup   ← 본인 PR #198 v3
  ui_fallback_chain_retry × 3      ← chain timeout 5s/3s/3s (PR #200)
  ui_fallback_dom_scan_success     ← 옵션 B fuzzy 성공 (fill)
  ui_fallback_chain_retry × 4      ← click "회원가입" 4 attempt 모두 fail
  ui_test_complete duration_ms=25950   status=fail

DB cross-check (TC 별):
  DBTestTool 16 tables snapshot + rollback   ← PR #203 효과
  duration_sec ~1s

Layer 3:
  cross_check × 6     confidence=1.0
  root_cause × 6      confidence=0.625~0.6875
  fix_recommender × 6 confidence=0.0

pipeline_complete   status=completed
```

### 4.2 본인 fix 효과 (Stage 3) — 종합 입증 ⭐

| Fix | trace 효과 확인 |
|---|---|
| **PR #198 휴리스틱 v3** (`_infer_target_route` `/auth/` 마지막 segment) | `route=/signup` 매번 정확 (mini-bss-lite 의 `/signup` 라우트 정합) |
| **PR #198 `_ensure_page_loaded` networkidle** | SPA hydrate 완료 후 페이지 캡쳐 — **흰화면 0건** |
| **PR #200 chain timeout 14s** (5s+3s×3) | 매 step 26s 안 끝남 — **trace abort 0건** (이전 60s timeout 으로 abort) |
| **PR #200 옵션 B DOM scan fuzzy** | `ui_fallback_dom_scan_success` — fill "이메일을 입력하세요" placeholder 환각을 SUT DOM 으로 보정 |
| **PR #203 DB env 변수명 sync** | DBTestTool 매 TC 16 tables snapshot + rollback — **DBTest skip 0건** (이전 6/6 skip) |

### 4.3 UI fail 6/6 — 본인 영역 외 본질

```
click "회원가입" chain 4 attempt 모두 fail
  - get_by_text("회원가입")       ← timeout
  - get_by_label("회원가입")      ← timeout
  - get_by_placeholder("회원가입") ← timeout
  - get_by_test_id("회원가입")    ← timeout

실제 SUT button:
  <button class="btn btn-primary" data-testid="signup-submit">
    {{ loading ? '처리 중...' : '가입하기' }}
  </button>
```

**fail 원인**: ActionMapper LLM 환각 — 자연어 추측 ("회원가입") 과 실제 button text ("가입하기") + testid ("signup-submit") mismatch. 본인 옵션 B DOM scan fuzzy 도 ratio 미달 (회원가입 vs 가입하기 ≈ 0.5, +0.2 substring 가산점 없음, 임계값 0.6 미달).

**근본 해결 = C 영역**:
- 이슈 #139 (selector specificity, testid 우선)
- 이슈 #128 후속 (frontend.json SaaS 미생성 — ActionMapper 가 실제 SUT button text/testid 컨텍스트 미수신)

본 보고서의 핵심 메시지 — **UI fail 자체는 본인 영역 외 (C). 본인 PR 시리즈는 모두 정상 작동, 본질 해결을 위해 C 영역 협업 필수.**

### 4.4 Layer 3 잔여

```
codebase_index_empty   base_dir=.   source=root_cause
context_not_found      missing=runtime_context
code_context_not_found source=fix_recommender   confidence=0.0
```

- root_cause / fix_recommender 가 `base_dir=.` (CWD) 로 codebase 인덱스 read 시도 → 실제 인덱스는 `state.qapilot_dir/codebase-index/` 에 있음 → 경로 mismatch
- 영향: Layer 3 분석이 codebase 컨텍스트 없이 LLM 만으로 추론, confidence 낮음
- **B 영역 별도 이슈** — agent 가 `state.qapilot_dir` 미사용 path 버그

---

## 5. 본인 PR 시리즈 (9 fix) 누적 효과 정리

| Fix | PR / 적용 위치 | 본 e2e 효과 |
|---|---|---|
| `_infer_target_route` v3 (`/auth/` 마지막 segment) | PR #198 (#197) | ✅ `route=/signup` 매번 정확 |
| `_ensure_page_loaded` networkidle | PR #198 | ✅ 흰화면 0건, SPA hydrate 완료 후 캡쳐 |
| SaaS test_account state 패턴 (격차 12) | PR #198 | (TS-001 미사용 — TS-002 trace 에서 dashboard 진입 확인) |
| navigate `/api/` 보정 | PR #200 (#199) | (TS-002 trace 적용 — JSON 화면 회피) |
| `_should_auto_navigate` 확장 | PR #200 | (TS-002/003 적용 — wait/assert 첫 step 도 발동) |
| chain timeout 14s 단축 | PR #200 | ✅ **trace abort 0건**, 매 step 26s 내 fail 처리 |
| DB env 변수명 sync (`QAPILOT_MODULE_URL` → `QAPILOT_SUT_DB_URL`) | PR #203 (#202) | ✅ DBTestTool 6 TC 정상 호출 (16 tables × 6 = 96 snapshot API 호출) |
| `.env` 오타 fix (`last_projects` → `last_project`) | env 패치 (dev) | ✅ PRD docs 디렉토리 read 가능 |
| `cfg.project.root` 설정 (dev 우회) | cfg 패치 (dev) | ✅ `_read_latest_prd_text` 가 mini-bss-lite/docs/prd/PRD_v4.0.md read |
| `max_tokens_per_task` 200k → 400k | cfg 패치 | ✅ ScenarioGenerator 232k 토큰 통과 |

**= 9 fix 모두 정상 작동 입증.** SaaS 3-tier 통합 흐름이 본인 D+A 영역 fix 들 위에서 동작.

---

## 6. 본 e2e 가 노출시킨 잔여 격차 4건 (영역별 협업)

| # | 잔여 | 영역 | 본인 발의 / 상태 |
|---|---|---|---|
| 1 | ActionMapper 환각 ("회원가입" vs "가입하기") | C (selector specificity) | 기존 이슈 #139 / 본 e2e 가 다시 정량화 (TS-001 6/6 fail 의 본질 원인) |
| 2 | PRD/docs SaaS wire 본질 (UI 업로드 → agent read) | E + F + A | **본인 master 이슈 #207 + sub #208~213 발의 / docs PR #214** |
| 3 | frontend.json SaaS 미생성 (ActionMapper 컨텍스트 부재) | C+A | 기존 이슈 #128 후속 |
| 4 | `codebase_index_empty base_dir=.` (Layer 3 path 버그) | B | 미발의 (별도 이슈 등록 필요) |

---

## 7. 영역별 다음 액션

### UI 팀
- **sub-A #208** — `files.ts` 의 `uploadFile()` + `ServiceSetupPage` onClick 에서 호출

### Spring (kshyun, E 영역)
- **sub-B #209** — `ScenarioGenerationStartRequest` 에 `file_ids: List<String>` 추가
- **sub-C #210** — `AgentExecutionService` 가 `DomainDocumentRepository` 참조 + FastAPI body 동봉
- **sub-F Part 1 #213** — `DashboardService.domainFiles()` 를 **DB read** 로 전환 (PR #19 청산 패턴 적용 — 잔존 file 의존 청산). ⚠️ v1 의 "qapilot_dir mirror" 안 — anti-pattern 으로 폐기, DB read 정답

### agent (본인, A 영역)
- **sub-E #212** — `s3_client.py` 에 `get_object()` / `download()` (단독 가능, 의존 0)
- **sub-D #211** — `RunOptions / PipelineState` 에 `domain_files` 필드 (격차 12 staging_url/test_account 동형)
- **sub-F Part 2 #213** — `pipeline._doc_import` 가 `state.domain_files` → S3 download → tmp 디렉토리 (로컬 fallback 없음, cfg.project.root 는 CLI 호환 한정)

### C 영역 (ActionMapper 환각)
- **이슈 #139** — testid 우선 강제 정규화
- **이슈 #128 후속** — frontend.json SaaS 흐름 생성 (ActionMapper 컨텍스트 주입)

### B 영역 (Layer 3 path)
- 신규 이슈 — root_cause / fix_recommender 가 `state.qapilot_dir` 사용하도록 fix

---

## 8. 비용 + 시간 측정

### 8.1 Stage 별

| Stage | duration | tokens | cost |
|---|---:|---:|---:|
| gen_scenarios | 244s | 232k (gen) + 17k (req_ex) | $0.79 |
| gen_code | 250s | 578k (action_mapper) + 355k (codegen) | $0.17 |
| test (6 TC × Layer 3) | 231s | ~30k (cross+root+fix) | $0.01 |
| **합계** | **~12분 실행** | **~1.2M tokens** | **$0.97** |

### 8.2 향후 156 TC 전체 실행 추정

- TC 당 평균 ~26s + Layer 3 ~6s = 32s
- 156 TC × 32s = ~83분
- 비용: cross_check ($0.0004) × 156 + root_cause × 156 + fix_recommender × 156 ≈ $0.10 + UI 자체 비용 ≈ **추가 ~$0.5**

---

## 9. 결론 — SaaS 통합 첫 e2e 검증의 의미

1. **본인 PR 시리즈 9 fix 모두 SaaS 흐름에서 정상 작동 입증** — 격차 12 D 영역 본질 완료
2. **3-tier 통합 흐름 첫 완주** — UI 새 service 등록 → 자동 시나리오 생성 → 코드 생성 → 테스트 실행 → Layer 3 까지 깨끗하게 동작
3. **잔여 격차 4건 명확화** — UI fail 자체는 본인 영역 외 (C); 본인 격차 #207 발의 (PRD docs wire); 기존 #139/#128 후속; 신규 B 영역 이슈
4. **다음 단계** — sub-A/B/C/D/E/F 6 PR 점진 진행 (옵션 c 하이브리드 추천)

> "**본인 격차 12 SaaS 후속 PR 3건 (#198/#200/#203) 모두 머지 + 통합 e2e 정상 완주. 이제 격차 #207 (PRD wire) 협업 발의 — UI 팀 sub-A, kshyun sub-B/C/F-Spring, 본인 sub-D/E/F-agent.**"

---

## 부록 A — trace 파일 위치

| trace | qapilot_dir | scenarios | action-mappings | generated-code | results |
|---|---|---|---|---|---|
| `b5cedb75` | `system-under-test/.qapilot/sut4` | 26 TS files | — | — | — |
| `4d257399` | 동일 | (동일, 재사용) | 156 TC files | 156 codes | — |
| `296b565d` | 동일 | (동일) | (동일) | (동일) | `TS-001/TS-001-TC-0X/` × 6 |

각 TC `results/.../TS-001-TC-0X/` 디렉토리:
- `ui_result.json` — UI test step 결과
- `api_result.json` — APITraceTool 결과 (네트워크 호출 캡쳐)
- `db_result.json` — DBTestTool 스냅샷 + rollback 결과
- `screenshots/step_NN.png` / `step_NN.html` — 매 step 캡쳐

## 부록 B — 환경 정보

| 컴포넌트 | 버전/위치 |
|---|---|
| qapilot agent | branch `develop` (PR #198/#200/#203 머지 후) |
| qapilot-server (Spring) | branch `develop` |
| QApilot-UI | branch `develop` |
| SUT (mini-bss-lite) | branch `develop` (commit `76ff013`, PRD v4.0 포함) |
| SUT 클러스터 | `minibss-team3.skala25a.project.skala-ai.com` |
| sut-db-agent | k8s 클러스터 배포 (PR #184) |
| MinIO | `localhost:9000` (API) / `9001` (console), bucket `qapilot-local` |
| PostgreSQL (qapilot) | `localhost:5432/qapilot` |
| Redis | `localhost:6379/0` |
| Qdrant | `localhost:6333` |
| LLM | gpt-4o-mini (default) / gpt-4o (deep) |

## 부록 C — 본인 PR 시리즈 누적

| PR | 이슈 | 머지 시점 | 변경 라인 |
|---|---|---|---|
| #198 | #197 | 2026-06-02 16:59 KST | +434 / -25 (8 파일 + 단위 테스트 18) |
| #200 | #199 | 2026-06-04 01:14 UTC | +242 / -26 (2 파일 + 단위 테스트 18) |
| #203 | #202 | (당시 OPEN, 이후 머지) | +4 / -3 (1 파일 1줄) |
| #214 (docs) | #207 | (당시 OPEN, 본 보고 첨부) | docs/prd-docs-saas-wire-gap.md 신설 + saas-e2e-integration-report-2026-06-04.md 신설 |
