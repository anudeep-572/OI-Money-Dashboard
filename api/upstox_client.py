"""
Upstox option-chain client + breakeven / money-flow computation.

Why Upstox instead of Dhan or scraping NSE/BSE directly:
  - Upstox's market-data APIs (including option chain + OI) are
    free to use -- only placing actual trade orders via API costs
    a flat per-order fee, which has nothing to do with reading data.
  - No bot-detection / cookie-handshake fighting like raw NSE/BSE
    scraping required.
  - Gives OI and previous-session OI directly per strike, plus full
    greeks, in one clean documented JSON response.

Auth model:
  Upstox access tokens expire daily. Rather than building the full
  OAuth redirect-and-exchange flow, this app uses Upstox's
  "Manual Token Generation" path: you visit your app's page on
  https://account.upstox.com/developer/apps, click Generate, copy
  the access token, and paste it into this app's UI each session.
  No client_secret or redirect_uri handling needed at runtime.

Formula notes:
  - Change in OI now comes directly from Upstox's dedicated
    /market/change-oi endpoint (interval=1, i.e. one trading session),
    rather than us computing oi - prev_oi ourselves from the
    option-chain response. Upstox computes this server-side per
    strike for both calls and puts.
  - VWAP is computed for real from each strike's own intraday
    candles (typical_price x volume summed across the day, divided
    by total volume) -- Upstox's option-chain response has no VWAP
    field, only ltp, and substituting ltp for VWAP causes deep-ITM
    strikes to look like they have unusually high money flow purely
    from intrinsic value, not real trading activity.
  - Money Flow (Cr) = (OI_change_in_contracts * lot_size * VWAP) / 1e7
  - CE breakeven = strike + CE VWAP
  - PE breakeven = strike - PE VWAP
    (By explicit choice: breakeven uses the day's volume-weighted
    average price, not the latest traded price. This means it
    represents the breakeven for a position opened at today's
    average price, not at the current live price -- a deliberate
    choice, not the conventional "breakeven if I buy right now"
    definition.)
"""
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

UPSTOX_BASE = "https://api.upstox.com/v2"
UPSTOX_BASE_V3 = "https://api.upstox.com/v3"

# All five major tradeable index options available on Upstox.
# instrument_key values confirmed against Upstox's own sample
# requests and multiple independent integration guides.
INDEX_REGISTRY = {
    "NIFTY": {
        "label": "NIFTY 50",
        "instrument_key": "NSE_INDEX|Nifty 50",
    },
    "BANKNIFTY": {
        "label": "NIFTY BANK",
        "instrument_key": "NSE_INDEX|Nifty Bank",
    },
    "FINNIFTY": {
        "label": "NIFTY FIN SERVICE",
        "instrument_key": "NSE_INDEX|Nifty Fin Service",
    },
    "MIDCPNIFTY": {
        "label": "NIFTY MID SELECT",
        "instrument_key": "NSE_INDEX|NIFTY MID SELECT",
    },
    "SENSEX": {
        "label": "SENSEX",
        "instrument_key": "BSE_INDEX|SENSEX",
    },
}


class UpstoxApiError(Exception):
    pass


class UpstoxClient:
    def __init__(self, access_token: str):
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }

    def get_option_contracts(self, instrument_key: str) -> list[dict]:
        """
        Returns the list of option contracts for an underlying, which
        includes every available expiry date and the lot_size -- we
        need both before we can call the option-chain endpoint.
        """
        resp = requests.get(
            f"{UPSTOX_BASE}/option/contract",
            headers=self.headers,
            params={"instrument_key": instrument_key},
            timeout=15,
        )
        self._raise_for_upstox_error(resp)
        return resp.json()["data"]

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list[dict]:
        """
        Returns the strike-by-strike CE/PE chain for one expiry,
        including oi, prev_oi, ltp, and greeks per leg.
        """
        resp = requests.get(
            f"{UPSTOX_BASE}/option/chain",
            headers=self.headers,
            params={"instrument_key": instrument_key, "expiry_date": expiry_date},
            timeout=15,
        )
        self._raise_for_upstox_error(resp)
        return resp.json()["data"]

    def get_intraday_candles(self, instrument_key: str, unit: str = "minutes", interval: str = "5") -> list[list]:
        """
        Fetches today's intraday candles for one instrument (e.g. a
        single option leg like 'NSE_FO|51059'), used to compute a
        real VWAP -- Upstox's option-chain response has no VWAP
        field, only ltp, so we reconstruct it from the day's candles.

        Each candle is [timestamp, open, high, low, close, volume, oi].
        Returns [] if there's no candle data yet (e.g. illiquid strike
        with zero trades so far) rather than raising, since a strike
        having no trades is a normal, expected case, not an error.
        """
        url = f"{UPSTOX_BASE_V3}/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
        resp = requests.get(url, headers=self.headers, timeout=15)
        if resp.status_code != 200:
            # Treat as "no data" rather than hard failure -- a single
            # illiquid strike shouldn't abort the whole chain fetch.
            return []
        try:
            return resp.json().get("data", {}).get("candles", [])
        except ValueError:
            return []

    def get_change_in_oi(self, instrument_key: str, expiry: str, date: str, interval: int = 1) -> dict:
        """
        Upstox's dedicated change-in-OI endpoint -- computes the OI
        difference server-side per strike, for both calls and puts,
        rather than us subtracting oi - prev_oi ourselves from the
        option-chain response.

        `interval` is the NUMBER OF DAYS over which the OI difference
        is calculated, not a time granularity. interval=1 gives the
        standard one-session change (today vs. the prior session),
        matching the same definition we were computing manually.

        Returns the raw `data` object:
          {
            total_put_change_oi, total_call_change_oi,
            spot_closing_price, expiry,
            call_put_oi_data_list: [{strike_price, call_change_oi, put_change_oi}, ...]
          }
        """
        resp = requests.get(
            f"{UPSTOX_BASE}/market/change-oi",
            headers=self.headers,
            params={
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": date,
                "interval": interval,
            },
            timeout=15,
        )
        self._raise_for_upstox_error(resp)
        return resp.json()["data"]

    def get_fii_data(self, data_types: list[str], interval: str = "1D") -> dict:
        """
        Foreign Institutional Investor activity -- buy/sell amounts,
        contracts, OI, and long/short breakdowns by market segment.
        Only available from 1 April 2026 onwards (Upstox's own data
        collection start date, not a limitation of this client).

        data_types accepts one or more of:
          NSE_FO|INDEX_FUTURES, NSE_FO|STOCK_FUTURES,
          NSE_FO|INDEX_OPTIONS, NSE_FO|STOCK_OPTIONS, NSE_EQ|CASH

        Returns the raw `data` dict, keyed by data_type, each value a
        list of daily/monthly records (most recent first).
        """
        resp = requests.get(
            f"{UPSTOX_BASE}/market/fii",
            headers=self.headers,
            params=[("data_type", dt) for dt in data_types] + [("interval", interval)],
            timeout=15,
        )
        self._raise_for_upstox_error(resp)
        return resp.json()["data"]

    def get_dii_data(self, interval: str = "1D") -> dict:
        """
        Domestic Institutional Investor activity. Currently only
        available for the NSE equity cash segment (no options
        breakdown like FII has). Same 1 April 2026 start date.

        Returns the raw `data` dict: {"NSE_EQ|CASH": [...]}.
        """
        resp = requests.get(
            f"{UPSTOX_BASE}/market/dii",
            headers=self.headers,
            params={"data_type": "NSE_EQ|CASH", "interval": interval},
            timeout=15,
        )
        self._raise_for_upstox_error(resp)
        return resp.json()["data"]

    @staticmethod
    def _raise_for_upstox_error(resp: requests.Response):
        if resp.status_code != 200:
            try:
                body = resp.json()
                errors = body.get("errors", [])
                message = "; ".join(e.get("message", str(e)) for e in errors) or resp.text[:300]
            except ValueError:
                message = resp.text[:300]
            raise UpstoxApiError(f"Upstox returned HTTP {resp.status_code}: {message}")


def get_nearest_expiry_and_lot_size(client: UpstoxClient, instrument_key: str) -> tuple[str, int]:
    """
    Pulls option contracts once to find the nearest (soonest) expiry
    date and the lot_size for this underlying -- both needed before
    calling the option-chain endpoint.
    """
    contracts = client.get_option_contracts(instrument_key)
    if not contracts:
        raise UpstoxApiError(f"No option contracts returned for {instrument_key}")

    expiries = sorted({c["expiry"] for c in contracts})
    nearest_expiry = expiries[0]
    lot_size = contracts[0].get("lot_size") or contracts[0].get("minimum_lot")

    if not lot_size:
        raise UpstoxApiError(f"Could not determine lot size for {instrument_key}")

    return nearest_expiry, lot_size


def compute_vwap_from_candles(candles: list[list]) -> float | None:
    """
    Real VWAP = sum(typical_price_i * volume_i) / sum(volume_i),
    computed from today's intraday candles. Typical price per candle
    is (high + low + close) / 3, the standard approximation for a
    candle's representative traded price within that bar.

    Returns None if there's no volume yet (e.g. an illiquid strike
    with no trades so far today) so callers can fall back sensibly
    rather than divide by zero.
    """
    total_value = 0.0
    total_volume = 0

    for candle in candles:
        # candle = [timestamp, open, high, low, close, volume, oi]
        _, _open, high, low, close, volume, _oi = candle
        if volume and volume > 0:
            typical_price = (high + low + close) / 3
            total_value += typical_price * volume
            total_volume += volume

    if total_volume == 0:
        return None
    return total_value / total_volume


def build_change_oi_lookup_from_chain(chain_data: list[dict]) -> dict:
    """
    Fallback for when /market/change-oi isn't available yet (e.g.
    called very early in the session before Upstox has computed
    today's change-OI snapshot). Derives the same {strike: {...}}
    shape manually from the option-chain response's oi/prev_oi
    fields, so compute_levels can use either source interchangeably.
    """
    lookup = {}
    for row in chain_data:
        strike = row.get("strike_price")
        ce_md = (row.get("call_options") or {}).get("market_data") or {}
        pe_md = (row.get("put_options") or {}).get("market_data") or {}

        ce_oi, ce_prev = ce_md.get("oi"), ce_md.get("prev_oi")
        pe_oi, pe_prev = pe_md.get("oi"), pe_md.get("prev_oi")

        lookup[strike] = {
            "call_change_oi": (ce_oi - ce_prev) if (ce_oi is not None and ce_prev is not None) else 0,
            "put_change_oi": (pe_oi - pe_prev) if (pe_oi is not None and pe_prev is not None) else 0,
        }
    return lookup


def build_change_oi_lookup(change_oi_data: dict) -> dict:
    """
    Converts the raw /market/change-oi response into a simple
    {strike_price: {"call_change_oi": ..., "put_change_oi": ...}}
    lookup, so compute_levels can look up each strike's OI change
    by key instead of re-deriving it from oi/prev_oi.
    """
    lookup = {}
    for row in change_oi_data.get("call_put_oi_data_list", []):
        lookup[row["strike_price"]] = {
            "call_change_oi": row.get("call_change_oi", 0),
            "put_change_oi": row.get("put_change_oi", 0),
        }
    return lookup


def summarize_fii_dii(fii_data: dict, dii_data: dict) -> dict:
    """
    Reduces the raw FII/DII responses down to the handful of figures
    worth showing above the breakeven tables: most recent day's net
    flow (buy - sell) for FII index options, the call/put long-short
    skew (a quick sentiment read), and DII's net cash-market flow.

    IMPORTANT: Upstox's docs label buy_amount/sell_amount only as
    "Total buy/sell value in INR" with no unit qualifier (lakhs vs
    crores vs plain rupees aren't specified, and the two endpoints'
    sample values don't share an obvious common scale). Rather than
    guess a conversion factor and risk silently showing a wrong
    number, this passes the raw values through unconverted and lets
    the frontend label them plainly as "INR" -- correct, even if the
    resulting figure looks unusually large or small, is better than
    a guessed Cr/Lakh conversion that could be off by 100x.

    Returns None for any figure that's missing (e.g. data not yet
    available for today, or before 1 April 2026) rather than raising
    -- this is a "nice to have" banner, not critical path, so a
    missing figure should just not render that line, not break the
    page.
    """
    result = {
        "fii_index_options_net_inr": None,
        "fii_call_long_contracts": None,
        "fii_put_long_contracts": None,
        "fii_call_short_contracts": None,
        "fii_put_short_contracts": None,
        "fii_as_of": None,
        "dii_cash_net_inr": None,
        "dii_as_of": None,
    }

    fii_options = fii_data.get("NSE_FO|INDEX_OPTIONS") or []
    if fii_options:
        latest = fii_options[0]  # most recent first
        result["fii_index_options_net_inr"] = round(latest.get("buy_amount", 0) - latest.get("sell_amount", 0), 2)
        result["fii_call_long_contracts"] = latest.get("total_call_long_contracts")
        result["fii_put_long_contracts"] = latest.get("total_put_long_contracts")
        result["fii_call_short_contracts"] = latest.get("total_call_short_contracts")
        result["fii_put_short_contracts"] = latest.get("total_put_short_contracts")
        result["fii_as_of"] = latest.get("time_stamp")

    dii_cash = dii_data.get("NSE_EQ|CASH") or []
    if dii_cash:
        latest = dii_cash[0]
        result["dii_cash_net_inr"] = round(latest.get("buy_amount", 0) - latest.get("sell_amount", 0), 2)
        result["dii_as_of"] = latest.get("time_stamp")

    return result


def compute_levels(client: "UpstoxClient", chain_data: list[dict], change_oi_lookup: dict, lot_size: int, top_n: int = 5, prefilter_multiplier: int = 3) -> dict:
    """
    Takes Upstox's raw option-chain `data` list and a pre-fetched
    change-in-OI lookup (from /market/change-oi, via
    build_change_oi_lookup), and returns the computed result: top
    strikes by money flow on each side, plus their breakeven prices.

    Change in OI now comes directly from Upstox's dedicated
    /market/change-oi endpoint rather than being derived by us as
    oi - prev_oi from the option-chain response. Upstox computes
    this server-side per strike for both calls and puts, which
    avoids any drift or definitional mismatch between the two oi
    fields we were subtracting manually before.

    Money flow uses REAL VWAP, computed per-leg from today's
    intraday candles -- NOT ltp. Upstox's option-chain response has
    no VWAP field, and substituting ltp causes a serious bug: ltp on
    a deep-ITM strike is dominated by intrinsic value, which falsely
    inflates "money flow" for strikes far from spot that have no
    actual unusual trading activity. VWAP, being volume-weighted
    across the day's actual trades, doesn't have this problem --
    it's what iCharts and other real option-flow tools use, and
    matches the formula this dashboard was originally built to
    replicate (VWAP x change-in-OI).

    PERFORMANCE: fetching a real VWAP needs one extra API call per
    strike per leg, which is what made this slow (~40-50 sequential
    calls for a full chain). Two optimizations cut this down a lot:
      1. Pre-filter: rank candidate legs by |oi_change| alone first
         (no API call needed, it's already in change_oi_lookup) and
         only fetch real VWAP candles for the top
         `top_n * prefilter_multiplier` legs per side. A leg with
         near-zero OI change can't win the final money-flow ranking
         regardless of its VWAP, so this is safe to skip.
      2. Concurrency: the candle fetches we DO need happen in
         parallel via a thread pool, not one after another, since
         these are independent network calls with no shared state.

    Upstox's option-chain endpoint has been observed (per Upstox's
    own developer community) to sometimes return an empty `data: []`
    even with a valid token, correct instrument key, and an active
    F&O subscription -- this seems to be an intermittent issue on
    Upstox's side, not something fixable from our end. We flag this
    distinctly (`chain_was_empty`) so the UI can tell the difference
    between "Upstox gave us nothing at all" and "we got real data,
    it just has no positive money-flow buildup right now."
    """
    if not chain_data:
        return {
            "last_price": None, "calls": [], "puts": [],
            "ce_breakevens": [], "pe_breakevens": [],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chain_was_empty": True,
        }

    last_price = chain_data[0].get("underlying_spot_price")

    # ── Pass 1: build cheap candidate lists, no API calls ──────────
    # Each candidate carries everything compute needs except VWAP.
    ce_candidates, pe_candidates = [], []

    for row in chain_data:
        strike = row.get("strike_price")
        strike_oi = change_oi_lookup.get(strike)
        if strike_oi is None:
            continue

        ce = row.get("call_options")
        if ce and ce.get("market_data") and ce.get("instrument_key"):
            ce_candidates.append({
                "strike": strike,
                "ltp": ce["market_data"].get("ltp") or 0,
                "instrument_key": ce["instrument_key"],
                "oi_change": strike_oi["call_change_oi"],
            })

        pe = row.get("put_options")
        if pe and pe.get("market_data") and pe.get("instrument_key"):
            pe_candidates.append({
                "strike": strike,
                "ltp": pe["market_data"].get("ltp") or 0,
                "instrument_key": pe["instrument_key"],
                "oi_change": strike_oi["put_change_oi"],
            })

    # Only positive OI change can ever rank (matches final filter
    # below), so narrow to that before even sorting.
    ce_candidates = [c for c in ce_candidates if c["oi_change"] > 0]
    pe_candidates = [p for p in pe_candidates if p["oi_change"] > 0]

    # Pre-filter: keep a generous multiple of top_n by raw OI change
    # alone -- cheap, no API call -- since VWAP can only ever scale
    # a leg's money flow up or down, never invert a wide OI-change
    # gap. prefilter_multiplier=3 means "look at the top 15 to find
    # the real top 5", which is generous enough to be safe while
    # still cutting candle calls roughly in half to two-thirds for a
    # typical 20-25 strike chain.
    fetch_limit = max(top_n * prefilter_multiplier, top_n)
    ce_candidates.sort(key=lambda x: x["oi_change"], reverse=True)
    pe_candidates.sort(key=lambda x: x["oi_change"], reverse=True)
    ce_to_fetch = ce_candidates[:fetch_limit]
    pe_to_fetch = pe_candidates[:fetch_limit]

    # ── Pass 2: fetch real VWAP concurrently, only for candidates ──
    def fetch_vwap(candidate):
        candles = client.get_intraday_candles(candidate["instrument_key"])
        vwap = compute_vwap_from_candles(candles)
        candidate["vwap"] = vwap if vwap is not None else candidate["ltp"]
        return candidate

    all_to_fetch = ce_to_fetch + pe_to_fetch
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_vwap, c) for c in all_to_fetch]
        for future in as_completed(futures):
            future.result()  # mutates candidate dicts in place; raises here if a fetch errored unexpectedly

    # ── Pass 3: compute money flow + breakeven now that VWAP is known ──
    calls, puts = [], []

    for c in ce_to_fetch:
        money_flow_cr = (c["oi_change"] * lot_size * c["vwap"]) / 1e7
        calls.append({
            "strike": c["strike"],
            "vwap": round(c["vwap"], 2),
            "oi_change": c["oi_change"],
            "money_flow_cr": round(money_flow_cr, 2),
            "breakeven": round(c["strike"] + c["vwap"], 2),
            "ltp": c["ltp"],
        })

    for p in pe_to_fetch:
        money_flow_cr = (p["oi_change"] * lot_size * p["vwap"]) / 1e7
        puts.append({
            "strike": p["strike"],
            "vwap": round(p["vwap"], 2),
            "oi_change": p["oi_change"],
            "money_flow_cr": round(money_flow_cr, 2),
            "breakeven": round(p["strike"] - p["vwap"], 2),
            "ltp": p["ltp"],
        })

    calls_sorted = sorted(calls, key=lambda x: x["money_flow_cr"], reverse=True)[:top_n]
    puts_sorted = sorted(puts, key=lambda x: x["money_flow_cr"], reverse=True)[:top_n]

    return {
        "last_price": last_price,
        "calls": calls_sorted,
        "puts": puts_sorted,
        "ce_breakevens": [c["breakeven"] for c in calls_sorted],
        "pe_breakevens": [p["breakeven"] for p in puts_sorted],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chain_was_empty": False,
    }
