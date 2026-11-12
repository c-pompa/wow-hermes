--[[---------------------------------------------------------------------------
    WoWHermes / Format.lua — WoW-native text styling for the transcript and the
    Hermes chat tab (Markdown-lite, display only).

    Rules:
      * Every string that came from the player or the bridge passes through
        Sanitize() FIRST: a literal `|` becomes `||` (the client's own escape
        for a pipe, see Blizzard_GlueXML/Mainline/AccountLogin.lua:213). Only
        this module then inserts real escape sequences, so message text can
        never inject a colour, texture or font sequence of its own.
      * Escape codes are ARGB hex `|cffRRGGBB ... |r`; nested `|r` pops back to
        the previous colour on Forever (wow-api-escape-sequences baseline).
      * Nothing here is animated: ChatTab uses Plain(), the window uses Lines().
--------------------------------------------------------------------------------]]--

local _, ns = ...

local Format = {}
ns.Format = Format

-- 0xRRGGBB spelled out for readability; all escapes start with `|cff`.
Format.COLORS = {
    user   = "|cff9ed9ff",   -- 0.62, 0.85, 1.00
    hermes = "|cff8cffa6",   -- 0.55, 1.00, 0.65
    error  = "|cffff6060",
    gold   = "|cffffd100",
    code   = "|cff9fe8a0",
    url    = "|cff7fbfff",
    grey   = "|cff8a8a8a",
    pin    = "|cff66ccff",   -- map coordinate links
}

-- Literal pipe -> the client's escaped pipe. Applied before any styling.
function Format.Sanitize(text)
    if text == nil then return "" end
    return (tostring(text):gsub("|", "||"))
end

-- ------------------------------------------------ map coordinates -> links
-- WoW map coordinates in a reply become a custom addon hyperlink
-- (`LinkTypes.AddOn = "addon"`, Blizzard_SharedXML/LinkUtil.lua:4). Detection
-- runs on ALREADY SANITIZED text: the only real escapes in the output are the
-- ones this module inserts.
--
-- A pair is two numbers in 0..100 separated by a comma, with optional
-- parentheses: "44.7, 44.5", "(44.7, 44.5)", "44.7,44.5", "44, 45".
-- Rejected on purpose (false positives seen in real replies):
--   * thousands separators / money / quantities: "1,500" (500 > 100),
--     "3, 000" (leading zero), "9s 50c" (no comma)
--   * a number that is part of a longer token: "144.7, 44.5", "44.7.1, 44.5"
--   * anything inside a URL: "https://x/44.7,44.5"
--   * a percentage pair ("44.7, 45%") and anything followed by a unit letter
Format.PIN_LINK = "addon:WoWHermes:pin:"
Format.COORD_MAX = 100

local COORD_NUM = "[%d]+%.?[%d]*"
local COORD_PAIR = "(" .. COORD_NUM .. ")%s*,%s*(" .. COORD_NUM .. ")"
local UNSAFE_BEFORE = "[%w_%./%-:]"
local UNSAFE_AFTER = "[%w_/%%%-]"

-- A "." right after a pair is a sentence period unless a digit follows it
-- ("44.7.1" is a version, "at 44.5." ends a sentence).
local function badAfter(text, e)
    local after = text:sub(e + 1, e + 1)
    if after == "" then return false end
    if after == "." then return text:sub(e + 2, e + 2):match("%d") ~= nil end
    return after:match(UNSAFE_AFTER) ~= nil
end

-- "000", "050", "00" are thousands groups or padding, never a coordinate; a
-- trailing "." is not a number the client would accept either.
local function badToken(tok)
    if tok == "" or tok:sub(-1) == "." then return true end
    if #tok > 1 and tok:sub(1, 1) == "0" then return true end
    local v = tonumber(tok)
    return v == nil or v < 0 or v > Format.COORD_MAX
end

-- True when the character run immediately before `s` is part of a URL
-- (scheme + "://" on the same whitespace-delimited token).
local function inUrl(text, s)
    local i = s - 1
    while i >= 1 do
        local c = text:sub(i, i)
        if c:match("%s") then return false end
        if c == ":" and text:sub(i, i + 2) == "://" then return true end
        i = i - 1
    end
    return false
end

-- Ordered list of { s, e, x, y, label } pairs; x/y are the raw tokens (so the
-- link displays what the reply said) and label is "x, y".
function Format.ScanPins(text)
    local found = {}
    if type(text) ~= "string" or text == "" then return found end
    local pos = 1
    while pos <= #text do
        local s, e, a, b = text:find(COORD_PAIR, pos)
        if not s then break end
        -- `[%d]+%.?[%d]*` swallows a sentence period after the second number
        -- ("at 30, 40.") — give the period back to the sentence.
        if b:sub(-1) == "." then
            b, e = b:sub(1, -2), e - 1
        end
        local start, stop = s, e
        local open = text:sub(s - 1, s - 1) == "("
        local close = text:sub(e + 1, e + 1) == ")"
        if open and close then start, stop = s - 1, e + 1 end
        local before = text:sub(start - 1, start - 1)
        local ok = not badToken(a) and not badToken(b)
            and not (before ~= "" and before:match(UNSAFE_BEFORE))
            and not badAfter(text, stop)
            and not inUrl(text, start)
        if ok then
            found[#found + 1] = { s = start, e = stop, x = a, y = b, label = a .. ", " .. b }
            pos = stop + 1
        else
            pos = s + 1
        end
    end
    return found
end

-- Coordinates of a message body as numbers, in reading order (the Pin button).
function Format.PinPairs(text)
    local pairsList = {}
    for _, pin in ipairs(Format.ScanPins(text)) do
        pairsList[#pairsList + 1] = { x = tonumber(pin.x), y = tonumber(pin.y), label = pin.label }
    end
    return pairsList
end

-- One clickable coordinate: the link target carries the two raw values; the
-- visible text is the bracketed pair in the pin colour.
function Format.PinLink(x, y)
    local label = tostring(x) .. ", " .. tostring(y)
    return Format.COLORS.pin .. "|H" .. Format.PIN_LINK .. tostring(x) .. ":" .. tostring(y)
        .. "|h[" .. label .. "]|h|r"
end

local function applyPins(text)
    local pins = Format.ScanPins(text)
    if #pins == 0 then return text end
    local out, pos = {}, 1
    for _, pin in ipairs(pins) do
        out[#out + 1] = text:sub(pos, pin.s - 1)
        out[#out + 1] = Format.PinLink(pin.x, pin.y)
        pos = pin.e + 1
    end
    out[#out + 1] = text:sub(pos)
    return table.concat(out)
end

-- Inline styles on already-sanitized text: `code`, URLs, coordinates, **bold**.
local function inline(text)
    text = text:gsub("`([^`\n]+)`", Format.COLORS.code .. "%1|r")
    text = text:gsub("(https?://[%w%-%._~:/?%#%[%]@!$&'()*+,;=%%]+)",
        Format.COLORS.url .. "%1|r")
    text = applyPins(text)
    text = text:gsub("%*%*(.-)%*%*", Format.COLORS.gold .. "%1|r")
    return text
end
Format.Inline = inline

-- A whole-line header: `## Title`, `# Title` or a line that is entirely bold
-- (`**Title**`, optionally ending in `:`). Returns the title text or nil.
local function headerTitle(line)
    local hash = line:match("^%s*#+%s+(.-)%s*$")
    if hash and hash ~= "" then return hash end
    local bold = line:match("^%s*%*%*(.-)%*%*%s*:?%s*$")
    if bold and bold ~= "" then return bold end
    return nil
end

-- Render a message body as an array of visual lines (one transcript message
-- each). Blank lines survive as a single space so paragraphs keep their gap.
function Format.Lines(text)
    local source = Format.Sanitize(text)
    local out = {}
    local inCode = false
    for raw in (source .. "\n"):gmatch("([^\n]*)\n") do
        local line = raw:gsub("\r$", "")
        if line:match("^%s*```") then
            inCode = not inCode
        elseif inCode then
            out[#out + 1] = Format.COLORS.code .. "  " .. line .. "|r"
        elseif line:match("^%s*$") then
            out[#out + 1] = " "
        else
            local title = headerTitle(line)
            local bullet = line:match("^%s*[%-%*]%s+(.+)$")
            local number, item = line:match("^%s*(%d+)[%.%)]%s+(.+)$")
            if title then
                out[#out + 1] = Format.COLORS.gold .. title .. "|r"
            elseif bullet then
                out[#out + 1] = "  " .. Format.COLORS.gold .. "\226\128\162|r " .. inline(bullet)
            elseif number then
                out[#out + 1] = "  " .. Format.COLORS.gold .. number .. ".|r " .. inline(item)
            else
                out[#out + 1] = inline(line)
            end
        end
    end
    -- a trailing newline in the source must not add an empty last line
    if #out > 0 and out[#out] == " " and source:sub(-1) == "\n" then
        out[#out] = nil
    end
    if #out == 0 then out[1] = " " end
    return out
end

-- One-line plain rendering for chat surfaces (no animation, no markers).
function Format.Plain(text)
    local source = Format.Sanitize(text)
    source = source:gsub("%*%*", ""):gsub("`", "")
    source = source:gsub("\r", "")
    source = source:gsub("%s*\n%s*", " \194\183 ")
    return source
end

-- Thinking indicator: ASCII spinner + elapsed seconds, all safely escaped.
Format.SPINNER = { "|", "/", "-", "\\" }
Format.SPIN_INTERVAL = 0.4

function Format.SpinnerPhase(now)
    local step = math.floor((now or 0) / Format.SPIN_INTERVAL)
    return (step % #Format.SPINNER) + 1
end

function Format.Thinking(seconds, phase)
    local frame = Format.SPINNER[phase or 1] or Format.SPINNER[1]
    local secs = math.max(0, math.floor(tonumber(seconds) or 0))
    return Format.COLORS.grey .. Format.Sanitize(frame) .. "|r "
        .. Format.COLORS.hermes .. "Hermes is thinking|r "
        .. Format.COLORS.grey .. "(" .. secs .. "s)|r"
end
