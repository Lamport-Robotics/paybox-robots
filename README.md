# paybox-robots

A wallet for a robot that only its owner can spend.

The robot registers once, heartbeats battery and budget, picks up tasks and raises
payment requests. The owner sees all of it inside [PayBox](https://paybox.sh) through
the **Lamport Robots** plugin: list robots, send a task with a spending cap, read what a
robot wants to pay and why, approve it. The robot never holds a key and the plugin never
signs; PayBox builds, simulates and signs every transaction with the owner's approval.

```
robot  --paybox-robot-->  hub (lamport.si/api/robots)  <--plugin--  PayBox (owner)
  heartbeat, tasks,          review: address age, wallet      list_robots, dispatch_task,
  payment requests           labels, merchant hygiene,        review_payment, settle_robot_payment,
                             budget                           fund_robot, payment_log
```

## On the robot

```
pip install paybox-robot
paybox-robot setup --owner <code> --name pincer1 --budget 20
paybox-robot run
```

Then in your own code:

```python
from paybox_robot import Robot

robot = Robot.load()
robot.battery = lambda: pack.percent()

@robot.on_task
def handle(task):            # "fetch tomorrow's forecast", cap $0.50
    pay = robot.pay_x402("https://api.example/forecast", max_usd=0.25, purpose=task.text)
    robot.wait(pay)          # blocks until PayBox confirms the outcome (pay.settled, pay.tx_hash)
    return {"ok": pay.settled}

robot.run()
```

`robot.pay(address, 0.01, "SOL")` and `robot.pay(address, 2.50, "USDC")` work the same way.
Every request comes back with a verdict (allow / hold / deny), the reasons, and one line the
robot can say out loud.

- [`sdk/`](sdk) Python package, zero dependencies; `paybox-robot-ros2` publishes the same on ROS 2 topics
- [`plugin/`](plugin) the PayBox plugin (TypeScript MCP server, bundled to one file)
- [`examples/`](examples) a generic robot and the Pincer1 bridge

## In PayBox

Enable the Lamport Robots plugin, then talk to it:

> list my robots, owner code `can`
> send pincer1 to check the mailbox, cap $1
> what does pincer1 want to pay?
> pay it

The plugin reaches one host, `lamport.si`, and needs no credentials.

## Hub

The hub is part of the Lamport API. It stores robots, tasks and payment requests and
reviews recipients before anything moves: Solana and EVM address age and history, wallet
labels from the launch radar, merchant URL hygiene, the robot's remaining budget. It also
speaks MCP at `https://lamport.si/api/robots/mcp` for any other agent.

MIT.
