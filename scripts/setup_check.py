"""다른 사람 환경 setup 검증 스크립트.

본 PR pull 받은 팀원이 한 번 실행해서 모두 OK 인지 확인:
- ✅ PostgreSQL 연결 + `metadata_indices` 테이블 존재 (ORM 자동 생성 확인)
- ✅ MinIO 연결 + `qapilot-local` bucket 존재 (docker-compose minio-bucket-init 동작)
- ✅ 본 데이터 layer 의 5 public API import 가능
- ✅ Node bridge (@vue/compiler-sfc) 가용

사용법:
    python scripts/setup_check.py

실패 시 어떤 단계가 안 됐는지 + 해결 방법 출력.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path


# ANSI 색
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _ok(msg: str) -> None:
    print(f"{_GREEN}✅ {msg}{_RESET}")


def _fail(msg: str, fix: str | None = None) -> None:
    print(f"{_RED}❌ {msg}{_RESET}")
    if fix:
        print(f"   → {fix}")


def _warn(msg: str) -> None:
    print(f"{_YELLOW}⚠️  {msg}{_RESET}")


def _section(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


# ────────────────────────────────────────────────────────────────────────
# 1. 환경 변수
# ────────────────────────────────────────────────────────────────────────

REQUIRED_ENV = (
    "DATABASE_URL",
    "S3_ENDPOINT",
    "S3_BUCKET",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
)


def check_env() -> bool:
    _section("1. 환경변수 (.env 로드)")
    # .env 자동 로드 시도
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        _warn("python-dotenv 미설치 — .env 자동 로드 안 됨")

    ok = True
    for key in REQUIRED_ENV:
        val = os.environ.get(key)
        if val:
            _ok(f"{key} = {val[:30]}...")
        else:
            _fail(f"{key} 미설정",
                  fix=f"cp qapilot-server/.env.example .env 후 값 채우기")
            ok = False
    return ok


# ────────────────────────────────────────────────────────────────────────
# 2. PostgreSQL + metadata_indices 테이블
# ────────────────────────────────────────────────────────────────────────

def check_postgres_and_table() -> bool:
    _section("2. PostgreSQL + metadata_indices 테이블")
    try:
        from qapilot.shared.database import create_tables
    except Exception as e:
        _fail(f"qapilot.shared.database import 실패: {e}",
              fix="uv sync 실행 후 재시도")
        return False

    try:
        asyncio.run(create_tables())
        _ok("create_tables() 호출 성공 — ORM 모델 자동 등록")
    except Exception as e:
        _fail(f"create_tables() 실패: {e}",
              fix="docker compose up -d (qapilot-server/) — postgres 띄우기")
        return False

    # 테이블 직접 확인
    try:
        from qapilot.db.connection import get_pool
        pool = get_pool()
        if pool is None:
            _fail("DB pool 미초기화", fix="DATABASE_URL 확인")
            return False
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'metadata_indices')"
            )
            exists = cur.fetchone()[0]
        if exists:
            _ok("public.metadata_indices 테이블 존재")
            return True
        else:
            _fail("metadata_indices 테이블 미존재 (ORM 등록 안 됨)",
                  fix="qapilot/shared/models.py 의 MetadataIndexRecord 확인")
            return False
    except Exception as e:
        _fail(f"테이블 확인 실패: {e}")
        return False


# ────────────────────────────────────────────────────────────────────────
# 3. MinIO + qapilot-local bucket
# ────────────────────────────────────────────────────────────────────────

def check_minio_and_bucket() -> bool:
    _section("3. MinIO + qapilot-local bucket")
    try:
        from qapilot.storage import s3_client
        client, bucket = s3_client.get_client()
    except Exception as e:
        _fail(f"s3_client 초기화 실패: {e}")
        return False

    if client is None:
        _fail("S3 client None — 환경변수 미설정 또는 연결 실패",
              fix="docker compose up -d (qapilot-server/) — minio 띄우기")
        return False

    _ok(f"S3 client 연결 OK (bucket={bucket})")

    try:
        client.head_bucket(Bucket=bucket)
        _ok(f"bucket '{bucket}' 존재 확인")
        return True
    except Exception as e:
        _fail(f"bucket '{bucket}' 미존재 또는 접근 실패: {e}",
              fix="docker compose 의 minio-bucket-init 서비스가 정상 실행됐는지 확인 "
                  "(docker logs qapilot-minio-init)")
        return False


# ────────────────────────────────────────────────────────────────────────
# 4. 본 데이터 layer 의 5 public API import
# ────────────────────────────────────────────────────────────────────────

def check_public_api_imports() -> bool:
    _section("4. 본 데이터 layer 의 5 public API")
    targets = [
        ("scan_all_metadata", "qapilot.scan.orchestrator"),
        ("load_metadata_index", "qapilot.shared.scan_storage"),
        ("load_source", "qapilot.shared.scan_storage"),
        ("get_db_snapshot_cached", "qapilot.shared.db_state"),
        ("TVValidator", "qapilot.shared.tv_validator"),
        # 본 PR 신규
        ("TVFromCodebaseAgent", "qapilot.agents.tv_codebase_aware_agent"),
        ("filter_metadata_for_tc", "qapilot.shared.metadata_filters"),
        ("strip_sensitive_from_schemas", "qapilot.shared.sensitive_mask"),
    ]
    ok = True
    for name, module in targets:
        try:
            mod = __import__(module, fromlist=[name])
            getattr(mod, name)
            _ok(f"{module}.{name}")
        except Exception as e:
            _fail(f"{module}.{name} import 실패: {e}",
                  fix=f"uv sync 후 재시도")
            ok = False
    return ok


# ────────────────────────────────────────────────────────────────────────
# 5. Node bridge (Vue 추출용)
# ────────────────────────────────────────────────────────────────────────

def check_node_bridge() -> bool:
    _section("5. Node bridge (@vue/compiler-sfc)")
    if shutil.which("node") is None:
        _fail("`node` binary not found on PATH",
              fix="Node.js (>=18) 설치")
        return False
    _ok(f"node 가용 ({subprocess.run(['node', '--version'], capture_output=True, text=True).stdout.strip()})")

    bridge_dir = Path(__file__).resolve().parents[1] / "qapilot" / "node-bridge"
    node_modules = bridge_dir / "node_modules" / "@vue" / "compiler-sfc"
    if not node_modules.exists():
        _fail(f"@vue/compiler-sfc 미설치 ({node_modules})",
              fix=f"cd {bridge_dir} && npm install")
        return False
    _ok(f"@vue/compiler-sfc 설치됨")
    return True


# ────────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────────

def main() -> int:
    print("\n" + "═" * 60)
    print("  QApilot 데이터 layer setup 검증")
    print("═" * 60)

    results = [
        check_env(),
        check_postgres_and_table(),
        check_minio_and_bucket(),
        check_public_api_imports(),
        check_node_bridge(),
    ]

    print("\n" + "═" * 60)
    if all(results):
        print(f"{_GREEN}모든 검증 통과 — e2e 실행 준비 완료{_RESET}")
        print("═" * 60)
        return 0
    failed = sum(1 for r in results if not r)
    print(f"{_RED}{failed}/{len(results)} 단계 실패{_RESET} — 위 fix 안내 참고")
    print("═" * 60)
    return 1


if __name__ == "__main__":
    sys.exit(main())
