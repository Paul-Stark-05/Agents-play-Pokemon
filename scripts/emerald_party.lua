-- emerald_party.lua
-- Reads Pokemon Emerald (USA, Europe) state in mGBA 0.10+.
-- Addresses from the pokeemerald symbol file, struct offsets from its headers.
-- Load with: Tools > Scripting > File > Load script.
-- Then type report(), enemy() or battle() in the prompt.

local PLAYER_PARTY   = 0x020244EC
local PLAYER_COUNT   = 0x020244E9
local ENEMY_PARTY    = 0x02024744
local ENEMY_COUNT    = 0x020244EA
local BATTLE_MONS    = 0x02024084
local BATTLERS_COUNT = 0x0202406C
local BATTLE_TYPE    = 0x02022FEC

local SPECIES_NAMES = 0x083185C8  -- 11 bytes per entry
local MOVE_NAMES    = 0x0831977C  -- 13 bytes per entry
local TYPE_NAMES    = 0x0831AE38  -- 7 bytes per entry
local ABILITY_NAMES = 0x0831B6DB  -- 13 bytes per entry

local MON_SIZE        = 100
local BATTLE_MON_SIZE = 88
local SECURE_OFFSET   = 0x20
local SUBSTRUCT_SIZE  = 12
local PARTY_SIZE      = 6

-- personality % 24 -> physical slot of substruct types 0..3 (growth, attacks, evs, misc)
local ORDER = {
  [0]  = {0,1,2,3}, [1]  = {0,1,3,2}, [2]  = {0,2,1,3}, [3]  = {0,3,1,2},
  [4]  = {0,2,3,1}, [5]  = {0,3,2,1}, [6]  = {1,0,2,3}, [7]  = {1,0,3,2},
  [8]  = {2,0,1,3}, [9]  = {3,0,1,2}, [10] = {2,0,3,1}, [11] = {3,0,2,1},
  [12] = {1,2,0,3}, [13] = {1,3,0,2}, [14] = {2,1,0,3}, [15] = {3,1,0,2},
  [16] = {2,3,0,1}, [17] = {3,2,0,1}, [18] = {1,2,3,0}, [19] = {1,3,2,0},
  [20] = {2,1,3,0}, [21] = {3,1,2,0}, [22] = {2,3,1,0}, [23] = {3,2,1,0},
}

-- Gen 3 text: 0x00 space, 0xA1-0xAA digits, 0xBB-0xD4 A-Z, 0xD5-0xEE a-z, 0xFF end
local function decodeText(addr, maxLen)
  local out = {}
  for i = 0, maxLen - 1 do
    local b = emu:read8(addr + i)
    if b == 0xFF then break end
    if b == 0x00 then
      out[#out + 1] = " "
    elseif b >= 0xA1 and b <= 0xAA then
      out[#out + 1] = string.char(string.byte("0") + b - 0xA1)
    elseif b >= 0xBB and b <= 0xD4 then
      out[#out + 1] = string.char(string.byte("A") + b - 0xBB)
    elseif b >= 0xD5 and b <= 0xEE then
      out[#out + 1] = string.char(string.byte("a") + b - 0xD5)
    else
      out[#out + 1] = "?"
    end
  end
  return table.concat(out)
end

local function speciesName(id) return decodeText(SPECIES_NAMES + id * 11, 10) end
local function moveName(id)    return decodeText(MOVE_NAMES + id * 13, 12) end
local function typeName(id)    return decodeText(TYPE_NAMES + id * 7, 6) end
local function abilityName(id) return decodeText(ABILITY_NAMES + id * 13, 12) end

local function statusText(status1)
  if status1 & 0x07 ~= 0 then return "asleep" end
  if status1 & 0x80 ~= 0 then return "badly poisoned" end
  if status1 & 0x08 ~= 0 then return "poisoned" end
  if status1 & 0x10 ~= 0 then return "burned" end
  if status1 & 0x20 ~= 0 then return "frozen" end
  if status1 & 0x40 ~= 0 then return "paralyzed" end
  return "ok"
end

-- 16-bit field inside a logical substruct (type 0..3) of a party slot
local function substructU16(base, pid, substructType, byteOffset, key)
  local slot = ORDER[pid % 24][substructType + 1]
  local byteInSecure = slot * SUBSTRUCT_SIZE + byteOffset
  local word = emu:read32(base + SECURE_OFFSET + (byteInSecure // 4) * 4) ~ key
  if byteInSecure % 4 == 0 then return word & 0xFFFF end
  return (word >> 16) & 0xFFFF
end

local function readMon(base)
  local pid  = emu:read32(base)
  local key  = pid ~ emu:read32(base + 4)
  local mon = {
    nickname = decodeText(base + 8, 10),
    level    = emu:read8(base + 0x54),
    hp       = emu:read16(base + 0x56),
    maxHP    = emu:read16(base + 0x58),
    attack   = emu:read16(base + 0x5A),
    defense  = emu:read16(base + 0x5C),
    speed    = emu:read16(base + 0x5E),
    spAtk    = emu:read16(base + 0x60),
    spDef    = emu:read16(base + 0x62),
    status   = emu:read32(base + 0x50),
    species  = substructU16(base, pid, 0, 0, key),
    heldItem = substructU16(base, pid, 0, 2, key),
    moves    = {},
  }
  for i = 0, 3 do mon.moves[i + 1] = substructU16(base, pid, 1, i * 2, key) end
  return mon
end

-- The game only refreshes the stored enemy counter when it feels like it,
-- so count filled slots the same way CalculateEnemyPartyCount does.
local function countFilled(partyAddr)
  local n = 0
  while n < PARTY_SIZE and readMon(partyAddr + n * MON_SIZE).species ~= 0 do
    n = n + 1
  end
  return n
end

local function moveList(moves)
  local out = {}
  for _, id in ipairs(moves) do
    if id ~= 0 then out[#out + 1] = moveName(id) end
  end
  return table.concat(out, ", ")
end

local function dump(label, partyAddr, countAddr)
  local stored = emu:read8(countAddr)
  local n = countFilled(partyAddr)
  console:log(string.format("%s: %d mon (stored counter says %d)", label, n, stored))
  for i = 0, n - 1 do
    local mon = readMon(partyAddr + i * MON_SIZE)
    console:log(string.format("  %d. %s (%s #%d)  Lv%d  HP %d/%d  %s",
      i + 1, mon.nickname, speciesName(mon.species), mon.species,
      mon.level, mon.hp, mon.maxHP, statusText(mon.status)))
    console:log("     moves: " .. moveList(mon.moves))
  end
end

function report()
  dump("player party", PLAYER_PARTY, PLAYER_COUNT)
end

function enemy()
  dump("enemy party", ENEMY_PARTY, ENEMY_COUNT)
end

-- Live state of the Pokemon actually on the field.
function battle()
  local flags = emu:read32(BATTLE_TYPE)
  if flags == 0 then
    console:log("not in a battle (gBattleTypeFlags is 0)")
    return
  end
  local n = emu:read8(BATTLERS_COUNT)
  console:log(string.format("battle: flags 0x%08X, %d battlers", flags, n))
  for i = 0, n - 1 do
    local b = BATTLE_MONS + i * BATTLE_MON_SIZE
    local species = emu:read16(b)
    local side = (i % 2 == 0) and "yours" or "opponent"
    local t1, t2 = emu:read8(b + 0x21), emu:read8(b + 0x22)
    local types = typeName(t1)
    if t2 ~= t1 then types = types .. "/" .. typeName(t2) end
    console:log(string.format("  battler %d (%s): %s  Lv%d  HP %d/%d  %s  %s  ability %s",
      i, side, speciesName(species), emu:read8(b + 0x2A),
      emu:read16(b + 0x28), emu:read16(b + 0x2C),
      types, statusText(emu:read32(b + 0x4C)), abilityName(emu:read8(b + 0x20))))
    local moves = {}
    for m = 0, 3 do
      local id = emu:read16(b + 0x0C + m * 2)
      if id ~= 0 then
        moves[#moves + 1] = string.format("%s (%d pp)", moveName(id), emu:read8(b + 0x24 + m))
      end
    end
    console:log("     moves: " .. table.concat(moves, ", "))
  end
end

report()
