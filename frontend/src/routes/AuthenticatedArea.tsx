import { Navigate, Outlet, useLocation } from 'react-router-dom';

import { GITHUB_SIGN_IN_PATH } from '../api/endpoints';
import { CatalogueProvider } from '../catalogue/CatalogueProvider';
import { GitHubIcon } from '../components/icons';
import { ErrorState, LoadingState } from '../components/States';
import { useSession } from '../session/useSession';

/**
 * The auth guard. A 401 anywhere in the app flips the session to
 * `anonymous`, which lands the user back on `/` from wherever they were.
 */
export function AuthenticatedArea() {
  const { status, preferences, error, reload } = useSession();
  const location = useLocation();

  if (status === 'loading') {
    return (
      <div className="standalone">
        <LoadingState label="Checking your session" />
      </div>
    );
  }

  if (status === 'anonymous') {
    return <Navigate to="/" replace state={{ from: location.pathname }} />;
  }

  if (status === 'error' || !preferences) {
    return (
      <div className="standalone">
        {error ? <ErrorState error={error} onRetry={reload} what="your profile" /> : null}
        <p className="standalone__fallback">
          <a className="button" href={GITHUB_SIGN_IN_PATH}>
            <GitHubIcon />
            Sign in again
          </a>
        </p>
      </div>
    );
  }

  const onboarding = location.pathname === '/onboarding';
  if (!preferences.onboarding_completed && !onboarding) {
    return <Navigate to="/onboarding" replace />;
  }
  if (preferences.onboarding_completed && onboarding) {
    return <Navigate to="/feed" replace />;
  }

  return (
    <CatalogueProvider>
      <Outlet />
    </CatalogueProvider>
  );
}
