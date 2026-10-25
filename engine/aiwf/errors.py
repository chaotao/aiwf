"""Typed, user-facing errors.

The CLI is the boundary an agent talks to, so every failure it can cause or
report must carry enough structure to be acted on without reading a traceback:

    code  stable machine token (E_INPUT_INVALID, E_STALE_ACTION, ...)
    message  one line, the actual problem
    at    where it happened (JSON pointer / dotted path / run id), optional
    hint  what to change, optional
    next  a command that resolves it, optional

``SchemaError`` also subclasses ``ValueError`` and ``StaleActionError`` also
subclasses ``RuntimeError`` so existing callers that catch those built-ins keep
working.
"""
from typing import Optional


class AiwfError(Exception):
    code = "E_ERROR"
    exit_code = 2

    def __init__(
        self,
        message: str,
        *,
        at: Optional[str] = None,
        hint: Optional[str] = None,
        next_command: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.at = at
        self.hint = hint
        self.next_command = next_command

    def as_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.at:
            payload["at"] = self.at
        if self.hint:
            payload["hint"] = self.hint
        if self.next_command:
            payload["next"] = self.next_command
        return payload


class UsageError(AiwfError, RuntimeError):
    """Bad invocation: unknown/contradictory flags, missing required value.

    Also a RuntimeError so callers that treat protocol misuse as a runtime
    failure keep catching it.
    """

    code = "E_USAGE"


class InstallError(AiwfError):
    """Bundled data (schemas, library, skills) is not reachable."""

    code = "E_INSTALL"


class NotFoundError(AiwfError):
    """A workflow, run or file named on the command line does not exist."""

    code = "E_NOT_FOUND"


class SchemaError(AiwfError, ValueError):
    """A JSON Schema validation failed; ``at`` is the instance path."""

    code = "E_INPUT_INVALID"


class StaleActionError(AiwfError, RuntimeError):
    """The action id does not describe the run's current state."""

    code = "E_STALE_ACTION"
    exit_code = 4


class OutputRejectedError(AiwfError):
    """A reported output did not satisfy the node's contract.

    The engine treats a rejection as a normal state (the node stays open and
    receives feedback), so this type is currently only used to describe the
    result to the CLI; it is not raised by the engine.
    """

    code = "E_OUTPUT_REJECTED"
    exit_code = 3
