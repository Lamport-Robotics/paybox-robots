"""ROS 2 node: the same Robot, on topics. Needs rclpy (any ROS 2 distro) and the robot's config file.

    ros2 run paybox_robot paybox-robot-ros2     # or just: paybox-robot-ros2

Subscribes
    /battery_state   sensor_msgs/BatteryState   percentage (0..1 or 0..100) -> heartbeat
    /paybox/pay      std_msgs/String  JSON {"to": addr, "amount": 0.01, "asset": "SOL", "purpose": "..."}
                     or {"x402": url, "max_usd": 0.25, "purpose": "..."}
    /paybox/task_result  std_msgs/String  JSON {"id": task_id, "result": {...}, "failed": false}
Publishes
    /paybox/task     std_msgs/String  JSON {"id","text","max_spend_cents"} when the owner dispatches one
    /paybox/payment  std_msgs/String  JSON {"id","verdict","say","reasons","status"} on request and on change
    /paybox/say      std_msgs/String  one spoken line per event, for whatever TTS the robot has
"""

from __future__ import annotations

import json
import threading

from .robot import Robot


def main(argv=None) -> int:
    try:
        import rclpy
        from rclpy.node import Node
        from sensor_msgs.msg import BatteryState
        from std_msgs.msg import String
    except ImportError:
        print("rclpy not found; source your ROS 2 install first")
        return 1

    class PayBoxRobotNode(Node):
        def __init__(self):
            super().__init__("paybox_robot")
            self.robot = Robot.load()
            self.declare_parameter("heartbeat_s", 5.0)
            self.declare_parameter("task_timeout_s", 600.0)
            self._battery = None
            self.robot.battery = lambda: self._battery
            self.robot.on_task(self._task)
            self.pub_task = self.create_publisher(String, "paybox/task", 10)
            self.pub_pay = self.create_publisher(String, "paybox/payment", 10)
            self.pub_say = self.create_publisher(String, "paybox/say", 10)
            self.create_subscription(BatteryState, "battery_state", self._on_battery, 10)
            self.create_subscription(String, "paybox/pay", self._on_pay, 10)
            self.create_subscription(String, "paybox/task_result", self._on_task_result, 10)
            self._open = {}
            self._pending = {}
            self.create_timer(float(self.get_parameter("heartbeat_s").value), self._beat)
            self.create_timer(3.0, self._poll_payments)
            self.get_logger().info(f"{self.robot.name} online as {self.robot.hub.robot_id}")

        def _say(self, text: str):
            self.pub_say.publish(String(data=text))

        def _on_battery(self, m: BatteryState):
            p = m.percentage
            self._battery = p * 100 if 0 <= p <= 1 else p

        def _beat(self):
            try:
                self.robot.step()
            except Exception as e:  # keep beating
                self.get_logger().warning(f"hub: {e}")

        def _task(self, task):
            # hand the task to the robot's own nodes and return when they report; step() blocks the timer meanwhile
            self.pub_task.publish(String(data=json.dumps({"id": task.id, "text": task.text, "max_spend_cents": task.max_spend_cents})))
            self._say(f"New task from my owner: {task.text}")
            ev = threading.Event()
            self._pending[task.id] = ev
            ev.wait(float(self.get_parameter("task_timeout_s").value))
            out = self._pending.pop(task.id, None)
            if isinstance(out, dict):
                if out.get("failed"):
                    raise RuntimeError(str(out.get("result", "failed")))
                return out.get("result")
            return {"timeout": True}

        def _on_task_result(self, m: String):
            d = json.loads(m.data)
            ev = self._pending.get(d.get("id"))
            if isinstance(ev, threading.Event):
                self._pending[d["id"]] = d
                ev.set()

        def _on_pay(self, m: String):
            d = json.loads(m.data)
            try:
                pay = self.robot.pay_x402(d["x402"], float(d.get("max_usd", 0)), d.get("purpose", "")) if "x402" in d else self.robot.pay(d["to"], float(d["amount"]), d.get("asset", "USDC"), d.get("purpose", ""))
            except Exception as e:
                self._say(f"I could not request that payment: {e}")
                return
            self._open[pay.id] = pay
            self._publish(pay)
            self._say(pay.say or f"I asked my owner to approve this payment in PayBox.")

        def _publish(self, pay):
            self.pub_pay.publish(String(data=json.dumps({"id": pay.id, "verdict": pay.verdict, "say": pay.say, "reasons": pay.reasons, "status": pay.status})))

        def _poll_payments(self):
            for pid, pay in list(self._open.items()):
                try:
                    before = pay.status
                    self.robot.refresh(pay)
                except Exception:
                    continue
                if pay.status != before:
                    self._publish(pay)
                    self._say("My owner approved the payment." if pay.settled else f"Payment {pay.status}.")
                    if pay.status not in ("requested",):
                        self._open.pop(pid, None)

    rclpy.init(args=argv)
    node = PayBoxRobotNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
