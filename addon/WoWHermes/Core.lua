--[[---------------------------------------------------------------------------
    WoWHermes / Core.lua — event wiring, SavedVariables, session, slash commands,
    error capture. Loaded last so every module exists.

    Startup order (Forever):
      file scope        : create the event frame only
      ADDON_LOADED      : WoWHermesDB defaults, session token, character name,
                          history, window, settings category
      PLAYER_LOGIN      : doorbell probe, slot count, hello, inbox poll loop
      PLAYER_LOGOUT     : bye record

    Restrictions: nothing here touches a protected frame or a secret value; the
    addon never reads chat (PROTOCOL E).
--------------------------------------------------------------------------------]]

local addonName, ns = ...

local Strip = ns.Strip
local Bridge = ns.Bridge
local Inbox = ns.Inbox
local Outbox = ns.Outbox
local History = ns.History
local Display = ns.Display
local Options = ns.Options

local Core = {}
ns.Core = Core

Core.initialized = false
Core.errors = {}

local DEFAULTS = {
    transport = "strip",
    sendContext = true,
    autoOpen = true,      -- open the in-game window on /h <text> and when a reply lands
    gamepadNav = false,   -- legacy flag; unsafe FrameControlsManager integration is removed
    echoReplies = true,   -- also print each finished reply in the chat frame
    stripDebug = false,
    opacity = 0.9,
    fadeOnMove = true,    -- dim the window to 25% while the character is moving
    minimapButton = true, -- round button on the Minimap edge (Forever has no AddonCompartment)
    minimapAngle = 200,   -- degrees, 0 = top of the minimap, clockwise
    chatTab = true,       -- dedicated "Hermes" chat tab next to General / Combat Log
}

-- ------------------------------------------------------------------ output

function ns.Print(msg)
    print("|cff33ff99WoWHermes|r: " .. tostring(msg))
end

-- ------------------------------------------------------------------ helpers

local function isSecret(value)
    if type(issecretvalue) ~= "function" then return false end
    local ok, secret = pcall(issecretvalue, value)
    return ok and secret == true
end

local function characterName()
    local name, realm
    if type(UnitName) == "function" then
        local ok, n, r = pcall(UnitName, "player")
        if ok and not isSecret(n) then name, realm = n, r end
    end
    if type(name) ~= "string" or name == "" then return nil end
    if type(realm) ~= "string" or realm == "" then
        if type(GetRealmName) == "function" then
            local ok, r = pcall(GetRealmName)
            if ok and not isSecret(r) then realm = r end
        end
    end
    if type(realm) ~= "string" or realm == "" then return name end
    return name .. "-" .. realm:gsub("%s+", "")
end

local function mintSession()
    -- math.randomseed is nil in the Forever client (in-game round 1: "Core.lua:72: attempt to
    -- call a nil value"); the client's math.random is already seeded. Prefer fastrandom.
    local rand = (type(fastrandom) == "function" and fastrandom) or math.random -- luacheck: ignore 113
    if type(math.randomseed) == "function" then
        local seed = (type(time) == "function" and time() or 0) + math.floor((GetTime() or 0) * 1000)
        pcall(math.randomseed, seed)
    end
    local out = {}
    for i = 1, 8 do out[i] = ("%x"):format(rand(0, 15)) end
    return table.concat(out)
end

local function initDB()
    if type(WoWHermesDB) ~= "table" then WoWHermesDB = {} end
    local d = WoWHermesDB

    d.v = 1
    if type(d.nextId) ~= "number" or d.nextId < 1 or d.nextId > 65535 then d.nextId = 1 end

    d.settings = d.settings or {}
    local s = d.settings
    if s.transport ~= "strip" and s.transport ~= "reload" then s.transport = DEFAULTS.transport end
    if s.sendContext == nil then s.sendContext = DEFAULTS.sendContext end
    if s.autoOpen == nil then s.autoOpen = DEFAULTS.autoOpen end
    if s.gamepadNav == nil then s.gamepadNav = DEFAULTS.gamepadNav end
    if s.echoReplies == nil then s.echoReplies = DEFAULTS.echoReplies end
    if s.stripDebug == nil then s.stripDebug = DEFAULTS.stripDebug end
    if type(s.opacity) ~= "number" then s.opacity = DEFAULTS.opacity end
    if s.fadeOnMove == nil then s.fadeOnMove = DEFAULTS.fadeOnMove end
    if s.minimapButton == nil then s.minimapButton = DEFAULTS.minimapButton end
    if type(s.minimapAngle) ~= "number" then s.minimapAngle = DEFAULTS.minimapAngle end
    if s.chatTab == nil then s.chatTab = DEFAULTS.chatTab end

    d.outbox = d.outbox or {}
    d.chats = d.chats or {}
    d.chats.c0 = d.chats.c0 or { history = {} }
    d.chats.c0.history = d.chats.c0.history or {}

    if type(d.session) ~= "string" or not d.session:match("^%x%x%x%x%x%x%x%x$") then
        d.session = mintSession()
    end

    local char = characterName()
    if char then d.char = char end

    return d
end

-- ------------------------------------------------------------------ errors

local function reportError(where, err)
    local text = tostring(err or "unknown error")
    Core.errors[#Core.errors + 1] = where .. ": " .. text
    if #Core.errors > 10 then table.remove(Core.errors, 1) end
    ns.Print("internal error in " .. where .. " (reported to the bridge): " .. text)
    if Outbox and Outbox.ReportError and not Core.reporting then
        Core.reporting = true
        pcall(Outbox.ReportError, Outbox, where .. ": " .. text)
        Core.reporting = false
    end
end

-- Wrap one of our own code paths so a bug cannot break the client's UI.
-- Lua 5.1 xpcall takes no extra arguments, so they are captured explicitly.
local function guarded(where, fn)
    return function(...)
        local args = { ... }
        local count = select("#", ...)
        local ok, err = xpcall(function()
            return fn(unpack(args, 1, count))
        end, function(e)
            local trace = ""
            if type(debug) == "table" and type(debug.traceback) == "function" then
                trace = "\n" .. tostring(debug.traceback("", 2))
            end
            return tostring(e) .. trace
        end)
        if not ok then reportError(where, err) end
    end
end

-- ------------------------------------------------------------------ status

function Core.StatusText()
    local d = WoWHermesDB or {}
    local now = GetTime()
    local lastSeen = Bridge.lastSeen or 0
    local age = lastSeen > 0 and string.format("%.0fs ago", now - lastSeen) or "never"
    return table.concat({
        "transport=" .. tostring(d.settings and d.settings.transport),
        "bridge=" .. tostring(Bridge:Refresh(now)),
        "doorbell=" .. tostring(Bridge.mode),
        "slots=" .. tostring(Bridge.slotsTotal) .. " (left " .. tostring(Inbox:SlotsLeft()) .. ")",
        "seq=" .. tostring(Bridge.lastSeq),
        "ack=" .. tostring(Bridge.ack),
        "pending=" .. tostring(Inbox:PendingCount()),
        "queue=" .. tostring(#Outbox.queue) .. (Outbox.inflight and "+1" or ""),
        "session=" .. tostring(d.session),
        "char=" .. tostring(d.char),
        "lastPublish=" .. age,
    }, "  ")
end

-- ------------------------------------------------------------------ selftest

local function buildInfo()
    if type(GetBuildInfo) ~= "function" then return nil end
    local ok, version, build, _, interface = pcall(GetBuildInfo)
    if not ok then return nil end
    return version, build, interface
end

-- One-screen dev report for `/h selftest`; the coordinator /dumps this in game.
-- Every value is read fresh so it reflects UI scale / display changes.
function Core.SelfTestLines()
    local lines = {}

    local version, build, interface = buildInfo()
    lines[#lines + 1] = ("build=%s.%s interface=%s"):format(
        tostring(version), tostring(build), tostring(interface))

    local project = (type(WOW_PROJECT_ID) == "number") and WOW_PROJECT_ID or "?"
    local camelot = (type(WOW_PROJECT_CAMELOT) == "number") and WOW_PROJECT_CAMELOT or nil
    lines[#lines + 1] = ("project=%s%s"):format(
        tostring(project), (camelot and project == camelot) and " (camelot)" or "")

    local physical = "?"
    if type(GetPhysicalScreenSize) == "function" then
        local ok, w, h = pcall(GetPhysicalScreenSize)
        if ok then physical = ("%dx%d"):format(w, h) end
    end
    lines[#lines + 1] = ("physical=%s uiScale=%s"):format(
        physical,
        tostring(UIParent and UIParent.GetEffectiveScale and UIParent:GetEffectiveScale()))

    lines[#lines + 1] = ("doorbell=%s PlaySoundFile=%s"):format(
        tostring(Bridge.mode), type(PlaySoundFile) == "function" and "yes" or "absent")

    local loadedSlots = 0
    for _ in pairs(Inbox.attempted) do loadedSlots = loadedSlots + 1 end
    lines[#lines + 1] = ("slots=%d (left %d, loaded %d)"):format(
        Bridge.slotsTotal, Inbox:SlotsLeft(), loadedSlots)

    local scale = Strip:GetScale()
    local eff = scale
    local frame = Strip.frame
    if frame and frame.GetEffectiveScale then
        local ok, s = pcall(frame.GetEffectiveScale, frame)
        if ok and type(s) == "number" then eff = s end
    end
    local rows = math.max(1, Strip.rows or 1)
    lines[#lines + 1] = ("strip: scale=%.4f effective=%.4f px/unit=%.4f size=%dx%d shown=%s"):format(
        scale, eff, Strip:PixelsPerUnit(), Strip.WIDTH, rows * 4,
        tostring(Strip:IsShown()))

    local lockdown = "?"
    if type(C_ChatInfo) == "table" and type(C_ChatInfo.InChatMessagingLockdown) == "function" then
        local ok, v = pcall(C_ChatInfo.InChatMessagingLockdown)
        if ok then lockdown = tostring(v) end
    end
    lines[#lines + 1] = ("lockdown=%s transport=%s pending=%d queue=%d%s"):format(
        tostring(lockdown), tostring(WoWHermesDB and WoWHermesDB.settings
            and WoWHermesDB.settings.transport),
        Inbox:PendingCount(), #Outbox.queue, Outbox.inflight and "+1" or "")

    return lines
end

-- ------------------------------------------------------------------ slash

local function handleCommand(msg)
    local text = tostring(msg or ""):gsub("^%s+", ""):gsub("%s+$", "")
    local lower = text:lower()

    if text == "" then
        Display:Toggle()
        return
    end
    if lower == "status" then
        ns.Print(Core.StatusText())
        return
    end
    if lower == "selftest" then
        -- dev aid: one screen of client/transport/geometry facts for /dump
        local lines = Core.SelfTestLines()
        for i = 1, #lines do ns.Print(lines[i]) end
        return
    end
    if lower == "reload" then
        -- Same manual reminder as after an install, with the generic name. Forever
        -- forbids an addon-initiated UI reload, so this only tells the player what
        -- to type; it never reloads on its own.
        if ns.ReloadPrompt then
            ns.ReloadPrompt:Start(nil, nil)
        else
            ns.Print("Type /reload to reload the UI.")
        end
        return
    end
    if lower == "probe" then
        Strip:Probe()
        C_Timer.After(5, function()
            if not Outbox.inflight then Strip:Hide() end
        end)
        ns.Print("test pattern drawn on the strip for 5 seconds.")
        return
    end
    if lower == "hello" then
        Outbox:SubmitHello()
        ns.Print("hello queued.")
        return
    end
    if lower == "clear" then
        Display:ClearLocal()
        return
    end
    if lower == "new" then
        Display:NewSession()
        return
    end
    if lower == "notes" or lower == "note" then
        ns.NotesFrame:Show()
        return
    end
    if lower == "copy" or lower == "copy all" or lower == "copy full" then
        local mode = (lower == "copy") and ns.CopyFrame.MODE_LATEST or ns.CopyFrame.MODE_FULL
        ns.CopyFrame:Show(mode)
        return
    end
    if lower == "settings" or lower == "options" then
        Options:Open()
        return
    end

    local ok, reason = Outbox:SubmitMessage(text)
    if not ok then
        if reason == "oversize" then
            ns.Print("message too long for one frame (max 3200 bytes) — it was not sent.")
        elseif reason == "busy" then
            ns.Print("outbox is full; wait for the current message to be acknowledged.")
        end
        return
    end
    if WoWHermesDB.settings.autoOpen then Display:Show() else Display:Refresh() end
end

SLASH_WOWHERMES1 = "/hermes"
SLASH_WOWHERMES2 = "/h"
SlashCmdList["WOWHERMES"] = guarded("slash", handleCommand)

-- ------------------------------------------------------------------ events

local frame = CreateFrame("Frame", "WoWHermesEventFrame", UIParent)

local function onAddonLoaded(name)
    if name ~= addonName then
        -- a slot addon? hand it to the inbox.
        Inbox:OnAddonLoaded(name)
        return
    end
    if Core.initialized then return end
    initDB()
    History.Init()
    guarded("Notes:Init", function() ns.Notes:Init() end)()
    -- UI steps are isolated: a UI failure on a new client build must never take the transport
    -- down with it (in-game round 2: a SetFont error in Display:Init blocked every later step).
    guarded("Display:Init", function() Display:Init() end)()
    guarded("NotesFrame:Init", function() ns.NotesFrame:Init() end)()
    guarded("CopyFrame:Init", function() ns.CopyFrame:Init() end)()
    guarded("Options:Init", function() Options:Init() end)()
    guarded("MinimapButton:Init", function() ns.MinimapButton:Init() end)()
    guarded("Strip:SetDebugBorder", function() Strip:SetDebugBorder(WoWHermesDB.settings.stripDebug) end)()
    Core.initialized = true
end

function Core.ReportBlocked(event, culprit, fn)
    if culprit ~= addonName then return end
    local msg = ("%s: %s was blocked (please report this)"):format(event, tostring(fn))
    ns.Print(msg)
    Core.errors[#Core.errors + 1] = msg
    if Outbox and Outbox.ReportError then pcall(Outbox.ReportError, Outbox, msg) end
end

local handlers = {
    ADDON_LOADED = function(name)
        onAddonLoaded(name)
    end,

    PLAYER_LOGIN = function()
        if not Core.initialized then return end
        -- Inbox.lua (TOC-listed file the bridge rewrites) may have left the last
        -- published state in the global while our files loaded: apply it once.
        local seeded = _G.WoWHermes_Slot
        if type(seeded) == "table" then
            _G.WoWHermes_Slot = nil
            Inbox:Apply(seeded)
        end
        Bridge.Probe()
        Bridge.ScanSlots()
        Display:RefreshStatus()
        Outbox:SubmitHello()
        Inbox:Start()
        C_Timer.NewTicker(60, guarded("hello", function()
            local lastSeen = Bridge.lastSeen or 0
            if lastSeen == 0 or (GetTime() - lastSeen) > 60 then
                Outbox:SubmitHello()
            end
        end))
        ns.Print("ready. /h <text> sends, /h opens the window, /h status shows the link.")
    end,

    PLAYER_ENTERING_WORLD = function()
        if not Core.initialized then return end
        Display:RefreshStatus()
        -- Quest snapshots are rebuilt lazily; a new world invalidates the cache.
        if ns.Quest then ns.Quest.MarkDirty() end
        -- The chat tab is created on the first visit to the world, not at
        -- ADDON_LOADED: chat windows are still being restored from the client's
        -- chat cache during load, so creating one there risks a duplicate.
        guarded("ChatTab:OnEnteringWorld", function() ns.ChatTab:OnEnteringWorld() end)()
        guarded("MinimapButton:Apply", function() ns.MinimapButton:Apply() end)()
    end,

    PLAYER_LOGOUT = function()
        Outbox:SubmitBye()
    end,

    -- Quest snapshot invalidation (Forever: QuestLogDocumentation.lua:1373 / :1420).
    QUEST_LOG_UPDATE = function()
        if ns.Quest then ns.Quest.OnEvent() end
    end,
    QUEST_WATCH_UPDATE = function()
        if ns.Quest then ns.Quest.OnEvent() end
    end,

    -- Synchronous movement events (UnitDocumentation.lua:3946 / :3964): while the
    -- player moves the Hermes window fades to 25% and comes back on stop.
    PLAYER_STARTED_MOVING = function()
        if ns.Display then ns.Display:OnMovementEvent("PLAYER_STARTED_MOVING") end
    end,
    PLAYER_STOPPED_MOVING = function()
        if ns.Display then ns.Display:OnMovementEvent("PLAYER_STOPPED_MOVING") end
    end,

    -- Name the protected function when the client blocks us (in-game round 4 showed the popup
    -- with no detail). Payload per RestrictedActionsDocumentation: isTainted (addon name), function.
    ADDON_ACTION_BLOCKED = function(culprit, fn)
        Core.ReportBlocked("ADDON_ACTION_BLOCKED", culprit, fn)
    end,
    ADDON_ACTION_FORBIDDEN = function(culprit, fn)
        Core.ReportBlocked("ADDON_ACTION_FORBIDDEN", culprit, fn)
    end,
}

frame:SetScript("OnEvent", guarded("OnEvent", function(_, event, ...)
    local handler = handlers[event]
    if handler then handler(...) end
end))

for event in pairs(handlers) do frame:RegisterEvent(event) end

-- ------------------------------------------------------------------ compartment

function WoWHermes_OnCompartmentClick()
    Display:Toggle()
end

function WoWHermes_OnCompartmentEnter(_, button)
    if not GameTooltip then return end
    GameTooltip:SetOwner(button, "ANCHOR_RIGHT")
    GameTooltip:SetText("WoW Hermes")
    GameTooltip:AddLine("Left-click: open or close the Hermes window.", 1, 1, 1)
    GameTooltip:AddLine("/h <text> sends a message.", 1, 1, 1)
    GameTooltip:AddLine("Also on the minimap edge and in the 'Hermes' chat tab.", 1, 1, 1)
    GameTooltip:Show()
end

function WoWHermes_OnCompartmentLeave()
    if GameTooltip then GameTooltip:Hide() end
end

Core.initDB = initDB
Core.reportError = reportError
Core.frame = frame
