"""
logger.py - Logging system for LATS Debugging Agent.

Functions only (no classes).
Provides live formatted terminal logs and generates a structured run log file.
"""

import os
from datetime import datetime

_log_lines = []
_log_file_path = "lats_run_log.md"


def init_logger(log_file="lats_run_log.md"):
    """Initializes the logging system and resets in-memory logs."""
    global _log_lines, _log_file_path
    _log_lines = []
    _log_file_path = log_file


def _timestamp():
    return datetime.now().strftime("%H:%M:%S")


def log(msg="", console=True):
    """Logs a line to terminal and appends to the log buffer."""
    global _log_lines
    if console:
        print(msg)
    _log_lines.append(msg)


def log_section(title, char="="):
    """Logs a prominent section header."""
    bar = char * 70
    log(f"\n{bar}", console=True)
    log(f" {title.upper()}", console=True)
    log(f"{bar}", console=True)


def log_iteration_header(iter_num, max_iters):
    """Logs the start of an MCTS search iteration."""
    log(f"\n{'='*70}")
    log(f" [ITERATION {iter_num}/{max_iters}] - {_timestamp()}")
    log(f"{'='*70}")


def log_selection(leaf_node):
    """Logs the node selected by UCT descent."""
    parent_id = leaf_node["parent"]["id"] if leaf_node.get("parent") else "None"
    avg_v = (leaf_node["value_sum"] / leaf_node["visits"]) if leaf_node["visits"] > 0 else 0.0
    action_type = leaf_node["action"]["type"] if leaf_node.get("action") else "ROOT"
    log(f"\n[1. SELECTION]")
    log(f"  -> Selected Node #{leaf_node['id']} (depth={leaf_node['depth']}, action={action_type}, parent=#{parent_id})")
    log(f"     Visits N={leaf_node['visits']}, Value Sum W={leaf_node['value_sum']:.3f}, Avg Q={avg_v:.3f}")


def log_expansion_start(node_id, num_actions):
    """Logs start of expansion."""
    log(f"\n[2. EXPANSION]")
    log(f"  -> Node #{node_id}: Proposing {num_actions} candidate actions...")


def log_action_validation(action, ok, why=""):
    """Logs action validation outcome."""
    a_type = action.get("type", "unknown")
    if ok:
        log(f"  - Action [{a_type}]: VALID")
    else:
        log(f"  - Action [{a_type}]: REJECTED -> {why}")


def log_node_created(node):
    """Logs the creation and execution outcome of a newly generated tree node."""
    a = node.get("action") or {}
    a_type = a.get("type", "ROOT")
    obs = node.get("observation") or {}
    obs_text = obs.get("text", "").replace("\n", " ")
    if len(obs_text) > 120:
        obs_text = obs_text[:117] + "..."

    log(f"    * Generated Node #{node['id']} (depth={node['depth']}):")
    log(f"      Action: {a_type} -> rationale: \"{a.get('rationale', '')}\"")
    if a_type == "run_snippet":
        log(f"      Snippet: {a.get('code')}")
    elif a_type == "inspect_code":
        log(f"      Focus: '{a.get('focus')}'")
    elif a_type == "hypothesize":
        log(f"      Hypothesis: '{a.get('hypothesis')}'")
    elif a_type == "apply_patch":
        log(f"      Target Hyp: {a.get('hypothesis_id')}")

    log(f"      Observation: \"{obs_text}\"")
    flags = []
    if obs.get("reproduced_error"):
        flags.append("REPRODUCED_ERROR")
    if obs.get("hypothesis_supported"):
        flags.append("HYPOTHESIS_SUPPORTED")
    if obs.get("hypothesis_refuted"):
        flags.append("HYPOTHESIS_REFUTED")
    if obs.get("tests_fixed", 0) > 0:
        flags.append(f"FIXED_{obs['tests_fixed']}_TESTS")
    if obs.get("tests_broken", 0) > 0:
        flags.append(f"BROKEN_{obs['tests_broken']}_TESTS")
    if flags:
        log(f"      Signals: [{', '.join(flags)}]")

    term = f" -> TERMINAL: {node['terminal_status'].upper()}" if node.get("terminal_status") else ""
    log(f"      Step Reward: {node['reward']:.2f}{term}")


def log_evaluation(node, v_llm, r_obj, final_v):
    """Logs the hybrid evaluation of a node."""
    a_type = node["action"]["type"] if node.get("action") else "ROOT"
    log(f"  [3. EVALUATION] Node #{node['id']} ({a_type}):")
    log(f"      LLM Quality Score (V_llm) = {v_llm:.3f}")
    log(f"      Objective Reward  (R_obj) = {r_obj:.3f}")
    log(f"      Blended Value     (V)     = {final_v:.3f} (0.5 * V_llm + 0.5 * R_obj)")


def log_simulation(start_node, end_node, rollout_steps, final_v):
    """Logs the simulation rollout."""
    log(f"\n[4. SIMULATION]")
    log(f"  -> Rolling out greedily from best child Node #{start_node['id']}...")
    log(f"     Steps taken: {rollout_steps}, reached Node #{end_node['id']} (depth={end_node['depth']})")
    status = end_node.get("terminal_status") or "active"
    log(f"     Rollout outcome: status={status.upper()}, value={final_v:.3f}")


def log_backpropagation(path, value):
    """Logs backpropagation up the tree."""
    path_ids = " -> ".join(f"#{n['id']}" for n in reversed(path))
    log(f"\n[5. BACKPROPAGATION]")
    log(f"  -> Pushing value {value:.3f} along path: {path_ids}")
    for n in path:
        avg_q = n["value_sum"] / max(n["visits"], 1)
        a_desc = n["action"]["type"] if n.get("action") else "ROOT"
        log(f"     Node #{n['id']} ({a_desc}): visits={n['visits']}, W={n['value_sum']:.2f}, Q={avg_q:.2f}")


def log_reflection(node, reflection_text):
    """Logs reflection triggered by a failed or stalled path."""
    log(f"\n[6. REFLECTION]")
    log(f"  -> Stalled or failed branch detected at Node #{node['id']} (status={node.get('terminal_status')})")
    log(f"     Lesson learned:")
    for line in reflection_text.strip().splitlines():
        log(f"       \"{line}\"")


def save_run_log_to_file(filepath=None):
    """Writes the accumulated log to a structured markdown file."""
    path = filepath or _log_file_path
    try:
        content = "\n".join(_log_lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log(f"\n[OK] Run log successfully saved to: {os.path.abspath(path)}")
        return path
    except Exception as e:
        log(f"\n[ERROR] Failed to save log file: {e}")
        return None

