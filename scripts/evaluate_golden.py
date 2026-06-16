#!/usr/bin/env python3
"""Golden vs generated scenario evaluator.

Deterministic baseline evaluator for:
- requirement coverage
- scenario recall / precision
- cross-feature confusion
- value field coverage / exact / type / purpose match

This script intentionally avoids external dependencies so it can run in the
current repository as a first-pass evaluator. The "semantic" matching here is
an approximation based on normalization, token overlap, and string similarity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


DEFAULT_PAIR_THRESHOLD = 0.40
DEFAULT_PURPOSE_THRESHOLD = 0.72
DEFAULT_AMBIGUOUS_LOWER = 0.35
DEFAULT_AMBIGUOUS_UPPER = 0.70
DEFAULT_SCENARIO_THRESHOLD = 0.28
DEFAULT_SOFT_PAIR_THRESHOLD = 0.35
DEFAULT_SCENARIO_NAME_ONLY_THRESHOLD = 0.24

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("golden_evaluator")

SEMANTIC_REPLACEMENTS = {
    "법정대리인": "guardian",
    "보호자": "guardian",
    "부모": "guardian",
    "요금제": "plan",
    "플랜": "plan",
    "구독상품": "plan",
    "구독": "subscription",
    "회원": "user",
    "고객": "user",
    "이용자": "user",
    "본인": "my",
    "내": "my",
    "자신의": "my",
    "프로필": "profile",
    "내정보": "profile",
    "내 정보": "profile",
    "본인정보": "profile",
    "본인 정보": "profile",
    "계정": "account",
    "로그인": "login",
    "인증": "auth",
    "권한": "authorization",
    "가입신청": "signup",
    "가입 신청": "signup",
    "신청": "request",
    "신규가입": "signup",
    "신규 가입": "signup",
    "가입": "signup",
    "등록": "register",
    "생성": "create",
    "생성하기": "create",
    "등록하기": "register",
    "약정생성": "contract create",
    "약정 생성": "contract create",
    "약정등록": "contract create",
    "약정 등록": "contract create",
    "탈퇴": "cancel",
    "해지": "cancel",
    "취소": "cancel",
    "삭제": "delete",
    "조회": "read",
    "검색": "read",
    "반환된다": "return",
    "응답한다": "return",
    "생성된다": "create",
    "등록한다": "create",
    "등록할 때": "create",
    "요청할 때": "request",
    "요청하면": "request",
    "성공": "success",
    "실패": "failure",
    "불가": "reject",
    "거부": "reject",
    "오류": "error",
    "유효성": "validation",
    "검증": "validation",
    "미존재": "missing",
    "존재하지 않는": "missing",
    "필수": "required",
    "반드시": "required",
}

SEMANTIC_LABELS = {
    "actor": {
        "user": {"user", "가입자", "member", "customer"},
        "guardian": {"guardian", "parent"},
        "admin": {"admin", "administrator", "관리자"},
        "system": {"system", "server", "backend"},
    },
    "action": {
        "create": {"create", "register", "signup", "가입", "등록"},
        "read": {"read", "list", "fetch", "조회", "검색"},
        "update": {"update", "modify", "변경", "수정"},
        "delete": {"delete", "remove", "삭제"},
        "cancel": {"cancel", "terminate", "해지", "탈퇴", "취소"},
        "login": {"login", "signin", "로그인"},
        "logout": {"logout", "signout", "로그아웃"},
        "verify": {"verify", "validation", "검증", "확인"},
        "request": {"request", "요청"},
    },
    "object": {
        "account": {"account", "계정"},
        "user_profile": {"profile", "프로필"},
        "plan": {"plan", "pricing", "tier", "요금제"},
        "subscription": {"subscription", "구독"},
        "contract": {"contract", "약관", "동의서", "계약"},
        "family": {"family", "가족"},
        "usage": {"usage", "quota", "limit", "사용량"},
        "auth": {"auth", "token", "jwt", "인증"},
        "notice": {"notice", "공지"},
        "payment": {"payment", "billing", "결제", "청구"},
    },
    "outcome": {
        "success": {"success", "200", "201", "정상"},
        "reject": {"reject", "denied", "forbidden", "401", "403", "거부"},
        "validation_error": {"validation", "invalid", "422", "400", "유효성"},
        "not_found": {"404", "missing", "없음", "미존재"},
        "conflict": {"409", "duplicate", "중복", "충돌"},
        "error": {"500", "error", "실패", "오류"},
    },
    "condition": {
        "minor": {"minor", "underage", "미성년", "아동"},
        "existing": {"existing", "registered", "가입된", "존재하는"},
        "missing": {"missing", "absent", "미존재", "없는"},
        "invalid": {"invalid", "wrong", "incorrect", "잘못된", "틀린"},
        "expired": {"expired", "만료"},
        "required": {"required", "필수"},
    },
}

FIELD_ALIASES = {
    "planid": "plan_id",
    "plan_id": "plan_id",
    "orderid": "order_id",
    "order_id": "order_id",
    "birthdate": "birth_date",
    "birth_date": "birth_date",
    "authheader": "authorization",
    "authorization": "authorization",
    "guardiansconsent": "guardian_consent",
    "guardianconsent": "guardian_consent",
    "guardian_consent": "guardian_consent",
}

TYPE_ALIASES = {
    "str": "string",
    "string": "string",
    "text": "string",
    "varchar": "string",
    "int": "integer",
    "integer": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "float": "number",
    "double": "number",
    "decimal": "number",
    "numeric": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "timestamp": "datetime",
    "uuid": "uuid",
}

TOKEN_RE = re.compile(r"[0-9A-Za-z_]+|[가-힣]+")
CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
SPACE_RE = re.compile(r"\s+")
GENERIC_SCENARIO_NAME_RE = re.compile(
    r"\b(시나리오|기능|검증|정보|기능검증|기능 검증|상세 정보|상세정보)\b",
)


@dataclass
class CaseRef:
    scenario_id: str
    case_id: str
    name: str
    req_id: str | None
    feature_id: str | None


@dataclass
class ValueItem:
    field: str
    norm_field: str
    value: Any
    norm_value: Any
    value_class: str
    value_class_value: str
    type: str
    norm_type: str
    purpose: str
    norm_purpose: str


@dataclass
class FlatCase:
    ref: CaseRef
    scenario_name: str
    tags: list[str]
    name: str
    given: str
    when: str
    then: str
    norm_given: str
    norm_when: str
    norm_then: str
    semantic_text: str
    actor_labels: list[str]
    action_labels: list[str]
    object_labels: list[str]
    outcome_labels: list[str]
    condition_labels: list[str]
    values: list[ValueItem]


@dataclass
class AdjudicationConfig:
    enabled: bool
    model: str | None
    max_pairs: int
    config_path: Path | None
    scenario_mode: str = "ambiguous_only"


@dataclass
class ScenarioGroup:
    scenario_id: str
    scenario_name: str
    req_ids: list[str]
    case_ids: list[str]
    case_names: list[str]
    case_count: int
    feature_name_text: str
    semantic_text: str
    actor_labels: list[str]
    action_labels: list[str]
    object_labels: list[str]
    outcome_labels: list[str]
    condition_labels: list[str]
    value_fields: list[str]
    tags: list[str]


def _snake_case(text: str) -> str:
    text = CAMEL_BOUNDARY_RE.sub(r"\1_\2", text)
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"__+", "_", text)
    return text.lower().strip("_")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    normalized = text.lower()
    for source, target in SEMANTIC_REPLACEMENTS.items():
        normalized = normalized.replace(source.lower(), f" {target} ")
    normalized = normalized.replace('"', " ").replace("'", " ")
    normalized = re.sub(r"[()\[\]{}:;,.!?/\\|`~@#$%^&*+=<>-]+", " ", normalized)
    normalized = SPACE_RE.sub(" ", normalized).strip()
    return normalized


def tokenize(text: str | None) -> list[str]:
    normalized = normalize_text(text)
    return TOKEN_RE.findall(normalized)


def normalize_field(field: str | None) -> str:
    if not field:
        return ""
    base = _snake_case(str(field))
    lookup = base.replace("_", "")
    return FIELD_ALIASES.get(lookup, FIELD_ALIASES.get(base, base))


def normalize_type(type_name: str | None) -> str:
    if not type_name:
        return "unknown"
    base = _snake_case(str(type_name))
    return TYPE_ALIASES.get(base, base)


def normalize_value(value: Any) -> tuple[Any, str, str]:
    """Return normalized value, class name, and class-normalized value string."""
    if value is None:
        return None, "null", "null"

    if isinstance(value, bool):
        norm = bool(value)
        return norm, "boolean", "true" if norm else "false"

    if isinstance(value, int) and not isinstance(value, bool):
        return int(value), "integer", str(int(value))

    if isinstance(value, float):
        return float(value), "number", str(float(value))

    raw = str(value).strip()
    lowered = raw.lower()

    if lowered in {"true", "false"}:
        return lowered == "true", "boolean", lowered

    if lowered in {"1", "0"}:
        return int(lowered), "integer", lowered

    if re.fullmatch(r"-?\d+", raw):
        return int(raw), "integer", str(int(raw))

    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw), "number", str(float(raw))

    if re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", lowered):
        return lowered, "email", lowered

    if lowered.startswith("process.env."):
        return lowered, "placeholder", lowered

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw, "date", raw

    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    ):
        return lowered, "uuid", lowered

    return raw, "string", normalize_text(raw)


def normalize_tags(tags: list[str] | None) -> list[str]:
    mapping = {
        "edge": "edge_case",
        "edge_case": "edge_case",
    }
    normalized = []
    for tag in tags or []:
        key = _snake_case(str(tag))
        normalized.append(mapping.get(key, key))
    return sorted(set(normalized))


def collect_semantic_labels(texts: list[str], label_map: dict[str, set[str]]) -> list[str]:
    blob = " ".join(normalize_text(text) for text in texts if text)
    if not blob:
        return []
    labels: list[str] = []
    for label, aliases in label_map.items():
        if any(alias.lower() in blob for alias in aliases):
            labels.append(label)
    return sorted(set(labels))


def build_semantic_text(name: str, given: str, when: str, then: str, values: list[ValueItem], tags: list[str]) -> str:
    value_bits = []
    for item in values:
        value_bits.append(
            " ".join(
                bit for bit in [
                    item.norm_field,
                    item.norm_type,
                    normalize_text(str(item.norm_value)) if item.norm_value is not None else "",
                    item.norm_purpose,
                ] if bit
            )
        )
    parts = [name, given, when, then, " ".join(tags), " ".join(value_bits)]
    return normalize_text(" ".join(part for part in parts if part))


def token_f1(a: str, b: str) -> float:
    a_tokens = set(tokenize(a))
    b_tokens = set(tokenize(b))
    if not a_tokens and not b_tokens:
        return 1.0
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    if overlap == 0:
        return 0.0
    precision = overlap / len(b_tokens)
    recall = overlap / len(a_tokens)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def text_similarity(a: str, b: str) -> float:
    na = normalize_text(a)
    nb = normalize_text(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    f1 = token_f1(na, nb)
    return round(0.55 * f1 + 0.45 * seq, 4)


def label_similarity(a: list[str], b: list[str]) -> float:
    return jaccard_similarity(a, b)


def step_similarity(golden: FlatCase, generated: FlatCase) -> float:
    scores = [
        text_similarity(golden.given, generated.given),
        text_similarity(golden.when, generated.when),
        text_similarity(golden.then, generated.then),
    ]
    return round(sum(scores) / len(scores), 4)


def overlap_bonus(a: list[str], b: list[str], weight: float) -> float:
    if not a or not b:
        return 0.0
    return weight if set(a) & set(b) else 0.0


def name_similarity(golden: FlatCase, generated: FlatCase) -> float:
    return text_similarity(golden.name, generated.name)


def capability_bonus(golden: FlatCase, generated: FlatCase) -> float:
    bonus = 0.0
    if set(golden.action_labels) & set(generated.action_labels):
        bonus += 0.06
    if set(golden.object_labels) & set(generated.object_labels):
        bonus += 0.06
    if set(golden.outcome_labels) & set(generated.outcome_labels):
        bonus += 0.04
    if name_similarity(golden, generated) >= 0.45:
        bonus += 0.05
    return round(min(0.18, bonus), 4)


def jaccard_similarity(a: list[str], b: list[str]) -> float:
    sa = set(a)
    sb = set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return round(len(sa & sb) / len(sa | sb), 4)


def value_field_overlap_score(golden: FlatCase, generated: FlatCase) -> float:
    gf = {v.norm_field for v in golden.values if v.norm_field}
    rf = {v.norm_field for v in generated.values if v.norm_field}
    if not gf and not rf:
        return 1.0
    if not gf or not rf:
        return 0.0
    overlap = len(gf & rf)
    precision = overlap / len(rf)
    recall = overlap / len(gf)
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 4)


def semantic_similarity(golden: FlatCase, generated: FlatCase) -> tuple[float, dict[str, float]]:
    components = {
        "actor": label_similarity(golden.actor_labels, generated.actor_labels),
        "action": label_similarity(golden.action_labels, generated.action_labels),
        "object": label_similarity(golden.object_labels, generated.object_labels),
        "outcome": label_similarity(golden.outcome_labels, generated.outcome_labels),
        "condition": label_similarity(golden.condition_labels, generated.condition_labels),
        "text": text_similarity(golden.semantic_text, generated.semantic_text),
    }
    score = (
        0.15 * components["actor"]
        + 0.20 * components["action"]
        + 0.20 * components["object"]
        + 0.15 * components["outcome"]
        + 0.10 * components["condition"]
        + 0.20 * components["text"]
    )
    return round(score, 4), components


def pair_score(golden: FlatCase, generated: FlatCase) -> tuple[float, dict[str, float]]:
    steps_score = step_similarity(golden, generated)
    fields_score = value_field_overlap_score(golden, generated)
    tag_score = jaccard_similarity(golden.tags, generated.tags)
    semantic_score, semantic_components = semantic_similarity(golden, generated)
    name_score = name_similarity(golden, generated)
    bonus = capability_bonus(golden, generated)
    final_score = (
        0.25 * steps_score
        + 0.20 * fields_score
        + 0.05 * tag_score
        + 0.15 * name_score
        + 0.35 * semantic_score
        + bonus
    )
    components = {
        "steps": round(steps_score, 4),
        "value_fields": round(fields_score, 4),
        "tags": round(tag_score, 4),
        "name": round(name_score, 4),
        "semantic": round(semantic_score, 4),
        "semantic_actor": semantic_components["actor"],
        "semantic_action": semantic_components["action"],
        "semantic_object": semantic_components["object"],
        "semantic_outcome": semantic_components["outcome"],
        "semantic_condition": semantic_components["condition"],
        "semantic_text": semantic_components["text"],
        "capability_bonus": bonus,
    }
    return round(min(1.0, final_score), 4), components


def build_value_item(raw: dict[str, Any]) -> ValueItem:
    field = str(raw.get("field") or "")
    value = raw.get("value")
    type_name = str(raw.get("type") or "unknown")
    purpose = str(raw.get("purpose") or "")
    norm_value, value_class, value_class_value = normalize_value(value)
    return ValueItem(
        field=field,
        norm_field=normalize_field(field),
        value=value,
        norm_value=norm_value,
        value_class=value_class,
        value_class_value=value_class_value,
        type=type_name,
        norm_type=normalize_type(type_name),
        purpose=purpose,
        norm_purpose=normalize_text(purpose),
    )


def build_flat_case(
    scenario_id: str,
    scenario_name: str,
    feature_id: str | None,
    raw_case: dict[str, Any],
) -> FlatCase:
    ref = CaseRef(
        scenario_id=scenario_id,
        case_id=str(raw_case.get("tc_id") or ""),
        name=str(raw_case.get("name") or ""),
        req_id=raw_case.get("req_id"),
        feature_id=feature_id or raw_case.get("req_id"),
    )
    values = [build_value_item(v) for v in raw_case.get("values") or []]
    return FlatCase(
        ref=ref,
        scenario_name=scenario_name,
        tags=normalize_tags(raw_case.get("tags") or []),
        name=str(raw_case.get("name") or ""),
        given=str(raw_case.get("given") or ""),
        when=str(raw_case.get("when") or ""),
        then=str(raw_case.get("then") or ""),
        norm_given=normalize_text(raw_case.get("given") or ""),
        norm_when=normalize_text(raw_case.get("when") or ""),
        norm_then=normalize_text(raw_case.get("then") or ""),
        semantic_text=build_semantic_text(
            name=str(raw_case.get("name") or ""),
            given=str(raw_case.get("given") or ""),
            when=str(raw_case.get("when") or ""),
            then=str(raw_case.get("then") or ""),
            values=values,
            tags=normalize_tags(raw_case.get("tags") or []),
        ),
        actor_labels=collect_semantic_labels(
            [str(raw_case.get("name") or ""), str(raw_case.get("given") or ""), str(raw_case.get("when") or ""), str(raw_case.get("then") or "")],
            SEMANTIC_LABELS["actor"],
        ),
        action_labels=collect_semantic_labels(
            [str(raw_case.get("name") or ""), str(raw_case.get("given") or ""), str(raw_case.get("when") or ""), str(raw_case.get("then") or "")],
            SEMANTIC_LABELS["action"],
        ),
        object_labels=collect_semantic_labels(
            [str(raw_case.get("name") or ""), str(raw_case.get("given") or ""), str(raw_case.get("when") or ""), str(raw_case.get("then") or "")],
            SEMANTIC_LABELS["object"],
        ),
        outcome_labels=collect_semantic_labels(
            [str(raw_case.get("then") or "")],
            SEMANTIC_LABELS["outcome"],
        ),
        condition_labels=collect_semantic_labels(
            [str(raw_case.get("given") or ""), str(raw_case.get("when") or "")],
            SEMANTIC_LABELS["condition"],
        ),
        values=values,
    )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_golden_cases(golden_root: Path, scope: str) -> tuple[list[FlatCase], set[str], int]:
    if scope == "feature":
        root = golden_root / "scenarios-by-feature"
    elif scope == "domain":
        root = golden_root / "scenarios"
    else:
        raise ValueError(f"unsupported golden scope: {scope}")

    cases: list[FlatCase] = []
    scenario_count = 0
    req_ids: set[str] = set()
    for path in sorted(root.glob("*.json")):
        data = load_json(path)
        scenario_count += 1
        feature_id = data.get("test_cases", [{}])[0].get("req_id") if scope == "feature" else None
        for raw_case in data.get("test_cases") or []:
            case = build_flat_case(
                scenario_id=str(data.get("ts_id") or path.stem),
                scenario_name=str(data.get("name") or path.stem),
                feature_id=feature_id,
                raw_case=raw_case,
            )
            cases.append(case)
            if case.ref.req_id:
                req_ids.add(case.ref.req_id)
    return cases, req_ids, scenario_count


def load_generated_cases(generated_root: Path) -> tuple[list[FlatCase], set[str], int]:
    cases: list[FlatCase] = []
    req_ids: set[str] = set()
    scenario_count = 0

    # Backup scenario-run format:
    #   <root>/tc_generation/TS-001/latest.json
    # where each latest.json contains a whole scenario with test_cases[].
    backup_latest_paths = sorted(generated_root.glob("tc_generation/TS-*/latest.json"))
    if backup_latest_paths:
        for latest_path in backup_latest_paths:
            scenario_count += 1
            data = load_json(latest_path)
            feature_id = (data.get("requirements") or [None])[0]
            for raw_case in data.get("test_cases") or []:
                case = build_flat_case(
                    scenario_id=str(data.get("ts_id") or latest_path.parent.name),
                    scenario_name=str(data.get("name") or latest_path.parent.name),
                    feature_id=feature_id,
                    raw_case=raw_case,
                )
                cases.append(case)
                if case.ref.req_id:
                    req_ids.add(case.ref.req_id)
        return cases, req_ids, scenario_count

    for metadata_path in sorted(generated_root.glob("TS-*/metadata.json")):
        scenario_count += 1
        metadata = load_json(metadata_path)
        ts_dir = metadata_path.parent
        for tc_path in sorted(ts_dir.glob("TC-*/latest.json")):
            raw_case = load_json(tc_path)
            case = build_flat_case(
                scenario_id=str(metadata.get("ts_id") or ts_dir.name),
                scenario_name=str(metadata.get("name") or ts_dir.name),
                feature_id=(raw_case.get("req_id") or None),
                raw_case=raw_case,
            )
            cases.append(case)
            if case.ref.req_id:
                req_ids.add(case.ref.req_id)
    return cases, req_ids, scenario_count


def candidate_ok(golden: FlatCase, generated: FlatCase) -> bool:
    if value_field_overlap_score(golden, generated) > 0:
        return True
    if label_similarity(golden.action_labels, generated.action_labels) > 0 and label_similarity(golden.object_labels, generated.object_labels) > 0:
        return True
    if label_similarity(golden.action_labels, generated.action_labels) > 0 and name_similarity(golden, generated) >= 0.25:
        return True
    if step_similarity(golden, generated) >= 0.35:
        return True
    if name_similarity(golden, generated) >= 0.40:
        return True
    if text_similarity(golden.semantic_text, generated.semantic_text) >= 0.55:
        return True
    return False


def hungarian_maximize(weights: list[list[float]]) -> list[int]:
    n = len(weights)
    m = len(weights[0]) if weights else 0
    size = max(n, m)
    matrix = [[0.0] * size for _ in range(size)]
    for i in range(n):
        for j in range(m):
            matrix[i][j] = weights[i][j]

    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)

    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [math.inf] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = math.inf
            j1 = 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                cur = -(matrix[i0 - 1][j - 1]) - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * n
    for j in range(1, size + 1):
        if p[j] and p[j] <= n and j <= m:
            assignment[p[j] - 1] = j - 1
    return assignment


def safe_div(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def metric(numerator: int, denominator: int, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "score": safe_div(numerator, denominator),
        "numerator": numerator,
        "denominator": denominator,
    }
    if details is not None:
        payload["details"] = details
    return payload


def strip_code_fences(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    return candidate


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def markdown_escape(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", "<br>")


def case_ref_dict(case: FlatCase) -> dict[str, Any]:
    return {
        "scenario_id": case.ref.scenario_id,
        "case_id": case.ref.case_id,
        "name": case.ref.name,
        "req_id": case.ref.req_id,
        "feature_id": case.ref.feature_id,
    }


def compact_values(values: list[ValueItem]) -> list[dict[str, Any]]:
    return [
        {
            "field": item.norm_field or item.field,
            "type": item.norm_type,
            "value": item.norm_value,
            "purpose": item.norm_purpose or normalize_text(item.purpose),
        }
        for item in values
    ]


def build_feature_name_text(scenario_name: str, case_names: list[str]) -> str:
    parts = [scenario_name] if scenario_name else []
    parts.extend(case_names[:8])
    return normalize_text(" ".join(part for part in parts if part))


def normalize_feature_title(text: str | None) -> str:
    normalized = normalize_text(text)
    normalized = GENERIC_SCENARIO_NAME_RE.sub(" ", normalized)
    normalized = normalized.replace("my profile", "profile")
    normalized = normalized.replace("profile read", "profile")
    normalized = normalized.replace("signup request", "signup")
    normalized = normalized.replace("contract create", "contract_create")
    normalized = SPACE_RE.sub(" ", normalized).strip()
    return normalized


def build_scenario_groups(cases: list[FlatCase]) -> list[ScenarioGroup]:
    grouped: dict[str, list[FlatCase]] = {}
    for case in cases:
        grouped.setdefault(case.ref.scenario_id, []).append(case)

    groups: list[ScenarioGroup] = []
    for scenario_id, members in sorted(grouped.items()):
        scenario_name = next((case.scenario_name for case in members if case.scenario_name), scenario_id)
        req_ids = sorted({case.ref.req_id for case in members if case.ref.req_id})
        case_ids = [case.ref.case_id for case in members]
        case_names = [case.name for case in members if case.name]
        actor_labels = sorted({label for case in members for label in case.actor_labels})
        action_labels = sorted({label for case in members for label in case.action_labels})
        object_labels = sorted({label for case in members for label in case.object_labels})
        outcome_labels = sorted({label for case in members for label in case.outcome_labels})
        condition_labels = sorted({label for case in members for label in case.condition_labels})
        value_fields = sorted({value.norm_field for case in members for value in case.values if value.norm_field})
        tags = sorted({tag for case in members for tag in case.tags})
        semantic_text = normalize_text(" ".join(case.semantic_text for case in members if case.semantic_text))
        groups.append(
            ScenarioGroup(
                scenario_id=scenario_id,
                scenario_name=scenario_name,
                req_ids=req_ids,
                case_ids=case_ids,
                case_names=case_names,
                case_count=len(members),
                feature_name_text=build_feature_name_text(scenario_name, case_names),
                semantic_text=semantic_text,
                actor_labels=actor_labels,
                action_labels=action_labels,
                object_labels=object_labels,
                outcome_labels=outcome_labels,
                condition_labels=condition_labels,
                value_fields=value_fields,
                tags=tags,
            )
        )
    return groups


def numeric_similarity(a: int, b: int) -> float:
    if a <= 0 and b <= 0:
        return 1.0
    high = max(a, b)
    low = min(a, b)
    return round(low / high, 4) if high else 0.0


def scenario_value_field_similarity(golden: ScenarioGroup, generated: ScenarioGroup) -> float:
    return jaccard_similarity(golden.value_fields, generated.value_fields)


def scenario_group_score(golden: ScenarioGroup, generated: ScenarioGroup) -> tuple[float, dict[str, float]]:
    components = {
        "text": text_similarity(golden.semantic_text, generated.semantic_text),
        "actor": label_similarity(golden.actor_labels, generated.actor_labels),
        "action": label_similarity(golden.action_labels, generated.action_labels),
        "object": label_similarity(golden.object_labels, generated.object_labels),
        "outcome": label_similarity(golden.outcome_labels, generated.outcome_labels),
        "condition": label_similarity(golden.condition_labels, generated.condition_labels),
        "value_fields": scenario_value_field_similarity(golden, generated),
        "tags": jaccard_similarity(golden.tags, generated.tags),
        "case_count": numeric_similarity(golden.case_count, generated.case_count),
    }
    score = (
        0.25 * components["text"]
        + 0.10 * components["actor"]
        + 0.15 * components["action"]
        + 0.15 * components["object"]
        + 0.10 * components["outcome"]
        + 0.05 * components["condition"]
        + 0.10 * components["value_fields"]
        + 0.05 * components["tags"]
        + 0.05 * components["case_count"]
    )
    return round(score, 4), components


def scenario_name_only_score(golden: ScenarioGroup, generated: ScenarioGroup) -> tuple[float, dict[str, float]]:
    golden_title = normalize_feature_title(golden.scenario_name) or golden.feature_name_text
    generated_title = normalize_feature_title(generated.scenario_name) or generated.feature_name_text
    components = {
        "feature_name_text": text_similarity(golden.feature_name_text, generated.feature_name_text),
        "scenario_name": text_similarity(golden.scenario_name, generated.scenario_name),
        "normalized_title": text_similarity(golden_title, generated_title),
    }
    score = max(
        components["normalized_title"],
        0.65 * components["scenario_name"] + 0.35 * components["feature_name_text"],
    )
    return round(score, 4), components


def scenario_candidate_ok(golden: ScenarioGroup, generated: ScenarioGroup) -> bool:
    if label_similarity(golden.action_labels, generated.action_labels) > 0:
        return True
    if label_similarity(golden.object_labels, generated.object_labels) > 0:
        return True
    if scenario_value_field_similarity(golden, generated) > 0:
        return True
    if text_similarity(golden.semantic_text, generated.semantic_text) >= 0.20:
        return True
    return False


def collect_soft_case_evidence(
    golden_cases: list[FlatCase],
    generated_cases: list[FlatCase],
    threshold: float = DEFAULT_SOFT_PAIR_THRESHOLD,
) -> dict[str, Any]:
    if not golden_cases or not generated_cases:
        return {
            "golden_case_ids": set(),
            "generated_case_ids": set(),
            "pairs": [],
        }

    scored_pairs: list[tuple[float, FlatCase, FlatCase]] = []
    for golden in golden_cases:
        for generated in generated_cases:
            if not candidate_ok(golden, generated):
                continue
            score, _ = pair_score(golden, generated)
            if score < threshold:
                continue
            scored_pairs.append((score, golden, generated))

    scored_pairs.sort(
        key=lambda item: (
            item[0],
            name_similarity(item[1], item[2]),
            len(set(item[1].action_labels) & set(item[2].action_labels)),
            len(set(item[1].object_labels) & set(item[2].object_labels)),
        ),
        reverse=True,
    )

    evidence_pairs: list[dict[str, Any]] = []
    seen_golden: set[str] = set()
    seen_generated: set[str] = set()
    for score, golden, generated in scored_pairs:
        if golden.ref.case_id in seen_golden and generated.ref.case_id in seen_generated:
            continue
        evidence_pairs.append({
            "golden_case_id": golden.ref.case_id,
            "golden_name": golden.name,
            "generated_case_id": generated.ref.case_id,
            "generated_name": generated.name,
            "pair_score": round(score, 4),
        })
        seen_golden.add(golden.ref.case_id)
        seen_generated.add(generated.ref.case_id)

    return {
        "golden_case_ids": seen_golden,
        "generated_case_ids": seen_generated,
        "pairs": evidence_pairs,
    }


def evaluate_scenario_name_only_groups(
    golden_cases: list[FlatCase],
    generated_cases: list[FlatCase],
    threshold: float = DEFAULT_SCENARIO_NAME_ONLY_THRESHOLD,
) -> dict[str, Any]:
    golden_groups = build_scenario_groups(golden_cases)
    generated_groups = build_scenario_groups(generated_cases)

    matches: list[dict[str, Any]] = []
    matched_golden_ids: set[str] = set()
    matched_generated_ids: set[str] = set()
    for golden in golden_groups:
        best_generated: ScenarioGroup | None = None
        best_score = 0.0
        best_components: dict[str, float] = {}
        for generated in generated_groups:
            score, components = scenario_name_only_score(golden, generated)
            if score > best_score:
                best_score = score
                best_generated = generated
                best_components = components
        if best_generated is None or best_score <= 0:
            continue
        generated = best_generated
        score = best_score
        matched = score >= threshold
        if matched:
            matched_golden_ids.add(golden.scenario_id)
            matched_generated_ids.add(generated.scenario_id)
        matches.append({
            "golden_scenario_id": golden.scenario_id,
            "golden_scenario_name": golden.scenario_name,
            "generated_scenario_id": generated.scenario_id,
            "generated_scenario_name": generated.scenario_name,
            "pair_score": round(score, 4),
            "matched": matched,
            "component_scores": best_components,
        })

    unmatched_golden = [group.scenario_id for group in golden_groups if group.scenario_id not in matched_golden_ids]
    unmatched_generated = [group.scenario_id for group in generated_groups if group.scenario_id not in matched_generated_ids]
    matched_only = [m for m in matches if m["matched"]]
    matched_generated_unique = {
        m["generated_scenario_id"]
        for m in matched_only
        if m.get("generated_scenario_id")
    }
    return {
        "threshold": threshold,
        "summary": {
            "golden_scenarios": len(golden_groups),
            "generated_scenarios": len(generated_groups),
            "matched_scenarios": len(matched_only),
            "missing_scenarios": len(unmatched_golden),
            "extra_scenarios": len(unmatched_generated),
        },
        "metrics": {
            "scenario_group_recall": metric(len(matched_only), len(golden_groups)),
            "scenario_group_precision": metric(len(matched_generated_unique), len(generated_groups)),
        },
        "matches": matches,
        "unmatched": {
            "golden_scenario_ids": unmatched_golden,
            "generated_scenario_ids": unmatched_generated,
        },
    }


def evaluate_scenario_groups(
    golden_cases: list[FlatCase],
    generated_cases: list[FlatCase],
    matched_pairs: list[dict[str, Any]],
    scenario_threshold: float = DEFAULT_SCENARIO_THRESHOLD,
) -> dict[str, Any]:
    golden_groups = build_scenario_groups(golden_cases)
    generated_groups = build_scenario_groups(generated_cases)
    golden_by_scenario: dict[str, list[FlatCase]] = {}
    generated_by_scenario: dict[str, list[FlatCase]] = {}
    for case in golden_cases:
        golden_by_scenario.setdefault(case.ref.scenario_id, []).append(case)
    for case in generated_cases:
        generated_by_scenario.setdefault(case.ref.scenario_id, []).append(case)

    score_matrix = [[0.0 for _ in generated_groups] for _ in golden_groups]
    component_map: dict[tuple[int, int], dict[str, float]] = {}
    for gi, golden in enumerate(golden_groups):
        for ri, generated in enumerate(generated_groups):
            if not scenario_candidate_ok(golden, generated):
                continue
            score, components = scenario_group_score(golden, generated)
            score_matrix[gi][ri] = score
            component_map[(gi, ri)] = components

    assignment = hungarian_maximize(score_matrix) if golden_groups and generated_groups else []
    case_pairs_by_scenario: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pair in matched_pairs:
        key = (pair["golden"]["scenario_id"], pair["generated"]["scenario_id"])
        case_pairs_by_scenario.setdefault(key, []).append(pair)

    matches: list[dict[str, Any]] = []
    matched_golden_ids: set[str] = set()
    matched_generated_ids: set[str] = set()
    for gi, ri in enumerate(assignment):
        if ri < 0 or ri >= len(generated_groups):
            continue
        score = score_matrix[gi][ri]
        if score <= 0:
            continue
        golden = golden_groups[gi]
        generated = generated_groups[ri]
        soft_evidence = collect_soft_case_evidence(
            golden_by_scenario.get(golden.scenario_id, []),
            generated_by_scenario.get(generated.scenario_id, []),
        )
        pairs = case_pairs_by_scenario.get((golden.scenario_id, generated.scenario_id), [])
        within_recall = metric(len(soft_evidence["golden_case_ids"]), golden.case_count)
        within_precision = metric(len(soft_evidence["generated_case_ids"]), generated.case_count)
        strong_semantic_match = score >= max(scenario_threshold, 0.50)
        soft_coverage_match = (
            len(soft_evidence["pairs"]) >= 2
            and (
                within_recall["score"] >= 0.33
                or within_precision["score"] >= 0.40
            )
            and (
                component_map[(gi, ri)]["action"] > 0
                or component_map[(gi, ri)]["object"] > 0
            )
        )
        matched = strong_semantic_match or soft_coverage_match
        if matched:
            matched_golden_ids.add(golden.scenario_id)
            matched_generated_ids.add(generated.scenario_id)
        matches.append({
            "golden_scenario_id": golden.scenario_id,
            "generated_scenario_id": generated.scenario_id,
            "pair_score": round(score, 4),
            "matched": matched,
            "component_scores": component_map[(gi, ri)],
            "golden_case_count": golden.case_count,
            "generated_case_count": generated.case_count,
            "within_scenario_case_recall": within_recall,
            "within_scenario_case_precision": within_precision,
            "matched_case_pairs": len(pairs),
            "soft_matched_case_pairs": len(soft_evidence["pairs"]),
            "case_evidence_pairs": soft_evidence["pairs"][:8],
            "golden_case_names_sample": golden.case_names[:8],
            "generated_case_names_sample": generated.case_names[:8],
        })

    unmatched_golden = [group.scenario_id for group in golden_groups if group.scenario_id not in matched_golden_ids]
    unmatched_generated = [group.scenario_id for group in generated_groups if group.scenario_id not in matched_generated_ids]
    matched_only = [m for m in matches if m["matched"]]
    within_recall_num = sum(m["within_scenario_case_recall"]["numerator"] for m in matched_only)
    within_recall_den = sum(m["within_scenario_case_recall"]["denominator"] for m in matched_only)
    within_precision_num = sum(m["within_scenario_case_precision"]["numerator"] for m in matched_only)
    within_precision_den = sum(m["within_scenario_case_precision"]["denominator"] for m in matched_only)

    return {
        "threshold": scenario_threshold,
        "summary": {
            "golden_scenarios": len(golden_groups),
            "generated_scenarios": len(generated_groups),
            "matched_scenarios": len(matched_only),
            "missing_scenarios": len(unmatched_golden),
            "extra_scenarios": len(unmatched_generated),
            "ambiguous_scenarios": sum(
                1 for item in matches if DEFAULT_AMBIGUOUS_LOWER <= float(item["pair_score"]) < DEFAULT_AMBIGUOUS_UPPER
            ),
        },
        "metrics": {
            "scenario_group_recall": metric(len(matched_only), len(golden_groups)),
            "scenario_group_precision": metric(len(matched_only), len(generated_groups)),
            "within_scenario_case_recall": metric(within_recall_num, within_recall_den),
            "within_scenario_case_precision": metric(within_precision_num, within_precision_den),
        },
        "matches": matches,
        "unmatched": {
            "golden_scenario_ids": unmatched_golden,
            "generated_scenario_ids": unmatched_generated,
        },
        "adjudication_summary": {},
    }


def compute_value_metrics(golden: FlatCase, generated: FlatCase, purpose_threshold: float) -> dict[str, Any]:
    generated_by_field = {v.norm_field: v for v in generated.values if v.norm_field}
    matched_fields = 0
    exact_matches = 0
    type_matches = 0
    purpose_matches = 0

    golden_fields = [v for v in golden.values if v.norm_field]
    comparable = 0
    for gv in golden_fields:
        rv = generated_by_field.get(gv.norm_field)
        if rv is None:
            continue
        matched_fields += 1
        comparable += 1

        if gv.value_class == rv.value_class and gv.value_class_value == rv.value_class_value:
            exact_matches += 1
        elif gv.norm_value == rv.norm_value:
            exact_matches += 1

        if gv.norm_type == rv.norm_type:
            type_matches += 1

        purpose_score = max(
            text_similarity(gv.purpose, rv.purpose),
            text_similarity(normalize_field(gv.purpose), normalize_field(rv.purpose)),
        )
        if purpose_score >= purpose_threshold:
            purpose_matches += 1

    total_golden_fields = len(golden_fields)
    field_coverage = metric(matched_fields, total_golden_fields)
    exact_match = metric(exact_matches, comparable)
    type_match = metric(type_matches, comparable)
    purpose_match = metric(purpose_matches, comparable)
    return {
        "field_coverage": field_coverage,
        "exact_match": exact_match,
        "type_match": type_match,
        "purpose_match": purpose_match,
    }


def build_adjudication_prompts(golden: FlatCase, generated: FlatCase, pair_score_value: float) -> tuple[str, str]:
    system_prompt = """\
당신은 golden test case 와 generated test case 의 의미 동일성을 판정하는 QA evaluator 입니다.

목표:
- 두 케이스가 "같은 검증 의도"를 가지면 match=true
- 표현이 달라도 actor / action / object / condition / expected outcome 이 실질적으로 같으면 match=true
- 세부 assertion 위치, HTTP 상태 코드 생략, 메시지 표현 차이는 핵심 의도가 같으면 허용한다
- generated 케이스가 golden 케이스보다 더 구체적이거나 더 넓은 성공/실패 확인을 포함해도 match=true 가능하다
- 라벨이나 ID가 비슷해 보여도 검증 의도나 기대 결과가 명확히 다르면 match=false

판정 규칙:
1. 문자열 일치보다 의미를 우선한다.
2. positive/negative 가 뒤바뀌면 match=false.
3. expected HTTP status / validation result / business outcome 이 실질적으로 같다면 표현 차이는 허용한다.
4. field/value 는 보조 증거다. 핵심 검증 의도가 같으면 일부 값 표현 차이는 허용한다.
5. generated 가 golden 의 핵심 검증 intent 를 포함하고 있으면 match=true 로 본다.
6. borderline 이면 match=true 쪽으로 판단한다.

반드시 JSON object만 응답:
{
  "match": true,
  "confidence": 0.91,
  "reason": "짧은 한 문장",
  "matched_dimensions": ["action", "object", "outcome"],
  "mismatched_dimensions": []
}
"""
    payload = {
        "pair_score": round(pair_score_value, 4),
        "golden": {
            "ref": {
                "scenario_id": golden.ref.scenario_id,
                "case_id": golden.ref.case_id,
                "name": golden.ref.name,
            },
            "name": golden.name,
            "given": golden.given,
            "when": golden.when,
            "then": golden.then,
            "semantic": {
                "actor": golden.actor_labels,
                "action": golden.action_labels,
                "object": golden.object_labels,
                "outcome": golden.outcome_labels,
                "condition": golden.condition_labels,
            },
            "values": compact_values(golden.values),
            "tags": golden.tags,
        },
        "generated": {
            "ref": {
                "scenario_id": generated.ref.scenario_id,
                "case_id": generated.ref.case_id,
                "name": generated.ref.name,
            },
            "name": generated.name,
            "given": generated.given,
            "when": generated.when,
            "then": generated.then,
            "semantic": {
                "actor": generated.actor_labels,
                "action": generated.action_labels,
                "object": generated.object_labels,
                "outcome": generated.outcome_labels,
                "condition": generated.condition_labels,
            },
            "values": compact_values(generated.values),
            "tags": generated.tags,
        },
    }
    user_prompt = "다음 두 케이스가 의미상 동일한 테스트 케이스인지 판정하세요.\n\n" + json.dumps(
        payload, ensure_ascii=False, indent=2,
    )
    return system_prompt, user_prompt


def parse_adjudication_response(content: str) -> dict[str, Any]:
    text = strip_code_fences(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {
            "match": False,
            "confidence": 0.0,
            "reason": f"invalid_json: {content[:160]}",
            "matched_dimensions": [],
            "mismatched_dimensions": ["parse_error"],
        }
    if not isinstance(data, dict):
        return {
            "match": False,
            "confidence": 0.0,
            "reason": "invalid_shape",
            "matched_dimensions": [],
            "mismatched_dimensions": ["parse_error"],
        }
    return {
        "match": bool(data.get("match")),
        "confidence": max(0.0, min(1.0, float(data.get("confidence", 0.0) or 0.0))),
        "reason": str(data.get("reason") or ""),
        "matched_dimensions": [str(x) for x in (data.get("matched_dimensions") or []) if isinstance(x, str)],
        "mismatched_dimensions": [str(x) for x in (data.get("mismatched_dimensions") or []) if isinstance(x, str)],
    }


def build_scenario_adjudication_prompts(match: dict[str, Any]) -> tuple[str, str]:
    system_prompt = """\
당신은 golden scenario 와 generated scenario 의 의미 동일성을 판정하는 QA evaluator 입니다.

목표:
- 두 시나리오가 같은 핵심 기능 capability 를 검증하면 match=true
- 세부 TC 수가 다르거나 UI/API 관점 차이가 있어도, 실질적으로 같은 기능군을 대표하면 match=true
- generated scenario 가 golden scenario 의 핵심 성공/실패 축 중 일부만 대표적으로 포함해도 match=true 가능
- 같은 화면/같은 API/같은 업무 기능을 다루고 있으면 borderline 이라도 match=true 쪽으로 본다
- 완전히 다른 기능이거나 전혀 다른 사용자 목표를 검증할 때만 match=false

판정 규칙:
1. 시나리오 이름보다 내부 케이스 집합과 의미 분포를 우선한다.
2. actor / action / object 가 전반적으로 같고 결과가 크게 충돌하지 않으면 positive 신호다.
3. 세부 케이스 granularity 차이, UI/API 표현 차이, assertion 위치 차이는 허용한다.
4. generated scenario 에 extra case 가 있더라도 golden 의 핵심 시나리오 의도가 살아 있으면 match=true 가능하다.
5. exact equivalence 를 요구하지 말고 "이 골든 시나리오가 생성본 안에 존재한다고 볼 수 있는가"를 판단한다.
6. 확신이 아주 낮을 때만 match=false 로 둔다. borderline 이면 match=true 쪽으로 판단한다.

반드시 JSON object만 응답:
{
  "match": true,
  "confidence": 0.9,
  "reason": "짧은 한 문장",
  "matched_dimensions": ["action", "object", "coverage intent"],
  "mismatched_dimensions": []
}
"""
    payload = {
        "pair_score": match.get("pair_score"),
        "golden_scenario_id": match.get("golden_scenario_id"),
        "generated_scenario_id": match.get("generated_scenario_id"),
        "golden_case_count": match.get("golden_case_count"),
        "generated_case_count": match.get("generated_case_count"),
        "component_scores": match.get("component_scores"),
        "within_scenario_case_recall": (match.get("within_scenario_case_recall") or {}).get("score"),
        "within_scenario_case_precision": (match.get("within_scenario_case_precision") or {}).get("score"),
        "matched_case_pairs": match.get("matched_case_pairs"),
        "soft_matched_case_pairs": match.get("soft_matched_case_pairs"),
        "golden_case_names_sample": match.get("golden_case_names_sample"),
        "generated_case_names_sample": match.get("generated_case_names_sample"),
        "case_evidence_pairs": match.get("case_evidence_pairs"),
    }
    user_prompt = "다음 두 시나리오가 의미상 같은 시나리오 그룹인지 판정하세요.\n\n" + json.dumps(
        payload, ensure_ascii=False, indent=2,
    )
    return system_prompt, user_prompt


def rebuild_scenario_level_summary(scenario_level: dict[str, Any]) -> None:
    matches = scenario_level.get("matches") or []
    matched_only = [m for m in matches if m.get("matched")]
    summary = scenario_level.get("summary") or {}
    metrics = scenario_level.get("metrics") or {}
    unmatched = scenario_level.get("unmatched") or {}

    matched_golden_ids = {m.get("golden_scenario_id") for m in matched_only}
    matched_generated_ids = {m.get("generated_scenario_id") for m in matched_only}

    all_golden = {m.get("golden_scenario_id") for m in matches}
    all_generated = {m.get("generated_scenario_id") for m in matches}
    unmatched["golden_scenario_ids"] = sorted(x for x in all_golden if x and x not in matched_golden_ids)
    unmatched["generated_scenario_ids"] = sorted(x for x in all_generated if x and x not in matched_generated_ids)

    within_recall_num = sum((m.get("within_scenario_case_recall") or {}).get("numerator", 0) for m in matched_only)
    within_recall_den = sum((m.get("within_scenario_case_recall") or {}).get("denominator", 0) for m in matched_only)
    within_precision_num = sum((m.get("within_scenario_case_precision") or {}).get("numerator", 0) for m in matched_only)
    within_precision_den = sum((m.get("within_scenario_case_precision") or {}).get("denominator", 0) for m in matched_only)

    summary.update({
        "matched_scenarios": len(matched_only),
        "missing_scenarios": len(unmatched["golden_scenario_ids"]),
        "extra_scenarios": len(unmatched["generated_scenario_ids"]),
        "ambiguous_scenarios": sum(
            1 for item in matches if DEFAULT_AMBIGUOUS_LOWER <= float(item.get("pair_score") or 0.0) < DEFAULT_AMBIGUOUS_UPPER
        ),
    })
    metrics.update({
        "scenario_group_recall": metric(len(matched_only), summary.get("golden_scenarios", 0)),
        "scenario_group_precision": metric(len(matched_only), summary.get("generated_scenarios", 0)),
        "within_scenario_case_recall": metric(within_recall_num, within_recall_den),
        "within_scenario_case_precision": metric(within_precision_num, within_precision_den),
    })


async def adjudicate_ambiguous_scenarios(
    scenario_level: dict[str, Any],
    config: AdjudicationConfig,
) -> dict[str, Any]:
    if not config.enabled:
        logger.info("scenario_llm_adjudication_skipped enabled=false")
        return {
            "enabled": False,
            "attempted_pairs": 0,
            "adjudicated_pairs": 0,
            "promoted_matches": 0,
            "demoted_matches": 0,
            "llm_calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": config.model,
            "skipped_reason": "disabled",
        }

    repo_root = Path(__file__).resolve().parents[2]
    load_env_file(repo_root / ".env")
    load_env_file(repo_root / "qapilot" / ".env")
    if str(repo_root / "qapilot") not in sys.path:
        sys.path.insert(0, str(repo_root / "qapilot"))
    if not os.getenv("OPENAI_API_KEY"):
        logger.info("scenario_llm_adjudication_skipped reason=missing_openai_api_key")
        return {
            "enabled": False,
            "attempted_pairs": 0,
            "adjudicated_pairs": 0,
            "promoted_matches": 0,
            "demoted_matches": 0,
            "llm_calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": config.model,
            "skipped_reason": "missing_openai_api_key",
        }

    try:
        from qapilot.shared.config import load_config  # type: ignore
        from qapilot.shared.llm_client import LLMClient  # type: ignore
    except Exception as exc:
        logger.info("scenario_llm_adjudication_skipped reason=llm_dependencies_unavailable error=%s", str(exc))
        return {
            "enabled": False,
            "attempted_pairs": 0,
            "adjudicated_pairs": 0,
            "promoted_matches": 0,
            "demoted_matches": 0,
            "llm_calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": config.model,
            "skipped_reason": f"llm_dependencies_unavailable: {exc}",
        }

    loaded_config = load_config(config.config_path)
    llm = LLMClient(loaded_config.llm, trace_id="golden-evaluator-scenario-adjudication", default_model=config.model)
    matches = scenario_level.get("matches") or []
    if config.scenario_mode == "all_pairs":
        candidate_matches = list(matches)
    else:
        candidate_matches = [
            match for match in matches
            if DEFAULT_AMBIGUOUS_LOWER <= float(match.get("pair_score") or 0.0) < DEFAULT_AMBIGUOUS_UPPER
        ]
    ambiguous_matches = candidate_matches[:max(config.max_pairs, 0)]
    logger.info(
        "scenario_llm_adjudication_started mode=%s candidates=%s max_pairs=%s model=%s",
        config.scenario_mode,
        len(ambiguous_matches),
        config.max_pairs,
        config.model or "default",
    )

    promoted = 0
    demoted = 0
    adjudicated = 0
    for match in ambiguous_matches:
        system_prompt, user_prompt = build_scenario_adjudication_prompts(match)
        try:
            response = await llm.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=config.model,
                temperature=0.0,
                json_mode=True,
            )
            decision = parse_adjudication_response(response.content)
            adjudicated += 1
            previous = bool(match.get("matched"))
            now = bool(decision["match"])
            if not previous and now:
                promoted += 1
            if previous and not now:
                demoted += 1
            match["matched"] = now
            match["adjudication"] = {
                "used": True,
                "decision": "match" if now else "non_match",
                "reason": decision["reason"],
                "confidence": decision["confidence"],
                "matched_dimensions": decision["matched_dimensions"],
                "mismatched_dimensions": decision["mismatched_dimensions"],
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cost_usd": response.cost_usd,
            }
        except Exception as exc:
            match["adjudication"] = {
                "used": True,
                "decision": "error",
                "reason": str(exc),
                "confidence": 0.0,
                "matched_dimensions": [],
                "mismatched_dimensions": ["llm_error"],
                "model": config.model,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            }

    summary = {
        "enabled": True,
        "mode": config.scenario_mode,
        "attempted_pairs": len(ambiguous_matches),
        "adjudicated_pairs": adjudicated,
        "promoted_matches": promoted,
        "demoted_matches": demoted,
        "llm_calls": adjudicated,
        "cost_usd": round(llm.total_cost_usd, 6),
        "input_tokens": llm.total_input_tokens,
        "output_tokens": llm.total_output_tokens,
        "model": config.model or loaded_config.llm.default_model,
        "skipped_reason": None,
    }
    logger.info(
        "scenario_llm_adjudication_completed attempted_pairs=%s adjudicated_pairs=%s promoted_matches=%s demoted_matches=%s cost_usd=%s",
        summary["attempted_pairs"],
        summary["adjudicated_pairs"],
        summary["promoted_matches"],
        summary["demoted_matches"],
        summary["cost_usd"],
    )
    return summary


async def adjudicate_ambiguous_matches(
    chosen: list[dict[str, Any]],
    golden_cases: list[FlatCase],
    generated_cases: list[FlatCase],
    config: AdjudicationConfig,
) -> dict[str, Any]:
    if not config.enabled:
        logger.info("llm_adjudication_skipped enabled=false")
        return {
            "enabled": False,
            "attempted_pairs": 0,
            "adjudicated_pairs": 0,
            "promoted_matches": 0,
            "demoted_matches": 0,
            "llm_calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": config.model,
            "skipped_reason": "disabled",
        }

    repo_root = Path(__file__).resolve().parents[2]
    load_env_file(repo_root / ".env")
    load_env_file(repo_root / "qapilot" / ".env")
    if str(repo_root / "qapilot") not in sys.path:
        sys.path.insert(0, str(repo_root / "qapilot"))
    if not os.getenv("OPENAI_API_KEY"):
        logger.info("llm_adjudication_skipped reason=missing_openai_api_key")
        return {
            "enabled": False,
            "attempted_pairs": 0,
            "adjudicated_pairs": 0,
            "promoted_matches": 0,
            "demoted_matches": 0,
            "llm_calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": config.model,
            "skipped_reason": "missing_openai_api_key",
        }

    try:
        from qapilot.shared.config import load_config  # type: ignore
        from qapilot.shared.llm_client import LLMClient  # type: ignore
    except Exception as exc:
        logger.info("llm_adjudication_skipped reason=llm_dependencies_unavailable error=%s", str(exc))
        return {
            "enabled": False,
            "attempted_pairs": 0,
            "adjudicated_pairs": 0,
            "promoted_matches": 0,
            "demoted_matches": 0,
            "llm_calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": config.model,
            "skipped_reason": f"llm_dependencies_unavailable: {exc}",
        }

    loaded_config = load_config(config.config_path)

    llm = LLMClient(loaded_config.llm, trace_id="golden-evaluator-adjudication", default_model=config.model)
    ambiguous_indices = [
        idx for idx, match in enumerate(chosen)
        if match.get("diagnostics", {}).get("ambiguous_band")
    ][:max(config.max_pairs, 0)]
    logger.info(
        "llm_adjudication_started ambiguous_candidates=%s max_pairs=%s model=%s",
        len(ambiguous_indices),
        config.max_pairs,
        config.model or "default",
    )

    promoted = 0
    demoted = 0
    adjudicated = 0
    for idx in ambiguous_indices:
        match = chosen[idx]
        golden_case = next(
            case for case in golden_cases
            if case.ref.case_id == match["golden"]["case_id"]
        )
        generated_case = next(
            case for case in generated_cases
            if case.ref.case_id == match["generated"]["case_id"]
        )
        system_prompt, user_prompt = build_adjudication_prompts(
            golden_case,
            generated_case,
            float(match["pair_score"]),
        )
        try:
            response = await llm.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=config.model,
                temperature=0.0,
                json_mode=True,
            )
            decision = parse_adjudication_response(response.content)
            adjudicated += 1
            previous = bool(match["matched"])
            now = previous or bool(decision["match"])
            if not previous and now:
                promoted += 1
            match["matched"] = now
            match["confused_feature"] = False
            match["adjudication"] = {
                "used": True,
                "decision": "match" if now else "non_match",
                "reason": decision["reason"],
                "confidence": decision["confidence"],
                "matched_dimensions": decision["matched_dimensions"],
                "mismatched_dimensions": decision["mismatched_dimensions"],
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cost_usd": response.cost_usd,
            }
        except Exception as exc:
            match["adjudication"] = {
                "used": True,
                "decision": "error",
                "reason": str(exc),
                "confidence": 0.0,
                "matched_dimensions": [],
                "mismatched_dimensions": ["llm_error"],
                "model": config.model,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            }

    summary = {
        "enabled": True,
        "attempted_pairs": len(ambiguous_indices),
        "adjudicated_pairs": adjudicated,
        "promoted_matches": promoted,
        "demoted_matches": demoted,
        "llm_calls": adjudicated,
        "cost_usd": round(llm.total_cost_usd, 6),
        "input_tokens": llm.total_input_tokens,
        "output_tokens": llm.total_output_tokens,
        "model": config.model or loaded_config.llm.default_model,
        "skipped_reason": None,
    }
    logger.info(
        "llm_adjudication_completed attempted_pairs=%s adjudicated_pairs=%s promoted_matches=%s demoted_matches=%s cost_usd=%s",
        summary["attempted_pairs"],
        summary["adjudicated_pairs"],
        summary["promoted_matches"],
        summary["demoted_matches"],
        summary["cost_usd"],
    )
    return summary


def rebuild_summary_and_metrics(
    result: dict[str, Any],
    golden_cases: list[FlatCase],
    golden_req_ids: set[str],
    generated_cases: list[FlatCase],
    purpose_threshold: float,
) -> None:
    chosen = result["matches"]
    matched_pairs = [m for m in chosen if m["matched"]]
    matched_golden_case_ids = {m["golden"]["case_id"] for m in matched_pairs}
    matched_generated_case_ids = {m["generated"]["case_id"] for m in matched_pairs}
    matched_req_ids = {m["golden"]["req_id"] for m in matched_pairs if m["golden"]["req_id"]}
    confused_cases = sum(1 for m in matched_pairs if m["confused_feature"])
    ambiguous_cases = sum(1 for m in chosen if m["diagnostics"]["ambiguous_band"])

    value_field_num = 0
    value_field_den = 0
    value_exact_num = 0
    value_exact_den = 0
    value_type_num = 0
    value_type_den = 0
    value_purpose_num = 0
    value_purpose_den = 0
    for match in matched_pairs:
        vm = match["value_metrics"]
        value_field_num += vm["field_coverage"]["numerator"]
        value_field_den += vm["field_coverage"]["denominator"]
        value_exact_num += vm["exact_match"]["numerator"]
        value_exact_den += vm["exact_match"]["denominator"]
        value_type_num += vm["type_match"]["numerator"]
        value_type_den += vm["type_match"]["denominator"]
        value_purpose_num += vm["purpose_match"]["numerator"]
        value_purpose_den += vm["purpose_match"]["denominator"]

    unmatched_golden = [case_ref_dict(case) for case in golden_cases if case.ref.case_id not in matched_golden_case_ids]
    unmatched_generated = [case_ref_dict(case) for case in generated_cases if case.ref.case_id not in matched_generated_case_ids]
    missing_req_ids = sorted(golden_req_ids - matched_req_ids)
    extra_req_ids = sorted({case.ref.req_id for case in generated_cases if case.ref.req_id} - golden_req_ids)

    missing_by_req: dict[str, list[dict[str, Any]]] = {}
    for entry in unmatched_golden:
        req_id = entry.get("req_id") or "(none)"
        missing_by_req.setdefault(req_id, []).append(entry)
    extra_by_req: dict[str, list[dict[str, Any]]] = {}
    for entry in unmatched_generated:
        req_id = entry.get("req_id") or "(none)"
        extra_by_req.setdefault(req_id, []).append(entry)

    result["summary"].update({
        "matched_cases": len(matched_pairs),
        "missing_cases": len(unmatched_golden),
        "extra_cases": len(unmatched_generated),
        "missing_requirements": len(missing_req_ids),
        "extra_requirements": len(extra_req_ids),
        "ambiguous_cases": ambiguous_cases,
        "confused_cases": confused_cases,
    })
    result["metrics"].update({
        "requirement_coverage": metric(len(matched_req_ids), len(golden_req_ids)),
        "scenario_recall": metric(len(matched_pairs), len(golden_cases)),
        "scenario_precision": metric(len(matched_pairs), len(generated_cases)),
        "cross_feature_confusion": metric(confused_cases, len(matched_pairs)),
        "value_field_coverage": metric(value_field_num, value_field_den),
        "value_exact_match": metric(value_exact_num, value_exact_den),
        "value_type_match": metric(value_type_num, value_type_den),
        "value_purpose_match": metric(value_purpose_num, value_purpose_den),
    })
    result["diff"] = {
        "missing_vs_golden": {
            "requirements": missing_req_ids,
            "cases": unmatched_golden,
            "cases_by_req_id": missing_by_req,
        },
        "extra_vs_generated": {
            "requirements": extra_req_ids,
            "cases": unmatched_generated,
            "cases_by_req_id": extra_by_req,
        },
    }
    result["unmatched"] = {
        "golden_cases": unmatched_golden,
        "generated_cases": unmatched_generated,
    }


def evaluate(
    golden_cases: list[FlatCase],
    golden_req_ids: set[str],
    golden_scenarios: int,
    generated_cases: list[FlatCase],
    generated_req_ids: set[str],
    generated_scenarios: int,
    pair_threshold: float,
    purpose_threshold: float,
    ambiguous_lower: float,
    ambiguous_upper: float,
    adjudication_config: AdjudicationConfig,
) -> dict[str, Any]:
    score_matrix = [[0.0 for _ in generated_cases] for _ in golden_cases]
    component_map: dict[tuple[int, int], dict[str, float]] = {}
    for gi, golden in enumerate(golden_cases):
        for ri, generated in enumerate(generated_cases):
            if not candidate_ok(golden, generated):
                continue
            score, components = pair_score(golden, generated)
            score_matrix[gi][ri] = score
            component_map[(gi, ri)] = components

    assignment = hungarian_maximize(score_matrix) if golden_cases and generated_cases else []
    chosen: list[dict[str, Any]] = []
    used_golden: set[int] = set()
    used_generated: set[int] = set()

    for gi, ri in enumerate(assignment):
        if ri < 0 or ri >= len(generated_cases):
            continue
        score = score_matrix[gi][ri]
        if score <= 0:
            continue
        components = component_map[(gi, ri)]
        used_golden.add(gi)
        used_generated.add(ri)
        golden = golden_cases[gi]
        generated = generated_cases[ri]
        matched = score >= pair_threshold
        value_metrics = compute_value_metrics(golden, generated, purpose_threshold)
        ambiguous = ambiguous_lower <= score < ambiguous_upper
        chosen.append({
            "golden": {
                "scenario_id": golden.ref.scenario_id,
                "case_id": golden.ref.case_id,
                "name": golden.ref.name,
                "req_id": golden.ref.req_id,
                "feature_id": golden.ref.feature_id,
            },
            "generated": {
                "scenario_id": generated.ref.scenario_id,
                "case_id": generated.ref.case_id,
                "name": generated.ref.name,
                "req_id": generated.ref.req_id,
                "feature_id": generated.ref.feature_id,
            },
            "pair_score": round(score, 4),
            "matched": matched,
            "confused_feature": False,
            "component_scores": components,
            "value_metrics": value_metrics,
            "adjudication": {
                "used": False,
                "decision": "not_used",
                "reason": "deterministic baseline evaluator",
            },
            "diagnostics": {
                "ambiguous_band": ambiguous,
                "golden_tags": golden.tags,
                "generated_tags": generated.tags,
                "golden_semantic": {
                    "actor": golden.actor_labels,
                    "action": golden.action_labels,
                    "object": golden.object_labels,
                    "outcome": golden.outcome_labels,
                    "condition": golden.condition_labels,
                },
                "generated_semantic": {
                    "actor": generated.actor_labels,
                    "action": generated.action_labels,
                    "object": generated.object_labels,
                    "outcome": generated.outcome_labels,
                    "condition": generated.condition_labels,
                },
            },
        })

    result = {
        "meta": {
            "schema_version": "1.2.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": None,
            "service_id": None,
            "evaluator_version": "semantic-only-baseline-1",
        },
        "inputs": {
            "golden_root": "",
            "generated_root": "",
            "golden_scope": "feature",
            "generated_snapshot": None,
        },
        "matching_config": {
            "matching_mode": "semantic_only",
            "pair_score_threshold": pair_threshold,
            "purpose_similarity_threshold": purpose_threshold,
            "weights": {
                "steps": 0.30,
                "value_fields": 0.20,
                "tags": 0.05,
                "semantic": 0.45,
            },
            "llm_adjudication": {
                "enabled": adjudication_config.enabled,
                "model": adjudication_config.model,
                "max_pairs": adjudication_config.max_pairs,
                "lower_bound": ambiguous_lower,
                "upper_bound": ambiguous_upper,
            },
        },
        "summary": {
            "golden_scenarios": golden_scenarios,
            "golden_cases": len(golden_cases),
            "generated_scenarios": generated_scenarios,
            "generated_cases": len(generated_cases),
            "matched_cases": 0,
            "missing_cases": 0,
            "extra_cases": 0,
            "missing_requirements": 0,
            "extra_requirements": 0,
            "ambiguous_cases": 0,
            "confused_cases": 0,
        },
        "metrics": {
            "requirement_coverage": metric(0, len(golden_req_ids)),
            "scenario_recall": metric(0, len(golden_cases)),
            "scenario_precision": metric(0, len(generated_cases)),
            "cross_feature_confusion": metric(0, 0),
            "value_field_coverage": metric(0, 0),
            "value_exact_match": metric(0, 0),
            "value_type_match": metric(0, 0),
            "value_purpose_match": metric(0, 0),
        },
        "matches": chosen,
        "diff": {},
        "unmatched": {},
    }
    result["adjudication_summary"] = asyncio.run(
        adjudicate_ambiguous_matches(chosen, golden_cases, generated_cases, adjudication_config)
    )
    rebuild_summary_and_metrics(result, golden_cases, golden_req_ids, generated_cases, purpose_threshold)
    result["scenario_level"] = evaluate_scenario_groups(
        golden_cases=golden_cases,
        generated_cases=generated_cases,
        matched_pairs=[m for m in result["matches"] if m["matched"]],
    )
    result["scenario_level"]["adjudication_summary"] = asyncio.run(
        adjudicate_ambiguous_scenarios(result["scenario_level"], adjudication_config)
    )
    rebuild_scenario_level_summary(result["scenario_level"])
    result["scenario_name_only_level"] = evaluate_scenario_name_only_groups(
        golden_cases=golden_cases,
        generated_cases=generated_cases,
    )
    return result


def render_missing_extra_markdown(result: dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    metrics = result.get("metrics") or {}
    adjudication = result.get("adjudication_summary") or {}
    scenario_level = result.get("scenario_level") or {}
    scenario_summary = scenario_level.get("summary") or {}
    scenario_metrics = scenario_level.get("metrics") or {}
    scenario_adjudication = scenario_level.get("adjudication_summary") or {}
    scenario_name_only_level = result.get("scenario_name_only_level") or {}
    scenario_name_only_summary = scenario_name_only_level.get("summary") or {}
    scenario_name_only_metrics = scenario_name_only_level.get("metrics") or {}
    missing = ((result.get("diff") or {}).get("missing_vs_golden") or {})
    extra = ((result.get("diff") or {}).get("extra_vs_generated") or {})

    lines: list[str] = []
    lines.append("# Golden Diff Report")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Golden scenarios | {summary.get('golden_scenarios', 0)} |")
    lines.append(f"| Golden cases | {summary.get('golden_cases', 0)} |")
    lines.append(f"| Generated scenarios | {summary.get('generated_scenarios', 0)} |")
    lines.append(f"| Generated cases | {summary.get('generated_cases', 0)} |")
    lines.append(f"| Matched cases | {summary.get('matched_cases', 0)} |")
    lines.append(f"| Missing cases | {summary.get('missing_cases', 0)} |")
    lines.append(f"| Extra cases | {summary.get('extra_cases', 0)} |")
    lines.append(f"| Missing requirements | {summary.get('missing_requirements', 0)} |")
    lines.append(f"| Extra requirements | {summary.get('extra_requirements', 0)} |")
    if scenario_summary:
        lines.append(f"| Matched scenarios (TS-level) | {scenario_summary.get('matched_scenarios', 0)} |")
        lines.append(f"| Missing scenarios (TS-level) | {scenario_summary.get('missing_scenarios', 0)} |")
        lines.append(f"| Extra scenarios (TS-level) | {scenario_summary.get('extra_scenarios', 0)} |")
    if scenario_name_only_summary:
        lines.append(f"| Matched scenarios (Name-only TS) | {scenario_name_only_summary.get('matched_scenarios', 0)} |")
        lines.append(f"| Missing scenarios (Name-only TS) | {scenario_name_only_summary.get('missing_scenarios', 0)} |")
        lines.append(f"| Extra scenarios (Name-only TS) | {scenario_name_only_summary.get('extra_scenarios', 0)} |")
    lines.append("")
    if scenario_summary:
        lines.append("## Scenario-Level Metrics")
        lines.append("")
        lines.append("| Metric | Score | Numerator | Denominator |")
        lines.append("|---|---:|---:|---:|")
        for key in (
            "scenario_group_recall",
            "scenario_group_precision",
            "within_scenario_case_recall",
            "within_scenario_case_precision",
        ):
            item = scenario_metrics.get(key) or {}
            lines.append(
                f"| {markdown_escape(key)} | {item.get('score', 0):.6f} | "
                f"{item.get('numerator', 0)} | {item.get('denominator', 0)} |"
            )
        lines.append("")
        lines.append("## Scenario-Level Adjudication")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|---|---:|")
        lines.append(f"| Enabled | {markdown_escape(scenario_adjudication.get('enabled', False))} |")
        lines.append(f"| Mode | {markdown_escape(scenario_adjudication.get('mode') or '')} |")
        lines.append(f"| Attempted pairs | {scenario_adjudication.get('attempted_pairs', 0)} |")
        lines.append(f"| Adjudicated pairs | {scenario_adjudication.get('adjudicated_pairs', 0)} |")
        lines.append(f"| Promoted matches | {scenario_adjudication.get('promoted_matches', 0)} |")
        lines.append(f"| Demoted matches | {scenario_adjudication.get('demoted_matches', 0)} |")
        lines.append(f"| LLM calls | {scenario_adjudication.get('llm_calls', 0)} |")
        lines.append(f"| Input tokens | {scenario_adjudication.get('input_tokens', 0)} |")
        lines.append(f"| Output tokens | {scenario_adjudication.get('output_tokens', 0)} |")
        lines.append(f"| Cost USD | {scenario_adjudication.get('cost_usd', 0.0)} |")
        lines.append(f"| Model | {markdown_escape(scenario_adjudication.get('model') or '')} |")
        lines.append(f"| Skipped reason | {markdown_escape(scenario_adjudication.get('skipped_reason') or '')} |")
        lines.append("")
        lines.append("## Scenario-Level Matches")
        lines.append("")
        lines.append("| Golden Scenario | Generated Scenario | Score | Matched | Golden Cases | Generated Cases | Within Recall | Within Precision | Adjudication |")
        lines.append("|---|---|---:|---|---:|---:|---:|---:|---|")
        for item in scenario_level.get("matches") or []:
            adjud = item.get("adjudication") or {}
            lines.append(
                f"| {markdown_escape(item.get('golden_scenario_id') or '')} | "
                f"{markdown_escape(item.get('generated_scenario_id') or '')} | "
                f"{float(item.get('pair_score') or 0.0):.4f} | "
                f"{markdown_escape(item.get('matched') or False)} | "
                f"{item.get('golden_case_count', 0)} | "
                f"{item.get('generated_case_count', 0)} | "
                f"{float(((item.get('within_scenario_case_recall') or {}).get('score') or 0.0)):.4f} | "
                f"{float(((item.get('within_scenario_case_precision') or {}).get('score') or 0.0)):.4f} | "
                f"{markdown_escape(adjud.get('decision') or '')} |"
            )
        lines.append("")
    if scenario_name_only_summary:
        lines.append("## Scenario Name-Only Metrics")
        lines.append("")
        lines.append("| Metric | Score | Numerator | Denominator |")
        lines.append("|---|---:|---:|---:|")
        for key in (
            "scenario_group_recall",
            "scenario_group_precision",
        ):
            item = scenario_name_only_metrics.get(key) or {}
            lines.append(
                f"| {markdown_escape(key)} | {item.get('score', 0):.6f} | "
                f"{item.get('numerator', 0)} | {item.get('denominator', 0)} |"
            )
        lines.append("")
        lines.append("## Scenario Name-Only Matches")
        lines.append("")
        lines.append("| Golden Scenario | Golden Name | Generated Scenario | Generated Name | Score | Matched |")
        lines.append("|---|---|---|---|---:|---|")
        for item in scenario_name_only_level.get("matches") or []:
            lines.append(
                f"| {markdown_escape(item.get('golden_scenario_id') or '')} | "
                f"{markdown_escape(item.get('golden_scenario_name') or '')} | "
                f"{markdown_escape(item.get('generated_scenario_id') or '')} | "
                f"{markdown_escape(item.get('generated_scenario_name') or '')} | "
                f"{float(item.get('pair_score') or 0.0):.4f} | "
                f"{markdown_escape(item.get('matched') or False)} |"
            )
        lines.append("")
    lines.append("## Core Metrics")
    lines.append("")
    lines.append("| Metric | Score | Numerator | Denominator |")
    lines.append("|---|---:|---:|---:|")
    for key in (
        "requirement_coverage",
        "scenario_recall",
        "scenario_precision",
        "cross_feature_confusion",
        "value_field_coverage",
        "value_exact_match",
        "value_type_match",
        "value_purpose_match",
    ):
        item = metrics.get(key) or {}
        lines.append(
            f"| {markdown_escape(key)} | {item.get('score', 0):.6f} | "
            f"{item.get('numerator', 0)} | {item.get('denominator', 0)} |"
        )

    lines.append("")
    lines.append("## Ambiguous Adjudication")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Enabled | {markdown_escape(adjudication.get('enabled', False))} |")
    lines.append(f"| Attempted pairs | {adjudication.get('attempted_pairs', 0)} |")
    lines.append(f"| Adjudicated pairs | {adjudication.get('adjudicated_pairs', 0)} |")
    lines.append(f"| Promoted matches | {adjudication.get('promoted_matches', 0)} |")
    lines.append(f"| Demoted matches | {adjudication.get('demoted_matches', 0)} |")
    lines.append(f"| LLM calls | {adjudication.get('llm_calls', 0)} |")
    lines.append(f"| Input tokens | {adjudication.get('input_tokens', 0)} |")
    lines.append(f"| Output tokens | {adjudication.get('output_tokens', 0)} |")
    lines.append(f"| Cost USD | {adjudication.get('cost_usd', 0.0)} |")
    lines.append(f"| Model | {markdown_escape(adjudication.get('model') or '')} |")
    lines.append(f"| Skipped reason | {markdown_escape(adjudication.get('skipped_reason') or '')} |")

    adjudicated_matches = [
        match for match in (result.get("matches") or [])
        if (match.get("adjudication") or {}).get("used")
    ]
    lines.append("")
    lines.append("## Adjudicated Pairs")
    lines.append("")
    if adjudicated_matches:
        lines.append("| Golden Case | Generated Case | Pair Score | Decision | Confidence | Reason |")
        lines.append("|---|---|---:|---|---:|---|")
        for match in adjudicated_matches:
            adjud = match.get("adjudication") or {}
            golden_label = f"{match['golden'].get('case_id') or ''} {match['golden'].get('name') or ''}".strip()
            generated_label = f"{match['generated'].get('case_id') or ''} {match['generated'].get('name') or ''}".strip()
            lines.append(
                f"| {markdown_escape(golden_label)} | "
                f"{markdown_escape(generated_label)} | "
                f"{match.get('pair_score', 0):.4f} | "
                f"{markdown_escape(adjud.get('decision') or '')} | "
                f"{float(adjud.get('confidence') or 0.0):.2f} | "
                f"{markdown_escape(adjud.get('reason') or '')} |"
            )
        lines.append("")
        lines.append("### Adjudicated Pair Details")
        lines.append("")
        for match in adjudicated_matches:
            adjud = match.get("adjudication") or {}
            lines.append(
                f"### `{markdown_escape(match['golden'].get('case_id') or '')}` vs "
                f"`{markdown_escape(match['generated'].get('case_id') or '')}`"
            )
            lines.append("")
            lines.append(f"- Decision: `{markdown_escape(adjud.get('decision') or '')}`")
            lines.append(f"- Confidence: `{float(adjud.get('confidence') or 0.0):.2f}`")
            lines.append(f"- Pair score: `{float(match.get('pair_score') or 0.0):.4f}`")
            lines.append(f"- Reason: {markdown_escape(adjud.get('reason') or '')}")
            lines.append(f"- Matched dimensions: {markdown_escape(', '.join(adjud.get('matched_dimensions') or []))}")
            lines.append(f"- Mismatched dimensions: {markdown_escape(', '.join(adjud.get('mismatched_dimensions') or []))}")
            lines.append(f"- Golden: `{markdown_escape(match['golden'].get('case_id') or '')}` {markdown_escape(match['golden'].get('name') or '')}")
            lines.append(f"- Generated: `{markdown_escape(match['generated'].get('case_id') or '')}` {markdown_escape(match['generated'].get('name') or '')}")
            lines.append("")
    else:
        lines.append("- 없음")

    missing_requirements = missing.get("requirements") or []
    extra_requirements = extra.get("requirements") or []
    missing_cases = missing.get("cases") or []
    extra_cases = extra.get("cases") or []

    lines.append("")
    lines.append("## Missing Requirements")
    lines.append("")
    if missing_requirements:
        for req_id in missing_requirements:
            lines.append(f"- `{req_id}`")
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## Extra Requirements")
    lines.append("")
    if extra_requirements:
        for req_id in extra_requirements:
            lines.append(f"- `{req_id}`")
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## Missing Cases vs Golden")
    lines.append("")
    lines.append("| Req ID | Scenario ID | Case ID | Name |")
    lines.append("|---|---|---|---|")
    if missing_cases:
        for case in missing_cases:
            lines.append(
                f"| {markdown_escape(case.get('req_id') or '')} | "
                f"{markdown_escape(case.get('scenario_id') or '')} | "
                f"{markdown_escape(case.get('case_id') or '')} | "
                f"{markdown_escape(case.get('name') or '')} |"
            )
    else:
        lines.append("|  |  |  | 없음 |")

    lines.append("")
    lines.append("## Extra Cases vs Generated")
    lines.append("")
    lines.append("| Req ID | Scenario ID | Case ID | Name |")
    lines.append("|---|---|---|---|")
    if extra_cases:
        for case in extra_cases:
            lines.append(
                f"| {markdown_escape(case.get('req_id') or '')} | "
                f"{markdown_escape(case.get('scenario_id') or '')} | "
                f"{markdown_escape(case.get('case_id') or '')} | "
                f"{markdown_escape(case.get('name') or '')} |"
            )
    else:
        lines.append("|  |  |  | 없음 |")

    lines.append("")
    lines.append("## Missing Cases by Requirement")
    lines.append("")
    missing_by_req = missing.get("cases_by_req_id") or {}
    if missing_by_req:
        for req_id in sorted(missing_by_req):
            lines.append(f"### `{req_id}`")
            lines.append("")
            lines.append("| Scenario ID | Case ID | Name |")
            lines.append("|---|---|---|")
            for case in missing_by_req.get(req_id) or []:
                lines.append(
                    f"| {markdown_escape(case.get('scenario_id') or '')} | "
                    f"{markdown_escape(case.get('case_id') or '')} | "
                    f"{markdown_escape(case.get('name') or '')} |"
                )
            lines.append("")
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## Extra Cases by Requirement")
    lines.append("")
    extra_by_req = extra.get("cases_by_req_id") or {}
    if extra_by_req:
        for req_id in sorted(extra_by_req):
            lines.append(f"### `{req_id}`")
            lines.append("")
            lines.append("| Scenario ID | Case ID | Name |")
            lines.append("|---|---|---|")
            for case in extra_by_req.get(req_id) or []:
                lines.append(
                    f"| {markdown_escape(case.get('scenario_id') or '')} | "
                    f"{markdown_escape(case.get('case_id') or '')} | "
                    f"{markdown_escape(case.get('name') or '')} |"
                )
            lines.append("")
    else:
        lines.append("- 없음")

    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate golden vs generated scenarios.")
    parser.add_argument(
        "--golden-root",
        type=Path,
        default=Path("system-under-test/golden"),
        help="Path to golden root directory.",
    )
    parser.add_argument(
        "--generated-root",
        type=Path,
        default=Path("qapilot/.qapilot/scenarios"),
        help="Path to generated scenario root directory.",
    )
    parser.add_argument(
        "--golden-scope",
        choices=["feature", "domain"],
        default="feature",
        help="Which golden grouping to load.",
    )
    parser.add_argument(
        "--pair-threshold",
        type=float,
        default=DEFAULT_PAIR_THRESHOLD,
        help="Match threshold for pair_score.",
    )
    parser.add_argument(
        "--purpose-threshold",
        type=float,
        default=DEFAULT_PURPOSE_THRESHOLD,
        help="Threshold for purpose semantic match.",
    )
    parser.add_argument(
        "--ambiguous-lower",
        type=float,
        default=DEFAULT_AMBIGUOUS_LOWER,
        help="Lower bound of ambiguous band.",
    )
    parser.add_argument(
        "--ambiguous-upper",
        type=float,
        default=DEFAULT_AMBIGUOUS_UPPER,
        help="Upper bound of ambiguous band.",
    )
    parser.add_argument(
        "--llm-adjudication",
        action="store_true",
        help="Use LLM to adjudicate ambiguous pairs only.",
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        default=None,
        help="Optional model override for ambiguous adjudication.",
    )
    parser.add_argument(
        "--llm-max-pairs",
        type=int,
        default=50,
        help="Maximum number of ambiguous pairs to adjudicate.",
    )
    parser.add_argument(
        "--scenario-llm-all",
        action="store_true",
        help="At TS-level, adjudicate all scenario pairs instead of ambiguous-only.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional qapilot config path for loading LLM settings.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output JSON file path.",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=None,
        help="Optional output Markdown report path for missing/extra diff.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    golden_cases, golden_req_ids, golden_scenarios = load_golden_cases(args.golden_root, args.golden_scope)
    generated_cases, generated_req_ids, generated_scenarios = load_generated_cases(args.generated_root)
    del generated_req_ids  # reserved for future diagnostics

    result = evaluate(
        golden_cases=golden_cases,
        golden_req_ids=golden_req_ids,
        golden_scenarios=golden_scenarios,
        generated_cases=generated_cases,
        generated_req_ids=set(),
        generated_scenarios=generated_scenarios,
        pair_threshold=args.pair_threshold,
        purpose_threshold=args.purpose_threshold,
        ambiguous_lower=args.ambiguous_lower,
        ambiguous_upper=args.ambiguous_upper,
        adjudication_config=AdjudicationConfig(
            enabled=args.llm_adjudication,
            model=args.llm_model,
            max_pairs=args.llm_max_pairs,
            config_path=args.config,
            scenario_mode="all_pairs" if args.scenario_llm_all else "ambiguous_only",
        ),
    )
    result["inputs"]["golden_root"] = str(args.golden_root)
    result["inputs"]["generated_root"] = str(args.generated_root)
    result["inputs"]["golden_scope"] = args.golden_scope

    output = json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.output:
        args.output.write_text(output + ("\n" if not output.endswith("\n") else ""), encoding="utf-8")
    else:
        print(output)
    if args.markdown_output:
        args.markdown_output.write_text(render_missing_extra_markdown(result), encoding="utf-8")
    logger.info(
        "evaluation_completed matched_cases=%s missing_cases=%s extra_cases=%s output=%s markdown_output=%s",
        result["summary"].get("matched_cases", 0),
        result["summary"].get("missing_cases", 0),
        result["summary"].get("extra_cases", 0),
        str(args.output) if args.output else "",
        str(args.markdown_output) if args.markdown_output else "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
