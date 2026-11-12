/**
 * WoW Hermes — Hermes desktop plugin (settings page + status).
 *
 * Lives in the unified package at hermes_plugin/wow/desktop/plugin.js; the
 * Electron shell copies that half into desktop-plugins/wow/ when the package
 * is installed. It defaults ON so a first-time user gets the setup page without
 * having to discover a second, desktop-specific toggle (the backend remains a
 * separate, required agent-plugin toggle).
 *
 * Plain ESM, loaded uncompiled by the renderer:
 *   - imports only @hermes/plugin-sdk, react, react/jsx-runtime
 *   - UI is jsx()/jsxs() calls, never JSX syntax
 *   - theme variables only, no hardcoded colors
 *
 * Backend contract (dashboard/plugin_api.py, mounted at /api/plugins/wow):
 *   GET  /status -> { connected, character, mode, last_hello, listener,
 *                     sources: { primary, secondary, other } }
 *   POST /setup  -> { ok, steps: [{ step, ok, detail }], token: string|null,
 *                     gateway_url, profile,
 *                     pairing: { addon_zip, bridge_install } }
 *   POST /sources -> { ok, profile, sources, defaults, changed, reset }
 *                    body { profile, primary, secondary, other } | { profile, reset: true }
 * Every field degrades to '—' when the backend has not answered it yet.
 *
 * Component/prop names below were verified against
 * apps/desktop/src/sdk/index.ts and the components' own source — see
 * docs/notes/ws-desktop.md for the list.
 */

import {
  Badge,
  Button,
  CopyButton,
  Input,
  LogView,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Separator,
  StatusDot,
  Tip,
  atom,
  cn,
  host,
  queryClient,
  useMutation,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'wow'
const PAGE_PATH = '/wow'
const STATUS_KEY = [ID, 'status']
const LOGS_KEY = [ID, 'logs']

const DEFAULTS = {
  character: '',
  profile: 'wow',
  mode: 'standard',
  host: '127.0.0.1',
  port: '8791',
  curseforgeApiKey: ''
}

/**
 * Research sources (v1.3) — the per-profile developer setting. Defaults mirror
 * `sources.py`; the backend validates again, so this copy is only for fast
 * inline feedback (the POST is the gate).
 */
const SOURCES_DEFAULTS = { primary: 'wowhead.com', secondary: 'ahledger.com', other: 'web' }
const SOURCE_MAX_CHARS = 100
const OTHER_MAX_HOSTS = 3

// Bare hostname with an optional path prefix; no scheme, port, spaces or quotes.
const HOST_SEGMENT = '[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
const HOST_RE = new RegExp(`^${HOST_SEGMENT}(?:\\.${HOST_SEGMENT})+(?:/[A-Za-z0-9._~%+-]+)*$`)
const PLAIN_HOST_RE = new RegExp(`^${HOST_SEGMENT}(?:\\.${HOST_SEGMENT})+$`)

function hostProblem(label, raw) {
  const value = String(raw === null || raw === undefined ? '' : raw).trim().toLowerCase()
  if (!value) return `${label} cannot be empty.`
  if (value.length > SOURCE_MAX_CHARS) return `${label} must be at most ${SOURCE_MAX_CHARS} characters.`
  if (!HOST_RE.test(value)) {
    return `${label} must be a bare hostname, optionally with a path (e.g. ${SOURCES_DEFAULTS.primary}).`
  }
  return ''
}

function otherProblem(raw) {
  const value = String(raw === null || raw === undefined ? '' : raw).trim().toLowerCase()
  if (!value) return "Other must be 'web', 'none' or up to three hostnames."
  if (value === 'web' || value === 'none') return ''
  const parts = value.split(',').map(part => part.trim())
  if (parts.some(part => part === '')) return 'Other has an empty entry — remove the stray comma.'
  if (parts.length > OTHER_MAX_HOSTS) return `Other accepts at most ${OTHER_MAX_HOSTS} hostnames.`
  for (const host of parts) {
    if (host === 'web' || host === 'none') return "Other cannot mix 'web'/'none' with hostnames."
    if (host.length > SOURCE_MAX_CHARS) return `Each hostname must be at most ${SOURCE_MAX_CHARS} characters.`
    if (!PLAIN_HOST_RE.test(host)) return `Other entry "${host}" must be a bare hostname.`
  }
  return ''
}

function sourceProblems(value) {
  return {
    primary: hostProblem('Primary', value.primary),
    secondary: hostProblem('Secondary', value.secondary),
    other: otherProblem(value.other)
  }
}

function cleanOther(raw) {
  const value = String(raw === null || raw === undefined ? '' : raw).trim().toLowerCase()
  if (value === 'web' || value === 'none') return value
  return value
    .split(',')
    .map(part => part.trim())
    .filter(Boolean)
    .join(', ')
}

/** SegmentedControl options are `{ id, label }` (NOT `{ value, label }`). */
const MODES = [
  { id: 'standard', label: 'Standard' },
  { id: 'wow', label: 'WoW only' },
  { id: 'full', label: 'Full control' }
]

/** Hover/focus copy for each mode — what it grants, before anything is saved. */
const MODE_DESCRIPTION = {
  standard: 'Everyday Hermes tools, including WoW quest and addon help. No full desktop control.',
  wow: 'WoW-only tools plus web search: quests, addons, installs, and game guidance. Best while playing.',
  full: 'Your entire Hermes toolset, including terminal, files, and desktop control. Needs in-game confirmation and expires after 60 minutes.'
}

const MODE_LABEL = MODES.reduce((acc, m) => Object.assign(acc, { [m.id]: m.label }), {})

// ctx.rest / ctx.os only exist inside register(); the page's queryFn runs later,
// so the module holds the bound doors and rejects loudly if called too early.
let rest = () => Promise.reject(new Error('WoW plugin backend is not mounted'))
let osReveal = () => Promise.resolve(false)

const statusQuery = () => rest('/status')

// Form state lives in an SDK atom (module scope) so hot reload and re-renders
// do not drop what the user typed. Handlers read it with .get().
const formAtom = atom({ ...DEFAULTS })
const logsOpenAtom = atom(false)
// Developer section: collapsed by default; `sourcesAtom` holds the uncommitted
// edit (null = show what the profile currently has).
const devOpenAtom = atom(false)
const sourcesAtom = atom(null)

let storage = null
let osOpen = () => false

function setField(key, value) {
  const next = { ...formAtom.get(), [key]: value }
  formAtom.set(next)
  if (storage) storage.set('form', next)
}

// ── formatting helpers ──────────────────────────────────────────────────────

const dash = v => (v === null || v === undefined || v === '' ? '—' : String(v))

/** Accepts epoch seconds, epoch ms, or an ISO string; '—' when unparseable. */
function fmtTime(value) {
  if (value === null || value === undefined || value === '') return '—'

  let ms = null

  if (typeof value === 'number') {
    ms = value < 1e12 ? value * 1000 : value
  } else if (typeof value === 'string') {
    const asNumber = Number(value)
    ms = Number.isFinite(asNumber) && value.trim() !== '' ? (asNumber < 1e12 ? asNumber * 1000 : asNumber) : Date.parse(value)
  }

  if (ms === null || !Number.isFinite(ms)) return dash(value)

  const date = new Date(ms)
  return Number.isNaN(date.getTime()) ? dash(value) : date.toLocaleString()
}

// ── layout atoms ────────────────────────────────────────────────────────────

function Field({ label, hint, error, children }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsx('div', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: label }),
      children,
      error
        ? jsx('div', { className: 'text-[0.6875rem] text-destructive', children: error })
        : hint
          ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: hint })
          : null
    ]
  })
}

function Row({ label, children }) {
  return jsxs('div', {
    className: 'flex items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) py-1.5 text-xs last:border-b-0',
    children: [
      jsx('span', { className: 'shrink-0 text-(--ui-text-secondary)', children: label }),
      jsx('span', { className: 'min-w-0 truncate text-right text-(--ui-text-primary)', children })
    ]
  })
}

function Section({ title, description, children }) {
  return jsxs('section', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex flex-col gap-0.5',
        children: [
          jsx('h2', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: title }),
          description ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: description }) : null
        ]
      }),
      children
    ]
  })
}

function Copyable({ text, appearance = 'icon', label }) {
  return jsx(CopyButton, { appearance, text, label, buttonSize: 'xs' })
}

/**
 * Mode picker with per-option hover/focus copy.
 *
 * Deliberately NOT the SDK `SegmentedControl`: its option shape is only
 * `{ id, label, icon? }` (apps/desktop/src/components/ui/segmented-control.tsx)
 * and it spreads nothing onto the option buttons, so there is no way to attach a
 * title or wrap each option in `Tip`. This mirrors that control's markup and
 * classes exactly — same track, pill, sizes, `aria-pressed` — and wraps each
 * button in the app's `Tip` primitive, which opens on hover AND keyboard focus
 * (Radix sets `aria-describedby` while open, so screen readers hear the copy).
 * Mode semantics are unchanged: `onChange(id)` only.
 */
function ModeToggle({ value, onChange }) {
  return jsx('div', {
    role: 'group',
    'aria-label': 'Default mode',
    className: 'inline-grid w-fit auto-cols-fr grid-flow-col gap-0.5 rounded-[5px] bg-(--ui-bg-tertiary) p-0.5',
    children: MODES.map(m => {
      const active = value === m.id

      return jsx(Tip, {
        key: m.id,
        label: MODE_DESCRIPTION[m.id],
        children: jsx('button', {
          type: 'button',
          'aria-pressed': active,
          className: cn(
            'flex items-center justify-center gap-1 rounded-[3px] px-2.5 py-0.5 text-[0.6875rem] font-medium transition-colors hover:transition-none',
            active ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
          ),
          onClick: () => onChange(m.id),
          children: m.label
        })
      })
    })
  })
}

// ── status ──────────────────────────────────────────────────────────────────

function StatusSection({ query }) {
  const s = query.data || {}
  const connected = Boolean(s.connected)
  const listening = Boolean(s.listener)

  return jsxs(Section, {
    title: 'Status',
    description: 'Read from the plugin backend; refreshes every 10 seconds.',
    children: [
      jsxs('div', {
        className: 'flex flex-col rounded-md border border-(--ui-stroke-tertiary) px-2.5 py-1',
        children: [
          jsx(Row, {
            label: 'Adapter',
            children: jsxs('span', {
              className: 'inline-flex items-center gap-1.5',
              children: [
                jsx(StatusDot, { tone: connected ? 'good' : 'bad' }),
                jsx('span', { children: connected ? 'connected' : 'offline' })
              ]
            })
          }),
          jsx(Row, {
            label: 'Listener',
            children: listening ? 'accepting' : 'not listening'
          }),
          s.health_error && !connected
            ? jsx(Row, { label: 'Health', children: String(s.health_error) })
            : null,
          jsx(Row, { label: 'Character', children: dash(s.character) }),
          jsx(Row, {
            label: 'Current mode',
            children: s.mode
              ? jsx(Tip, {
                  label: MODE_DESCRIPTION[s.mode] || `${MODE_LABEL[s.mode] || String(s.mode)} mode.`,
                  children: jsx('span', {
                    // Badge forwards no ref, so Tip wraps a focusable span instead.
                    tabIndex: 0,
                    className: 'inline-flex rounded-[3px] focus-visible:outline-hidden focus-visible:ring-[0.1875rem] focus-visible:ring-ring/50',
                    children: jsx(Badge, { variant: 'muted', children: MODE_LABEL[s.mode] || String(s.mode) })
                  })
                })
              : '—'
          }),
          jsx(Row, { label: 'Last bridge hello', children: fmtTime(s.last_hello) })
        ]
      }),
      jsxs('div', {
        className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx(Button, {
            size: 'xs',
            variant: 'ghost',
            onClick: () => query.refetch(),
            children: query.isFetching ? 'Refreshing…' : 'Refresh'
          }),
          query.isError ? jsx(Badge, { variant: 'destructive', children: 'backend unreachable' }) : null
        ]
      })
    ]
  })
}

// ── setup result ────────────────────────────────────────────────────────────

function Steps({ steps }) {
  if (!Array.isArray(steps) || steps.length === 0) {
    return jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'No steps reported.' })
  }

  return jsx('div', {
    className: 'flex flex-col gap-1',
    children: steps.map((step, index) => {
      const ok = step && step.ok === true

      return jsxs(
        'div',
        {
          className: 'flex items-start gap-2 text-xs',
          children: [
            jsx(StatusDot, { tone: ok ? 'good' : 'bad', className: 'mt-1.5' }),
            jsxs('div', {
              className: 'flex min-w-0 flex-col',
              children: [
                jsx('span', { className: 'text-(--ui-text-primary)', children: dash(step && step.step) }),
                step && step.detail
                  ? jsx('span', { className: 'select-text break-words text-[0.6875rem] text-(--ui-text-tertiary)', children: String(step.detail) })
                  : null
              ]
            })
          ]
        },
        `step-${index}`
      )
    })
  })
}

function Pairing({ data }) {
  if (!data) return null

  const pairing = data.pairing || {}
  const gatewayUrl = data.gateway_url
  const token = data.token
  const addonZip = pairing.addon_zip
  const bridgeInstall = pairing.bridge_install

  return jsxs(Section, {
    title: 'Pairing',
    description: 'Get the addon onto the gaming PC and point it at this gateway.',
    children: [
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'Gateway URL' }),
              gatewayUrl ? jsx(Copyable, { text: gatewayUrl }) : null
            ]
          }),
          jsx(LogView, { className: 'max-h-24', children: dash(gatewayUrl) })
        ]
      }),
      token
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'Bridge token — shown once' }),
                  jsx(Copyable, { text: token })
                ]
              }),
              jsx(LogView, { className: 'max-h-24', children: token }),
              jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: 'Paste it into the bridge config on the gaming PC. It will not be shown again; rotate it only with an explicit token rotation.'
              })
            ]
          })
        : jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: 'Token unchanged (already configured). It is only returned once, on the setup that created it.'
          }),
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'Addon package' }),
              addonZip
                ? jsx(Button, {
                    size: 'xs',
                    variant: 'ghost',
                    onClick: () => osReveal(addonZip),
                    children: 'Reveal zip'
                  })
                : null
            ]
          }),
          jsx(LogView, { className: 'max-h-24', children: dash(addonZip) })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'Bridge installer (gaming PC)' }),
              bridgeInstall ? jsx(Copyable, { text: bridgeInstall }) : null
            ]
          }),
          jsx(LogView, { className: 'max-h-24', children: dash(bridgeInstall) })
        ]
      })
    ]
  })
}

// ── logs ────────────────────────────────────────────────────────────────────

function LogsPanel() {
  const logs = useQuery({
    queryKey: LOGS_KEY,
    queryFn: () => host.logs({ lines: 120, search: 'wow' }),
    refetchInterval: 15000
  })
  const lines = (logs.data && Array.isArray(logs.data.lines) && logs.data.lines) || []

  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: logs.data && logs.data.file ? logs.data.file : 'gateway log' }),
      logs.isError
        ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Could not read logs from the gateway.' })
        : jsx(LogView, {
            className: 'max-h-64',
            children: lines.length ? lines.join('\n') : 'No WoW lines in the last 120 log entries.'
          })
    ]
  })
}

function LogsSection() {
  const open = useValue(logsOpenAtom)

  return jsxs(Section, {
    title: 'Logs',
    description: 'Gateway log lines mentioning the WoW adapter.',
    children: [
      jsx(Button, {
        size: 'xs',
        variant: 'outline',
        onClick: () => logsOpenAtom.set(!logsOpenAtom.get()),
        children: open ? 'Hide logs' : 'Show logs'
      }),
      open ? jsx(LogsPanel, {}) : null
    ]
  })
}

// ── developer: research sources ─────────────────────────────────────────────

/**
 * Per-profile research sources (v1.3). Collapsed by default so the setup page
 * stays novice-friendly; the values are read from `GET /status.sources` and
 * saved with `POST /sources` for the profile named in the form above.
 */
function DeveloperSection({ status, profile }) {
  const open = useValue(devOpenAtom)
  const draft = useValue(sourcesAtom)
  const statusProfile = status && status.data ? status.data.profile : null
  // Only trust /status when it answered for the profile this form names —
  // otherwise the page would show another profile's sources as current.
  const matchesProfile = !statusProfile || statusProfile === profile
  const live = (matchesProfile && status && status.data && status.data.sources) || SOURCES_DEFAULTS
  const liveKnown = matchesProfile && Boolean(status && status.data && status.data.sources)
  const value = draft || { primary: live.primary, secondary: live.secondary, other: live.other }
  const problems = sourceProblems(value)
  const invalid = Boolean(problems.primary || problems.secondary || problems.other)

  const save = useMutation({
    mutationFn: body => rest('/sources', { method: 'POST', body, timeoutMs: 15000 }),
    onSuccess: () => {
      sourcesAtom.set(null)
      queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  })

  const edit = (key, next) => sourcesAtom.set({ ...value, [key]: next })
  const submit = () =>
    save.mutate({
      profile,
      primary: String(value.primary || '').trim().toLowerCase(),
      secondary: String(value.secondary || '').trim().toLowerCase(),
      other: cleanOther(value.other)
    })
  const resetToDefaults = () => save.mutate({ profile, reset: true })

  const errorText = save.isError && save.error ? String((save.error && save.error.message) || save.error) : null
  const saved = save.data && save.data.sources ? save.data.sources : null

  return jsxs(Section, {
    title: 'Developer',
    description: 'Where in-game answers may look things up. Saved per profile; the next message uses it.',
    children: [
      jsx(Button, {
        size: 'xs',
        variant: 'outline',
        onClick: () => devOpenAtom.set(!devOpenAtom.get()),
        children: open ? 'Hide developer options' : 'Show developer options'
      }),
      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
        children: liveKnown
          ? `Primary ${dash(live.primary)} · secondary ${dash(live.secondary)} · other ${dash(live.other)}`
          : "Current: not loaded yet — save, then reopen to see this profile's stored values"
      }),
      open
        ? jsxs('div', {
            className: 'flex flex-col gap-3',
            children: [
              jsxs('div', {
                className: 'grid grid-cols-1 gap-3 md:grid-cols-3',
                children: [
                  jsx(Field, {
                    label: 'Primary source',
                    hint: 'Searched first. A hostname, optionally with a path.',
                    error: problems.primary || null,
                    children: jsx(Input, {
                      value: value.primary,
                      placeholder: SOURCES_DEFAULTS.primary,
                      onChange: e => edit('primary', e.target.value)
                    })
                  }),
                  jsx(Field, {
                    label: 'Secondary source',
                    hint: 'Searched when the primary has nothing.',
                    error: problems.secondary || null,
                    children: jsx(Input, {
                      value: value.secondary,
                      placeholder: SOURCES_DEFAULTS.secondary,
                      onChange: e => edit('secondary', e.target.value)
                    })
                  }),
                  jsx(Field, {
                    label: 'Other',
                    hint: "web (general search), none, or up to 3 hostnames separated by commas.",
                    error: problems.other || null,
                    children: jsx(Input, {
                      value: value.other,
                      placeholder: SOURCES_DEFAULTS.other,
                      onChange: e => edit('other', e.target.value)
                    })
                  })
                ]
              }),
              jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: 'Replies that used a lookup end with one plain line: Source: <domain>.'
              }),
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(Button, {
                    size: 'xs',
                    disabled: invalid || save.isPending,
                    onClick: submit,
                    children: save.isPending ? 'Saving…' : 'Save sources'
                  }),
                  jsx(Button, {
                    size: 'xs',
                    variant: 'ghost',
                    disabled: save.isPending,
                    onClick: resetToDefaults,
                    children: 'Reset to defaults'
                  }),
                  errorText ? jsx(Badge, { variant: 'destructive', children: 'sources not saved' }) : null,
                  saved && !errorText ? jsx(Badge, { variant: 'success', children: 'saved' }) : null
                ]
              }),
              errorText
                ? jsx('div', {
                    className: 'select-text break-words text-[0.6875rem] text-destructive',
                    children: errorText
                  })
                : null,
              saved
                ? jsx('div', {
                    className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: `Saved: primary ${dash(saved.primary)} · secondary ${dash(saved.secondary)} · other ${dash(saved.other)}`
                  })
                : null
            ]
          })
        : null
    ]
  })
}

// ── page ────────────────────────────────────────────────────────────────────

function Page() {
  const form = useValue(formAtom)
  const status = useQuery({ queryKey: STATUS_KEY, queryFn: statusQuery, refetchInterval: 10000, retry: 0 })
  const save = useMutation({
    mutationFn: body => rest('/setup', { method: 'POST', body, timeoutMs: 180000 }),
    onSuccess: result => {
      if (storage && result && result.profile) storage.set('last_profile', result.profile)
      queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  })

  const result = save.data || null
  const statusData = status.data || null

  const submit = () => {
    const current = formAtom.get()
    save.mutate({
      character: current.character.trim(),
      profile_name: current.profile.trim() || DEFAULTS.profile,
      default_mode: current.mode,
      host: current.host.trim() || DEFAULTS.host,
      port: Number.parseInt(current.port, 10) || Number(DEFAULTS.port),
      // write-only: empty string = leave the stored key unchanged (never prefilled)
      curseforge_api_key: current.curseforgeApiKey
    })
  }

  const errorText = save.isError && save.error ? String((save.error && save.error.message) || save.error) : null

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-auto p-4 text-sm',
    children: [
      // header
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(StatusDot, { tone: statusData ? (statusData.connected ? 'good' : 'bad') : 'muted' }),
          jsx('span', { className: 'text-sm font-semibold text-(--ui-text-primary)', children: 'World of Warcraft' }),
          jsx(Badge, { variant: 'muted', children: 'in-game chat' }),
          result && result.ok ? jsx(Badge, { variant: 'success', children: 'configured' }) : null
        ]
      }),

      jsx(Separator, {}),

      // setup form
      jsxs(Section, {
        title: 'Setup',
        description: 'Save creates or updates the Hermes profile, adapter environment, and the WoW-side pairing.',
        children: [
          jsxs('div', {
            className: 'grid grid-cols-1 gap-3 md:grid-cols-2',
            children: [
              jsx(Field, {
                label: 'Character (Name-Realm)',
                hint: 'Exactly as it appears in game, e.g. Thrall-Area52.',
                children: jsx(Input, {
                  value: form.character,
                  placeholder: 'Thrall-Area52',
                  onChange: e => setField('character', e.target.value)
                })
              }),
              jsx(Field, {
                label: 'Hermes profile',
                hint: 'Owns this character\u2019s sessions.',
                children: jsx(Input, {
                  value: form.profile,
                  placeholder: 'wow',
                  onChange: e => setField('profile', e.target.value)
                })
              }),
              jsx(Field, {
                label: 'Default mode',
                hint: 'The character can switch later in game with /mode.',
                children: jsxs('div', {
                  className: 'flex flex-col gap-1',
                  children: [
                    jsx(ModeToggle, {
                      value: form.mode,
                      onChange: id => setField('mode', id)
                    }),
                    jsx('div', {
                      className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: MODE_DESCRIPTION[form.mode] || ''
                    })
                  ]
                })
              }),
              jsx(Field, {
                label: 'CurseForge API key (optional)',
                hint: 'Only needed for automatic addon installs. Saved as a Hermes secret in the selected profile (this profile only), never shown again. Leave blank to keep the current key or use guided links.',
                children: jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx(Input, {
                      value: form.curseforgeApiKey,
                      type: 'password',
                      placeholder: 'paste your key',
                      autoComplete: 'off',
                      className: 'min-w-0 flex-1',
                      onChange: e => setField('curseforgeApiKey', e.target.value)
                    }),
                    jsx(Button, {
                      variant: 'ghost',
                      onClick: () => osOpen('https://console.curseforge.com'),
                      children: 'Get a key'
                    })
                  ]
                })
              }),
              jsx(Field, {
                label: 'Bridge host / port',
                hint: 'Where the adapter listens for the gaming PC.',
                children: jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx(Input, {
                      value: form.host,
                      placeholder: DEFAULTS.host,
                      className: 'min-w-0 flex-1',
                      onChange: e => setField('host', e.target.value)
                    }),
                    jsx(Input, {
                      value: form.port,
                      placeholder: DEFAULTS.port,
                      inputMode: 'numeric',
                      className: 'w-24',
                      onChange: e => setField('port', e.target.value)
                    })
                  ]
                })
              })
            ]
          }),
          jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: 'New to this? No CurseForge app is needed for chat. To click-install addons instead, get the app at curseforge.com/download/app — or paste a key above and let Hermes install them for you.'
          }),
          form.mode === 'full'
            ? jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: 'Full control is time-boxed (60 min) and needs a two-step confirm in game: /mode full, then /mode full confirm.'
              })
            : null,
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Button, {
                disabled: save.isPending || form.character.trim() === '',
                onClick: submit,
                children: save.isPending ? 'Saving…' : 'Save'
              }),
              errorText
                ? jsx(Badge, { variant: 'destructive', children: 'setup failed' })
                : null,
              errorText
                ? jsx('span', { className: 'select-text break-words text-[0.6875rem] text-(--ui-text-tertiary)', children: errorText })
                : null
            ]
          })
        ]
      }),

      // result of the last save
      result
        ? jsx(Section, {
            title: result.ok ? 'Setup result' : 'Setup failed',
            description: 'Steps run by the backend on the last save.',
            children: jsx(Steps, { steps: result.steps })
          })
        : null,

      result ? jsx(Pairing, { data: result }) : null,

      jsx(Separator, {}),

      jsx(StatusSection, { query: status }),

      jsx(Separator, {}),

      jsx(LogsSection, {}),

      jsx(Separator, {}),

      jsx(DeveloperSection, {
        status,
        profile: form.profile.trim() || DEFAULTS.profile
      }),

      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
        children: result && result.profile ? `Profile: ${result.profile}` : 'Profile: —'
      })
    ]
  })
}

// ── statusbar chip ──────────────────────────────────────────────────────────

function StatusChip() {
  const query = useQuery({ queryKey: STATUS_KEY, queryFn: statusQuery, refetchInterval: 15000, retry: 0 })
  const s = query.data || {}
  const connected = Boolean(s.connected)
  const detail = connected
    ? ['Adapter connected', s.character ? `character ${s.character}` : null, s.mode ? `mode ${MODE_LABEL[s.mode] || s.mode}` : null]
        .filter(Boolean)
        .join(' · ')
    : query.isError
      ? 'WoW backend unreachable — open setup'
      : 'WoW adapter offline — open setup'

  return jsx(Tip, {
    label: detail,
    children: jsxs('button', {
      type: 'button',
      className: cn(
        'inline-flex h-full cursor-pointer items-center gap-1.5 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      onClick: () => host.navigate(PAGE_PATH),
      children: [
        jsx(StatusDot, { tone: query.data ? (connected ? 'good' : 'bad') : 'muted' }),
        jsx('span', { children: connected && s.character ? s.character : 'WoW' })
      ]
    })
  })
}

// ── plugin ──────────────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'WoW Hermes',
  description: 'World of Warcraft in-game chat: one-screen setup, pairing, and adapter status.',
  defaultEnabled: true,
  register(ctx) {
    rest = (path, opts) => ctx.rest(path, opts)
    osReveal = path => ctx.os.revealPath(path)
    osOpen = url => ctx.os.openExternal(url)
    storage = ctx.storage

    const stored = ctx.storage.get('form', null)
    if (stored && typeof stored === 'object') formAtom.set({ ...DEFAULTS, ...stored })

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      title: 'World of Warcraft',
      order: 200,
      data: { path: PAGE_PATH },
      render: () => jsx(Page, {})
    })

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: PAGE_PATH, label: 'World of Warcraft', codicon: 'game' }
    })

    ctx.register({
      id: 'open-setup',
      area: PALETTE_AREA,
      data: {
        id: 'wow.open-setup',
        label: 'WoW: open setup & status',
        keywords: ['wow', 'warcraft', 'bridge', 'character', 'adapter'],
        detail: () => 'profile & pairing',
        detailVariant: 'muted',
        run: () => host.navigate(PAGE_PATH)
      }
    })

    ctx.register({
      id: 'status-chip',
      area: 'statusBar.right',
      order: 145,
      render: () => jsx(StatusChip, {})
    })

    // Live push from the Python half; polling stays the fallback.
    ctx.onEvent(`plugin.${ID}.bridge.seen`, () => queryClient.invalidateQueries({ queryKey: STATUS_KEY }))
  }
}
