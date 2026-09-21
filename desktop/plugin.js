import { useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { ROUTES_AREA, SIDEBAR_NAV_AREA, queryClient, useQuery } from '@hermes/plugin-sdk'

const ID = 'agent-audit'
const PAGE_PATH = '/agent-audit'
let pluginCtx

const api = path => pluginCtx.rest(path)
const query = params => {
  const values = Object.entries(params).filter(([, value]) => value)
  return values.length ? `?${new URLSearchParams(values).toString()}` : ''
}
const timestamp = value => {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value || '시간 미확인'
  const pad = number => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}
const title = event => [event.tool, event.reason, event.validator, event.skill, event.trigger, event.event].filter(Boolean).join(' · ')
const tone = status => status === 'failed' || status === 'error' ? 'text-(--dt-destructive)' : status === 'passed' || status === 'success' ? 'text-(--ui-accent)' : 'text-(--ui-text-secondary)'
const pill = 'rounded-full border border-(--ui-stroke-secondary) px-2 py-0.5 text-[11px]'
const button = 'rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5 text-xs text-(--ui-text-secondary) transition hover:bg-(--ui-bg-tertiary) hover:text-(--ui-text-primary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent) disabled:cursor-not-allowed disabled:opacity-50'
const select = 'h-8 min-w-0 rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 text-xs text-(--ui-text-primary) outline-none focus:border-(--ui-accent)'

function useAuditSummary() {
  return useQuery({ queryKey: [ID, 'summary'], queryFn: () => api('/summary'), refetchOnWindowFocus: false })
}

function useAuditEvents(filters) {
  return useQuery({
    queryKey: [ID, 'events', filters],
    queryFn: () => api(`/events${query({ profile_name: filters.profile, event_type: filters.eventType, status: filters.status, limit: '100' })}`),
    refetchOnWindowFocus: false
  })
}

function Metric({ label, value }) {
  return jsxs('div', { className: 'min-w-28 rounded-md border border-(--ui-stroke-secondary) px-3 py-2', children: [
    jsx('dt', { className: 'text-[11px] text-(--ui-text-tertiary)', children: label }),
    jsx('dd', { className: 'mt-0.5 text-lg font-semibold text-(--ui-text-primary)', children: String(value) })
  ] })
}

function Filters({ summary, filters, setFilters }) {
  const profiles = Object.keys(summary?.profiles || {})
  const eventTypes = Object.keys(summary?.event_types || {})
  const statuses = Object.keys(summary?.statuses || {})
  const update = key => event => setFilters(current => ({ ...current, [key]: event.target.value }))
  return jsxs('section', { className: 'flex flex-wrap items-end gap-2 border-b border-(--ui-stroke-secondary) px-4 py-3', 'aria-label': 'Audit event filter', children: [
    jsxs('label', { className: 'grid gap-1 text-[11px] text-(--ui-text-tertiary)', children: ['Profile', jsx('select', { className: select, value: filters.profile, onChange: update('profile'), children: [jsx('option', { value: '', children: '모든 Profile' }), ...profiles.map(name => jsx('option', { value: name, children: name }, name))] })] }),
    jsxs('label', { className: 'grid gap-1 text-[11px] text-(--ui-text-tertiary)', children: ['Event', jsx('select', { className: select, value: filters.eventType, onChange: update('eventType'), children: [jsx('option', { value: '', children: '모든 Event' }), ...eventTypes.map(name => jsx('option', { value: name, children: name }, name))] })] }),
    jsxs('label', { className: 'grid gap-1 text-[11px] text-(--ui-text-tertiary)', children: ['Status', jsx('select', { className: select, value: filters.status, onChange: update('status'), children: [jsx('option', { value: '', children: '모든 Status' }), ...statuses.map(name => jsx('option', { value: name, children: name }, name))] })] })
  ] })
}

function Timeline({ events, selected, onSelect }) {
  if (!events.length) return jsx('div', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: '현재 필터와 일치하는 audit event가 없습니다.' })
  return jsx('ol', { className: 'divide-y divide-(--ui-stroke-secondary)', children: events.map((event, index) => jsx('li', { children: jsx('button', {
    type: 'button', onClick: () => onSelect(event), 'aria-pressed': selected === event,
    className: `grid w-full grid-cols-[10.5rem_minmax(8rem,0.7fr)_minmax(12rem,1fr)_auto] items-center gap-3 px-4 py-2.5 text-left text-xs transition hover:bg-(--ui-bg-tertiary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-(--ui-accent) ${selected === event ? 'bg-(--ui-bg-tertiary)' : ''}`,
    children: [jsx('time', { className: 'font-mono text-(--ui-text-secondary)', children: timestamp(event.timestamp) }), jsx('code', { className: 'truncate text-(--ui-text-primary)', title: event.profile_name || 'unknown', children: event.profile_name || 'unknown' }), jsx('span', { className: 'truncate text-(--ui-text-secondary)', title: title(event), children: title(event) }), event.status ? jsx('span', { className: `${pill} ${tone(event.status)}`, children: event.status }) : jsx('span', { className: `${pill} text-(--ui-text-tertiary)`, children: event.event })]
  }) }, `${event.timestamp}-${index}`)) })
}

function Details({ event }) {
  if (!event) return jsx('aside', { className: 'border-l border-(--ui-stroke-secondary) p-4 text-sm text-(--ui-text-secondary)', children: 'Timeline에서 event를 선택하면 privacy-safe 상세 정보를 표시합니다.' })
  const fields = Object.fromEntries(Object.entries(event).filter(([key]) => !['timestamp', 'event', 'profile_name', 'session_id', 'task_id', 'turn_id', 'status'].includes(key)))
  return jsxs('aside', { className: 'min-w-0 border-l border-(--ui-stroke-secondary) p-4', 'aria-label': 'Selected audit event', children: [
    jsx('h2', { className: 'text-sm font-semibold text-(--ui-text-primary)', children: event.event }),
    jsxs('dl', { className: 'mt-3 grid gap-2 text-xs', children: [
      ['시간', timestamp(event.timestamp)], ['Profile', event.profile_name || 'unknown'], ['Session', event.session_id], ['Status', event.status]
    ].filter(([, value]) => value).map(([label, value]) => jsxs('div', { children: [jsx('dt', { className: 'text-(--ui-text-tertiary)', children: label }), jsx('dd', { className: 'mt-0.5 break-all text-(--ui-text-secondary)', children: value })] }, label)) }),
    Object.keys(fields).length ? jsx('pre', { className: 'mt-4 max-h-80 overflow-auto rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) p-3 text-[11px] leading-4 text-(--ui-text-secondary)', children: JSON.stringify(fields, null, 2) }) : null
  ] })
}

function Page() {
  const [filters, setFilters] = useState({ profile: '', eventType: '', status: '' })
  const [selected, setSelected] = useState(null)
  const summary = useAuditSummary()
  const events = useAuditEvents(filters)
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: [ID, 'summary'] }),
    queryClient.invalidateQueries({ queryKey: [ID, 'events'] })
  ])
  const metrics = useMemo(() => ({
    profiles: Object.keys(summary.data?.profiles || {}).length,
    deviations: summary.data?.event_types?.workflow_deviation || 0,
    failures: (summary.data?.statuses?.failed || 0) + (summary.data?.statuses?.error || 0)
  }), [summary.data])
  return jsx('main', { className: 'mx-auto max-w-7xl p-4 sm:p-6', children: [
    jsxs('header', { className: 'flex flex-wrap items-start justify-between gap-4', children: [
      jsx('div', { children: [jsx('p', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-text-tertiary)', children: 'Privacy-safe workflow evidence' }), jsx('h1', { className: 'mt-1 text-2xl font-semibold tracking-tight text-(--ui-text-primary)', children: 'Audit Explorer' }), jsx('p', { className: 'mt-1 text-sm text-(--ui-text-secondary)', children: 'Profile, validation rule, session별 SQLite audit event를 조회합니다.' })] }),
      jsx('button', { type: 'button', className: button, disabled: summary.isFetching || events.isFetching, onClick: refresh, children: summary.isFetching || events.isFetching ? '갱신 중…' : '새로고침' })
    ] }),
    jsxs('dl', { className: 'mt-5 flex flex-wrap gap-2', children: [jsx(Metric, { label: 'Event', value: summary.data?.total_events || 0 }), jsx(Metric, { label: 'Profile', value: metrics.profiles }), jsx(Metric, { label: 'Deviation', value: metrics.deviations }), jsx(Metric, { label: 'Failed', value: metrics.failures })] }),
    summary.error ? jsx('p', { className: 'mt-4 rounded-md border border-(--dt-destructive)/50 p-3 text-sm text-(--dt-destructive)', role: 'alert', children: `Summary를 불러오지 못했습니다: ${summary.error.message || String(summary.error)}` }) : null,
    jsx('section', { className: 'mt-5 overflow-hidden rounded-lg border border-(--ui-stroke-secondary)', children: [jsx(Filters, { summary: summary.data, filters, setFilters }), events.error ? jsx('p', { className: 'p-4 text-sm text-(--dt-destructive)', role: 'alert', children: `Event를 불러오지 못했습니다: ${events.error.message || String(events.error)}` }) : events.isLoading ? jsx('p', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: 'Audit event를 불러오는 중…' }) : jsxs('div', { className: 'grid min-h-96 grid-cols-[minmax(0,1fr)_20rem]', children: [jsx('div', { className: 'min-w-0 overflow-auto', children: jsx(Timeline, { events: events.data?.events || [], selected, onSelect: setSelected }) }), jsx(Details, { event: selected })] })] })
  ] })
}

export default {
  id: ID,
  name: 'Agent Audit Explorer',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PAGE_PATH }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PAGE_PATH, label: 'Audit Explorer', codicon: 'pulse' } }
    ])
  }
}
