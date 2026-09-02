// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';

/**
 * The fetch layer, exercised through a stubbed `globalThis.fetch` rather
 * than a request-mocking library. The seam is one function, so a five-line
 * stub reaches everything `msw` would and adds no interceptor tree to the
 * dependency set.
 *
 * The DOM environment is *not* optional here, and the reason is worth
 * knowing before someone tries to take it back out. `createClient` is
 * configured with no `baseUrl` — deliberately, so no API host is baked into
 * the build — so openapi-fetch calls `new Request('/api/v1/me', …)` with a
 * root-relative URL. A browser resolves that against the document; Node's
 * undici does not, and throws `TypeError: Failed to parse URL`. The
 * same-origin assumption this module is built on is therefore load-bearing
 * at the point the `Request` is constructed, not merely at the point it is
 * sent, and only a DOM environment reproduces it.
 *
 * It is declared per-file, in the docblock above, rather than in
 * `vite.config.ts`. The rest of the suite runs in the default `node`
 * environment on purpose — `theme.test.ts` installs by hand every global it
 * touches so that a new one shows up as a failure — and a global environment
 * setting would quietly hand those files a DOM they never asked for.
 */

type Responder = (request: Request) => Response | Promise<Response>;

interface Loaded {
  mod: typeof import('./client');
  /** Every request that reached the stubbed `fetch`, in order. */
  sent: Request[];
}

/**
 * `createClient` destructures `globalThis.fetch` when the module is
 * evaluated, not when a request is made, so the stub has to be installed
 * before the import — hence the reset and the dynamic import rather than a
 * `vi.stubGlobal` beside a static one. The reset also gives each test its
 * own `unauthorisedListeners` set, which is module-level state.
 */
async function loadClient(respond: Responder): Promise<Loaded> {
  const sent: Request[] = [];
  vi.resetModules();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: Request) => {
      sent.push(request);
      return await respond(request);
    }),
  );
  const mod = await import('./client');
  return { mod, sent };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Anything at all, for the cases where only the request matters. */
const ok: Responder = () => json({ ok: true });

function withCookie(cookie: string) {
  vi.stubGlobal('document', { cookie });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

// --- what goes out ------------------------------------------------------

describe('the request the client builds', () => {
  it('resolves the schema path against the page origin, with no host baked in', async () => {
    const { mod, sent } = await loadClient(ok);
    await mod.api.GET('/api/v1/me');

    const url = new URL(sent[0].url);
    expect(url.pathname).toBe('/api/v1/me');
    // Not "some absolute URL": the same origin the app was served from.
    // A baseUrl added here would be a deployment assumption the Caddyfile
    // and the FastAPI mount both contradict.
    expect(url.origin).toBe(window.location.origin);
  });

  it('sends credentials same-origin', async () => {
    const { mod, sent } = await loadClient(ok);
    await mod.api.GET('/api/v1/me');

    // The session is an HttpOnly cookie; `omit` would sign every user out
    // and `include` would offer it cross-origin.
    expect(sent[0].credentials).toBe('same-origin');
  });

  it('asks for JSON', async () => {
    const { mod, sent } = await loadClient(ok);
    await mod.api.GET('/api/v1/me');

    expect(sent[0].headers.get('Accept')).toBe('application/json');
  });
});

// --- CSRF ---------------------------------------------------------------

describe('the CSRF double-submit header', () => {
  it('is not sent on a GET', async () => {
    const { mod, sent } = await loadClient(ok);
    withCookie('csrftoken=abc123');
    await mod.api.GET('/api/v1/me');

    // Safe methods take no CSRF check server-side; echoing the token on
    // them would widen where it can leak for nothing in return.
    expect(sent[0].headers.get('X-CSRF-Token')).toBeNull();
  });

  it.each([
    ['POST', async (m: typeof import('./client')) => m.api.POST('/api/v1/auth/logout', {})],
    [
      'PUT',
      async (m: typeof import('./client')) =>
        m.api.PUT('/api/v1/items/{item_id}/bookmark', { params: { path: { item_id: 1 } } }),
    ],
    [
      'PATCH',
      async (m: typeof import('./client')) =>
        m.api.PATCH('/api/v1/me/preferences', { body: { theme: 'dark' } }),
    ],
    ['DELETE', async (m: typeof import('./client')) => m.api.DELETE('/api/v1/me', {})],
  ])('is echoed from the cookie on a %s', async (method, call) => {
    const { mod, sent } = await loadClient(ok);
    withCookie('csrftoken=abc123');
    await call(mod);

    expect(sent[0].method).toBe(method);
    expect(sent[0].headers.get('X-CSRF-Token')).toBe('abc123');
  });

  it('sends the request without the header when no cookie is set', async () => {
    const { mod, sent } = await loadClient(ok);
    withCookie('');
    await mod.api.POST('/api/v1/auth/logout', {});

    // The server will refuse it, and that 403 is the right thing for the
    // user to see. Refusing to send it here would turn a recoverable
    // "sign in again" into a silent no-op.
    expect(sent).toHaveLength(1);
    expect(sent[0].headers.get('X-CSRF-Token')).toBeNull();
  });

  it('honours the build-time cookie and header name overrides', async () => {
    // The escape hatch for an operator who changed CSRF_COOKIE_NAME or
    // CSRF_HEADER_NAME server-side. Read at module scope, so it only takes
    // effect if the environment is set before the import.
    vi.stubEnv('VITE_CSRF_COOKIE_NAME', 'sre-csrf');
    vi.stubEnv('VITE_CSRF_HEADER_NAME', 'X-SRE-CSRF');
    const { mod, sent } = await loadClient(ok);
    expect(mod.CSRF_COOKIE_NAME).toBe('sre-csrf');
    expect(mod.CSRF_HEADER_NAME).toBe('X-SRE-CSRF');

    withCookie('csrftoken=wrong; sre-csrf=right');
    await mod.api.POST('/api/v1/auth/logout', {});

    expect(sent[0].headers.get('X-SRE-CSRF')).toBe('right');
    expect(sent[0].headers.get('X-CSRF-Token')).toBeNull();
  });
});

describe('readCookie', () => {
  it('returns null when there is no document at all', async () => {
    const { mod } = await loadClient(ok);
    vi.stubGlobal('document', undefined);

    expect(mod.readCookie('csrftoken')).toBeNull();
  });

  it('returns null when the cookie is absent', async () => {
    const { mod } = await loadClient(ok);
    withCookie('session=x; other=y');

    expect(mod.readCookie('csrftoken')).toBeNull();
  });

  it('picks the named cookie out of several, whatever its position', async () => {
    const { mod } = await loadClient(ok);

    withCookie('csrftoken=first; other=y');
    expect(mod.readCookie('csrftoken')).toBe('first');

    withCookie('other=y; csrftoken=middle; last=z');
    expect(mod.readCookie('csrftoken')).toBe('middle');

    withCookie('other=y; csrftoken=last');
    expect(mod.readCookie('csrftoken')).toBe('last');
  });

  it('does not match a cookie whose name merely ends with the one asked for', async () => {
    const { mod } = await loadClient(ok);
    withCookie('xcsrftoken=wrong; csrftoken=right');

    expect(mod.readCookie('csrftoken')).toBe('right');
  });

  it('does not match a cookie whose name merely starts with the one asked for', async () => {
    const { mod } = await loadClient(ok);
    withCookie('csrftoken2=wrong; csrftoken=right');

    expect(mod.readCookie('csrftoken')).toBe('right');
  });

  it('decodes percent-encoding in the value', async () => {
    const { mod } = await loadClient(ok);
    withCookie('csrftoken=a%20b');

    expect(mod.readCookie('csrftoken')).toBe('a b');
  });

  it('returns an empty value as the empty string, not as absent', async () => {
    const { mod } = await loadClient(ok);
    withCookie('csrftoken=');

    // `null` here would be indistinguishable from "no cookie", and the
    // middleware's `if (token)` treats both the same way anyway — but the
    // distinction is the function's contract, so pin it.
    expect(mod.readCookie('csrftoken')).toBe('');
  });

  // Marked `.fails`: this asserts the behaviour we want and records that
  // the client does not have it. `decodeURIComponent` throws `URIError` on
  // a stray `%`, and `readCookie` does not catch it.
  //
  // The server never writes such a value — the token is base64url — but the
  // cookie is not `HttpOnly` (that is the whole mechanism: this file has to
  // read it) and it carries no `__Host-` prefix, so a sibling subdomain can
  // set one, which is the same exposure the roadmap already records for the
  // OAuth state cookie. See the request-level consequence below.
  it.fails('tolerates a value that is not valid percent-encoding', async () => {
    const { mod } = await loadClient(ok);
    withCookie('csrftoken=100%-genuine');

    expect(mod.readCookie('csrftoken')).toBe('100%-genuine');
  });
});

// --- how failures surface -----------------------------------------------

describe('unwrap', () => {
  it('returns the body of a 2xx', async () => {
    const { mod } = await loadClient(() => json({ user: { id: 7 } }));

    expect(mod.unwrap(await mod.api.GET('/api/v1/me'))).toEqual({ user: { id: 7 } });
  });

  it('throws an ApiError carrying the HTTP status', async () => {
    const { mod } = await loadClient(() => json({ detail: 'Not signed in.' }, 401));

    const thrown: unknown = await mod.api.GET('/api/v1/me').then(
      (result) => {
        try {
          mod.unwrap(result);
          return null;
        } catch (cause: unknown) {
          return cause;
        }
      },
    );

    expect(thrown).toBeInstanceOf(mod.ApiError);
    const error = thrown as InstanceType<typeof mod.ApiError>;
    expect(error.status).toBe(401);
    // `SessionProvider` branches on this to drop to the landing page
    // rather than to an error banner, so it is contract, not convenience.
    expect(error.isUnauthorised).toBe(true);
    expect(error.message).toBe('Not signed in.');
    expect(error.body).toEqual({ detail: 'Not signed in.' });
  });

  it('reads a FastAPI string detail as the message', async () => {
    const { mod } = await loadClient(() => json({ detail: 'Source is disabled.' }, 409));

    await expect(
      mod.api.GET('/api/v1/me').then((r) => mod.unwrap(r)),
    ).rejects.toThrow('Source is disabled.');
  });

  it('joins a 422 validation detail into one message', async () => {
    const { mod } = await loadClient(() =>
      json(
        {
          detail: [
            { loc: ['query', 'limit'], msg: 'Input should be less than or equal to 100' },
            { loc: ['query', 'read_state'], msg: 'Input should be one of all, read, unread' },
          ],
        },
        422,
      ),
    );

    await expect(mod.api.GET('/api/v1/me').then((r) => mod.unwrap(r))).rejects.toThrow(
      'Input should be less than or equal to 100; Input should be one of all, read, unread',
    );
  });

  it('falls back to the status when the validation detail carries no messages', async () => {
    const { mod } = await loadClient(() => json({ detail: [{ loc: ['body'] }] }, 422));

    await expect(mod.api.GET('/api/v1/me').then((r) => mod.unwrap(r))).rejects.toThrow(
      'Request failed (HTTP 422).',
    );
  });

  it('names 501 specifically', async () => {
    const { mod } = await loadClient(() => new Response('', { status: 501 }));

    // The stub routes answer 501 before a backend lands; "Request failed"
    // would send someone looking for a bug that is not there.
    await expect(mod.api.GET('/api/v1/me').then((r) => mod.unwrap(r))).rejects.toThrow(
      'This part of the API is not implemented yet.',
    );
  });

  it('survives a non-JSON error body from something in front of the app', async () => {
    const { mod } = await loadClient(
      () =>
        new Response('<html><body>502 Bad Gateway</body></html>', {
          status: 502,
          headers: { 'Content-Type': 'text/html' },
        }),
    );

    // Caddy's own error page, not the application's. `describe` must not
    // try to read `detail` off a string.
    await expect(mod.api.GET('/api/v1/me').then((r) => mod.unwrap(r))).rejects.toThrow(
      'Request failed (HTTP 502).',
    );
  });

  it('throws on a 2xx that carries no body', async () => {
    const { mod } = await loadClient(() => new Response(null, { status: 204 }));

    // A caller reaching for `unwrap` wants a value; handing back
    // `undefined` typed as `T` is the failure that shows up three
    // components later.
    await expect(mod.api.GET('/api/v1/me').then((r) => mod.unwrap(r))).rejects.toBeInstanceOf(
      mod.ApiError,
    );
  });
});

describe('unwrapEmpty', () => {
  it('accepts a 204 with no body', async () => {
    const { mod } = await loadClient(() => new Response(null, { status: 204 }));

    expect(() => {
      mod.unwrapEmpty({ response: new Response(null, { status: 204 }) });
    }).not.toThrow();
    await expect(
      mod.api.DELETE('/api/v1/me', {}).then((r) => {
        mod.unwrapEmpty(r);
      }),
    ).resolves.toBeUndefined();
  });

  it('throws with the status when the delete was refused', async () => {
    const { mod } = await loadClient(() => json({ detail: 'CSRF token missing.' }, 403));

    await expect(
      mod.api.DELETE('/api/v1/me', {}).then((r) => {
        mod.unwrapEmpty(r);
      }),
    ).rejects.toThrow('CSRF token missing.');
  });
});

describe('toApiError', () => {
  it('normalises a network-level failure to status 0', async () => {
    const { mod } = await loadClient(() => {
      // What `fetch` rejects with when the host is unreachable, DNS fails,
      // or the connection is refused. There is no response and no status.
      throw new TypeError('Failed to fetch');
    });

    const error = await mod.api.GET('/api/v1/me').then(
      () => null,
      (cause: unknown) => mod.toApiError(cause),
    );

    expect(error).toBeInstanceOf(mod.ApiError);
    expect(error?.status).toBe(0);
    expect(error?.message).toBe('Could not reach the server. Check your connection and try again.');
  });

  it('distinguishes a cancelled request from an unreachable server', async () => {
    const { mod } = await loadClient(() => {
      const abort = new Error('The operation was aborted.');
      abort.name = 'AbortError';
      throw abort;
    });

    const error = await mod.api.GET('/api/v1/me').then(
      () => null,
      (cause: unknown) => mod.toApiError(cause),
    );

    // Both are status 0, so only the message separates "you navigated
    // away" from "the server is down" — and one of those is worth showing.
    expect(error?.status).toBe(0);
    expect(error?.message).toBe('Request cancelled.');
  });

  it('passes an ApiError straight through rather than re-wrapping it', async () => {
    const { mod } = await loadClient(ok);
    const original = new mod.ApiError(401, 'Not signed in.', { detail: 'Not signed in.' });

    // `guard` in endpoints.ts wraps every call, so an HTTP error thrown by
    // `unwrap` passes back through here. Re-wrapping would flatten every
    // status to 0 and `isUnauthorised` would never be true again.
    expect(mod.toApiError(original)).toBe(original);
  });

  it('normalises something that is not an Error at all', async () => {
    const { mod } = await loadClient(ok);

    expect(mod.toApiError('a string nobody expected').status).toBe(0);
  });
});

describe('ApiError', () => {
  it('reports 401 and 501 by name and nothing else', async () => {
    const { mod } = await loadClient(ok);

    expect(new mod.ApiError(401, 'x').isUnauthorised).toBe(true);
    expect(new mod.ApiError(403, 'x').isUnauthorised).toBe(false);
    expect(new mod.ApiError(501, 'x').isNotImplemented).toBe(true);
    expect(new mod.ApiError(500, 'x').isNotImplemented).toBe(false);
    expect(new mod.ApiError(500, 'x').name).toBe('ApiError');
  });
});

// --- the 401 broadcast --------------------------------------------------

describe('onUnauthorised', () => {
  it('notifies every listener when any response comes back 401', async () => {
    const { mod } = await loadClient(() => json({ detail: 'Not signed in.' }, 401));
    const first = vi.fn();
    const second = vi.fn();
    mod.onUnauthorised(first);
    mod.onUnauthorised(second);

    await mod.api.GET('/api/v1/me');

    // Fired from the middleware, so it does not depend on the caller
    // reaching `unwrap` — a screen that swallows its own error still
    // drops the session.
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('stays silent on every other status', async () => {
    for (const status of [200, 403, 404, 422, 500]) {
      const { mod } = await loadClient(() => json({ detail: 'no' }, status));
      const listener = vi.fn();
      mod.onUnauthorised(listener);

      await mod.api.GET('/api/v1/me');

      // 403 is the CSRF refusal. Treating it as a sign-out would log the
      // user out of a session that is still perfectly valid.
      expect(listener, `status ${String(status)}`).not.toHaveBeenCalled();
    }
  });

  it('stops notifying once unsubscribed', async () => {
    const { mod } = await loadClient(() => json({ detail: 'Not signed in.' }, 401));
    const listener = vi.fn();
    const unsubscribe = mod.onUnauthorised(listener);

    await mod.api.GET('/api/v1/me');
    expect(listener).toHaveBeenCalledTimes(1);

    unsubscribe();
    await mod.api.GET('/api/v1/me');
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

// --- the consequence of the readCookie gap above ------------------------

describe('a malformed CSRF cookie', () => {
  // Marked `.fails` for the same reason as the `readCookie` case above,
  // and recorded separately because this is the half a user would notice:
  // the `URIError` escapes the request middleware, so `guard` in
  // endpoints.ts normalises it to status 0, "Could not reach the server".
  // Every write in the app then reports a network outage that is not
  // happening, and no request is ever sent — indistinguishable, from the
  // screen, from being offline.
  it.fails('does not stop a mutating request from being sent', async () => {
    const { mod, sent } = await loadClient(ok);
    withCookie('csrftoken=100%-genuine');

    await mod.api.POST('/api/v1/auth/logout', {});

    expect(sent).toHaveLength(1);
  });
});
