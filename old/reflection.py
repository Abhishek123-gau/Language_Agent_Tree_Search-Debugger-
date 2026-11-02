"""
reflection.py - LLM Reflector on failed or stalled trajectories.

Functions only (no classes).
Implements Section 6.5 (REFLECTION) from the LATS pseudocode specification.
"""

from llm_client import llm_call, REFLECTION_PROMPT
from environment import render_state


def get_trajectory(node):
    """Gathers the sequence of (action, observation_text) on the path from root to node."""
    path = []
    curr = node
    while curr.get("parent") is not None:
        obs_text = (curr.get("observation") or {}).get("text", "")
        path.append((curr.get("action"), obs_text))
        curr = curr["parent"]
    path.reverse()
    return path


def reflect(node):
    """Analyzes a failed or stalled trajectory and writes a lesson for future search paths."""
    trajectory = get_trajectory(node)
    steps = "\n".join(
        f"{i+1}. ACTION: {a} -> OBSERVATION: {obs[:200]}"
        for i, (a, obs) in enumerate(trajectory)
    )

    user_prompt = (
        f"TRAJECTORY:\n{steps}\n\n"
        f"FINAL STATE:\n{render_state(node['state'])}\n\n"
        f"Status: {node.get('terminal_status', 'stalled')}"
    )

    reflection_text = llm_call(REFLECTION_PROMPT, user_prompt, temperature=0.4)
    return reflection_text.strip()

