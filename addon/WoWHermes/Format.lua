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
}

-- Literal pipe -> the client's escaped pipe. Applied before any styling.
function Format.Sanitize(text)
    if text == nil then return "" end
    return (tostring(text):gsub("|", "||"))
end

-- Inline styles on already-sanitized text: `code`, URLs, **bold**.
local function inline(text)
    text = text:gsub("`([^`\n]+)`", Format.COLORS.code .. "%1|r")
    text = text:gsub("(https?://[%w%-%._~:/?%#%[%]@!$&'()*+,;=%%]+)",
        Format.COLORS.url .. "%1|r")
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
