"""Three-valued rule evaluation, explanation traces and minimal missing-field analysis.

Truth values are the strings "true", "false" and "unknown" (Kleene-style
three-valued logic):

* a comparison is ``unknown`` when any field it references is missing from the
  facts (or cannot be resolved);
* ``not`` flips true/false and preserves unknown;
* ``all`` is false if any child is false, else unknown if any child is unknown,
  else true;
* ``any`` is true if any child is true, else unknown if any child is unknown,
  else false.

Evaluation is monotonic: adding more facts can only turn ``unknown`` nodes
into definite ones, never flip a definite value.
"""
from __future__ import annotations

import json
from itertools import product
from typing import Any

TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"

OPERATORS = ("==", "!=", "<", "<=", ">", ">=", "in")

# Caps that keep the minimal-missing-set computation bounded on adversarial
# policies. When a cap is hit the result is still sound (every returned set is
# inclusion-minimal and sufficient) but may not be exhaustive.
MAX_SETS_PER_NODE = 64
MAX_COMBOS = 512

_MISSING = object()  # sentinel for "field not present in facts"


# ---------------------------------------------------------------------------
# Tri-valued boolean algebra
# ---------------------------------------------------------------------------

def negate(value: str) -> str:
    if value == TRUE:
        return FALSE
    if value == FALSE:
        return TRUE
    return UNKNOWN


def tri_and(values) -> str:
    vals = list(values)
    if any(v == FALSE for v in vals):
        return FALSE
    if any(v == UNKNOWN for v in vals):
        return UNKNOWN
    return TRUE


def tri_or(values) -> str:
    vals = list(values)
    if any(v == TRUE for v in vals):
        return TRUE
    if any(v == UNKNOWN for v in vals):
        return UNKNOWN
    return FALSE


# ---------------------------------------------------------------------------
# Fact lookup
# ---------------------------------------------------------------------------

def resolve_path(facts: Any, path: str) -> Any:
    """Resolve a dotted path like ``applicant.income`` against the facts."""
    current = facts
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


# ---------------------------------------------------------------------------
# Condition evaluation with per-node trace
# ---------------------------------------------------------------------------

def eval_operand(operand: Any, facts: dict) -> tuple[Any, set, dict]:
    """Evaluate an operand. Returns (value, missing_paths, trace)."""
    if isinstance(operand, dict) and set(operand) == {"field"}:
        path = operand["field"]
        value = resolve_path(facts, path)
        if value is _MISSING:
            return _MISSING, {path}, {"node": "field", "path": path, "resolved": False}
        return value, set(), {
            "node": "field",
            "path": path,
            "resolved": True,
            "value": value,
        }
    if isinstance(operand, dict) and set(operand) == {"const"}:
        return operand["const"], set(), {"node": "const", "value": operand["const"]}
    # Bare JSON scalar literal.
    return operand, set(), {"node": "const", "value": operand}


def apply_operator(op: str, left: Any, right: Any) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "in":
        return left in right
    raise ValueError(f"unsupported operator: {op}")


def eval_condition(node: dict, facts: dict) -> tuple[str, dict, set]:
    """Evaluate a condition tree.

    Returns (tri_value, trace_tree, missing_paths) where trace_tree mirrors
    the condition structure and annotates every node with its value.
    """
    if "all" in node:
        parts = [eval_condition(child, facts) for child in node["all"]]
        value = tri_and(p[0] for p in parts)
        trace = {"node": "all", "value": value, "children": [p[1] for p in parts]}
        missing = set().union(*(p[2] for p in parts)) if parts else set()
        return value, trace, missing

    if "any" in node:
        parts = [eval_condition(child, facts) for child in node["any"]]
        value = tri_or(p[0] for p in parts)
        trace = {"node": "any", "value": value, "children": [p[1] for p in parts]}
        missing = set().union(*(p[2] for p in parts)) if parts else set()
        return value, trace, missing

    if "not" in node:
        child_value, child_trace, child_missing = eval_condition(node["not"], facts)
        value = negate(child_value)
        return value, {"node": "not", "value": value, "child": child_trace}, child_missing

    # Comparison node.
    spec = node["cmp"]
    op = spec["op"]
    left_value, left_missing, left_trace = eval_operand(spec["left"], facts)
    right_value, right_missing, right_trace = eval_operand(spec["right"], facts)
    missing = left_missing | right_missing
    trace = {"node": "cmp", "op": op, "left": left_trace, "right": right_trace}
    if missing:
        trace["value"] = UNKNOWN
        trace["reason"] = "missing field(s): " + ", ".join(sorted(missing))
        return UNKNOWN, trace, missing
    trace["left_value"] = left_value
    trace["right_value"] = right_value
    try:
        result = apply_operator(op, left_value, right_value)
    except TypeError as exc:
        # Incompatible types (e.g. "abc" < 3): the comparison is known to be
        # not-satisfiable rather than unknown — the data is present, just
        # ill-typed. Reported as false with an explanatory reason.
        trace["value"] = FALSE
        trace["reason"] = f"comparison error: {exc}"
        return FALSE, trace, missing
    trace["value"] = TRUE if result else FALSE
    return trace["value"], trace, missing


# ---------------------------------------------------------------------------
# Minimal missing-field sets
# ---------------------------------------------------------------------------

class _Budget:
    """Shared truncation flag for the bounded enumerations below."""

    def __init__(self) -> None:
        self.truncated = False


def _minimize(sets) -> list[frozenset]:
    """Keep only inclusion-minimal sets (drop any strict superset of another)."""
    unique = list(dict.fromkeys(frozenset(s) for s in sets))
    return [s for s in unique if not any(other < s for other in unique)]


def sets_to(node: dict, target: bool, facts: dict, budget: _Budget) -> list[frozenset]:
    """Inclusion-minimal missing-field sets that can drive ``node`` to ``target``.

    A set S is returned iff there exists an assignment of values to the fields
    in S (all currently missing) under which ``node`` evaluates to ``target``,
    and no proper subset of S has that property. Returns [] when the target is
    unreachable (node already has the opposite definite value).
    """
    value, _, missing = eval_condition(node, facts)
    definite = TRUE if target else FALSE
    if value == definite:
        return [frozenset()]  # already there — nothing is needed
    if value != UNKNOWN:
        return []  # opposite definite value — unreachable by adding facts

    if "cmp" in node:
        # A comparison with all fields present can take either truth value.
        return [frozenset(missing)]

    if "not" in node:
        return sets_to(node["not"], not target, facts, budget)

    key = "all" if "all" in node else "any"
    children = node[key]
    # AND reaches true / OR reaches false only if *every* child reaches the
    # target; otherwise *some* child reaching the target is enough.
    if (key == "all") == target:
        per_child = [sets_to(child, target, facts, budget) for child in children]
        if any(len(options) == 0 for options in per_child):
            return []
        combos = []
        for pick in product(*per_child):
            combos.append(frozenset().union(*pick))
            if len(combos) >= MAX_COMBOS:
                budget.truncated = True
                break
        return _minimize(combos)

    candidates: list[frozenset] = []
    for child in children:
        candidates.extend(sets_to(child, target, facts, budget))
        if len(candidates) > MAX_SETS_PER_NODE:
            budget.truncated = True
            candidates = candidates[:MAX_SETS_PER_NODE]
            break
    return _minimize(candidates)


def decidability_sets(condition: dict, facts: dict, budget: _Budget) -> list[frozenset]:
    """Minimal missing-field sets that can make a condition fully decidable."""
    candidates = sets_to(condition, True, facts, budget) + sets_to(condition, False, facts, budget)
    return _minimize(candidates)


def level_missing_sets(unknown_conditions: list[dict], facts: dict, budget: _Budget) -> list[frozenset]:
    """Minimal missing-field sets making every unknown condition in the level decidable."""
    per_rule = [decidability_sets(cond, facts, budget) for cond in unknown_conditions]
    if any(len(options) == 0 for options in per_rule):
        return []
    combos = []
    for pick in product(*per_rule):
        combos.append(frozenset().union(*pick))
        if len(combos) >= MAX_COMBOS:
            budget.truncated = True
            break
    return _minimize(combos)


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def evaluate_policy(policy: dict, facts: dict) -> dict:
    """Evaluate a policy against facts using priority levels.

    Levels (groups of rules sharing a priority) are inspected from highest to
    lowest. The first level containing a true or unknown rule decides:

    * any unknown rule in it      -> verdict "unknown" + minimal missing sets
    * all known, one conclusion   -> verdict "concluded"
    * all known, >1 conclusions   -> verdict "conflict"
    * no level ever matched       -> verdict "no_match"
    """
    levels: dict[float, list[dict]] = {}
    for rule in policy["rules"]:
        levels.setdefault(rule["priority"], []).append(rule)

    trace_levels: list[dict] = []

    for priority in sorted(levels, reverse=True):
        rules = levels[priority]
        rule_traces = []
        values = []
        for rule in rules:
            value, trace, _missing = eval_condition(rule["condition"], facts)
            values.append(value)
            rule_traces.append(
                {
                    "rule_id": rule["rule_id"],
                    "conclusion": rule["conclusion"],
                    "value": value,
                    "trace": trace,
                }
            )

        if all(v == FALSE for v in values):
            trace_levels.append({"priority": priority, "status": "all_false", "rules": rule_traces})
            continue

        trace_levels.append({"priority": priority, "status": "deciding", "rules": rule_traces})

        if any(v == UNKNOWN for v in values):
            budget = _Budget()
            unknown_conditions = [
                rule["condition"] for rule, v in zip(rules, values) if v == UNKNOWN
            ]
            missing_sets = level_missing_sets(unknown_conditions, facts, budget)
            return {
                "verdict": "unknown",
                "conclusion": None,
                "deciding_priority": priority,
                "missing_field_sets": sorted(sorted(s) for s in missing_sets),
                "missing_sets_truncated": budget.truncated,
                "trace": {"levels": trace_levels},
            }

        conclusions: list[Any] = []
        seen: set[str] = set()
        for rule, v in zip(rules, values):
            if v == TRUE:
                key = _canonical(rule["conclusion"])
                if key not in seen:
                    seen.add(key)
                    conclusions.append(rule["conclusion"])

        if len(conclusions) == 1:
            return {
                "verdict": "concluded",
                "conclusion": conclusions[0],
                "deciding_priority": priority,
                "trace": {"levels": trace_levels},
            }
        return {
            "verdict": "conflict",
            "conclusion": None,
            "conflicting_conclusions": conclusions,
            "deciding_priority": priority,
            "trace": {"levels": trace_levels},
        }

    return {
        "verdict": "no_match",
        "conclusion": None,
        "deciding_priority": None,
        "trace": {"levels": trace_levels},
    }
