#!/usr/bin/env python3
"""
moment.py — «стоп-кадр» демки Dota 2: что происходит в конкретный момент.

Показывает по каждому герою (или одному выбранному):
  • где стоит — мировые координаты + регион карты («мид, сторона Radiant»);
  • куда направлен — угол поворота сущности (yaw из демки) + стороны света,
    и совпадает ли взгляд с направлением бега;
  • что делает прямо сейчас — кастует/каналит способность (фаза каста),
    только что использовал способность/предмет, атаковал или получал урон
    (по боевому логу вокруг момента), последний приказ игрока перед моментом
    (движение/атака/каст/покупка — то, что «кликал человек»), бежит или стоит;
  • скорость и направление движения (по снимкам до/после момента);
  • кто рядом: союзники и враги в радиусе зрения, их HP;
  • HP/мана/уровень/предметы/кулдауны/KDA в этот момент.

И рисует кадр миникарты `moment_*.png`, где у героев стрелки:
белая — куда смотрит, жёлтая — куда бежит (у выбранного героя — крупнее).

Использование:
  python moment.py match_decoded/ --time 05:30 --player 0
  python moment.py match_decoded/ --time 12:00 --hero Razor --window 10
  python moment.py match.dem --time 25:00 --hero nevermore   # сначала сам разберёт демку
  python moment.py match_decoded/ --time end                 # финальный момент

Результат печатается в консоль и сохраняется рядом: moment_<время>[_<герой>].md/.json/.png
"""
import argparse
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decode import fmt_clock, region_name, write_png, draw_text  # noqa: E402

COMPASS = ["восток", "северо-восток", "север", "северо-запад",
           "запад", "юго-запад", "юг", "юго-восток"]

ORDER_TYPES = {
    0: "нет приказа", 1: "движение в точку", 2: "движение к цели",
    3: "атака-движение (attack-move)", 4: "атака цели", 5: "каст в точку",
    6: "каст по цели", 7: "каст по дереву", 8: "каст без цели (ноутаргет)",
    9: "вкл/выкл способности", 10: "удерживать позицию", 11: "изучить способность",
    12: "выбросить предмет", 13: "передать предмет", 14: "поднять предмет",
    15: "поднять руну", 16: "покупка предмета", 17: "продать предмет",
    18: "разобрать предмет", 19: "переложить предмет", 20: "автокаст вкл/выкл",
    21: "стоп", 22: "насмешка (taunt)", 23: "байбек", 24: "глиф",
    25: "достать предмет из тайника", 26: "каст руны", 27: "пинг способности",
    28: "движение в направлении", 29: "патруль", 30: "векторный каст",
    31: "радар/скан", 33: "продолжить", 39: "относительное движение",
}


def parse_time(s, entries):
    if s is None or str(s).lower() in ("end", "final", "конец"):
        return entries[-1]["game_time_s"]
    s = str(s).strip()
    if ":" in s:
        sign = -1 if s.startswith("-") else 1
        parts = s.lstrip("-").split(":")
        val = int(parts[0]) * 60 + float(parts[1])
        return sign * val
    return float(s)


def compass_of(angle_deg):
    idx = int(((angle_deg % 360) + 22.5) // 45) % 8
    return COMPASS[idx]


def angle_of(dx, dy):
    return math.degrees(math.atan2(dy, dx)) % 360


def hero_key(h):
    pid = h.get("player_id")
    return pid if pid is not None and pid >= 0 else f"ent{h.get('entity_index')}"


def find_hero(entry, pred):
    for h in entry.get("heroes", []):
        if pred(h):
            return h
    return None


def describe_order(order, hero, snap):
    """Человеческое описание последнего приказа игрока."""
    if not order:
        return None
    otype = order.get("order_type")
    text = ORDER_TYPES.get(otype, f"приказ типа {otype}")
    parts = [text]
    pos = order.get("position")
    if pos:
        x = pos.get("x")
        y = pos.get("y")
        if x is not None and y is not None:
            parts.append(f"координаты ({x:.0f}, {y:.0f}) — {region_name(x, y)}")
            if hero and hero.get("x") is not None:
                dist = math.hypot(x - hero["x"], y - hero["y"])
                parts.append(f"({dist:.0f} ед. от героя)")
    if order.get("target_index"):
        parts.append(f"цель: сущность #{order['target_index']}")
    if order.get("ability_id") and order["ability_id"] > 0:
        resolved = None
        if hero:
            for a in hero.get("abilities", []) + hero.get("items", []):
                if a.get("entity_index") == order["ability_id"] or a.get("slot") == order["ability_id"]:
                    resolved = a.get("ability") or a.get("item")
                    break
        parts.append(f"способность/предмет: {resolved}" if resolved
                     else f"способность/предмет id={order['ability_id']}")
    if order.get("queue"):
        parts.append("(в очередь)")
    return "; ".join(parts)


def render_moment(path, snap, focus_key=None, movements=None, size=720):
    """Кадр миникарты со стрелками: белая — взгляд, жёлтая — бег."""
    w = h = size
    px = bytearray(bytes((16, 42, 26)) * (w * h))
    WORLD = 8400

    def to_px(x, y):
        x = max(-WORLD, min(WORLD, x))
        y = max(-WORLD, min(WORLD, y))
        return int((x + WORLD) / (2 * WORLD) * (w - 1)), int((WORLD - y) / (2 * WORLD) * (h - 1))

    def set_px(x, y, color):
        if 0 <= x < w and 0 <= y < h:
            i = (y * w + x) * 3
            px[i:i + 3] = bytes(color)

    row_cache = {}

    def fill_rect(x, y, rw, rh, color):
        x1, x2 = max(0, x), min(w, x + rw)
        if x2 <= x1:
            return
        key = (x2 - x1, color)
        row = row_cache.get(key)
        if row is None:
            row = bytes(color) * (x2 - x1)
            row_cache[key] = row
        for yy in range(max(0, y), min(h, y + rh)):
            i = (yy * w + x1) * 3
            px[i:i + len(row)] = row

    def line(x1, y1, x2, y2, color, thick=3):
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        for i in range(steps + 1):
            x = int(x1 + (x2 - x1) * i / steps)
            y = int(y1 + (y2 - y1) * i / steps)
            fill_rect(x - thick // 2, y - thick // 2, thick, thick, color)

    pa, pb = to_px(-8200, 8200), to_px(8200, -8200)
    line(*pa, *pb, (38, 84, 124), 14)
    for coords in [((-7200, -7200), (7200, 7200)),
                   ((-7200, -7200), (-7200, 7200), (7200, 7200)),
                   ((-7200, -7200), (7200, -7200), (7200, 7200))]:
        for a, b in zip(coords, coords[1:]):
            line(*to_px(*a), *to_px(*b), (92, 92, 78), 5)
    for (bx, by), c in [((-7200, -7200), (46, 140, 60)), ((7200, 7200), (170, 52, 44))]:
        cx, cy = to_px(bx, by)
        fill_rect(cx - 12, cy - 12, 24, 24, c)

    for n in snap.get("npcs", []):
        if n.get("x") is None or (n.get("hp") or 0) <= 0:
            continue
        cx, cy = to_px(n["x"], n["y"])
        c = (46, 190, 80) if n.get("team_num") == 2 else (230, 70, 55) if n.get("team_num") == 3 else (150, 150, 150)
        s = 7 if "Tower" in (n.get("class") or "") else 6
        fill_rect(cx - s // 2, cy - s // 2, s, s, c)

    def arrow(cx, cy, angle_deg, length, color, thick=3):
        # угол в координатах мира (0=восток, 90=север) -> экран (y вниз)
        rad = math.radians(angle_deg)
        ex = int(cx + math.cos(rad) * length)
        ey = int(cy - math.sin(rad) * length)
        line(cx, cy, ex, ey, color, thick)
        for sgn in (150, -150):
            brad = rad + math.radians(sgn)
            bx = int(ex + math.cos(brad) * length * 0.35)
            by = int(ey - math.sin(brad) * length * 0.35)
            line(ex, ey, bx, by, color, thick)

    for hero in snap.get("heroes", []):
        if hero.get("x") is None:
            continue
        cx, cy = to_px(hero["x"], hero["y"])
        focus = hero_key(hero) == focus_key
        dead = not hero.get("alive", True)
        c = (90, 90, 90) if dead else (52, 235, 105) if hero.get("team_num") == 2 else (255, 74, 60)
        r = 15 if focus else 10
        if focus:  # белое кольцо вокруг выбранного
            for yy in range(cy - r - 5, cy + r + 6):
                for xx in range(cx - r - 5, cx + r + 6):
                    d = (xx - cx) ** 2 + (yy - cy) ** 2
                    if (r + 3) ** 2 <= d <= (r + 5) ** 2:
                        set_px(xx, yy, (255, 255, 255))
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                    set_px(xx, yy, c)
        if hero.get("facing_yaw_deg") is not None:
            arrow(cx, cy, hero["facing_yaw_deg"], 34 if focus else 22, (255, 255, 255), 3 if focus else 2)
        mv = (movements or {}).get(hero_key(hero))
        if mv and mv.get("move_angle_deg") is not None and mv.get("speed", 0) > 40:
            arrow(cx, cy, mv["move_angle_deg"], 30 if focus else 20, (255, 210, 60), 3 if focus else 2)
        hp_frac = hero.get("hp_pct") or 0
        fill_rect(cx - r, cy - r - 6, int(2 * r * hp_frac / 100), 3, (80, 255, 120))
        pid = hero.get("player_id")
        if pid is not None and pid >= 0:
            draw_text(px, w, h, cx - 4, cy - 5, str(pid), (10, 10, 10), 2)
    write_png(path, w, h, px)


def main():
    ap = argparse.ArgumentParser(description="Стоп-кадр демки Dota 2 в заданный момент")
    ap.add_argument("target", help="папка <матч>_decoded ИЛИ файл .dem")
    ap.add_argument("--time", default=None, help="игровое время mm:ss (0:00 = горн), секунды или 'end'")
    ap.add_argument("--hero", default=None, help="имя героя/игрока или его часть (nevermore, Razor, ник)")
    ap.add_argument("--player", type=int, default=None, help="player_id героя (0..9)")
    ap.add_argument("--window", type=float, default=8.0, help="окно контекста в секундах (по умолч. 8)")
    ap.add_argument("--out", default=None, help="куда сохранить .md/.json/.png (по умолч. в папку разбора)")
    args = ap.parse_args()

    decoded = args.target
    if os.path.isfile(decoded):  # дали саму демку — разберём, если ещё нет разбора
        name = os.path.basename(decoded)
        for suf in (".dem.bz2", ".dem.gz", ".dem"):
            if name.lower().endswith(suf):
                name = name[:-len(suf)]
                break
        candidate = f"{name}_decoded"
        if not os.path.isdir(candidate):
            here = os.path.dirname(os.path.abspath(__file__))
            print("Разбираю демку (python decode.py)…")
            subprocess.run([sys.executable, os.path.join(here, "decode.py"), decoded], check=True)
        decoded = candidate
    timeline_path = os.path.join(decoded, "timeline.jsonl")
    if not os.path.isfile(timeline_path):
        print(f"Не нашёл {timeline_path} — сначала разберите демку decode.py", file=sys.stderr)
        return 1

    # --- читаем таймлайн потоково, держим только окно вокруг момента + соседей для скорости
    all_entries = []
    with open(timeline_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                all_entries.append(json.loads(line))
    if not all_entries:
        print("Таймлайн пуст", file=sys.stderr)
        return 1

    target_time = parse_time(args.time, all_entries)
    snap = min(all_entries, key=lambda e: abs(e["game_time_s"] - target_time))
    t_idx = all_entries.index(snap)
    target_tick = snap["tick"]
    win_ticks = int(args.window * 30)

    # скорость/направление: соседи в пределах ±5 секунд
    movements = {}
    for dt_idx in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5):
        pass  # (окно берём прямым поиском ниже)
    window_entries = [e for e in all_entries if abs(e["game_time_s"] - snap["game_time_s"]) <= 2.5]
    if len(window_entries) < 2:
        window_entries = [e for e in all_entries if abs(e["game_time_s"] - snap["game_time_s"]) <= 6.0]
    for hero in snap.get("heroes", []):
        key = hero_key(hero)
        pts = []
        for e in window_entries:
            hh = find_hero(e, lambda x: hero_key(x) == key)
            if hh and hh.get("x") is not None:
                pts.append((e["game_time_s"], hh["x"], hh["y"]))
        if len(pts) >= 2:
            (t0, x0, y0), (t1, x1, y1) = pts[0], pts[-1]
            dt = t1 - t0
            if dt > 0:
                speed = math.hypot(x1 - x0, y1 - y0) / dt
                movements[key] = {
                    "speed": round(speed), "move_angle_deg": round(angle_of(x1 - x0, y1 - y0), 1),
                    "move_compass": compass_of(angle_of(x1 - x0, y1 - y0)),
                }

    # --- боевой лог и приказы вокруг момента (по тикам)
    combat_near = []
    cl_path = os.path.join(decoded, "combatlog.jsonl")
    if os.path.isfile(cl_path):
        with open(cl_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                if abs(e.get("_tick", 0) - target_tick) <= win_ticks:
                    combat_near.append(e)
    last_orders = {}
    ord_path = os.path.join(decoded, "unit_orders.jsonl")
    if os.path.isfile(ord_path):
        with open(ord_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                o = json.loads(line)
                if o.get("_tick", 0) > target_tick:
                    continue
                for uid in (o.get("units") or [o.get("entindex")]):
                    last_orders[uid] = o

    events_near = []
    ev_path = os.path.join(decoded, "events.jsonl")
    if os.path.isfile(ev_path):
        with open(ev_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                if abs(e.get("game_time_s", 0) - snap["game_time_s"]) <= args.window:
                    events_near.append(e)

    # Активные баффы/дебаффы: проигрываем боевой лог (MODIFIER_ADD/REMOVE) до
    # целевого тика. Это точный поток наложений/снятий видимых модификаторов;
    # сырая таблица modifiers.jsonl для этого не годится (там нет надёжных снятий).
    MOD_NOISE = {"modifier_path_movespeed"}
    mod_state = {}  # internal -> {inflictor: {"count","stack","left_s"}}
    cl_path = os.path.join(decoded, "combatlog.jsonl")
    hero_internals = {h.get("hero_internal") for h in snap.get("heroes", [])}
    if os.path.isfile(cl_path):
        with open(cl_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("_tick", 0) > target_tick:
                    break
                tn = e.get("_type_name")
                if tn not in ("DOTA_COMBATLOG_MODIFIER_ADD", "DOTA_COMBATLOG_MODIFIER_REMOVE",
                              "DOTA_COMBATLOG_MODIFIER_STACK_EVENT"):
                    continue
                tgt, infl = e.get("_target"), e.get("_inflictor")
                if tgt not in hero_internals or not infl or infl in MOD_NOISE:
                    continue
                st = mod_state.setdefault(tgt, {})
                if tn == "DOTA_COMBATLOG_MODIFIER_STACK_EVENT":
                    cur = st.setdefault(infl, {"count": 1, "stack": 0, "left_s": None})
                    if e.get("stack_count") is not None:
                        cur["stack"] = e["stack_count"]
                elif tn == "DOTA_COMBATLOG_MODIFIER_ADD":
                    cur = st.setdefault(infl, {"count": 0, "stack": 0, "left_s": None})
                    cur["count"] += 1
                    cur["stack"] = e.get("stack_count") or cur["stack"]
                    dur = e.get("modifier_duration")
                    if dur:
                        cur["left_s"] = round((e["_tick"] + dur * 30 - target_tick) / 30.0, 1)
                else:
                    cur = st.get(infl)
                    if cur:
                        cur["count"] -= 1
                        if cur["count"] <= 0:
                            del st[infl]
    mods_by_hero = {}
    for h in snap.get("heroes", []):
        st = mod_state.get(h.get("hero_internal"), {})
        mods_by_hero[h.get("entity_index")] = [
            {"name": k.replace("modifier_", "", 1) if k.startswith("modifier_") else k,
             "count": v["count"], "stack": v["stack"], "left_s": v["left_s"]}
            for k, v in sorted(st.items())]

    players = {}
    pj = os.path.join(decoded, "players.json")
    if os.path.isfile(pj):
        for p in json.load(open(pj, encoding="utf-8")):
            players[p["player_id"]] = p

    # --- кого показываем подробно
    def matches(h):
        if args.player is not None:
            return h.get("player_id") == args.player
        if args.hero:
            q = args.hero.lower()
            name = (players.get(h.get("player_id"), {}) or {}).get("name") or ""
            return (q in (h.get("hero") or "").lower() or q in (h.get("hero_internal") or "").lower()
                    or q in name.lower())
        return True

    chosen = [h for h in snap.get("heroes", []) if matches(h)]
    if not chosen:
        print("Герой не найден. Герои в этот момент:",
              ", ".join(f"{h.get('hero')}(игрок {h.get('player_id')})" for h in snap.get("heroes", [])))
        return 1
    focus_key = hero_key(chosen[0]) if (args.hero or args.player is not None) else None

    # --- собираем описание по каждому выбранному герою
    L = [f"# Стоп-кадр {snap['clock']} (тик {target_tick}, игра {snap['game_time_s']} c)\n"]
    result = {"clock": snap["clock"], "tick": target_tick, "game_time_s": snap["game_time_s"],
              "scores": snap.get("scores"), "heroes": []}

    for h in chosen:
        key = hero_key(h)
        pid = h.get("player_id")
        pname = (players.get(pid, {}) or {}).get("name")
        mv = movements.get(key, {})
        yaw = h.get("facing_yaw_deg")
        block = []
        title = f"## {h['hero']}" + (f" — {pname} " if pname else " ") + f"(игрок {pid}, {h['team']})"
        block.append(title)
        if not h["alive"]:
            block.append(f"💀 **Мёртв.** До возрождения ~{h.get('respawn_seconds')} c.")
        block.append(f"- Позиция: ({h['x']}, {h['y']}) — **{h['region']}**")
        if yaw is not None:
            facing_txt = f"- Направление взгляда: **{compass_of(yaw)}** (yaw {yaw}°)"
            if mv.get("speed", 0) > 40 and mv.get("move_angle_deg") is not None:
                diff = abs((yaw - mv["move_angle_deg"] + 180) % 360 - 180)
                facing_txt += (f"; бежит примерно туда же, куда смотрит (расхождение {diff:.0f}°)"
                               if diff < 35 else
                               f"; НО смотрит в другую сторону от бега (расхождение {diff:.0f}°) — "
                               f"например, кастует/атакует назад или разворачивается")
            block.append(facing_txt)
        else:
            block.append("- Направление взгляда: нет данных (старый разбор — передекодируйте демку)")
        if mv:
            if mv.get("speed", 0) > 40:
                block.append(f"- Движение: **бежит на {mv['move_compass']}** со скоростью ~{mv['speed']} ед/с")
            else:
                block.append("- Движение: **стоит на месте** (скорость < 40 ед/с)")
        block.append(f"- Состояние: HP {h['hp']}/{h['hp_max']} ({h['hp_pct']}%), "
                     f"мана {h['mana']}/{h['mana_max']}, уровень {h['level']}"
                     + (f", KDA {h['stats']['kills']}/{h['stats']['deaths']}/{h['stats']['assists']}, "
                        f"нетворс {h['stats'].get('net_worth')}" if h.get("stats") else ""))
        block.append("- Предметы: " + (", ".join(i["item"] for i in h["items"] if i.get("item")) or "—"))

        # ЧТО ДЕЛАЕТ
        actions = []
        casting = [a for a in h.get("abilities", []) if a.get("in_ability_phase")]
        if casting:
            actions.append(f"⏳ **прямо сейчас кастует/каналит: {casting[0]['ability']}**")
        internal = h.get("hero_internal") or ""
        mine = [e for e in combat_near
                if (e.get("_attacker") or "") == internal or (e.get("_target") or "") == internal]
        casts = [e for e in mine if e.get("_type_name", "").endswith(("ABILITY", "ITEM"))
                 and e.get("_attacker") == internal and e.get("_tick", 0) <= target_tick]
        if casts:
            c = casts[-1]
            dt = (target_tick - c["_tick"]) / 30.0
            actions.append(f"только что применил **{c.get('_inflictor') or 'способность'}** "
                           f"({dt:.1f} c назад)")
        dealt = [e for e in mine if e.get("_type_name", "").endswith("DAMAGE")
                 and e.get("_attacker") == internal]
        taken = [e for e in mine if e.get("_type_name", "").endswith("DAMAGE")
                 and e.get("_target") == internal]
        if dealt:
            tgt = dealt[-1].get("_target")
            actions.append(f"атаковал/бил по **{tgt}** (урона за окно: "
                           f"{sum(e.get('value') or 0 for e in dealt)}, последний удар "
                           f"{(target_tick - dealt[-1]['_tick']) / 30.0:.1f} c назад)")
        if taken:
            src = taken[-1].get("_attacker")
            actions.append(f"получал урон от **{src}** (всего за окно: "
                           f"{sum(e.get('value') or 0 for e in taken)})")
        order = last_orders.get(h.get("entity_index"))
        order_txt = describe_order(order, h, snap) if order else None
        if order_txt:
            ago = (target_tick - order["_tick"]) / 30.0
            actions.append(f"последний приказ игрока ({ago:.1f} c назад): **{order_txt}**")
        ready = [f"{a['ability']}[{a['level']}]" for a in h.get("abilities", [])
                 if a.get("level") and (a.get("cooldown_left") or 0) == 0
                 and not a.get("in_ability_phase")][:6]
        on_cd = [f"{a['ability']} (ещё {a['cooldown_left']:.0f} c)" for a in h.get("abilities", [])
                 if a.get("level") and (a.get("cooldown_left") or 0) > 0][:6]
        if not actions and h["alive"]:
            actions.append("явных боевых действий в окне нет — " +
                           ("перемещается" if mv.get("speed", 0) > 40 else "ждёт/фармит на месте"))

        block.append("- **Что делает:**")
        for a in actions:
            block.append(f"  - {a}")
        if on_cd:
            block.append(f"  - на кулдауне: {', '.join(on_cd)}")
        if ready:
            block.append(f"  - готово к применению: {', '.join(ready)}")

        # баффы/дебаффы прямо сейчас
        my_mods = mods_by_hero.get(h.get("entity_index"), [])
        if my_mods:
            def mod_label(m):
                extra = []
                if m.get("count", 1) > 1:
                    extra.append(f"×{m['count']}")
                if m.get("stack"):
                    extra.append(f"стаков {m['stack']}")
                if m.get("left_s") is not None and m["left_s"] >= 1:
                    extra.append(f"ещё ~{m['left_s']:.0f} c")
                return m["name"] + (f" ({', '.join(extra)})" if extra else "")
            names = sorted(mod_label(m) for m in my_mods)
            shown = ", ".join(names[:16]) + (f" … и ещё {len(names) - 16}" if len(names) > 16 else "")
            block.append(f"- Баффы/дебаффы на герое ({len(names)}): {shown}")
        if h.get("stats", {}).get("buyback_cost"):
            block.append(f"- Цена байбека сейчас: ~{h['stats']['buyback_cost']} золота")

        # кто рядом
        near_en, near_al = [], []
        for o in snap.get("heroes", []):
            if o is h or o.get("x") is None or h.get("x") is None:
                continue
            d = math.hypot(o["x"] - h["x"], o["y"] - h["y"])
            if d <= 1800:
                row = f"{o['hero']} ({d:.0f} ед., HP {o['hp_pct']}%" + (", мёртв" if not o["alive"] else "") + ")"
                (near_en if o.get("team_num") != h.get("team_num") else near_al).append(row)
        if near_en:
            block.append("- Враги рядом (≤1800): " + "; ".join(near_en))
        if near_al:
            block.append("- Союзники рядом (≤1800): " + "; ".join(near_al))
        named_near = []
        for n in snap.get("npcs", []):
            if n.get("name") and n.get("x") is not None and h.get("x") is not None:
                d = math.hypot(n["x"] - h["x"], n["y"] - h["y"])
                if d <= 2200:
                    named_near.append((d, f"{n['name']} ({d:.0f} ед., HP {n['hp']})"))
        if named_near:
            named_near.sort()
            block.append("- Объекты рядом: " + "; ".join(x[1] for x in named_near[:5]))

        L.append("\n".join(block) + "\n")
        result["heroes"].append({
            "player_id": pid, "name": pname, "hero": h["hero"], "team": h["team"],
            "x": h["x"], "y": h["y"], "region": h["region"],
            "facing_yaw_deg": yaw,
            "facing_compass": compass_of(yaw) if yaw is not None else None,
            "movement": mv, "alive": h["alive"], "hp_pct": h["hp_pct"],
            "active_modifiers": mods_by_hero.get(h.get("entity_index"), []),
            "level": h["level"], "actions": actions, "last_order": order_txt,
            "stats": h.get("stats"),
        })

    roshan = next((n for n in snap.get("npcs", []) if n.get("class") == "CDOTA_Unit_Roshan"), None)
    if roshan is not None:
        result["roshan"] = {"hp": roshan.get("hp"), "hp_max": roshan.get("hp_max"),
                            "alive": roshan.get("alive")}
        L.append(f"Рошан в этот момент: " +
                 (f"жив, HP {roshan.get('hp')}/{roshan.get('hp_max')}" if roshan.get("alive")
                  else "мёртв/отсутствует") + "\n")

    if events_near:
        L.append("## События вокруг момента\n")
        for e in events_near[:30]:
            L.append(f"- `{e['clock']}` {e['text']}")
        L.append("")

    out_base = args.out or decoded
    os.makedirs(out_base, exist_ok=True)
    tag = f"{snap['game_time_s']:07.1f}".replace(".", "_")
    suffix = f"_{chosen[0]['hero']}" if (args.hero or args.player is not None) and len(chosen) == 1 else ""
    md_path = os.path.join(out_base, f"moment_{tag}{suffix}.md")
    json_path = os.path.join(out_base, f"moment_{tag}{suffix}.json")
    png_path = os.path.join(out_base, f"moment_{tag}{suffix}.png")
    render_moment(png_path, snap, focus_key=focus_key, movements=movements)
    L.append(f"Кадр миникарты: `{os.path.basename(png_path)}` "
             f"(белая стрелка — куда смотрит герой, жёлтая — куда бежит; "
             f"выбранный герой обведён белым кольцом)")
    text = "\n".join(L)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(text)
    print(f"\n[сохранено: {md_path}, {json_path}, {png_path}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
