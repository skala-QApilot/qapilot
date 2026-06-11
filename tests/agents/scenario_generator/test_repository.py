"""scenario_generator.repository 단위 테스트.

TS/TC 폴더+버전 구조 (scenarios/TS-XXX/{metadata.json, TC-XX/{latest,vN}.json})에
대한 save/load 라운드트립, 버전 증가/미증가, TC 삭제, total_TS.json 재생성을 검증한다.
"""

from __future__ import annotations

import json

import pytest

from qapilot.agents.scenario_generator import repository


def _sample_ts(ts_id: str = "TS-001") -> dict:
    return {
        "ts_id": ts_id,
        "name": "회원가입 시나리오",
        "description": "회원가입 정상/예외 흐름",
        "trigger": "init",
        "affected_files": ["app/routers/auth.py"],
        "domain_rules_used": ["rule-1"],
        "requirements": ["FR-AUTH-01"],
        "depends_on": [],
        "test_cases": [
            {
                "tc_id": f"{ts_id}-TC-01",
                "name": "정상 회원가입",
                "given": "유효한 입력",
                "when": "회원가입 요청",
                "then": "201 반환",
                "values": [],
                "tags": ["normal"],
                "req_id": "FR-AUTH-01",
                "api": "POST /api/auth/signup",
                "depends_on": [],
            },
            {
                "tc_id": f"{ts_id}-TC-02",
                "name": "중복 이메일",
                "given": "이미 가입된 이메일",
                "when": "회원가입 요청",
                "then": "409 반환",
                "values": [],
                "tags": ["exception"],
                "req_id": "FR-AUTH-01",
                "api": "POST /api/auth/signup",
                "depends_on": [],
            },
        ],
    }


def test_save_and_load_roundtrip(tmp_path):
    ts = _sample_ts()
    result = repository.save_scenario(ts, base_dir=tmp_path)

    assert result["ts_dir"] == tmp_path / "TS-001"
    assert (tmp_path / "TS-001" / "metadata.json").exists()
    assert (tmp_path / "TS-001" / "TC-01" / "v1.json").exists()
    assert (tmp_path / "TS-001" / "TC-01" / "latest.json").exists()
    assert (tmp_path / "TS-001" / "TC-02" / "v1.json").exists()
    assert (tmp_path / "total_TS.json").exists()

    loaded = repository.load_scenario("TS-001", base_dir=tmp_path)
    assert loaded["ts_id"] == "TS-001"
    assert loaded["name"] == ts["name"]
    assert "last_modified_at" in loaded
    assert [tc["tc_id"] for tc in loaded["test_cases"]] == [
        "TS-001-TC-01",
        "TS-001-TC-02",
    ]

    # metadata.json 에는 test_cases 가 없어야 한다
    metadata = json.loads((tmp_path / "TS-001" / "metadata.json").read_text(encoding="utf-8"))
    assert "test_cases" not in metadata


def test_unchanged_tc_does_not_create_new_version(tmp_path):
    ts = _sample_ts()
    repository.save_scenario(ts, base_dir=tmp_path)

    # 동일 내용으로 재저장 — changed_tc_ids=None (전체 검사)이지만 내용 동일하므로 skip
    ts2 = _sample_ts()
    result = repository.save_scenario(ts2, base_dir=tmp_path)

    tc1_dir = tmp_path / "TS-001" / "TC-01"
    versions = sorted(tc1_dir.glob("v*.json"))
    assert [p.name for p in versions] == ["v1.json"]
    assert result["tc_results"]["TS-001-TC-01"]["new_version"] is None


def test_changed_tc_creates_new_version(tmp_path):
    ts = _sample_ts()
    repository.save_scenario(ts, base_dir=tmp_path)

    ts2 = _sample_ts()
    ts2["test_cases"][0]["then"] = "201 반환 + 환영 이메일 발송"
    result = repository.save_scenario(
        ts2, changed_tc_ids={"TS-001-TC-01"}, base_dir=tmp_path
    )

    tc1_dir = tmp_path / "TS-001" / "TC-01"
    versions = sorted(p.name for p in tc1_dir.glob("v*.json"))
    assert versions == ["v1.json", "v2.json"]
    assert result["tc_results"]["TS-001-TC-01"]["new_version"] == 2

    latest = json.loads((tc1_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["then"] == "201 반환 + 환영 이메일 발송"

    # TC-02 는 changed_tc_ids 에 없으므로 그대로
    tc2_dir = tmp_path / "TS-001" / "TC-02"
    assert sorted(p.name for p in tc2_dir.glob("v*.json")) == ["v1.json"]


def test_provenance_preserved_when_unchanged(tmp_path):
    ts = _sample_ts()
    ts["test_cases"][0]["provenance"] = {
        "trace_id": "trace-1",
        "generated_at": "2026-06-01T00:00:00Z",
        "source": "init",
        "analysis": None,
        "metrics": None,
    }
    repository.save_scenario(ts, base_dir=tmp_path)

    # 다음 저장에서 provenance 가 다른 값으로 채워져도, 내용(provenance 제외)이
    # 동일하면 새 버전을 만들지 않고 기존 provenance 가 유지된다.
    ts2 = _sample_ts()
    ts2["test_cases"][0]["provenance"] = {
        "trace_id": "trace-2",
        "generated_at": "2026-06-02T00:00:00Z",
        "source": "natural_lang",
        "analysis": None,
        "metrics": None,
    }
    repository.save_scenario(ts2, base_dir=tmp_path)

    latest = json.loads(
        (tmp_path / "TS-001" / "TC-01" / "latest.json").read_text(encoding="utf-8")
    )
    assert latest["provenance"]["trace_id"] == "trace-1"


def test_deleted_tc_moved_to_deleted_folder(tmp_path):
    ts = _sample_ts()
    repository.save_scenario(ts, base_dir=tmp_path)

    ts2 = _sample_ts()
    ts2["test_cases"] = [ts2["test_cases"][0]]  # TC-02 제거
    result = repository.save_scenario(
        ts2, deleted_tc_ids=["TS-001-TC-02"], base_dir=tmp_path
    )

    assert not (tmp_path / "TS-001" / "TC-02").exists()
    assert (tmp_path / "TS-001" / "_deleted" / "TC-02" / "latest.json").exists()
    assert len(result["deleted_paths"]) == 1

    loaded = repository.load_scenario("TS-001", base_dir=tmp_path)
    assert [tc["tc_id"] for tc in loaded["test_cases"]] == ["TS-001-TC-01"]


def test_load_all_scenarios_and_total_ts(tmp_path):
    repository.save_scenario(_sample_ts("TS-001"), base_dir=tmp_path)
    repository.save_scenario(_sample_ts("TS-002"), base_dir=tmp_path)

    all_scenarios = repository.load_all_scenarios(base_dir=tmp_path)
    assert [s["ts_id"] for s in all_scenarios] == ["TS-001", "TS-002"]

    total = json.loads((tmp_path / "total_TS.json").read_text(encoding="utf-8"))
    assert [s["ts_id"] for s in total] == ["TS-001", "TS-002"]
    # total_TS.json은 metadata.json만 모은 개요이므로 test_cases를 포함하지 않는다
    assert all("test_cases" not in s for s in total)


def test_tc_generation_context_roundtrip(tmp_path):
    ts = _sample_ts()
    ts["tc_generation_context"] = {
        "query": "회원가입 API 명세",
        "retrieved_docs": [
            {"source": "API명세서.md", "section": "회원가입", "content": "POST /api/auth/signup ...", "score": 0.61},
        ],
        "codebase_refs": [
            {"file": "app/routers/auth.py", "line_start": 10, "line_end": 25, "content": "def signup(...): ..."},
        ],
    }
    repository.save_scenario(ts, base_dir=tmp_path)

    metadata = json.loads((tmp_path / "TS-001" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["tc_generation_context"]["query"] == "회원가입 API 명세"
    assert metadata["tc_generation_context"]["retrieved_docs"][0]["source"] == "API명세서.md"
    assert metadata["tc_generation_context"]["codebase_refs"][0]["file"] == "app/routers/auth.py"

    loaded = repository.load_scenario("TS-001", base_dir=tmp_path)
    assert loaded["tc_generation_context"] == ts["tc_generation_context"]

    total = json.loads((tmp_path / "total_TS.json").read_text(encoding="utf-8"))
    assert total[0]["tc_generation_context"]["query"] == "회원가입 API 명세"


def test_delete_scenario(tmp_path):
    repository.save_scenario(_sample_ts("TS-001"), base_dir=tmp_path)
    repository.save_scenario(_sample_ts("TS-002"), base_dir=tmp_path)

    assert repository.delete_scenario("TS-001", base_dir=tmp_path) is True
    assert not (tmp_path / "TS-001").exists()

    all_scenarios = repository.load_all_scenarios(base_dir=tmp_path)
    assert [s["ts_id"] for s in all_scenarios] == ["TS-002"]

    total = json.loads((tmp_path / "total_TS.json").read_text(encoding="utf-8"))
    assert [s["ts_id"] for s in total] == ["TS-002"]

    assert repository.delete_scenario("TS-999", base_dir=tmp_path) is False


def test_load_scenario_missing_returns_none(tmp_path):
    assert repository.load_scenario("TS-999", base_dir=tmp_path) is None


def test_load_all_scenarios_empty_dir(tmp_path):
    assert repository.load_all_scenarios(base_dir=tmp_path / "nonexistent") == []
