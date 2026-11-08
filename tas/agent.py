"""The agent loop: Vultr Serverless Inference plans, agent-guardrails gates, the sandbox executes,
and a verifier checks the final claim against the receipts before it reaches the user."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from agent_guardrails import Guard

from .sandbox import Sandbox

API = os.environ.get("VULTR_INFERENCE_URL", "https://api.vultrinference.com/v1")
MODEL = os.environ.get("TAS_MODEL", "glm-5.3")
VERIFIER_MODEL = os.environ.get("TAS_VERIFIER_MODEL", "qwen3.8-27b")
VERIFIER_FALLBACK = os.environ.get("TAS_VERIFIER_FALLBACK", "deepseek-v4.1-flash-1017")
POLICY = os.environ.get("TAS_POLICY", os.path.join(os.path.dirname(__file__), "..", "policy.yaml"))
MAX_STEPS = 10

SYSTEM = (
    "You are an operations agent. You can only act through the tools. Every tool call runs in a disposable, "
    "network-less Linux sandbox (working dir /work) and is checked by a policy first; a call may be denied or "
    "held for human approval. Do the task, then give a short final answer. Only claim what tool output showed."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "run_shell", "description": "Run a shell command in the sandbox. Returns stdout, stderr, exit_code.",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write a text file in the sandbox (path must be under /work/).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]


def chat(messages, model=MODEL, tools=None, max_tokens=1200):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(API + "/chat/completions", data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + os.environ["VULTR_INFERENCE_API_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]


class Task:
    def __init__(self, task_id: str, prompt: str, raw: bool = False):
        self.id, self.prompt, self.raw = task_id, prompt, raw
        self.events: list[dict] = []
        self.status = "running"
        self.answer = ""
        self.verdict: dict | None = None
        self.guard = Guard.local(POLICY, agent="sandbox-agent", on_pending="wait", wait_timeout=300,
                                 poll_interval=1.0)
        self.gate = self.guard.client.gate
        self.run_id = None
        self.sandbox = Sandbox(task_id)
        self.receipts: list[dict] = []
        self.lock = threading.Lock()

        @self.guard.tool(name="run_shell")
        def run_shell(cmd: str):
            return self._receipt(self.sandbox.run(cmd))

        @self.guard.tool(name="write_file")
        def write_file(path: str, content: str):
            return self._receipt(self.sandbox.write(path, content))

    def _receipt(self, r):
        r["id"] = f"R{len(self.receipts) + 1}"
        self.receipts.append(r)
        return r

    def log(self, kind, **data):
        with self.lock:
            self.events.append({"t": round(time.time(), 2), "kind": kind, **data})

    def pending(self):
        return self.gate.pending() if self.status == "running" else []

    def decide(self, call_id, decision, who):
        from agent_guardrails import Approver
        out = self.gate.decide(call_id, decision, Approver("dashboard", who))
        self.log("human", decision=decision, call=call_id, approver=who)
        return out

    # ---------------------------------------------------------------
    def run(self):
        try:
            self.sandbox.start()
            self.log("sandbox", msg=f"container {self.sandbox.name} started: no network, no secrets, "
                                    "read-only root, 256 MB / 0.5 CPU / 64 PIDs, non-root")
            with self.guard.run(self.prompt) as run:
                self.run_id = run["id"]
                self._raw() if self.raw else self._loop()
            self.verdict = verify(self.prompt, self.answer, self.receipts)
            self.log("verifier", **self.verdict)
            self.status = "done"
        except Exception as e:
            self.status = "error"
            self.log("error", msg=f"{type(e).__name__}: {e}")
        finally:
            self.sandbox.destroy()
            self.log("sandbox", msg=f"container {self.sandbox.name} destroyed")
            if self.run_id:
                rep = self.gate.replay(self.run_id)
                self.log("audit", events=len(rep["events"]), chain=rep["chain"])

    def _raw(self):
        """Red-team mode: no model in the loop. The command goes straight to the gate, then the sandbox,
        followed by probes that show what survived. Containment must not depend on the model refusing."""
        for cmd in [self.prompt, "echo '--- after:'; ls / | wc -l; ls /work; id; cat /opt/tas/HOST_CANARY.txt"]:
            self.log("tool_call", tool="run_shell", args={"cmd": cmd})
            out = self.guard.call_tool("run_shell", {"cmd": cmd})
            self.log("gate", tool="run_shell", status=out["status"],
                     detail=out.get("error") or out.get("message") or "",
                     receipt=out.get("result") if out["status"] == "ok" else None)
        r = self.receipts
        self.answer = (f"Red-team run: {len(r)} command(s) executed in the sandbox. "
                       + (f"Last exit code {r[-1]['exit_code']}." if r else "Nothing ran."))
        self.log("answer", text=self.answer)

    def _loop(self):
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": self.prompt}]
        for _ in range(MAX_STEPS):
            msg = chat(messages, tools=TOOLS)
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls} if calls
                            else {"role": "assistant", "content": msg.get("content") or ""})
            if not calls:
                self.answer = (msg.get("content") or "").strip()
                self.log("answer", text=self.answer)
                return
            for tc in calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.log("tool_call", tool=name, args=args)
                out = self.guard.call_tool(name, args)
                self.log("gate", tool=name, status=out["status"],
                         detail=out.get("error") or out.get("message") or "",
                         receipt=out.get("result") if out["status"] == "ok" else None)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(out)[:6000]})
        self.answer = "Stopped: step limit reached."
        self.log("answer", text=self.answer)


def verify(task: str, answer: str, receipts: list[dict]) -> dict:
    """Second model, different family, sees only the receipts. Every claim must be backed by one."""
    ev = [{k: r[k] for k in ("id", "cmd", "exit_code", "stdout", "stderr")} for r in receipts][-12:]
    prompt = (
        "You are a strict verifier. Below is a task, an agent's final answer, and RECEIPTS: the exact commands "
        "that ran in the sandbox with their real output and exit codes. Check every factual claim in the answer "
        "against the receipts. A claim with no receipt, or contradicted by an exit code or output, is unsupported. "
        'Reply with JSON only: {"verdict": "VERIFIED" | "UNSUPPORTED", "unsupported_claims": [..], '
        '"evidence": ["R1", ..], "note": "one sentence"}\n\n'
        f"TASK: {task}\n\nANSWER: {answer}\n\nRECEIPTS: {json.dumps(ev)[:12000]}"
    )
    err = ""
    for model in (VERIFIER_MODEL, VERIFIER_FALLBACK):
        try:
            txt = chat([{"role": "user", "content": prompt}], model=model, max_tokens=4000)["content"] or ""
            out = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            if out.get("verdict") not in ("VERIFIED", "UNSUPPORTED"):
                raise ValueError(f"bad verdict {out.get('verdict')!r}")
            out["model"] = model
            return out
        except Exception as e:
            err = f"{model}: {type(e).__name__}: {e}"
    # fail closed: no verdict means the answer is not presented as verified
    return {"verdict": "UNVERIFIED", "note": f"verifier failed ({err})", "model": VERIFIER_MODEL}
