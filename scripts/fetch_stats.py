#!/usr/bin/env python3
"""Собирает статистику пиков и сборки героев в data/*.json, чтобы сайт не ходил в API сам.

data/stats.json  — герои, турниры, пики/баны, сборки про-сцены и TI.
data/builds.json — сборки по рангам и позициям: Stratz (если задан STRATZ_TOKEN),
                   иначе попытка через публичные матчи OpenDota.
"""

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

    # Позиция 1–5 в про-матчах: линия из OpenDota, а на паре героев в одной линии
    # кор — тот, у кого больше золота в минуту (1 или 3), второй — поддержка (5 или 4).
    try:
        pos_rows = sql(f"""WITH p AS (
                         SELECT pm.match_id, pm.hero_id, pm.lane_role, pm.gold_per_min,
                                pm.player_slot < 128 AS radiant
                         FROM player_matches pm
                         JOIN matches m ON m.match_id = pm.match_id
                         JOIN match_patch mp ON mp.match_id = m.match_id
                         JOIN leagues l ON l.leagueid = m.leagueid
                         {where} AND pm.lane_role IS NOT NULL),
                       r AS (SELECT *, row_number() OVER (PARTITION BY match_id, radiant, lane_role
                                                          ORDER BY gold_per_min DESC) AS rn FROM p)
                       SELECT hero_id,
                              CASE WHEN lane_role = 2 AND rn = 1 THEN 2
                                   WHEN lane_role = 1 AND rn = 1 THEN 1
                                   WHEN lane_role = 1 THEN 5
                                   WHEN lane_role = 3 AND rn = 1 THEN 3
                                   ELSE 4 END AS pos,
                              count(*) AS n
                       FROM r GROUP BY 1, 2""")
    except RuntimeError as e:
        log(f"  positions: {e}")
        pos_rows = []
    pos = {}
    for r in pos_rows:
        pos.setdefault(str(r["hero_id"]), [0] * 5)[int(r["pos"]) - 1] += int(r["n"])
    log(f"positions for {len(pos)} heroes")

    return {
        "patch": patch,
        "pos": pos,
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

    # Поля ответа: ищем (до трёх уровней вглубь) объект с id предмета и числом матчей.
    ret = unwrap(fp["type"])["name"]
    tree = []
    queue = [(ret, [])]
    found = None
    while queue and not found:
        tname, path = queue.pop(0)
        fields = gql(TYPE_Q, {"n": tname})["__type"]["fields"] or []
        names = {f["name"] for f in fields}
        tree.append(f"{'.'.join([ret] + path)}: {sorted(names)}")
        item = next((x for x in ITEM_KEYS if x in names), None)
        count = next((x for x in COUNT_KEYS if x in names), None)
        if item and count:
            found = (path, item, count, next((x for x in WIN_KEYS if x in names), None),
                     "instance" if "instance" in names else None)
            break
        if len(path) < 3:
            for f in fields:
                t = unwrap(f["type"])
                if t.get("kind") == "OBJECT":
                    queue.append((t["name"], path + [f["name"]]))
    log("stratz schema:\n  " + "\n  ".join(tree))
    if not found:
        raise RuntimeError(f"не нашёл список предметов в {ret}")
    path, item, count, win, inst = found
    sel = " ".join(x for x in (item, count, win, inst) if x)
    for name in reversed(path):
        sel = f"{name} {{ {sel} }}"
    log(f"stratz: brackets {brackets}, positions {positions}, selection {{ {sel} }}")
    return brackets, positions, sel, found


ITEM_KEYS = ("itemId", "item_id")
COUNT_KEYS = ("matchCount", "count", "matches", "purchaseCount")
WIN_KEYS = ("winCount", "wins", "winsCount")


def events(node, path):
    """Спускается по пути полей, раскрывая списки на любом уровне."""
    nodes = node if isinstance(node, list) else [node]
    for name in path:
        nxt = []
        for n in nodes:
            v = (n or {}).get(name)
            nxt.extend(v if isinstance(v, list) else [v])
        nodes = nxt
    return [n for n in nodes if n]


def fold(evs, found):
    """Сводит строки Stratz (предмет × минута × номер покупки) в итог по предмету.

    Номер покупки считаем только первый из встретившихся у предмета: с какого
    числа Stratz начинает нумерацию, в схеме не сказано.
    """
    _, item, count, win, inst = found
    first = {}
    if inst:
        for e in evs:
            if e.get(item) and e.get(inst) is not None:
                first[e[item]] = min(first.get(e[item], e[inst]), e[inst])
    acc = {}
    for e in evs:
        iid = e.get(item)
        if not iid or (iid in first and e.get(inst) != first[iid]):
            continue
        cur = acc.setdefault(iid, [iid, 0, 0])
        cur[1] += e.get(count) or 0
        cur[2] += (e.get(win) or 0) if win else 0
    return sorted(acc.values(), key=lambda x: -x[1])[:TOP]


def debug_sample(hid, evs, found):
    _, item, count, win, inst = found
    insts = sorted({e.get(inst) for e in evs}, key=str) if inst else []
    log(f"  sample hero {hid}: {len(evs)} rows, {len({e.get(item) for e in evs})} items, instances {insts[:10]}")
    for e in sorted(evs, key=lambda e: -(e.get(count) or 0))[:8]:
        log(f"    {e}")


def collect_stratz(hero_ids):
    try:
        brackets, positions, sel, found = stratz_schema()
    except RuntimeError as e:
        # Бесплатный ключ Stratz пускает не больше чем с двух IP за 15 минут,
        # а у каждого запуска Actions свой адрес — ждём, пока место освободится.
        m = re.search(r"frees up in (\d+) minute", str(e))
        if "IP Address" not in str(e):
            raise
        wait = (int(m.group(1)) + 1) * 60 if m else 16 * 60
        log(f"stratz: лимит по IP, жду {wait // 60} мин")
        time.sleep(wait)
        brackets, positions, sel, found = stratz_schema()
    path = found[0]
    if not brackets:
        raise RuntimeError("Stratz не принимает фильтр по рангу")
    ranks, pos = {}, {}
    for hid in hero_ids:
        # При парных рангах (Рекрут+Страж и т.п.) запрашиваем каждую пару один раз.
        uniq = sorted(set(brackets.values()))
        parts = [f'b{i}: itemFullPurchase(heroId: {hid}, {arg}: [{val}]) {{ {sel} }}'
                 for i, (arg, val) in enumerate(uniq)]
        parts += [f'p{n}: itemFullPurchase(heroId: {hid}, positionIds: [{val}]) {{ {sel} }}'
                  for n, val in positions.items()]
        try:
            d = gql("{ heroStats { " + " ".join(parts) + " } }")["heroStats"]
        except RuntimeError as e:
            log(f"  stratz hero {hid}: {e}")
            continue
        if hid == hero_ids[0]:
            debug_sample(hid, events(d.get("b0"), path), found)
        folded = {bv: fold(events(d.get(f"b{i}"), path), found) for i, bv in enumerate(uniq)}
        ranks[str(hid)] = {str(n): {"list": folded[bv]} for n, bv in brackets.items()}
        pos[str(hid)] = {str(n): {"list": fold(events(d.get(f"p{n}"), path), found)} for n in positions}
    return ranks, pos, {str(n): v for n, (_, v) in brackets.items()}


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
        log("STRATZ_TOKEN не задан — сборки по рангам пропускаю")

    builds_updated = now
    if not ranks:
        # Stratz не ответил — оставляем прошлые сборки (с их датой, чтобы сайт
        # видел возраст), а не затираем их пустыми.
        try:
            prev = json.loads((OUT / "builds.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        if prev.get("ranks"):
            log(f"stratz недоступен — оставляю сборки от {prev.get('updated')}")
            ranks, positions = prev["ranks"], prev.get("positions", {})
            source, rank_bracket = prev.get("source", ""), prev.get("rank_bracket", {})
            builds_updated = prev.get("updated", now)

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
    write("builds.json", {"updated": builds_updated, "source": source, "rank_bracket": rank_bracket,
                          "ranks": ranks, "positions": positions})


if __name__ == "__main__":
    main()
