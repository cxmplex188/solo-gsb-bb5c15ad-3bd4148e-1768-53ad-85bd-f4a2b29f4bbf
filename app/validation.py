"""Structural validation for policies and condition expression trees."""
from __future__ import annotations

import re
from typing import Any

from .logic import OPERATORS

POLICY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

MAX_CONDITION_DEPTH = 64
MAX_CONDITION_NODES = 512


def validate_operand(operand: Any, path: str, errors: list[str]) -> None:
    if isinstance(operand, dict):
        keys = set(operand)
        if keys == {"field"}:
            field = operand["field"]
            if not isinstance(field, str) or not field.strip():
                errors.append(f"{path}.field: must be a non-empty string")
            elif any(not part for part in field.split(".")):
                errors.append(f"{path}.field: invalid dotted path {field!r}")
        elif keys == {"const"}:
            pass  # any JSON value is a valid constant
        else:
            errors.append(
                f"{path}: operand object must be exactly {{'field': ...}} or {{'const': ...}}"
            )
    elif isinstance(operand, list):
        errors.append(f"{path}: array literals must be wrapped as {{'const': [...]}}")
    # Bare JSON scalars (string, number, boolean, null) are valid constants.


def validate_condition(
    node: Any,
    path: str,
    errors: list[str],
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_CONDITION_NODES:
        errors.append(f"{path}: condition tree exceeds {MAX_CONDITION_NODES} nodes")
        return
    if depth > MAX_CONDITION_DEPTH:
        errors.append(f"{path}: condition tree exceeds depth {MAX_CONDITION_DEPTH}")
        return

    if not isinstance(node, dict) or len(node) != 1:
        errors.append(
            f"{path}: condition must be an object with exactly one of 'all', 'any', 'not', 'cmp'"
        )
        return

    key = next(iter(node))
    value = node[key]

    if key in ("all", "any"):
        if not isinstance(value, list) or not value:
            errors.append(f"{path}.{key}: must be a non-empty array of conditions")
            return
        for i, child in enumerate(value):
            validate_condition(child, f"{path}.{key}[{i}]", errors, depth + 1, counter)
    elif key == "not":
        validate_condition(value, f"{path}.not", errors, depth + 1, counter)
    elif key == "cmp":
        if not isinstance(value, dict):
            errors.append(f"{path}.cmp: must be an object with 'op', 'left', 'right'")
            return
        op = value.get("op")
        if op not in OPERATORS:
            errors.append(f"{path}.cmp.op: must be one of {list(OPERATORS)}")
        for side in ("left", "right"):
            if side not in value:
                errors.append(f"{path}.cmp: missing operand '{side}'")
            else:
                validate_operand(value[side], f"{path}.cmp.{side}", errors)
        extra = set(value) - {"op", "left", "right"}
        if extra:
            errors.append(f"{path}.cmp: unexpected keys {sorted(extra)}")
    else:
        errors.append(f"{path}: unknown condition operator {key!r}")


def validate_policy(policy: Any) -> list[str]:
    """Validate a policy payload. Returns a list of human-readable errors."""
    errors: list[str] = []

    if not isinstance(policy, dict):
        return ["policy must be a JSON object"]

    policy_id = policy.get("policy_id")
    if not isinstance(policy_id, str) or not POLICY_ID_PATTERN.match(policy_id or ""):
        errors.append(
            "policy_id: must be 1-128 chars of letters, digits, '_', '-', '.', starting with a letter or digit"
        )

    rules = policy.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("rules: must be a non-empty array")
        return errors

    seen_rule_ids: set[str] = set()
    for i, rule in enumerate(rules):
        rpath = f"rules[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{rpath}: must be an object")
            continue

        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(f"{rpath}.rule_id: must be a non-empty string")
        elif rule_id in seen_rule_ids:
            errors.append(f"{rpath}.rule_id: duplicate rule_id {rule_id!r}")
        else:
            seen_rule_ids.add(rule_id)

        priority = rule.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            errors.append(f"{rpath}.priority: must be a number (higher = evaluated earlier)")

        if "conclusion" not in rule:
            errors.append(f"{rpath}.conclusion: is required (any JSON value)")

        if "condition" not in rule:
            errors.append(f"{rpath}.condition: is required")
        else:
            validate_condition(rule["condition"], f"{rpath}.condition", errors)

    return errors
