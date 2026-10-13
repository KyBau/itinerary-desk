"""Live end-to-end run against Duffel TEST mode. Writes sanitized live_transcript.json (no token)."""
import json, os, sys, datetime, uuid
os.environ.setdefault("ITINERARY_IDEM_PATH", "/workspace/prototypes/itinerary_mcp/.idempotency.live.json")
import server
assert server.mode() == "duffel_test", server.mode()
T = []
def call(n, a):
    r = server.handle({"jsonrpc": "2.0", "id": len(T) + 1, "method": "tools/call", "params": {"name": n, "arguments": a}})["result"]
    T.append({"tool": n, "arguments": a, "isError": r["isError"], "result": r["structuredContent"]})
    print(n, "ERR" if r["isError"] else "ok", json.dumps(r["structuredContent"])[:300], flush=True); return r["structuredContent"]
def pax(ids): return [{"id": i, "title": "mr", "given_name": "Tony", "family_name": "Stark", "born_on": "1980-07-24", "gender": "m",
                       "email": "tony@example.com", "phone_number": "+442080160509"} for i in ids]
date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
run = uuid.uuid4().hex[:8]
try:
    s = call("search_flights", {"origin": "LHR", "destination": "JFK", "departure_date": date, "limit": 20})
    zz = [o for o in s.get("offers", []) if o["airline"] == "Duffel Airways"]
    inst = zz[0]; hold = next((o for o in zz if o["hold_available"]), None)
    call("get_offer", {"offer_id": inst["offer_id"]})
    pc = call("price_check", {"offer_id": inst["offer_id"], "expected_amount": inst["total_amount"]})
    o = pc["offer"]
    b = call("book_order", {"offer_id": o["offer_id"], "amount": o["total_amount"], "currency": o["currency"], "type": "instant", "passengers": pax(o["passenger_ids"]),
                            "confirm": True, "confirm_token": pc["confirm_tokens"]["instant"], "idempotency_key": f"live-inst-{run}"})
    oid = b["order"]["order_id"]
    call("book_order", {"offer_id": o["offer_id"], "amount": o["total_amount"], "currency": o["currency"], "type": "instant", "passengers": pax(o["passenger_ids"]),
                        "confirm": True, "confirm_token": pc["confirm_tokens"]["instant"], "idempotency_key": f"live-inst-{run}"})  # replay
    call("get_order", {"order_id": oid})
    cq = call("change_order", {"order_id": oid, "new_departure_date": (datetime.date.today() + datetime.timedelta(days=32)).isoformat()})
    if cq.get("options"):
        op = cq["options"][0]
        call("change_order", {"order_id": oid, "change_offer_id": op["change_offer_id"], "change_total_amount": op["change_total_amount"],
                              "confirm_token": op["confirm_token"], "confirm": True, "idempotency_key": f"live-chg-{run}"})
        call("get_order", {"order_id": oid})
    q = call("cancel_order", {"order_id": oid})
    if q.get("cancellation_id"):
        call("cancel_order", {"order_id": oid, "cancellation_id": q["cancellation_id"], "refund_amount": q["refund_amount"], "confirm_token": q["confirm_token"],
                              "confirm": True, "idempotency_key": f"live-cxl-{run}"})
        call("get_order", {"order_id": oid})
    s2 = call("search_flights", {"origin": "LHR", "destination": "JFK", "departure_date": (datetime.date.today() + datetime.timedelta(days=31)).isoformat(), "limit": 20})
    hold = next((o for o in s2.get("offers", []) if o["airline"] == "Duffel Airways" and o["hold_available"]), None)
    if hold:
        ph = call("price_check", {"offer_id": hold["offer_id"]})["offer"]
        h = call("book_order", {"offer_id": ph["offer_id"], "amount": ph["total_amount"], "currency": ph["currency"], "type": "hold", "passengers": pax(ph["passenger_ids"]),
                                "confirm": True, "confirm_token": call.__globals__["T"][-1]["result"]["confirm_tokens"]["hold"], "idempotency_key": f"live-hold-{run}"})
        if h.get("order"): call("get_order", {"order_id": h["order"]["order_id"]})
    else: T.append({"note": "no holdable Duffel Airways offer returned"})
finally:
    json.dump({"run": run, "mode": "duffel_test", "date": str(datetime.datetime.now()), "calls": T}, open("live_transcript.json", "w"), indent=1)
