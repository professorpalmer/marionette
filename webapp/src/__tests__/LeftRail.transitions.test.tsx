import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import LeftRail from '../components/LeftRail';
import { api, type Session } from '../lib/api';
import { clearSWRCache, writeSWRCache, readSWRCache } from '../lib/useStaleWhileRevalidate';
import { clearTranscriptCache, peekTranscriptCacheEntry } from '../components/conversation/transcriptCache';
vi.mock('../lib/api', () => ({ api: {
  getWorkspace: vi.fn().mockResolvedValue({ repo: '/workspace', recents: [], is_git: false }),
  openWorkspace: vi.fn(), sessions: vi.fn(), createSession: vi.fn(), archiveSession: vi.fn(), renameSession: vi.fn(), deleteSession: vi.fn(), switchSession: vi.fn(),
  workspaces: vi.fn().mockResolvedValue([]), jobs: vi.fn().mockResolvedValue([]),
} }));
vi.mock('../lib/usePolling', () => ({ usePolling: vi.fn() }));
vi.mock('../lib/useOperationalDiagnostic', () => ({ useOperationalDiagnostic: () => null }));
const old: Session = { id: 'old', title: 'Current', created: 1, active: true, repo: '/workspace' };
const created: Session = { id: 'server-id', title: 'Server title', created: 2, repo: '/workspace' };
const savedOther: Session = { id: 'saved-other', title: 'Saved other session', created: 3, repo: '/other' };
function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  let reject: (reason: unknown) => void = () => {};
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
beforeEach(() => {
  localStorage.clear(); clearSWRCache(); vi.clearAllMocks();
  clearTranscriptCache();
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: [], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockResolvedValue([old]);
  vi.mocked(api.createSession).mockResolvedValue(created);
  vi.mocked(api.archiveSession).mockResolvedValue({ ok: true });
  vi.mocked(api.openWorkspace).mockReset();
});
afterEach(cleanup);
it('discovers saved sessions on the first project expansion after cold boot', async () => {
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [savedOther] : [old]);
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  expect(api.sessions).toHaveBeenCalledWith('/other');
  expect(screen.getByRole('button', { name: savedOther.title, exact: true })).toBeVisible();
  expect(screen.queryByTitle('Open other and start a session')).toBeNull();
  expect(api.openWorkspace).not.toHaveBeenCalled();
  expect(api.createSession).not.toHaveBeenCalled();
  expect(changed).toHaveBeenLastCalledWith(old.id);
});

it('does not label a failed project read empty and retries on expansion', async () => {
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(root => root === '/other' ? Promise.reject(new Error('offline')) : Promise.resolve([old]));
  render(<LeftRail jobsRefresh={0} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  expect(screen.queryByTitle('Open other and start a session')).toBeNull();
  expect(api.createSession).not.toHaveBeenCalled();
  expect(api.openWorkspace).not.toHaveBeenCalled();
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [savedOther] : [old]);
  fireEvent.click(screen.getByTitle('/other'));
  fireEvent.click(screen.getByTitle('/other'));
  await screen.findByRole('button', { name: savedOther.title, exact: true });
});

it('publishes a resolved project without waiting for other project reads', async () => {
  const other = deferred<Session[]>();
  const stalled = deferred<Session[]>();
  writeSWRCache('workspace', { repo: '/workspace', recents: ['/other', '/stalled'], is_git: false });
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.sessions).mockImplementation(root => root === '/other' ? other.promise : root === '/stalled' ? stalled.promise : Promise.resolve([old]));
  render(<LeftRail jobsRefresh={0} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  fireEvent.click(screen.getByTitle('/other'));
  expect(screen.queryByTitle('Open other and start a session')).toBeNull();
  await act(async () => { other.resolve([savedOther]); });
  await waitFor(() => expect(screen.getByRole('button', { name: savedOther.title, exact: true })).toBeVisible());
  expect(api.createSession).not.toHaveBeenCalled();
});

it('publishes the server row and identity before background lists finish', async () => {
  const changed = vi.fn((id: string) => {
    if (id === created.id) expect(readSWRCache<Session[]>('sessions:/workspace')?.find(row => row.id === id)?.active).toBe(true);
  });
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
  await act(async () => { fireEvent.click(screen.getByLabelText('New session in workspace')); });
  expect(screen.getByRole('button', { name: 'Server title', exact: true })).toHaveAttribute('aria-current', 'true');
  expect(changed).toHaveBeenLastCalledWith('server-id');
});
it('hides the empty CTA while archive replacement is pending', async () => {
  const next = deferred<Session>();
  vi.mocked(api.createSession).mockReturnValue(next.promise);
  vi.mocked(api.archiveSession).mockImplementation(async () => {
    vi.mocked(api.sessions).mockResolvedValue([{ ...old, archived: true }]);
    return { ok: true };
  });
  render(<LeftRail jobsRefresh={0} />);
  const row = await screen.findByRole('button', { name: 'Current', exact: true });
  fireEvent.contextMenu(row);
  await act(async () => { fireEvent.click(screen.getByText('Archive', { exact: true })); });
  expect(api.createSession).toHaveBeenCalled();
  expect(screen.queryByTitle('Open workspace and start a session')).toBeNull();
  await act(async () => {
    vi.mocked(api.sessions).mockResolvedValue([{ ...old, archived: true, active: false }, { ...created, active: true }]);
    next.resolve(created);
  });
  expect(screen.getByRole('button', { name: 'Server title', exact: true })).toHaveAttribute('aria-current', 'true');
});
it('does not publish a create response after another project is selected', async () => {
  const next = deferred<Session>();
  vi.mocked(api.createSession).mockReturnValue(next.promise);
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  fireEvent.click(screen.getByLabelText('New session in workspace'));
  changed.mockClear();
  await act(async () => {
    window.dispatchEvent(new CustomEvent('harness-project-selected', { detail: '/other' }));
    next.resolve(created);
  });
  expect(changed).not.toHaveBeenCalled();
});
it('keeps project plus keyboard reachable and removes decorative row icons', async () => {
  render(<LeftRail jobsRefresh={0} />);
  const row = await screen.findByRole('button', { name: 'Current', exact: true });
  expect(row.querySelector('.lucide-message-square')).toBeNull();
  expect(screen.getByLabelText('New session in workspace')).toHaveClass('opacity-0', 'group-hover:opacity-100', 'focus-visible:opacity-100');
});

it('rejects pre-create lists that resolve after the server row was published', async () => {
  const stale = deferred<Session[]>();
  writeSWRCache('sessions:/workspace', [old]);
  vi.mocked(api.sessions).mockReturnValue(stale.promise);
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
  await act(async () => { fireEvent.click(screen.getByLabelText('New session in workspace')); });
  await act(async () => { stale.resolve([old]); });
  expect(screen.getByRole('button', { name: 'Server title', exact: true })).toHaveAttribute('aria-current', 'true');
  expect(changed).toHaveBeenLastCalledWith('server-id');
});
it('keeps lease exhaustion visible when automatic replacement cannot start', async () => {
  vi.mocked(api.createSession).mockRejectedValue({ code: 'lease_exhausted' });
  vi.mocked(api.archiveSession).mockImplementation(async () => {
    vi.mocked(api.sessions).mockResolvedValue([{ ...old, archived: true }]);
    return { ok: true };
  });
  render(<LeftRail jobsRefresh={0} />);
  fireEvent.contextMenu(await screen.findByRole('button', { name: 'Current', exact: true }));
  await act(async () => { fireEvent.click(screen.getByText('Archive', { exact: true })); });
  expect(screen.getByText(/too many sessions are busy right now/)).toBeVisible();
  expect(screen.queryByTitle('Open workspace and start a session')).toBeNull();
  expect(screen.getByLabelText('New session in workspace')).toBeEnabled();
});

it('opens an authoritative empty project once and publishes its usable session immediately', async () => {
  const opening = deferred<Awaited<ReturnType<typeof api.openWorkspace>>>();
  const changed = vi.fn();
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [] : [old]);
  vi.mocked(api.openWorkspace).mockReturnValue(opening.promise);
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  expect(api.openWorkspace).not.toHaveBeenCalled();
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  expect(api.openWorkspace).toHaveBeenCalledExactlyOnceWith('/other');
  expect(api.createSession).not.toHaveBeenCalled();
  expect(screen.queryByTitle('Open other and start a session')).toBeNull();
  fireEvent.click(screen.getByTitle('/other'));
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  await act(async () => { opening.resolve({ ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', active_session: 'empty-open-id', created_session: true }); });
  expect(api.openWorkspace).toHaveBeenCalledTimes(1);
  expect(api.createSession).not.toHaveBeenCalled();
  expect(changed).toHaveBeenLastCalledWith('empty-open-id');
  expect(readSWRCache<Session[]>('sessions:/other')).toEqual([expect.objectContaining({ id: 'empty-open-id', active: true })]);
  expect(screen.getByRole('button', { name: 'New session', exact: true, current: true })).toBeVisible();
});

it('uses a saved session discovered by workspace-open after the empty list', async () => {
  const changed = vi.fn();
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [] : [old]);
  vi.mocked(api.openWorkspace).mockImplementation(async () => {
    vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
    vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
    return { ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', active_session: savedOther.id, created_session: false };
  });
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  expect(api.openWorkspace).toHaveBeenCalledExactlyOnceWith('/other');
  expect(api.createSession).not.toHaveBeenCalled();
  expect(changed).toHaveBeenLastCalledWith(savedOther.id);
  expect(readSWRCache<Session[]>('sessions:/other')).toEqual([expect.objectContaining({ id: savedOther.id, active: true })]);
  expect(peekTranscriptCacheEntry(savedOther.id)?.seededEmpty).not.toBe(true);
});

it('retries a failed read from its visible retry action before deciding whether to open', async () => {
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(root => root === '/other' ? Promise.reject(new Error('offline')) : Promise.resolve([old]));
  render(<LeftRail jobsRefresh={0} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [savedOther] : [old]);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Could not load sessions. Retry' })); });
  expect(screen.getByRole('button', { name: savedOther.title })).toBeVisible();
  expect(api.openWorkspace).not.toHaveBeenCalled();
  expect(api.createSession).not.toHaveBeenCalled();
});

it('refreshes the active root independently of a stalled project after rename', async () => {
  const stale = deferred<Session[]>();
  const stalled = deferred<Session[]>();
  writeSWRCache('workspace', { repo: '/workspace', recents: ['/stalled'], is_git: false });
  writeSWRCache('sessions:/workspace', [old]);
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.sessions).mockImplementation(root => root === '/stalled' ? stalled.promise : stale.promise);
  const renamed = { ...old, title: 'Renamed session' };
  vi.mocked(api.renameSession).mockImplementation(async () => {
    vi.mocked(api.sessions).mockImplementation(root => root === '/stalled' ? stalled.promise : Promise.resolve([renamed]));
    return { ok: true };
  });
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  const row = await screen.findByRole('button', { name: 'Current', exact: true });
  fireEvent.doubleClick(row);
  fireEvent.change(screen.getByRole('textbox'), { target: { value: renamed.title } });
  await act(async () => { fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' }); });
  expect(screen.getByRole('button', { name: renamed.title })).toBeVisible();
  expect(changed).toHaveBeenLastCalledWith(old.id);
  await act(async () => { stale.resolve([old]); });
  expect(readSWRCache<Session[]>('sessions:/workspace')?.[0].title).toBe(renamed.title);
});

it.each(['archive', 'delete'])('keeps a removed row out when a pre-%s list arrives late', async action => {
  const stale = deferred<Session[]>();
  const removable = { ...created, id: 'remove-me', title: 'Remove me' };
  writeSWRCache('workspace', { repo: '/workspace', recents: [], is_git: false });
  writeSWRCache('sessions:/workspace', [old, removable]);
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.sessions).mockReturnValue(stale.promise);
  const removed = async () => {
    vi.mocked(api.sessions).mockResolvedValue([old]);
    return { ok: true };
  };
  vi.mocked(api.archiveSession).mockImplementation(removed);
  vi.mocked(api.deleteSession).mockImplementation(removed);
  render(<LeftRail jobsRefresh={0} />);
  const row = await screen.findByRole('button', { name: removable.title });
  if (action === 'archive') {
    fireEvent.contextMenu(row);
    await act(async () => { fireEvent.click(screen.getByText('Archive', { exact: true })); });
  } else {
    fireEvent.click(screen.getAllByTitle('Delete session')[0]);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Yes', exact: true })); });
    expect(api.deleteSession).toHaveBeenCalledWith(removable.id);
  }
  await act(async () => { stale.resolve([old, removable]); });
  expect(screen.queryByRole('button', { name: removable.title })).toBeNull();
  expect(readSWRCache<Session[]>('sessions:/workspace')?.some(item => item.id === removable.id && !item.archived)).toBe(false);
  expect(api.createSession).not.toHaveBeenCalled();
});

it('keeps a pre-switch list from restoring the former active session', async () => {
  const stale = deferred<Session[]>();
  writeSWRCache('workspace', { repo: '/workspace', recents: [], is_git: false });
  writeSWRCache('sessions:/workspace', [old, created]);
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.sessions).mockReturnValue(stale.promise);
  vi.mocked(api.switchSession).mockResolvedValue({ ok: true, repo: '/workspace' });
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: created.title });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: created.title })); });
  await act(async () => { stale.resolve([old, created]); });
  expect(screen.getByRole('button', { name: created.title })).toHaveAttribute('aria-current', 'true');
  expect(changed).toHaveBeenLastCalledWith(created.id);
});

it('does not promote the original A list after A-B-A view navigation', async () => {
  const stale = deferred<Session[]>();
  writeSWRCache('workspace', { repo: '/workspace', recents: ['/other'], is_git: false });
  writeSWRCache('sessions:/workspace', [old]);
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.sessions).mockImplementation(root => root === '/workspace' ? stale.promise : Promise.resolve([savedOther]));
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: old.title });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  fireEvent.click(screen.getByTitle('/workspace'));
  fireEvent.click(screen.getByTitle('/workspace'));
  changed.mockClear();
  await act(async () => { stale.resolve([old]); });
  expect(changed).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: old.title })).toBeVisible();
  expect(api.openWorkspace).not.toHaveBeenCalled();
});

it('does not let a pre-open workspace GET undo the returned workspace identity', async () => {
  const stale = deferred<Awaited<ReturnType<typeof api.getWorkspace>>>();
  writeSWRCache('workspace', { repo: '/workspace', recents: ['/other'], is_git: false });
  vi.mocked(api.getWorkspace).mockReturnValue(stale.promise);
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [] : [old]);
  vi.mocked(api.openWorkspace).mockImplementation(async () => {
    vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
    vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
    return { ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', active_session: 'opened-id', created_session: true };
  });
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: old.title });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  await act(async () => { stale.resolve({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' }); });
  expect(readSWRCache<{ repo: string }>('workspace')?.repo).toBe('/other');
  expect(changed).toHaveBeenLastCalledWith('opened-id');
});

it.each(['collapse', 'other project', 'abort'])('does not activate an empty result after %s supersedes its read', async action => {
  const pending = deferred<Session[]>();
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(root => root === '/other' ? pending.promise : Promise.resolve([old]));
  render(<LeftRail jobsRefresh={0} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  fireEvent.click(screen.getByTitle('/other'));
  expect(screen.getByText('Loading sessions...')).toBeVisible();
  expect(api.openWorkspace).not.toHaveBeenCalled();
  expect(vi.mocked(api.sessions).mock.calls.filter(([root]) => root === '/other')).toHaveLength(1);
  await act(async () => {
    if (action === 'collapse') fireEvent.click(screen.getByTitle('/other'));
    if (action === 'other project') fireEvent.click(screen.getByTitle('/workspace'));
    if (action === 'abort') pending.reject(new DOMException('Cancelled', 'AbortError'));
    else pending.resolve([]);
  });
  expect(api.openWorkspace).not.toHaveBeenCalled();
  expect(api.createSession).not.toHaveBeenCalled();
});

it('offers retry after a failed empty-project activation without creating an extra session', async () => {
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [] : [old]);
  vi.mocked(api.openWorkspace).mockRejectedValue(new Error('offline'));
  render(<LeftRail jobsRefresh={0} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  const retry = screen.getByRole('button', { name: /Could not open project.*Retry/ });
  expect(screen.queryByText('Loading sessions...')).toBeNull();
  expect(api.createSession).not.toHaveBeenCalled();
  vi.mocked(api.openWorkspace).mockImplementation(async () => {
    vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
    vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
    return { ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', active_session: 'retry-id', created_session: true };
  });
  await act(async () => { fireEvent.click(retry); });
  expect(api.openWorkspace).toHaveBeenCalledTimes(2);
  expect(readSWRCache<Session[]>('sessions:/other')?.[0].id).toBe('retry-id');
  expect(api.createSession).not.toHaveBeenCalled();
});

it('fences a late activation after A-B-A navigation', async () => {
  const opening = deferred<Awaited<ReturnType<typeof api.openWorkspace>>>();
  const changed = vi.fn();
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [] : [old]);
  vi.mocked(api.openWorkspace).mockReturnValue(opening.promise);
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  fireEvent.click(screen.getByTitle('/workspace'));
  await act(async () => { fireEvent.click(screen.getByTitle('/workspace')); });
  changed.mockClear();
  await act(async () => { opening.resolve({ ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', active_session: 'late-id', created_session: true }); });
  expect(changed).not.toHaveBeenCalled();
  expect(readSWRCache<{ repo: string }>('workspace')?.repo).toBe('/workspace');
  expect(readSWRCache<Session[]>('sessions:/other')?.some(row => row.id === 'late-id')).not.toBe(true);
});

it.each(['collapse', 'session click'])('does not publish a pending workspace open after a newer %s', async action => {
  const opening = deferred<Awaited<ReturnType<typeof api.openWorkspace>>>();
  const changed = vi.fn();
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [] : [old]);
  vi.mocked(api.openWorkspace).mockReturnValue(opening.promise);
  vi.mocked(api.switchSession).mockResolvedValue({ ok: true, repo: '/workspace' });
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: old.title });
  await act(async () => { fireEvent.click(screen.getByTitle('/other')); });
  expect(api.openWorkspace).toHaveBeenCalledExactlyOnceWith('/other');
  await act(async () => {
    fireEvent.click(action === 'collapse' ? screen.getByTitle('/other') : screen.getByRole('button', { name: old.title }));
  });
  changed.mockClear();
  await act(async () => { opening.resolve({ ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', active_session: 'late-collapse-id', created_session: true }); });
  expect(changed).not.toHaveBeenCalled();
  expect(readSWRCache<{ repo: string }>('workspace')?.repo).toBe('/workspace');
  expect(screen.queryByText('Loading sessions...')).toBeNull();
  expect(api.createSession).not.toHaveBeenCalled();
});

it('publishes an automatically created workspace session without waiting for workspace refresh', async () => {
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockImplementation(async root => root === '/other' ? [] : [old]);
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.openWorkspace).mockResolvedValue({ ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', created_session: true, active_session: 'workspace-server-id' });
  await act(async () => { fireEvent.click(screen.getByLabelText('New session in other')); });
  expect(changed).toHaveBeenLastCalledWith('workspace-server-id');
  expect(readSWRCache<Session[]>('sessions:/other')).toEqual([expect.objectContaining({ id: 'workspace-server-id', active: true })]);
  expect(api.createSession).not.toHaveBeenCalled();
});
