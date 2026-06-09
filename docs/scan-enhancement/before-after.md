# Before / After — 본인 영역 구현 전 vs 후

> 본인 코드스캔 고도화 이전 vs 이후의 차이를 객관적으로 정리.
> 3 측면 — 툴/에이전트, 아키텍처 (코드 위치/구조), 퀄리티 (결함 → 개선).

---

## 1. 툴/에이전트 측면

### 1.1 Before — 본인 작업 전 (2026-06-07 시점)

```text
[ScanTool — qapilot/tools/codebase_scanner_tool.py]
    │ extract: endpoints / models / functions / callgraph / manifest (5 kind)
    │ (test_*.py / *.spec.ts 는 의도적 제외 — _EXCLUDE_PATTERNS)
    ▼
[FrontendDomScanner — qapilot/tools/frontend_dom_scanner.py]
    │ regex 휴리스틱 (tree-sitter-vue Python binding 부재)
    │ 정확도: mini-bss Signup.vue 7/9 (78%) — guardian-consent 누락
    ▼
[codebase_indices DB + S3 (C 영역)]
    │ kind 1축: endpoints/models/functions/callgraph/manifest/frontend
    │
    ▼
[ScenarioGenerator agent — qapilot/agents/scenario_generator/]
    │ 단일 LLM 호출: PRD + 정책서 + 약관 + API + codebase-index
    │ → TS + TC + TC.values (한 번에 모두)
    │ 격차 A-1: 코드 동작 = 정답으로 박제 (오라클)
    │ 격차 A-2: BSS 한국어 키워드 hardcoded
    │ 격차 A-3: TC-01 의 order_id → TC-02 에 못 전달
    │ 격차 B-3: cache_module.py NotImplementedError
    │ 격차 B-4: LLM seed 없음
    ▼
[ActionMapper / CodeGenerator / UITestTool / cross_check]
   (본인 누적 14 PR #232~#259 의 L2 fix 영역)
```

### 1.2 After — 본인 작업 후 (2026-06-09)

```text
[기존 ScanTool / FrontendDomScanner — 변경 X, codebase_indices 그대로 유지]
                              │
                              ▼ (별개 layer)
[본인 신규 데이터 layer — qapilot/scan/ + qapilot/shared/ + qapilot/db/]

  ① 추출 (5 extractor — qapilot/scan/extractors/)
     ├─ vue_sfc_parser           → frontend.selectors  (Vue SFC AST, 100% 정확)
     ├─ vue_router_parser        → frontend.routes     (Vue Router JS/TS AST)
     ├─ backend_schema_parser    → backend.schemas     (Pydantic + SQLAlchemy AST)
     ├─ pytest_ast_parser        → sut_tests.patterns  (pytest AST)
     └─ llm_pattern_classifier   → sut_tests.patterns  (unknown 의미 분류, confidence 0.85)
                              │
                              ▼
  ② 저장 (DB + S3 mirror — qapilot/db/metadata_writer.py)
     └─ upsert_metadata_index() + upsert_source_file()
        - PostgreSQL `metadata_indices` (별도 namespace)
        - S3 `services/{sid}/metadata-index/{sha}/{kind}-{sub_kind}.json`
        - head_object 기반 cache skip (재스캔 시 PUT 회피)
                              │
                              ▼
  ③ 조회 (qapilot/shared/scan_storage.py)
     ├─ load_metadata_index(sid, kind, sub_kind, commit=None)
     │     - commit=None → 최신 scanned_at 자동 해결
     │     - process-local LRU (maxsize 64)
     └─ load_source(sid, sha, path, line_start, line_end)
           - line range 슬라이싱 (token 절감, 같은 파일 다른 range = S3 GET 1회)
                              │
                              ▼
  ④ 검증 (qapilot/shared/tv_validator.py)
     └─ TVValidator.validate(tv_field, intent, db_snapshot, schemas, schema_name)
           - schema 일치 + type 매칭 + format (email/iso_date/regex/...)
           - DB 존재성/부재성 (scenario_intent.expects_*)
           - ValidationResult.reasons (한국어, LLM 재시도 prompt 입력)
                              │
                              ▼
  ⑤ DB cache (qapilot/shared/db_state.py)
     └─ get_db_snapshot_cached(sid, table, ttl_seconds=60)
           - 기존 DBTestTool wrap + per-process TTL cache
                              │
                              ▼
[유빈 TC/TV Generator agent (별도 구현, 본인 영역 외)]
   │ PRD → TS (단독)
   │ TS + 정책서/약관/API → TC (자연어 G/W/T)
   │ TC + 본인 4 public API → V (재시도 + TVValidator)
   │
   ▼
[기존 ActionMapper 이후 흐름 — 변경 X, 회의: 액션 매핑 이전까지 구현]
```

### 1.3 핵심 차이

| 항목 | Before | After |
|---|---|---|
| **TC 생성** | 1 LLM 호출 (PRD+정책+코드 → TS+TC+TV) | 4 단계 분리 (PRD→TS, TS+문서→TC, TC+코드→TV+검증) |
| **TV 신뢰** | LLM 추측 | schema/format/DB 검증 + 재시도 |
| **frontend 추출** | regex 휴리스틱 78% | AST 100% (Vue SFC) + Vue Router |
| **backend schema** | (codebase-index endpoints/models 만) | 신규 Pydantic + SQLAlchemy 추출 (LLM 친화 schema) |
| **test 코드** | 제외 (`_EXCLUDE_PATTERNS`) | 신규 추출 (pytest + LLM 의미 분류) |
| **cache** | NotImplementedError | 4 layer (head_object skip, LRU metadata/source, TTL DB) |
| **결정성** | 없음 | sort_keys JSON + AST 결정성 + LRU 고정 |

---

## 2. 아키텍처 측면 (코드 위치 / 구조)

### 2.1 디렉토리 구조 변경

```text
qapilot/
├── tools/                          # 기존 — C/E 영역, 변경 X
│   ├── codebase_scanner_tool.py    # endpoints/models/functions/... (그대로)
│   ├── frontend_dom_scanner.py     # regex 휴리스틱 (그대로 — 본인 신규 vue_sfc_parser 와 병존)
│   ├── db_test_tool.py             # DBTool (그대로 — 본인 db_state.py 가 wrap)
│   └── ...
│
├── scan/                           # ✅ 신규 — 본인 영역
│   ├── __init__.py
│   └── extractors/
│       ├── __init__.py
│       ├── vue_sfc_parser.py
│       ├── vue_router_parser.py
│       ├── backend_schema_parser.py
│       ├── pytest_ast_parser.py
│       └── llm_pattern_classifier.py
│
├── node-bridge/                    # ✅ 신규 — Node subprocess (Vue 공식 parser)
│   ├── package.json
│   ├── parse_vue_sfc.js
│   └── .gitignore
│
├── db/
│   ├── code_writer.py              # 기존 (C 영역, codebase_indices 처리)
│   ├── code_reader.py              # 기존
│   ├── metadata_writer.py          # ✅ 신규 — 본인 영역 (metadata_indices)
│   └── metadata_reader.py          # ✅ 신규 — 본인 영역
│
├── shared/
│   ├── metadata_schemas.py         # ✅ 신규 — Pydantic 4영역 model
│   ├── scan_storage.py             # ✅ 신규 — 통합 조회 API + LRU
│   ├── tv_validator.py             # ✅ 신규 — TV 검증 helper
│   ├── db_state.py                 # ✅ 신규 — DB snapshot TTL cache
│   ├── llm_client.py               # 기존
│   └── ...
│
└── storage/
    └── s3_client.py                # 본인이 head_object() 함수만 추가 (+21줄)
```

### 2.2 namespace 분리 (회의 결정 4 충실)

| 테이블 | 책임 | 담당 |
|---|---|---|
| `codebase_indices` | SUT 구조 추출 (불변 fact, AST) — endpoints/models/functions/callgraph/manifest/frontend | C 영역 (기존) |
| `metadata_indices` | TC/TV 생성 보조 (LLM 친화 schema 변환) — selectors/routes/schemas/patterns | 본인 영역 (신규) |

→ 한 service 에 두 테이블 row 공존, 충돌 X. UNIQUE 제약도 별도.

### 2.3 S3 path 통합 (기존 + 신규)

```text
qapilot-local/services/{service_id}/
├── codebase-index/{sha}/          # 기존 C 영역 (변경 X)
├── metadata-index/{sha}/          # ✅ 신규 — 본인 영역
│   ├── frontend-selectors.json
│   ├── frontend-routes.json
│   ├── backend-schemas.json
│   └── sut_tests-patterns.json
├── source/{sha}/{relative_path}   # ✅ 신규 — 본인 영역 (코드베이스 원본)
├── generated-code/{tc_id}/        # 기존 (변경 X)
├── domain/                        # 기존 (변경 X)
└── results/                       # 기존 (변경 X)
```

### 2.4 패턴 차용 (기존 → 본인)

본인이 기존 검증된 패턴 그대로 차용 (재발명 없음):

| 본인 PoC | 차용 출처 | 차용 패턴 |
|---|---|---|
| PoC 3 upsert | PR #240 `upsert_codebase_index` | DB INSERT + S3 PUT graceful |
| PoC 3 mirror fallback | PR #240 `_load_codebase_index_from_db_mirror` | s3_key 컬럼 + S3 GET fallback |
| PoC 4 reader | `code_reader.load_codebase_index` | DB SELECT → S3 GET → JSON parse |
| PoC 8 wrap | `db_test_tool._get_snapshot` | async DB 호출 + graceful None |
| PoC 5 AST | `codebase_scanner_tool` tree-sitter Parser singleton | thread-safe lazy init |

---

## 3. 퀄리티 측면 (결함 → 개선)

### 3.1 8 격차 (회의 2026-06-09 도출) ↔ 본인 PoC 기여

| 격차 | Before 결함 | 본인 PoC 의 기여 |
|---|---|---|
| **A-1** 오라클 | LLM 이 코드 동작 = 정답 박제 → 버그도 PASS | PoC 7 TVValidator + PoC 4 load_source 로 명세 + 코드 양쪽 LLM 주입 |
| **A-2** 도메인 hardcoded | `_ROUTER_KEYWORDS` 등 BSS 한국어 키워드 | 본인 추출기 5개 = AST/표준 규칙 (도메인 키워드 0) |
| **A-3** 런타임 상태 전달 | TC-01 의 `order_id` → TC-02 못 받음 | PoC 7+8 의 DB 존재성 검증 + TV pool (유빈 영역 구현 시) |
| **B-1** 격리 없음 | 단일 page 6 TC 재사용 | (L2 격차 — 본인 #246/247 영역, PoC 영역 외) |
| **B-2** skip 남발 | `test.skip()` 묵시 PASS | (본인 #234 와 동형, PoC 영역 외) |
| **B-3** 캐시 미구현 | `cache_module.py` NotImplementedError | PoC 3 head_object + PoC 4 LRU + PoC 8 TTL (3 layer cache) |
| **B-4** 결정성 | LLM seed 없음 + dict 순서 의존 | sort_keys=True (PoC 3) + AST 결정성 + LRU 고정 + LLMClient temperature=0 |
| **B-5** 커버리지 피상 | req_id 1건이면 100% | (유빈 영역) |

### 3.2 Before 의 구체적 결함 sample (mini-bss Signup.vue)

| testid | regex 휴리스틱 (Before) | AST (After PoC 2) |
|---|---|---|
| name | ✅ | ✅ |
| email | ✅ | ✅ |
| password | ✅ | ✅ |
| birth_date | ✅ | ✅ |
| **guardian-consent-field** | ❌ (v-if 의 self-closing 검출 실패) | ✅ OutputElement |
| **guardian-consent** | ❌ (부모 v-if 추적 불가) | ✅ DynamicElement (parent_v_if="isMinor") |
| signup-error | ✅ | ✅ |
| signup-success-toast | ✅ | ✅ |
| signup-submit | ✅ | ✅ |
| **정확도** | **78%** | **100%** |

추가로 PoC 2 는 `disabled_when.expr='loading || (isMinor && !form.guardian_consent)'` 같은 정보도 보존 (Before 의 regex 는 이런 disabled 표현식 추출 불가).

### 3.3 결정성 보장 (B-4 격차) — 본인 5중 안전 장치

| 위치 | 결정성 |
|---|---|
| AST 추출 자체 | tree-sitter / @vue/compiler-sfc = same input → same AST |
| JSON 직렬화 (PoC 3) | `json.dumps(..., sort_keys=True, ensure_ascii=False, indent=2)` → 같은 model → 같은 sha256 |
| LRU 캐시 (PoC 4) | `(sid, kind, sub_kind, commit)` 키 — process restart 시 자동 비움 (안전) |
| LLM 호출 (PoC 5.1) | LLMClient `temperature=0.0` 기본 |
| DB cache (PoC 8) | TTL 명시 + service_id 격리 |

### 3.4 cache 비용 시뮬레이션 (PoC 3 docs §1 +)

mini-bss 한 service 등록 시:
- metadata-index: 4 PUT × 5KB ≈ 20KB
- source: 150 file PUT ≈ $0.00075 (AWS S3 가격)
- 재스캔 시 (같은 sha): metadata head_object 4회 + source head_object 150회 ≈ $0.00006

→ 1000 service 누적 = $0.75 (PoC 단계 무시 가능).

---

## 4. 운영 측면 영향

### 4.1 배포 영향

| 변경 | 영향 |
|---|---|
| `metadata_indices` DDL 적용 | 1회 (`docs/scan-enhancement/migrations/001_metadata_indices.sql`) |
| Node.js 의존 (PoC 2) | 현재 dev 환경 이미 Node 가용 (frontend build 의존). Docker 화 시점에 base image 에 추가 (~80MB) |
| S3 bucket lifecycle | 운영 시점에 `source/` prefix 30일 정책 추가 (docs 명시) |
| 환경변수 변경 | 0 (`S3_*` + `DATABASE_URL` + `QAPILOT_SUT_DB_URL` 모두 기존) |

### 4.2 backward compatibility

- 기존 `codebase_indices` / `code_reader` / `code_writer` / `frontend_dom_scanner` 변경 0 — 본인 코드가 병존, 충돌 X
- 기존 단위 테스트 영향 0 — 본인이 새 모듈만 추가
- 기존 agent (ScenarioGenerator/CodeGenerator/ActionMapper) 동작 변경 0 — 유빈 agent 가 합류해야 본인 layer 활용 시작

### 4.3 운영 측 추후 작업 (본인 영역 wiring)

1. **service register flow** — git clone → 본인 4 추출기 → 4 영역 upsert (PoC 9/10)
2. **S3 lifecycle policy** — `services/*/source/*` 30일 자동 삭제
3. **alembic 통합** — 현재 raw SQL → alembic revision 등록

---

## 5. 객관 정리

| 측면 | Before | After |
|---|---|---|
| 본인 영역 새 모듈 수 | 0 | 14 (extractor 5 + writer/reader 2 + shared 4 + node-bridge 3) |
| 새 DB 테이블 | 0 | 1 (`metadata_indices`) |
| 새 S3 prefix | 0 | 2 (`metadata-index/`, `source/`) |
| 단위 테스트 | (기존) | +204 (전부 PASS) |
| docs 페이지 | 0 (본인 영역) | 10 (`docs/scan-enhancement/`) |
| 기존 코드 수정 | — | 1 파일 1 함수 (`s3_client.head_object`) |
| 격차 직접 해결 | — | A-1 (간접) / A-2 (직접) / B-3 (직접) / B-4 (직접) |
| 격차 부분 기여 | — | A-3 (TV pool 데이터 layer 제공) |
| 미흡 영역 | — | git clone orchestrator + service register caller (PoC 9/10 후속) |
