import { act } from '@testing-library/react';
import { vi } from 'vitest';

/** Fake only the timer calls, so app polls advance on demand and promises stay real. */
export function useFakePollTimers() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
}

/** Advance fake time in steps until check() is true. waitFor cannot be used: it polls with the faked setInterval. */
export async function advanceUntil(check: () => boolean, stepMs: number, maxMs: number) {
  for (let elapsed = 0; elapsed <= maxMs; elapsed += stepMs) {
    if (check()) return;
    await act(async () => { await vi.advanceTimersByTimeAsync(stepMs); });
  }
  if (!check()) throw new Error(`condition not met after ${maxMs} ms of fake time`);
}
