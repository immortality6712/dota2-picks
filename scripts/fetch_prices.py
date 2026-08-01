#!/usr/bin/env python3
"""Собирает цены предметов Dota 2 со Steam Market в prices.json."""

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APPID = 570
CURRENCIES = {"usd": 1, "rub": 5, "cny": 23}
UA = "dota2-picks price bot (github.com/immortality6712/dota2-picks)"
OUT = pathlib.Path(__file__).resolve().parent.parent / "prices.json"

# Гемы, подходящие к аркане Terrorblade — по гайду
# https://steamcommunity.com/sharedfiles/filedetails/?id=3336022190
ARCANA_GEMS = {
    "Prismatic: Red", "Prismatic: Gold", "Prismatic: Blue", "Prismatic: Purple",
    "Prismatic: Orange", "Prismatic: Light Green", "Prismatic: Deep Blue",
    "Prismatic: Sea Green", "Prismatic: Verdant Green", "Prismatic: Deep Green",
    "Prismatic: Bright Green", "Prismatic: Bright Purple", "Prismatic: Placid Blue",
    "Prismatic: Summer Warmth", "Prismatic: Sombre Red", "Prismatic: Creator's Light",
    "Prismatic: Blossom Red", "Prismatic: Crystalline Blue", "Prismatic: Rubiline",
    "Prismatic: Cursed Black", "Prismatic: Plague Grey", "Prismatic: Champion's Blue",
    "Prismatic: Champion's Green", "Prismatic: Champion's Purple", "Prismatic: Midas Gold",
    "Prismatic: Earth Green", "Prismatic: Ember Flame", "Prismatic: Diretide Orange",
    "Prismatic: Dredge Earth", "Prismatic: Dungeon Doom", "Prismatic: Tnim S'nnam",
    "Prismatic: Brusque Britches Beige", "Prismatic: Unhallowed Ground",
    "Prismatic: Ships in the Night", "Prismatic: Miasmatic Grey",
    "Prismatic: Pristine Platinum", "Prismatic: Vermillion Renewal",
    "Prismatic: Reflection's Shade", "Prismatic: Pyroclastic Flow",
    "Prismatic: Glacial Flow", "Prismatic: Plushy Shag", "Prismatic: Explosive Burst",
}

GROUPS = [
    {
        "key": "crimson",
        "title": "Crimson Witness",
        "params": {"query": '"Crimson Witness"'},
        "limit": 50,
        "keep": lambda i: "Crimson Witness" in i["name"],
    },
    {
        "key": "terrorblade",
        "title": "Аркана Terrorblade",
        "params": {"category_570_Hero[]": "tag_npc_dota_hero_terrorblade"},
        "limit": 20,
        "keep": lambda i: "Fractal Horns of Inner Abysm" in i["name"],
    },
    {
        "key": "gems",
        "title": "Гемы к аркане",
        "parent": "terrorblade",
        "params": {"query": "Prismatic:"},
        "limit": len(ARCANA_GEMS),
        "keep": lambda i: i["name"] in ARCANA_GEMS,
    },
]


def get(url, attempts=7):
    delay = 15
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if i == attempts - 1:
                raise
        print(f"  retry in {delay}s", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 240)
    raise RuntimeError("unreachable")


def search(group):
    params = {
        "appid": APPID,
        "norender": 1,
        "count": 100,
        "sort_column": "price",
        "sort_dir": "desc",
        **group["params"],
    }
    keep = group.get("keep", lambda i: True)
    found, start = [], 0
    while len(found) < group["limit"]:
        url = "https://steamcommunity.com/market/search/render/?" + urllib.parse.urlencode(
            {**params, "start": start}
        )
        data = get(url)
        page = data.get("results") or []
        if not page:
            break
        for r in page:
            asset = r.get("asset_description") or {}
            item = {
                "name": r["hash_name"],
                "listings": r["sell_listings"],
                "icon": asset.get("icon_url", ""),
                "type": asset.get("type", ""),
            }
            if keep(item):
                found.append(item)
        start += len(page)
        if start >= data.get("total_count", 0):
            break
        time.sleep(3)
    return found[: group["limit"]]


def price(name, currency):
    url = "https://steamcommunity.com/market/priceoverview/?" + urllib.parse.urlencode(
        {"appid": APPID, "currency": currency, "market_hash_name": name}
    )
    data = get(url)
    if not data.get("success"):
        return None
    return {
        "low": data.get("lowest_price"),
        "median": data.get("median_price"),
        "volume": data.get("volume"),
    }


def load_previous():
    """Цены с прошлого запуска: подстраховка на случай, когда Steam оборвёт сбор."""
    try:
        data = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, None
    return {i["name"]: i.get("prices", {}) for i in data.get("items", [])}, data.get("updated")


def main():
    previous, prev_updated = load_previous()
    reuse_hours = float(os.environ.get("REUSE_HOURS", 0))
    fresh = False
    if previous and prev_updated and reuse_hours:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(prev_updated)
        fresh = age.total_seconds() < reuse_hours * 3600
        print(f"прошлые цены: {len(previous)}, возраст {age}, переиспользую: {fresh}", file=sys.stderr)

    items = {}
    for group in GROUPS:
        found = search(group)
        print(f"{group['key']}: {len(found)} items", file=sys.stderr)
        if not found:
            sys.exit(f"Steam вернул пустую группу {group['key']} — не перезаписываю prices.json")
        keys = [group["key"]] + ([group["parent"]] if group.get("parent") else [])
        for item in found:
            items.setdefault(item["name"], {**item, "groups": []})["groups"].extend(keys)
        time.sleep(5)

    for n, item in enumerate(items.values(), 1):
        old = previous.get(item["name"], {})
        if fresh and len(old) == len(CURRENCIES):
            item["prices"] = old
            print(f"{n}/{len(items)} {item['name']} — из прошлого запуска", file=sys.stderr)
            continue
        item["prices"] = {}
        try:
            for code, currency in CURRENCIES.items():
                p = price(item["name"], currency)
                if p:
                    item["prices"][code] = p
                time.sleep(5)
        except Exception as e:
            item["prices"] = old
            item["stale"] = True
            print(f"{n}/{len(items)} {item['name']} — Steam оборвал ({e}), беру прошлые цены", file=sys.stderr)
            continue
        print(f"{n}/{len(items)} {item['name']}", file=sys.stderr)

    priced = sum(1 for i in items.values() if i["prices"])
    if priced < len(items) / 2:
        sys.exit(f"цены собраны только для {priced} из {len(items)} — не перезаписываю prices.json")

    OUT.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "groups": [{"key": g["key"], "title": g["title"]} for g in GROUPS],
                "items": sorted(items.values(), key=lambda i: -(i["listings"] or 0)),
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
