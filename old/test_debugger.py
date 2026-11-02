"""
test_debugger.py - Verification tests for the plain-function LATS debugging agent.
"""

from executor import run_python, run_tests, is_safe
from environment import make_state, validate_action, run_action
from reward import objective_reward, terminal_reward
from mcts import make_node, uct

CODE = """def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)
"""

TESTS = [
    {"call": "calculate_average([])", "expect": ["None", "ValueError"]},
    {"call": "calculate_average([2, 4])", "expect": ["3.0"]},
]


def test_is_safe():
    assert is_safe("print('hello world')")
    assert not is_safe("import os; os.system('ls')")
    assert not is_safe("import subprocess")
    assert not is_safe("open('file.txt', 'w')")
    assert not is_safe("x" * 2500)
    print("test_is_safe passed.")


def test_run_tests_reports_failure():
    res = run_tests(CODE, TESTS)
    assert len(res) == 2
    assert res[0]["status"] == "exception"
    assert not res[0]["passed"]
    assert res[1]["status"] == "result"
    assert res[1]["passed"]
    print("test_run_tests_reports_failure passed.")


def test_validation_rejects_duplicates_and_forbidden():
    st = make_state(CODE, "ZeroDivisionError", TESTS, "")

    # Forbidden snippet
    ok, why = validate_action(st, {"type": "run_snippet", "code": "import os"})
    assert not ok
    assert "unsafe code" in why

    # Unknown type
    ok, why = validate_action(st, {"type": "destroy_all"})
    assert not ok
    assert "unknown type" in why

    # Valid inspect
    valid_act = {"type": "inspect_code", "focus": "return"}
    ok, _ = validate_action(st, valid_act)
    assert ok

    # Duplicate check in actions_taken
    st["actions_taken"].append(valid_act)
    ok, why = validate_action(st, valid_act)
    assert not ok
    assert "duplicate" in why
    print("test_validation_rejects_duplicates_and_forbidden passed.")


def test_patch_fixes_and_updates_hypothesis():
    st = make_state(CODE, "ZeroDivisionError", TESTS, "")
    st, _ = run_action(st, {"type": "hypothesize", "hypothesis": "no empty guard"})
    assert len(st["hypotheses"]) == 1
    assert st["hypotheses"][0]["status"] == "open"

    new_code = """def calculate_average(numbers):
    if not numbers:
        raise ValueError('empty')
    return sum(numbers) / len(numbers)
"""
    patch_action = {"type": "apply_patch", "new_code": new_code, "hypothesis_id": "h1"}
    st, obs = run_action(st, patch_action)

    assert obs["tests_fixed"] == 1
    assert obs["tests_broken"] == 0
    assert obs["hypothesis_supported"]
    assert st["hypotheses"][0]["status"] == "supported"
    print("test_patch_fixes_and_updates_hypothesis passed.")


def test_rewards():
    st1 = make_state(CODE, "ZeroDivisionError", TESTS, "")
    st2 = make_state(CODE, "ZeroDivisionError", TESTS, "")
    st2["evidence"].append({"id": "e1", "kind": "inspect", "text": "line 5"})

    r = objective_reward(st1, st2, {"type": "inspect_code", "focus": "return"}, {"reproduced_error": True})
    # 0.15 for reproduced error + 0.05 for 1 new evidence = 0.20
    assert abs(r - 0.20) < 1e-4

    # Terminal reward
    st2["hypotheses"].append({"id": "h1", "text": "guard", "status": "supported"})
    st2["evidence"].append({"id": "e2", "kind": "patch", "text": "fixed=1 broken=0"})

    rew, status = terminal_reward(st2, {"type": "conclude", "evidence_ids": ["e2"]})
    assert status == "success"
    assert rew == 0.95
    print("test_rewards passed.")


def test_uct():
    parent = {"visits": 20}
    child_unvisited = {"visits": 0, "value_sum": 0.0}
    assert uct(parent, child_unvisited) == float("inf")

    child_visited = {"visits": 5, "value_sum": 2.75}  # avg = 0.55
    val = uct(parent, child_visited)
    assert val > 0.55
    print("test_uct passed.")


if __name__ == "__main__":
    test_is_safe()
    test_run_tests_reports_failure()
    test_validation_rejects_duplicates_and_forbidden()
    test_patch_fixes_and_updates_hypothesis()
    test_rewards()
    test_uct()
    print("\nALL UNIT TESTS PASSED!")

