"""
reward.py - Objective environment rewards and terminal rewards.

Functions only (no classes).
Implements Section 6.4 from the LATS pseudocode specification.
"""


def clamp(val, low=0.0, high=1.0):
    """Clamps a numeric value between low and high."""
    return max(low, min(high, val))


def objective_reward(old_state, new_state, action, obs):
    """Computes objective step reward based purely on facts produced by real execution."""
    r = 0.0

    # 1. Did the action reproduce the original error?
    if obs.get("reproduced_error"):
        r += 0.15

    # 2. How many new evidence items were collected?
    new_ev = len(new_state.get("evidence", [])) - len(old_state.get("evidence", []))
    r += min(0.15, 0.05 * max(0, new_ev))

    # 3. Did a test or patch support a hypothesis?
    if obs.get("hypothesis_supported"):
        r += 0.20

    # 4. Did an experiment refute a hypothesis (reducing uncertainty)?
    if obs.get("hypothesis_refuted"):
        r += 0.10

    # 5. Did a patch fix failing tests?
    if obs.get("tests_fixed", 0) > 0:
        r += 0.30

    # 6. Did a patch break previously passing tests?
    if obs.get("tests_broken", 0) > 0:
        r -= 0.20

    # 7. Penalty for no-information actions (excluding hypothesize and conclude)
    if new_ev == 0 and action.get("type") not in ("hypothesize", "conclude"):
        r -= 0.10

    return clamp(r, 0.0, 1.0)


def terminal_reward(state, action):
    """Evaluates a 'conclude' action using only recorded evidence and verified hypotheses."""
    cited_ids = set(action.get("evidence_ids", []))
    evidence_items = state.get("evidence", [])

    # Check if any cited evidence is a patch that fixed > 0 tests
    cited_patch = any(
        e["id"] in cited_ids and e["kind"] == "patch" and "fixed=0" not in e["text"]
        for e in evidence_items
    )

    # Check if there is any supported hypothesis
    supported = any(h["status"] == "supported" for h in state.get("hypotheses", []))

    if supported and cited_patch:
        return 0.95, "success"
    if supported or len(cited_ids) >= 2:
        return 0.50, "success"
    return 0.10, "failure"

