"""시나리오 Repository — 파일 I/O.

scenarios/ 아래에 TS별 폴더 + TC별 폴더(버전 파일) 구조로 저장한다::

    scenarios/
      total_TS.json              # 전체 TS의 metadata.json만 모은 개요 배열 (캐시)
      TS-001/
        metadata.json            # TS 구조 정보 (test_cases 제외)
        TC-01/
          latest.json            # 최신 버전
          v1.json
          v2.json
        _deleted/                # 삭제된 TC 보관 (이력 보존)
          TC-03/
            latest.json
            v1.json

`metadata.json`이 source of truth이며 `total_TS.json`은 매 저장 시 재생성되는,
test_cases를 제외한 TS 메타데이터 개요 캐시다.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qapilot.shared.schemas import TestScenario

_SCENARIOS_DIR = Path(".qapilot/scenarios")

_METADATA_KEYS = (
    "ts_id",
    "name",
    "description",
    "trigger",
    "affected_files",
    "domain_rules_used",
    "requirements",
    "depends_on",
    "tc_generation_context",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _tc_folder_name(tc_id: str, ts_id: str) -> str:
    """tc_id에서 '{ts_id}-' 접두사를 제거한 짧은 폴더명을 반환한다.

    예: tc_id="TS-001-TC-01", ts_id="TS-001" → "TC-01"
    """
    prefix = f"{ts_id}-"
    return tc_id[len(prefix):] if tc_id.startswith(prefix) else tc_id


def _next_version(tc_dir: Path) -> int:
    versions: list[int] = []
    for p in tc_dir.glob("v*.json"):
        try:
            versions.append(int(p.stem[1:]))
        except ValueError:
            continue
    return (max(versions) + 1) if versions else 1


def _tc_content(tc: dict) -> dict:
    """provenance를 제외한 TC 내용을 반환 (변경 여부 비교용)."""
    return {k: v for k, v in tc.items() if k != "provenance"}


def _tc_content_equal(a: dict, b: dict) -> bool:
    return _tc_content(a) == _tc_content(b)


def save_scenarios(scenarios: list[TestScenario], *, base_dir: Path | None = None) -> None:
    """각 시나리오를 새 폴더 구조로 저장한다."""
    for scenario in scenarios:
        save_scenario(scenario, base_dir=base_dir)


def save_scenario(
    scenario: TestScenario,
    *,
    changed_tc_ids: set[str] | None = None,
    deleted_tc_ids: list[str] | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """단일 시나리오를 `scenarios/TS-{id}/` 구조로 저장한다.

    - `changed_tc_ids`가 None이면 모든 TC를 대상으로 변경 여부를 검사한다 (init 등 전체 저장).
    - `changed_tc_ids`가 주어지면 해당 TC만 검사하고, 그 외 TC는 건드리지 않는다.
    - 신규 TC는 항상 v1로 기록된다.
    - 내용이 기존 latest.json과 동일하면(provenance 제외) 새 버전을 만들지 않는다.
    - `deleted_tc_ids`에 해당하는 TC 폴더는 `_deleted/`로 이동한다.

    반환값(payload)은 metadata + 각 TC의 latest.json을 합쳐 재조립한 풀 TS dict로,
    DB upsert / S3·로컬 미러 동기화에 그대로 사용할 수 있다.
    """
    root = base_dir or _SCENARIOS_DIR
    ts_id = scenario["ts_id"]
    ts_dir = root / ts_id

    metadata = {k: scenario[k] for k in _METADATA_KEYS if k in scenario}
    metadata["last_modified_at"] = _now_iso()
    metadata_path = ts_dir / "metadata.json"
    _write_json(metadata_path, metadata)

    tc_results: dict[str, dict[str, Any]] = {}
    for tc in scenario.get("test_cases") or []:
        tc_id = tc["tc_id"]
        folder = _tc_folder_name(tc_id, ts_id)
        tc_dir = ts_dir / folder
        latest_path = tc_dir / "latest.json"

        if not tc_dir.exists():
            version_path = tc_dir / "v1.json"
            _write_json(version_path, tc)
            _write_json(latest_path, tc)
            tc_results[tc_id] = {
                "folder": folder,
                "new_version": 1,
                "version_path": version_path,
                "latest_path": latest_path,
            }
            continue

        should_check = changed_tc_ids is None or tc_id in changed_tc_ids
        if not should_check:
            tc_results[tc_id] = {
                "folder": folder,
                "new_version": None,
                "version_path": None,
                "latest_path": latest_path,
            }
            continue

        existing = _read_json(latest_path) if latest_path.exists() else None
        if existing is not None and _tc_content_equal(tc, existing):
            tc_results[tc_id] = {
                "folder": folder,
                "new_version": None,
                "version_path": None,
                "latest_path": latest_path,
            }
            continue

        next_version = _next_version(tc_dir)
        version_path = tc_dir / f"v{next_version}.json"
        _write_json(version_path, tc)
        _write_json(latest_path, tc)
        tc_results[tc_id] = {
            "folder": folder,
            "new_version": next_version,
            "version_path": version_path,
            "latest_path": latest_path,
        }

    deleted_paths: list[Path] = []
    for tc_id in deleted_tc_ids or []:
        folder = _tc_folder_name(tc_id, ts_id)
        src = ts_dir / folder
        if not src.exists():
            continue
        dest = ts_dir / "_deleted" / folder
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
        deleted_paths.append(dest)

    payload = load_scenario(ts_id, base_dir=root)
    total_ts_path = _regenerate_total_ts(base_dir=root)

    return {
        "ts_dir": ts_dir,
        "metadata_path": metadata_path,
        "tc_results": tc_results,
        "deleted_paths": deleted_paths,
        "total_ts_path": total_ts_path,
        "payload": payload,
    }


def load_scenario(ts_id: str, *, base_dir: Path | None = None) -> TestScenario | None:
    """ts_id에 해당하는 시나리오를 metadata.json + TC 폴더들로부터 재조립한다."""
    root = base_dir or _SCENARIOS_DIR
    ts_dir = root / ts_id
    metadata_path = ts_dir / "metadata.json"
    if not metadata_path.exists():
        return None

    metadata = _read_json(metadata_path)
    test_cases: list[dict] = []
    for tc_dir in sorted(p for p in ts_dir.iterdir() if p.is_dir() and p.name != "_deleted"):
        latest_path = tc_dir / "latest.json"
        if latest_path.exists():
            test_cases.append(_read_json(latest_path))
    test_cases.sort(key=lambda tc: tc.get("tc_id", ""))

    return {**metadata, "test_cases": test_cases}


def load_all_scenarios(*, base_dir: Path | None = None) -> list[TestScenario]:
    """저장된 모든 시나리오를 ts_id 순으로 반환한다 (폴더 구조에서 직접 재조립)."""
    root = base_dir or _SCENARIOS_DIR
    if not root.exists():
        return []
    result: list[TestScenario] = []
    for ts_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("TS-")):
        scenario = load_scenario(ts_dir.name, base_dir=root)
        if scenario is not None:
            result.append(scenario)
    return result


def delete_scenario(ts_id: str, *, base_dir: Path | None = None) -> bool:
    """ts_id에 해당하는 시나리오 폴더 전체를 삭제한다. 삭제 여부를 반환한다."""
    root = base_dir or _SCENARIOS_DIR
    ts_dir = root / ts_id
    if not ts_dir.exists():
        return False
    shutil.rmtree(ts_dir)
    _regenerate_total_ts(base_dir=root)
    return True


def _regenerate_total_ts(*, base_dir: Path | None = None) -> Path:
    """모든 TS의 `metadata.json`(test_cases 제외)만 모아 `total_TS.json`을 갱신한다."""
    root = base_dir or _SCENARIOS_DIR
    root.mkdir(parents=True, exist_ok=True)
    metadatas: list[dict] = []
    for ts_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("TS-")):
        metadata_path = ts_dir / "metadata.json"
        if metadata_path.exists():
            metadatas.append(_read_json(metadata_path))
    path = root / "total_TS.json"
    _write_json(path, metadatas)
    return path
