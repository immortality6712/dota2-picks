#!/usr/bin/env python3
"""Собирает статистику пиков и сборки героев в data/*.json, чтобы сайт не ходил в API сам.

data/stats.json  — герои, турниры, пики/баны, сборки про-сцены и TI.
data/builds.json — сборки по рангам и позициям: Stratz (если задан STRATZ_TOKEN),
                   иначе попытка через публичные матчи OpenDota.
"""

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.opendota.com/api"
STRATZ = "https://api.stratz.com/graphql"
UA = "dota2-picks stats bot (github.com/immortality6712/dota2-picks)"
OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
SLEEP = float(os.environ.get("SLEEP", "1.2"))  # OpenDota без ключа — 60 запросов в минуту
STRATZ_TOKEN = os.environ.get("STRATZ_TOKEN", "").strip()

SEASON_START = "extract(epoch from date '2026-01-01')"
TIERS = "l.tier IN ('premium', 'professional')"
PHASES = ["start_game_items", "early_game_items", "mid_game_items", "late_game_items"]
RANK_KEYS = ["HERALD", "GUARDIAN", "CRUSADER", "ARCHON", "LEGEND", "ANCIENT", "DIVINE", "IMMORTAL"]
TOP = 12


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def request(url, data=None, headers=None, attempts=4):
    delay = 10
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            if e.code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                raise RuntimeError(f"HTTP {e.code}: {body}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if i == attempts - 1:
                raise RuntimeError(str(e)) from None
        log(f"  retry in {delay}s")
        time.sleep(delay)
        delay *= 2


def od(path):
    time.sleep(SLEEP)
    return request(API + path)


def sql(query):
    time.sleep(SLEEP)
    d = request(API + "/explorer?" + urllib.parse.urlencode({"sql": query}))
    if d.get("err"):
        raise RuntimeError(d["err"])
    return d.get("rows") or []


# ---------- OpenDota: турниры и про-сборки ----------

def collect_stats():
    hero_list = od("/heroStats")
    keep = ["id", "localized_name", "img", "primary_attr", "attack_type", "roles", "pro_pick", "pro_win"]
    keep += [f"{n}_{k}" for n in range(1, 9) for k in ("pick", "win")]
    heroes = [{k: h.get(k) for k in keep} for h in hero_list]

    patch = sql("""SELECT mp.patch, count(*) AS n
                   FROM match_patch mp JOIN matches m ON m.match_id = mp.match_id
                   WHERE m.start_time >= extract(epoch from now() - interval '30 days')
                   GROUP BY 1 ORDER BY n DESC LIMIT 1""")[0]["patch"]
    log("patch", patch)
    where = f"WHERE mp.patch = '{patch}' AND m.start_time >= {SEASON_START} AND {TIERS}"

    leagues = sql(f"""SELECT l.leagueid, l.name,
                             count(distinct m.match_id) AS matches,
                             min(m.start_time) AS first_match, max(m.start_time) AS last_match
                      FROM leagues l
                      JOIN matches m ON m.leagueid = l.leagueid
                      JOIN match_patch mp ON mp.match_id = m.match_id
                      {where}
                      GROUP BY l.leagueid, l.name
                      ORDER BY last_match DESC""")
    picks = sql(f"""SELECT m.leagueid, pb.hero_id,
                           sum(CASE WHEN pb.is_pick THEN 1 ELSE 0 END) AS picks,
                           sum(CASE WHEN NOT pb.is_pick THEN 1 ELSE 0 END) AS bans,
                           sum(CASE WHEN pb.is_pick AND ((pb.team = 0 AND m.radiant_win)
                                     OR (pb.team = 1 AND NOT m.radiant_win)) THEN 1 ELSE 0 END) AS wins
                    FROM picks_bans pb
                    JOIN matches m ON m.match_id = pb.match_id
                    JOIN match_patch mp ON mp.match_id = m.match_id
                    JOIN leagues l ON l.leagueid = m.leagueid
                    {where}
                    GROUP BY 1, 2""")
    log(f"leagues {len(leagues)}, pick rows {len(picks)}")

    # Итоговый инвентарь на TI одним запросом на всех героев.
    ti_where = """FROM player_matches pm
                  JOIN matches m ON m.match_id = pm.match_id
                  JOIN leagues l ON l.leagueid = m.leagueid
                  WHERE l.name ILIKE '%International 2026%'"""
    ti_items = sql(f"""SELECT hero_id, item, count(*) AS n FROM (
                         SELECT pm.hero_id,
                                unnest(ARRAY[pm.item_0, pm.item_1, pm.item_2, pm.item_3, pm.item_4, pm.item_5]) AS item
                         {ti_where}
                       ) s WHERE item > 0 GROUP BY 1, 2""")
    ti_games = sql(f"SELECT pm.hero_id, count(*) AS n {ti_where} GROUP BY 1")
    ti = {str(r["hero_id"]): {"picks": int(r["n"]), "list": []} for r in ti_games}
    for r in sorted(ti_items, key=lambda r: -int(r["n"])):
        b = ti.get(str(r["hero_id"]))
        if b and len(b["list"]) < TOP:
            b["list"].append([int(r["item"]), int(r["n"])])

    pro = {}
    for h in heroes:
        try:
            d = od(f"/heroes/{h['id']}/itemPopularity")
        except RuntimeError as e:
            log(f"  itemPopularity {h['id']}: {e}")
            continue
        pro[str(h["id"])] = {
            p: sorted(([int(k), int(v)] for k, v in (d.get(p) or {}).items()), key=lambda x: -x[1])[:8]
            for p in PHASES
        }
    log(f"pro builds {len(pro)}")

    return {
        "patch": patch,
        "heroes": heroes,
        "leagues": [[int(l["leagueid"]), l["name"], int(l["matches"]),
                     int(l["first_match"]), int(l["last_match"])] for l in leagues],
        "picks": [[int(r["leagueid"]), int(r["hero_id"]), int(r["picks"]), int(r["bans"]), int(r["wins"])]
                  for r in picks],
        "ti": ti,
        "pro": pro,
    }


# ---------- Stratz: сборки по рангам и позициям ----------

def gql(query, variables=None):
    time.sleep(0.4)  # Stratz: не больше 250 запросов в минуту
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    d = request(STRATZ, data=body, headers={
        "Authorization": f"Bearer {STRATZ_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "STRATZ_API",
    })
    if d.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in d["errors"])[:300])
    return d["data"]


def unwrap(t):
    while t and t.get("ofType"):
        t = t["ofType"]
    return t or {}


TYPE_Q = """query($n: String!) { __type(name: $n) {
  fields { name type { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
    args { name type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } } }
  enumValues { name } } }"""


def stratz_schema():
    """Схема Stratz меняется, поэтому аргументы и поля узнаём у самого API, а не зашиваем."""
    root = gql(TYPE_Q, {"n": "DotaQuery"})["__type"]
    hs = next(f for f in root["fields"] if f["name"] == "heroStats")
    hs_type = unwrap(hs["type"])["name"]
    fields = gql(TYPE_Q, {"n": hs_type})["__type"]["fields"]
    fp = next(f for f in fields if f["name"] == "itemFullPurchase")
    args = {a["name"]: unwrap(a["type"])["name"] for a in fp["args"]}

    def enum(name):
        return [v["name"] for v in gql(TYPE_Q, {"n": name})["__type"]["enumValues"] or []]

    brackets = {}
    if "bracketIds" in args:
        vals = enum(args["bracketIds"])
        for i, key in enumerate(RANK_KEYS):
            hit = next((v for v in vals if v == key), None)
            if hit:
                brackets[i + 1] = ("bracketIds", hit)
    if not brackets and "bracketBasicIds" in args:
        vals = enum(args["bracketBasicIds"])
        for i, key in enumerate(RANK_KEYS):
            hit = next((v for v in vals if key in v.split("_")), None)
            if hit:
                brackets[i + 1] = ("bracketBasicIds", hit)
    positions = {}
    if "positionIds" in args:
        for v in enum(args["positionIds"]):
            if v.startswith("POSITION_") and v[-1] in "12345":
                positions[int(v[-1])] = v

    # Поля ответа: ищем список событий с itemId и matchCount.
    ret = unwrap(fp["type"])["name"]
    rfields = gql(TYPE_Q, {"n": ret})["__type"]["fields"]
    list_field = None
    ev_fields = set()
    for f in rfields:
        t = unwrap(f["type"])
        if t.get("kind") == "OBJECT":
            sub = {x["name"] for x in gql(TYPE_Q, {"n": t["name"]})["__type"]["fields"]}
            if {"itemId", "matchCount"} <= sub:
                list_field, ev_fields = f["name"], sub
                break
    if not list_field:
        raise RuntimeError(f"не нашёл список предметов в {ret}")
    ev = " ".join(x for x in ("itemId", "matchCount", "winCount", "instance") if x in ev_fields)
    log(f"stratz: brackets {brackets}, positions {positions}, {ret}.{list_field}{{{ev}}}")
    return brackets, positions, list_field, ev, ev_fields


def fold(events, ev_fields):
    acc = {}
    for e in events or []:
        if "instance" in ev_fields and e.get("instance") not in (None, 1):
            continue  # вторую покупку того же предмета не считаем
        cur = acc.setdefault(e["itemId"], [e["itemId"], 0, 0])
        cur[1] += e.get("matchCount") or 0
        cur[2] += e.get("winCount") or 0
    return sorted(acc.values(), key=lambda x: -x[1])[:TOP]


def collect_stratz(hero_ids):
    brackets, positions, list_field, ev, ev_fields = stratz_schema()
    if not brackets:
        raise RuntimeError("Stratz не принимает фильтр по рангу")
    ranks, pos = {}, {}
    for hid in hero_ids:
        # При парных рангах (Рекрут+Страж и т.п.) запрашиваем каждую пару один раз.
        uniq = sorted(set(brackets.values()))
        parts = [f'b{i}: itemFullPurchase(heroId: {hid}, {arg}: [{val}]) {{ {list_field} {{ {ev} }} }}'
                 for i, (arg, val) in enumerate(uniq)]
        parts += [f'p{n}: itemFullPurchase(heroId: {hid}, positionIds: [{val}]) {{ {list_field} {{ {ev} }} }}'
                  for n, val in positions.items()]
        try:
            d = gql("{ heroStats { " + " ".join(parts) + " } }")["heroStats"]
        except RuntimeError as e:
            log(f"  stratz hero {hid}: {e}")
            continue
        def events(key):
            v = d.get(key) or {}
            if isinstance(v, list):  # в части версий схемы поле отдаёт список
                v = v[0] if v else {}
            return v.get(list_field)

        folded = {bv: fold(events(f"b{i}"), ev_fields) for i, bv in enumerate(uniq)}
        ranks[str(hid)] = {str(n): {"list": folded[bv]} for n, bv in brackets.items()}
        pos[str(hid)] = {str(n): {"list": fold(events(f"p{n}"), ev_fields)} for n in positions}
    return ranks, pos, {str(n): v for n, (_, v) in brackets.items()}


# ---------- OpenDota: запасной путь для рангов ----------

def collect_opendota_ranks():
    ranks = {}
    for n in range(1, 9):
        base = f"""FROM public_player_matches p
                   JOIN public_matches pm ON pm.match_id = p.match_id
                   WHERE pm.start_time >= extract(epoch from now() - interval '3 days')
                     AND pm.avg_rank_tier >= {n * 10} AND pm.avg_rank_tier < {n * 10 + 10}"""
        win = "CASE WHEN (p.player_slot < 128) = pm.radiant_win THEN 1 ELSE 0 END"
        try:
            rows = sql(f"""SELECT hero_id, item, count(*) AS n, sum(win) AS w FROM (
                             SELECT p.hero_id, {win} AS win,
                                    unnest(ARRAY[p.item_0, p.item_1, p.item_2, p.item_3, p.item_4, p.item_5]) AS item
                             {base}) s WHERE item > 0 GROUP BY 1, 2""")
            games = sql(f"SELECT p.hero_id, count(*) AS n, sum({win}) AS w {base} GROUP BY 1")
        except RuntimeError as e:
            log(f"  opendota rank {n}: {e}")
            return {}  # если не вышло на одном ранге — таблицы нет, дальше не пробуем
        for g in games:
            ranks.setdefault(str(g["hero_id"]), {})[str(n)] = {
                "games": int(g["n"]), "wins": int(g["w"] or 0), "list": []}
        for r in sorted(rows, key=lambda r: -int(r["n"])):
            b = ranks.get(str(r["hero_id"]), {}).get(str(n))
            if b is not None and len(b["list"]) < TOP:
                b["list"].append([int(r["item"]), int(r["n"]), int(r["w"] or 0)])
    return ranks


def write(name, data):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {name}: {(OUT / name).stat().st_size // 1024} KB")


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = collect_stats()

    ranks, positions, source, rank_bracket = {}, {}, "", {}
    if STRATZ_TOKEN:
        try:
            ranks, positions, rank_bracket = collect_stratz([h["id"] for h in stats["heroes"]])
            source = "stratz" if ranks else ""
        except (RuntimeError, StopIteration, KeyError, TypeError) as e:
            log(f"stratz failed: {e!r}")
    else:
        log("STRATZ_TOKEN не задан — пробую ранги через OpenDota")
    if not ranks:
        ranks = collect_opendota_ranks()
        source = "opendota" if ranks else ""

    # Словарь предметов — только те, что встречаются в сборках.
    used = set()
    for b in stats["ti"].values():
        used.update(i[0] for i in b["list"])
    for ph in stats["pro"].values():
        for lst in ph.values():
            used.update(i[0] for i in lst)
    for table in (ranks, positions):
        for per in table.values():
            for b in per.values():
                used.update(i[0] for i in b["list"])
    raw = od("/constants/items")
    items = {str(v["id"]): [v.get("dname") or "", v.get("img") or ""]
             for v in raw.values() if v.get("id") in used and v.get("dname")}

    write("stats.json", {"updated": now, **stats, "items": items})
    write("builds.json", {"updated": now, "source": source, "rank_bracket": rank_bracket,
                          "ranks": ranks, "positions": positions})


if __name__ == "__main__":
    main()
