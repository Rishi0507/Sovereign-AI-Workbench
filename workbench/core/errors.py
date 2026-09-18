"""Exception types shared across the workbench."""

from __future__ import annotations


class WorkbenchError(Exception):
    """Base class for all workbench errors."""


class ConfigError(WorkbenchError):
    """A configuration file is missing or invalid."""


class PolicyError(WorkbenchError):
    """A label, ACL or workspace policy refused the operation."""


class JailError(PolicyError):
    """A path resolved outside its workspace or through a symlink."""


class InvalidModelOutput(WorkbenchError):
    """A model output failed schema validation after all retries."""

    def __init__(self, message: str, attempts: list[str] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


class ToolError(WorkbenchError):
    """A tool failed. The agent loop records it as a failed observation."""


class ServiceUnavailable(WorkbenchError):
    """A host service (sandboxd, egressd, model server) could not be reached."""


class RateLimited(ServiceUnavailable):
    """A model service refused the call for now and said when to try again."""

    def __init__(self, message: str, retry_after_s: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class NotFound(WorkbenchError):
    """A requested object does not exist."""


class ApprovalRequired(WorkbenchError):
    """A precondition for approval is not met."""
