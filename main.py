import os, re, hmac, json, time, asyncio, hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
BYBIT_BASE    = os.getenv("BYBIT_BASE", "https://api-testnet.bybit.com")
BYBIT_KEY     = os.getenv("BYBIT_KEY", "")
BYBIT_SECRET  = os.getenv("BYBIT_SECRET", "")

# Trading-Guards
COOLDOWN_MIN  = int(os.getenv("COOLDOWN_MIN", "45"))      # 45 min globaler Cooldown
DD_LIMIT_PCT  = float(os.getenv("DD_LIMIT_PCT", "2.8"))   # daily drawdown in %
ENTRY_EXP_MIN = int(os.getenv("ENTRY_EXP_MIN", "60"))     # Entry-Expiry (Minuten)

# Positionslogik
TP1_POS_PCT   = float(os.getenv("TP1_POS_PCT", "20"))     # 20/80 Split
TP2_POS_PCT   = float(os.getenv("TP2_POS_PCT", "80"))

MAX_LEV_CAP   = int(os.getenv("MAX_LEV_CAP", "75"))
SAFETY_PCT    = float(os.getenv("SAFETY_PCT", "80"))      # Hebel = floor( SAFETY_PCT / SL% )

# Timeframe-Filter (z. B. "H1" oder "H1,H4")
ALLOWED_TFS   = set(os.getenv("ALLOWED_TFS", "H1").replace(" ", "").split(","))

# ========= App / State =========
app = FastAPI()
client = httpx.AsyncClient(timeout=10.0)

STATE: Dict[str, Any] = {
    "last_trade_ts": 0.0,
    "trading_paused_until": None,     # iso-zeit
    "day_key": None,                  # "YYYY-MM-DD"
    "day_start_equity": None,         # USDT (optional)
    "day_realized_pnl": 0.0,          # USDT (vereinfachte Summierung)
    "open_watch": {}                  # symbol -> meta (ids, preise, flags)
}

# grobe Tick-Rundung je Coin
TICK_DECIMALS = {
    "SHIB": 8, "DOGE": 5, "XRP": 4, "SOL": 2, "AVAX": 3, "AAVE": 2, "LINK": 3,
    "BTC": 2, "ETH": 2, "BNB": 2, "LTC": 2, "ADA": 5, "MATIC": 5, "EOS": 4, "BCH": 2, "ATOM": 3, "ALGO": 5
}

def now_ts() -> float:
    return time.time()

def today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def round_tick(base: str, v: float) -> float:
    d = TICK_DECIMALS.get(base, 4)
    p = 10 ** d
    return round(v * p) / p

# ========= Bybit Signing =========
def _qs(params: Dict[str, Any]) -> str:
    return "&".join(f"{k}={str(v)}" for k, v in params.items() if v is not None and v != "")

async def bybit(path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not BYBIT_KEY or not BYBIT_SECRET:
        raise HTTPException(500, "BYBIT_KEY/SECRET not set")
    payload = {**params, "api_key": BYBIT_KEY, "timestamp": str(int(now_ts()*1000)), "recv_window":"5000"}
    q = _qs(payload)
    sig = hmac.new(BYBIT_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{BYBIT_BASE}{path}?{q}&sign={sig}"
    r = await client.request(method, url)
    data = r.json()
    if r.status_code != 200 or data.get("retCode") != 0:
        raise HTTPException(502, f"Bybit error {data.get('retCode')}: {data.get('retMsg')}, {data}")
    return data.get("result", {})

# ========= Wallet / Positions =========
async def get_wallet_equity() -> Optional[float]:
    try:
        res = await bybit("/v5/account/wallet-balance", "GET", {"accountType":"UNIFIED"})
        for item in res.get("list", []):
            for coin in item.get("coin", []):
                if coin.get("coin") == "USDT":
                    return float(coin.get("walletBalance"))
    except Exception:
        pass
    return None

async def positions(symbol: Optional[str] = None):
    params = {"category":"linear"}
    if symbol: params["symbol"] = symbol
    res = await bybit("/v5/position/list", "GET", params)
    return res.get("list", [])

async def close_all_positions():
    pos = await positions()
    tasks = []
    for p in pos:
        sym = p.get("symbol")
        sz  = float(p.get("size") or 0)
        side= p.get("side")  # "Buy" / "Sell"
        if sym and sz > 0:
            opp = "Sell" if side=="Buy" else "Buy"
            tasks.append(bybit("/v5/order/create","POST",{
                "category":"linear","symbol":sym,"side":opp,"orderType":"Market",
                "reduceOnly":"true","qty":sz
            }))
    if tasks:
        await asyncio.gather(*tasks)

# ========= Parser (mit TF-Filter) =========
def parse_signals(text: str):
    """
    Erwartet den kompletten Signaltext (evtl. mehrere Blöcke).
    Gibt NUR Blöcke zurück, die 'Timeframe: ...' enthalten und in ALLOWED_TFS liegen.
    Rückgabe: Liste [{base, quote, side, entry, tp1, tp2, sl, tf}]
    """
    txt = text.replace("\r","")
    blocks = re.split(r"\n\s*\n", txt.strip())
    signals = []
    for b in blocks:
        m_tf = re.search(r"Timeframe:\s*([A-Za-z0-9]+)", b, re.I)
        if not m_tf:
            continue
        tf = m_tf.group(1).upper()
        if tf not in ALLOWED_TFS:
            continue

        m_side = re.search(r"\b(BUY|SELL)\b", b, re.I)
        m_pair = re.search(r"on\s+([A-Z0-9]+)[/\-]([A-Z0-9]+)", b, re.I)
        m_entry= re.search(r"Price:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_tp1  = re.search(r"TP\s*1:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_tp2  = re.search(r"TP\s*2:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_sl   = re.search(r"\bSL\s*:\s*([0-9]*\.?[0-9]+)", b, re.I)
        if not (m_side and m_pair and m_entry and m_tp1 and m_tp2 and m_sl):
            continue

        side = "long" if m_side.group(1).upper()=="BUY" else "short"
        base = m_pair.group(1).upper()
        quote= m_pair.group(2).upper()
        if quote == "USD": quote = "USDT"

        entry=float(m_entry.group(1)); tp1=float(m_tp1.group(1))
        tp2=float(m_tp2.group(1));    sl=float(m_sl.group(1))

        # Plausibilität
        if side=="long"  and not (sl < entry < tp1 <= tp2): continue
        if side=="short" and not (sl > entry > tp1 >= tp2): continue

        # Tick-Rundung
        entry=round_tick(base,entry); tp1=round_tick(base,tp1)
        tp2=round_tick(base,tp2);     sl=round_tick(base,sl)

        signals.append({
            "base":base,"quote":quote,"side":side,
            "entry":entry,"tp1":tp1,"tp2":tp2,"sl":sl,
            "tf": tf
        })
    return signals

# ========= Leverage / Orders =========
def leverage_from_sl(entry: float, sl: float, side: str) -> int:
    if side=="long":  sl_pct = (entry - sl)/entry*100.0
    else:             sl_pct = (sl - entry)/entry*100.0
    lev = int(SAFETY_PCT // max(0.0001, sl_pct))
    lev = max(1, min(lev, MAX_LEV_CAP))
    return lev

async def set_leverage(symbol: str, lev: int):
    await bybit("/v5/position/set-leverage","POST",{
        "category":"linear","symbol":symbol,
        "buyLeverage": str(lev),"sellLeverage": str(lev)
    })

async def place_orders(symbol: str, side: str, entry: float, tp1: float, tp2: float, sl: float,
                       notional_usdt: float):
    qty = max(0.001, round(notional_usdt/entry, 6))
    BY = "Buy" if side=="long" else "Sell"
    OP = "Sell" if BY=="Buy" else "Buy"

    uid = hex(int(time.time()*1000))[2:]
    link_entry = f"ent_{symbol}_{uid}"
    link_tp1   = f"tp1_{symbol}_{uid}"
    link_tp2   = f"tp2_{symbol}_{uid}"
    link_sl    = f"sl_{symbol}_{uid}"

    # Entry
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":BY,
        "orderType":"Limit","price":entry,"qty":qty,
        "timeInForce":"GTC","reduceOnly":"false","orderLinkId":link_entry
    })

    # Partials 20/80
    q1 = max(0.001, round(qty * (TP1_POS_PCT/100.0), 6))
    q2 = max(0.001, round(qty - q1, 6))

    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Limit","price":tp1,"qty":q1,
        "reduceOnly":"true","timeInForce":"GTC","orderLinkId":link_tp1
    })
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Limit","price":tp2,"qty":q2,
        "reduceOnly":"true","timeInForce":"GTC","orderLinkId":link_tp2
    })

    # SL (Stop-Market)
    trigDir = 2 if BY=="Buy" else 1
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Market","reduceOnly":"true",
        "triggerPrice":sl,"triggerDirection":trigDir,
        "stopOrderType":"StopLoss","timeInForce":"GTC",
        "orderLinkId":link_sl
    })

    STATE["open_watch"][symbol] = {
        "tp1_id": link_tp1, "tp2_id": link_tp2, "sl_id": link_sl,
        "entry_id": link_entry, "side": side,
        "entry_px": entry, "tp1_px": tp1, "tp2_px": tp2,
        "created_ts": now_ts(), "expiry_min": ENTRY_EXP_MIN,
        "did_be": False, "did_tp2_move": False
    }
    return {"entry":link_entry,"tp1":link_tp1,"tp2":link_tp2,"sl":link_sl,"qty":qty}

async def order_status_by_link(symbol: str, link_id: str) -> Optional[str]:
    res = await bybit("/v5/order/realtime","GET",{
        "category":"linear","symbol":symbol,"orderLinkId":link_id
    })
    lst = res.get("list",[])
    if not lst: return None
    return lst[0].get("orderStatus")  # New / PartiallyFilled / Filled / Cancelled / Rejected

async def set_stop_to_BE(symbol: str, side: str):
    pos = await positions(symbol)
    if not pos: return
    avg = float(pos[0].get("avgPrice") or 0)
    if avg <= 0: return
    # mini-offset (1-2 bp), damit BE sofort greift
    off = avg * 0.0002
    be  = (avg - off) if side=="long" else (avg + off)
    await bybit("/v5/position/set-trading-stop","POST",{
        "category":"linear","symbol":symbol,"tpSlMode":"Full",
        "stopLoss": f"{be}"
    })

async def move_stop_to(symbol: str, price: float):
    await bybit("/v5/position/set-trading-stop","POST",{
        "category":"linear","symbol":symbol,"tpSlMode":"Full",
        "stopLoss": f"{price}"
    })

async def cancel_order(symbol: str, link_id: str):
    try:
        await bybit("/v5/order/cancel","POST",{
            "category":"linear","symbol":symbol,"orderLinkId":link_id
        })
    except Exception:
        pass

# ========= Monitor-Loop =========
async def monitor_loop():
    while True:
        await asyncio.sleep(5)

        # Trading-Pause bis 00:00 aufheben
        if STATE.get("trading_paused_until"):
            if datetime.now(timezone.utc) >= datetime.fromisoformat(STATE["trading_paused_until"]):
                STATE["trading_paused_until"] = None

        # Entries expiren; BE/TP2-Moves ausführen; Cleanup wenn Position 0
        to_del = []
        for symbol, meta in list(STATE["open_watch"].items()):
            # Entry-Expiry
            if now_ts() - meta["created_ts"] > meta.get("expiry_min", 60)*60:
                st = await order_status_by_link(symbol, meta["entry_id"])
                if st in ("New","PartiallyFilled"):
                    await cancel_order(symbol, meta["entry_id"])

            # TP1 filled -> SL -> BE
            st_tp1 = await order_status_by_link(symbol, meta["tp1_id"])
            if st_tp1 == "Filled" and not meta.get("did_be"):
                await set_stop_to_BE(symbol, meta["side"])
                await cancel_order(symbol, meta["sl_id"])
                meta["did_be"] = True

            # TP2 filled -> SL -> TP1
            st_tp2 = await order_status_by_link(symbol, meta["tp2_id"])
            if st_tp2 == "Filled" and not meta.get("did_tp2_move"):
                await move_stop_to(symbol, meta["tp1_px"])
                meta["did_tp2_move"] = True

            # Cleanup, wenn Position geschlossen
            pos = await positions(symbol)
            if pos:
                sz = float(pos[0].get("size") or 0)
                if sz == 0:
                    to_del.append(symbol)
            else:
                to_del.append(symbol)

        for s in to_del:
            STATE["open_watch"].pop(s, None)

# Hintergrund starten
asyncio.get_event_loop().create_task(monitor_loop())

# ========= Guards =========
def in_cooldown() -> bool:
    return (now_ts() - STATE.get("last_trade_ts", 0.0)) < COOLDOWN_MIN*60

def trading_paused() -> bool:
    pu = STATE.get("trading_paused_until")
    return pu is not None and datetime.now(timezone.utc) < datetime.fromisoformat(pu)

async def check_daily_dd_and_pause(pnl_delta: float = 0.0):
    STATE["day_realized_pnl"] += pnl_delta
    eq = await get_wallet_equity()
    if eq and STATE.get("day_start_equity") is None:
        STATE["day_start_equity"] = eq
    start_eq = STATE.get("day_start_equity") or eq
    if start_eq and STATE["day_realized_pnl"] < 0:
        dd = abs(STATE["day_realized_pnl"])/start_eq*100.0
        if dd >= DD_LIMIT_PCT:
            # Pause bis 00:00 UTC, alle Positionen schließen
            tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
            pu = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc).isoformat()
            STATE["trading_paused_until"] = pu
            await close_all_positions()
            return True
    return False

# ========= HTTP Endpoints =========
@app.post("/webhook")
async def webhook(request: Request):
    """
    Erwartet JSON: {"text":"...Signalblock(e)...", "notional":50}
    - filtert auf ALLOWED_TFS (z. B. H1)
    - nimmt das erste valide Signal
    - respektiert Cooldown & Daily-DD-Stop
    """
    if trading_paused():
        raise HTTPException(423, "Trading paused (daily DD or schedule)")

    body = await request.json()
    text = (body.get("text") or "").strip()
    notional = float(body.get("notional") or 50.0)

    if not text:
        raise HTTPException(400, "Missing text")

    sigs = parse_signals(text)
    if not sigs:
        raise HTTPException(422, "No valid H1/H4 signal found")

    if in_cooldown():
        raise HTTPException(429, f"In cooldown ({COOLDOWN_MIN} min)")

    sig = sigs[0]
    base, quote, side = sig["base"], sig["quote"], sig["side"]
    entry, tp1, tp2, sl = sig["entry"], sig["tp1"], sig["tp2"], sig["sl"]
    symbol = f"{base}{quote}"  # Bybit linear: e.g. SOLUSDT

    lev = leverage_from_sl(entry, sl, side)
    await set_leverage(symbol, lev)

    paused = await check_daily_dd_and_pause(0.0)
    if paused:
        raise HTTPException(423, "Trading paused by daily drawdown")

    res = await place_orders(symbol, side, entry, tp1, tp2, sl, notional_usdt=notional)
    STATE["last_trade_ts"] = now_ts()

    return {
        "ok": True,
        "symbol": symbol, "side": side,
        "entry": entry, "tp1": tp1, "tp2": tp2, "sl": sl,
        "leverage": lev,
        "order_ids": res,
        "cooldown_min": COOLDOWN_MIN
    }

@app.get("/health")
async def health():
    return {"ok": True, "paused": trading_paused(), "watch": list(STATE["open_watch"].keys())}
