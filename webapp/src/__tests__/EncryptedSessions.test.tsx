import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import EncryptedSessions from '../components/EncryptedSessions';

let bridge: unknown;
let backend = { configured: false, enabled: false, unlocked: false };
vi.mock('../lib/transport', () => ({
  getHarnessIpc: () => bridge,
  getJSON: async () => backend,
}));

// Fixture only, not a real recovery code.
const fixtureCode = 'AAAA-BBBB-CCCC-DDDD-EEEE-FFFF';
let keys = { configured: false, enabled: false, protection: 'os', keyAvailable: false };
const seal = {
  status: vi.fn(async () => ({ ok: true, ...keys })),
  enable: vi.fn(async () => {
    keys = { ...keys, configured: true, enabled: true, keyAvailable: true };
    backend = { configured: true, enabled: true, unlocked: true };
    return { ok: true, recoveryCode: fixtureCode };
  }),
  disable: vi.fn(async () => { keys = { ...keys, enabled: false }; return { ok: true }; }),
  recover: vi.fn(async () => ({ ok: false, code: 'SEAL_RECOVERY_REJECTED', error: 'The recovery code is not correct.' })),
};

beforeEach(() => {
  keys = { configured: false, enabled: false, protection: 'os', keyAvailable: false };
  backend = { configured: false, enabled: false, unlocked: false };
  bridge = { seal };
  Object.values(seal).forEach((fn) => fn.mockClear());
});

async function mount() {
  await act(async () => { render(<EncryptedSessions />); });
}

describe('Encrypted sessions', () => {
  it('needs the desktop bridge', async () => {
    bridge = {};
    await mount();
    expect(screen.getByText(/need the desktop app/)).toBeInTheDocument();
  });

  it('offers no switch without an OS key store', async () => {
    keys = { ...keys, protection: 'none' };
    await mount();
    expect(screen.getByText(/no OS key store/)).toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('shows the recovery code once after the first enable', async () => {
    await mount();
    await act(async () => { fireEvent.click(screen.getByRole('checkbox', { name: 'Encrypt new sessions' })); });
    expect(seal.enable).toHaveBeenCalledTimes(1);
    expect(screen.getByText(fixtureCode)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Encrypt new sessions' })).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'I saved the code' }));
    expect(screen.queryByText(fixtureCode)).not.toBeInTheDocument();
  });

  it('offers unlock with the recovery code when the backend is locked', async () => {
    keys = { configured: true, enabled: true, protection: 'os', keyAvailable: false };
    backend = { configured: true, enabled: true, unlocked: false };
    await mount();
    expect(screen.getByText(/locked/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Recovery code'), { target: { value: 'wrong' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Unlock' })); });
    expect(seal.recover).toHaveBeenCalledWith('wrong');
    expect(screen.getByRole('alert')).toHaveTextContent('not correct');
  });
});
