import { COMPOSER_AREAS, host } from '@hermes/plugin-sdk'

const ID = 'kanban-control'
const POLL_INTERVAL_MS = 1500
const POLL_TIMEOUT_MS = 120000
const pendingRunIds = new Set()

function afterMetadata(remainder, end) {
  const rest = remainder.slice(end)
  return /^\s/.test(rest) ? rest.slice(1) : rest
}

export function parseKcpCommand(text) {
  const command = String(text || '').match(/^\/kcp(?:(\s)|$)([\s\S]*)$/i)
  if (!command) return { request: '', runId: '' }

  let remainder = command[2] || ''
  let runId = ''
  let legacyRunSeen = false
  let boardSeen = false
  while (remainder) {
    const legacyRun = remainder.match(/^run(?=\s|$)/i)
    if (legacyRun && !legacyRunSeen) {
      legacyRunSeen = true
      remainder = afterMetadata(remainder, legacyRun[0].length)
      continue
    }

    const runIdOption = remainder.match(/^--run-id\s+(\S+)(?=\s|$)/i)
    if (runIdOption && !runId) {
      runId = runIdOption[1]
      remainder = afterMetadata(remainder, runIdOption[0].length)
      continue
    }

    const boardOption = remainder.match(
      /^--(?:new-board|board)\s+(?:"[^"]+"|'[^']+'|\S+)(?=\s|$)/i
    )
    if (boardOption && !boardSeen) {
      boardSeen = true
      remainder = afterMetadata(remainder, boardOption[0].length)
      continue
    }
    break
  }
  return { request: remainder, runId }
}

export function injectRunId(text, runId) {
  const command = String(text || '').match(/^\/kcp(?:(\s)|$)([\s\S]*)$/i)
  if (!command) return text
  const suffix = command[1] ? `${command[1]}${command[2] || ''}` : ''
  return `/kcp --run-id ${runId}${suffix}`
}

export function createRunId() {
  const timestamp = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z').replace(/[-:]/g, '')
  const random = globalThis.crypto?.randomUUID?.().replace(/-/g, '').slice(0, 8)
    || Math.random().toString(16).slice(2, 10).padEnd(8, '0')
  return `kcp-${timestamp}-${random}`
}

export function taskTitle(request, runId) {
  let summary = request.split(/\s+/).filter(Boolean).join(' ')
  if (summary.length > 92) summary = `${summary.slice(0, 89).trimEnd()}...`
  return `KCP run ${runId}: ${summary}`
}

async function profileRoster() {
  const response = await host.request('profiles.list', { include_sessions: true })
  return Array.isArray(response?.profiles) ? response.profiles : []
}

export function workerCandidate(profiles, expectedTitle, previousIds) {
  for (const profile of profiles) {
    const session = profile?.worker_session
    const sessionId = String(session?.id || '')
    if (
      sessionId &&
      !previousIds.has(sessionId) &&
      String(session?.title || '') === expectedTitle
    ) {
      return { profile: String(profile?.name || ''), sessionId }
    }
  }
  return null
}

export async function openWorkerTabWhenReady(expectedTitle, previousIds, runId, runtime) {
  if (pendingRunIds.has(runId)) return false
  pendingRunIds.add(runId)
  const deadline = Date.now() + POLL_TIMEOUT_MS
  try {
    while (!runtime.signal.aborted && Date.now() < deadline) {
      try {
        const candidate = workerCandidate(await runtime.roster(), expectedTitle, previousIds)
        if (candidate) {
          await runtime.openSession(candidate.sessionId, {
            profile: candidate.profile,
            intent: 'tab',
            keepAllProfilesScope: true
          })
          return true
        }
      } catch (error) {
        runtime.warn(`worker-session discovery/open failed for ${runId}: ${error}`)
      }
      if (!await runtime.sleep(POLL_INTERVAL_MS)) return false
    }
    if (!runtime.signal.aborted) runtime.warn(`worker-session tab timed out for ${runId}`)
    return false
  } finally {
    pendingRunIds.delete(runId)
  }
}

async function captureKcpRun(draft, runtime) {
  const parsed = parseKcpCommand(draft?.text)
  if (!parsed.request.trim()) return draft

  const runId = parsed.runId || createRunId()
  const nextDraft = parsed.runId
    ? draft
    : { ...draft, text: injectRunId(draft.text, runId) }

  let previousIds
  try {
    previousIds = new Set(
      (await runtime.roster())
        .map(profile => String(profile?.worker_session?.id || ''))
        .filter(Boolean)
    )
  } catch (error) {
    runtime.warn(`initial worker-session snapshot failed for ${runId}: ${error}`)
    return nextDraft
  }
  void openWorkerTabWhenReady(taskTitle(parsed.request, runId), previousIds, runId, runtime)
  return nextDraft
}

function scopedSleep(ctx, signal, milliseconds) {
  return new Promise(resolve => {
    if (signal.aborted) {
      resolve(false)
      return
    }
    let settled = false
    const finish = value => {
      if (settled) return
      settled = true
      signal.removeEventListener('abort', onAbort)
      cancelTimer()
      resolve(value)
    }
    const onAbort = () => finish(false)
    const cancelTimer = ctx.setTimeout(() => finish(true), milliseconds)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

export default {
  id: ID,
  name: 'Kanban Control',
  register(ctx) {
    const lifetime = new AbortController()
    ctx.onDispose(() => {
      lifetime.abort()
      pendingRunIds.clear()
    })
    const runtime = {
      signal: lifetime.signal,
      roster: profileRoster,
      openSession: (...args) => host.openSession(...args),
      sleep: milliseconds => scopedSleep(ctx, lifetime.signal, milliseconds),
      warn: message => console.warn(`[kanban-control] ${message}`)
    }
    ctx.register({
      id: 'kanban-control.open-worker-tab',
      area: COMPOSER_AREAS.middleware,
      order: 5,
      data: { handler: draft => captureKcpRun(draft, runtime) }
    })
  }
}