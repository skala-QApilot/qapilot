# Scan Enhancement Foundation

> **One-line**: 코드 스캔 결과를 TC/TV 생성 agent 가 완벽하게 활용할 수 있는 데이터 layer.

본 디렉토리는 2026-06-09 팀 토론 결과 (PRD → TS → TC → TV 4단계 워크플로우의 데이터 layer 재설계) 의 구현 문서다. 작업 영역은 **주환 (A 영역 + 데이터 layer 확장)**. 본 문서는 **before / after / 회의 결정사항 / 컴포넌트 연결부** 를 사람이 읽기 쉽게 정리하는 것이 목적이다.

---

## 0. 한눈에 보는 변화 (Before → After)

### Before (2026-06-07 시점)

```
┌─────────────────────────────────────────────────────────────┐
│ ScanTool (frontend.json + codebase-index 5 kind)            │
│   - endpoints / models / functions / callgraph / manifest    │
│   - frontend.json (별도, 통합 안 됨)                          │
└────────────────────┬────────────────────────────────────────┘
                     │ DB: codebase_indices
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ ScenarioGenerator (LLM)                                      │
│   - PRD + 정책서 + 약관 + API 문서 + 코드베이스 인덱스        │
│   - 출력: TS + TC + TC.values (한 LLM call 에 모두)         │
│   - 격차 A-1: 코드 동작을 정답으로 박제 (then 절을 코드에서)│
│   - 격차 A-2: BSS 한국어 키워드 hardcoded                    │
│   - 격차 A-3: TC-01 의 order_id 를 TC-02 에 못 전달          │
└──────────────────────┬──────────────────────────────────────┘
                       │ ActionMapping
                       ▼
                  CodeGenerator → UITestTool → cross_check
                  (2026-06-07 누적 14 PR 의 격차들)
```

**문제**:
- 시나리오 생성 단계에서 SUT 의 **프론트 셀렉터 / 라우터 / 스키마 구조** 정보가 없거나 단편적 → ActionMapper 가 환각 selector 생성
- SUT 의 **기존 테스트 코드** 정보 없음 → 인증 흐름 / DB 시드 / 대기 패턴 다시 추측
- **TV (test value)** 가 LLM 추측 → "이미 가입된 이메일" 시나리오인데 DB 에 없는 email 생성 = 시나리오 의도 미달
- **결정성 없음** — same code 다른 메타데이터 → flaky test
- **캐시 없음** — SHA 무관하게 매번 재생성

### After (2026-06-09 토론 후)

```
┌─────────────────────────────────────────────────────────────┐
│ Scan Layer (본 데이터 layer)                                       │
│ ┌─────────────────────────────────────────────────────────┐│
│ │ 기존 codebase-index (5 kind + frontend) [그대로 유지]   ││
│ └─────────────────────────────────────────────────────────┘│
│ ┌─────────────────────────────────────────────────────────┐│
│ │ 신규 metadata_indices (kind/sub_kind 2축)               ││
│ │   - frontend.selectors   (Vue/React data-testid 카탈로그)││
│ │   - frontend.routes      (Vue Router/React Router)       ││
│ │   - backend.schemas      (Pydantic/SQLAlchemy field)     ││
│ │   - sut_tests.patterns   (기존 e2e/spec 의 패턴)         ││
│ └─────────────────────────────────────────────────────────┘│
│ ┌─────────────────────────────────────────────────────────┐│
│ │ 신규 source/ (코드베이스 원본)                       ││
│ │   - S3 path: services/{service_id}/source/{sha}/{path}   ││
│ │   - TTL 30일                                              ││
│ └─────────────────────────────────────────────────────────┘│
└──────┬───────────────────────────────────────────────────────┘
       │  load_metadata_index() / load_source() / DBTool snapshot
       │  (조회 헬퍼 - 본 데이터 layer)
       ▼
┌─────────────────────────────────────────────────────────────┐
│ TC/TV Generator Agent (유빈 영역)                            │
│   PRD → TS (PRD 단독)                                        │
│   TS + 정책서/약관/API 문서 → TC (G/W/T 자연어)              │
│   TC + 코드베이스/메타데이터/DB → TV (별도 entity, pool)     │
└────────────────────┬────────────────────────────────────────┘
                     │  TVValidator.validate() ← 헬퍼
                     ▼
                재시도 (best-of-N, max 2회)
```

**해결**:
- 격차 A-1: 메타데이터로 명세 1차 우선화 가능
- 격차 A-2: AST 추출 = 도메인 무관
- 격차 A-3: TV pool 로 런타임 상태 전달
- 격차 B-3: `(commit_sha, file_path)` 키 cache 로 결정성
- 격차 B-4: LLM seed 고정 + AST/LLM 분리 + confidence 명시

---

## 1. 회의 결정사항 (2026-06-09)

### 결정 1: TV 데이터 모델 = **별도 entity** (옵션 B)
```
TC: {tc_id, given, when, then, tags, tv_refs: ["TV-001", "TV-002"]}
TV: {tv_id, field, value, source, confidence, ref_count, valid_until, validator_passed}
```

**왜 이렇게**:
- 격차 A-3 직접 해결 (TC-01 의 order_id 가 TV pool 에 들어가 TC-02 가 참조)
- 격차 B-4 결정성 (TV 의 SHA/valid_until 추적)
- 격차 B-3 캐시의 자연스러운 단위
- TV 만 따로 refresh 가능 (DB 변경 시)

### 결정 2: 메타데이터 schema = **4 영역 + 결정성 + confidence + AST 기본 + LLM 보강**

| 영역 | 추출 방법 | confidence | LLM 필요? |
|---|---|---|---|
| selectors | AST 정적 (Vue/React `data-testid` 추출) | 1.0 | ❌ |
| routes | AST 정적 (Vue Router/React Router config) | 1.0 | ❌ |
| schemas | AST 정적 (Pydantic/SQLAlchemy field) | 1.0 | ❌ |
| test_patterns | AST 추출 + **LLM 분류** | 0.85 | ✅ "auth-setup/db-seed/page-object" 분류 |
| 동적 element | AST + **LLM 의미 라벨** | 0.65 | ✅ "v-if=isMinor 의 비즈니스 의미" |

**결정성 보장**:
- 추출 cache 키 = `(commit_sha, file_path)`
- LLM 호출 시 `seed` 고정 (예: `service_id` 의 hash)
- AST 추출 결과는 deterministic by definition

### 결정 3: 코드베이스 자체 저장 = **PoC full S3 dump + TTL 30일**

```
S3 path: services/{service_id}/source/{commit_sha}/{relative_path}
예: services/9f2a7d4f-.../source/76ff013448.../mini-bss-lite/backend/app/main.py
```

- PoC = `git clone --depth 1` → S3 PUT → 즉시 폐기 (workspace)
- repeat scan = 같은 SHA 시 skip (이미 저장됨 → list_objects 확인)
- TTL = S3 lifecycle policy 30일 무접근 시 삭제
- **IAM 분리 = 폐기** (PoC 단계 path 격리만으로 충분, production SaaS 시점 별도)
- 비용 ↑ 시 대체 = GitHub MCP on-demand (단 PoC 단계에선 결과물 퀄리티 우선)

### 결정 4: 메타데이터 저장 = **별도 `metadata_indices` 테이블** (codebase_indices 와 분리)

```sql
metadata_indices:
  id, service_id, commit_hash, kind, sub_kind, s3_key, bytes, sha256,
  file_count, confidence, extracted_from, scanned_at
```

**왜 분리**:
- `codebase_indices` = SUT 구조 추출 (불변 fact, AST 결과)
- `metadata_indices` = TC/TV 생성 보조 (LLM 친화 schema 변환)
- 개념적 분리 + sub_kind 자유 확장
- 기존 PR #240 mirror fallback 패턴 그대로 재사용

### 결정 5: DBTool TV 검증 = **snapshot 활용 (PoC) + 큰 SUT 시점 협업 발의**

- PoC = 기존 `/db/snapshot?table=customers` 활용
- cache layer (TV 생성 중 재사용) — `db_state_cache: {(service_id, table) → snapshot}`
- TVValidator helper = 본 데이터 layer utility, 호출자 = 유빈 agent
- 큰 SUT (10만 row+) 사용 시점 = `/db/query` endpoint E 영역 (kshyun) 협업

---

## 2. 컴포넌트 연결부

### 본 데이터 layer → 유빈 영역 (인터페이스)

```python
# 제공 (qapilot/shared/scan_storage.py 가칭)
def load_metadata_index(
    service_id: str,
    kind: str,           # "frontend" | "backend" | "sut_tests"
    sub_kind: str,       # "selectors" | "routes" | "schemas" | "patterns"
    commit_hash: str | None = None,
) -> dict: ...

def load_source(
    service_id: str,
    sha: str,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> str: ...

# 제공 (qapilot/shared/db_state.py 가칭)
async def get_db_snapshot_cached(
    service_id: str,
    table: str,
    ttl_seconds: int = 60,
) -> list[dict]: ...

# 제공 (qapilot/shared/tv_validator.py)
class TVValidator:
    async def validate(
        self,
        tv_field: dict,        # {name, value, source}
        scenario_intent: dict, # tc.given/when/then/tags
        db_snapshot: list[dict],
        schemas: dict,         # metadata_indices 의 schema kind
    ) -> ValidationResult: ...
```

```python
# 유빈이 호출 (qapilot/agents/tc_generator/agent.py 가칭)
async def _execute(self, ...):
    selectors = load_metadata_index(service_id, "frontend", "selectors")
    schemas = load_metadata_index(service_id, "backend", "schemas")
    db = await get_db_snapshot_cached(service_id, "customers")
    
    for attempt in range(3):
        tv = await llm.generate_tv(tc, selectors, schemas, db)
        result = await self.tv_validator.validate(tv, tc.intent, db, schemas)
        if result.valid: break
```

### 본 데이터 layer → 기존 PR #240 패턴 재사용 (mirror fallback)

신규 헬퍼들은 2026-06-07 에 만든 `_load_codebase_index_from_db_mirror()` 패턴 그대로:
1. 디스크 cache 우선
2. S3 mirror fallback
3. service_id 인자로 멀티 테넌트 안전

---

## 3. 파일 구조 (본 디렉토리)

```
docs/scan-enhancement/
├── README.md                       # 본 문서 (overview + before/after + 결정사항)
├── metadata-schema-spec.md         # 4 영역 schema 상세
├── s3-path-spec.md                 # S3 path/SHA/TTL 규약 + FAQ (RDB/그래프/depth 1)
├── poc2-vue-sfc-parser.md          # PoC 2 — Vue SFC AST 추출 (9/9, 100%)  ✅
├── poc3-metadata-writer.md         # PoC 3 — DB+S3 mirror writer + cache skip  ✅
├── poc4-scan-storage.md            # PoC 4 — load_metadata_index + load_source + LRU  ✅
├── poc5-pytest-ast-parser.md       # PoC 5 — pytest AST 추출 (sut_tests.patterns)  ✅
├── poc6-backend-schemas-and-vue-routes.md  # PoC 6 — backend.schemas + frontend.routes  ✅
├── poc7-8-validator-and-db-cache.md  # PoC 7+8 — TVValidator + DBTool TTL cache  ✅
├── poc9-10-orchestrator.md         # PoC 9+10 — source dumper + 4영역 통합 orchestrator  ✅
├── file-inventory.md               # 구현한 모든 files 분류 (extractor/writer/reader/shared/...)
├── verification.md                 # 회의 결론 ↔ 구현 역추적 검증 + 미흡 영역 명시
├── before-after.md                 # Before/After (툴/아키텍처/퀄리티 3 측면)
├── deep-verification.md            # 심층 의미적 역검증 (외부 reviewer 시각)
└── migrations/
    └── 001_metadata_indices.sql    # DDL

qapilot/
├── shared/
│   ├── metadata_schemas.py         # Pydantic model 4 영역  ✅ PoC 1
│   ├── scan_storage.py             # ✅ PoC 4 신설 (load_metadata_index + load_source + LRU)
│   ├── tv_validator.py             # ✅ PoC 7 신설 (schema/format/DB 검증)
│   └── db_state.py                 # ✅ PoC 8 신설 (DBTool snapshot + TTL cache)
├── scan/                           # ✅ PoC 2 신설
│   ├── source_dumper.py            # ✅ PoC 9   — git clone + S3 source/ dump helper
│   ├── orchestrator.py             # ✅ PoC 10  — scan_all_metadata (4영역 통합 한 호출)
│   └── extractors/
│       ├── vue_sfc_parser.py       # ✅ PoC 2   — Vue SFC → frontend.selectors (Input/Button/...)
│       ├── vue_router_parser.py    # ✅ PoC 6.B — Vue Router → frontend.routes
│       ├── backend_schema_parser.py # ✅ PoC 6.A — Pydantic/SQLAlchemy → backend.schemas
│       ├── pytest_ast_parser.py    # ✅ PoC 5   — pytest → sut_tests.patterns (fixture/auth-setup/mock/unknown)
│       └── llm_pattern_classifier.py  # ✅ PoC 5.1 — unknown → db-seed/cleanup/... (confidence 0.85)
├── node-bridge/                    # ✅ PoC 2 신설 (Node.js subprocess)
│   ├── package.json                # @vue/compiler-sfc 의존성
│   ├── parse_vue_sfc.js            # SFC parser script
│   └── .gitignore                  # node_modules 제외
├── storage/
│   └── s3_client.py                # head_object 추가  ✅ PoC 3
└── db/
    ├── metadata_writer.py          # ✅ PoC 3 신설 (upsert_metadata_index, upsert_source_file)
    └── metadata_reader.py          # ✅ PoC 4 신설 (load_metadata_index_raw + get_latest_commit_hash)
```

PoC 1 = README + spec + Pydantic + DDL. PoC 2 = Vue SFC parser (frontend.selectors). PoC 3 부터 writer/reader.

---

## 4. 본 데이터 layer 작업 트랙 + PoC 단위

### 트랙 1 — 코드 스캔 고도화
- (1.1) 테스트 코드 스캔 — `tests/e2e/`, `tests/`, `spec/` 추출 → `sut_tests.patterns`
- (1.2) 프론트 셀렉터/라우터/스키마 스캔 → `frontend.selectors`, `frontend.routes`, `backend.schemas`

### 트랙 2 — 메타데이터 저장 + 시나리오 agent 활용
- (2.1) 코드베이스 전체 S3 dump (PoC) — `git clone --depth 1` + PUT + 폐기
- (2.2) `metadata_indices` 테이블 + S3 namespace
- (2.3) 조회 헬퍼 (`load_metadata_index()` + `load_source()`)

### 트랙 3 — DB 스캔 툴 활용 연결
- (3.1) 기존 DBTool snapshot 활용 + cache layer
- (3.2) `TVValidator` helper (utility, 유빈 호출)
- (3.3) 큰 SUT 시점 `/db/query` E 영역 협업 발의

### PoC 단위 (작은 단위 commit)

| PoC | 산출물 | 상태 |
|---|---|---|
| **PoC 1** | docs + Pydantic model + DDL | ✅ |
| **PoC 2** | Vue SFC AST 추출 (frontend.selectors) | ✅ |
| **PoC 3** | S3 writer (`upsert_metadata_index`) + cache skip | ✅ |
| **PoC 4** | 조회 헬퍼 (`load_metadata_index` + `load_source`) | ✅ |
| **PoC 5** | pytest AST 추출 (`sut_tests.patterns` 첫 framework) | ✅ |
| **PoC 5.1** | LLM 의미 분류 (unknown → db-seed/cleanup/...) | ✅ |
| **PoC 6.A** | `backend.schemas` 추출 (Pydantic BaseModel + SQLAlchemy Mapped/Column) | ✅ |
| **PoC 6.B** | `frontend.routes` 추출 (Vue Router createRouter routes) | ✅ |
| **PoC 7** | TVValidator helper (schema/format/DB 존재성 검증) | ✅ |
| **PoC 8** | DBTool snapshot + TTL cache (TVValidator 의 DB 입력 helper) | ✅ |
| **PoC 9** | git clone --depth 1 + source/ S3 dump helper | ✅ |
| **PoC 10** | scan_all_metadata 4영역 통합 orchestrator | ✅ |

#### 4영역 완성

- ✅ `frontend.selectors` (PoC 2)
- ✅ `frontend.routes` (PoC 6.B)
- ✅ `backend.schemas` (PoC 6.A)
- ✅ `sut_tests.patterns` (PoC 5 + 5.1)

#### Framework 확장 (추후)

다른 framework 추출기는 후속 단위로 추가 가능:
- React JSX (`react_jsx_parser.py` — @babel/parser subprocess, vue-bridge 패턴 동형)
- React Router (`react_router_parser.py` — vue_router_parser 의 80% 재사용)
- Playwright / Cypress / Jest / Vitest (각각 tree-sitter-typescript/javascript)
- Java JPA / Spring `@RestController` (tree-sitter-java)

---

## 5. 격차 매핑 (토론 8 격차 → 본 작업 단위)

| 격차 | 작업 단위 | PR 또는 PoC |
|---|---|---|
| A-1 오라클 | 메타데이터 → 명세 1차 우선화 가능하게 함 (단 명세 우선화 자체는 유빈 agent) | PoC 5 (sut_tests 추가) |
| A-2 도메인 hardcoded | AST 추출로 도메인 무관 | PoC 2~5 |
| A-3 런타임 상태 전달 | TV pool entity | (유빈 agent + TV pool 저장) |
| B-3 캐시 미구현 | `(sha, path)` cache + S3 SHA 키 | PoC 3 |
| B-4 결정성 | LLM seed 고정 + AST 결정성 | PoC 2, 5 |

---

## 6. 메모리 메모 (다음 세션 진입 시)

본 디렉토리 + `memory/project_qapilot_scan_enhancement_foundation.md` 함께 참조.

- 회의 결정사항 변경 시 본 README 의 "1. 회의 결정사항" 섹션 갱신
- 새 PoC 단위 진행 시 "4. 작업 트랙 + PoC 단위" 의 해당 PoC 를 done 표기
- 격차 새로 발견 시 "5. 격차 매핑" 에 추가
