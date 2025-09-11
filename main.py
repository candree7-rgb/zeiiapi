# main.py
import os, re, hmac, json, time, asyncio, hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
BYBIT_BASE     = os.getenv("BYBIT_BASE", "https://api-testnet.bybit.com")
BYBIT_KEY      = os.getenv("BYBIT_KEY", "")
BYBIT_SECRET   = os.getenv("BYBIT_SECRET", "")

# Timeframe-Filter (z.B. "H1" oder "H1,H4" oder "M15")
ALLOWED_TFS    = set(os.getenv("ALLOWED_TFS", "H1").replace(" ", "").split(","))

# Positionslogik
TP1_POS_PCT    = float(os.getenv("TP1_POS_PCT", "20"))     # z.B. 20/80 Split
TP2_POS_PCT    = float(os.getenv("TP2_POS_PCT", "80"))
MAX_LEV_CAP    = int(os.getenv("MAX_LEV_CAP", "75"))
SAFETY_PCT     = float(os.getenv("SAFETY_PCT", "80"))      # Hebel = floor(SAFETY_PCT / SL%)

# Guards
COOLDOWN_MIN   = int(os.getenv("COOLDOWN_MIN", "45"))      # globaler Cooldown
ENTRY_EXP_MIN  = int(os.getenv("ENTRY_EXP_MIN", "60"))     # Entry-Expiry (Min.)
DD_LIMIT_PCT   = float(os.getenv("DD_LIMIT_PCT", "2.8"))   # Daily-Drawdown-Stop (%)

# Eingangs-Payload: wo liegt der Text? (bei rohem Discord-JSON meist "content")
TEXT_PATH      = os.getenv("TEXT_PATH", "content")
DEFAULT_NOTION = float(os.getenv("DEFAULT_NOTIONAL", "50"))  # USDT pro Trade

# ========= App / State =========
app = FastAPI()
_httpx_client: Optional[httpx.AsyncClient] = None

STATE: Dict[str, Any] = {
    "last_trade_ts": 0.0,
    "trading_paused_until": None,     # iso-zeit
    "day_key": None,                  # "YYYY-MM-DD"
    "day_start_equity": None,         # USDT (optional)
    "day_realized_pnl": 0.0,          # USDT (vereinfachte Summierung)
    "open_watch": {}                  # symbol -> meta (ids, preise, flags)
}

# grobe Tick-Rundung je Coin (bei Bedarf erweitern)
TICK_DECIMALS = {
    "SHIB": 8, "DOGE": 5, "XRP": 4, "SOL": 2, "AVAX": 3, "AAVE": 2, "LINK": 3,
    "BTC": 2, "ETH": 2, "BNB": 2, "LTC": 2, "ADA": 5, "MATIC": 5, "EOS": 4, "BCH": 2, "ATOM": 3, "ALGO": 5
}

def now_ts() -> float: return time.time()
def today_key() -> str: return datetime.now(timezone.utc).date().isoformat()

def round_tick(base: str, v: float) -> float:
    d = TICK_DECIMALS.get(base, 4)
    p = 10 ** d
    return round(v * p) / p

# ========= FastAPI Lifecycle =========
@app.on_event("startup")
async def _startup():
    global _httpx_client
    _httpx_client = httpx.AsyncClient(timeout=10.0)

@app.on_event("shutdown")
async def _shutdown():
    global _httpx_client
    if _httpx_client:
        await _httpx_client.aclose()
        _httpx_client = None

def _qs(params: Dict[str, Any]) -> str:
    # Bybit v5 expects query string by key order (stable with Python 3.7+ dict)
    return "&".join(f"{k}={str(v)}" for k, v in params.items() if v is not None and v != "")

async def bybit(path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not BYBIT_KEY or not BYBIT_SECRET:
        raise HTTPException(500, "BYBIT_KEY/SECRET not set")
    global _httpx_client
    if not _httpx_client:
        raise HTTPException(500, "HTTP client not ready")

    payload = {
        **params,
        "api_key": BYBIT_KEY,
        "timestamp": str(int(now_ts()*1000)),
        "recv_window": "5000"
    }
    q = _qs(payload)
    sig = hmac.new(BYBIT_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{BYBIT_BASE}{path}?{q}&sign={sig}"
    r = await _httpx_client.request(method, url)
    data = r.json()
    if r.status_code != 200 or data.get("retCode") != 0:
        raise HTTPException(502, f"Bybit error {data.get('retCode')}: {data.get('retMsg')}, {data}")
    return data.get("result", {}) or {}

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
                "reduceOnly":"true","qty":str(sz)
            }))
    if tasks: await asyncio.gather(*tasks)

# ========= Parser (mit TF-Filter) =========
def parse_signals(text: str):
    """
    Erwartet den kompletten Signaltext (evtl. mehrere Blöcke).
    Gibt NUR Blöcke zurück, die 'Timeframe: ...' enthalten und in ALLOWED_TFS liegen.
    Rückgabe: Liste [{base, quote, side, entry, tp1, tp2, sl, tf}]
    """
    txt = text.replace("\r","")
    # grob in Blöcke splitten (leere Zeile trennt Setups zuverlässig genug)
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
        tp2=float(m_tp2.group(1));     sl=float(m_sl.group(1))

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

# ---- Entry zuerst, TPs/SL später (nach Fill) ----
async def place_entry_only(symbol: str, side: str, entry: float, tp1: float, tp2: float, sl: float,
                           notional_usdt: float):
    qty = max(0.001, round(notional_usdt/entry, 6))
    BY = "Buy" if side=="long" else "Sell"

    uid = hex(int(time.time()*1000))[2:]
    link_entry = f"ent_{symbol}_{uid}"

    # NUR Entry-Limit
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":BY,
        "orderType":"Limit","price":str(entry),"qty":str(qty),
        "timeInForce":"GTC","reduceOnly":"false","orderLinkId":link_entry
    })

    STATE["open_watch"][symbol] = {
        "entry_id": link_entry, "side": side,
        "entry_px": entry, "tp1_px": tp1, "tp2_px": tp2, "sl_px": sl,
        "created_ts": now_ts(), "expiry_min": ENTRY_EXP_MIN,
        "tps_placed": False,     # <— neu
        "did_be": False, "did_tp2_move": False
    }
    return {"entry":link_entry, "qty":qty}

async def place_tp_sl_after_fill(symbol: str, side: str, tp1: float, tp2: float, sl: float):
    # echte Positionsgröße holen
    pos = await positions(symbol)
    if not pos:
        return False
    sz  = float(pos[0].get("size") or 0)
    if sz <= 0:
        return False

    OP = "Sell" if side=="long" else "Buy"

    uid = hex(int(time.time()*1000))[2:]
    link_tp1   = f"tp1_{symbol}_{uid}"
    link_tp2   = f"tp2_{symbol}_{uid}"

    # reduceOnly TPs – jetzt erlaubt, da Position > 0
    q1 = max(0.001, round(sz * (TP1_POS_PCT/100.0), 6))
    q2 = max(0.001, round(sz - q1, 6))

    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Limit","price":str(tp1),"qty":str(q1),
        "reduceOnly":"true","timeInForce":"GTC","orderLinkId":link_tp1
    })
    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":OP,
        "orderType":"Limit","price":str(tp2),"qty":str(q2),
        "reduceOnly":"true","timeInForce":"GTC","orderLinkId":link_tp2
    })

    # SL via Trading-Stop an Position hängen (robust, kein reduceOnly-Problem)
    await bybit("/v5/position/set-trading-stop","POST",{
        "category":"linear","symbol":symbol,"tpSlMode":"Full","stopLoss": f"{sl}"
    })

    # Meta speichern
    meta = STATE["open_watch"].get(symbol, {})
    meta["tp1_id"] = link_tp1
    meta["tp2_id"] = link_tp2
    meta["sl_id"]  = "pos_stop"     # via trading-stop gesetzt
    meta["tps_placed"] = True
    STATE["open_watch"][symbol] = meta
    return True

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
    # mini offset (1–2bp), damit BE sofort greift
    off = avg * 0.0002
    be  = (avg - off) if side=="long" else (avg + off)
    await bybit("/v5/position/set-trading-stop","POST",{
        "category":"linear","symbol":symbol,"tpSlMode":"Full","stopLoss": f"{be}"
    })

async def move_stop_to(symbol: str, price: float):
    await bybit("/v5/position/set-trading-stop","POST",{
        "category":"linear","symbol":symbol,"tpSlMode":"Full","stopLoss": f"{price}"
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

        # Trading-Pause bis 00:00 UTC automatisch aufheben
        if STATE.get("trading_paused_until"):
            if datetime.now(timezone.utc) >= datetime.fromisoformat(STATE["trading_paused_until"]):
                STATE["trading_paused_until"] = None

        # Entries expiren; TPs/SL nach Fill; BE/TP2-Moves; Cleanup
        to_del = []
        for symbol, meta in list(STATE["open_watch"].items()):
            # Entry-Expiry: unfilled Entry nach ENTRY_EXP_MIN min löschen
            if now_ts() - meta["created_ts"] > meta.get("expiry_min", 60)*60:
                st = await order_status_by_link(symbol, meta["entry_id"])
                if st in ("New","PartiallyFilled"):
                    await cancel_order(symbol, meta["entry_id"])

            # Wenn Entry gefüllt und TPs/SL noch nicht dran: jetzt platzieren
            st_entry = await order_status_by_link(symbol, meta["entry_id"])
            if st_entry == "Filled" and not meta.get("tps_placed"):
                ok = await place_tp_sl_after_fill(
                    symbol, meta["side"], meta["tp1_px"], meta["tp2_px"], meta["sl_px"]
                )
                if ok:
                    meta["tps_placed"] = True

            # TP1 filled -> SL -> BE
            if meta.get("tps_placed") and meta.get("tp1_id"):
                st_tp1 = await order_status_by_link(symbol, meta["tp1_id"])
                if st_tp1 == "Filled" and not meta.get("did_be"):
                    await set_stop_to_BE(symbol, meta["side"])
                    meta["did_be"] = True

            # TP2 filled -> SL -> TP1 (Profit lock)
            if meta.get("tps_placed") and meta.get("tp2_id"):
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
    # vereinfachte PnL-Summe; optional Wallet-Equity für Schwelle
    STATE["day_realized_pnl"] += pnl_delta
    eq = await get_wallet_equity()
    if eq and STATE.get("day_start_equity") is None:
        STATE["day_start_equity"] = eq
    start_eq = STATE.get("day_start_equity") or eq
    if start_eq and STATE["day_realized_pnl"] < 0:
        dd = abs(STATE["day_realized_pnl"])/start_eq*100.0
        if dd >= DD_LIMIT_PCT:
            # Pause bis 00:00 UTC, alles schließen
            tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
            pu = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc).isoformat()
            STATE["trading_paused_until"] = pu
            await close_all_positions()
            return True
    return False

# ========= Payload-Text-Extractor =========
def extract_text_from_payload(payload: dict) -> str:
    node = payload
    for part in TEXT_PATH.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return ""
    return node if isinstance(node, str) else ""

# ========= HTTP Endpoints =========
@app.post("/webhook")
async def webhook(request: Request):
    """
    Akzeptiert EITHER:
      A) {"text": "...ganzer Signal-Text...", "notional": 50}
      B) rohen Discord-Forward-Payload mit Text im Feld TEXT_PATH (Default: "content")
    """
    if trading_paused():
        raise HTTPException(423, "Trading paused (daily DD or schedule)")

    body = await request.json()

    # Text finden
    text = (body.get("text") or "").strip()
    if not text:
        text = extract_text_from_payload(body).strip()
    if not text:
        raise HTTPException(400, "No signal text found (neither 'text' nor TEXT_PATH)")

    # Positionsgröße
    notional = float(body.get("notional") or DEFAULT_NOTION)

    # TF filtern & erstes valides Setup
    sigs = parse_signals(text)
    if not sigs:
        raise HTTPException(422, f"No valid signal for TFs {sorted(list(ALLOWED_TFS))}")

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

    res = await place_entry_only(symbol, side, entry, tp1, tp2, sl, notional_usdt=notional)
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
