"""Run a PoB Community installation's Lua calculation modules locally."""
from __future__ import annotations

import ctypes
import atexit
import json
import os
import tempfile
import threading
from pathlib import Path

ENGINE_LOCK = threading.RLock()
POB_ENV_KEYS = (
    "WITCHCRAFT_BRIDGE_HOME",
    "WITCHCRAFT_POB_HOME",
    "WITCHCRAFT_APP_HOME",
    "WITCHCRAFT_POB_PROFILE",
    "WITCHCRAFT_INPUT_XML",
    "WITCHCRAFT_OUTPUT_JSON",
)


def find_pob_installation() -> Path | None:
    candidates = []
    configured = os.environ.get("WITCHCRAFT_POB_HOME")
    if configured:
        candidates.append(Path(configured).expanduser())
    for variable in ("APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Path of Building Community")
    required = ("Launch.lua", "Modules/Calcs.lua", "Classes/CalcsTab.lua", "lua51.dll", "lua/dkjson.lua")
    for candidate in candidates:
        if candidate.is_dir() and all((candidate / item).is_file() for item in required):
            return candidate.resolve()
    return None


def engine_status(app_root: Path) -> dict:
    home = find_pob_installation()
    bridge = app_root / "pob_bridge"
    bridge_files = all((bridge / name).is_file() for name in ("HeadlessWrapper.lua", "_SimpleGraphic.def.lua", "Worker.lua"))
    if home and bridge_files:
        return {"detected": True, "label": "LOCAL POB FILES FOUND", "message": "PoB Community calculation files and the local headless bridge were found."}
    if not home:
        return {"detected": False, "label": "LOCAL POB NOT FOUND", "message": "Install Path of Building Community to enable local build calculations."}
    return {"detected": False, "label": "POB BRIDGE FILES MISSING", "message": "The app’s PoB headless support files are incomplete."}


def _cold_calculate_with_pob(xml: str, app_root: Path, data_root: Path) -> dict:
    """Load a clean build in PoB and return stats from its real calculation pass."""
    if os.name != "nt":
        raise RuntimeError("The detected PoB bridge currently supports Windows installations only.")
    home = find_pob_installation()
    bridge = app_root / "pob_bridge"
    if not home or not all((bridge / name).is_file() for name in ("HeadlessWrapper.lua", "_SimpleGraphic.def.lua")):
        raise RuntimeError("Path of Building Community’s local calculation engine is not available.")

    profile = data_root / "pob_profile"
    profile.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pob-check-", dir=data_root) as temp:
        temp_root = Path(temp)
        input_xml = temp_root / "candidate.xml"
        output_json = temp_root / "calculated.json"
        input_xml.write_text(xml, encoding="utf-8")

        script = r'''
local bridge = assert(os.getenv("WITCHCRAFT_BRIDGE_HOME"))
dofile(bridge .. "/HeadlessWrapper.lua")
if __mainObject__.promptMsg then error(__mainObject__.promptMsg) end
local file = assert(io.open(assert(os.getenv("WITCHCRAFT_INPUT_XML")), "rb"))
local xml = file:read("*a")
file:close()
loadBuildFromXML(xml, "Witchcraft local calculation")
runCallback("OnFrame")
if not build.calcsTab then error("PoB did not load the candidate build") end
build.calcsTab:BuildOutput()
local output = build.calcsTab.mainOutput
if type(output) ~= "table" then error("PoB did not return calculated build stats") end
local stats = {}
for _, key in ipairs({"Life", "EnergyShield", "Armour", "Evasion", "FullDPS", "FullDotDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS", "IgniteDPS", "WithIgniteDPS", "FireResist", "ColdResist", "LightningResist", "ChaosResist", "Str", "Dex", "Int", "Omni", "ReqStr", "ReqDex", "ReqInt", "ReqOmni"}) do
  if type(output[key]) == "number" then stats[key] = output[key] end
end
if next(stats) == nil then error("PoB returned an empty stat table") end
local encoder = require("dkjson")
local used, ascUsed, secondaryAscUsed = build.spec:CountAllocNodes()
local extra = output.ExtraPoints or 0
local passives = {used = used, maximum = build.characterLevel - 1 + 23 + extra,
  requiredLevel = used + 1 - 23 - extra, ascendancy = ascUsed - secondaryAscUsed,
  secondaryAscendancy = secondaryAscUsed}
local payload = assert(encoder.encode({calculated = true, stats = stats, passives = passives, version = launch.versionNumber}))
local result = assert(io.open(assert(os.getenv("WITCHCRAFT_OUTPUT_JSON")), "wb"))
result:write(payload)
result:close()
'''
        values = {
            "WITCHCRAFT_BRIDGE_HOME": str(bridge.resolve()).replace("\\", "/"),
            "WITCHCRAFT_POB_HOME": str(home).replace("\\", "/"),
            "WITCHCRAFT_APP_HOME": str(app_root.resolve()).replace("\\", "/"),
            "WITCHCRAFT_POB_PROFILE": str(profile.resolve()).replace("\\", "/"),
            "WITCHCRAFT_INPUT_XML": str(input_xml).replace("\\", "/"),
            "WITCHCRAFT_OUTPUT_JSON": str(output_json).replace("\\", "/"),
        }

        with ENGINE_LOCK:
            saved_env = {key: os.environ.get(key) for key in POB_ENV_KEYS}
            previous_cwd = os.getcwd()
            state = None
            try:
                os.environ.update(values)
                os.chdir(home)
                with os.add_dll_directory(str(home)):
                    lua = ctypes.WinDLL(str(home / "lua51.dll"))
                    lua.luaL_newstate.restype = ctypes.c_void_p
                    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
                    lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                    lua.luaL_loadstring.restype = ctypes.c_int
                    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
                    lua.lua_pcall.restype = ctypes.c_int
                    lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
                    lua.lua_tolstring.restype = ctypes.c_char_p
                    lua.lua_close.argtypes = [ctypes.c_void_p]

                    state = lua.luaL_newstate()
                    if not state:
                        raise RuntimeError("PoB’s Lua runtime could not create a calculation session.")
                    lua.luaL_openlibs(state)
                    status = lua.luaL_loadstring(state, script.encode("utf-8"))
                    if status == 0:
                        status = lua.lua_pcall(state, 0, 0, 0)
                    if status != 0:
                        detail = lua.lua_tolstring(state, -1, None)
                        message = detail.decode("utf-8", errors="replace") if detail else "unknown Lua error"
                        raise RuntimeError(f"Path of Building could not calculate this PoB: {message[:500]}")
                if not output_json.is_file():
                    raise RuntimeError("Path of Building finished without returning calculated stats.")
                result = json.loads(output_json.read_text(encoding="utf-8"))
                if not result.get("calculated"):
                    raise RuntimeError("Path of Building did not confirm a successful calculation.")
                return result
            except (AttributeError, OSError) as exc:
                raise RuntimeError(f"Could not start PoB’s bundled Lua runtime: {exc}") from exc
            finally:
                if state:
                    try:
                        lua.lua_close(state)
                    except Exception:
                        pass
                os.chdir(previous_cwd)
                for key, value in saved_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class PobWorker:
    """A serialized, reusable Lua session. Each XML import resets build state.

    PoB uses process-global environment variables and a working directory, so
    every operation (including close) shares ENGINE_LOCK with cold calculations.
    """

    def __init__(self, home: Path, app_root: Path, data_root: Path):
        self.home = home.resolve()
        self.app_root = app_root.resolve()
        self.data_root = data_root.resolve()
        self.state = None
        self.lua = None
        self.calls = 0

    def _execute(self, script: str):
        status = self.lua.luaL_loadstring(self.state, script.encode("utf-8"))
        if status == 0:
            status = self.lua.lua_pcall(self.state, 0, 0, 0)
        if status:
            detail = self.lua.lua_tolstring(self.state, -1, None)
            message = detail.decode("utf-8", errors="replace") if detail else "unknown Lua error"
            self.lua.lua_settop(self.state, 0)
            raise RuntimeError(f"Path of Building calculation failed: {message[:800]}")
        self.lua.lua_settop(self.state, 0)

    def close(self):
        with ENGINE_LOCK:
            if self.state:
                self.lua.lua_close(self.state)
                self.state = None

    def request(self, operation: str, **payload) -> dict:
        if os.name != "nt":
            raise RuntimeError("The PoB bridge currently supports Windows installations only.")
        self.data_root.mkdir(parents=True, exist_ok=True)
        profile = self.data_root / "pob_profile"
        profile.mkdir(parents=True, exist_ok=True)
        with ENGINE_LOCK, tempfile.TemporaryDirectory(prefix="pob-worker-", dir=self.data_root) as temp:
            request_path = Path(temp) / "request.json"
            output_path = Path(temp) / "result.json"
            request_path.write_text(json.dumps({"operation": operation, **payload}), encoding="utf-8")
            values = {
                "WITCHCRAFT_BRIDGE_HOME": str(self.app_root / "pob_bridge").replace("\\", "/"),
                "WITCHCRAFT_POB_HOME": str(self.home).replace("\\", "/"),
                "WITCHCRAFT_APP_HOME": str(self.app_root).replace("\\", "/"),
                "WITCHCRAFT_POB_PROFILE": str(profile).replace("\\", "/"),
                "WITCHCRAFT_INPUT_XML": str(request_path).replace("\\", "/"),
                "WITCHCRAFT_OUTPUT_JSON": str(output_path).replace("\\", "/"),
            }
            saved = {key: os.environ.get(key) for key in POB_ENV_KEYS}
            cwd = os.getcwd()
            try:
                os.environ.update(values)
                os.chdir(self.home)
                with os.add_dll_directory(str(self.home)):
                    if self.state is None:
                        self.lua = ctypes.WinDLL(str(self.home / "lua51.dll"))
                        self.lua.luaL_newstate.restype = ctypes.c_void_p
                        for name in ("luaL_openlibs", "lua_close"):
                            getattr(self.lua, name).argtypes = [ctypes.c_void_p]
                        self.lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                        self.lua.luaL_loadstring.restype = ctypes.c_int
                        self.lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
                        self.lua.lua_pcall.restype = ctypes.c_int
                        self.lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
                        self.lua.lua_tolstring.restype = ctypes.c_char_p
                        self.lua.lua_settop.argtypes = [ctypes.c_void_p, ctypes.c_int]
                        self.state = self.lua.luaL_newstate()
                        if not self.state:
                            raise RuntimeError("PoB's Lua runtime could not create a session")
                        self.lua.luaL_openlibs(self.state)
                        self._execute('dofile(os.getenv("WITCHCRAFT_BRIDGE_HOME") .. "/Worker.lua")')
                    self._execute("witchcraftRequest()")
                result = json.loads(output_path.read_text(encoding="utf-8"))
                if result.get("error"):
                    raise RuntimeError(result["error"])
                self.calls += 1
                return result
            except Exception:
                # A failed load must never contaminate the next request.
                self.close()
                raise
            finally:
                os.chdir(cwd)
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


_worker: PobWorker | None = None


def get_worker(app_root: Path, data_root: Path) -> PobWorker:
    global _worker
    home = find_pob_installation()
    if home is None:
        raise RuntimeError("Path of Building Community's local engine is not available")
    key = (home.resolve(), Path(app_root).resolve(), Path(data_root).resolve())
    with ENGINE_LOCK:
        if _worker is None or (_worker.home, _worker.app_root, _worker.data_root) != key:
            if _worker:
                _worker.close()
            _worker = PobWorker(*key)
        return _worker


def close_worker():
    global _worker
    with ENGINE_LOCK:
        if _worker:
            _worker.close()
            _worker = None


atexit.register(close_worker)


def calculate_with_pob(xml: str, app_root: Path, data_root: Path) -> dict:
    """Import a clean candidate into the persistent real PoB calculation engine."""
    return get_worker(app_root, data_root).request("calculate", xml=xml)


def export_with_pob(xml: str, app_root: Path, data_root: Path) -> dict:
    """Serialize through PoB and verify that reimport preserves its calculation.

    Minimal candidate XML is sufficient for calculations, but sharing requires
    PoB's saved PlayerStat/MinionStat data and normal export metadata.
    """
    worker = get_worker(app_root, data_root)
    exported = worker.request("export", xml=xml)
    reimported = worker.request("calculate", xml=exported["xml"])
    if reimported != {key: value for key, value in exported.items() if key != "xml"}:
        raise RuntimeError("PoB export changed the calculated build; sharing stopped")
    return {**reimported, "xml": exported["xml"]}
