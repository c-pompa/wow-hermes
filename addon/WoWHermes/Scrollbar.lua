--[[---------------------------------------------------------------------------
    WoWHermes / Scrollbar.lua — the visible transcript scroll bar.

    Forever evidence (wow-ui-source-forever @ 1.60.1.70862):
      * The chat window's own transcript is a `ScrollingMessageFrame` with a
        `<EventFrame parentKey="ScrollBar" inherits="MinimalScrollBar">` child
        (Blizzard_ChatFrameBase/Mainline/ChatFrame.xml:5-9) and is bound with
        `ScrollUtil.InitScrollingMessageFrameWithScrollBar`
        (Blizzard_ChatFrameBase/Mainline/ChatFrameOverrides.lua:40).
      * That helper is the supported seam between a message frame and a bar
        (Blizzard_SharedXML/Shared/Scroll/ScrollUtil.lua:166-212): it snap-locks
        the bar to whole message lines, drops external position updates while a
        stepper/thumb is held (ScrollBar.lua:155-171, `EnableInternalPriority`),
        turns bar movement into `SetScrollOffset` (ScrollUtil.lua:207-211) and
        re-reads the frame on every display refresh via
        `AddOnDisplayRefreshedCallback` (Blizzard_SharedXML/ScrollingMessageFrame.lua:172-180).
      * `MinimalScrollBar` itself is Blizzard_SharedXML/Shared/Scroll/MinimalScrollBar.xml
        (EventFrame, ScrollBarMixin from Shared/Scroll/ScrollBar.lua:4), loaded by
        Blizzard_SharedXML.toc:184-185 (SecureScrollTemplates.lua/.xml) and the
        ScrollBox files. Both are default-UI code, always present on Forever.

    We deliberately do NOT re-derive the offset<->percentage mapping: the helper
    already handles insert mode (bottom for chat), snapping and drag ownership.
    What stays ours is the mouse wheel handler (Display.OnTranscriptWheel), so the
    helper is called with `noMouseWheel = true`.

    Fallback: if a client lacks ScrollUtil (a pre-ScrollBox build), Attach returns
    false and the bar is hidden. Mouse-wheel scrolling keeps working and the window
    still opens; nothing here is allowed to throw during Display:Ensure.
--------------------------------------------------------------------------------]]--

local _, ns = ... -- luacheck: ignore 212

local Scroll = {}
ns.Scroll = Scroll

-- Verified template name (MinimalScrollBar.xml:14) and the EventFrame type the
-- chat XML uses for its own bar (ChatFrame.xml:5).
Scroll.TEMPLATE = "MinimalScrollBar"
Scroll.FRAME_TYPE = "EventFrame"
Scroll.WIDTH = 8

local function scrollUtil()
    -- rawget: ScrollUtil is not in the generated API annotations, and a
    -- capability probe must not raise on a client that lacks it.
    local util = rawget(_G, "ScrollUtil")
    if type(util) ~= "table" then return nil end
    local fn = util.InitScrollingMessageFrameWithScrollBar
    if type(fn) ~= "function" then return nil end
    return fn
end

-- True when the client can drive a message frame from a minimal bar at all.
function Scroll.Available()
    return scrollUtil() ~= nil
end

-- Create a `MinimalScrollBar` anchored to the right edge of `anchor`, offset by
-- `xOffset` so the bar sits outside the text run (chat does the same: the bar is
-- anchored TOPRIGHT/BOTTOMRIGHT of the message frame, ChatFrame.xml:6-9).
-- Returns the bar or nil; never raises.
function Scroll.CreateBar(parent, anchor, xOffset)
    if not parent or not anchor or type(CreateFrame) ~= "function" then return nil end
    local x = tonumber(xOffset) or 12
    local ok, bar = pcall(CreateFrame, Scroll.FRAME_TYPE, nil, parent, Scroll.TEMPLATE)
    if not ok or type(bar) ~= "table" then return nil end
    if bar.SetPoint then
        bar:SetPoint("TOPRIGHT", anchor, "TOPRIGHT", x, 0)
        bar:SetPoint("BOTTOMRIGHT", anchor, "BOTTOMRIGHT", x, 0)
    end
    if type(bar.SetWidth) == "function" then pcall(bar.SetWidth, bar, Scroll.WIDTH) end
    return bar
end

-- Wire `messageFrame` to `bar`. Returns true when the native helper took over.
function Scroll.Attach(messageFrame, bar)
    if type(messageFrame) ~= "table" or type(bar) ~= "table" then return false end
    local init = scrollUtil()
    if not init then return false end
    -- noMouseWheel = true: Display owns OnMouseWheel (line steps + Shift paging),
    -- the helper would otherwise install its own 3-lines-per-notch handler.
    local ok = pcall(init, messageFrame, bar, true)
    return ok == true
end

-- Recompute the bar from the message frame right now. The helper already does
-- this on every display refresh; this is the explicit path for callers that
-- rebuild the transcript outside a refresh (and for the specs).
function Scroll.Sync(messageFrame, bar)
    if type(messageFrame) ~= "table" or type(bar) ~= "table" then return false end
    if type(messageFrame.GetMaxScrollRange) ~= "function" then return false end
    local maxRange = messageFrame:GetMaxScrollRange() or 0
    local offset = messageFrame:GetScrollOffset() or 0
    local percentage = 0
    local panExtent = 0
    if maxRange > 0 then
        percentage = offset / maxRange
        panExtent = 1 / maxRange
    end
    -- Insert mode bottom (the transcript's): bar 0 is the top of the content.
    local insertMode = 1
    if type(messageFrame.GetInsertMode) == "function" then
        insertMode = messageFrame:GetInsertMode()
    end
    if insertMode ~= 1 then percentage = 1 - percentage end
    local visible = 0
    local messages = messageFrame:GetNumMessages() or 0
    if messages > 1 then visible = 1 / messages end
    if type(bar.SetScrollPercentage) == "function" then
        pcall(bar.SetScrollPercentage, bar, percentage, true)
    end
    if type(bar.SetVisibleExtentPercentage) == "function" then
        pcall(bar.SetVisibleExtentPercentage, bar, visible)
    end
    if type(bar.SetPanExtentPercentage) == "function" then
        pcall(bar.SetPanExtentPercentage, bar, panExtent)
    end
    return true
end
