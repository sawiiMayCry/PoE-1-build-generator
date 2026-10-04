-- One PoB startup per worker; candidate imports contain no inherited mechanics.
dofile(os.getenv("WITCHCRAFT_BRIDGE_HOME") .. "/HeadlessWrapper.lua")
if __mainObject__.promptMsg then error(__mainObject__.promptMsg) end
local json = require("dkjson")
local statKeys = {"Life", "EnergyShield", "Armour", "Evasion", "FullDPS", "FullDotDPS",
  "CombinedDPS", "TotalDPS", "TotalDotDPS", "IgniteDPS", "WithIgniteDPS",
  "FireResist", "ColdResist", "LightningResist", "ChaosResist", "Str", "Dex", "Int",
  "Omni", "ReqStr", "ReqDex", "ReqInt", "ReqOmni", "ExtraPoints", "Mana",
  "ManaUnreserved", "ManaUnreservedPercent", "LifeUnreserved", "LifeUnreservedPercent", "Duration",
  "TotalEHP", "ManaCost", "ManaRegen", "LifeCost", "LifeRegenRecovery",
  "Speed", "HitChance", "ActiveMinionLimit", "SummonedMinionsPerCast",
  -- Defensive, recovery and resource channels (missing outputs stay unknown, never zero).
  "PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken",
  "ChaosMaximumHitTaken", "BlockChance", "SpellBlockChance", "SpellSuppressionChance",
  "AttackDodgeChance", "SpellDodgeChance", "EnergyShieldRegen", "EnergyShieldRegenRecovery",
  "EnergyShieldRecharge", "EnergyShieldRechargeDelay", "LifeRegen", "ManaRegenRecovery",
  "LifeRecoverable", "LifeLeechRate", "ManaLeechRate", "EnergyShieldLeechRate",
  "TotalDegen", "TotalNetRegen", "NetLifeRegen", "NetManaRegen", "NetEnergyShieldRegen",
  "ManaReserved", "LifeReserved", "ManaCostRaw", "ManaPerSecondCost", "LifePerSecondCost",
  "ChaosInoculation", "PowerCharges", "PowerChargesMax", "FrenzyCharges", "FrenzyChargesMax",
  "EnduranceCharges", "EnduranceChargesMax", "ChaosResistOverCap", "FireResistOverCap",
  "ColdResistOverCap", "LightningResistOverCap", "AreaOfEffectRadius", "CritChance",
  "EffectiveMovementSpeedMod", "StunThreshold", "ProjectileCount"}

local function stats(output)
  local result = {}
  for _, key in ipairs(statKeys) do
    local value = output[key]
    if type(value) == "number" and value == value and math.abs(value) < math.huge then result[key] = value end
  end
  return result
end

local function load(xml, reset)
  -- Drop the previous build object before calculations; PoB can otherwise
  -- retain selected-spec state when requests arrive in a different order.
  if reset then newBuild() end
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
  local gems, bases, mods, jewelMods = {}, {}, {}, {}
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
    local skillTypes = {}
    local effectData = gem.grantedEffect
    if effectData and effectData.skillTypes then
      for typeName, typeId in pairs(SkillType) do
        if effectData.skillTypes[typeId] then skillTypes[#skillTypes + 1] = typeName end
      end
      table.sort(skillTypes)
    end
    local statIds = {}
    if effectData then
      for _, statId in ipairs(effectData.stats or {}) do statIds[#statIds + 1] = tostring(statId) end
      for _, constant in ipairs(effectData.constantStats or {}) do statIds[#statIds + 1] = tostring(constant[1]) end
    end
    gems[#gems + 1] = {id = id, skillTypes = skillTypes, statIds = statIds,
      legacy = (effectData and effectData.legacy) or false,
      createsMinions = (effectData and effectData.minionList ~= nil) or false,
      baseEffectiveness = effectData and effectData.baseEffectiveness or 0, gameId = gem.gameId, variantId = gem.variantId,
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
  for id, mod in pairs(data.itemMods.Jewel) do
    local lines = {}
    for _, line in ipairs(mod) do if type(line) == "string" then lines[#lines + 1] = line end end
    jewelMods[#jewelMods + 1] = {id = id, kind = mod.type, group = mod.group, level = mod.level,
      weightKey = mod.weightKey, weightVal = mod.weightVal, lines = lines}
  end
  return {gems = gems, bases = bases, mods = mods, jewelMods = jewelMods}
end

local function uniqueMetadata()
  local result = {}
  for category, list in pairsSortByKey(data.uniques) do
    if type(list) == "table" then
      for _, raw in ipairs(list) do
        if type(raw) == "string" then
          local item = new("Item", raw)
          if item.base then
            item:NormaliseVariantSelections()
            item:BuildAndParseRaw()
            if item.rarity == "UNIQUE" then
              result[#result + 1] = {name = item.title or item.name, base = item.baseName,
                type = item.base.type, subType = item.base.subType, raw = item:BuildRaw(),
                uniqueID = item.uniqueID, selectedVersion = item.selectedVersion,
                selectedVersionLabel = item.versionList and item.versionList[item.selectedVersion],
                versionList = item.versionList, selectedVariant = item.variant,
                selectedVariantLabel = item.variantList and item.variantList[item.variant],
                variantList = item.variantList, selectedVariantGroups = item.variantGroupSelections,
                requirements = item.requirements, sockets = item.sockets,
                classRestriction = item.classRestriction, foulborn = item.foulborn,
                unreleased = item.unreleased, category = category}
            end
          end
        end
      end
    end
  end
  table.sort(result, function(a, b)
    if a.name == b.name then return a.base < b.base end
    return a.name < b.name
  end)
  return {items = result}
end

local function treeMetadata()
  newBuild()
  local tree = assert(build.spec.tree)
  local result, effects = {}, {}
  for id, node in pairs(tree.nodes or {}) do
    if node.group then
      local links = {}
      for _, linked in ipairs(node.linkedId or {}) do links[#links + 1] = tostring(linked) end
      local masteryIds = {}
      for _, mastery in ipairs(node.masteryEffects or {}) do
        local effectId = type(mastery) == "table" and mastery.effect or mastery
        if effectId then
          masteryIds[#masteryIds + 1] = tonumber(effectId)
          if type(mastery) == "table" then
            effects[tostring(effectId)] = {name = mastery.name, stats = mastery.stats}
          end
        end
      end
      result[tostring(id)] = {name = node.name, links = links, masteryEffects = masteryIds,
        isJewelSocket = node.isJewelSocket or false, isMastery = node.type == "Mastery",
        isProxy = node.isProxy or false, isBloodline = node.isBloodline or false,
        ascendancyName = node.ascendancyName}
    end
  end
  return {nodes = result, masteryEffects = effects, version = build.spec.treeVersion}
end

local function handle(request)
  assert(request.operation == "metadata" or request.operation == "calculate" or request.operation == "supports"
    or request.operation == "nodes" or request.operation == "supportScores" or request.operation == "export"
    or request.operation == "loadouts" or request.operation == "uniques" or request.operation == "treeMetadata",
    "Unknown worker operation")
  if request.operation == "metadata" then return metadata() end
  if request.operation == "uniques" then return uniqueMetadata() end
  if request.operation == "treeMetadata" then return treeMetadata() end
  load(request.xml, request.operation ~= "loadouts")
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
        -- A support's requirements are matched against the gem's own skill types (as in game); PoB's
        -- extra match against a summoned minion's skill types would pair e.g. attack-only supports
        -- with spell summons.
        local allowed = true
        if skill.minionSkillTypes and not effect.ignoreMinionTypes and effect.requireSkillTypes
          and effect.requireSkillTypes[1] then
          allowed = calcLib.doesTypeExpressionMatch(effect.requireSkillTypes, skill.skillTypes)
        end
        if allowed then result[#result + 1] = id end
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
