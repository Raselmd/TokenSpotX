#!/usr/bin/env python3
"""
New crypto token monitor.
- Finds newly created DEX pools (GeckoTerminal, free)
- Checks if the token is on a centralized exchange (CoinGecko, free)
- Gives an automatic rating 1-7
- Posts to Telegram and saves docs/data.json for the Blogger website
- Reads overrides.json so you can raise a rating manually (up to 10)
"""
import html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ------------------------------------------------------------------
# SETTINGS (you can change these numbers)
# ------------------------------------------------------------------
MIN_LIQUIDITY_USD = 10_000      # below this, the token is ignored completely
MAX_NEW_PER_RUN = 8             # max new tokens processed every run (every ~10 min)
MAX_STORED = 500                # how many tokens stay on the website list
TELEGRAM_MIN_RATING = 1         # post to Telegram only if rating >= this
PAGES_TO_FETCH = 2              # GeckoTerminal pages (20 pools per page)

DATA_FILE = Path("docs/data.json")
OVERRIDES_FILE = Path("overrides.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CG_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()

# GeckoTerminal network id -> CoinGecko platform id
CG_PLATFORM = {
    "eth": "ethereum",
    "bsc": "binance-smart-chain",
    "solana": "solana",
    "base": "base",
    "arbitrum": "arbitrum-one",
    "polygon_pos": "polygon-pos",
    "avax": "avalanche",
    "optimism": "optimistic-ethereum",
    "ton": "the-open-network",
    "sui-network": "sui",
    "tron": "tron",
    "ftm": "fantom",
    "linea": "linea",
    "blast": "blast",
    "zksync": "zksync",
    "mantle": "mantle",
    "scroll": "scroll",
}

# Centralized exchanges (CoinGecko market identifiers). Add more if you want.
CEX_IDS = {
    "binance", "gdax", "okex", "bybit_spot", "kraken", "upbit", "bithumb",
    "gate", "kucoin", "mxc", "bitget", "huobi", "bingx", "bitmart", "lbank",
    "crypto_com", "whitebit", "coinex", "phemex", "bitfinex", "bitstamp",
    "bitvavo", "ascendex", "htx", "bitmex", "probit", "xt", "toobit",
}
TOP_TIER = {"binance", "gdax", "okex", "bybit_spot", "kraken", "upbit"}


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(url, params=None, headers=None, tries=3):
    """Returns (status_code, json_or_None). Retries on rate limit."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=25)
        except requests.RequestException as e:
            print("request error:", e)
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(10 * (i + 1))
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return 0, None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_usd(n):
    if n is None:
        return "N/A"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= limit:
            return f"${n / limit:.2f}{suffix}"
    return f"${n:,.0f}"


def fmt_num(n):
    if n is None:
        return "N/A"
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= limit:
            return f"{n / limit:.2f}{suffix}"
    return f"{n:,.0f}"


# ------------------------------------------------------------------
# data sources
# ------------------------------------------------------------------
def fetch_new_pools():
    """Newest DEX pools on all chains (GeckoTerminal)."""
    out = {}
    headers = {"Accept": "application/json;version=20230302"}
    for page in range(1, PAGES_TO_FETCH + 1):
        status, js = get(
            "https://api.geckoterminal.com/api/v2/networks/new_pools",
            params={"include": "base_token,dex", "page": page},
            headers=headers,
        )
        if status != 200 or not js:
            print("GeckoTerminal failed, page", page, status)
            break
        included = {i["id"]: i.get("attributes", {}) for i in js.get("included", [])}
        for p in js.get("data", []):
            try:
                a = p["attributes"]
                rel = p["relationships"]
                token_id = rel["base_token"]["data"]["id"]          # e.g. solana_<address>
                network, address = token_id.rsplit("_", 1)
                dex_id = rel.get("dex", {}).get("data", {}).get("id", "")
                tok = included.get(token_id, {})
                price = to_float(a.get("base_token_price_usd"))
                fdv = to_float(a.get("fdv_usd"))
                supply = (fdv / price) if (fdv and price) else None
                key = f"{network}:{address}"
                if key in out:
                    continue
                out[key] = {
                    "key": key,
                    "network": network,
                    "address": address,
                    "name": tok.get("name") or a.get("name", "?").split(" / ")[0],
                    "symbol": tok.get("symbol") or "?",
                    "dex": included.get(dex_id, {}).get("name") or dex_id or "DEX",
                    "pool": a.get("address", ""),
                    "liquidity": to_float(a.get("reserve_in_usd")) or 0.0,
                    "fdv": fdv,
                    "supply": supply,
                    "created": a.get("pool_created_at"),
                }
            except (KeyError, TypeError, ValueError) as e:
                print("skip pool:", e)
        time.sleep(2.5)
    return out


def coingecko_check(network, address):
    """Is the token on CoinGecko, and on which centralized exchanges?"""
    platform = CG_PLATFORM.get(network)
    if not platform:
        return {"checked": False, "on_cg": False, "cex": [], "cex_ids": set(), "volume": None, "supply": None}
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else None
    status, js = get(
        f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{address}",
        params={"localization": "false", "tickers": "true", "market_data": "true",
                "community_data": "false", "developer_data": "false", "sparkline": "false"},
        headers=headers,
    )
    if status == 404:
        return {"checked": True, "on_cg": False, "cex": [], "cex_ids": set(), "volume": None, "supply": None}
    if status != 200 or not js:
        return {"checked": False, "on_cg": False, "cex": [], "cex_ids": set(), "volume": None, "supply": None}
    names, ids = [], set()
    for t in js.get("tickers", []):
        m = t.get("market", {}) or {}
        ident = (m.get("identifier") or "").lower()
        if ident in CEX_IDS and ident not in ids:
            ids.add(ident)
            names.append(m.get("name") or ident)
    md = js.get("market_data", {}) or {}
    return {
        "checked": True,
        "on_cg": True,
        "cex": names,
        "cex_ids": ids,
        "volume": (md.get("total_volume") or {}).get("usd"),
        "supply": md.get("total_supply"),
    }


# ------------------------------------------------------------------
# rating 1-7
# ------------------------------------------------------------------
def auto_rating(liquidity, cg):
    ids = cg["cex_ids"]
    if not ids:
        # not on any exchange: maximum 3
        r = 1
        if liquidity >= 100_000:
            r += 1
        if cg["on_cg"] or liquidity >= 500_000:
            r += 1
        return min(r, 3)
    # on at least one exchange: 4 to 7
    r = 4
    if len(ids) >= 2:
        r += 1
    if ids & TOP_TIER:
        r += 1
    if (cg["volume"] or 0) >= 1_000_000 or liquidity >= 500_000:
        r += 1
    return min(r, 7)


# ------------------------------------------------------------------
# telegram
# ------------------------------------------------------------------
def build_message(t):
    e = html.escape
    if t.get("manual"):
        rating = f"{t['rating']}/10 (manually reviewed)"
    else:
        rating = f"{t['rating']}/7 (auto)"
    dot = "🔴" if t["rating"] <= 3 else ("🟡" if t["rating"] <= 5 else "🟢")
    exch = ", ".join(t["cex"]) if t["cex"] else f"Not on any exchange yet (DEX: {t['dex']})"
    return (
        f"🆕 <b>New token: {e(t['name'])} ({e(t['symbol'])})</b>\n"
        f"⛓ Chain: {e(t['network'])}\n"
        f"🏦 Exchange: {e(exch)}\n"
        f"📦 Supply: {fmt_num(t['supply'])}\n"
        f"💧 Liquidity: {fmt_usd(t['liquidity'])}\n"
        f"🔗 <a href=\"{e(t['url'])}\">Chart &amp; pool</a>\n"
        f"{dot} Rating: <b>{e(rating)}</b>\n"
        f"⚠️ New tokens are very risky. Not financial advice."
    )


def tg_call(method, payload):
    if not (BOT_TOKEN and CHAT_ID):
        return None
    for i in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                              json=payload, timeout=25)
        except requests.RequestException as e:
            print("telegram error:", e)
            time.sleep(3)
            continue
        js = {}
        try:
            js = r.json()
        except ValueError:
            pass
        if r.status_code == 429:
            time.sleep(js.get("parameters", {}).get("retry_after", 10) + 1)
            continue
        if not js.get("ok"):
            print("telegram not ok:", js.get("description"))
            return None
        return js.get("result")
    return None


def tg_send(t):
    res = tg_call("sendMessage", {
        "chat_id": CHAT_ID, "text": build_message(t), "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    return res.get("message_id") if res else None


def tg_edit(t):
    tg_call("editMessageText", {
        "chat_id": CHAT_ID, "message_id": t["tg_message_id"], "text": build_message(t),
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def manual_rating(overrides, key):
    o = overrides.get(key)
    if isinstance(o, dict):
        o = o.get("rating")
    try:
        r = int(o)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= 10 else None


def main():
    data = load_json(DATA_FILE, {"updated": None, "tokens": [], "skipped": []})
    tokens = data.get("tokens", [])
    skipped = set(data.get("skipped", []))
    overrides = load_json(OVERRIDES_FILE, {})
    if not isinstance(overrides, dict):
        overrides = {}

    # 1) apply manual rating changes to tokens we already posted
    for t in tokens:
        r = manual_rating(overrides, t["key"])
        if r is not None and (not t.get("manual") or t["rating"] != r):
            t["rating"], t["manual"] = r, True
            if t.get("tg_message_id"):
                tg_edit(t)
                time.sleep(2)

    # 2) find new tokens
    seen = {t["key"] for t in tokens} | skipped
    pools = fetch_new_pools()
    fresh = []
    for key, p in pools.items():
        if key in seen:
            continue
        if p["liquidity"] < MIN_LIQUIDITY_USD:
            skipped.add(key)
            continue
        fresh.append(p)
    fresh.sort(key=lambda p: p["liquidity"], reverse=True)
    print(f"pools: {len(pools)}, new after filter: {len(fresh)}")

    for p in fresh[:MAX_NEW_PER_RUN]:
        try:
            cg = coingecko_check(p["network"], p["address"])
            time.sleep(2.5 if CG_KEY else 6)
            if cg["supply"]:
                p["supply"] = to_float(cg["supply"]) or p["supply"]
            rating = auto_rating(p["liquidity"], cg)
            manual = False
            m = manual_rating(overrides, p["key"])
            if m is not None:
                rating, manual = m, True
            rec = {
                "key": p["key"],
                "name": p["name"],
                "symbol": p["symbol"],
                "network": p["network"],
                "address": p["address"],
                "dex": p["dex"],
                "cex": cg["cex"],
                "on_coingecko": cg["on_cg"],
                "cg_checked": cg["checked"],
                "supply": p["supply"],
                "liquidity": p["liquidity"],
                "fdv": p["fdv"],
                "rating": rating,
                "manual": manual,
                "created": p["created"],
                "found": now_iso(),
                "url": f"https://www.geckoterminal.com/{p['network']}/pools/{p['pool']}",
            }
            if rating >= TELEGRAM_MIN_RATING:
                rec["tg_message_id"] = tg_send(rec)
                time.sleep(3)
            tokens.insert(0, rec)
        except Exception as e:  # one bad token must not stop the run
            print("token failed:", p.get("key"), e)

    data = {
        "updated": now_iso(),
        "tokens": tokens[:MAX_STORED],
        "skipped": list(skipped)[-3000:],
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", len(data["tokens"]), "tokens")


if __name__ == "__main__":
    main()
