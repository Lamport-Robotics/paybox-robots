import json, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from paybox_robot import Hub, Robot
from paybox_robot.robot import to_atomic, to_cents

STATE = {"tasks": [], "payments": {}, "beats": []}

class FakeHub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _body(self):
        n = int(self.headers.get("content-length") or 0); return json.loads(self.rfile.read(n) or b"{}")
    def do_POST(self):
        p, b = self.path, self._body()
        if p == "/register":
            return self._json(200, {"robot_id": "rb_1", "robot_token": "rbt_secret", "owner_code": b["owner_code"], "hub": "x"})
        if self.headers.get("authorization") != "Bearer rbt_secret":
            return self._json(401, {"error": "robot token"})
        if p.endswith("/heartbeat"):
            STATE["beats"].append(b); t = STATE["tasks"][0] if STATE["tasks"] else None
            return self._json(200, {"ok": True, "task": t})
        if "/tasks/" in p and p.endswith("/take"):
            STATE["tasks"].clear(); return self._json(200, {"ok": True})
        if "/tasks/" in p and p.endswith("/result"):
            STATE["result"] = b; return self._json(200, {"ok": True})
        if p.endswith("/review"):
            return self._json(200, {"verdict": "deny" if (b.get("amount_cents") or 0) > 100000 else "allow", "say": "fine", "reasons": ["known"]})
        if p.endswith("/payments"):
            pid = f"pay_{len(STATE['payments'])+1}"; STATE["payments"][pid] = {**b, "status": "requested"}
            return self._json(200, {"id": pid, "review": {"verdict": "allow", "say": "looks fine", "reasons": []}})
        if "/payments/" in p:
            STATE["payments"][p.rsplit("/", 1)[1]].update(b); return self._json(200, {"ok": True})
        self._json(404, {"error": "nope"})
    def do_GET(self):
        if "/payments/" in self.path:
            pid = self.path.rsplit("/", 1)[1]; return self._json(200, {"id": pid, **STATE["payments"][pid]})
        if self.path.startswith("/owner/"):
            return self._json(200, {"robots": [{"id": "rb_1", "name": "t"}]})
        self._json(404, {"error": "nope"})

@pytest.fixture(scope="module")
def hub_url():
    srv = HTTPServer(("127.0.0.1", 0), FakeHub); threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"; srv.shutdown()

@pytest.fixture
def robot(hub_url, tmp_path):
    r = Robot.setup("t", "owner-1", hub_url=hub_url, budget_cents=1000, path=tmp_path / "robot.json")
    assert oct((tmp_path / "robot.json").stat().st_mode & 0o777) == "0o600"
    return Robot.load(tmp_path / "robot.json")

def test_amounts():
    assert to_cents(2.5, "USDC") == 250 and to_cents(0.01, "SOL") is None
    assert to_atomic(0.01, "SOL") == "10000000" and to_atomic(2.5, "usdc") == "2500000"
    with pytest.raises(ValueError):
        to_atomic(1, "DOGE")

def test_setup_and_heartbeat(robot):
    robot.battery = lambda: 87.26
    assert robot.beat() is None
    assert STATE["beats"][-1]["battery_pct"] == 87.3 and STATE["beats"][-1]["budget_cents"] == 1000

def test_task_round_trip(robot):
    STATE["tasks"].append({"id": "task_1", "task": "fetch the weather", "max_spend_cents": 50})
    seen = []
    robot.on_task(lambda t: (seen.append(t), {"done": t.text})[1])
    assert robot.step() is True and seen[0].max_spend_usd == 0.5
    assert STATE["result"] == {"status": "done", "result": {"done": "fetch the weather"}}
    assert robot.step() is False

def test_task_cap_blocks_pay(robot):
    STATE["tasks"].append({"id": "task_2", "task": "buy", "max_spend_cents": 50})
    def handler(t):
        with pytest.raises(ValueError):
            robot.pay("0x" + "1" * 40, 2.0, "USDC")
        return robot.pay("0x" + "1" * 40, 0.25, "USDC").id
    robot.on_task(handler); robot.step()
    assert STATE["result"]["result"].startswith("pay_") and STATE["payments"][STATE["result"]["result"]]["amount_cents"] == 25

def test_pay_wait_and_record(robot):
    pay = robot.pay("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", 0.01, "SOL", "route fee")
    assert pay.allowed and STATE["payments"][pay.id]["asset"] == "SOL" and STATE["payments"][pay.id]["amount_atomic"] == "10000000" and "amount_cents" not in STATE["payments"][pay.id]
    threading.Timer(0.3, lambda: STATE["payments"][pay.id].update(status="proposed")).start()
    assert robot.wait(pay, timeout=5, every=0.1, until="approved").approved and not pay.settled and not pay.done
    assert not robot.wait(pay, timeout=0.5, every=0.1).done  # still in flight: wait() for done times out
    threading.Timer(0.3, lambda: STATE["payments"][pay.id].update(status="success", tx_hash="5xyz")).start()
    pay = robot.wait(pay, timeout=5, every=0.1)
    assert pay.done and pay.settled and pay.tx_hash == "5xyz"
    robot.record(pay, "success", amount_cents=1000)
    assert robot.spent_cents == 1000
    with pytest.raises(ValueError):
        robot.pay("0x" + "2" * 40, 0.01, "USDC")  # 1000 cents budget already spent

def test_x402(robot):
    robot.budget_cents = None
    pay = robot.pay_x402("https://api.example.com/forecast?city=bkk", 0.25, "weather")
    p = STATE["payments"][pay.id]
    assert p["kind"] == "x402" and p["merchant"] == "api.example.com" and p["amount_cents"] == 25 and p["amount_atomic"] == "250000"

def test_drop(robot):
    pay = robot.pay("0x" + "4" * 40, 0.10, "USDC", "oops")
    robot.drop(pay)
    assert pay.done and STATE["payments"][pay.id]["status"] == "dropped"


def test_review(robot):
    assert robot.review(to="0x" + "3" * 40, amount=5000, asset="USDC")["verdict"] == "deny"

def test_bad_token(hub_url):
    from paybox_robot.hub import HubError
    with pytest.raises(HubError) as e:
        Hub(hub_url, "rbt_wrong", "rb_1").heartbeat()
    assert e.value.status == 401
