"""Disposable Docker sandbox: one container per task, destroyed when the task ends.

Containment, per container:
  - no network (--network none)
  - no secrets: the environment is empty; the inference key lives only in the control plane
  - non-root user (nobody), all capabilities dropped, no-new-privileges
  - read-only root filesystem; only /work and /tmp are writable (tmpfs, size-capped)
  - memory, CPU, and PID caps; a wall-clock timeout per command
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

IMAGE = "python:3.12-slim"
CMD_TIMEOUT = 20
MAX_OUT = 8000


def _docker(*args, timeout=30, input_=None):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, input=input_)


class Sandbox:
    def __init__(self, task_id: str):
        self.name = f"tas-{task_id}-{uuid.uuid4().hex[:6]}"
        self.alive = False

    def start(self):
        r = _docker(
            "run", "-d", "--rm", "--name", self.name,
            "--network", "none",
            "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "64",
            "--read-only", "--tmpfs", "/work:rw,size=64m,uid=65534", "--tmpfs", "/tmp:rw,size=16m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "-w", "/work", "--env", "HOME=/work",
            IMAGE, "sleep", "900",
        )
        if r.returncode != 0:
            raise RuntimeError(f"sandbox start failed: {r.stderr.strip()}")
        self.alive = True
        return self

    def run(self, cmd: str) -> dict:
        try:
            r = _docker("exec", self.name, "timeout", "-s", "KILL", str(CMD_TIMEOUT), "sh", "-c", cmd,
                        timeout=CMD_TIMEOUT + 10)
            out, err, code = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            out, err, code = "", "killed: wall-clock timeout", 137
        return receipt(cmd, out, err, code)

    def write(self, path: str, content: str) -> dict:
        r = _docker("exec", "-i", self.name, "sh", "-c", 'cat > "$1"', "sh", path, input_=content)
        return receipt(f"write_file {path} ({len(content)} bytes)", r.stdout, r.stderr, r.returncode)

    def destroy(self):
        if self.alive:
            _docker("rm", "-f", self.name)
            self.alive = False


def receipt(cmd, out, err, code) -> dict:
    """What actually happened, hashed so the transcript can't be quietly edited later."""
    body = {"cmd": cmd, "exit_code": code, "stdout": out[-MAX_OUT:], "stderr": err[-MAX_OUT:]}
    body["sha256"] = hashlib.sha256(f"{cmd}\0{code}\0{out}\0{err}".encode()).hexdigest()
    return body
