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
RANK_KEYS = ["HERALD", "GUARDIAN", "CRUSADER", "ARCHON", "LEGEND", "ANCIENT", "DIVINE"]  # без Титана
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
    keep += [f"{n}_{k}" for n in range(1, 8) for k in ("pick", "win")]
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
                                pm.player_slot < 128 AS radiant,
                                (pm.player_slot < 128) = m.radiant_win AS win
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
                              count(*) AS n,
                              sum(CASE WHEN win THEN 1 ELSE 0 END) AS w
                       FROM r GROUP BY 1, 2""")
    except RuntimeError as e:
        log(f"  positions: {e}")
        pos_rows = []
    pos, pos_w = {}, {}
    for r in pos_rows:
        h, i = str(r["hero_id"]), int(r["pos"]) - 1
        pos.setdefault(h, [0] * 5)[i] += int(r["n"])
        pos_w.setdefault(h, [0] * 5)[i] += int(r.get("w") or 0)
    log(f"positions for {len(pos)} heroes")

    matchups = collect_matchups(where)

    return {
        "patch": patch,
        "pos": pos,
        "pos_w": pos_w,
        "matchups": matchups,
        "heroes": heroes,
        "leagues": [[int(l["leagueid"]), l["name"], int(l["matches"]),
                     int(l["first_match"]), int(l["last_match"])] for l in leagues],
        "picks": [[int(r["leagueid"]), int(r["hero_id"]), int(r["picks"]), int(r["bans"]), int(r["wins"])]
                  for r in picks],
        "ti": ti,
        "pro": pro,
    }


def rank_pairs(lst, base=None):
    """Лучшие и худшие пары по винрейту, сглаженному к общему винрейту героя."""
    if base is None:
        n = sum(x[1] for x in lst)
        base = sum(x[2] for x in lst) / n if n else 0.5
    ok = [x for x in lst if x[1] >= MATCHUP_MIN]
    # Пара из пяти игр не должна выглядеть лучше пары из пятидесяти.
    ok.sort(key=lambda x: -((x[2] + base * MATCHUP_K) / (x[1] + MATCHUP_K)))
    best = ok[:MATCHUP_TOP]
    return {"best": best, "worst": [x for x in ok[::-1] if x not in best][:MATCHUP_TOP]}


MATCHUP_MIN = 5   # меньше матчей на пару — не показываем
MATCHUP_K = 10    # сглаживание винрейта к среднему героя
MATCHUP_TOP = 6


def collect_matchups(where):
    """Союзники и противники героя в про-матчах патча: с кем и против кого он выигрывает чаще обычного."""
    # Пары считаем здесь, а не в SQL: самообъединение player_matches не укладывается
    # в лимит времени Explorer, а плоский список героев по матчам отдаётся быстро.
    try:
        rows = sql(f"""SELECT pm.match_id, pm.hero_id, pm.player_slot < 128 AS radiant,
                              CASE WHEN (pm.player_slot < 128) = m.radiant_win THEN 1 ELSE 0 END AS win
                       FROM player_matches pm
                       JOIN matches m ON m.match_id = pm.match_id
                       JOIN match_patch mp ON mp.match_id = m.match_id
                       JOIN leagues l ON l.leagueid = m.leagueid
                       {where}""")
    except RuntimeError as e:
        log(f"  matchups: {e}")
        return {}
    by_match = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append(
            (int(r["hero_id"]), r["radiant"] in (True, "true", 1, "t"), int(r["win"])))
    acc = {}
    for team in by_match.values():
        for h, side, win in team:
            for o, oside, _ in team:
                if o == h:
                    continue
                key = (h, o, side == oside)
                cur = acc.setdefault(key, [0, 0])
                cur[0] += 1
                cur[1] += win
    pairs = {}
    for (h, o, ally), (n, w) in acc.items():
        pairs.setdefault(str(h), {"with": [], "vs": []})["with" if ally else "vs"].append([o, n, w])
    out = {}
    for h, kinds in pairs.items():
        # Общий винрейт героя: против каждого соперника он играет ровно пять раз за матч.
        n = sum(x[1] for x in kinds["vs"])
        base = sum(x[2] for x in kinds["vs"]) / n if n else 0.5
        out[h] = {kind: rank_pairs(lst, base) for kind, lst in kinds.items()}
        out[h]["wr"] = round(base, 4)
    log(f"matchups for {len(out)} heroes from {len(by_match)} matches")
    return out


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

    sel, found = item_shape(unwrap(fp["type"])["name"])
    log(f"stratz: brackets {brackets}, positions {positions}, selection {{ {sel} }}")

    # Дополнительные поля того же вида: старт, сапоги, нейтралки — и матчапы.
    by_name = {f["name"]: f for f in fields}
    extras = {}
    for key, name in EXTRA_FIELDS.items():
        f = by_name.get(name)
        if not f:
            log(f"stratz: поля {name} нет")
            continue
        fargs = {a["name"] for a in f["args"]}
        arg = brackets[1][0] if brackets else None
        if arg not in fargs or "heroId" not in fargs:
            log(f"stratz: {name} без фильтра по рангу: {sorted(fargs)}")
            continue
        try:
            extras[key] = (name,) + item_shape(unwrap(f["type"])["name"])
        except RuntimeError as e:
            log(f"stratz: {name}: {e}")
    mu = None
    f = next((f for f in fields if "matchup" in f["name"].lower()), None)
    if f:
        fargs = {a["name"] for a in f["args"]}
        try:
            mu = (f["name"], "take" in fargs) + matchup_shape(unwrap(f["type"])["name"])
        except RuntimeError as e:
            log(f"stratz: {f['name']}: {e}")
    else:
        log("stratz: поля матчапов нет: " + ", ".join(sorted(by_name)))
    return brackets, positions, sel, found, extras, mu


EXTRA_FIELDS = {"start": "itemStartingPurchase", "boots": "itemBootPurchase", "neutral": "itemNeutral"}


def item_shape(ret):
    """Ищет (до трёх уровней вглубь) объект с id предмета и числом матчей и строит выборку полей."""
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
                     "instance" if "instance" in names else None,
                     "time" if "time" in names else None)
            break
        if len(path) < 3:
            for f in fields:
                t = unwrap(f["type"])
                if t.get("kind") == "OBJECT":
                    queue.append((t["name"], path + [f["name"]]))
    log("stratz schema:\n  " + "\n  ".join(tree))
    if not found:
        raise RuntimeError(f"не нашёл список предметов в {ret}")
    path, item, count, win, inst, tm = found
    sel = " ".join(x for x in (item, count, win, inst, tm) if x)
    for name in reversed(path):
        sel = f"{name} {{ {sel} }}"
    return sel, found


def matchup_shape(ret):
    """Находит списки vs/with с парами героев: второй герой, число матчей и побед."""
    tree, paths = [], {}
    queue = [(ret, [])]
    while queue:
        tname, path = queue.pop(0)
        fields = gql(TYPE_Q, {"n": tname})["__type"]["fields"] or []
        names = {f["name"] for f in fields}
        tree.append(f"{'.'.join([ret] + path)}: {sorted(names)}")
        if path and path[-1] in ("vs", "with") and "heroId2" in names and "matchCount" in names:
            win = next((x for x in WIN_KEYS if x in names), None)
            paths.setdefault(path[-1], []).append((path, win))
            continue
        if len(path) < 3:
            for f in fields:
                t = unwrap(f["type"])
                if t.get("kind") == "OBJECT":
                    queue.append((t["name"], path + [f["name"]]))
    log("stratz matchup schema:\n  " + "\n  ".join(tree))
    if not paths:
        raise RuntimeError(f"не нашёл списки vs/with в {ret}")

    def tree_sel(plist):
        # Собираем выборку из всех путей: {a {vs {…}} b {vs {…}}}
        root = {}
        for path, win in plist:
            node = root
            for name in path:
                node = node.setdefault(name, {})
            node["__leaf"] = " ".join(x for x in ("heroId2", "matchCount", win) if x)

        def render(n):
            return " ".join(v if k == "__leaf" else f"{k} {{ {render(v)} }}" for k, v in n.items())
        return render(root)

    allp = [x for v in paths.values() for x in v]
    log(f"stratz matchups: {[('.'.join(p), w) for p, w in allp]}")
    return tree_sel(allp), paths


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
    _, item, count, win, inst, tm = found
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
        cur = acc.setdefault(iid, [iid, 0, 0, 0])
        c = e.get(count) or 0
        cur[1] += c
        cur[2] += (e.get(win) or 0) if win else 0
        cur[3] += (e.get(tm) or 0) * c if tm else 0
    out = sorted(acc.values(), key=lambda x: -x[1])[:TOP]
    for x in out:
        # Четвёртое число — средняя минута покупки (взвешенная по числу матчей).
        t = x[3] / x[1] if tm and x[1] else None
        # В схеме не сказано, минуты это или секунды; позже 150-й минуты предметы не покупают.
        x[3] = round(t / 60 if t and t > 150 else t, 1) if t is not None else None
    return [x if x[3] is not None else x[:3] for x in out]


def debug_sample(hid, evs, found):
    _, item, count, win, inst, _ = found
    insts = sorted({e.get(inst) for e in evs}, key=str) if inst else []
    log(f"  sample hero {hid}: {len(evs)} rows, {len({e.get(item) for e in evs})} items, instances {insts[:10]}")
    for e in sorted(evs, key=lambda e: -(e.get(count) or 0))[:8]:
        log(f"    {e}")


def collect_stratz(hero_ids):
    try:
        brackets, positions, sel, found, extras, mu = stratz_schema()
    except RuntimeError as e:
        # Бесплатный ключ Stratz пускает не больше чем с двух IP за 15 минут,
        # а у каждого запуска Actions свой адрес — ждём, пока место освободится.
        m = re.search(r"frees up in (\d+) minute", str(e))
        if "IP Address" not in str(e):
            raise
        wait = (int(m.group(1)) + 1) * 60 if m else 16 * 60
        log(f"stratz: лимит по IP, жду {wait // 60} мин")
        time.sleep(wait)
        brackets, positions, sel, found, extras, mu = stratz_schema()
    path = found[0]
    if not brackets:
        raise RuntimeError("Stratz не принимает фильтр по рангу")
    ranks, pos, ext, mus = {}, {}, {}, {}
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

        # Старт, сапоги и нейтралки по каждой паре рангов — отдельным запросом, чтобы не упереться в сложность.
        if extras:
            parts = [f'{key}{i}: {name}(heroId: {hid}, {arg}: [{val}]) {{ {esel} }}'
                     for key, (name, esel, _) in extras.items() for i, (arg, val) in enumerate(uniq)]
            try:
                d = gql("{ heroStats { " + " ".join(parts) + " } }")["heroStats"]
                ext[str(hid)] = {bv: {key: fold(events(d.get(f"{key}{i}"), ef[0]), ef)[:EXTRA_TOP]
                                      for key, (_, _, ef) in extras.items()}
                                 for i, (_, bv) in enumerate(uniq)}
            except RuntimeError as e:
                log(f"  stratz extras {hid}: {e}")

        # Матчапы по рангам: с кем и против кого герой выигрывает.
        if mu:
            mname, take, msel, mpaths = mu
            extra_arg = ", take: 200" if take else ""
            parts = [f'm{i}: {mname}(heroId: {hid}, {arg}: [{val}]{extra_arg}) {{ {msel} }}'
                     for i, (arg, val) in enumerate(uniq)]
            try:
                d = gql("{ heroStats { " + " ".join(parts) + " } }")["heroStats"]
                mus[str(hid)] = {bv: fold_matchups(d.get(f"m{i}"), mpaths) for i, (_, bv) in enumerate(uniq)}
                if hid == hero_ids[0]:
                    log(f"  matchups sample: {json.dumps(mus[str(hid)])[:400]}")
            except RuntimeError as e:
                log(f"  stratz matchups {hid}: {e}")
    return ranks, pos, {str(n): v for n, (_, v) in brackets.items()}, ext, mus


EXTRA_TOP = 8


def fold_matchups(node, paths):
    """Пары героя из ответа Stratz → лучшие и худшие соперники и союзники (как в про-матчапах)."""
    out = {}
    for kind in ("vs", "with"):
        best = {}
        for path, win in paths.get(kind, []):
            for e in events(node, path):
                o, n = e.get("heroId2"), e.get("matchCount") or 0
                w = (e.get(win) or 0) if win else None
                if o and n and w is not None and n > best.get(o, [0, 0, 0])[1]:
                    best[o] = [o, n, w]
        out[kind] = rank_pairs(list(best.values()))
    return out


def write(name, data):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {name}: {(OUT / name).stat().st_size // 1024} KB")


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = collect_stats()

    ranks, positions, source, rank_bracket, extras, rank_mu = {}, {}, "", {}, {}, {}
    if STRATZ_TOKEN:
        try:
            ranks, positions, rank_bracket, extras, rank_mu = collect_stratz([h["id"] for h in stats["heroes"]])
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
            extras, rank_mu = prev.get("extras", {}), prev.get("matchups", {})
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
    for per in extras.values():
        for kinds in per.values():
            for lst in kinds.values():
                used.update(i[0] for i in lst)
    raw = od("/constants/items")
    # Третье поле — из каких предметов собирается этот: чтобы убирать компоненты из ленты сборки.
    id_by_name = {k: v.get("id") for k, v in raw.items()}
    items = {str(v["id"]): [v.get("dname") or "", v.get("img") or "",
                            [id_by_name[c] for c in (v.get("components") or []) if id_by_name.get(c)]]
             for v in raw.values() if v.get("id") in used and v.get("dname")}

    write("stats.json", {"updated": now, **stats, "items": items})
    write("builds.json", {"updated": builds_updated, "source": source, "rank_bracket": rank_bracket,
                          "ranks": ranks, "positions": positions, "extras": extras, "matchups": rank_mu})
    write_history(stats["heroes"], now)


HISTORY_DAYS = 21


def write_history(heroes, now):
    """Снимок винрейта и пиков по рангам раз в сутки — для стрелок тренда в тир-листе."""
    try:
        hist = json.loads((OUT / "history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = {"days": {}}
    day = now[:10]
    hist["days"][day] = {str(h["id"]): [[h.get(f"{n}_pick") or 0, h.get(f"{n}_win") or 0] for n in range(1, 8)]
                         for h in heroes}
    for d in sorted(hist["days"])[:-HISTORY_DAYS]:
        del hist["days"][d]
    write("history.json", hist)


if __name__ == "__main__":
    main()
