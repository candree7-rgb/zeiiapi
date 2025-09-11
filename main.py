# main.py
import os, re, hmac, json, time, asyncio, hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
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
    "open_watch": {}
}

TICK_DECIMALS = {
    "SHIB": 8, "DOGE": 5, "XRP": 4, "SOL": 2, "AVAX": 3, "AAVE": 2, "LINK": 3,
    "BTC": 2, "ETH": 2, "BNB": 2, "LTC": 2, "ADA": 5, "MATIC": 5, "EOS": 4, "BCH": 2, "ATOM": 3, "ALGO": 5
}

def now_ts() -> float: return time.time()
def round_tick(base: str, v: float) -> float:
    d = TICK_DECIMALS.get(base, 4)
    return round(v, d)

# ========= FastAPI Lifecycle =========
@app.on_event("startup")
async def _startup():
    global _httpx_client
    _httpx_client = httpx.AsyncClient(timeout=15.0)
    # Start monitor loop
    asyncio.create_task(monitor_loop())
    print("✅ Server started, monitor loop running...")

@app.on_event("shutdown")
async def _shutdown():
    global _httpx_client
    if _httpx_client:
        await _httpx_client.aclose()
        _httpx_client = None

# ========= Bybit Client =========
def _qs(params: Dict[str, Any]) -> str:
    items = [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)

async def bybit(path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not BYBIT_KEY or not BYBIT_SECRET:
        raise HTTPException(500, "BYBIT_KEY/SECRET not set")

    ts = str(int(now_ts() * 1000))
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

    data = r.json()
    
    # Debug logging
    print(f"📡 Bybit {method} {path}: {data.get('retCode')} - {data.get('retMsg')}")
    
    if data.get("retCode") == 110043:  # leverage not modified
        return {}
    if r.status_code != 200 or data.get("retCode") != 0:
        print(f"❌ Bybit error response: {data}")
        raise HTTPException(502, f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")
    return data.get("result", {}) or {}

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

async def get_order_status(symbol: str, order_link_id: str) -> Optional[str]:
    """Get order status by orderLinkId"""
    try:
        res = await bybit("/v5/order/realtime", "GET", {
            "category": "linear",
            "symbol": symbol,
            "orderLinkId": order_link_id
        })
        order_list = res.get("list", [])
        if order_list:
            return order_list[0].get("orderStatus")
    except:
        pass
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
        m_entry= re.search(r"Price:\s*([0-9.]+)", b, re.I)
        m_tp1  = re.search(r"TP\s*1:\s*([0-9.]+)", b, re.I)
        m_tp2  = re.search(r"TP\s*2:\s*([0-9.]+)", b, re.I)
        m_sl   = re.search(r"\bSL\s*:\s*([0-9.]+)", b, re.I)
        if not (m_side and m_pair and m_entry and m_tp1 and m_tp2 and m_sl): continue

        side = "long" if m_side.group(1).upper()=="BUY" else "short"
        base, quote = m_pair.group(1).upper(), m_pair.group(2).upper()
        if quote=="USD": quote="USDT"
        entry,tp1,tp2,sl = map(float,[m_entry.group(1),m_tp1.group(1),m_tp2.group(1),m_sl.group(1)])
        signals.append({"base":base,"quote":quote,"side":side,"entry":entry,"tp1":tp1,"tp2":tp2,"sl":sl,"tf":tf})
    return signals

# ========= Orders =========
def leverage_from_sl(entry: float, sl: float, side: str) -> int:
    sl_pct = abs((entry-sl)/entry*100.0)
    lev = int(SAFETY_PCT // max(0.0001, sl_pct))
    return max(1, min(lev, MAX_LEV_CAP))

async def set_leverage(symbol: str, lev: int):
    try:
        await bybit("/v5/position/set-leverage","POST",{
            "category":"linear",
            "symbol":symbol,
            "buyLeverage": str(lev),
            "sellLeverage": str(lev)
        })
        print(f"✅ Leverage set to {lev}x for {symbol}")
    except:
        pass  # Leverage might already be set

async def place_entry(symbol: str, side: str, entry: float, notional_usdt: float, leverage: int):
    # Calculate quantity based on notional * leverage
    qty = max(0.001, round((notional_usdt * leverage) / entry, 6))
    BY = "Buy" if side=="long" else "Sell"
    uid = hex(int(now_ts()*1000))[2:]
    link_entry = f"ent_{symbol}_{uid}"
    
    res = await bybit("/v5/order/create","POST",{
        "category":"linear",
        "symbol":symbol,
        "side":BY,
        "orderType":"Limit",
        "price":str(entry),
        "qty":str(qty),
        "timeInForce":"GTC",
        "reduceOnly":False,
        "closeOnTrigger":False,
        "orderLinkId":link_entry
    })
    
    print(f"📍 Entry order placed: {symbol} {BY} {qty} @ {entry}")
    return link_entry, qty

async def place_tp_sl(symbol: str, side: str, size: float, tp1: float, tp2: float, sl: float):
    """Place TP1, TP2 and SL orders with correct Bybit v5 syntax"""
    OP = "Sell" if side=="long" else "Buy"
    uid = hex(int(now_ts()*1000))[2:]
    
    # Calculate quantities
    q1 = max(0.001, round(size * (TP1_POS_PCT/100.0), 6))
    q2 = max(0.001, round(size * (TP2_POS_PCT/100.0), 6))
    
    link_tp1 = f"tp1_{symbol}_{uid}"
    link_tp2 = f"tp2_{symbol}_{uid}"
    link_sl = f"sl_{symbol}_{uid}"
    
    errors = []
    
    # TP1 - Regular limit order
    try:
        await bybit("/v5/order/create","POST",{
            "category":"linear",
            "symbol":symbol,
            "side":OP,
            "orderType":"Limit",
            "price":str(tp1),
            "qty":str(q1),
            "timeInForce":"GTC",
            "reduceOnly":True,
            "closeOnTrigger":False,
            "orderLinkId":link_tp1
        })
        print(f"✅ TP1 placed: {symbol} {OP} {q1} @ {tp1}")
    except Exception as e:
        errors.append(f"TP1: {e}")
        print(f"❌ TP1 failed: {e}")
    
    # TP2 - Regular limit order
    try:
        await bybit("/v5/order/create","POST",{
            "category":"linear",
            "symbol":symbol,
            "side":OP,
            "orderType":"Limit",
            "price":str(tp2),
            "qty":str(q2),
            "timeInForce":"GTC",
            "reduceOnly":True,
            "closeOnTrigger":False,
            "orderLinkId":link_tp2
        })
        print(f"✅ TP2 placed: {symbol} {OP} {q2} @ {tp2}")
    except Exception as e:
        errors.append(f"TP2: {e}")
        print(f"❌ TP2 failed: {e}")
    
    # SL - Conditional market order
    try:
        # For stop loss: trigger direction depends on side
        # Long position (sell to close): trigger when price falls below SL (direction=2)
        # Short position (buy to close): trigger when price rises above SL (direction=1)
        trigDir = 2 if side=="long" else 1
        
        await bybit("/v5/order/create","POST",{
            "category":"linear",
            "symbol":symbol,
            "side":OP,
            "orderType":"Market",
            "qty":str(size),  # Full position for SL
            "triggerPrice":str(sl),
            "triggerDirection":trigDir,
            "triggerBy":"LastPrice",
            "timeInForce":"IOC",
            "reduceOnly":True,
            "closeOnTrigger":True,
            "orderLinkId":link_sl
        })
        print(f"✅ SL placed: {symbol} {OP} {size} trigger @ {sl}")
    except Exception as e:
        errors.append(f"SL: {e}")
        print(f"❌ SL failed: {e}")
    
    if errors:
        print(f"⚠️ Some orders failed: {errors}")
    
    return link_tp1, link_tp2, link_sl

async def cancel_order(symbol: str, order_link_id: str):
    """Cancel an order by orderLinkId"""
    try:
        await bybit("/v5/order/cancel","POST",{
            "category":"linear",
            "symbol":symbol,
            "orderLinkId":order_link_id
        })
        print(f"✅ Cancelled order: {order_link_id}")
    except Exception as e:
        print(f"⚠️ Could not cancel {order_link_id}: {e}")

async def move_sl_to_be(symbol: str, side: str, entry_price: float, remaining_size: float):
    """Move stop loss to break even"""
    OP = "Sell" if side=="long" else "Buy"
    trigDir = 2 if side=="long" else 1
    uid = hex(int(now_ts()*1000))[2:]
    link_sl_be = f"sl_be_{symbol}_{uid}"
    
    await bybit("/v5/order/create","POST",{
        "category":"linear",
        "symbol":symbol,
        "side":OP,
        "orderType":"Market",
        "qty":str(remaining_size),
        "triggerPrice":str(entry_price),
        "triggerDirection":trigDir,
        "triggerBy":"LastPrice",
        "timeInForce":"IOC",
        "reduceOnly":True,
        "closeOnTrigger":True,
        "orderLinkId":link_sl_be
    })
    
    print(f"✅ SL moved to BE @ {entry_price} for {symbol}")
    return link_sl_be

# ========= Monitor Loop =========
async def monitor_loop():
    """Monitor positions and manage SL to BE after TP1"""
    print("🔄 Monitor loop started...")
    while True:
        await asyncio.sleep(5)
        to_del = []
        
        for symbol, meta in list(STATE["open_watch"].items()):
            try:
                # Phase 1: Wait for position to open and set exits
                if not meta.get("exits_set"):
                    size = await positions_size_symbol(symbol)
                    if size > 0:
                        print(f"📊 Position opened for {symbol}, size: {size}")
                        # Position opened, place exit orders
                        tp1_id, tp2_id, sl_id = await place_tp_sl(
                            symbol, meta["side"], size,
                            meta["tp1_px"], meta["tp2_px"], meta["sl_px"]
                        )
                        meta.update({
                            "tp1_id": tp1_id,
                            "tp2_id": tp2_id,
                            "sl_id": sl_id,
                            "exits_set": True,
                            "tp1_hit": False,
                            "sl_moved_to_be": False,
                            "position_size": size
                        })
                        STATE["last_trade_ts"] = now_ts()
                
                # Phase 2: Monitor TP1 and move SL to BE
                elif meta.get("exits_set") and not meta.get("tp1_hit"):
                    # Check if TP1 was filled
                    tp1_status = await get_order_status(symbol, meta["tp1_id"])
                    
                    if tp1_status == "Filled":
                        meta["tp1_hit"] = True
                        print(f"🎯 TP1 hit for {symbol}, moving SL to BE...")
                        
                        # Get remaining position size
                        remaining_size = await positions_size_symbol(symbol)
                        
                        if remaining_size > 0 and not meta.get("sl_moved_to_be"):
                            # Cancel old SL
                            await cancel_order(symbol, meta["sl_id"])
                            
                            # Place new SL at break even
                            new_sl_id = await move_sl_to_be(
                                symbol, meta["side"], 
                                meta["entry_px"], remaining_size
                            )
                            
                            meta["sl_id"] = new_sl_id
                            meta["sl_moved_to_be"] = True
                
                # Phase 3: Check if position is closed
                current_size = await positions_size_symbol(symbol)
                if current_size == 0.0:
                    to_del.append(symbol)
                    print(f"🏁 Position closed for {symbol}")
                    
            except Exception as e:
                print(f"❌ Error monitoring {symbol}: {e}")
        
        # Clean up closed positions
        for s in to_del:
            STATE["open_watch"].pop(s, None)

# ========= Guards =========
def in_cooldown() -> bool:
    return (now_ts() - STATE.get("last_trade_ts",0.0)) < COOLDOWN_MIN*60

# ========= HTTP Endpoints =========
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    text = (body.get("text") or extract_text_from_payload(body)).strip()
    if not text: 
        raise HTTPException(400, "No signal text")
    
    sigs = parse_signals(text)
    if not sigs: 
        raise HTTPException(422, "No valid signal")
    
    sig = sigs[0]
    base = sig["base"]
    quote = sig["quote"]
    side = sig["side"]
    entry = sig["entry"]
    tp1 = sig["tp1"]
    tp2 = sig["tp2"]
    sl = sig["sl"]
    symbol = f"{base}{quote}"
    
    print(f"📨 Received signal: {symbol} {side} Entry:{entry} TP1:{tp1} TP2:{tp2} SL:{sl}")
    
    # Check cooldown
    if in_cooldown(): 
        raise HTTPException(429, "Cooldown active")
    
    # Check max open positions
    counts = await count_open_filled()
    if side == "long" and counts["longs"] >= MAX_OPEN_LONGS:
        raise HTTPException(429, f"Max open longs ({MAX_OPEN_LONGS}) reached")
    if side == "short" and counts["shorts"] >= MAX_OPEN_SHORTS:
        raise HTTPException(429, f"Max open shorts ({MAX_OPEN_SHORTS}) reached")
    
    # Calculate leverage and set it
    lev = leverage_from_sl(entry, sl, side)
    await set_leverage(symbol, lev)
    
    # Place entry order
    notional = float(body.get("notional") or DEFAULT_NOTION)
    entry_id, qty = await place_entry(symbol, side, entry, notional, lev)
    
    # Track position for monitoring
    STATE["open_watch"][symbol] = {
        "entry_id": entry_id,
        "side": side,
        "entry_px": entry,  # Store entry price for BE
        "tp1_px": tp1,
        "tp2_px": tp2,
        "sl_px": sl,
        "exits_set": False
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
        "quantity": qty,
        "entry_order_id": entry_id
    }

def extract_text_from_payload(payload: dict) -> str:
    node = payload
    for part in TEXT_PATH.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return ""
    return node if isinstance(node, str) else ""

@app.get("/health")
async def health():
    return {
        "ok": True,
        "watching": list(STATE["open_watch"].keys()),
        "count": len(STATE["open_watch"])
    }

@app.get("/status")
async def status():
    """Get detailed status of all watched positions"""
    positions_info = []
    for symbol, meta in STATE["open_watch"].items():
        size = await positions_size_symbol(symbol)
        positions_info.append({
            "symbol": symbol,
            "side": meta["side"],
            "current_size": size,
            "exits_set": meta.get("exits_set", False),
            "tp1_hit": meta.get("tp1_hit", False),
            "sl_moved_to_be": meta.get("sl_moved_to_be", False)
        })
    
    return {
        "ok": True,
        "in_cooldown": in_cooldown(),
        "positions": positions_info
    }

@app.get("/test-orders/{symbol}")
async def test_orders(symbol: str, side: str = "long"):
    """Test endpoint to manually test order placement"""
    # Example test values
    entry = 100.0
    tp1 = 105.0
    tp2 = 110.0
    sl = 95.0
    size = 0.01
    
    try:
        tp1_id, tp2_id, sl_id = await place_tp_sl(symbol, side, size, tp1, tp2, sl)
        return {
            "ok": True,
            "tp1_id": tp1_id,
            "tp2_id": tp2_id,
            "sl_id": sl_id
        }
    except Exception as e:
        return {"error": str(e)}

# Run with: uvicorn main:app --reload --host 0.0.0.0 --port 8000
