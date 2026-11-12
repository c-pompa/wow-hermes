--[[---------------------------------------------------------------------------
    WoWHermes / Quest.lua — the `q` quest snapshot record (PROTOCOL A.1, v1.1).

    Forever has no quest-prose getter (docs/research/08-wow-only-mode.md §5.2), so the
    snapshot carries the identity and the objective lines the player can already see:

        id=<questID>;title=<title>;zone=<zone>;objs=<obj1> | <obj2> | …

    One quest per record: the super-tracked quest when something is super-tracked,
    otherwise the first watched quest. Nothing is sent unless an `m` goes out and the
    rendered text differs from the last one actually framed (Outbox owns that check).

    Evidence (Forever 1.60.1.70862, see docs/notes/ws-quest.md):
      C_SuperTrack.GetSuperTrackedQuestID   SuperTrackManagerDocumentation.lua:65
      C_QuestLog.GetNumQuestWatches         QuestLogDocumentation.lua:328
      C_QuestLog.GetQuestIDForQuestWatchIndex  QuestLogDocumentation.lua:411
      C_QuestLog.GetLogIndexForQuestID      QuestLogDocumentation.lua:206
      C_QuestLog.GetInfo                    QuestLogDocumentation.lua:191
      C_QuestLog.GetQuestObjectives         QuestLogDocumentation.lua:477
      C_QuestLog.GetTitleForQuestID         QuestLogDocumentation.lua:668
      QuestInfo / QuestObjectiveInfo        QuestLogDocumentation.lua:1547 / 1581
      QUEST_LOG_UPDATE / QUEST_WATCH_UPDATE QuestLogDocumentation.lua:1373 / 1420
      PLAYER_ENTERING_WORLD                 SystemDocumentation.lua:136

    `SecretArguments = "AllowedWhenUntainted"` is on every getter we call: pass only
    plain numbers/strings, wrap each call in pcall and treat a secret result as "omit".
    The addon never reads chat and never touches a protected frame here.
--------------------------------------------------------------------------------]]

local _, ns = ...  -- luacheck: ignore 211

local Quest = {}
ns.Quest = Quest

Quest.MAX_BYTES = 700          -- PROTOCOL A.1 budget for the rendered q text
Quest.MAX_OBJECTIVES = 8       -- hard cap before the byte budget applies
Quest.MAX_TITLE_BYTES = 140
Quest.MAX_ZONE_BYTES = 60

Quest.dirty = true
Quest.cached = nil

local US, RS = "\31", "\30"

local function secretValue(value)
    if type(issecretvalue) ~= "function" then return false end
    local ok, secret = pcall(issecretvalue, value)
    return ok and secret == true
end

-- pcall + secret guard: a secret or missing value is simply "unknown".
local function safeCall(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, value = pcall(fn, ...)
    if not ok then return nil end
    if secretValue(value) then return nil end
    return value
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

-- User text: US/RS would split the record, `;` would split the k=v pairs and `|`
-- would split the objective list. Newlines/tabs collapse to one space.
local function cleanText(text)
    if type(text) ~= "string" then return "" end
    text = text:gsub(US, " "):gsub(RS, " ")
        :gsub("[;|]", " ")
        :gsub("[%c]", " ")
        :gsub("%s+", " ")
        :gsub("^%s+", "")
        :gsub("%s+$", "")
    return text
end

local function finiteNumber(value)
    if type(value) ~= "number" then return nil end
    if value ~= value or value == math.huge or value == -math.huge then return nil end
    return value
end

-- ---------------------------------------------------------------- selection

local function firstWatchedQuestID()
    local log = _G.C_QuestLog
    if type(log) ~= "table" then return nil end
    local count = finiteNumber(safeCall(log.GetNumQuestWatches))
    if not count or count < 1 then return nil end
    local questID = finiteNumber(safeCall(log.GetQuestIDForQuestWatchIndex, 1))
    if questID and questID > 0 then return questID end
    return nil
end

-- Public for tests and for /h status style diagnostics.
function Quest.TrackedQuestID()
    -- A secret super-track id means we cannot safely name the quest: omit the record
    -- rather than fall back to a different one the player is not looking at.
    local super = _G.C_SuperTrack
    if super ~= nil and type(super) == "table" and type(super.GetSuperTrackedQuestID) == "function" then
        local ok, raw = pcall(super.GetSuperTrackedQuestID)
        if ok and secretValue(raw) then return nil end
        raw = ok and finiteNumber(raw) or nil
        if raw and raw > 0 then return raw end
    end
    return firstWatchedQuestID()
end

local function questTitle(questID, info)
    local log = _G.C_QuestLog
    if info and type(info.title) == "string" then
        local cleaned = cleanText(info.title)
        if cleaned ~= "" then return cleaned end
    end
    if type(log) == "table" then
        local title = safeCall(log.GetTitleForQuestID, questID)
        if type(title) == "string" then return cleanText(title) end
    end
    return ""
end

local function numberIfPlain(value)
    if secretValue(value) then return nil end
    return finiteNumber(value)
end

local function objectiveLine(objective)
    if type(objective) ~= "table" then return nil end
    if secretValue(objective.text) then return nil end
    local text = cleanText(objective.text)
    if text == "" then return nil end
    local have = numberIfPlain(objective.numFulfilled)
    local need = numberIfPlain(objective.numRequired)
    if have and need and need > 0 then
        text = text .. " (" .. string.format("%d/%d", have, need) .. ")"
    end
    return text
end

-- ---------------------------------------------------------------- snapshot

-- Render the current snapshot. Returns "" when there is no tracked quest (or when the
-- client API is absent, e.g. an old build), which Outbox treats as "no q record".
function Quest.Render()
    local questID = Quest.TrackedQuestID()
    if not questID then return "" end

    local log = _G.C_QuestLog
    local info
    if type(log) == "table" then
        local index = safeCall(log.GetLogIndexForQuestID, questID)
        index = finiteNumber(index)
        if index then info = safeCall(log.GetInfo, index) end
    end
    if type(info) ~= "table" then info = nil end

    local title = utf8Cut(questTitle(questID, info), Quest.MAX_TITLE_BYTES)
    if title == "" then return "" end  -- unnameable quest: degrade to no record

    local zone = utf8Cut(cleanText(safeCall(GetZoneText)) or "", Quest.MAX_ZONE_BYTES)

    local objectives = {}
    if type(log) == "table" then
        local raw = safeCall(log.GetQuestObjectives, questID)
        if type(raw) == "table" and not secretValue(raw) then
            for i = 1, math.min(#raw, Quest.MAX_OBJECTIVES) do
                local line = objectiveLine(raw[i])
                if line then objectives[#objectives + 1] = line end
            end
        end
    end

    local prefix = "id=" .. string.format("%d", questID)
        .. ";title=" .. title
        .. ";zone=" .. zone
        .. ";objs="

    local included = {}
    local text = prefix
    for i = 1, #objectives do
        local candidate = text .. (#included > 0 and " | " or "") .. objectives[i]
        if #candidate <= Quest.MAX_BYTES then
            included[#included + 1] = objectives[i]
            text = candidate
        elseif #included == 0 then
            -- A single long objective: keep what fits, then stop.
            local room = Quest.MAX_BYTES - #prefix - 1
            if room > 8 then
                local cut = utf8Cut(objectives[i], room)
                if cut ~= "" then
                    included[1] = cut
                    text = prefix .. cut
                end
            end
            break
        else
            break
        end
    end
    if #included == 0 then text = prefix end
    return text
end

function Quest.MarkDirty()
    Quest.dirty = true
end

-- Quest-log events only invalidate the cache; the render happens when a message is
-- sent, so an idle player costs nothing (research 08 §5.3 send policy).
function Quest.OnEvent()
    Quest.MarkDirty()
end

-- Cached snapshot for Outbox. `dirty` is set by the quest events and by PLAYER_ENTERING_WORLD.
function Quest.Text()
    if Quest.dirty or Quest.cached == nil then
        local ok, text = pcall(Quest.Render)
        Quest.cached = ok and text or ""
        Quest.dirty = false
    end
    return Quest.cached
end

-- Test/diagnostic hook: drop the cache and re-render immediately.
function Quest.Reset()
    Quest.cached = nil
    Quest.dirty = true
end
