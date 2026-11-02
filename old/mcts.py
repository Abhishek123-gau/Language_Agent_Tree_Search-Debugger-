"""
mcts.py - Monte Carlo Tree Search for Language Agent Tree Search (LATS).

Functions only (no classes).
Implements Section 6.6 and 6.7 from the LATS pseudocode specification.
"""

import math
from llm_client import (
    C,
    K,
    MAX_DEPTH,
    ITERATIONS,
    ROLLOUT_DEPTH,
    BETA,
    MAX_LLM_CALLS,
    get_llm_calls,
    reflections,
)
from environment import validate_action, run_action
from reward import objective_reward, terminal_reward, clamp
from policy import propose_actions
from evaluator import evaluate_state
from reflection import reflect
from logger import (
    log,
    log_iteration_header,
    log_selection,
    log_expansion_start,
    log_action_validation,
    log_node_created,
    log_evaluation,
    log_simulation,
    log_backpropagation,
    log_reflection,
)

_node_counter = 0


def _get_next_node_id():
    """Returns a unique integer ID for a tree node."""
    global _node_counter
    _node_counter += 1
    return _node_counter


# ==============================================================================
# 6.6 The Six LATS Functions
# ==============================================================================

def make_node(parent, state, action, obs, reward):
    """Creates a new node dictionary and attaches it to parent if provided."""
    node = {
        "id": _get_next_node_id(),
        "parent": parent,
        "children": [],
        "state": state,
        "action": action,
        "observation": obs,
        "depth": (parent["depth"] + 1) if parent else 0,
        "visits": 0,
        "value_sum": 0.0,
        "reward": reward,
        "reflection": None,
        "terminal_status": None,
        "pruned": False,
        "value": 0.0,
    }
    if parent is not None:
        parent["children"].append(node)
    return node


def uct(parent, child):
    """Calculates Upper Confidence bound applied to Trees (UCT) for a child node."""
    if child["visits"] == 0:
        return float("inf")
    average_value = child["value_sum"] / child["visits"]
    parent_visits = max(parent["visits"], 1)
    exploration_bonus = C * math.sqrt(math.log(parent_visits) / child["visits"])
    return average_value + exploration_bonus


# A. SELECTION
def select(root):
    """Walks from root to an unexpanded leaf by selecting child with highest UCT."""
    node = root
    while node["children"] and node["terminal_status"] is None:
        live = [ch for ch in node["children"] if not ch["pruned"]]
        if not live:
            node["pruned"] = True
            return root
        node = max(live, key=lambda ch: uct(node, ch))
    return node


# B. EXPANSION
def expand(node, k):
    """Proposes k candidate actions, validates and executes each, creating child nodes."""
    made = []
    feedback = ""
    log_expansion_start(node["id"], k)
    for _ in range(2):
        actions = propose_actions(node["state"], k, feedback)
        siblings = [ch["action"] for ch in node["children"] if ch.get("action")]
        rejected = []

        for action in actions:
            ok, why = validate_action(node["state"], action, siblings)
            log_action_validation(action, ok, why)
            if not ok:
                rejected.append(f"{action.get('type')}: {why}")
                continue

            new_state, obs = run_action(node["state"], action)
            r = objective_reward(node["state"], new_state, action, obs)
            child = make_node(node, new_state, action, obs, r)
            siblings.append(action)

            if action.get("type") == "conclude":
                child["reward"], child["terminal_status"] = terminal_reward(new_state, action)
            elif child["depth"] >= MAX_DEPTH:
                child["terminal_status"] = "depth"

            log_node_created(child)
            made.append(child)

        if made:
            break
        feedback = "Rejected: " + "; ".join(rejected)

    if not made:
        node["pruned"] = True

    return made


# C. EVALUATION
def evaluate(node):
    """Combines LLM state judgment and objective environment reward."""
    if node["terminal_status"] in ("success", "failure"):
        log_evaluation(node, node["reward"], node["reward"], node["reward"])
        return node["reward"]
    v_llm = evaluate_state(node["state"], node["observation"])
    final_v = clamp(BETA * v_llm + (1.0 - BETA) * node["reward"], 0.0, 1.0)
    log_evaluation(node, v_llm, node["reward"], final_v)
    return final_v


# D. SIMULATION
def simulate(node, value):
    """Performs a forward greedy rollout up to ROLLOUT_DEPTH."""
    curr = node
    curr_val = value
    steps = 0
    for _ in range(ROLLOUT_DEPTH):
        if curr["terminal_status"] is not None or curr["depth"] >= MAX_DEPTH:
            break
        kids = expand(curr, 1)
        if not kids:
            break
        steps += 1
        curr = kids[0]
        curr_val = evaluate(curr)
    log_simulation(node, curr, steps, curr_val)
    return curr, curr_val


# E. BACKPROPAGATION
def backprop(node, v):
    """Updates visit count and accumulated value along the path from node to root."""
    path = []
    curr = node
    while curr is not None:
        curr["visits"] += 1
        curr["value_sum"] += v
        path.append(curr)
        curr = curr["parent"]
    log_backpropagation(path, v)


# ==============================================================================
# 6.7 Main Search Loop and Query Functions
# ==============================================================================

def search(root):
    """Runs the main MCTS search loop over ITERATIONS iterations."""
    root["visits"] = 1
    for i in range(ITERATIONS):
        if get_llm_calls() >= MAX_LLM_CALLS:
            break

        log_iteration_header(i + 1, ITERATIONS)
        leaf = select(root)
        log_selection(leaf)

        if leaf["terminal_status"] is not None:
            backprop(leaf, leaf["reward"])
            continue

        kids = expand(leaf, K)
        if not kids:
            backprop(leaf, 0.0)
            continue

        for kid in kids:
            kid["value"] = evaluate(kid)

        best = max(kids, key=lambda ch: ch["value"])

        # Backpropagate non-best children with their immediate evaluations
        for kid in kids:
            if kid is not best:
                backprop(kid, kid["value"])

        # Simulate forward from best child
        end, v = simulate(best, best["value"])
        backprop(end, v)

        # F. REFLECTION on failure or a weak, stalled path
        if end["terminal_status"] in ("failure", "depth") or (end["terminal_status"] is None and v < 0.4):
            end["reflection"] = reflect(end)
            reflections.append(end["reflection"])
            log_reflection(end, end["reflection"])

    return root


def best_conclusions(root):
    """Traverses tree to find all conclude nodes, sorted by (reward, average_value) descending."""
    all_nodes = []
    stack = [root]
    while stack:
        n = stack.pop()
        all_nodes.append(n)
        stack.extend(n.get("children", []))

    concl = [
        n for n in all_nodes
        if n.get("action") and n["action"].get("type") == "conclude"
    ]

    def _sort_key(n):
        avg_val = (n["value_sum"] / n["visits"]) if n["visits"] > 0 else 0.0
        return (n["reward"], avg_val)

    return sorted(concl, key=_sort_key, reverse=True)
