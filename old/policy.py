"""
policy.py - LLM Policy for proposing candidate actions.

Functions only (no classes).
Implements Section 6.5 (POLICY) from the LATS pseudocode specification.
"""

from llm_client import llm_json, POLICY_PROMPT, reflections
from environment import render_state


def propose_actions(state, k=4, feedback=""):
    """Given state and past reflections, prompts the LLM to propose k diverse actions."""
    # Build reflection lessons text (last 5)
    past_lessons = "\n".join(f"- {r}" for r in reflections[-5:]) or "(none)"

    user_prompt = (
        f"CURRENT STATE:\n{render_state(state)}\n\n"
        f"LESSONS FROM PREVIOUS FAILED PATHS:\n{past_lessons}\n\n"
        f"Propose {k} diverse actions as JSON."
    )
    if feedback:
        user_prompt += f"\nPrevious proposals were rejected: {feedback}"

    result = llm_json(POLICY_PROMPT, user_prompt, temperature=0.8)
    if result is None or not isinstance(result, dict):
        return []

    actions = result.get("actions", [])
    if not isinstance(actions, list):
        return []

    return actions[:k]

