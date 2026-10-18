import { ROUTES_AREA, SIDEBAR_NAV_AREA, Tip, cn, useQuery } from '@hermes/plugin-sdk'
import { jsx } from 'react/jsx-runtime'

const ID = 'hermes-langchain'
const REFRESH_MS = 10_000
const PAGE_PATH = '/hermes-langchain-logs'

function ControllerStatus({ ctx }) {
  const status = useQuery({
    queryKey: [ID, 'status'],
    queryFn: async () => {
      await ctx.rest('/runtime/lease', { method: 'POST', timeoutMs: 3_000 })
      return ctx.rest('/status', { timeoutMs: 3_000 })
    },
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
    retry: false,
    staleTime: 5_000
  })
  const running = status.isSuccess && status.data?.running === true
  const label = running ? 'running' : 'stopped'

  return jsx(Tip, {
    label: `Hermes LangChain: ${label}`,
    children: jsx('span', {
      className: 'inline-flex h-full items-center px-1.5 text-[0.6875rem] font-medium',
      children: [
        jsx('span', { children: 'LangChain' }),
        jsx('span', {
          className: cn(running ? 'text-(--ui-success)' : 'text-(--ui-danger,#f87171)'),
          children: `(${label})`
        })
      ]
    })
  })
}

function ActivityPage({ ctx }) {
  const activity = useQuery({
    queryKey: [ID, 'activity'],
    queryFn: () => ctx.rest('/activity', { query: { limit: 100 }, timeoutMs: 3_000 }),
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
    retry: false,
    staleTime: 2_000
  })
  const items = Array.isArray(activity.data?.items) ? activity.data.items : []
  const content = activity.isLoading
    ? jsx('p', { className: 'text-sm text-(--ui-text-secondary)', children: '로그를 불러오는 중…' })
    : activity.isError
      ? jsx('p', { className: 'text-sm text-(--ui-danger,#f87171)', children: 'KWC 로그 API를 읽지 못했습니다.' })
      : items.length === 0
        ? jsx('p', { className: 'text-sm text-(--ui-text-secondary)', children: '기록된 활동이 없습니다.' })
        : jsx('div', {
            className: 'overflow-hidden rounded-md border border-(--ui-stroke-secondary)',
            children: items.map((item) => jsx('div', {
              className: 'grid grid-cols-[10rem_8rem_7rem_1fr] gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2 text-xs last:border-b-0',
              children: [
                jsx('time', {
                  className: 'text-(--ui-text-secondary)',
                  children: new Date(Number(item.created_at) * 1000).toLocaleString()
                }),
                jsx('span', { className: 'font-medium', children: item.action }),
                jsx('span', {
                  className: cn(item.outcome === 'accepted' || item.outcome === 'active'
                    ? 'text-(--ui-success)'
                    : item.outcome === 'retry_pending'
                      ? 'text-(--ui-warning)'
                      : 'text-(--ui-text-secondary)'),
                  children: item.outcome
                }),
                jsx('span', {
                  className: 'min-w-0 truncate text-(--ui-text-secondary)',
                  title: [item.board, item.task_id, item.event_kind, item.reason].filter(Boolean).join(' · '),
                  children: [item.board, item.task_id, item.event_kind, item.reason].filter(Boolean).join(' · ')
                })
              ]
            }, item.id))
          })

  return jsx('main', {
    className: 'h-full overflow-auto p-5 text-(--ui-text-primary)',
    children: jsx('section', {
      className: 'mx-auto flex max-w-5xl flex-col gap-4',
      children: [
        jsx('header', {
          children: [
            jsx('h1', { className: 'text-lg font-semibold', children: 'Hermes LangChain activity log' }),
            jsx('p', {
              className: 'mt-1 text-sm text-(--ui-text-secondary)',
              children: '최근 30일의 privacy-safe controller 활동입니다. 댓글 본문, prompt, session key는 저장하지 않습니다.'
            })
          ]
        }),
        content
      ]
    })
  })
}

export default {
  id: ID,
  name: 'hermes-langchain',
  defaultEnabled: true,
  register(ctx) {
    ctx.registerMany([
      {
        id: 'controller-status',
        area: 'statusBar.right',
        order: 140,
        render: () => jsx(ControllerStatus, { ctx })
      },
      { id: 'activity-page', area: ROUTES_AREA, data: { path: PAGE_PATH }, render: () => jsx(ActivityPage, { ctx }) },
      { id: 'activity-nav', area: SIDEBAR_NAV_AREA, data: { path: PAGE_PATH, label: 'LangChain logs', codicon: 'history' } }
    ])
  }
}