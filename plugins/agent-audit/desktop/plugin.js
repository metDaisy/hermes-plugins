import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { Checkbox, Popover, PopoverContent, PopoverTrigger, ROUTES_AREA, SIDEBAR_NAV_AREA, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, host, queryClient, useQuery } from '@hermes/plugin-sdk'

const ID = 'agent-audit'
const PAGE_PATH = '/agent-audit'
let pluginCtx

const api = path => pluginCtx.rest(path)
const query = params => {
  const values = Object.entries(params)
    .map(([key, value]) => [key, Array.isArray(value) ? value.join(',') : value])
    .filter(([, value]) => value)
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
const failureTypeLabel = type => ({
  nonzero_exit: '종료 코드 오류', timeout: '시간 초과', permission_denied: '권한 거부',
  command_not_found: '명령을 찾지 못함', network_error: '네트워크 오류',
  cancelled: '사용자 또는 시스템에 의해 취소됨', unknown: '원인 분류 미확인'
}[type] || type)
const tone = (state, importance) => importance === 'error' || state === 'failed' || state === 'error'
  ? 'text-(--dt-destructive)'
  : importance === 'warning' || state === 'not_run' || state === 'continue'
    ? 'text-(--dt-warning)'
    : state === 'succeeded' || state === 'passed' || state === 'success' || state === 'ok'
      ? 'text-(--ui-accent)'
      : 'text-(--ui-text-secondary)'
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
  if (code === 'filesystem.search') return '파일 검색'
  if (code === 'filesystem.read') return '파일 내용 확인'
  if (code === 'filesystem.modify') return '파일 수정'
  if (code === 'system.command') return '명령 실행'
  if (code === 'desktop.interact') return 'Desktop 조작'
  if (code === 'web.interact' || code === 'web.research') return '웹 정보 확인'
  if (code === 'skill.lifecycle') return 'Skill 사용'
  if (code === 'skill.inspect') return 'Skill 지침 확인'
  if (code === 'code.discovery') return '코드 구조 조사'
  if (code === 'code.index') return '코드 검색 인덱스 갱신'
  if (code === 'workflow.plan') return '작업 계획 갱신'
  if (code === 'validation.trigger') return '변경으로 검증이 필요해졌습니다'
  if (code === 'validation.result') return state === 'failed' ? '검증이 실패했습니다' : '검증이 통과했습니다'
  if (code === 'validation.verification_gate') return state === 'succeeded' ? '완료 조건을 충족했습니다' : '완료 전에 추가 검증이 필요합니다'
  if (code === 'workflow.deviation') return '워크플로 규칙을 지키지 못했습니다'
  if (code === 'session.end') return 'Agent 작업이 종료되었습니다'
  const fallback = event.tool || event.skill || event.validator || event.kind || event.event
  return fallback ? `${fallback} 활동을 기록했습니다` : 'Agent 활동을 기록했습니다'
}
const targetPathLabel = path => {
  const normalized = String(path).replace(/\\/g, '/')
  if (normalized === 'i18n') return '다국어(i18n)'
  if (normalized.endsWith('/i18n')) return `${normalized} (다국어 기능)`
  return normalized
}
const eventSummary = event => {
  const paths = event.scope?.paths || event.paths || event.changed_paths || []
  const shortPath = path => {
    const name = String(path).replace(/\\/g, '/').split('/').pop()
    return name === 'i18n' ? '다국어(i18n)' : name
  }
  const scope = paths.length ? `${paths.slice(0, 3).map(shortPath).join(', ')}${paths.length > 3 ? ` 외 ${paths.length - 3}개` : ''}` : null
  const code = activityCode(event)
  if (code === 'filesystem.search') return scope ? `${scope} 경로에서 파일 검색` : '프로젝트 파일 검색'
  if (code === 'filesystem.read') return scope ? `${scope} 읽음` : '파일 읽음'
  if (code === 'filesystem.modify') return scope ? `${scope} 수정` : '프로젝트 파일 수정'
  if (code === 'system.command') return '로컬 개발 명령 실행'
  if (code === 'desktop.interact') return 'Desktop 화면 확인·조작'
  if (code === 'web.interact' || code === 'web.research') return '웹 정보 확인'
  if (code === 'skill.lifecycle') return `${event.activity?.skill || event.skill || 'Skill'} 사용`
  if (code === 'skill.inspect') return event.activity?.skill || event.skill || 'Skill 이름 미확인'
  if (code === 'code.discovery') return `${event.activity?.provider || event.scope?.provider || event.provider || '코드 검색 도구'}로 구조 조사`
  if (code === 'code.index') return '코드 검색 인덱스 갱신'
  if (code === 'workflow.plan') return '작업 단계와 진행 상태 갱신'
  if (code?.startsWith('validation.')) return '변경 사항 검증 상태 기록'
  if (code === 'workflow.deviation') return '정해진 작업 절차와 다른 행동 감지'
  if (code === 'session.end') return 'Agent 작업 종료'
  return 'Privacy-safe 활동 기록'
}
const nextAction = event => ({
  missing_validation: '변경 범위에 필요한 검증을 실행한 뒤 결과를 다시 확인하세요.',
  failed_validation: '실패한 검증의 원인을 수정하고 같은 검증을 다시 실행하세요.',
  unresolved_mutation: '변경 경로를 확인할 수 있는 방식으로 수정 작업을 다시 수행하세요.',
  direct_gradle_invocation: 'Gradle 작업은 gradle-mcp를 통해 다시 실행하세요.',
  kanban_create_without_expected_discovery: 'Task를 만들기 전에 Semble과 Codebase Memory로 코드 구조를 조사하세요.',
  complete_required_validation: '필요한 검증을 완료한 뒤 작업 완료 여부를 다시 확인하세요.'
}[event.explanation?.next_action_code || event.evidence?.reason_code || event.reason] || null)
const hasFailureDetails = outcome => Boolean(
  (Number.isInteger(outcome?.exit_code) && outcome.exit_code !== 0) ||
  outcome?.failure_type || outcome?.failure_summary
)
const hasEventDetails = event => {
  const code = activityCode(event)
  const paths = event.scope?.paths || event.paths || event.changed_paths || []
  const rules = event.scope?.rules || event.rules || event.required_rules || []
  const outcome = event.outcome || {}
  return Boolean(
    event.activity?.command || event.command ||
    hasFailureDetails(outcome) || paths.length || rules.length || nextAction(event) ||
    event.scope?.validator || event.scope?.provider ||
    (code !== 'skill.inspect' && outcome.duration_ms != null)
  )
}
const eventKey = (event, index = 0) => String(event.id ?? `${event.timestamp || 'event'}-${index}`)

function useAuditSummary() {
  return useQuery({ queryKey: [ID, 'summary'], queryFn: () => api('/summary'), refetchOnWindowFocus: false })
}

function useAuditSessions(filters) {
  return useQuery({
    queryKey: [ID, 'sessions', filters.project, filters.profile],
    queryFn: () => api(`/sessions${query({ project_name: filters.project, profile_name: filters.profile })}`),
    enabled: filters.project.length === 1,
    refetchOnWindowFocus: false
  })
}

function useAuditInsights(filters) {
  return useQuery({
    queryKey: [ID, 'insights', filters.project],
    queryFn: () => api(`/insights${query({ project_name: filters.project })}`),
    enabled: filters.project.length === 1,
    refetchOnWindowFocus: false
  })
}

function useAuditEvents(filters, page, pageSize) {
  return useQuery({
    queryKey: [ID, 'events', filters, page, pageSize],
    queryFn: () => api(`/events${query({ project_name: filters.project, profile_name: filters.profile, session_id: filters.session, event_type: filters.eventType, status: filters.status, offset: String(page * pageSize), limit: String(pageSize) })}`),
    enabled: filters.project.length === 1,
    refetchOnWindowFocus: false
  })
}

function Metric({ label, value }) {
  return jsxs('div', { className: 'min-w-28 rounded-md border border-(--ui-stroke-secondary) px-3 py-2', children: [
    jsx('dt', { className: 'text-[11px] text-(--ui-text-tertiary)', children: label }),
    jsx('dd', { className: 'mt-0.5 text-lg font-semibold text-(--ui-text-primary)', children: String(value) })
  ] })
}

function ProjectSelector({ summary, selected, onSelect }) {
  const projects = Object.entries(summary?.projects || {})
  return jsxs('section', { className: 'mt-5 border-y border-(--ui-stroke-secondary) py-4', 'aria-labelledby': 'project-selector-title', children: [
    jsxs('div', { className: 'flex flex-wrap items-end justify-between gap-3', children: [
      jsxs('div', { children: [
        jsx('h2', { id: 'project-selector-title', className: 'text-sm font-semibold text-(--ui-text-primary)', children: '프로젝트 선택' }),
        jsx('p', { className: 'mt-1 text-xs text-(--ui-text-tertiary)', children: '프로젝트를 선택하면 해당 프로젝트의 기록과 세션을 표시합니다.' })
      ] }),
      jsxs(Select, { value: selected, onValueChange: onSelect, children: [
        jsx(SelectTrigger, { className: 'h-9 min-w-72 text-sm', 'aria-label': '프로젝트 선택', children: jsx(SelectValue, { placeholder: '프로젝트를 선택하세요' }) }),
        jsx(SelectContent, { children: projects.map(([name, count]) => jsx(SelectItem, { value: name, children: `${name} · ${count}건` }, name)) })
      ] })
    ] })
  ] })
}

function ProjectSummary({ project, insights, loading, error }) {
  if (error) return jsx('p', { className: 'mt-4 rounded-md border border-(--dt-destructive)/50 p-3 text-sm text-(--dt-destructive)', role: 'alert', children: `프로젝트 요약을 불러오지 못했습니다: ${error.message || String(error)}` })
  if (loading) return jsx('section', { className: 'mt-4 border-b border-(--ui-stroke-secondary) pb-4 text-sm text-(--ui-text-secondary)', 'aria-busy': true, children: '프로젝트 기록을 계산하는 중…' })
  const totals = insights?.totals || {}
  const metrics = [
    { label: '기록', value: totals.events || 0 },
    { label: '세션', value: totals.sessions || 0 },
    { label: '성공률', value: `${totals.success_rate || 0}%` },
    { label: '실패', value: totals.failed || 0 }
  ]
  return jsxs('section', { className: 'mt-4 flex flex-wrap items-center justify-between gap-4 border-b border-(--ui-stroke-secondary) pb-4', 'aria-labelledby': 'project-summary-title', children: [
    jsxs('div', { children: [
      jsx('p', { className: 'text-[11px] text-(--ui-text-tertiary)', children: '선택한 프로젝트' }),
      jsx('h2', { id: 'project-summary-title', className: 'mt-1 text-lg font-semibold text-(--ui-text-primary)', children: project })
    ] }),
    jsx('dl', { className: 'grid grid-cols-2 gap-2 sm:grid-cols-4', children: metrics.map(metric => jsx(Metric, metric, metric.label)) })
  ] })
}

const toggleFilterValue = (values, value) => values.includes(value)
  ? values.filter(current => current !== value)
  : [...values, value]

function Filters({ summary, sessions, filters, setFilters }) {
  const profiles = Object.keys(summary?.profiles || {})
  const sessionItems = sessions?.sessions || []
  const sessionOptions = sessionItems.map(session => session.id)
  const sessionLabel = id => {
    const session = sessionItems.find(item => item.id === id)
    if (!session) return '세션 이름 미확인'
    return `${session.title || '세션 이름 미확인'} · ${session.project || 'Project 미확인'} · ${session.count}건`
  }
  const eventTypes = Object.keys(summary?.event_types || {})
  const statuses = Object.keys(summary?.statuses || {})
  const update = key => value => setFilters(current => ({
    ...current,
    [key]: value,
    ...((key === 'project' || key === 'profile') ? { session: [] } : {})
  }))
  return jsxs('section', { className: 'flex flex-wrap items-end gap-2 border-b border-(--ui-stroke-secondary) px-4 py-3', 'aria-label': 'Audit event filter', children: [
    jsx(MultiFilter, { label: 'Session', values: filters.session, onChange: update('session'), allLabel: '모든 Session', options: sessionOptions, format: sessionLabel }),
    jsx(MultiFilter, { label: 'Profile', values: filters.profile, onChange: update('profile'), allLabel: '모든 Profile', options: profiles }),
    jsx(MultiFilter, { label: '활동 유형', values: filters.eventType, onChange: update('eventType'), allLabel: '모든 활동', options: eventTypes, format: eventTypeLabel }),
    jsx(MultiFilter, { label: '결과', values: filters.status, onChange: update('status'), allLabel: '모든 결과', options: statuses, format: statusLabel })
  ] })
}

function FilterOption({ checked, label, onToggle }) {
  const activate = event => {
    if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') return
    if (event.type === 'keydown') event.preventDefault()
    onToggle()
  }
  return jsxs('div', {
    role: 'checkbox',
    tabIndex: 0,
    'aria-checked': checked,
    onClick: activate,
    onKeyDown: activate,
    className: 'flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs text-(--ui-text-secondary) hover:bg-(--ui-bg-tertiary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent)',
    children: [
      jsx(Checkbox, { checked, tabIndex: -1, className: 'pointer-events-none', onCheckedChange: onToggle, 'aria-hidden': true }),
      jsx('span', { children: label })
    ]
  })
}

function MultiFilter({ label, values, onChange, allLabel, options, format = value => value }) {
  const [open, setOpen] = useState(false)
  const selection = values.length === 0 ? allLabel : values.length === 1 ? format(values[0]) : `${values.length}개 선택`
  return jsxs('div', { className: 'grid gap-1 text-[11px] text-(--ui-text-tertiary)', children: [
    jsx('span', { children: label }),
    jsxs(Popover, { open, onOpenChange: setOpen, children: [
      jsx(PopoverTrigger, { className: `${selectTrigger} flex items-center justify-between rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary) px-3 text-(--ui-text-secondary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent)`, 'aria-label': label, children: selection }),
      jsxs(PopoverContent, { align: 'start', variant: 'menu', className: 'max-h-72 min-w-48 overflow-auto p-1', children: [
        jsx(FilterOption, { checked: values.length === 0, label: allLabel, onToggle: () => onChange([]) }),
        ...options.map(option => jsx(FilterOption, {
          checked: values.includes(option),
          label: format(option),
          onToggle: () => onChange(toggleFilterValue(values, option))
        }, option))
      ] })
    ] })
  ] })
}

const profileTone = profile => {
  const hues = [12, 38, 142, 188, 224, 276, 326]
  const hash = [...String(profile || '')].reduce((total, character) => total + character.codePointAt(0), 0)
  return hues[hash % hues.length]
}

function ProfileBadge({ profile }) {
  const name = profile || 'unknown'
  const hue = profileTone(name)
  return jsxs('span', { className: `${pill} inline-flex items-center gap-1.5 text-(--ui-text-secondary)`, children: [
    jsx('span', { className: 'size-1.5 rounded-full', style: { backgroundColor: `hsl(${hue} 68% 52%)` }, 'aria-hidden': true }),
    name
  ] })
}

function ModelBadge({ model }) {
  const name = model?.name || '모델 미확인'
  const effort = model?.reasoning_effort || '미확인'
  return jsx('span', { className: `${pill} text-(--ui-text-secondary)`, title: model?.provider || name, children: `${name}(${effort})` })
}

function EventRow({ event, eventId, selected, expandable, onSelect }) {
  const heading = activityTitle(event)
  const state = event.outcome?.state || event.status || 'unknown'
  const detailsId = `audit-detail-${eventId}`
  const content = jsxs('div', { className: 'grid gap-1.5', children: [
    jsxs('div', { className: 'flex flex-wrap items-center gap-2 text-[11px]', children: [
      jsx('time', { className: 'font-mono text-(--ui-text-secondary)', children: timestamp(event.timestamp) }),
      jsx('span', { className: `${pill} ${tone(state, event.importance)}`, children: statusLabel(state) }),
      jsx(ProfileBadge, { profile: event.actor?.profile || event.profile_name }),
      jsx(ModelBadge, { model: event.model })
    ] }),
    jsx('strong', { className: 'text-sm font-medium text-(--ui-text-primary)', children: heading }),
    jsx('p', { className: 'line-clamp-2 text-xs leading-5 text-(--ui-text-tertiary)', children: eventSummary(event) })
  ] })
  if (!expandable) return jsx('div', { className: 'w-full px-4 py-3 text-left', children: content })
  return jsx('button', {
    type: 'button',
    onClick: () => onSelect(selected ? null : eventId),
    'aria-expanded': selected,
    'aria-controls': detailsId,

    className: `w-full px-4 py-3 text-left transition hover:bg-(--ui-bg-tertiary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-(--ui-accent) ${selected ? 'bg-(--ui-bg-tertiary)' : ''}`,
    children: content
  })
}

function DetailValue({ value }) {
  if (Array.isArray(value)) return jsx('ul', { className: 'mt-1 grid gap-1 text-xs text-(--ui-text-secondary)', children: value.map((item, index) => jsx('li', { className: 'break-words', children: `• ${typeof item === 'object' ? JSON.stringify(item) : String(item)}` }, index)) })
  if (typeof value === 'object' && value !== null) return jsx('pre', { className: 'mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) p-2 font-mono text-[11px] leading-4 text-(--ui-text-secondary)', children: JSON.stringify(value, null, 2) })
  return jsx('dd', { className: 'mt-1 break-words text-xs text-(--ui-text-secondary)', children: String(value ?? '') })
}

const hasDetailValue = value => Array.isArray(value)
  ? value.length > 0
  : typeof value === 'object' && value !== null
    ? Object.keys(value).length > 0
    : value !== undefined && value !== null && value !== ''

function DefinitionList({ entries }) {
  return jsx('dl', { className: 'grid gap-3 sm:grid-cols-2', children: entries.filter(([, value]) => hasDetailValue(value)).map(([label, value]) => jsxs('div', { className: 'min-w-0', children: [
    jsx('dt', { className: 'text-[11px] text-(--ui-text-tertiary)', children: label }),
    jsx(DetailValue, { value })
  ] }, label)) })
}

function TargetDetails({ event, paths, rules }) {
  const code = activityCode(event)
  const targetLabel = code === 'filesystem.search'
    ? '검색 위치'
    : code === 'filesystem.read'
      ? '확인한 파일'
      : code === 'filesystem.modify'
        ? '수정한 파일'
        : '대상 경로'
  const description = code === 'filesystem.search'
    ? '표시된 경로 안에서 파일 이름 또는 내용을 검색했습니다.'
    : code === 'filesystem.read'
      ? '표시된 파일의 내용을 확인했습니다.'
      : code === 'filesystem.modify'
        ? '표시된 파일을 변경했습니다.'
        : null
  const targetEntries = [[targetLabel, paths.map(targetPathLabel)], ['작업 설명', description]]
  const verificationEntries = [
    ['검증 규칙', rules],
    ['검증 도구', event.scope?.validator],
    ['코드 조사 도구', event.scope?.provider]
  ]
  const hasTargets = targetEntries.some(([, value]) => hasDetailValue(value))
  const hasVerification = verificationEntries.some(([, value]) => hasDetailValue(value))
  if (!hasTargets && !hasVerification) return null
  return jsxs('section', { className: 'mt-4 grid gap-4', children: [
    hasTargets ? jsxs('div', { children: [
      jsx('h3', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: '작업 대상' }),
      jsx('div', { className: 'mt-2', children: jsx(DefinitionList, { entries: targetEntries }) })
    ] }) : null,
    hasVerification ? jsxs('div', { children: [
      jsx('h3', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: '검증 정보' }),
      jsx('div', { className: 'mt-2', children: jsx(DefinitionList, { entries: verificationEntries }) }),
      rules.length ? jsx('p', { className: 'mt-2 text-[11px] leading-4 text-(--ui-text-tertiary)', children: '검증 규칙은 변경 사항이 완료 조건을 충족하는지 확인하는 자동 검사 항목입니다.' }) : null
    ] }) : null
  ] })
}

function CommandDetails({ command }) {
  if (!command) return null
  return jsxs('details', { className: 'mt-4 rounded-md border border-(--ui-stroke-secondary)', children: [
    jsx('summary', { className: 'cursor-pointer select-none px-3 py-2 text-xs font-semibold text-(--ui-text-primary) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-(--ui-accent)', children: '실행 명령 더보기' }),
    jsx('pre', { className: 'max-h-64 overflow-auto whitespace-pre-wrap break-words border-t border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) p-3 font-mono text-xs leading-5 text-(--ui-text-secondary)', children: command })
  ] })
}

function FailureDetails({ outcome }) {
  const entries = [
    ['종료 코드', Number.isInteger(outcome?.exit_code) && outcome.exit_code !== 0 ? outcome.exit_code : null],
    ['실패 유형', outcome?.failure_type ? failureTypeLabel(outcome.failure_type) : null],
    ['실패 원인', outcome?.failure_summary]
  ]
  if (!entries.some(([, value]) => hasDetailValue(value))) return null
  return jsxs('section', { className: 'mt-4 border-l-2 border-(--dt-destructive) pl-3', children: [
    jsx('h3', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: '실패 정보' }),
    jsx('div', { className: 'mt-2', children: jsx(DefinitionList, { entries }) })
  ] })
}

function Details({ event, eventId, onClose }) {
  const paths = event.scope?.paths || event.paths || event.changed_paths || []
  const rules = event.scope?.rules || event.rules || event.required_rules || []
  const action = nextAction(event)
  const command = event.activity?.command || event.command
  return jsxs('section', { id: `audit-detail-${eventId}`, className: 'border-t border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-4 py-4', 'aria-label': '선택한 Agent 활동 상세', children: [
    jsxs('header', { className: 'flex items-start justify-between gap-3', children: [
      jsx('h2', { className: 'text-sm font-semibold text-(--ui-text-primary)', children: '추가 정보' }),
      jsx('button', { type: 'button', className: button, onClick: onClose, 'aria-label': '활동 상세 닫기', children: '닫기' })
    ] }),
    event.outcome?.duration_ms != null ? jsx('p', { className: 'mt-2 text-[11px] text-(--ui-text-tertiary)', children: `소요 시간 · ${event.outcome.duration_ms}ms` }) : null,
    jsx(CommandDetails, { command }),
    jsx(FailureDetails, { outcome: event.outcome }),
    jsx(TargetDetails, { event, paths, rules }),
    action ? jsxs('section', { className: 'mt-4 border-l-2 border-(--ui-accent) pl-3', children: [
      jsx('h3', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: '다음 행동' }),
      jsx('p', { className: 'mt-1 text-xs leading-5 text-(--ui-text-secondary)', children: action })
    ] }) : null,
  ] })
}

function Timeline({ events, selectedId, onSelect }) {
  if (!events.length) return jsx('div', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: '현재 필터와 일치하는 Agent 활동이 없습니다.' })
  return jsx('ol', { className: 'divide-y divide-(--ui-stroke-secondary)', children: events.map((event, index) => {
    const id = eventKey(event, index)
    const selected = selectedId === id
    const expandable = hasEventDetails(event)
    return jsxs('li', { children: [
      jsx(EventRow, { event, eventId: id, selected, expandable, onSelect }),
      selected && expandable ? jsx(Details, { event, eventId: id, onClose: () => onSelect(null) }) : null
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
  const [filters, setFilters] = useState({ project: [], session: [], profile: [], eventType: [], status: [] })
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(25)
  const [selectedId, setSelectedId] = useState(null)
  const summary = useAuditSummary()
  const sessions = useAuditSessions(filters)
  const insights = useAuditInsights(filters)
  const events = useAuditEvents(filters, page, pageSize)
  const selectedProject = filters.project[0] || ''
  const updateFilters = updater => {
    setFilters(updater)
    setPage(0)
    setSelectedId(null)
  }
  const selectProject = project => updateFilters(current => ({
    ...current,
    project: [project],
    session: [],
    profile: [],
    eventType: [],
    status: []
  }))
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: [ID, 'summary'] }),
    queryClient.invalidateQueries({ queryKey: [ID, 'sessions'] }),
    queryClient.invalidateQueries({ queryKey: [ID, 'insights'] }),
    queryClient.invalidateQueries({ queryKey: [ID, 'events'] })
  ])
  return jsx('main', { className: 'mx-auto max-w-7xl p-4 sm:p-6', children: [
    jsxs('header', { className: 'flex flex-wrap items-start justify-between gap-4', children: [
      jsx('div', { children: [jsx('p', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-text-tertiary)', children: 'Privacy-safe workflow evidence' }), jsx('h1', { className: 'mt-1 text-2xl font-semibold tracking-tight text-(--ui-text-primary)', children: 'agent-audit' }), jsx('p', { className: 'mt-1 text-sm text-(--ui-text-secondary)', children: '프로젝트를 선택하고 Agent 활동과 검증 결과를 시간순으로 확인합니다.' }), jsx('p', { className: 'mt-1 text-[11px] text-(--ui-text-tertiary)', children: 'Prompt, reasoning과 tool 결과는 저장하지 않습니다. Terminal 명령은 credential과 절대 경로를 제거합니다.' })] }),
      jsx('button', { type: 'button', className: button, disabled: summary.isFetching || events.isFetching, onClick: refresh, children: summary.isFetching || events.isFetching ? '갱신 중…' : '새로고침' })
    ] }),
    summary.error ? jsx('p', { className: 'mt-4 rounded-md border border-(--dt-destructive)/50 p-3 text-sm text-(--dt-destructive)', role: 'alert', children: `프로젝트 목록을 불러오지 못했습니다: ${summary.error.message || String(summary.error)}` }) : null,
    jsx(ProjectSelector, { summary: summary.data, selected: selectedProject, onSelect: selectProject }),
    selectedProject ? jsx(ProjectSummary, { project: selectedProject, insights: insights.data, loading: insights.isLoading, error: insights.error }) : null,
    selectedProject ? jsx('section', { className: 'mt-4 overflow-hidden rounded-lg border border-(--ui-stroke-secondary)', children: [
      jsx(Filters, { summary: summary.data, sessions: sessions.data, filters, setFilters: updateFilters }),
      jsx(Pagination, { page, pageSize, total: events.data?.total || 0, onPageChange: nextPage => { setPage(nextPage); setSelectedId(null) }, onPageSizeChange: nextSize => { setPageSize(nextSize); setPage(0); setSelectedId(null) }, disabled: events.isFetching }),
      events.error
        ? jsx('p', { className: 'p-4 text-sm text-(--dt-destructive)', role: 'alert', children: `Agent 활동을 불러오지 못했습니다: ${events.error.message || String(events.error)}` })
        : events.isLoading
          ? jsx('p', { className: 'p-8 text-center text-sm text-(--ui-text-secondary)', role: 'status', children: 'Agent 활동을 불러오는 중…' })
          : jsx('div', { className: 'min-w-0 overflow-auto', children: jsx(Timeline, { events: events.data?.events || [], selectedId, onSelect: setSelectedId }) })
    ] }) : jsx('section', { className: 'mt-8 border-l-2 border-(--ui-accent) py-1 pl-4', role: 'status', children: [
      jsx('h2', { className: 'text-sm font-semibold text-(--ui-text-primary)', children: '프로젝트를 먼저 선택하세요' }),
      jsx('p', { className: 'mt-1 text-xs text-(--ui-text-secondary)', children: '전체 프로젝트 기록을 한 화면에 섞지 않습니다. 위에서 확인할 프로젝트를 선택하세요.' })
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