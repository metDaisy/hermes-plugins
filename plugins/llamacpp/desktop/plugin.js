import { useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { ROUTES_AREA, SIDEBAR_NAV_AREA, queryClient, useQuery } from '@hermes/plugin-sdk'

const ID = 'llamacpp'
const LOG_NEWLINE = String.fromCharCode(10)
let pluginCtx
const modelIdFromPath = path => String(path || '').split('/').pop().replace(/-\d{5}-of-\d{5}(?=\.gguf$)/i, '').replace(/\.gguf$/i, '')
const card = 'rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)'
const muted = 'text-sm text-(--ui-text-secondary)'
const button = 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-sm transition hover:bg-(--ui-bg-tertiary) disabled:cursor-not-allowed disabled:opacity-50'
const primary = 'rounded-md bg-(--ui-accent) px-3 py-2 text-sm text-(--ui-accent-foreground) transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50'
const danger = 'rounded-md border border-(--dt-destructive)/50 px-3 py-2 text-sm text-(--dt-destructive) transition hover:bg-(--dt-destructive)/10 disabled:cursor-not-allowed disabled:opacity-50'
const input = 'w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent px-3 py-2 text-sm text-(--ui-text-primary) outline-none focus:border-(--ui-accent)'
const compactInput = 'h-8 w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs text-(--ui-text-primary) outline-none focus:border-(--ui-accent) focus-visible:ring-1 focus-visible:ring-(--ui-accent)'
const compactDanger = 'shrink-0 rounded-md border border-(--dt-destructive)/50 px-2 py-1 text-xs text-(--dt-destructive) transition hover:bg-(--dt-destructive)/10 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent)'
const compactPrimary = 'h-8 shrink-0 rounded-md bg-(--ui-accent) px-2 py-1 text-xs text-(--ui-accent-foreground) transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent)'
const themeColorScheme = () => {
  if (typeof document === 'undefined') return 'dark'
  const mode = document.documentElement.dataset.hermesMode
  if (mode === 'light' || mode === 'dark') return mode
  return getComputedStyle(document.documentElement).colorScheme === 'light' ? 'light' : 'dark'
}
const themedSelect = () => ({ colorScheme: themeColorScheme(), backgroundColor: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)' })
const themedOption = { backgroundColor: 'var(--ui-bg-elevated)', color: 'var(--ui-text-primary)' }
const api = (path, options) => pluginCtx.rest(path, options)

const optionValueError = (option, value) => {
  const text = String(value || '')
  if (option.requires_value && !text.trim()) return `parameter '${option.key}'에 값이 필요합니다.`
  if (!option.requires_value && text.trim()) return `parameter '${option.key}'는 값을 받지 않는 flag입니다.`
  if (option.choices?.length && !option.choices.includes(text)) return `${option.key} 값은 ${option.choices.join(', ')} 중 하나여야 합니다.`
  if (option.value_kind === 'integer' && !/^[+-]?\d+$/.test(text)) return `${option.key}에는 정수를 입력해야 합니다.`
  if (option.value_kind === 'number' && !Number.isFinite(Number(text))) return `${option.key}에는 숫자를 입력해야 합니다.`
  return ''
}

function Badge({ children, tone = 'neutral' }) {
  const color = tone === 'good' ? 'bg-(--ui-accent)/15 text-(--ui-accent)' : tone === 'warn' ? 'bg-(--ui-accent-secondary)/15 text-(--ui-accent-secondary)' : 'bg-(--ui-bg-tertiary) text-(--ui-text-secondary)'
  return jsx('span', { className: `rounded-full px-2 py-1 text-xs ${color}`, children })
}

function formatVram(device) {
  const total = Number(device?.vram_total_bytes)
  const free = Number(device?.vram_free_bytes)
  if (!Number.isFinite(total) || !Number.isFinite(free) || total <= 0) return '확인 중'
  const used = Math.max(0, total - free)
  const toGb = bytes => (bytes / (1024 ** 3)).toFixed(1)
  return `${toGb(used)}GB/${toGb(total)}GB`
}

function JobProgress({ job }) {
  if (!job) return null
  const activityLabel = { 'model-download': '다운로드 중…', 'server-start': 'server 시작 중…', 'server-restart': 'server 재시작 중…', 'runtime-install': 'runtime 설치 중…', 'prism-runtime-install': 'Prism-ML 다운로드 중…' }[job.kind] || '작업 중…'
  const reportedPercent = Number(job.percent)
  const determinate = job.percent != null && Number.isFinite(reportedPercent) && !(reportedPercent === 3 && (job.phase === 'starting' || job.phase === 'downloading'))
  const percent = determinate ? Math.max(0, Math.min(100, reportedPercent)) : job.status === 'done' ? 100 : 35
  const failed = job.status === 'error'
  const done = job.status === 'done'
  const running = !failed && !done
  const detail = job.kind === 'server-start' ? (done ? 'llama-server 준비 완료' : failed ? 'llama-server 시작 실패' : activityLabel) : job.kind === 'server-restart' ? (done ? 'llama-server 재시작 완료' : failed ? 'llama-server 재시작 실패' : activityLabel) : String(job.detail || job.phase || activityLabel).replace(/\s+\(via hf CLI\)$/i, '')
  return jsxs('div', { className: `relative mt-4 rounded-md p-3 text-sm ${failed ? 'bg-(--dt-destructive)/10' : 'bg-(--ui-accent)/10'}`, role: 'status', children: [
    jsxs('div', { className: 'flex justify-between gap-3', children: [jsx('span', { children: detail }), jsx('span', { children: failed ? '실패' : done ? '완료' : determinate ? `${percent}%` : activityLabel })] }),
    jsx('div', { className: 'mt-2 h-1.5 overflow-hidden rounded bg-(--ui-stroke-secondary)', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': determinate ? percent : undefined, 'aria-label': done ? '완료' : failed ? '실패' : activityLabel, children: jsx('div', { className: `h-full transition-all ${failed ? 'bg-(--dt-destructive)' : 'bg-(--ui-accent)'} ${running && !determinate ? 'animate-pulse' : ''}`, style: { width: `${percent}%` } }) }),
    job.error ? jsx('p', { className: 'mt-2 text-(--dt-destructive)', children: job.error }) : null
  ] })
}

function ServerLogPanel({ status, jobs }) {
  const [open, setOpen] = useState(true)
  const serverJob = (jobs || []).find(job => job.kind === 'server-start')
  const logQuery = useQuery({
    queryKey: [ID, 'server-log'],
    queryFn: () => api('/logs?limit=250'),
    enabled: open,
    refetchInterval: query => status?.server_running || serverJob?.status === 'running' ? 1500 : false,
    refetchOnWindowFocus: false
  })
  useEffect(() => {
    if (open && serverJob && serverJob.status !== 'running') logQuery.refetch()
  }, [open, serverJob?.job_id, serverJob?.status])
  useEffect(() => {
    if (open && status?.server_running === false) logQuery.refetch()
  }, [open, status?.server_running])
  const lines = logQuery.data?.lines || []
  return jsxs('section', { className: 'relative mt-3 overflow-hidden rounded-md border border-(--ui-stroke-secondary)', 'aria-labelledby': 'llama-server-log-title', children: [
    jsxs('div', { className: 'flex items-center justify-between gap-3 bg-(--ui-bg-tertiary) px-3 py-2', children: [jsx('h2', { id: 'llama-server-log-title', className: 'text-xs font-medium text-(--ui-text-primary)', children: 'llama-server log' }), jsxs('div', { className: 'flex items-center gap-2', children: [open ? jsx('button', { className: 'text-xs text-(--ui-text-secondary) hover:text-(--ui-text-primary)', onClick: () => logQuery.refetch(), disabled: logQuery.isFetching, children: logQuery.isFetching ? '갱신 중…' : '새로고침' }) : null, jsx('button', { className: 'text-xs text-(--ui-accent) hover:underline', onClick: () => setOpen(current => !current), 'aria-expanded': open, children: open ? '접기' : '보기' })] })] }),
    open ? logQuery.error ? jsx('p', { className: 'px-3 py-3 text-xs text-(--dt-destructive)', role: 'alert', children: String(logQuery.error.message || logQuery.error) }) : jsx('pre', { className: 'max-h-64 select-text cursor-text overflow-auto whitespace-pre-wrap break-all bg-(--ui-bg-primary) px-3 py-2 font-mono text-[11px] leading-4 text-(--ui-text-secondary)', style: { userSelect: 'text', WebkitUserSelect: 'text' }, tabIndex: 0, role: 'log', 'aria-label': 'llama-server log', 'aria-live': 'polite', children: lines.length ? lines.join(LOG_NEWLINE) : 'llama-server log가 아직 없습니다.' }) : null
  ] })
}

function RuntimeCard({ status, jobs, onRefresh }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [requestedKind, setRequestedKind] = useState(status?.runtime_kind || 'official')
  const runtimeJob = (jobs || []).find(job => ['runtime-install', 'prism-runtime-install'].includes(job.kind) && job.status === 'running') || (jobs || []).find(job => ['runtime-install', 'prism-runtime-install'].includes(job.kind) && ['error', 'done'].includes(job.status))
  const serverJob = (jobs || []).find(job => job.kind === 'server-start')
  const activeKind = status?.runtime_kind || 'official'
  const available = status?.runtime_options?.[requestedKind] || {}
  const runtimeBusy = busy || runtimeJob?.status === 'running'
  const serverBusy = serverJob?.status === 'running'
  useEffect(() => { setRequestedKind(status?.runtime_kind || 'official') }, [status?.runtime_kind])
  useEffect(() => { if (runtimeJob?.status === 'done') { setMessage('runtime 준비가 완료되었습니다.'); onRefresh() } }, [runtimeJob?.job_id, runtimeJob?.status])
  const install = async () => { setBusy(true); setMessage(''); try { await api('/runtime/install', { method: 'POST', body: { kind: requestedKind } }); setMessage('runtime 준비 작업을 시작했습니다.') } catch (cause) { setMessage(`runtime 준비 실패: ${cause?.message || String(cause)}`) } finally { setBusy(false) } }
  const useRuntime = async kind => { setBusy(true); setMessage(''); try { await api('/runtime', { method: 'PUT', body: { kind } }); await onRefresh(); setMessage('선택한 runtime으로 전환했습니다.') } catch (cause) { setMessage(`runtime 전환 실패: ${cause?.message || String(cause)}`) } finally { setBusy(false) } }
  const changeRuntime = async event => { const kind = event.target.value; setRequestedKind(kind); if (kind === activeKind) return; const candidate = status?.runtime_options?.[kind] || {}; if (!candidate.installed) { setMessage('먼저 선택한 runtime을 다운로드하세요.'); return } await useRuntime(kind) }
  const openFolder = async () => { try { await api('/runtime/open', { method: 'POST', body: { kind: requestedKind } }) } catch (cause) { setMessage(`폴더 열기 실패: ${cause?.message || String(cause)}`) } }
  const serverAction = async action => { setBusy(true); try { await api('/server', { method: 'POST', body: { action } }); await onRefresh() } catch (cause) { setMessage(`server 작업 실패: ${cause?.message || String(cause)}`) } finally { setBusy(false) } }
  const activeModel = status?.models?.find(model => model.id === status?.active_model_id)
  return jsxs('section', { className: `${card} p-4 sm:p-5`, children: [
    jsxs('div', { className: 'flex flex-wrap items-start justify-between gap-4', children: [jsx('div', { children: [jsx('h1', { className: 'text-xl font-semibold tracking-tight', children: 'llama.cpp Manager' }), jsx('p', { className: `mt-1 ${muted}`, children: 'runtime, 모델, parameter를 분리해 관리합니다.' })] }), jsx('div', { className: 'text-right', children: [jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: activeKind === 'prism_ml' ? 'Prism-ML runtime' : 'official runtime' }), jsx('p', { className: 'font-mono text-sm text-(--ui-text-primary)', children: status?.runtime_version || '미설치' })] })] }),
    jsxs('div', { className: 'mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]', children: [jsx('select', { className: input, style: themedSelect(), value: requestedKind, disabled: runtimeBusy || serverBusy, 'aria-label': 'runtime 선택', onChange: changeRuntime, children: [jsx('option', { style: themedOption, value: 'official', children: 'official' }), jsx('option', { style: themedOption, value: 'prism_ml', children: 'Prism-ML' })] }), jsxs('div', { className: 'flex flex-wrap gap-2', children: [jsx('button', { className: button, disabled: runtimeBusy, onClick: openFolder, children: '열기' }), requestedKind === activeKind ? null : jsx('button', { className: primary, disabled: runtimeBusy || serverBusy || !available.installed, onClick: () => useRuntime(requestedKind), children: `${requestedKind === 'prism_ml' ? 'Prism-ML' : 'official'} 사용` }), jsx('button', { className: primary, disabled: runtimeBusy || serverBusy, onClick: install, children: requestedKind === 'prism_ml' ? (available.version ? 'Prism-ML 업데이트' : 'Prism-ML 다운로드') : 'official 업데이트' })] })] }),
    jsxs('div', { className: 'mt-4 grid gap-3 border-t border-(--ui-stroke-secondary) pt-4 sm:grid-cols-3', children: [jsxs('div', { children: [jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'backend' }), jsx('p', { className: 'mt-1 font-mono text-sm', children: activeKind === 'prism_ml' ? 'Prism-ML' : 'official' })] }), jsxs('div', { children: [jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: '활성 모델' }), jsx('p', { className: 'mt-1 truncate font-mono text-sm', title: activeModel?.id, children: activeModel?.id || '선택되지 않음' })] }), jsxs('div', { children: [jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'server' }), jsxs('div', { className: 'mt-1 flex items-center gap-2', children: [jsx(Badge, { tone: status?.server_running ? 'good' : 'warn', children: status?.server_running ? 'running' : 'stopped' }), status?.server_running ? jsx('button', { className: compactDanger, disabled: runtimeBusy, onClick: () => serverAction('stop'), children: '중지' }) : jsx('button', { className: compactPrimary, disabled: runtimeBusy || !status?.runtime_installed || !status?.active_model_id, onClick: () => serverAction('start'), children: '시작' })] })] })] }),
    message ? jsx('p', { className: `mt-3 text-xs ${message.includes('실패') ? 'text-(--dt-destructive)' : muted}`, role: 'status', children: message }) : null,
    jsx(JobProgress, { job: runtimeJob }), jsx(JobProgress, { job: serverJob }), jsx(ServerLogPanel, { status, jobs })
  ] })
}
function ParameterSummary({ options, order }) {
  const keys = order?.length ? order.filter(key => Object.prototype.hasOwnProperty.call(options || {}, key)) : Object.keys(options || {})
  const entries = keys.map(key => [key, options[key]])
  const [expanded, setExpanded] = useState(false)
  if (!entries.length) return null
  const visible = expanded ? entries : entries.slice(0, 4)
  return jsxs('div', { className: 'mt-2 flex flex-wrap gap-1.5 text-xs', children: [
    jsx('div', { className: 'flex flex-wrap gap-1.5', children: visible.map(([key, value]) => jsx('span', { className: 'rounded bg-(--ui-bg-tertiary) px-2 py-1 text-(--ui-text-secondary)', children: `${key}(${value})` }, key)) }),
    entries.length > 4 ? jsx('button', { className: 'px-1 text-(--ui-accent) hover:underline', onClick: () => setExpanded(current => !current), children: expanded ? '접기' : `더보기 (${entries.length - 4}개)` }) : null
  ] })
}

function ModelRow({ model, status, refresh }) {
  const [busy, setBusy] = useState(false)
  const [parameterOpen, setParameterOpen] = useState(false)
  const loaded = Boolean(status?.loaded_models && Object.prototype.hasOwnProperty.call(status.loaded_models, model.id))
  const isActive = status?.active_model_id === model.id
  const display = model.id || 'unknown model'
  const parameterQuery = useQuery({ queryKey: [ID, 'model-settings', model.id], queryFn: () => api(`/settings/${encodeURIComponent(model.id)}`), refetchOnWindowFocus: false })
  const source = model.hf_repo ? 'HF cache · native serving' : 'local GGUF path'
  const serverUsing = Boolean(status?.server_running && isActive)
  const activate = async () => { setBusy(true); try { await api('/activate', { method: 'POST', body: { model_id: model.id } }); refresh() } finally { setBusy(false) } }
  const eject = async () => { setBusy(true); try { await api('/eject', { method: 'POST', body: { model_id: model.id } }); refresh() } finally { setBusy(false) } }
  const remove = async () => { if (!window.confirm(`'${display}' 모델을 삭제할까요?`)) return; setBusy(true); try { await api(`/models/${encodeURIComponent(model.id)}`, { method: 'DELETE' }); refresh() } finally { setBusy(false) } }
  return jsxs('article', { className: 'border-b border-(--ui-stroke-secondary) py-5 last:border-0', 'aria-label': `${display} 등록 모델`, children: [
    jsxs('div', { className: 'grid gap-4 md:grid-cols-[minmax(0,1fr)_auto]', children: [
      jsxs('div', { className: 'min-w-0', children: [
        jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [jsx('h3', { className: 'min-w-0 break-words font-medium', title: display, children: display }), jsx(Badge, { tone: loaded ? 'good' : 'neutral', children: loaded ? '메모리 사용 중' : '대기 중' }), isActive ? jsx(Badge, { tone: serverUsing ? 'good' : 'warn', children: serverUsing ? 'server 사용 중' : 'plugin active' }) : null] }),
        jsxs('dl', { className: 'mt-3 grid grid-cols-2 gap-x-5 gap-y-2 text-xs sm:grid-cols-3', children: [
          jsxs('div', { children: [jsx('dt', { className: 'text-(--ui-text-tertiary)', children: '용량' }), jsx('dd', { className: 'mt-0.5 text-(--ui-text-secondary)', children: model.size_label || 'size unknown' })] }),
          jsxs('div', { children: [jsx('dt', { className: 'text-(--ui-text-tertiary)', children: '소스' }), jsx('dd', { className: 'mt-0.5 text-(--ui-text-secondary)', children: source })] }),
          jsxs('div', { children: [jsx('dt', { className: 'text-(--ui-text-tertiary)', children: '상태' }), jsx('dd', { className: 'mt-0.5 text-(--ui-text-secondary)', children: serverUsing ? 'server 사용 중' : loaded ? '메모리에 적재됨' : '등록됨' })] })
        ] }),
        jsx(ParameterSummary, { options: parameterQuery.data?.options, order: parameterQuery.data?.order })
      ] }),
      jsxs('div', { className: 'flex flex-wrap items-center gap-2 md:justify-end', children: [isActive ? jsx('button', { className: button, disabled: true, 'aria-label': `${display} 선택됨`, children: '선택됨' }) : jsx('button', { className: primary, disabled: busy || status?.server_running, onClick: activate, 'aria-label': `${display} 선택`, title: status?.server_running ? 'server 중지 후 다른 모델을 선택할 수 있습니다.' : undefined, children: '선택' }), jsx('button', { className: danger, disabled: busy || status?.server_running, onClick: remove, 'aria-label': `${display} 삭제`, children: '삭제' })] })
    ] }),
  ] })
}

function SettingsAdder({ settings, options, query, setQuery, selectedKey, setSelectedKey, value, setValue, onAdd, onRemove }) {
  const selected = options.find(option => option.key === selectedKey)
  return jsxs('div', { className: 'mt-4 rounded-md border border-(--ui-stroke-secondary) p-4', children: [
    jsx('p', { className: `mb-3 ${muted}`, children: '현재 llama-server가 허용하는 parameter를 검색해 모델별 설정으로 추가합니다.' }),
    Object.keys(settings).length ? jsxs('div', { className: 'mb-4 space-y-2', children: Object.entries(settings).map(([key, current]) => jsxs('div', { className: 'flex items-center justify-between gap-3 rounded bg-(--ui-bg-tertiary) px-3 py-2 text-sm', children: [jsxs('span', { className: 'min-w-0', children: [jsx('code', { children: key }), jsx('span', { className: `ml-2 ${muted}`, children: `- ${current}` })] }), jsx('button', { className: button, onClick: () => onRemove(key), children: '삭제' })] }, key)) }) : null,
    jsx('input', { className: input, value: query, placeholder: 'parameter 검색 (예: ctx, flash, batch)', onChange: event => setQuery(event.target.value) }),
    options.length ? jsx('div', { className: 'mt-2 max-h-48 overflow-y-auto rounded-md border border-(--ui-stroke-secondary)', children: options.map(option => jsx('button', { className: `flex w-full items-start justify-between gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2 text-left text-sm last:border-0 hover:bg-(--ui-bg-tertiary) ${selectedKey === option.key ? 'bg-(--ui-bg-tertiary)' : ''}`, onClick: () => setSelectedKey(option.key), children: [jsxs('span', { className: 'min-w-0', children: [jsx('code', { children: option.name }), option.value_hint ? jsx('span', { className: `ml-2 ${muted}`, children: option.value_hint }) : null, option.description ? jsx('span', { className: `mt-1 block text-xs ${muted}`, children: option.description }) : null] }), jsx('span', { className: `shrink-0 text-xs ${muted}`, children: option.key })] }, option.key)) }) : query.trim() ? jsx('p', { className: `mt-2 text-xs ${muted}`, children: '일치하는 parameter가 없습니다.' }) : null,
    selected ? jsxs('div', { className: 'mt-3 flex gap-2', children: [jsx('input', { className: input, value, placeholder: selected.value_hint || `${selected.key} 값`, onChange: event => setValue(event.target.value), onKeyDown: event => { if (event.key === 'Enter') onAdd() } }), jsx('button', { className: primary, disabled: !value.trim(), onClick: onAdd, children: '설정 추가' })] }) : null
  ] })
}

function ParameterControl({ id, option, value, onChange, onKeyDown, error, describedBy, ariaLabel, className = input }) {
  const common = { id, value, 'aria-label': ariaLabel, 'aria-invalid': Boolean(error), 'aria-describedby': describedBy, onChange, onKeyDown }
  if (option?.choices?.length) {
    return jsx('select', { ...common, className, style: themedSelect(), children: option.choices.map(choice => jsx('option', { style: themedOption, value: choice, children: choice }, choice)) })
  }
  return jsx('input', { ...common, className })
}

function ModelSettingsEditor({ modelId, refresh, open }) {
  const [query, setQuery] = useState('')
  const [selectedKey, setSelectedKey] = useState('')
  const [value, setValue] = useState('')
  const [draft, setDraft] = useState({})
  const [saving, setSaving] = useState(false)
  const [fieldErrors, setFieldErrors] = useState({})
  const [message, setMessage] = useState('')
  const [presetName, setPresetName] = useState('')
  const [selectedPresetId, setSelectedPresetId] = useState('')
  const settings = useQuery({ queryKey: [ID, 'model-settings', modelId], queryFn: () => api(`/settings/${encodeURIComponent(modelId)}`), enabled: open, refetchOnWindowFocus: false })
  const options = useQuery({ queryKey: [ID, 'model-settings-options', query], queryFn: () => api(`/settings?q=${encodeURIComponent(query)}&limit=20`), enabled: open && query.trim().length > 0, refetchOnWindowFocus: false })
  const presetsQuery = useQuery({ queryKey: [ID, 'parameter-presets'], queryFn: () => api('/presets'), enabled: open, refetchOnWindowFocus: false })
  const availableOptions = (options.data?.options || []).filter(option => !Object.prototype.hasOwnProperty.call(draft, option.key))
  const selected = availableOptions.find(option => option.key === selectedKey)
  useEffect(() => {
    if (open && settings.data) { setDraft(settings.data.options || {}); setFieldErrors({}) }
  }, [open, settings.data?.model_id])
  const validateDraft = next => Object.fromEntries(Object.entries(next).map(([key, current]) => [key, optionValueError(settings.data?.metadata?.[key] || { key, requires_value: true }, current)]).filter(([, error]) => error))
  const persist = async next => {
    const errors = validateDraft(next)
    if (Object.keys(errors).length) { setFieldErrors(errors); setMessage('입력값을 확인하세요.'); return false }
    setSaving(true)
    setMessage('')
    try {
      const result = await api(`/settings/${encodeURIComponent(modelId)}`, { method: 'PUT', body: { options: next } })
      const savedOptions = result.options || next
      queryClient.setQueryData([ID, 'model-settings', modelId], previous => ({ ...(previous || {}), model_id: modelId, options: savedOptions }))
      await settings.refetch()
      setDraft(savedOptions)
      setFieldErrors({})
      setMessage(result.requires_restart ? '저장했습니다. 현재 server는 유지되며 다음 start에 적용됩니다.' : '반영했습니다.')
      refresh()
      return true
    } catch (cause) {
      setMessage(`반영 실패: ${cause?.message || String(cause)}`)
      return false
    } finally {
      setSaving(false)
    }
  }
  const add = async () => {
    if (!selected) return
    const validationError = optionValueError(selected, value)
    if (validationError) { setMessage(validationError); return }
    const next = { ...draft, [selectedKey]: selected.requires_value ? value.trim() : '' }
    if (await persist(next)) { setDraft(next); setSelectedKey(''); setValue('') }
  }
  const remove = async key => {
    const next = { ...draft }
    delete next[key]
    if (await persist(next)) { setDraft(next); setFieldErrors(previous => { const copy = { ...previous }; delete copy[key]; return copy }) }
  }
  const update = (key, nextValue) => { setDraft(previous => ({ ...previous, [key]: nextValue })); setFieldErrors(previous => { const copy = { ...previous }; delete copy[key]; return copy }) }
  const savePreset = async () => {
    if (!presetName.trim()) { setMessage('preset 이름을 입력하세요.'); return }
    try {
      await api('/presets', { method: 'POST', body: { name: presetName.trim(), options: draft } })
      await presetsQuery.refetch()
      setPresetName('')
      setMessage('현재 parameter를 preset으로 저장했습니다.')
    } catch (cause) { setMessage(`preset 저장 실패: ${cause?.message || String(cause)}`) }
  }
  const applyPreset = async () => {
    if (!selectedPresetId) return
    try {
      const result = await api(`/presets/${encodeURIComponent(selectedPresetId)}/apply`, { method: 'POST', body: { model_id: modelId } })
      setDraft(result.options || {})
      await settings.refetch()
      const omitted = result.omitted_options?.length ? ` Prism-ML에서 관리하지 않는 ${result.omitted_options.join(', ')}은 제외했습니다.` : ''
      setMessage((result.requires_restart ? 'preset을 적용했습니다. 현재 server는 유지되며 다음 start에 반영됩니다.' : 'preset을 적용했습니다.') + omitted)
      refresh()
    } catch (cause) { setMessage(`preset 적용 실패: ${cause?.message || String(cause)}`) }
  }
  const deletePreset = async () => {
    if (!selectedPresetId) return
    try {
      await api(`/presets/${encodeURIComponent(selectedPresetId)}`, { method: 'DELETE' })
      await presetsQuery.refetch()
      setSelectedPresetId('')
      setMessage('preset을 삭제했습니다.')
    } catch (cause) { setMessage(`preset 삭제 실패: ${cause?.message || String(cause)}`) }
  }
  return open ? jsxs('div', { className: 'mt-3 rounded-md border border-(--ui-stroke-secondary) p-3 sm:p-4', children: [
      jsxs('div', { className: 'mb-2 flex items-center justify-between gap-3', children: [jsx('p', { className: `text-xs ${muted}`, children: '이 등록 모델에 적용할 llama-server parameter를 수정합니다.' }), jsx('button', { className: compactPrimary, disabled: saving, onClick: () => persist(draft), 'aria-label': `${modelId} parameter 저장`, children: '저장' })] }),
      jsxs('div', { className: 'mb-4 rounded-md bg-(--ui-bg-tertiary) p-3', children: [
        jsx('p', { className: 'mb-1 text-xs font-medium text-(--ui-text-primary)', children: '공용 parameter preset' }),
        jsx('p', { className: `mb-2 text-xs ${muted}`, children: 'runtime·모델과 무관하게 공유됩니다. 적용하면 현재 선택 모델에 저장되고, server는 다음 시작에 반영됩니다.' }),
        jsxs('div', { className: 'flex flex-wrap gap-2', children: [
          jsx('select', { className: compactInput, style: themedSelect(), value: selectedPresetId, 'aria-label': `${modelId} parameter preset 선택`, onChange: event => setSelectedPresetId(event.target.value), children: [jsx('option', { style: themedOption, value: '', children: presetsQuery.isLoading ? 'preset 불러오는 중…' : 'preset 선택' }), ...(presetsQuery.data?.presets || []).map(preset => jsx('option', { style: themedOption, value: preset.id, children: `${preset.name} · 공용` }, preset.id))] }),
          jsx('button', { className: compactPrimary, disabled: !selectedPresetId || saving, onClick: applyPreset, children: '적용' }),
          jsx('button', { className: compactDanger, disabled: !selectedPresetId || saving, onClick: deletePreset, children: '삭제' })
        ] }),
        jsxs('div', { className: 'mt-2 flex gap-2', children: [jsx('input', { className: compactInput, value: presetName, 'aria-label': `${modelId} 새 preset 이름`, placeholder: '새 preset 이름', onChange: event => setPresetName(event.target.value), onKeyDown: event => { if (event.key === 'Enter') savePreset() } }), jsx('button', { className: compactPrimary, disabled: saving || !presetName.trim(), onClick: savePreset, children: '현재 설정 저장' })] })
      ] }),
      settings.isLoading ? jsx('p', { className: muted, role: 'status', children: '현재 설정을 불러오는 중…' }) : null,
      jsx('div', { className: 'grid gap-x-4 gap-y-1 sm:grid-cols-2', children: Object.entries(draft).map(([key, current]) => jsxs('div', { className: 'flex min-w-0 items-start gap-2 border-b border-(--ui-stroke-secondary) py-1.5', children: [jsx('label', { className: 'w-24 shrink-0 truncate pt-2 text-xs font-medium text-(--ui-text-primary)', htmlFor: `parameter-${modelId}-${key}`, children: key }), jsxs('div', { className: 'min-w-0 flex-1', children: [jsxs('div', { className: 'flex min-w-0 items-center gap-1.5', children: [jsx(ParameterControl, { id: `parameter-${modelId}-${key}`, option: settings.data?.metadata?.[key], value: current, className: compactInput, error: fieldErrors[key], describedBy: fieldErrors[key] ? `parameter-error-${modelId}-${key}` : undefined, ariaLabel: `${key} 값`, onChange: event => update(key, event.target.value) }), jsx('button', { className: compactDanger, disabled: saving, onClick: () => remove(key), 'aria-label': `${key} parameter 삭제`, children: '삭제' })] }), fieldErrors[key] ? jsx('p', { id: `parameter-error-${modelId}-${key}`, className: 'mt-1 text-[11px] text-(--dt-destructive)', role: 'alert', children: fieldErrors[key] }) : null] })] }, key)) }),
      jsx('div', { className: 'mt-4 border-t border-(--ui-stroke-secondary) pt-3', children: jsx('p', { className: 'mb-2 text-xs font-medium text-(--ui-text-primary)', children: 'parameter 추가' }) }),
      jsx('input', { className: compactInput, type: 'search', value: query, 'aria-label': '추가할 llama-server parameter 검색', placeholder: 'parameter 검색 (예: ctx, flash, batch)', onChange: event => setQuery(event.target.value) }),
      availableOptions.length ? jsx('div', { className: 'mt-2 max-h-40 overflow-y-auto rounded-md border border-(--ui-stroke-secondary)', role: 'listbox', 'aria-label': 'llama-server parameter 검색 결과', children: availableOptions.map(option => jsx('button', { className: `flex w-full items-start justify-between gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2 text-left text-sm last:border-0 hover:bg-(--ui-bg-tertiary) ${selectedKey === option.key ? 'bg-(--ui-bg-tertiary)' : ''}`, type: 'button', role: 'option', 'aria-selected': selectedKey === option.key, onClick: () => { setSelectedKey(option.key); setValue(Object.prototype.hasOwnProperty.call(draft, option.key) ? String(draft[option.key]) : option.default_value || '') }, children: [jsx('code', { children: option.name }), jsx('span', { className: `shrink-0 text-xs ${muted}`, children: option.requires_value ? option.default_value ? `기본값 ${option.default_value}` : option.key : 'flag' })] }, option.key)) }) : query.trim() && !options.isLoading ? jsx('p', { className: `mt-2 text-xs ${muted}`, role: 'status', children: '일치하는 parameter가 없습니다.' }) : null,
      selected ? jsxs('div', { className: 'mt-2 flex gap-2', children: [selected.requires_value ? jsx(ParameterControl, { id: `new-parameter-${selected.key}`, option: selected, value, className: compactInput, ariaLabel: `${selected.key} 값`, onChange: event => setValue(event.target.value), onKeyDown: event => { if (event.key === 'Enter') add() } }) : jsx('span', { className: `flex flex-1 items-center px-2 text-xs ${muted}`, role: 'status', children: '값이 없는 flag parameter' }), jsx('button', { className: compactPrimary, disabled: saving || (selected.requires_value && !value.trim()), onClick: add, 'aria-label': `${selected.key} parameter 추가 또는 수정`, children: '추가/수정' })] }) : null,
      message ? jsx('p', { className: `mt-3 text-xs ${message.startsWith('반영 실패') || message === '입력값을 확인하세요.' ? 'text-(--dt-destructive)' : muted}`, role: 'status', children: message }) : null
    ] }) : null
}

function RegisterWizard({ close, refresh, initialRepo = '', mode = 'download' }) {
  const [queryText, setQueryText] = useState('')
  const [searched, setSearched] = useState(false)
  const [repo, setRepo] = useState(initialRepo)
  const [fileIndex, setFileIndex] = useState('')
  const [jobId, setJobId] = useState('')
  const [actionMessage, setActionMessage] = useState('')
  const search = useQuery({ queryKey: [ID, 'search', queryText], queryFn: () => api(`/search?q=${encodeURIComponent(queryText)}&limit=20`), enabled: mode === 'download' && searched && queryText.trim().length > 1 })
  const files = useQuery({ queryKey: [ID, 'files', repo], queryFn: () => api(`/repo?repo_id=${encodeURIComponent(repo)}`), enabled: mode === 'download' && Boolean(repo) })
  const cachedFiles = useQuery({ queryKey: [ID, 'cached-files', repo], queryFn: () => api(`/hf-models/files?repo_id=${encodeURIComponent(repo)}`), enabled: mode === 'register' && Boolean(repo), refetchOnWindowFocus: false })
  const groups = mode === 'register' ? cachedFiles.data?.files || [] : files.data?.files || []
  const selected = fileIndex === '' ? (mode === 'register' && groups.length === 1 ? groups[0] : undefined) : groups[Number(fileIndex)]
  const job = useQuery({
    queryKey: [ID, 'job', jobId],
    queryFn: () => api(`/jobs/${encodeURIComponent(jobId)}`),
    enabled: Boolean(jobId),
    refetchInterval: query => query.state.data?.status === 'running' ? 1000 : false,
    refetchOnWindowFocus: false
  })
  useEffect(() => { if (job.data?.status === 'done') refresh() }, [job.data?.status])
  const download = async () => {
    if (!repo || !selected) return
    setActionMessage('')
    const result = await api('/download-browsed', { method: 'POST', body: { repo, paths: selected.paths } })
    if (result.job_id) setJobId(result.job_id)
    else refresh()
  }
  const registerModel = async () => {
    if (!repo || !selected) return
    setActionMessage('')
    await api('/register', { method: 'POST', body: { repo, paths: selected.paths } })
    setActionMessage('모델을 명시적으로 등록했습니다. 다운로드와 등록은 별개입니다.')
    refresh()
  }
  const running = job.data?.status === 'running'
  useEffect(() => { setRepo(initialRepo); setFileIndex(''); setActionMessage('') }, [initialRepo])
  useEffect(() => { if (mode === 'register' && fileIndex === '' && groups.length) setFileIndex('0') }, [mode, fileIndex, groups.length])
  return jsxs('section', { className: `${card} mt-5 p-4 sm:p-5`, 'aria-labelledby': 'llama-wizard-title', children: [
    jsxs('div', { className: 'flex items-start justify-between gap-4', children: [jsx('div', { className: 'min-w-0', children: [jsx('div', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-accent)', children: mode === 'register' ? '다운받은 모델 등록' : '모델 다운로드' }), jsx('h2', { id: 'llama-wizard-title', className: 'mt-1 text-lg font-semibold', children: mode === 'register' ? '다운받은 GGUF 파일 등록' : 'Hugging Face 저장소와 양자화 선택' })] }), jsx('button', { className: button, onClick: close, 'aria-label': '모델 wizard 닫기', children: '닫기' })] }),
    mode === 'download' ? jsxs('div', { className: 'mt-5 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]', children: [jsx('input', { className: input, type: 'search', value: queryText, 'aria-label': 'Hugging Face 모델 검색', placeholder: '예: Qwen GGUF, unsloth/Qwen…', onChange: event => setQueryText(event.target.value), onKeyDown: event => { if (event.key === 'Enter') setSearched(true) } }), jsx('button', { className: primary, onClick: () => setSearched(true), 'aria-label': 'Hugging Face 검색 실행', children: 'HF 검색' })] }) : null,
    mode === 'download' && search.isLoading ? jsx('p', { className: `mt-3 ${muted}`, children: 'Hugging Face 검색 중…' }) : null,
    mode === 'download' && search.error ? jsx('p', { className: 'mt-3 text-sm text-(--dt-destructive)', children: String(search.error.message || search.error) }) : null,
    mode === 'download' && search.data?.hits?.length ? jsx('div', { className: 'mt-3 rounded-md border border-(--ui-stroke-secondary) overscroll-contain', style: { maxHeight: '24rem', overflowY: 'auto', scrollbarColor: 'var(--ui-stroke-secondary) transparent' }, children: search.data.hits.map(hit => jsx('button', { className: 'flex w-full items-center justify-between border-b border-(--ui-stroke-secondary) px-3 py-2 text-left text-sm last:border-0 hover:bg-(--ui-bg-tertiary)', onClick: () => { setRepo(hit.repo); setFileIndex(''); setActionMessage('') }, children: [jsx('span', { className: 'min-w-0 break-words pr-3 font-mono', children: hit.repo }), jsx('span', { className: `shrink-0 ${muted}`, children: `${Number(hit.downloads || 0).toLocaleString()} downloads` })] }, hit.repo))}) : null,
    mode === 'register' && repo ? jsxs('div', { className: 'mt-5', children: [jsx('div', { className: 'mb-2 flex items-center justify-between gap-3', children: [jsx('span', { className: 'min-w-0 break-words text-sm font-medium', children: repo }), jsx('span', { className: `shrink-0 text-xs ${muted}`, children: '다운로드 완료 파일' })] }), cachedFiles.isLoading ? jsx('p', { className: muted, role: 'status', children: '다운받은 파일을 확인하는 중…' }) : cachedFiles.error ? jsx('p', { className: 'text-sm text-(--dt-destructive)', role: 'alert', children: String(cachedFiles.error.message || cachedFiles.error) }) : cachedFiles.data?.warning ? jsx('p', { className: 'text-sm text-(--dt-destructive)', role: 'alert', children: cachedFiles.data.warning }) : groups.length === 1 ? jsx('div', { className: 'rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) px-3 py-2 text-sm', children: selected?.paths.join(', ') }) : groups.length ? jsx('select', { className: input, style: themedSelect(), value: fileIndex, 'aria-label': '등록할 GGUF 파일 선택', onChange: event => setFileIndex(event.target.value), children: groups.map((group, index) => jsx('option', { style: themedOption, value: index, children: group.paths.join(', ') }, `${group.label}-${index}`)) }) : jsx('p', { className: muted, children: '다운로드가 완료된 GGUF 파일이 없습니다.' }), selected ? jsx('p', { className: `mt-2 ${muted}`, children: `${selected.paths.length > 1 ? `${selected.paths.length}개 split part · ` : ''}${selected.fit}` }) : null] }) : null,
    mode === 'download' && repo ? jsxs('div', { className: 'mt-5', children: [jsx('div', { className: 'mb-2 flex items-center justify-between gap-3', children: [jsx('span', { className: 'min-w-0 break-words text-sm font-medium', children: repo }), jsx('span', { className: `shrink-0 text-xs ${muted}`, children: 'Q4 / Q5 / IQ / F16' })] }), files.isLoading ? jsx('p', { className: muted, role: 'status', children: 'repo의 GGUF 양자화 목록을 불러오는 중…' }) : jsx('select', { className: input, style: themedSelect(), value: fileIndex, 'aria-label': '다운로드할 양자화 선택', onChange: event => setFileIndex(event.target.value), children: [jsx('option', { style: themedOption, value: '', children: '양자화를 선택하세요' }), ...groups.map((group, index) => jsx('option', { style: themedOption, value: index, children: `${group.label} · ${(Number(group.total_bytes || 0) / (1 << 30)).toFixed(1)} GB · ${group.fit}` }, `${group.label}-${index}`))] }), selected ? jsx('p', { className: `mt-2 ${muted}`, children: `${selected.paths.length > 1 ? `${selected.paths.length}개 split part 다운로드 · ` : ''}${selected.fit}` }) : null] }) : null,
    jsx(JobProgress, { job: job.data }),
    actionMessage ? jsx('p', { className: `mt-3 text-xs ${muted}`, role: 'status', children: actionMessage }) : null,
    jsxs('div', { className: 'mt-5 flex flex-wrap justify-end gap-2', children: [jsx('button', { className: button, onClick: close, 'aria-label': '모델 wizard 취소', children: '취소' }), mode === 'download' ? jsx('button', { className: primary, disabled: !selected || running, onClick: download, 'aria-label': '선택한 GGUF 다운로드', children: '선택 항목 다운로드' }) : jsx('button', { className: primary, disabled: !selected || running, onClick: registerModel, 'aria-label': '선택한 GGUF 등록', children: '선택 항목 등록' })] })
  ] })
}

function DownloadedModelRow({ model, onRegister, onDelete }) {
  return jsxs('article', { className: 'grid gap-3 border-b border-(--ui-stroke-secondary) py-4 last:border-0 sm:grid-cols-[minmax(0,1fr)_auto]', 'aria-label': `${model.repo_id} 다운로드 모델`, children: [
    jsxs('div', { className: 'min-w-0', children: [jsx('h3', { className: 'break-words font-medium', title: model.repo_id, children: model.repo_id }), jsx('p', { className: `mt-1 text-xs ${muted}`, children: 'HF cache inventory · 아직 plugin에 등록되지 않음' })] }),
    jsxs('div', { className: 'flex flex-wrap items-center gap-2 sm:justify-end', children: [jsx(Badge, { children: model.size || 'size unknown' }), jsx('button', { className: primary, onClick: () => onRegister(model.repo_id), 'aria-label': `${model.repo_id} 등록`, children: '등록' }), jsx('button', { className: danger, onClick: () => onDelete(model.repo_id), 'aria-label': `${model.repo_id} cache 삭제`, children: '삭제' })] })
  ] })
}

function TabBar({ active, setActive }) {
  const tabs = [['runtime', '운영'], ['models', '모델'], ['parameters', '파라미터']]
  return jsx('nav', { className: 'mb-5 flex gap-1 border-b border-(--ui-stroke-secondary)', 'aria-label': 'llama.cpp 관리 탭', children: tabs.map(([id, label]) => jsx('button', { className: `border-b-2 px-3 py-2 text-sm ${active === id ? 'border-(--ui-accent) text-(--ui-text-primary)' : 'border-transparent text-(--ui-text-secondary) hover:text-(--ui-text-primary)'}`, onClick: () => setActive(id), 'aria-current': active === id ? 'page' : undefined, children: label }, id)) })
}

function Page() {
  const [wizard, setWizard] = useState(null)
  const [tab, setTab] = useState('runtime')
  const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => api('/status'), refetchInterval: query => query.state.data?.server_running ? 1500 : 3000, refetchOnWindowFocus: true })
  const jobs = useQuery({ queryKey: [ID, 'jobs'], queryFn: () => api('/jobs'), refetchInterval: query => query.state.data?.jobs?.some(job => job.status === 'running') ? 1000 : false, refetchOnWindowFocus: false })
  const hfModels = useQuery({ queryKey: [ID, 'hf-models', status.data?.runtime_kind], queryFn: () => api('/hf-models'), enabled: Boolean(status.data), refetchOnWindowFocus: false })
  const data = status.data; const models = data?.models || []; const localModels = hfModels.data?.models || []; const runtimeJobs = jobs.data?.jobs || []
  useEffect(() => { if (runtimeJobs.some(job => ['runtime-install', 'prism-runtime-install'].includes(job.kind) && job.status === 'done')) queryClient.invalidateQueries({ queryKey: [ID, 'status'] }) }, [runtimeJobs])
  const refreshStatus = () => Promise.all([queryClient.invalidateQueries({ queryKey: [ID, 'status'] }), queryClient.invalidateQueries({ queryKey: [ID, 'jobs'] })])
  const refreshInventory = () => queryClient.invalidateQueries({ queryKey: [ID, 'hf-models'] })
  const refreshWizard = () => Promise.all([refreshStatus(), refreshInventory()])
  const openDownload = () => setWizard({ mode: 'download', repo: '' })
  const openRegister = repo => setWizard({ mode: 'register', repo })
  const deleteDownloaded = async repoId => { if (!window.confirm(`'${repoId}' HF cache를 삭제할까요?`)) return; try { await api('/hf-models/delete', { method: 'POST', body: { repo_id: repoId } }); await hfModels.refetch() } catch (cause) { window.alert(`모델 삭제 실패: ${cause?.message || String(cause)}`) } }
  const activeModel = models.find(model => model.id === data?.active_model_id)
  return jsxs('main', { className: 'mx-auto max-w-5xl p-4 sm:p-6', children: [jsx(TabBar, { active: tab, setActive: setTab }), tab === 'runtime' ? jsx(RuntimeCard, { status: data, jobs: runtimeJobs, onRefresh: refreshStatus }) : null,
    tab === 'models' ? jsxs('div', { children: [jsxs('div', { className: 'flex items-end justify-between gap-4', children: [jsx('div', { children: [jsx('h2', { className: 'text-lg font-semibold', children: '모델' }), jsx('p', { className: `mt-1 ${muted}`, children: data?.runtime_kind === 'prism_ml' ? 'Prism-ML Bonsai/Ternary GGUF만 표시·등록합니다.' : '등록 모델과 HF cache inventory를 분리해 표시합니다.' })] }), jsx('button', { className: primary, onClick: openDownload, children: '+ 모델 다운로드' })] }), jsx('section', { className: `${card} mt-4 px-5`, children: status.isLoading ? jsx('p', { className: `py-6 ${muted}`, children: '상태를 불러오는 중…' }) : models.length ? models.map(model => jsx(ModelRow, { model, status: data, refresh: refreshStatus }, model.id)) : jsx('p', { className: `py-8 text-center ${muted}`, children: '등록된 모델이 없습니다.' }) }), jsxs('section', { className: `${card} mt-5 px-5`, children: [jsxs('div', { className: 'flex items-center justify-between gap-3 py-4', children: [jsx('h2', { className: 'text-lg font-semibold', children: '다운받은 모델' }), jsx('button', { className: button, disabled: hfModels.isFetching, onClick: () => hfModels.refetch(), children: '새로고침' })] }), hfModels.isLoading ? jsx('p', { className: `pb-5 ${muted}`, children: 'inventory를 불러오는 중…' }) : localModels.length ? localModels.map(model => jsx(DownloadedModelRow, { model, onRegister: openRegister, onDelete: deleteDownloaded }, model.repo_id)) : jsx('p', { className: `pb-5 ${muted}`, children: data?.runtime_kind === 'prism_ml' ? 'Prism-ML 호환 다운로드 모델이 없습니다.' : '다운받은 모델이 없습니다.' })] })] }) : null,
    tab === 'parameters' ? jsxs('section', { className: `${card} p-4 sm:p-5`, children: [jsx('h2', { className: 'text-lg font-semibold', children: '파라미터와 preset' }), jsx('p', { className: `mt-1 ${muted}`, children: '모델을 선택한 뒤 parameter를 검색·추가하고 현재 설정을 preset으로 저장합니다.' }), activeModel ? jsx(ModelSettingsEditor, { modelId: activeModel.id, refresh: refreshStatus, open: true }) : jsx('p', { className: `mt-6 rounded-md bg-(--ui-bg-tertiary) p-4 ${muted}`, children: '먼저 모델 탭에서 사용할 모델을 선택하세요.' })] }) : null,
    wizard ? jsx(RegisterWizard, { close: () => setWizard(null), refresh: refreshWizard, initialRepo: wizard.repo, mode: wizard.mode }) : null
  ] })
}
export default {
  id: ID,
  name: 'llama.cpp Manager',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/llamacpp' }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/llamacpp', label: 'llama.cpp', codicon: 'server-process' } }
    ])
  }
}
