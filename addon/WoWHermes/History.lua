--[[---------------------------------------------------------------------------
    WoWHermes / History.lua — the conversation stored in
    WoWHermesDB.chats.c0.history (PROTOCOL A.5).

    Entry shape: { role = "user"|"hermes", id, t, text, status }
    `id` is the outbound frame id that carried (or answered) the message, so a
    slot reply can be matched back to the player's line without extra state.
--------------------------------------------------------------------------------]]

local _, ns = ...

local History = {}
ns.History = History

History.CHAT = "c0"
History.MAX = 200

local function chatTable(create)
    local db = WoWHermesDB
    if not db then return nil end
    db.chats = db.chats or {}
    local chat = db.chats[History.CHAT]
    if not chat and create then
        chat = { history = {} }
        db.chats[History.CHAT] = chat
    end
    if chat and not chat.history and create then chat.history = {} end
    return chat
end

function History.Init()
    chatTable(true)
end

-- array of entries (never nil; the DB list is the storage)
function History.All()
    local chat = chatTable(true)
    return chat and chat.history or {}
end

function History:Count()
    return #self:All()
end

function History:Add(role, id, text, status, t)
    local chat = chatTable(true)
    if not chat then return nil end
    local list = chat.history
    list[#list + 1] = {
        role = role,
        id = id,
        t = t or time(),
        text = text or "",
        status = status,
    }
    local over = #list - self.MAX
    if over > 0 then
        local kept = {}
        for i = over + 1, #list do kept[#kept + 1] = list[i] end
        chat.history = kept
        list = kept
    end
    return list[#list]
end

-- Most recent entry with this id, optionally filtered by role.
function History.Find(id, role)
    local list = History.All()
    for i = #list, 1, -1 do
        local e = list[i]
        if e.id == id and (role == nil or e.role == role) then return e, i end
    end
    return nil
end

-- Patch the newest entry with this id. fields: text/status/t/role.
function History.Update(id, fields, role)
    local entry = History.Find(id, role)
    if not entry then return nil end
    for k, v in pairs(fields) do entry[k] = v end
    return entry
end

-- The bridge's restore bundle: { {role, id, t, text}, ... }
function History:Replace(list)
    local chat = chatTable(true)
    if not chat then return end
    local copy = {}
    for i = 1, #list do
        local e = list[i]
        copy[i] = { role = e.role, id = e.id, t = e.t, text = e.text, status = e.status }
    end
    if #copy > self.MAX then
        local trimmed = {}
        for i = #copy - self.MAX + 1, #copy do trimmed[#trimmed + 1] = copy[i] end
        copy = trimmed
    end
    chat.history = copy
end

function History.Clear()
    local chat = chatTable(true)
    if chat then chat.history = {} end
end
