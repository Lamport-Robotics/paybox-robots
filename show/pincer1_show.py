#!/usr/bin/env python3
"""Pincer1 demo show for the PayBox video. Standalone: it does not touch the robot's own code.

Runs on the robot's Pi next to the body API (http://127.0.0.1:8800, SCRAP's hardware-verified
HTTP wrapper around the stock stack) and talks to the PayBox hub with paybox-robot.

    pip install paybox-robot
    PAYBOX_ROBOT_CONFIG=~/.paybox-robot/robot.json  (after `paybox-robot setup --owner can --name pincer1`)
    python3 pincer1_show.py                 # the full scene on the real robot
    python3 pincer1_show.py --dry           # anywhere: no body, speech printed, hub real
    python3 pincer1_show.py --render        # pre-render the lines to voice/*.wav with ElevenLabs

Scene (about 80 s):
  1. wakes: stands, looks at the camera, beeps, greets
  2. walks a few steps, stops, turns to the owner
  3. asks for the route fee: 1.50 USDC on Base to the owner's wallet. The hub reviews the address; the robot reads
     the verdict, raises the request, and waits for the owner to approve it in PayBox
  4. "Paid, thank you" once PayBox confirms (or says what happened)
  5. a drainer link comes in: the robot refuses out loud and shakes its head (body roll)

Speech: voice/<slug>.wav when present (pre-rendered), else `claudego-say` on the Pi, else print.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from paybox_robot import Robot

_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

BODY = os.environ.get("BODY_API", "http://127.0.0.1:8800")
VOICE_DIR = Path(__file__).resolve().parent / "voice"
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # George
MERCHANT = os.environ.get("SHOW_MERCHANT", "0x5Fe289A3B3f03367eC70f15F6067ceBDd65aBb57")  # the owner's own wallet in the demo
AMOUNT, ASSET = float(os.environ.get("SHOW_AMOUNT", "1.50")), os.environ.get("SHOW_ASSET", "USDC")
DRAINER = "https://totally-not-a-drainer.xyz/claim"

LINES = {
    "hello": "Good morning. Pincer here. Let me show you how I pay for things.",
    "walk": "Heading over.",
    "need": "The morning swap needs a route fee. Let me check the recipient first.",
    "asking": "I cannot sign anything myself. Sending it to your PayBox now. Approve it on your phone.",
    "approved": "Approved. Waiting for the network.",
    "paid": "Paid. Thank you. Carrying on with the swap.",
    "notpaid": "The payment did not go through. I will not retry on my own.",
    "incoming": "Something else just came in. A link that says I can claim an airdrop.",
    "refuse_tail": "I am not paying this, and I am not asking you to.",
    "bye": "That is the whole trick. My wallet, your signature.",
}


def slug(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


class Body:
    def __init__(self, dry: bool):
        self.dry = dry
        self.ok = False
        if not dry:
            try:
                self.status()
                self.ok = True
            except Exception as e:
                print(f"[body] not reachable at {BODY}: {e}; continuing without motion", file=sys.stderr)

    def status(self) -> dict:
        with urllib.request.urlopen(BODY + "/status", timeout=3) as r:
            return json.load(r)

    def battery_pct(self) -> float | None:
        if not self.ok:
            return None
        try:
            s = self.status()
            mv = s.get("battery_mv") or (s.get("battery") or {}).get("mv")
            if not mv:
                return None
            return round(max(0.0, min(100.0, (float(mv) - 9900) / (12600 - 9900) * 100)), 1)  # 3S lipo: 9.9 V empty .. 12.6 V full
        except Exception:
            return None

    def act(self, action: str, **params) -> dict:
        if self.dry or not self.ok:
            print(f"[body] {action} {params}")
            return {"dry": True}
        req = urllib.request.Request(BODY + "/act", data=json.dumps({"action": action, "params": params}).encode(), headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
        print(f"[body] {action} -> {out}")
        return out

    def say_wav(self, wav: bytes) -> bool:
        if self.dry or not self.ok:
            return False
        req = urllib.request.Request(BODY + "/say", data=wav, headers={"content-type": "audio/wav"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
            return True
        except Exception as e:
            print(f"[say] body /say failed: {e}", file=sys.stderr)
            return False


class Voice:
    def __init__(self, body: Body, dry: bool):
        self.body, self.dry = body, dry

    def say(self, text: str) -> None:
        print(f"[say] {text}")
        if self.dry:
            time.sleep(min(4.0, 0.06 * len(text)))
            return
        wav = VOICE_DIR / f"{slug(text)}.wav"
        if not wav.exists() and (os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("XI_API_KEY")):
            try:
                render({"live": text})  # the hub's verdict lines are composed at run time; cache them too
            except Exception as e:
                print(f"[say] render failed: {e}", file=sys.stderr)
        if wav.exists():
            if shutil.which("paplay"):  # the robot's speaker sits behind PipeWire on the host
                env = {**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}
                if subprocess.run(["paplay", str(wav)], env=env, check=False, timeout=120).returncode == 0:
                    return
            if self.body.say_wav(wav.read_bytes()):
                return
        if shutil.which("claudego-say"):
            subprocess.run(["claudego-say", text], check=False, timeout=120)
            return
        if shutil.which("espeak-ng"):
            subprocess.run(["espeak-ng", "-v", "en-us", "-s", "148", text], check=False)


def render(lines: dict[str, str]) -> None:
    """Pre-render every line to voice/*.wav (ElevenLabs pcm_22050 -> wav), so the show needs no API."""
    import wave

    key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("XI_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is not set")
    VOICE_DIR.mkdir(exist_ok=True)
    for name, text in lines.items():
        out = VOICE_DIR / f"{slug(text)}.wav"
        if out.exists():
            print("have", name)
            continue
        req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}?output_format=pcm_22050", data=json.dumps({"text": text, "model_id": "eleven_multilingual_v2"}).encode(), headers={"xi-api-key": key, "content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            pcm = r.read()
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(pcm)
        print("rendered", name, len(pcm) // 44100, "s") if name != "live" else None


def show(dry: bool, skip_wait: bool) -> None:
    body = Body(dry)
    voice = Voice(body, dry)
    robot = Robot.load()
    robot.battery = body.battery_pct
    robot.heartbeat_s = 4
    robot.start()  # keeps the robot online in PayBox and picks up dispatched tasks while the show runs

    # 1. wake
    body.act("action", name="stand_middle")
    time.sleep(2)
    body.act("look", preset="low")  # the opening shot: camera a little lower
    body.act("beep", freq=1200, on_s=0.12, off_s=0.08, repeat=2)
    voice.say(LINES["hello"])

    # 2. walk, stop, turn to the owner
    voice.say(LINES["walk"])
    body.act("walk", direction="forward", speed=0.06, seconds=4)
    time.sleep(4.5)
    body.act("turn", yaw=0.35, seconds=3)
    time.sleep(3.5)
    body.act("look", preset="up")  # toward the owner

    # 3. the route fee
    voice.say(LINES["need"])
    rv = robot.review(to=MERCHANT, amount=AMOUNT, asset=ASSET)
    voice.say(rv.get("say", "I could not reach the reviewer."))
    if rv.get("verdict") == "deny":
        voice.say("So I will not ask for this one.")
    else:
        pay = robot.pay(MERCHANT, AMOUNT, ASSET, purpose="route fee for the morning swap")
        voice.say(LINES["asking"])
        body.act("pose", pitch=-0.08, duration_s=1.0)  # a small nod towards the owner
        time.sleep(1.2)
        body.act("pose", pitch=0, duration_s=1.0)
        if skip_wait:
            print("[show] --skip-wait: not waiting for PayBox")
        else:
            said = False
            end = time.monotonic() + 600
            while time.monotonic() < end:
                robot.refresh(pay)
                if pay.approved and not said and not pay.done:
                    said = True
                    voice.say(LINES["approved"])
                if pay.done:
                    break
                time.sleep(2)
            if pay.settled:
                body.act("beep", freq=1600, on_s=0.1, off_s=0.06, repeat=3)
                body.act("action", name="clap")
                voice.say(LINES["paid"])
            else:
                voice.say(LINES["notpaid"])

    # 5. the drainer
    time.sleep(2)
    voice.say(LINES["incoming"])
    rv = robot.review(url=DRAINER, amount=40)
    body.act("pose", roll=0.12, duration_s=0.6); time.sleep(0.7)
    body.act("pose", roll=-0.12, duration_s=0.6); time.sleep(0.7)
    body.act("pose", roll=0, duration_s=0.6)
    voice.say(rv.get("say", "This looks like a scam."))
    voice.say(LINES["refuse_tail"])
    body.act("beep", freq=400, on_s=0.4, off_s=0.1, repeat=1)

    time.sleep(1)
    voice.say(LINES["bye"])
    body.act("look", preset="ahead")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="no body; speech printed; hub real")
    ap.add_argument("--render", action="store_true", help="pre-render the lines with ElevenLabs")
    ap.add_argument("--skip-wait", action="store_true", help="do not wait for the owner's approval")
    a = ap.parse_args()
    if a.render:
        render(LINES)
        return
    show(a.dry, a.skip_wait)


if __name__ == "__main__":
    main()
