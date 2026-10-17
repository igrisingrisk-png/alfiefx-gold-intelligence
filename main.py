from enum import Enum
from datetime import datetime, timezone
from typing import Literal
import csv, io

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

app = FastAPI(title="AlfieFX Gold Intelligence", version="0.3.0")
IMPORTED = {}

class Market(str, Enum):
    XAUUSD = "XAUUSD"
    MGC = "MGC"

class MacroInputs(BaseModel):
    usd: int = Field(0, description="-1 bearish for gold, 0 neutral/unknown, +1 bullish")
    real_yields: int = 0
    fed_expectations: int = 0
    risk_sentiment: int = 0
    inflation_surprise: int = 0

    @field_validator("usd", "real_yields", "fed_expectations", "risk_sentiment", "inflation_surprise")
    @classmethod
    def valid_direction(cls, value):
        if value not in (-1, 0, 1):
            raise ValueError("Use -1, 0, or 1.")
        return value

class TechnicalInputs(BaseModel):
    htf_bias_aligned: bool = False
    price_at_quality_aoi: bool = False
    liquidity_sweep: bool = False
    displacement: bool = False
    structure_shift: bool = False
    retest_confirmed: bool = False
    volume_confirmation: bool = False
    risk_reward_at_least_2: bool = False

class ScoreRequest(BaseModel):
    market: Market
    data_status: Literal["manual", "demo", "live", "delayed", "historical"] = "manual"
    macro: MacroInputs
    technical: TechnicalInputs

def analyse(req):
    m = req.macro.model_dump()
    mw = {"usd":1.0,"real_yields":1.0,"fed_expectations":1.2,"risk_sentiment":0.8,"inflation_surprise":0.7}
    macro = round(sum(m[k]*w for k,w in mw.items())/sum(mw.values())*100)
    bias = "Bullish" if macro >= 20 else "Bearish" if macro <= -20 else "Mixed / neutral"
    t = req.technical.model_dump()
    tw = {"htf_bias_aligned":1.5,"price_at_quality_aoi":1.5,"liquidity_sweep":1.0,"displacement":1.0,"structure_shift":1.5,"retest_confirmed":1.0,"volume_confirmation":0.75,"risk_reward_at_least_2":0.75}
    tech = round(sum((1 if t[k] else 0)*w for k,w in tw.items())/sum(tw.values())*100)
    score = round((0.35*max(0,macro)+0.65*tech)/10,1)
    gates = [("htf_bias_aligned","Higher-timeframe bias is not aligned."),
             ("price_at_quality_aoi","Price is not confirmed at a quality area of interest."),
             ("structure_shift","No confirmed market-structure shift."),
             ("retest_confirmed","Retest confirmation is missing."),
             ("risk_reward_at_least_2","Risk/reward is below 2:1 or unknown.")]
    blockers = [msg for key,msg in gates if not getattr(req.technical,key)]
    ready = not blockers
    if req.data_status in ("demo","manual"):
        warning = "Demo/manual inputs only; this is not live market analysis."
    elif req.data_status == "historical":
        warning = "Imported historical data only; not a live signal."
    elif req.data_status == "delayed":
        warning = "Data is marked delayed; not a live signal."
    else:
        warning = "Live status must be independently verified against provider timestamps and entitlement."
    if ready and score >= 8 and req.data_status == "live":
        status = "Candidate — verify feed freshness and risk"
    elif ready and score >= 8:
        status = "Conditions align, but data is not verified live"
    else:
        status = "Wait — confirmation gates not satisfied"
    return {"market":req.market,"data_status":req.data_status,"is_live_data":req.data_status=="live",
            "macro_score":macro,"macro_bias":bias,"technical_score":tech,"technical_max":100,
            "setup_score":score,"setup_status":status,"blockers":blockers,
            "explanations":[f"Macro score {macro}/100; positive values lean supportive for gold.",
                            f"Technical checklist score {tech}/100; not a proven win probability.",
                            "The setup score is a heuristic, not a forecast."],"warning":warning}

@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>AlfieFX Gold Intelligence</title>
<style>
body{margin:0;background:#0a0e15;color:#edf3fb;font:15px system-ui;padding:18px}header{font-weight:800;font-size:21px;padding:10px 0 20px}small,.muted{color:#91a1b8}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card{background:#111925;border:1px solid #29364a;border-radius:12px;padding:14px;margin-bottom:12px}.big{font-size:21px;font-weight:800;margin:7px 0}select,button{background:#172232;color:white;border:1px solid #29364a;border-radius:8px;padding:10px;font:inherit;margin:4px 4px 4px 0}button{cursor:pointer}.amber{color:#f1bd58}.green{color:#39d6a0}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:#c8d6e8}.row{padding:9px 0;border-bottom:1px solid #29364a}.notice{background:#241f15;border:1px solid #59472b;color:#e1cda5;padding:11px;border-radius:8px;margin:10px 0;font-size:12px}@media(min-width:800px){body{max-width:1100px;margin:auto}.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
</style></head><body><header>◈ AlfieFX Gold Intelligence<br><small>MACRO · SENTIMENT · DISCOUNT STRATEGY</small></header>
<h2>Gold market command centre</h2><p class="muted">Standalone website · no TradingView indicator · no automatic orders.</p>
<select id="market"><option value="XAUUSD">XAU/USD · Spot gold</option><option value="MGC">MGC · Micro Gold futures</option></select><button onclick="refresh()">Refresh status</button>
<div id="notice" class="notice">Checking backend…</div><div class="grid">
<div class="card"><small>MARKET DATA</small><div id="feed" class="big amber">Not connected</div><small id="feedsub">No live provider configured</small></div>
<div class="card"><small>MACRO BIAS</small><div class="big">Awaiting inputs</div><small>USD · real yields · Fed · data</small></div>
<div class="card"><small>SETUP STATUS</small><div class="big amber">Wait</div><small>No live setup asserted</small></div>
<div class="card"><small>EXECUTION</small><div class="big green">Disabled</div><small>Analysis only</small></div></div>
<div class="card"><h3>Discount Strategy workflow</h3><div class="row"><b>4H bias</b><br><small>Macro + higher-timeframe structure</small></div><div class="row"><b>1H zones</b><br><small>Freshness, displacement and confluence</small></div><div class="row"><b>15M area of interest</b><br><small>Liquidity and premium/discount</small></div><div class="row"><b>5M confirmation</b><br><small>Sweep, displacement, structure shift, retest</small></div></div>
<div class="card"><h3>Backend analysis</h3><button onclick="demo()">Load labelled demo analysis</button><button onclick="candles()">Check candles</button><pre id="output">No analysis loaded yet.</pre></div>
<div class="card"><h3>Data connection status</h3><p class="muted">Live market feeds and real-time macro/news are not connected yet. Historical CSV imports are supported. Never treat demo data as live.</p><div id="details" class="muted"></div></div>
<script>
async function api(u){let r=await fetch(u);let j=await r.json();if(!r.ok)throw Error(j.detail||r.statusText);return j}
async function refresh(){try{let s=await api('/api/markets/status'),m=document.getElementById('market').value,x=s.markets[m];document.getElementById('feed').textContent=x.data_status==='historical_imported'?'Historical CSV':'Not connected';document.getElementById('feedsub').textContent=x.message;document.getElementById('notice').textContent='Backend online · live feeds not connected · order execution disabled.';document.getElementById('details').textContent=JSON.stringify(s,null,2)}catch(e){document.getElementById('notice').textContent='Backend error: '+e.message}}
async function demo(){try{document.getElementById('output').textContent=JSON.stringify(await api('/api/analysis/demo?market='+document.getElementById('market').value),null,2)}catch(e){document.getElementById('output').textContent=e.message}}
async function candles(){try{document.getElementById('output').textContent=JSON.stringify(await api('/api/markets/'+document.getElementById('market').value+'/candles'),null,2)}catch(e){document.getElementById('output').textContent=e.message}}
document.getElementById('market').onchange=refresh;refresh();
</script></body></html>"""

@app.get("/health")
def health():
    return {"status":"ok","live_data_connected":False,"order_execution":False}

@app.get("/api/markets/status")
def status():
    markets = {
      "XAUUSD":{"label":"XAU/USD spot gold","data_status":"unavailable","message":"Configure permitted spot provider or import historical CSV."},
      "MGC":{"label":"Micro Gold futures","data_status":"unavailable","message":"Configure entitled CME provider or import historical CSV."}}
    for symbol,rows in IMPORTED.items():
        markets[symbol].update(data_status="historical_imported",message=f"{len(rows)} imported historical candles; not live.")
    return {"provider":None,"connected":False,"status":"not_connected","checked_at":datetime.now(timezone.utc).isoformat(),"markets":markets}

@app.get("/api/markets/{market}/candles")
def candles(market:Market,limit:int=200):
    if not 1<=limit<=5000: raise HTTPException(400,"limit must be between 1 and 5000")
    rows=IMPORTED.get(market.value,[])
    return {"market":market.value,"data_status":"historical" if rows else "unavailable","is_live_data":False,"count":min(len(rows),limit),"candles":rows[-limit:]}

@app.post("/api/markets/{market}/candles/import-csv")
async def import_csv(market:Market,file:UploadFile=File(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"): raise HTTPException(400,"Upload a .csv file.")
    raw=await file.read()
    if len(raw)>20_000_000: raise HTTPException(413,"CSV must be smaller than 20 MB.")
    try:
        reader=csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        required={"timestamp","open","high","low","close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames): raise ValueError("CSV needs timestamp,open,high,low,close headers; volume optional.")
        rows=[]
        for n,r in enumerate(reader,start=2):
            try:
                datetime.fromisoformat(r["timestamp"].strip().replace("Z","+00:00"))
                o,h,l,c=(float(r[k]) for k in ("open","high","low","close"))
                v=float(r["volume"]) if r.get("volume","").strip() else None
                if min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c,h): raise ValueError("invalid OHLC bounds")
                rows.append({"timestamp":r["timestamp"],"open":o,"high":h,"low":l,"close":c,"volume":v})
            except Exception as e: raise ValueError(f"Invalid CSV row {n}: {e}")
        if not rows: raise ValueError("CSV contains no candle rows.")
    except (UnicodeDecodeError,ValueError) as e: raise HTTPException(400,str(e))
    IMPORTED[market.value]=rows
    return {"market":market.value,"imported":len(rows),"data_status":"historical","is_live_data":False,"message":"Historical import only; no live feed or trading enabled."}

@app.get("/api/analysis/demo")
def demo(market:Market=Market.XAUUSD):
    req=ScoreRequest(market=market,data_status="demo",macro=MacroInputs(usd=-1,real_yields=-1,fed_expectations=1,risk_sentiment=1,inflation_surprise=0),technical=TechnicalInputs(htf_bias_aligned=True,price_at_quality_aoi=True,liquidity_sweep=True,displacement=True,structure_shift=False,retest_confirmed=False,volume_confirmation=True,risk_reward_at_least_2=True))
    return analyse(req)

@app.post("/api/analysis/score")
def score(req:ScoreRequest): return analyse(req)
