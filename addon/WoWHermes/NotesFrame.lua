--[[---------------------------------------------------------------------------
    WoWHermes / NotesFrame.lua — the saved-notes viewer.

    A separate Blizzard-native window (PortraitFrameTemplate, the same template the
    chat window uses: Display.lua:127) so reading a note never disturbs the live
    conversation. Newest first, full text (Format.Lines styling, Format.lua:60),
    its own minimal scroll bar (Scrollbar.lua) and a "Back to chat" button.

    Nothing here touches the bridge: notes are local SavedVariables only.
--------------------------------------------------------------------------------]]--

local _, ns = ... -- luacheck: ignore 212

local NotesFrame = {}
ns.NotesFrame = NotesFrame

local Format = ns.Format

NotesFrame.WIDTH = 440
NotesFrame.HEIGHT = 500

local COLOR_BODY = { 0.94, 0.94, 0.94 }
local COLOR_SYSTEM = { 0.7, 0.7, 0.7 }

local function stamp(t)
    if type(t) ~= "number" then return "" end
    local ok, text = pcall(date, "%Y-%m-%d %H:%M", t)
    return ok and text or ""
end

function NotesFrame:Ensure()
    if self.frame then return self.frame end

    local f = CreateFrame("Frame", "WoWHermesNotesFrame", UIParent, "PortraitFrameTemplate")
    f:SetSize(self.WIDTH, self.HEIGHT)
    f:SetPoint("CENTER", UIParent, "CENTER", 0, 0)
    f:SetToplevel(true)
    f:SetClampedToScreen(true)
    f:SetMovable(true)
    if f.EnableMouse then pcall(f.EnableMouse, f, true) end
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(frame) frame:StartMoving() end)
    f:SetScript("OnDragStop", function(frame) frame:StopMovingOrSizing() end)
    if f.SetTitle then f:SetTitle("Hermes Notes") end
    f:SetScript("OnShow", function() NotesFrame:Refresh() end)
    if f.CloseButton then
        f.CloseButton:SetScript("OnClick", function() f:Hide() end)
    end
    f:Hide()

    local transcript = CreateFrame("ScrollingMessageFrame", nil, f)
    transcript:SetPoint("TOPLEFT", f, "TOPLEFT", 12, -58)
    -- right edge stops short of the scroll bar (Scrollbar.lua:74-77) so the bar
    -- never draws over a line of text
    transcript:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -34, 54)
    transcript:SetFontObject(ChatFontNormal)
    transcript:SetJustifyH("LEFT")
    transcript:SetFading(false)
    transcript:SetMaxLines(600)
    if transcript.EnableMouseWheel then
        pcall(transcript.EnableMouseWheel, transcript, true)
    end
    transcript:SetScript("OnMouseWheel", function(frame, delta)
        if ns.Display and ns.Display.OnTranscriptWheel then
            ns.Display.OnTranscriptWheel(frame, delta)
        end
    end)
    f.transcript = transcript

    local bar = ns.Scroll and ns.Scroll.CreateBar(f, transcript, 14)
    if bar then
        f.scrollBar = bar
        if not ns.Scroll.Attach(transcript, bar) then
            -- no ScrollUtil on this client: hide rather than leave a dead thumb
            bar:Hide()
        end
    end

    -- "Back to chat" and "Close" are ordinary UIPanelButtonTemplate buttons
    -- (Blizzard_SharedXML/Mainline/SharedUIPanelTemplates.xml: UIPanelButtonTemplate),
    -- the same template as the window's Send button (Display.lua:249).
    local back = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    back:SetSize(120, 22)
    back:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 18, 16)
    back:SetText("Back to chat")
    back:SetScript("OnClick", function() NotesFrame:BackToChat() end)
    f.backButton = back

    local close = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    close:SetSize(90, 22)
    close:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -18, 16)
    close:SetText("Close")
    close:SetScript("OnClick", function() NotesFrame:Hide() end)
    f.closeButton = close

    local hint = f:CreateFontString(nil, "OVERLAY")
    hint:SetFontObject(GameFontDisableSmall)
    hint:SetPoint("BOTTOM", f, "BOTTOM", 0, 22)
    hint:SetTextColor(0.6, 0.6, 0.6)
    hint:SetText("Wheel scrolls  ·  newest first")
    f.hint = hint

    self.frame = f
    return f
end

-- One note block: a gold header line, then the full reply body.
function NotesFrame.AddNote(t, index, note)
    local header = Format.COLORS.gold .. "#" .. index .. "|r "
        .. Format.COLORS.grey .. stamp(note.t) .. "|r"
    if type(note.char) == "string" and note.char ~= "" then
        header = header .. " " .. Format.COLORS.grey .. note.char .. "|r"
    end
    t:AddMessage(header, COLOR_BODY[1], COLOR_BODY[2], COLOR_BODY[3])
    local body = Format.Lines(note.text or "")
    for i = 1, #body do
        t:AddMessage(body[i], COLOR_BODY[1], COLOR_BODY[2], COLOR_BODY[3])
    end
    t:AddMessage(" ", COLOR_SYSTEM[1], COLOR_SYSTEM[2], COLOR_SYSTEM[3])
end

function NotesFrame:Refresh()
    local f = self.frame
    if not f or not f.transcript then return end
    local t = f.transcript
    t:Clear()

    local notes = ns.Notes and ns.Notes.All() or {}
    if #notes == 0 then
        t:AddMessage("No saved notes yet. Press Save under a Hermes reply to keep it.",
            COLOR_SYSTEM[1], COLOR_SYSTEM[2], COLOR_SYSTEM[3])
    else
        for i = 1, #notes do NotesFrame.AddNote(t, i, notes[i]) end
    end

    if f.SetTitle then
        f:SetTitle(("Hermes Notes (%d/%d)"):format(#notes, (ns.Notes and ns.Notes.MAX) or 50))
    end

    -- Notes are listed newest first, so reading starts at the top of the buffer
    -- (insert mode bottom: ScrollToTop = the oldest visible line is the newest note).
    if type(t.ScrollToTop) == "function" then t:ScrollToTop() end
    if f.scrollBar then ns.Scroll.Sync(t, f.scrollBar) end
end

function NotesFrame:BackToChat()
    if self.frame then self.frame:Hide() end
    if ns.Display then ns.Display:Show() end
end

function NotesFrame:Show()
    self:Ensure()
    self:Refresh()
    self.frame:Show()
end

function NotesFrame:Toggle()
    self:Ensure()
    if self.frame:IsShown() then
        self.frame:Hide()
    else
        self:Show()
    end
end

function NotesFrame:Hide()
    if self.frame then self.frame:Hide() end
end

function NotesFrame:Init()
    self:Ensure()
end
