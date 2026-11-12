--[[---------------------------------------------------------------------------
    WoWHermes / Outbox.lua — outbound records (PROTOCOL A.1, A.2, A.4, A.5).

    One frame in flight, four queued. Each frame is drawn on the strip, then we
    wait for the bridge's ack doorbell (`ack\NNN.wav`) every 0.25 s. No ack in
    20 s → redraw the same id, three tries, then mark the message `unsent`.

    Transport setting `reload` (A.5): records are appended to WoWHermesDB.outbox
    and the player is told to /reload; the bridge reads the SavedVariables file.
--------------------------------------------------------------------------------]]

local _, ns = ...

local Codec = ns.Codec
local Strip = ns.Strip
local History = ns.History

local Outbox = {}
ns.Outbox = Outbox

Outbox.QUEUE_MAX = 4        -- 4 queued behind the one in flight (A.4)
Outbox.RETRY_SECONDS = 20
Outbox.MAX_TRIES = 3
Outbox.ACK_LOADS = { 0.25, 0.75, 1.5, 4, 9, 15 }  -- seconds after first draw: load a slot to read `ack`

-- PROTOCOL A.1 hello key `cap`: what this addon can produce (v1.1).
Outbox.CAPABILITIES = "quest,cmd"

Outbox.queue = {}
Outbox.inflight = nil
Outbox.ticker = nil
Outbox.lastContextSig = nil
Outbox.lastQuestSig = nil
Outbox.helloLastSent = nil

local function say(msg)
    if ns.Print then ns.Print(msg) else print(msg) end
end

-- Mirror the player's own line into the "Hermes" chat tab (when it exists), so
-- the tab reads as a conversation instead of only showing replies.
local function notifyUserLine(text)
    if ns.ChatTab then ns.ChatTab:PlayerLine(text) end
end

-- ------------------------------------------------------------------ context

local function secretGuard(value)
    if type(issecretvalue) == "function" then
        local ok, secret = pcall(issecretvalue, value)
        if ok and secret then return nil end
    end
    return value
end

local function safeCall(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, value = pcall(fn, ...)
    if not ok then return nil end
    return secretGuard(value)
end

-- pcall for multi-return getters (GetProfessions, GetProfessionInfo): the first
-- value alone is useless, and every value needs the secret check.
local function safeCallN(fn, ...)
    if type(fn) ~= "function" then return nil end
    local results = { pcall(fn, ...) }
    if not results[1] then return nil end
    return secretGuard(results[2]), secretGuard(results[3]), secretGuard(results[4]),
        secretGuard(results[5]), secretGuard(results[6]), secretGuard(results[7])
end

local function fmtCoord(v)
    if type(v) ~= "number" then return nil end
    return string.format("%.1f", v * 100)
end

-- Byte-exact UTF-8 cut (Lua 5.1 strings are byte strings; never split a sequence).
local function utf8Cut(text, maxBytes)
    if #text <= maxBytes then return text end
    local n = maxBytes
    while n > 0 do
        local firstRemoved = text:byte(n + 1)
        if firstRemoved == nil then break end
        if firstRemoved < 0x80 or firstRemoved >= 0xC0 then break end
        n = n - 1
    end
    if n <= 0 then return "" end
    return text:sub(1, n)
end

-- A single `k=v` value: `;` would split the pair list, `=` would split key from
-- value, `|` reads as a list separator downstream, and any control byte (newlines
-- included) would break the record text. Mirrors Quest.lua's cleanText.
local function cleanValue(value)
    if value == nil then return nil end
    if type(value) == "number" then value = tostring(value) end
    if type(value) ~= "string" then return nil end
    value = value:gsub("[;=|]", " ")
        :gsub("[%c]", " ")
        :gsub("%s+", " ")
        :gsub("^%s+", "")
        :gsub("%s+$", "")
    if value == "" then return nil end
    return value
end

local Context = {}
ns.Context = Context

Context.MAX_BYTES = 350           -- whole `k` text budget (v1.3, PROTOCOL A.1)
Context.MAX_PROF_BYTES = 80       -- `prof`
Context.MAX_QUEST_BYTES = 60      -- `quest`
-- Resend gate granularity: xp moves with every kill, so the *signature* rounds it
-- (see Context.Signature); the resent text still carries the exact value.
Context.XP_STEP = 5
Context.RESTED_STEP = 10

-- ------------------------------------------------------------------ context fields
-- Each getter returns `value, reason`: the value when it is usable, otherwise nil
-- plus a short reason for the `/h selftest` readout. Everything is feature-detected
-- (a missing global is "unavailable", never an error) and every returned value is
-- pcall- + secret-guarded.
--
-- WoW globals that the API annotations do not cover are read with rawget(_G, …),
-- the idiom ChatTab/Options/Scrollbar already use for the same reason.

-- Primary professions with rank: `Mining 12/75,Skinning 30/75`, capped at 80 bytes.
-- GetProfessions returns profession *indices* (Camelot/Forever: prim1, prim2, then
-- five secondary slots — Blizzard_Professions/Camelot/Blizzard_ProfessionsFrame.lua:5).
-- GetProfessionInfo(index) -> name, texture, rank, maxRank, …
-- (Blizzard_ProfessionsBook/Camelot/Blizzard_ProfessionsBook.lua:55).
local function wowGlobal(name)
    return rawget(_G, name)
end

local function professionsValue()
    local getProfs, getInfo = wowGlobal("GetProfessions"), wowGlobal("GetProfessionInfo")
    if type(getProfs) ~= "function" then return nil, "GetProfessions absent" end
    if type(getInfo) ~= "function" then return nil, "GetProfessionInfo absent" end
    local first, second = safeCallN(getProfs)
    local parts = {}
    for _, index in ipairs({ first or 0, second or 0 }) do
        if type(index) == "number" and index > 0 then
            local name, _, rank, maxRank = safeCallN(getInfo, index)
            name = cleanValue(name)
            if name then
                if type(rank) == "number" and type(maxRank) == "number" and maxRank > 0 then
                    name = name .. " " .. string.format("%d/%d", rank, maxRank)
                end
                local candidate = (#parts > 0) and (table.concat(parts, ",") .. "," .. name) or name
                if #candidate <= Context.MAX_PROF_BYTES then parts[#parts + 1] = name end
            end
        end
    end
    if #parts == 0 then return nil, "no profession" end
    return table.concat(parts, ",")
end

-- Free slots across the backpack and the equipped bags. Bag indices are
-- 0..NUM_BAG_SLOTS (Enum.BagIndex.Backpack = 0, Bag_1..Bag_4 = 1..4) plus the
-- reagent bag at Enum.BagIndex.ReagentBag = 5 when Forever has one
-- (BagIndexConstantsDocumentation.lua:25-31, Blizzard_FrameXMLBase/Constants.lua:165-170).
-- An empty bag slot returns nil from the getter, so it simply contributes nothing.
local function freeBagSlots()
    local container = wowGlobal("C_Container")
    if type(container) ~= "table" or type(container.GetContainerNumFreeSlots) ~= "function" then
        return nil, "C_Container.GetContainerNumFreeSlots absent"
    end
    local getFree = container.GetContainerNumFreeSlots
    local bagIndex = wowGlobal("Enum")
    bagIndex = (type(bagIndex) == "table" and type(bagIndex.BagIndex) == "table")
        and bagIndex.BagIndex or nil
    local first = 0
    if bagIndex and type(bagIndex.Backpack) == "number" then
        first = bagIndex.Backpack
    elseif type(wowGlobal("BACKPACK_CONTAINER")) == "number" then
        first = wowGlobal("BACKPACK_CONTAINER")
    end
    local last = tonumber(wowGlobal("NUM_BAG_SLOTS")) or (first + 4)
    local total, seen = 0, false
    for i = first, last do
        local free = safeCall(getFree, i)
        if type(free) == "number" and free >= 0 then
            total, seen = total + free, true
        end
    end
    local reagentSlots = tonumber(wowGlobal("NUM_REAGENTBAG_SLOTS")) or 0
    local reagentIndex = bagIndex and bagIndex.ReagentBag or nil
    if reagentSlots > 0 and type(reagentIndex) == "number" then
        local free = safeCall(getFree, reagentIndex)
        if type(free) == "number" and free >= 0 then
            total, seen = total + free, true
        end
    end
    if not seen then return nil, "no bag slots readable" end
    return total
end

-- UnitXP / UnitXPMax (UnitDocumentation.lua:3474 / :3489, both
-- SecretArguments = AllowedWhenUntainted) as a whole percent of the current level.
local function xpPercent()
    local current = safeCall(wowGlobal("UnitXP"), "player")
    local maximum = safeCall(wowGlobal("UnitXPMax"), "player")
    if type(current) ~= "number" then return nil, "UnitXP unavailable" end
    if type(maximum) ~= "number" or maximum <= 0 then return nil, "UnitXPMax unavailable" end
    local pct = math.floor(current * 100 / maximum + 0.5)
    if pct < 0 then pct = 0 elseif pct > 100 then pct = 100 end
    return pct, string.format("raw %d/%d", current, maximum)
end

-- GetXPExhaustion() -> rested xp in points, nil when nothing is rested
-- (PlayerScriptDocumentation.lua:1168). Reported as a whole percent of the level.
local function restedPercent()
    local getExhaustion = wowGlobal("GetXPExhaustion")
    if type(getExhaustion) ~= "function" then return nil, "GetXPExhaustion absent" end
    local rested = safeCall(getExhaustion)
    if type(rested) ~= "number" then return nil, "no rest" end
    if rested <= 0 then return nil, "0 rested" end
    local maximum = safeCall(wowGlobal("UnitXPMax"), "player")
    if type(maximum) ~= "number" or maximum <= 0 then return nil, "UnitXPMax unavailable" end
    local pct = math.floor(rested * 100 / maximum + 0.5)
    if pct < 1 then return nil, "under 1%" end
    return pct, string.format("raw %d", rested)
end

-- Super-tracked quest, else the first watched quest: `<questID>:<title>`, 60 bytes.
-- Quest.TrackedQuestID already implements that selection (Quest.lua:106) and
-- C_QuestLog.GetTitleForQuestID is the title source (QuestLogDocumentation.lua:668).
local function questKey()
    local quest = ns.Quest
    if type(quest) ~= "table" or type(quest.TrackedQuestID) ~= "function" then
        return nil, "Quest module unavailable"
    end
    local ok, questID = pcall(quest.TrackedQuestID)
    if not ok or type(questID) ~= "number" or questID <= 0 then
        return nil, "no tracked quest"
    end
    local log = wowGlobal("C_QuestLog")
    local title
    if type(log) == "table" then
        local raw = safeCall(log.GetTitleForQuestID, questID)
        if type(raw) == "string" then title = raw end
        if not title and type(log.GetLogIndexForQuestID) == "function" then
            local index = safeCall(log.GetLogIndexForQuestID, questID)
            if type(index) == "number" then
                local info = safeCall(log.GetInfo, index)
                if type(info) == "table" then
                    local rawTitle = secretGuard(info.title)
                    if type(rawTitle) == "string" then title = rawTitle end
                end
            end
        end
    end
    title = cleanValue(title)
    if not title then return nil, "no title for " .. tostring(questID) end
    return utf8Cut(string.format("%d:%s", questID, title), Context.MAX_QUEST_BYTES), "id " .. tostring(questID)
end

-- Current game context as a `k=v;…` string (PROTOCOL A.1 kind `k`).
Context.DROP_ORDER = { "quest", "prof", "rested", "bags", "xp" }

function Context.Build()
    local pairs_ = {}
    local function add(key, value)
        if value ~= nil and value ~= "" then pairs_[#pairs_ + 1] = { key, tostring(value) } end
    end

    add("zone", safeCall(GetZoneText))
    add("sub", safeCall(GetSubZoneText))

    local mapId = safeCall(C_Map and C_Map.GetBestMapForUnit, "player")
    if mapId and C_Map and type(C_Map.GetPlayerMapPosition) == "function" then
        local pos = safeCall(C_Map.GetPlayerMapPosition, mapId, "player")
        if pos and type(pos.GetXY) == "function" then
            local ok, px, py = pcall(pos.GetXY, pos)
            if ok then
                add("x", fmtCoord(secretGuard(px)))
                add("y", fmtCoord(secretGuard(py)))
            end
        end
    end

    add("lvl", safeCall(UnitLevel, "player"))

    if type(UnitClass) == "function" then
        local ok, _, classFile = pcall(UnitClass, "player")
        if ok and type(classFile) == "string" then add("class", secretGuard(classFile)) end
    end
    if type(UnitRace) == "function" then
        local ok, _, raceFile = pcall(UnitRace, "player")
        if ok and type(raceFile) == "string" then add("race", secretGuard(raceFile)) end
    end

    add("money", safeCall(GetMoney))
    add("combat", (safeCall(UnitAffectingCombat, "player")) and 1 or 0)

    local inInstance = safeCall(IsInInstance)
    add("instance", inInstance and 1 or 0)
    add("lockdown", (safeCall(C_ChatInfo and C_ChatInfo.InChatMessagingLockdown)) and 1 or 0)

    -- v1.3 (WS-W): appended after the original 11 keys, because an older adapter
    -- keeps only the first N fields of the record.
    add("prof", (professionsValue()))
    add("bags", (freeBagSlots()))
    add("xp", (xpPercent()))
    add("rested", (restedPercent()))
    add("quest", (questKey()))

    local text = Codec.format_kv(pairs_)
    -- Hard budget: shed the optional tail, cheapest-to-lose first, never an original key.
    for _, key in ipairs(Context.DROP_ORDER) do
        if #text <= Context.MAX_BYTES then break end
        local kept = {}
        for _, pair in ipairs(pairs_) do
            if pair[1] ~= key then kept[#kept + 1] = pair end
        end
        pairs_ = kept
        text = Codec.format_kv(pairs_)
    end
    return text
end

-- Resend gate. The bridge keeps the last `k` per character, so a change means a
-- fresh ~200 B record in the next frame; xp and rested move on every kill. Round
-- those two down to a step for the *comparison* only — the text that is actually
-- sent still carries the exact value. 5 % of a level is finer than any advice that
-- depends on it, and the level itself (`lvl`) is an exact key.
function Context.Signature(text)
    text = tostring(text or "")
    if text == "" then return "" end
    local fields = Codec.parse_kv(text)
    local function bucket(key, step)
        local n = tonumber(fields[key])
        if n then fields[key] = tostring(math.floor(n / step) * step) end
    end
    bucket("xp", Context.XP_STEP)
    bucket("rested", Context.RESTED_STEP)
    local ordered = {}
    for key, value in pairs(fields) do ordered[#ordered + 1] = { key, value } end
    table.sort(ordered, function(a, b) return a[1] < b[1] end)
    return Codec.format_kv(ordered)
end

-- Dev aid for `/h selftest`: one line per v1.3 field with its value or the reason
-- it is missing, plus the rendered size. The first in-game screenshot then shows
-- exactly which of these APIs return usable values on the live client.
function Context.SelftestLines()
    local lines = {}
    local function line(key, value, reason)
        if value ~= nil then
            lines[#lines + 1] = ("ctx %s=%s"):format(key, tostring(value))
        else
            lines[#lines + 1] = ("ctx %s: %s"):format(key, tostring(reason or "unavailable"))
        end
    end
    line("prof", professionsValue())
    line("bags", freeBagSlots())
    line("xp", xpPercent())
    line("rested", restedPercent())
    line("quest", questKey())
    lines[#lines + 1] = ("ctx bytes=%d cap=%d"):format(#Context.Build(), Context.MAX_BYTES)
    return lines
end

-- ------------------------------------------------------------------ helpers

local function db()
    return WoWHermesDB
end

function Outbox.UseStrip()
    local d = db()
    return not (d and d.settings and d.settings.transport == "reload")
end

function Outbox.SendContext()
    local d = db()
    if not d or not d.settings then return true end
    return d.settings.sendContext ~= false
end

-- Per-session frame id, monotonic, wraps 65535 -> 1 (0 is unused).
function Outbox.AllocId()
    local d = db()
    if not d then return 1 end
    local id = d.nextId or 1
    if id < 1 or id > 65535 then id = 1 end
    d.nextId = (id >= 65535) and 1 or (id + 1)
    return id
end

function Outbox.LastId()
    local d = db()
    if not d then return 0 end
    local n = (d.nextId or 1) - 1
    if n < 0 then n = 65535 end
    return n
end

-- PROTOCOL A.1 flag `q`: the slot number the addon will load next (1..256),
-- 0 when the pool is exhausted. Only `h` and `m` records carry flags.
local function flagsFor(kind)
    if kind ~= "h" and kind ~= "m" then return "" end
    local q = 0
    if ns.Inbox and ns.Inbox.NextSlotNumber then q = ns.Inbox:NextSlotNumber() end
    return "q=" .. tostring(q)
end

local function buildPayload(item, id)
    local d = db()
    local session = (d and d.session) or "00000000"
    local records = {}
    if item.context then
        records[#records + 1] = {
            session = session, chat = History.CHAT, id = id, kind = "k", flags = "",
            text = item.context,
        }
    end
    if item.quest then
        -- PROTOCOL A.1 kind `q`: flags empty, and it rides the same frame id as the `m`
        -- it explains (like `k`, it never consumes the frame id).
        records[#records + 1] = {
            session = session, chat = History.CHAT, id = id, kind = "q", flags = "",
            text = item.quest,
        }
    end
    records[#records + 1] = {
        session = session, chat = History.CHAT, id = id, kind = item.kind,
        flags = flagsFor(item.kind),
        text = item.text or "",
    }
    return Codec.encode_records(records)
end

-- ------------------------------------------------------------------ queue

local function appendOutboxRecord(id, kind, text)
    local d = db()
    if not d then return end
    d.outbox = d.outbox or {}
    d.outbox[#d.outbox + 1] = { id = id, chat = History.CHAT, kind = kind, text = text, t = time() }
end

-- Move a strip queue item into the A.5 SavedVariables outbox without changing
-- its id. If the bridge already decoded the visible frame, its normal dedupe
-- prevents a second submission after /reload.
local function appendReloadItem(item, id)
    if item.kind == "m" and item.quest then appendOutboxRecord(id, "q", item.quest) end
    appendOutboxRecord(id, item.kind, item.text or "")
    if item.kind == "m" then History.Update(id, { status = "queued" }, "user") end
end

-- User message. Returns true or false, reason.
-- `opts.silent` sends the record without putting it in the transcript or the
-- Hermes chat tab — used by Display:NewSession for `/new`, where the addon wants
-- the backend reset but not a fake conversation line in the fresh session.
function Outbox:SubmitMessage(text, opts)
    text = tostring(text or "")
    if text == "" then return false, "empty" end
    opts = opts or {}
    local silent = opts.silent == true

    local context, contextSig
    if Outbox.SendContext() then
        local ctx = ns.Context.Build()
        if ctx ~= "" then
            local sig = ns.Context.Signature(ctx)
            if sig ~= self.lastContextSig then
                context, contextSig = ctx, sig
            end
        end
    end

    -- PROTOCOL A.1 kind `q`: only when the snapshot changed since the last one sent.
    local quest
    if ns.Quest then
        local snapshot = ns.Quest.Text()
        if type(snapshot) == "string" and snapshot ~= "" and snapshot ~= self.lastQuestSig then
            quest = snapshot
        end
    end

    if not Outbox.UseStrip() then
        local id = Outbox.AllocId()
        -- A.5 keeps the same order as the strip: q first, then the m it explains.
        if quest then appendOutboxRecord(id, "q", quest) end
        appendOutboxRecord(id, "m", text)
        if not silent then
            History:Add("user", id, text, "queued")
            notifyUserLine(text)
            say("Queued for /reload (outbound id " .. id .. "). Type /reload to send it.")
        end
        if quest then self.lastQuestSig = quest end
        return true
    end

    if #self.queue >= self.QUEUE_MAX then
        return false, "busy"
    end

    local id = Outbox.AllocId()
    local payload = buildPayload({ kind = "m", text = text, context = context, quest = quest }, id)
    if #payload > Codec.MAX_PAYLOAD then
        -- Refuse, keep the draft, roll the id back so the sequence stays tight.
        local d = db()
        if d and d.nextId == ((id >= 65535) and 1 or (id + 1)) then d.nextId = id end
        -- The context is the cheapest thing to drop (the bridge already has the last k);
        -- then the quest snapshot; only then refuse.
        if context then
            payload = buildPayload({ kind = "m", text = text, quest = quest }, id)
            context, contextSig = nil, nil
        end
        if #payload > Codec.MAX_PAYLOAD and quest then
            payload = buildPayload({ kind = "m", text = text, context = context }, id)
            if #payload <= Codec.MAX_PAYLOAD then quest = nil end
        end
        if #payload > Codec.MAX_PAYLOAD then
            return false, "oversize", #payload
        end
    end

    if not silent then
        History:Add("user", id, text, "queued")
        notifyUserLine(text)
    end
    self.queue[#self.queue + 1] = {
        kind = "m",
        text = text,
        context = context,
        ctxSig = contextSig,
        quest = quest,
        qSig = quest,
        preallocatedId = id,
        precomputedPayload = payload,
    }
    self:Pump()
    return true
end

-- Control record: hello `h`, bye `x`, error `e`.
function Outbox:SendControl(kind, text)
    text = text or ""
    if not Outbox.UseStrip() then
        local id = Outbox.AllocId()
        appendOutboxRecord(id, kind, text)
        return true
    end
    if #self.queue >= self.QUEUE_MAX and kind ~= "e" then return false end
    self.queue[#self.queue + 1] = { kind = kind, text = text }
    self:Pump()
    return true
end

-- Player confirmation of a bridge command (PROTOCOL A.1 kind `c`, research 08 §8.2).
-- `text` is already `id=<command id>;ok=1|0`. A fresh outbound id, like any record.
function Outbox:SendConfirm(text)
    text = tostring(text or "")
    if text == "" then return false end
    if not Outbox.UseStrip() then
        local id = Outbox.AllocId()
        appendOutboxRecord(id, "c", text)
        say("Confirmation queued (outbound id " .. id .. "). Type /reload to send it.")
        return true
    end
    if #self.queue >= self.QUEUE_MAX then return false end
    self.queue[#self.queue + 1] = { kind = "c", text = text }
    self:Pump()
    return true
end

function Outbox:Pump()
    if self.inflight then return end
    local item = table.remove(self.queue, 1)
    if not item then return end

    local id = item.preallocatedId or Outbox.AllocId()
    item.id = id
    local payload = item.precomputedPayload or buildPayload(item, id)
    if item.kind == "m" or item.kind == "h" then
        -- The `q` flag must name the slot that is next *now*, not at submit time.
        payload = buildPayload(item, id)
    end
    if #payload > Codec.MAX_PAYLOAD then
        say("Message too long for one frame; dropped before transmission.")
        self:Pump()
        return
    end
    if item.ctxSig then self.lastContextSig = item.ctxSig end
    if item.qSig then self.lastQuestSig = item.qSig end

    local frame = Codec.encode_frame(id, payload)
    self.inflight = {
        id = id, payload = payload, frame = frame, tries = 1, since = GetTime(), item = item,
        -- Bridge.ack at transmit time: only an ack observed *while waiting* counts
        -- (guards the 65535 -> 1 wrap against a stale slot ack).
        ackAtSend = (ns.Bridge and ns.Bridge.ack) or 0,
    }
    if Outbox.UseStrip() then
        Strip:ShowFrame(frame, #payload)
        self:StartTicker()
    end
    if item.kind == "m" then
        if ns.Inbox and ns.Inbox.ExpectReply then ns.Inbox:ExpectReply(id) end
        if ns.Display and ns.Display.Refresh then ns.Display:Refresh() end
    end
end

function Outbox:Redraw()
    local inf = self.inflight
    if not inf or not Outbox.UseStrip() then return end
    Strip:ShowFrame(inf.frame, #inf.payload)
end

-- Apply a live transport change to work that was already queued. In particular,
-- reload mode must take the pixels down immediately; waiting for the old ack
-- path leaves the last frame visible and strands later records in a hidden
-- in-flight slot.
function Outbox:OnTransportChanged()
    if Outbox.UseStrip() then
        self:Redraw()
        return
    end

    Strip:Hide()
    self:StopTicker()
    local pending = {}
    if self.inflight then pending[#pending + 1] = { item = self.inflight.item, id = self.inflight.id } end
    for _, item in ipairs(self.queue) do pending[#pending + 1] = { item = item } end
    self.inflight = nil
    self.queue = {}

    for _, pendingItem in ipairs(pending) do
        local item = pendingItem.item
        local id = pendingItem.id or item.preallocatedId or Outbox.AllocId()
        item.id = id
        appendReloadItem(item, id)
        if item.qSig then self.lastQuestSig = item.qSig end
    end
    if #pending > 0 and ns.Display and ns.Display.Refresh then ns.Display:Refresh() end
end

function Outbox:Complete(ok)
    local inf = self.inflight
    if not inf then return end
    self.inflight = nil
    if Outbox.UseStrip() then Strip:Hide() end
    local item = inf.item
    if item and item.kind == "m" then
        History.Update(inf.id, { status = ok and "sent" or "unsent" }, "user")
        if ns.Display and ns.Display.Refresh then ns.Display:Refresh() end
    end
    if not ok then
        say(("The bridge did not pick up message %d; it is saved as unsent."):format(inf.id))
    end
    self:StopTicker()
    self:Pump()
end

-- ------------------------------------------------------------------ ack poll

function Outbox:StartTicker()
    if self.ticker then return end
    self.ticker = C_Timer.NewTicker(0.25, function()
        Outbox:Tick()
    end)
end

function Outbox:StopTicker()
    if self.ticker then
        if self.ticker.Cancel then self.ticker:Cancel() end
        self.ticker = nil
    end
end

function Outbox:Tick()
    local inf = self.inflight
    if not inf then
        self:StopTicker()
        return
    end
    -- PROTOCOL B.3: the ack doorbell OR any loaded slot whose `ack` field is
    -- >= this id. The slot path is the only one available in doorbell mode 0.
    local bridgeAck = (ns.Bridge and ns.Bridge.ack) or 0
    if bridgeAck >= inf.id and bridgeAck > (inf.ackAtSend or 0) then
        self:Complete(true)
        return
    end
    local now = GetTime()
    if ns.Bridge and ns.Bridge.DoorbellsEnabled() then
        if ns.Bridge.Exists(ns.Bridge.AckPath(inf.id)) then
            self:Complete(true)
            return
        end
    elseif ns.Inbox then
        -- Ack via slots: the bridge raises the slot `ack` for every decoded frame. Load one
        -- slot at each step after the frame first went up (~4 slots per frame worst case).
        inf.firstShown = inf.firstShown or inf.since or now
        inf.ackStep = inf.ackStep or 0
        local due = self.ACK_LOADS[inf.ackStep + 1]
        if due and now - inf.firstShown >= due then
            inf.ackStep = inf.ackStep + 1
            ns.Inbox:LoadNextSlot("ack")
            bridgeAck = (ns.Bridge and ns.Bridge.ack) or 0
            if bridgeAck >= inf.id and bridgeAck > (inf.ackAtSend or 0) then
                self:Complete(true)
                return
            end
        end
    end
    if now - inf.since >= self.RETRY_SECONDS then
        inf.tries = inf.tries + 1
        if inf.tries > self.MAX_TRIES then
            self:Complete(false)
        else
            inf.since = now
            self:Redraw()
        end
    end
end

-- ------------------------------------------------------------------ hello

function Outbox.HelloText()
    local d = db()
    -- `b` is version*build*, e.g. 1.60.1.70862 (the vectors' spelling)
    local build, interface = "unknown", 0
    if type(GetBuildInfo) == "function" then
        local version, buildNum, _, i = GetBuildInfo()
        build = tostring(version)
        if buildNum and tostring(buildNum) ~= "" then
            build = build .. "." .. tostring(buildNum)
        end
        interface = tonumber(i) or 0
    end
    local width, height = 0, 0
    if type(GetPhysicalScreenSize) == "function" then
        local ok, w, h = pcall(GetPhysicalScreenSize)
        if ok then width, height = w or 0, h or 0 end
    end
    local scale = 1
    if UIParent and UIParent.GetEffectiveScale then
        local ok, s = pcall(UIParent.GetEffectiveScale, UIParent)
        if ok and s then scale = s end
    end

    local pairs_ = {
        { "v", Codec.VERSION },
        { "b", build },
        { "i", interface },
        { "char", (d and d.char) or "Unknown" },
        { "p", (ns.Bridge and ns.Bridge.slotsTotal) or 0 },
        { "s", (ns.Bridge and ns.Bridge.mode) or "0" },
        { "u", string.format("%.2f", scale) },
        { "g", string.format("%dx%d", width or 0, height or 0) },
        { "n", Outbox.LastId() },
        { "cap", Outbox.CAPABILITIES },
    }
    return Codec.format_kv(pairs_)
end

function Outbox:SubmitHello()
    self.helloLastSent = GetTime()
    return self:SendControl("h", Outbox.HelloText())
end

function Outbox:SubmitBye()
    return self:SendControl("x", "")
end

-- Development aid: our own Lua errors travel as `e` records (PROTOCOL A.1).
function Outbox:ReportError(message)
    if #self.queue > 20 then return end
    local text = tostring(message or "unknown error")
    self:SendControl("e", text:sub(1, 900))
end
