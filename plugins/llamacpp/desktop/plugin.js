import React from 'react'
import pluginSdk from '@hermes/plugin-sdk'

const { createElement, useEffect, useMemo, useState } = React
const { ROUTES_AREA, SIDEBAR_NAV_AREA, useQuery, queryClient } = pluginSdk
const jsx = (type, props, key) => createElement(type, key === undefined ? props : { ...props, key })
const jsxs = jsx

const ID = 'llamacpp'
let pluginCtx
const api = (path, options) => pluginCtx.rest(path, options)
const panel = 'rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-4'
const button = 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-sm transition hover:bg-(--ui-bg-tertiary) disabled:cursor-not-allowed disabled:opacity-50'
const primary = 'rounded-md bg-(--ui-accent) px-3 py-2 text-sm text-(--ui-accent-foreground) transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50'
const danger = 'rounded-md border border-(--dt-destructive)/50 px-3 py-2 text-sm text-(--dt-destructive) transition hover:bg-(--dt-destructive)/10 disabled:cursor-not-allowed disabled:opacity-50'
const select = 'min-w-0 flex-1 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-3 py-2 text-sm text-(--ui-text-primary) outline-none focus:border-(--ui-accent)'
const muted = 'text-sm text-(--ui-text-secondary)'

function bytes(value) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? `${(number / (1024 ** 3)).toFixed(1)} GB` : '확인되지 않음'
}

function Badge({ good, children }) {
  const tone = good ? 'bg-(--ui-accent)/15 text-(--ui-accent)' : 'bg-(--ui-bg-tertiary) text-(--ui-text-secondary)'
  return jsx('span', { className: `rounded-full px-2 py-1 text-xs ${tone}`, children })
}

function RoleRow({ role, label, profile, options, onRefresh }) {
  const [modelId, setModelId] = useState(profile?.model_id || '')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => setModelId(profile?.model_id || ''), [profile?.model_id])
  const save = async () => {
    if (!modelId) return
    setSaving(true); setMessage('')
    try {
      await api(`/core/profiles/${role}`, { method: 'PUT', body: { model_id: modelId } })
      await onRefresh()
      setMessage('Core assignment에 저장됨')
    } catch (cause) {
      setMessage(`저장 실패: ${cause?.message || String(cause)}`)
    } finally { setSaving(false) }
  }
  return jsxs('div', { className: 'grid gap-2 border-b border-(--ui-stroke-secondary) py-3 last:border-0 md:grid-cols-[8rem_minmax(0,1fr)_auto]', children: [
    jsxs('div', { children: [jsx('p', { className: 'text-sm font-medium', children: label }), jsx('p', { className: 'mt-1 text-xs text-(--ui-text-tertiary)', children: profile?.provider || '미지정' })] }),
    jsx('select', { className: select, value: modelId, 'aria-label': `${label} 모델`, onChange: event => setModelId(event.target.value), children: [jsx('option', { value: '', children: '모델 선택' }), ...options.map(option => jsx('option', { value: option.id, children: option.label }, option.id))] }),
    jsxs('div', { className: 'flex items-center gap-2', children: [jsx('button', { className: primary, disabled: saving || !modelId, onClick: save, children: saving ? '저장 중…' : '저장' }), message ? jsx('span', { className: `text-xs ${message.startsWith('저장 실패') ? 'text-(--dt-destructive)' : 'text-(--ui-text-tertiary)'}`, role: 'status', children: message }) : null] })
  ] })
}

function CoreControls({ status, onRefresh }) {
  const [selected, setSelected] = useState(status?.active_model_id || '')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const options = status?.model_options || []
  useEffect(() => setSelected(status?.active_model_id || ''), [status?.active_model_id])
  const act = async (path, body, success) => {
    setBusy(true); setMessage('')
    try {
      const result = await api(path, { method: 'POST', body })
      setMessage(result?.job_id ? `${success} · job ${result.job_id}` : success)
      await onRefresh()
    } catch (cause) {
      setMessage(`작업 실패: ${cause?.message || String(cause)}`)
    } finally { setBusy(false) }
  }
  return jsxs('section', { className: panel, 'aria-labelledby': 'core-controls-title', children: [
    jsx('h2', { id: 'core-controls-title', className: 'text-base font-semibold', children: 'Core lifecycle 제어' }),
    jsx('p', { className: `mt-1 ${muted}`, children: '모델 registry와 llama-server lifecycle은 Hermes Core가 소유합니다.' }),
    jsxs('div', { className: 'mt-4 flex flex-wrap gap-2', children: [
      jsx('button', { className: primary, disabled: busy || status?.server_running, onClick: () => act('/core/server', { action: 'start' }, 'Core server 시작 요청됨'), children: 'Server 시작' }),
      jsx('button', { className: button, disabled: busy || !status?.server_running, onClick: () => act('/core/server', { action: 'stop' }, 'Core server 중지 요청됨'), children: 'Server 중지' })
    ] }),
    jsxs('div', { className: 'mt-4 grid gap-2 md:grid-cols-[minmax(0,1fr)_auto_auto]', children: [
      jsx('select', { className: select, value: selected, 'aria-label': '제어할 Core 모델', onChange: event => setSelected(event.target.value), children: [jsx('option', { value: '', children: '모델 선택' }), ...options.map(option => jsx('option', { value: option.id, children: option.label }, option.id))] }),
      jsx('button', { className: primary, disabled: busy || !selected, onClick: () => act('/core/activate', { model_id: selected }, 'Core activate 요청됨'), children: 'Activate' }),
      jsx('button', { className: danger, disabled: busy || !selected, onClick: () => act('/core/eject', { model_id: selected }, 'Core eject 요청됨'), children: 'Eject' })
    ] }),
    message ? jsx('p', { className: `mt-3 text-xs ${message.startsWith('작업 실패') ? 'text-(--dt-destructive)' : 'text-(--ui-text-secondary)'}`, role: 'status', children: message }) : null
  ] })
}

function StatusPanel({ status }) {
  const hardware = status?.hardware || {}
  const loaded = status?.loaded_models && typeof status.loaded_models === 'object' ? Object.keys(status.loaded_models) : []
  const cells = [
    ['정책', status?.exclusive_residency ? 'exclusive autoload' : 'non-exclusive autoload'],
    ['models_max', String(status?.models_max ?? 'unknown')],
    ['활성 모델', status?.active_model_id || '없음'],
    ['Resident', loaded.length ? loaded.join(', ') : '없음'],
    ['Runtime', status?.runtime_backend || (status?.runtime_installed ? 'installed' : 'not installed')],
    ['GPU', hardware.gpu_name || hardware.device_name || '확인되지 않음'],
    ['VRAM', bytes(hardware.vram_total_bytes)],
    ['등록 모델', String(status?.model_options?.length || 0)]
  ]
  return jsxs('section', { className: panel, 'aria-labelledby': 'core-status-title', children: [
    jsxs('div', { className: 'flex flex-wrap items-start justify-between gap-3', children: [
      jsxs('div', { children: [jsx('h2', { id: 'core-status-title', className: 'text-base font-semibold', children: 'Core Local Models 상태' }), jsx('p', { className: `mt-1 ${muted}`, children: 'Plugin은 상태를 투영하며 별도 coordinator나 두 번째 llama-server를 시작하지 않습니다.' })] }),
      jsx(Badge, { good: status?.server_running, children: status?.server_running ? 'server running' : 'server stopped' })
    ] }),
    jsx('dl', { className: 'mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4', children: cells.map(([label, value]) => jsxs('div', { children: [jsx('dt', { className: 'text-(--ui-text-tertiary)', children: label }), jsx('dd', { className: 'mt-1 break-all font-mono', children: value })] }, label)) })
  ] })
}

function JobsPanel() {
  const jobs = useQuery({ queryKey: [ID, 'core-jobs'], queryFn: () => api('/core/jobs'), refetchInterval: query => query.state.data?.jobs?.some(job => job.status === 'running') ? 1000 : false, refetchOnWindowFocus: false })
  const rows = useMemo(() => jobs.data?.jobs || [], [jobs.data])
  return jsxs('section', { className: panel, 'aria-labelledby': 'core-jobs-title', children: [
    jsxs('div', { className: 'flex items-center justify-between gap-3', children: [jsx('h2', { id: 'core-jobs-title', className: 'text-base font-semibold', children: 'Core 작업' }), jsx('button', { className: button, disabled: jobs.isFetching, onClick: () => jobs.refetch(), children: jobs.isFetching ? '갱신 중…' : '새로고침' })] }),
    jobs.error ? jsx('p', { className: 'mt-3 text-sm text-(--dt-destructive)', role: 'alert', children: String(jobs.error.message || jobs.error) }) : rows.length ? jsx('ul', { className: 'mt-3 divide-y divide-(--ui-stroke-secondary)', children: rows.slice(0, 8).map(job => jsxs('li', { className: 'flex flex-wrap items-center justify-between gap-2 py-2 text-sm', children: [jsx('span', { className: 'font-mono', children: job.kind || job.job_id || 'job' }), jsx(Badge, { good: job.status === 'done', children: job.status || 'unknown' })] }, job.job_id || `${job.kind}-${job.created_at}`)) }) : jsx('p', { className: `mt-3 ${muted}`, children: '표시할 Core 작업이 없습니다.' })
  ] })
}

function Page() {
  const status = useQuery({ queryKey: [ID, 'core-status'], queryFn: () => api('/core/status'), refetchOnWindowFocus: false })
  const refresh = () => Promise.all([queryClient.invalidateQueries({ queryKey: [ID, 'core-status'] }), queryClient.invalidateQueries({ queryKey: [ID, 'core-jobs'] })])
  const data = status.data
  return jsxs('main', { className: 'mx-auto max-w-5xl p-4 sm:p-6', children: [
    jsxs('header', { className: 'mb-5 flex flex-wrap items-start justify-between gap-3', children: [
      jsxs('div', { children: [jsx('h1', { className: 'text-xl font-semibold tracking-tight', children: 'Core Local Models' }), jsx('p', { className: `mt-1 ${muted}`, children: 'Main/Compression 역할과 Core-native 단일 모델 residency를 관리합니다.' })] }),
      jsx('button', { className: button, disabled: status.isFetching, onClick: () => status.refetch(), children: status.isFetching ? '갱신 중…' : '상태 새로고침' })
    ] }),
    status.isLoading ? jsx('div', { className: panel, role: 'status', children: 'Core 상태를 불러오는 중…' }) : status.error ? jsx('div', { className: `${panel} text-(--dt-destructive)`, role: 'alert', children: `Core API 연결 실패: ${status.error.message || String(status.error)}` }) : jsxs('div', { className: 'grid gap-4', children: [
      jsx(StatusPanel, { status: data }),
      data?.exclusive_residency ? null : jsx('div', { className: 'rounded-lg border border-(--dt-destructive)/50 bg-(--dt-destructive)/10 p-4 text-sm text-(--dt-destructive)', role: 'alert', children: `Core local_runtime.models_max가 ${data?.models_max ?? 'unknown'}입니다. 16GB VRAM exclusive residency에는 1이 필요합니다.` }),
      jsxs('section', { className: panel, 'aria-labelledby': 'roles-title', children: [
        jsx('h2', { id: 'roles-title', className: 'text-base font-semibold', children: '역할 assignment' }),
        jsx('p', { className: `mt-1 ${muted}`, children: data?.exclusive_residency ? 'Main과 Compression은 Core provider assignment와 models_max=1 정책을 사용합니다.' : 'Main과 Compression은 Core provider assignment를 사용하지만 현재 residency cap은 exclusive가 아닙니다.' }),
        jsxs('div', { className: 'mt-3', children: [
          jsx(RoleRow, { role: 'main', label: 'Main', profile: data?.profiles?.main, options: data?.model_options || [], onRefresh: refresh }),
          jsx(RoleRow, { role: 'compression', label: 'Compression', profile: data?.profiles?.compression, options: data?.model_options || [], onRefresh: refresh })
        ] })
      ] }),
      jsx(CoreControls, { status: data, onRefresh: refresh }),
      jsx(JobsPanel, {})
    ] })
  ] })
}

export default {
  id: ID,
  name: 'llamacpp',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/llamacpp' }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/llamacpp', label: 'llamacpp', codicon: 'server-process' } }
    ])
  }
}