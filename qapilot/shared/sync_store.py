"""CLI sync 산출물 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qapilot.shared.logger import get_logger

_logger = get_logger("sync_store")


def sync_scenarios(service: dict, items: list[dict]) -> dict:
    """시나리오 목록을 서비스 .qapilot/scenarios 하위에 저장한다.

    Args:
        service: service_store에서 조회한 서비스 dict.
        items: TestScenario dict 목록.

    Returns:
        동기화 성공/실패 수와 저장 경로 목록.
    """
    base_dir = _qapilot_dir(service) / "scenarios"
    return _sync_json_items(
        items=items,
        base_dir=base_dir,
        id_key="ts_id",
        value_getter=lambda item: item,
    )


def sync_generated_code(service: dict, items: list[dict]) -> dict:
    """생성된 Playwright 코드를 서비스 .qapilot/generated-code 하위에 저장한다."""
    base_dir = _qapilot_dir(service) / "generated-code"
    synced = 0
    failed = 0
    paths: list[str] = []

    for item in items:
        try:
            tc_id = _required_str(item, "tc_id")
            code = _required_str(item, "code")
            path = base_dir / f"{tc_id}.js"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(code, encoding="utf-8")
            paths.append(str(path))
            synced += 1
        except Exception as e:
            failed += 1
            _logger.error("generated_code_sync_failed", item=item, error=str(e))

    return {"synced": synced, "failed": failed, "paths": paths}


def sync_results(service: dict, items: list[dict]) -> dict:
    """테스트 결과 목록을 서비스 .qapilot/results 하위에 저장한다."""
    base_dir = _qapilot_dir(service) / "results"
    return _sync_json_items(
        items=items,
        base_dir=base_dir,
        id_key="trace_id",
        value_getter=lambda item: item.get("data", {}),
    )


def _sync_json_items(
    items: list[dict],
    base_dir: Path,
    id_key: str,
    value_getter,
) -> dict:
    synced = 0
    failed = 0
    paths: list[str] = []

    for item in items:
        try:
            item_id = _required_str(item, id_key)
            path = base_dir / f"{item_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(value_getter(item), ensure_ascii=False, indent=2)
            path.write_text(content, encoding="utf-8")
            paths.append(str(path))
            synced += 1
        except Exception as e:
            failed += 1
            _logger.error("json_item_sync_failed", id_key=id_key, item=item, error=str(e))

    return {"synced": synced, "failed": failed, "paths": paths}


def _qapilot_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"]))


def _required_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if value in (None, ""):
        raise ValueError(f"{key} is required")
    return str(value)
