"""
evaluator.py - LLM State Evaluator (Score + Self-Consistency).

Functions only (no classes).
Implements Section 6.5 (EVALUATOR) from the LATS pseudocode specification.
"""

from collections import Counter
from llm_client import llm_json, EVALUATOR_PROMPT, M_SAMPLES, LAMBDA
from environment import render_state
from reward import clamp


def evaluate_state(state, last_obs):
    """Scores an investigation state by combining LLM judgment and self-consistency."""
    obs_text = (last_obs or {}).get("text", "")
    user_prompt = f"{render_state(state)}\n\nLAST OBSERVATION:\n{obs_text}"

    samples = []
    for _ in range(M_SAMPLES):
        out = llm_json(EVALUATOR_PROMPT, user_prompt, temperature=0.8)
        if out and isinstance(out, dict) and isinstance(out.get("score"), (int, float)):
            samples.append(out)

    if not samples:
        return 0.3

    # 1. LM score: average of clamped scores
    lm = sum(clamp(s["score"], 0.0, 1.0) for s in samples) / len(samples)

    # 2. SC score: self-consistency across samples on leading_hypothesis_id
    counts = Counter(s.get("leading_hypothesis_id") for s in samples)
    most_common_count = counts.most_common(1)[0][1]
    sc = most_common_count / len(samples)

    # Hybrid evaluator value
    return LAMBDA * lm + (1.0 - LAMBDA) * sc

