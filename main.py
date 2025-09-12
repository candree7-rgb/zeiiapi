# main.py
import os, re, hmac, json, time, asyncio, hashlib, math
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple
import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
BYBIT_BASE       = os.getenv("BYBIT_BASE", "https://api-testnet.bybit.com")
BYBIT_KEY        = os.getenv("BYBIT_KEY", "")
BYBIT_SECRET     = os.getenv("BYBIT_SECRET", "")
SETTLE_COIN      = os.getenv("SETTLE_COIN", "USDT")

ALLOWED_TFS      = set(os.getenv("ALLOWED_TFS", "H1").replace(" ", "").split(","))

TP1_POS_PCT      = float(os.getenv("TP1_POS_PCT", "20"))
TP2_POS_PCT      = float(os.getenv("TP2_POS_PCT", "80"))
MAX_LEV_CAP      = int(os.getenv("MAX_LEV_CAP", "75"))
SAFETY_PCT       = float(os.getenv("SAFETY_PCT", "80"))

COOLDOWN_MIN     = int(os.getenv("COOLDOWN_MIN", "45"))
ENTRY_EXP_MIN    = int(os.getenv("ENTRY_EXP_MIN", "60"))
DD_LIMIT_PCT     = float(os.getenv("DD_LIMIT_PCT", "2.8"))

MAX_OPEN_LONGS   = int(os.getenv("MAX_OPEN_LONGS", "999"))
MAX_OPEN_SHORTS  = int(os.getenv("MAX_OPEN_SHORTS", "999"))

TEXT_PATH        = os.getenv("TEXT_PATH", "content")
DEFAULT_NOTION   = float(os.getenv("DEFAULT_NOTIONAL", "50"))  # Margin pro Trade (ohne Hebel)

# ========= App / State =========
app = FastAPI()
_httpx_client: Optional[httpx.AsyncClient] = None

STATE: Dict[str, Any] = {
    "last_trade_ts": 0.0,
    "trading_paused_until": None,
    "day_start_equity": None,
    "day_realized_pnl": 0.0,
    "open_watch": {}   # symbol -> meta
}

SYMBOL_META: Dict[str, Dict[str, float]] = {}  # tickSize/stepSize/minQty Cache

def now_ts() -> float: return time.time()

# ========= FastAPI Lifecycle =========
@app.on_event("startup")
async def _startup():
    global _httpx_client
    _httpx_client = httpx.AsyncClient(timeout=15.0)
    asyncio.create_task(monitor_loop())
    print("✅ Server started, monitor loop running...")

@app.on_event("shutdown")
async def _shutdown():
    global _httpx_client
    if _httpx_client:
        await _httpx_client.aclose()
        _httpx_client = None

# ========= Bybit Client (SignType=2) =========
def _qs(params: Dict[str, Any]) -> str:
    items = [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)

async def bybit(path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not BYBIT_KEY or not BYBIT_SECRET:
        raise HTTPException(500, "BYBIT_KEY/SECRET not set")
    ts = str(int(now_ts()*1000))
    recv = "5000"
    headers = {
        "X-BAPI-API-KEY": BYBIT_KEY,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "Content-Type": "application/json",
    }
    method = method.upper()
    if method == "GET":
        q = _qs(params)
        prehash = ts + BYBIT_KEY + recv + q
        sig = hmac.new(BYBIT_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        headers["X-BAPI-SIGN"] = sig
        url = f"{BYBIT_BASE}{path}" + (f"?{q}" if q else "")
        r = await _httpx_client.get(url, headers=headers)
    else:
        body = json.dumps(params, separators=(',', ':'), sort_keys=True)
        prehash = ts + BYBIT_KEY + recv + body
        sig = hmac.new(BYBIT_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        headers["X-BAPI-SIGN"] = sig
        url = f"{BYBIT_BASE}{path}"
        r = await _httpx_client.post(url, headers=headers, content=body.encode())

    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, f"Bybit non-JSON response ({r.status_code}): {r.text[:200]}")

    # Debug
    if data.get("retCode") not in (0, 110043):
        print(f"❌ Bybit {method} {path} -> {data.get('retCode')} {data.get('retMsg')} :: {data}")

    # tolerate leverage-not-modified
    if data.get("retCode") in (0, 110043):
        return data.get("result", {}) or {}

    raise HTTPException(502, f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")

# ========= Symbol Filters (tick/step/min) =========
def _quantize(v: float, step: float, mode: str = "floor") -> float:
    if step <= 0: return v
    n = v / step
    if mode == "ceil":
        n = math.ceil(n - 1e-12)
    elif mode == "round":
        n = round(n)
    else:
        n = math.floor(n + 1e-12)
    return float(n * step)

async def get_symbol_meta(symbol: str) -> Dict[str, float]:
    """
    Fetch and cache tickSize (price), stepSize (qty), minOrderQty for linear perps.
    """
    if symbol in SYMBOL_META:
        return SYMBOL_META[symbol]
    res = await bybit("/v5/market/instruments-info", "GET", {
        "category": "linear",
        "symbol": symbol
    })
    lst = res.get("list", [])
    if not lst:
        raise HTTPException(400, f"Symbol not found/inactive: {symbol}")
    item = lst[0]
    pf  = item.get("priceFilter", {}) or {}
    lf  = item.get("lotSizeFilter", {}) or {}
    meta = {
        "tickSize": float(pf.get("tickSize", "0.01")),
        "stepSize": float(lf.get("qtyStep", lf.get("stepSize", "0.001"))),
        "minQty":   float(lf.get("minOrderQty", "0.001")),
        # optional notional filter if present (rare on perps):
        "minNotional": float(lf.get("minOrderAmt", "0")) if lf.get("minOrderAmt") else 0.0
    }
    SYMBOL_META[symbol] = meta
    print(f"ℹ️ {symbol} filters: {meta}")
    return meta

def quant_price(price: float, tick: float) -> float:
    return _quantize(price, tick, "round")

def quant_qty(qty: float, step: float) -> float:
    return _quantize(qty, step, "floor")

# ========= Wallet / Positions =========
async def positions(symbol: Optional[str] = None):
    params = {"category":"linear", "settleCoin": SETTLE_COIN}
    if symbol:
        params["symbol"] = symbol
    res = await bybit("/v5/position/list", "GET", params)
    return res.get("list", [])

async def positions_size_symbol(symbol: str) -> float:
    pos = await positions(symbol)
    if not pos: return 0.0
    return float(pos[0].get("size") or 0)

async def count_open_filled() -> Dict[str,int]:
    res = await positions()
    longs = shorts = 0
    for p in res:
        sz = float(p.get("size") or 0)
        if sz <= 0: continue
        side = (p.get("side") or "").lower()
        if side == "buy": longs += 1
        elif side == "sell": shorts += 1
    return {"longs": longs, "shorts": shorts}

async def get_order_status(symbol: str, link_id: str) -> Optional[str]:
    try:
        res = await bybit("/v5/order/realtime", "GET", {
            "category": "linear",
            "symbol": symbol,
            "orderLinkId": link_id
        })
        lst = res.get("list", [])
        if lst: return lst[0].get("orderStatus")
    except: pass
    return None

# ========= Parser =========
def parse_signals(text: str):
    txt = text.replace("\r","")
    blocks = re.split(r"\n\s*\n", txt.strip())
    signals = []
    for b in blocks:
        m_tf = re.search(r"Timeframe:\s*([A-Za-z0-9]+)", b, re.I)
        if not m_tf: continue
        tf = m_tf.group(1).upper()
        if tf not in ALLOWED_TFS: continue
        m_side = re.search(r"\b(BUY|SELL)\b", b, re.I)
        m_pair = re.search(r"on\s+([A-Z0-9]+)[/\-]([A-Z0-9]+)", b, re.I)
        m_entry= re.search(r"Price:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_tp1  = re.search(r"TP\s*1:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_tp2  = re.search(r"TP\s*2:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_sl   = re.search(r"\bSL\s*:\s*([0-9]*\.?[0-9]+)", b, re.I)
        if not (m_side and m_pair and m_entry and m_tp1 and m_tp2 and m_sl): continue
        side = "long" if m_side.group(1).upper()=="BUY" else "short"
        base, quote = m_pair.group(1).upper(), m_pair.group(2).upper()
        if quote=="USD": quote="USDT"
        entry,tp1,tp2,sl = map(float,[m_entry.group(1),m_tp1.group(1),m_tp2.group(1),m_sl.group(1)])
        # grobe Plausibilität
        if side=="long"  and not (sl < entry < tp1 <= tp2): continue
        if side=="short" and not (sl > entry > tp1 >= tp2): continue
        signals.append({"base":base,"quote":quote,"side":side,"entry":entry,"tp1":tp1,"tp2":tp2,"sl":sl,"tf":tf})
    return signals

# ========= Orders =========
def leverage_from_sl(entry: float, sl: float, side: str) -> int:
    sl_pct = abs((entry-sl)/entry*100.0)
    lev = int(SAFETY_PCT // max(0.0001, sl_pct))
    return max(1, min(lev, MAX_LEV_CAP))

async def set_leverage(symbol: str, lev: int):
    await bybit("/v5/position/set-leverage","POST",{
        "category":"linear",
        "symbol":symbol,
        "buyLeverage": str(lev),
        "sellLeverage": str(lev)
    })

async def place_entry(symbol: str, side: str, entry: float, notional_usdt: float, leverage: int) -> Tuple[str, float]:
    # Fetch filters
    meta = await get_symbol_meta(symbol)
    tick = meta["tickSize"]; step = meta["stepSize"]; minQty = meta["minQty"]

    entry_q = quant_price(entry, tick)

    # Qty based on (margin * leverage) / price
    raw_qty = (notional_usdt * leverage) / entry_q
    qty = quant_qty(max(raw_qty, minQty), step)
    if qty < minQty:
        raise HTTPException(400, f"Qty too small after rounding for {symbol}. Increase notional or leverage.")

    BY = "Buy" if side=="long" else "Sell"
    uid = hex(int(now_ts()*1000))[2:]
    link_entry = f"ent_{symbol}_{uid}"

    await bybit("/v5/order/create","POST",{
        "category":"linear",
        "symbol":symbol,
        "side":BY,
        "orderType":"Limit",
        "price":str(entry_q),
        "qty":str(qty),
        "timeInForce":"GTC",
        "reduceOnly":False,
        "closeOnTrigger":False,
        "orderLinkId":link_entry
    })
    print(f"📍 Entry placed {symbol} {BY} {qty} @ {entry_q} (tick {tick}, step {step}, minQty {minQty})")
    return link_entry, qty

async def place_tp_sl(symbol: str, side: str, size: float, tp1: float, tp2: float, sl: float):
    meta = await get_symbol_meta(symbol)
    tick = meta["tickSize"]; step = meta["stepSize"]; minQty = meta["minQty"]

    tp1_q = quant_price(tp1, tick)
    tp2_q = quant_price(tp2, tick)
    sl_q  = quant_price(sl,  tick)

    # Split sizes
    q1_raw = size * (TP1_POS_PCT/100.0)
    q2_raw = size * (TP2_POS_PCT/100.0)

    q1 = quant_qty(max(q1_raw, 0.0), step)
    q2 = quant_qty(max(q2_raw, 0.0), step)

    # Ensure both TP legs meet minQty, adjust if needed
    if q1 < minQty and q2 >= (minQty*2):
        q1 = minQty
        q2 = quant_qty(max(size - q1, 0.0), step)
    if q2 < minQty and q1 >= (minQty*2):
        q2 = minQty
        q1 = quant_qty(max(size - q2, 0.0), step)

    # Final sanity: total cannot exceed size
    if q1 + q2 > size:
        q2 = quant_qty(max(size - q1, 0.0), step)

    # If still too small overall, collapse to a single TP
    just_tp1 = False
    if q1 < minQty and q2 < minQty:
        q1 = quant_qty(size, step)
        q2 = 0.0
        just_tp1 = True

    OP = "Sell" if side=="long" else "Buy"
    uid = hex(int(now_ts()*1000))[2:]
    link_tp1 = f"tp1_{symbol}_{uid}"
    link_tp2 = f"tp2_{symbol}_{uid}"
    link_sl  = f"sl_{symbol}_{uid}"

    # TP1
    if q1 >= minQty:
        await bybit("/v5/order/create","POST",{
            "category":"linear","symbol":symbol,"side":OP,
            "orderType":"Limit","price":str(tp1_q),"qty":str(q1),
            "timeInForce":"GTC","reduceOnly":True,"closeOnTrigger":False,
            "orderLinkId":link_tp1
        })
        print(f"✅ TP1 {symbol} {OP} {q1} @ {tp1_q}")

    # TP2 (if any)
    if not just_tp1 and q2 >= minQty:
        await bybit("/v5/order/create","POST",{
            "category":"linear","symbol":symbol,"side":OP,
            "orderType":"Limit","price":str(tp2_q),"qty":str(q2),
            "timeInForce":"GTC","reduceOnly":True,"closeOnTrigger":False,
            "orderLinkId":link_tp2
        })
        print(f"✅ TP2 {symbol} {OP} {q2} @ {tp2_q}")
    else:
        link_tp2 = None
        print(f"ℹ️ TP2 skipped (q2<{minQty})")

    # SL – conditional market (full remaining size)
    # Use current size for safety; Bybit enforces step too
    size_q = quant_qty(size, step)
    trigDir = 2 if side=="long" else 1
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Market","qty":str(size_q),
        "triggerPrice":str(sl_q),"triggerDirection":trigDir,"triggerBy":"LastPrice",
        "timeInForce":"IOC","reduceOnly":True,"closeOnTrigger":True,
        "orderLinkId":link_sl
    })
    print(f"✅ SL {symbol} {OP} {size_q} trigger @ {sl_q}")

    return link_tp1, link_tp2, link_sl

async def cancel_order(symbol: str, order_link_id: str):
    try:
        await bybit("/v5/order/cancel","POST",{
            "category":"linear","symbol":symbol,"orderLinkId":order_link_id
        })
        print(f"✅ Cancelled order: {order_link_id}")
    except Exception as e:
        print(f"⚠️ Cancel failed {order_link_id}: {e}")

async def move_sl_to_be(symbol: str, side: str, entry_price: float, remaining_size: float):
    meta = await get_symbol_meta(symbol)
    tick = meta["tickSize"]; step = meta["stepSize"]
    entry_q = quant_price(entry_price, tick)
    size_q  = quant_qty(remaining_size, step)
    OP = "Sell" if side=="long" else "Buy"
    trigDir = 2 if side=="long" else 1
    uid = hex(int(now_ts()*1000))[2:]
    link_sl_be = f"sl_be_{symbol}_{uid}"
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Market","qty":str(size_q),
        "triggerPrice":str(entry_q),"triggerDirection":trigDir,"triggerBy":"LastPrice",
        "timeInForce":"IOC","reduceOnly":True,"closeOnTrigger":True,
        "orderLinkId":link_sl_be
    })
    print(f"✅ SL→BE {symbol} qty {size_q} @ {entry_q}")
    return link_sl_be

# ========= Monitor Loop =========
async def monitor_loop():
    print("🔄 Monitor loop started...")
    while True:
        await asyncio.sleep(5)
        to_del = []
        for symbol, meta in list(STATE["open_watch"].items()):
            try:
                # Phase 1: warten bis Position (teil)gefüllt -> Exits anlegen
                if not meta.get("exits_set"):
                    # Ablaufzeit?
                    if now_ts() - meta.get("created_ts", now_ts()) > meta.get("expiry_min", ENTRY_EXP_MIN)*60:
                        st = await get_order_status(symbol, meta["entry_id"])
                        if st in ("New","PartiallyFilled"):
                            await cancel_order(symbol, meta["entry_id"])
                            to_del.append(symbol)
                            continue
                    size = await positions_size_symbol(symbol)
                    if size > 0:
                        tp1_id, tp2_id, sl_id = await place_tp_sl(
                            symbol, meta["side"], size,
                            meta["tp1_px"], meta["tp2_px"], meta["sl_px"]
                        )
                        meta.update({
                            "tp1_id": tp1_id, "tp2_id": tp2_id, "sl_id": sl_id,
                            "exits_set": True, "tp1_hit": False, "sl_moved_to_be": False
                        })
                        STATE["last_trade_ts"] = now_ts()  # Cooldown erst jetzt
                        continue

                # Phase 2: nach TP1 -> SL zu BE
                if meta.get("exits_set") and not meta.get("tp1_hit") and meta.get("tp1_id"):
                    st_tp1 = await get_order_status(symbol, meta["tp1_id"])
                    if st_tp1 == "Filled":
                        meta["tp1_hit"] = True
                        rem = await positions_size_symbol(symbol)
                        if rem > 0 and not meta.get("sl_moved_to_be") and meta.get("sl_id"):
                            await cancel_order(symbol, meta["sl_id"])
                            new_sl = await move_sl_to_be(symbol, meta["side"], meta["entry_px"], rem)
                            meta["sl_id"] = new_sl
                            meta["sl_moved_to_be"] = True

                # Phase 3: Cleanup wenn Pos zu
                cur = await positions_size_symbol(symbol)
                if cur == 0.0 and meta.get("exits_set"):
                    to_del.append(symbol)
                    print(f"🏁 Position closed {symbol}")

            except Exception as e:
                print(f"❌ Monitor error {symbol}: {e}")

        for s in to_del:
            STATE["open_watch"].pop(s, None)

# ========= Guards / Helpers =========
def in_cooldown() -> bool:
    return (now_ts() - STATE.get("last_trade_ts",0.0)) < COOLDOWN_MIN*60

def extract_text_from_payload(payload: dict) -> str:
    node = payload
    for part in TEXT_PATH.split("."):
        if isinstance(node, dict) and part in node: node = node[part]
        else: return ""
    return node if isinstance(node, str) else ""

# ========= HTTP Endpoints =========
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    text = (body.get("text") or extract_text_from_payload(body)).strip()
    if not text:
        raise HTTPException(400, "No signal text")

    sigs = parse_signals(text)
    if not sigs:
        raise HTTPException(422, f"No valid signal for TFs {sorted(ALLOWED_TFS)}")

    # nur erstes valides Setup
    sig = sigs[0]
    base, quote, side = sig["base"], sig["quote"], sig["side"]
    entry, tp1, tp2, sl = sig["entry"], sig["tp1"], sig["tp2"], sig["sl"]
    symbol = f"{base}{quote}"

    print(f"📨 Signal: {symbol} {side} Entry:{entry} TP1:{tp1} TP2:{tp2} SL:{sl}")

    # Limits offene Positionen (nur gefüllte)
    counts = await count_open_filled()
    if side == "long" and counts["longs"] >= MAX_OPEN_LONGS:
        raise HTTPException(429, f"Max open longs reached ({counts['longs']}/{MAX_OPEN_LONGS})")
    if side == "short" and counts["shorts"] >= MAX_OPEN_SHORTS:
        raise HTTPException(429, f"Max open shorts reached ({counts['shorts']}/{MAX_OPEN_SHORTS})")

    # Cooldown nur nach Fill blocken
    if in_cooldown():
        raise HTTPException(429, f"In cooldown ({COOLDOWN_MIN} min since last fill)")

    # Hebel
    lev = leverage_from_sl(entry, sl, side)
    await set_leverage(symbol, lev)

    # Entry platzieren (quantized, inkl. minQty)
    notional = float(body.get("notional") or DEFAULT_NOTION)
    entry_id, qty = await place_entry(symbol, side, entry, notional, lev)

    # Watch für Exit-Management
    STATE["open_watch"][symbol] = {
        "entry_id": entry_id, "side": side,
        "entry_px": entry, "tp1_px": tp1, "tp2_px": tp2, "sl_px": sl,
        "created_ts": now_ts(), "expiry_min": ENTRY_EXP_MIN,
        "exits_set": False, "tp1_hit": False, "sl_moved_to_be": False
    }

    return {
        "ok": True,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "leverage": lev,
        "notional": notional,
        "entry_qty": qty,
        "entry_order_id": entry_id,
        "open_counts": counts
    }

@app.get("/health")
async def health():
    return {"ok": True, "paused": STATE.get("trading_paused_until") is not None,
            "watch": list(STATE["open_watch"].keys())}

# Run: uvicorn main:app --host 0.0.0.0 --port $PORT
