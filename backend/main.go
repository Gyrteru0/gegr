// dota2-backend — низкоуровневый декодер демок Dota 2 (Source 2, PBDEMS2).
//
// Основан на github.com/dotabuff/manta (MIT). Читает .dem и пишет в выходную
// папку потоки JSONL, которые потом собирает decode.py в удобный для ИИ вид:
//
//	backend_meta.json   — заголовок демки + CDemoFileInfo (матч, игроки, пики/баны)
//	combatlog.jsonl     — боевой лог (урон/хил/смерти/покупки/руны/опыт/золото...)
//	chat.jsonl          — сообщения чата
//	game_events.jsonl   — ВСЕ игровые события (dota_*, player_*, ...)
//	unit_orders.jsonl   — приказы игроков (куда кликали, что кастили) — spectator orders
//	modifiers.jsonl     — включение/выключение баффов и дебаффов
//	snapshots.jsonl     — снимки состояния мира каждые N тиков (герои, здания, ...)
//	entity_classes.json — все классы сущностей, встреченные в демке, со счётчиками
//
// Использование:
//	dota2-backend -input match.dem -out outdir [-sample-every 30] [-all-entities]
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/dotabuff/manta"
	"github.com/dotabuff/manta/dota"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

var (
	inPath      = flag.String("input", "", "path to .dem file")
	outDir      = flag.String("out", "", "output directory")
	sampleEvery = flag.Int("sample-every", 30, "snapshot interval in ticks (30 ticks = 1 second)")
	allEntities = flag.Bool("all-entities", false, "include every CDOTA entity in snapshots (large!)")
)

type writer struct{ f *os.File; w *bufio.Writer }

func newWriter(path string) *writer {
	f, err := os.Create(path)
	if err != nil {
		panic(err)
	}
	return &writer{f: f, w: bufio.NewWriterSize(f, 1<<16)}
}
func (w *writer) writeJSON(v interface{}) {
	b, err := json.Marshal(v)
	if err != nil {
		return
	}
	w.w.Write(b)
	w.w.WriteByte('\n')
}
func (w *writer) close() { w.w.Flush(); w.f.Close() }

// jsonValue превращает значение поля сущности в JSON-совместимое.
func jsonValue(v interface{}) interface{} {
	switch x := v.(type) {
	case nil, string, bool, int32, int64, uint32, uint64, float32, float64, int, uint:
		return x
	case []float32:
		out := make([]float64, len(x))
		for i, f := range x {
			out[i] = float64(f)
		}
		return out
	case []byte:
		return string(x)
	default:
		// Неизвестный тип — честно пишем его строковое представление.
		return fmt.Sprintf("%v", x)
	}
}

// pickFields берёт из сущности только перечисленные поля, которые реально есть.
func pickFields(e *manta.Entity, names []string) map[string]interface{} {
	out := map[string]interface{}{}
	for _, n := range names {
		if v := e.Get(n); v != nil {
			out[n] = jsonValue(v)
		}
	}
	return out
}

// numberFields берёт все поля сущности, чьё имя содержит одно из подстрок
// (для gamerules / playerresource, где список полей меняется от патча к патчу).
func prefixedFields(e *manta.Entity, prefix string) map[string]interface{} {
	out := map[string]interface{}{}
	for k, v := range e.Map() {
		if strings.HasPrefix(k, prefix) {
			out[strings.TrimPrefix(k, prefix)] = jsonValue(v)
		}
	}
	return out
}

// handleIndex: entity handle -> entity index (младшие 14 бит).
func handleIndex(h uint32) int32 {
	if h == 16777215 { // INVALID_EHANDLE_INDEX
		return -1
	}
	return int32(h & 0x3FFF)
}

var heroFields = []string{
	"m_iPlayerID", "m_iTeamNum", "m_nPlayerOwnerID",
	"m_iHealth", "m_iMaxHealth", "m_flMana", "m_flMaxMana",
	"m_flHealthRegen", "m_flManaRegen",
	"m_iCurrentLevel", "m_iCurrentXP", "m_iAbilityPoints", "m_iTotalAbilityPoints",
	"m_lifeState", "m_flRespawnTime", "m_flDeathTime", "m_bIsIllusion",
	"m_flLastAttackTime", "m_flLastDamageTime", "m_flLastDealtDamageTime",
	"CBodyComponent.m_cellX", "CBodyComponent.m_cellY", "CBodyComponent.m_cellZ",
	"CBodyComponent.m_vecOrigin", "CBodyComponent.m_angRotation",
}

var npcFields = []string{
	"m_iTeamNum", "m_iHealth", "m_iMaxHealth", "m_flMana", "m_flMaxMana",
	"m_iCurrentLevel", "m_lifeState", "m_iPlayerID",
	"CBodyComponent.m_cellX", "CBodyComponent.m_cellY", "CBodyComponent.m_cellZ",
	"CBodyComponent.m_vecOrigin",
}

var abilityFields = []string{
	"m_iLevel", "m_fCooldown", "m_fCooldownLength", "m_bInAbilityPhase",
	"m_iCurrentCharges", "m_iSecondaryCharges",
}

func protoToMap(m proto.Message) map[string]interface{} {
	if m == nil {
		return nil
	}
	b, err := protojson.MarshalOptions{EmitUnpopulated: false, UseProtoNames: true}.Marshal(m)
	if err != nil {
		return map[string]interface{}{"_error": err.Error()}
	}
	var out map[string]interface{}
	if err := json.Unmarshal(b, &out); err != nil {
		return map[string]interface{}{"_raw": string(b)}
	}
	return out
}

func main() {
	flag.Parse()
	if *inPath == "" || *outDir == "" {
		fmt.Fprintln(os.Stderr, "usage: dota2-backend -input match.dem -out outdir [-sample-every 30]")
		os.Exit(2)
	}
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		panic(err)
	}

	meta := map[string]interface{}{"input_file": filepath.Base(*inPath)}
	counts := map[string]int64{}

	combatW := newWriter(filepath.Join(*outDir, "combatlog.jsonl"))
	defer combatW.close()
	chatW := newWriter(filepath.Join(*outDir, "chat.jsonl"))
	defer chatW.close()
	eventsW := newWriter(filepath.Join(*outDir, "game_events.jsonl"))
	defer eventsW.close()
	ordersW := newWriter(filepath.Join(*outDir, "unit_orders.jsonl"))
	defer ordersW.close()
	modsW := newWriter(filepath.Join(*outDir, "modifiers.jsonl"))
	defer modsW.close()
	snapsW := newWriter(filepath.Join(*outDir, "snapshots.jsonl"))
	defer snapsW.close()

	f, err := os.Open(*inPath)
	if err != nil {
		panic(err)
	}
	defer f.Close()

	parser, err := manta.NewStreamParser(f)
	if err != nil {
		panic(err)
	}

	// ---------- отслеживаемые сущности ----------
	heroes := map[int32]*manta.Entity{}     // CDOTA_Unit_Hero_*
	npcs := map[int32]*manta.Entity{}       // башни, казармы, Рошан, курьеры, варды...
	abilities := map[int32]*manta.Entity{}  // предметы и способности (по entity index)
	var playerResource, gamerules *manta.Entity
	teamData := map[string]*manta.Entity{} // CDOTA_DataRadiant / CDOTA_DataDire / ...
	classEvents := map[string]int64{}
	classAlive := map[string]map[int32]bool{}

	track := func(e *manta.Entity) {
		cn := e.GetClassName()
		if _, ok := classAlive[cn]; !ok {
			classAlive[cn] = map[int32]bool{}
		}
		classAlive[cn][e.GetIndex()] = true
		switch {
		case strings.HasPrefix(cn, "CDOTA_Unit_Hero_"):
			heroes[e.GetIndex()] = e
		case strings.HasPrefix(cn, "CDOTA_Item_"), strings.HasPrefix(cn, "CDOTA_Ability_"), cn == "CDOTABaseAbility":
			abilities[e.GetIndex()] = e
		case cn == "CDOTA_PlayerResource":
			playerResource = e
		case cn == "CDOTAGamerulesProxy" || cn == "CDOTA_GamerulesProxy":
			gamerules = e
		case strings.HasPrefix(cn, "CDOTA_Data"):
			teamData[cn] = e
		case cn == "CDOTA_BaseNPC_Tower" || cn == "CDOTA_BaseNPC_Barracks" ||
			cn == "CDOTA_BaseNPC_Fort" || cn == "CDOTA_BaseNPC_Building" ||
			cn == "CDOTA_Unit_Roshan" || cn == "CDOTA_Unit_Courier" ||
			cn == "CDOTA_NPC_Observer_Ward" || cn == "CDOTA_NPC_Sentry_Ward" ||
			cn == "CDOTA_BaseNPC_Creep_Lane" || cn == "CDOTA_BaseNPC_Creep_Siege" ||
			strings.HasPrefix(cn, "CDOTA_BaseNPC") && *allEntities:
			npcs[e.GetIndex()] = e
		}
	}
	untrack := func(e *manta.Entity) {
		cn := e.GetClassName()
		if s, ok := classAlive[cn]; ok {
			delete(s, e.GetIndex())
		}
		delete(heroes, e.GetIndex())
		delete(npcs, e.GetIndex())
		delete(abilities, e.GetIndex())
	}

	parser.OnEntity(func(e *manta.Entity, op manta.EntityOp) error {
		classEvents[e.GetClassName()]++
		if op.Flag(manta.EntityOpCreated) || op.Flag(manta.EntityOpEntered) {
			track(e)
		}
		if op.Flag(manta.EntityOpDeleted) {
			untrack(e)
		}
		return nil
	})

	// ---------- метаданные ----------
	parser.Callbacks.OnCDemoFileHeader(func(m *dota.CDemoFileHeader) error {
		meta["file_header"] = protoToMap(m)
		return nil
	})
	parser.Callbacks.OnCDemoFileInfo(func(m *dota.CDemoFileInfo) error {
		meta["file_info"] = protoToMap(m)
		writeSnapshot(parser, heroes, npcs, abilities, playerResource, gamerules, teamData, snapsW, true)
		return nil
	})

	// ---------- боевой лог ----------
	combatName := func(idx uint32) string {
		if s, ok := parser.LookupStringByIndex("CombatLogNames", int32(idx)); ok {
			return s
		}
		return fmt.Sprintf("combatlog_name_%d", idx)
	}
	writeCombat := func(tick uint32, e *dota.CMsgDOTACombatLogEntry) {
		counts["combatlog"]++
		row := protoToMap(e)
		row["_tick"] = tick
		row["_type_name"] = e.GetType().String()
		// Имена в боевом логе — индексы в таблице CombatLogNames: разрешаем в строки.
		if e.TargetName != nil {
			row["_target"] = combatName(e.GetTargetName())
		}
		if e.AttackerName != nil {
			row["_attacker"] = combatName(e.GetAttackerName())
		}
		if e.DamageSourceName != nil {
			row["_damage_source"] = combatName(e.GetDamageSourceName())
		}
		if e.InflictorName != nil {
			row["_inflictor"] = combatName(e.GetInflictorName())
		}
		if e.TargetSourceName != nil {
			row["_target_source"] = combatName(e.GetTargetSourceName())
		}
		combatW.writeJSON(row)
	}
	// Путь 1: юзер-сообщения с батчами боевого лога (спектаторский HLTV-лог).
	parser.Callbacks.OnCDOTAUserMsg_CombatLogBulkData(func(m *dota.CDOTAUserMsg_CombatLogBulkData) error {
		for _, e := range m.GetCombatEntries() {
			writeCombat(parser.Tick, e)
		}
		return nil
	})
	// Путь 1б: HLTV-стрим боевого лога — основной путь в серверных матчмейкинг-демках.
	parser.Callbacks.OnCMsgDOTACombatLogEntry(func(e *dota.CMsgDOTACombatLogEntry) error {
		writeCombat(parser.Tick, e)
		return nil
	})
	// Путь 2: игровое событие dota_combatlog (старые демки).
	parser.OnGameEvent("dota_combatlog", func(ge *manta.GameEvent) error {
		fields := ge.Fields()
		// Поля игрового события повторяют CMsgDOTACombatLogEntry по смыслу;
		// пишем их как есть + нормализованные _-поля, если ключи числовые.
		row := map[string]interface{}{"_tick": parser.Tick, "_source": "game_event"}
		for k, v := range fields {
			row[k] = v
		}
		if t, err := ge.GetInt32("type"); err == nil {
			row["_type_name"] = dota.DOTA_COMBATLOG_TYPES(t).String()
		}
		counts["combatlog"]++
		combatW.writeJSON(row)
		return nil
	})

	// ---------- чат ----------
	parser.Callbacks.OnCDOTAUserMsg_ChatMessage(func(m *dota.CDOTAUserMsg_ChatMessage) error {
		counts["chat"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		row["_kind"] = "dota_chat_message"
		chatW.writeJSON(row)
		return nil
	})
	parser.Callbacks.OnCUserMessageSayText2(func(m *dota.CUserMessageSayText2) error {
		counts["chat"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		row["_kind"] = "say_text2"
		chatW.writeJSON(row)
		return nil
	})
	parser.Callbacks.OnCDOTAUserMsg_ChatEvent(func(m *dota.CDOTAUserMsg_ChatEvent) error {
		counts["chat_events"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		row["_kind"] = "chat_event"
		chatW.writeJSON(row)
		return nil
	})

	// ---------- все игровые события ----------
	parser.OnGameEvent("*", func(ge *manta.GameEvent) error {
		if ge.Name() == "dota_combatlog" {
			return nil // уже записан выше
		}
		counts["game_events"]++
		row := map[string]interface{}{"_tick": parser.Tick, "_event": ge.Name()}
		for k, v := range ge.Fields() {
			row[k] = v
		}
		eventsW.writeJSON(row)
		return nil
	})

	// ---------- приказы игроков (клики/касты, как их видел сервер) ----------
	parser.Callbacks.OnCDOTAUserMsg_SpectatorPlayerUnitOrders(func(m *dota.CDOTAUserMsg_SpectatorPlayerUnitOrders) error {
		counts["unit_orders"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		ordersW.writeJSON(row)
		return nil
	})

	// ---------- баффы/дебаффы ----------
	parser.OnModifierTableEntry(func(m *dota.CDOTAModifierBuffTableEntry) error {
		counts["modifiers"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		if name, ok := parser.LookupStringByIndex("ModifierNames", m.GetModifierClass()); ok {
			row["_modifier_name"] = name
		}
		if m.Parent != nil {
			row["_parent_index"] = handleIndex(m.GetParent())
		}
		if m.Caster != nil {
			row["_caster_index"] = handleIndex(m.GetCaster())
		}
		if m.Ability != nil {
			row["_ability_index"] = handleIndex(m.GetAbility())
		}
		modsW.writeJSON(row)
		return nil
	})

	// ---------- пинги и линии по карте ----------
	pingsW := newWriter(filepath.Join(*outDir, "pings.jsonl"))
	defer pingsW.close()
	parser.Callbacks.OnCDOTAUserMsg_Ping(func(m *dota.CDOTAUserMsg_Ping) error {
		counts["pings"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		row["_kind"] = "ping"
		pingsW.writeJSON(row)
		return nil
	})
	parser.Callbacks.OnCDOTAUserMsg_LocationPing(func(m *dota.CDOTAUserMsg_LocationPing) error {
		counts["pings"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		row["_kind"] = "location_ping"
		pingsW.writeJSON(row)
		return nil
	})
	parser.Callbacks.OnCDOTAUserMsg_MapLine(func(m *dota.CDOTAUserMsg_MapLine) error {
		counts["pings"]++
		row := protoToMap(m)
		row["_tick"] = parser.Tick
		row["_kind"] = "map_line"
		pingsW.writeJSON(row)
		return nil
	})

	// ---------- снимки мира ----------
	lastSample := -1
	trySample := func() {
		if *sampleEvery <= 0 {
			return
		}
		if int(parser.Tick)-lastSample >= *sampleEvery || lastSample < 0 {
			lastSample = int(parser.Tick)
			writeSnapshot(parser, heroes, npcs, abilities, playerResource, gamerules, teamData, snapsW, false)
		}
	}
	parser.Callbacks.OnCDemoPacket(func(m *dota.CDemoPacket) error { trySample(); return nil })
	parser.Callbacks.OnCDemoFullPacket(func(m *dota.CDemoFullPacket) error { trySample(); return nil })

	if err := parser.Start(); err != nil {
		meta["parse_error"] = err.Error()
		fmt.Fprintln(os.Stderr, "parse error:", err)
	}
	meta["final_tick"] = parser.Tick
	meta["game_build"] = parser.GameBuild
	meta["counts"] = counts

	// классы сущностей
	type classStat struct {
		Class  string `json:"class"`
		Events int64  `json:"entity_events"`
		Alive  int    `json:"alive_at_end"`
	}
	var stats []classStat
	for cn, n := range classEvents {
		stats = append(stats, classStat{cn, n, len(classAlive[cn])})
	}
	sort.Slice(stats, func(i, j int) bool { return stats[i].Events > stats[j].Events })
	ecf, _ := os.Create(filepath.Join(*outDir, "entity_classes.json"))
	enc := json.NewEncoder(ecf)
	enc.SetIndent("", "  ")
	enc.Encode(stats)
	ecf.Close()

	mb, _ := json.MarshalIndent(meta, "", "  ")
	os.WriteFile(filepath.Join(*outDir, "backend_meta.json"), mb, 0o644)
	fmt.Printf("done: tick=%d counts=%v\n", parser.Tick, counts)
}

// worldPos пересчитывает позицию из клетки (cell) и смещения внутри неё
// в мировые координаты Dota (карта примерно от -16384 до +16384).
// Поддерживает оба поколения полей: m_vecOrigin (вектор) и m_vecX/m_vecY (скаляры).
func numFloat(v interface{}) (float64, bool) {
	switch x := v.(type) {
	case float32:
		return float64(x), true
	case float64:
		return x, true
	case int32:
		return float64(x), true
	case int:
		return float64(x), true
	case uint32:
		return float64(x), true
	case uint64:
		return float64(x), true
	}
	return 0, false
}

func worldPos(e *manta.Entity) (float64, float64, bool) {
	cx, okX := numFloat(e.Get("CBodyComponent.m_cellX"))
	cy, okY := numFloat(e.Get("CBodyComponent.m_cellY"))
	if !okX || !okY {
		return 0, 0, false
	}
	var ox, oy float64
	if vec, ok := e.Get("CBodyComponent.m_vecOrigin").([]float32); ok && len(vec) >= 2 {
		ox, oy = float64(vec[0]), float64(vec[1])
	} else if vx, ok1 := numFloat(e.Get("CBodyComponent.m_vecX")); ok1 {
		vy, ok2 := numFloat(e.Get("CBodyComponent.m_vecY"))
		if !ok2 {
			return 0, 0, false
		}
		ox, oy = vx, vy
	} else {
		return 0, 0, false
	}
	x := (cx-128.0)*128.0 + ox
	y := (cy-128.0)*128.0 + oy
	if math.IsNaN(x) || math.IsNaN(y) {
		return 0, 0, false
	}
	return x, y, true
}

var playerKeep = map[string]bool{
	"m_iszPlayerName": true, "m_iPlayerSteamID": true, "m_iPlayerTeam": true,
	"m_bIsValid": true, "m_bIsBot": true, "m_bFakeClient": true,
	"m_iConnectionState": true, "m_nPlayerSlot": true,
}

var teamKeep = map[string]bool{
	"m_iKills": true, "m_iDeaths": true, "m_iAssists": true, "m_iLevel": true,
	"m_iRespawnSeconds": true, "m_flLastBuybackTime": true, "m_iCustomBuybackCost": true,
	"m_iStreak": true, "m_iFirstBloodClaimed": true, "m_iFirstBloodGiven": true,
	"m_iTeamSlot": true, "m_hSelectedHero": true, "m_nSelectedHeroID": true,
	"m_bHasRandomed": true, "m_iBountyRunes": true, "m_iPowerRunes": true,
	"m_iWaterRunes": true, "m_iOutpostsCaptured": true, "m_flTeamFightParticipation": true,
}

func keepTeamField(name string) bool {
	if teamKeep[name] {
		return true
	}
	for _, s := range []string{"Gold", "LastHit", "Deny", "NetWorth", "XP", "HeroId", "HeroID"} {
		if strings.Contains(name, s) {
			return true
		}
	}
	return false
}

var dataTeamKeep = map[string]bool{
	"m_iTotalEarnedGold": true, "m_iTotalEarnedXP": true, "m_iNetWorth": true,
	"m_iReliableGold": true, "m_iUnreliableGold": true, "m_iLastHitCount": true,
	"m_iDenyCount": true, "m_iNearbyCreepDeathCount": true, "m_flHeroDamage": true,
	"m_flTowerDamage": true, "m_fHealing": true, "m_fStuns": true,
	"m_iCampsStacked": true, "m_iCreepsStacked": true, "m_iObserverWardsPlaced": true,
	"m_iSentryWardsPlaced": true, "m_iWardsDestroyed": true, "m_iRoshanKills": true,
	"m_iTormentorKills": true, "m_iTowerKills": true, "m_iRunePickups": true,
	"m_iCommandsIssued": true, "m_iPlayerSteamID": true, "m_nPlayerID": true,
	"m_iGoldSpentOnItems": true, "m_iGoldSpentOnSupport": true,
	"m_iGoldSpentOnConsumables": true, "m_iGoldSpentOnBuybacks": true,
	"m_iSmokesUsed": true, "m_iTPScrollsPurchased": true, "m_vPosition": true,
}

func keepTeamDataKey(k string) bool {
	if !strings.Contains(k, ".") {
		return true
	}
	if strings.HasPrefix(k, "m_iNetWorth.") {
		return true
	}
	parts := strings.Split(k, ".")
	if len(parts) >= 3 && parts[0] == "m_vecDataTeam" {
		return dataTeamKeep[parts[2]]
	}
	return false
}

func keepGameRuleKey(k string) bool {
	if strings.HasPrefix(k, "m_h") {
		return false
	}
	for _, s := range []string{"Time", "State", "Winner", "Paused", "Glyph", "Radar", "Tormentor", "GameMode", "Day", "Night", "Draft", "Pick"} {
		if strings.Contains(k, s) {
			return true
		}
	}
	return false
}

func writeSnapshot(parser *manta.Parser, heroes, npcs, abilities map[int32]*manta.Entity,
	playerResource, gamerules *manta.Entity, teamData map[string]*manta.Entity,
	out *writer, final bool) {

	snap := map[string]interface{}{"tick": parser.Tick, "final": final}

	if gamerules != nil {
		gr := map[string]interface{}{}
		for k, v := range prefixedFields(gamerules, "m_pGameRules.") {
			if keepGameRuleKey(k) {
				gr[k] = v
			}
		}
		if len(gr) > 0 {
			snap["gamerules"] = gr
		}
	}

	if playerResource != nil {
		pr := playerResource.Map()
		players := map[string]interface{}{}
		teams := map[string]interface{}{}
		extra := map[string]interface{}{}
		for k, v := range pr {
			switch {
			case strings.HasPrefix(k, "m_vecPlayerData."):
				rest := strings.TrimPrefix(k, "m_vecPlayerData.")
				parts := strings.SplitN(rest, ".", 2)
				if len(parts) == 2 {
					m, _ := players[parts[0]].(map[string]interface{})
					if m == nil {
						m = map[string]interface{}{}
						players[parts[0]] = m
					}
					if playerKeep[parts[1]] {
						m[parts[1]] = jsonValue(v)
					}
				}
			case strings.HasPrefix(k, "m_vecPlayerTeamData."):
				rest := strings.TrimPrefix(k, "m_vecPlayerTeamData.")
				parts := strings.SplitN(rest, ".", 2)
				if len(parts) == 2 {
					m, _ := teams[parts[0]].(map[string]interface{})
					if m == nil {
						m = map[string]interface{}{}
						teams[parts[0]] = m
					}
					if keepTeamField(parts[1]) {
						m[parts[1]] = jsonValue(v)
					}
				}
			default:
				extra[k] = jsonValue(v)
			}
		}
		// Оставляем только слоты живых игроков и слоты с игровой статистикой.
		fTeams := map[string]interface{}{}
		for slot, v := range teams {
			keep := false
			if pv, ok := players[slot]; ok {
				if valid, _ := pv.(map[string]interface{})["m_bIsValid"].(bool); valid {
					keep = true
				}
			}
			if m, ok := v.(map[string]interface{}); ok {
				if lv, _ := m["m_iLevel"].(int32); lv > 0 {
					keep = true
				}
				if nw, _ := m["m_iNetWorth"].(int32); nw > 0 {
					keep = true
				}
			}
			if keep {
				fTeams[slot] = v
			}
		}
		snap["players"] = players
		snap["player_teams"] = fTeams
		if len(extra) > 0 {
			snap["player_resource_extra"] = extra
		}
	}

	td := map[string]interface{}{}
	for name, e := range teamData {
		filtered := map[string]interface{}{}
		for k, v := range e.Map() {
			if !keepTeamDataKey(k) {
				continue
			}
			filtered[k] = jsonValue(v)
		}
		td[name] = filtered
	}
	if len(td) > 0 {
		snap["team_data"] = td
	}

	// (abilities/предметы добавим ниже — только те, что держат герои)
	referenced := map[int32]bool{}

	heroList := []interface{}{}
	for idx, e := range heroes {
		row := map[string]interface{}{"entity_index": idx, "class": e.GetClassName()}
		for k, v := range pickFields(e, heroFields) {
			row[k] = v
		}
		if x, y, ok := worldPos(e); ok {
			row["world_x"] = math.Round(x*10) / 10
			row["world_y"] = math.Round(y*10) / 10
		}
		// инвентарь и способности: хэндлы 0000..0023
		items := []interface{}{}
		for i := 0; i < 24; i++ {
			hv := e.Get(fmt.Sprintf("m_hItems.%04d", i))
			h, ok := hv.(uint32)
			if !ok || h == 16777215 {
				continue
			}
			item := map[string]interface{}{"slot": i, "handle": h, "entity_index": handleIndex(h)}
			referenced[handleIndex(h)] = true
			if ae, ok2 := abilities[handleIndex(h)]; ok2 {
				item["class"] = ae.GetClassName()
			}
			_ = item
			items = append(items, item)
		}
		row["items"] = items
		abils := []interface{}{}
		for i := 0; i < 32; i++ {
			hv := e.Get(fmt.Sprintf("m_vecAbilities.%04d", i))
			if hv == nil {
				hv = e.Get(fmt.Sprintf("m_hAbilities.%04d", i))
			}
			h, ok := hv.(uint32)
			if !ok || h == 16777215 {
				continue
			}
			ab := map[string]interface{}{"slot": i, "handle": h, "entity_index": handleIndex(h)}
			referenced[handleIndex(h)] = true
			if ae, ok2 := abilities[handleIndex(h)]; ok2 {
				ab["class"] = ae.GetClassName()
				if lv, ok3 := ae.Get("m_iLevel").(int32); ok3 {
					ab["level"] = lv
				}
				if cd, ok3 := ae.Get("m_fCooldown").(float32); ok3 {
					ab["cooldown"] = cd
				}
			}
			abils = append(abils, ab)
		}
		row["ability_handles"] = abils
		heroList = append(heroList, row)
	}
	sort.Slice(heroList, func(i, j int) bool {
		return heroList[i].(map[string]interface{})["entity_index"].(int32) <
			heroList[j].(map[string]interface{})["entity_index"].(int32)
	})
	snap["heroes"] = heroList

	// предметы/способности, которые реально держат герои в этом снимке
	abilitySnap := map[string]interface{}{}
	for idx := range referenced {
		e, ok := abilities[idx]
		if !ok {
			continue
		}
		row := map[string]interface{}{"class": e.GetClassName()}
		for k, v := range pickFields(e, abilityFields) {
			row[k] = v
		}
		abilitySnap[fmt.Sprint(idx)] = row
	}
	snap["abilities"] = abilitySnap

	npcList := []interface{}{}
	for idx, e := range npcs {
		row := map[string]interface{}{"entity_index": idx, "class": e.GetClassName()}
		for k, v := range pickFields(e, npcFields) {
			row[k] = v
		}
		if x, y, ok := worldPos(e); ok {
			row["world_x"] = math.Round(x*10) / 10
			row["world_y"] = math.Round(y*10) / 10
		}
		npcList = append(npcList, row)
	}
	sort.Slice(npcList, func(i, j int) bool {
		return npcList[i].(map[string]interface{})["entity_index"].(int32) <
			npcList[j].(map[string]interface{})["entity_index"].(int32)
	})
	snap["npcs"] = npcList

	out.writeJSON(snap)
}
