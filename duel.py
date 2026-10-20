#!/usr/bin/env python3
"""
duel.py — дуэльный разбор 1v1 (например, человек на Shadow Fiend против бота
на Shadow Fiend, соло мид) поверх разобранной демки decode.py.

  python duel.py match_decoded/ [--players 0,1] [--lane мид]

Пишет в папку разбора:
  DUEL.md    — компактный отчёт для человека и ИИ: поминутная таблица двух
               игроков (CS/денаи/нетворс/опыт/уровни/души), точность рейзов,
               урон, смерти, эпизоды отступлений на низком HP, положение волны
               крипов по минутам, урон по Т1.
  duel.json  — то же в структурированном виде (его читает compare.py).

Пара выбирается автоматически: сначала человек (is_bot=False), против него —
самый активный герой другой команды (обычно бот). Явно: --players 3,8.

Чего тут нет и почему: в зеркальном матчапе (два одинаковых героя) боевой лог
не различает героев — урон/касты в нём общие на пару. Поэтому касты каждого
игрока считаются по кулдаунам из снимков (они индивидуальны), а попадания
рейзов по герою приписываются игроку, только когда второй в этот момент тот же
рейз не бросал (однозначные случаи; покрытие указывается).
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decode import fmt_clock, lane_of_pos, region_name  # noqa: E402

BASE_PT = {2: (-7200, -7200), 3: (7200, 7200)}


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


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def pick_pair(decoded, players_arg=None):
    players = load_json(os.path.join(decoded, "players.json"), [])
    cand = [p for p in players if p.get("hero") and p.get("team_num") in (2, 3)]

    def activity(p):
        st = p.get("final_stats") or {}
        return (st.get("last_hits") or 0) + (st.get("total_gold") or 0) / 1000.0

    if players_arg:
        ids = [int(x) for x in players_arg.split(",")]
        by_id = {p["player_id"]: p for p in players}
        return [by_id[i] for i in ids if i in by_id]
    humans = [p for p in cand if p.get("is_bot") is False]
    p1 = max(humans, key=activity) if humans else (max(cand, key=activity) if cand else None)
    if p1 is None:
        return []
    foes = [p for p in cand if p["team_num"] != p1["team_num"] and p["player_id"] != p1["player_id"]]
    p2 = max(foes, key=activity) if foes else None
    return [p for p in (p1, p2) if p]


def compute_duel(decoded, players_arg=None, lane="мид"):
    pair = pick_pair(decoded, players_arg)
    if len(pair) < 2:
        raise SystemExit("Не нашёл двух игроков для дуэли (укажите --players A,B)")
    pids = [p["player_id"] for p in pair]
    info = {p["player_id"]: {"player_id": p["player_id"], "name": p.get("name"),
                             "hero": p.get("hero"), "team": p.get("team"),
                             "team_num": p.get("team_num"), "is_bot": p.get("is_bot")}
            for p in pair}

    # internal-имена героев пары — мини-проход по первым снимкам таймлайна
    for e in iter_jsonl(os.path.join(decoded, "timeline.jsonl")):
        for h in e.get("heroes", []):
            if h.get("player_id") in info and h.get("hero_internal"):
                info[h["player_id"]].setdefault("hero_internal", h["hero_internal"])
        if all(info[pid].get("hero_internal") for pid in pids):
            break
    internals = {pid: info[pid].get("hero_internal") for pid in pids}
    mirror = internals[pids[0]] == internals[pids[1]]
    pair_set = {internals[pids[0]], internals[pids[1]]}

    # --- боевой лог (первым: нужен кап душ и попадания рейзов) ---
    dmg_by_inflictor = Counter()
    dmg_by_attacker = Counter()
    raze_hits_dmg = []
    stack_series = defaultdict(list)
    stack_max_pair = 0
    cl_deaths = []
    for e in iter_jsonl(os.path.join(decoded, "combatlog.jsonl")):
        tn = e.get("_type_name")
        if tn == "DOTA_COMBATLOG_DAMAGE" and e.get("is_attacker_hero") and e.get("is_target_hero"):
            a, t = e.get("_attacker"), e.get("_target")
            if a in pair_set and t in pair_set and (mirror or a != t) \
                    and not e.get("target_is_self"):
                dmg_by_inflictor[e.get("_inflictor") or "(атака)"] += e.get("value") or 0
                if not mirror:
                    dmg_by_attacker[a] += e.get("value") or 0
                if "shadowraze" in (e.get("_inflictor") or ""):
                    raze_hits_dmg.append((e["_tick"], e.get("_inflictor") or "", e.get("value") or 0))
        elif tn == "DOTA_COMBATLOG_MODIFIER_STACK_EVENT" and "necromastery" in (e.get("_inflictor") or ""):
            if e.get("_target") in pair_set:
                stack_series[e["_target"]].append((e["_tick"], e.get("stack_count") or 0))
                stack_max_pair = max(stack_max_pair, e.get("stack_count") or 0)
        elif tn == "DOTA_COMBATLOG_DEATH" and e.get("is_target_hero") \
                and e.get("_target") in pair_set and not e.get("will_reincarnate"):
            cl_deaths.append(e)
    souls_cap = stack_max_pair or None  # кап душ: максимум стаков из лога этой игры

    series = {pid: [] for pid in pids}          # (gt, tick, hp_pct, x, y, alive, level, stats)
    minute_best = {}                            # minute -> {pid: (dist, row)}
    wave_min = defaultdict(lambda: {"sx": 0.0, "sy": 0.0, "n": 0, "r_n": 0, "d_n": 0, "cnt": 0})
    tower_hp = {}                               # name -> [(gt, hp)]
    tower_first_dmg = {}
    casts = {pid: [] for pid in pids}           # (tick, ability)
    prev_ab = {pid: {} for pid in pids}
    prev_lh = {pid: None for pid in pids}
    prev_alive = {pid: True for pid in pids}
    souls_est = {pid: 0 for pid in pids}
    souls_min = {}                              # minute -> {pid: est}
    lane_counter = Counter()
    duration = 0.0

    for e in iter_jsonl(os.path.join(decoded, "timeline.jsonl")):
        gt, tick = e["game_time_s"], e["tick"]
        duration = max(duration, gt)
        heroes = {h.get("player_id"): h for h in e.get("heroes", [])}
        for pid in pids:
            h = heroes.get(pid)
            if not h:
                continue
            info[pid].setdefault("hero_internal", h.get("hero_internal"))
            st = h.get("stats") or {}
            series[pid].append((gt, tick, h.get("hp_pct"), h.get("x"), h.get("y"),
                                bool(h.get("alive")), h.get("level"), st))
            if gt >= 0:
                m = int(round(gt / 60.0))
                if abs(gt - m * 60) <= 20:
                    slot = minute_best.setdefault(m, {})
                    d = abs(gt - m * 60)
                    if pid not in slot or d < slot[pid][0]:
                        necro_lv = next((a.get("level") or 0 for a in h.get("abilities", [])
                                         if "necromastery" in (a.get("ability") or "")), 0)
                        slot[pid] = (d, {"last_hits": st.get("last_hits"),
                                         "denies": st.get("denies"),
                                         "net_worth": st.get("net_worth"),
                                         "total_gold": st.get("total_gold"),
                                         "total_xp": st.get("total_xp"),
                                         "level": h.get("level"), "hp_pct": h.get("hp_pct"),
                                         "necro_level": necro_lv, "alive": bool(h.get("alive"))})
            # оценка душ по ластхитам (смерти делят оценку пополам в постобработке ниже)
            lh = st.get("last_hits")
            if lh is not None:
                if prev_lh[pid] is not None and lh > prev_lh[pid]:
                    souls_est[pid] += lh - prev_lh[pid]
                    if souls_cap:
                        souls_est[pid] = min(souls_est[pid], souls_cap)
                prev_lh[pid] = lh
            if prev_alive[pid] and not h.get("alive"):
                souls_est[pid] //= 2  # смерть теряет половину душ (оценка)
            prev_alive[pid] = bool(h.get("alive"))
            if gt >= 0 and int(gt // 60) != int((gt - 1) // 60):
                souls_min.setdefault(int(gt // 60), {})[pid] = souls_est[pid]
            if 0 <= gt <= 600 and h.get("x") is not None and h.get("alive"):
                lane_counter[lane_of_pos(h["x"], h["y"])] += 1
            # касты по переходам кулдауна/фазы (индивидуально по сущности)
            cur = {a["ability"]: a for a in h.get("abilities", []) if a.get("ability")}
            for ab, a in cur.items():
                pv = prev_ab[pid].get(ab)
                if pv is not None:
                    phase_start = a.get("in_ability_phase") and not pv.get("in_ability_phase")
                    cd_jump = (a.get("cooldown_left") or 0) > (pv.get("cooldown_left") or 0) + 1.0 \
                        and (a.get("cooldown_left") or 0) >= 2
                    if phase_start or cd_jump:
                        casts[pid].append((tick, ab))
            prev_ab[pid] = cur
        # волна на основной линии
        cr = e.get("creeps") or {}
        if cr:
            w = wave_min[int(gt // 60)] if gt >= 0 else None
            if w is not None:
                rn = (cr.get("2") or {}).get(lane)
                dn = (cr.get("3") or {}).get(lane)
                if rn and dn:
                    w["sx"] += (rn["cx"] + dn["cx"]) / 2
                    w["sy"] += (rn["cy"] + dn["cy"]) / 2
                    w["r_n"] += rn["n"]
                    w["d_n"] += dn["n"]
                    w["cnt"] += 1
        # HP башен Т1 основной линии
        for n in e.get("npcs", []):
            nm = n.get("name") or ""
            if nm.endswith(f"T1 {lane}") and n.get("hp") is not None:
                tower_hp.setdefault(nm, []).append((gt, n["hp"], n.get("hp_max")))
                if (n.get("hp_max") and n["hp"] < n["hp_max"]
                        and nm not in tower_first_dmg):
                    tower_first_dmg[nm] = e["clock"]

    main_lane = lane_counter.most_common(1)[0][0] if lane_counter else lane

    # смерти по снимкам (индивидуально) + убийца из боевого лога рядом по тику
    deaths = {pid: [] for pid in pids}
    for pid in pids:
        dead_since = None
        for (gt, tick, _hp, _x, _y, alive, _lv, _st) in series[pid]:
            if not alive and dead_since is None:
                dead_since = (gt, tick)
            elif alive and dead_since is not None:
                deaths[pid].append({"clock": fmt_clock(dead_since[0]), "tick": dead_since[1],
                                    "respawn_s": round(gt - dead_since[0], 1)})
                dead_since = None
        if dead_since is not None:
            deaths[pid].append({"clock": fmt_clock(dead_since[0]), "tick": dead_since[1],
                                "respawn_s": None})

    # отступления: HP<35% и за 12 c заметно ближе к своей базе, либо у фонтана
    retreats = {pid: [] for pid in pids}
    for pid in pids:
        rows = series[pid]
        base = BASE_PT.get(info[pid]["team_num"], (-7200, -7200))
        i = 0
        while i < len(rows):
            gt, _t, hp, x, y, alive, _lv, _st = rows[i]
            if alive and hp is not None and hp < 35 and x is not None and gt >= 0:
                d0 = math.hypot(x - base[0], y - base[1])
                min_d, min_hp, j = d0, hp, i
                while j < len(rows) and rows[j][0] <= gt + 12:
                    if rows[j][3] is not None:
                        min_d = min(min_d, math.hypot(rows[j][3] - base[0], rows[j][4] - base[1]))
                    if rows[j][2] is not None:
                        min_hp = min(min_hp, rows[j][2])
                    j += 1
                if d0 - min_d >= 1500 or min_d < 2600:
                    retreats[pid].append({"clock": fmt_clock(gt), "game_time_s": round(gt, 1),
                                          "min_hp_pct": min_hp,
                                          "to_fountain": bool(min_d < 2600),
                                          "dist_gain": round(d0 - min_d)})
                    i = j + 15  # не плодить эпизоды на одном отступлении
                    continue
            i += 1

    # убийцы для смертей из снимков
    for pid in pids:
        for d in deaths[pid]:
            near = min(cl_deaths, key=lambda e: abs(e["_tick"] - d["tick"]), default=None)
            if near is not None and abs(near["_tick"] - d["tick"]) <= 120:
                d["killer"] = (near.get("_attacker") or "").replace("npc_dota_hero_", "")
                d["inflictor"] = near.get("_inflictor")

    # минуты
    minutes = []
    for m in sorted(minute_best):
        row = {"minute": m, "clock": fmt_clock(m * 60)}
        for pid in pids:
            r = minute_best[m].get(pid, (0, {}))[1]
            row[f"p{pid}"] = r
        a, b = row.get(f"p{pids[0]}", {}), row.get(f"p{pids[1]}", {})
        if a.get("net_worth") is not None and b.get("net_worth") is not None:
            row["nw_diff"] = a["net_worth"] - b["net_worth"]
        if a.get("total_xp") is not None and b.get("total_xp") is not None:
            row["xp_diff"] = a["total_xp"] - b["total_xp"]
        w = wave_min.get(m)
        if w and w["cnt"]:
            row["wave"] = {"x": round(w["sx"] / w["cnt"]), "y": round(w["sy"] / w["cnt"]),
                           "creeps_radiant": round(w["r_n"] / w["cnt"], 1),
                           "creeps_dire": round(w["d_n"] / w["cnt"], 1)}
            row["wave"]["region"] = region_name(row["wave"]["x"], row["wave"]["y"])
        for nm, hist in tower_hp.items():
            cand = [hp for (g2, hp, _mx) in hist if abs(g2 - m * 60) <= 20]
            if cand:
                row[f"tower_{nm}"] = cand[-1]
        minutes.append(row)
    for row in minutes:
        for pid in pids:
            sm = souls_min.get(row["minute"], {}).get(pid)
            if sm is not None:
                row[f"p{pid}"]["souls_est"] = sm

    # души из стаков боевого лога, если герои разные (это точные значения)
    souls_exact_min = {}
    if not mirror:
        tick0 = None
        # соответствие тик->игровое время возьмём из series первого игрока
        ref = series[pids[0]]
        import bisect
        ref_ticks = [r[1] for r in ref]

        def gt_of_tick(t):
            i = bisect.bisect_left(ref_ticks, t)
            cand = []
            if i < len(ref):
                cand.append(ref[i])
            if i > 0:
                cand.append(ref[i - 1])
            if not cand:
                return 0.0
            r = min(cand, key=lambda r: abs(r[1] - t))
            return r[0] + (t - r[1]) / 30.0

        for pid in pids:
            evs = sorted(stack_series.get(internals[pid], []))
            per_min = {}
            for (t, sc) in evs:
                per_min[int(gt_of_tick(t) // 60)] = sc
            souls_exact_min[pid] = per_min
        for row in minutes:
            for pid in pids:
                v = souls_exact_min.get(pid, {}).get(row["minute"])
                if v is not None:
                    row[f"p{pid}"]["souls"] = v

    # рейзы: касты каждого (по кулдаунам), попадания по герою — однозначно атрибутированные
    raze = {}
    all_casts = {pid: [c for c in casts[pid] if "shadowraze" in c[1]] for pid in pids}
    for pid in pids:
        other = pids[1] if pid == pids[0] else pids[0]
        per_tier = {}
        for tier in ("shadowraze1", "shadowraze2", "shadowraze3"):
            my = [t for (t, ab) in all_casts[pid] if ab.endswith(tier)]
            op = [t for (t, ab) in all_casts[other] if ab.endswith(tier)]
            hits = amb = 0
            for t in my:
                if any(abs(t - ot) <= 60 for ot in op):
                    amb += 1
                    continue
                if any(t - 30 <= ht <= t + 75 and hi.endswith(tier) for (ht, hi, _v) in raze_hits_dmg):
                    hits += 1
            per_tier[tier] = {"casts": len(my), "hits_hero_attributed": hits, "ambiguous": amb}
        tot_c = sum(v["casts"] for v in per_tier.values())
        tot_h = sum(v["hits_hero_attributed"] for v in per_tier.values())
        tot_a = sum(v["ambiguous"] for v in per_tier.values())
        raze[str(pid)] = {"by_tier": per_tier, "casts": tot_c, "hits": tot_h,
                          "ambiguous": tot_a,
                          "accuracy_attributed_pct": round(100 * tot_h / (tot_c - tot_a), 1)
                          if tot_c - tot_a > 0 else None}
    raze_dmg_by_tier = {}
    for tier in ("shadowraze1", "shadowraze2", "shadowraze3"):
        vals = [v for (_t, hi, v) in raze_hits_dmg if hi.endswith(tier)]
        raze_dmg_by_tier[tier] = {"hits_events": len(vals), "damage": sum(vals)}

    towers_out = {}
    for nm, hist in tower_hp.items():
        towers_out[nm] = {"first_damage_clock": tower_first_dmg.get(nm),
                          "hp_min": min(hp for (_g, hp, _m) in hist) if hist else None,
                          "hp_final": hist[-1][1] if hist else None,
                          "hp_max": hist[0][2] if hist else None}

    notes = []
    if mirror:
        notes.append("Зеркальный матчап: боевой лог не различает двух одинаковых героев. "
                     "Касты посчитаны по кулдаунам снимков индивидуально; урон и души из лога — "
                     "на пару (души по игрокам — оценка по ластхитам, смерть делит пополам).")
    if not any(e.get("creeps") for e in iter_jsonl(os.path.join(decoded, "timeline.jsonl"))):
        notes.append("В этом разборе нет данных о волнах крипов (старый decode) — "
                     "передекодируйте демку актуальной версией.")

    return {
        "decoded": decoded, "lane": main_lane, "duration_s": round(duration, 1),
        "duration_clock": fmt_clock(duration),
        "players": [info[pids[0]], info[pids[1]]],
        "mirror_matchup": mirror,
        "score_kills": {str(pid): (series[pid][-1][7].get("kills") if series[pid] and series[pid][-1][7] else None)
                        for pid in pids},
        "minutes": minutes,
        "raze": raze, "raze_damage_pair": raze_dmg_by_tier,
        "hero_damage_pair_total": sum(dmg_by_inflictor.values()),
        "hero_damage_by_inflictor_top": dict(dmg_by_inflictor.most_common(10)),
        "hero_damage_by_attacker": ({internals[pids[0]]: dmg_by_attacker.get(internals[pids[0]], 0),
                                     internals[pids[1]]: dmg_by_attacker.get(internals[pids[1]], 0)}
                                    if not mirror else None),
        "deaths": {str(pid): deaths[pid] for pid in pids},
        "retreats": {str(pid): retreats[pid] for pid in pids},
        "towers_t1": towers_out,
        "notes": notes,
    }


def render_md(d):
    p1, p2 = d["players"]
    n1 = f"{p1['hero']} ({p1['name'] or 'игрок ' + str(p1['player_id'])}{', бот' if p1['is_bot'] else ''})"
    n2 = f"{p2['hero']} ({p2['name'] or 'игрок ' + str(p2['player_id'])}{', бот' if p2['is_bot'] else ''})"
    L = [f"# Дуэль 1v1: {n1} vs {n2}\n",
         f"Линия: {d['lane']}. Длительность разбора: {d['duration_clock']}. "
         f"Убийства: {d['score_kills'].get(str(p1['player_id']))} — "
         f"{d['score_kills'].get(str(p2['player_id']))}.\n"]
    for nt in d["notes"]:
        L.append(f"> {nt}\n")
    L.append("## По минутам\n")
    L.append(f"| Мин | CS {p1['hero']} | CS {p2['hero']} | Денаи | Нетворс {p1['hero']} | "
             f"Нетворс {p2['hero']} | Разн. NW | Разн. XP | Уровни | Души (оц.) | Волна крипов |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in d["minutes"]:
        a, b = r.get(f"p{p1['player_id']}", {}), r.get(f"p{p2['player_id']}", {})
        wv = r.get("wave") or {}
        wave_txt = (f"{wv.get('region', '')} (кр. {wv.get('creeps_radiant')} vs {wv.get('creeps_dire')})"
                    if wv else "—")
        souls = []
        for a2 in (a, b):
            v = a2.get("souls", a2.get("souls_est"))
            souls.append("—" if v is None else str(v))
        L.append(f"| {r['minute']} | {a.get('last_hits')} | {b.get('last_hits')} | "
                 f"{a.get('denies')}/{b.get('denies')} | {a.get('net_worth')} | {b.get('net_worth')} | "
                 f"{r.get('nw_diff', '—')} | {r.get('xp_diff', '—')} | "
                 f"{a.get('level')}/{b.get('level')} | {souls[0]}/{souls[1]} | {wave_txt} |")
    L.append("\n## Рейзы и урон\n")
    for p in (p1, p2):
        rz = d["raze"].get(str(p["player_id"]), {})
        L.append(f"- {p['hero']} (игрок {p['player_id']}): кастов рейзов {rz.get('casts')}, "
                 f"однозначно атрибутировано попаданий по герою {rz.get('hits')} "
                 f"(точность {rz.get('accuracy_attributed_pct')}%, неоднозначных кастов {rz.get('ambiguous')})")
    rd = d["raze_damage_pair"]
    L.append(f"- Урон рейзов по героям на пару: " +
             ", ".join(f"{t}: {v['damage']} ({v['hits_events']} попаданий)" for t, v in rd.items()))
    L.append(f"- Весь урон герой→герой за игру: {d['hero_damage_pair_total']}. Топ источников: " +
             ", ".join(f"{k.replace('npc_dota_hero_', '')} {v}" for k, v in
                       list(d["hero_damage_by_inflictor_top"].items())[:6]))
    if d["hero_damage_by_attacker"]:
        L.append(f"- По игрокам: " + ", ".join(f"{k.replace('npc_dota_hero_', '')}: {v}"
                                               for k, v in d["hero_damage_by_attacker"].items()))
    L.append("\n## Смерти\n")
    any_death = False
    for p in (p1, p2):
        for dt in d["deaths"].get(str(p["player_id"]), []):
            any_death = True
            L.append(f"- `{dt['clock']}` умер {p['hero']} (игрок {p['player_id']}), "
                     f"убийца: {dt.get('killer') or '—'}, чем: {dt.get('inflictor') or '—'}, "
                     f"респаун {dt.get('respawn_s')} c → `moment.py --time {dt['clock']} --player {p['player_id']}`")
    if not any_death:
        L.append("- Смертей героев в разборе нет.")
    L.append("\n## Отступления на низком HP (HP<35% и уход к своей базе)\n")
    any_r = False
    for p in (p1, p2):
        for r in d["retreats"].get(str(p["player_id"]), []):
            any_r = True
            L.append(f"- `{r['clock']}` {p['hero']} (игрок {p['player_id']}): просел до {r['min_hp_pct']}%, "
                     f"{'ушёл к фонтану' if r['to_fountain'] else 'отошёл назад'} "
                     f"(сблизился с базой на {r['dist_gain']} ед.)")
    if not any_r:
        L.append("- Явных отступлений не найдено.")
    L.append("\n## Башни T1 линии\n")
    for nm, t in d["towers_t1"].items():
        L.append(f"- {nm}: первый урон {t['first_damage_clock'] or '—'}, "
                 f"мин. HP {t['hp_min']}/{t['hp_max']}, финальный HP {t['hp_final']}")
    L.append("\nСтоп-кадры: `python moment.py <папка> --time MM:SS --player N`\n")
    return "\n".join(L) + "\n"


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    decoded = args[0]
    players_arg = None
    lane = "мид"
    if "--players" in args:
        players_arg = args[args.index("--players") + 1]
    if "--lane" in args:
        lane = args[args.index("--lane") + 1]
    d = compute_duel(decoded, players_arg, lane)
    with open(os.path.join(decoded, "duel.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    with open(os.path.join(decoded, "DUEL.md"), "w", encoding="utf-8") as f:
        f.write(render_md(d))
    print(f"Готово: DUEL.md и duel.json в {decoded} "
          f"(пара: игрок {d['players'][0]['player_id']} vs {d['players'][1]['player_id']}, "
          f"линия {d['lane']}, {d['duration_clock']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
