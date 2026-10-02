import { useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { ROUTES_AREA, SIDEBAR_NAV_AREA, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, queryClient, useQuery } from '@hermes/plugin-sdk'

const ID = 'agent-audit'
const PAGE_PATH = '/agent-audit'
const ALL = '__all__'
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
const pill = 'rounded-full border border-(--ui-stroke-secondary) px-2 py-0.5 text-[11px]'
const button = 'rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1.5 text-xs text-(--ui-text-secondary) transition hover:bg-(--ui-bg-tertiary) hover:text-(--ui-text-primary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent) disabled:cursor-not-allowed disabled:opacity-50'
const selectTrigger = 'h-8 min-w-40 text-xs'

const statusLabel = state => ({
  succeeded: '성공', success: '성공', passed: '통과', ok: '성공', allow: '완료 가능',
  failed: '실패', error: '오류', interrupted: '중단', cancelled: '취소',
  not_run: '검증 필요', continue: '추가 작업 필요', unknown: '상태 미확인'
}[state] || state || '상태 미확인')
const eventTypeLabel = type => ({
  discovery_observation: '코드 구조 조사', session_end: 'Agent 작업 종료',
  skill_lifecycle: 'Skill 사용', tool_call: '도구 사용', validation_result: '검증 결과',
  validation_trigger: '검증 필요', verification_gate: '완료 조건 확인', workflow_deviation: '워크플로 이탈'
}[type] || type)
const tone = (state, importance) => importance === 'error' || state === 'failed' || state === 'error'
  ? 'text-(--dt-destructive)'
  : importance === 'warning' || state === 'not_run' || state === 'continue'
    ? 'text-(--dt-warning)'
    : state === 'succeeded' || state === 'passed' || state === 'success' || state === 'ok'
      ? 'text-(--ui-accent)'
      : 'text-(--ui-text-secondary)'
const modelKindLabel = kind => kind === 'local' ? '로컬 모델' : kind === 'cloud' ? '클라우드 모델' : '모델 유형 미확인'
const activityCode = event => {
  if (event.activity?.code) return event.activity.code
  const tool = event.tool
  if (tool === 'read_file') return 'filesystem.read'
  if (tool === 'search_files') return 'filesystem.search'
  if (tool === 'write_file' || tool === 'patch') return 'filesystem.modify'
  if (tool === 'terminal') return 'system.command'
  if (tool === 'computer_use') return 'desktop.interact'
  if (tool === 'browser_exec') return 'web.interact'
  if (tool === 'web_extract') return 'web.research'
  if (tool === 'skill_view') return 'skill.inspect'
  if (tool === 'todo_list') return 'workflow.plan'
  if (tool === 'mcp__codebase_memory_mcp__index_repository') return 'code.index'
  if (typeof tool === 'string' && (tool.startsWith('mcp__semble_mcp__') || tool.startsWith('mcp__codebase_memory_mcp__'))) return 'code.discovery'
  return undefined
}
const activityTitle = event => {
  const code = activityCode(event)
  const state = event.outcome?.state || event.status
  if (code === 'filesystem.search') return '프로젝트 파일을 검색했습니다'
  if (code === 'filesystem.read') return '파일 내용을 확인했습니다'
  if (code === 'filesystem.modify') return '프로젝트 파일을 수정했습니다'
  if (code === 'system.command') return '명령을 실행했습니다'
  if (code === 'desktop.interact') return 'Desktop UI와 상호작용했습니다'
  if (code === 'web.interact' || code === 'web.research') return '웹에서 정보를 확인했습니다'
  if (code === 'skill.lifecycle') return 'Skill을 사용했습니다'
  if (code === 'skill.inspect') return 'Skill 사용 방법을 확인했습니다'
  if (code === 'code.discovery') return '코드 구조를 조사했습니다'
  if (code === 'code.index') return '코드 검색 인덱스를 갱신했습니다'
  if (code === 'workflow.plan') return '작업 계획을 갱신했습니다'
  if (code === 'validation.trigger') return '변경으로 검증이 필요해졌습니다'
  if (code === 'validation.result') return state === 'failed' ? '검증이 실패했습니다' : '검증이 통과했습니다'
  if (code === 'validation.verification_gate') return state === 'succeeded' ? '완료 조건을 충족했습니다' : '완료 전에 추가 검증이 필요합니다'
  if (code === 'workflow.deviation') return '워크플로 규칙을 지키지 못했습니다'
  if (code === 'session.end') return 'Agent 작업이 종료되었습니다'
  const fallback = event.tool || event.skill || event.validator || event.kind || event.event
  return fallback ? `${fallback} 활동을 기록했습니다` : 'Agent 활동을 기록했습니다'
}
const eventSummary = event => {
  const profile = event.actor?.profile || event.profile_name || '알 수 없는 Profile'
  const paths = event.scope?.paths || event.paths || event.changed_paths || []
  const scope = paths.length ? `${paths.slice(0, 3).join(', ')}${paths.length > 3 ? ` 외 ${paths.length - 3}개` : ''}` : null
  const code = activityCode(event)
  if (code === 'filesystem.search') return `${profile} Profile이${scope ? ` ${scope} 범위에서` : ''} 프로젝트 파일을 검색했습니다.`
  if (code === 'filesystem.read') return `${profile} Profile이${scope ? ` ${scope}` : ' 프로젝트 파일'} 내용을 확인했습니다.`
  if (code === 'filesystem.modify') return `${profile} Profile이${scope ? ` ${scope}` : ' 프로젝트 파일'}을 수정했습니다.`
  if (code === 'system.command') return `${profile} Profile이 로컬 개발 환경에서 명령을 실행했습니다.`
  if (code === 'desktop.interact') return `${profile} Profile이 Hermes 또는 다른 Desktop 화면을 확인하고 조작했습니다.`
  if (code === 'web.interact' || code === 'web.research') return `${profile} Profile이 웹 페이지에서 필요한 정보를 확인했습니다.`
  if (code === 'skill.lifecycle') return `${profile} Profile이 ${event.activity?.skill || event.skill || 'Skill'}을 사용했습니다.`
  if (code === 'skill.inspect') return `${profile} Profile이 작업에 필요한 Skill 지침을 확인했습니다.`
  if (code === 'code.discovery') return `${profile} Profile이 ${event.activity?.provider || event.scope?.provider || event.provider || 'discovery provider'}로 코드 구조를 조사했습니다.`
  if (code === 'code.index') return `${profile} Profile이 코드 검색과 관계 분석에 사용하는 인덱스를 갱신했습니다.`
  if (code === 'workflow.plan') return `${profile} Profile이 현재 작업 단계와 진행 상태를 갱신했습니다.`
  if (code?.startsWith('validation.')) return `${profile} Profile의 변경 사항에 대한 검증 상태를 기록했습니다.`
  if (code === 'workflow.deviation') return `${profile} Profile에서 정해진 작업 절차와 다른 행동이 감지되었습니다.`
  if (code === 'session.end') return `${profile} Profile의 Agent 작업이 종료되었습니다.`
  return `${profile} Profile의 privacy-safe 활동 기록입니다.`
}
const nextAction = event => ({
  missing_validation: '변경 범위에 필요한 검증을 실행한 뒤 결과를 다시 확인하세요.',
  failed_validation: '실패한 검증의 원인을 수정하고 같은 검증을 다시 실행하세요.',
  unresolved_mutation: '변경 경로를 확인할 수 있는 방식으로 수정 작업을 다시 수행하세요.',
  direct_gradle_invocation: 'Gradle 작업은 gradle-mcp를 통해 다시 실행하세요.',
  kanban_create_without_expected_discovery: 'Task를 만들기 전에 Semble과 Codebase Memory로 코드 구조를 조사하세요.',
  complete_required_validation: '필요한 검증을 완료한 뒤 작업 완료 여부를 다시 확인하세요.'
}[event.explanation?.next_action_code || event.evidence?.reason_code || event.reason] || null)
const eventKey = (event, index = 0) => String(event.id ?? `${event.timestamp || 'event'}-${index}`)

function useAuditSummary() {
  return useQuery({ queryKey: [ID, 'summary'], queryFn: () => api('/summary'), refetchOnWindowFocus: false })
}

function useAuditEvents(filters, page, pageSize) {
  return useQuery({
    queryKey: [ID, 'events', filters, page, pageSize],
    queryFn: () => api(`/events${query({ project_name: filters.project, profile_name: filters.profile, event_type: filters.eventType, status: filters.status, offset: String(page * pageSize), limit: String(pageSize) })}`),
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
  const projects = Object.keys(summary?.projects || {})
  const profiles = Object.keys(summary?.profiles || {})
  const eventTypes = Object.keys(summary?.event_types || {})
  const statuses = Object.keys(summary?.statuses || {})
  const update = key => value => setFilters(current => ({ ...current, [key]: value === ALL ? '' : value }))
  return jsxs('section', { className: 'flex flex-wrap items-end gap-2 border-b border-(--ui-stroke-secondary) px-4 py-3', 'aria-label': 'Audit event filter', children: [
    jsx(FilterSelect, { label: 'Project', value: filters.project, onChange: update('project'), allLabel: '모든 Project', options: projects }),
    jsx(FilterSelect, { label: 'Profile', value: filters.profile, onChange: update('profile'), allLabel: '모든 Profile', options: profiles }),
    jsx(FilterSelect, { label: '활동 유형', value: filters.eventType, onChange: update('eventType'), allLabel: '모든 활동', options: eventTypes, format: eventTypeLabel }),
    jsx(FilterSelect, { label: '결과', value: filters.status, onChange: update('status'), allLabel: '모든 결과', options: statuses, format: statusLabel })
  ] })
}

function FilterSelect({ label, value, onChange, allLabel, options, format = value => value }) {
  return jsxs('label', { className: 'grid gap-1 text-[11px] text-(--ui-text-tertiary)', children: [
    label,
    jsxs(Select, { value: value || ALL, onValueChange: onChange, children: [
      jsx(SelectTrigger, { className: selectTrigger, 'aria-label': label, children: jsx(SelectValue, {}) }),
      jsx(SelectContent, { children: [
        jsx(SelectItem, { value: ALL, children: allLabel }),
        ...options.map(option => jsx(SelectItem, { value: option, children: format(option) }, option))
      ] })
    ] })
  ] })
}

function ModelBadge({ model }) {
  if (!model?.name) return jsx('span', { className: `${pill} text-(--ui-text-tertiary)`, children: '모델 미확인' })
  return jsx('span', { className: `${pill} text-(--ui-text-secondary)`, title: model.provider || model.name, children: `${modelKindLabel(model.kind)} · ${model.name}` })
}

function EventRow({ event, eventId, selected, onSelect }) {
  const heading = activityTitle(event)
  const state = event.outcome?.state || event.status || 'unknown'
  const detailsId = `audit-detail-${eventId}`
  return jsx('button', {
    type: 'button',
    onClick: () => onSelect(selected ? null : eventId),
    'aria-expanded': selected,
    'aria-controls': detailsId,
    'aria-label': `${heading}, ${timestamp(event.timestamp)}`,
    className: `w-full px-4 py-3 text-left transition hover:bg-(--ui-bg-tertiary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-(--ui-accent) ${selected ? 'bg-(--ui-bg-tertiary)' : ''}`,
    children: jsxs('div', { className: 'grid gap-1.5', children: [
      jsxs('div', { className: 'flex flex-wrap items-center gap-2 text-[11px]', children: [
        jsx('time', { className: 'font-mono text-(--ui-text-secondary)', children: timestamp(event.timestamp) }),
        jsx('span', { className: `${pill} ${tone(state, event.importance)}`, children: statusLabel(state) }),
        jsx('code', { className: 'text-(--ui-text-tertiary)', children: event.scope?.project || event.project_name || 'project 미확인' }),
        jsx('code', { className: 'text-(--ui-text-tertiary)', children: event.actor?.profile || event.profile_name || 'unknown' }),
        jsx(ModelBadge, { model: event.model })
      ] }),
      jsx('strong', { className: 'text-sm font-medium text-(--ui-text-primary)', children: heading }),
      jsx('p', { className: 'line-clamp-2 text-xs leading-5 text-(--ui-text-tertiary)', children: eventSummary(event) })
    ] })
  })
}

function DetailValue({ value }) {
  if (Array.isArray(value)) return jsx('ul', { className: 'mt-1 grid gap-1 text-xs text-(--ui-text-secondary)', children: value.map((item, index) => jsx('li', { className: 'break-words', children: `• ${typeof item === 'object' ? JSON.stringify(item) : String(item)}` }, index)) })
  if (typeof value === 'object' && value !== null) return jsx('pre', { className: 'mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) p-2 font-mono text-[11px] leading-4 text-(--ui-text-secondary)', children: JSON.stringify(value, null, 2) })
  return jsx('dd', { className: 'mt-1 break-words text-xs text-(--ui-text-secondary)', children: String(value ?? '') })
}

function DefinitionList({ entries }) {
  return jsx('dl', { className: 'grid gap-3 sm:grid-cols-2', children: entries.filter(([, value]) => value !== undefined && value !== null && value !== '').map(([label, value]) => jsxs('div', { className: 'min-w-0', children: [
    jsx('dt', { className: 'text-[11px] text-(--ui-text-tertiary)', children: label }),
    jsx(DetailValue, { value })
  ] }, label)) })
}

function Details({ event, eventId, onClose }) {
  const state = event.outcome?.state || event.status || 'unknown'
  const paths = event.scope?.paths || event.paths || event.changed_paths || []
  const rules = event.scope?.rules || event.rules || event.required_rules || []
  const action = nextAction(event)
  const metadata = event.evidence?.metadata || {}
  const model = event.model
  return jsxs('section', { id: `audit-detail-${eventId}`, className: 'border-t border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-4 py-4', 'aria-label': '선택한 Agent 활동 상세', children: [
    jsxs('header', { className: 'flex items-start justify-between gap-3', children: [
      jsx('div', { children: [jsx('p', { className: 'text-[11px] font-semibold uppercase tracking-[0.14em] text-(--ui-text-tertiary)', children: '활동 상세' }), jsx('h2', { className: 'mt-1 text-base font-semibold text-(--ui-text-primary)', children: activityTitle(event) })] }),
      jsx('button', { type: 'button', className: button, onClick: onClose, 'aria-label': '활동 상세 닫기', children: '닫기' })
    ] }),
    jsx('p', { className: 'mt-3 text-sm leading-6 text-(--ui-text-secondary)', children: eventSummary(event) }),
    jsxs('div', { className: 'mt-3 flex flex-wrap gap-2', children: [
      jsx('span', { className: `${pill} ${tone(state, event.importance)}`, children: `결과 · ${statusLabel(state)}` }),
      event.outcome?.duration_ms != null ? jsx('span', { className: `${pill} text-(--ui-text-secondary)`, children: `소요 시간 · ${event.outcome.duration_ms}ms` }) : null,
      jsx(ModelBadge, { model })
    ] }),
    jsxs('section', { className: 'mt-4 rounded-md border border-(--ui-stroke-secondary) p-3', children: [
      jsx('h3', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: '사용 모델' }),
      jsx(DefinitionList, { entries: [['모델', model?.name || '모델 미확인'], ['실행 유형', model?.name ? modelKindLabel(model.kind) : '미확인'], ['Provider', model?.provider || '미확인']] })
    ] }),
    paths.length || rules.length ? jsxs('section', { className: 'mt-4', children: [
      jsx('h3', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: '대상과 검증 범위' }),
      jsx(DefinitionList, { entries: [['경로', paths], ['Rule', rules], ['Validator', event.scope?.validator], ['Discovery provider', event.scope?.provider]] })
    ] }) : null,
    action ? jsxs('section', { className: 'mt-4 border-l-2 border-(--ui-accent) pl-3', children: [
      jsx('h3', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: '다음 행동' }),
      jsx('p', { className: 'mt-1 text-xs leading-5 text-(--ui-text-secondary)', children: action })
    ] }) : null,
    jsx('details', { className: 'mt-5 border-t border-(--ui-stroke-secondary) pt-4', children: [
      jsx('summary', { className: 'cursor-pointer text-xs font-medium text-(--ui-text-secondary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent)', children: '기술 정보 보기' }),
      jsx('div', { className: 'mt-3', children: jsx(DefinitionList, { entries: [
        ['시간', timestamp(event.timestamp)],
        ['Profile', event.actor?.profile || event.profile_name],
        ['Session', event.correlation?.session_id || event.session_id],
        ['Task', event.correlation?.task_id || event.task_id],
        ['Turn', event.correlation?.turn_id || event.turn_id],
        ['Generation', event.correlation?.generation ?? event.generation],
        ['Event type', event.evidence?.event_type || event.kind || event.event],
        ['Reason', event.evidence?.reason_code || event.reason],
        ['Metadata', metadata]
      ] }) })
    ] }),
    jsx('p', { className: 'mt-4 text-[11px] leading-4 text-(--ui-text-tertiary)', children: 'Prompt, reasoning, raw command와 tool 결과는 저장하지 않은 privacy-safe 기록입니다.' })
  ] })
}

function Timeline({ events, selectedId, onSelect }) {
  if (!events.length) return jsx('div', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: '현재 필터와 일치하는 Agent 활동이 없습니다.' })
  return jsx('ol', { className: 'divide-y divide-(--ui-stroke-secondary)', children: events.map((event, index) => {
    const id = eventKey(event, index)
    const selected = selectedId === id
    return jsxs('li', { children: [
      jsx(EventRow, { event, eventId: id, selected, onSelect }),
      selected ? jsx(Details, { event, eventId: id, onClose: () => onSelect(null) }) : null
    ] }, id)
  }) })
}

function Pagination({ page, pageSize, total, onPageChange, onPageSizeChange, disabled }) {
  if (!total) return null
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const first = page * pageSize + 1
  const last = Math.min(total, (page + 1) * pageSize)
  return jsxs('nav', { className: 'flex flex-wrap items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-4 py-3', 'aria-label': 'Audit event pagination', children: [
    jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: `${first}–${last} / ${total}개 · ${page + 1} / ${pageCount} 페이지` }),
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsxs('label', { className: 'flex items-center gap-1 text-[11px] text-(--ui-text-tertiary)', children: ['페이지 크기', jsxs(Select, { value: String(pageSize), disabled, onValueChange: value => onPageSizeChange(Number(value)), children: [jsx(SelectTrigger, { className: 'h-8 w-24 text-xs', 'aria-label': '페이지 크기', children: jsx(SelectValue, {}) }), jsx(SelectContent, { children: [10, 25, 50, 100].map(size => jsx(SelectItem, { value: String(size), children: `${size}개` }, size)) })] })] }),
      jsx('button', { type: 'button', className: button, disabled: disabled || page === 0, onClick: () => onPageChange(page - 1), children: '이전' }),
      jsx('button', { type: 'button', className: button, disabled: disabled || page + 1 >= pageCount, onClick: () => onPageChange(page + 1), children: '다음' })
    ] })
  ] })
}

function Page() {
  const [filters, setFilters] = useState({ project: '', profile: '', eventType: '', status: '' })
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(25)
  const [selectedId, setSelectedId] = useState(null)
  const summary = useAuditSummary()
  const events = useAuditEvents(filters, page, pageSize)
  const updateFilters = updater => {
    setFilters(updater)
    setPage(0)
    setSelectedId(null)
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
      jsx('div', { children: [jsx('p', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-text-tertiary)', children: 'Privacy-safe workflow evidence' }), jsx('h1', { className: 'mt-1 text-2xl font-semibold tracking-tight text-(--ui-text-primary)', children: 'agent-audit' }), jsx('p', { className: 'mt-1 text-sm text-(--ui-text-secondary)', children: 'Agent가 수행한 행동, 사용 모델, 검증 결과와 워크플로 이탈을 확인합니다.' })] }),
      jsx('button', { type: 'button', className: button, disabled: summary.isFetching || events.isFetching, onClick: refresh, children: summary.isFetching || events.isFetching ? '갱신 중…' : '새로고침' })
    ] }),
    jsxs('dl', { className: 'mt-5 flex flex-wrap gap-2', children: [jsx(Metric, { label: '전체 이벤트', value: summary.data?.total_events || 0 }), jsx(Metric, { label: 'Profile', value: metrics.profiles }), jsx(Metric, { label: '워크플로 이탈', value: metrics.deviations }), jsx(Metric, { label: '실패', value: metrics.failures })] }),
    summary.error ? jsx('p', { className: 'mt-4 rounded-md border border-(--dt-destructive)/50 p-3 text-sm text-(--dt-destructive)', role: 'alert', children: `요약을 불러오지 못했습니다: ${summary.error.message || String(summary.error)}` }) : null,
    jsx('section', { className: 'mt-5 overflow-hidden rounded-lg border border-(--ui-stroke-secondary)', children: [
      jsx(Filters, { summary: summary.data, filters, setFilters: updateFilters }),
      jsx(Pagination, { page, pageSize, total: events.data?.total || 0, onPageChange: nextPage => { setPage(nextPage); setSelectedId(null) }, onPageSizeChange: nextSize => { setPageSize(nextSize); setPage(0); setSelectedId(null) }, disabled: events.isFetching }),
      events.error
        ? jsx('p', { className: 'p-4 text-sm text-(--dt-destructive)', role: 'alert', children: `Agent 활동을 불러오지 못했습니다: ${events.error.message || String(events.error)}` })
        : events.isLoading
          ? jsx('p', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: 'Agent 활동을 불러오는 중…' })
          : jsx('div', { className: 'min-w-0 overflow-auto', children: jsx(Timeline, { events: events.data?.events || [], selectedId, onSelect: setSelectedId }) })
    ] })
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