"""시나리오 저장 구조 마이그레이션: 단일 `{ts_id}.json` → TS/TC 폴더+버전 구조.

기존:
    scenarios/
      TS-001.json   # 메타데이터 + test_cases 전체

신규:
    scenarios/
      total_TS.json
      TS-001/
        metadata.json
        TC-01/
          latest.json
          v1.json

세 위치를 변환한다:
1. 로컬 `.qapilot/scenarios/` (기본: `<repo_root>/qapilot/.qapilot/scenarios`)
2. `qapilot-local/services/{service_id}/scenarios/`
3. S3 `services/{service_id}/scenarios/`

사용법:
    python scripts/migrate_scenario_layout.py [--service-id ID] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from qapilot.agents.scenario_generator import repository
from qapilot.storage import s3_client


def _repo_root() -> Path:
    """`qapilot/scripts/` 기준 repo root (qapilot/ 의 부모)를 반환한다."""
    return Path(__file__).resolve().parents[2]


def convert_ts_json_to_layout(old_ts: dict, scenarios_root: Path) -> None:
    """단일 `{ts_id}.json` 내용을 새 폴더+버전 구조로 v1 기록한다 (provenance=None)."""
    repository.save_scenario(old_ts, changed_tc_ids=None, deleted_tc_ids=None, base_dir=scenarios_root)


def migrate_local_dir(scenarios_root: Path, *, dry_run: bool) -> int:
    """`scenarios_root` 아래의 `TS-*.json` 파일들을 새 구조로 변환한다. 변환 개수 반환."""
    if not scenarios_root.exists():
        return 0
    old_files = sorted(scenarios_root.glob("TS-*.json"))
    if not old_files:
        return 0

    print(f"[{scenarios_root}] {len(old_files)}개 TS 파일 변환 대상")
    if dry_run:
        for p in old_files:
            print(f"  (dry-run) {p.name} -> {p.stem}/")
        return len(old_files)

    for p in old_files:
        old_ts = json.loads(p.read_text(encoding="utf-8"))
        convert_ts_json_to_layout(old_ts, scenarios_root)
        p.unlink()
        print(f"  변환 완료: {p.name} -> {p.stem}/")

    repository._regenerate_total_ts(base_dir=scenarios_root)
    return len(old_files)


def migrate_local_qapilot(*, dry_run: bool) -> None:
    scenarios_root = _repo_root() / "qapilot" / ".qapilot" / "scenarios"
    migrate_local_dir(scenarios_root, dry_run=dry_run)


def migrate_qapilot_local(*, service_id: str | None, dry_run: bool) -> None:
    services_root = _repo_root() / "qapilot-local" / "services"
    if not services_root.exists():
        return
    for service_dir in sorted(services_root.iterdir()):
        if not service_dir.is_dir():
            continue
        if service_id and service_dir.name != service_id:
            continue
        scenarios_root = service_dir / "scenarios"
        migrate_local_dir(scenarios_root, dry_run=dry_run)


def migrate_s3(*, service_id: str | None, dry_run: bool) -> None:
    services_root = _repo_root() / "qapilot-local" / "services"
    if not services_root.exists():
        return
    service_ids = [
        d.name for d in sorted(services_root.iterdir())
        if d.is_dir() and (not service_id or d.name == service_id)
    ]

    for sid in service_ids:
        prefix = f"services/{sid}/scenarios/"
        keys = s3_client.list_objects(prefix)
        # depth 1의 "{ts_id}.json" 키만 대상 (예: services/{sid}/scenarios/TS-001.json)
        old_keys = [
            k for k in keys
            if k[len(prefix):].count("/") == 0 and k.endswith(".json") and k[len(prefix):].startswith("TS-")
        ]
        if not old_keys:
            continue

        print(f"[S3 {prefix}] {len(old_keys)}개 TS 객체 변환 대상")
        if dry_run:
            for k in old_keys:
                print(f"  (dry-run) {k}")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            scenarios_root = Path(tmp) / "scenarios"
            scenarios_root.mkdir(parents=True, exist_ok=True)

            for k in old_keys:
                data = s3_client.get_object(k)
                if data is None:
                    print(f"  다운로드 실패, 건너뜀: {k}")
                    continue
                old_ts = json.loads(data.decode("utf-8"))
                convert_ts_json_to_layout(old_ts, scenarios_root)

            repository._regenerate_total_ts(base_dir=scenarios_root)

            # 변환된 파일 전체를 동일 prefix 아래에 업로드
            for path in scenarios_root.rglob("*.json"):
                rel = path.relative_to(scenarios_root)
                key = f"{prefix}{rel.as_posix()}"
                body = path.read_bytes()
                result = s3_client.put_bytes(key, body, "application/json")
                if result is None:
                    print(f"  업로드 실패: {key}")
                else:
                    print(f"  업로드: {key}")

            # 기존 {ts_id}.json 키 삭제
            for k in old_keys:
                if s3_client.delete_object(k):
                    print(f"  삭제: {k}")
                else:
                    print(f"  삭제 실패: {k}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-id", default=None, help="특정 서비스만 마이그레이션")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 대상 목록만 출력")
    args = parser.parse_args()

    migrate_local_qapilot(dry_run=args.dry_run)
    migrate_qapilot_local(service_id=args.service_id, dry_run=args.dry_run)
    migrate_s3(service_id=args.service_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
