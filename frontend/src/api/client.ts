/**
 * The one place that talks to the network.
 *
 * Auth is a same-origin `HttpOnly` session cookie: the browser carries it,
 * no token is ever read into or stored by JavaScript. Mutating requests
 * additionally echo the signed double-submit CSRF cookie into a header —
 * the cookie is deliberately *not* `HttpOnly` so this file can read it,
 * which is the whole mechanism.
 *
 * Names default to the server's own defaults (`CSRF_COOKIE_NAME` and
 * `CSRF_HEADER_NAME` in `.env.example`) and are overridable at build time
 * only for the case where an operator has changed them server-side.
 */
import createClient, { type Middleware } from 'openapi-fetch';

import type { paths } from './schema';

export const CSRF_COOKIE_NAME = import.meta.env.VITE_CSRF_COOKIE_NAME || 'csrftoken';
export const CSRF_HEADER_NAME = import.meta.env.VITE_CSRF_HEADER_NAME || 'X-CSRF-Token';

const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const candidate = part.trimStart();
    if (candidate.startsWith(prefix)) {
      return decodeURIComponent(candidate.slice(prefix.length));
    }
  }
  return null;
}

/** Subscribers notified whenever any response comes back 401. */
type UnauthorisedListener = () => void;
const unauthorisedListeners = new Set<UnauthorisedListener>();

export function onUnauthorised(listener: UnauthorisedListener): () => void {
  unauthorisedListeners.add(listener);
  return () => {
    unauthorisedListeners.delete(listener);
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }

  get isUnauthorised(): boolean {
    return this.status === 401;
  }

  /** True for the 501s the stub routes return before the backend lands. */
  get isNotImplemented(): boolean {
    return this.status === 501;
  }
}

const csrfMiddleware: Middleware = {
  onRequest({ request }) {
    if (!MUTATING_METHODS.has(request.method.toUpperCase())) return undefined;
    const token = readCookie(CSRF_COOKIE_NAME);
    if (token) request.headers.set(CSRF_HEADER_NAME, token);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) {
      for (const listener of unauthorisedListeners) listener();
    }
    return undefined;
  },
};

export const api = createClient<paths>({
  // Same-origin in every deployment; the generated paths already carry the
  // `/api/v1` prefix, so there is no base URL to configure.
  credentials: 'same-origin',
  headers: { Accept: 'application/json' },
});

api.use(csrfMiddleware);

interface ValidationDetail {
  loc?: (string | number)[];
  msg?: string;
}

function describe(status: number, body: unknown): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const { detail } = body;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      const parts = (detail as ValidationDetail[])
        .map((entry) => entry.msg)
        .filter((msg): msg is string => typeof msg === 'string');
      if (parts.length > 0) return parts.join('; ');
    }
  }
  if (status === 501) return 'This part of the API is not implemented yet.';
  if (status === 0) return 'Could not reach the server.';
  return `Request failed (HTTP ${status}).`;
}

export interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Unwrap a response that carries a body, throwing `ApiError` otherwise. */
export function unwrap<T>(result: FetchResult<T>): T {
  if (!result.response.ok || result.data === undefined) {
    throw new ApiError(
      result.response.status,
      describe(result.response.status, result.error),
      result.error,
    );
  }
  return result.data;
}

/** Unwrap a 204-style response that carries no body. */
export function unwrapEmpty(result: FetchResult<unknown>): void {
  if (!result.response.ok) {
    throw new ApiError(
      result.response.status,
      describe(result.response.status, result.error),
      result.error,
    );
  }
}

/** Network failures surface as thrown `TypeError`s; normalise them. */
export function toApiError(cause: unknown): ApiError {
  if (cause instanceof ApiError) return cause;
  if (cause instanceof Error && cause.name === 'AbortError') {
    return new ApiError(0, 'Request cancelled.');
  }
  return new ApiError(0, 'Could not reach the server. Check your connection and try again.');
}
