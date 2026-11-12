--[[---------------------------------------------------------------------------
    WoWHermes / MapPin.lua — turn coordinates in a Hermes reply into a user map
    waypoint (WS-V).

    Two entry points:
      * a transcript hyperlink `addon:WoWHermes:pin:<x>:<y>` (Format.lua inserts
        it around coordinate pairs), handled by Display's OnHyperlinkClick;
      * the window's "Pin" button, which walks the coordinate pairs of the newest
        finished reply so a controller/mouse player can pin without hovering.

    Every client API is feature-detected (type checks) and pcall'ed: a new client
    build that drops or refuses one of them must print a plain message and leave
    the addon alive. Failures are routed through Core.reportError, the same path
    every other handler uses, so they reach the bridge error queue.

    Forever evidence (wow-ui-source-forever @ 1.60.1.70862):
      * C_Map.GetBestMapForUnit(unitToken) -> uiMapID (nilable)
        (Blizzard_APIDocumentationGenerated/MapDocumentation.lua:49-61)
      * C_Map.CanSetUserWaypointOnMap(uiMapID) -> canSet (MapDocumentation.lua:11-24)
      * C_Map.SetUserWaypoint(point) -> wasSet (MapDocumentation.lua:599-612)
      * C_Map.GetMapInfo(uiMapID) -> UiMapDetails, MayReturnNothing (MapDocumentation.lua:312-326)
      * C_SuperTrack.SetSuperTrackedUserWaypoint(superTracked)
        (SuperTrackManagerDocumentation.lua:169-177)
      * UiMapPoint.CreateFromCoordinates(mapID, x, y, z) — x/y are 0..1 FRACTIONS,
        not percent (Blizzard_ObjectAPI/Mainline/UiMapPoint.lua:3-7).
      * All of the above are `SecretArguments = "AllowedWhenUntainted"`: plain
        numbers this addon computes itself are fine.

    Nothing here is protected and nothing registers with a gamepad manager
    (GamepadMode.FrameControlsManager is forbidden on Forever; see Display.lua).
--------------------------------------------------------------------------------]]--

-- C_Map / C_SuperTrack / UiMapPoint are real Forever globals (.luacheckrc lists
-- them); the bundled LuaLS annotations lag, so the undefined-global noise is
-- suppressed here instead of in every call site.
---@diagnostic disable: undefined-global

local _, ns = ... -- luacheck: ignore 212

local MapPin = {}
ns.MapPin = MapPin

-- Same target Format.PinLink writes.
MapPin.LINK = "addon:WoWHermes:pin:"

local function say(msg)
    if ns.Print then ns.Print(msg) else print(msg) end
end

-- Route a failure through the addon's error path (Core.lua reportError ->
-- Core.errors + chat + Outbox.ReportError for the bridge), then tell the player.
local function fail(message, err)
    say(message)
    local core = ns.Core
    if core and type(core.reportError) == "function" and err ~= nil then
        pcall(core.reportError, "MapPin", tostring(err))
    end
    return false
end

local function coordText(v)
    if math.floor(v) == v then return ("%d"):format(v) end
    return ("%g"):format(v)
end
MapPin.CoordText = coordText

-- Map name for the "always name the map used" line; falls back to the id.
function MapPin.MapName(mapID)
    if type(C_Map) == "table" and type(C_Map.GetMapInfo) == "function" then
        local ok, info = pcall(C_Map.GetMapInfo, mapID)
        if ok and type(info) == "table" and type(info.name) == "string" and info.name ~= "" then
            return info.name
        end
    end
    return "map " .. tostring(mapID)
end

-- "addon:WoWHermes:pin:44.7:44.5" -> 44.7, 44.5 (numbers), else nil.
function MapPin.ParseLink(link)
    if type(link) ~= "string" then return nil end
    if link:sub(1, #MapPin.LINK) ~= MapPin.LINK then return nil end
    local x, y = link:sub(#MapPin.LINK + 1):match("^(%d+%.?%d*):(%d+%.?%d*)$")
    if not x or not y then return nil end
    local nx, ny = tonumber(x), tonumber(y)
    if not nx or not ny then return nil end
    return nx, ny
end

function MapPin.IsPinLink(link)
    return MapPin.ParseLink(link) ~= nil
end

-- A transcript link. Returns true when this module owned the link (so Display
-- does not fall through to the client's own link handler).
function MapPin.HandleLink(link)
    local x, y = MapPin.ParseLink(link)
    if not x then return false end
    MapPin.Set(x, y)
    return true
end

-- Set a user waypoint at percent coordinates (x, y both 0..100) on the map the
-- player is on. `suffix` is appended to the one success line (e.g. " (2 of 3)").
function MapPin.Set(x, y, suffix)
    x, y = tonumber(x), tonumber(y)
    if not x or not y or x < 0 or x > 100 or y < 0 or y > 100 then
        return fail("Map pin: that is not a 0-100 coordinate pair, so no pin was set.")
    end

    if type(C_Map) ~= "table" or type(C_Map.GetBestMapForUnit) ~= "function" then
        return fail("Map pin: this client has no C_Map.GetBestMapForUnit, so no pin was set.",
            "C_Map.GetBestMapForUnit is unavailable")
    end

    local ok, mapID = pcall(C_Map.GetBestMapForUnit, "player")
    if not ok then
        return fail("Map pin: the client refused to resolve your map, so no pin was set.", mapID)
    end
    if type(mapID) ~= "number" then
        return fail("Map pin: there is no map for your character right now, so no pin was set.")
    end

    -- The reply's coordinates may describe another zone; the pin always lands
    -- on the zone the player is standing in, and the line below names it.
    local name = MapPin.MapName(mapID)

    if type(C_Map.CanSetUserWaypointOnMap) == "function" then
        local cok, canSet = pcall(C_Map.CanSetUserWaypointOnMap, mapID)
        if not cok then
            return fail("Map pin: the client could not answer whether " .. name
                .. " accepts a waypoint, so no pin was set.", canSet)
        end
        if canSet ~= true then
            return fail(("Map pin: you can't set a waypoint on %s right now."):format(name))
        end
    end

    if type(UiMapPoint) ~= "table" or type(UiMapPoint.CreateFromCoordinates) ~= "function" then
        return fail("Map pin: this client has no UiMapPoint.CreateFromCoordinates, so no pin was set.",
            "UiMapPoint.CreateFromCoordinates is unavailable")
    end
    -- Percent -> 0..1 fractions (UiMapPoint.lua:3).
    local pok, point = pcall(UiMapPoint.CreateFromCoordinates, mapID, x / 100, y / 100)
    if not pok or type(point) ~= "table" then
        return fail("Map pin: the client refused those coordinates, so no pin was set.", point)
    end

    if type(C_Map.SetUserWaypoint) ~= "function" then
        return fail("Map pin: this client has no C_Map.SetUserWaypoint, so no pin was set.",
            "C_Map.SetUserWaypoint is unavailable")
    end
    local sok, wasSet = pcall(C_Map.SetUserWaypoint, point)
    if not sok then
        return fail("Map pin: the client refused the waypoint, so no pin was set.", wasSet)
    end
    if wasSet ~= true then
        return fail("Map pin: the client did not accept that waypoint; nothing was changed.")
    end

    -- Super-track only marks the pin on screen; the waypoint is already set, so
    -- a refusal here is reported but does not turn the pin into a failure.
    if type(C_SuperTrack) == "table" and type(C_SuperTrack.SetSuperTrackedUserWaypoint) == "function" then
        local tok, terr = pcall(C_SuperTrack.SetSuperTrackedUserWaypoint, true)
        if not tok then
            local core = ns.Core
            if core and type(core.reportError) == "function" then
                pcall(core.reportError, "MapPin:SuperTrack", tostring(terr))
            end
        end
    end

    say(("Pinned %s, %s on %s%s"):format(coordText(x), coordText(y), name, suffix or ""))
    return true
end

-- Coordinate pairs of the newest finished reply, in reading order.
function MapPin.LatestPairs()
    local notes = ns.Notes
    if not notes or type(notes.LatestReply) ~= "function" then return {}, nil end
    local entry = notes.LatestReply()
    if type(entry) ~= "table" or type(entry.text) ~= "string" then return {}, nil end
    local format = ns.Format
    if not format or type(format.PinPairs) ~= "function" then return {}, entry end
    return format.PinPairs(entry.text), entry
end

-- The Pin button: pin the first pair of the newest reply, then advance. The
-- cursor resets when the reply changes, so each press names its own position.
function MapPin.PinNext()
    local pairsList, entry = MapPin.LatestPairs()
    if #pairsList == 0 then
        MapPin.cursor, MapPin.cursorEntry = nil, nil
        say("No coordinates in the last reply")
        return false, "no-coordinates"
    end
    if entry ~= MapPin.cursorEntry then
        MapPin.cursorEntry = entry
        MapPin.cursor = 0
    end
    local index = ((MapPin.cursor or 0) % #pairsList) + 1
    MapPin.cursor = index
    local suffix = #pairsList > 1 and ((" (%d of %d)"):format(index, #pairsList)) or ""
    local pair = pairsList[index]
    return MapPin.Set(pair.x, pair.y, suffix)
end

-- `/h selftest` extension: the first screenshot answers whether the waypoint
-- APIs exist and work on the live client (open question Q1 in v1.3-assist.md).
function MapPin.SelftestLines()
    local cmap = type(C_Map) == "table" and C_Map or {}
    local track = type(C_SuperTrack) == "table" and C_SuperTrack or {}
    local point = type(UiMapPoint) == "table" and UiMapPoint or {}

    local lines = {
        ("mapPin: C_Map.SetUserWaypoint=%s C_Map.CanSetUserWaypointOnMap=%s"
            .. " C_SuperTrack.SetSuperTrackedUserWaypoint=%s UiMapPoint=%s"
            .. " CreateFromCoordinates=%s"):format(
            type(cmap.SetUserWaypoint), type(cmap.CanSetUserWaypointOnMap),
            type(track.SetSuperTrackedUserWaypoint), type(UiMapPoint),
            type(point.CreateFromCoordinates)),
    }

    local best, name, canSet = "?", "?", "?"
    if type(cmap.GetBestMapForUnit) == "function" then
        local ok, id = pcall(cmap.GetBestMapForUnit, "player")
        if ok and type(id) == "number" then
            best = tostring(id)
            name = MapPin.MapName(id)
            if type(cmap.CanSetUserWaypointOnMap) == "function" then
                local cok, value = pcall(cmap.CanSetUserWaypointOnMap, id)
                if cok then canSet = tostring(value) end
            end
        end
    end
    local links = (ns.Display and ns.Display.linksEnabled) and "yes" or "no"
    lines[2] = ("mapPin: bestMap=%s (%s) canSet=%s transcriptLinks=%s"):format(best, name, canSet, links)
    return lines
end
