#@
---@diagnostic disable: lowercase-global
-- This wrapper allows the program to run headless on any OS (in theory)
-- It can be run using a standard lua interpreter, although LuaJIT is preferable

-- define global SimpleGraphic API functions. some of these have dummy function
-- bodies intended for headless use.
local bridgeHome = assert(os.getenv("WITCHCRAFT_BRIDGE_HOME"), "Witchcraft bridge path is missing")
local pobHome = assert(os.getenv("WITCHCRAFT_POB_HOME"), "Path of Building installation path is missing")
dofile(bridgeHome .. "/_SimpleGraphic.def.lua")

-- Keep headless sessions away from a user's saved PoB builds and settings.
GetScriptPath = function() return pobHome end
GetRuntimePath = function() return pobHome end
GetUserPath = function() return assert(os.getenv("WITCHCRAFT_POB_PROFILE")) end
arg = arg or {}

local pobLuaPath = pobHome:gsub("\\", "/")
package.path = pobLuaPath .. "/?.lua;" .. pobLuaPath .. "/?/init.lua;" .. pobLuaPath .. "/lua/?.lua;" .. pobLuaPath .. "/lua/?/init.lua;" .. package.path
package.cpath = pobLuaPath .. "/?.dll;" .. pobLuaPath .. "/lua/?.dll;" .. package.cpath

function GetVirtualScreenSize()
	return 1920, 1080
end

-- Callbacks
__callbackTable__ = { }

function runCallback(name, ...)
	if __callbackTable__[name] then
		return __callbackTable__[name](...)
	elseif __mainObject__ and __mainObject__[name] then
		return __mainObject__[name](__mainObject__, ...)
	end
end

local l_require = require
function require(name)
	-- Hack to stop it looking for lcurl, which we don't really need
	if name == "lcurl.safe" then
		return
	end
	return l_require(name)
end


dofile(pobHome .. "/Launch.lua")

-- Prevents loading of ModCache
-- Allows running mod parsing related tests without pushing ModCache
-- The CI env var will be true when run from github workflows but should be false for other tools using the headless wrapper 
__mainObject__.continuousIntegrationMode = os.getenv("CI")

runCallback("OnInit")
runCallback("OnFrame") -- Need at least one frame for everything to initialise

if __mainObject__.promptMsg then
	-- Something went wrong during startup
	print(__mainObject__.promptMsg)
	return
end

-- The build module; once a build is loaded, you can find all the good stuff in here
build = __mainObject__.main.modes["BUILD"]

-- Here's some helpful helper functions to help you get started
function newBuild()
	__mainObject__.main:SetMode("BUILD", false, "Help, I'm stuck in Path of Building!")
	runCallback("OnFrame")
end
function loadBuildFromXML(xmlText, name)
	__mainObject__.main:SetMode("BUILD", false, name or "", xmlText)
	runCallback("OnFrame")
end
function loadBuildFromJSON(characterJSON)
	__mainObject__.main:SetMode("BUILD", false, "")
	runCallback("OnFrame")
	-- characterJSON could, for example, be the response from the PoE API:
	-- https://www.pathofexile.com/developer/docs/reference#characters-get
	local dkjson = require "dkjson"
	local input = dkjson.decode(characterJSON)
	local charData = build.importTab:ImportItemsAndSkills(input)
	build.importTab:ImportPassiveTreeAndJewels(input)
	-- You now have a build without a correct main skill selected, or any configuration options set
	-- Good luck!
end
