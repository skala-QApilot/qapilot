# 파일 인벤토리 — 본인 영역 신규/수정 (브랜치 `feat/me/scan-enhancement-foundation`)

> 총 **40 files changed, +7496 insertions** (develop 기준 diff).

---

## 1. 문서 (`docs/scan-enhancement/`) — 신규 10 파일

| 파일 | 용도 | PoC |
|---|---|---|
| `README.md` | Overview + before/after + 회의 결정사항 + 작업 트랙 + 파일 구조 | 모든 PoC |
| `metadata-schema-spec.md` | 4 영역 schema 상세 + 추출 방법/confidence 매트릭스 | 1 |
| `s3-path-spec.md` | S3 path/SHA/TTL/Lifecycle + Q1~Q3 (RDB/그래프/depth 1) | 1 + 보강 |
| `migrations/001_metadata_indices.sql` | DDL — metadata_indices 테이블 | 1 |
| `poc2-vue-sfc-parser.md` | Vue SFC AST 추출 (frontend.selectors) | 2 |
| `poc3-metadata-writer.md` | DB+S3 mirror writer + cache skip | 3 |
| `poc4-scan-storage.md` | 조회 헬퍼 + LRU | 4 |
| `poc5-pytest-ast-parser.md` | pytest AST 추출 (sut_tests.patterns) | 5 + 5.1 |
| `poc6-backend-schemas-and-vue-routes.md` | Pydantic/SQLAlchemy + Vue Router 추출 | 6.A + 6.B |
| `poc7-8-validator-and-db-cache.md` | TVValidator + DBTool TTL cache | 7 + 8 |
| `poc9-10-orchestrator.md` | source dumper + 4영역 통합 orchestrator | 9 + 10 |
| `file-inventory.md` | 본 인벤토리 (이 문서) | 종합 |
| `verification.md` | 회의 결론 ↔ 본인 구현 역추적 | 종합 |
| `before-after.md` | Before/After 3 측면 | 종합 |

---

## 2. 추출기 (`qapilot/scan/extractors/`) — 신규 5 모듈

| 파일 | kind.sub_kind | framework | 도구 | PoC |
|---|---|---|---|---|
| `vue_sfc_parser.py` | `frontend.selectors` | Vue SFC | @vue/compiler-sfc subprocess | 2 |
| `vue_router_parser.py` | `frontend.routes` | Vue Router (JS/TS) | tree-sitter-javascript/typescript | 6.B |
| `backend_schema_parser.py` | `backend.schemas` | Pydantic + SQLAlchemy | tree-sitter-python | 6.A |
| `pytest_ast_parser.py` | `sut_tests.patterns` | pytest | tree-sitter-python | 5 |
| `llm_pattern_classifier.py` | (pytest 출력 보강) | LLM | LLMClient (LangChain ChatOpenAI) | 5.1 |

## 2.1 Scan layer (`qapilot/scan/`) — 추가 2 모듈

| 파일 | 함수 | PoC |
|---|---|---|
| `source_dumper.py` | `clone_repo_shallow`, `dump_source_to_s3`, `iter_source_files`, `_mask_url` | 9 |
| `orchestrator.py` | `scan_all_metadata`, `_build_frontend_selectors`, `_build_frontend_routes`, `_build_backend_schemas`, `_build_sut_tests_patterns`, `_infer_route_from_path` | 10 |

`__init__.py` 2개 (`qapilot/scan/__init__.py` + `qapilot/scan/extractors/__init__.py`).

---

## 3. Node bridge (`qapilot/node-bridge/`) — 신규 3 파일

| 파일 | 용도 |
|---|---|
| `package.json` | `@vue/compiler-sfc ^3.5.13` 의존성 |
| `parse_vue_sfc.js` | Vue SFC AST → JSON stdout (Python subprocess 호출) |
| `.gitignore` | `node_modules/`, `package-lock.json` 제외 |

`npm install` 1회 — `qapilot/node-bridge/node_modules/` 생성 (11MB, gitignore).

---

## 4. DB writer/reader (`qapilot/db/`) — 신규 2 모듈

| 파일 | 함수 | PoC |
|---|---|---|
| `metadata_writer.py` | `upsert_metadata_index`, `upsert_source_file`, `_build_s3_key`, `_to_json_bytes` | 3 |
| `metadata_reader.py` | `load_metadata_index_raw`, `get_latest_commit_hash` | 4 |

---

## 5. Shared utility (`qapilot/shared/`) — 신규 4 모듈

| 파일 | Public API | PoC |
|---|---|---|
| `metadata_schemas.py` | Pydantic 4영역 (FrontendSelectorsIndex/FrontendRoutesIndex/BackendSchemasIndex/SutTestsPatternsIndex) + sub-model 다수 + `KIND_SUB_KIND_TO_MODEL`, `get_model_for` | 1 |
| `scan_storage.py` | `load_metadata_index`, `load_source`, `clear_cache`, `cache_info` | 4 |
| `tv_validator.py` | `TVValidator.validate`, `ValidationResult`, `ValidationCheck` | 7 |
| `db_state.py` | `list_db_tables_cached`, `get_db_snapshot_cached`, `clear_db_cache`, `cache_stats` | 8 |

---

## 6. 기존 파일 수정 — 1 파일

| 파일 | 변경 | 사유 |
|---|---|---|
| `qapilot/storage/s3_client.py` | `head_object()` 함수 추가 (+21 줄) | PoC 3 cache skip 정책의 핵심 헬퍼 |

기존 함수 (`put_bytes`, `get_object`, `put_file`, `download`) 는 그대로 보존.

---

## 7. 테스트 (`tests/`) — 신규 9 파일 + 4 fixture

### 7.1 test 모듈 (9)

| 파일 | 케이스 수 | 대상 |
|---|---|---|
| `test_vue_sfc_parser.py` | 16 | PoC 2 |
| `test_metadata_writer.py` | 14 | PoC 3 |
| `test_scan_storage.py` | 18 | PoC 4 |
| `test_pytest_ast_parser.py` | 22 | PoC 5 |
| `test_llm_pattern_classifier.py` | 16 | PoC 5.1 |
| `test_backend_schema_parser.py` | 25 | PoC 6.A |
| `test_vue_router_parser.py` | 25 | PoC 6.B |
| `test_tv_validator.py` | 50 | PoC 7 |
| `test_db_state.py` | 18 | PoC 8 |
| `test_source_dumper.py` | 12 | PoC 9 |
| `test_scan_orchestrator.py` | 15 | PoC 10 |
| **합계** | **231 PASS** | |

### 7.2 fixture 디렉토리 (4 파일)

| 파일 | 용도 |
|---|---|
| `tests/fixtures/vue/signup_minimal.vue` | PoC 2 검증 (6 testid + parent v-if + disabled_when) |
| `tests/fixtures/pytest/sample_auth_test.py` | PoC 5 검증 (2 fixture + 4 test) |
| `tests/fixtures/python/sample_schemas.py` | PoC 6.A 검증 (2 Pydantic + 2 SQLAlchemy) |
| `tests/fixtures/js/sample_router.js` | PoC 6.B 검증 (4 routes — guards/redirect/params) |

mock 격리 — 실 PostgreSQL/MinIO/Node/LLM 의존 없이 단위 검증 가능.

---

## 8. 외부 의존성 (`pyproject.toml`) — 본인이 추가한 것 X

본인이 기존 의존성만 사용:
- `tree-sitter`, `tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript` (이미 있음 — `codebase_scanner_tool.py` 에서 사용 중)
- `pydantic` v2 (이미 있음)
- `psycopg` pool (이미 있음 — `db.connection.get_pool`)
- `boto3` (이미 있음 — `storage.s3_client`)
- `langchain-openai` ChatOpenAI (이미 있음 — PoC 5.1 의 LLM)

Node 의존성 1개 추가: **`@vue/compiler-sfc`** (`qapilot/node-bridge/package.json` 안. Python `pyproject.toml` 영향 0).

---

## 9. PoC ↔ commit 매핑

| PoC | commit | 파일 수 (해당 commit) |
|---|---|---|
| 1 | `e22222e` | docs 4 + Pydantic 1 + DDL 1 = 6 |
| 2 | `6c3c5b1` | extractor 1 + node-bridge 3 + scan __init__ 2 + test 1 + fixture 1 + docs 1 + README +갱신 = 10 |
| 3 | `dcd6bb3` | db_writer 1 + s3_client +head + test 1 + docs 1 + README = 6 |
| 4 | `808e6cd` | db_reader 1 + scan_storage 1 + test 1 + docs 1 + README = 5 |
| 5 | `d8ae1ac` | extractor 1 + test 1 + fixture 1 + docs 1 + README = 5 |
| 5.1 | `dc39dd4` | extractor 1 + test 1 = 2 |
| 6 | `e6d6970` | extractor 2 + test 2 + fixture 2 + docs 1 + README = 8 |
| 7 | `5697ef0` | shared 1 + test 1 + README = 3 |
| 8 | `301f04f` | shared 1 + test 1 + docs 1 (PoC 7+8 통합) + README = 4 |
| 9+10 | (다음 commit) | scan 2 + test 2 + docs 1 (PoC 9+10 통합) + README + verification + before-after + file-inventory + node-bridge fix = 9 |
