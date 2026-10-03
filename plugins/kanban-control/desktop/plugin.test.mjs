import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const sourceUrl = new URL('./plugin.js', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const instrumented = source.replace(
  "import { COMPOSER_AREAS, host } from '@hermes/plugin-sdk'",
  "const COMPOSER_AREAS = { middleware: 'middleware' }; const host = globalThis.__kcpHost"
)
const plugin = await import(`data:text/javascript;base64,${Buffer.from(instrumented).toString('base64')}`)

test('injectRunId preserves the exact request payload', () => {
  const original = '/kcp  leading  spaces\nbody\n\n'
  const runId = 'kcp-20261003T120000Z-a1b2c3d4'
  const injected = plugin.injectRunId(original, runId)
  const parsed = plugin.parseKcpCommand(injected)

  assert.equal(parsed.runId, runId)
  assert.equal(parsed.request, ' leading  spaces\nbody\n\n')
})

test('createRunId always matches the Python client run id contract', () => {
  assert.match(plugin.createRunId(), /^kcp-\d{8}T\d{6}Z-[0-9a-f]{8}$/)
})

test('workerCandidate requires a new exact-title worker session', () => {
  const expectedTitle = 'KCP run kcp-20261003T120000Z-a1b2c3d4: request'
  const profiles = [
    { name: 'project-manager', worker_session: { id: 'old', title: expectedTitle } },
    { name: 'other', worker_session: { id: 'new-prefix', title: `${expectedTitle} extra` } },
    { name: 'project-manager', worker_session: { id: 'new', title: expectedTitle } }
  ]

  assert.deepEqual(plugin.workerCandidate(profiles, expectedTitle, new Set(['old'])), {
    profile: 'project-manager',
    sessionId: 'new'
  })
})

test('polling stops when the plugin lifetime is aborted', async () => {
  let rosterCalls = 0
  const controller = new AbortController()
  const runtime = {
    signal: controller.signal,
    roster: async () => {
      rosterCalls += 1
      return []
    },
    openSession: async () => assert.fail('session must not open after disposal'),
    sleep: async () => {
      controller.abort()
      return false
    },
    warn: () => {}
  }

  const opened = await plugin.openWorkerTabWhenReady(
    'KCP run kcp-20261003T120000Z-a1b2c3d4: request',
    new Set(),
    'kcp-20261003T120000Z-a1b2c3d4',
    runtime
  )

  assert.equal(opened, false)
  assert.equal(rosterCalls, 1)
})