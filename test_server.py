import json, os, subprocess, sys, unittest, importlib
os.environ.pop("DUFFEL_ACCESS_TOKEN", None)
import tempfile
os.environ["ITINERARY_IDEM_PATH"] = os.path.join(tempfile.mkdtemp(), "idem.json")
import server
def call(n, a): return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": n, "arguments": a}})["result"]
PAX = [{"id": "pas_0000FIXTURE1", "title": "mr", "given_name": "Test", "family_name": "Traveler", "born_on": "1990-01-01", "gender": "m", "email": "t@example.com", "phone_number": "+15555550100"}]
class Protocol(unittest.TestCase):
    def test_init(self):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}})["result"]
        self.assertEqual(r["protocolVersion"], "2026-07-28")
    def test_tools(self):
        t = {x["name"]: x for x in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(set(t), {"search_flights", "get_offer", "price_check", "book_order", "get_order", "cancel_order", "change_order"})
        for n in ("book_order", "cancel_order", "change_order"): self.assertTrue(t[n]["annotations"]["destructiveHint"])
        for n in ("search_flights", "get_offer", "get_order"): self.assertTrue(t[n]["annotations"]["readOnlyHint"])
    def test_notification(self): self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    def test_stdio(self):
        out = subprocess.run([sys.executable, "server.py"], input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n', capture_output=True, text=True, timeout=10, env={k: v for k, v in os.environ.items() if k != "DUFFEL_ACCESS_TOKEN"}).stdout
        self.assertEqual(len(json.loads(out)["result"]["tools"]), 7)
class Flow(unittest.TestCase):
    def book(self, key, kind="instant"):
        s = call("search_flights", {"origin": "lhr", "destination": "JFK", "departure_date": "2099-06-01"})["structuredContent"]
        self.assertEqual(s["mode"], "fixtures"); off = s["offers"][-1] if kind == "hold" else s["offers"][0]
        pc = call("price_check", {"offer_id": off["offer_id"], "expected_amount": off["total_amount"]})["structuredContent"]
        self.assertFalse(pc["price_changed"])
        return off, pc, call("book_order", {"offer_id": off["offer_id"], "amount": off["total_amount"], "currency": "USD", "type": kind, "passengers": PAX,
                                            "confirm": True, "confirm_token": pc["confirm_tokens"][kind], "idempotency_key": key})
    def test_search_sorted(self):
        o = call("search_flights", {"origin": "LHR", "destination": "JFK", "departure_date": "2099-06-01"})["structuredContent"]["offers"]
        self.assertLessEqual(float(o[0]["total_amount"]), float(o[1]["total_amount"]))
    def test_book_get_cancel(self):
        _, _, b = self.book("test-key-cancel-1"); self.assertFalse(b["isError"], b); oid = b["structuredContent"]["order"]["order_id"]
        self.assertIn("cancel", call("get_order", {"order_id": oid})["structuredContent"]["order"]["available_actions"])
        q = call("cancel_order", {"order_id": oid})["structuredContent"]; self.assertEqual(q["step"], "quote")
        self.assertIsNone(call("get_order", {"order_id": oid})["structuredContent"]["order"]["cancelled_at"])  # quote does not cancel
        bad = call("cancel_order", {"order_id": oid, "cancellation_id": q["cancellation_id"], "refund_amount": "999.00", "confirm_token": q["confirm_token"], "confirm": True, "idempotency_key": "cancel-key-1"})
        self.assertTrue(bad["isError"])
        ok = call("cancel_order", {"order_id": oid, "cancellation_id": q["cancellation_id"], "refund_amount": q["refund_amount"], "confirm_token": q["confirm_token"], "confirm": True, "idempotency_key": "cancel-key-2"})
        self.assertTrue(ok["structuredContent"]["cancelled"])
        self.assertTrue(call("get_order", {"order_id": oid})["structuredContent"]["order"]["cancelled_at"])
    def test_hold(self):
        _, _, b = self.book("test-key-hold-1", "hold"); self.assertTrue(b["structuredContent"]["order"]["awaiting_payment"])
    def test_idempotent(self):
        _, _, a = self.book("same-key-123"); _, _, b = self.book("same-key-123")
        self.assertEqual(a["structuredContent"]["order"]["order_id"], b["structuredContent"]["order"]["order_id"]); self.assertTrue(b["structuredContent"]["idempotent_replay"])
    def test_refusals(self):
        off, pc, _ = self.book("refusal-base-1"); base = {"offer_id": off["offer_id"], "amount": off["total_amount"], "currency": "USD", "passengers": PAX, "idempotency_key": "refuse-key-1"}
        self.assertTrue(call("book_order", dict(base, confirm_token=pc["confirm_tokens"]["instant"]))["isError"])  # no confirm
        self.assertTrue(call("book_order", dict(base, confirm=True))["isError"])  # no token
        self.assertTrue(call("book_order", dict(base, confirm=True, confirm_token=pc["confirm_tokens"]["instant"], amount="1.00"))["isError"])  # tampered amount
        self.assertTrue(call("book_order", dict(base, confirm=True, confirm_token=pc["confirm_tokens"]["instant"], idempotency_key="x"))["isError"])
    def test_change(self):
        _, _, b = self.book("test-key-change-1"); oid = b["structuredContent"]["order"]["order_id"]
        q = call("change_order", {"order_id": oid, "new_departure_date": "2099-06-03"})["structuredContent"]; opt = q["options"][0]
        self.assertEqual(opt["change_total_amount"], "15.00")
        r = call("change_order", {"order_id": oid, "change_offer_id": opt["change_offer_id"], "change_total_amount": opt["change_total_amount"],
                                  "confirm_token": opt["confirm_token"], "confirm": True, "idempotency_key": "change-key-1"})["structuredContent"]
        self.assertTrue(r["changed"])
        self.assertTrue(call("get_order", {"order_id": oid})["structuredContent"]["order"]["slices"][0]["segments"][0]["departs"].startswith("2099-06-03"))
class Persist(unittest.TestCase):
    def test_survives_restart(self):
        server._IDEM["persist-key-1"] = {"booked": True}
        self.assertEqual(server._Idem(server.IDEM_PATH)["persist-key-1"], {"booked": True})
class Auth(unittest.TestCase):
    def test_gate(self):
        old = server.API_KEY; server.API_KEY = "s3cret-key"
        try:
            self.assertFalse(server.authorized({}, "/mcp"))
            self.assertTrue(server.authorized({"Authorization": "Bearer s3cret-key"}, "/mcp"))
            self.assertTrue(server.authorized({"X-API-Key": "s3cret-key"}, "/mcp"))
            self.assertTrue(server.authorized({}, "/mcp?key=s3cret-key"))
            self.assertFalse(server.authorized({}, "/mcp?key=wrong"))
        finally: server.API_KEY = old
        self.assertTrue(server.authorized({}, "/mcp"))
class LiveGuard(unittest.TestCase):
    def test_live_token_refused(self):
        old = server.TOKEN; server.TOKEN = "duffel_live_abc"
        try:
            r = call("search_flights", {"origin": "LHR", "destination": "JFK", "departure_date": "2099-06-01"})
            self.assertTrue(r["isError"]); self.assertIn("test", r["structuredContent"]["error"])
        finally: server.TOKEN = old
if __name__ == "__main__": unittest.main(verbosity=2)
