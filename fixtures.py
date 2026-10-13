"""Recorded-shape Duffel v2 fixtures (Duffel Airways, IATA ZZ, test mode). Used when DUFFEL_ACCESS_TOKEN is unset.
Shapes follow duffel.com/docs/api/v2 (offer_requests, offers, orders, order_cancellations, order_change_requests, order_changes)."""
import copy, re
def _place(code, name, city): return {"type": "airport", "iata_code": code, "name": name, "city_name": city, "id": f"arp_{code.lower()}"}
LHR, JFK = _place("LHR", "Heathrow", "London"), _place("JFK", "John F. Kennedy International Airport", "New York")
ZZ = {"name": "Duffel Airways", "iata_code": "ZZ", "id": "arl_00009VME7D6ivUu8dn35WK"}
def _slice(sid, o, d, dep, arr, fn):
    return {"id": sid, "origin": o, "destination": d, "duration": "PT7H", "fare_brand_name": "Basic",
            "segments": [{"id": "seg_" + sid[4:], "origin": o, "destination": d, "departing_at": dep, "arriving_at": arr,
                          "marketing_carrier": ZZ, "operating_carrier": ZZ, "marketing_carrier_flight_number": fn,
                          "passengers": [{"passenger_id": "pas_0000FIXTURE1", "cabin_class": "economy", "baggages": [{"type": "checked", "quantity": 1}]}]}]}
COND = {"refund_before_departure": {"allowed": True, "penalty_amount": "20.00", "penalty_currency": "USD"},
        "change_before_departure": {"allowed": True, "penalty_amount": "15.00", "penalty_currency": "USD"}}
def _offer(oid, amt, dep, fn, hold=True):
    return {"id": oid, "live_mode": False, "owner": ZZ, "total_amount": amt, "total_currency": "USD", "base_amount": str(round(float(amt) * .8, 2)),
            "tax_amount": str(round(float(amt) * .2, 2)), "expires_at": "2099-01-01T00:00:00Z", "conditions": COND,
            "payment_requirements": {"requires_instant_payment": not hold, "price_guarantee_expires_at": "2099-01-01T00:00:00Z" if hold else None,
                                     "payment_required_by": "2099-01-02T00:00:00Z" if hold else None},
            "passengers": [{"id": "pas_0000FIXTURE1", "type": "adult"}],
            "slices": [_slice("sli_" + oid[4:], LHR, JFK, dep, dep[:11] + "18:00:00", fn)]}
class FakeDuffel:
    def __init__(self): self.orders, self.cancels, self.ocr, self.ocs, self.n = {}, {}, {}, {}, 0
    def _id(self, p): self.n += 1; return f"{p}_0000FIX{self.n:05d}"
    def req(self, method, path, body=None):
        d = (body or {}).get("data", {})
        if method == "POST" and path.startswith("/air/offer_requests"):
            date = d["slices"][0]["departure_date"]
            offs = [_offer("off_0000FIXA", "412.50", date + "T09:00:00", "101"), _offer("off_0000FIXB", "389.00", date + "T13:30:00", "103", hold=False)]
            return {"data": {"id": "orq_0000FIXTURE", "live_mode": False, "slices": d["slices"], "cabin_class": d.get("cabin_class"), "offers": offs}}
        m = re.fullmatch(r"/air/offers/(off_\w+)", path)
        if method == "GET" and m:
            o = {"off_0000FIXA": ("412.50", "101", True), "off_0000FIXB": ("389.00", "103", False)}.get(m[1])
            if not o: raise LookupError(404, "offer not found")
            return {"data": _offer(m[1], o[0], "2099-06-01T09:00:00", o[1], o[2])}
        if method == "POST" and path == "/air/orders":
            off = self.req("GET", "/air/offers/" + d["selected_offers"][0])["data"]
            if d.get("type") == "instant" and d["payments"][0]["amount"] != off["total_amount"]: raise LookupError(422, "price_changed")
            oid = self._id("ord"); o = {"id": oid, "live_mode": False, "type": d.get("type", "instant"), "booking_reference": "ZZ" + oid[-4:],
                 "owner": ZZ, "total_amount": off["total_amount"], "total_currency": "USD", "slices": off["slices"], "conditions": COND,
                 "passengers": [dict(p) for p in d["passengers"]], "metadata": d.get("metadata"), "cancelled_at": None, "cancellation": None,
                 "available_actions": ["cancel", "change", "update"], "payment_status": {"awaiting_payment": d.get("type") == "hold"}}
            self.orders[oid] = o; return {"data": copy.deepcopy(o)}
        m = re.fullmatch(r"/air/orders/(ord_\w+)", path)
        if method == "GET" and m:
            if m[1] not in self.orders: raise LookupError(404, "order not found")
            return {"data": copy.deepcopy(self.orders[m[1]])}
        if method == "POST" and path == "/air/order_cancellations":
            o = self.orders.get(d["order_id"])
            if not o or "cancel" not in o["available_actions"]: raise LookupError(422, "order not cancellable")
            cid = self._id("ore"); c = {"id": cid, "order_id": o["id"], "refund_amount": str(round(float(o["total_amount"]) - 20, 2)), "refund_currency": "USD",
                 "refund_to": "balance", "expires_at": "2099-01-01T00:00:00Z", "confirmed_at": None, "live_mode": False}
            self.cancels[cid] = c; return {"data": dict(c)}
        m = re.fullmatch(r"/air/order_cancellations/(ore_\w+)/actions/confirm", path)
        if method == "POST" and m:
            c = self.cancels[m[1]]; c["confirmed_at"] = "2026-10-13T07:00:00Z"; o = self.orders[c["order_id"]]
            o.update(cancelled_at=c["confirmed_at"], cancellation=dict(c), available_actions=[]); return {"data": dict(c)}
        if method == "POST" and path == "/air/order_change_requests":
            o = self.orders.get(d["order_id"])
            if not o or "change" not in o["available_actions"]: raise LookupError(422, "order not changeable")
            rid = self._id("ocr"); new = d["slices"]["add"][0]["departure_date"]
            offs = [{"id": f"oco_0000FIX{i}{rid[-3:]}", "change_total_amount": amt, "change_total_currency": "USD", "penalty_total_amount": "15.00",
                     "penalty_total_currency": "USD", "new_total_amount": str(round(float(o["total_amount"]) + float(amt), 2)), "new_total_currency": "USD",
                     "refund_to": "original_form_of_payment", "expires_at": "2099-01-01T00:00:00Z",
                     "slices": {"remove": [s for s in o["slices"] if s["id"] in [r["slice_id"] for r in d["slices"]["remove"]]],
                                "add": [_slice(f"sli_0000NEW{i}", LHR, JFK, new + t, new + "T18:00:00", fn)]}}
                    for i, (amt, t, fn) in enumerate([("45.00", "T08:00:00", "201"), ("15.00", "T19:00:00", "205")])]
            self.ocr[rid] = {"id": rid, "order_id": o["id"], "order_change_offers": offs, "live_mode": False}
            return {"data": copy.deepcopy(self.ocr[rid])}
        if method == "POST" and path == "/air/order_changes":
            off = next(x for r in self.ocr.values() for x in r["order_change_offers"] if x["id"] == d["selected_order_change_offer"])
            oid = next(r["order_id"] for r in self.ocr.values() if off in r["order_change_offers"])
            cid = self._id("oce"); self.ocs[cid] = dict(off, id=cid, order_id=oid, confirmed_at=None); return {"data": dict(self.ocs[cid])}
        m = re.fullmatch(r"/air/order_changes/(oce_\w+)/actions/confirm", path)
        if method == "POST" and m:
            c = self.ocs[m[1]]
            if float(c["change_total_amount"]) > 0 and (d.get("payment") or {}).get("amount") != c["change_total_amount"]: raise LookupError(422, "payment amount mismatch")
            c["confirmed_at"] = "2026-10-13T07:00:00Z"; o = self.orders[c["order_id"]]
            rm = {s["id"] for s in c["slices"]["remove"]}; o["slices"] = [s for s in o["slices"] if s["id"] not in rm] + c["slices"]["add"]
            o["total_amount"] = c["new_total_amount"]; return {"data": dict(c)}
        raise LookupError(404, f"no fixture for {method} {path}")
