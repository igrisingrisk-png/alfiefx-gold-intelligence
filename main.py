import os, csv, io, statistics
from datetime import datetime, timezone
from enum import Enum
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse

app=FastAPI(title="AlfieFX Gold Intelligence",version="1.1.0")
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

@app.get("/api/news")
async def news():
    if not FINNHUB:return {"status":"not_configured","message":"Optional FINNHUB_API_KEY enables general market headlines.","items":[]}
    try:
        j=await get_json("https://finnhub.io/api/v1/news",{"category":"general","token":FINNHUB})
        terms=("gold","fed","federal reserve","inflation","treasury","yield","dollar","employment","payroll","cpi","pce","tariff","geopolit","central bank")
        out=[]
        for n in j:
            title=str(n.get("headline",""));summary=str(n.get("summary",""))
            if any(w in (title+" "+summary).lower() for w in terms):
                out.append({"headline":title,"summary":summary[:250],"source":n.get("source"),"url":n.get("url"),"published_at":datetime.fromtimestamp(int(n["datetime"]),timezone.utc).isoformat() if n.get("datetime") else None})
            if len(out)>=10:break
        return {"status":"connected","checked_at":now(),"message":"Filtered general market headlines; not verified directional signals.","items":out}
    except Exception as e:return {"status":"error","message":str(e)[:200],"items":[]}

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

HTML=r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a0e15"><title>AlfieFX Gold Intelligence</title>
<style>
:root{color-scheme:dark;--bg:#0a0e15;--panel:#111925;--line:#29364a;--text:#edf3fb;--muted:#91a1b8;--green:#39d6a0;--red:#ff737d;--amber:#f1bd58}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui;padding:15px;max-width:1200px;margin:auto}header{font-weight:850;font-size:22px;padding:8px 0 18px}small,.muted{color:var(--muted)}h1{font-size:25px}h2{font-size:18px;margin:0 0 12px}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:14px;margin:10px 0;min-width:0}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}select,button{background:#172232;color:white;border:1px solid var(--line);border-radius:8px;padding:11px;font:inherit}.big{font-size:21px;font-weight:850;margin:6px 0;overflow-wrap:anywhere}.green{color:var(--green)}.red{color:var(--red)}.amber{color:var(--amber)}.notice{background:#241f15;border:1px solid #59472b;color:#e1cda5;padding:11px;border-radius:9px;margin:10px 0}.row{padding:9px 0;border-bottom:1px solid var(--line)}.row:last-child{border:0}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.metric{background:#0d1420;padding:9px;border-radius:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;color:#c8d6e8;max-height:400px;overflow:auto}a{color:#8dbbff}.news{padding:9px 0;border-bottom:1px solid var(--line)}@media(max-width:650px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}body{padding:12px}}
</style></head><body>
<header>◈ AlfieFX Gold Intelligence<br><small>MACRO · SENTIMENT · DISCOUNT STRATEGY</small></header><h1>Gold market command centre</h1><p class="muted">Standalone website · provider data only when confirmed · no automatic orders.</p>
<div class="toolbar"><select id="market"><option value="XAUUSD">XAU/USD · Spot gold</option><option value="MGC">MGC · Micro Gold futures</option></select><button onclick="refreshAll()">Refresh analysis</button></div><div id="notice" class="notice">Checking feeds…</div>
<div class="grid"><div class="card"><small>MARKET DATA</small><div id="feed" class="big amber">Checking</div><small id="feedsub">Provider status</small></div><div class="card"><small>LATEST PRICE</small><div id="price" class="big">—</div><small id="priceat">Last candle —</small></div><div class="card"><small>TECHNICAL SETUP</small><div id="signal" class="big amber">WAIT</div><small id="signalsub">Waiting for data</small></div><div class="card"><small>EXECUTION</small><div class="big green">Disabled</div><small>Analysis only</small></div></div>
<div class="two"><section class="card"><h2>Signal engine · first pass</h2><p class="muted">5M technical screen with multi-timeframe candle context. Full AOI/confluence logic is still to be built and validated.</p><div class="metrics"><div class="metric"><small>Score</small><b id="score">—</b></div><div class="metric"><small>Trend</small><b id="trend">—</b></div><div class="metric"><small>Volume</small><b id="vol">—</b></div><div class="metric"><small>Data</small><b id="isLive">—</b></div></div><div class="row"><b>Entry reference</b><div id="entry" class="muted">—</div></div><div class="row"><b>Stop reference</b><div id="sl" class="muted">—</div></div><div class="row"><b>TP1 / TP2 reference</b><div id="tp" class="muted">—</div></div><div class="row"><b>Reasons</b><ul id="reasons" class="muted"><li>Waiting for candles.</li></ul></div><div class="notice">A candidate is not a guaranteed entry, forecast or validated profitable strategy. Check spread, news and your rules.</div></section>
<section class="card"><h2>Discount Strategy workflow</h2><div class="row"><b>4H bias</b><div id="t4" class="muted">Awaiting candles</div></div><div class="row"><b>1H zones / structure</b><div id="t1" class="muted">Awaiting candles</div></div><div class="row"><b>15M area of interest</b><div id="t15" class="muted">Awaiting candles</div></div><div class="row"><b>5M confirmation</b><div id="t5" class="muted">Awaiting candles</div></div><p class="muted">Current first pass checks trend, local sweep/reclaim, displacement and a 20-candle breakout. It does not yet implement the complete stateful entry engine.</p></section></div>
<div class="two"><section class="card"><h2>Macro bias · FRED</h2><div id="mbias" class="big">Awaiting configuration</div><p id="mmsg" class="muted">FRED key required for daily USD and real-yield context.</p><div id="mi"></div><button onclick="loadMacro()">Refresh macro</button></section><section class="card"><h2>Market news context</h2><p id="nmsg" class="muted">Optional headlines feed.</p><div id="ni"></div><button onclick="loadNews()">Refresh headlines</button></section></div>
<section class="card"><h2>Provider status / diagnostics</h2><pre id="debug">Waiting…</pre><p class="muted">XAU/USD uses Twelve Data when configured and permitted by your plan. MGC requires an authorised CME futures provider. FRED is daily macro data; headlines are context, not a signal. API keys belong in Render Environment Variables.</p><p class="muted">Endpoints: /health · /api/markets/status · /api/signals/XAUUSD · /api/macro · /api/news</p></section>
<p class="muted">AlfieFX Gold Intelligence v1.1 · no automatic order execution · signal score is not a win probability.</p>
<script>
const $=x=>document.getElementById(x);const fmt=x=>x==null||!Number.isFinite(Number(x))?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:3});async function api(u){let r=await fetch(u),j=await r.json();if(!r.ok)throw Error(j.detail||j.message||r.statusText);return j}
async function status(){let s=await api('/api/markets/status'),m=s.markets[$('market').value];$('feed').textContent=m.data_status==='provider_connected'?'Connected':m.data_status==='historical_imported'?'Historical only':m.data_status==='provider_error'?'Provider error':'Not connected';$('feed').className='big '+(m.data_status==='provider_connected'?'green':m.data_status==='historical_imported'?'':'amber');$('feedsub').textContent=m.message;$('debug').textContent=JSON.stringify(s,null,2)}
async function signal(){try{let s=await api('/api/signals/'+$('market').value);$('price').textContent=fmt(s.last_price);$('priceat').textContent='Candle: '+(s.last_candle_at||'—');$('signal').textContent=s.signal||'WAIT';$('signal').className='big '+(s.signal==='BUY CANDIDATE'?'green':s.signal==='SELL CANDIDATE'?'red':'amber');$('signalsub').textContent=s.warning||'';$('score').textContent=(s.signal_score??'—')+'/'+(s.score_max??4);$('trend').textContent=s.trend||'—';$('vol').textContent=s.volume_above_recent_average==null?'Unavailable':s.volume_above_recent_average?'Above average':'Not above average';$('isLive').textContent=s.is_live_data?'Provider':'Historical';$('entry').textContent=fmt(s.entry_reference);$('sl').textContent=fmt(s.stop_reference);$('tp').textContent=fmt(s.target_1_reference)+' / '+fmt(s.target_2_reference);$('reasons').innerHTML='';(s.reasons||[]).forEach(x=>{let li=document.createElement('li');li.textContent=x;$('reasons').appendChild(li)});let t=s.timeframes||{};$('t4').textContent=t['4H']?.trend||'Unavailable';$('t1').textContent=t['1H']?.trend||'Unavailable';$('t15').textContent=t['15M']?.trend||'Unavailable';$('t5').textContent=s.trend||'Unavailable';$('notice').textContent=s.is_live_data?'Provider candles received. Check last-candle timestamp and entitlement.':'Historical/imported only; no live signal claimed.';$('debug').textContent=JSON.stringify(s,null,2)}catch(e){$('signal').textContent='WAIT';$('signalsub').textContent='Data unavailable';$('notice').textContent='Cannot calculate a live setup: '+e.message;$('debug').textContent=e.message}}
async function loadMacro(){try{let x=await api('/api/macro');$('mbias').textContent=x.bias||x.status;$('mmsg').textContent=x.message||'';$('mi').innerHTML='';(x.items||[]).forEach(i=>{let d=document.createElement('div');d.className='row';d.textContent=i.label+': '+fmt(i.value)+' as of '+i.date+' · change '+fmt(i.change);$('mi').appendChild(d)})}catch(e){$('mmsg').textContent=e.message}}
async function loadNews(){try{let x=await api('/api/news');$('nmsg').textContent=x.message||x.status;$('ni').innerHTML='';(x.items||[]).forEach(i=>{let d=document.createElement('div');d.className='news';let a=document.createElement('a');a.href=i.url||'#';a.target='_blank';a.rel='noopener';a.textContent=i.headline;d.appendChild(a);let p=document.createElement('div');p.className='muted';p.textContent=(i.source||'Source')+' · '+(i.published_at||'time unavailable');d.appendChild(p);$('ni').appendChild(d)})}catch(e){$('nmsg').textContent=e.message}}
async function refreshAll(){try{await status();await signal()}catch(e){$('notice').textContent=e.message;$('debug').textContent=e.message}await loadMacro();await loadNews()}$('market').addEventListener('change',refreshAll);refreshAll();
</script></body></html>"""
