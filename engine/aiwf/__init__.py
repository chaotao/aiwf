"""aiwf - schema-driven AI workflow engine.

The engine is a pure state machine: it never calls an LLM or executes task
logic itself. An AI agent drives a run through the CLI/SDK:

    run       -> create a run, receive the first node's execution spec
    current   -> re-read the current node spec
    complete  -> report node output, engine audits and advances
    fail      -> mark the run failed
    resume    -> reopen a failed run

Public API:
    from aiwf import Storage, WorkflowEngine
"""
from .engine import WorkflowEngine
from .storage import Storage

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0"

__all__ = [
    "Storage",
    "WorkflowEngine",
    "SCHEMA_VERSION",
]
