#!/usr/bin/env python3
"""
find.py — поиск моментов в разобранной демке Dota 2.

Каждая найденная строка содержит время в формате MM:SS — момент открывается через
moment.py:   python moment.py <папка> --time MM:SS --player N

Использование:
  python find.py <папка>_decoded deaths [--hero ИМЯ] [--player N]
  python find.py <папка>_decoded item <подстрока> [--player N] [--hero ИМЯ]
  python find.py <папка>_decoded ability <подстрока> [--player N] [--hero ИМЯ]
  python find.py <папка>_decoded towers
  python find.py <папка>_decoded Roshan
  python find.py <папка>_decoded buybacks
  python find.py <папка>_decoded wards
  python find.py <папка>_decoded chat [подстрока]
  python find.py <папка>_decoded lowhp [--hp N] [--player N]
  python find.py <папка>_decoded fights          (нужен analyze.py)
  python find.py <папка>_decoded ping            (пинги/линии по карте)

Подстроки ищутся без учёта регистра; hero определяет игрока по имени героя/игрока.
"""
import bisect
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from decode import region_name
except Exception:  # noqa
    def region_name(x, y):
        return ""


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


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


class Ctx:
    def __init__(self, decoded):
        self.decoded = decoded
        self.players = load_json(os.path.join(decoded, "players.json"), [])
        self.by_pid = {p["player_id"]: p for p in self.players}
        self.internal_to_pid = {}
        # Соответствие тик -> игровое время по снимкам таймлайна (для боевого лога)
        self.series = []  # (tick, game_time_s)
        for e in iter_jsonl(os.path.join(decoded, "timeline.jsonl")):
            self.series.append((e["tick"], e["game_time_s"]))
            for h in e.get("heroes", []):
                if h.get("player_id") is not None and h["player_id"] >= 0:
                    self.internal_to_pid[h["hero_internal"]] = h["player_id"]
        self.series_t = [s[0] for s in self.series]

    def clock(self, tick):
        if not self.series:
            return "?", 0.0
        i = bisect.bisect_left(self.series_t, tick)
        cands = []
        if i < len(self.series):
            cands.append(self.series[i])
        if i > 0:
            cands.append(self.series[i - 1])
        bt, bgt = min(cands, key=lambda s: abs(s[0] - tick))
        gt = bgt + (tick - bt) / 30.0
        sign = "-" if gt < 0 else ""
        gt = abs(gt)
        return f"{sign}{int(gt // 60):02d}:{int(gt % 60):02d}", bgt + (tick - bt) / 30.0

    def pid_of(self, internal):
        if internal in self.internal_to_pid:
            return self.internal_to_pid[internal]
        if internal:  # по имени героя
            short = internal.replace("npc_dota_hero_", "")
            for pid, p in self.by_pid.items():
                if (p.get("hero") or "").lower() == short.lower():
                    return pid
        return None

    def resolve_player(self, hero_q=None, player_q=None):
        """Возвращает множество player_id по фильтрам (или None = все)."""
        want = None
        if player_q is not None:
            want = {int(player_q)}
        if hero_q:
            q = hero_q.lower()
            got = {p["player_id"] for p in self.players
                   if q in (p.get("hero") or "").lower() or q in (p.get("name") or "").lower()
                   or q in (p.get("hero_internal") or "").lower()}
            want = got if want is None else (want & got)
        return want


def hint(pid):
    return f"  [moment.py --time {{t}} --player {pid}]" if pid is not None else ""


def out(clock, pid, msg):
    print(f"{clock}  {msg}  -> moment: --time {clock}" + (f" --player {pid}" if pid is not None else ""))


def main():
    try:
        from signal import signal, SIGPIPE, SIG_DFL
        signal(SIGPIPE, SIG_DFL)
    except Exception:
        pass
    args = sys.argv[1:]
    if len(args) < 2 or args[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    decoded, cmd = args[0], args[1]
    rest = args[2:]

    def opt(name, default=None):
        if name in rest:
            i = rest.index(name)
            return rest[i + 1] if i + 1 < len(rest) else default
        return default

    positional = [a for i, a in enumerate(rest)
                  if not a.startswith("--") and (i == 0 or rest[i - 1] not in ("--hero", "--player", "--hp"))]
    query = positional[0] if positional else ""

    ctx = Ctx(decoded)
    want = ctx.resolve_player(opt("--hero"), opt("--player"))

    def player_ok(pid):
        return want is None or (pid is not None and pid in want)

    if cmd == "deaths":
        deaths = list(iter_jsonl(os.path.join(decoded, "deaths.jsonl")))
        if not deaths:  # fallback: events.jsonl
            n = 0
            for e in iter_jsonl(os.path.join(decoded, "events.jsonl")):
                if e.get("type") == "hero_death":
                    pid_v = ctx.by_pid and [p for p in ctx.players if p["hero"] == e.get("hero")]
                    pid_v = pid_v[0]["player_id"] if pid_v else None
                    if not player_ok(pid_v):
                        continue
                    n += 1
                    out(e["clock"], pid_v, f"Смерть {e.get('hero')} (убийца: {e.get('killer')}) "
                                           f"в «{e.get('region')}»")
            print(f"найдено: {n} (deaths.jsonl нет — запустите analyze.py для развёрнутого разбора)")
            return 0
        n = 0
        for d in deaths:
            pid_k = ctx.pid_of_name(d.get("killer"))
            pid_v = None
            for pid, internal in [(p, p) for p in ctx.internal_to_pid]:
                pass
            # жертва -> player по hero-имени
            for p in ctx.players:
                if p.get("hero") == d.get("victim_hero"):
                    pid_v = p["player_id"]
            if not player_ok(pid_v) and not player_ok(ctx.pid_of_name(d.get("killer"))):
                continue
            n += 1
            dmg = ", ".join(f"{x.get('attacker_hero') or x.get('attacker')} {x['damage']}" for x in d.get("damage_taken_last_10s", [])[:3])
            out(d["clock"], pid_v, f"Смерть {d.get('victim_hero')} ← {d.get('killer_hero') or '—'} "
                                   f"в «{d.get('region')}»; урон 10 c: {dmg}")
        print(f"найдено: {n}")

    elif cmd == "item":
        if not query:
            print("укажите подстроку предмета: find.py <папка> item black_king_bar")
            return 2
        q = query.lower()
        n = 0
        for e in iter_jsonl(os.path.join(decoded, "events.jsonl")):
            if e.get("kind") == "item_timing" and q in (e.get("item") or "").lower():
                pid = e.get("player_id")
                if not player_ok(pid):
                    continue
                n += 1
                out(e["clock"], pid, f"{e.get('hero')} получил {e.get('item')}")
        print(f"найдено: {n}")

    elif cmd == "ability":
        if not query:
            print("укажите подстроку способности: find.py <папка> ability black_hole")
            return 2
        q = query.lower()
        n = 0
        last_print = {}
        for e in iter_jsonl(os.path.join(decoded, "combatlog.jsonl")):
            if e.get("_type_name") in ("DOTA_COMBATLOG_ABILITY", "DOTA_COMBATLOG_ITEM") \
                    and e.get("_inflictor") and q in (e.get("_inflictor") or "").lower():
                pid = ctx.pid_of_name(e.get("_attacker"))
                if not player_ok(pid):
                    continue
                # не спамим: не чаще раза в 3 секунды по тому же источнику
                keyp = (pid, e.get("_inflictor"))
                if e["_tick"] - last_print.get(keyp, -10 ** 9) < 90:
                    continue
                last_print[keyp] = e["_tick"]
                n += 1
                clock, _gt = ctx.clock(e["_tick"])
                hero = ctx.by_pid.get(pid, {}).get("hero", e.get("_attacker"))
                tgt = e.get("_target")
                out(clock, pid, f"{hero} применил {e.get('_inflictor')}"
                                + (f" по {tgt}" if tgt and tgt != e.get("_attacker") else ""))
        print(f"найдено: {n} (с дедупликацией не чаще 1 раза в 3 c)")

    elif cmd == "towers":
        n = 0
        for e in iter_jsonl(os.path.join(decoded, "events.jsonl")):
            if e.get("kind") == "building_death" and e.get("source") == "snapshot" \
                    and any(t in (e.get("npc_class") or "") for t in ("Tower", "Barracks", "Fort")):
                n += 1
                out(e["clock"], None, e.get("text") or "")
        print(f"найдено: {n}")

    elif cmd == "Roshan":
        rosh = []
        for e in iter_jsonl(os.path.join(decoded, "combatlog.jsonl")):
            if e.get("_type_name") == "DOTA_COMBATLOG_DEATH" and e.get("_target") == "npc_dota_roshan":
                pid = ctx.pid_of_name(e.get("_attacker"))
                hero = ctx.by_pid.get(pid, {}).get("hero") or (e.get("_attacker") or "").replace("npc_dota_hero_", "")
                team = {2: "Radiant", 3: "Dire"}.get(e.get("attacker_team"), "?")
                rosh.append((e["_tick"], pid, hero, team))
        n = 0
        for e in iter_jsonl(os.path.join(decoded, "events.jsonl")):
            if e.get("kind") == "roshan_death":
                near = min(rosh, key=lambda r: abs(r[0] - e["tick"])) if rosh else None
                extra = ""
                pid = None
                if near and abs(near[0] - e["tick"]) < 300:
                    pid, extra = near[1], f" — убил {near[2]} (команда {near[3]})"
                n += 1
                out(e["clock"], pid, "Рошан убит" + extra)
        print(f"найдено: {n}")

    elif cmd == "buybacks":
        n = 0
        for e in iter_jsonl(os.path.join(decoded, "events.jsonl")):
            if e.get("kind") == "buyback":
                pid = e.get("value")
                pid = int(pid) if isinstance(pid, (int, float)) else None
                if not player_ok(pid):
                    continue
                n += 1
                hero = ctx.by_pid.get(pid, {}).get("hero", f"игрок {pid}")
                out(e["clock"], pid, f"Байбек: {hero}")
        print(f"найдено: {n}")

    elif cmd == "wards":
        wards = load_json(os.path.join(decoded, "wards.json"), None)
        if wards is None:
            n = 0
            for e in iter_jsonl(os.path.join(decoded, "events.jsonl")):
                if e.get("kind") == "ward_down":
                    n += 1
                    out(e["clock"], None, e.get("text") or "вард исчез")
            print(f"найдено исчезновений вардов: {n} (для карты поставленных вардов запустите analyze.py)")
            return 0
        n = 0
        for w in wards:
            n += 1
            kind = "обсервер" if "Observer" in w["class"] else "сентри"
            out(w["clock"], None, f"{kind} ({w['team']}) в «{w['region']}»")
        print(f"найдено: {n}; карта: wards_map.png")

    elif cmd == "chat":
        q = query.lower()
        n = 0
        for c in iter_jsonl(os.path.join(decoded, "chat.jsonl")):
            if c.get("_kind") != "dota_chat_message":
                continue
            text = c.get("message_text") or ""
            if q and q not in text.lower():
                continue
            pid = c.get("source_player_id")
            if not player_ok(pid):
                continue
            n += 1
            clock, _gt = ctx.clock(c.get("_tick", 0))
            hero = ctx.by_pid.get(pid, {}).get("hero", f"игрок {pid}")
            out(clock, pid, f"[{hero}] {text}")
        print(f"найдено: {n}")

    elif cmd == "lowhp":
        thr = float(opt("--hp", "10"))
        n = 0
        cur_low = set()
        for e in iter_jsonl(os.path.join(decoded, "timeline.jsonl")):
            for h in e.get("heroes", []):
                pid = h.get("player_id")
                if not player_ok(pid) or not h.get("alive"):
                    continue
                hm = h.get("hp_max") or 0
                pct = (h.get("hp") or 0) / hm * 100 if hm else 100
                keyp = pid
                if pct <= thr and keyp not in cur_low:
                    cur_low.add(keyp)
                    n += 1
                    out(e["clock"], pid, f"{h['hero']} просел до {pct:.0f}% HP "
                                         f"({h.get('hp')}/{hm}) в «{h.get('region')}»")
                elif pct > thr + 15:
                    cur_low.discard(keyp)
        print(f"найдено: {n}")

    elif cmd == "fights":
        fights = list(iter_jsonl(os.path.join(decoded, "teamfights.jsonl")))
        if not fights:
            print("teamfights.jsonl не найден — сначала запустите analyze.py")
            return 1
        for f in fights:
            print(f["summary"])
        print(f"всего драк: {len(fights)}")

    elif cmd == "ping":
        n = 0
        lines_started = 0
        for p in iter_jsonl(os.path.join(decoded, "pings.jsonl")):
            kind = p.get("_kind", "ping")
            pid = p.get("player_id")
            if not player_ok(pid):
                continue
            hero = ctx.by_pid.get(pid, {}).get("hero", f"игрок {pid}")
            clock, _gt = ctx.clock(p.get("_tick", 0))
            if kind == "location_ping":
                d = p.get("location_ping") or {}
                if d.get("x") is None:
                    continue
                n += 1
                out(clock, pid, f"пинг {hero}: точка ({d['x']}, {d['y']}) — {region_name(d['x'], d['y'])}")
            elif kind == "map_line":
                d = p.get("mapline") or {}
                if d.get("initial"):
                    lines_started += 1
                    n += 1
                    out(clock, pid, f"линия на карте {hero}: от ({d.get('x')}, {d.get('y')}) — "
                                    f"{region_name(d.get('x') or 0, d.get('y') or 0)}")
            else:
                n += 1
                out(clock, pid, f"{kind} {hero}: {p}")
        print(f"найдено: {n} (пингов-точек и начатых линий; сегментов линий всего {lines_started})")

    else:
        print(f"неизвестная команда: {cmd}\n")
        print(__doc__)
        return 2
    return 0


def _pid_of_name(self, internal):
    return self.pid_of(internal)


Ctx.pid_of_name = _pid_of_name

if __name__ == "__main__":
    raise SystemExit(main())
