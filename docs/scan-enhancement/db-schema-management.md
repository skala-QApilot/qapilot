# DB Schema 관리 — `metadata_indices` 자동 생성 보완 노트

> 본 PR develop 머지 후 발견된 문제 — 다른 사람이 pull 받아도 `metadata_indices` 테이블이 자동 생성 안 되는 이유와 보완.

---

## 1. 한눈에 보는 3 layer

`metadata_indices` 와 관련된 파일은 책임이 다른 3 layer:

| Layer | 역할 | 파일 | 이번 보완 |
|---|---|---|---|
| **DDL** (테이블 정의) | "테이블은 어떤 컬럼/제약을 가지나" | `docs/scan-enhancement/migrations/001_metadata_indices.sql` ＋<br>`qapilot/shared/models.py` 의 `MetadataIndexRecord` | ✅ ORM 추가 |
| **DML** (row CRUD) | "테이블에 row 를 어떻게 넣고 읽나" | `qapilot/db/metadata_writer.py`<br>`qapilot/db/metadata_reader.py` | 변경 0 |
| **자동 생성 매커니즘** | "언제 어떻게 테이블이 만들어지나" | `qapilot/shared/database.py:create_tables()` | ✅ ORM import 추가 |

각 layer 가 헷갈리기 쉬워서 명확히 분리:

---

## 2. 각 파일의 정확한 역할

### 2.1 `docs/scan-enhancement/migrations/001_metadata_indices.sql`

**역할**: raw SQL DDL — 테이블 정의의 "원본 spec".

내용:
```sql
CREATE TABLE IF NOT EXISTS metadata_indices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id      UUID NOT NULL,
    commit_hash     VARCHAR(64) NOT NULL,
    kind            VARCHAR(32) NOT NULL,
    sub_kind        VARCHAR(32) NOT NULL,
    s3_key          TEXT NOT NULL,
    bytes           BIGINT,
    sha256          VARCHAR(64),
    file_count      INTEGER,
    confidence      NUMERIC(3, 2),
    extraction_method VARCHAR(16),
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (service_id, commit_hash, kind, sub_kind),
    CHECK (kind IN ('frontend', 'backend', 'sut_tests')),
    ...
);
```

이 파일을 누가 어떻게 사용하나:
- **PoC 단계 dev 환경**: `psql` 또는 `docker exec ... psql -f ...` 으로 직접 적용
- **운영 단계**: 향후 alembic 도입 시 첫 revision 의 origin spec 으로 활용
- **다른 사람이 pull 받았을 때**: **자동 실행 매커니즘이 없음** — 본 PR 머지 직후 발견된 문제

→ 이 파일만으로는 테이블이 자동 안 생긴다.

---

### 2.2 `qapilot/db/metadata_writer.py`

**역할**: 테이블 정의가 아니라 **row INSERT/UPDATE 의 비즈니스 로직** (DML).

```python
def upsert_metadata_index(index, *, service_id, commit_hash, ...) -> bool:
    """
    1. Pydantic → JSON 직렬화 + sha256
    2. S3 head_object 로 cache skip 판단
    3. S3 PUT (변경 있으면)
    4. raw SQL: INSERT INTO metadata_indices ... ON CONFLICT ... DO UPDATE
    """
```

핵심:
- 이 함수는 **테이블이 이미 존재한다고 전제**.
- 테이블이 없으면 `INSERT` 가 `relation "metadata_indices" does not exist` 에러로 실패.
- 따라서 이 파일 import / 호출만으로는 테이블이 생기지 않음.

`upsert_source_file` 도 동일 — S3 PUT 만 하고 DB row 는 없음 (`source/` path 자체가 카탈로그).

---

### 2.3 `qapilot/db/metadata_reader.py`

**역할**: row **SELECT 의 비즈니스 로직** (DML).

```python
def load_metadata_index_raw(service_id, kind, sub_kind, commit_hash=None) -> dict | None:
    """
    1. raw SQL: SELECT s3_key FROM metadata_indices WHERE ... ORDER BY scanned_at DESC LIMIT 1
    2. S3 GET → JSON parse → dict
    """

def get_latest_commit_hash(service_id, kind=None, sub_kind=None) -> str | None:
    """raw SQL: SELECT commit_hash FROM metadata_indices ORDER BY scanned_at DESC LIMIT 1"""
```

writer 와 동일하게 **테이블 존재 전제** — 테이블 없으면 그냥 빈 결과 또는 에러.

---

### 2.4 `qapilot/shared/database.py:create_tables()` (기존 qapilot 패턴)

**역할**: 애플리케이션 기동 시 모든 ORM 모델의 테이블을 자동 생성.

```python
async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```

`Base.metadata.create_all` 은 `Base` 를 상속한 모든 SQLAlchemy 모델의 테이블을 생성. **이미 존재하면 skip** (idempotent).

호출 위치: `qapilot/agents/requirement_extractor/agent.py:107`. 즉 그 agent 가 한 번 호출되면 자동 생성됨.

---

### 2.5 `qapilot/shared/models.py` — `MetadataIndexRecord` (본 PR 신설)

**역할**: SQLAlchemy ORM 모델 — `Base.metadata.create_all` 자동 생성 대상으로 등록.

```python
class MetadataIndexRecord(Base):
    __tablename__ = "metadata_indices"
    id:           Mapped[UUID] = mapped_column(..., server_default=text("gen_random_uuid()"))
    service_id:   Mapped[UUID] = ...
    commit_hash:  Mapped[str] = mapped_column(String(64), ...)
    kind:         Mapped[str] = mapped_column(String(32), ...)
    sub_kind:     Mapped[str] = mapped_column(String(32), ...)
    ...
    __table_args__ = (
        UniqueConstraint(...),
        CheckConstraint("kind IN ('frontend', 'backend', 'sut_tests')", ...),
        ...
    )
```

DDL 원본과 1:1 동등 — 컬럼 12개 / UNIQUE 1 / CHECK 4 / index 2 모두 동일.

`metadata_writer.py` 는 여전히 raw SQL `INSERT ... ON CONFLICT` 그대로. 본 ORM 모델은 **schema 정의 + 자동 생성용** 이지 writer/reader 가 본 모델로 동작하는 게 아님 (성능 + 기존 PR #240 패턴 일관성).

---

## 3. 왜 본 PR 머지 후 자동 생성 안 됐나

### 3.1 원인

PoC 단계 작업 흐름:
1. DDL 작성 — `docs/scan-enhancement/migrations/001_metadata_indices.sql`
2. **dev 환경에 수동 적용** — `docker exec ... psql -f ...`
3. writer/reader 작성 — raw SQL, 테이블 존재 전제
4. 검증 통과 (본인 dev 환경에 테이블이 이미 있어서)

문제 = **3 → 4 단계에 ORM 등록을 빠뜨림**. 본인 dev 환경에서 동작했지만 다른 사람의 새 환경에서는 테이블 자체가 없음.

### 3.2 qapilot 의 기존 schema 관리 패턴

qapilot 은 **alembic 안 씀**. 대신 `Base.metadata.create_all` 패턴:
- `qapilot/shared/models.py` 의 ORM 모델 → 자동 생성
- ORM 등록 안 된 테이블 (codebase_indices, services 등 대부분) → 수동 또는 외부에서 적용

→ 본인 metadata_indices 도 ORM 등록 안 했으니 같은 운명이었음.

### 3.3 본 보완으로 해결

| 변경 | 효과 |
|---|---|
| `qapilot/shared/models.py` 에 `MetadataIndexRecord` 추가 | `Base.metadata` 등록 |
| `qapilot/shared/database.py:create_tables()` 안에 `from qapilot.shared import models` import 추가 | `Base.metadata.create_all` 호출 시 본 ORM 도 등록 보장 |

이제 어디든 `create_tables()` 가 호출되면 `metadata_indices` 자동 생성.

---

## 4. 다른 사람이 pull 받았을 때 검증 방법

### 4.1 자동 생성 트리거
qapilot 의 어딘가 `create_tables()` 가 호출되면 자동 생성. 가장 확실한 트리거:

```bash
# 1. qapilot 의존성 설치
uv sync

# 2. .env 로드 (DATABASE_URL 등)
set -a; source .env; set +a

# 3. ORM 자동 생성 수동 트리거
python -c "
import asyncio
from qapilot.shared.database import create_tables
asyncio.run(create_tables())
"
```

### 4.2 생성 확인

```bash
# Docker MinIO + Postgres 가 떠 있다는 전제
docker exec qapilot-postgres psql -U qapilot -d qapilot -c "\d metadata_indices"
```

기대 출력:
```
                             Table "public.metadata_indices"
      Column       |           Type           | Nullable |      Default      
-------------------+--------------------------+----------+-------------------
 id                | uuid                     | not null | gen_random_uuid()
 service_id        | uuid                     | not null | 
 commit_hash       | character varying(64)    | not null | 
 ...
Indexes:
    "metadata_indices_pkey" PRIMARY KEY, btree (id)
    "idx_metadata_indices_commit" btree (commit_hash)
    "idx_metadata_indices_lookup" btree (service_id, kind, sub_kind, scanned_at)
    "metadata_indices_service_id_..._sub_kind_key" UNIQUE CONSTRAINT, btree (...)
Check constraints:
    ...
```

### 4.3 직접 적용도 가능 (선택)

ORM 자동 생성을 못 쓰는 환경이면 DDL 직접 적용:
```bash
docker exec -i qapilot-postgres psql -U qapilot -d qapilot \
  < docs/scan-enhancement/migrations/001_metadata_indices.sql
```

ORM 정의와 DDL 이 동등하므로 어느 쪽으로 적용해도 결과는 같음.

---

## 5. 본 보완에서 변경한 파일

| 파일 | 변경 |
|---|---|
| `qapilot/shared/models.py` | `MetadataIndexRecord` ORM 클래스 추가 (+62 줄) |
| `qapilot/shared/database.py` | `create_tables()` 안에 `from qapilot.shared import models` import 추가 (+3 줄) |
| `docs/scan-enhancement/db-schema-management.md` | 본 노트 (신규) |

기존 `metadata_writer.py` / `metadata_reader.py` / DDL `.sql` / 단위 테스트 영향 0.
단위 테스트 231/231 PASS 유지.

---

## 6. 후속 (운영 시점 — 본 PR 범위 외)

- alembic 도입 시 본 ORM 모델을 첫 revision 의 origin 으로 활용
- qapilot 의 다른 raw SQL 테이블 (codebase_indices 등) 도 같은 방식으로 ORM 정리 가능 (별도 단위)
