-- One PoB startup per worker; candidate imports contain no inherited mechanics.
dofile(os.getenv("WITCHCRAFT_BRIDGE_HOME") .. "/HeadlessWrapper.lua")
if __mainObject__.promptMsg then error(__mainObject__.promptMsg) end
local json = require("dkjson")
local statKeys = {"Life", "EnergyShield", "Armour", "Evasion", "FullDPS", "FullDotDPS",
  "CombinedDPS", "TotalDPS", "TotalDotDPS", "IgniteDPS", "WithIgniteDPS",
  "FireResist", "ColdResist", "LightningResist", "ChaosResist", "Str", "Dex", "Int",
  "Omni", "ReqStr", "ReqDex", "ReqInt", "ReqOmni", "ExtraPoints", "Mana",
  "ManaUnreserved", "ManaUnreservedPercent", "LifeUnreserved", "LifeUnreservedPercent",
  "TotalEHP", "ManaCost", "ManaRegen", "Speed", "HitChance", "ActiveMinionLimit"}

local function stats(output)
  local result = {}
  for _, key in ipairs(statKeys) do
    local value = output[key]
    if type(value) == "number" and value == value and math.abs(value) < math.huge then result[key] = value end
  end
  return result
end

local function load(xml)
  loadBuildFromXML(assert(xml), "Witchcraft generated candidate")
  runCallback("OnFrame")
  assert(build.calcsTab, "PoB did not load candidate")
  build.calcsTab:BuildOutput()
end

local function calculation()
  local output = assert(build.calcsTab.mainOutput)
  local used, asc, secondary = build.spec:CountAllocNodes()
  local extra = output.ExtraPoints or 0
  return {calculated = true, stats = stats(output), version = launch.versionNumber,
    passives = {used = used, maximum = build.characterLevel - 1 + 23 + extra,
      requiredLevel = used + 1 - 23 - extra, ascendancy = asc - secondary, secondaryAscendancy = secondary}}
end

local function metadata()
  local gems, bases, mods = {}, {}, {}
  for id, gem in pairs(data.gems) do
    local levels = {}
    for level = 1, gem.naturalMaxLevel or 20 do
      local effect = gem.grantedEffect and gem.grantedEffect.levels[level]
      if effect then
        levels[#levels + 1] = {level = level, requiredLevel = effect.levelRequirement or 1,
          str = calcLib.getGemStatRequirement(effect.levelRequirement or 1, gem.grantedEffect.support, gem.reqStr or 0),
          dex = calcLib.getGemStatRequirement(effect.levelRequirement or 1, gem.grantedEffect.support, gem.reqDex or 0),
          int = calcLib.getGemStatRequirement(effect.levelRequirement or 1, gem.grantedEffect.support, gem.reqInt or 0)}
      end
    end
    gems[#gems + 1] = {id = id, gameId = gem.gameId, variantId = gem.variantId,
      name = gem.name, skillId = gem.grantedEffectId, tags = gem.tags,
      support = gem.grantedEffect and gem.grantedEffect.support or false,
      unsupported = gem.grantedEffect and gem.grantedEffect.unsupported or false,
      weaponTypes = gem.grantedEffect and gem.grantedEffect.weaponTypes,
      maxLevel = gem.naturalMaxLevel or 20, levels = levels,
      vaal = gem.vaalGem or false, baseName = gem.baseTypeName}
  end
  for name, base in pairs(data.itemBases) do
    bases[name] = {type = base.type, subType = base.subType, tags = base.tags,
      req = {level = base.req and base.req.level or 1, str = base.req and base.req.str or 0,
        dex = base.req and base.req.dex or 0, int = base.req and base.req.int or 0},
      socketLimit = base.socketLimit, implicit = base.implicit}
  end
  for id, mod in pairs(data.itemMods.Explicit) do
    local lines = {}
    for _, line in ipairs(mod) do if type(line) == "string" then lines[#lines + 1] = line end end
    mods[#mods + 1] = {id = id, kind = mod.type, group = mod.group, level = mod.level,
      weightKey = mod.weightKey, weightVal = mod.weightVal, lines = lines}
  end
  return {gems = gems, bases = bases, mods = mods}
end

local function handle(request)
  assert(request.operation == "metadata" or request.operation == "calculate" or request.operation == "supports"
    or request.operation == "nodes" or request.operation == "supportScores" or request.operation == "export"
    or request.operation == "loadouts",
    "Unknown worker operation")
  if request.operation == "metadata" then return metadata() end
  load(request.xml)
  if request.operation == "loadouts" then
    build:SyncLoadouts()
    return {loadouts = build.controls.buildLoadouts.list}
  elseif request.operation == "export" then
    local result = calculation()
    result.xml = assert(build:SaveDB("Witchcraft export"), "PoB could not serialize the build")
    return result
  elseif request.operation == "supports" then
    local result = {}
    local skill = build.calcsTab.mainEnv.player.mainSkill
    for id, gem in pairs(data.gems) do
      local effect = gem.grantedEffect
      if effect and effect.support and not effect.isTrigger
        and calcLib.canGrantedEffectSupportActiveSkill(effect, skill) then
        result[#result + 1] = id
      end
    end
    return {supports = result}
  elseif request.operation == "nodes" then
    local calculator = build.calcsTab:GetMiscCalculator()
    local result = {}
    for _, candidate in ipairs(request.candidates) do
      local added = {}
      for _, id in ipairs(candidate.nodes) do
        local node = assert(build.spec.nodes[tonumber(id)], "Unknown passive node " .. id)
        added[node] = true
      end
      result[#result + 1] = {id = candidate.id, stats = stats(calculator({addNodes = added}, true))}
    end
    return {candidates = result}
  elseif request.operation == "supportScores" then
    local result = {}
    local group = build.skillsTab.socketGroupList[build.mainSocketGroup]
    local index = #group.gemList + 1
    for _, id in ipairs(request.candidates) do
      local gem = assert(data.gems[id])
      group.gemList[index] = {gemId = id, nameSpec = gem.name, level = math.min(20, gem.naturalMaxLevel or 20),
        quality = 0, enabled = true, enableGlobal1 = true, enableGlobal2 = false, count = 1}
      build.skillsTab:ProcessSocketGroup(group)
      build.calcsTab:BuildOutput()
      local skill = build.calcsTab.mainEnv.player.mainSkill
      local compatible = true
      for _, instance in ipairs(group.gemList) do
        if instance.gemData.grantedEffect.support and
          not calcLib.canGrantedEffectSupportActiveSkill(instance.gemData.grantedEffect, skill) then
          compatible = false
        end
      end
      if compatible then result[#result + 1] = {id = id, stats = stats(build.calcsTab.mainOutput)} end
    end
    group.gemList[index] = nil
    build.skillsTab:ProcessSocketGroup(group)
    return {candidates = result}
  end
  return calculation()
end

function witchcraftRequest()
  local input = assert(io.open(os.getenv("WITCHCRAFT_INPUT_XML"), "rb"))
  local request = assert(json.decode(input:read("*a")))
  input:close()
  local result = handle(request)
  local output = assert(io.open(os.getenv("WITCHCRAFT_OUTPUT_JSON"), "wb"))
  output:write(assert(json.encode(result)))
  output:close()
end
