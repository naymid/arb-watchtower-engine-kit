"""Paper brokerage: the same job flow as a real broker, with no money.

Fills at the live quote the server looked up before the job started. Positions
persist in data/paper.json. Messages use auto-rsa's own wording so the server
parses paper and real results with the same rules.
"""

import hashlib
import json
import time
from pathlib import Path


def _account_no(login_id: str) -> str:
    return "PA" + str(int(hashlib.sha1(login_id.encode()).hexdigest(), 16))[:8]


def _mask(acct: str) -> str:
    return "x" * (len(acct) - 4) + acct[-4:]


def run(spec: dict, emit, ask) -> None:
    paper = spec["paper"]
    path = Path(paper["state_path"])
    state = json.loads(path.read_text()) if path.exists() else {"logins": {}}
    quotes: dict[str, float] = {k.upper(): v for k, v in (paper.get("quotes") or {}).items() if v}

    def save() -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(path)

    for login in paper["logins"]:
        name = f"Paper {login['index']}"
        acct = _account_no(login["id"])
        rec = state["logins"].setdefault(
            login["id"],
            {"cash": float(login.get("cash") or 1000), "positions": {}, "verified": False, "account": acct},
        )
        print(f"Logging in to {name}...")
        time.sleep(0.35)
        if login.get("ask_code") and not rec.get("verified"):
            code = ask(f"{name}: enter the 6-digit code we texted you (any 6 digits work)")
            if code == "cancel" or not (code.isdigit() and len(code) == 6):
                emit("msg", text=f"{name}: Error logging in: code {'cancelled' if code == 'cancel' else 'rejected'}")
                continue
            rec["verified"] = True
            save()
        print(f"Logged in to {name}")

        if spec["kind"] == "holdings":
            positions = []
            for sym, p in sorted(rec["positions"].items()):
                if p["qty"] <= 0:
                    continue
                price = quotes.get(sym) or p.get("last") or 0
                positions.append({"symbol": sym, "qty": p["qty"], "price": round(price, 4), "total": round(p["qty"] * price, 2)})
            total = rec["cash"] + sum(x["total"] for x in positions)
            emit("holdings", parent=name, accounts=[{"account": "…" + acct[-4:], "total": round(total, 2), "type": "Paper", "positions": positions}])
            continue

        action, qty = spec["action"], float(spec["qty"])
        for sym in spec["symbols"]:
            sym = sym.upper()
            time.sleep(0.25)
            emit("msg", text=f"{name}: {action}ing {qty:g} of {sym}")
            price = quotes.get(sym)
            if not price:
                emit("msg", text=f"{name}: Error {action}ing {qty:g} of {sym} in {_mask(acct)}: no quote available")
                continue
            if spec["dry"]:
                emit("msg", text=f"{name} {_mask(acct)} Running in DRY mode. Transaction would've been: {action} {qty:g} of {sym} @ {price}")
                continue
            pos = rec["positions"].setdefault(sym, {"qty": 0.0, "cost": 0.0, "last": price})
            if action == "buy":
                cost = qty * price
                if cost > rec["cash"]:
                    emit("msg", text=f"{name}: Error buying {qty:g} of {sym} in {_mask(acct)}: insufficient cash (${rec['cash']:.2f})")
                    continue
                rec["cash"] -= cost
                pos["qty"] += qty
                pos["cost"] += cost
            else:
                if pos["qty"] < qty:
                    emit("msg", text=f"{name}: Error selling {qty:g} of {sym} in {_mask(acct)}: only {pos['qty']:g} held")
                    continue
                rec["cash"] += qty * price
                avg = pos["cost"] / pos["qty"] if pos["qty"] else 0
                pos["qty"] -= qty
                pos["cost"] -= avg * qty
            pos["last"] = price
            save()
            emit("msg", text=f"{name}: {action} {qty:g} of {sym} in {_mask(acct)} @ {price}: Success")
    save()
