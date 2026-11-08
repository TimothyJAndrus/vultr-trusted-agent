"""Control plane: FastAPI on a Vultr VM. Holds the inference key; sandboxes never see it."""

from __future__ import annotations

import os
import threading
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import MODEL, VERIFIER_MODEL, Task

app = FastAPI(title="Trusted Agent Stack on Vultr")
TASKS: dict[str, Task] = {}
MAX_RUNNING = 3
STATIC = os.path.join(os.path.dirname(__file__), "static")


class NewTask(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    raw: bool = False  # red-team mode: send the text straight to the gate + sandbox, no model


class Decision(BaseModel):
    decision: str
    approver: str = Field(default="demo-human", max_length=80)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/info")
def info():
    return {"agent_model": MODEL, "verifier_model": VERIFIER_MODEL, "inference": "Vultr Serverless Inference"}


@app.post("/api/tasks")
def create(t: NewTask):
    if sum(1 for x in TASKS.values() if x.status == "running") >= MAX_RUNNING:
        raise HTTPException(429, "too many running tasks, try again in a minute")
    tid = uuid.uuid4().hex[:8]
    task = Task(tid, t.prompt, raw=t.raw)
    TASKS[tid] = task
    threading.Thread(target=task.run, daemon=True).start()
    return {"id": tid}


@app.get("/api/tasks/{tid}")
def get(tid: str):
    t = TASKS.get(tid) or _404()
    return {"id": t.id, "prompt": t.prompt, "status": t.status, "events": t.events, "pending": t.pending(),
            "answer": t.answer, "verdict": t.verdict, "receipts": t.receipts}


@app.post("/api/tasks/{tid}/calls/{cid}")
def decide(tid: str, cid: str, d: Decision):
    t = TASKS.get(tid) or _404()
    if d.decision not in ("approve", "reject"):
        raise HTTPException(400, "approve or reject")
    try:
        return t.decide(cid, d.decision, d.approver)
    except Exception as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/host-canary")
def canary():
    """Proof the host was untouched: a file the sandbox would have destroyed if it could reach the host."""
    p = "/opt/tas/HOST_CANARY.txt"
    return {"path": p, "exists": os.path.exists(p), "content": open(p).read() if os.path.exists(p) else None}


def _404():
    raise HTTPException(404, "no such task")
