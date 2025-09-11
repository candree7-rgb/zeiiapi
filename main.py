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

# Timeframe-Filter
ALLOWED_TFS      = set(os.getenv("ALLOWED_TFS", "H1").replace(" ", "").split(","))

# Positionslogik
TP1_POS_PCT      = float(os.getenv("TP1_POS_PCT", "20"))
TP2_POS_PCT      = float(os.getenv("TP2_POS_PCT", "80"))
MAX_LEV_CAP      = int(os.getenv("MAX_LEV_CAP", "75"))
SAFETY_PCT       = float(os.getenv("SAFETY_PCT", "80"))

# Guards
COOLDOWN_MIN     = int(os.getenv("COOLDOWN_MIN", "45"))
ENTRY_EXP_MIN    = int(os.getenv("ENTRY_EXP_MIN", "60"))
DD_LIMIT_PCT     = float(os.getenv("DD_LIMIT_PCT", "2.8"))

# Positions-Limits
MAX_OPEN_LONGS   = int(os.getenv("MAX_OPEN_LONGS", "999"))
MAX_OPEN_SHORTS  = int(os.getenv("MAX_OPEN_SHORTS", "999"))

# Eingangs-Payload
TEXT_PATH        = os.getenv("TEXT_PATH", "content")
DEFAULT_NOTION   = float(os.getenv("DEFAULT_NOTIONAL", "50"))

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

# ========= Tools =========
def now_ts() -> float: return time.time()

def round_tick(base: str, v: float) -> float:
    return round(v, 6)

# ========= FastAPI Lifecycle =========
@app.on_event("startup")
async def _startup():
    global _httpx_client
    _httpx_client = httpx.AsyncClient(timeout=15.0)

@app.on_event("shutdown")
async def _shutdown():
    global _httpx_client
    if _httpx_client:
        await _httpx_client.aclose()
        _httpx_client = None

# ========= Bybit v5 Client =========
def _qs(params: Dict[str, Any]) -> str:
    items = [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)

async def bybit(path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not BYBIT_KEY or not BYBIT_SECRET:
        raise HTTPException(500, "BYBIT_KEY/SECRET not set")
    if not _httpx_client:
        raise HTTPException(500, "HTTP client not ready")

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
        r = await _httpx_client.post(url, headers=headers, content=body.encode("utf-8"))

    try: data = r.json()
    except Exception: raise HTTPException(502, f"Bybit non-JSON {r.status_code}: {r.text[:200]}")

    if data.get("retCode") == 110043:
        return {}
    if r.status_code != 200 or data.get("retCode") != 0:
        raise HTTPException(502, f"Bybit error {data.get('retCode')}: {data.get('retMsg')}, {data}")
    return data.get("result", {}) or {}

# ========= Instruments & Qty =========
INSTR_CACHE: Dict[str, Dict[str, float]] = {}

async def get_symbol_filters(symbol: str) -> Dict[str, float]:
    if symbol in INSTR_CACHE: return INSTR_CACHE[symbol]
    res = await bybit("/v5/market/instruments-info", "GET", {"category": "linear","symbol":symbol})
    lst = res.get("list", [])
    if not lst:
        INSTR_CACHE[symbol] = {"qtyStep": 0.001, "minOrderQty": 0.001}
        return INSTR_CACHE[symbol]
    f = lst[0].get("lotSizeFilter", {})
    qty_step = float(f.get("qtyStep", "0.001"))
    min_qty  = float(f.get("minOrderQty", "0.001"))
    INSTR_CACHE[symbol] = {"qtyStep": qty_step, "minOrderQty": min_qty}
    return INSTR_CACHE[symbol]

def quantize_qty(q: float, step: float) -> float:
    return max(0.0, (int(q / step)) * step) if step>0 else q

# ========= Wallet / Positions =========
async def get_wallet_equity() -> Optional[float]:
    try:
        res = await bybit("/v5/account/wallet-balance", "GET", {"accountType":"UNIFIED"})
        for item in res.get("list", []):
            for coin in item.get("coin", []):
                if coin.get("coin") == "USDT":
                    return float(coin.get("walletBalance"))
    except: pass
    return None

async def positions(symbol: Optional[str] = None):
    params = {"category":"linear"}
    if symbol: params["symbol"] = symbol
    else: params["settleCoin"] = SETTLE_COIN
    res = await bybit("/v5/position/list", "GET", params)
    return res.get("list", [])

async def positions_size_symbol(symbol: str) -> float:
    pos = await positions(symbol)
    if not pos: return 0.0
    return float(pos[0].get("size") or 0)

async def count_open_filled() -> Dict[str, int]:
    res = await positions()
    longs = shorts = 0
    for p in res:
        try:
            sz = float(p.get("size") or 0)
            if sz <= 0: continue
            side = (p.get("side") or "").lower()
            if side == "buy": longs += 1
            elif side == "sell": shorts += 1
        except: pass
    return {"longs": longs, "shorts": shorts}

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
        base = m_pair.group(1).upper()
        quote= m_pair.group(2).upper()
        if quote == "USD": quote="USDT"
        entry,tp1,tp2,sl = map(float,[m_entry.group(1),m_tp1.group(1),m_tp2.group(1),m_sl.group(1)])
        signals.append({"base":base,"quote":quote,"side":side,"entry":entry,"tp1":tp1,"tp2":tp2,"sl":sl,"tf":tf})
    return signals

# ========= Orders =========
def leverage_from_sl(entry: float, sl: float, side: str) -> int:
    sl_pct = (entry-sl)/entry*100.0 if side=="long" else (sl-entry)/entry*100.0
    lev = int(SAFETY_PCT // max(0.0001, sl_pct))
    return max(1, min(lev, MAX_LEV_CAP))

async def set_leverage(symbol: str, lev: int):
    await bybit("/v5/position/set-leverage","POST",{
        "category":"linear","symbol":symbol,
        "buyLeverage": str(lev),"sellLeverage": str(lev)
    })

async def place_entry_only(symbol: str, side: str, entry: float, notional_usdt: float):
    filters = await get_symbol_filters(symbol)
    step = filters["qtyStep"]; min_qty = filters["minOrderQty"]
    raw_qty = max(min_qty, notional_usdt / entry)
    qty = quantize_qty(raw_qty, step)
    if qty < min_qty: qty = min_qty
    BY = "Buy" if side=="long" else "Sell"
    uid = hex(int(time.time()*1000))[2:]
    link_entry = f"ent_{symbol}_{uid}"
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":BY,
        "orderType":"Limit","price":str(entry),"qty":str(qty),
        "timeInForce":"GTC","reduceOnly":"false","orderLinkId":link_entry
    })
    return link_entry, qty

async def place_tp_sl_after_fill(symbol: str, side: str, size: float, tp1: float, tp2: float, sl: float):
    filters = await get_symbol_filters(symbol)
    step = filters["qtyStep"]; min_qty = filters["minOrderQty"]
    pos_idx = 1 if side=="long" else 2
    OP = "Sell" if side=="long" else "Buy"
    uid = hex(int(time.time()*1000))[2:]
    link_tp1,link_tp2,link_sl = f"tp1_{symbol}_{uid}",f"tp2_{symbol}_{uid}",f"sl_{symbol}_{uid}"
    want_q1, want_q2 = size*(TP1_POS_PCT/100.0), size-size*(TP1_POS_PCT/100.0)
    q1,q2 = quantize_qty(want_q1,step), quantize_qty(want_q2,step)
    if q1<min_qty or q2<min_qty: q1=size; q2=0
    # TP1
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Limit","price":str(tp1),"qty":str(q1),
        "reduceOnly":"true","timeInForce":"GTC","orderLinkId":link_tp1,"positionIdx":pos_idx
    })
    # TP2
    if q2>=min_qty:
        await bybit("/v5/order/create","POST",{
            "category":"linear","symbol":symbol,"side":OP,
            "orderType":"Limit","price":str(tp2),"qty":str(q2),
            "reduceOnly":"true","timeInForce":"GTC","orderLinkId":link_tp2,"positionIdx":pos_idx
        })
    else: link_tp2=None
    # SL
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Market","reduceOnly":"true","triggerPrice":str(sl),
        "triggerDirection": (2 if OP=="Sell" else 1),
        "triggerBy":"LastPrice","stopOrderType":"StopLoss","timeInForce":"GTC",
        "orderLinkId":link_sl,"positionIdx":pos_idx
    })
    return link_tp1,link_tp2,link_sl

# ========= Monitor-Loop =========
async def monitor_loop():
    while True:
        await asyncio.sleep(5)
        if STATE.get("trading_paused_until") and datetime.now(timezone.utc)>=datetime.fromisoformat(STATE["trading_paused_until"]):
            STATE["trading_paused_until"]=None
        to_del=[]
        for symbol, meta in list(STATE["open_watch"].items()):
            if not meta.get("filled"):
                if now_ts()-meta["created_ts"]>meta.get("expiry_min",60)*60:
                    to_del.append(symbol); continue
                size = await positions_size_symbol(symbol)
                if size>0:
                    tp1,tp2,sl=await place_tp_sl_after_fill(symbol,meta["side"],size,meta["tp1_px"],meta["tp2_px"],meta["sl_px"])
                    meta.update({"tp1_id":tp1,"tp2_id":tp2,"sl_id":sl,"filled":True})
                    STATE["last_trade_ts"]=now_ts(); continue
            else:
                size = await positions_size_symbol(symbol)
                if size==0: to_del.append(symbol)
        for s in to_del: STATE["open_watch"].pop(s,None)

asyncio.get_event_loop().create_task(monitor_loop())

# ========= Guards =========
def in_cooldown() -> bool: return (now_ts()-STATE.get("last_trade_ts",0.0))<COOLDOWN_MIN*60
def trading_paused() -> bool:
    pu=STATE.get("trading_paused_until")
    return pu and datetime.now(timezone.utc)<datetime.fromisoformat(pu)

# ========= HTTP Endpoints =========
@app.post("/webhook")
async def webhook(request: Request):
    if trading_paused(): raise HTTPException(423,"Trading paused")
    body=await request.json()
    text=(body.get("text") or extract_text_from_payload(body)).strip()
    if not text: raise HTTPException(400,"No signal text found")
    notional=float(body.get("notional") or DEFAULT_NOTION)
    sigs=parse_signals(text)
    if not sigs: raise HTTPException(422,"No valid signal")
    counts=await count_open_filled()
    side_preview=sigs[0]["side"]
    if side_preview=="long" and counts["longs"]>=MAX_OPEN_LONGS: raise HTTPException(429,"Too many longs")
    if side_preview=="short" and counts["shorts"]>=MAX_OPEN_SHORTS: raise HTTPException(429,"Too many shorts")
    if in_cooldown(): raise HTTPException(429,"In cooldown")
    sig=sigs[0]; base,quote,side=sig["base"],sig["quote"],sig["side"]
    entry,tp1,tp2,sl=sig["entry"],sig["tp1"],sig["tp2"],sig["sl"]
    symbol=f"{base}{quote}"
    lev=leverage_from_sl(entry,sl,side); await set_leverage(symbol,lev)
    entry_id,qty=await place_entry_only(symbol,side,entry,notional)
    STATE["open_watch"][symbol]={"entry_id":entry_id,"side":side,"entry_px":entry,"tp1_px":tp1,"tp2_px":tp2,"sl_px":sl,"created_ts":now_ts(),"expiry_min":ENTRY_EXP_MIN,"filled":False}
    return {"ok":True,"symbol":symbol,"side":side,"entry":entry,"tp1":tp1,"tp2":tp2,"sl":sl,"leverage":lev,"entry_order_id":entry_id,"positions_open_counts":counts}

def extract_text_from_payload(payload: dict) -> str:
    node=payload
    for part in TEXT_PATH.split("."):
        if isinstance(node, dict) and part in node: node=node[part]
        else: return ""
    return node if isinstance(node,str) else ""

@app.get("/health")
async def health():
    return {"ok":True,"paused":trading_paused(),"watch":list(STATE["open_watch"].keys())}
