import { afterEach, describe, expect, it, vi } from 'vitest';
import { CharacterLibraryConflictError, fetchCharacters, saveCharacters } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('character library saves', () => {
  it('does not write unchanged cached roles when a project is saved', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify([{id:'role',name:'Role',profiles:[]}]), {headers:{ETag:'"before-import"'}}));
    vi.stubGlobal('fetch', fetch);
    const roles = await fetchCharacters();
    await saveCharacters(roles);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('uses the loaded revision and preserves edits when an external import conflicts', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([]), {headers:{ETag:'"before-import"'}}))
      .mockResolvedValueOnce(new Response('{}', {status:412}));
    vi.stubGlobal('fetch', fetch);
    await fetchCharacters();
    await expect(saveCharacters([{id:'new',name:'New',profiles:[],aliases:[],notes:'',fallback_profiles:[]}])).rejects.toBeInstanceOf(CharacterLibraryConflictError);
    expect(fetch.mock.calls[1][1].headers['If-Match']).toBe('"before-import"');
  });
});
