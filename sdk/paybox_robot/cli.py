"""paybox-robot command line.

    paybox-robot setup --owner can --name pincer1 [--wallet <addr>] [--budget 20]
    paybox-robot status
    paybox-robot run            # heartbeat loop with the built-in echo task handler
    paybox-robot pay <address> <amount> [--asset SOL|USDC] [--purpose "..."] [--wait]
    paybox-robot x402 <url> --max 0.25 [--purpose "..."] [--wait]
    paybox-robot review <address-or-url> [--amount 5]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .hub import DEFAULT_HUB, HubError
from .robot import CONFIG, Robot, Task


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_setup(a) -> int:
    budget = None if a.budget is None else round(a.budget * 100)
    r = Robot.setup(a.name, a.owner, hub_url=a.hub, kind=a.kind, wallet=a.wallet, chain=a.chain, budget_cents=budget, path=CONFIG)
    print(f"registered {r.name} as {r.hub.robot_id} under owner code '{r.owner_code}'")
    print(f"token saved to {CONFIG} (keep it on the robot)")
    print("in PayBox: enable the Lamport Robots plugin, then ask it to list robots for owner code", r.owner_code)
    return 0


def cmd_status(a) -> int:
    r = Robot.load()
    me = next((x for x in r.hub.owner_robots(r.owner_code) if x.get("id") == r.hub.robot_id), None)
    _print(me or {"robot_id": r.hub.robot_id, "owner_code": r.owner_code, "note": "not visible under owner code"})
    return 0


def echo_task(task: Task):
    print(f"task {task.id}: {task.text} (cap {task.max_spend_usd})")
    time.sleep(1)
    return {"ok": True, "note": "echo handler; replace with the robot's own"}


def cmd_run(a) -> int:
    r = Robot.load()
    r.heartbeat_s = a.every
    r.on_task(echo_task)
    print(f"{r.name} online, heartbeat every {a.every}s, ctrl-c to stop")
    try:
        r.run()
    except KeyboardInterrupt:
        pass
    return 0


def _wait(r: Robot, pay, do_wait: bool) -> None:
    print(f"{pay.id}: review {pay.verdict} — {pay.say}")
    for line in pay.reasons:
        print("  ·", line)
    if not do_wait:
        return
    print("waiting for the owner in PayBox…")
    pay = r.wait(pay)
    print(f"{pay.id}: {pay.status}")


def cmd_pay(a) -> int:
    r = Robot.load()
    _wait(r, r.pay(a.address, a.amount, a.asset, a.purpose), a.wait)
    return 0


def cmd_x402(a) -> int:
    r = Robot.load()
    _wait(r, r.pay_x402(a.url, a.max, a.purpose), a.wait)
    return 0


def cmd_review(a) -> int:
    r = Robot.load()
    target = a.target
    _print(r.review(url=target, amount=a.amount) if "://" in target else r.review(to=target, amount=a.amount))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="paybox-robot", description="a wallet for a robot that only the owner can spend")
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("setup", help="register this robot once"); x.add_argument("--owner", required=True, help="short owner code you will type in PayBox"); x.add_argument("--name", required=True); x.add_argument("--kind", default="robot"); x.add_argument("--wallet", help="the robot's own public address, if it has one"); x.add_argument("--chain", help="solana:mainnet or eip155:8453"); x.add_argument("--budget", type=float, help="budget in USD"); x.add_argument("--hub", default=DEFAULT_HUB); x.set_defaults(fn=cmd_setup)
    x = s.add_parser("status"); x.set_defaults(fn=cmd_status)
    x = s.add_parser("run", help="heartbeat loop"); x.add_argument("--every", type=float, default=5.0); x.set_defaults(fn=cmd_run)
    x = s.add_parser("pay", help="ask the owner to pay an address"); x.add_argument("address"); x.add_argument("amount", type=float); x.add_argument("--asset", default="USDC"); x.add_argument("--purpose", default=""); x.add_argument("--wait", action="store_true"); x.set_defaults(fn=cmd_pay)
    x = s.add_parser("x402", help="ask the owner to settle a paid request"); x.add_argument("url"); x.add_argument("--max", type=float, required=True, help="max USD"); x.add_argument("--purpose", default=""); x.add_argument("--wait", action="store_true"); x.set_defaults(fn=cmd_x402)
    x = s.add_parser("review", help="what the hub thinks of a recipient"); x.add_argument("target"); x.add_argument("--amount", type=float, default=0); x.set_defaults(fn=cmd_review)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (HubError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
