--[[---------------------------------------------------------------------------
    WoWHermes / Options.lua — native Settings category "WoW Hermes".

    Uses the Settings API documented by Blizzard in
    Blizzard_Settings_Shared/Blizzard_ImplementationReadme.lua (Forever):
    RegisterVerticalLayoutCategory + RegisterProxySetting + Create{Checkbox,
    Dropdown,Slider} + RegisterAddOnCategory.
--------------------------------------------------------------------------------]]

local _, ns = ...

local Options = {}
ns.Options = Options

Options.category = nil

local function settings()
    local d = WoWHermesDB
    return d and d.settings
end

local VARIABLES = {
    transport = "WOWHERMES_TRANSPORT",
    sendContext = "WOWHERMES_SEND_CONTEXT",
    stripDebug = "WOWHERMES_STRIP_DEBUG",
    opacity = "WOWHERMES_OPACITY",
    minimapButton = "WOWHERMES_MINIMAP_BUTTON",
    chatTab = "WOWHERMES_CHAT_TAB",
    fadeOnMove = "WOWHERMES_FADE_ON_MOVE",
}

-- 1 = strip (default), 2 = reload-only
local function getTransport()
    local s = settings()
    return (s and s.transport == "reload") and 2 or 1
end

local function setTransport(value)
    local s = settings()
    if not s then return end
    local transport = (value == 2) and "reload" or "strip"
    if s.transport == transport then return end
    s.transport = transport
    if ns.Outbox and ns.Outbox.OnTransportChanged then ns.Outbox:OnTransportChanged() end
    if ns.Print then
        ns.Print(s.transport == "reload"
            and "Reload transport: messages are queued until you /reload."
            or "Strip transport: messages are drawn for the bridge to capture.")
    end
end

local function getSendContext()
    local s = settings()
    return (s == nil) or (s.sendContext ~= false)
end

local function setSendContext(value)
    local s = settings()
    if s then s.sendContext = value and true or false end
end

local function getStripDebug()
    local s = settings()
    return s and s.stripDebug == true
end

local function setStripDebug(value)
    local s = settings()
    if not s then return end
    s.stripDebug = value and true or false
    if ns.Strip and ns.Strip.SetDebugBorder then ns.Strip:SetDebugBorder(s.stripDebug) end
end

local function getOpacity()
    local s = settings()
    return (s and s.opacity) or 0.9
end

local function setOpacity(value)
    local s = settings()
    if not s then return end
    s.opacity = value
    if ns.Display then ns.Display:ApplyOpacity() end
end

local function getMinimapButton()
    local s = settings()
    return (s == nil) or (s.minimapButton ~= false)
end

local function setMinimapButton(value)
    local s = settings()
    if not s then return end
    s.minimapButton = value and true or false
    if ns.MinimapButton then ns.MinimapButton:Apply() end
end

local function getChatTab()
    local s = settings()
    return (s == nil) or (s.chatTab ~= false)
end

local function setChatTab(value)
    local s = settings()
    if not s then return end
    s.chatTab = value and true or false
    -- Turning it on again after the player closed the tab creates a fresh one;
    -- turning it off only stops the routing — the tab stays until it is closed
    -- (Blizzard owns chat-window lifetime and we do not force-close it).
    if s.chatTab and ns.ChatTab then
        s.chatTabCreated = nil
        ns.ChatTab:Apply()
    end
end

local function getFadeOnMove()
    local s = settings()
    return (s == nil) or (s.fadeOnMove ~= false)
end

local function setFadeOnMove(value)
    local s = settings()
    if not s then return end
    s.fadeOnMove = value and true or false
    -- Applies the restored opacity (or 0.25 while the player is moving) at once.
    if ns.Display then ns.Display:ApplyOpacity() end
end

function Options:Init()
    if self.category then return true end
    if not (Settings and Settings.RegisterVerticalLayoutCategory
            and Settings.RegisterProxySetting and Settings.RegisterAddOnCategory) then
        return false
    end

    local category = Settings.RegisterVerticalLayoutCategory("WoW Hermes")

    local transport = Settings.RegisterProxySetting(category, VARIABLES.transport,
        Settings.VarType.Number, "Transport", 1, getTransport, setTransport)
    Settings.CreateDropdown(category, transport, function()
        local container = Settings.CreateControlTextContainer()
        container:Add(1, "Pixel strip (fast)")
        container:Add(2, "Saved variables (type /reload to send)")
        return container:GetData()
    end, "How messages leave the game. The pixel strip is fast; the reload mode never draws on screen.")

    local context = Settings.RegisterProxySetting(category, VARIABLES.sendContext,
        Settings.VarType.Boolean, "Send game context", true, getSendContext, setSendContext)
    Settings.CreateCheckbox(category, context,
        "Attach zone, level, class, race, money and combat state to each message.")

    local debugBorder = Settings.RegisterProxySetting(category, VARIABLES.stripDebug,
        Settings.VarType.Boolean, "Show strip debug border", false, getStripDebug, setStripDebug)
    Settings.CreateCheckbox(category, debugBorder,
        "Outline the pixel strip so its position and size are easy to check.")

    local minimapButton = Settings.RegisterProxySetting(category, VARIABLES.minimapButton,
        Settings.VarType.Boolean, "Show minimap button", true, getMinimapButton, setMinimapButton)
    Settings.CreateCheckbox(category, minimapButton,
        "A round Hermes button on the edge of the minimap. Left-click opens the window, "
        .. "right-click opens these settings, drag it around the edge.")

    local chatTab = Settings.RegisterProxySetting(category, VARIABLES.chatTab,
        Settings.VarType.Boolean, "Show a Hermes chat tab", true, getChatTab, setChatTab)
    Settings.CreateCheckbox(category, chatTab,
        "Add a 'Hermes' tab next to General and Combat Log that shows your exchanges. "
        .. "If you close that tab yourself, the addon will not recreate it.")

    local opacity = Settings.RegisterProxySetting(category, VARIABLES.opacity,
        Settings.VarType.Number, "Window opacity", 0.9, getOpacity, setOpacity)
    local sliderOptions = Settings.CreateSliderOptions(0.3, 1.0, 0.05)
    local sliderMixin = rawget(_G, "MinimalSliderWithSteppersMixin")
    local sliderLabel = type(sliderMixin) == "table" and sliderMixin.Label or nil
    if sliderOptions.SetLabelFormatter and sliderLabel and sliderLabel.Right
        and type(_G.FormatPercentage) == "function" then
        sliderOptions:SetLabelFormatter(sliderLabel.Right, _G.FormatPercentage)
    end
    Settings.CreateSlider(category, opacity, sliderOptions, "Opacity of the Hermes chat window.")

    local fadeOnMove = Settings.RegisterProxySetting(category, VARIABLES.fadeOnMove,
        Settings.VarType.Boolean, "Fade while moving", true, getFadeOnMove, setFadeOnMove)
    Settings.CreateCheckbox(category, fadeOnMove,
        "Drop the window to 25% opacity while your character is moving, and fade back "
        .. "to the configured opacity when you stop. Never opens or hides the window.")

    Settings.RegisterAddOnCategory(category)
    self.category = category
    return true
end

function Options:Open()
    self:Init()
    if self.category and Settings and Settings.OpenToCategory then
        Settings.OpenToCategory(self.category:GetID())
        return true
    end
    return false
end

Options.VARIABLES = VARIABLES
