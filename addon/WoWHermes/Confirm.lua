--[[---------------------------------------------------------------------------
    WoWHermes / Confirm.lua — the player's Accept/Cancel for bridge commands
    (PROTOCOL A.1 kind `c`, research 08 §8.2).

    The bridge never writes an addon without the player saying yes in the game.
    A command that needs confirmation is published in the slot state as

        confirm = { id = "c17", action = "addon.install", name = "WeakAuras 5.12", t, exp }

    and this file turns it into one addon-owned window (PromptFrame.lua). Accept
    emits a `c` record ``id=<command id>;ok=1`` and Cancel/close emits `ok=0`, each
    with a fresh outbound frame id; the bridge executes only on a matching record,
    from the same character and session, within 10 min.

    Fail closed: if the window cannot be built, or cannot be shown, the addon
    answers Cancel. Closing the window (including its title-bar close button) is
    always Cancel, never Accept.
--------------------------------------------------------------------------------]]-->

local _, ns = ... -- luacheck: ignore 212

local Confirm = {}
ns.Confirm = Confirm

Confirm.pending = nil      -- { id, action, name } waiting for the player
Confirm.shown = false
Confirm.frame = nil        -- the addon-owned window (PromptFrame.lua)
Confirm.lastNote = nil     -- dedupe for bridge notices (`note` slot key)

local MAX_ID = 32

local function say(msg)
    if ns.Print then ns.Print(msg) else print(msg) end
end

-- Player-facing action names. Unknown actions are shown verbatim (never hidden).
Confirm.ACTIONS = {
    ["addon.install"] = "Install addon",
    ["addon.uninstall"] = "Uninstall addon",
    ["app.curseforge.install"] = "Install the CurseForge app",
}

Confirm.TITLE = "Hermes needs your permission"
Confirm.BODY = "%s\n\nHermes is asking for your permission. Accept only if you asked for this."

local function cleanId(value)
    local id = tostring(value or ""):gsub("[^%w%-_]", "")
    if #id == 0 or #id > MAX_ID then return nil end
    return id
end

local function labelFor(pending)
    local verb = Confirm.ACTIONS[pending.action] or pending.action
    if pending.name ~= "" then return verb .. ": " .. pending.name end
    return verb
end

-- ------------------------------------------------------------------ window

function Confirm:Ensure()
    if self.frame then return self.frame end
    if not (ns.Prompt and ns.Prompt.Create) then return nil end

    local frame = ns.Prompt.Create({
        name = "WoWHermesConfirmFrame",
        title = self.TITLE,
        acceptText = "Accept",
        cancelText = "Cancel",
        onAccept = function() Confirm:Answer(true) end,
        onCancel = function() Confirm:Answer(false) end,
    })
    if frame then self.frame = frame end
    return frame
end

function Confirm:Show()
    if not self.pending then return end

    local frame = self:Ensure()
    if not frame then
        -- No window API (or a restricted client state): refuse rather than act silently.
        say("Hermes asked for a confirmation, but the confirmation window is unavailable; nothing was done.")
        self:Answer(false)
        return
    end

    local body = (self.BODY):format(labelFor(self.pending))
    if not (ns.Prompt.SetBody(frame, body) and ns.Prompt.Show(frame)) then
        say("Hermes asked for a confirmation, but the confirmation window could not open; nothing was done.")
        self:Answer(false)
        return
    end
    self.shown = true
end

function Confirm:Hide()
    self.pending = nil
    if self.shown then
        self.shown = false
        if self.frame and ns.Prompt then ns.Prompt.Hide(self.frame) end
    end
end

-- ------------------------------------------------------------------ inbound

-- Called from Inbox:Apply with the slot's `confirm` value (nil when nothing is pending).
function Confirm:Apply(cmd)
    if type(cmd) ~= "table" then
        -- The bridge no longer proposes anything: close a window for a superseded command.
        if self.pending then self:Hide() end
        return
    end
    local id = cleanId(cmd.id)
    if not id then return end
    if self.pending and self.pending.id == id then return end  -- already on screen
    self.pending = {
        id = id,
        action = tostring(cmd.action or ""),
        name = tostring(cmd.name or ""),
    }
    self:Show()
end

-- Bridge notices (`note` slot key): printed once per distinct text.
function Confirm:Note(text)
    if type(text) ~= "string" or text == "" then return end
    if text == self.lastNote then return end
    self.lastNote = text
    say(text)
end

-- ------------------------------------------------------------------ outbound

-- ok=true/false. Returns true when the `c` record was queued.
function Confirm:Answer(ok)
    local pending = self.pending
    self:Hide()
    if not pending then return false end

    local text = ("id=%s;ok=%d"):format(pending.id, ok and 1 or 0)
    local queued = false
    if ns.Outbox and ns.Outbox.SendConfirm then
        queued = ns.Outbox:SendConfirm(text)
    end
    if not queued then
        say("Could not send your answer to the bridge; nothing was installed.")
        return false
    end
    say(ok and "Confirmed — Hermes is working on it." or "Cancelled — nothing was changed.")
    return true
end
