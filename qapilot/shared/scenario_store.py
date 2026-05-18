"""service scope 시나리오 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from qapilot.shared.logger import get_logger

_logger = get_logger("scenario_store")


def save_scenario(service: dict, scenario: dict) -> Path:
    """시나리오를 서비스 .qapilot/scenarios 하위에 저장한다."""
    scenarios_dir = _scenarios_dir(service)
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    path = scenarios_dir / f"{scenario['ts_id']}.json"
    path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_scenario(service: dict, ts_id: str) -> dict | None:
    """ts_id에 해당하는 시나리오를 읽는다."""
    path = _scenarios_dir(service) / f"{ts_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.error("scenario_load_failed", path=str(path), error=str(e))
        return None
    return data if isinstance(data, dict) else None


def load_all_scenarios(service: dict) -> list[dict]:
    """저장된 모든 시나리오를 ts_id 순으로 반환한다."""
    scenarios_dir = _scenarios_dir(service)
    if not scenarios_dir.exists():
        return []

    scenarios = []
    for path in sorted(scenarios_dir.glob("*.json")):
        scenario = _load_file(path)
        if scenario:
            scenarios.append(scenario)
    return sorted(scenarios, key=lambda item: item.get("ts_id", ""))


def delete_scenario(service: dict, ts_id: str) -> bool:
    """시나리오 파일을 삭제한다."""
    path = _scenarios_dir(service) / f"{ts_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def export_scenarios_csv(service: dict) -> str:
    """시나리오 목록을 CSV 문자열로 반환한다."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["ts_id", "name", "description", "trigger", "tc_count"],
    )
    writer.writeheader()
    for scenario in load_all_scenarios(service):
        writer.writerow(
            {
                "ts_id": scenario.get("ts_id", ""),
                "name": scenario.get("name", ""),
                "description": scenario.get("description", ""),
                "trigger": scenario.get("trigger", ""),
                "tc_count": len(scenario.get("test_cases", [])),
            }
        )
    return output.getvalue()


def _scenarios_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "scenarios"


def _load_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None
