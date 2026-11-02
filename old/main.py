"""
main.py - Entry point and demonstration of the LATS Debugging Agent.

Functions only (no classes).
Implements Section 6.7 (MAIN) from the LATS pseudocode specification.
"""

from llm_client import llm_call, get_llm_calls, reflections, OLLAMA_MODEL
from environment import make_state
from mcts import make_node, search, best_conclusions
from logger import init_logger, log, log_section, save_run_log_to_file

# Target buggy function
CODE = """def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)
"""

# Test suite specifications
TESTS = [
    {"call": "calculate_average([])", "expect": ["None", "ValueError"]},
    {"call": "calculate_average([1, 2, 3])", "expect": ["2.0"]},
    {"call": "calculate_average([-2, 2])", "expect": ["0.0"]},
]

ERROR = "ZeroDivisionError: division by zero"

EXPECTED = "Handle empty input per a clear contract: return None or raise a meaningful ValueError."


def print_tree(node, prefix="", is_last=True):
    """Visualizes the MCTS search tree in the terminal using ASCII characters."""
    avg_v = (node["value_sum"] / node["visits"]) if node["visits"] > 0 else 0.0
    action_desc = "ROOT"
    if node["action"]:
        a_type = node["action"]["type"]
        if a_type == "inspect_code":
            action_desc = f"inspect('{node['action'].get('focus')}')"
        elif a_type == "run_snippet":
            action_desc = f"snippet('{node['action'].get('code')}')"
        elif a_type == "run_tests":
            action_desc = "run_tests"
        elif a_type == "hypothesize":
            action_desc = f"hyp('{node['action'].get('hypothesis')[:25]}...')"
        elif a_type == "apply_patch":
            action_desc = f"patch('{node['action'].get('hypothesis_id')}')"
        elif a_type == "conclude":
            action_desc = f"conclude('{node['action'].get('root_cause')[:25]}...')"
        else:
            action_desc = a_type

    status_flag = f" [{node['terminal_status'].upper()}]" if node["terminal_status"] else ""
    branch = "\\-- " if is_last else "|-- "
    line = f"{prefix}{branch}[Node #{node['id']}] {action_desc} (N={node['visits']}, Q={avg_v:.2f}, R={node['reward']:.2f}){status_flag}"
    log(line)

    children = node.get("children", [])
    for i, child in enumerate(children):
        last_child = (i == len(children) - 1)
        next_prefix = prefix + ("    " if is_last else "|   ")
        print_tree(child, next_prefix, last_child)


def run_debugging_agent(log_filename="lats_run_log.md"):
    """Runs the LATS Debugging process end-to-end with live and file logging."""
    init_logger(log_filename)

    log_section("LATS Debugging Agent Starting")
    log(f"Model configured: {OLLAMA_MODEL}")
    log(f"Initial Code:\n```python\n{CODE.strip()}\n```")
    log(f"Observed Error: {ERROR}")
    log(f"Contract Goal:  {EXPECTED}\n")

    # 1. Initialize root state and node
    state = make_state(CODE, ERROR, TESTS, EXPECTED)
    root = make_node(None, state, None, None, 0.0)

    # 2. Run MCTS search
    log("[PHASE 1] Starting MCTS Search loop...")
    search(root)
    log(f"\n[PHASE 2] Search Complete. Total LLM calls: {get_llm_calls()}. Reflections: {len(reflections)}.")

    # 3. Print the Tree structure
    log_section("Search Tree Exploration Visualization")
    print_tree(root)

    # 4. Extract best conclusions
    conclusions = best_conclusions(root)
    best = conclusions[0] if conclusions else None

    log_section("Result & Root Cause Report")

    if best is None:
        all_children = root.get("children", [])
        best_child = max(all_children, key=lambda n: (n["value_sum"] / n["visits"]) if n["visits"] else 0.0) if all_children else root
        avg_q = (best_child["value_sum"] / best_child["visits"]) if best_child["visits"] else 0.0
        log(f"STATUS: Inconclusive (no validated conclude action reached)")
        log(f"Highest-average-value child node: #{best_child['id']} with Q={avg_q:.2f}")
        log(f"\nEvidence gathered ({len(root['state'].get('evidence', []))} items):")
        for ev in root["state"].get("evidence", []):
            log(f"  [{ev['id']}] ({ev['kind']}) {ev['text']}")
        log(f"\nOpen hypotheses:")
        for h in root["state"].get("hypotheses", []):
            log(f"  [{h['id']}] {h['text']} (status: {h['status']})")
    else:
        system = (
            "Write a root cause report using ONLY the listed evidence, "
            "cite evidence ids, state uncertainty, treat None-vs-ValueError "
            "as a product decision"
        )
        evidence_str = "\n".join(
            f"[{e['id']}] ({e['kind']}) {e['text']}" for e in best["state"]["evidence"]
        )
        user_prompt = (
            f"ROOT CAUSE: {best['action'].get('root_cause', '')}\n\n"
            f"RECORDED EVIDENCE:\n{evidence_str}\n\n"
            f"FINAL CODE:\n{best['state']['current_code']}"
        )
        report = llm_call(system, user_prompt, temperature=0.2)
        log(report)

    # 5. Save the complete log file
    save_run_log_to_file(log_filename)


if __name__ == "__main__":
    run_debugging_agent()
