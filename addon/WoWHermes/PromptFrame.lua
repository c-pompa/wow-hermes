--[[---------------------------------------------------------------------------
    WoWHermes / PromptFrame.lua — addon-owned Accept/Cancel window.

    A small native window (PortraitFrameTemplate, the template the chat and notes
    windows already use) with a wrapped body line and two ordinary
    UIPanelButtonTemplate buttons. Confirm.lua and ReloadPrompt.lua build their
    prompts from this factory; nothing here delegates to a Blizzard dialog.

    Deliberately absent, because all of it produced ADDON_ACTION_FORBIDDEN /
    ADDON_ACTION_BLOCKED on Forever:
      * no Blizzard dialog registration or show/hide helpers
      * no protected API (keyboard capture, action-bar attributes, macro runs)
      * no controller/navigation registration and no focus stealing

    The player drives the window with the mouse or a controller cursor; the
    buttons are plain insecure buttons owned by the addon.
--------------------------------------------------------------------------------]]-->

local _, ns = ... -- luacheck: ignore 212

local Prompt = {}
ns.Prompt = Prompt

Prompt.WIDTH = 460
Prompt.HEIGHT = 200
Prompt.TITLE = "Hermes"

-- opts = {
--   name = optional global frame name,
--   title = window title,
--   acceptText / cancelText = button labels,
--   onAccept / onCancel = click handlers,
-- }
-- Returns nil when the client cannot build the window; callers must fail closed.
function Prompt.Create(opts)
    opts = opts or {}
    if type(CreateFrame) ~= "function" or type(UIParent) ~= "table" then return nil end

    local ok, frame = pcall(function()
        local f = CreateFrame("Frame", opts.name, UIParent, "PortraitFrameTemplate")
        f:SetSize(opts.width or Prompt.WIDTH, opts.height or Prompt.HEIGHT)
        f:SetPoint("CENTER", UIParent, "CENTER", 0, 0)
        f:SetToplevel(true)
        f:SetClampedToScreen(true)
        f:SetMovable(true)
        if f.EnableMouse then pcall(f.EnableMouse, f, true) end
        f:RegisterForDrag("LeftButton")
        f:SetScript("OnDragStart", function(window) window:StartMoving() end)
        f:SetScript("OnDragStop", function(window) window:StopMovingOrSizing() end)
        if f.SetTitle then f:SetTitle(opts.title or Prompt.TITLE) end

        local body = f:CreateFontString(nil, "OVERLAY")
        body:SetFontObject(GameFontHighlight)
        body:SetJustifyH("LEFT")
        body:SetWordWrap(true)
        body:SetPoint("TOPLEFT", f, "TOPLEFT", 18, -46)
        body:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -18, 54)
        f.body = body

        local accept = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        accept:SetSize(120, 22)
        accept:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -18, 16)
        accept:SetText(opts.acceptText or "Accept")
        accept:SetScript("OnClick", function()
            if opts.onAccept then opts.onAccept() end
        end)
        f.accept = accept

        local cancel = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        cancel:SetSize(100, 22)
        cancel:SetPoint("RIGHT", accept, "LEFT", -10, 0)
        cancel:SetText(opts.cancelText or "Cancel")
        cancel:SetScript("OnClick", function()
            if opts.onCancel then opts.onCancel() end
        end)
        f.cancel = cancel

        -- The title-bar close button is a Cancel: closing a permission prompt
        -- never means "yes".
        if f.CloseButton then
            f.CloseButton:SetScript("OnClick", function()
                if opts.onCancel then opts.onCancel() end
            end)
        end

        f:Hide()
        return f
    end)

    if ok then return frame end
    return nil
end

-- Returns true when the body was updated.
function Prompt.SetBody(frame, text)
    if type(frame) ~= "table" or type(frame.body) ~= "table" then return false end
    local ok = pcall(frame.body.SetText, frame.body, text)
    return ok
end

-- Returns true when the window is on screen.
function Prompt.Show(frame)
    if type(frame) ~= "table" or type(frame.Show) ~= "function" then return false end
    return pcall(frame.Show, frame)
end

function Prompt.Hide(frame)
    if type(frame) ~= "table" or type(frame.Hide) ~= "function" then return end
    pcall(frame.Hide, frame)
end
