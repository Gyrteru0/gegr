#!/usr/bin/env python3
"""
compare.py — сводка по серии дуэлей 1v1 (несколько разобранных демок).

  python compare.py dir1_decoded dir2_decoded ... [--players 0,1]

Для каждой папки берёт duel.json (если нет — считает сам через duel.py) и
строит COMPARE.md + compare.json в текущей папке (или --out ПАПКА):
таблицу игр с ключевыми цифрами игрока 1 (обычно человек) против игрока 2
(обычно бот) и динамику от первой игры к последней. Порядок игр — как передан
на входе (обычно хронологический).

Смысл: слабость бота — это регулярность. По одной демке видны эпизоды,
по серии — привычки бота и прогресс игрока.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from duel import compute_duel  # noqa: E402


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def at_minute(d, minute, pid, field):
    for r in d["minutes"]:
        if r["minute"] == minute:
            return (r.get(f"p{pid}") or {}).get(field)
    if d["minutes"]:
        r = d["minutes"][-1]
        return (r.get(f"p{pid}") or {}).get(field)
    return None


def main():
    players_arg = None
    out_dir = "."
    argv = sys.argv[1:]
    if "--players" in argv:
        players_arg = argv[argv.index("--players") + 1]
    if "--out" in argv:
        out_dir = argv[argv.index("--out") + 1]
    skip = set()
    for flag in ("--players", "--out"):
        if flag in argv:
            skip.update((argv.index(flag), argv.index(flag) + 1))
    args = [a for i, a in enumerate(argv) if i not in skip and not a.startswith("--")]
    if len(args) < 1:
        print(__doc__)
        return 2

    duels = []
    for decoded in args:
        dj = os.path.join(decoded, "duel.json")
        d = load_json(dj, None)
        if d is None:
            d = compute_duel(decoded, players_arg)
            with open(dj, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
        d["_label"] = os.path.basename(os.path.normpath(decoded))
        duels.append(d)

    rows = []
    for d in duels:
        p1, p2 = d["players"]
        pid1, pid2 = p1["player_id"], p2["player_id"]
        rz1 = d["raze"].get(str(pid1), {})
        rz2 = d["raze"].get(str(pid2), {})
        towers = d.get("towers_t1") or {}
        first_tower = None
        for _nm, t in towers.items():
            if t.get("first_damage_clock"):
                first_tower = t["first_damage_clock"]
                break
        nw10_a = at_minute(d, 10, pid1, "net_worth")
        nw10_b = at_minute(d, 10, pid2, "net_worth")
        rows.append({
            "label": d["_label"], "duration": d["duration_clock"],
            "heroes": f"{p1['hero']} vs {p2['hero']}",
            "kills": f"{d['score_kills'].get(str(pid1))} — {d['score_kills'].get(str(pid2))}",
            "deaths1": len(d["deaths"].get(str(pid1), [])),
            "deaths2": len(d["deaths"].get(str(pid2), [])),
            "cs10_1": at_minute(d, 10, pid1, "last_hits"),
            "cs10_2": at_minute(d, 10, pid2, "last_hits"),
            "dn10_1": at_minute(d, 10, pid1, "denies"),
            "dn10_2": at_minute(d, 10, pid2, "denies"),
            "nw_diff10": (nw10_a - nw10_b) if nw10_a is not None and nw10_b is not None else None,
            "raze_casts1": rz1.get("casts"), "raze_acc1": rz1.get("accuracy_attributed_pct"),
            "raze_casts2": rz2.get("casts"), "raze_acc2": rz2.get("accuracy_attributed_pct"),
            "souls10_1": None,  # заполняется из строки 10-й минуты ниже
            "retreats1": len(d["retreats"].get(str(pid1), [])),
            "retreats2": len(d["retreats"].get(str(pid2), [])),
            "first_tower_dmg": first_tower,
        })
        # души@10 — из строки 10-й минуты (или последней, если игра короче)
        cand = [r for r in d["minutes"] if r["minute"] <= 10] or d["minutes"]
        if cand:
            rr = cand[-1].get(f"p{pid1}") or {}
            rows[-1]["souls10_1"] = rr.get("souls", rr.get("souls_est"))

    L = ["# Серия дуэлей 1v1\n"]
    L.append("| Игра | Длит. | Счёт убийств | Смерти И1/И2 | CS@10 И1 — И2 | Денаи@10 | "
             "Разн. NW@10 (И1) | Рейзы И1: касты/точн. | Рейзы И2: касты/точн. | Души@10 И1 | "
             "Отступл. И1/И2 | Первый урон по T1 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['label']} | {r['duration']} | {r['kills']} | {r['deaths1']}/{r['deaths2']} | "
                 f"{r['cs10_1']} — {r['cs10_2']} | {r['dn10_1']}/{r['dn10_2']} | {r['nw_diff10']} | "
                 f"{r['raze_casts1']}/{r['raze_acc1']}% | {r['raze_casts2']}/{r['raze_acc2']}% | "
                 f"{r['souls10_1']} | {r['retreats1']}/{r['retreats2']} | {r['first_tower_dmg']} |")
    if len(rows) >= 2:
        f, l = rows[0], rows[-1]
        L.append("\n## Динамика первая → последняя игра\n")
        for key, name in (("cs10_1", "CS@10 игрока 1"), ("nw_diff10", "разница нетворса@10"),
                          ("raze_acc1", "точность рейзов игрока 1, %"),
                          ("raze_casts2", "кастов рейзов игрока 2 (бота)"),
                          ("raze_acc2", "точность рейзов игрока 2 (бота), %"),
                          ("retreats2", "отступлений игрока 2 (бота)"),
                          ("deaths2", "смертей игрока 2 (бота)")):
            if isinstance(f.get(key), (int, float)) and isinstance(l.get(key), (int, float)):
                L.append(f"- {name}: {f[key]} → {l[key]} (Δ {l[key] - f[key]:+})")
    p1 = duels[0]["players"][0]
    p2 = duels[0]["players"][1]
    L.append(f"\nИгрок 1: {p1['hero']} ({p1.get('name') or p1['player_id']}"
             f"{', бот' if p1.get('is_bot') else ''}); "
             f"игрок 2: {p2['hero']} ({p2.get('name') or p2['player_id']}"
             f"{', бот' if p2.get('is_bot') else ''}). "
             f"Подробности каждой игры — в её DUEL.md; моменты открываются "
             f"`moment.py --time MM:SS --player N`.\n")

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "COMPARE.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    with open(os.path.join(out_dir, "compare.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"Готово: COMPARE.md и compare.json в {out_dir} (игр: {len(rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
