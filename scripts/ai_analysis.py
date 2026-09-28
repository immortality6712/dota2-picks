#!/usr/bin/env python3
"""Разбор матча моделью из GitHub Models по заявке (issue).

Заявку создаёт кнопка «Разбор через GitHub» на сайте: в заголовке номер матча,
в тексте строка «slot: N» — какого игрока разбирать. Результат пишется в
data/ai/<матч>-<слот>.json (его показывает сайт) и в comment.md для ответа в заявке.
"""

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "ai"
COMMENT = ROOT / "comment.md"
MODEL = os.environ.get("AI_MODEL", "openai/gpt-4.1")
ENDPOINT = "https://models.github.ai/inference/chat/completions"
# Если основная модель не ответила — пробуем попроще; у лёгких моделей бесплатный лимит больше.
FALLBACKS = [(ENDPOINT, MODEL), (ENDPOINT, "openai/gpt-4.1-mini"), (ENDPOINT, "openai/gpt-4o-mini")]
UA = "dota2-picks ai bot (github.com/immortality6712/dota2-picks)"
SITE = "https://immortality6712.github.io/dota2-picks/"

SYSTEM = (
    "Ты опытный тренер по Dota 2. Разбираешь матч игрока по цифрам. Пиши по-русски, коротко и по делу, "
    "обращайся на «ты». Не выдумывай событий, которых нет в данных. Структура ответа: одна строка «Итог:» "
    "с общей оценкой; затем «Что получилось:» — 2–3 пункта; «Ошибки:» — 2–4 пункта с объяснением, почему "
    "это важно; «Что делать в следующей игре:» — 3 конкретных совета для этого героя и роли. "
    "Пункты начинай с «- »."
)

BENCH = [
    ("gold_per_min", "Золото в минуту"),
    ("xp_per_min", "Опыт в минуту"),
    ("last_hits_per_min", "Добито крипов в минуту"),
    ("kills_per_min", "Убийства в минуту"),
    ("hero_damage_per_min", "Урон по героям в минуту"),
    ("hero_healing_per_min", "Лечение в минуту"),
    ("tower_damage", "Урон по строениям"),
]


def get_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


class KeepPost(urllib.request.HTTPRedirectHandler):
    """urllib на переадресации превращает POST в пустой GET — так запрос к модели терялся.
    Не следуем за переадресацией сами: ask_model повторит POST по новому адресу."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(KeepPost)


def post_json(url, body, headers):
    for _ in range(3):
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with OPENER.open(req, timeout=120) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                print(f"переадресация на {url}", file=sys.stderr)
                continue
            raise
    raise RuntimeError("слишком много переадресаций")


def ask_model(prompt):
    errors = []
    for url, model in FALLBACKS:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "temperature": 0.6,
        }).encode()
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        }
        try:
            status, raw = post_json(url, body, headers)
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
            continue
        except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
            errors.append(f"{model}: {e}")
            continue
        try:
            return json.loads(raw)["choices"][0]["message"]["content"].strip(), model
        except (ValueError, KeyError, IndexError, TypeError):
            errors.append(f"{model}: неожиданный ответ (HTTP {status}): {raw[:200]!r}")
    for e in errors:
        print(e, file=sys.stderr)
    raise RuntimeError("; ".join(errors))


def say(text):
    COMMENT.write_text(text + "\n", encoding="utf-8")


def dur(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def summary(m, p, heroes, items):
    name = lambda hid: heroes.get(hid, f"Hero {hid}")
    side = lambda rad: "Силы Света" if rad else "Силы Тьмы"
    roles = {}
    for rad in (True, False):
        team = sorted((x for x in m["players"] if x["isRadiant"] == rad), key=lambda x: -(x.get("net_worth") or 0))
        for i, x in enumerate(team):
            roles[x["player_slot"]] = "кор" if i < 3 else "поддержка"
    team = lambda rad: "; ".join(
        f"{name(x['hero_id'])} ({roles[x['player_slot']]}, {x['kills']}/{x['deaths']}/{x['assists']}, ценность {x.get('net_worth') or 0})"
        for x in m["players"] if x["isRadiant"] == rad)
    b = p.get("benchmarks") or {}
    bench = "; ".join(
        f"{label}: {b[k]['raw']:.2f} (лучше {round(b[k]['pct'] * 100)}% игроков на герое)" if b[k]["raw"] < 10 else
        f"{label}: {round(b[k]['raw'])} (лучше {round(b[k]['pct'] * 100)}% игроков на герое)"
        for k, label in BENCH if b.get(k, {}).get("pct") is not None)
    seen, buys = set(), []
    for x in p.get("purchase_log") or []:
        it = items.get(x["key"])
        if it and it["cost"] >= 1000 and x["time"] > 0 and x["key"] not in seen:
            seen.add(x["key"])
            buys.append(f"{it['name']} на {dur(x['time'])}")
    by_id = {v["id"]: v["name"] for v in items.values()}
    final = ", ".join(by_id[p[f"item_{i}"]] for i in range(6) if p.get(f"item_{i}") in by_id)
    adv = m.get("radiant_gold_adv") or []
    adv_at = ", ".join(f"{t} мин: {adv[t]:+d}" for t in (10, 20, 30, 40) if t < len(adv))
    won = p["isRadiant"] == m["radiant_win"]
    lines = [
        f"Матч {m['match_id']}, длительность {dur(m['duration'])}, победили {side(m['radiant_win'])}, "
        f"счёт {m.get('radiant_score')}:{m.get('dire_score')}.",
        f"Разбираемый игрок: {name(p['hero_id'])} за {side(p['isRadiant'])}, роль {roles[p['player_slot']]}, "
        f"{'победа' if won else 'поражение'}.",
        f"K/D/A {p['kills']}/{p['deaths']}/{p['assists']}, ценность {p.get('net_worth') or 0}, GPM {p.get('gold_per_min')}, "
        f"XPM {p.get('xp_per_min')}, добито {p.get('last_hits')}, не отдано {p.get('denies')}, урон по героям "
        f"{p.get('hero_damage') or 0}, по строениям {p.get('tower_damage') or 0}, лечение {p.get('hero_healing') or 0}.",
        bench and f"Сравнение с другими игроками на этом герое: {bench}.",
        len(p.get("lh_t") or []) > 10
        and f"Добито к 10-й минуте: {p['lh_t'][10]}, не отдано: {(p.get('dn_t') or [0] * 11)[10]}.",
        p.get("lane_efficiency_pct") is not None and f"Эффективность на линии: {p['lane_efficiency_pct']}%.",
        p.get("teamfight_participation") is not None
        and f"Участие в драках: {round(p['teamfight_participation'] * 100)}%.",
        p.get("obs_placed") is not None and f"Поставлено обсерверов {p['obs_placed']}, сентри {p.get('sen_placed') or 0}.",
        p.get("stuns") is not None and f"Станы: {round(p['stuns'])} с. Выкупов: {p.get('buyback_count') or 0}.",
        buys and f"Ключевые покупки: {', '.join(buys[:10])}.",
        final and f"Итоговые предметы: {final}.",
        adv_at and f"Преимущество Сил Света по золоту: {adv_at}.",
        f"Силы Света: {team(True)}.",
        f"Силы Тьмы: {team(False)}.",
    ]
    return "\n".join(x for x in lines if x)


def main():
    text = os.environ.get("ISSUE_TITLE", "") + "\n" + os.environ.get("ISSUE_BODY", "")
    found = re.search(r"\b(\d{8,12})\b", text)
    if not found:
        say("Не нашёл в заявке номер матча. Нужен ID из 8–12 цифр — его видно в клиенте Dota 2 во вкладке «Матчи».")
        return 1
    match_id = found.group(1)
    slot_m = re.search(r"slot:\s*(\d{1,3})", text)

    try:
        m = get_json(f"https://api.opendota.com/api/matches/{match_id}")
    except urllib.error.HTTPError as e:
        say(f"OpenDota не отдала матч {match_id} (HTTP {e.code}). Проверьте номер или попробуйте позже.")
        return 1
    if not m.get("players"):
        say(f"В OpenDota нет данных об игроках матча {match_id}.")
        return 1

    players = {p["player_slot"]: p for p in m["players"]}
    slot = int(slot_m.group(1)) if slot_m and int(slot_m.group(1)) in players else \
        max(m["players"], key=lambda x: x.get("net_worth") or 0)["player_slot"]
    p = players[slot]

    stats = json.loads((ROOT / "data" / "stats.json").read_text(encoding="utf-8"))
    heroes = {h["id"]: h["localized_name"] for h in stats["heroes"]}
    try:
        raw = get_json("https://api.opendota.com/api/constants/items")
        items = {k: {"id": v.get("id"), "name": v.get("dname") or k, "cost": v.get("cost") or 0} for k, v in raw.items()}
    except Exception as e:
        print(f"предметы не загрузились: {e}", file=sys.stderr)
        items = {}

    prompt = summary(m, p, heroes, items)
    print(prompt, file=sys.stderr)
    try:
        answer, model = ask_model(prompt)
    except RuntimeError as e:
        limit = "429" in str(e)
        say("GitHub Models не ответил. " + (
            "Похоже, закончился бесплатный лимит — попробуйте позже или используйте кнопку «Разбор от ИИ» на сайте."
            if limit else f"Подробности:\n\n```\n{str(e)[:900]}\n```"))
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{match_id}-{slot}.json").write_text(json.dumps({
        "match": int(match_id), "slot": slot, "hero": p["hero_id"], "model": model, "text": answer,
        "parsed": bool(m.get("version")),
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    hero = heroes.get(p["hero_id"], "")
    say(f"## Разбор матча {match_id} — {hero}\n\n{answer}\n\n---\n"
        f"Модель `{model}` из GitHub Models. ИИ видит только цифры матча из OpenDota, а не саму игру, и может ошибаться.\n"
        f"{'' if m.get('version') else 'Реплей матча не разобран OpenDota — после разбора у ИИ будет больше данных. '}"
        f"Разбор появится на сайте через минуту-две: {SITE}#tab=player&match={match_id}")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # что бы ни случилось — в заявке должна быть причина, а не молчание
        say(f"Бот споткнулся: `{type(e).__name__}: {str(e)[:500]}`. Попробуйте ещё раз позже.")
        raise
    sys.exit(code)
