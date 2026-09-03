/**
 * API tokens, on the Settings page.
 *
 * The screen has one unusual constraint and everything here follows from
 * it: the server keeps only a hash, so the raw token exists for exactly
 * one render. It is held in component state, shown once with a copy
 * button, and dismissed — never written to `localStorage`, never put in
 * the URL, and never re-fetched, because there is nothing to re-fetch.
 * The panel says so in as many words, since a user who closes it without
 * copying has lost the token and their only option is to mint another.
 *
 * Everything else is a list with a revoke button. `last_used_at` is the
 * reason the list is worth having at all: a token nobody has presented
 * in six months is the one to revoke, and without that column the list
 * would be a set of labels the user no longer recognises.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import { createApiToken, fetchApiTokens, revokeApiToken } from '../api/endpoints';
import type { ApiToken, ApiTokenScope } from '../api/types';
import { ErrorState, LoadingState } from '../components/States';
import { formatAbsolute, formatRelative } from '../lib/format';

const SCOPES: { value: ApiTokenScope; label: string; hint: string }[] = [
  { value: 'read', label: 'Read only', hint: 'Safe methods. Cannot change anything.' },
  { value: 'full', label: 'Full access', hint: 'Everything your account can do.' },
];

type Status = 'loading' | 'ready' | 'error';

export function ApiTokensSection() {
  const [status, setStatus] = useState<Status>('loading');
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [issued, setIssued] = useState<string | null>(null);
  // Captured when the list arrives rather than read during render:
  // "is this expired?" has to be a value the render is given, not a
  // clock it consults, or the answer changes under an unrelated repaint.
  const [loadedAt, setLoadedAt] = useState(() => Date.now());
  const [reloadToken, setReloadToken] = useState(0);

  // Reload by bumping a counter, as CatalogueProvider does, rather than
  // by calling a loader from the effect body.
  useEffect(() => {
    const controller = new AbortController();
    fetchApiTokens(controller.signal)
      .then((list) => {
        if (controller.signal.aborted) return;
        setTokens(list);
        setLoadedAt(Date.now());
        setLoadError(null);
        setStatus('ready');
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
        setStatus('error');
      });
    return () => {
      controller.abort();
    };
  }, [reloadToken]);

  const load = useCallback(() => {
    setStatus('loading');
    setReloadToken((value) => value + 1);
  }, []);

  return (
    <section className="settings__section" aria-labelledby="settings-tokens">
      <h2 id="settings-tokens">API tokens</h2>
      <p className="settings__hint">
        A token lets another application call this API as you — a script, a status board, a
        terminal. Send it as <code>Authorization: Bearer …</code>. Read-only is the right choice
        for anything that only displays your feed.
      </p>

      {issued ? (
        <IssuedToken
          value={issued}
          onDismiss={() => {
            setIssued(null);
          }}
        />
      ) : null}

      <CreateTokenForm
        onCreated={(value) => {
          setIssued(value);
          load();
        }}
      />

      {status === 'loading' ? <LoadingState label="Loading your tokens" /> : null}
      {status === 'error' && loadError ? (
        <ErrorState error={loadError} onRetry={load} what="your tokens" />
      ) : null}
      {status === 'ready' ? (
        <TokenList tokens={tokens} now={loadedAt} onRevoked={load} />
      ) : null}
    </section>
  );
}

function IssuedToken({ value, onDismiss }: { value: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
    },
    [],
  );

  const copy = () => {
    // `navigator.clipboard` is absent on an insecure origin and can be
    // refused by permissions policy, so the failure path leaves the value
    // on screen to be selected by hand rather than reporting success.
    // The indirection through `Promise.resolve` is what turns the absent
    // case — a synchronous TypeError on the property access — into the
    // same rejection a refused permission produces.
    Promise.resolve()
      .then(() => navigator.clipboard.writeText(value))
      .then(() => {
        setCopied(true);
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => {
          setCopied(false);
        }, 3000);
      })
      .catch(() => {
        setCopied(false);
      });
  };

  return (
    <div className="token-reveal" role="alert">
      <h3>Copy this token now</h3>
      <p>
        This is the only time it is shown. Only a hash of it is stored, so it cannot be retrieved
        again — if you lose it, revoke it and create another.
      </p>
      <p className="token-reveal__value">
        <code>{value}</code>
      </p>
      <div className="settings__account-actions">
        <button type="button" className="button button--primary" onClick={copy}>
          {copied ? 'Copied' : 'Copy to clipboard'}
        </button>
        <button type="button" className="button button--quiet" onClick={onDismiss}>
          I have saved it
        </button>
      </div>
    </div>
  );
}

function CreateTokenForm({ onCreated }: { onCreated: (value: string) => void }) {
  const [label, setLabel] = useState('');
  const [scope, setScope] = useState<ApiTokenScope>('read');
  const [expires, setExpires] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const submit = () => {
    setBusy(true);
    setError(null);
    const days = Number.parseInt(expires, 10);
    createApiToken({
      label: label.trim(),
      scope,
      expires_in_days: Number.isNaN(days) ? null : days,
    })
      .then((created) => {
        setLabel('');
        setExpires('');
        setScope('read');
        onCreated(created.value);
      })
      .catch((cause: unknown) => {
        setError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <form
      className="token-form"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      {error ? (
        <p className="banner banner--error" role="alert">
          {error.message}
        </p>
      ) : null}

      <div className="settings__field">
        <label className="settings__label" htmlFor="token-label">
          What is it for?
        </label>
        <input
          id="token-label"
          className="input"
          type="text"
          value={label}
          maxLength={100}
          required
          autoComplete="off"
          placeholder="Status board on the office screen"
          onChange={(event) => {
            setLabel(event.target.value);
          }}
        />
      </div>

      <fieldset className="settings__field">
        <legend>Access</legend>
        <div className="radio-row">
          {SCOPES.map((option) => (
            <label key={option.value} className="option option--radio">
              <input
                type="radio"
                name="token-scope"
                value={option.value}
                checked={scope === option.value}
                onChange={() => {
                  setScope(option.value);
                }}
              />
              <span className="option__label">{option.label}</span>
              <span className="option__hint">{option.hint}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="settings__field">
        <label className="settings__label" htmlFor="token-expiry">
          Expires after
        </label>
        <input
          id="token-expiry"
          className="input input--number"
          type="number"
          min={1}
          max={3650}
          step={1}
          value={expires}
          aria-describedby="token-expiry-hint"
          onChange={(event) => {
            setExpires(event.target.value);
          }}
        />
        <p id="token-expiry-hint" className="settings__hint">
          Days. Leave empty for a token that does not expire.
        </p>
      </div>

      <button
        type="submit"
        className="button button--primary"
        disabled={busy || label.trim().length === 0}
      >
        {busy ? 'Creating…' : 'Create token'}
      </button>
    </form>
  );
}

function TokenList({
  tokens,
  now,
  onRevoked,
}: {
  tokens: ApiToken[];
  now: number;
  onRevoked: () => void;
}) {
  if (tokens.length === 0) {
    return <p className="settings__hint">You have no API tokens.</p>;
  }
  return (
    <ul className="token-list">
      {tokens.map((token) => (
        <TokenRow key={token.id} token={token} now={now} onRevoked={onRevoked} />
      ))}
    </ul>
  );
}

function TokenRow({
  token,
  now,
  onRevoked,
}: {
  token: ApiToken;
  now: number;
  onRevoked: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const expired = token.expires_at !== null && Date.parse(token.expires_at) <= now;

  const revoke = () => {
    setBusy(true);
    setError(null);
    revokeApiToken(token.id)
      .then(onRevoked)
      .catch((cause: unknown) => {
        setError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
        setBusy(false);
      });
  };

  return (
    <li className="token-list__row">
      <div>
        <p className="token-list__label">
          <strong>{token.label}</strong>{' '}
          <span className="chip__flag chip__flag--inline">
            {token.scope === 'read' ? 'read only' : 'full access'}
          </span>
          {expired ? <span className="chip__flag chip__flag--inline">expired</span> : null}
        </p>
        <p className="settings__hint">
          <code>{token.display_prefix}…</code> · created{' '}
          <time dateTime={token.created_at} title={formatAbsolute(token.created_at)}>
            {formatRelative(token.created_at)}
          </time>{' '}
          ·{' '}
          {token.last_used_at ? (
            <>
              last used{' '}
              <time dateTime={token.last_used_at} title={formatAbsolute(token.last_used_at)}>
                {formatRelative(token.last_used_at)}
              </time>
            </>
          ) : (
            'never used'
          )}
          {token.expires_at ? (
            <>
              {' '}
              · {expired ? 'expired' : 'expires'}{' '}
              <time dateTime={token.expires_at} title={formatAbsolute(token.expires_at)}>
                {formatRelative(token.expires_at)}
              </time>
            </>
          ) : null}
        </p>
        {error ? (
          <p className="banner banner--error" role="alert">
            {error.message}
          </p>
        ) : null}
      </div>

      {confirming ? (
        <div className="settings__account-actions">
          <button type="button" className="button button--danger" disabled={busy} onClick={revoke}>
            Revoke permanently
          </button>
          <button
            type="button"
            className="button button--quiet"
            onClick={() => {
              setConfirming(false);
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="button button--quiet"
          onClick={() => {
            setConfirming(true);
          }}
        >
          Revoke
        </button>
      )}
    </li>
  );
}
