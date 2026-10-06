import React from 'react'
import pluginSdk from '@hermes/plugin-sdk'

const { createElement, useEffect, useLayoutEffect, useRef, useState } = React
const { ROUTES_AREA, SIDEBAR_NAV_AREA, host, queryClient, useQuery, useValue } = pluginSdk
const jsx = (type, props, key) => createElement(type, key === undefined ? props : { ...props, key })
const jsxs = jsx

const ID = 'llamacpp'
const LOG_NEWLINE = String.fromCharCode(10)
let pluginCtx
let desktopLeaseDispose
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

function mergeResourceMetricFrame(previous, frame) {
  if (!previous || !frame?.snapshot || !frame?.sample) return previous
  const windowSeconds = Number(previous.window_seconds) || 60
  const sampledAt = Number(frame.sample.at || frame.snapshot.sampled_at || frame.timestamp)
  if (!Number.isFinite(sampledAt)) return previous
  const cutoff = sampledAt - windowSeconds
  const series = (Array.isArray(previous.series) ? previous.series : [])
    .filter(row => Number(row?.at) >= cutoff && Number(row?.at) !== sampledAt)
    .concat(frame.sample)
  const preserveCompressionPage = previous.compression_cursor !== frame.snapshot.compression_cursor
  return {
    ...previous,
    ...frame.snapshot,
    ...(preserveCompressionPage ? {
      compression_calls: previous.compression_calls,
      compression_calls_next_cursor: previous.compression_calls_next_cursor,
      compression_cursor: previous.compression_cursor
    } : {}),
    window_seconds: windowSeconds,
    series
  }
}

function applyResourceMetricFrame(frame) {
  queryClient.setQueriesData(
    { queryKey: [ID, 'resource-metrics'] },
    previous => mergeResourceMetricFrame(previous, frame)
  )
}

function useCoordinatorPush() {
  const [pushConnected, setPushConnected] = useState(false)
  const activeProfile = useValue(host.state.profile)
  useEffect(() => {
    setPushConnected(false)
    const timers = new Map()
    let lastFrameAt = 0
    const keys = {
      status: [ID, 'status'],
      jobs: [ID, 'jobs'],
      logs: [ID, 'activity-log'],
      metrics: [ID, 'resource-metrics'],
      inventory: [ID, 'hf-models']
    }
    const schedule = name => {
      const key = keys[name]
      if (!key) return
      const current = timers.get(name)
      if (current) window.clearTimeout(current)
      timers.set(name, window.setTimeout(() => {
        timers.delete(name)
        queryClient.invalidateQueries({ queryKey: key })
      }, 200))
    }
    let disposeSocket = () => {}
    try {
      disposeSocket = pluginCtx.socket('/events', frame => {
        if (!frame || typeof frame !== 'object') return
        lastFrameAt = Date.now()
        setPushConnected(true)
        if (frame.type === 'resource_sample') applyResourceMetricFrame(frame)
        for (const name of Array.isArray(frame.refresh) ? frame.refresh : []) schedule(name)
      })
    } catch {
      setPushConnected(false)
    }
    const stopHealthCheck = pluginCtx.setInterval(() => {
      if (lastFrameAt && Date.now() - lastFrameAt > 25000) setPushConnected(false)
    }, 5000)
    return () => {
      disposeSocket()
      stopHealthCheck()
      for (const timer of timers.values()) window.clearTimeout(timer)
      timers.clear()
    }
  }, [activeProfile])
  return pushConnected
}

function startDesktopLease(ctx) {
  const clientId = globalThis.crypto?.randomUUID?.() || `desktop-${Date.now()}-${Math.random().toString(16).slice(2)}`
  let stopped = false
  let sending = false
  let stopTimer = () => {}
  let stopPagehide = () => {}
  let stopBeforeUnload = () => {}
  const touch = async () => {
    if (stopped || sending) return
    sending = true
    try { await ctx.rest('/lifecycle/lease', { method: 'POST', body: { client_id: clientId } }) } catch { /* expiry handles backend/app shutdown races */ } finally { sending = false }
  }
  const release = () => {
    if (stopped) return
    stopped = true
    stopTimer()
    stopPagehide()
    stopBeforeUnload()
    ctx.rest('/lifecycle/lease', { method: 'DELETE', body: { client_id: clientId } }).catch(() => {})
  }
  stopTimer = ctx.setInterval(touch, 2500)
  stopPagehide = ctx.addEventListener(window, 'pagehide', release, { once: true })
  stopBeforeUnload = ctx.addEventListener(window, 'beforeunload', release, { once: true })
  ctx.onDispose(release)
  touch()
  return release
}

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

function isPrismOnlyModel(value) {
  return String(value || '').split('/').pop().startsWith('Ternary-Bonsai')
}

function PrismOnlyHint({ model }) {
  if (!isPrismOnlyModel(model)) return null
  return jsx('span', { className: 'inline-flex h-5 w-5 shrink-0 cursor-help items-center justify-center rounded-full border border-(--ui-stroke-secondary) text-[11px] font-semibold text-(--ui-text-secondary)', title: 'Prism-ML 전용', 'aria-label': 'Prism-ML 전용 모델 안내', tabIndex: 0, children: '?' })
}

function formatRate(value) {
  return value != null && Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} tok/s` : '—'
}

function formatWindowOffset(seconds) {
  if (seconds < 60) return `${seconds}초 전`
  if (seconds >= 86400) return `${seconds / 86400}일 전`
  if (seconds >= 3600) return `${seconds / 3600}시간 전`
  return `${Math.round(seconds / 60)}분 전`
}

function formatSampleTime(value) {
  if (!value) return '—'
  return new Date(Number(value) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatTimestamp(value) {
  if (!value) return '—'
  return new Date(Number(value) * 1000).toLocaleString([], {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  })
}

function niceMaximum(value, fixedMaximum) {
  if (fixedMaximum) return Number(fixedMaximum)
  const raw = Math.max(1, Number(value) || 0)
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const normalized = raw / magnitude
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10
  return step * magnitude
}

function formatAxisTick(value, unit) {
  if (unit === '%') return `${Math.round(value)}%`
  if (unit === '요청') return String(Math.round(value))
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`
  if (value >= 100) return String(Math.round(value))
  return Number(value).toFixed(value > 0 && value < 10 ? 1 : 0)
}

function chartValue(value, unit) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  if (unit === '%') return `${Number(value).toFixed(1)}%`
  if (unit === '요청') return String(Math.round(Number(value)))
  return `${Number(value).toFixed(1)} ${unit || ''}`.trim()
}

function MetricChart({ samples, series, label, leftAxis, rightAxis, windowSeconds, latestAt: requestedLatestAt }) {
  const rows = samples || []
  const [hoveredSample, setHoveredSample] = useState(null)
  const svgRef = useRef(null)
  const plot = { left: 58, right: 582, top: 20, bottom: 150 }
  const ticks = [0, 0.25, 0.5, 0.75, 1]
  const latestAt = Number(requestedLatestAt) || (rows.length ? Math.max(...rows.map(row => Number(row.at) || 0)) : Date.now() / 1000)
  const earliestAt = latestAt - windowSeconds
  const axisValues = axis => rows.flatMap(sample => series.filter(item => item.axis === axis && sample?.[item.field] != null).map(item => Math.max(0, Number(sample[item.field]) || 0)))
  const hasLeftData = axisValues('left').length > 0
  const hasRightData = rightAxis ? axisValues('right').length > 0 : false
  const hasData = hasLeftData || hasRightData
  const leftMaximum = niceMaximum(Math.max(Number(leftAxis?.minimumMaximum) || 0, ...axisValues('left')), leftAxis?.maximum)
  const rightMaximum = rightAxis ? niceMaximum(Math.max(Number(rightAxis.minimumMaximum) || 0, ...axisValues('right')), rightAxis.maximum) : leftMaximum
  const xFor = sample => plot.left + Math.max(0, Math.min(1, ((Number(sample?.at) || earliestAt) - earliestAt) / windowSeconds)) * (plot.right - plot.left)
  const yFor = (value, maximum) => plot.bottom - (Math.max(0, Number(value) || 0) / maximum) * (plot.bottom - plot.top)
  const segmentsFor = item => {
    const maximum = item.axis === 'right' ? rightMaximum : leftMaximum
    const points = rows.filter(sample => sample?.[item.field] != null)
      .map(sample => `${xFor(sample)},${yFor(sample[item.field], maximum)}`)
    return points.length ? [points] : []
  }
  const onPointerMove = event => {
    if (!rows.length || !svgRef.current) return
    const bounds = svgRef.current.getBoundingClientRect()
    const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / Math.max(1, bounds.width)))
    const target = earliestAt + ratio * windowSeconds
    setHoveredSample(rows.reduce((best, row) => Math.abs(Number(row.at) - target) < Math.abs(Number(best.at) - target) ? row : best, rows[0]))
  }
  const hoverX = hoveredSample ? xFor(hoveredSample) : null
  const hoverDetails = hoveredSample ? [formatSampleTime(hoveredSample.at), ...series.map(item => `${item.label}: ${chartValue(hoveredSample[item.field], item.axis === 'right' ? rightAxis?.unit : leftAxis?.unit)}`)].join(' · ') : '그래프 위에 마우스를 올리면 시점별 값을 확인할 수 있습니다.'
  return jsxs('div', { children: [
    jsx('svg', { ref: svgRef, className: 'mt-1 h-44 w-full touch-none', viewBox: '0 0 640 184', preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': label, onPointerMove, onPointerLeave: () => setHoveredSample(null), children: [
      ...ticks.map(ratio => {
        const y = plot.bottom - ratio * (plot.bottom - plot.top)
        return jsxs('g', { children: [
          jsx('line', { x1: plot.left, y1: y, x2: plot.right, y2: y, stroke: 'var(--ui-stroke-secondary)', strokeWidth: ratio === 0 ? 1 : 0.6, strokeDasharray: ratio === 0 ? undefined : '3 4' }),
          ratio === 0 || hasLeftData ? jsx('text', { x: plot.left - 7, y: y + 3, textAnchor: 'end', fill: 'var(--ui-text-tertiary)', fontSize: 9, children: formatAxisTick(leftMaximum * ratio, leftAxis?.unit) }) : null,
          rightAxis && (ratio === 0 || hasRightData) ? jsx('text', { x: plot.right + 7, y: y + 3, textAnchor: 'start', fill: 'var(--ui-text-tertiary)', fontSize: 9, children: formatAxisTick(rightMaximum * ratio, rightAxis.unit) }) : null
        ] }, `tick-${ratio}`)
      }),
      jsx('text', { x: plot.left, y: 10, textAnchor: 'start', fill: 'var(--ui-text-secondary)', fontSize: 9, fontWeight: 600, children: leftAxis?.label }),
      rightAxis ? jsx('text', { x: plot.right, y: 10, textAnchor: 'end', fill: 'var(--ui-text-secondary)', fontSize: 9, fontWeight: 600, children: rightAxis.label }) : null,
      jsx('text', { x: plot.left, y: 173, textAnchor: 'start', fill: 'var(--ui-text-tertiary)', fontSize: 9, children: formatWindowOffset(windowSeconds) }),
      jsx('text', { x: (plot.left + plot.right) / 2, y: 173, textAnchor: 'middle', fill: 'var(--ui-text-tertiary)', fontSize: 9, children: formatWindowOffset(windowSeconds / 2) }),
      jsx('text', { x: plot.right, y: 173, textAnchor: 'end', fill: 'var(--ui-text-tertiary)', fontSize: 9, children: '현재' }),
      !hasData ? jsx('text', { x: (plot.left + plot.right) / 2, y: (plot.top + plot.bottom) / 2, textAnchor: 'middle', fill: 'var(--ui-text-tertiary)', fontSize: 11, children: '데이터 없음' }) : null,
      ...series.flatMap(item => segmentsFor(item).map((points, index) => points.length > 1 ? jsx('polyline', { points: points.join(' '), fill: 'none', stroke: item.stroke, strokeWidth: 2, strokeDasharray: item.dash || undefined, vectorEffect: 'non-scaling-stroke' }, `${item.field}-${index}`) : jsx('circle', { cx: Number(points[0].split(',')[0]), cy: Number(points[0].split(',')[1]), r: 2, fill: item.stroke }, `${item.field}-${index}`))),
      hoverX != null ? jsx('line', { x1: hoverX, y1: plot.top, x2: hoverX, y2: plot.bottom, stroke: 'var(--ui-text-primary)', strokeWidth: 1, strokeDasharray: '2 3', pointerEvents: 'none' }) : null,
      jsx('rect', { x: plot.left, y: plot.top, width: plot.right - plot.left, height: plot.bottom - plot.top, fill: 'transparent' })
    ] }),
    jsx('p', { className: 'min-h-4 truncate text-[10px] tabular-nums text-(--ui-text-tertiary)', role: 'status', children: hoverDetails })
  ] })
}

function ChartLegend({ items }) {
  return jsxs('div', { className: 'flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-(--ui-text-tertiary)', children: items.map(item => jsxs('span', { className: 'inline-flex items-center gap-1.5', children: [
    jsx('svg', { width: 18, height: 6, viewBox: '0 0 18 6', 'aria-hidden': true, children: jsx('line', { x1: 0, y1: 3, x2: 18, y2: 3, stroke: item.stroke, strokeWidth: 2, strokeDasharray: item.dash || undefined }) }),
    item.label
  ] }, item.label)) })
}

const MAIN_COLOR = 'var(--ui-accent)'
const PROMPT_COLOR = 'var(--ui-accent)'
const GENERATION_COLOR = 'var(--ui-cyan)'
const METRIC_WINDOWS = [
  { seconds: 5, label: '5초' },
  { seconds: 15, label: '15초' },
  { seconds: 30, label: '30초' },
  { seconds: 60, label: '1분' },
  { seconds: 300, label: '5분' },
  { seconds: 900, label: '15분' },
  { seconds: 1800, label: '30분' },
  { seconds: 3600, label: '1시간' },
  { seconds: 10800, label: '3시간' },
  { seconds: 21600, label: '6시간' },
  { seconds: 43200, label: '12시간' },
  { seconds: 86400, label: '24시간' },
  { seconds: 172800, label: '2일' },
  { seconds: 604800, label: '7일' },
  { seconds: 1209600, label: '14일' }
]
const TOKEN_SERIES = [
  { field: 'mainPrompt', label: 'Main · Prompt (왼쪽 축)', stroke: PROMPT_COLOR, axis: 'left' },
  { field: 'mainGeneration', label: 'Main · Generation (오른쪽 축)', stroke: GENERATION_COLOR, axis: 'right', dash: '6 4' }
]
const CONTEXT_SERIES = [
  { field: 'mainContextPercent', label: 'Main context', stroke: MAIN_COLOR, axis: 'left' }
]
function contextSummary(metric) {
  const tokens = metric?.context_tokens == null ? null : Number(metric.context_tokens)
  const limit = Number(metric?.context_limit) || 0
  const percent = tokens != null && limit ? Math.min(100, tokens / limit * 100) : null
  return tokens != null && limit ? `${tokens.toLocaleString()} / ${limit.toLocaleString()} (${percent.toFixed(1)}%)` : tokens != null ? tokens.toLocaleString() : '—'
}

function RoleMetricsSummary({ roles, serverRunning }) {
  const rows = [
    { key: 'main', label: 'Main', metric: roles?.main, color: MAIN_COLOR }
  ]
  return jsx('section', { className: 'overflow-x-auto border-t border-(--ui-stroke-secondary) px-3 py-3', 'aria-label': 'Main 모델 지표', children: jsxs('table', { className: 'w-full min-w-[38rem] table-fixed text-[11px]', children: [
    jsx('thead', { children: jsxs('tr', { className: 'text-left text-(--ui-text-tertiary)', children: [
      jsx('th', { className: 'w-[34%] pb-2 font-medium', scope: 'col', children: 'Role / model' }),
      jsx('th', { className: 'pb-2 text-right font-medium', scope: 'col', children: 'Prompt' }),
      jsx('th', { className: 'pb-2 text-right font-medium', scope: 'col', children: 'Generation' }),
      jsx('th', { className: 'pb-2 text-right font-medium', scope: 'col', children: 'Tokens/min' }),
      jsx('th', { className: 'pb-2 text-right font-medium', scope: 'col', children: 'Context' })
    ] }) }),
    jsx('tbody', { children: rows.map(({ key, label, metric, color }) => jsxs('tr', { className: 'border-t border-(--ui-stroke-secondary)', children: [
      jsx('th', { className: 'py-2 pr-3 text-left font-normal', scope: 'row', children: jsxs('div', { className: 'flex min-w-0 items-center gap-2', children: [
        jsx('span', { className: 'h-2 w-2 shrink-0 rounded-full', style: { backgroundColor: color }, 'aria-hidden': true }),
        jsxs('div', { className: 'min-w-0', children: [
          jsxs('p', { className: 'flex items-center gap-2 text-(--ui-text-primary)', children: [label, metric?.active && serverRunning ? jsx('span', { className: 'rounded border border-(--ui-accent) px-1 py-0.5 text-[8px] font-medium text-(--ui-accent)', children: 'ACTIVE' }) : null] }),
          jsx('p', { className: 'truncate font-mono text-[10px] text-(--ui-text-tertiary)', title: metric?.model_id || '', children: metric?.model_id || '모델 미지정' })
        ] })
      ] }) }),
      jsx('td', { className: 'py-2 text-right tabular-nums text-(--ui-text-secondary)', children: formatRate(metric?.prompt_tokens_per_second) }),
      jsx('td', { className: 'py-2 text-right tabular-nums text-(--ui-text-secondary)', children: formatRate(metric?.generation_tokens_per_second) }),
      jsx('td', { className: 'py-2 text-right tabular-nums text-(--ui-text-secondary)', children: Number(metric?.tokens_per_minute || 0).toLocaleString() }),
      jsx('td', { className: 'py-2 text-right tabular-nums text-(--ui-text-secondary)', children: contextSummary(metric) })
    ] }, key)) })
  ] }) })
}

function CompressionCallsSummary({ calls, canPrevious, canNext, onPrevious, onNext }) {
  const rows = Array.isArray(calls) ? calls : []
  const duration = value => value == null ? '—' : `${(Number(value) / 1000).toFixed(2)}초`
  return jsxs('section', { className: 'border-t border-(--ui-stroke-secondary) px-3 py-3', 'aria-label': '최근 Aux compression 호출', children: [
    jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
      jsx('h3', { className: 'text-xs font-medium text-(--ui-text-primary)', children: '최근 Aux compression 호출' }),
      jsxs('div', { className: 'flex items-center gap-1', children: [
        jsx('button', { className: 'rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-xs text-(--ui-text-secondary) disabled:opacity-40', type: 'button', disabled: !canPrevious, onClick: onPrevious, 'aria-label': '이전 Aux 호출', children: '←' }),
        jsx('button', { className: 'rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-xs text-(--ui-text-secondary) disabled:opacity-40', type: 'button', disabled: !canNext, onClick: onNext, 'aria-label': '다음 Aux 호출', children: '→' })
      ] })
    ] }),
    jsx('p', { className: 'mt-1 text-[10px] text-(--ui-text-tertiary)', children: '호출별 입력 context 크기와 전체 compression 소요 시간을 표시합니다.' }),
    rows.length ? jsx('div', { className: 'mt-2 overflow-x-auto', children: jsxs('table', { className: 'w-full min-w-[32rem] text-[11px]', children: [
      jsx('thead', { children: jsxs('tr', { className: 'text-left text-(--ui-text-tertiary)', children: [
        jsx('th', { className: 'pb-2 font-medium', scope: 'col', children: '시각' }),
        jsx('th', { className: 'pb-2 font-medium', scope: 'col', children: 'Profile / session' }),
        jsx('th', { className: 'pb-2 text-right font-medium', scope: 'col', children: '입력 context' }),
        jsx('th', { className: 'pb-2 text-right font-medium', scope: 'col', children: '소요 시간' })
      ] }) }),
      jsx('tbody', { children: rows.map(call => jsxs('tr', { className: 'border-t border-(--ui-stroke-secondary)', children: [
        jsx('td', { className: 'whitespace-nowrap py-2 tabular-nums text-(--ui-text-secondary)', children: formatTimestamp(call.timestamp) }),
        jsxs('td', { className: 'max-w-48 whitespace-nowrap py-2 font-mono text-[10px] text-(--ui-text-secondary)', title: `${call.profile || ''}/${call.session || ''}`, children: [
          `${call.profile || '—'} / `,
          jsx('span', { className: 'select-text cursor-text', style: { userSelect: 'text', WebkitUserSelect: 'text' }, tabIndex: 0, 'aria-label': `Session ${call.session || '없음'}`, children: call.session || '—' })
        ] }),
        jsx('td', { className: 'py-2 text-right tabular-nums text-(--ui-text-secondary)', children: call.input_context_tokens == null ? '—' : `${Number(call.input_context_tokens).toLocaleString()} tok` }),
        jsx('td', { className: 'py-2 text-right tabular-nums text-(--ui-text-secondary)', children: duration(call.duration_ms) })
      ] }, call.request_id || `${call.profile}-${call.completed_at}`)) })
    ] }) }) : jsx('p', { className: 'mt-2 text-[11px] text-(--ui-text-tertiary)', role: 'status', children: '아직 Aux compression 호출 기록이 없습니다.' })
  ] })
}

function ResourceMetricsPanel({ status, pushConnected }) {
  const [showDetails, setShowDetails] = useState(false)
  const [windowSeconds, setWindowSeconds] = useState(1800)
  const [compressionCursors, setCompressionCursors] = useState([])
  const compressionCursor = compressionCursors[compressionCursors.length - 1] || ''
  const serverStopped = status?.server_running === false
  const metricsQuery = useQuery({
    queryKey: [ID, 'resource-metrics', showDetails ? windowSeconds : 60, compressionCursor],
    queryFn: () => api(`/metrics?window=${showDetails ? windowSeconds : 60}${compressionCursor ? `&compression_cursor=${encodeURIComponent(compressionCursor)}` : ''}`),
    enabled: Boolean(status),
    refetchInterval: pushConnected ? false : status?.server_running ? 10000 : false,
    refetchOnWindowFocus: false,
    placeholderData: previousData => previousData,
    staleTime: 30000
  })
  const snapshot = metricsQuery.data
  const metricHistory = snapshot?.series || []
  const roles = snapshot?.roles || {}
  const compressionCalls = snapshot?.compression_calls || []
  const nextCompressionCursor = snapshot?.compression_calls_next_cursor || ''
  const selectedWindowLabel = METRIC_WINDOWS.find(item => item.seconds === windowSeconds)?.label || formatWindowOffset(windowSeconds).replace(' 전', '')
  const latestAt = Number(snapshot?.sampled_at) || Date.now() / 1000
  return jsxs('section', { className: 'mt-3 overflow-hidden rounded-md border border-(--ui-stroke-secondary)', 'aria-label': 'llama.cpp 실시간 리소스', children: [
    jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2 bg-(--ui-bg-tertiary) px-3 py-2', children: [
      jsxs('div', { children: [
        jsx('h2', { className: 'text-xs font-medium text-(--ui-text-primary)', children: '리소스 통계' }),
        serverStopped ? jsx('p', { className: 'mt-0.5 text-[10px] text-(--ui-text-tertiary)', children: 'Server stopped · 마지막 수집값 유지' }) : null
      ] }),
      jsx('button', { className: 'rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary) px-2.5 py-1 text-[11px] text-(--ui-text-secondary) hover:bg-(--ui-bg-secondary) hover:text-(--ui-text-primary)', type: 'button', onClick: () => setShowDetails(current => !current), 'aria-expanded': showDetails, 'aria-controls': 'llamacpp-resource-details', children: showDetails ? '간단히 보기' : '상세 보기' })
    ] }),
    !serverStopped && metricsQuery.error ? jsx('p', { className: 'px-3 py-3 text-xs text-(--dt-destructive)', role: 'alert', children: String(metricsQuery.error.message || metricsQuery.error) }) : null,
    jsx(RoleMetricsSummary, { roles, serverRunning: !serverStopped }),
    jsx(CompressionCallsSummary, {
      calls: compressionCalls,
      canPrevious: compressionCursors.length > 0,
      canNext: Boolean(nextCompressionCursor),
      onPrevious: () => setCompressionCursors(current => current.slice(0, -1)),
      onNext: () => { if (nextCompressionCursor) setCompressionCursors(current => [...current, nextCompressionCursor]) }
    }),
    showDetails ? jsxs('div', { id: 'llamacpp-resource-details', className: 'border-t border-(--ui-stroke-secondary)', children: [
      jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2 bg-(--ui-bg-tertiary) px-3 py-2', children: [
        jsxs('div', { children: [
          jsx('h3', { className: 'text-xs font-medium text-(--ui-text-primary)', children: `${selectedWindowLabel} 추이` }),
          jsx('p', { className: 'mt-0.5 text-[10px] text-(--ui-text-tertiary)', children: '2초 간격 수집 · 최대 360개 점으로 자동 축약' })
        ] }),
        jsx('select', { className: compactInput, style: { ...themedSelect(), width: '7rem', maxWidth: '7rem', flex: '0 0 7rem' }, value: String(windowSeconds), 'aria-label': '리소스 그래프 시간 범위', onChange: event => setWindowSeconds(Number(event.target.value)), children: METRIC_WINDOWS.map(item => jsx('option', { style: themedOption, value: String(item.seconds), children: item.label }, item.seconds)) })
      ] }),
      !serverStopped && !metricsQuery.error && !metricHistory.length ? jsx('p', { className: 'px-3 py-4 text-xs text-(--ui-text-secondary)', role: 'status', children: 'worker가 실행되면 처리량과 context 추이를 수집합니다.' }) : null,
      jsxs('section', { className: 'border-t border-(--ui-stroke-secondary) px-3 py-3', 'aria-label': 'Main token 처리량 그래프', children: [
        jsx('h3', { className: 'text-xs font-medium text-(--ui-text-primary)', children: 'Token 처리량 · Main timeline' }),
        jsx('p', { className: 'mt-1 text-[10px] text-(--ui-text-tertiary)', children: '실선은 Prompt, 점선은 Generation입니다. 요청이 발생한 측정점들을 하나의 선으로 연결하며 두 처리량은 서로 다른 Y축을 사용합니다.' }),
        jsx(MetricChart, { samples: metricHistory, series: TOKEN_SERIES, label: 'Main prompt와 generation token 처리량', leftAxis: { label: 'Prompt tok/s', unit: 'tok/s' }, rightAxis: { label: 'Generation tok/s', unit: 'tok/s' }, windowSeconds, latestAt }),
        jsx(ChartLegend, { items: TOKEN_SERIES })
      ] }),
      jsxs('section', { className: 'border-t border-(--ui-stroke-secondary) px-3 py-3', 'aria-label': 'Main context 사용량 그래프', children: [
        jsx('h3', { className: 'text-xs font-medium text-(--ui-text-primary)', children: 'Context 사용량 · Main timeline' }),
        jsx(MetricChart, { samples: metricHistory, series: CONTEXT_SERIES, label: 'Main context 사용률', leftAxis: { label: 'Context 사용률', unit: '%', maximum: 100 }, windowSeconds, latestAt }),
        jsx(ChartLegend, { items: CONTEXT_SERIES })
      ] })
    ] }) : null
  ] })
}

function ServerLogPanel({ status, jobs, pushConnected }) {
  const [open, setOpen] = useState(true)
  const [manualRefreshPending, setManualRefreshPending] = useState(false)
  const serverJob = (jobs || []).find(job => job.kind === 'server-start')
  const serverStopped = status?.server_running === false && serverJob?.status !== 'running'
  const logQuery = useQuery({
    queryKey: [ID, 'activity-log'],
    queryFn: () => api('/logs?limit=250'),
    enabled: open,
    refetchInterval: query => pushConnected ? false : status?.server_running || serverJob?.status === 'running' ? 2500 : false,
    refetchOnWindowFocus: false,
    placeholderData: previousData => previousData
  })
  useEffect(() => {
    if (open && (status?.server_running === true || serverStopped)) logQuery.refetch()
  }, [open, status?.server_running, serverStopped, serverJob?.job_id, serverJob?.status])
  const lines = logQuery.data?.lines || []
  const logRef = useRef(null)
  const refreshLogs = async () => {
    setManualRefreshPending(true)
    try { await logQuery.refetch() } finally { setManualRefreshPending(false) }
  }
  // push event, fallback fetch, 수동 갱신으로 내용이 바뀌면 최신 로그를 하단에 유지
  useLayoutEffect(() => {
    const el = logRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [lines, logQuery.error, open])
  return jsxs('section', { className: 'relative mt-3 overflow-hidden rounded-md border border(--ui-stroke-secondary)', 'aria-label': 'llama.cpp 로그', children: [
    jsx('div', { className: 'flex items-center justify-between bg(--ui-bg-tertiary) px-3 py-2', children: [
      serverStopped ? jsx('span', { className: 'text-[10px] text-(--ui-text-tertiary)', role: 'status', children: 'Server stopped · 기존 로그 유지' }) : jsx('span', {}),
      jsxs('div', { className: 'flex items-center gap-2', children: [open ? jsx('button', { className: 'text-xs text(--ui-text-secondary) hover:text(--ui-text-primary)', onClick: refreshLogs, disabled: manualRefreshPending, children: manualRefreshPending ? '갱신 중…' : '새로고침' }) : null, jsx('button', { className: 'text-xs text(--ui-accent) hover:underline', onClick: () => setOpen(current => !current), 'aria-expanded': open, children: open ? '접기' : '보기' })] })
    ] }),
    open ? logQuery.error ? jsx('p', { className: 'px-3 py-3 text-xs text(--dt-destructive)', role: 'alert', children: String(logQuery.error.message || logQuery.error) }) : jsx('pre', { ref: logRef, className: 'max-h-64 select-text cursor-text overflow-auto whitespace-pre-wrap break-all bg(--ui-bg-primary) px-3 py-2 font-mono text-[11px] leading-4 text(--ui-text-secondary)', style: { userSelect: 'text', WebkitUserSelect: 'text' }, tabIndex: 0, role: 'log', 'aria-label': 'llama.cpp 로그', 'aria-live': 'polite', children: lines.length ? lines.join(LOG_NEWLINE) : '로그가 아직 없습니다.' }) : null
  ] })
}

function UnexpectedExitPanel({ diagnostic }) {
  if (!diagnostic) return null
  const lines = [
    `source: ${diagnostic.source || 'unknown'}`,
    `pid: ${diagnostic.pid ?? 'unknown'}`,
    `return code: ${diagnostic.returncode ?? 'unknown'}${diagnostic.returncode_hex ? ` (${diagnostic.returncode_hex})` : ''}`,
    `model: ${diagnostic.model_id || 'unknown'}`,
    `port: ${diagnostic.port ?? 'unknown'}`,
    '',
    ...(diagnostic.log_tail?.lines || [])
  ]
  return jsxs('section', { className: 'mt-3 overflow-hidden rounded-md border border-(--dt-destructive)/50 bg-(--dt-destructive)/10', role: 'alert', 'aria-labelledby': 'llama-server-exit-title', children: [
    jsx('h2', { id: 'llama-server-exit-title', className: 'px-3 py-2 text-xs font-medium text-(--dt-destructive)', children: '최근 예기치 않은 llama-server 종료' }),
    jsx('pre', { className: 'max-h-52 select-text cursor-text overflow-auto whitespace-pre-wrap break-all border-t border-(--dt-destructive)/30 px-3 py-2 font-mono text-[11px] leading-4 text-(--ui-text-secondary)', style: { userSelect: 'text', WebkitUserSelect: 'text' }, tabIndex: 0, children: lines.join(LOG_NEWLINE) })
  ] })
}

function ExecutionProfileRow({ role, label, auxRole, status, modelId, onModelChange, saving }) {
  const profile = status?.profiles?.[role] || {}
  const models = (role === 'main' ? status?.main_model_options : status?.auxiliary_model_options) || []
  const runtimeKind = modelId.startsWith('Ternary-Bonsai') ? 'prism_ml' : 'official'
  const serverRunning = Boolean(status?.server_running)
  const running = Boolean(serverRunning && status?.execution?.active_role === role && status?.active_model_id === modelId)
  return jsxs('div', { className: 'flex flex-wrap items-center gap-2 border-b border-(--ui-stroke-secondary) py-2 last:border-0', children: [
    jsxs('div', { className: 'w-20 shrink-0', children: [jsx('p', { className: 'text-xs font-medium text-(--ui-text-primary)', children: label }), jsx('p', { className: 'mt-0.5 font-mono text-[10px] text-(--ui-text-tertiary)', children: profile.logical_model || `${role}-local` })] }),
    auxRole ? jsx('select', { className: compactInput, style: { ...themedSelect(), width: '7rem' }, value: 'compress', 'aria-label': 'Auxiliary 역할', onChange: () => {}, children: jsx('option', { style: themedOption, value: 'compress', children: 'compress' }) }) : null,
    jsx('div', { className: 'flex h-8 w-24 shrink-0 items-center rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) px-2 font-mono text-xs text-(--ui-text-secondary)', 'aria-label': `${label} runtime`, children: runtimeKind === 'prism_ml' ? 'Prism-ML' : 'official' }),
    jsx('select', { className: compactInput, style: { ...themedSelect(), minWidth: '12rem', flex: '1 1 14rem' }, value: modelId, disabled: serverRunning || saving, 'aria-label': `${label} model`, onChange: event => onModelChange(event.target.value), children: [jsx('option', { style: themedOption, value: '', children: '사용 안 함' }), ...models.map(model => jsx('option', { style: themedOption, value: model.id, children: model.label }, model.id))] }),
    jsx(Badge, { tone: running ? 'good' : modelId ? 'neutral' : 'warn', children: running ? 'running' : modelId ? role === 'compression' ? 'on demand' : 'selected' : 'not set' })
  ] })
}

function ExecutionProfilesPanel({ status, onRefresh }) {
  const mainProfile = status?.profiles?.main || {}
  const compressionProfile = status?.profiles?.compression || {}
  const [mainModelId, setMainModelId] = useState(mainProfile.model_id || '')
  const [compressionModelId, setCompressionModelId] = useState(compressionProfile.model_id || '')
  const [starting, setStarting] = useState(false)
  const [savingRole, setSavingRole] = useState('')
  const [message, setMessage] = useState('')
  const running = Boolean(status?.server_running)
  useEffect(() => { setMainModelId(mainProfile.model_id || '') }, [mainProfile.model_id])
  useEffect(() => { setCompressionModelId(compressionProfile.model_id || '') }, [compressionProfile.model_id])
  const saveProfile = async (role, value, previous, setter) => {
    setter(value); setSavingRole(role); setMessage('')
    try {
      await api(`/profiles/${role}`, { method: 'PUT', body: { model_id: value } })
      await onRefresh()
      setMessage(`${role === 'main' ? 'Main' : 'Auxiliary'} 구성을 저장했습니다.`)
    } catch (cause) {
      setter(previous)
      setMessage(`모델 구성 저장 실패: ${cause?.message || String(cause)}`)
    } finally { setSavingRole('') }
  }
  const start = async () => {
    setStarting(true); setMessage('')
    try {
      const result = await api('/profiles/start', { method: 'POST', body: { main_model_id: mainModelId, compression_model_id: compressionModelId } })
      await onRefresh()
      setMessage(`${result.startup_role === 'main' ? 'Main' : 'Auxiliary'} 서버 시작 요청됨`)
    } catch (cause) { setMessage(`서버 시작 실패: ${cause?.message || String(cause)}`) } finally { setStarting(false) }
  }
  const stop = async () => {
    setStarting(true); setMessage('')
    try {
      await api('/server', { method: 'POST', body: { action: 'stop' } })
      queryClient.setQueryData([ID, 'status'], previous => previous ? { ...previous, server_running: false, coordinator: { ...(previous.coordinator || {}), ok: false, stopped: true, pid: null, desktop_clients: 0 } } : previous)
      setMessage('서버와 Singleton proxy를 중지했습니다.')
    }
    catch (cause) { setMessage(`서버 중지 실패: ${cause?.message || String(cause)}`) } finally { setStarting(false) }
  }
  return jsxs('section', { className: 'mt-4 rounded-md border border-(--ui-stroke-secondary) p-3', 'aria-labelledby': 'llamacpp-profiles-title', children: [
    jsxs('div', { className: 'flex flex-wrap items-start justify-between gap-2', children: [jsx('div', { children: [jsx('h2', { id: 'llamacpp-profiles-title', className: 'text-sm font-medium text-(--ui-text-primary)', children: 'Main / Auxiliary 모델' }), jsx('p', { className: 'mt-1 text-xs text-(--ui-text-tertiary)', children: '각 역할은 선택하지 않아도 됩니다. Main이 선택되어 있으면 Main을 우선 로드합니다. Auxiliary만 선택하면 Auxiliary로 시작합니다.' })] }), jsx(Badge, { children: 'single worker' })] }),
    jsxs('div', { className: 'mt-3', children: [
      jsx(ExecutionProfileRow, { role: 'main', label: 'Main', status, modelId: mainModelId, saving: Boolean(savingRole), onModelChange: value => saveProfile('main', value, mainModelId, setMainModelId) }),
      jsx(ExecutionProfileRow, { role: 'compression', label: 'Auxiliary', auxRole: true, status, modelId: compressionModelId, saving: Boolean(savingRole), onModelChange: value => saveProfile('compression', value, compressionModelId, setCompressionModelId) })
    ] }),
    jsxs('div', { className: 'mt-3 flex flex-wrap items-center gap-2', children: [running ? jsx('button', { className: compactDanger, disabled: starting, onClick: stop, children: '서버 중지' }) : jsx('button', { className: compactPrimary, disabled: starting || (!mainModelId && !compressionModelId), onClick: start, children: starting ? '시작 중…' : '서버 시작' }), message ? jsx('span', { className: `text-[11px] ${message.includes('실패') ? 'text-(--dt-destructive)' : 'text-(--ui-text-tertiary)'}`, role: 'status', children: message }) : null] })
  ] })
}

function CoordinatorStatusPanel({ status }) {
  const coordinator = status?.coordinator || {}
  const proxyState = coordinator.ok ? 'running' : coordinator.stopped ? 'stopped' : 'unavailable'
  const identity = coordinator.pid ? `PID ${coordinator.pid} · :${coordinator.port}` : `:${coordinator.port || 18380}`
  const transition = `${status?.execution?.active_role || '-'} · ${status?.execution?.transition_phase || 'IDLE'}`
  return jsxs('section', { className: 'mt-4 flex flex-wrap items-center gap-2 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) px-3 py-2 text-xs', 'aria-labelledby': 'llamacpp-coordinator-title', children: [
    jsx('h2', { id: 'llamacpp-coordinator-title', className: 'mr-1 text-sm font-medium text-(--ui-text-primary)', children: 'Singleton proxy' }),
    jsx(Badge, { tone: coordinator.ok ? 'good' : 'warn', children: proxyState }),
    jsx('span', { className: 'font-mono text-(--ui-text-secondary)', children: identity }),
    jsx('span', { className: 'text-(--ui-text-tertiary)', 'aria-hidden': true, children: '·' }),
    jsx('span', { className: 'text-(--ui-text-secondary)', children: status?.execution_mode === 'exclusive_swap' ? 'Exclusive swap' : status?.execution_mode || 'Exclusive swap' }),
    jsx('span', { className: 'font-mono text-(--ui-text-secondary)', children: transition }),
    jsx('span', { className: 'text-(--ui-text-secondary)', children: coordinator.cancellation_propagation ? 'cancel propagation on' : 'cancel propagation unknown' }),
    jsx('span', { className: 'ml-auto text-(--ui-text-tertiary)', children: `${coordinator.desktop_clients ?? 0} client${Number(coordinator.desktop_clients) === 1 ? '' : 's'}` })
  ] })
}

function RuntimeUpdateControl({ kind, label, option, disabled, onInstall }) {
  const installed = Boolean(option?.installed)
  const actionable = Boolean(option) && (!installed || Boolean(option?.update_available))
  const version = option?.version || '미설치'
  const latest = option?.latest_version
  const actionLabel = installed ? '업데이트' : '설치'
  const title = actionable
    ? `${label} runtime ${actionLabel}${latest ? ` · 최신 ${latest}` : ''}`
    : `${label} runtime 최신 상태${latest ? ` · ${latest}` : ''}`
  return jsxs('button', {
    type: 'button',
    className: `inline-flex items-center gap-1 rounded px-1.5 py-1 font-mono text-xs transition disabled:cursor-not-allowed disabled:opacity-50 ${actionable ? 'text-(--ui-text-primary) hover:bg-(--ui-bg-tertiary)' : 'text-(--ui-text-tertiary)'}`,
    disabled: disabled || !actionable,
    title,
    'aria-label': `${label}(${version}) ${actionable ? actionLabel + ' 가능' : '최신 상태'}`,
    onClick: () => onInstall(kind),
    children: [jsx('span', { children: `${label}(${version})` }), jsx('span', { className: actionable ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)', 'aria-hidden': true, children: '↻' })]
  })
}

function RuntimeCard({ status, jobs, onRefresh, pushConnected }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const runtimeJob = (jobs || []).find(job => ['runtime-install', 'prism-runtime-install'].includes(job.kind) && job.status === 'running') || (jobs || []).find(job => ['runtime-install', 'prism-runtime-install'].includes(job.kind) && ['error', 'done'].includes(job.status))
  const serverJob = (jobs || []).find(job => job.kind === 'server-start')
  const runtimeBusy = busy || runtimeJob?.status === 'running'
  const serverBusy = serverJob?.status === 'running'

  useEffect(() => { if (runtimeJob?.status === 'done') { setMessage('runtime 준비가 완료되었습니다.'); onRefresh() } }, [runtimeJob?.job_id, runtimeJob?.status])
  const install = async kind => {
    const label = kind === 'prism_ml' ? 'Prism-ML' : 'Official'
    setBusy(true); setMessage('')
    try {
      await api('/runtime/install', { method: 'POST', body: { kind } })
      await onRefresh()
      setMessage(`${label} runtime 준비 작업을 시작했습니다.`)
    } catch (cause) { setMessage(`${label} runtime 준비 실패: ${cause?.message || String(cause)}`) } finally { setBusy(false) }
  }

  return jsxs('section', { className: `${card} p-4 sm:p-5`, children: [
    jsxs('div', { className: 'flex flex-wrap items-start justify-between gap-4', children: [
      jsx('div', { children: [jsx('h1', { className: 'text-xl font-semibold tracking-tight', children: 'llama.cpp Manager' }), jsx('p', { className: `mt-1 ${muted}`, children: 'runtime, 모델, parameter를 분리해 관리합니다.' })] }),
      jsxs('div', { className: 'flex flex-wrap items-center justify-end gap-1', 'aria-label': 'runtime 버전 및 업데이트', children: [
        jsx(RuntimeUpdateControl, { kind: 'official', label: 'Official', option: status?.runtime_options?.official, disabled: runtimeBusy || serverBusy, onInstall: install }),
        jsx('span', { className: 'text-(--ui-text-tertiary)', 'aria-hidden': true, children: '|' }),
        jsx(RuntimeUpdateControl, { kind: 'prism_ml', label: 'Prism-ML', option: status?.runtime_options?.prism_ml, disabled: runtimeBusy || serverBusy, onInstall: install })
      ] })
    ] }),
    jsx(ExecutionProfilesPanel, { status, onRefresh }),
    jsx(ServerLogPanel, { status, jobs, pushConnected }),
    jsx(ResourceMetricsPanel, { status, pushConnected }),
    message ? jsx('p', { className: `mt-3 text-xs ${message.includes('실패') ? 'text-(--dt-destructive)' : muted}`, role: 'status', children: message }) : null,
    jsx(JobProgress, { job: runtimeJob }), jsx(JobProgress, { job: serverJob }), jsx(UnexpectedExitPanel, { diagnostic: status?.last_unexpected_exit }), jsx(CoordinatorStatusPanel, { status })
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
  const [selectedPresetId, setSelectedPresetId] = useState('')
  const [presetMessage, setPresetMessage] = useState('')
  const isActive = status?.active_model_id === model.id
  const display = model.id || 'unknown model'
  const parameterQuery = useQuery({ queryKey: [ID, 'model-settings', model.id], queryFn: () => api(`/settings/${encodeURIComponent(model.id)}`), refetchOnWindowFocus: false })
  const presetsQuery = useQuery({ queryKey: [ID, 'presets'], queryFn: () => api('/presets'), staleTime: 0, refetchOnMount: 'always', refetchOnWindowFocus: true })
  useEffect(() => { const applied = status?.model_presets?.[model.id] || ''; setSelectedPresetId(applied) }, [status?.model_presets?.[model.id], model.id])
  const activeState = status?.server_running && isActive ? 'running' : 'stopped'
  const compactSize = String(model.size_label || 'unknown').replace(/\s+/g, '')
  const unregister = async () => { setBusy(true); try { await api(`/models/${encodeURIComponent(model.id)}/registration`, { method: 'DELETE' }); await refresh() } finally { setBusy(false) } }
  const applyPreset = async () => {
    if (!selectedPresetId) return
    setBusy(true); setPresetMessage('')
    try {
      const result = await api(`/presets/${encodeURIComponent(selectedPresetId)}/apply`, { method: 'POST', body: { model_id: model.id } })
      await parameterQuery.refetch()
      const omitted = result.omitted_options?.length ? ` · 제외: ${result.omitted_options.join(', ')}` : ''
      setPresetMessage(`preset으로 모델 설정을 교체했습니다. 다음 server 시작에 반영됩니다${omitted}`)
      refresh()
    } catch (cause) { setPresetMessage(`preset 적용 실패: ${cause?.message || String(cause)}`) } finally { setBusy(false) }
  }
  return jsxs('article', { className: 'border-b border-(--ui-stroke-secondary) py-5 last:border-0', 'aria-label': `${display} 등록 모델`, children: [
    jsxs('div', { className: 'flex flex-wrap items-center gap-x-3 gap-y-2', children: [
      jsx('h3', { className: 'min-w-0 break-words font-medium', title: display, children: display }),
      jsx(PrismOnlyHint, { model: display }),
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', 'aria-hidden': true, children: '|' }),
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: `크기(${compactSize})` }),
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', 'aria-hidden': true, children: '|' }),
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: `상태(${activeState})` }),
      jsx('button', { className: 'ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-lg text-(--ui-text-secondary) transition hover:bg-(--dt-destructive)/10 hover:text-(--dt-destructive) disabled:cursor-not-allowed disabled:opacity-50', disabled: busy || (status?.server_running && isActive), onClick: unregister, title: '모델 등록에서 제거', 'aria-label': `${display} 모델 등록 해제`, children: '×' })
    ] }),
    jsx(ParameterSummary, { options: parameterQuery.data?.options, order: parameterQuery.data?.order }),
    jsxs('div', { className: 'mt-3 flex flex-wrap items-center gap-2 rounded-md bg-(--ui-bg-tertiary) p-2', 'data-testid': 'model-card-presets', children: [
      jsx('label', { className: 'shrink-0 text-xs font-medium text-(--ui-text-primary)', htmlFor: `model-preset-${model.id}`, children: 'parameter preset' }),
      jsx('select', { id: `model-preset-${model.id}`, className: 'min-w-44 flex-1 rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs text-(--ui-text-primary)', style: themedSelect(), value: selectedPresetId, onChange: event => setSelectedPresetId(event.target.value), 'aria-label': `${display} preset 선택`, children: [jsx('option', { style: themedOption, value: '', children: presetsQuery.isLoading ? 'preset 불러오는 중…' : 'preset 선택' }), ...(presetsQuery.data?.presets || []).map(preset => jsx('option', { style: themedOption, value: preset.id, children: preset.name }, preset.id))] }),
      jsx('button', { className: compactPrimary, disabled: busy || !selectedPresetId, onClick: applyPreset, children: '적용' }),
      presetMessage ? jsx('span', { className: `basis-full text-xs ${presetMessage.includes('실패') ? 'text-(--dt-destructive)' : 'text-(--ui-text-secondary)'}`, role: 'status', children: presetMessage }) : null
    ] })
  ] })
}

function ParameterControl({ id, option, value, onChange, onBlur, onKeyDown, error, describedBy, ariaLabel, className = input, allowEmpty = false }) {
  if (option && !option.requires_value) return jsx('span', { className: `min-w-0 flex h-8 flex-1 items-center px-2 text-xs ${muted}`, role: 'status', children: 'flag' })
  const common = { id, value, 'aria-label': ariaLabel, 'aria-invalid': Boolean(error), 'aria-describedby': describedBy, onChange, onBlur, onKeyDown }
  if (option?.choices?.length) {
    return jsx('select', { ...common, className, style: themedSelect(), children: [allowEmpty ? jsx('option', { style: themedOption, value: '', children: '값을 선택하세요' }) : null, ...option.choices.map(choice => jsx('option', { style: themedOption, value: choice, children: choice }, choice))] })
  }
  const type = option?.value_kind === 'integer' || option?.value_kind === 'number' ? 'number' : 'text'
  const step = option?.value_kind === 'integer' ? '1' : option?.value_kind === 'number' ? 'any' : undefined
  return jsx('input', { ...common, className, type, step, inputMode: type === 'number' ? 'decimal' : undefined })
}

function ModelSettingsEditor({ modelId, appliedPresetId = '', refresh, open }) {
  const [query, setQuery] = useState('')
  const [selectedKey, setSelectedKey] = useState('')
  const [value, setValue] = useState('')
  const [draft, setDraft] = useState({})
  const [saving, setSaving] = useState(false)
  const [fieldErrors, setFieldErrors] = useState({})
  const [message, setMessage] = useState('')
  const [presetName, setPresetName] = useState('')
  const [presetRename, setPresetRename] = useState('')
  const [selectedPresetId, setSelectedPresetId] = useState('')
  const settings = useQuery({ queryKey: [ID, 'model-settings', modelId], queryFn: () => api(`/settings/${encodeURIComponent(modelId)}`), enabled: open, refetchOnWindowFocus: false })
  const options = useQuery({ queryKey: [ID, 'model-settings-options', query], queryFn: () => api(`/settings?q=${encodeURIComponent(query)}&limit=20`), enabled: open && query.trim().length > 0, refetchOnWindowFocus: false })
  const presetsQuery = useQuery({ queryKey: [ID, 'presets'], queryFn: () => api('/presets'), enabled: open, staleTime: 0, refetchOnMount: 'always', refetchOnWindowFocus: true })
  const selectedPreset = (presetsQuery.data?.presets || []).find(preset => preset.id === selectedPresetId)
  const availableOptions = (options.data?.options || []).filter(option => !Object.prototype.hasOwnProperty.call(draft, option.key))
  const selected = availableOptions.find(option => option.key === selectedKey)
  useEffect(() => {
    setSelectedPresetId(appliedPresetId)
    setDraft({})
    setFieldErrors({})
    setSelectedKey('')
    setValue('')
  }, [modelId, open, appliedPresetId])
  useEffect(() => {
    const next = selectedPreset ? { ...(selectedPreset.options || {}) } : {}
    setDraft(next)
    setFieldErrors({})
    setSelectedKey('')
    setValue('')
  }, [selectedPresetId, selectedPreset?.updated_at])
  useEffect(() => { setPresetRename(selectedPreset?.name || '') }, [selectedPresetId, selectedPreset?.name])
  const validateDraft = (next, extraMetadata = {}) => {
    const metadata = { ...(settings.data?.metadata || {}), ...extraMetadata }
    return Object.fromEntries(Object.entries(next).map(([key, current]) => [key, optionValueError(metadata[key] || { key, requires_value: true }, current)]).filter(([, error]) => error))
  }
  const persistPreset = async next => {
    if (!selectedPresetId) { setMessage('먼저 preset을 선택하세요.'); return false }
    const errors = validateDraft(next)
    if (Object.keys(errors).length) { setFieldErrors(errors); setMessage('입력값을 확인하세요.'); return false }
    setSaving(true)
    setMessage('')
    try {
      await api(`/presets/${encodeURIComponent(selectedPresetId)}`, { method: 'PATCH', body: { options: next } })
      await presetsQuery.refetch()
      setDraft(next)
      setFieldErrors({})
      setMessage('preset parameter를 저장했습니다.')
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
    if (await persistPreset(next)) { setDraft(next); setSelectedKey(''); setValue('') }
  }
  const remove = async key => {
    const next = { ...draft }
    delete next[key]
    if (await persistPreset(next)) { setDraft(next); setFieldErrors(previous => { const copy = { ...previous }; delete copy[key]; return copy }) }
  }
  const update = (key, nextValue) => { setDraft(previous => ({ ...previous, [key]: nextValue })); setFieldErrors(previous => { const copy = { ...previous }; delete copy[key]; return copy }) }
  const commit = (key, nextValue) => persistPreset({ ...draft, [key]: nextValue })
  const savePreset = async () => {
    if (!presetName.trim()) { setMessage('preset 이름을 입력하세요.'); return }
    try {
      const result = await api('/presets', { method: 'POST', body: { name: presetName.trim(), options: draft } })
      await Promise.all([presetsQuery.refetch(), queryClient.invalidateQueries({ queryKey: [ID, 'presets'] })])
      setSelectedPresetId(result.preset.id)
      setPresetName('')
      setMessage('현재 parameter를 preset으로 저장했습니다.')
    } catch (cause) { setMessage(`preset 저장 실패: ${cause?.message || String(cause)}`) }
  }
  const applyPreset = async () => {
    if (!selectedPresetId) return
    try {
      const result = await api(`/presets/${encodeURIComponent(selectedPresetId)}/apply`, { method: 'POST', body: { model_id: modelId } })
      setDraft({ ...(selectedPreset?.options || {}) })
      await presetsQuery.refetch()
      await settings.refetch()
      const omitted = result.omitted_options?.length ? ` Prism-ML에서 관리하지 않는 ${result.omitted_options.join(', ')}은 제외했습니다.` : ''
      setMessage((result.requires_restart ? 'preset을 현재 모델 설정에 저장했습니다. 현재 server는 유지되며 다음 start에 반영됩니다.' : 'preset을 현재 모델 설정에 저장했습니다.') + omitted)
      refresh()
    } catch (cause) { setMessage(`preset 적용 실패: ${cause?.message || String(cause)}`) }
  }
  const renamePreset = async () => {
    if (!selectedPresetId || !presetRename.trim()) { setMessage('변경할 preset 이름을 입력하세요.'); return }
    try {
      await api(`/presets/${encodeURIComponent(selectedPresetId)}`, { method: 'PATCH', body: { name: presetRename.trim() } })
      await Promise.all([presetsQuery.refetch(), queryClient.invalidateQueries({ queryKey: [ID, 'presets'] })])
      setMessage('preset 이름을 변경했습니다.')
    } catch (cause) { setMessage(`preset 이름 변경 실패: ${cause?.message || String(cause)}`) }
  }
  const deletePreset = async () => {
    if (!selectedPresetId) return
    try {
      await api(`/presets/${encodeURIComponent(selectedPresetId)}`, { method: 'DELETE' })
      await Promise.all([
        presetsQuery.refetch(),
        queryClient.invalidateQueries({ queryKey: [ID, 'presets'] }),
        queryClient.invalidateQueries({ queryKey: [ID, 'status'] })
      ])
      setSelectedPresetId('')
      setDraft({})
      setMessage('preset을 삭제했습니다.')
    } catch (cause) { setMessage(`preset 삭제 실패: ${cause?.message || String(cause)}`) }
  }
  return open ? jsxs('div', { className: 'mt-3 rounded-md border border-(--ui-stroke-secondary) p-3 sm:p-4', children: [
      jsx('p', { className: `mb-2 text-xs ${muted}`, children: 'preset을 선택하면 해당 preset의 parameter만 표시됩니다. preset 선택을 해제하면 parameter가 비워집니다.' }),
      jsxs('div', { className: 'mb-4 rounded-md bg-(--ui-bg-tertiary) p-3', children: [
        jsx('p', { className: 'mb-1 text-xs font-medium text-(--ui-text-primary)', children: 'parameter preset' }),
        jsx('p', { className: `mb-2 text-xs ${muted}`, children: '저장은 선택한 preset 값으로 현재 모델 설정을 교체합니다.' }),
        jsxs('div', { className: 'flex items-stretch gap-2', 'data-testid': 'preset-actions', children: [
          jsx('select', { className: `${compactInput} min-w-0 flex-1`, style: { ...themedSelect(), width: 'auto', minWidth: 0, flex: '1 1 auto' }, value: selectedPresetId, 'aria-label': `${modelId} parameter preset 선택`, onChange: event => setSelectedPresetId(event.target.value), children: [jsx('option', { style: themedOption, value: '', children: presetsQuery.isLoading ? 'preset 불러오는 중…' : 'preset 선택' }), ...(presetsQuery.data?.presets || []).map(preset => jsx('option', { style: themedOption, value: preset.id, children: preset.name }, preset.id))] }),
          jsx('button', { className: `${compactPrimary} shrink-0`, disabled: !selectedPresetId || saving, onClick: applyPreset, children: '저장' }),
          jsx('button', { className: `${compactDanger} shrink-0`, disabled: !selectedPresetId || saving, onClick: deletePreset, children: '삭제' })
        ] }),
        jsxs('div', { className: 'mt-2 flex items-stretch gap-2', 'data-testid': 'preset-name-actions', children: [
          selectedPreset ? jsx('input', { className: `${compactInput} min-w-0 flex-1`, value: presetRename, 'aria-label': `${selectedPreset.name} preset 이름`, onChange: event => setPresetRename(event.target.value), onKeyDown: event => { if (event.key === 'Enter') renamePreset() } }) : null,
          selectedPreset ? jsx('button', { className: `${compactPrimary} shrink-0`, disabled: saving || !presetRename.trim(), onClick: renamePreset, children: '이름 변경' }) : null,
          jsx('input', { className: `${compactInput} min-w-0 flex-1`, value: presetName, 'aria-label': `${modelId} 새 preset 이름`, placeholder: '새 preset 이름', onChange: event => setPresetName(event.target.value), onKeyDown: event => { if (event.key === 'Enter') savePreset() } }),
          jsx('button', { className: `${compactPrimary} shrink-0`, disabled: saving || !presetName.trim(), onClick: savePreset, children: '새 preset 만들기' })
        ] })
      ] }),
      settings.isLoading ? jsx('p', { className: muted, role: 'status', children: '현재 설정을 불러오는 중…' }) : null,
      jsx('div', { className: 'grid gap-x-4 gap-y-1 sm:grid-cols-2', children: Object.entries(draft).map(([key, current]) => jsxs('div', { className: 'flex min-w-0 items-start gap-2 border-b border-(--ui-stroke-secondary) py-1.5', children: [jsx('label', { className: 'w-24 shrink-0 truncate pt-2 text-xs font-medium text-(--ui-text-primary)', htmlFor: `parameter-${modelId}-${key}`, children: key }), jsxs('div', { className: 'min-w-0 flex-1', children: [jsxs('div', { className: 'flex min-w-0 items-center gap-1.5', children: [jsx(ParameterControl, { id: `parameter-${modelId}-${key}`, option: settings.data?.metadata?.[key], value: current, className: compactInput, error: fieldErrors[key], describedBy: fieldErrors[key] ? `parameter-error-${modelId}-${key}` : undefined, ariaLabel: `${key} 값`, onChange: event => update(key, event.target.value), onBlur: event => commit(key, event.target.value) }), jsx('button', { className: compactDanger, disabled: saving, onClick: () => remove(key), 'aria-label': `${key} parameter 삭제`, children: '삭제' })] }), fieldErrors[key] ? jsx('p', { id: `parameter-error-${modelId}-${key}`, className: 'mt-1 text-[11px] text-(--dt-destructive)', role: 'alert', children: fieldErrors[key] }) : null] })] }, key)) }),
      jsx('div', { className: 'mt-4 border-t border-(--ui-stroke-secondary) pt-3', children: jsx('p', { className: 'mb-2 text-xs font-medium text-(--ui-text-primary)', children: 'parameter 추가' }) }),
      jsx('input', { className: compactInput, type: 'search', value: query, 'aria-label': '추가할 llama-server parameter 검색', placeholder: 'parameter 검색 (예: ctx, flash, batch)', onChange: event => setQuery(event.target.value) }),
      availableOptions.length ? jsx('div', { className: 'mt-2 max-h-40 overflow-y-auto rounded-md border border-(--ui-stroke-secondary)', role: 'listbox', 'aria-label': 'llama-server parameter 검색 결과', children: availableOptions.map(option => jsx('button', { className: `flex w-full items-start justify-between gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2 text-left text-sm last:border-0 hover:bg-(--ui-bg-tertiary) ${selectedKey === option.key ? 'bg-(--ui-bg-tertiary)' : ''}`, type: 'button', role: 'option', 'aria-selected': selectedKey === option.key, onClick: () => { setSelectedKey(option.key); setValue(Object.prototype.hasOwnProperty.call(draft, option.key) ? String(draft[option.key]) : option.default_value || '') }, children: [jsx('code', { children: option.name }), jsx('span', { className: `shrink-0 max-w-[55%] truncate text-right text-xs ${muted}`, title: option.choices?.length ? option.choices.join(' | ') : undefined, children: option.requires_value ? option.choices?.length ? option.choices.join(' | ') : option.default_value ? `기본값 ${option.default_value}` : option.key : 'flag' })] }, option.key)) }) : query.trim() && !options.isLoading ? jsx('p', { className: `mt-2 text-xs ${muted}`, role: 'status', children: '일치하는 parameter가 없습니다.' }) : null,
      selected ? jsxs('div', { className: 'mt-2 flex gap-2', children: [selected.requires_value ? jsx(ParameterControl, { id: `new-parameter-${selected.key}`, option: selected, value, className: compactInput, allowEmpty: true, ariaLabel: `${selected.key} 값`, onChange: event => setValue(event.target.value), onKeyDown: event => { if (event.key === 'Enter') add() } }) : jsx('span', { className: `flex flex-1 items-center px-2 text-xs ${muted}`, role: 'status', children: '값이 없는 flag parameter' }), jsx('button', { className: compactPrimary, disabled: saving || (selected.requires_value && !value.trim()), onClick: add, 'aria-label': `${selected.key} parameter 추가 또는 수정`, children: '추가/수정' })] }) : null,
      message ? jsx('p', { className: `mt-3 text-xs ${message.startsWith('반영 실패') || message === '입력값을 확인하세요.' ? 'text-(--dt-destructive)' : muted}`, role: 'status', children: message }) : null
    ] }) : null
}

function RegisterWizard({ close, refresh, initialRepo = '', mode = 'download' }) {
  const [queryText, setQueryText] = useState('')
  const [searched, setSearched] = useState(false)
  const [repo, setRepo] = useState(initialRepo)
  const [fileIndex, setFileIndex] = useState('')
  const [alias, setAlias] = useState('')
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
    try {
      const result = await api('/register', { method: 'POST', body: { repo, paths: selected.paths, alias } })
      setActionMessage(`${result.model_id} 모델을 등록했습니다.`)
      if (result.suggested_alias) setAlias(result.suggested_alias)
      await refresh()
    } catch (cause) {
      setActionMessage(`등록 실패: ${cause?.message || String(cause)}`)
    }
  }
  const running = job.data?.status === 'running'
  useEffect(() => { setRepo(initialRepo); setFileIndex(''); setAlias(''); setActionMessage('') }, [initialRepo])
  useEffect(() => { if (mode === 'register' && fileIndex === '' && groups.length) setFileIndex('0') }, [mode, fileIndex, groups.length])
  useEffect(() => { if (mode === 'register' && cachedFiles.data?.suggested_alias) setAlias(cachedFiles.data.suggested_alias) }, [mode, repo, cachedFiles.data?.suggested_alias])
  return jsxs('section', { className: `${card} mt-4 p-4 sm:p-5`, 'aria-labelledby': 'llama-wizard-title', children: [
    jsxs('div', { className: 'flex items-start justify-between gap-4', children: [jsx('div', { className: 'min-w-0', children: [jsx('div', { className: 'text-[11px] font-semibold uppercase tracking-[0.16em] text-(--ui-accent)', children: mode === 'register' ? '다운받은 모델 등록' : '모델 다운로드' }), jsx('h2', { id: 'llama-wizard-title', className: 'mt-1 text-lg font-semibold', children: mode === 'register' ? '다운받은 GGUF 파일 등록' : 'Hugging Face 저장소와 양자화 선택' })] }), jsx('button', { className: button, onClick: close, 'aria-label': '모델 wizard 닫기', children: '닫기' })] }),
    mode === 'download' ? jsxs('div', { className: 'mt-5', children: [
      jsx('input', { className: input, type: 'search', value: queryText, 'aria-label': 'Hugging Face 모델 검색', placeholder: '예: Qwen GGUF, unsloth/Qwen…', onChange: event => setQueryText(event.target.value), onKeyDown: event => { if (event.key === 'Enter') setSearched(true) } }),
      jsxs('div', { className: 'mt-3 grid grid-cols-2 gap-3', 'data-testid': 'download-actions', children: [
        jsx('button', { className: primary, onClick: () => setSearched(true), 'aria-label': 'Hugging Face 검색 실행', children: 'HF 검색' }),
        jsx('button', { className: primary, disabled: !selected || running, onClick: download, 'aria-label': '선택한 GGUF 다운로드', children: '선택 항목 다운로드' })
      ] })
    ] }) : null,
    mode === 'download' && search.isLoading ? jsx('p', { className: `mt-3 ${muted}`, children: 'Hugging Face 검색 중…' }) : null,
    mode === 'download' && search.error ? jsx('p', { className: 'mt-3 text-sm text-(--dt-destructive)', children: String(search.error.message || search.error) }) : null,
    mode === 'download' && search.data?.hits?.length ? jsx('div', { className: 'mt-3 rounded-md border border-(--ui-stroke-secondary) overscroll-contain', style: { maxHeight: '24rem', overflowY: 'auto', scrollbarColor: 'var(--ui-stroke-secondary) transparent' }, children: search.data.hits.map(hit => jsx('button', { className: 'flex w-full items-center justify-between border-b border-(--ui-stroke-secondary) px-3 py-2 text-left text-sm last:border-0 hover:bg-(--ui-bg-tertiary)', onClick: () => { setRepo(hit.repo); setFileIndex(''); setActionMessage('') }, children: [jsx('span', { className: 'min-w-0 break-words pr-3 font-mono', children: hit.repo }), jsx('span', { className: `shrink-0 ${muted}`, children: `${Number(hit.downloads || 0).toLocaleString()} downloads` })] }, hit.repo))}) : null,
    mode === 'register' && repo ? jsxs('div', { className: 'mt-5', children: [jsx('div', { className: 'mb-2 flex items-center justify-between gap-3', children: [jsx('span', { className: 'min-w-0 break-words text-sm font-medium', children: repo }), jsx('span', { className: `shrink-0 text-xs ${muted}`, children: '다운로드 완료 파일' })] }), cachedFiles.isLoading ? jsx('p', { className: muted, role: 'status', children: '다운받은 파일을 확인하는 중…' }) : cachedFiles.error ? jsx('p', { className: 'text-sm text-(--dt-destructive)', role: 'alert', children: String(cachedFiles.error.message || cachedFiles.error) }) : cachedFiles.data?.warning ? jsx('p', { className: 'text-sm text-(--dt-destructive)', role: 'alert', children: cachedFiles.data.warning }) : groups.length ? jsxs('div', { className: 'flex items-stretch gap-3', 'data-testid': 'registration-actions', children: [jsx('input', { className: input, style: { width: '16rem', maxWidth: '16rem', flex: '0 0 16rem' }, value: alias, 'aria-label': '등록 이름(alias)', placeholder: '모델 이름(alias)', onChange: event => setAlias(event.target.value) }), jsx('select', { className: `${input} min-w-0 flex-1`, style: { ...themedSelect(), width: 'auto', minWidth: 0, flex: '1 1 auto' }, value: fileIndex, 'aria-label': '등록할 GGUF 파일 선택', onChange: event => setFileIndex(event.target.value), children: groups.map((group, index) => jsx('option', { style: themedOption, value: index, children: group.paths.join(', ') }, `${group.label}-${index}`)) }), jsx('button', { className: `${primary} shrink-0`, disabled: !selected || !alias.trim() || running, onClick: registerModel, 'aria-label': '선택한 GGUF 등록', children: '선택' })] }) : jsx('p', { className: muted, children: '다운로드가 완료된 GGUF 파일이 없습니다.' })] }) : null,
    mode === 'download' && repo ? jsxs('div', { className: 'mt-5', children: [jsx('div', { className: 'mb-2 flex items-center justify-between gap-3', children: [jsx('span', { className: 'min-w-0 break-words text-sm font-medium', children: repo }), jsx('span', { className: `shrink-0 text-xs ${muted}`, children: 'Q4 / Q5 / IQ / F16' })] }), files.isLoading ? jsx('p', { className: muted, role: 'status', children: 'repo의 GGUF 양자화 목록을 불러오는 중…' }) : jsx('select', { className: input, style: themedSelect(), value: fileIndex, 'aria-label': '다운로드할 양자화 선택', onChange: event => setFileIndex(event.target.value), children: [jsx('option', { style: themedOption, value: '', children: '양자화를 선택하세요' }), ...groups.map((group, index) => jsx('option', { style: themedOption, value: index, children: `${group.label} · ${(Number(group.total_bytes || 0) / (1 << 30)).toFixed(1)} GB · ${group.fit}` }, `${group.label}-${index}`))] }), selected ? jsx('p', { className: `mt-2 ${muted}`, children: `${selected.paths.length > 1 ? `${selected.paths.length}개 split part 다운로드 · ` : ''}${selected.fit}` }) : null] }) : null,
    jsx(JobProgress, { job: job.data }),
    actionMessage ? jsx('p', { className: `mt-3 text-xs ${actionMessage.startsWith('등록 실패:') ? 'text-(--dt-destructive)' : muted}`, role: 'status', children: actionMessage }) : null
  ] })
}

function DownloadedModelRow({ model, onRegister, onDelete }) {
  return jsxs('article', { className: 'grid gap-3 border-b border-(--ui-stroke-secondary) py-4 last:border-0 sm:grid-cols-[minmax(0,1fr)_auto]', 'aria-label': `${model.repo_id} 다운로드 모델`, children: [
    jsx('div', { className: 'min-w-0', children: jsxs('div', { className: 'flex items-center gap-2', children: [jsx('h3', { className: 'break-words font-medium', title: model.repo_id, children: model.repo_id }), jsx(PrismOnlyHint, { model: model.repo_id })] }) }),
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
  const pushConnected = useCoordinatorPush()
  const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => api('/status'), refetchInterval: query => query.state.data?.coordinator?.stopped ? false : query.state.data?.coordinator?.ok === false ? false : pushConnected ? false : query.state.data?.server_running ? 1500 : 3000, refetchOnWindowFocus: query => query.state.data?.coordinator?.ok !== false })
  const jobs = useQuery({ queryKey: [ID, 'jobs'], queryFn: () => api('/jobs'), refetchInterval: query => query.state.data?.jobs?.some(job => job.status === 'running') ? 1000 : false, refetchOnWindowFocus: false })
  const hfModels = useQuery({ queryKey: [ID, 'hf-models'], queryFn: () => api('/hf-models'), enabled: Boolean(status.data), refetchOnWindowFocus: false })
  const data = status.data; const models = data?.models || []; const localModels = hfModels.data?.models || []; const runtimeJobs = jobs.data?.jobs || []
  useEffect(() => { if (runtimeJobs.some(job => ['runtime-install', 'prism-runtime-install'].includes(job.kind) && job.status === 'done')) queryClient.invalidateQueries({ queryKey: [ID, 'status'] }) }, [runtimeJobs])
  const refreshStatus = () => Promise.all([queryClient.invalidateQueries({ queryKey: [ID, 'status'] }), queryClient.invalidateQueries({ queryKey: [ID, 'jobs'] })])
  const refreshInventory = () => queryClient.invalidateQueries({ queryKey: [ID, 'hf-models'] })
  const refreshWizard = () => Promise.all([refreshStatus(), refreshInventory()])
  const openDownload = () => setWizard({ mode: 'download', repo: '' })
  const openRegister = repo => setWizard({ mode: 'register', repo })
  const deleteDownloaded = async repoId => { if (!window.confirm(`'${repoId}' HF cache를 삭제할까요?`)) return; try { await api('/hf-models/delete', { method: 'POST', body: { repo_id: repoId } }); await hfModels.refetch() } catch (cause) { window.alert(`모델 삭제 실패: ${cause?.message || String(cause)}`) } }
  const activeModel = models.find(model => model.id === data?.active_model_id)
  return jsxs('main', { className: 'mx-auto max-w-5xl p-4 sm:p-6', children: [jsx(TabBar, { active: tab, setActive: setTab }), tab === 'runtime' ? jsx(RuntimeCard, { status: data, jobs: runtimeJobs, onRefresh: refreshStatus, pushConnected }) : null,
    tab === 'models' ? jsxs('div', { children: [jsxs('div', { className: 'flex items-end justify-between gap-4', children: [jsx('div', { children: [jsx('h2', { className: 'text-lg font-semibold', children: '모델' }), jsx('p', { className: `mt-1 ${muted}`, children: '등록 모델과 다운받은 HF cache 모델을 runtime 종류와 관계없이 모두 표시합니다.' })] }), jsx('button', { className: primary, onClick: openDownload, children: '+ 모델 다운로드' })] }), wizard ? jsx(RegisterWizard, { close: () => setWizard(null), refresh: refreshWizard, initialRepo: wizard.repo, mode: wizard.mode }) : null, jsx('section', { className: `${card} mt-4 px-5`, children: status.isLoading ? jsx('p', { className: `py-6 ${muted}`, children: '상태를 불러오는 중…' }) : models.length ? models.map(model => jsx(ModelRow, { model, status: data, refresh: refreshStatus }, model.id)) : jsx('p', { className: `py-8 text-center ${muted}`, children: '등록된 모델이 없습니다.' }) }), jsxs('section', { className: `${card} mt-5 px-5`, children: [jsxs('div', { className: 'flex items-center justify-between gap-3 py-4', children: [jsx('h2', { className: 'text-lg font-semibold', children: '다운받은 모델' }), jsx('button', { className: button, disabled: hfModels.isFetching, onClick: () => hfModels.refetch(), children: '새로고침' })] }), hfModels.isLoading ? jsx('p', { className: `pb-5 ${muted}`, children: 'inventory를 불러오는 중…' }) : localModels.length ? localModels.map(model => jsx(DownloadedModelRow, { model, onRegister: openRegister, onDelete: deleteDownloaded }, model.repo_id)) : jsx('p', { className: `pb-5 ${muted}`, children: '다운받은 모델이 없습니다.' })] })] }) : null,
    tab === 'parameters' ? jsxs('section', { className: `${card} p-4 sm:p-5`, children: [jsx('h2', { className: 'text-lg font-semibold', children: '파라미터와 preset' }), jsx('p', { className: `mt-1 ${muted}`, children: '모델을 선택한 뒤 parameter를 검색·추가하고 현재 설정을 preset으로 저장합니다.' }), activeModel ? jsx(ModelSettingsEditor, { modelId: activeModel.id, appliedPresetId: data?.model_presets?.[activeModel.id] || '', refresh: refreshStatus, open: true }) : jsx('p', { className: `mt-6 rounded-md bg-(--ui-bg-tertiary) p-4 ${muted}`, children: '먼저 모델 탭에서 사용할 모델을 선택하세요.' })] }) : null
  ] })
}
export default {
  id: ID,
  name: 'llama.cpp Manager',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    desktopLeaseDispose?.()
    desktopLeaseDispose = startDesktopLease(ctx)
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/llamacpp' }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/llamacpp', label: 'llama.cpp', codicon: 'server-process' } }
    ])
  }
}
