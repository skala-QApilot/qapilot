# 역추적 검증 — 회의 결론 ↔ 본인 구현 매핑

> 본인이 객관적 시각으로 점검 (구현자 아닌 외부 reviewer 가정).
> 회의 verbatim 결론을 항목별로 본인 구현과 1:1 매핑 + 미흡 영역 명시.

---

## 0. 검증 방법

회의 verbatim 결론 6 항목을 그대로 quote → 본인 구현 모듈/PoC 매핑 → 상태 표기:
- ✅ **완전 구현**: 회의 의도 그대로 모듈/함수 존재 + 동작 검증됨
- ⚠️ **부분 구현**: component 는 있으나 caller/wiring 없음 (단독 호출 불가)
- ❌ **미구현**: 모듈/함수 자체 없음
- 🔵 **타 영역**: 본인 영역 외 (다른 팀 담당, OK)

---

## 1. 회의 결론

### 결론 1. PRD 로 요구사항 추출 및 TS(기능) 생성

| 본인 영역? | 상태 |
|---|---|
| 🔵 유빈 (TS Generator agent) | 본인 영역 외 — OK |

본인 데이터 layer 가 PRD 제공 = 없음 (PRD 는 본인 `metadata_indices` schema 의 추출 대상 아님 — PRD 는 별도 `domain_documents` 영역). 본인이 만든 것 = PRD 외 코드베이스/메타데이터 layer.

---

### 결론 2. 테스트 시나리오 HITL 검증 (후순위)

| 본인 영역? | 상태 |
|---|---|
| 🔵 흐름 제어 / UI | 본인 영역 외 — OK (후순위로 회의 명시) |

---

### 결론 3. 시나리오 기반 TC(Happy/예외) 생성

| 본인 영역? | 상태 |
|---|---|
| 🔵 유빈 (TC Generator agent) | 본인 영역 외 — OK |

본인이 제공: `load_metadata_index` + `load_source` — 유빈이 TC 생성 LLM context 로 활용.

---

### 결론 4. 정책서/약관/api/코드베이스 기반 TC 별 G/W/T/V 생성

> 본인 영역의 핵심. 6 sub-항목 모두 검증.

#### 4.1 given/when/then/value 까지 전부

| 본인 영역? | 상태 |
|---|---|
| 🔵 유빈 (TV Generator agent) | LLM 호출 = 유빈 / 검증 helper = 본인 (PoC 7) |

본인이 제공 = `TVValidator.validate()` (PoC 7). 유빈이 호출 → valid=False 시 재시도.

#### 4.2 DB 스캔 툴 사용해서 value 가져오기

| 모듈 | 본인 PoC | 상태 |
|---|---|---|
| DBTool snapshot 호출 wrap | PoC 8 (`qapilot/shared/db_state.py`) | ✅ `list_db_tables_cached` + `get_db_snapshot_cached` |
| TTL cache (반복 호출 cost 절감) | PoC 8 (`_TTLCache` 클래스) | ✅ 300s/60s 기본 + caller override |
| 큰 SUT (10만+ row) `/db/query` endpoint | 회의: E 영역 협업 | 🔵 본인 영역 외 |

end-to-end: `get_db_snapshot_cached(sid, "customers") → TVValidator.validate(..., db_snapshot=snap["rows"], expects_existing_in_db=True)` 통과 (PoC 7 단위 테스트).

#### 4.3 코드베이스

##### a) GitHub → 스캔 → S3 전부 저장 (비용 따져 결정)

| 모듈 | 본인 PoC | 상태 |
|---|---|---|
| S3 PUT 함수 (`source/{sha}/{path}`) | PoC 3 (`upsert_source_file`) | ✅ 함수 존재 + 단위 테스트 |
| `head_object` cache skip | PoC 3 (`s3_client.head_object`) | ✅ |
| 비용 시뮬레이션 (mini-bss 150 file ≈ $0.00075) | docs (`s3-path-spec.md` §2.2) | ✅ 문서 |
| **`git clone --depth 1` → walk → upsert caller** | — | ❌ **없음** |
| TTL Lifecycle policy (S3 30일) | docs (`s3-path-spec.md` §4) | ⚠️ 문서만, S3 bucket lifecycle 적용 X |

⚠️ **미흡**: 본인이 `upsert_source_file()` 함수만 만들었음. 실제 "git clone → file walk → 각 파일 PUT" 의 high-level orchestrator (`scan_source_to_s3(service_id, repo_url, commit_sha)` 같은 함수) **없음**. 호출자가 직접 git clone + walk 해야 함.

##### b) codebase-index 추출 현재 유지 → S3 저장

| 모듈 | 본인 영역? | 상태 |
|---|---|---|
| 기존 `codebase_indices` (endpoints/models/functions/callgraph/manifest/frontend) | 🔵 C 영역 (`qapilot/tools/codebase_scanner_tool.py`) | ✅ 이미 존재 (변경 X) |
| S3 저장 | 🔵 C 영역 (`qapilot/db/code_writer.upsert_codebase_index`) | ✅ 이미 존재 |
| 본인 영역과의 충돌 | 본인 `metadata_indices` 별도 namespace (회의 결정 4) | ✅ 충돌 X |

##### c) 테스트 대상 시스템의 테스트 코드 스캔

| 모듈 | 본인 PoC | 상태 |
|---|---|---|
| pytest AST 추출 | PoC 5 (`pytest_ast_parser.py`) | ✅ + mini-bss 106 records |
| LLM 의미 분류 (unknown 보강) | PoC 5.1 (`llm_pattern_classifier.py`) | ✅ + 16 단위 테스트 |
| Pydantic schema (TestPatternRecord) | PoC 1 (`SutTestsPatternsIndex`) | ✅ |
| 다른 framework (Playwright/Cypress/JUnit) | 추후 — docs 명시 | 🔵 본 PoC 영역 외 |

##### d) 프론트 셀렉터/라우터/스키마 메타데이터화

| 메타데이터 | 본인 PoC | 상태 |
|---|---|---|
| 프론트 셀렉터 | PoC 2 (`vue_sfc_parser.py` — Vue) | ✅ + mini-bss 9/9 |
| 프론트 라우터 | PoC 6.B (`vue_router_parser.py` — Vue Router) | ✅ + mini-bss 16/16 |
| 백엔드 스키마 | PoC 6.A (`backend_schema_parser.py` — Pydantic + SQLAlchemy) | ✅ + mini-bss 45 records |
| 다른 framework (React/Angular/Java JPA) | 추후 | 🔵 |

회의 verbatim: "**애 먼저 정해야함**" — schema 정의 (PoC 1) 완료, 모든 추출기 schema 따름.

##### e) "3, 4 를 기존 2 에 추가해야 하는 느낌"

회의 verbatim 해석 — 기존 `codebase-index (5 kind)` 에 `(test 코드 스캔)` + `(프론트/백엔드 메타데이터)` 를 더해야 함.

| 결정 (회의 결정 4) | 본인 구현 |
|---|---|
| 별도 `metadata_indices` 테이블 (codebase_indices 와 분리) | ✅ DDL + Pydantic + UNIQUE 제약 |
| kind/sub_kind 2축 (4 영역 자유 확장) | ✅ |
| 본인 mirror fallback 패턴 (#240) 재사용 | ✅ `upsert_metadata_index` |

##### f) "b / 5 기준으로 검색해서 TC, TV 만들기"

회의 verbatim 해석 — codebase-index (b) + 메타데이터-index (5 = d 의 결과) 둘 다 조회해서 LLM context 주입.

| 모듈 | 본인 PoC | 상태 |
|---|---|---|
| metadata-index 조회 | PoC 4 (`load_metadata_index`) | ✅ + LRU |
| codebase-index 조회 | 🔵 C 영역 (`code_reader.load_codebase_index`) | ✅ 이미 존재 (본인 패턴 #240 차용) |
| source 본문 조회 | PoC 4 (`load_source`) | ✅ + line range 슬라이싱 |
| 유빈 agent caller | 🔵 유빈 영역 | ⏳ 본인 영역 외 |

---

### 결론 5. "시나리오 → PRD/정책서/약관 검색해서 TC 생성 → 코드베이스 기반으로 TV 생성"

| 영역 | 상태 |
|---|---|
| 🔵 흐름 = 유빈 agent | 본인 영역 외 |
| 본인이 코드베이스 기반 TV 생성에 제공: `load_metadata_index`, `load_source`, `get_db_snapshot_cached`, `TVValidator.validate` | ✅ 모두 PoC 4/8/7 |

---

### 결론 6. "DB 스캔 툴 일 하고 있나요? → 비교 → 테스트 데이터 준비할 때 스캔 먼저"

| 모듈 | 본인 PoC | 상태 |
|---|---|---|
| DBTool snapshot wrap | PoC 8 | ✅ |
| TTL cache (반복 호출 1회) | PoC 8 | ✅ |
| 비교 검증 (`expects_existing_in_db`, `expects_absent_in_db`) | PoC 7 `TVValidator` | ✅ |
| "테스트 데이터 준비할 때 스캔 먼저" = 액션 매핑 단계 통합 | 🔵 액션 매핑 = 추후 결정 (회의: 액션 매핑 이전까지 구현) | OK |

---

## 2. 미흡 영역 종합 (본인이 객관적 인정)

### 2.1 ⚠️ 부분 구현 (component 있음, caller 없음)

#### A. GitHub repo → S3 source/ dump orchestrator
**현재**: `upsert_source_file(service_id, commit_hash, relative_path, content)` — **파일 1개씩** PUT 하는 저수준 함수.
**없음**: `scan_source_to_s3(service_id, repo_url, commit_sha)` 같은 high-level — git clone (`--depth 1`) → file walk (whitelist `.py/.js/.ts/.vue/...`) → 각 파일 `upsert_source_file` 호출.

→ 본인 PoC 9 (후속) 필요. 또는 caller (service register flow) 가 직접 git clone 후 본인 함수 반복 호출.

#### B. Service register flow → 4영역 추출기 통합 caller
**현재**: 본인이 단위 components 만 만듦 (각 extractor 1개 호출 = 1 파일 추출).
**없음**: service 등록 시 자동으로:
1. git clone
2. 모든 `.vue` 파일 → vue_sfc_parser → 합쳐서 `FrontendSelectorsIndex` → `upsert_metadata_index`
3. `router/index.{js,ts}` → vue_router_parser → `FrontendRoutesIndex` → upsert
4. `backend/**/*.py` (Pydantic/SQLAlchemy) → backend_schema_parser → `BackendSchemasIndex` → upsert
5. `tests/**/test_*.py` → pytest_ast_parser → `SutTestsPatternsIndex` → upsert (+ LLM 보강)

→ 본인 PoC 10 (후속) 필요. 또는 ScanTool 의 entry point 가 본인 모듈 호출.

#### C. S3 Lifecycle policy 적용
**현재**: docs 에 정책만 명시 (`s3-path-spec.md` §4).
**없음**: 실제 MinIO/AWS S3 bucket 에 lifecycle rule 설정 — `services/*/source/* 30일 후 삭제`.

→ 운영 배포 시점에 필요 (현재 PoC 단계는 무관).

### 2.2 🔵 본인 영역 외 (의도적 미구현)

- 유빈 TC/TV Generator agent — 본인 함수 호출자
- 액션 매핑 단계 통합 — 회의 결정 "액션 매핑 이전까지 구현"
- HITL 시나리오 검증 UI — 회의 후순위
- 큰 SUT `/db/query` endpoint — E 영역 (kshyun)
- 다른 framework 추출기 (React/Playwright/Cypress/JUnit) — 회의 명시 X

### 2.3 ❌ 미구현 (확장 시 발견될 가능성)

- ScanTool 의 통합 entry point — 본인 영역 모든 추출기를 한 호출에 실행하는 wrapper
- LLM 보강 (PoC 5.1) 의 다른 영역 적용 — selectors/routes/schemas 에는 LLM 의미 라벨링 미적용 (회의에서 동적 element / OutputElement.semantic_kind 미정의)
- alembic migration 통합 — 현재 DDL 은 `migrations/001_metadata_indices.sql` 만, alembic version 등록 X

---

## 3. 회의 결론 ↔ 본인 구현 종합 매트릭스

| 회의 결론 | 본인 PoC | 상태 |
|---|---|---|
| 1. PRD → TS | 🔵 유빈 | — |
| 2. HITL 검증 | 🔵 후순위 | — |
| 3. TS → TC | 🔵 유빈 | — |
| 4.1 G/W/T/V (LLM) | 🔵 유빈 | — |
| 4.1 검증 helper | PoC 7 TVValidator | ✅ |
| 4.2 DB 스캔 → value | PoC 8 db_state | ✅ |
| 4.3.a github → S3 (함수) | PoC 3 upsert_source_file | ✅ |
| 4.3.a github → S3 (orchestrator) | — | ⚠️ caller 없음 |
| 4.3.b codebase-index → S3 | 🔵 C 영역 (기존) | ✅ |
| 4.3.c SUT 테스트 스캔 | PoC 5 + 5.1 | ✅ |
| 4.3.d 프론트 셀렉터 | PoC 2 | ✅ |
| 4.3.d 프론트 라우터 | PoC 6.B | ✅ |
| 4.3.d 백엔드 스키마 | PoC 6.A | ✅ |
| 4.3.e namespace 분리 | PoC 1+3 metadata_indices | ✅ |
| 4.3.f 검색 → TC/TV | PoC 4 load_metadata_index/source | ✅ |
| 5. 흐름 통합 | 🔵 유빈 + ⚠️ wiring | 부분 |
| 6. DB 스캔 → 비교 | PoC 7 + 8 | ✅ |

**13 완전 구현 / 2 부분 (caller 없음) / 6 본인 영역 외**.

---

## 4. 검증 결론

본인 데이터 layer (추출/저장/조회/검증/cache) 5 components 는 **모두 단독 동작 + 단위 테스트 + 실 환경 검증 통과**. 단 **service register flow 와의 통합 caller 가 없어, 본인 모듈을 사용하려면 caller (유빈 agent 또는 ScanTool entry point) 가 직접 호출 체인 구성 필요**.

→ 다음 본인 작업 우선순위:
1. **PoC 9**: `scan_source_to_s3` orchestrator (git clone → walk → upsert_source_file)
2. **PoC 10**: service register entry point — 4영역 추출기 통합 caller
3. (선택) PoC 11+: framework 확장 (React/Playwright 등)
