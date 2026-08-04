#!/usr/bin/env python3
"""Собирает цены предметов Dota 2 со Steam Market в prices.json."""

import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APPID = 570
# Доллары приходят из поиска пачкой, поштучно спрашиваем только рубли.
CURRENCIES = {"rub": 5}
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
        "limit": 100,  # запас: берём все, что есть на площадке, их около 70
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


def get_text(url, attempts=3):
    """Страница лотов отдаётся HTML-ом, а не JSON, поэтому загрузка отдельная."""
    delay = 15
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if i == attempts - 1:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 120)
    raise RuntimeError("unreachable")


# В данных страницы кавычки экранированы по нескольку раз, отсюда \\+ в шаблонах.
RE_LISTING = re.compile(r'\\+"listingid\\+":')
RE_PRICE = re.compile(r'\\+"unPricePerUnit\\+":(\d+)')
RE_FEE = re.compile(r'\\+"unFeePerUnit\\+":(\d+)')
RE_GEM = re.compile(
    r'font-size: 18px[^>]*?>([^<]{2,40})<\\*/span><br><span[^>]*font-size: 12px[^>]*>\s*Prismatic Gem'
)
RE_CURRENCY = re.compile(r'\\+"eCurrency\\+":(\d+)')


def sockets(name, pages):
    """Что за гем вставлен в каждый лот арканы и почём этот лот целиком."""
    found = []
    for page in range(pages):
        url = "https://steamcommunity.com/market/listings/570/" + urllib.parse.quote(name) + "?" + \
            urllib.parse.urlencode({"l": "english", "start": page * 20})
        html = get_text(url)
        # Страница считает в валюте своего региона и параметр валюты игнорирует.
        # Брать чужую валюту как доллары нельзя, а пересчитывать тут нечем.
        codes = set(RE_CURRENCY.findall(html))
        if codes - {"1"}:
            raise RuntimeError(f"страница отдаёт валюту {sorted(codes)}, а не доллары")
        chunks = RE_LISTING.split(html)[1:]
        if not chunks:
            break
        for c in chunks:
            price, fee, gem = RE_PRICE.search(c), RE_FEE.search(c), RE_GEM.search(c)
            if price and fee and gem:
                found.append({"gem": gem.group(1), "usd": (int(price.group(1)) + int(fee.group(1))) / 100})
        if len(chunks) < 20:
            break
        time.sleep(5)
    return found


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
                # Доллары выдача отдаёт сразу — это экономит запрос priceoverview на предмет.
                "usd_low": r.get("sell_price_text"),
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
    # Под жёстким лимитом долгие повторы бесполезны: дешевле пропустить и вернуться позже.
    data = get(url, attempts=int(os.environ.get("ATTEMPTS", 7)))
    if not data.get("success"):
        return None
    return {
        "low": data.get("lowest_price"),
        "median": data.get("median_price"),
        "volume": data.get("volume"),
    }


def to_number(s):
    """'31 311,51 руб.' и '$1,894.85' -> float. Пересчитанные цены уже числа."""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    # rstrip убирает точку из «руб.», иначе она сойдёт за десятичный разделитель.
    t = "".join(c for c in s if c.isdigit() or c in ".,").rstrip(".,")
    dec = re.search(r"[.,](\d{1,2})$", t)
    whole = (t[: dec.start()] if dec else t).replace(".", "").replace(",", "")
    if not whole.isdigit():
        return None
    return float(whole + ("." + dec.group(1) if dec else ""))


def steam_rate(items, code):
    """Курс Steam из реальных пар цен: медиана отношений устойчива к разовым выбросам."""
    ratios = []
    for i in items:
        p = i.get("prices", {})
        if p.get(code, {}).get("approx"):
            continue  # иначе прошлый пересчёт сам себя подтвердит и курс застынет
        usd, other = to_number(p.get("usd", {}).get("low")), to_number(p.get(code, {}).get("low"))
        if usd and other:
            ratios.append(other / usd)
    if len(ratios) < 2:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def load_previous():
    """Цены с прошлого запуска: подстраховка на случай, когда Steam оборвёт сбор."""
    try:
        data = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, None
    return {i["name"]: i.get("prices", {}) for i in data.get("items", [])}, data.get("updated")


def write(items, rates=None, socket_list=None):
    """Пишем после каждого предмета: обрыв на середине не должен стоить всего прогона."""
    OUT.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "groups": [{"key": g["key"], "title": g["title"]} for g in GROUPS],
                # Курс нужен странице, чтобы честно подписать пересчитанные цены.
                "rate": rates or {},
                # Аркана с уже вставленным гемом — одна покупка, а не две.
                "sockets": socket_list or [],
                "items": sorted(
                    (i for i in items.values() if i.get("prices")),
                    key=lambda i: -(i["listings"] or 0),
                ),
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )


def collect_sockets(items):
    """Самая дешёвая аркана с каждым гемом внутри. Не критично: не вышло — идём дальше."""
    pages = int(os.environ.get("SOCKET_PAGES", 5))
    if not pages:
        return []
    best = {}
    for item in items.values():
        if "terrorblade" not in item["groups"] or "gems" in item["groups"]:
            continue
        low = to_number(item["prices"].get("usd", {}).get("low"))
        try:
            found = sockets(item["name"], pages)
        except Exception as e:
            print(f"состав лотов {item['name']}: не вышло ({e})", file=sys.stderr)
            continue
        # Подстраховка на случай, если Steam сменит разметку: самый дешёвый лот
        # со страницы обязан сойтись с ценой из поиска.
        cheap = min(found, key=lambda f: f["usd"], default=None)
        cheapest = cheap["usd"] if cheap else None
        if not low or not cheapest or abs(cheapest / low - 1) > 0.25:
            print(f"состав лотов {item['name']}: {cheapest} не сходится с {low}, пропускаю", file=sys.stderr)
            continue
        # Цена в строке — это самый дешёвый лот, значит и гем показываем его.
        item["socket"] = cheap["gem"]
        for f in found:
            cur = best.get(f["gem"])
            if not cur or f["usd"] < cur["usd"]:
                best[f["gem"]] = {"gem": f["gem"], "usd": f["usd"], "arcana": item["name"]}
        print(f"состав лотов {item['name']}: {len(found)} лотов, гемов {len(best)}", file=sys.stderr)
        time.sleep(5)
    return sorted(best.values(), key=lambda x: x["usd"])


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

    for item in items.values():
        item["prices"] = {"usd": {"low": item.pop("usd_low", None)}}

    # Лимит Steam часто обрывает проход на середине, поэтому дорогие Crimson идут первыми.
    order = [g["key"] for g in GROUPS]
    queue = sorted(items.values(), key=lambda i: min(order.index(k) for k in i["groups"]))

    gap = float(os.environ.get("SLEEP", 5))
    miss_limit = int(os.environ.get("MISS_LIMIT", 3))
    misses = 0
    for n, item in enumerate(queue, 1):
        write(items)
        if misses >= miss_limit:
            break
        old = previous.get(item["name"], {})
        if fresh and all(c in old for c in CURRENCIES):
            item["prices"].update({c: old[c] for c in CURRENCIES})
            print(f"{n}/{len(items)} {item['name']} — из прошлого запуска", file=sys.stderr)
            continue
        try:
            for code, currency in CURRENCIES.items():
                p = price(item["name"], currency)
                if p:
                    item["prices"][code] = p
                time.sleep(gap)
        except Exception as e:
            misses += 1
            for code in CURRENCIES:
                if code in old:
                    item["prices"][code] = old[code]
                    item["stale"] = True
            print(f"{n}/{len(items)} {item['name']} — Steam оборвал ({e}), беру прошлые цены", file=sys.stderr)
            time.sleep(gap)
            continue
        misses = 0
        print(f"{n}/{len(items)} {item['name']}", file=sys.stderr)

    rates = {}
    for code in CURRENCIES:
        rate = steam_rate(items.values(), code)
        if not rate:
            print(f"{code}: курс не вывести, пересчёта не будет", file=sys.stderr)
            continue
        filled = dropped = 0
        for item in items.values():
            usd = to_number(item["prices"].get("usd", {}).get("low"))
            if not usd:
                continue
            have = to_number(item["prices"].get(code, {}).get("low"))
            if have:
                # Доллары приходят из поиска, а priceoverview Steam отдаёт из кеша и для
                # редких предметов не обновляет. Разъехались больше чем на десятую —
                # значит цена протухла, и такой лот на площадке уже не купить.
                if abs(have / (usd * rate) - 1) <= 0.1:
                    continue
                dropped += 1
            item["prices"][code] = {"low": round(usd * rate, 2), "approx": True}
            filled += 1
        if filled:
            rates[code] = round(rate, 4)
        print(f"{code}: курс Steam {rate:.2f}, пересчитано {filled} (из них протухших {dropped})", file=sys.stderr)

    write(items, rates, collect_sockets(items))
    real = sum(1 for i in items.values() if not i["prices"].get("rub", {}).get("approx"))
    print(f"wrote {OUT}: {len(items)} предметов, живых рублёвых цен {real}", file=sys.stderr)


if __name__ == "__main__":
    main()
