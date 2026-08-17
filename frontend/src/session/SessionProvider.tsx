import { createContext, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { ApiError, onUnauthorised } from '../api/client';
import { deleteAccount, fetchMe, logout, patchPreferences } from '../api/endpoints';
import type { Preferences, PreferencesPatch, User } from '../api/types';

export type SessionStatus = 'loading' | 'authenticated' | 'anonymous' | 'error';

export interface SessionValue {
  status: SessionStatus;
  user: User | null;
  preferences: Preferences | null;
  error: ApiError | null;
  reload: () => void;
  /** Partial update; absent fields are left untouched server-side. */
  updatePreferences: (patch: PreferencesPatch) => Promise<void>;
  signOut: () => Promise<void>;
  removeAccount: () => Promise<void>;
}

export const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>('loading');
  const [user, setUser] = useState<User | null>(null);
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const mounted = useRef(true);
  // Read by the optimistic-update callback, which must not close over a
  // render-scoped copy of the preferences.
  const preferencesRef = useRef<Preferences | null>(null);

  useEffect(() => {
    preferencesRef.current = preferences;
  }, [preferences]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchMe(controller.signal)
      .then((me) => {
        if (controller.signal.aborted) return;
        setUser(me.user);
        setPreferences(me.preferences);
        setError(null);
        setStatus('authenticated');
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        const apiError = cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.');
        setUser(null);
        setPreferences(null);
        if (apiError.isUnauthorised) {
          setError(null);
          setStatus('anonymous');
        } else {
          setError(apiError);
          setStatus('error');
        }
      });
    return () => {
      controller.abort();
    };
  }, [reloadToken]);

  const reload = useCallback(() => {
    setStatus('loading');
    setReloadToken((value) => value + 1);
  }, []);

  // Any 401 anywhere in the app drops us back to the landing page.
  useEffect(
    () =>
      onUnauthorised(() => {
        if (!mounted.current) return;
        setUser(null);
        setPreferences(null);
        setError(null);
        setStatus('anonymous');
      }),
    [],
  );

  const updatePreferences = useCallback(async (patch: PreferencesPatch) => {
    // Optimistic so theme and layout switch instantly; reconciled with the
    // server's authoritative response, rolled back on failure.
    const previous = preferencesRef.current;
    if (previous) setPreferences({ ...previous, ...defined(patch) });
    try {
      const updated = await patchPreferences(patch);
      setPreferences(updated);
    } catch (cause) {
      setPreferences(previous);
      throw cause;
    }
  }, []);

  const signOut = useCallback(async () => {
    await logout();
    setUser(null);
    setPreferences(null);
    setError(null);
    setStatus('anonymous');
  }, []);

  const removeAccount = useCallback(async () => {
    await deleteAccount();
    setUser(null);
    setPreferences(null);
    setError(null);
    setStatus('anonymous');
  }, []);

  const value = useMemo<SessionValue>(
    () => ({
      status,
      user,
      preferences,
      error,
      reload,
      updatePreferences,
      signOut,
      removeAccount,
    }),
    [status, user, preferences, error, reload, updatePreferences, signOut, removeAccount],
  );

  return <SessionContext value={value}>{children}</SessionContext>;
}

/**
 * A patch omits the fields it does not change, so only the fields actually
 * present take part in the optimistic merge.
 */
function defined(patch: PreferencesPatch): Partial<Preferences> {
  const result: Partial<Preferences> = {};
  if (patch.theme != null) result.theme = patch.theme;
  if (patch.layout != null) result.layout = patch.layout;
  if (patch.max_visible_cards != null) result.max_visible_cards = patch.max_visible_cards;
  if (patch.onboarding_completed != null) result.onboarding_completed = patch.onboarding_completed;
  if (patch.topics != null) result.topics = patch.topics;
  if (patch.sources != null) result.sources = patch.sources;
  return result;
}
