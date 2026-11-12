--[[---------------------------------------------------------------------------
    WoWHermes / MinimapButton.lua — a round Blizzard-style button on the
    Minimap edge. No external library (LibDBIcon is not vendored).

    Blizzard assets, all referenced by Forever's own FrameXML (grepped
    2026-11-09 in wow-ui-source-forever):
      * Interface\\Minimap\\MiniMap-TrackingBorder   (Blizzard_Minimap/Mainline/Minimap.xml:272 area,
        Blizzard_WorldMap/Mainline/Blizzard_WorldMapTemplates.xml:49)
      * Interface\\Minimap\\UI-Minimap-Background    (Blizzard_WorldMapTemplates.xml:32)
      * Interface\\Minimap\\UI-Minimap-ZoomButton-Highlight (Minimap.xml:272, alphaMode ADD)
    Template: MiniMapButtonTemplate is loaded for camelot by
    Blizzard_Minimap/Blizzard_Minimap.toc:8-9
    (`Shared\\MinimapButtonTemplate.lua/.xml [AllowLoadGameType classic, camelot]`)
    and only provides the classic press nudge on OnMouseDown/OnMouseUp; it needs
    a texture named <ButtonName>Icon, which we create.

    Placement: the button hangs on the minimap edge at an angle in degrees,
    0 = straight up, increasing clockwise. The angle is persisted in
    WoWHermesDB.settings.minimapAngle, so a drag survives /reload.
--------------------------------------------------------------------------------]]--

local _, ns = ...

local MinimapButton = {}
ns.MinimapButton = MinimapButton

MinimapButton.NAME = "WoWHermesMinimapButton"
MinimapButton.SIZE = 31     -- LibDBIcon geometry: 31 px button, 53 px TOPLEFT ring
MinimapButton.RADIUS_PAD = 5  -- button centre sits 5 px outside the minimap edge
MinimapButton.DEFAULT_ANGLE = 200   -- lower-left-ish, clear of the Blizzard buttons
MinimapButton.FALLBACK_RADIUS = 70  -- used only while the Minimap has no size yet

local TEX_BORDER = "Interface\\Minimap\\MiniMap-TrackingBorder"
local TEX_BACKGROUND = "Interface\\Minimap\\UI-Minimap-Background"
local TEX_HIGHLIGHT = "Interface\\Minimap\\UI-Minimap-ZoomButton-Highlight"
local TEX_ICON_FALLBACK = "Interface\\AddOns\\WoWHermes\\media\\hermes"

local function settings()
    local d = WoWHermesDB
    return d and d.settings
end

local function minimap()
    local m = _G.Minimap
    if m and type(m.GetWidth) == "function" then return m end
    return nil
end

-- icon from the TOC (# IconTexture:), with a hard fallback
function MinimapButton.IconTexture()
    if type(_G.C_AddOns) == "table" and type(_G.C_AddOns.GetAddOnMetadata) == "function" then
        local ok, tex = pcall(_G.C_AddOns.GetAddOnMetadata, "WoWHermes", "IconTexture")
        if ok and type(tex) == "string" and tex ~= "" then return tex end
    end
    return TEX_ICON_FALLBACK
end

-- Pure geometry, so the specs can check it without a client.
-- angle: degrees, 0 = top, clockwise. radius: minimap radius in UI units.
function MinimapButton.PositionFor(angle, radius)
    local rad = math.rad(tonumber(angle) or MinimapButton.DEFAULT_ANGLE)
    radius = tonumber(radius) or MinimapButton.FALLBACK_RADIUS
    return radius * math.sin(rad), radius * math.cos(rad)
end

function MinimapButton:GetAngle()
    local s = settings()
    local angle = s and s.minimapAngle
    if type(angle) ~= "number" then angle = self.DEFAULT_ANGLE end
    return angle % 360
end

function MinimapButton:SetAngle(angle)
    local s = settings()
    if type(angle) ~= "number" then return end
    angle = angle % 360
    if s then s.minimapAngle = angle end
    self:UpdatePosition()
end

function MinimapButton:Radius()
    local map = minimap()
    local width = map and map:GetWidth() or 0
    if type(width) ~= "number" or width <= 0 then return self.FALLBACK_RADIUS end
    return width / 2 + self.RADIUS_PAD
end

function MinimapButton:UpdatePosition()
    local button = self.button
    local map = minimap()
    if not button or not map then return end
    local x, y = self.PositionFor(self:GetAngle(), self:Radius())
    button:ClearAllPoints()
    button:SetPoint("CENTER", map, "CENTER", x, y)
end

-- Angle under the cursor, measured the same way as PositionFor.
function MinimapButton.CursorAngle()
    local map = minimap()
    if not map or type(_G.GetCursorPosition) ~= "function" then return nil end
    local ok, cx, cy = pcall(_G.GetCursorPosition)
    if not ok or type(cx) ~= "number" or type(cy) ~= "number" then return nil end
    local centerX, centerY
    if type(map.GetCenter) == "function" then
        centerX, centerY = map:GetCenter()
    end
    if type(centerX) ~= "number" or type(centerY) ~= "number" then return nil end
    local scale = 1
    if type(map.GetEffectiveScale) == "function" then
        local eff = map:GetEffectiveScale()
        if type(eff) == "number" and eff > 0 then scale = eff end
    end
    local dx = (cx / scale) - centerX
    local dy = (cy / scale) - centerY
    if dx == 0 and dy == 0 then return nil end
    return math.deg(math.atan2(dx, dy)) % 360
end

function MinimapButton:OnUpdate()
    if not self.dragging then return end
    local angle = MinimapButton.CursorAngle()
    if not angle then return end
    if math.abs(angle - self:GetAngle()) > 1 then
        if self.button then self.button.__woWHermesDragMoved = true end
    end
    self:SetAngle(angle)
end

function MinimapButton:OnDragStart()
    self.dragging = true
    if self.button then
        self.button.__woWHermesDragMoved = false
        self.button:SetScript("OnUpdate", function() MinimapButton:OnUpdate() end)
    end
end

function MinimapButton:OnDragStop()
    self.dragging = false
    if self.button then self.button:SetScript("OnUpdate", nil) end
end

function MinimapButton:OnClick(mouseButton)
    local button = self.button
    if self.dragging then return end
    if button and button.__woWHermesDragMoved then
        -- The click that ends a drag must not toggle anything.
        button.__woWHermesDragMoved = false
        return
    end
    if mouseButton == "RightButton" then
        ns.Options:Open()
        return
    end
    ns.Display:Toggle()
end

function MinimapButton.ShowTooltip(owner)
    local tip = _G.GameTooltip
    local display = ns.Display
    if not tip or not display then return end
    tip:SetOwner(owner, "ANCHOR_LEFT")
    tip:SetText("WoW Hermes")
    local r, g, b, status = display.StatusColor()
    tip:AddLine("Bridge: " .. tostring(status), r, g, b)
    if ns.Bridge then
        tip:AddLine("Slots: " .. tostring(ns.Bridge.slotsTotal) ..
            "  ·  waiting: " .. tostring(ns.Inbox and ns.Inbox:PendingCount() or 0), 1, 1, 1)
    end
    tip:AddLine("/h <text> sends a message", 1, 1, 1)
    tip:AddLine("Left-click: Hermes window · Right-click: settings", 1, 1, 1)
    tip:Show()
end

function MinimapButton:Ensure()
    if self.button then return self.button end
    local map = minimap()
    local parent = map or _G.UIParent
    if not parent then return nil end

    local button = CreateFrame("Button", self.NAME, parent, "MiniMapButtonTemplate")
    button:SetSize(self.SIZE, self.SIZE)
    button:SetFrameLevel(8)
    -- The Minimap cluster sits at LOW; a MEDIUM button stays clickable above it.
    button:SetFrameStrata("MEDIUM")
    button:SetClampedToScreen(true)
    button:SetMovable(true)
    button:RegisterForClicks("AnyUp")
    button:RegisterForDrag("LeftButton")

    -- MiniMap-TrackingBorder's ring sits in the TOP-LEFT of its 64x64 art, so it is anchored
    -- TOPLEFT, never CENTER (in-game round 4: CENTER put the ring up-left of the icon). Same
    -- layout as Blizzard's own uses (Deprecated_ArenaUI.xml:233, AzeriteEssenceUI.xml:574) and
    -- LibDBIcon: background 20 @ (7,-5), icon 17 @ (7,-6), ring 53 @ TOPLEFT.
    local background = button:CreateTexture(nil, "BACKGROUND")
    background:SetTexture(TEX_BACKGROUND)
    background:SetSize(20, 20)
    background:SetPoint("TOPLEFT", button, "TOPLEFT", 7, -5)

    local icon = button:CreateTexture(self.NAME .. "Icon", "ARTWORK")
    icon:SetTexture(self.IconTexture())
    icon:SetSize(17, 17)
    icon:SetPoint("TOPLEFT", button, "TOPLEFT", 7, -6)
    button.icon = icon

    local border = button:CreateTexture(nil, "OVERLAY")
    border:SetTexture(TEX_BORDER)
    border:SetSize(53, 53)
    border:SetPoint("TOPLEFT", button, "TOPLEFT", 0, 0)

    local highlight = button:CreateTexture(nil, "HIGHLIGHT")
    highlight:SetTexture(TEX_HIGHLIGHT)
    highlight:SetBlendMode("ADD")
    highlight:SetAllPoints()

    button:SetScript("OnClick", function(_, mouseButton)
        MinimapButton:OnClick(mouseButton)
    end)
    button:SetScript("OnDragStart", function()
        MinimapButton:OnDragStart()
    end)
    button:SetScript("OnDragStop", function()
        MinimapButton:OnDragStop()
    end)
    -- clears a stale drag flag on the next press (HookScript keeps the
    -- template's own press nudge intact)
    button:HookScript("OnMouseDown", function()
        button.__woWHermesDragMoved = false
    end)
    button:SetScript("OnEnter", function(self2)
        MinimapButton.ShowTooltip(self2)
    end)
    button:SetScript("OnLeave", function()
        if _G.GameTooltip then _G.GameTooltip:Hide() end
    end)

    if map and type(map.HookScript) == "function" then
        map:HookScript("OnSizeChanged", function()
            MinimapButton:UpdatePosition()
        end)
    end

    self.button = button
    self:UpdatePosition()
    return button
end

-- Create/show or hide according to settings.minimapButton (default on).
function MinimapButton:Apply()
    local s = settings()
    local wanted = not (s and s.minimapButton == false)
    if not wanted then
        if self.button then self.button:Hide() end
        return false
    end
    local button = self:Ensure()
    if not button then return false end
    self:UpdatePosition()
    button:Show()
    return true
end

function MinimapButton:Init()
    return self:Apply()
end

MinimapButton.TEXTURES = {
    border = TEX_BORDER,
    background = TEX_BACKGROUND,
    highlight = TEX_HIGHLIGHT,
}
