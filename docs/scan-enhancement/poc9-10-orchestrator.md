# PoC 9 + 10 — git clone + 4영역 통합 orchestrator (본 데이터 layer 완전 완성)

> 본 데이터 layer 의 마지막 미흡 영역 (verification.md §2.1 A + B) 해소.
> 단위 components (PoC 1~8) 를 high-level orchestrator 로 cascade 호출.

---

## 1. 한 줄 요약

| | |
|---|---|
| **PoC 9 목적** | `git clone --depth 1` + repo walk + `source/` S3 dump 의 high-level helper |
| **PoC 10 목적** | `scan_all_metadata(service_id, repo_root, commit_sha)` — 4영역 추출/저장 한 호출 |
| **결과** | 본 데이터 layer 추출/저장/조회/검증/orchestration 모두 완성. 미흡 영역 0 |
| **검증** | 27/27 단위 PASS + 실 환경 mini-bss-lite 전체 한 호출 (selectors 125 / routes 16 / schemas 51 / patterns 129) |
| **상태** | ✅ commit (브랜치 `feat/juhwan/scan-enhancement-foundation`) |

---

## 2. PoC 9 — source_dumper.py

### 2.1 `clone_repo_shallow(repo_url, ...)` → `(repo_path, commit_sha)`

```python
from qapilot.scan.source_dumper import clone_repo_shallow

repo_path, sha = clone_repo_shallow(
    "https://x:TOKEN@github.com/foo/bar.git",
    branch="main",  # 기본 default branch
)
# repo_path = tempfile.mkdtemp() 의 임시 디렉토리
# sha = 풀 SHA (40자)
```

- `subprocess.run(["git", "clone", "--depth", "1", ...], shell=False)` — command injection 방어
- URL 의 PAT/password 는 log 시 `_mask_url()` 으로 마스킹 (`https://***@github.com/...`)
- caller 가 사용 후 `shutil.rmtree(repo_path)` 책임

### 2.2 `dump_source_to_s3(...)` → `SourceDumpResult`

```python
result = dump_source_to_s3(
    service_id=sid, repo_root=path, commit_sha=sha,
    whitelist=DEFAULT_WHITELIST,    # .py .vue .ts .tsx .jsx .js .html ...
    exclude_dirs=DEFAULT_EXCLUDE_DIRS,  # node_modules .git venv ...
    force=False,                     # head_object cache skip 활용
)
# result.files_walked / files_uploaded / files_skipped_cache / files_failed
```

cache skip 정책 (PoC 3 의 head_object 기반):
- 같은 `(service, sha, path)` 재호출 시 head_object 가 bytes 일치 확인 → PUT 회피
- `force=True` 면 무조건 PUT

graceful:
- 개별 파일 read/PUT 실패 = `failed_paths` 누적, 다른 파일 계속
- 전체 함수는 raise 없음 → caller 가 `SourceDumpResult.files_failed` 로 판단

### 2.3 walk 정책

| 정책 | 값 |
|---|---|
| whitelist 확장자 | `.py .js .ts .tsx .jsx .vue .html .css .scss .json .yaml .yml .toml .md .sql` |
| 디렉토리 제외 | `node_modules .git __pycache__ dist build venv .venv .qapilot ...` |
| 파일명 제외 | `package-lock.json yarn.lock pnpm-lock.yaml poetry.lock uv.lock` |
| 단일 파일 크기 상한 | 5MB (minified bundle 차단) |

---

## 3. PoC 10 — orchestrator.py

### 3.1 `scan_all_metadata(...)` — 한 호출에 4영역

```python
from qapilot.scan.orchestrator import scan_all_metadata

result = await scan_all_metadata(
    service_id=sid,
    repo_root=path,
    commit_sha=sha,
    skip_source_dump=False,            # source/ 본문 PUT
    skip_llm_classification=False,     # PoC 5.1 LLM 의미 분류 활성
    llm_client=my_llm,                 # None 이면 LLM 보강 skip
)

# result: ScanAllResult
#   source_dump: SourceDumpResult (skip_source_dump=False 시)
#   selectors_count / routes_count / schemas_*_count / patterns_count
#   *_upserted: bool (upsert 성공/실패)
#   patterns_classified: int (LLM 보강된 record 수)
#   errors: list[str] (영역별 실패 누적)
```

### 3.2 실행 순서

```text
1. dump_source_to_s3(...)           → source/ 본문 PUT (PoC 3 활용)
2. _build_frontend_selectors(...)   → vue_sfc_parser × 모든 .vue
   └→ upsert_metadata_index(FrontendSelectorsIndex)
3. _build_frontend_routes(...)      → vue_router_parser × router 파일
   └→ upsert_metadata_index(FrontendRoutesIndex)
4. _build_backend_schemas(...)      → backend_schema_parser × backend/**/*.py
   └→ upsert_metadata_index(BackendSchemasIndex)
5. _build_sut_tests_patterns(...)   → pytest_ast_parser × test_*.py
   └→ (선택) classify_unknown_patterns (LLM, PoC 5.1)
   └→ upsert_metadata_index(SutTestsPatternsIndex)
```

영역별 실패 graceful — 한 영역 실패해도 다른 영역 계속 진행.

### 3.3 route 추론 (frontend.selectors 의 by_route 구성)

```python
def _infer_route_from_path(vue_file: Path, repo_root: Path) -> str:
    """frontend/src/pages/Signup.vue → /signup
       frontend/src/components/Sidebar.vue → /_components/sidebar
    """
```

PoC 단계 단순 휴리스틱. 향후 PoC 6.B 의 vue_router_parser 결과와 cross-link 하면 component → route 정확 매핑 가능 (후속).

### 3.4 test 파일 vs backend schema 파일 분리

같은 `.py` 확장자지만:
- `test_*.py`, `*_test.py`, `conftest.py` → `pytest_ast_parser` (sut_tests.patterns)
- 그 외 `.py` → `backend_schema_parser` (backend.schemas)

상호 중복 X (orchestrator 가 명시적으로 분기).

---

## 4. 실 환경 통합 검증 — mini-bss-lite 전체 (commit 3fc4330)

```text
=== scan_all_metadata(service_id, mini-bss-lite, sha) ===
source_dump:   157 / 157 files PUT
selectors:     125 elements (across 9 routes)
routes:         16 records
schemas:       51 (req 12 + resp 24 + db_models 15)
patterns:     129 records
errors:          0
```

**한 호출에 4영역 + source 모두 완성**. PoC 3 의 head_object cache skip, PoC 4 의 LRU,
PoC 7 의 TVValidator 모두 그대로 동작.

---

## 5. 디버깅 노트 — node-bridge stdout flush

큰 `.vue` 파일 (Sidebar.vue 등) 처리 시 JSON 응답이 64KB 부근에서 잘리는 현상이 있어 `parse_vue_sfc.js` 의 모든 종료 경로에 stdout flush 보장을 추가했다.

```javascript
process.stdout.write(JSON.stringify(out), () => process.exit(0));
```

원인: `process.stdout.write(...)` 직후 callback 없이 `process.exit(0)` 를 호출하면 큰 PIPE write 가 완료되기 전 process 가 종료 — stdout 끝부분이 잘린다. callback 안에서 exit 하면 안전.

비슷한 subprocess 출력 잘림 문제 디버깅 시 참고.

---

## 6. 단위 테스트 매트릭스

### 6.1 PoC 9 (`tests/test_source_dumper.py` — 12 PASS)
- `_mask_url` 2개 (PAT 마스킹, no-auth 보존)
- `iter_source_files` 7개 (whitelist / exclude_dirs / exclude_filenames / 크기 상한 / 재귀 / missing root / custom whitelist)
- `dump_source_to_s3` 3개 (모든 파일 PUT / cache skip / force / failed 누적 / 빈 디렉토리)

### 6.2 PoC 10 (`tests/test_scan_orchestrator.py` — 15 PASS)
- `_infer_route_from_path` 2개 (pages/ vs components/)
- `scan_all_metadata` 9개 (빈 repo / routes / schemas aggregation / patterns 파일 분리 / backend test 제외 / source 실패 graceful / skip_source / LLM 호출 / skip_llm)
- `ScanAllResult` 1개 (초기 상태)

mock 으로 격리 — Pydantic schemas 의 실제 instance 사용 (MagicMock 은 strict validation 충돌).

---

## 7. 본 데이터 layer PoC 1~10 종합 — 완전 완성

| 영역 | 상태 |
|---|---|
| 추출 (PoC 2 + 5 + 5.1 + 6.A + 6.B) | ✅ 5 extractor |
| 저장 (PoC 3) | ✅ DB + S3 mirror + cache skip |
| 조회 (PoC 4) | ✅ LRU + line range slicing |
| 검증 (PoC 7) | ✅ TVValidator (schema/format/DB 존재성) |
| DB cache (PoC 8) | ✅ TTL cache |
| Source dump (PoC 9) | ✅ git clone + S3 PUT helper |
| **Orchestration (PoC 10)** | ✅ 4영역 + source 한 호출 |

**전체 단위 테스트 231/231 PASS**.

caller (유빈 agent / service register flow / CLI) 가 5 public API 만 호출:
1. `scan_all_metadata(sid, repo_root, sha)` — 한 호출에 모두 (PoC 10)
2. `load_metadata_index(sid, kind, sub_kind)` — 4영역 조회 (PoC 4)
3. `load_source(sid, sha, path, line_start, line_end)` — 본문 조회 (PoC 4)
4. `get_db_snapshot_cached(sid, table)` — DB 캐시 (PoC 8)
5. `TVValidator.validate(tv_field, intent, snap, schemas, schema_name)` — 검증 (PoC 7)
