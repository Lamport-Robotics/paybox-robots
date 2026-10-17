#!/usr/bin/env python3
"""A thin motion server for the Pincer1 show. Runs inside the robot's ROS 2 container, HTTP on :8800.

GET  /status                      battery_mv, tilt, last action
POST /act {"action", "params"}    walk | turn | stop | pose | look | action | beep | led

It publishes to the stack's public topics only (the nex actuator bridge forwards them), so nothing
in the robot's own code changes. Limits are deliberately tight: this is a demo, not a driver.
"""
from __future__ import annotations

import json, math, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Imu
from std_msgs.msg import UInt16
from interfaces.msg import RunActionSet
from kinematics_msgs.msg import Pose as KPose
from servo_controller_msgs.msg import ServoPosition, ServosPosition
from ros_robot_controller_msgs.msg import BuzzerState, LedState

PORT = 8800
ARM_REST = {19: 500, 20: 720, 21: 130, 22: 150, 23: 500, 24: 500}
LOOK = {"down": {20: 720, 21: 130, 22: 150}, "ahead": {20: 700, 21: 15, 22: 450}}
MAX_X, MAX_YAW, MAX_RPY, MAX_XYZ = 0.10, 0.5, 0.2, 0.03
FLOOR_MV = 10300

clamp = lambda v, lo, hi: max(lo, min(hi, v))


class Body(Node):
    def __init__(self):
        super().__init__("pincer_show_body")
        self.lock = threading.Lock()
        self.battery_mv = None; self.tilt = 0.0; self.last = None; self.moving_until = 0.0
        self.create_subscription(UInt16, "/ros_robot_controller/battery", lambda m: setattr(self, "battery_mv", int(m.data)), 5)
        self.create_subscription(Imu, "/imu", self._imu, 10)
        self.pub_vel = self.create_publisher(Twist, "/controller/cmd_vel", 5)
        self.pub_pose = self.create_publisher(KPose, "/controller/set_pose_euler", 5)
        self.pub_servo = self.create_publisher(ServosPosition, "/nex/private/servo_controller", 5)
        self.pub_action = self.create_publisher(RunActionSet, "/controller/run_actionset", 5)
        self.pub_buzzer = self.create_publisher(BuzzerState, "/ros_robot_controller/set_buzzer", 5)
        self.pub_led = self.create_publisher(LedState, "/ros_robot_controller/set_led", 5)

    def _imu(self, m):
        x, y, z, w = m.orientation.x, m.orientation.y, m.orientation.z, m.orientation.w
        roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)); pitch = math.asin(clamp(2 * (w * y - z * x), -1, 1))
        self.tilt = math.degrees(max(abs(roll), abs(pitch)))

    def status(self):
        return {"battery_mv": self.battery_mv, "battery_v": None if self.battery_mv is None else round(self.battery_mv / 1000, 2), "tilt_deg": round(self.tilt, 1), "moving": time.time() < self.moving_until, "last": self.last}

    def _ok_to_move(self):
        if self.battery_mv is not None and self.battery_mv < FLOOR_MV: return f"battery {self.battery_mv/1000:.2f} V below floor"
        if self.tilt > 35: return f"tilted {self.tilt:.0f} deg"
        return None

    def _drive(self, x, y, yaw, secs):
        t = Twist(); t.linear.x = float(x); t.linear.y = float(y); t.angular.z = float(yaw)
        end = time.time() + secs; self.moving_until = end
        while time.time() < end:
            self.pub_vel.publish(t); time.sleep(0.05)
        self.pub_vel.publish(Twist())

    def act(self, a, p):
        self.last = {"action": a, "params": p, "ts": time.time()}
        if a == "walk":
            why = self._ok_to_move()
            if why: return {"moved": False, "reason": why}
            secs = clamp(float(p.get("seconds", 3)), 0.5, 8); speed = clamp(float(p.get("speed", 0.06)), 0.02, MAX_X)
            direction = p.get("direction", "forward"); x = speed if direction == "forward" else -speed if direction == "backward" else 0
            y = speed if direction == "left" else -speed if direction == "right" else 0
            threading.Thread(target=self._drive, args=(x, y, 0.0, secs), daemon=True).start()
            return {"moved": True, "seconds": secs, "speed": speed, "direction": direction}
        if a == "turn":
            why = self._ok_to_move()
            if why: return {"moved": False, "reason": why}
            secs = clamp(float(p.get("seconds", 3)), 0.5, 8); yaw = clamp(float(p.get("yaw", 0.3)), -MAX_YAW, MAX_YAW)
            threading.Thread(target=self._drive, args=(0.0, 0.0, yaw, secs), daemon=True).start()
            return {"moved": True, "seconds": secs, "yaw": yaw}
        if a == "stop":
            self.moving_until = 0; self.pub_vel.publish(Twist()); return {"stopped": True}
        if a == "pose":
            m = KPose(); m.position.x = clamp(float(p.get("x_mm", 0)) / 1000, -MAX_XYZ, MAX_XYZ); m.position.y = clamp(float(p.get("y_mm", 0)) / 1000, -MAX_XYZ, MAX_XYZ); m.position.z = clamp(float(p.get("z_mm", 0)) / 1000, -MAX_XYZ, MAX_XYZ)
            m.orientation.roll = clamp(float(p.get("roll", 0)), -MAX_RPY, MAX_RPY); m.orientation.pitch = clamp(float(p.get("pitch", 0)), -MAX_RPY, MAX_RPY); m.orientation.yaw = clamp(float(p.get("yaw", 0)), -MAX_RPY, MAX_RPY)
            self.pub_pose.publish(m); return {"posed": True}
        if a == "look":
            preset = p.get("preset", ""); base = dict(LOOK.get(preset, {})) if preset else {}
            k = 1000.0 / 240.0; pan = clamp(float(p.get("pan_deg", 0)), -60, 60); tilt = clamp(float(p.get("tilt_deg", 0)), -40, 40)
            tilt_base = base.get(22, ARM_REST[22]); targets = {**base, 19: ARM_REST[19] + pan * k, 22: clamp(tilt_base + tilt * k, 50, 600)}
            m = ServosPosition(); m.duration = clamp(float(p.get("duration_s", 0.8)), 0.2, 3); m.position_unit = "pulse"
            m.position = [ServoPosition(id=int(i), position=float(v)) for i, v in targets.items()]
            self.pub_servo.publish(m); return {"looking": True, "pulses": {i: round(v) for i, v in targets.items()}}
        if a == "action":
            name = str(p.get("name", ""))
            if name.startswith(("go_", "climb", "turn_", "left_", "right_", "back_", "stepping")):
                why = self._ok_to_move()
                if why: return {"ran": False, "reason": why}
                self.moving_until = time.time() + 8
            m = RunActionSet(); m.action_path = name; m.interrupt = bool(p.get("interrupt", False)); self.pub_action.publish(m); return {"ran": True, "name": name}
        if a == "beep":
            m = BuzzerState(); m.freq = int(p.get("freq", 1200)); m.on_time = float(p.get("on_s", 0.15)); m.off_time = float(p.get("off_s", 0.1)); m.repeat = int(p.get("repeat", 2)); self.pub_buzzer.publish(m); return {"beeped": True}
        if a == "led":
            m = LedState(); m.id = 1; m.on_time = float(p.get("on_s", 0.2)); m.off_time = float(p.get("off_s", 0.2)); m.repeat = int(p.get("repeat", 3)); self.pub_led.publish(m); return {"led": True}
        return {"error": f"unknown action {a}"}


def serve(node: Body):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            self._send(200, node.status()) if self.path.startswith("/status") else self._send(404, {"error": "not found"})
        def do_POST(self):
            if not self.path.startswith("/act"): return self._send(404, {"error": "not found"})
            n = int(self.headers.get("content-length", "0")); req = json.loads(self.rfile.read(n) or b"{}")
            with node.lock:
                self._send(200, node.act(str(req.get("action", "")), dict(req.get("params") or {})))
        def log_message(self, *a): pass
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


def main():
    rclpy.init(); node = Body()
    threading.Thread(target=serve, args=(node,), daemon=True).start()
    node.get_logger().info(f"pincer show body on :{PORT}")
    rclpy.spin(node)


if __name__ == "__main__":
    main()
