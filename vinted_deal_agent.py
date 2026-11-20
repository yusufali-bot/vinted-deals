#!/usr/bin/env python3
"""
Vinted deal checker - runs ONCE per invocation (GitHub Actions calls it
every 5 minutes). Reads watchlist.json, remembers seen items in
seen_items.json, and sends a push notification through ntfy.sh.

Env vars:
  NTFY_TOPIC         required - your private ntfy topic name
  ANTHROPIC_API_KEY  optional - lets Claude sanity-check each deal
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

WATCHLIST = Path("watchlist.json")
SEEN_FILE = Path("seen_items.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
USE_AI = bool(os.environ.get("ANTHROPIC_API_KEY"))
AI_MODEL = "claude-sonnet-5-5"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def price_of(item):
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    return float(p)


def fetch(session, domain, query):
    r = session.get(
        f"https://{domain}/api/v2/catalog/items",
        params={
            "search_text": query,
            "order": "newest_first",
            "per_page": 50,
            "page": 1,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def passes_filters(item, search):
    title = (item.get("title") or "").lower()
    if any(w.lower() in title for w in search.get("exclude_words", [])):
        return False
    sizes = search.get("sizes", [])
    if sizes:
        size = (item.get("size_title") or "").lower()
        if not any(s.lower() in size for s in sizes):
            return False
    return True


def ai_check(item, price, median):
    try:
        import anthropic

        client = anthropic.Anthropic()
        prompt = (
            "You judge second-hand fashion deals. Reply with JSON only: "
            '{"good": true/false, "reason": "short"}.\n'
            f"Title: {item.get('title')}\nBrand: {item.get('brand_title')}\n"
            f"Size: {item.get('size_title')}\nCondition: {item.get('status')}\n"
            f"Price: {price}\nMedian price for similar listings: {median}\n"
            "Be skeptical of fakes, damaged items, or suspiciously cheap listings."
        )
        msg = client.messages.create(
            model=AI_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip().strip("`").removeprefix("json").strip()
        data = json.loads(text)
        return bool(data.get("good")), data.get("reason", "")
    except Exception as e:
        return True, f"(AI check skipped: {e})"


def notify(domain, item, price, median, reason):
    url = item.get("url") or f"https://{domain}/items/{item['id']}"
    discount = round((1 - price / median) * 100)
    body = (
        f"{item.get('title')}\n"
        f"{price:.2f} - about {discount}% under the median ({median:.2f})\n"
        f"{item.get('brand_title') or ''} {item.get('size_title') or ''}\n{reason}"
    ).strip()
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": "Vinted deal", "Click": url, "Tags": "moneybag"},
        timeout=15,
    )
    print("NOTIFIED:", body.replace("\n", " | "))


def main():
    if not NTFY_TOPIC:
        sys.exit("Set the NTFY_TOPIC environment variable.")

    config = json.loads(WATCHLIST.read_text())
    domain = config["domain"]
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    first_run = not seen

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    session.get(f"https://{domain}", timeout=20)  # session cookies

    for search in config["searches"]:
        try:
            items = fetch(session, domain, search["query"])
        except Exception as e:
            print(f"[{search['query']}] fetch failed: {e}")
            continue

        prices = [price_of(i) for i in items if i.get("price") is not None]
        if len(prices) < 8:
            continue
        median = statistics.median(prices)

        for item in items:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            if first_run or not passes_filters(item, search):
                continue
            price = price_of(item)
            if price > search["max_price"]:
                continue
            if 1 - price / median < search["min_discount"]:
                continue
            reason = ""
            if USE_AI:
                good, reason = ai_check(item, price, median)
                if not good:
                    continue
            notify(domain, item, price, median, reason)
        time.sleep(2)

    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]))


if __name__ == "__main__":
    main()
