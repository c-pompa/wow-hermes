--[[---------------------------------------------------------------------------
    WoWHermes / ChatTab.lua — a dedicated "Hermes" chat tab.

    The player asked for a separate chat tab next to General and Combat Log
    instead of hunting for the addon window. We use Blizzard's own chat-window
    API, verified in the Forever source:

      * FCF_OpenNewWindow(name, noDefaultChannels)  — Blizzard_ChatFrameBase/
        Mainline/FloatingChatFrame.lua:706 (a [Family] file: loads for camelot).
        It names the window (FCF_SetWindowName), clears it, removes the standard
        message groups/channels and docks it into GENERAL_CHAT_DOCK.
      * FCF_DockFrame(frame, index, selected)       — same file, line 1626.
      * FCF_GetChatWindowInfo(id) / FCF_GetChatFrameByID(id) — line 448 / 1069.
      * Constants.ChatFrameConstants.MaxChatWindows = 10 — ChatConstantsDocumentation.lua:210.
      * frame:RemoveAllMessageGroups() / frame:RemoveAllChannels() —
        Shared/ChatFrame.lua:130 / 192 (ChatFrameMixin).

    We never hook the chat edit box or the send path: Enter on the tab is still
    Blizzard's, so there is no taint and no way for the addon to read chat
    (PROTOCOL E). The tab only ever receives lines the addon itself writes.

    Lifecycle: created once, on the first PLAYER_ENTERING_WORLD that sees
    settings.chatTab ~= false. If the player later closes the tab we treat that
    as a decision: chatTab is switched off and nothing is recreated.
--------------------------------------------------------------------------------]]--

local _, ns = ...

local ChatTab = {}
ns.ChatTab = ChatTab

ChatTab.NAME = "Hermes"
ChatTab.frame = nil
ChatTab.index = nil

local COLOR_USER = { 0.62, 0.85, 1.00 }
local COLOR_HERMES = { 0.55, 1.00, 0.65 }
local COLOR_ERROR = { 1.00, 0.40, 0.40 }
local COLOR_HINT = { 0.60, 0.60, 0.60 }

local HINT = "Type /h <message> to talk to Hermes, or open the Hermes window " ..
    "(minimap button)."

local function settings()
    local d = WoWHermesDB
    return d and d.settings
end

-- The chat-window API is legacy FrameXML: FCF_* and DEFAULT_CHAT_FRAME are not
-- in the generated API docs, so they are fetched by name and capability-checked.
-- A client that lacks them degrades to "no tab" instead of erroring.
local function globalFn(name)
    local value = rawget(_G, name)
    if type(value) == "function" then return value end
    return nil
end

local function globalValue(name)
    return rawget(_G, name)
end

local function stamp()
    local ok, text = pcall(date, "%H:%M")
    return (ok and type(text) == "string") and text or ""
end

-- Constants.ChatFrameConstants.MaxChatWindows (10) is the documented cap.
-- NUM_CHAT_WINDOWS is the legacy global; both are optional.
function ChatTab.MaxWindows()
    local c = globalValue("Constants")
    if type(c) == "table" and type(c.ChatFrameConstants) == "table" then
        local max = c.ChatFrameConstants.MaxChatWindows
        if type(max) == "number" and max > 0 then return max end
    end
    local legacy = globalValue("NUM_CHAT_WINDOWS")
    if type(legacy) == "number" and legacy > 0 then return legacy end
    return 10
end

function ChatTab.FrameByID(id)
    local byID = globalFn("FCF_GetChatFrameByID")
    if byID then
        local ok, frame = pcall(byID, id)
        if ok and frame then return frame end
    end
    local frame = rawget(_G, "ChatFrame" .. tostring(id))
    return frame or nil
end

-- Window name as the client stores it; falls back to the tab's text.
function ChatTab.NameForID(id)
    local info = globalFn("FCF_GetChatWindowInfo")
    if info then
        local ok, name = pcall(info, id)
        if ok and type(name) == "string" and name ~= "" then return name end
    end
    local tab = rawget(_G, "ChatFrame" .. tostring(id) .. "Tab")
    if tab and type(tab.GetText) == "function" then
        local ok, text = pcall(tab.GetText, tab)
        if ok and type(text) == "string" and text ~= "" then return text end
    end
    return nil
end

-- Reuse by name: whichever chat window currently carries it.
function ChatTab:Find()
    local max = self.MaxWindows()
    for id = 1, max do
        if self.NameForID(id) == self.NAME then
            local frame = self.FrameByID(id)
            if frame then return frame, id end
        end
    end
    return nil
end

-- A brand-new window: no message groups, no channels, nothing but our lines.
function ChatTab.Strip(frame)
    if not frame then return end
    for _, method in ipairs({ "RemoveAllMessageGroups", "RemoveAllChannels" }) do
        local fn = frame[method]
        if type(fn) == "function" then pcall(fn, frame) end
    end
end

-- Append one line. r,g,b optional; returns true when the tab took it.
function ChatTab:Add(text, r, g, b)
    local frame = self.frame
    if not frame or type(frame.AddMessage) ~= "function" then return false end
    local prefix = stamp()
    local line = (prefix ~= "") and ("[" .. prefix .. "] " .. tostring(text)) or tostring(text)
    local ok = pcall(frame.AddMessage, frame, line, r or 1, g or 1, b or 1)
    if not ok then return false end
    -- Bring the tab into view without stealing the selected tab: Blizzard's
    -- chat frame scrolls itself on AddMessage.
    return true
end

function ChatTab:Hint(frame)
    local target = frame or self.frame
    if not target or type(target.AddMessage) ~= "function" then return end
    pcall(target.AddMessage, target, HINT, COLOR_HINT[1], COLOR_HINT[2], COLOR_HINT[3])
end

-- One-line plain rendering: Markdown-lite markers are removed, literal pipes are
-- escaped, and there is no animation here (chat lines must not tick).
local function plainText(text)
    local fmt = ns.Format
    if fmt and fmt.Plain then return fmt.Plain(text) end
    return tostring(text or "")
end

function ChatTab:PlayerLine(text)
    return self:Add(plainText(text), COLOR_USER[1], COLOR_USER[2], COLOR_USER[3])
end

function ChatTab:ReplyLine(text, isError)
    local line = plainText(text)
    if isError then
        return self:Add(line, COLOR_ERROR[1], COLOR_ERROR[2], COLOR_ERROR[3])
    end
    return self:Add(line, COLOR_HERMES[1], COLOR_HERMES[2], COLOR_HERMES[3])
end

-- Create the window through Blizzard's API. Returns frame, index or nil.
function ChatTab:Create()
    local open = globalFn("FCF_OpenNewWindow")
    if not open then return nil end
    -- noDefaultChannels = true: no SAY/YELL/GUILD/WHISPER/PARTY/CHANNEL groups.
    local ok, frame, index = pcall(open, self.NAME, true)
    if not ok or not frame then return nil end
    if not index and type(frame.GetID) == "function" then index = frame:GetID() end
    self.Strip(frame)
    -- FCF_OpenNewWindow selects the new tab; hand the selection back so the
    -- player's chat keeps opening on the tab they had before.
    local back = globalValue("DEFAULT_CHAT_FRAME")
    local selectFrame = globalFn("FCF_SelectDockFrame")
    if back and selectFrame and back ~= frame then
        pcall(selectFrame, back)
    end
    self.frame, self.index = frame, index
    self:Hint(frame)
    self.announcedThisSession = true
    return frame, index
end

-- Called from Core on every PLAYER_ENTERING_WORLD.
function ChatTab:OnEnteringWorld()
    local s = settings()
    if type(s) ~= "table" then return nil end

    if s.chatTab == false then
        self.frame = nil
        return nil
    end

    local frame, index = self:Find()
    if frame then
        self.frame, self.index = frame, index
        -- A window restored from the chat cache keeps whatever Blizzard saved;
        -- an old build (or the player) may have added groups to it.
        self.Strip(frame)
        if not self.announcedThisSession then
            self:Hint(frame)
            self.announcedThisSession = true
        end
        return frame
    end

    if s.chatTabCreated then
        -- It existed once and is gone now: the player closed the tab. Respect
        -- that and stop offering it (the settings toggle can bring it back).
        s.chatTab = false
        s.chatTabCreated = nil
        s.chatTabIndex = nil
        self.frame = nil
        return nil
    end

    frame, index = self:Create()
    if frame then
        s.chatTabCreated = true
        s.chatTabIndex = index
        ns.Print("Hermes chat tab created (Settings > WoW Hermes to turn it off).")
    end
    return frame
end

-- Re-read the setting (used by the options checkbox).
function ChatTab:Apply()
    local s = settings()
    if type(s) ~= "table" then return end
    if s.chatTab == false then
        self.frame = nil
        return
    end
    if not self.frame then self:OnEnteringWorld() end
end

ChatTab.COLORS = { user = COLOR_USER, hermes = COLOR_HERMES, error = COLOR_ERROR }
