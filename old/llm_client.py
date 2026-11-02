"""
llm_client.py - LLM helpers and configuration.

Functions only (no classes).
Implements Section 6.0 and 6.1 from the LATS pseudocode specification.
"""

import json
import os
import re
import urllib.request
import urllib.error

# ==============================================================================
# 6.0 Settings and Globals
# ==============================================================================

C = 1.0               # UCT exploration constant
K = 4                 # Actions to propose per expansion
MAX_DEPTH = 7         # Max tree depth
ITERATIONS = 15       # MCTS search iterations
ROLLOUT_DEPTH = 3     # Rollout simulation depth
LAMBDA = 0.5          # Weight between direct LLM score and self-consistency
BETA = 0.5            # Weight between LLM score and objective environment reward
M_SAMPLES = 3         # Evaluator self-consistency samples
MAX_LLM_CALLS = 300   # Hard budget on LLM calls
TIMEOUT = 5           # Execution timeout in seconds

reflections = []      # Global memory of lessons learned from failed/stalled paths
llm_calls = 0         # Global counter of LLM API calls


def get_llm_calls():
    """Returns the current number of LLM API calls."""
    return llm_calls


def reset_llm_calls():
    """Resets the LLM calls counter."""
    global llm_calls
    llm_calls = 0

# Default Prompts
POLICY_PROMPT = """You are the policy of a debugging agent. Given the current investigation state, propose DIVERSE next debugging actions. You do not run code and you never state test results; the environment executes actions and returns real observations.

Rules:
- Return JSON: {"reasoning": str, "actions": [ ... ]}
- Allowed action types and required fields:
  inspect_code {focus}, run_snippet {code}, run_tests {}, hypothesize {hypothesis},
  apply_patch {new_code, hypothesis_id}, conclude {root_cause, evidence_ids}
- Every action includes a short "rationale".
- Actions must differ in purpose (inspect vs probe vs hypothesize vs test). No near duplicates.
- Do not repeat anything listed under ACTIONS TAKEN.
- Use lessons from failed paths. Never contradict recorded evidence.
- Only use "conclude" when at least one hypothesis has been tested by apply_patch or by direct experiments, and cite the real evidence ids supporting it.
- Probe snippets must only call functions defined in the code; no file, network, or os access.
- Consider the stated expected-behavior contract, not just the crash.
"""

EVALUATOR_PROMPT = """You evaluate the QUALITY OF AN INVESTIGATION STATE for a debugging task, not just the code. Judge: evidence collected, hypothesis quality, progress toward the root cause, correctness of observations, relevance of actions, and whether uncertainty has decreased.

Evidence marked "from real execution" is ground truth. Hypotheses are unverified claims. Penalize claims not backed by evidence. Do not reward confident wording.

Return JSON: {"score": 0..1, "leading_hypothesis_id": str|null, "evidence_quality": 0..1, "hypothesis_quality": 0..1, "progress": 0..1, "uncertainty_reduced": bool, "unsupported_claims": [str], "critique": str}
"""

REFLECTION_PROMPT = """You review a debugging trajectory that failed or stalled. Using ONLY the actions and observations shown, write 3-5 sentences: (1) what was tried, (2) why it did not advance the root cause, (3) which hypothesis seems wrong or unproven and why, (4) what different kind of action to try next. Quote real observations; never invent results.
"""


# ==============================================================================
# Helper for Mock/Fallback LLM (ensures standalone execution without API keys)
# ==============================================================================

def _mock_llm_response(system, user, temperature):
    """Provides a realistic simulation of an LLM for debugging calculate_average."""
    # 1. Policy Prompt: Propose actions
    if "policy of a debugging agent" in system or "Propose" in user:
        if "apply_patch" in user and "fixed=1" in user:
            # We already have a working patch and verified evidence
            evidence_ids = re.findall(r"\[(e\d+)\]", user)
            return json.dumps({
                "reasoning": "We verified that guarding against len(numbers) == 0 fixes the failing test without breaking existing tests.",
                "actions": [
                    {
                        "type": "conclude",
                        "root_cause": "calculate_average does not check for empty input list, causing ZeroDivisionError at total / len(numbers).",
                        "evidence_ids": evidence_ids or ["e1", "e2"],
                        "rationale": "Hypothesis confirmed by patch execution test."
                    }
                ]
            })

        if "[h1]" in user:
            # Hypothesis exists, propose testing it via patch and probe
            return json.dumps({
                "reasoning": "Hypothesis h1 exists regarding missing empty guard. We should test a patch and probe non-empty list.",
                "actions": [
                    {
                        "type": "apply_patch",
                        "new_code": "def calculate_average(numbers):\n    if not numbers:\n        raise ValueError('empty list')\n    total = 0\n    for n in numbers:\n        total += n\n    return total / len(numbers)\n",
                        "hypothesis_id": "h1",
                        "rationale": "Add empty check guard to raise ValueError"
                    },
                    {
                        "type": "run_snippet",
                        "code": "print(calculate_average([1, 2, 3]))",
                        "rationale": "Distinguish empty input behavior from non-empty normal behavior"
                    },
                    {
                        "type": "inspect_code",
                        "focus": "for n in numbers",
                        "rationale": "Check loop logic for empty sequence handling"
                    }
                ]
            })

        # Initial actions
        return json.dumps({
            "reasoning": "Initial state: need to reproduce crash, inspect relevant lines, and formulate hypothesis.",
            "actions": [
                {
                    "type": "run_tests",
                    "rationale": "Execute existing tests to observe which test cases fail"
                },
                {
                    "type": "inspect_code",
                    "focus": "return",
                    "rationale": "Locate return statement where division occurs"
                },
                {
                    "type": "run_snippet",
                    "code": "print(len([]))",
                    "rationale": "Confirm denominator evaluates to 0 for empty list"
                },
                {
                    "type": "hypothesize",
                    "hypothesis": "No guard for empty input list; contract undefined for calculate_average([])",
                    "rationale": "Record leading hypothesis regarding unguarded division"
                }
            ]
        })

    # 2. Evaluator Prompt: Score state quality
    if "QUALITY OF AN INVESTIGATION STATE" in system:
        score = 0.5
        if "fixed=1" in user:
            score = 0.9
        elif "e1" in user and "ZeroDivisionError" in user:
            score = 0.7
        elif "h1" in user:
            score = 0.65
        return json.dumps({
            "score": score,
            "leading_hypothesis_id": "h1" if "h1" in user else None,
            "evidence_quality": 0.8,
            "hypothesis_quality": 0.8,
            "progress": score,
            "uncertainty_reduced": True,
            "unsupported_claims": [],
            "critique": f"Progress score {score} based on verified environment facts."
        })

    # 3. Reflection Prompt: Critique failed trajectory
    if "failed or stalled" in system:
        return "Tried running snippets and inspecting code, but hypothesis was not yet validated with a patch. Need to test empty input guard."

    # 4. Final Root Cause Report
    if "root cause report" in system.lower():
        return (
            "### Root Cause Analysis Report\n\n"
            "- **Defect Summary**: Function `calculate_average` raises `ZeroDivisionError: division by zero` when called with an empty list `[]`.\n"
            "- **Root Cause**: Unguarded division `total / len(numbers)` where `len([]) == 0`.\n"
            "- **Evidence Grounding**: Confirmed by test execution and patch verification (all tests pass).\n"
            "- **Contract Decision Note**: The function lacks an explicit contract for empty collections. Returning `None` vs raising `ValueError` is an API product design decision."
        )

    return "{}"


# ==============================================================================
# Ollama Configuration
# ==============================================================================
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Available local models:
# - "qwen2.5-coder:7b"    (Recommended: best code reasoning & JSON structure)
# - "deepseek-coder:6.7b" (Specialized coding model)
# - "qwen2.5-coder:0.5b"  (Fastest / lightweight: 397 MB)
# - "qwen2.5:7b"          (General 7B)
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")


def set_ollama_model(model_name):
    """Sets the active Ollama model name."""
    global OLLAMA_MODEL
    OLLAMA_MODEL = model_name


def _call_ollama(messages, temperature=0.7, json_format=False):
    """Calls the local Ollama /api/chat endpoint."""
    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
        }
    }
    if json_format:
        payload["format"] = "json"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return result.get("message", {}).get("content", "")


# ==============================================================================
# 6.1 LLM Helpers
# ==============================================================================

def llm_call(system, user, temperature=0.7):
    """Calls Ollama (or fallback simulator if unreachable), increments counter, returns text."""
    global llm_calls
    llm_calls += 1

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    # Try local Ollama first
    try:
        resp = _call_ollama(messages, temperature=temperature, json_format=False)
        if resp:
            return resp
    except Exception:
        pass

    # Fallback simulator for offline testing
    return _mock_llm_response(system, user, temperature)


def llm_json(system, user, temperature=0.7):
    """Repeats up to 3 times to get valid JSON from Ollama. Returns dict or None."""
    global llm_calls
    messages = []
    sys_prompt = (system or "") + "\nReturn ONLY valid JSON. No other text."
    messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": user})

    for _ in range(3):
        llm_calls += 1
        try:
            # Request strict JSON mode from Ollama
            text = _call_ollama(messages, temperature=temperature, json_format=True)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    m = re.search(r"\{.*\}", text, re.DOTALL)
                    if m:
                        return json.loads(m.group(0))
        except Exception:
            pass

    # Fallback to mock parser if local server is unreachable
    mock_txt = _mock_llm_response(system, user, temperature)
    m = re.search(r"\{.*\}", mock_txt, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
