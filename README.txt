AlfieFX Gold Intelligence v1.1 upgrade

Replace main.py and requirements.txt in the existing GitHub repository with these files; keep render.yaml. Render redeploys after the commit.

Optional Render environment variables:
TWELVE_DATA_API_KEY = Twelve Data key for XAU/USD candles (check account symbol access/plan)
FRED_API_KEY = FRED key for daily broad USD index and 10-year real-yield context
FINNHUB_API_KEY = optional general market headlines

MGC intentionally remains unavailable until an authorised CME futures provider and entitlement are configured. Spot XAU/USD is not a substitute.

The first signal screen is a basic technical candle filter, not yet the full stateful multi-timeframe AOI/confluence strategy and not a validated profitable strategy. No order execution. CSV imports are historical only and in-memory.
