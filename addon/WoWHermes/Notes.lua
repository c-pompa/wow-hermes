--[[---------------------------------------------------------------------------
    WoWHermes / Notes.lua — saved replies, newest first, in WoWHermesDB.notes.

    Notes are a LOCAL feature: they live in SavedVariables only, never travel over
    the bridge (no protocol change). Saving is keyed on `(id, text)` so pressing
    Save twice, or saving after a reply was already stored, cannot duplicate it.

    Entry: { id, t, text, char, session }
      id      outbound frame id the reply answered (nil for hand-built entries)
      t       when the reply finished (client epoch seconds)
      text    the reply exactly as it sits in the history buffer
      char    Name-Realm at save time
      session the addon session token the reply belongs to
--------------------------------------------------------------------------------]]--

local _, ns = ... -- luacheck: ignore 212

local Notes = {}
ns.Notes = Notes

Notes.MAX = 50

-- Statuses a reply can carry while it is still streaming; anything else is a
-- finished reply (Slots.lua:259-271 stores the bridge's status verbatim).
Notes.WORKING = { working = true, typing = true }

local function db()
    return WoWHermesDB
end

local function store(create)
    local d = db()
    if not d then return nil end
    if type(d.notes) ~= "table" then
        if not create then return nil end
        d.notes = {}
    end
    return d.notes
end

-- Trim anything a hand-edited SavedVariables file could contain, and enforce the
-- cap. Called once at ADDON_LOADED.
function Notes.Init()
    local list = store(true)
    if not list then return end
    local clean = {}
    for i = 1, #list do
        local n = list[i]
        if type(n) == "table" and type(n.text) == "string" and n.text ~= "" then
            clean[#clean + 1] = {
                id = tonumber(n.id) or n.id,
                t = tonumber(n.t),
                text = n.text,
                char = n.char,
                session = n.session,
            }
            if #clean >= Notes.MAX then break end
        end
    end
    for i = #list, 1, -1 do list[i] = nil end
    for i = 1, #clean do list[i] = clean[i] end
end

function Notes.All()
    return store(true) or {}
end

function Notes.Count()
    return #(store(false) or {})
end

-- Newest first.
function Notes:Add(entry)
    local list = store(true)
    if not list or type(entry) ~= "table" then return nil, "invalid" end
    local text = tostring(entry.text or "")
    if text == "" then return nil, "empty" end
    if Notes.Has(entry) then return nil, "duplicate" end

    local note = {
        id = entry.id,
        t = tonumber(entry.t) or time(),
        text = text,
        char = entry.char,
        session = entry.session,
    }
    table.insert(list, 1, note)
    for i = self.MAX + 1, #list do list[i] = nil end
    return note
end

-- Same reply already saved? Match on the frame id it answered plus the text, so a
-- re-sent/edited entry with the same id still counts as new.
function Notes.Has(entry)
    local list = store(false)
    if not list or type(entry) ~= "table" then return false end
    local id = entry.id
    if id == nil then return false end
    local text = tostring(entry.text or "")
    for i = 1, #list do
        local n = list[i]
        if type(n) == "table" and n.id == id and n.text == text then return true end
    end
    return false
end

function Notes.Remove(index)
    local list = store(false)
    if not list then return false end
    index = tonumber(index)
    if not index or index < 1 or index > #list then return false end
    table.remove(list, index)
    return true
end

function Notes.Clear()
    local list = store(true)
    if list then
        for i = #list, 1, -1 do list[i] = nil end
    end
end

-- The newest Hermes line that is no longer streaming (History entries:
-- {role,id,t,text,status}, History.lua:5).
function Notes.LatestReply()
    local list = ns.History and ns.History:All() or {}
    for i = #list, 1, -1 do
        local e = list[i]
        if type(e) == "table" and e.role == "hermes"
            and not Notes.WORKING[e.status or ""]
            and type(e.text) == "string" and e.text ~= "" then
            return e
        end
    end
    return nil
end

-- Save the newest finished reply, stamped with character and session.
function Notes:SaveLatest()
    local entry = Notes.LatestReply()
    if not entry then return nil, "no-reply" end
    local d = db()
    return self:Add({
        id = entry.id,
        t = entry.t,
        text = entry.text,
        char = d and d.char,
        session = d and d.session,
    })
end
