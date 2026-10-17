"""The part a robot program talks to.

    robot = Robot.load()                      # ~/.paybox-robot/robot.json written by `paybox-robot setup`
    robot.battery = lambda: pack.percent()    # optional
    robot.on_task(lambda task: do(task.text)) # tasks the owner dispatches from PayBox
    robot.run()                               # heartbeat loop, blocks

    pay = robot.pay("JUP6Lkb...", 0.01, "SOL", purpose="route fee")
    pay = robot.pay_x402("https://api.example/forecast", max_usd=0.25, purpose="weather")

`pay` returns at once with the hub's review (allow / hold / deny). The owner sees the request in
PayBox and settles it there; `robot.wait(pay)` blocks until the status changes.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .hub import DEFAULT_HUB, Hub, HubError

CONFIG = Path(os.environ.get("PAYBOX_ROBOT_CONFIG", Path.home() / ".paybox-robot" / "robot.json"))
PENDING = ("requested",)           # waiting for the owner
SETTLED = ("proposed", "success")  # the owner's PayBox took it


DECIMALS = {"SOL": 9, "USDC": 6}


def to_atomic(amount: float, asset: str) -> str:
    """Exact base units as a decimal string: lamports for SOL, 6-decimal units for USDC."""
    d = DECIMALS.get(asset.upper())
    if d is None:
        raise ValueError(f"unsupported asset {asset}; use SOL or USDC")
    return str(round(float(amount) * 10**d))


def to_cents(amount: float, asset: str) -> int | None:
    """USD cents for the budget: exact for USDC and x402. SOL is priced by the hub, so None here."""
    return None if asset.upper() == "SOL" else round(float(amount) * 100)


@dataclass
class Task:
    id: str
    text: str
    max_spend_cents: int | None = None

    @property
    def max_spend_usd(self) -> float | None:
        return None if self.max_spend_cents is None else self.max_spend_cents / 100


@dataclass
class Payment:
    id: str
    verdict: str                      # allow | hold | deny
    say: str                          # one line to read aloud
    reasons: list[str] = field(default_factory=list)
    status: str = "requested"
    raw: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.verdict != "deny"

    @property
    def settled(self) -> bool:
        return self.status in SETTLED


class Robot:
    def __init__(self, hub: Hub, name: str = "robot", owner_code: str = "", wallet: str | None = None, budget_cents: int | None = None, heartbeat_s: float = 5.0):
        self.hub, self.name, self.owner_code, self.wallet = hub, name, owner_code, wallet
        self.budget_cents, self.heartbeat_s = budget_cents, heartbeat_s
        self.spent_cents = 0
        self.status = "idle"
        self.battery: Callable[[], float | None] | None = None
        self._task_handler: Callable[[Task], Any] | None = None
        self._stop = threading.Event()
        self.current_task: Task | None = None

    # ---- setup / persistence -------------------------------------------------------------
    @classmethod
    def setup(cls, name: str, owner_code: str, hub_url: str = DEFAULT_HUB, kind: str = "robot", wallet: str | None = None, chain: str | None = None, budget_cents: int | None = None, meta: dict | None = None, path: Path = CONFIG) -> "Robot":
        hub = Hub(hub_url)
        out = hub.register(name, owner_code, kind=kind, wallet=wallet, chain=chain, budget_cents=budget_cents, meta=meta)
        robot = cls(hub, name, out["owner_code"], wallet, budget_cents)
        robot.save(path)
        return robot

    def save(self, path: Path = CONFIG) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"hub": self.hub.base, "robot_id": self.hub.robot_id, "robot_token": self.hub.token, "name": self.name, "owner_code": self.owner_code, "wallet": self.wallet, "budget_cents": self.budget_cents}
        path.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # the token is the robot's identity

    @classmethod
    def load(cls, path: Path = CONFIG) -> "Robot":
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; run `paybox-robot setup --owner <code> --name <name>` first")
        d = json.loads(path.read_text())
        return cls(Hub(d["hub"], d["robot_token"], d["robot_id"]), d.get("name", "robot"), d.get("owner_code", ""), d.get("wallet"), d.get("budget_cents"))

    # ---- heartbeat / tasks ---------------------------------------------------------------
    def on_task(self, handler: Callable[[Task], Any]) -> Callable[[Task], Any]:
        self._task_handler = handler
        return handler

    def beat(self) -> Task | None:
        pct = None
        if self.battery:
            try:
                pct = self.battery()
            except Exception:
                pct = None
        out = self.hub.heartbeat(battery_pct=pct, status=self.status, budget_cents=self.budget_cents, spent_cents=self.spent_cents, wallet=self.wallet)
        t = out.get("task")
        return Task(t["id"], t["task"], t.get("max_spend_cents")) if t else None

    def step(self) -> bool:
        """One heartbeat; runs a task if the owner queued one. Returns True when a task ran."""
        task = self.beat()
        if not task or not self._task_handler or not self.hub.take_task(task.id):
            return False
        self.current_task, self.status = task, "working"
        try:
            result = self._task_handler(task)
            self.hub.finish_task(task.id, result)
        except Exception as e:  # the owner reads failures in PayBox
            self.hub.finish_task(task.id, {"error": str(e)[:300]}, failed=True)
        finally:
            self.current_task, self.status = None, "idle"
        return True

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except HubError as e:
                if e.status == 401:
                    raise
            except OSError:
                pass  # offline; try again next beat
            self._stop.wait(self.heartbeat_s)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="paybox-robot", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()

    # ---- payments ------------------------------------------------------------------------
    def _payment(self, out: dict) -> Payment:
        rv = out.get("review") or {}
        return Payment(out["id"], rv.get("verdict", "hold"), rv.get("say", ""), list(rv.get("reasons", [])), raw=out)

    def review(self, to: str | None = None, url: str | None = None, amount: float = 0, asset: str = "USDC", merchant: str | None = None) -> dict:
        return self.hub.review(address=to, url=url, merchant=merchant or to, amount_cents=to_cents(amount, asset), asset=asset.upper(), amount_atomic=to_atomic(amount, asset))

    def pay(self, to: str, amount: float, asset: str = "USDC", purpose: str = "", url: str | None = None) -> Payment:
        """Ask the owner to pay `amount` of `asset` to the address `to`. USDC on Base or SOL on Solana."""
        cents, atomic = to_cents(amount, asset), to_atomic(amount, asset)
        if cents is not None:  # usd-denominated: the robot can check its own caps before asking
            if self.current_task and self.current_task.max_spend_cents is not None and cents > self.current_task.max_spend_cents:
                raise ValueError(f"{amount} {asset} is above the task cap of ${self.current_task.max_spend_usd:.2f}")
            if self.budget_cents is not None and self.spent_cents + cents > self.budget_cents:
                raise ValueError("over the robot's budget")
        out = self.hub.create_payment(kind="transfer", merchant=to, url=url, asset=asset.upper(), amount_cents=cents, amount_atomic=atomic, purpose=purpose or (self.current_task.text if self.current_task else ""))
        return self._payment(out)

    def pay_x402(self, endpoint: str, max_usd: float, purpose: str = "", merchant: str | None = None) -> Payment:
        """Ask the owner to settle a paid HTTP request (x402) up to `max_usd`."""
        out = self.hub.create_payment(kind="x402", endpoint=endpoint, url=endpoint, merchant=merchant or endpoint.split("/")[2], asset="USDC", amount_cents=to_cents(max_usd, "USDC"), amount_atomic=to_atomic(max_usd, "USDC"), purpose=purpose)
        return self._payment(out)

    def refresh(self, pay: Payment) -> Payment:
        p = self.hub.payment(pay.id)
        pay.status, pay.raw = p.get("status", pay.status), p
        return pay

    def wait(self, pay: Payment, timeout: float = 600, every: float = 3.0) -> Payment:
        """Block until the owner acted on the request in PayBox (or `timeout` seconds)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.refresh(pay).status not in PENDING:
                return pay
            time.sleep(every)
        return pay

    def record(self, pay: Payment, status: str, request_id: str | None = None, amount_cents: int | None = None) -> None:
        """Tell the hub what happened on the robot's side (e.g. the goods arrived, or you gave up)."""
        self.hub.update_payment(pay.id, status, request_id, amount_cents)
        pay.status = status
        if status == "success" and amount_cents:
            self.spent_cents += amount_cents
