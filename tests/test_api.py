"""End-to-end tests for the adjudication API (FastAPI TestClient)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_policy(**overrides):
    policy = {
        "policy_id": "loan",
        "rules": [
            {
                "rule_id": "adult-with-income",
                "priority": 10,
                "conclusion": "eligible",
                "condition": {
                    "all": [
                        {"cmp": {"op": ">=", "left": {"field": "age"}, "right": 18}},
                        {"cmp": {"op": ">=", "left": {"field": "income"}, "right": 30000}},
                    ]
                },
            },
            {
                "rule_id": "fallback",
                "priority": 1,
                "conclusion": "review",
                "condition": {"cmp": {"op": "==", "left": {"field": "country"}, "right": "US"}},
            },
        ],
    }
    policy.update(overrides)
    return policy


@pytest.fixture(autouse=True)
def clean_store():
    # Wipe everything created by a test so cases stay independent.
    for p in client.get("/policies").json()["policies"]:
        client.delete(f"/policies/{p['policy_id']}")
    yield


# ---------------------------------------------------------------------------
# Health & validation
# ---------------------------------------------------------------------------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime_seconds" in body
    assert body["policies_loaded"] == 0


def test_validate_valid_policy():
    resp = client.post("/policies/validate", json=make_policy())
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "errors": []}


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (lambda p: p.update(policy_id="bad id!"), "policy_id"),
        (lambda p: p.update(rules=[]), "rules"),
        (lambda p: p["rules"][0].update(priority="high"), "priority"),
        (lambda p: p["rules"][0].update(priority=True), "priority"),
        (lambda p: p["rules"][0].pop("conclusion"), "conclusion"),
        (lambda p: p["rules"][0].pop("condition"), "condition"),
        (lambda p: p["rules"][0].update(condition={"all": []}), "non-empty"),
        (lambda p: p["rules"][0].update(condition={"xor": [{"cmp": {"op": "==", "left": 1, "right": 1}}]}), "unknown condition operator"),
        (lambda p: p["rules"][0].update(condition={"cmp": {"op": "~~", "left": 1, "right": 1}}), "cmp.op"),
        (lambda p: p["rules"][0].update(condition={"cmp": {"op": "==", "left": 1}}), "missing operand"),
        (lambda p: p["rules"][0].update(condition={"cmp": {"op": "==", "left": {"field": ""}, "right": 1}}), "field"),
        (lambda p: p["rules"][0].update(condition={"cmp": {"op": "in", "left": {"field": "x"}, "right": [1, 2]}}), "const"),
        (lambda p: p["rules"][0].update(condition={"all": [{"cmp": {"op": "==", "left": 1, "right": 1}}], "not": {"cmp": {"op": "==", "left": 1, "right": 1}}}), "exactly one"),
        (lambda p: p["rules"].append(dict(p["rules"][0])), "duplicate rule_id"),
    ],
)
def test_validate_invalid_policies(mutation, needle):
    policy = make_policy()
    mutation(policy)
    resp = client.post("/policies/validate", json=policy)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any(needle in err for err in body["errors"]), body["errors"]


def test_create_rejects_invalid_policy_with_422():
    policy = make_policy()
    policy["rules"][0]["condition"] = {"all": []}
    resp = client.post("/policies", json=policy)
    assert resp.status_code == 422
    assert resp.json()["detail"]["valid"] is False


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_get_list_delete():
    assert client.post("/policies", json=make_policy()).status_code == 201

    dup = client.post("/policies", json=make_policy())
    assert dup.status_code == 409

    got = client.get("/policies/loan")
    assert got.status_code == 200
    assert got.json()["rules"][0]["rule_id"] == "adult-with-income"

    listing = client.get("/policies").json()["policies"]
    assert listing[0]["policy_id"] == "loan"
    assert listing[0]["priorities"] == [10, 1]

    assert client.delete("/policies/loan").status_code == 204
    assert client.get("/policies/loan").status_code == 404
    assert client.delete("/policies/loan").status_code == 404


def test_replace_policy():
    assert client.post("/policies", json=make_policy()).status_code == 201
    updated = make_policy(description="v2")
    resp = client.put("/policies/loan", json=updated)
    assert resp.status_code == 200
    assert client.get("/policies/loan").json()["description"] == "v2"

    mismatch = client.put("/policies/loan", json=make_policy(policy_id="other"))
    assert mismatch.status_code == 422
    assert client.put("/policies/ghost", json=make_policy(policy_id="ghost")).status_code == 404


# ---------------------------------------------------------------------------
# Evaluation verdicts
# ---------------------------------------------------------------------------

def test_concluded():
    client.post("/policies", json=make_policy())
    resp = client.post("/policies/loan/evaluate", json={"facts": {"age": 30, "income": 50000}})
    body = resp.json()
    assert body["verdict"] == "concluded"
    assert body["conclusion"] == "eligible"
    assert body["deciding_priority"] == 10
    # Trace shows the deciding level with per-node values.
    level = body["trace"]["levels"][0]
    assert level["status"] == "deciding"
    rule_trace = level["rules"][0]["trace"]
    assert rule_trace["node"] == "all"
    assert rule_trace["value"] == "true"
    assert [c["value"] for c in rule_trace["children"]] == ["true", "true"]


def test_priority_fallthrough_to_lower_level():
    client.post("/policies", json=make_policy())
    # High-priority rule false (age < 18) -> lower level decides.
    resp = client.post("/policies/loan/evaluate", json={"facts": {"age": 15, "income": 99999, "country": "US"}})
    body = resp.json()
    assert body["verdict"] == "concluded"
    assert body["conclusion"] == "review"
    assert body["deciding_priority"] == 1
    assert [l["status"] for l in body["trace"]["levels"]] == ["all_false", "deciding"]


def test_no_match_when_everything_false():
    client.post("/policies", json=make_policy())
    resp = client.post("/policies/loan/evaluate", json={"facts": {"age": 10, "income": 0, "country": "FR"}})
    body = resp.json()
    assert body["verdict"] == "no_match"
    assert body["conclusion"] is None


def test_conflict_on_opposite_conclusions_same_level():
    policy = make_policy(
        rules=[
            {
                "rule_id": "r1",
                "priority": 5,
                "conclusion": "approve",
                "condition": {"cmp": {"op": ">=", "left": {"field": "score"}, "right": 10}},
            },
            {
                "rule_id": "r2",
                "priority": 5,
                "conclusion": "reject",
                "condition": {"cmp": {"op": "<=", "left": {"field": "score"}, "right": 20}},
            },
        ]
    )
    client.post("/policies", json=policy)
    resp = client.post("/policies/loan/evaluate", json={"facts": {"score": 15}})
    body = resp.json()
    assert body["verdict"] == "conflict"
    assert sorted(body["conflicting_conclusions"]) == ["approve", "reject"]


def test_same_conclusion_twice_is_not_a_conflict():
    policy = make_policy(
        rules=[
            {"rule_id": "r1", "priority": 5, "conclusion": "ok",
             "condition": {"cmp": {"op": ">=", "left": {"field": "x"}, "right": 1}}},
            {"rule_id": "r2", "priority": 5, "conclusion": "ok",
             "condition": {"cmp": {"op": "<=", "left": {"field": "x"}, "right": 100}}},
        ]
    )
    client.post("/policies", json=policy)
    body = client.post("/policies/loan/evaluate", json={"facts": {"x": 5}}).json()
    assert body["verdict"] == "concluded"
    assert body["conclusion"] == "ok"


def test_unknown_verdict_with_trace_and_missing_sets():
    client.post("/policies", json=make_policy())
    resp = client.post("/policies/loan/evaluate", json={"facts": {"age": 30}})
    body = resp.json()
    assert body["verdict"] == "unknown"
    assert body["deciding_priority"] == 10
    assert body["missing_field_sets"] == [["income"]]
    cmp_traces = body["trace"]["levels"][0]["rules"][0]["trace"]["children"]
    assert cmp_traces[0]["value"] == "true"
    assert cmp_traces[1]["value"] == "unknown"
    assert "income" in cmp_traces[1]["reason"]


def test_unknown_beats_lower_true_level():
    # Highest level unknown -> verdict unknown even though a lower level is true.
    client.post("/policies", json=make_policy())
    resp = client.post("/policies/loan/evaluate", json={"facts": {"country": "US"}})
    body = resp.json()
    assert body["verdict"] == "unknown"
    assert body["deciding_priority"] == 10
    # all([age, income]): supplying either field alone can decide the level
    # (e.g. age < 18 makes the conjunction false), so both singletons are minimal.
    assert body["missing_field_sets"] == [["age"], ["income"]]


def test_minimal_missing_sets_nested():
    # all([any([a >= 1, b >= 1]), c >= 1]) with no facts:
    # supplying "c" alone can decide (c false -> whole conjunction false),
    # or supplying "a" and "b" decides the disjunction.
    policy = make_policy(
        rules=[
            {
                "rule_id": "nested",
                "priority": 1,
                "conclusion": "yes",
                "condition": {
                    "all": [
                        {"any": [
                            {"cmp": {"op": ">=", "left": {"field": "a"}, "right": 1}},
                            {"cmp": {"op": ">=", "left": {"field": "b"}, "right": 1}},
                        ]},
                        {"cmp": {"op": ">=", "left": {"field": "c"}, "right": 1}},
                    ]
                },
            }
        ]
    )
    client.post("/policies", json=policy)
    body = client.post("/policies/loan/evaluate", json={"facts": {}}).json()
    assert body["verdict"] == "unknown"
    assert {tuple(s) for s in body["missing_field_sets"]} == {("c",), ("a", "b")}


def test_missing_sets_combine_across_unknown_rules_in_level():
    policy = make_policy(
        rules=[
            {"rule_id": "r1", "priority": 3, "conclusion": "x",
             "condition": {"cmp": {"op": "==", "left": {"field": "a"}, "right": 1}}},
            {"rule_id": "r2", "priority": 3, "conclusion": "y",
             "condition": {"cmp": {"op": "==", "left": {"field": "b"}, "right": 1}}},
        ]
    )
    client.post("/policies", json=policy)
    body = client.post("/policies/loan/evaluate", json={"facts": {}}).json()
    assert body["verdict"] == "unknown"
    assert body["missing_field_sets"] == [["a", "b"]]


def test_not_any_and_dotted_fields():
    policy = make_policy(
        rules=[
            {
                "rule_id": "vip",
                "priority": 1,
                "conclusion": "fast-track",
                "condition": {
                    "all": [
                        {"not": {"cmp": {"op": "==", "left": {"field": "user.blocked"}, "right": True}}},
                        {"any": [
                            {"cmp": {"op": "in", "left": {"field": "user.tier"}, "right": {"const": ["gold", "platinum"]}}},
                            {"cmp": {"op": ">", "left": {"field": "user.years"}, "right": 5}},
                        ]},
                    ]
                },
            }
        ]
    )
    client.post("/policies", json=policy)

    ok = client.post("/policies/loan/evaluate",
                     json={"facts": {"user": {"blocked": False, "tier": "gold"}}}).json()
    assert ok["verdict"] == "concluded"

    blocked = client.post("/policies/loan/evaluate",
                          json={"facts": {"user": {"blocked": True, "tier": "gold"}}}).json()
    assert blocked["verdict"] == "no_match"

    missing = client.post("/policies/loan/evaluate", json={"facts": {"user": {"blocked": False}}}).json()
    assert missing["verdict"] == "unknown"
    # Either tier or years alone can make the disjunction true -> both are minimal.
    assert missing["missing_field_sets"] == [["user.tier"], ["user.years"]]


def test_evaluate_unknown_policy_404():
    assert client.post("/policies/ghost/evaluate", json={"facts": {}}).status_code == 404


def test_evaluate_rejects_non_object_facts():
    client.post("/policies", json=make_policy())
    assert client.post("/policies/loan/evaluate", json={"facts": [1, 2]}).status_code == 422
