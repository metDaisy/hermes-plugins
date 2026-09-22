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
const tone = status => status === 'failed' || status === 'error' ? 'text-(--dt-destructive)' : status === 'passed' || status === 'success' || status === 'ok' ? 'text-(--ui-accent)' : 'text-(--ui-text-secondary)'
const pill = 'rounded-full border border-(--ui-stroke-secondary) px-2 py-0.5 text-[11px]'
const button = 'rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5 text-xs text-(--ui-text-secondary) transition hover:bg-(--ui-bg-tertiary) hover:text-(--ui-text-primary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent) disabled:cursor-not-allowed disabled:opacity-50'
const select = 'h-8 min-w-0 rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 text-xs text-(--ui-text-primary) outline-none focus:border-(--ui-accent)'
const coreFields = new Set(['timestamp', 'event', 'profile_name', 'session_id', 'task_id', 'turn_id', 'status'])
const fieldLabels = {
  timestamp: '시간', event: 'Event', profile_name: 'Profile', session_id: 'Session', task_id: 'Task',
  turn_id: 'Turn', status: 'Status', tool: 'Tool', skill: 'Skill', validator: 'Validator',
  trigger: 'Trigger', reason: 'Reason', action: 'Action', operation: 'Operation', category: 'Category',
  duration_ms: 'Duration (ms)', provider: 'Provider', rule_id: 'Rule', rules: 'Rules', paths: 'Paths',
  changed_paths: 'Changed paths', generation: 'Generation', validation_status: 'Validation status',
  rule_statuses: 'Rule statuses', missing_rules: 'Missing rules', failed_rules: 'Failed rules',
  expected_providers: 'Expected providers', observed_providers: 'Observed providers',
  completed: 'Completed', failed: 'Failed', interrupted: 'Interrupted', turn_exit_reason: 'Turn exit reason',
}

function useAuditSummary() {
  return useQuery({ queryKey: [ID, 'summary'], queryFn: () => api('/summary'), refetchOnWindowFocus: false })
}

function useAuditEvents(filters, page, pageSize) {
  return useQuery({
    queryKey: [ID, 'events', filters, page, pageSize],
    queryFn: () => api(`/events${query({ profile_name: filters.profile, event_type: filters.eventType, status: filters.status, offset: String(page * pageSize), limit: String(pageSize) })}`),
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

function EventRow({ event, selected, onSelect }) {
  const summary = title(event) || event.event || 'Audit event'
  const context = [event.tool, event.skill, event.validator].filter(Boolean).join(' · ')
  const detailCount = Object.keys(event).filter(key => !coreFields.has(key)).length
  return jsx('button', {
    type: 'button',
    onClick: () => onSelect(event),
    'aria-pressed': selected === event,
    'aria-label': `${event.event || 'Audit event'} ${timestamp(event.timestamp)}`,
    className: `w-full px-4 py-3 text-left transition hover:bg-(--ui-bg-tertiary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-(--ui-accent) ${selected === event ? 'bg-(--ui-bg-tertiary)' : ''}`,
    children: jsxs('div', { className: 'grid gap-1.5', children: [
      jsxs('div', { className: 'flex flex-wrap items-center gap-2 text-[11px]', children: [
        jsx('time', { className: 'font-mono text-(--ui-text-secondary)', children: timestamp(event.timestamp) }),
        jsx('span', { className: `${pill} text-(--ui-text-secondary)`, children: event.event || 'event' }),
        jsx('code', { className: 'text-(--ui-text-tertiary)', children: event.profile_name || 'unknown' }),
        event.status ? jsx('span', { className: `${pill} ${tone(event.status)}`, children: event.status }) : null
      ] }),
      jsx('strong', { className: 'truncate text-sm font-medium text-(--ui-text-primary)', title: summary, children: summary }),
      jsxs('div', { className: 'flex flex-wrap items-center gap-2 text-[11px] text-(--ui-text-tertiary)', children: [
        context ? jsx('span', { className: 'truncate', title: context, children: context }) : null,
        detailCount ? jsx('span', { className: pill, children: `${detailCount}개 상세 필드` }) : null,
        event.session_id ? jsx('span', { className: 'truncate font-mono', title: event.session_id, children: `session ${event.session_id}` }) : null
      ] })
    ] })
  })
}

function Timeline({ events, selected, onSelect }) {
  if (!events.length) return jsx('div', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: '현재 필터와 일치하는 audit event가 없습니다.' })
  return jsx('ol', { className: 'divide-y divide-(--ui-stroke-secondary)', children: events.map((event, index) => jsx('li', { children: jsx(EventRow, { event, selected, onSelect }) }, `${event.timestamp}-${index}`)) })
}

function displayValue(value) {
  if (value == null) return ''
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}

function DetailValue({ value }) {
  const formatted = displayValue(value)
  const structured = typeof value === 'object' && value !== null
  return structured
    ? jsx('pre', { className: 'mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) p-2 font-mono text-[11px] leading-4 text-(--ui-text-secondary)', children: formatted })
    : jsx('dd', { className: 'mt-1 break-words text-xs text-(--ui-text-secondary)', children: formatted })
}

function Details({ event, onClose }) {
  if (!event) return jsx('aside', { className: 'border-t border-(--ui-stroke-secondary) p-5 text-sm text-(--ui-text-secondary) lg:border-l lg:border-t-0', 'aria-label': 'Selected audit event', children: [
    jsx('p', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-text-tertiary)', children: 'Event detail' }),
    jsx('p', { className: 'mt-2', children: 'Timeline에서 event를 선택하면 privacy-safe 상세 정보를 표시합니다.' })
  ] })

  const detailEntries = Object.entries(event).filter(([key]) => !coreFields.has(key))
  return jsxs('aside', { className: 'border-t border-(--ui-stroke-secondary) p-5 lg:border-l lg:border-t-0', 'aria-label': 'Selected audit event', children: [
    jsxs('header', { className: 'flex items-start justify-between gap-3', children: [
      jsx('div', { children: [jsx('p', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-text-tertiary)', children: 'Event detail' }), jsx('h2', { className: 'mt-1 text-base font-semibold text-(--ui-text-primary)', children: event.event || 'Audit event' })] }),
      jsx('button', { type: 'button', className: button, onClick: onClose, 'aria-label': '상세 패널 닫기', children: '닫기' })
    ] }),
    jsx('p', { className: 'mt-2 break-words text-sm text-(--ui-text-secondary)', children: title(event) || '추가 설명이 없는 audit event입니다.' }),
    jsxs('div', { className: 'mt-4 flex flex-wrap gap-2', children: [
      event.status ? jsx('span', { className: `${pill} ${tone(event.status)}`, children: event.status }) : null,
      event.profile_name ? jsx('span', { className: `${pill} text-(--ui-text-secondary)`, children: event.profile_name }) : null,
      event.tool ? jsx('span', { className: `${pill} text-(--ui-text-secondary)`, children: event.tool }) : null
    ] }),
    jsx('dl', { className: 'mt-5 grid gap-3 border-y border-(--ui-stroke-secondary) py-4', children: [
      ['시간', timestamp(event.timestamp)], ['Session', event.session_id], ['Task', event.task_id], ['Turn', event.turn_id], ['Rule', event.rule_id], ['Generation', event.generation]
    ].filter(([, value]) => value !== undefined && value !== null && value !== '').map(([label, value]) => jsxs('div', { children: [jsx('dt', { className: 'text-[11px] text-(--ui-text-tertiary)', children: label }), jsx('dd', { className: 'mt-1 break-all font-mono text-xs text-(--ui-text-secondary)', children: String(value) })] }, label)) }),
    detailEntries.length ? jsxs('section', { className: 'mt-5', children: [jsx('h3', { className: 'text-[11px] font-semibold uppercase tracking-[0.14em] text-(--ui-text-tertiary)', children: 'Metadata' }), jsx('dl', { className: 'mt-3 grid gap-4', children: detailEntries.map(([key, value]) => jsxs('div', { children: [jsx('dt', { className: 'text-[11px] text-(--ui-text-tertiary)', children: fieldLabels[key] || key }), jsx(DetailValue, { value })] }, key)) })] }) : jsx('p', { className: 'mt-5 text-xs text-(--ui-text-tertiary)', children: '추가 metadata가 없는 event입니다.' }),
    jsx('p', { className: 'mt-6 text-[11px] leading-4 text-(--ui-text-tertiary)', children: '표시된 내용은 privacy-safe allowlist를 통과한 audit metadata입니다.' })
  ] })
}

function Pagination({ page, pageSize, total, onPageChange, onPageSizeChange, disabled }) {
  if (!total) return null
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const first = page * pageSize + 1
  const last = Math.min(total, (page + 1) * pageSize)
  return jsxs('nav', { className: 'flex flex-wrap items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-4 py-3', 'aria-label': 'Audit event pagination', children: [
    jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: `${first}–${last} / ${total} events · ${page + 1} / ${pageCount} 페이지` }),
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsxs('label', { className: 'flex items-center gap-1 text-[11px] text-(--ui-text-tertiary)', children: ['페이지 크기', jsx('select', { className: select, value: String(pageSize), disabled, onChange: event => onPageSizeChange(Number(event.target.value)), 'aria-label': '페이지 크기', children: [10, 25, 50, 100].map(size => jsx('option', { value: size, children: `${size}개` }, size)) })] }),
      jsx('button', { type: 'button', className: button, disabled: disabled || page === 0, onClick: () => onPageChange(page - 1), children: '이전' }),
      jsx('button', { type: 'button', className: button, disabled: disabled || page + 1 >= pageCount, onClick: () => onPageChange(page + 1), children: '다음' })
    ] })
  ] })
}

function Page() {
  const [filters, setFilters] = useState({ profile: '', eventType: '', status: '' })
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(25)
  const [selected, setSelected] = useState(null)
  const summary = useAuditSummary()
  const events = useAuditEvents(filters, page, pageSize)
  const updateFilters = updater => {
    setFilters(updater)
    setPage(0)
    setSelected(null)
  }
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
      jsx('div', { children: [jsx('p', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-text-tertiary)', children: 'Privacy-safe workflow evidence' }), jsx('h1', { className: 'mt-1 text-2xl font-semibold tracking-tight text-(--ui-text-primary)', children: 'agent-audit' }), jsx('p', { className: 'mt-1 text-sm text-(--ui-text-secondary)', children: 'Profile, validation rule, session별 SQLite audit event를 조회합니다.' })] }),
      jsx('button', { type: 'button', className: button, disabled: summary.isFetching || events.isFetching, onClick: refresh, children: summary.isFetching || events.isFetching ? '갱신 중…' : '새로고침' })
    ] }),
    jsxs('dl', { className: 'mt-5 flex flex-wrap gap-2', children: [jsx(Metric, { label: 'Event', value: summary.data?.total_events || 0 }), jsx(Metric, { label: 'Profile', value: metrics.profiles }), jsx(Metric, { label: 'Deviation', value: metrics.deviations }), jsx(Metric, { label: 'Failed', value: metrics.failures })] }),
    selected ? jsx('p', { className: 'mt-3 text-xs text-(--ui-accent)', role: 'status', children: `선택됨: ${selected.event || 'audit event'} · 상세 내용이 위에 표시됩니다.` }) : null,
    selected ? jsx(Details, { event: selected, onClose: () => setSelected(null) }) : null,
    summary.error ? jsx('p', { className: 'mt-4 rounded-md border border-(--dt-destructive)/50 p-3 text-sm text-(--dt-destructive)', role: 'alert', children: `Summary를 불러오지 못했습니다: ${summary.error.message || String(summary.error)}` }) : null,
    jsx('section', { className: 'mt-5 overflow-hidden rounded-lg border border-(--ui-stroke-secondary)', children: [jsx(Filters, { summary: summary.data, filters, setFilters: updateFilters }), jsx(Pagination, { page, pageSize, total: events.data?.total || 0, onPageChange: nextPage => { setPage(nextPage); setSelected(null) }, onPageSizeChange: nextSize => { setPageSize(nextSize); setPage(0); setSelected(null) }, disabled: events.isFetching }), events.error ? jsx('p', { className: 'p-4 text-sm text-(--dt-destructive)', role: 'alert', children: `Event를 불러오지 못했습니다: ${events.error.message || String(events.error)}` }) : events.isLoading ? jsx('p', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: 'Audit event를 불러오는 중…' }) : jsx('div', { className: 'min-w-0 overflow-auto', children: jsx(Timeline, { events: events.data?.events || [], selected, onSelect: setSelected }) })] })
  ] })
}

export default {
  id: ID,
  name: 'agent-audit',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PAGE_PATH }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PAGE_PATH, label: 'agent-audit', codicon: 'pulse' } }
    ])
  }
}
