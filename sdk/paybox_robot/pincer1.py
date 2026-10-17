"""Pincer1 adapter: sits next to the daemon on ws://127.0.0.1:7402, reads battery from `presence`,
speaks through the robot's own voice and asks for the owner's passphrase before any payment request.

    paybox-robot-pincer1            # after `paybox-robot setup` on the robot
    PINCER_WS=ws://127.0.0.1:7402

Tasks dispatched from PayBox are read aloud; the robot's own task runner can be plugged in with
`Pincer1Bridge.on_task`. Payment flow: a payment request is first reviewed by the hub (allow / hold /
deny). On allow or hold the robot says the verdict and asks the owner, in the room, for the passphrase;
only then does it raise the request in PayBox, where the owner approves the actual transfer.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Callable

from .robot import Payment, Robot, Task

WS = os.environ.get("PINCER_WS", "ws://127.0.0.1:7402")


class Pincer1Bridge:
    def __init__(self, robot: Robot, ws_url: str = WS):
        self.robot, self.ws_url = robot, ws_url
        self.battery_pct: float | None = None
        self.owner_here = False
        self._ws = None
        self._waiters: dict[str, asyncio.Future] = {}
        self._task_handler: Callable[[Task], Any] | None = None
        robot.battery = lambda: self.battery_pct

    def on_task(self, fn: Callable[[Task], Any]) -> None:
        self._task_handler = fn

    async def _send(self, obj: dict, wait: bool = True) -> dict | None:
        if self._ws is None:
            return None
        if wait:
            obj.setdefault("id", uuid.uuid4().hex[:8])
            fut = asyncio.get_running_loop().create_future()
            self._waiters[obj["id"]] = fut
        await self._ws.send(json.dumps(obj))
        if not wait:
            return None
        try:
            return await asyncio.wait_for(fut, 120)
        finally:
            self._waiters.pop(obj["id"], None)

    async def say(self, text: str) -> None:
        if self._ws is None:
            print(f"[say] {text}")
            return
        await self._send({"t": "say", "text": text})

    async def ask_passphrase(self) -> bool:
        if self._ws is None:
            return True  # bench mode: no robot attached
        r = await self._send({"t": "ask_passphrase"})
        return bool(r and r.get("ok"))

    async def request_payment(self, to: str | None = None, amount: float = 0, asset: str = "USDC", purpose: str = "", x402: str | None = None, max_usd: float = 0) -> Payment | None:
        rv = self.robot.review(url=x402, amount=max_usd) if x402 else self.robot.review(to=to, amount=amount, asset=asset)
        verdict, line = rv.get("verdict", "hold"), rv.get("say", "")
        if verdict == "deny":
            await self.say(f"I will not pay this. {line}")
            return None
        await self.say(f"{line} Say the passphrase if I should ask you for it in PayBox.")
        if not await self.ask_passphrase():
            await self.say("No passphrase. I dropped the request.")
            return None
        pay = self.robot.pay_x402(x402, max_usd, purpose) if x402 else self.robot.pay(to, amount, asset, purpose)
        await self.say("Sent to your PayBox. Approve it there and I will carry on.")
        asyncio.get_running_loop().create_task(self._follow(pay))
        return pay

    async def _follow(self, pay: Payment, timeout: float = 600, every: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        for _ in range(int(timeout / every)):
            await asyncio.sleep(every)
            try:
                await loop.run_in_executor(None, self.robot.refresh, pay)
            except Exception:
                continue
            if pay.status not in ("requested",):
                break
        await self.say("Thank you, payment approved." if pay.settled else f"The payment ended as {pay.status}.")

    async def _handle(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid in self._waiters and not self._waiters[mid].done():
            self._waiters[mid].set_result(msg)
        if msg.get("t") == "presence":
            b = msg.get("battery")
            if isinstance(b, dict):
                self.battery_pct = b.get("pct")
            elif isinstance(b, (int, float)):
                self.battery_pct = float(b)
            self.owner_here = bool(msg.get("present") or msg.get("owner"))

    async def _reader(self) -> None:
        import websockets
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    self._ws = ws
                    async for raw in ws:
                        try:
                            await self._handle(json.loads(raw))
                        except Exception:
                            pass
            except Exception:
                self._ws = None
                await asyncio.sleep(2)

    async def _heartbeat(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                task = await loop.run_in_executor(None, self.robot.beat)
                if task and self.robot.hub.take_task(task.id):
                    await self.say(f"New task from my owner: {task.text}")
                    try:
                        result = (await loop.run_in_executor(None, self._task_handler, task)) if self._task_handler else {"spoken": True}
                        self.robot.hub.finish_task(task.id, result)
                    except Exception as e:
                        self.robot.hub.finish_task(task.id, {"error": str(e)[:300]}, failed=True)
            except Exception:
                pass
            await asyncio.sleep(self.robot.heartbeat_s)

    async def run(self) -> None:
        await asyncio.gather(self._reader(), self._heartbeat())


def main(argv=None) -> int:
    robot = Robot.load()
    bridge = Pincer1Bridge(robot)
    print(f"{robot.name}: bridging {bridge.ws_url} <-> {robot.hub.base}")
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
