#!/usr/bin/env python3
"""
decode.py — расшифровщик демок Dota 2 (.dem, Source 2) для людей и ИИ.

Демка Dota 2 — это НЕ шифр, а бинарный контейнер PBDEMS2: protobuf-сообщения,
сжатые Snappy, внутри которых лежат снапшоты сущностей игры (герои, здания,
предметы...), боевой лог, чат, приказы игроков и игровые события. Эта программа
достаёт из неё АБСОЛЮТНО всё и раскладывает по файлам, которые удобно читать
нейросети (как «живой зритель»):

  summary.json      — что это за матч: карта, режим, длительность, игроки, итог
  players.json      — игроки и их герои (финальные KDA / уровень / нетворс / предметы)
  timeline.jsonl    — состояние мира каждую секунду: позиции, HP/MP, уровень,
                      золото, предметы и кулдауны всех героев, здания, Рошан, варды
  events.jsonl      — всё, что произошло: убийства, смерти, покупки предметов,
                      левел-апы, руны, падения башен, Рошана и т.д. (с источником)
  combatlog.jsonl   — полный боевой лог (урон, хил, золото, опыт, модификаторы)
  chat.jsonl        — чат игроков и системные сообщения чата
  game_events.jsonl — все игровые события движка
  unit_orders.jsonl — приказы игроков: куда и чем кликал каждый игрок
  modifiers.jsonl   — баффы/дебаффы: что, на кого и когда повесилось/снялось
  frames/           — кадры миникарты (PNG) + frames.jsonl с текстовым описанием
                      каждого кадра: мульти-модальный ИИ «видит» игру, текстовый —
                      читает описание той же картинки
  report.md         — человекочитаемая хроника матча (по минутам)
  AI_CONTEXT.md     — ОДИН файл, который можно целиком отдать нейросети:
                      сюжет матча по минутам + финальное состояние
  manifest.json     — что внутри разбора, счётчики, предупреждения

Использование:
  python decode.py match.dem
  python decode.py match.dem -o out_dir --frame-seconds 10
  python decode.py a.dem b.dem.bz2          # можно несколько, есть .bz2/.gz
  python decode.py match.dem --build-backend  # собрать Go-движок, если его нет

Движок сущностей (backend/dota2-backend, Go + manta) собирается один раз:
  python backend/setup_backend.py     (нужен Go >= 1.22, https://go.dev/dl/)
Без движка программа всё равно прочитает заголовок и FileInfo чистым Python
и честно напишет, чего не хватает.
"""
import argparse
import bz2
import gzip
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS_PER_SECOND = 30.0

# --------------------------------------------------------------------------
# Чистый Python: чтение внешнего контейнера PBDEMS2 (запасной путь и мета)
# --------------------------------------------------------------------------

def _read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def snappy_decompress(data):
    """Распаковка Snappy (block format) без внешних зависимостей."""
    if not data:
        return b""
    try:
        import snappy  # python-snappy, если вдруг установлен — он быстрее
        return snappy.decompress(data)
    except Exception:
        pass
    length, pos = _read_varint(data, 0)
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:  # literal
            n = tag >> 2
            if n < 60:
                ln = n + 1
            else:
                nb = n - 59
                ln = int.from_bytes(data[pos:pos + nb], "little") + 1
                pos += nb
            out += data[pos:pos + ln]
            pos += ln
        elif kind == 1:  # copy, 1-byte offset
            ln = ((tag >> 2) & 0x7) + 4
            off = ((tag & 0xE0) << 3) | data[pos]
            pos += 1
            for _ in range(ln):
                out.append(out[-off])
        elif kind == 2:  # copy, 2-byte offset
            ln = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
            for _ in range(ln):
                out.append(out[-off])
        else:  # copy, 4-byte offset
            ln = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
            for _ in range(ln):
                out.append(out[-off])
    assert len(out) == length, f"snappy: expected {length}, got {len(out)}"
    return bytes(out)


def _proto_fields(buf):
    pos = 0
    out = []
    while pos < len(buf):
        try:
            key, pos = _read_varint(buf, pos)
        except IndexError:
            break
        field, wt = key >> 3, key & 7
        if field == 0:
            break
        if wt == 0:
            v, pos = _read_varint(buf, pos)
            out.append((field, wt, v))
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            out.append((field, wt, buf[pos:pos + ln]))
            pos += ln
        elif wt == 5:
            out.append((field, wt, buf[pos:pos + 4]))
            pos += 4
        elif wt == 1:
            out.append((field, wt, buf[pos:pos + 8]))
            pos += 8
        else:
            break
    return out


DEMO_CMD_NAMES = {
    0: "Stop", 1: "FileHeader", 2: "FileInfo", 3: "SyncTick", 4: "SendTables",
    5: "ClassInfo", 6: "StringTables", 7: "Packet", 8: "SignonPacket",
    9: "ConsoleCmd", 10: "CustomData", 11: "CustomDataCallbacks", 12: "UserCmd",
    13: "FullPacket", 14: "SaveGame", 15: "SpawnGroups",
}

HEADER_FIELDS = {
    1: "demo_file_stamp", 2: "patch_version", 3: "server_name", 4: "client_name",
    5: "map_name", 6: "game_directory", 7: "fullpackets_version",
    8: "allow_clientside_entities", 9: "allow_clientside_particles", 10: "addons",
    11: "demo_version_name", 12: "demo_version_guid", 13: "build_num",
    14: "game", 15: "server_start_tick",
}


def python_census(dem_path):
    """Минимальный разбор без Go-движка: заголовок, FileInfo, перепись пакетов."""
    data = open(dem_path, "rb").read()
    if data[:8] != b"PBDEMS2\x00":
        raise ValueError("не похоже на демку Dota 2 (нет магии PBDEMS2)")
    info = {"file_size": len(data), "sha1": hashlib.sha1(data).hexdigest()[:16]}
    counts = Counter()
    header, fileinfo = {}, {}
    pos, max_tick = 16, 0
    while pos < len(data):
        try:
            cmd_raw, pos = _read_varint(data, pos)
            tick_raw, pos = _read_varint(data, pos)
            size, pos = _read_varint(data, pos)
        except IndexError:
            break
        cmd = cmd_raw & ~64
        payload = data[pos:pos + size]
        pos += size
        counts[DEMO_CMD_NAMES.get(cmd, f"cmd_{cmd}")] += 1
        if tick_raw != 0xFFFFFFFF:
            max_tick = max(max_tick, tick_raw)
        if cmd in (1, 2):
            try:
                raw = snappy_decompress(payload) if (cmd_raw & 64) else payload
            except Exception:
                continue
            fields = _proto_fields(raw)
            if cmd == 1:
                for num, _wt, v in fields:
                    name = HEADER_FIELDS.get(num, f"field_{num}")
                    header[name] = v.decode("utf-8", "replace") if isinstance(v, bytes) else v
            else:
                for num, wt, v in fields:
                    if num == 1 and wt == 5:
                        fileinfo["playback_time"] = round(struct.unpack("<f", v)[0], 3)
                    elif num == 2:
                        fileinfo["playback_ticks"] = v
                    elif num == 3:
                        fileinfo["playback_frames"] = v
                    elif num == 4 and wt == 2:
                        fileinfo["game_info_raw_bytes"] = len(v)
                        # CGameInfo -> dota (field 1?) — достаём match_id эвристически не будем,
                        # полные данные даст движок; сохраняем факт наличия.
                        fileinfo["has_game_info"] = True
    info["header"] = header
    info["file_info"] = fileinfo
    info["outer_packets"] = dict(counts)
    info["max_tick"] = max_tick
    info["duration_seconds_by_ticks"] = round(max_tick / TICKS_PER_SECOND, 1)
    return info


# --------------------------------------------------------------------------
# Имена героев / предметов / способностей из имён классов сущностей
# --------------------------------------------------------------------------

HERO_CLASS_ALIASES = {
    "Nevermore": "shadow_fiend", "VengefulSpirit": "vengefulspirit",
    "Wisp": "io", "DoomBringer": "doom_bringer", "ObsidianDestroyer": "obsidian_destroyer",
    "OutworldDevourer": "obsidian_destroyer", "ShadowShaman": "shadow_shaman",
    "Necrolyte": "necrophos", "SkeletonKing": "skeleton_king",
    "Zuus": "zuus", "Rattletrap": "rattletrap", "Furion": "furion",
    "TreantProtector": "treant", "Magnataur": "magnataur",
    "CentaurWarrunner": "centaur", "SlithereenGuard": "slardar",
    "Tidehunter": "tidehunter", "Undying": "undying",
    "QueenOfPain": "queenofpain", "FacelessVoid": "faceless_void",
    "PhantomAssassin": "phantom_assassin", "AntiMage": "antimage",
    "Windrunner": "windrunner", "CrystalMaiden": "crystal_maiden",
    "KeeperOfTheLight": "keeper_of_the_light", "Techies": "techies",
    "ElderTitan": "elder_titan", "LegionCommander": "legion_commander",
    "EarthSpirit": "earth_spirit", "EmberSpirit": "ember_spirit",
    "StormSpirit": "storm_spirit", "VoidSpirit": "void_spirit",
    "MonkeyKing": "monkey_king", "DarkWillow": "dark_willow",
    "ArcWarden": "arc_warden", "Mars": "Mars", "Dawnbreaker": "dawnbreaker",
    "PrimalBeast": "primal_beast", "Muerta": "muerta", "Ringmaster": "ringmaster",
    "Kez": "Kez",
}


def camel_to_snake(s):
    out = []
    for i, ch in enumerate(s):
        if ch.isupper() and i > 0 and (not s[i - 1].isupper() or (i + 1 < len(s) and s[i + 1].islower())):
            out.append("_")
        out.append(ch.lower())
    return "".join(out).replace("__", "_")


def hero_names(class_name):
    """CDOTA_Unit_Hero_Nevermore -> ('Nevermore', 'npc_dota_hero_shadow_fiend')"""
    short = class_name.replace("CDOTA_Unit_Hero_", "")
    internal = HERO_CLASS_ALIASES.get(short, camel_to_snake(short))
    return short, f"npc_dota_hero_{internal}"


def item_name(class_name):
    return "item_" + camel_to_snake(class_name.replace("CDOTA_Item_", ""))


def ability_name(class_name):
    return camel_to_snake(class_name.replace("CDOTA_Ability_", "").replace("CDOTA_Item_", "item_"))


TEAM_NAMES = {2: "Radiant", 3: "Dire", 0: "None", 1: "Spectator"}


def fmt_clock(seconds):
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else ""
    s = int(abs(seconds))
    return f"{sign}{s // 60:02d}:{s % 60:02d}"


def region_name(x, y):
    """Грубая, но честная география карты по мировым координатам."""
    if x is None or y is None:
        return "неизвестно"
    if math.hypot(x + 12500, y + 12500) < 3200:
        return "база Radiant"
    if math.hypot(x - 12500, y - 12500) < 3200:
        return "база Dire"
    if math.hypot(x - 2500, y + 2500) < 2600:
        return "район Рошана"
    lane_dist_mid = abs(x - y) / math.sqrt(2)
    if lane_dist_mid < 2200:
        return "мид (" + ("сторона Radiant)" if x < 0 else "сторона Dire)")
    if y > 4200:
        return "топ-лейн / север"
    if y < -4200:
        return "бот-лейн / юг"
    ns = "север" if y > 1400 else "юг" if y < -1400 else "центр"
    ew = "запад" if x < -1400 else "восток" if x > 1400 else "центр"
    side = "Radiant" if (x + y) < 0 else "Dire"
    if ns == "центр" and ew == "центр":
        return "центр карты / река"
    return f"{ns}-{ew} карты (половина {side})"


# --------------------------------------------------------------------------
# Рендер кадров миникарты: чистый PNG на zlib, без PIL
# --------------------------------------------------------------------------

def write_png(path, w, h, pixels):
    """pixels: bytearray RGB, w*h*3."""
    raw = bytearray()
    stride = w * 3
    for yy in range(h):
        raw.append(0)
        raw += pixels[yy * stride:(yy + 1) * stride]
    comp = zlib.compress(bytes(raw), 6)

    def chunk(typ, data_):
        c = struct.pack(">I", len(data_)) + typ + data_
        return c + struct.pack(">I", zlib.crc32(typ + data_) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


_FONT = {  # 3x5 цифр и пары букв — подписи героев на кадрах (номер игрока)
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "010", "010", "010", "010"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "011", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
    "R": ("110", "101", "110", "101", "101"), "D": ("110", "101", "101", "101", "110"),
}


def draw_text(px, w, h, x0, y0, text, color, scale=2):
    cx = x0
    for ch in str(text):
        glyph = _FONT.get(ch)
        if glyph:
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        for sx in range(scale):
                            for sy in range(scale):
                                xx, yy = cx + gx * scale + sx, y0 + gy * scale + sy
                                if 0 <= xx < w and 0 <= yy < h:
                                    i = (yy * w + xx) * 3
                                    px[i:i + 3] = bytes(color)
        cx += 4 * scale


def render_frame(path, snap, size=720):
    w = h = size
    px = bytearray(w * h * 3)

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

    # фон: тёмный лес (быстрая заливка всей картинки)
    px[:] = bytes((16, 42, 26)) * (w * h)

    WORLD = 8400  # играбельная часть карты Dota: примерно [-8400..8400]
    def to_px(x, y):
        x = max(-WORLD, min(WORLD, x))
        y = max(-WORLD, min(WORLD, y))
        return int((x + WORLD) / (2 * WORLD) * (w - 1)), int((WORLD - y) / (2 * WORLD) * (h - 1))

    def line(x1, y1, x2, y2, color, thick=3):
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        for i in range(steps + 1):
            x = int(x1 + (x2 - x1) * i / steps)
            y = int(y1 + (y2 - y1) * i / steps)
            fill_rect(x - thick // 2, y - thick // 2, thick, thick, color)

    # река (северо-запад -> юго-восток) и лейны
    pts = [to_px(-8200, 8200), to_px(8200, -8200)]
    line(*pts[0], *pts[1], (38, 84, 124), 14)
    lane_c = (92, 92, 78)
    for coords in [((-7200, -7200), (7200, 7200)),                      # мид
                   ((-7200, -7200), (-7200, 7200), (7200, 7200)),      # топ
                   ((-7200, -7200), (7200, -7200), (7200, 7200))]:     # бот
        for a, b in zip(coords, coords[1:]):
            pa, pb = to_px(*a), to_px(*b)
            line(*pa, *pb, lane_c, 5)
    # базы
    for (bx, by), c in [((-7200, -7200), (46, 140, 60)), ((7200, 7200), (170, 52, 44))]:
        cx, cy = to_px(bx, by)
        fill_rect(cx - 12, cy - 12, 24, 24, c)
    # здания (схема timeline: x/y/hp/team_num; поддерживаем и сырую world_x)
    for n in snap.get("npcs", []):
        nx = n.get("x", n.get("world_x"))
        ny = n.get("y", n.get("world_y"))
        nhp = n.get("hp", n.get("m_iHealth"))
        if nx is None or ny is None or (nhp or 0) <= 0:
            continue
        cx, cy = to_px(nx, ny)
        nteam = n.get("team_num", n.get("m_iTeamNum"))
        c = (46, 190, 80) if nteam == 2 else (230, 70, 55) if nteam == 3 else (150, 150, 150)
        s = 7 if "Tower" in (n.get("class") or "") else 6
        fill_rect(cx - s // 2, cy - s // 2, s, s, c)
    # герои
    for hero in snap.get("heroes", []):
        hx = hero.get("x", hero.get("world_x"))
        hy = hero.get("y", hero.get("world_y"))
        if hx is None or hy is None:
            continue
        cx, cy = to_px(hx, hy)
        dead = not hero.get("alive", (hero.get("m_lifeState") or 0) == 0)
        hteam = hero.get("team_num", hero.get("m_iTeamNum"))
        c = (90, 90, 90) if dead else (52, 235, 105) if hteam == 2 else (255, 74, 60)
        r = 11
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                    set_px(xx, yy, c)
        # полоска HP
        hp_frac = hero.get("hp_pct")
        if hp_frac is None and hero.get("m_iMaxHealth"):
            hp_frac = max(0.0, min(1.0, (hero.get("m_iHealth") or 0) / hero["m_iMaxHealth"])) * 100
        fill_rect(cx - r, cy - r - 6, int(2 * r * (hp_frac or 0) / 100), 3, (80, 255, 120))
        pid = hero.get("player_id", hero.get("m_iPlayerID"))
        if pid is not None and pid >= 0:
            draw_text(px, w, h, cx - 4, cy - 5, str(pid), (10, 10, 10), 2)
    write_png(path, w, h, px)


# --------------------------------------------------------------------------
# Пост-обработка потоков движка
# --------------------------------------------------------------------------

def load_jsonl(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def clean_hero(row, abilities_snap):
    short, internal = hero_names(row.get("class", ""))
    items = []
    for it in row.get("items", []):
        entry = {"slot": it.get("slot"), "item": item_name(it["class"]) if it.get("class") else None,
                 "class": it.get("class")}
        ab = abilities_snap.get(str(it.get("entity_index")), {})
        if ab.get("m_iCurrentCharges") is not None:
            entry["charges"] = ab["m_iCurrentCharges"]
        items.append(entry)
    abils = []
    for ab in row.get("ability_handles", []):
        if not ab.get("class"):
            continue
        entry = {"slot": ab.get("slot"), "ability": ability_name(ab["class"]),
                 "class": ab["class"], "level": ab.get("level"),
                 "entity_index": ab.get("entity_index"),
                 "cooldown_left": ab.get("cooldown")}
        extra = abilities_snap.get(str(ab.get("entity_index")), {})
        if extra.get("m_fCooldownLength") is not None:
            entry["cooldown_total"] = extra["m_fCooldownLength"]
        if extra.get("m_bInAbilityPhase") is not None:
            entry["in_ability_phase"] = bool(extra["m_bInAbilityPhase"])
        abils.append(entry)
    ang = row.get("CBodyComponent.m_angRotation") or []
    hp = row.get("m_iHealth")
    hp_max = row.get("m_iMaxHealth")
    mana = row.get("m_flMana")
    mana_max = row.get("m_flMaxMana")
    return {
        "player_id": row.get("m_iPlayerID"),
        "entity_player_id": row.get("m_iPlayerID"),
        "hero": short, "hero_internal": internal, "class": row.get("class"),
        "entity_index": row.get("entity_index"),
        "team": TEAM_NAMES.get(row.get("m_iTeamNum"), str(row.get("m_iTeamNum"))),
        "team_num": row.get("m_iTeamNum"),
        "x": row.get("world_x"), "y": row.get("world_y"),
        "region": region_name(row.get("world_x"), row.get("world_y")),
        "hp": hp, "hp_max": hp_max,
        "hp_pct": round(100 * hp / hp_max) if hp is not None and hp_max else None,
        "mana": round(mana) if mana is not None else None,
        "mana_max": round(mana_max) if mana_max is not None else None,
        "facing_yaw_deg": round(ang[1], 1) if len(ang) > 1 else None,
        "facing_pitch_deg": round(ang[0], 1) if len(ang) > 0 else None,
        "level": row.get("m_iCurrentLevel"), "xp": row.get("m_iCurrentXP"),
        "alive": (row.get("m_lifeState") or 0) == 0,
        "is_illusion": bool(row.get("m_bIsIllusion")),
        "respawn_seconds": round(row["m_flRespawnTime"], 1) if row.get("m_flRespawnTime") else 0,
        "last_attack_raw": row.get("m_flLastAttackTime"),
        "last_hurt_raw": row.get("m_flLastDamageTime"),
        "last_dealt_damage_raw": row.get("m_flLastDealtDamageTime"),
        "items": items, "abilities": abils,
    }


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



CREEP_CLASSES = {"CDOTA_BaseNPC_Creep_Lane", "CDOTA_BaseNPC_Creep_Siege"}


def summarize_waves(npcs):
    """Компактная сводка волн крипов линии: по команде и линии — сколько крипов,
    где центр волны (cx, cy) и средний %HP. Сами крипы в timeline не пишем:
    их сотни, а для анализа линии важен центр волны, а не каждый крип."""
    groups = {}
    for n in npcs:
        if n.get("class") not in CREEP_CLASSES or not n.get("alive"):
            continue
        if n.get("x") is None or n.get("team_num") not in (2, 3):
            continue
        lane = lane_of_pos(n["x"], n["y"])
        g = groups.setdefault((n["team_num"], lane),
                              {"n": 0, "melee": 0, "ranged": 0, "siege": 0,
                               "sx": 0.0, "sy": 0.0, "shp": 0.0})
        g["n"] += 1
        if n["class"] == "CDOTA_BaseNPC_Creep_Siege":
            g["siege"] += 1
        elif (n.get("hp_max") or 0) < 430:
            g["ranged"] += 1
        else:
            g["melee"] += 1
        g["sx"] += n["x"]
        g["sy"] += n["y"]
        if n.get("hp_max"):
            g["shp"] += (n.get("hp") or 0) / n["hp_max"] * 100
    out = {}
    for (team_num, lane), g in sorted(groups.items()):
        out.setdefault(str(team_num), {})[lane] = {
            "n": g["n"], "melee": g["melee"], "ranged": g["ranged"], "siege": g["siege"],
            "cx": round(g["sx"] / g["n"]), "cy": round(g["sy"] / g["n"]),
            "hp_pct": round(g["shp"] / g["n"])}
    return out


def lane_of_pos(x, y):
    """Линия по координатам: мид — диагональ, дальше по знаку (y-x)."""
    if x is None or y is None:
        return "?"
    if abs(x - y) <= 2400:
        return "мид"
    return "топ" if (y - x) > 2400 else "бот"


def build_npc_names(entries):
    """Имена башен/казарм по первому снимку, где они все на месте.

    Башни: 2 ближайшие к своей базе — T4, остальные по 3 на линию,
    снаружи внутрь — T1/T2/T3. Казармы: в паре на линии большая по HP —
    ближнего боя, меньшая — дальнего.
    """
    names = {}
    src = None
    for e in entries:
        towers = [n for n in e.get("npcs", []) if n.get("class") == "CDOTA_BaseNPC_Tower"]
        if len(towers) >= 18:
            src = e
            break
    if src is None:
        return names
    base_pt = {2: (-7000, -7000), 3: (7000, 7000)}
    for team_num, team_name in ((2, "Radiant"), (3, "Dire")):
        bx, by = base_pt[team_num]
        tw = [n for n in src["npcs"] if n.get("class") == "CDOTA_BaseNPC_Tower"
              and n.get("team_num") == team_num and n.get("x") is not None]
        tw.sort(key=lambda n: math.hypot(n["x"] - bx, n["y"] - by))
        t4, rest = tw[:2], tw[2:]
        for i, n in enumerate(t4, 1):
            names[(n["class"], n["entity_index"])] = f"{team_name} T4-{i} (у трона)"
        if len(rest) == 9:
            # Линии — кластеры по углу от базы: башни одной линии видны из угла
            # карты соседними лучами. Простая диагональная эвристика у базы
            # ошибается (T3 топ стоит почти на диагонали мида).
            rest.sort(key=lambda n: math.atan2(n["y"] - by, n["x"] - bx))
            groups = [rest[i:i + 3] for i in range(0, 9, 3)]
            groups.sort(key=lambda g: sum(n["y"] - n["x"] for n in g) / len(g))
            for lst, lane in zip(groups, ("бот", "мид", "топ")):
                lst.sort(key=lambda n: -math.hypot(n["x"] - bx, n["y"] - by))
                for tier, n in enumerate(lst, 1):
                    names[(n["class"], n["entity_index"])] = f"{team_name} T{tier} {lane}"
        else:
            by_lane = defaultdict(list)
            for n in rest:
                by_lane[lane_of_pos(n["x"], n["y"])].append(n)
            for lane, lst in by_lane.items():
                lst.sort(key=lambda n: -math.hypot(n["x"] - bx, n["y"] - by))
                for tier, n in enumerate(lst, 1):
                    names[(n["class"], n["entity_index"])] = f"{team_name} T{tier} {lane}"
        barracks = [n for n in src["npcs"] if n.get("class") == "CDOTA_BaseNPC_Barracks"
                    and n.get("team_num") == team_num and n.get("x") is not None]
        if len(barracks) == 6:
            # те же угловые кластеры от базы: 3 пары по линиям
            barracks.sort(key=lambda n: math.atan2(n["y"] - by, n["x"] - bx))
            pairs = [barracks[i:i + 2] for i in range(0, 6, 2)]
            pairs.sort(key=lambda g: sum(n["y"] - n["x"] for n in g) / len(g))
            for lst, lane in zip(pairs, ("бот", "мид", "топ")):
                lst.sort(key=lambda n: -(n.get("hp_max") or 0))
                for i, n in enumerate(lst):
                    kind = ["казарма ближнего боя", "казарма дальнего боя"][i] if i < 2 else "казарма"
                    names[(n["class"], n["entity_index"])] = f"{team_name} {kind} ({lane})"
        else:
            by_lane_b = defaultdict(list)
            for n in barracks:
                by_lane_b[lane_of_pos(n["x"], n["y"])].append(n)
            for lane, lst in by_lane_b.items():
                lst.sort(key=lambda n: -(n.get("hp_max") or 0))
                kinds = ["казарма ближнего боя", "казарма дальнего боя"]
                for i, n in enumerate(lst):
                    kind = kinds[i] if i < 2 else "казарма"
                    names[(n["class"], n["entity_index"])] = f"{team_name} {kind} ({lane})"
    for n in src["npcs"]:
        if n.get("class") == "CDOTA_BaseNPC_Fort":
            names[(n["class"], n["entity_index"])] = f"Трон {n.get('team')}"
        if n.get("class") == "CDOTA_Unit_Roshan":
            names[(n["class"], n["entity_index"])] = "Рошан"
    return names


# Боевой лог (таблица CombatLogNames) хранит legacy-имена нескольких героев,
# а сущности и таймлайн — современные. Приводим имена лога к именам сущностей,
# чтобы смерти/урон/модификаторы сопоставлялись с героями таймлайна.
COMBATLOG_HERO_ALIASES = {
    "npc_dota_hero_nevermore": "npc_dota_hero_shadow_fiend",
    "npc_dota_hero_wisp": "npc_dota_hero_io",
    "npc_dota_hero_shredder": "npc_dota_hero_timbersaw",
    "npc_dota_hero_abyssal_underlord": "npc_dota_hero_underlord",
    "npc_dota_hero_necrolyte": "npc_dota_hero_necrophos",
    "npc_dota_hero_obsidian_destroyer": "npc_dota_hero_outworld_destroyer",
}


def canon_cl_name(name):
    if not name:
        return name
    if name in COMBATLOG_HERO_ALIASES:
        return COMBATLOG_HERO_ALIASES[name]
    for old, new in COMBATLOG_HERO_ALIASES.items():
        if name.startswith(old + "_"):
            return new + name[len(old):]
    return name


def postprocess(raw_dir, out_dir, args, census):
    """Потоковая сборка: снимки читаем по одному, в памяти не держим весь матч."""
    meta = {}
    meta_path = os.path.join(raw_dir, "backend_meta.json")
    if os.path.isfile(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:
            meta = {}
    header = meta.get("file_header", {}) or census.get("header", {})
    file_info = meta.get("file_info", {}) or census.get("file_info", {})
    fi_dota = ((file_info.get("game_info") or {}).get("dota") or {})

    combatlog = load_jsonl(os.path.join(raw_dir, "combatlog.jsonl"))
    for e in combatlog:
        for k in ("_attacker", "_target", "_damage_source", "_target_source"):
            if e.get(k):
                e[k] = canon_cl_name(e[k])
    chat = load_jsonl(os.path.join(raw_dir, "chat.jsonl"))
    game_events = load_jsonl(os.path.join(raw_dir, "game_events.jsonl"))

    # --- якорь игрового времени (0 = горн, начало игры): переход боевого лога
    # в состояние GAME_IN_PROGRESS (value=5). Таймстампы боевого лога сами по
    # себе — время мира с момента создания, а НЕ игровой таймер, якорем быть
    # не могут (иначе часы уезжают на длительность драфта/прегейма).
    horn_demo_time, anchored = 0.0, "tick 0 (оценочно)"
    gs_ticks = [e["_tick"] for e in combatlog
                if (e.get("_type_name") or "").endswith("GAME_STATE")
                and e.get("value") == 5 and e.get("_tick") is not None]
    use_state_anchor = True
    if gs_ticks:
        horn_demo_time = min(gs_ticks) / TICKS_PER_SECOND
        anchored = "combatlog GAME_STATE -> in-progress"
        use_state_anchor = False

    # --- ПРОХОД 1: потоково по снимкам -> временный slim-таймлайн (demo_time),
    # реестр игроков, поиск горна по gamerules, если лога нет.
    players = {}
    for i, p in enumerate(fi_dota.get("player_info", []) or []):
        players[i] = {
            "player_id": i, "name": p.get("player_name"), "steam_id": p.get("steamid"),
            "hero_fileinfo": p.get("hero_name"), "is_bot_or_fake": p.get("is_fake_client"),
            "team_num": p.get("game_team"),
            "team": TEAM_NAMES.get(p.get("game_team")),
        }

    snap_path = os.path.join(raw_dir, "snapshots.jsonl")
    tmp_timeline = os.path.join(out_dir, ".timeline_tmp.jsonl")
    n_snapshots = 0
    state_horn_found = False
    hero_slot_map = {}  # entity_index героя -> слот игрока 0..9 (по m_hSelectedHero)
    with open(tmp_timeline, "w", encoding="utf-8") as tf:
        for s in iter_jsonl(snap_path):
            n_snapshots += 1
            gr = s.get("gamerules", {}) or {}
            if use_state_anchor and not state_horn_found and gr.get("m_nGameState") in (5, 6):
                horn_demo_time = s["tick"] / TICKS_PER_SECOND
                anchored = "gamerules state -> in-progress"
                state_horn_found = True

            # Слот игрока по хэндлу выбранного героя — надёжнее, чем m_iPlayerID
            # сущности (в новых билдах он идёт с шагом 2: 0,2,4,...,18).
            for slot, t_ in (s.get("player_teams", {}) or {}).items():
                handle = t_.get("m_hSelectedHero")
                if handle and handle != 16777215:
                    try:
                        hero_slot_map[handle & 0x3FFF] = int(slot)
                    except (ValueError, TypeError):
                        pass
            abilities_snap = s.get("abilities", {})
            heroes_raw = [clean_hero(h, abilities_snap) for h in s.get("heroes", [])]
            for h in heroes_raw:
                if h["entity_index"] in hero_slot_map:
                    h["player_id"] = hero_slot_map[h["entity_index"]]
                elif h["player_id"] is not None and h["player_id"] > 9:
                    h["player_id"] = h["player_id"] // 2  # запасной путь для B-нумерации
            by_pid = {}
            for h in heroes_raw:
                pid = h["player_id"]
                if pid is None or pid < 0:
                    continue
                cur = by_pid.get(pid)
                if cur is None or (not h["is_illusion"] and cur["is_illusion"]) or \
                        ((h["level"] or 0) > (cur["level"] or 0) and h["is_illusion"] == cur["is_illusion"]):
                    by_pid[pid] = h
            heroes = sorted(by_pid.values(), key=lambda h: h["player_id"])
            heroes += [h for h in heroes_raw if h["player_id"] is None or h["player_id"] < 0]

            pteams = s.get("player_teams", {}) or {}
            stats = {}
            for slot, t_ in pteams.items():
                try:
                    pid = int(slot)
                except (ValueError, TypeError):
                    continue
                stats[pid] = {"kills": t_.get("m_iKills"), "deaths": t_.get("m_iDeaths"),
                              "assists": t_.get("m_iAssists"), "level_pr": t_.get("m_iLevel"),
                              "buyback_cost": t_.get("m_iCustomBuybackCost"),
                              "respawn_seconds_pr": t_.get("m_iRespawnSeconds")}
            for team_class in ("CDOTA_DataRadiant", "CDOTA_DataDire"):
                td = (s.get("team_data", {}) or {}).get(team_class, {})
                per = defaultdict(dict)
                for k, v in td.items():
                    parts = k.split(".")
                    if len(parts) >= 3 and parts[0] == "m_vecDataTeam":
                        per[parts[1]][parts[2]] = v
                steam_to_slot = {}
                for slot2, pl2 in (s.get("players", {}) or {}).items():
                    if pl2.get("m_iPlayerSteamID"):
                        try:
                            steam_to_slot[pl2["m_iPlayerSteamID"]] = int(slot2)
                        except (ValueError, TypeError):
                            pass
                # m_nPlayerID в team_data — в "сырой" нумерации сущностей (шаг 2);
                # в локальных лобби с ботами Dire начинается с 2, а не с 10, поэтому
                # надёжно переводим в слот через героев этого же снимка.
                rawpid_to_slot = {h["entity_player_id"]: h["player_id"] for h in heroes
                                  if h.get("entity_player_id") is not None
                                  and h.get("player_id") is not None and h["player_id"] >= 0}
                for _idx, fields in per.items():
                    pid = fields.get("m_nPlayerID")
                    if pid is None:
                        continue
                    if pid in rawpid_to_slot:
                        pid = rawpid_to_slot[pid]
                    else:
                        pid = steam_to_slot.get(fields.get("m_iPlayerSteamID"),
                                                pid // 2 if isinstance(pid, int) and pid > 9 else pid)
                    st = stats.setdefault(pid, {})
                    st["net_worth"] = fields.get("m_iNetWorth")
                    st["total_gold"] = fields.get("m_iTotalEarnedGold")
                    st["total_xp"] = fields.get("m_iTotalEarnedXP")
                    st["last_hits"] = fields.get("m_iLastHitCount")
                    st["denies"] = fields.get("m_iDenyCount")
            for h in heroes:
                if h["player_id"] in stats:
                    h["stats"] = stats[h["player_id"]]

            # реестр игроков: по всем снимкам
            for slot, pl in (s.get("players", {}) or {}).items():
                try:
                    pid = int(slot)
                except (ValueError, TypeError):
                    continue
                if pl.get("m_bIsValid"):
                    p0 = players.setdefault(pid, {"player_id": pid})
                    if pl.get("m_iszPlayerName"):
                        p0["name"] = pl["m_iszPlayerName"]
                    if pl.get("m_iPlayerSteamID"):
                        p0["steam_id"] = pl["m_iPlayerSteamID"]
                    p0["is_bot"] = pl.get("m_bIsBot")
            for h in heroes:
                pid = h["player_id"]
                if pid is None or pid < 0:
                    continue
                p0 = players.setdefault(pid, {"player_id": pid})
                p0.setdefault("hero", h["hero"])
                p0.setdefault("team", h["team"])
                p0.setdefault("team_num", h["team_num"])
                p0["_last_level"] = h["level"]
                p0["_last_items"] = [i["item"] for i in h["items"] if i.get("item")]
                if h.get("stats"):
                    p0["_last_stats"] = h["stats"]
                if s.get("final"):
                    p0["final_level"] = h["level"]
                    p0["final_items"] = [i["item"] for i in h["items"] if i.get("item")]
                    if h.get("stats"):
                        p0["final_stats"] = h["stats"]

            npcs = [{
                "class": n.get("class"), "entity_index": n.get("entity_index"),
                "team": TEAM_NAMES.get(n.get("m_iTeamNum"), str(n.get("m_iTeamNum"))),
                "team_num": n.get("m_iTeamNum"), "hp": n.get("m_iHealth"),
                "hp_max": n.get("m_iMaxHealth"), "x": n.get("world_x"), "y": n.get("world_y"),
                "alive": (n.get("m_iHealth") or 0) > 0,
            } for n in s.get("npcs", [])]
            waves = summarize_waves(npcs)
            npcs = [n for n in npcs if n.get("class") not in CREEP_CLASSES]

            entry = {
                "tick": s["tick"], "demo_time_s": round(s["tick"] / TICKS_PER_SECOND, 2),
                "game_state": gr.get("m_nGameState"), "paused": gr.get("m_bGamePaused"),
                "winner_team_num": gr.get("m_nGameWinner"),
                "objectives": {k: v for k, v in gr.items()
                               if any(s in k for s in ("Glyph", "Radar", "TimeOfDay", "GameMode"))},
                "scores": {
                    "radiant": sum((h.get("stats", {}).get("kills") or 0) for h in heroes if h["team_num"] == 2),
                    "dire": sum((h.get("stats", {}).get("kills") or 0) for h in heroes if h["team_num"] == 3),
                },
                "heroes": heroes, "npcs": npcs, "final": s.get("final", False),
                **({"creeps": waves} if waves else {}),
            }
            tf.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def game_time(tick):
        return round(tick / TICKS_PER_SECOND - horn_demo_time, 2)

    for p in players.values():
        p.setdefault("final_level", p.pop("_last_level", None))
        p.setdefault("final_items", p.pop("_last_items", []))
        if "_last_stats" in p:
            p.setdefault("final_stats", p.pop("_last_stats"))
        p.pop("_last_level", None)
        p.pop("_last_items", None)

    # --- события из логов (боевой лог — главный источник в серверных демках)
    events = []

    def add_event(tick, kind, text, **kw):
        row = {"tick": tick, "game_time_s": game_time(tick), "clock": fmt_clock(game_time(tick)),
               "kind": kind, "text": text}
        row.update(kw)
        events.append(row)

    combat_deaths = []
    for e in combatlog:
        tname = (e.get("_type_name") or "?")
        for pref in ("DOTA_COMBATLOG_TYPES_", "DOTA_COMBATLOG_"):
            if tname.startswith(pref):
                tname = tname[len(pref):]
                break
        tick = e.get("_tick", 0)
        attacker, target, inflictor = e.get("_attacker"), e.get("_target"), e.get("_inflictor")
        base = dict(source="combatlog", attacker=attacker, target=target,
                    value=e.get("value"), inflictor=inflictor)
        if tname == "DEATH" and target:
            # в ленту — только смерти героев; смерти крипов целиком лежат в combatlog.jsonl
            if e.get("is_target_hero") or "npc_dota_hero" in target:
                add_event(tick, "death", f"Смерть: {target} (убийца: {attacker or '—'})", **base)
                combat_deaths.append((game_time(tick), (target or "").lower()))
            continue
        elif tname == "PURCHASE":
            add_event(tick, "purchase", f"Покупка предмета: {target or attacker} — {inflictor or e.get('value')}", **base)
        elif tname in ("GOLD", "XP", "DAMAGE", "HEAL", "MODIFIER_ADD", "MODIFIER_REMOVE",
                       "ABILITY", "ITEM", "GAME_STATE", "TEAM_BUILDING_KILL",
                       "PLAYERSTATS", "CRITICAL_DAMAGE", "MODIFIER_STACK_EVENT", "ABILITY_TRIGGER"):
            # слишком частые/технические типы целиком лежат в combatlog.jsonl,
            # в ленту событий не дублируем, чтобы лента оставалась читаемой
            if tname == "TEAM_BUILDING_KILL":
                add_event(tick, "building_death", f"Здание разрушено: {target}", **base)
            continue
        elif tname == "FIRST_BLOOD":
            add_event(tick, "first_blood", f"Первая кровь: {attacker} убивает {target}", **base)
        elif tname == "RUNE":
            add_event(tick, "rune", f"Руна: {attacker or target}", **base)
        elif tname in ("BUYBACK", "MULTIKILL", "KILLSTREAK"):
            add_event(tick, tname.lower(), f"{tname}: {attacker or target} ({e.get('value')})", **base)
        else:
            add_event(tick, tname.lower(), f"Боевой лог [{tname}]: {attacker} -> {target} ({e.get('value')})", **base)

    for e in game_events:
        if e.get("_event") in ("dota_chase_hero",):
            continue  # движение камеры наблюдателя — шум для ленты (файл сохранён целиком)
        add_event(e.get("_tick", 0), "game_event", f"Событие движка: {e.get('_event')}",
                  source="game_event", raw=e)
    for c in chat:
        tick = c.get("_tick", 0)
        if c.get("_kind") == "dota_chat_message":
            add_event(tick, "chat", f"Чат: игрок {c.get('source_player_id')}: {c.get('message_text')}",
                      source="chat", player_id=c.get("source_player_id"), text_raw=c.get("message_text"))
        elif c.get("_kind") == "chat_event":
            add_event(tick, "chat_event", f"Системное: {c.get('type')}", source="chat_event", raw=c)

    # --- ПРОХОД 2: по slim-таймлайну пишем финальный timeline.jsonl с игровым
    # временем, собираем кадры/минутки и дифф-события (смерти, предметы, здания)
    timeline_path = os.path.join(out_dir, "timeline.jsonl")
    pre_entries = []
    with open(tmp_timeline, encoding="utf-8") as _pf:
        for _line in _pf:
            if _line.strip():
                _e = json.loads(_line)
                pre_entries.append(_e)
                if sum(1 for n in _e.get("npcs", []) if n.get("class") == "CDOTA_BaseNPC_Tower") >= 18:
                    break
            if len(pre_entries) >= 20000:
                break
    npc_names = build_npc_names(pre_entries)
    frames_index, minute_snaps, frame_snaps = [], {}, []
    frame_step_ticks = max(1, int(args.frame_seconds * TICKS_PER_SECOND))
    next_frame_tick = -10**12
    prev = None
    hero_items_seen, hero_level_seen, npc_alive = defaultdict(set), {}, {}
    pending_npc_deaths = {}
    last_entry = None
    n_timeline = 0
    with open(tmp_timeline, encoding="utf-8") as tf, \
            open(timeline_path, "w", encoding="utf-8") as out_f:
        for line in tf:
            entry = json.loads(line)
            n_timeline += 1
            entry["game_time_s"] = game_time(entry["tick"])
            entry["clock"] = fmt_clock(entry["game_time_s"])
            for n in entry["npcs"]:
                nm = npc_names.get((n["class"], n["entity_index"]))
                if nm:
                    n["name"] = nm
            for h in entry["heroes"]:
                # m_flRespawnTime в демке — абсолютный момент возрождения,
                # а не "осталось секунд": пересчитываем в остаток.
                raw_resp = h.get("respawn_seconds") or 0
                if not h["alive"] and raw_resp > entry["game_time_s"]:
                    h["respawn_seconds"] = round(raw_resp - entry["game_time_s"], 1)
                elif not h["alive"] and raw_resp:
                    h["respawn_seconds"] = round(raw_resp, 1)
                else:
                    h["respawn_seconds"] = 0
            out_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            last_entry = entry
            minute = int((entry["game_time_s"] or 0) // 60)
            minute_snaps.setdefault(minute, entry)
            if not args.no_frames and entry["tick"] >= next_frame_tick:
                frame_snaps.append(entry)
                next_frame_tick = entry["tick"] + frame_step_ticks

            tick = entry["tick"]
            for h in entry["heroes"]:
                pid = h["player_id"]
                key = pid if pid is not None and pid >= 0 else f"ent{h['entity_index']}"
                if prev is not None:
                    ph = next((x for x in prev["heroes"]
                               if (x["player_id"] if x["player_id"] is not None and x["player_id"] >= 0
                                   else f"ent{x['entity_index']}") == key), None)
                    if ph is not None:
                        if ph["alive"] and not h["alive"]:
                            gt = entry["game_time_s"]
                            hero_norm = (h.get("hero_internal") or h["hero"]).lower()
                            dup = any(abs(gt - dt) < 4 and
                                      (hero_norm in tgt or h["hero"].lower().replace("_", "") in tgt.replace("_", ""))
                                      for dt, tgt in combat_deaths)
                            if not dup:
                                add_event(tick, "death", f"Смерть героя: {h['hero']} (игрок {pid})",
                                          source="snapshot", hero=h["hero"], player_id=pid)
                        if not ph["alive"] and h["alive"]:
                            add_event(tick, "respawn", f"Возрождение: {h['hero']}",
                                      source="snapshot", hero=h["hero"], player_id=pid)
                if h["level"] and hero_level_seen.get(key, 1) < h["level"]:
                    if key in hero_level_seen and h["level"] in (6, 12, 18, 25):
                        add_event(tick, "level_up",
                                  f"{h['hero']} (игрок {pid}) берёт уровень {h['level']}",
                                  source="snapshot", hero=h["hero"], player_id=pid, level=h["level"])
                hero_level_seen[key] = max(hero_level_seen.get(key, 0), h["level"] or 0)
                cur_items = {i["item"] for i in h["items"] if i.get("item")}
                for new_item in sorted(cur_items - hero_items_seen[key]):
                    if hero_items_seen[key]:
                        add_event(tick, "item_timing",
                                  f"{h['hero']} (игрок {pid}) получает предмет: {new_item} "
                                  f"(минута {entry['clock']})",
                                  source="snapshot", hero=h["hero"], player_id=pid, item=new_item)
                hero_items_seen[key] |= cur_items

            present = {(n["class"], n["entity_index"]): n for n in entry["npcs"]}
            for k, cand in list(pending_npc_deaths.items()):
                cur = present.get(k)
                if cur is not None and cur["alive"]:
                    del pending_npc_deaths[k]
                elif cur is not None and not cur["alive"]:
                    events.append(cand["row"])
                    del pending_npc_deaths[k]
                else:
                    cand["absent"] = cand.get("absent", 0) + 1
                    if cand["absent"] >= 3:
                        events.append(cand["row"])
                        del pending_npc_deaths[k]
            for k, n in present.items():
                if k not in npc_alive:
                    npc_alive[k] = n["alive"]
                    continue
                was = npc_alive[k]
                if was and not n["alive"] and prev is not None:
                    if "Ward" in (n["class"] or ""):
                        kind = "ward_down"
                        text = f"Вард ({n['team']}) исчез в районе: {region_name(n['x'], n['y'])}"
                    else:
                        label = n.get("name") or {"CDOTA_BaseNPC_Tower": "Башня", "CDOTA_BaseNPC_Barracks": "Казарма",
                                 "CDOTA_BaseNPC_Fort": "Трон", "CDOTA_Unit_Roshan": "Рошан"}.get(
                                     n["class"], n["class"])
                        kind = "building_death" if "Roshan" not in (n["class"] or "") else "roshan_death"
                        text = (f"Объект разрушен: {label} ({n['team']}), "
                                f"район: {region_name(n['x'], n['y'])}")
                    pending_npc_deaths[k] = {"absent": 0, "row": {
                        "tick": tick, "game_time_s": entry["game_time_s"], "clock": entry["clock"],
                        "kind": kind, "text": text, "source": "snapshot",
                        "npc_class": n["class"], "team": n["team"]}}
                npc_alive[k] = n["alive"]
            prev = entry
    for cand in pending_npc_deaths.values():
        if cand.get("absent", 0) >= 2:
            events.append(cand["row"])
    os.remove(tmp_timeline)
    events.sort(key=lambda r: (r["tick"], r["kind"]))

    # ---------------- запись файлов ----------------
    def dump(name, obj):
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        return path

    def dump_jsonl(name, rows):
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path

    summary = {
        "demo_file": census.get("file_name"),
        "file_size": census.get("file_size"),
        "map_name": header.get("map_name"),
        "server_name": header.get("server_name"),
        "client_name": header.get("client_name"),
        "patch_version": header.get("patch_version"),
        "build_num": header.get("build_num"),
        "demo_version": header.get("demo_version_name"),
        "match_id": fi_dota.get("match_id"),
        "game_mode_id": fi_dota.get("game_mode"),
        "winner_team_num": fi_dota.get("game_winner"),
        "winner": TEAM_NAMES.get(fi_dota.get("game_winner")),
        "league_id": fi_dota.get("leagueid"),
        "radiant_team_id": fi_dota.get("radiant_team_id"),
        "dire_team_id": fi_dota.get("dire_team_id"),
        "radiant_team_tag": fi_dota.get("radiant_team_tag"),
        "dire_team_tag": fi_dota.get("dire_team_tag"),
        "playback_seconds": file_info.get("playback_time") or census.get("duration_seconds_by_ticks"),
        "playback_ticks": file_info.get("playback_ticks") or census.get("max_tick"),
        "game_time_anchor": anchored,
        "horn_demo_time_s": round(horn_demo_time, 2),
        "players": [players[k] for k in sorted(players)],
        "picks_bans": fi_dota.get("picks_bans"),
        "counts": {
            "timeline_snapshots": n_timeline, "events": len(events),
            "combatlog": len(combatlog), "chat": len(chat), "game_events": len(game_events),
        },
        "unfinished_demo": not bool(fi_dota),
    }
    if not fi_dota:
        summary["note"] = ("В демке нет финального CDemoFileInfo с итогами матча "
                           "(запись оборвана до конца игры) — итог восстановлен из "
                           "последнего снимка мира.")
    dump("summary.json", summary)
    dump("players.json", summary["players"])
    dump_jsonl("events.jsonl", events)
    dump_jsonl("combatlog.jsonl", combatlog)
    dump_jsonl("chat.jsonl", chat)
    dump_jsonl("game_events.jsonl", game_events)
    for extra in ("unit_orders.jsonl", "modifiers.jsonl", "pings.jsonl", "entity_classes.json", "backend_meta.json"):
        src = os.path.join(raw_dir, extra)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(out_dir, extra))

    # ---------------- кадры ----------------
    if not args.no_frames and frame_snaps:
        frames_dir = os.path.join(out_dir, "frames")
        os.makedirs(frames_dir, exist_ok=True)
        for i, snap in enumerate(frame_snaps):
            fname = f"frame_{i:04d}_t{snap['game_time_s']:07.1f}s.png"
            render_frame(os.path.join(frames_dir, fname), snap)
            frames_index.append({"frame": i, "file": f"frames/{fname}",
                                   "tick": snap["tick"], "game_time_s": snap["game_time_s"],
                                   "clock": snap["clock"],
                                   "description": describe_frame(snap, players)})
        dump_jsonl("frames.jsonl", frames_index)

    # ---------------- текстовые отчёты ----------------
    timeline_for_reports = {"minute_snaps": minute_snaps, "last": last_entry}
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(build_report(summary, players, events, chat, timeline_for_reports))
    with open(os.path.join(out_dir, "AI_CONTEXT.md"), "w", encoding="utf-8") as f:
        f.write(build_ai_context(summary, players, events, timeline_for_reports, frames_index))

    manifest = {
        "demo": census,
        "backend_used": bool(meta),
        "parse_error": meta.get("parse_error"),
        "outputs": sorted(os.listdir(out_dir)),
        "counts": summary["counts"],
    }
    dump("manifest.json", manifest)
    return summary


def describe_frame(snap, players):
    parts = [f"{snap['clock']}: "]
    hs = []
    for h in snap["heroes"]:
        if h["player_id"] is None or h["player_id"] < 0:
            continue
        name = (players.get(h["player_id"], {}) or {}).get("name") or h["hero"]
        state = f"HP {h['hp_pct']}%" if h["alive"] else f"МЁРТВ (респ {h['respawn_seconds']}с)"
        hs.append(f"#{h['player_id']} {h['hero']} ({name}) ур.{h['level']} {state}, {h['region']}")
    parts.append("; ".join(hs) if hs else "героев в кадре нет")
    towers_alive = sum(1 for n in snap["npcs"] if "Tower" in (n["class"] or "") and n["alive"])
    parts.append(f"Живых башен на карте: {towers_alive}.")
    return " ".join(parts)


def hero_label(players_map, pid, hero):
    if pid is not None and pid in players_map and players_map[pid].get("name"):
        return f"{hero} ({players_map[pid]['name']})"
    return hero or "?"


def build_report(summary, players, events, chat, timeline):
    L = []
    L.append(f"# Разбор демки Dota 2 — {summary.get('demo_file')}\n")
    L.append(f"- Карта: `{summary.get('map_name')}`, сервер: {summary.get('server_name')}, "
             f"патч/билд: {summary.get('patch_version')}/{summary.get('build_num')}")
    L.append(f"- Длительность записи: {summary.get('playback_seconds')} c "
             f"({summary.get('playback_ticks')} тиков)")
    if summary.get("match_id"):
        L.append(f"- Match ID: {summary['match_id']}, режим: {summary.get('game_mode_id')}, "
                 f"победитель: {summary.get('winner')}")
    if summary.get("unfinished_demo"):
        L.append("- ⚠ Демка без финального экрана (запись оборвана) — финал восстановлен по миру.")
    L.append("\n## Игроки (финальное состояние)\n")
    L.append("| # | Игрок | Герой | Команда | Ур. | K/D/A | Нетворс | Предметы |")
    L.append("|---|-------|-------|---------|-----|-------|---------|----------|")
    for p in summary.get("players", []):
        st = p.get("final_stats", {}) or {}
        kda = f"{st.get('kills', '—')}/{st.get('deaths', '—')}/{st.get('assists', '—')}" if st else "—"
        L.append(f"| {p['player_id']} | {p.get('name') or '—'} | {p.get('hero') or p.get('hero_fileinfo') or '—'} "
                 f"| {p.get('team') or '—'} | {p.get('final_level') or '—'} | {kda} "
                 f"| {st.get('net_worth') or '—'} | {', '.join(p.get('final_items') or [])} |")
    L.append("\n## Ключевые события\n")
    key_kinds = {"death", "first_blood", "building_death", "roshan_death", "item_timing",
                 "level_up", "chat"}
    for e in events:
        if e["kind"] in key_kinds:
            L.append(f"- `{e['clock']}` {e['text']}  _(источник: {e['source']})_")
    if chat:
        L.append("\n## Чат\n")
        for c in chat:
            if c.get("_kind") == "dota_chat_message":
                L.append(f"- игрок {c.get('source_player_id')}: {c.get('message_text')}")
            elif c.get("_kind") == "chat_event":
                L.append(f"- системное событие чата: {c.get('type')} (игроки {c.get('playerid_1')}/{c.get('playerid_2')})")
    L.append("\n## Как смотреть дальше (для ИИ)\n")
    L.append("- `timeline.jsonl` — покадровое состояние мира (1 строка = 1 секунда игры).")
    L.append("- `frames/` + `frames.jsonl` — миникарта картинками и текстом одновременно.")
    L.append("- `AI_CONTEXT.md` — вся игра одним файлом по минутам.")
    return "\n".join(L) + "\n"


def build_ai_context(summary, players, events, timeline, frames_index):
    players_map = {p["player_id"]: p for p in summary.get("players", [])}
    L = []
    L.append("# Dota 2 — разбор демки для ИИ (смотреть как зритель)\n")
    L.append(f"Файл: {summary.get('demo_file')}; карта {summary.get('map_name')}; "
             f"длительность {summary.get('playback_seconds')} c. "
             f"Время ниже — игровое (0:00 = горн/начало игры).\n")
    L.append("## Составы\n")
    for p in summary.get("players", []):
        st = p.get("final_stats", {}) or {}
        L.append(f"- #{p['player_id']} {p.get('name') or 'бот/неизвестно'} — "
                 f"{p.get('hero') or p.get('hero_fileinfo') or '?'} ({p.get('team') or '?'}), "
                 f"финал: ур.{p.get('final_level')}, KDA {st.get('kills')}/{st.get('deaths')}/{st.get('assists')}, "
                 f"нетворс {st.get('net_worth')}, предметы: {', '.join(p.get('final_items') or [])}")
    # поминутная хроника
    L.append("\n## Игра по минутам\n")
    by_minute = defaultdict(list)
    for e in events:
        if e["kind"] in ("death", "first_blood", "building_death", "roshan_death",
                         "item_timing", "respawn"):
            by_minute[int((e["game_time_s"] or 0) // 60)].append(e)
    snap_by_minute = timeline.get("minute_snaps", {}) if isinstance(timeline, dict) else {}
    frame_by_minute = {}
    for fr in frames_index:
        frame_by_minute.setdefault(int((fr["game_time_s"] or 0) // 60), fr)
    for minute in sorted(snap_by_minute):
        snap = snap_by_minute[minute]
        L.append(f"### Минута {minute} ({snap['clock']})")
        if minute in frame_by_minute:
            L.append(f"Кадр миникарты: `{frame_by_minute[minute]['file']}` — "
                     f"{frame_by_minute[minute]['description']}")
        alive_desc = []
        for h in snap["heroes"]:
            if h["player_id"] is None or h["player_id"] < 0:
                continue
            alive_desc.append(
                f"{h['hero']} ур.{h['level']} HP{h['hp_pct']}% в «{h['region']}»"
                + ("" if h["alive"] else " (мёртв)"))
        if alive_desc:
            L.append("Позиции: " + "; ".join(alive_desc) + ".")
        for e in by_minute.get(minute, []):
            L.append(f"- `{e['clock']}` {e['text']}")
        L.append("")
    L.append("## Финальный снимок мира\n")
    fin = timeline.get("last") if isinstance(timeline, dict) else None
    if fin:
        for h in fin["heroes"]:
            items = ", ".join(i["item"] for i in h["items"] if i.get("item"))
            abils = ", ".join(f"{a['ability']}[{a['level']}]" for a in h["abilities"][:8])
            L.append(f"- {h['hero']} ({h['team']}) ур.{h['level']}: HP {h['hp']}/{h['hp_max']}, "
                     f"мана {h['mana']}/{h['mana_max']}, позиция {h['region']} "
                     f"({h['x']}, {h['y']}); предметы: {items}; способности: {abils}")
    L.append("\n_Полные данные: timeline.jsonl (каждая секунда), combatlog.jsonl, "
             "unit_orders.jsonl (все приказы игроков), frames/ (миникарта)._")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# Запуск движка, main
# --------------------------------------------------------------------------

def find_backend():
    names = ["dota2-backend.exe", "dota2-backend"] if os.name == "nt" else ["dota2-backend", "dota2-backend.exe"]
    for n in names:
        p = os.path.join(HERE, "backend", n)
        if os.path.isfile(p):
            return p
    return None


def maybe_decompress(path, tmpdir):
    """Распаковка по магическим байтам, а не по расширению: Valve с недавних пор
    отдаёт реплеи как '<id>.dem.bz2', внутри которых на самом деле zstd."""
    with open(path, "rb") as f:
        magic = f.read(6)
    base = os.path.basename(path)
    for suf in (".bz2", ".gz", ".zst"):
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    if magic[:3] == b"BZh":
        out = os.path.join(tmpdir, base)
        with bz2.open(path, "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return out
    if magic[:2] == b"\x1f\x8b":
        out = os.path.join(tmpdir, base)
        with gzip.open(path, "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return out
    if magic[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
        except ImportError:
            raise SystemExit(
                "Файл сжат zstd (так сейчас отдаёт реплеи Valve, даже с именем .dem.bz2).\n"
                "Поставьте модуль:  pip install zstandard   — или распакуйте вручную:  zstd -d файл")
        out = os.path.join(tmpdir, base)
        with open(path, "rb") as src, open(out, "wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)
        return out
    return path


def process_one(dem_path, out_root, args):
    name = os.path.basename(dem_path)
    for suf in (".dem.bz2", ".dem.gz", ".dem.zst", ".dem"):
        if name.lower().endswith(suf):
            name = name[: -len(suf)]
            break
    out_dir = os.path.join(out_root, f"{name}_decoded") if out_root else f"{name}_decoded"
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n=== {dem_path} -> {out_dir}")

    # Временные файлы — на диске рядом с результатом, а не в /tmp:
    # сырые снимки полного матча весят сотни мегабайт и не влезают в tmpfs.
    tmp_parent = os.path.join(os.path.abspath(out_root or "."), ".decode_tmp")
    os.makedirs(tmp_parent, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_parent) as tmp:
        real_dem = maybe_decompress(dem_path, tmp)
        census = python_census(real_dem)
        census["file_name"] = os.path.basename(dem_path)

        backend = find_backend()
        if args.build_backend and not backend:
            print("Собираю Go-движок (один раз)...")
            subprocess.run([sys.executable, os.path.join(HERE, "backend", "setup_backend.py")],
                           check=False)
            backend = find_backend()

        raw_dir = os.path.join(out_dir, "_raw") if args.keep_raw else os.path.join(tmp, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        if backend:
            sample_every = max(1, round(args.sample_seconds * TICKS_PER_SECOND))
            cmd = [backend, "-input", real_dem, "-out", raw_dir,
                   "-sample-every", str(sample_every)]
            if args.all_entities:
                cmd.append("-all-entities")
            print("Движок:", " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True)
            print(r.stdout.strip())
            if r.returncode != 0:
                print("Движок завершился с ошибкой:", r.stderr.strip()[:2000])
        else:
            print("ВНИМАНИЕ: backend/dota2-backend не собран — беру только то, что "
                  "читается чистым Python (заголовок/FileInfo).\n"
                  "Полный разбор: установите Go >= 1.22 и выполните "
                  "`python backend/setup_backend.py`, либо запустите с --build-backend.")

        summary = postprocess(raw_dir, out_dir, args, census)

    print(f"Готово. Снимков: {summary['counts']['timeline_snapshots']}, "
          f"событий: {summary['counts']['events']}, "
          f"боевой лог: {summary['counts']['combatlog']}, чат: {summary['counts']['chat']}")
    return out_dir


def main():
    ap = argparse.ArgumentParser(description="Расшифровщик демок Dota 2 для людей и ИИ")
    ap.add_argument("demos", nargs="+", help=".dem / .dem.bz2 / .dem.gz / .dem.zst файлы (сжатие определяется по содержимому)")
    ap.add_argument("-o", "--out", default=".", help="папка, куда класть <имя>_decoded")
    ap.add_argument("--sample-seconds", type=float, default=1.0,
                    help="период снимков мира в секундах игры (по умолчанию 1)")
    ap.add_argument("--frame-seconds", type=float, default=5.0,
                    help="период кадров миникарты (по умолчанию 5 c)")
    ap.add_argument("--no-frames", action="store_true", help="не рендерить кадры")
    ap.add_argument("--all-entities", action="store_true",
                    help="включать в снимки вообще все сущности (очень большие файлы)")
    ap.add_argument("--keep-raw", action="store_true",
                    help="сохранить сырые потоки движка в <out>/_raw")
    ap.add_argument("--build-backend", action="store_true",
                    help="собрать Go-движок, если он ещё не собран")
    args = ap.parse_args()

    made = []
    for dem in args.demos:
        if not os.path.isfile(dem):
            print(f"Нет такого файла: {dem}", file=sys.stderr)
            continue
        made.append(process_one(dem, args.out, args))
    if made:
        print("\nРазборы готовы:")
        for m in made:
            print(" -", m, " (начните с AI_CONTEXT.md или report.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
