// @vitest-environment happy-dom
import { act, createElement, type FunctionComponent } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The one screen in this client that handles a credential, so the one
 * screen worth mounting.
 *
 * `npm run check` proves this component compiles. It does not prove it
 * renders, and it certainly does not prove the claim the feature rests
 * on — that the raw token appears once and is gone. Both are asserted
 * against the real DOM here, with `fetch` stubbed the way
 * `src/api/client.test.ts` stubs it: no request-mocking library, and no
 * renderer library either, on the same reasoning
 * `usePagedResource.effects.test.ts` gives.
 *
 * The stub goes in before a `vi.resetModules()` and a dynamic import, for
 * the reason that file records: `createClient` destructures
 * `globalThis.fetch` when `api/client.ts` is *evaluated*, so a stub
 * installed beside a static import arrives too late and every request
 * goes to the real one. The symptom is not an error — it is a component
 * that renders "Loading…" for ever, which reads like a broken effect.
 */

const TOKEN_VALUE = 'sretab_pat_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHI';

interface Reply {
  status: number;
  body: unknown;
}

let replies: Reply[] = [];
let requests: { url: string; method: string }[] = [];
let container: HTMLDivElement;
let root: Root;
let ApiTokensSection: FunctionComponent;

function jsonResponse({ status, body }: Reply): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * Do `work`, then let everything it started finish.
 *
 * A macrotask turn rather than a fixed count of microtasks: the request
 * goes through `openapi-fetch`, `unwrap`, and the component's own
 * `.then`, and counting the awaits in somebody else's promise chain is a
 * number that goes stale on their next release. Yielding to the timer
 * queue drains all of it whatever the depth.
 */
async function settle(work: () => void = () => undefined): Promise<void> {
  await act(async () => {
    work();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function click(text: string): void {
  const button = [...container.querySelectorAll('button')].find(
    (candidate) => candidate.textContent.trim() === text,
  );
  if (!button) throw new Error(`no button labelled ${text}: ${container.textContent}`);
  button.click();
}

beforeEach(async () => {
  replies = [];
  requests = [];
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  // React 19 warns when `act` is used outside an environment that declares
  // itself one, and that warning is the only thing that would tell us the
  // renderer had stopped flushing — see usePagedResource.effects.test.ts.
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.resetModules();
  vi.stubGlobal(
    'fetch',
    vi.fn((input: Request) => {
      requests.push({ url: input.url, method: input.method });
      const next = replies.shift();
      if (!next) throw new Error(`unexpected request: ${input.method} ${input.url}`);
      return Promise.resolve(jsonResponse(next));
    }),
  );
  ({ ApiTokensSection } = await import('./ApiTokensSection'));
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
    await Promise.resolve();
  });
  container.remove();
  vi.unstubAllGlobals();
});

/**
 * Set a controlled input's value the way a keystroke would.
 *
 * React installs its own `value` setter on the element, so assigning
 * `input.value` directly updates the DOM without the component ever
 * hearing about it, and the next render puts the old value back. Going
 * through the prototype's setter and then dispatching `input` is what
 * makes the change reach `onChange`.
 */
function type(input: HTMLInputElement, value: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  if (!descriptor?.set) throw new Error('HTMLInputElement has no value setter');
  descriptor.set.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function mount(): Promise<void> {
  await settle(() => {
    root.render(createElement(ApiTokensSection));
  });
}

describe('ApiTokensSection', () => {
  it('lists tokens without ever receiving a value', async () => {
    replies.push({
      status: 200,
      body: {
        tokens: [
          {
            id: 1,
            label: 'status board',
            scope: 'read',
            display_prefix: 'sretab_pat_abcdef',
            created_at: '2026-09-01T10:00:00Z',
            last_used_at: null,
            expires_at: null,
          },
        ],
      },
    });

    await mount();

    expect(container.textContent).toContain('status board');
    expect(container.textContent).toContain('sretab_pat_abcdef');
    expect(container.textContent).toContain('never used');
    expect(requests).toEqual([
      { url: expect.stringContaining('/api/v1/me/tokens') as string, method: 'GET' },
    ]);
  });

  it('says so when there are none', async () => {
    replies.push({ status: 200, body: { tokens: [] } });
    await mount();
    expect(container.textContent).toContain('You have no API tokens');
  });

  it('shows the raw token once and then not at all', async () => {
    replies.push({ status: 200, body: { tokens: [] } });
    await mount();

    const created = {
      id: 7,
      label: 'laptop',
      scope: 'full',
      display_prefix: 'sretab_pat_abcdef',
      created_at: '2026-09-03T10:00:00Z',
      last_used_at: null,
      expires_at: null,
    };
    replies.push({ status: 201, body: { token: created, value: TOKEN_VALUE } });
    replies.push({ status: 200, body: { tokens: [created] } });

    const label = container.querySelector<HTMLInputElement>('#token-label');
    if (!label) throw new Error('the label field is missing');
    await settle(() => {
      type(label, 'laptop');
    });

    await settle(() => {
      click('Create token');
    });

    expect(container.textContent).toContain(TOKEN_VALUE);
    expect(container.textContent).toContain('only time it is shown');

    await settle(() => {
      click('I have saved it');
    });

    // Dismissed, and nothing re-fetches it: the listing that follows is
    // the one the server can actually produce.
    expect(container.textContent).not.toContain(TOKEN_VALUE);
    expect(container.textContent).toContain('laptop');
  });

  it('revokes a token and reloads the list', async () => {
    const token = {
      id: 3,
      label: 'old runner',
      scope: 'read',
      display_prefix: 'sretab_pat_abcdef',
      created_at: '2026-01-01T10:00:00Z',
      last_used_at: '2026-01-02T10:00:00Z',
      expires_at: null,
    };
    replies.push({ status: 200, body: { tokens: [token] } });
    await mount();

    replies.push({ status: 204, body: null });
    replies.push({ status: 200, body: { tokens: [] } });

    await settle(() => {
      click('Revoke');
    });
    await settle(() => {
      click('Revoke permanently');
    });

    expect(requests.map((request) => request.method)).toEqual(['GET', 'DELETE', 'GET']);
    expect(container.textContent).toContain('You have no API tokens');
  });

  it('reports a failure to load rather than rendering an empty list', async () => {
    replies.push({ status: 500, body: { detail: 'Something broke' } });
    await mount();
    expect(container.textContent).toContain('Could not load your tokens');
    expect(container.textContent).not.toContain('You have no API tokens');
  });
});
