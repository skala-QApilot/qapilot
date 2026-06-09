# PoC 3 — metadata_indices Writer (DB + S3 mirror)

> 본인 영역 (주환). 2026-06-09 토론 결정 4 (별도 `metadata_indices` 테이블) 의 writer 구현.

---

## 1. 한 줄 요약

| | |
|---|---|
| **목적** | Pydantic 메타데이터 index → S3 (본문 PUT) + PostgreSQL (카탈로그 upsert) 동시 기록 |
| **재사용** | 본인 PR #240 `upsert_codebase_index` mirror 패턴 그대로 차용 |
| **추가 기능** | cache skip (`head_object` bytes 일치 시 PUT 생략), force flag, source/ 본문 별도 헬퍼 |
| **검증** | 14/14 단위 PASS + 실 환경 (PostgreSQL + MinIO) end-to-end 통과 |
| **상태** | ✅ commit (브랜치 `feat/me/scan-enhancement-foundation`) |

---

## 2. 흐름도 (caller → writer → S3/DB)

```text
[caller — 본인 scanner 또는 직접 호출]
   │
   │ FrontendSelectorsIndex(service_id=..., commit_sha=..., by_route={...})
   ▼
[qapilot/db/metadata_writer.upsert_metadata_index(...)]
   │
   │ ① Pydantic → kind/sub_kind 추출 (BaseModel.kind / sub_kind 속성)
   │ ② JSON 직렬화 (sort_keys=True → sha256 결정성)
   │
   ▼  ③ S3 cache check
[qapilot/storage/s3_client.head_object(s3_key)]
   │
   ├── head.bytes == len(body) → PUT skip + info log
   │                              └→ DB upsert 만 (scanned_at 갱신)
   └── 미존재 / 불일치 → PUT 시도
                          │
                          ▼  ④ S3 PUT
                          [s3_client.put_bytes(key, body, "application/json")]
                          │
                          ├── 실패 → 즉시 False (DB 안 씀)
                          └── 성공
                                │
                                ▼  ⑤ DB upsert
                                [INSERT ... ON CONFLICT (service_id, commit_hash,
                                                          kind, sub_kind)
                                 DO UPDATE SET scanned_at=NOW(), ...]
```

---

## 3. 핵심 결정사항

### 3.1 S3 cache skip 정책 (`head_object` bytes 일치)

| 경우 | 동작 |
|---|---|
| 첫 호출 (S3 미존재) | PUT + DB INSERT |
| 같은 SHA 재호출 + 본문 동일 | **PUT skip** + DB UPDATE (scanned_at) |
| 같은 SHA 재호출 + 본문 변경 (드물지만 추출 코드 갱신 시) | PUT + DB UPDATE |
| `force=True` | 무조건 PUT + DB UPDATE |

`head_object` 만 호출 (`get_object` 대신) — 본문 다운로드 없음, 빠름 + 비용 절감.

### 3.2 UNIQUE constraint → ON CONFLICT DO UPDATE

PoC 1 DDL 의 `UNIQUE (service_id, commit_hash, kind, sub_kind)` 이 본 정책의 기반:

```sql
INSERT INTO metadata_indices (...)
VALUES (...)
ON CONFLICT (service_id, commit_hash, kind, sub_kind) DO UPDATE SET
    s3_key            = EXCLUDED.s3_key,
    bytes             = EXCLUDED.bytes,
    sha256            = EXCLUDED.sha256,
    file_count        = EXCLUDED.file_count,
    confidence        = EXCLUDED.confidence,
    extraction_method = EXCLUDED.extraction_method,
    scanned_at        = NOW()
```

→ 같은 (service, commit, 영역) 의 재스캔이 항상 한 row 로 수렴. `scanned_at` 으로 최신 시점 추적 가능.

### 3.3 결정성 (sha256 안정성)

`_to_json_bytes()` 가 `json.dumps(..., sort_keys=True, ensure_ascii=False, indent=2)` 사용:
- 같은 Pydantic model → 같은 bytes → 같은 sha256
- dict 순서 다른 두 입력도 sort_keys 로 같은 결과 (`test_to_json_bytes_deterministic` 검증)

→ 격차 B-4 (결정성) 정합.

### 3.4 source/ 본문 = 별도 헬퍼 (`upsert_source_file`)

코드베이스 원본은 DB row 없음 — S3 path 자체가 카탈로그 (`services/{sid}/source/{sha}/{rel_path}`).
이유: source 가 `metadata_indices` row 마다 1:N 으로 많아서 DB 비용 ↑. path 만 안다면 직접 조회.

```python
upsert_source_file(
    service_id=...,
    commit_hash=...,
    relative_path="backend/app/auth.py",
    content=b"...",  # 파일 본문
) -> str | None  # 성공 시 s3_key
```

path traversal 방어 — `..` 제거 + leading `/` 제거.

---

## 4. PoC 2 → PoC 3 → 다음 PoC 4 연결부

```text
[PoC 2] vue_sfc_parser.extract_selectors_from_vue(...)
              │ list[InputElement | ButtonElement | OutputElement | DynamicElement]
              ▼
[caller 가 합쳐서 FrontendSelectorsIndex 구성]
              │
              ▼
[PoC 3] upsert_metadata_index(idx, service_id=..., commit_hash=..., confidence=1.0, extraction_method='ast')
              │
              │  S3 PUT + DB upsert
              ▼
[PoC 4] load_metadata_index(service_id, "frontend", "selectors")
              │  DB SELECT s3_key → S3 GET → JSON parse
              ▼
[유빈 agent] LLM prompt context 주입 → TC/TV 생성
```

---

## 5. 실 환경 end-to-end 검증 결과 (2026-06-09)

mini-bss-lite 의 `Signup.vue` → PoC 2 추출 (9 element) → FrontendSelectorsIndex 구성 → PoC 3 upsert:

```text
=== 1차 upsert ===
S3 PUT: services/.../metadata-index/aaaa.../frontend-selectors.json (5184 bytes)
S3 head: {bytes: 5184, etag: "26065b127..."}
S3 GET → JSON parse → kind=frontend, sub_kind=selectors, routes=['/signup'], inputs=4 ✅
DB INSERT: confidence=1.00, extraction_method=ast, scanned_at=2026-06-09 14:09:01

=== 2차 upsert (cache skip 기대) ===
log: metadata_index_s3_skip reason=head_object_match_bytes ✅
DB UPDATE: scanned_at 갱신 ✅

=== source/ 본문 PUT ===
upsert_source_file → services/.../source/aaaa.../frontend/src/pages/Signup.vue (3809 bytes) ✅
```

---

## 6. 단위 테스트 매트릭스 (`tests/test_metadata_writer.py` — 14/14 PASS)

| 그룹 | 케이스 |
|---|---|
| helpers | Pydantic → kind/sub_kind, dict → kind/sub_kind, 누락 시 빈 tuple, s3_key 형식, JSON 결정성 |
| upsert (S3) | cache skip 시 PUT 호출 안 됨, force=True 시 PUT 호출됨, S3 실패 시 DB INSERT 안 됨 |
| upsert (DB) | pool 없음 시 False, kind 누락 시 False, confidence/extraction_method 가 SQL args 에 들어감 |
| upsert_source_file | `..` traversal 제거, cache skip, S3 실패 시 None |

mock 으로 격리 — 실 PostgreSQL/MinIO 의존 없이 단위 검증.

---

## 7. 다음 (PoC 4)

`qapilot/db/metadata_reader.py` + `qapilot/shared/scan_storage.py`:

```python
def load_metadata_index(
    service_id: str,
    kind: str,
    sub_kind: str,
    commit_hash: str | None = None,  # None 시 최신
) -> dict | None:
    """DB 의 s3_key → S3 GET → JSON parse. 본인 mirror fallback 패턴."""

def load_source(
    service_id: str,
    sha: str,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> str | None:
    """S3 GET + line range 잘라서 반환 (token 절감)."""
```

이후 유빈 agent 가 사용 가능.
