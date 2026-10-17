"""paybox-robot: a robot registers with the hub, heartbeats battery and budget, picks up tasks and
raises payment requests. Money never moves here; the owner settles requests in PayBox."""

from .hub import Hub, HubError
from .robot import Payment, Robot, Task

__all__ = ["Hub", "HubError", "Payment", "Robot", "Task"]
__version__ = "0.1.2"
