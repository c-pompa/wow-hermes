--[[---------------------------------------------------------------------------
    WoWHermes / CopyFrame.lua — "Copy Hermes text": get a Hermes reply (or the
    whole conversation) onto the clipboard.

    Why a dialog instead of selecting the transcript: a ScrollingMessageFrame is
    not text-selectable on Forever. The supported surface for selectable text is
    an EditBox, so this window puts the text in a multiline EditBox and lets the
    PLAYER press Ctrl+C. The addon never calls CopyToClipboard / C_Clipboard* or
    any other protected function, and it never synthesises input: it only calls
    SetText, SetFocus, SetCursorPosition and HighlightText — the same four calls
    Blizzard's own error window makes (Blizzard_ScriptErrorsFrame/
    Blizzard_ScriptErrorsFrame.lua:210-212: SetText, HighlightText(0),
    SetCursorPosition(0)).

    Forever evidence (wow-ui-source-forever @ 1.60.1.70862):
      * EditBox:SetMultiLine / SetAutoFocus / SetTextInsets / SetMaxLetters /
        SetCursorPosition / SetFocus / HighlightText / GetText —
        Blizzard_APIDocumentationGenerated/SimpleEditBoxAPIDocumentation.lua:779,
        616, 915, 769, 646, 668, 435, 15.
      * SetFocus is NOT a protected function; its only restriction is
        `ChecksForbiddenAspects = ScriptedInput` (same file:668-676), i.e. it is
        refused only for objects declared with that XML aspect (e.g.
        Blizzard_AuraContainer/Blizzard_AuraButton.xml:19). Addon-created frames
        carry no aspects, so the call is legal.
      * HighlightText(start=0, stop=-1) selects everything (file:435-446); the
        defaults are used, which is also what Blizzard does on focus
        (Blizzard_ScriptErrorsFrame.lua:58-60, OnEditFocusGained ->
        HighlightText(0)).
      * A multiline EditBox inside a ScrollFrameTemplate is Blizzard's own
        pattern for scrollable long text: Blizzard_Communities/GuildInfo.xml:
        317-352 (ScrollFrameTemplate -> ScrollChild EditBox multiLine,
        autoFocus="false", plus SetTextInsets/SetSpacing/SetCursorPosition(0)/
        SetFocus in Blizzard_Communities/GuildInfo.lua:120-143).
        ScrollFrameTemplate's OnLoad creates its MinimalScrollBar and binds it
        with ScrollUtil.InitScrollFrameWithScrollBar
        (Blizzard_SharedXML/SecureUIPanelTemplates.lua:1-29,
        Shared/Scroll/ScrollUtil.lua:215-267), which also installs the wheel
        handler. Nothing there is ours to re-implement.
      * EnableKeyboard is a PROTECTED function (SimpleFrameAPIDocumentation.lua:
        247-249), so Esc is handled by the EditBox's own OnEscapePressed
        (Blizzard_Communities/GuildInfo.xml:337, EditBox_ClearFocus) plus our
        Close button — never by grabbing the keyboard on the frame.

    Text fidelity: the box is filled with the RAW history text (History.lua
    entries), never with Format.Lines display markup, so newlines and wording
    survive the copy.
--------------------------------------------------------------------------------]]--

local _, ns = ... -- luacheck: ignore 212

local CopyFrame = {}
ns.CopyFrame = CopyFrame

CopyFrame.WIDTH = 520
CopyFrame.HEIGHT = 460
-- SetMaxLetters caps what the client keeps from SetText, so the addon clips first
-- and says so instead of letting the client silently drop the tail.
CopyFrame.MAX_LETTERS = 20000
CopyFrame.MODE_LATEST = "latest"
CopyFrame.MODE_FULL = "full"
-- Statuses a reply still streaming (Notes.lua:25 keeps the same pair).
CopyFrame.WORKING = { working = true, typing = true }

local function say(msg)
    if ns.Print then ns.Print(msg) else print(msg) end
end

local function stamp(t)
    if type(t) ~= "number" then return "" end
    local ok, text = pcall(date, "%Y-%m-%d %H:%M", t)
    return ok and text or ""
end

-- The newest finished Hermes reply, exactly as stored in the history buffer.
-- Notes.LatestReply (Notes.lua:125) is the same definition the Save button uses,
-- so "the reply" means one thing across the addon.
function CopyFrame.LatestText()
    local notes = ns.Notes
    if not notes or type(notes.LatestReply) ~= "function" then return nil end
    local entry = notes.LatestReply()
    if type(entry) ~= "table" then return nil end
    local text = entry.text
    if type(text) ~= "string" or text == "" then return nil end
    return text
end

-- The whole conversation as plain text: one block per entry, timestamped, with
-- the message body copied verbatim (its own newlines intact). Streaming
-- placeholders are skipped — they are a spinner, not content.
function CopyFrame.TranscriptText(list)
    list = list or (ns.History and ns.History:All()) or {}
    local blocks = {}
    for i = 1, #list do
        local entry = list[i]
        if type(entry) == "table"
            and type(entry.text) == "string" and entry.text ~= ""
            and not CopyFrame.WORKING[entry.status or ""] then
            local when = stamp(entry.t)
            local who = entry.role == "hermes" and "Hermes" or "You"
            local header = (when ~= "" and ("[" .. when .. "] ") or "") .. who .. ":"
            blocks[#blocks + 1] = header .. "\n" .. entry.text
        end
    end
    if #blocks == 0 then return nil end
    return table.concat(blocks, "\n\n")
end

-- Hard cap before SetMaxLetters can truncate mid-string.
function CopyFrame.Clip(text)
    if type(text) ~= "string" then return "", false end
    if #text <= CopyFrame.MAX_LETTERS then return text, false end
    return text:sub(1, CopyFrame.MAX_LETTERS) .. "\n[transcript truncated]", true
end

-- mode -> text, reason, truncated. reason names a STRING failure ("no-reply" /
-- "empty") and stays nil when text is usable; truncated is a separate third value
-- so a clipped transcript can never be mistaken for an empty one.
function CopyFrame.Content(mode)
    local text
    if mode == CopyFrame.MODE_FULL then
        text = CopyFrame.TranscriptText()
        if not text then return nil, "empty", false end
    else
        text = CopyFrame.LatestText()
        if not text then return nil, "no-reply", false end
    end
    local clipped, truncated = CopyFrame.Clip(text)
    return clipped, nil, truncated
end

function CopyFrame:Ensure()
    if self.frame then return self.frame end

    local f = CreateFrame("Frame", "WoWHermesCopyFrame", UIParent, "PortraitFrameTemplate")
    f:SetSize(self.WIDTH, self.HEIGHT)
    f:SetPoint("CENTER", UIParent, "CENTER", 0, 0)
    f:SetToplevel(true)
    f:SetClampedToScreen(true)
    f:SetMovable(true)
    if f.EnableMouse then pcall(f.EnableMouse, f, true) end
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(frame) frame:StartMoving() end)
    f:SetScript("OnDragStop", function(frame) frame:StopMovingOrSizing() end)
    if f.SetTitle then f:SetTitle("Copy Hermes text") end
    if f.CloseButton then
        f.CloseButton:SetScript("OnClick", function() CopyFrame:Hide() end)
    end
    f:SetScript("OnHide", function()
        CopyFrame.expected = nil
        if f.editBox then CopyFrame:ReleaseBox() end
    end)
    f:Hide()

    -- Mode row: "Latest reply" / "Full transcript" switch the same box. The
    -- loaded mode's own button is disabled, so the state is visible.
    local function modeButton(text, width, x, onClick)
        local button = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        button:SetSize(width, 20)
        button:SetPoint("TOPLEFT", f, "TOPLEFT", x, -50)
        button:SetText(text)
        button:SetScript("OnClick", function() onClick() end)
        button:SetScript("OnEnter", function(owner)
            if not GameTooltip then return end
            GameTooltip:SetOwner(owner, "ANCHOR_RIGHT")
            GameTooltip:SetText(text)
            GameTooltip:AddLine("Load this text into the copy box.", 1, 1, 1, true)
            GameTooltip:Show()
        end)
        button:SetScript("OnLeave", function()
            if GameTooltip then GameTooltip:Hide() end
        end)
        return button
    end

    f.latestButton = modeButton("Latest reply", 96, 14,
        function() CopyFrame:Show(CopyFrame.MODE_LATEST) end)
    f.fullButton = modeButton("Full transcript", 108, 116,
        function() CopyFrame:Show(CopyFrame.MODE_FULL) end)

    -- ScrollFrameTemplate owns its bar + wheel handler (SecureUIPanelTemplates.lua:1-29).
    local scroll = CreateFrame("ScrollFrame", nil, f, "ScrollFrameTemplate")
    scroll:SetPoint("TOPLEFT", f, "TOPLEFT", 14, -78)
    scroll:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -30, 46)
    if scroll.EnableMouseWheel then pcall(scroll.EnableMouseWheel, scroll, true) end
    f.scrollFrame = scroll

    local box = CreateFrame("EditBox", nil, scroll)
    box:SetPoint("TOPLEFT", scroll, "TOPLEFT", 0, 0)
    box:SetWidth(self.WIDTH - 60)
    box:SetHeight(200)
    box:SetMultiLine(true)
    box:SetAutoFocus(false)
    box:SetFontObject(ChatFontNormal)
    box:SetTextInsets(4, 4, 4, 4)
    box:SetJustifyH("LEFT")
    box:SetJustifyV("TOP")
    box:SetMaxLetters(self.MAX_LETTERS)
    -- Clicking anywhere in the text focuses the box (Blizzard does the same for
    -- its scrolling edit frames: InputBoxTemplates.lua:93-95).
    box:SetScript("OnMouseDown", function(editBox) editBox:SetFocus() end)
    -- Esc closes: the box holds focus, so its own OnEscapePressed fires. No
    -- frame-level EnableKeyboard (that call is protected, see header).
    box:SetScript("OnEscapePressed", function(editBox)
        editBox:ClearFocus()
        CopyFrame:Hide()
    end)
    -- Focus/highlight is the "ready for Ctrl+C" state: whatever path gives the
    -- box focus, the whole text is selected so Ctrl+C works immediately.
    box:SetScript("OnEditFocusGained", function(editBox)
        if type(editBox.HighlightText) == "function" then
            pcall(editBox.HighlightText, editBox)
        end
    end)
    -- Read-only emulation: the client has no read-only EditBox that still
    -- selects (SetEnabled(false) also blocks selection and Ctrl+C), so an edit is
    -- reverted to the preserved text and re-selected.
    box:SetScript("OnTextChanged", function(editBox)
        if CopyFrame.applying then return end
        local now = editBox:GetText() or ""
        if now == (CopyFrame.expected or "") then
            CopyFrame:FitHeight()
            return
        end
        CopyFrame:ApplyText(CopyFrame.expected or "")
    end)
    if scroll.SetScrollChild then scroll:SetScrollChild(box) end
    f.editBox = box

    -- Measurement FontString: same font object and wrapping width as the box, so
    -- the scroll child is exactly as tall as the wrapped text (no fake extent).
    local measurer = f:CreateFontString(nil, "ARTWORK")
    measurer:SetFontObject(ChatFontNormal)
    measurer:SetJustifyH("LEFT")
    measurer:SetWordWrap(true)
    measurer:SetAlpha(0)
    measurer:SetPoint("TOPLEFT", f, "TOPLEFT", 18, -82)
    measurer:SetWidth(self.WIDTH - 68)
    f.measurer = measurer

    local close = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    close:SetSize(90, 22)
    close:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -16, 12)
    close:SetText("Close")
    close:SetScript("OnClick", function() CopyFrame:Hide() end)
    f.closeButton = close

    local hint = f:CreateFontString(nil, "OVERLAY")
    hint:SetFontObject(GameFontDisableSmall)
    hint:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 16, 18)
    hint:SetTextColor(0.6, 0.6, 0.6)
    hint:SetText("Ctrl+C copies \194\183 drag selects text \194\183 Esc closes")
    f.hint = hint

    self.frame = f
    return f
end

-- Height of the scroll child from the wrapped text. Uses the client's own string
-- metrics (FontString:GetStringHeight, SimpleFontStringAPIDocumentation.lua:325);
-- a plain line count is only the fallback.
function CopyFrame:FitHeight()
    local f = self.frame
    if not f or not f.editBox or not f.scrollFrame then return end
    local box, scroll, measurer = f.editBox, f.scrollFrame, f.measurer
    local width = scroll.GetWidth and scroll:GetWidth() or 0
    if not width or width <= 0 then width = self.WIDTH - 60 end
    box:SetWidth(width)
    local text = box:GetText() or ""

    local height
    if measurer then
        local usable = width - 8
        if usable < 10 then usable = 10 end
        measurer:SetWidth(usable)
        measurer:SetText(text)
        if type(measurer.GetStringHeight) == "function" then
            local ok, measured = pcall(measurer.GetStringHeight, measurer)
            if ok and type(measured) == "number" and measured > 0 then
                height = measured + 12   -- text insets (4 top + 4 bottom) + slack
            end
        end
    end
    if not height then
        local lines = 0
        for _ in (text .. "\n"):gmatch("([^\n]*)\n") do lines = lines + 1 end
        height = lines * 14 + 12
    end
    local visible = scroll.GetHeight and scroll:GetHeight() or 0
    if not visible or visible <= 0 then visible = 200 end
    if height < visible then height = visible end
    box:SetHeight(height)
end

function CopyFrame:ApplyText(text)
    local f = self.frame
    if not f or not f.editBox then return end
    local box = f.editBox
    text = text or ""
    self.applying = true
    self.expected = text
    box:SetText(text)
    self.applying = false
    self:FitHeight()
end

function CopyFrame:FocusBox()
    local box = self.frame and self.frame.editBox
    if not box then return end
    if type(box.SetFocus) == "function" then pcall(box.SetFocus, box) end
    if type(box.SetCursorPosition) == "function" then pcall(box.SetCursorPosition, box, 0) end
    -- Highlight last: SelectAll-style selection is what makes Ctrl+C instant.
    if type(box.HighlightText) == "function" then pcall(box.HighlightText, box) end
end

function CopyFrame:ReleaseBox()
    local box = self.frame and self.frame.editBox
    if box and type(box.ClearFocus) == "function" then pcall(box.ClearFocus, box) end
end

-- Load one mode into the box. Returns text, reason (reason set when nothing was
-- loaded).
function CopyFrame:Load(mode)
    local f = self:Ensure()
    if mode ~= self.MODE_FULL then mode = self.MODE_LATEST end
    self.mode = mode

    local text, reason, truncated = self.Content(mode)
    self.truncated = truncated == true
    if not text then
        text = (mode == self.MODE_FULL) and "Nothing in the transcript yet."
            or "No completed reply yet."
        self:ApplyText(text)
        reason = reason or "empty"
    else
        self:ApplyText(text)
    end

    -- Mode state: the active mode is not clickable, and the latest-reply mode is
    -- disabled outright when there is no finished reply to copy.
    local latest, full = f.latestButton, f.fullButton
    local function enable(button, on)
        if not button then return end
        if type(button.SetEnabled) == "function" then
            pcall(button.SetEnabled, button, on and true or false)
        elseif on and type(button.Enable) == "function" then
            pcall(button.Enable, button)
        elseif not on and type(button.Disable) == "function" then
            pcall(button.Disable, button)
        end
    end
    enable(latest, mode ~= self.MODE_LATEST and self.LatestText() ~= nil)
    enable(full, mode ~= self.MODE_FULL and self.TranscriptText() ~= nil)
    return text, reason
end

function CopyFrame:Show(mode)
    self:Ensure()
    local text, reason = self:Load(mode or self.mode)
    self.frame:Show()
    -- Recompute against the real viewport: a hidden frame's anchored children have
    -- no laid-out height yet, so the pre-Show measurement can under-size the child.
    self:FitHeight()
    self:FocusBox()
    if self.truncated then
        say(("Transcript clipped to %d characters."):format(self.MAX_LETTERS))
    end
    if reason == "no-reply" then
        say("No completed reply yet.")
    elseif reason == "empty" then
        say("The transcript is empty.")
    end
    return text, reason
end

function CopyFrame:Toggle(mode)
    if self.frame and self.frame:IsShown() then
        self:Hide()
    else
        self:Show(mode)
    end
end

function CopyFrame:Hide()
    if self.frame then self.frame:Hide() end
end

function CopyFrame:Init()
    self:Ensure()
end
