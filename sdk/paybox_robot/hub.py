"""Thin HTTP client for the robots hub. Standard library only so it runs on any robot computer."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HUB = "https://lamport.si/api/robots"


class HubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"hub {status}: {message}")
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # a bearer token never follows a redirect
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Hub:
    def __init__(self, base: str = DEFAULT_HUB, token: str | None = None, robot_id: str | None = None, timeout: float = 15.0):
        base = base.rstrip("/")
        local = base.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
        if not base.startswith("https://") and not (local or os.environ.get("PAYBOX_ROBOT_INSECURE") == "1"):
            raise ValueError("the hub must be an https URL (set PAYBOX_ROBOT_INSECURE=1 only for a local test hub)")
        self.base = base
        self.token = token
        self.robot_id = robot_id
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"accept": "application/json", "user-agent": "paybox-robot/0.1"}
        if data is not None:
            headers["content-type"] = "application/json"
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with _opener.open(req, timeout=self.timeout) as res:
                return json.loads(res.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", e.reason)
            except Exception:
                msg = e.reason
            raise HubError(e.code, str(msg)) from None

    # one-time
    def register(self, name: str, owner_code: str, kind: str = "robot", wallet: str | None = None, chain: str | None = None, budget_cents: int | None = None, meta: dict | None = None) -> dict:
        body: dict[str, Any] = {"name": name, "owner_code": owner_code, "kind": kind, "meta": meta or {}}
        if wallet:
            body["wallet"] = wallet
        if chain:
            body["chain"] = chain
        if budget_cents is not None:
            body["budget_cents"] = int(budget_cents)
        out = self._call("POST", "/register", body)
        self.token, self.robot_id = out["robot_token"], out["robot_id"]
        return out

    # every few seconds
    def heartbeat(self, battery_pct: float | None = None, status: str = "idle", budget_cents: int | None = None, spent_cents: int | None = None, meta: dict | None = None, wallet: str | None = None) -> dict:
        body: dict[str, Any] = {"status": status}
        if battery_pct is not None:
            body["battery_pct"] = round(float(battery_pct), 1)
        if budget_cents is not None:
            body["budget_cents"] = int(budget_cents)
        if spent_cents is not None:
            body["spent_cents"] = int(spent_cents)
        if meta:
            body["meta"] = meta
        if wallet:
            body["wallet"] = wallet
        return self._call("POST", f"/{self.robot_id}/heartbeat", body)

    def take_task(self, task_id: str) -> bool:
        return bool(self._call("POST", f"/{self.robot_id}/tasks/{task_id}/take", {}).get("ok"))

    def finish_task(self, task_id: str, result: Any = None, failed: bool = False) -> None:
        self._call("POST", f"/{self.robot_id}/tasks/{task_id}/result", {"status": "failed" if failed else "done", "result": result})

    def review(self, **what: Any) -> dict:
        return self._call("POST", f"/{self.robot_id}/review", {k: v for k, v in what.items() if v is not None})

    def create_payment(self, **what: Any) -> dict:
        return self._call("POST", f"/{self.robot_id}/payments", {k: v for k, v in what.items() if v is not None})

    def payment(self, payment_id: str) -> dict:
        return self._call("GET", f"/{self.robot_id}/payments/{payment_id}")

    def update_payment(self, payment_id: str, status: str, request_id: str | None = None, amount_cents: int | None = None) -> None:
        body: dict[str, Any] = {"status": status}
        if request_id:
            body["request_id"] = request_id
        if amount_cents is not None:
            body["amount_cents"] = int(amount_cents)
        self._call("POST", f"/{self.robot_id}/payments/{payment_id}", body)

    def owner_robots(self, owner_code: str) -> list[dict]:
        return list(self._call("GET", f"/owner/{owner_code}").get("robots", []))
