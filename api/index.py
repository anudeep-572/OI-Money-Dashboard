"""
Option flow dashboard server -- Upstox version, all 5 major indices.
Deployed on Vercel as a Python serverless function (Flask via the
@vercel/python runtime). This file lives at api/index.py, which is
the entrypoint Vercel's Python runtime looks for; Vercel imports the
module-level `app` Flask instance directly rather than running
`app.run()` (the __main__ block at the bottom only matters for local
development).

Flow:
  1. Open the page
  2. Paste in today's Upstox access token (generated manually from
     https://account.upstox.com/developer/apps -- click your app,
     click Generate). Tokens expire daily, so this is a once-a-day
     paste, not a one-time setup step.
  3. Pick an index (NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY / SENSEX)
  4. Click Calculate
  5. Server fetches the live option chain from Upstox, then fetches
     intraday candles (concurrently, and only for the strikes that
     could plausibly rank in the top N) to compute a real VWAP
     (Upstox's option-chain response has no VWAP field), computes
     breakeven + money-flow ranking, and returns it to the page.

Real VWAP needs extra API calls beyond the single option-chain
fetch, but candle fetches happen concurrently and are pre-filtered
to only the likely top-ranking strikes, so this is much faster than
a naive "fetch every leg one at a time" approach. On Vercel's Fluid
Compute (default since 2025), this should comfortably fit inside the
60-second Hobby-tier function duration limit.

This code is already fully stateless -- the access token travels
with every request body rather than living in server memory, which
matches the serverless model with no changes needed (there's no
"server stays running between requests" assumption anywhere here).

Local development:
  1. pip install -r requirements.txt
  2. python api/index.py
  3. Open http://localhost:5000 in your browser
  4. Paste your access token, pick an index, click Calculate

Vercel deployment: see README.md for the full walkthrough.
"""
import os
import sys
from datetime import date, datetime

from flask import Flask, jsonify, render_template, request

# Ensure upstox_client.py (copied alongside this file in api/) is
# importable regardless of Vercel's working directory at runtime.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from upstox_client import (
    UpstoxClient,
    UpstoxApiError,
    INDEX_REGISTRY,
    get_nearest_expiry_and_lot_size,
    build_change_oi_lookup,
    build_change_oi_lookup_from_chain,
    summarize_fii_dii,
    compute_levels,
)

# Explicit template_folder pointing at the project-root templates/
# directory (one level up from api/), since Flask's default lookup
# (a "templates" folder next to this file) would otherwise look for
# api/templates/, which doesn't exist -- the actual templates/ folder
# lives at the project root alongside api/, not inside it.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=os.path.join(_PROJECT_ROOT, "templates"))


DEFAULT_TOP_N = 5
MAX_TOP_N = 20  # matches the old manual tool's slider ceiling


@app.route("/")
def index():
    indices = [{"key": k, "label": v["label"]} for k, v in INDEX_REGISTRY.items()]
    return render_template("index.html", indices=indices)


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    body = request.get_json(force=True) or {}
    index_key = body.get("index")
    access_token = body.get("access_token", "").strip()

    try:
        top_n = int(body.get("top_n", DEFAULT_TOP_N))
    except (TypeError, ValueError):
        top_n = DEFAULT_TOP_N
    top_n = max(1, min(top_n, MAX_TOP_N))

    if not access_token:
        return jsonify({
            "status": "error",
            "message": "Paste your Upstox access token first.",
        }), 400

    if index_key not in INDEX_REGISTRY:
        return jsonify({
            "status": "error",
            "message": f"Unknown index '{index_key}'. Choose one of: {list(INDEX_REGISTRY.keys())}",
        }), 400

    idx_cfg = INDEX_REGISTRY[index_key]
    instrument_key = idx_cfg["instrument_key"]

    try:
        client = UpstoxClient(access_token)
        expiry, lot_size = get_nearest_expiry_and_lot_size(client, instrument_key)
        chain_data = client.get_option_chain(instrument_key, expiry)

        # interval=1 means "OI difference over 1 trading day" -- the
        # standard single-session change, same definition we used to
        # compute manually as oi - prev_oi.
        today_dt = date.today()
        today = today_dt.isoformat()
        try:
            change_oi_data = client.get_change_in_oi(instrument_key, expiry, today, interval=1)
            change_oi_lookup = build_change_oi_lookup(change_oi_data)
            oi_data_date = today
            oi_data_source = "live"
        except UpstoxApiError:
            # Fall back to manual oi - prev_oi from the option-chain
            # response if the dedicated endpoint errors (e.g. called
            # before today's change-OI snapshot is ready) -- better
            # to show slightly-different-but-present data than fail
            # the whole page.
            change_oi_lookup = build_change_oi_lookup_from_chain(chain_data)
            oi_data_date = today
            oi_data_source = "fallback"

        result = compute_levels(client, chain_data, change_oi_lookup, lot_size=lot_size, top_n=top_n)
        result["status"] = "ok"
        result["index"] = index_key
        result["index_label"] = idx_cfg["label"]
        result["expiry"] = expiry
        result["lot_size"] = lot_size
        result["top_n"] = top_n
        result["oi_data_date"] = oi_data_date
        result["oi_data_weekday"] = today_dt.strftime("%A")
        result["oi_data_source"] = oi_data_source  # "live" = dedicated endpoint, "fallback" = manual oi-prev_oi
        result["vwap_data_date"] = today
        result["vwap_data_weekday"] = today_dt.strftime("%A")

        # FII/DII activity is daily, market-wide data -- not specific
        # to the selected index -- so it's fetched alongside but
        # treated as optional context. A failure here (e.g. data not
        # yet available for today) shouldn't break the whole page.
        try:
            fii_data = client.get_fii_data(["NSE_FO|INDEX_OPTIONS"], interval="1D")
            dii_data = client.get_dii_data(interval="1D")
            fii_dii_summary = summarize_fii_dii(fii_data, dii_data)

            # Convert the raw Unix-ms timestamps Upstox returns into
            # readable date + weekday strings, so the page can state
            # plainly which trading day this FII/DII data reflects
            # (it's frequently a day or two behind "today", since
            # institutional activity data has its own publish lag).
            for prefix in ("fii", "dii"):
                ts_key = f"{prefix}_as_of"
                if fii_dii_summary.get(ts_key):
                    as_of_dt = datetime.fromtimestamp(fii_dii_summary[ts_key] / 1000)
                    fii_dii_summary[f"{prefix}_as_of_date"] = as_of_dt.strftime("%Y-%m-%d")
                    fii_dii_summary[f"{prefix}_as_of_weekday"] = as_of_dt.strftime("%A")
                else:
                    fii_dii_summary[f"{prefix}_as_of_date"] = None
                    fii_dii_summary[f"{prefix}_as_of_weekday"] = None

            result["fii_dii"] = fii_dii_summary
        except UpstoxApiError:
            result["fii_dii"] = None

    except UpstoxApiError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 502
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Unexpected error: {exc}"}), 500

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
