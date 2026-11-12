--[[---------------------------------------------------------------------------
    WoWHermes / Strip.lua — the outbound pixel strip (PROTOCOL A.3).

    200 cells per row, each a 4x4 physical-pixel square. The frame lives on
    UIParent at TOOLTIP strata / level 10000, ignores the parent's scale and
    re-scales itself so that one frame unit is exactly one physical pixel:

        scale = 768 / physicalHeight      (GET physical screen size, not UI size)

    PROTOCOL A.3 "Units = pixels": with SetIgnoreParentScale(true) and that
    scale, the frame's effective scale equals Blizzard's pixel-to-UI factor
    (`PixelUtil.GetPixelToUIUnitFactor()` = 768/physicalHeight), so 1 unit ==
    1 pixel. Sizes and offsets are passed to SetSize/SetPoint as-is — never
    divided or multiplied by the scale.

    Plain textures only: nothing here is protected, so drawing during combat is
    allowed and nothing needs to be re-anchored on PLAYER_REGEN_ENABLED.
--------------------------------------------------------------------------------]]

local _, ns = ...

local Codec = ns.Codec
local Strip = {}
ns.Strip = Strip

Strip.WIDTH = Codec.CELLS_PER_ROW * Codec.CELL_PX   -- 800 physical px
Strip.REFERENCE_HEIGHT = 768                        -- scale reference (A.3)

Strip.frame = nil
Strip.textures = {}
Strip.rows = 0
Strip.cells = {}
Strip.wanted = false

local function physicalHeight()
    if type(GetPhysicalScreenSize) ~= "function" then return nil end
    local _, h = GetPhysicalScreenSize()
    if type(h) ~= "number" or h <= 0 then return nil end
    return h
end

function Strip:GetScale()
    local h = physicalHeight()
    if not h then return 1 end
    return self.REFERENCE_HEIGHT / h
end

-- A.3 is written in physical pixels *and* in frame units, which coincide here:
-- SetIgnoreParentScale(true) + SetScale(768/physH) makes 1 unit == 1 pixel.
-- Kept for /h selftest: screen pixels per frame unit (1.0 when correct).
function Strip:PixelsPerUnit()
    local h = physicalHeight()
    if not h then return 1 end
    return self:GetScale() * h / self.REFERENCE_HEIGHT
end

-- (Re)apply scale, size and every texture position. Safe to call any time.
function Strip:Layout()
    local f = self.frame
    if not f then return end
    f:SetScale(self:GetScale())
    f:SetSize(self.WIDTH, math.max(Codec.CELL_PX, self.rows * Codec.CELL_PX))
    for i, tex in ipairs(self.textures) do
        self:PlaceTexture(tex, i)
    end
    if self.debugBorder and self.debugFrame then
        self.debugFrame:SetAllPoints(f)
        self.debugFrame:Show()
    elseif self.debugFrame then
        self.debugFrame:Hide()
    end
end

-- Size and position one cell texture for its index. Units == pixels.
function Strip:PlaceTexture(tex, index)
    tex:SetSize(Codec.CELL_PX, Codec.CELL_PX)
    tex:ClearAllPoints()
    local x, y = Codec.cell_xy(index)          -- physical offsets inside the strip
    tex:SetPoint("TOPLEFT", self.frame, "TOPLEFT", x, -y)
end

local function ensureFrame()
    if Strip.frame then return Strip.frame end

    local f = CreateFrame("Frame", "WoWHermesStrip", UIParent)
    f:SetFrameStrata("TOOLTIP")
    f:SetFrameLevel(10000)
    f:SetIgnoreParentScale(true)
    f:SetPoint("TOPLEFT", 0, 0)
    f:SetSize(Strip.WIDTH, Codec.CELL_PX)
    f:Hide()
    f:RegisterEvent("UI_SCALE_CHANGED")
    f:RegisterEvent("DISPLAY_SIZE_CHANGED")
    f:SetScript("OnEvent", function()
        Strip:Layout()
    end)
    Strip.frame = f
    return f
end

local function ensureTexture(index)
    local tex = Strip.textures[index]
    if tex then return tex end
    local f = ensureFrame()
    tex = f:CreateTexture(nil, "ARTWORK")
    -- No resampling: a cell must land on whole physical pixels.
    tex:SetSnapToPixelGrid(false)
    tex:SetTexelSnappingBias(0)
    tex:Hide()
    Strip.textures[index] = tex
    Strip:PlaceTexture(tex, index)
    return tex
end

-- cells: array of 0..7, rows: row count for sizing. Draws and shows the strip.
function Strip:ShowCells(cells, rows)
    ensureFrame()
    local count = #cells
    for i = 1, count do
        local tex = ensureTexture(i)
        local r, g, b = Codec.cell_rgb(cells[i])
        tex:SetColorTexture(r, g, b, 1)
        tex:Show()
    end
    for i = count + 1, #self.textures do
        self.textures[i]:Hide()
    end
    self.cells = cells
    self.rows = math.max(1, rows or Codec.row_count(math.max(1, count)))
    self.wanted = true
    self:Layout()
    self.frame:Show()
end

-- Draw a whole frame (magic + header + payload + checksum).
function Strip:ShowFrame(frameBytes, payloadLen)
    local cells = Codec.bytes_to_cells(frameBytes)
    local len = payloadLen or (#frameBytes - 9)
    self:ShowFramePayload(cells, len)
end

function Strip:ShowFramePayload(cells, payloadLen)
    self:ShowCells(cells, Codec.row_count(payloadLen))
end

function Strip:Hide()
    self.wanted = false
    if self.frame then self.frame:Hide() end
    if self.debugFrame then self.debugFrame:Hide() end
end

function Strip:IsShown()
    return self.frame ~= nil and self.frame:IsShown()
end

function Strip:SetDebugBorder(on)
    self.debugBorder = on and true or false
    ensureFrame()
    if self.debugBorder and not self.debugFrame and self.frame then
        local f = CreateFrame("Frame", nil, self.frame)
        f:SetPoint("TOPLEFT", self.frame, "TOPLEFT", -1, 1)
        f:SetPoint("BOTTOMRIGHT", self.frame, "BOTTOMRIGHT", 1, -1)
        local lines = {}
        for i = 1, 4 do
            local t = f:CreateTexture(nil, "OVERLAY")
            t:SetColorTexture(0, 1, 0, 0.9)
            lines[i] = t
        end
        -- top / bottom / left / right, 1 physical px thick (units == pixels)
        lines[1]:SetPoint("TOPLEFT", f, "TOPLEFT")
        lines[1]:SetPoint("TOPRIGHT", f, "TOPRIGHT")
        lines[1]:SetHeight(1)
        lines[2]:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT")
        lines[2]:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT")
        lines[2]:SetHeight(1)
        lines[3]:SetPoint("TOPLEFT", f, "TOPLEFT")
        lines[3]:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT")
        lines[3]:SetWidth(1)
        lines[4]:SetPoint("TOPRIGHT", f, "TOPRIGHT")
        lines[4]:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT")
        lines[4]:SetWidth(1)
        self.debugFrame = f
    end
    if self.debugFrame then
        if self.debugBorder and self:IsShown() then self.debugFrame:Show() else self.debugFrame:Hide() end
    end
end

-- The `/h probe` test pattern: eight known colours in the first row, so a wrong
-- scale, origin or channel order is visible immediately.
function Strip:Probe()
    local cells = {}
    for col = 0, Codec.CELLS_PER_ROW - 1 do
        cells[col + 1] = col % 8
        cells[Codec.CELLS_PER_ROW + col + 1] = 7 - (col % 8)
    end
    self:ShowCells(cells, 2)
end
