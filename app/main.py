"""Explainable rule adjudication API.

Clients submit policies built from `all` / `any` / `not` / comparison
expressions; each rule carries a priority and a conclusion. Facts are
evaluated under three-valued logic (true / false / unknown). See README.md
for the full semantics.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response

from .logic import evaluate_policy
from .models import EvaluateRequest
from .store import PolicyStore
from .validation import validate_policy

VERSION = "1.0.0"
START_TIME = time.monotonic()

app = FastAPI(
    title="Explainable Rule Adjudication API",
    version=VERSION,
    description=(
        "Priority-ordered rule adjudication under three-valued logic "
        "(true / false / unknown) with per-node derivation traces and "
        "inclusion-minimal missing-field sets."
    ),
)

store = PolicyStore()

POLICY_BODY_EXAMPLE = {
    "policy_id": "loan-eligibility",
    "description": "Toy loan policy",
    "rules": [
        {
            "rule_id": "adults-with-income",
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


def _validate_or_422(payload: Any) -> dict:
    errors = validate_policy(payload)
    if errors:
        raise HTTPException(status_code=422, detail={"valid": False, "errors": errors})
    return payload


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "version": VERSION,
        "uptime_seconds": round(time.monotonic() - START_TIME, 3),
        "policies_loaded": store.count(),
    }


@app.post("/policies/validate", tags=["policies"])
def validate_only(policy: dict = Body(..., examples=[POLICY_BODY_EXAMPLE])) -> dict:
    """Validate a policy payload without storing it."""
    errors = validate_policy(policy)
    return {"valid": not errors, "errors": errors}


@app.post("/policies", status_code=201, tags=["policies"])
def create_policy(policy: dict = Body(..., examples=[POLICY_BODY_EXAMPLE])) -> dict:
    _validate_or_422(policy)
    if not store.create(policy):
        raise HTTPException(
            status_code=409,
            detail=f"policy {policy['policy_id']!r} already exists; use PUT to replace it",
        )
    return {"stored": True, "policy_id": policy["policy_id"], "rules": len(policy["rules"])}


@app.get("/policies", tags=["policies"])
def list_policies() -> dict:
    return {
        "policies": [
            {
                "policy_id": p["policy_id"],
                "rules": len(p["rules"]),
                "priorities": sorted({r["priority"] for r in p["rules"]}, reverse=True),
                "description": p.get("description"),
            }
            for p in store.list()
        ]
    }


@app.get("/policies/{policy_id}", tags=["policies"])
def get_policy(policy_id: str) -> dict:
    policy = store.get(policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"policy {policy_id!r} not found")
    return policy


@app.put("/policies/{policy_id}", tags=["policies"])
def replace_policy(policy_id: str, policy: dict = Body(..., examples=[POLICY_BODY_EXAMPLE])) -> dict:
    _validate_or_422(policy)
    if policy["policy_id"] != policy_id:
        raise HTTPException(
            status_code=422,
            detail=f"body policy_id {policy['policy_id']!r} does not match path {policy_id!r}",
        )
    if not store.replace(policy_id, policy):
        raise HTTPException(status_code=404, detail=f"policy {policy_id!r} not found")
    return {"stored": True, "policy_id": policy_id, "rules": len(policy["rules"])}


@app.delete("/policies/{policy_id}", status_code=204, tags=["policies"])
def delete_policy(policy_id: str) -> Response:
    if not store.delete(policy_id):
        raise HTTPException(status_code=404, detail=f"policy {policy_id!r} not found")
    return Response(status_code=204)


@app.post("/policies/{policy_id}/evaluate", tags=["evaluation"])
def evaluate(policy_id: str, request: EvaluateRequest) -> dict:
    policy = store.get(policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"policy {policy_id!r} not found")
    result = evaluate_policy(policy, request.facts)
    result["policy_id"] = policy_id
    return result
