import os, csv, io, statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from enum import Enum
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse

app=FastAPI(title="AlfieFX Gold Intelligence",version="1.2.0")
IMPORTED={}
CACHE={}
TD=os.getenv("TWELVE_DATA_API_KEY","").strip()
FRED=os.getenv("FRED_API_KEY","").strip()
FINNHUB=os.getenv("FINNHUB_API_KEY","").strip()

class Market(str,Enum):
    XAUUSD="XAUUSD"
    MGC="MGC"

def now(): return datetime.now(timezone.utc).isoformat()
async def get_json(url,params):
    async with httpx.AsyncClient(timeout=12) as c:
        r=await c.get(url,params=params); r.raise_for_status(); return r.json()

async def candles_for(market,interval="5min",limit=200):
    if market in IMPORTED and IMPORTED[market]:
        rows=IMPORTED[market][-limit:]
        return {"market":market,"provider":"CSV import","data_status":"historical","is_live_data":False,"checked_at":now(),"candles":rows,"count":len(rows)}
    if market=="MGC":
        raise RuntimeError("MGC needs an authorised CME futures provider and entitlement. Spot gold is not a substitute.")
    if not TD: raise RuntimeError("Add TWELVE_DATA_API_KEY in Render Environment Variables to enable XAU/USD candles.")
    key=f"{market}:{interval}:{limit}"
    if key in CACHE and (datetime.now(timezone.utc)-CACHE[key]["at"]).total_seconds()<45:
        return CACHE[key]["data"]
    j=await get_json("https://api.twelvedata.com/time_series",{"symbol":"XAU/USD","interval":interval,"outputsize":min(max(limit,30),500),"apikey":TD,"format":"JSON","timezone":"UTC"})
    if "values" not in j: raise RuntimeError(j.get("message","Twelve Data returned no candles; check symbol, API key and plan."))
    rows=[]
    for x in reversed(j["values"]):
        try:
            ts=x["datetime"]
            if len(ts)==19 and "T" not in ts: ts=ts.replace(" ","T")+"+00:00"
            elif not ts.endswith("Z") and "+" not in ts[10:]: ts+="+00:00"
            rows.append({"timestamp":datetime.fromisoformat(ts.replace("Z","+00:00")).isoformat(),"open":float(x["open"]),"high":float(x["high"]),"low":float(x["low"]),"close":float(x["close"]),"volume":float(x["volume"]) if x.get("volume") not in (None,"","null") else None})
        except Exception: pass
    if not rows: raise RuntimeError("No valid candles returned by provider.")
    data={"market":market,"provider":"Twelve Data","data_status":"provider_connected","is_live_data":True,"checked_at":now(),"candles":rows[-limit:],"count":min(len(rows),limit)}
    CACHE[key]={"at":datetime.now(timezone.utc),"data":data}
    return data

def ema(vals,n):
    if len(vals)<n:return None
    k=2/(n+1); v=vals[0]
    for x in vals[1:]:v=x*k+v*(1-k)
    return v

def technical(rows):
    if len(rows)<30:return {"available":False,"signal":"WAIT","reason":"At least 30 candles are needed."}
    cls=[x["close"] for x in rows]; last=rows[-1]
    e20=ema(cls[-80:],20); e50=ema(cls[-150:],50) if len(cls)>=50 else None
    prev=rows[-21:-1]; hi=max(x["high"] for x in prev); lo=min(x["low"] for x in prev)
    rng=max(last["high"]-last["low"],0.000001); body=abs(last["close"]-last["open"])/rng
    bs=last["low"]<lo and last["close"]>lo; ss=last["high"]>hi and last["close"]<hi
    bd=last["close"]>last["open"] and body>=.55; sd=last["close"]<last["open"] and body>=.55
    up=last["close"]>hi; dn=last["close"]<lo
    tu=last["close"]>e20 and (e50 is None or e20>e50); td=last["close"]<e20 and (e50 is None or e20<e50)
    buy=sum([tu,bs,bd,up]); sell=sum([td,ss,sd,dn])
    side="BUY CANDIDATE" if buy>=3 and buy>sell else "SELL CANDIDATE" if sell>=3 and sell>buy else "WAIT"
    reasons=[]
    if side=="BUY CANDIDATE":
        if tu:reasons.append("Bullish EMA/trend alignment")
        if bs:reasons.append("Local liquidity sweep and reclaim")
        if bd:reasons.append("Bullish displacement candle")
        if up:reasons.append("Close broke the prior 20-candle high")
    elif side=="SELL CANDIDATE":
        if td:reasons.append("Bearish EMA/trend alignment")
        if ss:reasons.append("Local liquidity sweep and rejection")
        if sd:reasons.append("Bearish displacement candle")
        if dn:reasons.append("Close broke the prior 20-candle low")
    else:reasons=["Conditions do not align strongly enough."]
    atr=statistics.mean([x["high"]-x["low"] for x in rows[-14:]])
    entry=last["close"]; sl=t1=t2=None
    if side=="BUY CANDIDATE":
        sl=min(last["low"],entry-1.2*atr); risk=entry-sl; t1=entry+2*risk; t2=entry+3*risk
    elif side=="SELL CANDIDATE":
        sl=max(last["high"],entry+1.2*atr); risk=sl-entry; t1=entry-2*risk; t2=entry-3*risk
    vols=[x["volume"] for x in rows[-21:-1] if x.get("volume") is not None]
    vconf=(last["volume"]>statistics.mean(vols)) if last.get("volume") is not None and vols else None
    return {"available":True,"signal":side,"signal_score":max(buy,sell),"score_max":4,"last_price":entry,"last_candle_at":last["timestamp"],"trend":"Bullish" if tu else "Bearish" if td else "Mixed","volume_above_recent_average":vconf,"entry_reference":entry if sl is not None else None,"stop_reference":sl,"target_1_reference":t1,"target_2_reference":t2,"reasons":reasons,"warning":"Initial technical candle screen only; not the full multi-timeframe strategy, not a win probability, and not a recommendation."}

async def fred_series(sid):
    if not FRED: raise RuntimeError("FRED_API_KEY not configured.")
    j=await get_json("https://api.stlouisfed.org/fred/series/observations",{"series_id":sid,"api_key":FRED,"file_type":"json","sort_order":"desc","limit":3})
    obs=[x for x in j.get("observations",[]) if x.get("value") not in ("",None,".")]
    if not obs:raise RuntimeError("No observations for "+sid)
    return {"series_id":sid,"date":obs[0]["date"],"value":float(obs[0]["value"]),"previous_value":float(obs[1]["value"]) if len(obs)>1 else None}

@app.get("/",response_class=HTMLResponse)
def home():return HTML
@app.get("/health")
def health():return {"status":"ok","version":app.version,"spot_key_configured":bool(TD),"fred_configured":bool(FRED),"news_configured":bool(FINNHUB),"mgc_live_configured":False,"order_execution":False}

@app.get("/api/markets/status")
async def status():
    out={}
    for m in ("XAUUSD","MGC"):
        if m=="MGC":out[m]={"data_status":"not_configured","message":"Requires authorised CME futures data; spot is not a substitute."}
        elif m in IMPORTED:out[m]={"data_status":"historical_imported","message":f"{len(IMPORTED[m])} imported candles; not live."}
        elif not TD:out[m]={"data_status":"not_configured","message":"Add TWELVE_DATA_API_KEY in Render."}
        else:
            try:
                d=await candles_for(m,"5min",40);out[m]={"data_status":"provider_connected","message":f"Received {d['count']} candles. Last candle {d['candles'][-1]['timestamp']}."}
            except Exception as e:out[m]={"data_status":"provider_error","message":str(e)[:200]}
    return {"checked_at":now(),"markets":out,"macro_configured":bool(FRED),"news_configured":bool(FINNHUB),"order_execution":False}

@app.get("/api/markets/{market}/candles")
async def get_candles(market:Market,interval:str="5min",limit:int=200):
    if interval not in ("5min","15min","1h","4h","1day"):raise HTTPException(400,"Supported intervals: 5min, 15min, 1h, 4h, 1day.")
    if not 30<=limit<=500:raise HTTPException(400,"limit must be 30-500.")
    try:return await candles_for(market.value,interval,limit)
    except Exception as e:raise HTTPException(503,str(e)[:250])

@app.get("/api/signals/{market}")
async def signals(market:Market):
    try:
        data={}
        for tf,interval,limit in [("5M","5min",200),("15M","15min",100),("1H","1h",100),("4H","4h",80)]:
            c=await candles_for(market.value,interval,limit)
            data[tf]={"candles":c["candles"],"data_status":c["data_status"],"count":c["count"]}
        result=technical(data["5M"]["candles"])
        result.update({"market":market.value,"data_status":data["5M"]["data_status"],"is_live_data":data["5M"]["data_status"]=="provider_connected","checked_at":now(),"timeframes":{tf:{"count":x["count"],"trend":technical(x["candles"]).get("trend","Unavailable"),"last_candle_at":x["candles"][-1]["timestamp"]} for tf,x in data.items()}})
        return result
    except Exception as e:raise HTTPException(503,str(e)[:250])

@app.get("/api/macro")
async def macro():
    if not FRED:return {"status":"not_configured","bias":"Unknown","message":"Add FRED_API_KEY for daily USD and 10-year real-yield context. This is not intraday news.","items":[]}
    items=[]; errors=[]
    for sid,label in [("DTWEXBGS","Broad US dollar index"),("DFII10","10-year real Treasury yield")]:
        try:
            x=await fred_series(sid);x["label"]=label;x["change"]=None if x["previous_value"] is None else x["value"]-x["previous_value"];items.append(x)
        except Exception as e:errors.append(sid+": "+str(e)[:120])
    pts=0
    for x in items:
        if x["change"] is not None:pts+=(-1 if x["change"]>0 else 1 if x["change"]<0 else 0)
    return {"status":"connected" if items and not errors else "partial" if items else "error","checked_at":now(),"items":items,"errors":errors,"bias":"Potential headwind for gold" if pts<0 else "Potential support for gold" if pts>0 else "Mixed / no directional edge","message":"Daily macro context only, not a standalone trade signal."}

@app.get("/api/calendar")
async def calendar():
    """Free FinanceCalendar feed, normalised to UK local time with visible attribution."""
    try:
        uk = ZoneInfo("Europe/London")
        current = datetime.now(timezone.utc)
        start_date = current.astimezone(uk).date()
        end_date = start_date + timedelta(days=7)
        payload = await get_json(
            "https://www.financecalendar.com/wp-json/fc/v1/calendar",
            {"from": start_date.isoformat(), "to": end_date.isoformat(), "limit": 500},
        )
        if isinstance(payload, list):
            raw_events = payload
            attribution = {}
        elif isinstance(payload, dict):
            raw_events = payload.get("events") or payload.get("items") or payload.get("data") or []
            if isinstance(raw_events, dict):
                raw_events = raw_events.get("events") or raw_events.get("items") or []
            attribution = payload.get("attribution") or {}
        else:
            raw_events, attribution = [], {}
        out = []
        excluded_title_terms = ("is the stock market open", "stock market open on", "market hours", "exchange hours", "trading hours", "market holiday")
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            title_text = str(event.get("title") or event.get("name") or event.get("event") or "Economic event")
            if any(term in title_text.lower() for term in excluded_title_terms):
                continue
            impact_text = str(event.get("impact") or "unspecified").lower()
            if impact_text == "low":
                continue
            raw_time = event.get("time_utc") or event.get("datetime") or event.get("timestamp") or event.get("date_time")
            dt = None
            if raw_time:
                try:
                    dt = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(timezone.utc)
                except (ValueError, TypeError):
                    dt = None
            all_day = bool(event.get("all_day", False))
            if dt and (dt < current - timedelta(minutes=5) or dt > current + timedelta(days=8)):
                continue
            if not dt and not event.get("date"):
                continue
            uk_dt = dt.astimezone(uk) if dt else None
            out.append({
                "title": title_text,
                "date": (uk_dt.date().isoformat() if uk_dt else str(event.get("date", ""))),
                "time_uk": (uk_dt.strftime("%a %d %b, %H:%M") if uk_dt and not all_day else "All day / time not supplied"),
                "time_uk_iso": uk_dt.isoformat() if uk_dt else None,
                "currency": str(event.get("currency") or event.get("currency_code") or event.get("country") or event.get("country_code") or ""),
                "impact": impact_text,
                "forecast": event.get("consensus") or event.get("forecast"),
                "previous": event.get("prior") or event.get("previous"),
                "actual": event.get("actual"),
                "url": event.get("url"),
                "all_day": all_day,
            })
        out.sort(key=lambda item: item.get("time_uk_iso") or item.get("date") or "9999")
        return {
            "status": "connected" if out else "connected_no_events",
            "checked_at": now(),
            "message": f"FinanceCalendar checked. {len(out)} medium/high or unclassified economic events returned for the next 7 days; exchange-hours notices and low-impact items are hidden. Times are UK local time.",
            "items": out[:100],
            "attribution": {
                "source": attribution.get("source") or "FinanceCalendar",
                "url": "https://www.financecalendar.com",
                "terms": attribution.get("terms") or "Free for use with visible attribution to FinanceCalendar.",
            },
        }
    except Exception as e:
        return {"status": "error", "checked_at": now(), "message": "Economic calendar unavailable: " + str(e)[:180], "items": [], "attribution": {"source": "FinanceCalendar", "url": "https://www.financecalendar.com"}}


@app.get("/api/news")
async def news():
    """Fetch fresh gold-relevant context from Finnhub general and forex headlines."""
    if not FINNHUB:
        return {"status":"not_configured","message":"Optional FINNHUB_API_KEY enables market headlines.","items":[]}
    current = datetime.now(timezone.utc)
    # Two broad categories improve coverage without weakening the 48-hour freshness rule.
    # If one category fails, keep usable results from the other and report partial status.
    terms = (
        "gold", "bullion", "precious metal", "silver", "fed", "federal reserve",
        "fomc", "interest rate", "rate cut", "rate hike", "monetary policy",
        "inflation", "treasury", "real yield", "bond yield", "bond market", "dollar",
        "usd", "employment", "payroll", "nonfarm", "jobless claims", "cpi", "pce",
        "tariff", "trade war", "geopolit", "central bank", "oil", "crude", "energy prices",
        "recession", "gdp", "china", "safe haven", "risk aversion", "risk-off", "currency"
    )
    out = []
    seen = set()
    errors = []
    for category in ("general", "forex"):
        try:
            records = await get_json(
                "https://finnhub.io/api/v1/news",
                {"category": category, "token": FINNHUB},
            )
            if not isinstance(records, list):
                errors.append(category + ": provider returned an unexpected response")
                continue
            for n in records:
                if not isinstance(n, dict):
                    continue
                title = str(n.get("headline", "")).strip()
                summary = str(n.get("summary", "")).strip()
                try:
                    published = datetime.fromtimestamp(int(n["datetime"]), timezone.utc) if n.get("datetime") else None
                except (TypeError, ValueError, OverflowError):
                    published = None
                # Never present stale stories as current; retain only the most recent 48 hours.
                if published is None or published < current - timedelta(hours=48) or published > current + timedelta(minutes=10):
                    continue
                combined = (title + " " + summary).lower()
                key = (title.lower(), str(n.get("url") or ""))
                if not title or key in seen or not any(term in combined for term in terms):
                    continue
                seen.add(key)
                out.append({
                    "headline": title,
                    "summary": summary[:250],
                    "source": n.get("source"),
                    "url": n.get("url"),
                    "category": category,
                    "published_at": published.isoformat(),
                    "age_hours": round((current - published).total_seconds() / 3600, 1),
                })
        except Exception as e:
            errors.append(category + ": " + str(e)[:120])
    out.sort(key=lambda item: item["published_at"], reverse=True)
    status = "connected" if out and not errors else "partial" if out else "error" if errors else "connected_no_fresh_headlines"
    if out:
        message = f"{len(out)} gold-relevant headlines from general and forex news, published within the last 48 hours. Headlines are context, not verified directional signals."
        if errors:
            message += " Some provider categories were unavailable."
    elif errors:
        message = "News provider could not return usable headlines. Try refreshing later."
    else:
        message = "No gold-relevant headlines were published in the last 48 hours. This is preferable to showing stale stories as current."
    return {"status": status, "checked_at": now(), "message": message, "items": out[:8], "errors": errors}

@app.post("/api/markets/{market}/candles/import-csv")
async def import_csv(market:Market,file:UploadFile=File(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"):raise HTTPException(400,"Upload a .csv file.")
    raw=await file.read()
    if len(raw)>20_000_000:raise HTTPException(413,"CSV must be under 20 MB.")
    try:
        reader=csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if not reader.fieldnames or not {"timestamp","open","high","low","close"}.issubset(reader.fieldnames):raise ValueError("CSV needs timestamp,open,high,low,close headers; volume optional.")
        rows=[]
        for n,r in enumerate(reader,start=2):
            datetime.fromisoformat(r["timestamp"].strip().replace("Z","+00:00"))
            o,h,l,c=(float(r[k]) for k in ("open","high","low","close"))
            if min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c,h):raise ValueError(f"Invalid OHLC at row {n}")
            rows.append({"timestamp":r["timestamp"],"open":o,"high":h,"low":l,"close":c,"volume":float(r["volume"]) if r.get("volume","").strip() else None})
        if not rows:raise ValueError("CSV contains no candle rows.")
    except Exception as e:raise HTTPException(400,str(e))
    IMPORTED[market.value]=rows;CACHE.clear()
    return {"market":market.value,"imported":len(rows),"data_status":"historical","is_live_data":False,"message":"Historical import only; not live."}

HTML=r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080d16"><title>AlfieFX Gold Intelligence</title>
<style>
:root{color-scheme:dark;--bg:#080d16;--panel:#111b2a;--panel2:#0c1421;--line:#263449;--text:#f1f5fb;--muted:#91a1b8;--gold:#d4ae59;--green:#39d6a0;--red:#ff737d;--blue:#8dbbff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#111a29 0%,var(--bg) 48%);color:var(--text);font:15px/1.5 system-ui,-apple-system,sans-serif;max-width:1400px;margin:auto;padding:20px}header{display:flex;align-items:center;gap:12px;padding:5px 0 18px}.logo{width:46px;height:46px;display:grid;place-items:center;background:var(--gold);color:#080d16;border-radius:13px;font-size:25px;font-weight:900}.brand{font-size:20px;font-weight:900;letter-spacing:.04em}.subbrand{font-size:11px;letter-spacing:.18em;color:var(--muted)}.spacer{flex:1}.pill{border:1px solid var(--line);border-radius:99px;padding:6px 10px;color:var(--muted);font-size:12px}.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--gold);margin-right:6px}.hero{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:10px 0 18px}.eyebrow{color:var(--gold);font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}h1{font-size:clamp(25px,4vw,36px);line-height:1.12;margin:5px 0 8px}h2{font-size:18px;margin:0 0 12px}h3{font-size:14px;margin:0 0 8px}.muted,small{color:var(--muted)}.hero p{margin:0;max-width:740px;color:var(--muted)}.toolbar{display:flex;flex-wrap:wrap;gap:9px;align-items:center}select,button{background:#172437;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:11px 13px;font:inherit}button{cursor:pointer}button:hover,.tab:hover{border-color:var(--gold)}.primary{background:var(--gold);color:#11151d;border-color:var(--gold);font-weight:800}.tabs{display:flex;gap:6px;overflow-x:auto;padding:7px;background:#0c1420;border:1px solid var(--line);border-radius:14px;margin:16px 0 13px;position:sticky;top:0;z-index:10}.tab{flex:0 0 auto;border:1px solid transparent;background:transparent;padding:10px 13px;border-radius:9px;color:var(--muted)}.tab.active{background:#1b293b;color:var(--text);border-color:#34455e}.page{display:none}.page.active{display:block}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.three{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.card{background:linear-gradient(145deg,var(--panel),#0f1724);border:1px solid var(--line);border-radius:16px;padding:16px;min-width:0;margin-bottom:12px;box-shadow:0 8px 24px #00000014}.card-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px}.label{font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--muted)}.big{font-size:clamp(20px,2.5vw,29px);font-weight:850;margin:7px 0;overflow-wrap:anywhere}.smallbig{font-size:18px;font-weight:800}.green{color:var(--green)}.red{color:var(--red)}.gold{color:var(--gold)}.blue{color:var(--blue)}.notice{background:#211d15;border:1px solid #51432a;color:#e8d8b5;padding:12px 14px;border-radius:12px;margin:12px 0}.notice.ok{background:#0e211e;border-color:#1e594a;color:#b8f5df}.notice.bad{background:#25161c;border-color:#63313e;color:#ffc1c6}.row{padding:11px 0;border-bottom:1px solid var(--line)}.row:last-child{border:0}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.metric{background:var(--panel2);border:1px solid #1e2c40;border-radius:11px;padding:11px;min-width:0}.metric b{display:block;margin-top:4px;overflow-wrap:anywhere}.chart-wrap{height:300px;position:relative;background:#0b1320;border:1px solid #1e2c40;border-radius:12px;overflow:hidden}.chart-wrap canvas{width:100%;height:100%;display:block}.feed-status{display:inline-flex;align-items:center;gap:7px;font-size:12px;border:1px solid var(--line);border-radius:99px;padding:5px 8px;color:var(--muted)}.feed-status:before{content:'';width:7px;height:7px;background:var(--gold);border-radius:50%}.feed-status.good:before{background:var(--green)}.feed-status.bad:before{background:var(--red)}.news-item{padding:12px 0;border-bottom:1px solid var(--line)}.news-item:last-child{border:0}.news-item a{color:var(--text);text-decoration:none;font-weight:700}.news-item a:hover{color:var(--gold)}.news-meta{font-size:12px;color:var(--muted);margin-top:5px}.list-empty{padding:14px;background:var(--panel2);border-radius:10px;color:var(--muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;color:#c8d6e8;max-height:380px;overflow:auto}.footer{padding:18px 0;color:var(--muted);font-size:12px}.tag{font-size:11px;border:1px solid var(--line);border-radius:7px;padding:4px 7px;color:var(--muted)}a{color:var(--blue)}@media(max-width:800px){body{padding:13px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.three{grid-template-columns:1fr}.hero{align-items:start;flex-direction:column}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-wrap{height:250px}}@media(max-width:520px){.two{grid-template-columns:1fr}.card{padding:13px}.brand{font-size:17px}.tabs{top:0}.tab{padding:9px 11px}.pill{font-size:11px}}
</style></head><body>
<header><div class="logo">◈</div><div><div class="brand">ALFIEFX</div><div class="subbrand">GOLD INTELLIGENCE</div></div><div class="spacer"></div><div class="pill"><span class="live-dot"></span><span id="overallState">CONNECTING</span></div></header>
<div class="hero"><div><div class="eyebrow">Market intelligence terminal</div><h1>Gold, understood from every angle.</h1><p>Market structure, macroeconomics and news in one workspace. Provider timestamps and data status are shown honestly; analysis is not an execution service.</p></div><div class="toolbar"><select id="market"><option value="XAUUSD">XAU/USD · Spot gold</option><option value="MGC">MGC · Micro Gold futures</option></select><button class="primary" onclick="refreshAll()">↻ Refresh data</button></div></div>
<div id="notice" class="notice">Checking data providers…</div>
<nav class="tabs" aria-label="Main sections"><button class="tab active" data-page="overview">Overview</button><button class="tab" data-page="macro">Macro &amp; fundamentals</button><button class="tab" data-page="news">News &amp; calendar</button><button class="tab" data-page="technical">Technical analysis</button><button class="tab" data-page="ideas">Trade ideas</button><button class="tab" data-page="data">Gold futures &amp; data</button></nav>
<section class="page active" id="page-overview"><div class="grid"><div class="card"><div class="label">MARKET FEED</div><div id="feed" class="big gold">Checking</div><small id="feedsub">Checking provider</small></div><div class="card"><div class="label">LATEST PRICE</div><div id="price" class="big">—</div><small id="priceat">Last candle —</small></div><div class="card"><div class="label">TECHNICAL READ</div><div id="signal" class="big gold">WAIT</div><small id="signalsub">Waiting for candles</small></div><div class="card"><div class="label">EXECUTION</div><div class="big green">Disabled</div><small>Research and analysis only</small></div></div>
<div class="two"><section class="card"><div class="card-head"><h2>Gold price action</h2><span id="chartStatus" class="feed-status">Waiting for data</span></div><div class="chart-wrap"><canvas id="priceChart" aria-label="Candlestick chart"></canvas></div><p class="muted">Candles are drawn only from the selected provider or imported history. No demo prices are invented.</p></section><section class="card"><div class="card-head"><h2>Market snapshot</h2><span class="tag" id="snapshotAt">Not updated</span></div><div class="row"><small>4H DIRECTIONAL CONTEXT</small><div id="t4" class="smallbig">Awaiting candles</div></div><div class="row"><small>1H TREND CONTEXT</small><div id="t1" class="smallbig">Awaiting candles</div></div><div class="row"><small>15M SETUP CONTEXT</small><div id="t15" class="smallbig">Awaiting candles</div></div><div class="row"><small>5M ENTRY SCREEN</small><div id="t5" class="smallbig">Awaiting candles</div></div><div class="row"><small>MACRO BACKDROP</small><div id="macroShort" class="smallbig">Awaiting macro feed</div></div></section></div>
<div class="three"><section class="card"><div class="label">MACRO</div><h2>Fundamental backdrop</h2><div id="macroCard" class="muted">Checking macro provider…</div><button onclick="showPage('macro')">Open macro desk →</button></section><section class="card"><div class="label">NEWS</div><h2>Latest gold-relevant headlines</h2><div id="newsCard" class="muted">Checking news provider…</div><button onclick="showPage('news')">Open news room →</button></section><section class="card"><div class="label">SETUP ENGINE</div><h2>Current technical screen</h2><div class="metrics"><div class="metric"><small>Score</small><b id="score">—</b></div><div class="metric"><small>Trend</small><b id="trend">—</b></div><div class="metric"><small>Volume</small><b id="vol">—</b></div><div class="metric"><small>Feed</small><b id="isLive">—</b></div></div><p class="muted">This is an initial candle screen, not yet the full validated multi-timeframe strategy.</p><button onclick="showPage('ideas')">Review trade ideas →</button></section></div></section>
<section class="page" id="page-macro"><div class="eyebrow">Dedicated research desk</div><h1>Macro &amp; fundamentals</h1><p class="muted">Track the forces that can move gold. Official daily series are context, not a real-time economic calendar.</p><div class="grid"><div class="card"><div class="label">CURRENT BIAS</div><div id="mbias" class="big">Unknown</div><small id="mstatus">Feed status not checked</small></div><div class="card"><div class="label">US DOLLAR</div><div id="usdValue" class="big">—</div><small id="usdDate">Awaiting data</small></div><div class="card"><div class="label">10Y REAL YIELD</div><div id="yieldValue" class="big">—</div><small id="yieldDate">Awaiting data</small></div><div class="card"><div class="label">DATA FREQUENCY</div><div class="big">Daily</div><small>FRED series are not intraday releases</small></div></div><section class="card"><div class="card-head"><h2>Macro drivers</h2><button onclick="loadMacro()">Refresh macro</button></div><p id="mmsg" class="muted">Macro data provider is not configured.</p><div id="mi"></div></section><div class="two"><section class="card"><h2>How to read the backdrop</h2><div class="row"><b>US dollar strength</b><p class="muted">A rising dollar can pressure dollar-priced gold, all else equal. It is not a standalone entry signal.</p></div><div class="row"><b>Real yields</b><p class="muted">Higher real yields can increase the opportunity cost of holding non-yielding gold.</p></div><div class="row"><b>Fed, inflation and employment</b><p class="muted">Policy expectations and surprises can move yields and the dollar quickly; scheduled-release data needs a dedicated calendar source.</p></div></section><section class="card"><h2>Interpretation rules</h2><p class="muted">The dashboard should state the source, observation date, latest value and prior value. Daily macro data must never be presented as live tick data. A directional bias is contextual and can conflict with short-term price action.</p></section></div></section>
<section class="page" id="page-news"><div class="eyebrow">Dedicated news desk</div><h1>News &amp; economic calendar</h1><p class="muted">Source-linked headlines and scheduled economic releases. Event times are converted to UK local time; unavailable forecast or actual values are left blank.</p><div class="two"><section class="card"><div class="card-head"><h2>Gold-relevant headlines</h2><button onclick="loadNews()">Refresh headlines</button></div><p id="nmsg" class="muted">Checking headlines provider…</p><div id="ni"></div></section><section class="card"><div class="card-head"><h2>Upcoming economic events</h2><button onclick="loadCalendar()">Refresh calendar</button></div><p id="calmsg" class="muted">Checking FinanceCalendar…</p><div id="calendarItems"></div><p class="muted">Source: <a href="https://www.financecalendar.com" target="_blank" rel="noopener noreferrer">FinanceCalendar</a>. Impact labels are supplied by the source; they are not a prediction of gold direction.</p></section></div></section>
<section class="page" id="page-technical"><div class="eyebrow">Structure &amp; confluence</div><h1>Technical analysis</h1><p class="muted">The current code is a first-pass candle screen. The full AlfieFX AOI, liquidity and confluence engine still needs implementing and validating.</p><div class="two"><section class="card"><div class="card-head"><h2>Price chart</h2><button onclick="loadCandles()">Reload candles</button></div><div class="chart-wrap"><canvas id="technicalChart" aria-label="Technical candlestick chart"></canvas></div><div class="toolbar"><span class="tag">4H bias</span><span class="tag">1H zones</span><span class="tag">15M AOI</span><span class="tag">5M confirmation</span></div></section><section class="card"><h2>Multi-timeframe status</h2><div class="row"><b>4H · Directional bias</b><div id="tech4" class="muted">Awaiting data</div></div><div class="row"><b>1H · Zones and structure</b><div id="tech1" class="muted">Awaiting data</div></div><div class="row"><b>15M · Area of interest</b><div id="tech15" class="muted">Awaiting data</div></div><div class="row"><b>5M · Entry confirmation</b><div id="tech5" class="muted">Awaiting data</div></div></section></div><div class="three"><section class="card"><h2>Liquidity</h2><p class="muted">Sweep/reclaim and inducement logic: partial first-pass checks only. Swing liquidity pools and full state tracking are not yet implemented.</p></section><section class="card"><h2>Imbalances &amp; zones</h2><p class="muted">FVGs, order-block quality, zone clustering and HTF AOI mapping are planned engine components, not currently confirmed features.</p></section><section class="card"><h2>Premium / discount</h2><p class="muted">4H dealing range and discount/premium alignment need to be added to the full strategy before this can be scored.</p></section></div></section>
<section class="page" id="page-ideas"><div class="eyebrow">Decision support · no order execution</div><h1>Trade ideas &amp; management</h1><p class="muted">Candidates are analysis outputs, not recommendations or a probability of winning. Confirm data freshness, spread, scheduled news and risk limits independently.</p><div class="grid"><div class="card"><div class="label">CURRENT SCREEN</div><div id="ideaSignal" class="big gold">WAIT</div><small id="ideaSub">Waiting for provider data</small></div><div class="card"><div class="label">SCREEN SCORE</div><div id="ideaScore" class="big">—</div><small>Raw conditions matched, not win probability</small></div><div class="card"><div class="label">ENTRY REFERENCE</div><div id="entry" class="big">—</div><small>Only if candidate conditions exist</small></div><div class="card"><div class="label">STOP REFERENCE</div><div id="sl" class="big">—</div><small>Preliminary calculation</small></div></div><div class="two"><section class="card"><h2>Targets &amp; rationale</h2><div class="row"><b>TP1 / TP2 reference</b><div id="tp" class="smallbig">—</div></div><div class="row"><b>Conditions detected</b><ul id="reasons" class="muted"><li>Waiting for candles.</li></ul></div><div class="notice">Do not use these first-pass references as live instructions. The full swing setup state machine and validation are not complete.</div></section><section class="card"><h2>Trade management status</h2><div class="notice">No broker/order connection. Open positions cannot be detected automatically from this website.</div><p class="muted">Trade monitoring can be added through an authorised read-only account integration or manual position entry. Actual order execution remains disabled by design.</p><div class="row"><b>Risk/reward framework</b><p class="muted">Targets are currently illustrative 2R and 3R references from a simple ATR-based stop, not zone-based trade management.</p></div></section></div></section>
<section class="page" id="page-data"><div class="eyebrow">Feed connections &amp; diagnostics</div><h1>Gold futures &amp; data</h1><p class="muted">Spot gold and exchange-traded futures are different instruments. Each feed must be independently connected and timestamped.</p><div class="two"><section class="card"><h2>XAU/USD · Spot gold</h2><div id="spotStatus" class="big gold">Checking</div><p id="spotMessage" class="muted">Provider status is loading.</p><div class="row"><b>Provider</b><div>Twelve Data, subject to symbol and plan entitlement</div></div><div class="row"><b>Required configuration</b><code>TWELVE_DATA_API_KEY</code> in Render environment variables</div></section><section class="card"><h2>MGC · Micro Gold futures</h2><div id="mgcStatus" class="big gold">Not connected</div><p id="mgcMessage" class="muted">Requires an authorised CME futures data provider and entitlement.</p><div class="row"><b>Important</b><p class="muted">Spot XAU/USD is not a substitute for MGC futures prices or contract data.</p></div></section></div><div class="three"><section class="card"><h2>Macro provider</h2><div id="fredStatus" class="smallbig">Checking</div><p class="muted">FRED key enables daily USD and 10-year real-yield context.</p></section><section class="card"><h2>News provider</h2><div id="newsStatus" class="smallbig">Checking</div><p class="muted">Finnhub general headlines are optional and subject to provider plan limits.</p></section><section class="card"><h2>Execution</h2><div class="smallbig green">Disabled</div><p class="muted">No orders are sent by this app.</p></section></div><section class="card"><div class="card-head"><h2>Diagnostics</h2><button onclick="refreshAll()">Refresh all status</button></div><pre id="debug">Waiting…</pre><p class="muted">Endpoints: <a href="/health">/health</a> · <a href="/api/markets/status">/api/markets/status</a> · <a href="/api/signals/XAUUSD">/api/signals/XAUUSD</a> · <a href="/api/macro">/api/macro</a> · <a href="/api/news">/api/news</a> · <a href="/api/calendar">/api/calendar</a></p></section></section>
<div class="footer">AlfieFX Gold Intelligence · design and navigation preview · feeds depend on configured providers · no automatic order execution · signal scores are not win probabilities.</div>
<script>
const $=id=>document.getElementById(id);let lastCandles=[];const fmt=x=>x==null||!Number.isFinite(Number(x))?'—':Number(x).toLocaleString('en-GB',{maximumFractionDigits:3});const datefmt=x=>{if(!x)return'—';let d=new Date(x);return Number.isNaN(d.getTime())?x:d.toLocaleString('en-GB',{dateStyle:'medium',timeStyle:'short',timeZone:'UTC'})+' UTC'};async function api(u){let r=await fetch(u,{cache:'no-store'}),j=await r.json();if(!r.ok)throw Error(j.detail||j.message||r.statusText);return j}
function showPage(name){document.querySelectorAll('.page').forEach(x=>x.classList.toggle('active',x.id==='page-'+name));document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.page===name));if(name==='technical')drawChart('technicalChart',lastCandles);window.scrollTo({top:0,behavior:'smooth'})}document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>showPage(b.dataset.page)));
function drawChart(id,rows){let c=$(id);if(!c)return;let rect=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1;c.width=Math.max(300,Math.floor(rect.width*dpr));c.height=Math.max(200,Math.floor(rect.height*dpr));let ctx=c.getContext('2d');ctx.scale(dpr,dpr);let w=c.width/dpr,h=c.height/dpr;ctx.fillStyle='#0b1320';ctx.fillRect(0,0,w,h);if(!rows||rows.length<2){ctx.fillStyle='#91a1b8';ctx.font='13px system-ui';ctx.fillText('Candles appear when a market data feed is connected.',16,28);return}rows=rows.slice(-70);let lo=Math.min(...rows.map(x=>x.low)),hi=Math.max(...rows.map(x=>x.high)),pad=(hi-lo)*.08||1;lo-=pad;hi+=pad;let top=16,bottom=h-25,usable=bottom-top;ctx.strokeStyle='#263449';ctx.lineWidth=1;for(let i=0;i<5;i++){let y=top+i*usable/4;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}ctx.font='10px system-ui';ctx.fillStyle='#91a1b8';ctx.fillText(fmt(hi),6,12);ctx.fillText(fmt(lo),6,h-6);let step=w/rows.length,cw=Math.max(2,step*.52);rows.forEach((r,i)=>{let x=i*step+step/2,yo=top+(hi-r.open)/(hi-lo)*usable,yc=top+(hi-r.close)/(hi-lo)*usable,yh=top+(hi-r.high)/(hi-lo)*usable,yl=top+(hi-r.low)/(hi-lo)*usable;let col=r.close>=r.open?'#39d6a0':'#ff737d';ctx.strokeStyle=col;ctx.fillStyle=col;ctx.beginPath();ctx.moveTo(x,yh);ctx.lineTo(x,yl);ctx.stroke();ctx.fillRect(x-cw/2,Math.min(yo,yc),cw,Math.max(1,Math.abs(yc-yo)))})}
async function status(){let s=await api('/api/markets/status'),m=s.markets[$('market').value];let connected=m.data_status==='provider_connected';$('feed').textContent=connected?'Connected':m.data_status==='historical_imported'?'Historical only':m.data_status==='provider_error'?'Provider error':'Not connected';$('feed').className='big '+(connected?'green':m.data_status==='provider_error'?'red':'gold');$('feedsub').textContent=m.message;$('overallState').textContent=connected?'PROVIDER CONNECTED':'DATA CHECK';$('spotStatus').textContent=s.markets.XAUUSD.data_status;$('spotMessage').textContent=s.markets.XAUUSD.message;$('mgcStatus').textContent=s.markets.MGC.data_status;$('mgcMessage').textContent=s.markets.MGC.message;$('fredStatus').textContent=s.macro_configured?'API key configured':'Not configured';$('newsStatus').textContent=s.news_configured?'API key configured':'Not configured';$('debug').textContent=JSON.stringify(s,null,2);return s}
async function signal(){try{let s=await api('/api/signals/'+$('market').value);$('price').textContent=fmt(s.last_price);$('priceat').textContent='Last candle: '+datefmt(s.last_candle_at);$('signal').textContent=s.signal||'WAIT';$('signal').className='big '+(s.signal==='BUY CANDIDATE'?'green':s.signal==='SELL CANDIDATE'?'red':'gold');$('signalsub').textContent=s.is_live_data?'Provider data · '+(s.warning||''):s.data_status==='historical'?'Historical only · not live':'No live feed · '+(s.warning||'');$('score').textContent=(s.signal_score??'—')+'/'+(s.score_max??4);$('trend').textContent=s.trend||'—';$('vol').textContent=s.volume_above_recent_average==null?'Unavailable':s.volume_above_recent_average?'Above average':'Not above average';$('isLive').textContent=s.is_live_data?'Provider':'Not live';$('entry').textContent=fmt(s.entry_reference);$('sl').textContent=fmt(s.stop_reference);$('tp').textContent=fmt(s.target_1_reference)+' / '+fmt(s.target_2_reference);$('ideaSignal').textContent=s.signal||'WAIT';$('ideaSignal').className='big '+(s.signal==='BUY CANDIDATE'?'green':s.signal==='SELL CANDIDATE'?'red':'gold');$('ideaSub').textContent=s.is_live_data?'Provider candles received':'No confirmed live feed';$('ideaScore').textContent=(s.signal_score??'—')+'/'+(s.score_max??4);$('reasons').replaceChildren();(s.reasons||[]).forEach(x=>{let li=document.createElement('li');li.textContent=x;$('reasons').appendChild(li)});let t=s.timeframes||{};[['4H','t4','tech4'],['1H','t1','tech1'],['15M','t15','tech15']].forEach(([tf,a,b])=>{let v=t[tf]?.trend||'Unavailable';$(a).textContent=v;$(b).textContent=v});$('t5').textContent=s.trend||'Unavailable';$('tech5').textContent=s.trend||'Unavailable';$('snapshotAt').textContent='Checked '+new Date().toLocaleTimeString('en-GB');$('chartStatus').textContent=s.is_live_data?'Provider candles':'Feed unavailable';$('chartStatus').className='feed-status '+(s.is_live_data?'good':'bad');$('notice').className='notice '+(s.is_live_data?'ok':'');$('notice').textContent=s.is_live_data?'Provider candles received. Last candle: '+datefmt(s.last_candle_at)+'. Verify provider entitlement and freshness.':'Market data is not confirmed live. '+(s.warning||'Configure a suitable data provider before relying on any setup.');$('debug').textContent=JSON.stringify(s,null,2);return s}catch(e){$('signal').textContent='WAIT';$('ideaSignal').textContent='WAIT';$('signalsub').textContent='Data unavailable';$('notice').className='notice bad';$('notice').textContent='Cannot calculate a confirmed live setup: '+e.message;$('debug').textContent=e.message;throw e}}
async function loadCandles(){try{let d=await api('/api/markets/'+$('market').value+'/candles?interval=5min&limit=120');lastCandles=d.candles||[];drawChart('priceChart',lastCandles);drawChart('technicalChart',lastCandles);$('chartStatus').textContent=d.is_live_data?'Provider candles · '+datefmt(d.candles.at(-1)?.timestamp):'Not live';}catch(e){lastCandles=[];drawChart('priceChart',[]);drawChart('technicalChart',[]);$('chartStatus').textContent='No candles';}}
async function loadMacro(){try{let x=await api('/api/macro');$('mbias').textContent=x.bias||x.status;$('macroShort').textContent=x.bias||x.status;$('macroCard').textContent=x.message||x.status;$('mmsg').textContent=x.message||'';$('mstatus').textContent=x.status||'Unknown';$('mi').replaceChildren();$('usdValue').textContent='—';$('yieldValue').textContent='—';(x.items||[]).forEach(i=>{let d=document.createElement('div');d.className='row';d.textContent=i.label+': '+fmt(i.value)+' · date '+(i.date||'—')+' · change '+fmt(i.change);$('mi').appendChild(d);if(i.series_id==='DTWEXBGS'){$('usdValue').textContent=fmt(i.value);$('usdDate').textContent='Observation date '+(i.date||'—')}if(i.series_id==='DFII10'){$('yieldValue').textContent=fmt(i.value)+'%';$('yieldDate').textContent='Observation date '+(i.date||'—')}});if(!x.items?.length)$('mi').innerHTML='<div class="list-empty">No macro observations available. Configure FRED_API_KEY to enable daily context.</div>'}catch(e){$('mmsg').textContent=e.message;$('macroCard').textContent='Macro feed unavailable';$('macroShort').textContent='Unavailable'}}
async function loadNews(){try{let x=await api('/api/news');$('nmsg').textContent=x.message||x.status;$('ni').replaceChildren();$('newsCard').textContent=x.items?.length?x.items[0].headline:(x.message||'No fresh headlines available');(x.items||[]).forEach(i=>{let d=document.createElement('div');d.className='news-item';let a=document.createElement('a');a.href=i.url||'#';a.target='_blank';a.rel='noopener noreferrer';a.textContent=i.headline||'Untitled headline';d.appendChild(a);let p=document.createElement('div');p.className='news-meta';let published='time unavailable';if(i.published_at){let dt=new Date(i.published_at);if(!Number.isNaN(dt.getTime()))published=dt.toLocaleString('en-GB',{timeZone:'Europe/London',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false})+' UK'}p.textContent=(i.source||'Source unavailable')+' - '+published+(i.age_hours!=null?' - '+i.age_hours+'h ago':'');d.appendChild(p);if(i.summary){let s=document.createElement('p');s.className='muted';s.textContent=i.summary;d.appendChild(s)}$('ni').appendChild(d)});if(!x.items?.length)$('ni').innerHTML='<div class="list-empty">No gold-relevant headlines published in the last 48 hours. This is preferable to showing stale stories as current.</div>'}catch(e){$('nmsg').textContent=e.message;$('newsCard').textContent='News feed unavailable'}}
async function loadCalendar(){try{let x=await api('/api/calendar');$('calmsg').textContent=x.message||x.status;$('calendarItems').replaceChildren();if(x.status==='error'){$('calendarItems').innerHTML='<div class="notice bad">Calendar feed unavailable. Try refreshing later.</div>';return}if(!x.items?.length){$('calendarItems').innerHTML='<div class="list-empty">No upcoming medium/high-impact economic events were returned for the selected window.</div>';return}(x.items||[]).forEach(i=>{let d=document.createElement('div');d.className='news-item';let title=document.createElement('b');title.textContent=i.title||'Economic event';d.appendChild(title);let meta=document.createElement('div');meta.className='news-meta';let bits=[i.time_uk||i.date||'Time not supplied'];if(i.currency)bits.push(i.currency);if(i.impact)bits.push('Impact: '+i.impact);meta.textContent=bits.join(' - ');d.appendChild(meta);let details=[];if(i.forecast!=null&&i.forecast!=='')details.push('Forecast: '+i.forecast);if(i.previous!=null&&i.previous!=='')details.push('Previous: '+i.previous);if(i.actual!=null&&i.actual!=='')details.push('Actual: '+i.actual);if(details.length){let p=document.createElement('p');p.className='muted';p.textContent=details.join(' - ');d.appendChild(p)}if(i.url){let a=document.createElement('a');a.href=i.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='View event source';d.appendChild(a)}$('calendarItems').appendChild(d)})}catch(e){$('calmsg').textContent='Calendar request failed: '+e.message;$('calendarItems').innerHTML='<div class="notice bad">Calendar feed unavailable. Try refreshing later.</div>'}}async function refreshAll(){try{await status()}catch(e){$('notice').className='notice bad';$('notice').textContent='Status check failed: '+e.message}try{await signal()}catch(e){}await Promise.all([loadMacro(),loadNews(),loadCalendar(),loadCandles()]);} $('market').addEventListener('change',refreshAll);window.addEventListener('resize',()=>{drawChart('priceChart',lastCandles);drawChart('technicalChart',lastCandles)});refreshAll();setInterval(refreshAll,60000);
</script></body></html>"""
he
