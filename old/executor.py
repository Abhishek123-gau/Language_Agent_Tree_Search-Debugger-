"""
executor.py - Sandboxed code and test execution engine.

Functions only (no classes).
Implements Section 6.2 from the LATS pseudocode specification.
"""

import os
import subprocess
import sys
import tempfile
from llm_client import TIMEOUT

FORBIDDEN = [
    "import os",
    "import subprocess",
    "import socket",
    "import shutil",
    "from os",
    "from subprocess",
    "open(",
    "exec(",
    "eval(",
    "sys.exit",
    "__import__",
]

HARNESS_TEMPLATE = """
import traceback
try:
    _res = {call}
    print("__RESULT__", repr(_res))
except BaseException as _e:
    print("__EXC__", type(_e).__name__, str(_e))
"""


def is_safe(snippet):
    """Checks whether a code snippet avoids forbidden imports/calls and length limit."""
    low = snippet.lower()
    for f in FORBIDDEN:
        if f in low:
            return False
    return len(snippet) <= 2000


def run_python(source):
    """Runs source code in a separate process with a TIMEOUT limit."""
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(source)
            temp_file = f.name

        proc = subprocess.run(
            [sys.executable, "-I", temp_file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        return {
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-2000:],
            "returncode": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "TimeoutExpired",
            "returncode": -1,
            "timed_out": True,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
            "timed_out": False,
        }
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def run_tests(code, tests):
    """Runs test cases against code in isolated processes and returns structured results."""
    results = []
    for test in tests:
        # test is a dict: {"call": "...", "expect": [...] or "expect_list": [...]}
        call = test["call"]
        expect_list = test.get("expect") or test.get("expect_list") or []

        harness = HARNESS_TEMPLATE.format(call=call)
        out = run_python(code + "\n" + harness)

        if out["timed_out"]:
            status = "timeout"
            got = "timeout"
            passed = False
        else:
            res_line = next((l for l in out["stdout"].splitlines() if l.startswith("__")), "")
            if res_line.startswith("__RESULT__"):
                status = "result"
                got = res_line[len("__RESULT__ "):].strip()
            elif res_line.startswith("__EXC__"):
                status = "exception"
                got = res_line.split()[1]
            else:
                status = "crash"
                got = out["stderr"][-200:] if out["stderr"] else "unknown error"

            # Check if got value or exception type matches expectations
            if expect_list:
                passed = got in expect_list
            else:
                passed = (status == "result")

        results.append({
            "call": call,
            "status": status,
            "got": got,
            "expect": expect_list,
            "passed": passed,
        })

    return results

