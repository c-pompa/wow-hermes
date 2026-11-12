--[[---------------------------------------------------------------------------
    WoWHermes / Codec.lua — the wire format, mirrored from protocol/PROTOCOL.md
    and protocol/wowhermes_protocol.py.

    Layers:
      record  payload text: fields joined by US (0x1F), records by RS (0x1E)
      frame   magic C7 1A | version | id (u16 BE) | len (u16 BE) | payload | fletcher16
      cells   frame bytes -> MSB-first 3-bit groups -> values 0..7 (one cell each)

    Pure PUC Lua 5.1: no `bit` library (tests run outside the client), no
    bit32/utf8. Strings are byte strings in both WoW and PUC Lua 5.1, so UTF-8
    round-trips byte-exact without any encoding step.
--------------------------------------------------------------------------------]]

local _, ns = ...

local Codec = {}
ns.Codec = Codec

Codec.MAGIC1 = 0xC7
Codec.MAGIC2 = 0x1A
Codec.VERSION = 1
Codec.MAX_PAYLOAD = 3200
Codec.US = "\31"
Codec.RS = "\30"
Codec.CELLS_PER_ROW = 200
Codec.CELL_PX = 4
Codec.MAX_ROWS = 48

local US, RS = Codec.US, Codec.RS

-- ------------------------------------------------------------------ helpers

local function pow2(n)
    return 2 ^ n
end

-- bit 2 = R, bit 1 = G, bit 0 = B; each channel is 0 or 1 (SetColorTexture
-- takes 0..1 floats, PROTOCOL A.3).
function Codec.cell_rgb(v)
    v = v % 8
    local r = (v >= 4) and 1 or 0
    local g = (v % 4 >= 2) and 1 or 0
    local b = (v % 2 >= 1) and 1 or 0
    return r, g, b
end

-- Inverse of cell_rgb for tests and for the `probe` command.
function Codec.rgb_cell(r, g, b)
    local v = 0
    if r >= 0.5 then v = v + 4 end
    if g >= 0.5 then v = v + 2 end
    if b >= 0.5 then v = v + 1 end
    return v
end

function Codec.sanitize(text)
    if text == nil then return "" end
    return (tostring(text):gsub(US, " "):gsub(RS, " "))
end

local function contains_separator(s)
    return s:find(US, 1, true) ~= nil or s:find(RS, 1, true) ~= nil
end

-- ------------------------------------------------------------------ records

-- encode_record(session, chat, id, kind, flags, text) -> payload bytes
function Codec.encode_record(session, chat, msgId, kind, flags, text)
    local fields = { tostring(session), tostring(chat), tostring(msgId), tostring(kind), flags or "" }
    for i = 1, 5 do
        if contains_separator(fields[i]) then
            error("separator in header field " .. i, 2)
        end
    end
    fields[6] = Codec.sanitize(text)
    return table.concat(fields, US)
end

-- records: array of { session, chat, id, kind, flags, text }
function Codec.encode_records(records)
    local parts = {}
    for i = 1, #records do
        local r = records[i]
        parts[i] = Codec.encode_record(r.session, r.chat, r.id, r.kind, r.flags, r.text)
    end
    return table.concat(parts, RS)
end

function Codec.decode_record(payload)
    local out = {}
    local pos = 1
    for i = 1, 6 do
        local sep = payload:find(US, pos, true)
        if i < 6 then
            if not sep then return nil, "record has fewer than 6 fields" end
            out[i] = payload:sub(pos, sep - 1)
            pos = sep + 1
        else
            out[i] = payload:sub(pos)
        end
    end
    return {
        session = out[1],
        chat = out[2],
        id = tonumber(out[3]) or 0,
        kind = out[4],
        flags = out[5],
        text = out[6],
    }
end

function Codec.decode_records(payload)
    local list = {}
    for part in (payload .. RS):gmatch("(.-)" .. RS) do
        if part ~= "" then
            local rec, err = Codec.decode_record(part)
            if not rec then return nil, err end
            list[#list + 1] = rec
        end
    end
    return list
end

-- "k=v;k=v" (hello and context text). Values may not contain ';'.
function Codec.parse_kv(text)
    local out = {}
    for item in (tostring(text or "") .. ";"):gmatch("(.-);") do
        if item ~= "" then
            local k, v = item:match("^([^=]*)=(.*)$")
            if k then out[k] = v else out[item] = "" end
        end
    end
    return out
end

function Codec.format_kv(pairs)
    -- pairs: array of {key, value}; skips nil values
    local out = {}
    for i = 1, #pairs do
        local p = pairs[i]
        if p[2] ~= nil then
            out[#out + 1] = p[1] .. "=" .. Codec.sanitize(p[2])
        end
    end
    return table.concat(out, ";")
end

-- ------------------------------------------------------------------ fletcher

-- Fletcher-16 mod 255 over data[first..last]. Returns s1, s2.
function Codec.fletcher16(data, first, last)
    first = first or 1
    last = last or #data
    local s1, s2 = 0, 0
    for i = first, last do
        s1 = (s1 + data:byte(i)) % 255
        s2 = (s2 + s1) % 255
    end
    return s1, s2
end

-- ------------------------------------------------------------------ frame

local function be16(n)
    n = n % 65536
    return math.floor(n / 256), n % 256
end

-- encode_frame(id, payload) -> frame bytes. Raises on an invalid size/id.
function Codec.encode_frame(frameId, payload)
    local len = #payload
    if len < 1 or len > Codec.MAX_PAYLOAD then
        error(("payload length %d outside 1..%d"):format(len, Codec.MAX_PAYLOAD), 2)
    end
    if frameId < 0 or frameId > 0xFFFF then
        error("frame id must be uint16", 2)
    end
    local h1, h2 = be16(frameId)
    local l1, l2 = be16(len)
    local body = string.char(Codec.VERSION, h1, h2, l1, l2) .. payload
    local s1, s2 = Codec.fletcher16(body)
    return string.char(Codec.MAGIC1, Codec.MAGIC2) .. body .. string.char(s1, s2)
end

-- decode_frame(data) -> id, payload | nil, err
function Codec.decode_frame(data)
    if #data < 10 then return nil, "short frame" end
    if data:byte(1) ~= Codec.MAGIC1 or data:byte(2) ~= Codec.MAGIC2 then
        return nil, "bad magic"
    end
    local version = data:byte(3)
    if version ~= Codec.VERSION then
        return nil, "unsupported version " .. tostring(version)
    end
    local frameId = data:byte(4) * 256 + data:byte(5)
    local len = data:byte(6) * 256 + data:byte(7)
    if len < 1 or len > Codec.MAX_PAYLOAD then return nil, "bad length" end
    local tail = 7 + len
    if #data < tail + 2 then return nil, "truncated frame" end
    local s1, s2 = Codec.fletcher16(data, 3, tail)
    if s1 ~= data:byte(tail + 1) or s2 ~= data:byte(tail + 2) then
        return nil, "checksum mismatch"
    end
    return frameId, data:sub(8, tail)
end

-- ------------------------------------------------------------------ cells

function Codec.cell_count(payloadLen)
    return math.ceil(8 * (9 + payloadLen) / 3)
end

function Codec.row_count(payloadLen)
    return math.ceil(Codec.cell_count(payloadLen) / Codec.CELLS_PER_ROW)
end

-- MSB-first 3-bit groups; the final group is zero-padded on the right.
-- Arithmetic only — no bit library.
function Codec.bytes_to_cells(data)
    local cells = {}
    local acc, n = 0, 0
    for i = 1, #data do
        local b = data:byte(i)
        for shift = 7, 0, -1 do
            acc = acc * 2 + math.floor(b / pow2(shift)) % 2
            n = n + 1
            if n == 3 then
                cells[#cells + 1] = acc
                acc, n = 0, 0
            end
        end
    end
    if n > 0 then
        cells[#cells + 1] = acc * pow2(3 - n)
    end
    return cells
end

function Codec.cells_to_bytes(cells)
    local out = {}
    local acc, n = 0, 0
    for i = 1, #cells do
        local v = cells[i] % 8
        for shift = 2, 0, -1 do
            acc = acc * 2 + math.floor(v / pow2(shift)) % 2
            n = n + 1
            if n == 8 then
                out[#out + 1] = string.char(acc)
                acc, n = 0, 0
            end
        end
    end
    return table.concat(out)
end

-- Top-left pixel offset of cell i (x right, y down) at pitch 1 (A.3).
function Codec.cell_xy(i)
    local idx = i - 1
    return (idx % Codec.CELLS_PER_ROW) * Codec.CELL_PX,
        math.floor(idx / Codec.CELLS_PER_ROW) * Codec.CELL_PX
end

function Codec.hex(data)
    local out = {}
    for i = 1, #data do out[i] = ("%02x"):format(data:byte(i)) end
    return table.concat(out)
end

function Codec.from_hex(hex)
    local out = {}
    for byte in hex:gmatch("%x%x") do out[#out + 1] = string.char(tonumber(byte, 16)) end
    return table.concat(out)
end
