import { useContext } from 'react';

import { SessionContext, type SessionValue } from './SessionProvider';

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error('useSession must be used inside <SessionProvider>');
  return value;
}

/** Convenience for screens behind the auth guard, where these are non-null. */
export function useAuthenticatedSession() {
  const session = useSession();
  if (!session.user || !session.preferences) {
    throw new Error('useAuthenticatedSession used outside an authenticated route');
  }
  return { ...session, user: session.user, preferences: session.preferences };
}
