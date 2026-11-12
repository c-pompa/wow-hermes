--[[---------------------------------------------------------------------------
    WoWHermes / Inbox.lua — inbound records through LoadOnDemand slot addons
    (PROTOCOL B.1-B.4), plus the doorbell primitives shared with the outbox.

    Inbound chain:
      bridge writes WoWHermes_Sxxx/Slot.lua -> addon calls
      C_AddOns.LoadAddOn("WoWHermes_Sxxx") -> ADDON_LOADED -> read the global
      WoWHermes_Slot -> set it back to nil -> apply replies / restore bundle.

    Doorbells (B.3) exist only if the client can play a .wav by path:
      ctl\absent.wav must be falsy, ctl\valid.wav truthy -> mode "A", else "0".
--------------------------------------------------------------------------------]]

local _, ns = ...

local History = ns.History

local function say(msg)
    if ns.Print then ns.Print(msg) else print(msg) end
end

-- ------------------------------------------------------------------ bridge

local Bridge = {}
ns.Bridge = Bridge

Bridge.ROOT = "Interface\\AddOns\\WoWHermes\\"
Bridge.mode = "0"
Bridge.slotsTotal = 0
Bridge.slotIndexes = {}
Bridge.lastSeq = 0
Bridge.lastSeen = 0
Bridge.boot = 0          -- bridge process incarnation (newer boot resets seq)
Bridge.ack = 0
Bridge.char = nil
Bridge.publishedBridge = "starting"
Bridge.FRESH_SECONDS = 45

function Bridge.AckPath(id)
    return Bridge.ROOT .. "ack\\" .. ("%03d"):format(((id - 1) % 256) + 1) .. ".wav"
end

function Bridge.SigPath(id)
    return Bridge.ROOT .. "sig\\" .. ("%03d"):format(((id - 1) % 256) + 1) .. ".wav"
end

function Bridge.CtlPath(name)
    return Bridge.ROOT .. "ctl\\" .. name .. ".wav"
end

-- Existence signalling: a file that plays exists. Silence the sample at once.
function Bridge.Exists(path)
    if type(PlaySoundFile) ~= "function" then return false end
    local ok, handle = pcall(PlaySoundFile, path)
    if not ok or not handle then return false end
    if type(StopSound) == "function" then pcall(StopSound, handle) end
    return true
end

-- Doorbells are opt-in (settings.doorbells). In-game round 4 (Forever 1.60.1.70862, Windows):
-- the ack doorbell never fired although the bridge created ack\NNN.wav. Most likely the client
-- caches a negative PlaySoundFile lookup, so polling a name before the bridge creates it
-- poisons that name for the session. Slots (LoadAddOn reads fresh) carry acks and replies.
function Bridge.DoorbellsEnabled()
    local s = WoWHermesDB and WoWHermesDB.settings
    return s ~= nil and s.doorbells == true
end

function Bridge.UseDoorbells()
    return Bridge.mode == "A" and Bridge.DoorbellsEnabled()
end

-- PROTOCOL B.3 probe. Returns "A" or "0" and caches it in Bridge.mode.
function Bridge.Probe()
    if type(PlaySoundFile) ~= "function" then
        Bridge.mode = "0"
        return Bridge.mode
    end
    local absent = Bridge.Exists(Bridge.CtlPath("absent"))
    local valid = Bridge.Exists(Bridge.CtlPath("valid"))
    Bridge.mode = (not absent and valid) and "A" or "0"
    return Bridge.mode
end

-- How many slot addons does the client actually see? (hello field `p`)
function Bridge.ScanSlots()
    local indexes = {}
    if C_AddOns and C_AddOns.GetNumAddOns and C_AddOns.GetAddOnInfo then
        local ok, count = pcall(C_AddOns.GetNumAddOns)
        if ok and type(count) == "number" then
            for i = 1, count do
                local okName, name = pcall(C_AddOns.GetAddOnInfo, i)
                if okName and type(name) == "string" then
                    local idx = name:match("^WoWHermes_S(%d%d%d)$")
                    idx = idx and tonumber(idx)
                    if idx and idx >= 1 and idx <= 256 then indexes[#indexes + 1] = idx end
                end
            end
        end
    end
    table.sort(indexes)
    Bridge.slotIndexes = indexes
    Bridge.slotsTotal = #indexes
    return Bridge.slotsTotal
end

function Bridge:Refresh(now)
    now = now or GetTime()
    if self.lastSeen > 0 and now - self.lastSeen <= self.FRESH_SECONDS then
        self.status = self.publishedBridge or "up"
    elseif self.lastSeen > 0 then
        self.status = "down"
    else
        self.status = "starting"
    end
    return self.status
end

-- ------------------------------------------------------------------ inbox

local Inbox = {}
ns.Inbox = Inbox

Inbox.pending = {}          -- [outbound id] = ladder state
Inbox.attempted = {}        -- [slot index] = true, once per UI session
Inbox.nextSlotPos = 1
Inbox.ticker = nil
Inbox.restored = false
Inbox.exhausted = false

Inbox.TICK = 0.5
Inbox.MODE0_TIMES = { 2, 4, 7, 11, 16, 22, 30, 45, 60, 90 }
Inbox.MODE0_REPEAT = 120
Inbox.SAFETY_TIMES = { 30, 120 }
Inbox.GIVE_UP = 600

function Inbox:Start()
    self:StartTicker()
end

function Inbox:StartTicker()
    if self.ticker then return end
    self.ticker = C_Timer.NewTicker(self.TICK, function()
        Inbox:Tick()
    end)
end

function Inbox:StopTicker()
    if self.ticker then
        if self.ticker.Cancel then self.ticker:Cancel() end
        self.ticker = nil
    end
end

-- A message went out; start watching for its reply (B.4).
function Inbox:ExpectReply(id)
    self.pending[id] = {
        started = GetTime(),
        step = 0,
        safety = 1,
        sigSeen = false,
    }
end

function Inbox:ClearPending(id)
    self.pending[id] = nil
end

function Inbox:PendingCount()
    local n = 0
    for _ in pairs(self.pending) do n = n + 1 end
    return n
end

-- Load the next slot addon, ascending, each at most once per UI session.
function Inbox:LoadNextSlot(reason)
    if Bridge.slotsTotal == 0 then Bridge.ScanSlots() end
    local indexes = Bridge.slotIndexes
    for pos = self.nextSlotPos, #indexes do
        local idx = indexes[pos]
        if not self.attempted[idx] then
            self.attempted[idx] = true
            self.nextSlotPos = pos
            local name = ("WoWHermes_S%03d"):format(idx)
            if C_AddOns and C_AddOns.LoadAddOn then
                local ok, loaded = pcall(C_AddOns.LoadAddOn, name)
                if not ok or not loaded then
                    -- the mock/client may refuse one slot; try the next
                    return self:LoadNextSlot(reason)
                end
            end
            return idx
        end
    end
    if not self.exhausted then
        self.exhausted = true
        say("All reply slots are used up for this session. Type /reload to pick up new replies.")
    end
    return nil
end

-- The slot number the next LoadNextSlot() will load (PROTOCOL A.1 flag `q`).
-- 0 when the pool is exhausted (or no slot addons are installed).
function Inbox:NextSlotNumber()
    if Bridge.slotsTotal == 0 then Bridge.ScanSlots() end
    for _, idx in ipairs(Bridge.slotIndexes) do
        if not self.attempted[idx] then return idx end
    end
    return 0
end

function Inbox:SlotsLeft()
    local left = 0
    for _, idx in ipairs(Bridge.slotIndexes) do
        if not self.attempted[idx] then left = left + 1 end
    end
    return left
end

-- Slot cadence per PROTOCOL B.4.
function Inbox:Tick()
    local now = GetTime()
    Bridge:Refresh(now)
    for id in pairs(self.pending) do
        local p = self.pending[id]
        local age = now - p.started
        if age >= self.GIVE_UP then
            self.pending[id] = nil
            say(("No reply for message %d yet; it will appear after /reload."):format(id))
        elseif Bridge.UseDoorbells() then
            if not p.sigSeen and Bridge.Exists(Bridge.SigPath(id)) then
                p.sigSeen = true
                self:LoadNextSlot("sig")
            end
            local limit = self.SAFETY_TIMES[p.safety]
            if limit and age >= limit then
                p.safety = p.safety + 1
                self:LoadNextSlot("safety")
            end
        else
            local idx = p.step + 1
            local offset = self.MODE0_TIMES[idx]
            if not offset then
                offset = self.MODE0_TIMES[#self.MODE0_TIMES] + (idx - #self.MODE0_TIMES) * self.MODE0_REPEAT
            end
            if age >= offset then
                p.step = idx
                self:LoadNextSlot("ladder")
            end
        end
    end
end

-- ------------------------------------------------------------------ apply

function Inbox:ApplyReply(reply)
    local id = tonumber(reply and reply.id)
    if not id then return end
    local text = reply.text
    local status = reply.status or "done"
    local t = tonumber(reply.t)

    local entry = History.Find(id, "hermes")
    if not entry then
        History:Add("hermes", id, text or "", status, t)
    else
        if text ~= nil and text ~= "" then entry.text = text end
        entry.status = status
        if t then entry.t = t end
    end

    if status == "done" or status == "error" then
        self.pending[id] = nil
        entry = entry or History.Find(id, "hermes")
        if entry and not entry.announced then
            entry.announced = true
            self:Announce(entry)
        end
    end

    if ns.Display and ns.Display.Refresh then ns.Display:Refresh() end
end

-- First time a reply finishes: open the window (autoOpen) and echo it in chat (echoReplies),
-- so the player sees it even with the window closed. Never re-announced after /reload.
function Inbox.Announce(_, entry)
    local settings = WoWHermesDB and WoWHermesDB.settings or {}
    if settings.echoReplies ~= false then
        local raw = tostring(entry.text or "")
        local text = (ns.Format and ns.Format.Plain(raw)) or raw
        text = text:gsub("%s+", " ")
        if #text > 240 then
            local cut = 237
            -- never split a UTF-8 sequence: back off over continuation bytes (0x80-0xBF)
            while cut > 1 and text:byte(cut + 1) and text:byte(cut + 1) >= 0x80 and text:byte(cut + 1) < 0xC0 do
                cut = cut - 1
            end
            -- never split the escaped-pipe pair either
            while cut > 1 and text:sub(cut, cut + 1) == "||" do
                cut = cut - 1
            end
            text = text:sub(1, cut) .. "..."
        end
        -- Prefer the dedicated "Hermes" chat tab; fall back to the default frame
        -- (or /h status when there is no chat frame at all).
        local routed = false
        if ns.ChatTab then
            routed = ns.ChatTab:ReplyLine(text, entry.status == "error")
        end
        if not routed then
            say((entry.status == "error" and "|cffff6060Hermes error:|r " or "|cff66ccffHermes:|r ") .. text)
        end
    end
    if settings.autoOpen ~= false and ns.Display and ns.Display.Show then ns.Display:Show() end
end

-- state = the table from protocol.render_slot()
function Inbox:Apply(state)
    if type(state) ~= "table" then return end
    if state.v and tonumber(state.v) ~= 1 then return end

    -- A bridge restart resets seq to 1. The additive `boot` timestamp lets us
    -- distinguish that from an older slot file read out of order: accept only a
    -- newer incarnation, never oscillate back to an older publisher.
    local boot = tonumber(state.boot) or 0
    if boot > 0 then
        if boot > Bridge.boot then
            Bridge.boot = boot
            Bridge.lastSeq = 0
        elseif boot < Bridge.boot then
            return
        end
    end
    local seq = tonumber(state.seq) or 0
    if seq <= Bridge.lastSeq then return end       -- duplicate/stale within one publisher
    Bridge.lastSeq = seq
    Bridge.lastSeen = GetTime()
    Bridge.publishedBridge = state.bridge or "up"

    local ack = tonumber(state.ack)
    if ack and ack > Bridge.ack then Bridge.ack = ack end
    if type(state.char) == "string" and state.char ~= "" then Bridge.char = state.char end

    if type(state.restore) == "table" and #state.restore > 0 then
        History:Replace(state.restore)
        self.restored = true
        self.pending = {}
        if ns.Display and ns.Display.Refresh then ns.Display:Refresh() end
    end

    if type(state.replies) == "table" then
        -- B.2 rev: `replies` is a sliding window (latest <= 4 exchanges), not the
        -- transcript. Apply what is present; NEVER delete or rewrite history for
        -- ids that dropped out of the window. Only a `restore` bundle (new
        -- session token) replaces the transcript.
        for _, reply in ipairs(state.replies) do
            if type(reply) == "table" then self:ApplyReply(reply) end
        end
    end

    -- v1.2 confirmed commands: a pending Accept popup and the bridge's last local
    -- notice. Unknown keys are ignored by every older addon, so this is additive.
    if ns.Confirm then
        ns.Confirm:Apply(state.confirm)
        ns.Confirm:Note(state.note)
    end

    -- v1.3: post-install UI reload countdown (`reload` slot key). Additive: older
    -- addons ignore the key, and a missing key never cancels a prompt already shown.
    if ns.ReloadPrompt then ns.ReloadPrompt:Apply(state.reload) end

    if ns.Display and ns.Display.RefreshStatus then ns.Display:RefreshStatus() end
end

-- Called from Core on every ADDON_LOADED; handles only our slot addons.
function Inbox:OnAddonLoaded(addonName)
    local idx = type(addonName) == "string" and addonName:match("^WoWHermes_S(%d%d%d)$")
    if not idx then return false end
    local state = _G.WoWHermes_Slot
    _G.WoWHermes_Slot = nil
    if type(state) == "table" then
        self:Apply(state)
    end
    return true
end
