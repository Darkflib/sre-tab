import type { ReactNode } from 'react';

import type { ApiError } from '../api/client';

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <span className="spinner" role="status">
      <span className="spinner__dot" />
      <span className="visually-hidden">{label}</span>
    </span>
  );
}

export function LoadingState({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="state state--loading">
      <Spinner label={label} />
      <p aria-hidden="true">{label}…</p>
    </div>
  );
}

interface ErrorStateProps {
  error: ApiError;
  onRetry?: () => void;
  /** Context for the reader, e.g. "the feed". */
  what?: string;
}

export function ErrorState({ error, onRetry, what = 'this page' }: ErrorStateProps) {
  const pending = error.isNotImplemented;
  return (
    <div className="state state--error" role="alert">
      <h2 className="state__title">{pending ? 'Not available yet' : `Could not load ${what}`}</h2>
      <p className="state__body">
        {pending
          ? 'The server is running but this endpoint has not been implemented yet. Nothing is broken on your side.'
          : error.message}
      </p>
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

interface EmptyStateProps {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}

export function EmptyState({ title, children, action }: EmptyStateProps) {
  return (
    <div className="state state--empty">
      <h2 className="state__title">{title}</h2>
      <div className="state__body">{children}</div>
      {action ? <div className="state__action">{action}</div> : null}
    </div>
  );
}
