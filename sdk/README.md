# paybox-robot

The robot side of [paybox-robots](https://github.com/Lamport-Robotics/paybox-robots).
Python 3.10+, no dependencies.

```
pip install "git+https://github.com/Lamport-Robotics/paybox-robots#subdirectory=sdk"
paybox-robot setup --owner <code> --name <robot> [--budget 20] [--wallet <addr>]
paybox-robot run
paybox-robot pay <address> 0.01 --asset SOL --purpose "route fee" --wait
paybox-robot x402 https://api.example/forecast --max 0.25 --wait
paybox-robot review <address-or-url> --amount 5
```

`setup` writes `~/.paybox-robot/robot.json` (mode 600). The token in it is the robot's
identity; nothing else is stored.

ROS 2: `paybox-robot-ros2` subscribes `/battery_state`, publishes `/paybox/task` when the
owner dispatches one, takes `/paybox/pay` requests and reports on `/paybox/payment` and
`/paybox/say`.

Pincer1: `pip install "paybox-robot[pincer1] @ git+https://github.com/Lamport-Robotics/paybox-robots#subdirectory=sdk"` and `paybox-robot-pincer1` bridges the robot's
own websocket (battery from presence, speech, passphrase before any request).
