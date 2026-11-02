"""
environment.py - State management, action validation, and environment execution.

Functions only (no classes).
Implements Section 6.3 from the LATS pseudocode specification.
"""

import copy
from executor import is_safe, run_python, run_tests

REQUIRED_FIELDS = {
    "inspect_code": ["focus"],
    "run_snippet": ["code"],
    "run_tests": [],
    "hypothesize": ["hypothesis"],
    "apply_patch": ["new_code", "hypothesis_id"],
    "conclude": ["root_cause", "evidence_ids"],
}


def make_state(code, error, tests, expected):
    """Creates a new state dictionary."""
    return {
        "original_code": code,
        "current_code": code,
        "error": error,
        "tests": tests,
        "expected_behavior": expected,
        "test_results": [],
        "hypotheses": [],
        "actions_taken": [],
        "evidence": [],
    }


def _action_str(action):
    """Helper to convert an action dictionary into a readable string."""
    t = action.get("type", "unknown")
    if t == "inspect_code":
        return f"inspect_code(focus='{action.get('focus')}')"
    if t == "run_snippet":
        return f"run_snippet(code='{action.get('code')}')"
    if t == "run_tests":
        return "run_tests()"
    if t == "hypothesize":
        return f"hypothesize('{action.get('hypothesis')}')"
    if t == "apply_patch":
        return f"apply_patch(hyp='{action.get('hypothesis_id')}')"
    if t == "conclude":
        return f"conclude('{action.get('root_cause')}')"
    return str(action)


def render_state(state):
    """Renders the state into structured text shown to the LLM."""
    tr_lines = "\n".join(
        f"  {r['call']} -> {r['got']} ({'PASS' if r['passed'] else 'FAIL'})"
        for r in state.get("test_results", [])
    ) or "  (not run yet)"

    hy_lines = "\n".join(
        f"  [{h['id']}] {h['text']} -> status: {h['status']}"
        for h in state.get("hypotheses", [])
    ) or "  (none)"

    ev_lines = "\n".join(
        f"  [{e['id']}] ({e['kind']}) {e['text']}"
        for e in state.get("evidence", [])[-10:]
    ) or "  (none)"

    ac_lines = "\n".join(
        f"  - {_action_str(a)}"
        for a in state.get("actions_taken", [])[-10:]
    ) or "  (none)"

    return (
        f"CODE:\n{state.get('current_code', '')}\n\n"
        f"ERROR:\n{state.get('error', '')}\n\n"
        f"EXPECTED BEHAVIOR:\n{state.get('expected_behavior', '')}\n\n"
        f"TEST RESULTS:\n{tr_lines}\n\n"
        f"HYPOTHESES:\n{hy_lines}\n\n"
        f"EVIDENCE (from real execution):\n{ev_lines}\n\n"
        f"ACTIONS TAKEN:\n{ac_lines}"
    )


def add_evidence(state, kind, text):
    """Appends a new evidence item to the state."""
    new_id = f"e{len(state.get('evidence', [])) + 1}"
    state.setdefault("evidence", []).append({
        "id": new_id,
        "kind": kind,
        "text": text[:500],
    })


def _action_key(action):
    """Generates a comparable key for duplicate detection."""
    return (
        action.get("type"),
        action.get("focus"),
        action.get("code"),
        action.get("hypothesis"),
        action.get("new_code"),
        action.get("hypothesis_id"),
        action.get("root_cause"),
        tuple(sorted(action.get("evidence_ids", []))),
    )


def validate_action(state, action, sibling_actions=()):
    """Validates whether an action proposed by the policy is syntactically and semantically valid."""
    if not isinstance(action, dict):
        return False, "action must be a dictionary"

    action_type = action.get("type")
    if action_type not in REQUIRED_FIELDS:
        return False, f"unknown type: {action_type}"

    # Check required fields
    for field in REQUIRED_FIELDS[action_type]:
        if field not in action or action[field] in (None, "", []):
            return False, f"missing field: {field}"

    # Check duplicates against actions taken and sibling actions
    curr_key = _action_key(action)
    for prev in state.get("actions_taken", []):
        if curr_key == _action_key(prev):
            return False, "duplicate"
    for sib in sibling_actions:
        if curr_key == _action_key(sib):
            return False, "duplicate"

    # Check code safety
    for code_field in ("code", "new_code"):
        if code_field in action:
            if not is_safe(action[code_field]):
                return False, "unsafe code"

    # Check conclude cites valid evidence
    if action_type == "conclude":
        existing_ids = {e["id"] for e in state.get("evidence", [])}
        cited_ids = set(action.get("evidence_ids", []))
        if not cited_ids:
            return False, "conclude requires cited evidence_ids"
        if not cited_ids.issubset(existing_ids):
            return False, "unknown evidence"

    return True, ""


def run_action(state, action):
    """The ONLY place observations are created. Executes the action in the environment."""
    new_state = copy.deepcopy(state)
    new_state.setdefault("actions_taken", []).append(action)

    obs = {
        "text": "",
        "reproduced_error": False,
        "tests_fixed": 0,
        "tests_broken": 0,
        "hypothesis_supported": False,
        "hypothesis_refuted": False,
    }

    action_type = action["type"]

    if action_type == "inspect_code":
        focus = str(action["focus"]).lower()
        lines = new_state["current_code"].splitlines()
        matched = [f"{i+1}: {l}" for i, l in enumerate(lines) if focus in l.lower()]
        if not matched:
            matched = [f"{i+1}: {l}" for i, l in enumerate(lines)]
        obs["text"] = "\n".join(matched[:15])
        add_evidence(new_state, "inspect", obs["text"])

    elif action_type == "run_snippet":
        source = new_state["current_code"] + "\n" + action["code"]
        out = run_python(source)
        obs["text"] = (out["stdout"] + out["stderr"]).strip()
        # Check if output reproduced the original error
        err_type = new_state["error"].split(":")[0].strip()
        if err_type and err_type in out["stderr"]:
            obs["reproduced_error"] = True
        add_evidence(new_state, "run", f"{action['code']} -> {obs['text']}")

    elif action_type == "run_tests":
        results = run_tests(new_state["current_code"], new_state["tests"])
        new_state["test_results"] = results
        obs["text"] = "\n".join(
            f"{r['call']} -> {'PASS' if r['passed'] else 'FAIL'} ({r['got']})"
            for r in results
        )
        add_evidence(new_state, "tests", obs["text"])

    elif action_type == "hypothesize":
        h_num = len(new_state.get("hypotheses", [])) + 1
        new_state.setdefault("hypotheses", []).append({
            "id": f"h{h_num}",
            "text": action["hypothesis"],
            "status": "open",
        })
        obs["text"] = "recorded, unverified"  # Adds NO evidence

    elif action_type == "apply_patch":
        before = run_tests(new_state["current_code"], new_state["tests"])
        after = run_tests(action["new_code"], new_state["tests"])
        fixed = sum(1 for b, a in zip(before, after) if not b["passed"] and a["passed"])
        broken = sum(1 for b, a in zip(before, after) if b["passed"] and not a["passed"])
        obs["tests_fixed"] = fixed
        obs["tests_broken"] = broken
        new_state["current_code"] = action["new_code"]
        new_state["test_results"] = after

        hyp_id = action.get("hypothesis_id")
        if fixed > 0 and broken == 0:
            obs["hypothesis_supported"] = True
            for h in new_state.get("hypotheses", []):
                if h["id"] == hyp_id:
                    h["status"] = "supported"
        else:
            obs["hypothesis_refuted"] = True
            for h in new_state.get("hypotheses", []):
                if h["id"] == hyp_id:
                    h["status"] = "refuted"

        obs["text"] = f"fixed={fixed} broken={broken}"
        add_evidence(new_state, "patch", obs["text"])

    elif action_type == "conclude":
        obs["text"] = "conclusion recorded"
        obs["root_cause"] = action.get("root_cause", "")
        obs["evidence_ids"] = action.get("evidence_ids", [])

    return new_state, obs

