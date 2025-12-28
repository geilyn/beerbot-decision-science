from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from typing import Dict, Any, List

app = FastAPI()

STUDENT_EMAIL = "geilyn@taltech.ee"
ALGO_NAME = "BullwhipBreaker"
VERSION = "v1.0.2"  # tuned to reduce inventory cost

ROLES = ["retailer", "wholesaler", "distributor", "factory"]

# Rollipõhised parameetrid 
PARAMS = {
    "retailer": {
        "cover_weeks": 2.8,   # ↑ agressiivsem
        "safety": 18,         # ↑ backlogi vältimine
        "beta": 0.45,         # ↑ kiirem nõudluse õppimine
        "alpha": 0.65,        # ↑ vähem silumist
        "cap": 260
    },
    "wholesaler": {
        "cover_weeks": 3.2,
        "safety": 20,
        "beta": 0.45,
        "alpha": 0.60,
        "cap": 280
    },
    "distributor": {
        "cover_weeks": 3.5,
        "safety": 22,
        "beta": 0.45,
        "alpha": 0.60,
        "cap": 300
    },
    "factory": {
        "cover_weeks": 4.0,
        "safety": 26,
        "beta": 0.40,
        "alpha": 0.55,
        "cap": 340
    }
}


def ewma(values: List[int], beta: float) -> float:
    """Deterministlik EWMA. Kui ajalugu puudub, kasuta 0."""
    if not values:
        return 0.0
    s = float(values[0])
    for v in values[1:]:
        s = beta * float(v) + (1.0 - beta) * s
    return s


def last_order_for_role(weeks: List[Dict[str, Any]], role: str) -> int:
    """Võta eelmine (sama bot’i poolt) tellimus, kui olemas; muidu 0."""
    if not weeks:
        return 0
    last = weeks[-1]
    orders = last.get("orders") or {}
    val = orders.get(role)
    return int(val) if isinstance(val, int) and val >= 0 else 0


def incoming_history_blackbox(weeks: List[Dict[str, Any]], role: str) -> List[int]:
    hist: List[int] = []
    for w in weeks:
        r = (w.get("roles") or {}).get(role) or {}
        v = r.get("incoming_orders")
        if isinstance(v, int) and v >= 0:
            hist.append(v)
    return hist


def compute_order(
    role_state: Dict[str, Any],
    hist_incoming: List[int],
    last_order: int,
    p: Dict[str, Any],
) -> int:
    inv = int(role_state.get("inventory", 0) or 0)
    back = int(role_state.get("backlog", 0) or 0)
    inc = int(role_state.get("incoming_orders", 0) or 0)
    arr = int(role_state.get("arriving_shipments", 0) or 0)

    # Netopositsioon ja ühe nädala ettevaade
    net_stock = inv - back
    projected_next = net_stock + arr - inc

    # Prognoos (EWMA)
    forecast = ewma(hist_incoming, p["beta"])

    # Order-up-to sihttase (agressiivsem: madalam target_mult)
    base_target = p["safety"] + p["cover_weeks"] * forecast
    target = p.get("target_mult", 1.0) * base_target

    # Ära kata backlog'i 100% (selles skooris vähendab inventari ja kulu)
    effective_projected = projected_next - p.get("backlog_cover", 1.0) * back

    # Kui target > effective_projected, telli vahe
    raw_order = max(0.0, target - effective_projected)

    # Silu tellimust, et vähendada kõikumist
    smoothed = p["alpha"] * raw_order + (1.0 - p["alpha"]) * float(last_order)
    order = int(round(smoothed))

    # Ohutud piirangud
    order = max(0, order)
    order = min(order, int(p["cap"]))
    return order


@app.post("/api/decision")
async def decision(req: Request):
    body = await req.json()

    # --- Handshake ---
    if body.get("handshake") is True:
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "student_email": STUDENT_EMAIL,
                "algorithm_name": ALGO_NAME,
                "version": VERSION,
                "supports": {"blackbox": True, "glassbox": True},
                "message": "BeerBot ready",
                "uses_llm": False,
                "student_comment": "EWMA + order-up-to (lower target) + partial backlog cover + smoothing (deterministic)",
            },
        )

    mode = body.get("mode", "blackbox")
    weeks = body.get("weeks") or []
    if not weeks:
        # Kui midagi on väga valesti, tagasta turvaline default
        return JSONResponse(status_code=200, content={"orders": {r: 10 for r in ROLES}})

    last_week = weeks[-1]
    roles_state = last_week.get("roles") or {}

    orders_out: Dict[str, int] = {}

    if mode == "glassbox":
        # Koordineeritud prognoos: võta lõpptarbija nõudlus retailerilt
        retailer_hist = incoming_history_blackbox(weeks, "retailer")
        for role in ROLES:
            p = PARAMS[role]
            last_ord = last_order_for_role(weeks, role)
            state = roles_state.get(role) or {}
            orders_out[role] = compute_order(state, retailer_hist, last_ord, p)
    else:
        # Blackbox: iga roll ainult oma rolli info põhjal
        for role in ROLES:
            p = PARAMS[role]
            hist = incoming_history_blackbox(weeks, role)
            last_ord = last_order_for_role(weeks, role)
            state = roles_state.get(role) or {}
            orders_out[role] = compute_order(state, hist, last_ord, p)

    # Spec: mitte-negatiivsed int-id
    for r in ROLES:
        v = orders_out.get(r, 0)
        if not isinstance(v, int) or v < 0:
            orders_out[r] = 0

    return JSONResponse(status_code=200, content={"orders": orders_out})
