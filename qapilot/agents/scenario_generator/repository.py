"""시나리오 Repository — 파일 I/O.

.qapilot/scenarios/{ts_id}.json 읽기/쓰기를 담당한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from qapilot.shared.schemas import TestScenario

_SCENARIOS_DIR = Path(".qapilot/scenarios")


def save_scenarios(scenarios: list[TestScenario]) -> None:
    """각 시나리오를 .qapilot/scenarios/{ts_id}.json에 저장한다."""
    _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        save_scenario(scenario)


def save_scenario(scenario: TestScenario) -> Path:
    """단일 시나리오를 .qapilot/scenarios/{ts_id}.json에 저장한다."""
    _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    path = _SCENARIOS_DIR / f"{scenario['ts_id']}.json"
    path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_scenario(ts_id: str) -> TestScenario | None:
    """ts_id에 해당하는 시나리오를 로드한다. 파일이 없으면 None을 반환한다."""
    path = _SCENARIOS_DIR / f"{ts_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_scenarios() -> list[TestScenario]:
    """저장된 모든 시나리오를 ts_id 순으로 반환한다."""
    if not _SCENARIOS_DIR.exists():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(_SCENARIOS_DIR.glob("TS-*.json"))
    ]


def delete_scenario(ts_id: str) -> bool:
    """ts_id에 해당하는 시나리오 파일을 삭제한다. 삭제 여부를 반환한다."""
    path = _SCENARIOS_DIR / f"{ts_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True
