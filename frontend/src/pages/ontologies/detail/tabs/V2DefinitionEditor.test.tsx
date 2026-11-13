import '@/i18n'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { apiClientV2 } from '@/api/client'
import V2DefinitionEditor from './V2DefinitionEditor'

it('clears selection and ignores an old save reload after switching ontology', async () => {
  let resolveSave!: () => void
  const save = new Promise<void>(resolve => { resolveSave = resolve })
  const get = vi.spyOn(apiClientV2, 'get').mockImplementation(url => Promise.resolve([
    { id: 'definition', name: url.includes('/old/') ? 'Old rule' : 'New rule', expression: {} },
  ]) as never)
  const patch = vi.spyOn(apiClientV2, 'patch').mockReturnValue(save)
  try {
    const view = render(<V2DefinitionEditor ontologyId="old" kind="logic" />)
    fireEvent.click(await screen.findByText('Old rule'))
    fireEvent.click(screen.getByTestId('v2-logic-save'))
    view.rerender(<V2DefinitionEditor ontologyId="new" kind="logic" />)
    await screen.findByText('New rule')
    await act(async () => { resolveSave(); await save })
    expect(screen.queryByText('Old rule')).toBeNull()
    expect(screen.getByText('New rule')).toBeTruthy()
    expect(screen.queryByTestId('v2-logic-save')).toBeNull()
  } finally { get.mockRestore(); patch.mockRestore() }
})
