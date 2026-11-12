--[[---------------------------------------------------------------------------
    WoWHermes / ReloadPrompt.lua — post-install UI reload reminder (PROTOCOL B.2
    slot key `reload`).

    The bridge installs an addon only after the player pressed Accept. When that
    install succeeded it publishes, for ~60 s,

        reload = { name = "Questie", ts = <epoch float>, exp = <epoch float>,
                   enable = { "Questie", "QuestieDB" } }   -- optional

    and this file first calls C_AddOns.EnableAddOn for each `enable` entry (installed
    folder first, then its present required dependencies) so the addon is not left
    disabled, then turns it into one addon-owned window (PromptFrame.lua): the addon
    name, a short 10-second countdown, and the manual instruction

        Type /reload to load the addon. If it does not appear afterward,
        restart WoW once.

    A successful **uninstall** uses the same slot key and window, with the same shape
    plus

        disable = { "Questie" }                       -- optional

    Here the addon calls C_AddOns.DisableAddOn for each entry (Forever:
    AllowedWhenUntainted, the call Blizzard's own AddOns list makes) before the
    reminder, and the wording is a removal: \"Questie was removed. Type /reload ...\".

    Forever forbids an addon from reloading the UI itself: the client answers an
    addon-initiated reload with ADDON_ACTION_BLOCKED and follows up with more
    blocked protected calls. So nothing here reloads, ever. The countdown only
    changes the wording (\"Type /reload in N seconds\" -> \"Type /reload now\"), and
    **Got it** / **Cancel** both just stop the timer and dismiss the window.

      * in combat, or when the window cannot be built -> one chat line instead
      * `/h reload` opens the same reminder with the generic name \"Your addon\"
      * the same bridge prompt (`ts`) is shown once, also across /reload
--------------------------------------------------------------------------------]]-->

local _, ns = ... -- luacheck: ignore 212

local ReloadPrompt = {}
ns.ReloadPrompt = ReloadPrompt

ReloadPrompt.SECONDS = 10
ReloadPrompt.GENERIC = "Your addon"

ReloadPrompt.name = nil
ReloadPrompt.ts = nil          -- slot `reload.ts` of the prompt on screen (nil for /h reload)
ReloadPrompt.remaining = 0
ReloadPrompt.countdownDone = false
ReloadPrompt.ticker = nil
ReloadPrompt.frame = nil       -- the addon-owned window (PromptFrame.lua)
ReloadPrompt.shown = false
ReloadPrompt.removing = false  -- this prompt is for a successful uninstall
ReloadPrompt.enableResult = nil -- { enabled=, total=, names= } for the current prompt
ReloadPrompt.disableResult = nil -- { disabled=, total=, names= } for a removal

ReloadPrompt.TITLE = "Hermes installed an addon"
ReloadPrompt.REMOVE_TITLE = "Hermes removed an addon"

-- The countdown ends in a manual instruction, never in an automatic reload.
ReloadPrompt.COUNTING = "%s installed. Type /reload in %d seconds to load the addon. "
    .. "If it does not appear afterward, restart WoW once."
ReloadPrompt.READY = "%s installed. Type /reload now to load the addon. "
    .. "If it does not appear afterward, restart WoW once."
ReloadPrompt.MANUAL = "%s installed. Type /reload to load the addon. "
    .. "If it does not appear afterward, restart WoW once."
ReloadPrompt.COMBAT = "%s installed. The UI can only be reloaded by you: type /reload when you "
    .. "are out of combat. If the addon does not appear afterward, restart WoW once."

-- Removal wording: nothing was added, so the player must not read "installed" or "load".
ReloadPrompt.REMOVED_COUNTING = "%s was removed. Type /reload in %d seconds to finish removing it."
ReloadPrompt.REMOVED_READY = "%s was removed. Type /reload now to finish removing it."
ReloadPrompt.REMOVED_MANUAL = "%s was removed. Type /reload to finish removing it. "
    .. "If it still appears in the AddOns list afterward, restart WoW once."
ReloadPrompt.REMOVED_COMBAT = "%s was removed. The UI can only be reloaded by you: type /reload "
    .. "when you are out of combat. If it still appears in the AddOns list afterward, restart WoW once."

-- Result of C_AddOns.EnableAddOn for the folders the bridge installed, reported in
-- chat before the reminder. The player must know whether /reload will really load it.
ReloadPrompt.ENABLED = "%d enabled."
ReloadPrompt.ENABLE_SOME = "%d enabled. Could not enable %d yet; enable it in the AddOns list after /reload."
ReloadPrompt.ENABLE_FAILED = "Could not enable %d yet; enable it in the AddOns list after /reload."

-- Result of C_AddOns.DisableAddOn for a removed folder. Non-fatal: the folder is already
-- gone on disk, so a refusal only means the stale entry may linger until a restart.
ReloadPrompt.DISABLED = "%d disabled."
ReloadPrompt.DISABLE_SOME = "%d disabled. Could not disable %d yet; the removed addon may stay "
    .. "listed until you restart WoW."
ReloadPrompt.DISABLE_FAILED = "Could not disable %d yet; the removed addon may stay listed "
    .. "until you restart WoW."

local MAX_NAME = 120
-- The bridge caps `enable` / `disable` well below this; the addon never trusts an unbounded slot.
ReloadPrompt.ENABLE_MAX = 16

local function say(msg)
    if ns.Print then ns.Print(msg) else print(msg) end
end

local function cleanName(value)
    local name = tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", "")
    if name == "" then return ReloadPrompt.GENERIC end
    if #name > MAX_NAME then name = name:sub(1, MAX_NAME) end
    return name
end

-- `reload.enable`: folder names only, in the bridge's order, deduped, bounded.
-- Anything that is not a plausible addon name is dropped rather than passed to the API.
local function cleanEnable(value)
    if type(value) ~= "table" then return nil end
    local names, seen = {}, {}
    for _, entry in ipairs(value) do
        if type(entry) == "string" then
            local name = entry:gsub("^%s+", ""):gsub("%s+$", "")
            local key = name:lower()
            if name ~= "" and #name <= MAX_NAME and not seen[key] then
                seen[key] = true
                names[#names + 1] = name
                if #names >= ReloadPrompt.ENABLE_MAX then break end
            end
        end
    end
    if #names == 0 then return nil end
    return names
end

-- ------------------------------------------------------------------ state

local function restricted()
    if type(InCombatLockdown) == "function" then
        local ok, combat = pcall(InCombatLockdown)
        if ok and combat == true then return true end
    end
    return false
end

local function nowEpoch()
    if type(time) == "function" then
        local ok, epoch = pcall(time)
        if ok and type(epoch) == "number" then return epoch end
    end
    return nil
end

-- Dedupe: the same bridge prompt (same `ts`) is shown once, even across /reload —
-- the client re-reads Inbox.lua on login and the bridge republishes while fresh.
local function seen(ts)
    local db = WoWHermesDB
    local last = type(db) == "table" and tonumber(db.reloadSeen) or nil
    return last ~= nil and ts ~= nil and ts <= last
end

local function markSeen(ts)
    if ts == nil then return end
    if type(WoWHermesDB) ~= "table" then return end
    local last = tonumber(WoWHermesDB.reloadSeen)
    if last == nil or ts > last then WoWHermesDB.reloadSeen = ts end
end

-- ------------------------------------------------------------------ enable

-- Ask the client to enable the folders the bridge just installed, in the bridge's
-- order. Forever documents C_AddOns.EnableAddOn as AllowedWhenUntainted, and this is
-- what Blizzard's own AddOns list uses; it is NOT a protected call and NOT a WTF
-- write, so nothing here can taint the client or reload the UI.
--
-- A folder the client has not discovered yet (first install of a brand-new addon)
-- refuses or errors. That is expected and non-fatal: the reminder still goes out and
-- the AddOns list is the manual fallback once the UI has been reloaded.
-- Returns (enabled, total); (0, 0) when the API is unavailable, i.e. nothing to say.
function ReloadPrompt.Enable(names)
    if type(names) ~= "table" or #names == 0 then return 0, 0 end
    local api = C_AddOns
    local enable = type(api) == "table" and api.EnableAddOn
    if type(enable) ~= "function" then return 0, 0 end

    local enabled = 0
    for _, name in ipairs(names) do
        local ok, result = pcall(enable, name)
        -- EnableAddOn may return nothing on success; only an explicit false (or an
        -- error caught by pcall) counts as "not enabled yet".
        if ok and result ~= false then enabled = enabled + 1 end
    end
    return enabled, #names
end

function ReloadPrompt:EnableMessage(enabled, total)
    if not total or total <= 0 then return nil end
    if enabled >= total then return (self.ENABLED):format(enabled) end
    if enabled > 0 then return (self.ENABLE_SOME):format(enabled, total - enabled) end
    return (self.ENABLE_FAILED):format(total)
end

-- Ask the client to disable the folder the bridge just removed. Forever documents
-- C_AddOns.DisableAddOn as AllowedWhenUntainted (AddOnsDocumentation.lua) and Blizzard's
-- own AddOns list toggles removal through it; it is not a protected call and writes no WTF
-- file. A folder the client already forgot returns false — expected and non-fatal.
function ReloadPrompt.Disable(names)
    if type(names) ~= "table" or #names == 0 then return 0, 0 end
    local api = C_AddOns
    local disable = type(api) == "table" and api.DisableAddOn
    if type(disable) ~= "function" then return 0, 0 end

    local disabled = 0
    for _, name in ipairs(names) do
        local ok, result = pcall(disable, name)
        if ok and result ~= false then disabled = disabled + 1 end
    end
    return disabled, #names
end

function ReloadPrompt:DisableMessage(disabled, total)
    if not total or total <= 0 then return nil end
    if disabled >= total then return (self.DISABLED):format(disabled) end
    if disabled > 0 then return (self.DISABLE_SOME):format(disabled, total - disabled) end
    return (self.DISABLE_FAILED):format(total)
end

-- ------------------------------------------------------------------ window

function ReloadPrompt:Title()
    if self.removing then return self.REMOVE_TITLE end
    return self.TITLE
end

function ReloadPrompt:Ensure()
    if self.frame then return self.frame end
    if not (ns.Prompt and ns.Prompt.Create) then return nil end

    local frame = ns.Prompt.Create({
        name = "WoWHermesReloadFrame",
        title = self:Title(),
        acceptText = "Got it",
        cancelText = "Cancel",
        onAccept = function() ReloadPrompt:Acknowledge() end,
        onCancel = function() ReloadPrompt:Dismiss() end,
    })
    if frame then self.frame = frame end
    return frame
end

function ReloadPrompt:Text()
    local name = self.name or self.GENERIC
    local seconds = math.max(0, math.floor(self.remaining or 0))
    if self.removing then
        if seconds > 0 then return (self.REMOVED_COUNTING):format(name, seconds) end
        if self.countdownDone then return (self.REMOVED_READY):format(name) end
        return (self.REMOVED_MANUAL):format(name)
    end
    if seconds > 0 then return (self.COUNTING):format(name, seconds) end
    if self.countdownDone then return (self.READY):format(name) end
    return (self.MANUAL):format(name)
end

function ReloadPrompt:Render()
    if not self.frame or not ns.Prompt then return end
    ns.Prompt.SetBody(self.frame, self:Text())
end

function ReloadPrompt:StopTicker()
    local ticker = self.ticker
    self.ticker = nil
    if ticker and ticker.Cancel then pcall(ticker.Cancel, ticker) end
end

function ReloadPrompt:Hide()
    self:StopTicker()
    self.shown = false
    self.remaining = 0
    self.countdownDone = false
    if self.frame and ns.Prompt then ns.Prompt.Hide(self.frame) end
end

-- ------------------------------------------------------------------ inbound

-- Start (or restart) the reminder for `name`. `ts` is the bridge's slot `reload.ts`
-- when the prompt came from the bridge; nil for `/h reload`. `enable` / `disable` are the
-- bridge's `reload.enable` / `reload.disable` lists: those folders are enabled or disabled
-- BEFORE the reminder, so the /reload the player is told to type really does load a new
-- addon — or forget a removed one.
function ReloadPrompt:Start(name, ts, enable, disable)
    self:Hide()
    self.name = cleanName(name)
    self.ts = tonumber(ts)

    -- Non-fatal by design: a failure here never blocks the reminder, never writes
    -- WTF/AddOns.txt and never reloads anything. It is reported in chat instead.
    local disableNames = cleanEnable(disable)
    -- Presence of the key (even an unusable list) marks a removal, so the wording never
    -- claims an addon was installed when one was taken away.
    self.removing = type(disable) == "table"
    if self.removing then
        self.enableResult = nil
        local disabled, total = self.Disable(disableNames)
        self.disableResult = { disabled = disabled, total = total, names = disableNames }
        local note = self:DisableMessage(disabled, total)
        if note then say(note) end
    else
        self.disableResult = nil
        local names = cleanEnable(enable)
        local enabled, total = self.Enable(names)
        self.enableResult = { enabled = enabled, total = total, names = names }
        local note = self:EnableMessage(enabled, total)
        if note then say(note) end
    end

    local manual = self.removing and self.REMOVED_MANUAL or self.MANUAL
    if restricted() then
        if self.removing then
            say((self.REMOVED_COMBAT):format(self.name))
        else
            say((self.COMBAT):format(self.name))
        end
        return false
    end

    local frame = self:Ensure()
    if not frame then
        say((manual):format(self.name))
        return false
    end
    -- The frame is shared between install and removal prompts; keep its title honest.
    if frame.SetTitle then pcall(frame.SetTitle, frame, self:Title()) end
    -- Accept/Cancel for the command that produced this install is over; do not
    -- leave two addon windows open.
    if ns.Confirm and ns.Confirm.shown and ns.Confirm.Hide then ns.Confirm:Hide() end

    self.remaining = self.SECONDS
    self.countdownDone = false
    self:Render()
    if not ns.Prompt.Show(frame) then
        self.remaining = 0
        say((manual):format(self.name))
        return false
    end
    self.shown = true
    self.ticker = C_Timer.NewTicker(1, function() ReloadPrompt:Tick() end)
    return true
end

-- Called from Inbox:Apply with the slot's `reload` value (nil when nothing is pending).
function ReloadPrompt:Apply(value)
    if type(value) ~= "table" then return end
    local ts = tonumber(value.ts)
    if ts == nil then return end
    if seen(ts) then return end
    local exp = tonumber(value.exp)
    local now = nowEpoch()
    if exp and now and exp < now then return end   -- stale publish, e.g. after a long /reload
    -- Remember the prompt as soon as it is handled: the bridge republishes it every
    -- publish for 60 s, and a refused (combat) prompt must not repeat either.
    markSeen(ts)
    self:Start(value.name, ts, value.enable, value.disable)
end

-- ------------------------------------------------------------------ countdown

function ReloadPrompt:Tick()
    if not self.shown then return end
    if restricted() then
        local name = self.name
        self:Hide()
        markSeen(self.ts)
        say((self.COMBAT):format(name or self.GENERIC))
        return
    end
    self.remaining = (self.remaining or 0) - 1
    if self.remaining <= 0 then
        -- The countdown never reloads: it stops and points at the manual command.
        self.remaining = 0
        self.countdownDone = true
        self:StopTicker()
    end
    self:Render()
end

-- "Got it": the player has read the instruction. No reload, no protected call.
function ReloadPrompt:Acknowledge()
    local name = self.name or self.GENERIC
    local ts = self.ts
    local removing = self.removing
    self:Hide()
    markSeen(ts)
    if removing then
        say("Reload the UI yourself with /reload when you are ready — " .. name .. " has been removed.")
    else
        say("Reload the UI yourself with /reload when you are ready — " .. name .. " is installed.")
    end
end

-- "Cancel": stop the reminder. Nothing reloads either way.
function ReloadPrompt:Dismiss()
    local ts = self.ts
    self:Hide()
    markSeen(ts)
    say("Reload reminder dismissed. Type /reload when ready.")
end
