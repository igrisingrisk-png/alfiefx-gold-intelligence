AlfieFX Gold Intelligence v1.2.0

This upgrade refreshes the responsive dashboard design and adds separate navigation tabs:
Overview, Macro & Fundamentals, News & Calendar, Technical Analysis, Trade Ideas, and Gold Futures & Data.
It preserves the existing FastAPI endpoints and adds a provider-fed candlestick chart plus a 60-second dashboard refresh.

Deployment via GitHub on iPhone:
1. Extract this ZIP in the Files app.
2. Open https://github.com/igrisingrisk-png/alfiefx-gold-intelligence/upload/main
3. Upload main.py, requirements.txt, README.txt, and render.yaml from this folder.
4. Commit directly to the main branch.
5. In Render, wait for the new deployment to become Live.

Do not remove or change environment variables if already configured.

Provider keys are optional and must be configured in Render > service > Environment:
- TWELVE_DATA_API_KEY: XAU/USD candles, subject to provider symbol/plan access
- FRED_API_KEY: daily macro context
- FINNHUB_API_KEY: general headlines, subject to provider plan limits
MGC live futures data is not connected by this code; it requires an authorised CME futures provider and entitlement.

Important limitations:
- This is a UI/navigation and data-display upgrade, not the completed full strategy engine.
- The technical screen is a first-pass candle-based candidate filter, not a validated profitable strategy or win probability.
- No automatic order execution is enabled.
- Macro series are daily and must not be presented as intraday data.
- Do not claim a feed is live unless the provider returns data and timestamps support that claim.
