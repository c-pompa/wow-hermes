--[[---------------------------------------------------------------------------
    WoWHermes / Display.lua — the in-game chat window.

    Blizzard-native: PortraitFrameTemplate (title, portrait, close button),
    a ScrollingMessageFrame transcript, an inset multiline EditBox and a
    UIPanelButtonTemplate send button. Nothing here is protected, and the addon
    never reads a secret value: the transcript only ever shows text the player
    typed or the bridge wrote into a slot file.
--------------------------------------------------------------------------------]]

local _, ns = ...

local History = ns.History
local Format = ns.Format

local Display = {}
ns.Display = Display

Display.WIDTH = 460
Display.HEIGHT = 560
Display.MIN_WIDTH = 340
Display.MIN_HEIGHT = 380
Display.MAX_WIDTH = 900
Display.MAX_HEIGHT = 1000
-- While the player is moving the window drops to this alpha (0.25 = "a quarter"),
-- then fades back to WoWHermesDB.settings.opacity when they stop.
Display.MOVE_ALPHA = 0.25
Display.FADE_TIME = 0.2
Display.frame = nil
-- Movement state, seeded from IsPlayerMoving() and updated by the synchronous
-- PLAYER_STARTED_MOVING / PLAYER_STOPPED_MOVING events.
Display.moving = false

local COLOR_SYSTEM = { 0.75, 0.75, 0.75 }
local COLOR_BODY = { 0.94, 0.94, 0.94 }

-- Working-entry animation state. Weak keys: history entries are stored tables,
-- so a restored transcript simply drops its stale start times.
Display.workingLines = {}
Display.thinkStarted = setmetatable({}, { __mode = "k" })
Display.thinkTexts = setmetatable({}, { __mode = "k" })
Display.thinkTicker = nil

local function db()
    return WoWHermesDB
end

-- The window layout lives in WoWHermesDB.window next to the anchor: position and
-- size are saved together, but each writer only touches its own keys.
local function layout(d)
    local d2 = d or db()
    if not d2 then return nil end
    if type(d2.window) ~= "table" then d2.window = {} end
    return d2.window
end

local function clampSize(w, h)
    w = tonumber(w) or Display.WIDTH
    h = tonumber(h) or Display.HEIGHT
    if w < Display.MIN_WIDTH then w = Display.MIN_WIDTH end
    if w > Display.MAX_WIDTH then w = Display.MAX_WIDTH end
    if h < Display.MIN_HEIGHT then h = Display.MIN_HEIGHT end
    if h > Display.MAX_HEIGHT then h = Display.MAX_HEIGHT end
    return w, h
end

local function say(msg)
    if ns.Print then ns.Print(msg) else print(msg) end
end

local function stamp(t)
    if type(t) ~= "number" then return "" end
    local ok, text = pcall(date, "%H:%M", t)
    return ok and text or ""
end

function Display:SavePosition()
    local f = self.frame
    local win = f and layout()
    if not f or not win then return end
    local point, _, relativePoint, x, y = f:GetPoint(1)
    if not point then return end
    win.point, win.relativePoint, win.x, win.y = point, relativePoint, x, y
end

function Display:RestorePosition()
    local f = self.frame
    local win = f and layout()
    if not win or not win.point then return end
    f:ClearAllPoints()
    f:SetPoint(win.point, UIParent, win.relativePoint or win.point, win.x or 0, win.y or 0)
end

-- Resizing the window saves both numbers (SetSizing/StartSizing are protected,
-- but this frame is addon-owned, so the calls are legal outside a secure path).
function Display:SaveSize()
    local f = self.frame
    local win = f and layout()
    if not f or not win then return end
    win.width, win.height = f:GetWidth(), f:GetHeight()
end

function Display:RestoreSize()
    local f = self.frame
    local win = f and layout()
    if not win then return end
    if type(win.width) == "number" and type(win.height) == "number" then
        local w, h = clampSize(win.width, win.height)
        f:SetSize(w, h)
    end
end

-- SetResizeBounds(minWidth, minHeight, maxWidth, maxHeight) — the client clamps
-- interactive sizing; our restored size is clamped to the same numbers.
function Display:ApplyResizeBounds()
    local f = self.frame
    if not f then return end
    if f.SetResizable then pcall(f.SetResizable, f, true) end
    if f.SetResizeBounds then
        pcall(f.SetResizeBounds, f, self.MIN_WIDTH, self.MIN_HEIGHT, self.MAX_WIDTH, self.MAX_HEIGHT)
    end
end

function Display:Ensure()
    if self.frame then return self.frame end

    local f = CreateFrame("Frame", "WoWHermesFrame", UIParent, "PortraitFrameTemplate")
    f:SetSize(self.WIDTH, self.HEIGHT)
    f:SetPoint("CENTER", UIParent, "CENTER", 0, 0)
    f:SetToplevel(true)
    f:SetClampedToScreen(true)
    f:SetMovable(true)
    -- EnableMouse is a protected function: harmless on our own frame, but never
    -- let it break initialisation (e.g. a /reload that lands close to combat).
    if f.EnableMouse then pcall(f.EnableMouse, f, true) end
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(frame)
        frame:StartMoving()
    end)
    f:SetScript("OnDragStop", function(frame)
        frame:StopMovingOrSizing()
        Display:SavePosition()
    end)
    if f.SetTitle then f:SetTitle("Hermes") end
    if f.SetPortraitToAsset then f:SetPortraitToAsset("Interface\\AddOns\\WoWHermes\\media\\hermes-portrait") end
    -- The thinking ticker lives only while the window is on screen. OnShow also
    -- re-seeds movement state (a window opened mid-run must come up faded, not
    -- flash at full opacity and then dim).
    f:SetScript("OnShow", function()
        Display:SyncMoving()
        Display:ApplyOpacity()
        Display:UpdateThinkTicker()
    end)
    f:SetScript("OnHide", function()
        Display:StopThinkTicker()
    end)
    if f.CloseButton then
        f.CloseButton:SetScript("OnClick", function()
            f:Hide()
        end)
    end
    f:Hide()

    -- status dot + text row under the title bar. A Button, not a Texture:
    -- Region:EnableMouse is a protected function, a Button takes mouse input as-is.
    local statusButton = CreateFrame("Button", nil, f)
    statusButton:SetSize(12, 12)
    statusButton:SetPoint("TOPLEFT", f, "TOPLEFT", 12, -34)
    local dot = statusButton:CreateTexture(nil, "OVERLAY")
    dot:SetAllPoints(statusButton)
    dot:SetColorTexture(1, 0.85, 0.2, 1)
    statusButton.dot = dot
    f.statusButton = statusButton
    f.statusDot = dot

    local statusText = f:CreateFontString(nil, "OVERLAY")
    statusText:SetFontObject(GameFontHighlightSmall)
    statusText:SetPoint("LEFT", statusButton, "RIGHT", 6, 0)
    statusText:SetTextColor(0.8, 0.8, 0.8)
    statusText:SetJustifyH("LEFT")
    statusText:SetText("bridge: starting")
    f.statusText = statusText

    statusButton:SetScript("OnEnter", function(button)
        Display.ShowStatusTooltip(button)
    end)
    statusButton:SetScript("OnLeave", function()
        if GameTooltip then GameTooltip:Hide() end
    end)

    -- Sessions header placeholder (v1.1: a real session list needs the bridge
    -- sessions API). Deliberately non-interactive so it cannot steal gamepad
    -- focus and cannot look like a working control.
    local sessions = f:CreateFontString(nil, "OVERLAY")
    sessions:SetFontObject(GameFontDisableSmall)
    sessions:SetPoint("TOPRIGHT", f, "TOPRIGHT", -30, -34)
    sessions:SetTextColor(0.55, 0.55, 0.55)
    sessions:SetJustifyH("RIGHT")
    sessions:SetText("Sessions: 1 (list in v1.1)")
    f.sessionsLabel = sessions

    -- transcript
    local transcript = CreateFrame("ScrollingMessageFrame", "WoWHermesTranscript", f)
    transcript:SetPoint("TOPLEFT", f, "TOPLEFT", 10, -50)
    -- Right edge stops short of the scroll bar so the bar never covers text;
    -- the bar is anchored to the transcript itself (Scrollbar.lua).
    transcript:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -24, 126)
    -- Forever: ScrollingMessageFrame:SetFont goes through FontableFrameMixin and requires flags
    -- (in-game round 2: "bad argument #3 to 'SetFont'"). Font objects avoid the issue entirely.
    transcript:SetFontObject(ChatFontNormal)
    transcript:SetJustifyH("LEFT")
    transcript:SetFading(false)
    transcript:SetMaxLines(400)
    -- Mouse-wheel scrolling: EnableMouseWheel(ScriptRegion) + OnMouseWheel(delta),
    -- the same wiring Blizzard uses for chat (FloatingChatFrame.lua:321-327 ->
    -- ScrollingMessageFrameMixin:ScrollUp/ScrollDown, SharedXML/ScrollingMessageFrame.lua:138/142).
    if transcript.EnableMouseWheel then
        pcall(transcript.EnableMouseWheel, transcript, true)
    end
    transcript:SetScript("OnMouseWheel", function(frame, delta)
        Display.OnTranscriptWheel(frame, delta)
    end)
    f.transcript = transcript

    -- Right-edge transcript scroll bar (WS-K). MinimalScrollBar + the native
    -- message-frame binding is exactly what the chat window does
    -- (ChatFrame.xml:5-9, ChatFrameOverrides.lua:40); see Scrollbar.lua. If the
    -- client has no ScrollUtil the bar is hidden and the wheel keeps working.
    local bar = ns.Scroll and ns.Scroll.CreateBar(f, transcript, 12)
    if bar then
        f.scrollBar = bar
        if not ns.Scroll.Attach(transcript, bar) then bar:Hide() end
    end

    -- input
    local inputBg = CreateFrame("Frame", nil, f, "InsetFrameTemplate")
    inputBg:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 8, 44)
    inputBg:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -8, 44)
    inputBg:SetHeight(70)
    f.inputBg = inputBg

    local editBox = CreateFrame("EditBox", nil, inputBg)
    editBox:SetPoint("TOPLEFT", inputBg, "TOPLEFT", 8, -8)
    editBox:SetPoint("BOTTOMRIGHT", inputBg, "BOTTOMRIGHT", -8, 8)
    editBox:SetMultiLine(true)
    editBox:SetAutoFocus(false)
    editBox:SetFontObject(ChatFontNormal)
    editBox:SetTextInsets(2, 2, 2, 2)
    editBox:SetJustifyH("LEFT")
    editBox:SetJustifyV("TOP")
    editBox:SetMaxLetters(2800)
    editBox:SetScript("OnEnterPressed", function()
        Display:OnEnterPressed()
    end)
    editBox:SetScript("OnEscapePressed", function(box)
        box:ClearFocus()
        Display:Hide()
    end)
    f.editBox = editBox

    local send = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    send:SetSize(70, 22)
    -- left of the resize grip's corner (grip occupies the last ~18 px)
    send:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -24, 6)
    send:SetText("Send")
    send:SetScript("OnClick", function()
        Display:Send()
    end)
    f.sendButton = send

    -- Action row (WS-K), bottom-left, clear of Send (right) and the resize grip
    -- (BOTTOMRIGHT -2,2). Save/Notes act on the transcript; Clear/New are set
    -- apart by a divider and by wording, so "clear my display" and "reset the
    -- backend" cannot be confused.
    local function actionButton(text, width, x, tip, onClick)
        -- UIPanelButtonTemplate: the same template as Send (SharedUIPanelTemplates.xml)
        local button = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        button:SetSize(width, 20)
        button:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", x, 6)
        button:SetText(text)
        button:SetScript("OnClick", function()
            onClick()
        end)
        button:SetScript("OnEnter", function(owner)
            if not GameTooltip then return end
            GameTooltip:SetOwner(owner, "ANCHOR_RIGHT")
            GameTooltip:SetText(text)
            GameTooltip:AddLine(tip, 1, 1, 1, true)
            GameTooltip:Show()
        end)
        button:SetScript("OnLeave", function()
            if GameTooltip then GameTooltip:Hide() end
        end)
        return button
    end

    local save = actionButton("Save", 52, 12,
        "Save the newest finished Hermes reply as a note (local, SavedVariables).",
        function() Display:SaveNote() end)
    f.saveButton = save

    local notes = actionButton("Notes 0", 62, 68,
        "Read saved notes without losing the conversation.",
        function() ns.NotesFrame:Show() end)
    f.notesButton = notes

    -- Visual grouping: Save/Notes read or keep; the divider separates the two
    -- session actions below. No shared label, no shared colour.
    local divider = f:CreateTexture(nil, "ARTWORK")
    divider:SetSize(1, 18)
    divider:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 133, 7)
    divider:SetColorTexture(0.4, 0.4, 0.4, 0.8)
    f.actionDivider = divider

    local clear = actionButton("Clear", 50, 140,
        "Clear this window and its history. Saved notes stay.",
        function() Display:ClearLocal() end)
    f.clearButton = clear

    local new = actionButton("New", 46, 194,
        "Clear the window AND ask Hermes for a fresh session (/new).",
        function() Display:NewSession() end)
    f.newButton = new

    local hint = f:CreateFontString(nil, "OVERLAY")
    hint:SetFontObject(GameFontDisableSmall)
    hint:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 74, 30)
    hint:SetTextColor(0.6, 0.6, 0.6)
    hint:SetText("Enter sends · Shift+Enter adds a line · Esc closes")
    f.hint = hint

    -- Copy lives in the narrow utility row between the action buttons and the
    -- input, NOT the title bar: the title bar is the window's drag handle. It
    -- fits exactly above Save (action row ends at y=26; input starts at y=44).
    -- A ScrollingMessageFrame cannot be selected, so this opens the copy dialog.
    local copy = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    copy:SetSize(54, 16)
    copy:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 14, 28)
    copy:SetText("Copy")
    copy:SetScript("OnClick", function()
        Display:OpenCopy()
    end)
    copy:SetScript("OnEnter", function(owner)
        if not GameTooltip then return end
        GameTooltip:SetOwner(owner, "ANCHOR_RIGHT")
        GameTooltip:SetText("Copy Hermes text")
        GameTooltip:AddLine("Opens the newest finished reply in a selectable box: "
            .. "Ctrl+C copies, drag selects, Esc closes.", 1, 1, 1, true)
        GameTooltip:Show()
    end)
    copy:SetScript("OnLeave", function()
        if GameTooltip then GameTooltip:Hide() end
    end)
    f.copyButton = copy

    -- Bottom-right resize grip. PanelResizeButtonTemplate is Blizzard's own 16x16
    -- size grabber (SharedXML/Mainline/SharedUIPanelTemplates.xml:1642, textures
    -- UI-ChatIM-SizeGrabber-*); EventTrace uses it the same way
    -- (Blizzard_EventTrace.xml:183). OnMouseDown/OnMouseUp mirror
    -- PanelResizeButtonMixin (SharedUIPanelTemplates.lua:1693-1730) but also save
    -- the size, which the mixin does not do.
    local grip = nil
    if CreateFrame then
        local ok, created = pcall(CreateFrame, "Button", nil, f, "PanelResizeButtonTemplate")
        if ok then grip = created end
    end
    if not grip then
        grip = CreateFrame("Button", nil, f)
        grip:SetSize(16, 16)
        if grip.SetNormalTexture then
            grip:SetNormalTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Up")
            grip:SetHighlightTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Highlight")
            grip:SetPushedTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Down")
        end
    end
    grip:SetSize(16, 16)
    grip:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -2, 2)
    grip:SetScript("OnMouseDown", function(button)
        button.isActive = true
        if f.StartSizing then pcall(f.StartSizing, f, "BOTTOMRIGHT", true) end
    end)
    grip:SetScript("OnMouseUp", function(button)
        button.isActive = false
        if f.StopMovingOrSizing then f:StopMovingOrSizing() end
        Display:SaveSize()
        Display:SavePosition()
    end)
    f.resizeGrip = grip

    self.frame = f
    self:ApplyResizeBounds()
    self:RestorePosition()
    self:RestoreSize()
    self:SyncMoving()
    self:ApplyOpacity()
    return f
end

-- ------------------------------------------------------- mouse wheel scroll
-- One notch = one line (ScrollUp/ScrollDown). Shift+wheel pages through
-- PageUp/PageDown (the client's paging wrapper around ScrollByAmount +
-- GetPagingScrollAmount). The client itself keeps the visible window stable on
-- AddMessage while scrolled (ScrollingMessageFrame.lua:9-15), so new replies and
-- the thinking rebuild do not yank the player's scroll position.
function Display.OnTranscriptWheel(transcript, delta)
    if not transcript then return end
    delta = tonumber(delta) or 0
    if delta == 0 then return end
    local paging = type(IsShiftKeyDown) == "function" and IsShiftKeyDown()
    if paging then
        if delta > 0 then
            if transcript.PageUp then transcript:PageUp() end
        else
            if transcript.PageDown then transcript:PageDown() end
        end
    elseif delta > 0 then
        if transcript.ScrollUp then transcript:ScrollUp() end
    else
        if transcript.ScrollDown then transcript:ScrollDown() end
    end
end

-- ------------------------------------------------------- movement fade
-- PLAYER_STARTED_MOVING / PLAYER_STOPPED_MOVING are synchronous unit events
-- (UnitDocumentation.lua:3946/3964); IsPlayerMoving() gives the state at load
-- (PlayerScriptDocumentation.lua:1453). While moving the window sits at
-- Display.MOVE_ALPHA (0.25); on stop it fades back to the configured opacity.
-- UIFrameFadeIn is used in BOTH directions: it only touches alpha, whereas
-- UIFrameFadeOut is the "fade out and hide" path.
function Display:SyncMoving()
    if type(IsPlayerMoving) == "function" then
        local ok, moving = pcall(IsPlayerMoving)
        if ok then self.moving = moving == true end
    end
    return self.moving
end

function Display:OnMovementEvent(event)
    if event == "PLAYER_STARTED_MOVING" then
        self.moving = true
    elseif event == "PLAYER_STOPPED_MOVING" then
        self.moving = false
    else
        return
    end
    self:ApplyMotionFade()
end

function Display.FadeEnabled()
    local s = db() and db().settings
    return not (s and s.fadeOnMove == false)
end

function Display:TargetAlpha()
    local s = db() and db().settings
    local base = (s and type(s.opacity) == "number") and s.opacity or 0.9
    if Display.FadeEnabled() and self.moving then return self.MOVE_ALPHA end
    return base
end

function Display:ApplyOpacity()
    local f = self.frame
    if not f then return end
    f:SetAlpha(self:TargetAlpha())
end

-- Fade toward TargetAlpha, but never call UIFrameFadeIn on a hidden frame:
-- UIFrameFade() ends with frame:Show(), so fading a closed window would pop it
-- open (Blizzard_SharedXMLBase/FrameUtil.lua:362).
function Display:ApplyMotionFade()
    local f = self.frame
    if not f or not f:IsShown() then return end
    local target = self:TargetAlpha()
    local current = f:GetAlpha() or 1
    if math.abs(current - target) < 0.001 then return end
    if type(UIFrameFadeIn) == "function" then
        -- A PortraitFrameTemplate is a Frame at runtime; the annotation only knows the template.
        ---@diagnostic disable-next-line: param-type-mismatch
        UIFrameFadeIn(f, self.FADE_TIME, current, target)
    else
        f:SetAlpha(target)
    end
end

-- ------------------------------------------------------------ gamepad reach
-- Forever's native gamepad navigation (wow-ui-source-forever, 2026-11-09):
-- SmartNavigation discovers every child Button/EditBox with a size once a panel
-- is registered (Blizzard_GamepadSmartNavigation/Utility.lua:263-277,
-- FindButtons), and the supported seam for an ordinary addon window is
-- GamepadMode.FrameControlsManager:FrameShown/FrameHidden
-- (Blizzard_GamepadSharedUtility/FrameControlsManager.lua:384/473; its own
-- comment: "A frame can subscribe to this by ... manually calling FrameShown on
-- their OnShow event"). FrameShown -> EnableNavigation -> HandlePanelOpen.
-- Capability-checked and pcall'd: on a client without these globals this is a
-- no-op, and in combat we stay out of the secure path entirely.
local function gamepadControls()
    -- GamepadMode is not in the API docs either; fetch it by name.
    local mode = rawget(_G, "GamepadMode")
    local manager = type(mode) == "table" and mode.FrameControlsManager
    if type(manager) ~= "table" then return nil end
    return manager
end

function Display:NotifyGamepad(shown)
    -- OFF by default since in-game round 4: calling FrameShown from addon code made the manager
    -- run its focus path tainted and the client raised "WoWHermes has been blocked from an action
    -- only available to the Blizzard UI" (ADDON_ACTION_FORBIDDEN). pcall cannot stop that.
    local settings = WoWHermesDB and WoWHermesDB.settings
    if not (settings and settings.gamepadNav) then return end
    if type(_G.InCombatLockdown) == "function" and _G.InCombatLockdown() then return end
    local manager = gamepadControls()
    if not manager or not self.frame then return end
    if shown then
        local fn = manager.FrameShown
        if type(fn) == "function" then pcall(fn, manager, self.frame, false, true) end
    else
        local fn = manager.FrameHidden
        if type(fn) == "function" then pcall(fn, manager, self.frame) end
    end
end

function Display:OnEnterPressed()
    if IsShiftKeyDown and IsShiftKeyDown() then
        return   -- let the edit box insert a newline
    end
    self:Send()
end

function Display:Send()
    self:Ensure()
    local eb = self.frame.editBox
    local text = eb:GetText() or ""
    text = text:gsub("^%s+", ""):gsub("%s+$", "")
    if text == "" then return end

    local ok, reason = ns.Outbox:SubmitMessage(text)
    if ok then
        eb:SetText("")
        self:Refresh()
    elseif reason == "oversize" then
        say("That message is too long for the transport (3200 bytes). It stays in the box.")
    elseif reason == "busy" then
        say("Outbox is full (1 in flight + 4 queued). Try again in a moment.")
    end
end

function Display:Toggle()
    self:Ensure()
    if self.frame:IsShown() then
        self.frame:Hide()
        self:StopThinkTicker()
        self:NotifyGamepad(false)
    else
        self:Refresh()
        self.frame:Show()
        self:NotifyGamepad(true)
    end
end

function Display:Show()
    self:Ensure()
    self:Refresh()
    self.frame:Show()
    self:NotifyGamepad(true)
end

function Display:Hide()
    self:StopThinkTicker()
    if self.frame then
        self.frame:Hide()
        self:NotifyGamepad(false)
    end
end

-- ------------------------------------------------------- thinking indicator
-- The bridge sends a placeholder `text` ("…") for a working exchange. That text
-- is never rendered: the row is replaced by `Hermes is thinking <spinner> (3s)`
-- and refreshes in place (ScrollingMessageFrameMixin:TransformMessages,
-- Blizzard_SharedXML/ScrollingMessageFrame.lua:95) while the window is shown.

function Display:WorkingSeconds(entry)
    local started = self.thinkStarted[entry]
    if not started then
        started = GetTime()
        self.thinkStarted[entry] = started
    end
    return GetTime() - started
end

function Display:ThinkingText(entry)
    return Format.Thinking(self:WorkingSeconds(entry),
        Format.SpinnerPhase(GetTime()))
end

function Display:UpdateThinkTicker()
    local f = self.frame
    local any = f ~= nil and f:IsShown() and #self.workingLines > 0
    if any then
        if not self.thinkTicker then
            self.thinkTicker = C_Timer.NewTicker(Format.SPIN_INTERVAL, function()
                Display:ThinkTick()
            end)
        end
    else
        self:StopThinkTicker()
    end
end

function Display:StopThinkTicker()
    if self.thinkTicker then
        if self.thinkTicker.Cancel then self.thinkTicker:Cancel() end
        self.thinkTicker = nil
    end
end

-- Replace only the indicator lines, so the scroll position and the rest of the
-- transcript stay untouched. No working entries -> the ticker stops.
function Display:ThinkTick()
    local f = self.frame
    if not f or not f.transcript or not f:IsShown() or #self.workingLines == 0 then
        self:StopThinkTicker()
        return
    end
    local t = f.transcript
    if type(t.TransformMessages) ~= "function" then return end
    local phase = Format.SpinnerPhase(GetTime())
    for _, row in ipairs(self.workingLines) do
        local entry = row.entry
        local old = self.thinkTexts[entry]
        local new = Format.Thinking(self:WorkingSeconds(entry), phase)
        if old and old ~= new then
            local want = old
            t:TransformMessages(function(message)
                return message == want
            end, function()
                return new, COLOR_BODY[1], COLOR_BODY[2], COLOR_BODY[3]
            end)
            self.thinkTexts[entry] = new
        end
    end
end

-- Rebuild the transcript from the stored history.
function Display:Refresh()
    local f = self.frame
    if not f or not f.transcript then return end
    local t = f.transcript
    t:Clear()
    self.workingLines = {}
    local entries = History.All()
    local count = 0
    local function addLine(text)
        count = count + 1
        t:AddMessage(text, COLOR_BODY[1], COLOR_BODY[2], COLOR_BODY[3])
    end
    for i = 1, #entries do
        local e = entries[i]
        if e.status == "working" then
            -- No speaker label, no placeholder text: a clear native indicator.
            local text = self:ThinkingText(e)
            addLine(text)
            self.thinkTexts[e] = text
            self.workingLines[#self.workingLines + 1] = { entry = e, index = count }
        else
            local when = stamp(e.t)
            local prefix = (when ~= "" and (Format.COLORS.grey .. when .. "|r ") or "")
            local who, color
            if e.role == "hermes" then
                who, color = "Hermes", Format.COLORS.hermes
            else
                who, color = "You", Format.COLORS.user
            end
            local body = Format.Lines(e.text or "")
            if e.status == "unsent" then
                body[#body] = body[#body] .. " " .. Format.COLORS.grey .. "(unsent)|r"
            elseif e.status == "error" then
                body[#body] = body[#body] .. " " .. Format.COLORS.error .. "(error)|r"
            end
            addLine(prefix .. color .. who .. ":|r " .. body[1])
            for j = 2, #body do addLine(body[j]) end
        end
    end
    if #entries == 0 then
        t:AddMessage("Type a message and press Enter. /h opens this window anywhere.",
            COLOR_SYSTEM[1], COLOR_SYSTEM[2], COLOR_SYSTEM[3])
    end
    -- Keep the bar's extent/position in step with the rebuilt buffer. The native
    -- helper also does this on the next display refresh; doing it here means the
    -- thumb is already right when Refresh returns (no one-frame lag after Save,
    -- Clear or a new reply).
    if f.scrollBar then ns.Scroll.Sync(t, f.scrollBar) end
    self:UpdateNotesButton()
    self:UpdateThinkTicker()
end

-- ------------------------------------------------------------ notes/session
-- Save the newest finished reply as a local note (Notes.lua). Duplicate presses
-- are refused: the note list is keyed on the reply's frame id + text.
function Display:SaveNote()
    self:Ensure()
    local note, reason = ns.Notes:SaveLatest()
    if note then
        say(("Note saved (%d/%d)."):format(ns.Notes.Count(), ns.Notes.MAX))
        self:UpdateNotesButton()
    elseif reason == "duplicate" then
        say("That reply is already saved as a note.")
    elseif reason == "no-reply" then
        say("There is no finished Hermes reply to save yet.")
    else
        say("That reply could not be saved (" .. tostring(reason) .. ").")
    end
    return note, reason
end

-- Copy (WS-L): hand the newest finished reply to the copy dialog. The dialog owns
-- the EditBox, focus and selection; nothing here touches the clipboard.
function Display:OpenCopy(mode)
    local copy = ns.CopyFrame
    local f = self.frame
    if not copy or not f then return end
    return copy:Show(mode or copy.MODE_LATEST)
end

function Display:UpdateNotesButton()
    local f = self.frame
    if not f or not f.notesButton then return end
    f.notesButton:SetText(("Notes %d"):format(ns.Notes and ns.Notes.Count() or 0))
end

-- Clear (local): the window and WoWHermesDB.chats.c0.history. Saved notes are a
-- different store (WoWHermesDB.notes) and are never touched.
function Display:ClearLocal()
    ns.History.Clear()
    self:Refresh()
    say("Local history cleared. Saved notes are untouched.")
end

-- New session: clear locally AND ask the backend to start over with `/new`
-- through the normal outbox (PROTOCOL A.1 kind `m`; no new record kind). The
-- local clear is unconditional, the backend reset is reported honestly when the
-- record cannot go out (outbox full, or the /reload transport).
function Display:NewSession()
    ns.History.Clear()
    self:Refresh()

    local ok, reason = ns.Outbox:SubmitMessage("/new", { silent = true })
    if not ok then
        say("Local history cleared, but the backend was NOT reset ("
            .. tostring(reason or "outbox busy") .. ").")
        return false, reason
    end
    if ns.Outbox.UseStrip() then
        say("New session requested: local history cleared and /new is on its way.")
    else
        say("Local history cleared. /new is queued for /reload; the backend resets then.")
    end
    return true
end

function Display.StatusColor()
    local status = ns.Bridge and ns.Bridge:Refresh() or "starting"
    if ns.Bridge and ns.Bridge.mode ~= "A" and status == "up" then
        -- bridge publishes fine but we cannot see acks: yellow
        return 1, 0.85, 0.2, status
    end
    if status == "up" then return 0.2, 1, 0.2, status end
    if status == "down" then return 1, 0.2, 0.2, status end
    return 1, 0.85, 0.2, status
end

function Display:RefreshStatus()
    local f = self.frame
    if not f or not f.statusDot then return end
    local r, g, b, status = Display.StatusColor()
    f.statusDot:SetColorTexture(r, g, b, 1)
    local label = "bridge: " .. tostring(status)
    if ns.Bridge and ns.Bridge.mode ~= "A" then label = label .. " (no doorbells)" end
    f.statusText:SetText(label)
end

function Display.ShowStatusTooltip(owner)
    if not GameTooltip then return end
    local bridge = ns.Bridge
    GameTooltip:SetOwner(owner, "ANCHOR_RIGHT")
    GameTooltip:SetText("WoW Hermes bridge")
    if bridge then
        local r, g, b, status = Display.StatusColor()
        GameTooltip:AddLine("Status: " .. tostring(status), r, g, b)
        GameTooltip:AddLine("Doorbell mode: " .. tostring(bridge.mode)
            .. (bridge.mode == "A" and " (existence)" or " (slots only)"))
        GameTooltip:AddLine("Slots installed: " .. tostring(bridge.slotsTotal))
        GameTooltip:AddLine("Replies waiting: " .. tostring(ns.Inbox and ns.Inbox:PendingCount() or 0))
        local last = bridge.lastSeen or 0
        if last > 0 then
            GameTooltip:AddLine("Last publish: " .. string.format("%.0f s ago", GetTime() - last))
        else
            GameTooltip:AddLine("No slot publish seen this session")
        end
        GameTooltip:AddLine("Ack high-water: " .. tostring(bridge.ack))
    end
    GameTooltip:Show()
end

-- Wire the window into the rest of the addon.
function Display:Init()
    self:Ensure()
    self:RefreshStatus()
    -- Refresh even when no new slot arrives: the 45 s freshness window must age
    -- visibly, and a bridge restart must clear a stale down/up badge promptly.
    if not self.statusTicker and type(C_Timer) == "table" and C_Timer.NewTicker then
        self.statusTicker = C_Timer.NewTicker(5, function() Display:RefreshStatus() end)
    end
    self:Refresh()
end
