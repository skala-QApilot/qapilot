# PoC 4 — Scan Storage 조회 헬퍼 (유빈 agent 의 entry point)

> 데이터 layer 담당 (주환). 유빈 agent (TC/TV generator) 가 호출할 통합 API.

---

## 1. 한 줄 요약

| | |
|---|---|
| **목적** | `load_metadata_index(...)` + `load_source(...)` — 데이터 layer 의 single entry point |
| **재사용** | 기존 PR #240 `load_codebase_index` 패턴 그대로 (DB s3_key → S3 GET → JSON parse) |
| **추가 기능** | process-local LRU 캐시, line range 슬라이싱, `commit=None` 자동 최신 해결 |
| **검증** | 18/18 단위 PASS + 실 환경 (PostgreSQL + MinIO) end-to-end 8/8 PASS |
| **상태** | ✅ commit (브랜치 `feat/juhwan/scan-enhancement-foundation`) |

---

## 2. Public API (유빈 호출용)

### 2.1 `load_metadata_index`

```python
from qapilot.shared.scan_storage import load_metadata_index

selectors = load_metadata_index(
    service_id="7732f844-...",
    kind="frontend",        # "frontend" | "backend" | "sut_tests"
    sub_kind="selectors",   # "selectors" | "routes" | "schemas" | "patterns"
    commit_hash=None,       # None → 최신 scanned_at 자동 해결
)
# → {"kind": "frontend", "sub_kind": "selectors", "by_route": {...}, ...}
# 또는 None (미존재 / S3 실패 시)
```

**동작**:
1. `commit_hash=None` 이면 `metadata_indices` 의 최신 `scanned_at` commit 자동 조회
2. DB `SELECT s3_key FROM metadata_indices WHERE ...` → S3 `get_object(s3_key)` → JSON parse
3. 같은 `(service, kind, sub_kind, commit)` 반복 호출 = LRU cache hit (DB/S3 호출 없음)

### 2.2 `load_source`

```python
from qapilot.shared.scan_storage import load_source

# 전체 본문
full = load_source(service_id, sha, "backend/app/auth.py")

# 특정 line range — token 절감용
snippet = load_source(service_id, sha,
                      "backend/app/auth.py",
                      line_start=42, line_end=58)
# 1-based inclusive
```

**동작**:
1. S3 path = `services/{sid}/source/{sha}/{path}` (`..` traversal 자동 제거)
2. `get_object` → utf-8 디코딩 (binary 파일 = None graceful)
3. line range 슬라이싱은 캐시 **후** 적용 — 같은 파일을 다른 range 로 요청 시 S3 GET 1회만

---

## 3. 흐름도 (PoC 2 → 3 → 4 → 유빈)

```text
[PoC 2] vue_sfc_parser.extract_selectors_from_vue(...)
              │
              ▼
[caller — service register flow]
   FrontendSelectorsIndex 구성
              │
              ▼
[PoC 3] upsert_metadata_index(...)
   S3 PUT + DB INSERT/UPDATE
              │
              │   ⏱ scan 단계 끝
              │   ⏱ TC/TV 생성 단계 시작
              ▼
[유빈 agent.generate_tc / generate_tv]
   selectors = load_metadata_index(sid, "frontend", "selectors")
   schemas   = load_metadata_index(sid, "backend",  "schemas")
   code_snippet = load_source(sid, sha, "auth.py", line_start=42, line_end=58)
              │
              ▼
[PoC 4 ] scan_storage.py
   ├─ load_metadata_index
   │     ├─ commit_hash=None → get_latest_commit_hash (DB)
   │     ├─ cache hit? → 반환
   │     └─ cache miss → load_metadata_index_raw (DB SELECT + S3 GET + JSON parse)
   └─ load_source
         ├─ path sanitize (.. 제거)
         ├─ cache hit? → line slice
         └─ cache miss → s3_client.get_object → utf-8 → line slice
              │
              ▼
[유빈 LLM prompt 컨텍스트 주입]
   환각 selector 차단 + 시나리오 의도 검증 가능 (PoC 0 문서의 격차 A-1/A-2/A-3 해결)
```

---

## 4. 캐시 정책

### 4.1 LRU (process-local)
- `_metadata_cached`: 영역 × commit × service ≈ 32 → maxsize=64
- `_source_cached`: 파일 호출 패턴 의존 → maxsize=128
- 둘 다 `functools.lru_cache` — process restart 마다 자동 비움

### 4.2 Cache key
- metadata: `(service_id, kind, sub_kind, commit_hash)` — 4 차원
- source: `(service_id, commit_sha, path)` — 3 차원 (line range 는 키 아님)

### 4.3 Hit 사례
| 사례 | DB call | S3 call |
|---|---|---|
| 처음 selectors 호출 (commit 명시) | 1회 | 1회 |
| 같은 selectors 다시 호출 | 0 | 0 (cache hit) |
| `commit=None` → 자동 해결 후 같은 commit | 1회 (latest 조회) | cache hit |
| 같은 source 파일 다른 line range | 0 | 1회 (첫 호출만) |

→ 한 trace 안에서 같은 데이터 반복 사용해도 cost 1회.

### 4.4 강제 새로고침
```python
from qapilot.shared.scan_storage import clear_cache
clear_cache()  # 테스트 격리 / 강제 새로고침
```

---

## 5. 실 환경 end-to-end 검증 (2026-06-09)

mini-bss-lite `Signup.vue` → PoC 2 추출 → PoC 3 저장 → PoC 4 조회 (8 케이스):

| Test | 결과 |
|---|---|
| explicit commit 으로 metadata 조회 | ✅ kind/sub_kind/routes/inputs 정확 |
| `commit=None` → 최신 자동 해결 | ✅ 방금 PUT 한 commit 으로 자동 매칭 |
| 같은 호출 4회 반복 | ✅ cache: hits=4, misses=1 |
| source 전체 본문 (3585 chars) | ✅ `data-testid="email"` 포함 |
| source line range 27-33 (email input) | ✅ 6 lines 정확 (`<input` ... `data-testid="email"`) |
| 같은 파일 다른 range 3회 | ✅ S3 GET 1회만, cache: hits=3 misses=1 |
| 미존재 path | ✅ None + `source_miss` 로그 |
| `../../etc/passwd` traversal | ✅ ".." 제거 → S3 미존재 → None |

---

## 6. 단위 테스트 (`tests/test_scan_storage.py` — 18/18 PASS)

| 그룹 | 케이스 |
|---|---|
| `_slice_lines` | inclusive range, no range, only start, only end, out-of-range clamp |
| `load_metadata_index` | explicit commit 통과, commit=None 자동 해결, latest 없음 graceful, 반복 호출 cache hit |
| `load_source` | 전체 본문, line range, cache hit (다른 range 같은 파일), 미존재 None, traversal sanitize, utf-8 실패 None, 빈 인자 |
| cache 관리 | `cache_info()` shape, `clear_cache()` reset |

mock 으로 격리 — 실 PostgreSQL/MinIO 의존 없이 단위 검증.

---

## 7. 격차 매핑 (PoC 0 의 8 격차)

| 격차 | PoC 4 의 기여 |
|---|---|
| **A-1 오라클** (LLM 이 코드 동작 = 정답 박제) | `load_source` 로 명세 문서 + 실 코드 양쪽 LLM 에 주입 가능 → 명세 우선화 |
| **A-2 도메인 hardcoded** (BSS 키워드) | `load_metadata_index("backend", "schemas")` 로 도메인 schema AST 추출 사용 |
| **A-3 런타임 상태** (TC-01 → TC-02 전달) | (PoC 6 TVValidator + TV pool 에서 완성) — 본 PoC 의 캐시 패턴 그대로 적용 |
| **B-3 캐시 미구현** | process-local LRU + `head_object` (PoC 3) 이중 cache 로 해결 |
| **B-4 결정성** | `(sha, file_path)` 결정성 — 같은 입력 = 같은 출력 |

---

## 8. 다음 (PoC 5+)

| PoC | PoC 4 와의 관계 |
|---|---|
| 5 (React jsx 확장) | `react_jsx_parser` 결과도 같은 `FrontendSelectorsIndex` → PoC 3 → PoC 4 그대로 사용 |
| 5 (3 영역 추가) | `routes/schemas/patterns` 각각 `load_metadata_index(kind, sub_kind)` 호출만 변경 |
| 6 (TVValidator helper) | `load_metadata_index("backend", "schemas")` + `load_source` 활용 |
| 7 (DBTool 통합) | DB snapshot cache 도 본 PoC 의 LRU 패턴 차용 |
