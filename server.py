#!/usr/bin/env python3
"""Itinerary Desk MCP server: search, hold, book, change and cancel flights through the Duffel API (v2).
TEST MODE ONLY: refuses to start write tools unless DUFFEL_ACCESS_TOKEN starts with 'duffel_test_'.
With no token it runs against recorded Duffel-shaped fixtures (Duffel Airways, ZZ) so tests and demos work offline.
Writes are two-step: a quote/price tool returns a short-lived confirm_token (HMAC over the exact action + amount);
the write tool only runs with that token, explicit confirm=true, and an idempotency_key.
Transports: stdio (default) and stateless Streamable HTTP (`--http`, POST /mcp). Zero dependencies (Python 3.10+)."""
import json, os, sys, time, hmac, hashlib, secrets, re, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fixtures
VERSION = "0.1.0"
SUPPORTED = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]
API = "https://api.duffel.com"
TOKEN = os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip()
SECRET = (os.environ.get("ITINERARY_CONFIRM_SECRET") or secrets.token_hex(32)).encode()
TOKEN_TTL = 600
_FAKE = fixtures.FakeDuffel()
IDEM_PATH = os.environ.get("ITINERARY_IDEM_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".idempotency.json"))
class _Idem(dict):
    """idempotency_key -> prior result, persisted to a JSON file so repeat keys survive restarts."""
    def __init__(self, path):
        super().__init__(); self.path = path
        try:
            with open(path) as f: super().update(json.load(f))
        except (OSError, ValueError): pass
    def __setitem__(self, k, v):
        super().__setitem__(k, v); tmp = self.path + ".tmp"
        with open(tmp, "w") as f: json.dump(dict(self), f)
        os.replace(tmp, self.path)
_IDEM = _Idem(IDEM_PATH)

def mode(): return "fixtures" if not TOKEN else ("duffel_test" if TOKEN.startswith("duffel_test_") else "LIVE_REFUSED")
def duffel(method, path, body=None):
    if not TOKEN: return _FAKE.req(method, path, body)
    if not TOKEN.startswith("duffel_test_"): raise PermissionError("Refusing: DUFFEL_ACCESS_TOKEN is not a test token (must start with duffel_test_). Itinerary Desk is test-mode only.")
    rq = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={
        "Authorization": "Bearer " + TOKEN, "Duffel-Version": "v2", "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "ItineraryDesk/" + VERSION})
    try:
        with urllib.request.urlopen(rq, timeout=60) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: errs = json.loads(e.read()).get("errors", [])
        except Exception: errs = []
        raise LookupError(e.code, "; ".join(x.get("message", "") for x in errs) or e.reason)

def _sign(*parts):
    exp = int(time.time()) + TOKEN_TTL; msg = "|".join(map(str, parts)) + f"|{exp}"
    return f"{exp}.{hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()[:32]}"
def _check(tok, *parts):
    try: exp, sig = str(tok).split(".", 1); exp = int(exp)
    except ValueError: raise ValueError("confirm_token missing or malformed. Call the quote/price tool first and show the human the amount.")
    if exp < time.time(): raise ValueError("confirm_token expired. Re-quote and get a fresh confirmation from the human.")
    good = hmac.new(SECRET, ("|".join(map(str, parts)) + f"|{exp}").encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, sig): raise ValueError("confirm_token does not match this exact action/amount. Re-quote.")
def _gate(a):
    if mode() == "LIVE_REFUSED": raise PermissionError("Refusing: live Duffel tokens are not allowed. Use a duffel_test_ token.")
    if a.get("confirm") is not True: raise ValueError("confirm must be true, set only after the human explicitly approved this exact amount.")
    k = str(a.get("idempotency_key") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", k): raise ValueError("idempotency_key required (8-64 chars, [A-Za-z0-9_-]). Reuse it on retries.")
    return k

def _seg(s): return {"from": s["origin"]["iata_code"], "to": s["destination"]["iata_code"], "departs": s["departing_at"], "arrives": s["arriving_at"],
                     "flight": s["marketing_carrier"]["iata_code"] + s.get("marketing_carrier_flight_number", "")}
def _slices(sl): return [{"slice_id": s["id"], "fare_brand": s.get("fare_brand_name"), "segments": [_seg(x) for x in s["segments"]]} for s in sl]
def _cond(c):
    c = c or {}; f = lambda k: (lambda v: v and {"allowed": v.get("allowed"), "penalty": v.get("penalty_amount"), "currency": v.get("penalty_currency")})(c.get(k))
    return {"refund_before_departure": f("refund_before_departure"), "change_before_departure": f("change_before_departure")}
def _offer(o):
    pr = o.get("payment_requirements") or {}
    return {"offer_id": o["id"], "airline": o["owner"]["name"], "total_amount": o["total_amount"], "currency": o["total_currency"],
            "expires_at": o.get("expires_at"), "slices": _slices(o["slices"]), "conditions": _cond(o.get("conditions")),
            "hold_available": pr.get("requires_instant_payment") is False, "passenger_ids": [p["id"] for p in o.get("passengers", [])]}
def _order(o):
    return {"order_id": o["id"], "booking_reference": o.get("booking_reference"), "type": o.get("type"), "live_mode": o.get("live_mode"),
            "total_amount": o["total_amount"], "currency": o["total_currency"], "slices": _slices(o["slices"]), "conditions": _cond(o.get("conditions")),
            "available_actions": o.get("available_actions", []), "cancelled_at": o.get("cancelled_at"),
            "awaiting_payment": (o.get("payment_status") or {}).get("awaiting_payment")}

def search_flights(a):
    sl = [{"origin": a["origin"].upper(), "destination": a["destination"].upper(), "departure_date": a["departure_date"]}]
    if a.get("return_date"): sl.append({"origin": a["destination"].upper(), "destination": a["origin"].upper(), "departure_date": a["return_date"]})
    n = max(1, min(9, int(a.get("adults", 1))))
    r = duffel("POST", "/air/offer_requests?return_offers=true&supplier_timeout=20000",
               {"data": {"slices": sl, "passengers": [{"type": "adult"}] * n, "cabin_class": a.get("cabin_class", "economy"),
                         **({"max_connections": a["max_connections"]} if "max_connections" in a else {})}})["data"]
    offs = sorted(r.get("offers", []), key=lambda o: float(o["total_amount"]))
    if a.get("max_price") is not None: offs = [o for o in offs if float(o["total_amount"]) <= a["max_price"]]
    return {"mode": mode(), "offer_request_id": r["id"], "count": len(offs), "offers": [_offer(o) for o in offs[:min(int(a.get("limit", 5)), 20)]],
            "next": "Call price_check on the chosen offer_id before booking."}
def get_offer(a): return {"mode": mode(), "offer": _offer(duffel("GET", f"/air/offers/{a['offer_id']}?return_available_services=false")["data"])}
def price_check(a):
    o = duffel("GET", f"/air/offers/{a['offer_id']}")["data"]; off = _offer(o)
    exp = a.get("expected_amount"); changed = exp is not None and f"{float(exp):.2f}" != f"{float(o['total_amount']):.2f}"
    kinds = ["instant"] + (["hold"] if off["hold_available"] else [])
    return {"mode": mode(), "offer": off, "price_changed": changed, "previous_amount": exp, "bookable_as": kinds,
            "confirm_tokens": {k: _sign("book", o["id"], o["total_amount"], o["total_currency"], k) for k in kinds},
            "confirm_token_ttl_seconds": TOKEN_TTL,
            "instructions": f"Show the human: {off['airline']}, {o['total_amount']} {o['total_currency']}, refund/change rules. Only after they say yes, call book_order with confirm=true."}
def book_order(a):
    k = _gate(a)
    if k in _IDEM: return dict(_IDEM[k], idempotent_replay=True)
    kind = a.get("type", "instant")
    if kind not in ("instant", "hold"): raise ValueError("type must be instant or hold")
    _check(a.get("confirm_token"), "book", a["offer_id"], a["amount"], a["currency"], kind)
    ps = a.get("passengers") or []
    if not ps: raise ValueError("passengers required (id from the offer, title, given_name, family_name, born_on, gender, email, phone_number)")
    body = {"type": kind, "selected_offers": [a["offer_id"]], "passengers": ps, "metadata": {"idempotency_key": k, "source": "itinerary_desk"}}
    if kind == "instant": body["payments"] = [{"type": "balance", "amount": a["amount"], "currency": a["currency"]}]
    o = duffel("POST", "/air/orders", {"data": body})["data"]
    res = {"mode": mode(), "booked": True, "order": _order(o), "note": "Test mode: paid from Duffel test balance; no real ticket or money."}
    _IDEM[k] = res; return res
def get_order(a): return {"mode": mode(), "order": _order(duffel("GET", f"/air/orders/{a['order_id']}")["data"])}
def cancel_order(a):
    if not a.get("cancellation_id"):
        o = duffel("GET", f"/air/orders/{a['order_id']}")["data"]
        if "cancel" not in o.get("available_actions", []): return {"mode": mode(), "cancellable": False, "reason": "Duffel lists no 'cancel' action for this order; contact the airline or Duffel support."}
        c = duffel("POST", "/air/order_cancellations", {"data": {"order_id": a["order_id"]}})["data"]
        return {"mode": mode(), "step": "quote", "cancellation_id": c["id"], "refund_amount": c["refund_amount"], "refund_currency": c["refund_currency"],
                "refund_to": c.get("refund_to"), "expires_at": c.get("expires_at"),
                "confirm_token": _sign("cancel", a["order_id"], c["id"], c["refund_amount"]),
                "instructions": "Nothing is cancelled yet. Show the refund to the human; on explicit yes call cancel_order again with cancellation_id, confirm_token, confirm=true, idempotency_key."}
    k = _gate(a)
    if k in _IDEM: return dict(_IDEM[k], idempotent_replay=True)
    _check(a.get("confirm_token"), "cancel", a["order_id"], a["cancellation_id"], a.get("refund_amount"))
    c = duffel("POST", f"/air/order_cancellations/{a['cancellation_id']}/actions/confirm")["data"]
    res = {"mode": mode(), "step": "confirmed", "cancelled": True, "cancellation_id": c["id"], "refund_amount": c["refund_amount"], "refund_currency": c["refund_currency"], "confirmed_at": c.get("confirmed_at")}
    _IDEM[k] = res; return res
def change_order(a):
    if not a.get("change_offer_id"):
        o = duffel("GET", f"/air/orders/{a['order_id']}")["data"]
        if "change" not in o.get("available_actions", []): return {"mode": mode(), "changeable": False, "reason": "Duffel lists no 'change' action for this order."}
        sid = a.get("remove_slice_id") or o["slices"][0]["id"]; old = next(s for s in o["slices"] if s["id"] == sid)
        add = {"origin": a.get("new_origin", old["origin"]["iata_code"]), "destination": a.get("new_destination", old["destination"]["iata_code"]),
               "departure_date": a["new_departure_date"], "cabin_class": a.get("cabin_class", "economy")}
        r = duffel("POST", "/air/order_change_requests", {"data": {"order_id": a["order_id"], "slices": {"remove": [{"slice_id": sid}], "add": [add]}}})["data"]
        offs = sorted(r.get("order_change_offers", []), key=lambda x: float(x["change_total_amount"]))
        return {"mode": mode(), "step": "quote", "order_change_request_id": r["id"], "options": [{
            "change_offer_id": x["id"], "change_total_amount": x["change_total_amount"], "penalty_total_amount": x["penalty_total_amount"],
            "new_total_amount": x["new_total_amount"], "currency": x["change_total_currency"], "expires_at": x.get("expires_at"),
            "new_slices": _slices(x["slices"]["add"]), "confirm_token": _sign("change", a["order_id"], x["id"], x["change_total_amount"])} for x in offs],
            "instructions": "Nothing has changed yet. Show options (fare difference + penalty) to the human; on explicit yes call change_order with change_offer_id, change_total_amount, confirm_token, confirm=true, idempotency_key."}
    k = _gate(a)
    if k in _IDEM: return dict(_IDEM[k], idempotent_replay=True)
    amt = a.get("change_total_amount"); _check(a.get("confirm_token"), "change", a["order_id"], a["change_offer_id"], amt)
    p = duffel("POST", "/air/order_changes", {"data": {"selected_order_change_offer": a["change_offer_id"]}})["data"]
    body = {"payment": {"type": "balance", "amount": p["change_total_amount"], "currency": p["change_total_currency"]}} if float(p["change_total_amount"]) > 0 else {}
    c = duffel("POST", f"/air/order_changes/{p['id']}/actions/confirm", {"data": body})["data"]
    res = {"mode": mode(), "step": "confirmed", "changed": True, "order_change_id": c["id"], "charged": c["change_total_amount"], "currency": c["change_total_currency"],
           "new_total_amount": c["new_total_amount"], "confirmed_at": c.get("confirmed_at")}
    _IDEM[k] = res; return res

S = lambda **p: {"type": "object", "properties": p, "additionalProperties": False}
STR, NUM, BOOL, INT = {"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "integer"}
WRITE = {"confirm": {"type": "boolean", "description": "Must be true; set only after explicit human approval of the quoted amount."},
         "confirm_token": {"type": "string", "description": "Token from the matching quote/price tool."},
         "idempotency_key": {"type": "string", "description": "Unique per intended action (8-64 chars). Reuse on retry."}}
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
PAX = {"type": "array", "minItems": 1, "items": S(id=STR, title=STR, given_name=STR, family_name=STR, born_on=STR, gender=STR, email=STR, phone_number=STR)}
def T(n, title, desc, schema, req, ann): schema["required"] = req; return {"name": n, "title": title, "description": desc, "inputSchema": schema, "annotations": {"title": title, **ann}}
TOOLS = [
 T("search_flights", "Search flights", "Search flight offers via a Duffel offer request. IATA airport/city codes, dates YYYY-MM-DD. Returns offers cheapest first with fare rules and whether a hold is possible. Read-only.",
   S(origin=STR, destination=STR, departure_date=STR, return_date=STR, adults=dict(INT, default=1), cabin_class={"type": "string", "enum": ["economy", "premium_economy", "business", "first"]},
     max_connections=INT, max_price=NUM, limit=dict(INT, default=5)), ["origin", "destination", "departure_date"], RO),
 T("get_offer", "Get offer details", "Get one offer by id with slices, price, refund/change conditions. Read-only.", S(offer_id=STR), ["offer_id"], RO),
 T("price_check", "Price check before booking/hold", "Re-fetch the offer's live price, report if it changed vs expected_amount, say whether it can be held (pay later) or must be paid instantly, and issue confirm_tokens for book_order. Does not book. Read-only.",
   S(offer_id=STR, expected_amount=STR), ["offer_id"], {**RO, "idempotentHint": False}),
 T("book_order", "Book or hold a flight (TEST MODE)", "Create a Duffel order (type instant = paid from test balance, hold = reserve, pay later). DESTRUCTIVE: requires confirm=true, a confirm_token from price_check for this exact offer/amount/type, and an idempotency_key. Refuses any non-test Duffel token.",
   S(offer_id=STR, amount=STR, currency=STR, type={"type": "string", "enum": ["instant", "hold"], "default": "instant"}, passengers=PAX, **WRITE),
   ["offer_id", "amount", "currency", "passengers", "confirm", "confirm_token", "idempotency_key"], {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}),
 T("get_order", "Get order", "Get a Duffel order: PNR, slices, conditions, available_actions (cancel/change). Read-only.", S(order_id=STR), ["order_id"], RO),
 T("cancel_order", "Cancel order (quote, then confirm)", "Step 1 (only order_id): creates a PENDING cancellation and returns refund amount + confirm_token; nothing is cancelled. Step 2 (cancellation_id, refund_amount, confirm_token, confirm=true, idempotency_key): confirms. DESTRUCTIVE at step 2.",
   S(order_id=STR, cancellation_id=STR, refund_amount=STR, **WRITE), ["order_id"], {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}),
 T("change_order", "Change order (quote, then confirm)", "Step 1 (order_id, new_departure_date, optional remove_slice_id/new_origin/new_destination): creates an order change request and lists options with fare difference + penalty; nothing changes. Step 2 (change_offer_id, change_total_amount, confirm_token, confirm=true, idempotency_key): creates and confirms the change, paying any difference from test balance. DESTRUCTIVE at step 2.",
   S(order_id=STR, new_departure_date=STR, remove_slice_id=STR, new_origin=STR, new_destination=STR, cabin_class=STR, change_offer_id=STR, change_total_amount=STR, **WRITE),
   ["order_id"], {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}),
]
FUNCS = {"search_flights": search_flights, "get_offer": get_offer, "price_check": price_check, "book_order": book_order,
         "get_order": get_order, "cancel_order": cancel_order, "change_order": change_order}
def handle(req):
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0": return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    mid, meth, p = req.get("id"), req.get("method"), req.get("params") or {}
    if "id" not in req: return None
    if meth == "initialize":
        v = p.get("protocolVersion"); v = v if v in SUPPORTED else SUPPORTED[0]
        r = {"protocolVersion": v, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "itinerary-desk", "title": "Itinerary Desk", "version": VERSION},
             "instructions": "Flights via Duffel, TEST MODE ONLY. Flow: search_flights -> price_check -> show the human price + rules -> book_order only after explicit yes. Cancel/change: call once to quote, show the human, call again with the token only after explicit yes. Never invent confirm tokens."}
    elif meth == "ping": r = {}
    elif meth == "tools/list": r = {"tools": TOOLS}
    elif meth == "tools/call":
        f = FUNCS.get(p.get("name"))
        if not f: return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {p.get('name')}"}}
        try: data = f(p.get("arguments") or {}); err = False
        except (ValueError, KeyError, LookupError, PermissionError, TypeError, urllib.error.URLError) as e:
            data = {"error": str(e.args[1]) if isinstance(e, LookupError) and len(e.args) == 2 else (f"missing argument {e}" if isinstance(e, KeyError) else str(e)), "mode": mode()}; err = True
        r = {"content": [{"type": "text", "text": json.dumps(data, indent=1)}], "structuredContent": data, "isError": err}
    else: return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {meth}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": r}
ALLOWED_ORIGINS = [o for o in os.environ.get("ITINERARY_ALLOWED_ORIGINS", "https://claude.ai,https://claude.com,https://grok.com,https://muse.ai").split(",") if o]
API_KEY = os.environ.get("ITINERARY_API_KEY", "").strip()
def authorized(headers, path):
    """Optional gate. If ITINERARY_API_KEY is set, require it as 'Authorization: Bearer <key>', 'X-API-Key: <key>',
    or '?key=<key>' on the /mcp URL (for clients like Claude/Grok custom connectors that only take a URL)."""
    if not API_KEY: return True
    import urllib.parse
    a = headers.get("Authorization") or ""
    cands = [a[7:].strip() if a.lower().startswith("bearer ") else "", headers.get("X-API-Key") or ""]
    cands += urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("key", [])
    return any(c and hmac.compare_digest(c, API_KEY) for c in cands)
class H(BaseHTTPRequestHandler):
    server_version = "itinerary-desk/" + VERSION
    def _send(self, code, body=None):
        b = b"" if body is None else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/healthz": return self._send(200, {"ok": True, "mode": mode(), "version": VERSION})
        self._send(405 if self.path.startswith("/mcp") else 404, {"error": "POST JSON-RPC to /mcp"})
    def do_DELETE(self): self._send(405, {"error": "stateless server; no sessions"})
    def do_POST(self):
        if not self.path.startswith("/mcp"): return self._send(404, {"error": "not found"})
        if not authorized(self.headers, self.path): return self._send(401, {"error": "missing or invalid API key"})
        o = self.headers.get("Origin")
        if o and o not in ALLOWED_ORIGINS and not o.startswith(("http://localhost", "http://127.0.0.1")): return self._send(403, {"error": "origin not allowed"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64_000: return self._send(413, {"error": "payload too large"})
        try: msg = json.loads(self.rfile.read(n) or b"null")
        except json.JSONDecodeError: return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        out = [r for r in map(handle, msg) if r] if isinstance(msg, list) else handle(msg)
        self._send(202) if not out else self._send(200, out)
if __name__ == "__main__":
    if "--http" in sys.argv: ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8789"))), H).serve_forever()
    else:
        for line in sys.stdin:
            if not line.strip(): continue
            try: out = handle(json.loads(line))
            except json.JSONDecodeError: out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            if out: print(json.dumps(out), flush=True)
