import '@/i18n'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { runtimeApi } from '@/api/runtime'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import RuntimeInvestigationPage from './RuntimeInvestigationPage'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

// These tests exercise the deep-link case: an operator arrives with both
// required fields already supplied via query params, so the page's gated
// auto-run (see the Minor auto-run fix in RuntimeInvestigationPage.tsx)
// fires immediately, same as it always did before that fix.
function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/?snapshot_id=snap-testing&ontology_id=ontology-testing']}>
      <RuntimeInvestigationPage />
    </MemoryRouter>,
  )
}

describe('RuntimeInvestigationPage', () => {
  it('ignores an auto-run result after the operator changes its inputs', async () => {
    type Result = Awaited<ReturnType<typeof runtimeApi.investigate>>
    let finish!: (value: Result) => void
    const pending = new Promise<Result>(resolve => { finish = resolve })
    const investigate = vi.spyOn(runtimeApi, 'investigate').mockReturnValue(pending)
    try {
      renderPage()
      fireEvent.change(screen.getByTestId('investigate-snapshot-id'), { target: { value: 'new-snapshot' } })
      await act(async () => {
        finish({ decision: 'ALLOW', reason_code: 'ALLOW', result: [], evidence_citations: [], rule_outcome: [] } as unknown as Result)
        await pending
      })
      expect(screen.queryByTestId('investigation-result')).toBeNull()
      expect(screen.getByTestId('run-investigation')).not.toBeDisabled()
    } finally { investigate.mockRestore() }
  })

  it('shows snapshot evidence and an ALLOW decision', async () => {
    server.use(
      http.post('*/api/v2/runtime/investigate', () =>
        HttpResponse.json({
          decision: 'ALLOW',
          reason_code: 'ALLOW',
          semantic_snapshot_id: 'snap-valid-001',
          ontology_release_id: 'release-001',
          evidence_citations: [{ source_id: 's-1', source_type: 'dataset', locator: 'row-1', content_hash: 'h' }],
          rule_outcome: [{ rule_id: 'r-1', result: 'matched', reason_code: 'ALLOW' }],
          result: [{ id: 'row-1' }],
          correlation_id: 'corr-1',
        })),
    )
    renderPage()
    expect(await screen.findByTestId('semantic-snapshot-id')).toHaveTextContent('snap-valid-001')
    expect(screen.getByTestId('investigation-decision')).toHaveTextContent('ALLOW')
  })

  it('distinguishes an authorized empty result from a denial', async () => {
    server.use(
      http.post('*/api/v2/runtime/investigate', () =>
        HttpResponse.json({
          decision: 'ALLOW',
          reason_code: 'ALLOW',
          semantic_snapshot_id: 'snap-empty-001',
          ontology_release_id: 'release-001',
          evidence_citations: [],
          rule_outcome: [],
          result: [],
          correlation_id: 'corr-2',
        })),
    )
    renderPage()
    expect(await screen.findByTestId('investigation-decision')).toHaveTextContent('ALLOW')
    expect(screen.getByTestId('investigation-result-count')).toHaveTextContent('0')
    expect(screen.queryByTestId('investigation-denied')).toBeNull()
  })

  it('renders a DENY decision with no result rows and no crash', async () => {
    server.use(
      http.post('*/api/v2/runtime/investigate', () =>
        HttpResponse.json(
          {
            decision: 'DENY',
            reason_code: 'POLICY_DENIED',
            semantic_snapshot_id: 'snap-denied-001',
            ontology_release_id: 'release-001',
            evidence_citations: [],
            rule_outcome: [],
            result: null,
            correlation_id: 'corr-3',
          },
          { status: 403 },
        )),
    )
    renderPage()
    expect(await screen.findByTestId('investigation-decision')).toHaveTextContent('DENY')
    expect(screen.getByTestId('investigation-denied')).toBeTruthy()
    expect(screen.queryByTestId('investigation-result-count')).toBeNull()
  })

  it('does not auto-run an investigation on mount when required fields are empty', async () => {
    let requestCount = 0
    server.use(
      http.post('*/api/v2/runtime/investigate', () => {
        requestCount += 1
        return HttpResponse.json({
          decision: 'DENY',
          reason_code: 'POLICY_DENIED',
          semantic_snapshot_id: '',
          ontology_release_id: null,
          evidence_citations: [],
          rule_outcome: [],
          result: null,
          correlation_id: 'corr-4',
        }, { status: 403 })
      }),
    )
    render(
      <MemoryRouter>
        <RuntimeInvestigationPage />
      </MemoryRouter>,
    )
    // Give any (incorrect) auto-run effect a chance to fire before asserting
    // it did not.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(requestCount).toBe(0)
    expect(screen.queryByTestId('investigation-result')).toBeNull()

    // The operator can still explicitly submit once fields are filled.
    await userEvent.type(screen.getByTestId('investigate-snapshot-id'), 'snap-manual-001')
    await userEvent.type(screen.getByTestId('investigate-ontology-id'), 'ontology-manual-001')
    await userEvent.click(screen.getByTestId('run-investigation'))
    expect(await screen.findByTestId('investigation-decision')).toHaveTextContent('DENY')
    expect(requestCount).toBe(1)
  })
})
