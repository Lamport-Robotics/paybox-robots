"""The smallest useful robot: heartbeat, one task handler, one paid API call.

    paybox-robot setup --owner demo --name bench --budget 5
    python examples/any_robot.py
"""

from paybox_robot import Robot

robot = Robot.load()
robot.battery = lambda: 91.0  # replace with your pack


@robot.on_task
def handle(task):
    print("task:", task.text, "cap:", task.max_spend_usd)
    if "weather" in task.text.lower():
        pay = robot.pay_x402("https://api.open-meteo.com/v1/forecast?latitude=13.75&longitude=100.5&current=temperature_2m", max_usd=0.05, purpose=task.text)
        print("review:", pay.verdict, "-", pay.say)
        if not pay.allowed:
            return {"refused": pay.reasons}
        robot.wait(pay, timeout=300)
        return {"paid": pay.settled, "status": pay.status}
    return {"note": "nothing to buy for this one"}


if __name__ == "__main__":
    print("online as", robot.hub.robot_id)
    robot.run()
