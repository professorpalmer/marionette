import { act, cleanup, render } from '@testing-library/react';
import { useVirtualizer, type Virtualizer } from '@tanstack/react-virtual';
import { useRef } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { measureAfterCommit } from '../lib/virtualRowMeasure';

afterEach(cleanup);

type Measure = (v: Virtualizer<HTMLDivElement, Element>) => (node: HTMLElement | null) => void;

// The transcript measures rows from their ref callback, during React's commit.
const direct: Measure = v => node => v.measureElement(node);

let latest: Virtualizer<HTMLDivElement, Element> | null = null;

// jsdom lays everything out at 0px; give the scroll container a real viewport
// so the rendered range stays mounted after the virtualizer reads its size.
function sized(target: { current: HTMLDivElement | null }) {
  return (node: HTMLDivElement | null) => {
    target.current = node;
    if (!node) return;
    for (const [key, value] of [['offsetHeight', 500], ['clientHeight', 500], ['offsetWidth', 400], ['clientWidth', 400]] as const)
      Object.defineProperty(node, key, { configurable: true, value });
    node.getBoundingClientRect = () => ({ x: 0, y: 0, top: 0, left: 0, width: 400, height: 500, right: 400, bottom: 500, toJSON: () => ({}) });
  };
}

function List({ items, measure }: { items: string[]; measure: Measure }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const v = latest = useVirtualizer({
    count: items.length, getScrollElement: () => scrollRef.current, estimateSize: () => 100,
    getItemKey: i => items[i], initialOffset: 1000, initialRect: { width: 400, height: 500 }, overscan: 20,
  });
  const ref = measure(v);
  return <div ref={sized(scrollRef)} style={{ height: 500, overflow: 'auto' }}>
    <div style={{ height: v.getTotalSize(), position: 'relative' }}>
      {v.getVirtualItems().map(item => <div key={item.key} data-index={item.index} ref={ref}>{items[item.index]}</div>)}
    </div>
  </div>;
}

function flushSyncWarnings(spy: ReturnType<typeof vi.spyOn>) {
  return spy.mock.calls.filter(args => args.some(a => String(a).includes('flushSync was called from inside a lifecycle method')));
}

it('reproduces the lifecycle flushSync warning when a row above the fold is first measured in commit', async () => {
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  const items = Array.from({ length: 30 }, (_, i) => `row-${i}`);
  render(<List items={items} measure={direct} />);
  await act(async () => { await Promise.resolve(); });
  expect(flushSyncWarnings(error).length).toBeGreaterThan(0);
  error.mockRestore();
});

it('measures after the commit instead, with no warning and the same sizes', async () => {
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  const items = Array.from({ length: 30 }, (_, i) => `row-${i}`);
  render(<List items={items} measure={v => node => { if (node) measureAfterCommit(v, node); }} />);
  await act(async () => { await Promise.resolve(); });
  expect(flushSyncWarnings(error)).toEqual([]);
  // jsdom lays every row out at 0px: the deferred pass still measured them.
  expect(latest!.getTotalSize()).toBeLessThan(30 * 100);
  error.mockRestore();
});
