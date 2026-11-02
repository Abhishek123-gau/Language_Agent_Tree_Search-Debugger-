# lats_debugger.py  -  LATS debugging / root-cause agent. Functions + dicts only, no classes.
# Setup:  pip install anthropic     |     export ANTHROPIC_API_KEY=...
# Run:    python lats_debugger.py
import json, re, math, copy, subprocess, sys, tempfile, os
import urllib.request, urllib.error
from logger import (
    init_logger, log, log_section, log_iteration_header, log_selection,
    log_expansion_start, log_action_validation, log_node_created,
    log_evaluation, log_simulation, log_backpropagation, log_reflection,
    save_run_log_to_file
)

# ---------------- settings (Ollama) ----------------
# Available local models: 'qwen2.5-coder:7b', 'deepseek-coder:6.7b', 'qwen2.5-coder:0.5b', 'qwen2.5:7b'
OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5-coder:7b')

C, K, MAX_DEPTH, ITERATIONS, ROLLOUT_DEPTH = 1.0, 3, 6, 10, 2
LAMBDA, BETA, M_SAMPLES, MAX_LLM_CALLS, TIMEOUT = 0.5, 0.5, 2, 200, 5
llm_calls = 0
reflections = []      # lessons from failed paths
next_id = 0

POLICY_PROMPT = """You are the policy of a debugging agent. Propose DIVERSE next debugging actions.
You never run code or state results; the environment does that.
Return JSON with your reasoning and next actions:
{
  "reasoning": "Step-by-step thinking: what was observed, what is the current bottleneck, and why these actions are chosen...",
  "actions": [
    {"type": "...", "rationale": "..."}
  ]
}
Types:
 inspect_code {focus}            - show source lines containing the text in focus
 run_snippet {code}              - run a python snippet (the current code is already loaded)
 run_tests {}                    - run all tests
 hypothesize {hypothesis}        - record a possible root cause
 apply_patch {new_code, hypothesis_id} - test a hypothesis by replacing the full code and re-running tests
 conclude {root_cause, evidence_ids}   - final answer; cite evidence ids; only after a hypothesis was tested
Rules: Think step-by-step in 'reasoning'; actions must differ in purpose; do not repeat ACTIONS TAKEN; use lessons from failed paths; no file/network/os access in snippets."""

EVALUATOR_PROMPT = """You score a debugging investigation STATE (not just the code).
Judge evidence collected, hypothesis quality, progress toward the root cause, relevance, and reduced uncertainty.
Evidence is ground truth from real execution; hypotheses are unverified. Penalize unsupported claims.
Return JSON: {"score": 0..1, "leading_hypothesis_id": "h1" or null, "critique": "..."}"""

REFLECTION_PROMPT = """A debugging path failed or stalled. Using ONLY the steps shown, write 3-4 sentences:
what was tried, why it did not help, and what different kind of action to try next. Never invent results."""


# ---------------- LLM helpers (Ollama with Live Streaming) ----------------
def _call_ollama_stream(messages, temperature=0.7, json_format=False, label=""):
    """Streams tokens word-by-word to the terminal live as the LLM reasons."""
    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature}
    }
    if json_format:
        payload["format"] = "json"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    if label:
        print(f"\n  >>> {label}:\n  ", end="", flush=True)

    accumulated = []
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line.decode("utf-8"))
                token = chunk.get("message", {}).get("content", "")
                if token:
                    # Live token-by-token print
                    print(token, end="", flush=True)
                    accumulated.append(token)
        print()  # newline when generation completes
    except Exception as e:
        print(f"\n  [Streaming Error: {e}]")
        return ""

    full_text = "".join(accumulated)
    if label:
        log(f"  [{label}]:\n{full_text}", console=False)
    return full_text


def llm_call(system, user, temperature=0.7, label="[LLM Generating Thought]"):
    global llm_calls
    llm_calls += 1
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    return _call_ollama_stream(messages, temperature=temperature, json_format=False, label=label)


def llm_json(system, user, temperature=0.7, label="[LLM Reasoning & Output]"):
    global llm_calls
    messages = []
    if system:
        messages.append({"role": "system", "content": system + "\nReturn ONLY valid JSON."})
    messages.append({"role": "user", "content": user})
    for attempt in range(3):
        llm_calls += 1
        att_label = f"{label} (Attempt {attempt + 1})" if attempt > 0 else label
        text = _call_ollama_stream(messages, temperature=temperature, json_format=True, label=att_label)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except json.JSONDecodeError:
                        pass
    return None


# ---------------- running code ----------------
def is_safe(snippet):
    bad = ["import os", "subprocess", "socket", "shutil", "open(", "exec(", "eval(", "sys.exit", "__import__"]
    return len(snippet) <= 2000 and not any(b in snippet for b in bad)


def run_python(source):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.py")
        open(path, "w").write(source)
        try:
            r = subprocess.run([sys.executable, "-I", path], capture_output=True, text=True,
                               timeout=TIMEOUT, cwd=d)
            return {"stdout": r.stdout[-1500:], "stderr": r.stderr[-1500:], "timed_out": False}
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "Timeout", "timed_out": True}


def run_tests(code, tests):
    results = []
    for t in tests:   # t = {"call": "f([])", "expect": ["None", "ValueError"]}
        script = code + "\n\ntry:\n    print('RESULT', repr(" + t["call"] + "))\n" \
                 "except BaseException as e:\n    print('EXC', type(e).__name__)\n"
        out = run_python(script)
        got = "timeout" if out["timed_out"] else "crash"
        for line in out["stdout"].splitlines():
            if line.startswith("RESULT "): got = line[7:]
            elif line.startswith("EXC "): got = line[4:]
        results.append({"call": t["call"], "got": got, "passed": got in t["expect"]})
    return results


# ---------------- state and actions ----------------
def make_state(code, error, tests, expected):
    return {"current_code": code, "error": error, "tests": tests, "expected": expected,
            "test_results": [], "hypotheses": [], "actions_taken": [], "evidence": []}


def render_state(s):
    tr = "\n".join(f"  {r['call']} -> {r['got']} {'PASS' if r['passed'] else 'FAIL'}" for r in s["test_results"]) or "  (not run)"
    hy = "\n".join(f"  [{h['id']}] {h['text']} ({h['status']})" for h in s["hypotheses"]) or "  (none)"
    ev = "\n".join(f"  [{e['id']}] ({e['kind']}) {e['text']}" for e in s["evidence"][-10:]) or "  (none)"
    ac = "\n".join(f"  - {a}" for a in s["actions_taken"][-10:]) or "  (none)"
    return (f"CODE:\n{s['current_code']}\n\nERROR: {s['error']}\nEXPECTED: {s['expected']}\n\n"
            f"TEST RESULTS:\n{tr}\n\nHYPOTHESES:\n{hy}\n\nEVIDENCE (real execution):\n{ev}\n\nACTIONS TAKEN:\n{ac}")


def action_key(a):
    return json.dumps({k: v for k, v in a.items() if k != "rationale"}, sort_keys=True)


def add_evidence(s, kind, text):
    s["evidence"].append({"id": f"e{len(s['evidence']) + 1}", "kind": kind, "text": text[:400]})


REQUIRED = {"inspect_code": ["focus"], "run_snippet": ["code"], "run_tests": [],
            "hypothesize": ["hypothesis"], "apply_patch": ["new_code", "hypothesis_id"],
            "conclude": ["root_cause", "evidence_ids"]}


def validate_action(s, a, sibling_keys):
    if not isinstance(a, dict) or a.get("type") not in REQUIRED: return False, "unknown type"
    if a["type"] == "hypothesize" and not a.get("hypothesis"):
        a["hypothesis"] = a.get("rationale") or a.get("description") or "unspecified hypothesis"
    if a["type"] == "inspect_code" and not a.get("focus"):
        a["focus"] = a.get("rationale") or "return"
    if a["type"] == "run_snippet" and not a.get("code"):
        a["code"] = a.get("snippet")

    if any(not a.get(f) for f in REQUIRED[a["type"]]): return False, "missing field"
    key = action_key(a)
    if key in s["actions_taken"] or key in sibling_keys: return False, "duplicate"
    for f in ("code", "new_code"):
        if f in a and not is_safe(a[f]): return False, "unsafe code"
    if a["type"] == "conclude":
        ids = {e["id"] for e in s["evidence"]}
        if not isinstance(a.get("evidence_ids"), list) or not a["evidence_ids"] or not set(a["evidence_ids"]) <= ids:
            return False, "unknown or empty evidence"
    return True, ""


def run_action(state, a):
    """The ONLY place observations are created. Returns (new_state, obs)."""
    s = copy.deepcopy(state)
    s["actions_taken"].append(action_key(a))
    obs = {"text": "", "reproduced_error": False, "tests_fixed": 0, "tests_broken": 0,
           "hypothesis_supported": False, "hypothesis_refuted": False}
    t = a["type"]
    if t == "inspect_code":
        lines = s["current_code"].splitlines()
        hits = [f"{i + 1}: {l}" for i, l in enumerate(lines) if str(a["focus"]).lower() in l.lower()]
        obs["text"] = "\n".join(hits[:15]) or "no matching lines"
        add_evidence(s, "inspect", obs["text"])
    elif t == "run_snippet":
        out = run_python(s["current_code"] + "\n" + a["code"])
        obs["text"] = (out["stdout"] + out["stderr"]).strip()[-400:]
        obs["reproduced_error"] = s["error"].split(":")[0] in out["stderr"]
        add_evidence(s, "run", f"ran `{a['code'][:100]}` -> {obs['text'] or '(no output)'}")
    elif t == "run_tests":
        s["test_results"] = run_tests(s["current_code"], s["tests"])
        obs["text"] = "; ".join(f"{r['call']} -> {r['got']} {'PASS' if r['passed'] else 'FAIL'}" for r in s["test_results"])
        add_evidence(s, "tests", obs["text"])
    elif t == "hypothesize":
        s["hypotheses"].append({"id": f"h{len(s['hypotheses']) + 1}", "text": a["hypothesis"], "status": "open"})
        obs["text"] = "recorded, unverified"      # no evidence added
    elif t == "apply_patch":
        before = run_tests(s["current_code"], s["tests"])
        after = run_tests(a["new_code"], s["tests"])
        obs["tests_fixed"] = sum(1 for b, c in zip(before, after) if not b["passed"] and c["passed"])
        obs["tests_broken"] = sum(1 for b, c in zip(before, after) if b["passed"] and not c["passed"])
        s["current_code"], s["test_results"] = a["new_code"], after
        ok = obs["tests_fixed"] > 0 and obs["tests_broken"] == 0
        for h in s["hypotheses"]:
            if h["id"] == a["hypothesis_id"]:
                h["status"] = "supported" if ok else "refuted"
        obs["hypothesis_supported"], obs["hypothesis_refuted"] = ok, not ok
        obs["text"] = f"patch: fixed={obs['tests_fixed']} broken={obs['tests_broken']}"
        add_evidence(s, "patch", obs["text"])
    elif t == "conclude":
        obs["text"] = "conclusion recorded"
    return s, obs


# ---------------- rewards ----------------
def objective_reward(old, new, a, obs):
    r = 0.0
    if obs["reproduced_error"]: r += 0.15
    new_ev = len(new["evidence"]) - len(old["evidence"])
    r += min(0.15, 0.05 * new_ev)
    if obs["hypothesis_supported"]: r += 0.20
    if obs["hypothesis_refuted"]: r += 0.10
    if obs["tests_fixed"] > 0: r += 0.30
    if obs["tests_broken"] > 0: r -= 0.20
    if new_ev == 0 and a["type"] not in ("hypothesize", "conclude"): r -= 0.10
    return max(0.0, min(1.0, r))


def terminal_reward(s, a):
    ev = {e["id"]: e for e in s["evidence"]}
    patch_ok = any(ev[i]["kind"] == "patch" and "fixed=0" not in ev[i]["text"] for i in a["evidence_ids"] if i in ev)
    supported = any(h["status"] == "supported" for h in s["hypotheses"])
    if supported and patch_ok: return 0.95, "success"
    if supported or len(a["evidence_ids"]) >= 2: return 0.5, "success"
    return 0.1, "failure"


# ---------------- the three LLM jobs ----------------
def propose_actions(state, k, feedback=""):
    user = (render_state(state) + "\n\nLESSONS FROM FAILED PATHS:\n" +
            ("\n".join("- " + r for r in reflections[-5:]) or "(none)") +
            f"\n\nPropose {k} diverse actions as JSON." + (f"\nRejected before: {feedback}" if feedback else ""))
    out = llm_json(POLICY_PROMPT, user, 0.8, label="Policy LLM: Proposing Actions & Reasoning")
    if out and isinstance(out, dict) and "reasoning" in out:
        log(f"  [Policy Thought Summary]: {out['reasoning']}", console=True)
    return (out or {}).get("actions", [])[:k]


def evaluate_state(state, last_obs):
    samples = []
    for idx in range(M_SAMPLES):
        out = llm_json(EVALUATOR_PROMPT, render_state(state) + "\n\nLAST OBSERVATION:\n" + last_obs.get("text", ""), 0.8, label=f"Evaluator LLM: Scoring State (Sample {idx+1}/{M_SAMPLES})")
        if out and isinstance(out.get("score"), (int, float)):
            samples.append(out)
    if not samples: return 0.3
    lm = sum(max(0, min(1, x["score"])) for x in samples) / len(samples)
    leads = [x.get("leading_hypothesis_id") for x in samples]
    sc = max(leads.count(l) for l in leads) / len(samples)       # self-consistency
    return LAMBDA * lm + (1 - LAMBDA) * sc


def trajectory(node):
    path = []
    while node["parent"] is not None:
        path.append((node["action"], node["observation"]["text"]))
        node = node["parent"]
    return path[::-1]


def reflect(node):
    steps = "\n".join(f"{i + 1}. {a['type']} {action_key(a)[:150]} -> {o[:150]}" for i, (a, o) in enumerate(trajectory(node)))
    return llm_call(REFLECTION_PROMPT, steps + "\n\n" + render_state(node["state"]), 0.4, label="Reflection LLM: Analyzing Stalled Trajectory").strip()


# ---------------- LATS ----------------
def make_node(parent, state, action, obs, reward):
    global next_id
    next_id += 1
    node = {"id": next_id, "parent": parent, "children": [], "state": state, "action": action,
            "observation": obs, "depth": parent["depth"] + 1 if parent else 0, "visits": 0,
            "value_sum": 0.0, "reward": reward, "reflection": None, "terminal_status": None, "pruned": False}
    if parent: parent["children"].append(node)
    return node


def uct(parent, child):
    if child["visits"] == 0: return float("inf")
    return child["value_sum"] / child["visits"] + C * math.sqrt(math.log(max(parent["visits"], 1)) / child["visits"])


def select(root):                                                   # A. selection
    node = root
    step = 0
    log(f"\n[1. SELECTION - UCT Tree Descent]")
    while node["children"] and node["terminal_status"] is None:
        step += 1
        live = [c for c in node["children"] if not c["pruned"]]
        if not live:
            node["pruned"] = True
            log(f"  -> All children of Node #{node['id']} pruned. Backtracking to root.")
            return root

        # Log all child candidates and their UCT values at this level
        candidates = []
        for c in live:
            u = uct(node, c)
            q = c["value_sum"] / max(c["visits"], 1)
            act_name = c["action"]["type"] if c.get("action") else "ROOT"
            candidates.append((c, act_name, u, q, c["visits"]))

        best_child = max(live, key=lambda c: uct(node, c))

        log(f"  -> Step {step}: At Node #{node['id']} (depth {node['depth']}), evaluating {len(live)} children:")
        for c, act_name, u, q, vis in candidates:
            selected_tag = " <=== HIGHEST UCT (PICKED)" if c["id"] == best_child["id"] else ""
            log(f"     * Child Node #{c['id']} [{act_name}]: UCT={u:.3f} (Q={q:.3f}, N={vis}){selected_tag}")

        node = best_child

    action_desc = node["action"]["type"] if node.get("action") else "ROOT"
    parent_id = node["parent"]["id"] if node.get("parent") else "None"
    avg_q = node["value_sum"] / max(node["visits"], 1)
    log(f"  -> REACHED LEAF Node #{node['id']} at depth {node['depth']} (action={action_desc}, parent=#{parent_id})")
    log(f"     Leaf Statistics: visits N={node['visits']}, avg Q={avg_q:.3f}, value_sum W={node['value_sum']:.3f}")
    return node


def expand(node, k):                                                # B. expansion
    made, feedback = [], ""
    log_expansion_start(node["id"], k)
    for _ in range(2):
        sibling_keys = {action_key(c["action"]) for c in node["children"]}
        rejected = []
        for a in propose_actions(node["state"], k, feedback):
            ok, why = validate_action(node["state"], a, sibling_keys)
            log_action_validation(a, ok, why)
            if not ok:
                rejected.append(why)
                continue
            new_state, obs = run_action(node["state"], a)           # real execution
            r = objective_reward(node["state"], new_state, a, obs)
            child = make_node(node, new_state, a, obs, r)
            sibling_keys.add(action_key(a))
            if a["type"] == "conclude":
                child["reward"], child["terminal_status"] = terminal_reward(new_state, a)
            elif child["depth"] >= MAX_DEPTH:
                child["terminal_status"] = "depth"
            log_node_created(child)
            made.append(child)
        if made: break
        feedback = "; ".join(rejected)
    if not made: node["pruned"] = True
    return made


def evaluate(node):                                                 # C. evaluation
    if node["terminal_status"] in ("success", "failure"):
        log_evaluation(node, node["reward"], node["reward"], node["reward"])
        return node["reward"]
    v_llm = evaluate_state(node["state"], node["observation"])
    final_v = max(0.0, min(1.0, BETA * v_llm + (1 - BETA) * node["reward"]))
    log_evaluation(node, v_llm, node["reward"], final_v)
    return final_v


def simulate(node, value):                                          # D. simulation
    for _ in range(ROLLOUT_DEPTH):
        if node["terminal_status"] is not None or node["depth"] >= MAX_DEPTH: break
        kids = expand(node, 1)
        if not kids: break
        node = kids[0]
        value = evaluate(node)
    return node, value


def backprop(node, v):                                              # E. backpropagation
    path = []
    curr = node
    while curr is not None:
        curr["visits"] += 1
        curr["value_sum"] += v
        path.append(curr)
        curr = curr["parent"]
    log_backpropagation(path, v)


def search(root):
    root["visits"] = 1
    for i in range(ITERATIONS):
        if llm_calls >= MAX_LLM_CALLS: break
        log_iteration_header(i + 1, ITERATIONS)
        leaf = select(root)
        if leaf["terminal_status"] is not None:
            backprop(leaf, leaf["reward"]); continue
        kids = expand(leaf, K)
        if not kids:
            backprop(leaf, 0.0); continue
        for kid in kids: kid["value"] = evaluate(kid)
        best = max(kids, key=lambda c: c["value"])
        for kid in kids:
            if kid is not best: backprop(kid, kid["value"])
        end, v = simulate(best, best["value"])
        backprop(end, v)
        if end["terminal_status"] in ("failure", "depth") or (end["terminal_status"] is None and v < 0.4):
            end["reflection"] = reflect(end)                        # F. reflection
            reflections.append(end["reflection"])
            log_reflection(end, end["reflection"])
        log(f"iter {i + 1}: LLM calls={llm_calls}, best value={best['value']:.2f}")
    return root


def best_conclusions(root):
    found, stack = [], [root]
    while stack:
        n = stack.pop()
        stack.extend(n["children"])
        if n["action"] and n["action"]["type"] == "conclude":
            found.append(n)
    return sorted(found, key=lambda n: (n["reward"], n["value_sum"] / max(n["visits"], 1)), reverse=True)


# ---------------- main ----------------
if __name__ == "__main__":
    init_logger("lats_run_log.md")
    log_section("LATS Debugging Agent Starting (app3.py)")
    log(f"Configured Model: {MODEL}")
    code = """def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)
"""
    tests = [{"call": "calculate_average([])", "expect": ["None", "ValueError"]},
             {"call": "calculate_average([1, 2, 3])", "expect": ["2.0"]},
             {"call": "calculate_average([-2, 2])", "expect": ["0.0"]}]
    state = make_state(code, "ZeroDivisionError: division by zero", tests,
                       "Handle empty input per a clear contract: return None or raise a meaningful ValueError.")
    root = make_node(None, state, None, {"text": "start"}, 0.0)
    search(root)

    best = (best_conclusions(root) or [None])[0]
    log_section("Final Conclusion")
    if best is None:
        log("\nINCONCLUSIVE. Evidence gathered on the most-visited path:")
        node = root
        while node["children"]:
            node = max(node["children"], key=lambda c: c["visits"])
        log(render_state(node["state"]))
    else:
        ev = "\n".join(f"[{e['id']}] {e['text']}" for e in best["state"]["evidence"])
        report = llm_call("Write a root cause report using ONLY the evidence listed. Cite evidence ids. "
                          "State remaining uncertainty. Treat None-vs-ValueError as a product decision.",
                          f"CONCLUSION: {best['action']['root_cause']}\nEVIDENCE:\n{ev}\nFINAL CODE:\n{best['state']['current_code']}", 0.2)
        log("\n" + report)
    save_run_log_to_file("lats_run_log.md")