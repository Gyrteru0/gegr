#!/usr/bin/env python3
"""
analyze.py — глубокая аналитика разобранной демки Dota 2.

Запускается на папку <матч>_decoded (результат decode.py) и добавляет туда:

  teamfights.jsonl  — командные драки: когда, где, кто участвовал, кто сколько
                      внёс/получил урона, какие способности/предметы нажал,
                      счёт убийств, свинг нетворса, итог одной строкой
  deaths.jsonl      — разбор каждой смерти героя: убийца, ассисты, весь урон
                      за 10 сек до смерти по источникам, место смерти
  builds.json       — билд каждого игрока: порядок прокачки способностей,
                      таланты, порядок предметов, счётчики использований предметов
  laning.json       — лейнинг 0–10 мин: кто против кого, CS/золото/опыт к 10-й,
                      разница с оппонентом по линии
  economy.json      — нетворс/опыт команд по минутам, разница, самый большой перелом
  apm.json          — приказы игроков: количество, в минуту, разбивка по типам
  economy_diff.png  — график разницы нетворса по времени
  heatmap_radiant.png / heatmap_dire.png — тепловые карты позиций команд
  deaths_map.png    — карта всех смертей героев
  wards_map.png     — карта поставленных вардов
  analyze.md        — всё выше человеческим текстом

Каждая запись содержит clock/game_time_s/tick — любой момент потом открывается:
  python moment.py <матч>_decoded --time 23:40 --player 4

Использование:  python analyze.py match_decoded/
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decode import fmt_clock, region_name, write_png, lane_of_pos  # noqa: E402
from moment import ORDER_TYPES  # noqa: E402


def iter_jsonl(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def draw_map_base(px, w, h):
    """Тёмная карта с рекой/лейнами/базами, как в кадрах миникарты."""
    WORLD = 8400

    def to_px(x, y):
        x = max(-WORLD, min(WORLD, x))
        y = max(-WORLD, min(WORLD, y))
        return int((x + WORLD) / (2 * WORLD) * (w - 1)), int((WORLD - y) / (2 * WORLD) * (h - 1))

    def fill_rect(x, y, rw, rh, color):
        for yy in range(max(0, y), min(h, y + rh)):
            for xx in range(max(0, x), min(w, x + rw)):
                if 0 <= xx < w and 0 <= yy < h:
                    i = (yy * w + xx) * 3
                    px[i:i + 3] = bytes(color)

    def line(x1, y1, x2, y2, color, thick=3):
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        for i in range(steps + 1):
            x = int(x1 + (x2 - x1) * i / steps)
            y = int(y1 + (y2 - y1) * i / steps)
            fill_rect(x - thick // 2, y - thick // 2, thick, thick, color)

    px[:] = bytes((16, 42, 26)) * (w * h)
    pa, pb = to_px(-8200, 8200), to_px(8200, -8200)
    line(*pa, *pb, (38, 84, 124), 12)
    for coords in [((-7200, -7200), (7200, 7200)),
                   ((-7200, -7200), (-7200, 7200), (7200, 7200)),
                   ((-7200, -7200), (7200, -7200), (7200, 7200))]:
        for a, b in zip(coords, coords[1:]):
            line(*to_px(*a), *to_px(*b), (92, 92, 78), 4)
    for (bx, by), c in [((-7200, -7200), (46, 140, 60)), ((7200, 7200), (170, 52, 44))]:
        cx, cy = to_px(bx, by)
        fill_rect(cx - 10, cy - 10, 20, 20, c)
    return to_px


def blend(px, w, x, y, color, alpha):
    if 0 <= x < w and 0 <= y < w:
        i = (y * w + x) * 3
        for ch in range(3):
            px[i + ch] = int(px[i + ch] * (1 - alpha) + color[ch] * alpha)


def main():
    if len(sys.argv) < 2:
        print("использование: python analyze.py match_decoded/")
        return 2
    decoded = sys.argv[1]
    tl_path = os.path.join(decoded, "timeline.jsonl")
    cl_path = os.path.join(decoded, "combatlog.jsonl")
    players_list = json.load(open(os.path.join(decoded, "players.json"), encoding="utf-8")) \
        if os.path.isfile(os.path.join(decoded, "players.json")) else []
    players = {p["player_id"]: p for p in players_list}

    print("Читаю боевой лог…")
    hero_deaths = []        # записи DEATH по героям
    hero_damage = []        # (tick, attacker, target, value, inflictor)
    ability_use = Counter()   # (attacker, inflictor)
    item_use = Counter()      # (attacker, inflictor)
    heal_by = Counter()
    for e in iter_jsonl(cl_path):
        tn = e.get("_type_name", "")
        atk, tgt = e.get("_attacker"), e.get("_target")
        if tn == "DOTA_COMBATLOG_DEATH" and e.get("is_target_hero") and tgt and not e.get("will_reincarnate"):
            hero_deaths.append(e)
        elif tn == "DOTA_COMBATLOG_DAMAGE" and e.get("is_attacker_hero") and e.get("is_target_hero"):
            hero_damage.append((e["_tick"], atk, tgt, e.get("value") or 0, e.get("_inflictor")))
        elif tn == "DOTA_COMBATLOG_ABILITY" and atk:
            ability_use[(atk, e.get("_inflictor"))] += 1
        elif tn == "DOTA_COMBATLOG_ITEM" and atk:
            item_use[(atk, e.get("_inflictor"))] += 1
        elif tn == "DOTA_COMBATLOG_HEAL" and atk and e.get("is_target_hero"):
            heal_by[atk] += e.get("value") or 0
    hero_deaths.sort(key=lambda e: e["_tick"])
    print(f"  смертей героев: {len(hero_deaths)}, герой-vs-герой урона: {len(hero_damage)}")

    # карта internal -> игрок/команда появится из таймлайна; пока по players.json
    internal_of_player = {}
    for p in players_list:
        if p.get("hero_internal"):
            internal_of_player[p["player_id"]] = p["hero_internal"]

    print("Читаю таймлайн (билды, лейнинг, экономика, позиции)…")
    need_ticks = sorted({e["_tick"] for e in hero_deaths})
    death_pos = {}          # tick -> {internal: (x, y)}
    death_pos_dist = {}     # tick -> расстояние до ближайшего снимка
    team_nw_series = []     # (tick, game_time, radiant_nw, dire_nw, radiant_xp, dire_xp)
    builds = {}             # player_id -> {...}
    lane_counter = defaultdict(Counter)
    lane_stats = {}         # player_id -> stats около 10:00
    heat = {2: Counter(), 3: Counter()}
    entity_player = defaultdict(Counter)
    wards_seen = {}
    prev_ability_levels = {}
    prev_items = {}
    minute_economy = {}

    def hero_build(pid):
        return builds.setdefault(pid, {"player_id": pid, "ability_order": [],
                                       "talents": [], "item_order": [], "item_uses": {}})

    for entry in iter_jsonl(tl_path):
        gt = entry["game_time_s"]
        heroes = entry.get("heroes", [])
        # командная экономика
        sum_r = sum((h.get("stats", {}).get("net_worth") or 0) for h in heroes if h["team_num"] == 2)
        sum_d = sum((h.get("stats", {}).get("net_worth") or 0) for h in heroes if h["team_num"] == 3)
        sum_rx = sum((h.get("stats", {}).get("total_xp") or 0) for h in heroes if h["team_num"] == 2)
        sum_dx = sum((h.get("stats", {}).get("total_xp") or 0) for h in heroes if h["team_num"] == 3)
        team_nw_series.append((entry["tick"], gt, sum_r, sum_d, sum_rx, sum_dx))
        minute = int(gt // 60)
        if minute not in minute_economy and gt >= 0:
            minute_economy[minute] = {"minute": minute, "clock": entry["clock"],
                                      "radiant_networth": sum_r, "dire_networth": sum_d,
                                      "diff": sum_r - sum_d,
                                      "radiant_xp": sum_rx, "dire_xp": sum_dx}
        # позиции для смертей
        if need_ticks and entry["tick"] <= need_ticks[-1]:
            for t in [t for t in need_ticks if abs(entry["tick"] - t) <= 30]:
                if abs(entry["tick"] - t) < death_pos_dist.get(t, 10 ** 9):
                    death_pos_dist[t] = abs(entry["tick"] - t)
                    death_pos[t] = {h["hero_internal"]: (h["x"], h["y"])
                                    for h in heroes if h.get("x") is not None}
        for h in heroes:
            pid = h.get("player_id")
            if pid is None or pid < 0:
                continue
            entity_player[h["entity_index"]][pid] += 1
            internal_of_player.setdefault(pid, h["hero_internal"])
            b = hero_build(pid)
            b.setdefault("hero", h["hero"])
            b.setdefault("hero_internal", h["hero_internal"])
            b.setdefault("team", h["team"])
            # прокачка
            cur = {a["ability"]: a.get("level") or 0 for a in h.get("abilities", [])}
            prev = prev_ability_levels.get(pid, {})
            for ab, lv in cur.items():
                if lv > prev.get(ab, 0):
                    rec = {"clock": entry["clock"], "game_time_s": gt, "ability": ab, "level": lv}
                    b["ability_order"].append(rec)
                    if "special_bonus" in ab:
                        b["talents"].append(rec)
            prev_ability_levels[pid] = cur
            # предметы: первые появления
            cur_items = {i["item"] for i in h.get("items", []) if i.get("item")}
            if pid not in prev_items:
                prev_items[pid] = cur_items  # стартовый инвентарь — не "тайминг"
            else:
                for it in sorted(cur_items - prev_items[pid]):
                    b["item_order"].append({"clock": entry["clock"], "game_time_s": gt, "item": it})
                prev_items[pid] |= cur_items
            # лейнинг и тепло
            if h.get("x") is not None:
                gx, gy = int((h["x"] + 8400) // 280), int((h["y"] + 8400) // 280)
                if h["team_num"] in heat and h["alive"]:
                    heat[h["team_num"]][(gx, gy)] += 1
                if 0 <= gt <= 600 and h["alive"]:
                    base = (-7200, -7200) if h["team_num"] == 2 else (7200, 7200)
                    if math.hypot(h["x"] - base[0], h["y"] - base[1]) > 2800:
                        lane_counter[pid][lane_of_pos(h["x"], h["y"])] += 1
                if gt <= 600:
                    lane_stats[pid] = h.get("stats", {})
        for n in entry.get("npcs", []):
            if n.get("class") in ("CDOTA_NPC_Observer_Ward", "CDOTA_NPC_Sentry_Ward"):
                key = (n["class"], n["entity_index"])
                if key not in wards_seen and n.get("x") is not None:
                    wards_seen[key] = {"class": n["class"], "team": n["team"],
                                       "x": n["x"], "y": n["y"], "clock": entry["clock"],
                                       "game_time_s": gt, "region": region_name(n["x"], n["y"])}

    for (atk, infl), cnt in item_use.items():
        for pid, internal in internal_of_player.items():
            if internal == atk:
                hero_build(pid)["item_uses"][infl or "?"] = cnt
    # команда/герой по internal-имени
    team_of_internal, hero_of_internal = {}, {}
    for pid, internal in internal_of_player.items():
        p = players.get(pid, {})
        hero_of_internal[internal] = p.get("hero") or internal.replace("npc_dota_hero_", "")
        team_of_internal[internal] = p.get("team_num")

    pid_by_internal = {v: k for k, v in internal_of_player.items()}

    def resolve_internal(internal):
        if not internal:
            return None
        if internal in pid_by_internal:
            return internal
        for known in pid_by_internal:
            if internal.startswith(known + "_"):
                return known
        # саммоны без 'hero_': npc_dota_brewmaster_earth_1 и т.п.
        for known in pid_by_internal:
            short = known.replace("npc_dota_hero_", "")
            if internal.startswith("npc_dota_" + short + "_"):
                return known
        return None

    def hero_name_of(internal):
        base = resolve_internal(internal)
        if base:
            return hero_of_internal.get(base, base)
        name = (internal or "")
        name = name[len("npc_dota_hero_"):] if name.startswith("npc_dota_hero_") else name
        name = name[len("npc_dota_"):] if name.startswith("npc_dota_") else name
        if name == "roshan":
            return "Рошан"
        return name or None

    def pid_of_internal(internal):
        base = resolve_internal(internal)
        return pid_by_internal.get(base) if base else None

    # ---------------- смерти: разбор ----------------
    deaths_out = []
    for e in hero_deaths:
        tick, victim = e["_tick"], e["_target"]
        # позиция: location_x/y из лога, иначе герой из ближайшего снимка
        x = e.get("location_x")
        y = e.get("location_y")
        if x is None:
            pos = death_pos.get(tick, {}).get(victim)
            if pos:
                x, y = pos
        taken = defaultdict(int)
        hits = Counter()
        for (t, atk, tgt, val, infl) in hero_damage:
            if tgt == victim and tick - 300 <= t <= tick:
                taken[(atk, infl)] += val
                hits[(atk, infl)] += 1
        dmg_list = [{"attacker": a, "attacker_hero": hero_name_of(a), "inflictor": i,
                     "damage": v, "hits": hits[(a, i)]}
                    for (a, i), v in sorted(taken.items(), key=lambda kv: -kv[1])[:6]]
        killer_pid = pid_of_internal(e.get("_attacker"))
        assists = []
        for v in (e.get("assist_players") or []):
            if isinstance(v, (int, float)) and int(v) != killer_pid:
                ap = players.get(int(v), {})
                if ap:
                    assists.append({"player_id": int(v), "hero": ap.get("hero")})
        deaths_out.append({
            "tick": tick, "game_time_s": round(tick / 30.0, 1), "clock": None,
            "victim": victim, "victim_hero": hero_of_internal.get(victim, victim),
            "victim_team": team_of_internal.get(victim),
            "killer": e.get("_attacker"), "killer_hero": hero_name_of(e.get("_attacker")), "killer_player_id": killer_pid,
            "assists": assists, "inflictor": e.get("_inflictor"),
            "x": x, "y": y, "region": region_name(x, y) if x is not None else None,
            "damage_taken_last_10s": dmg_list,
            "total_damage_taken_10s": sum(d["damage"] for d in dmg_list),
        })
    # игровое время смертей берём из ближайшей записи таймлайна по тику
    tick_to_clock = {}
    for (tick, gt, *_rest) in team_nw_series:
        tick_to_clock[tick] = gt
    series_ticks = [s[0] for s in team_nw_series]

    def clock_for_tick(tick):
        # ближайшее известное соответствие тик -> игровое время (серия по секундам)
        if not series_ticks:
            return fmt_clock(tick / 30.0), tick / 30.0
        import bisect
        i = bisect.bisect_left(series_ticks, tick)
        cands = []
        if i < len(series_ticks):
            cands.append(series_ticks[i])
        if i > 0:
            cands.append(series_ticks[i - 1])
        best = min(cands, key=lambda t: abs(t - tick))
        gt = tick_to_clock[best] + (tick - best) / 30.0
        return fmt_clock(gt), round(gt, 1)

    for d in deaths_out:
        d["clock"], d["game_time_s"] = clock_for_tick(d["tick"])

    # ---------------- командные драки ----------------
    print("Ищу командные драки…")
    fights = []
    clusters = []
    cur = []
    for e in hero_deaths:
        if cur and e["_tick"] - cur[-1]["_tick"] > 45 * 30:
            clusters.append(cur)
            cur = []
        cur.append(e)
    if cur:
        clusters.append(cur)
    for cl in clusters:
        if len(cl) < 2:
            continue  # одиночные пикоффы — не командная драка (они есть в deaths.jsonl)
        t0 = cl[0]["_tick"] - 15 * 30
        t1 = cl[-1]["_tick"] + 10 * 30
        dealt, taken_d, used = defaultdict(int), defaultdict(int), defaultdict(Counter)
        participants = set()
        for (t, atk, tgt, val, infl) in hero_damage:
            if t0 <= t <= t1:
                dealt[atk] += val
                taken_d[tgt] += val
                participants.add(atk)
                participants.add(tgt)
        kills_r = sum(1 for e in cl if team_of_internal.get(e["_target"]) == 3)
        kills_d = sum(1 for e in cl if team_of_internal.get(e["_target"]) == 2)

        def nw_at(tick):
            import bisect
            i = bisect.bisect_left(series_ticks, tick)
            vals = []
            if i < len(series_ticks):
                vals.append(team_nw_series[i])
            if i > 0:
                vals.append(team_nw_series[i - 1])
            if not vals:
                return 0
            s = min(vals, key=lambda s: abs(s[0] - tick))
            return s[2] - s[3]

        swing = nw_at(t1) - nw_at(t0)
        # место: центр точек смертей
        pts = []
        for e in cl:
            if e.get("location_x") is not None:
                pts.append((e["location_x"], e["location_y"]))
            else:
                pos = death_pos.get(e["_tick"], {}).get(e["_target"])
                if pos:
                    pts.append(pos)
        cx = sum(p[0] for p in pts) / len(pts) if pts else None
        cy = sum(p[1] for p in pts) / len(pts) if pts else None
        clock0, gt0 = clock_for_tick(cl[0]["_tick"])
        kind = "teamfight" if len(cl) >= 3 else "skirmish"
        kind_ru = "командная драка" if kind == "teamfight" else "стычка"
        winner = "Radiant" if kills_r > kills_d else "Dire" if kills_d > kills_r else "равный размен"
        kill_list = [f"{hero_name_of(e['_target'])} (убил {hero_name_of(e.get('_attacker'))})" for e in cl]
        fights.append({
            "kind": kind, "deaths_total": len(cl),
            "start_tick": cl[0]["_tick"], "end_tick": cl[-1]["_tick"],
            "clock": clock0, "game_time_s": gt0,
            "duration_s": round((cl[-1]["_tick"] - cl[0]["_tick"]) / 30.0, 1),
            "x": cx, "y": cy, "region": region_name(cx, cy) if cx is not None else None,
            "kills_radiant": kills_r, "kills_dire": kills_d, "outcome": winner,
            "deaths": kill_list,
            "participants": sorted(participants),
            "damage_dealt": {hero_name_of(k): v for k, v in sorted(dealt.items(), key=lambda kv: -kv[1])},
            "damage_taken": {hero_name_of(k): v for k, v in sorted(taken_d.items(), key=lambda kv: -kv[1])},
            "gold_swing_radiant": swing,
            "summary": (f"{clock0} {kind_ru} в районе «{region_name(cx, cy) if cx is not None else '?'}»: "
                        f"Radiant {kills_r} — {kills_d} Dire по убийствам, итог: {winner}; "
                        f"свинг нетворса Radiant за драку: {swing:+}"),
        })

    # ---------------- лейнинг ----------------
    laning = {}
    for pid, cnt in lane_counter.items():
        lane, _n = cnt.most_common(1)[0]
        p = players.get(pid, {})
        st = lane_stats.get(pid, {})
        laning[pid] = {"player_id": pid, "name": p.get("name"), "hero": p.get("hero"),
                       "team": p.get("team"), "lane": lane,
                       "last_hits_10": st.get("last_hits"), "denies_10": st.get("denies"),
                       "total_gold_10": st.get("total_gold"), "total_xp_10": st.get("total_xp"),
                       "net_worth_10": st.get("net_worth")}
    for pid, row in laning.items():
        opps = [o for o in laning.values()
                if o["lane"] == row["lane"] and o["team"] != row["team"]]
        row["opponents"] = [o["hero"] for o in opps]
        if opps and row["total_gold_10"] is not None:
            row["gold_diff_vs_lane"] = row["total_gold_10"] - \
                sum(o["total_gold_10"] or 0 for o in opps) / len(opps)

    # ---------------- экономика ----------------
    econ_series = [minute_economy[m] for m in sorted(minute_economy)]
    # обрезаем послематчевый хвост: после конца игры снимки повторяют финальную статистику
    while len(econ_series) > 1:
        a, b = econ_series[-2], econ_series[-1]
        if (a["radiant_networth"], a["dire_networth"], a["radiant_xp"], a["dire_xp"]) == \
                (b["radiant_networth"], b["dire_networth"], b["radiant_xp"], b["dire_xp"]):
            econ_series.pop()
        else:
            break
    biggest = None
    for a, b in zip(econ_series, econ_series[1:]):
        ch = b["diff"] - a["diff"]
        if biggest is None or abs(ch) > abs(biggest["change"]):
            biggest = {"from_minute": a["minute"], "to_minute": b["minute"], "change": ch}
    economy = {"series": econ_series, "biggest_swing_1min": biggest,
               "final_diff": econ_series[-1]["diff"] if econ_series else None}

    # ---------------- APM ----------------
    entity_to_player = {idx: c.most_common(1)[0][0] for idx, c in entity_player.items()}
    apm_counter = Counter()
    apm_types = defaultdict(Counter)
    total_orders = 0
    for o in iter_jsonl(os.path.join(decoded, "unit_orders.jsonl")):
        units_ = o.get("units") or []
        pid = None
        for u in units_:
            if u in entity_to_player:
                pid = entity_to_player[u]
                break
        if pid is None and o.get("entindex") in entity_to_player:
            pid = entity_to_player[o["entindex"]]
        if pid is None:
            continue
        total_orders += 1
        apm_counter[pid] += 1
        apm_types[pid][ORDER_TYPES.get(o.get("order_type"), str(o.get("order_type")))] += 1
    duration_min = max(1.0, (team_nw_series[-1][1] if team_nw_series else 0) / 60.0)
    apm = {str(pid): {"player_id": pid, "hero": players.get(pid, {}).get("hero"),
                      "orders": n, "orders_per_min": round(n / duration_min, 1),
                      "by_type": dict(apm_types[pid].most_common())}
           for pid, n in apm_counter.items()}

    # ---------------- запись ----------------
    def dump(name, obj):
        with open(os.path.join(decoded, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)

    def dump_jsonl(name, rows):
        with open(os.path.join(decoded, name), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    dump_jsonl("teamfights.jsonl", fights)
    dump_jsonl("deaths.jsonl", deaths_out)
    dump("builds.json", [builds[k] for k in sorted(builds)])
    dump("laning.json", [laning[k] for k in sorted(laning)])
    dump("economy.json", economy)
    dump("apm.json", apm)
    dump("wards.json", list(wards_seen.values()))

    # график разницы нетворса
    if econ_series:
        w, h = 1080, 520
        px = bytearray(w * h * 3)
        px[:] = bytes((18, 20, 26)) * (w * h)

        def rect(x, y, rw, rh, c):
            for yy in range(max(0, y), min(h, y + rh)):
                for xx in range(max(0, x), min(w, x + rw)):
                    i = (yy * w + xx) * 3
                    px[i:i + 3] = bytes(c)
        maxabs = max(1000, max(abs(s["diff"]) for s in econ_series))
        midy = h // 2
        rect(60, midy, w - 80, 2, (120, 120, 120))
        pts = []
        for i, s in enumerate(econ_series):
            x = 60 + int(i / max(1, len(econ_series) - 1) * (w - 120))
            y = midy - int(s["diff"] / maxabs * (h // 2 - 30))
            pts.append((x, y, s["diff"]))
        for (x1, y1, _d1), (x2, y2, _d2) in zip(pts, pts[1:]):
            steps = max(abs(x2 - x1), abs(y2 - y1), 1)
            for i in range(steps + 1):
                x = int(x1 + (x2 - x1) * i / steps)
                y = int(y1 + (y2 - y1) * i / steps)
                c = (80, 220, 110) if y < midy else (235, 90, 70)
                rect(x - 2, y - 2, 5, 5, c)
        for x, y, _d in pts[:: max(1, len(pts) // 12)]:
            rect(x - 3, y - 3, 7, 7, (255, 255, 255))
        write_png(os.path.join(decoded, "economy_diff.png"), w, h, px)

    # тепловые карты и карты смертей/вардов
    for team_num, fname, base_color in ((2, "heatmap_radiant.png", (60, 255, 120)),
                                        (3, "heatmap_dire.png", (255, 80, 60))):
        w = h = 720
        px = bytearray(w * h * 3)
        to_px = draw_map_base(px, w, h)
        grid = heat[team_num]
        if grid:
            # масштаб — по 98-му перцентилю и корню: иначе фонтан (где герои стоят
            # минутами) съедает всю шкалу и карта получается пустой
            vals = sorted(grid.values())
            mx = vals[min(len(vals) - 1, int(len(vals) * 0.98))] or 1
            cell = w // 60
            for (gx, gy), cnt in grid.items():
                # gy считали от юга (y вверх), на экране север сверху
                sx, sy = gx * cell, (59 - gy) * cell
                a = min(0.9, (cnt / mx) ** 0.5 * 0.85 + 0.05)
                for yy in range(sy, sy + cell):
                    for xx in range(sx, sx + cell):
                        blend(px, w, xx, yy, base_color, a * 0.7)
        write_png(os.path.join(decoded, fname), w, h, px)

    for fname, points in (("deaths_map.png", [(d["x"], d["y"], d["victim_team"]) for d in deaths_out
                                               if d["x"] is not None]),
                          ("wards_map.png", [(wd["x"], wd["y"], 2 if wd["team"] == "Radiant" else 3)
                                             for wd in wards_seen.values()])):
        w = h = 720
        px = bytearray(w * h * 3)
        to_px = draw_map_base(px, w, h)
        for (x, y, team_num) in points:
            cx, cy = to_px(x, y)
            c = (60, 255, 120) if team_num == 2 else (255, 80, 60)
            for yy in range(cy - 5, cy + 6):
                for xx in range(cx - 5, cx + 6):
                    if (xx - cx) ** 2 + (yy - cy) ** 2 <= 25:
                        blend(px, w, xx, yy, c, 0.9)
        write_png(os.path.join(decoded, fname), w, h, px)

    # ---------------- текстовый отчёт ----------------
    L = ["# Глубокая аналитика матча\n"]
    L.append(f"## Драки и стычки ({len(fights)}: командных {sum(1 for f_ in fights if f_.get('kind') == 'teamfight')}, "
             f"стычек {sum(1 for f_ in fights if f_.get('kind') != 'teamfight')})\n")
    for f_ in fights:
        L.append(f"- {f_['summary']}")
        top = ", ".join(f"{k}: {v}" for k, v in list(f_["damage_dealt"].items())[:3])
        L.append(f"  Топ урона: {top}. Смерти: {'; '.join(f_['deaths'])}")
    L.append(f"\n## Смерти героев ({len(deaths_out)})\n")
    for d in deaths_out:
        who = ", ".join(f"{x.get('attacker_hero') or x.get('attacker')}({x['damage']})" for x in d["damage_taken_last_10s"][:3])
        L.append(f"- `{d['clock']}` {d['victim_hero']} убит {d['killer_hero'] or '—'} "
                 f"в «{d['region']}»; урон за 10 c: {who or '—'}")
    L.append("\n## Лейнинг (к 10-й минуте)\n")
    L.append("| Игрок | Герой | Линия | Против | CS | Денаи | Золото | Разница |")
    L.append("|---|---|---|---|---|---|---|---|")
    for pid in sorted(laning):
        r = laning[pid]
        L.append(f"| {r.get('name') or pid} | {r.get('hero')} | {r['lane']} | "
                 f"{', '.join(r.get('opponents') or [])} | {r.get('last_hits_10')} | "
                 f"{r.get('denies_10')} | {r.get('total_gold_10')} | "
                 f"{r.get('gold_diff_vs_lane') if r.get('gold_diff_vs_lane') is None else round(r['gold_diff_vs_lane'])} |")
    if biggest:
        L.append(f"\n## Экономика\nСамый большой перелом за минуту: "
                 f"{biggest['change']:+} золота к Radiant между {biggest['from_minute']}-й и "
                 f"{biggest['to_minute']}-й минутами. Финальная разница: {economy['final_diff']:+}. "
                 f"График: `economy_diff.png`.")
    L.append("\n## Билды (кратко)\n")
    for pid in sorted(builds):
        b = builds[pid]
        items = " → ".join(x["item"].replace("item_", "") for x in b["item_order"][:14])
        talents = ", ".join(f"{x['clock']}:{x['ability'].replace('special_bonus_', '')}"
                            for x in b["talents"])
        L.append(f"- {b.get('hero')} (игрок {pid}): {items or '—'}"
                 + (f"; таланты: {talents}" if talents else ""))
    L.append("\n## Активность (приказы)\n")
    for pid in sorted(apm, key=int):
        a = apm[pid]
        L.append(f"- {a.get('hero') or pid}: {a['orders']} приказов ({a['orders_per_min']}/мин)")
    L.append("\nЛюбой момент открывается: `python moment.py <папка> --time MM:SS --player N`\n")
    with open(os.path.join(decoded, "analyze.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"Готово: драк {len(fights)}, смертей {len(deaths_out)}, вардов {len(wards_seen)}. "
          f"Отчёт: analyze.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
