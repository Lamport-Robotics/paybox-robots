"""Pincer1 demo: the robot asks its owner, in the room, before asking PayBox.

Runs against the real robot on ws://127.0.0.1:7402, or on a laptop with no robot
(speech is printed, the passphrase step is skipped).

    pip install "paybox-robot[pincer1]"
    paybox-robot setup --owner can --name pincer1 --kind hexapod --budget 20
    python examples/pincer1_demo.py
"""

import asyncio

from paybox_robot import Robot
from paybox_robot.pincer1 import Pincer1Bridge

robot = Robot.load()
bridge = Pincer1Bridge(robot)


def do_task(task):
    # the owner typed this in PayBox; the robot's planner would go here
    return {"heard": task.text}


bridge.on_task(do_task)


async def scripted():
    await asyncio.sleep(3)
    await bridge.say("Good morning. The swap you asked for needs a route fee.")
    await bridge.request_payment(to="JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", amount=0.01, asset="SOL", purpose="route fee for the morning swap")
    await asyncio.sleep(20)
    await bridge.say("Something else came in.")
    await bridge.request_payment(x402="https://totally-not-a-drainer.xyz/claim", max_usd=40, purpose="claim airdrop")


async def main():
    await asyncio.gather(bridge.run(), scripted())


if __name__ == "__main__":
    asyncio.run(main())
