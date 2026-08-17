import { Navigate } from 'react-router-dom';

import { GITHUB_SIGN_IN_PATH } from '../api/endpoints';
import { GitHubIcon } from '../components/icons';
import { ErrorState, LoadingState } from '../components/States';
import { useSession } from '../session/useSession';

export function Landing() {
  const { status, preferences, error, reload } = useSession();

  if (status === 'loading') {
    return (
      <div className="landing">
        <LoadingState label="Checking your session" />
      </div>
    );
  }

  if (status === 'authenticated') {
    return <Navigate to={preferences?.onboarding_completed ? '/feed' : '/onboarding'} replace />;
  }

  return (
    <div className="landing">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <main id="main" className="landing__panel" tabIndex={-1}>
        <h1 className="landing__title">
          <span className="brand__mark" aria-hidden="true" />
          Developer News Dashboard
        </h1>

        <p className="landing__lede">
          One private place for the technology and news feeds you actually read. Pick your topics and
          sources once; your selection, bookmarks, and read state live on this server and nowhere else.
        </p>

        <ul className="landing__points">
          <li>Sources are configured by the operator and fetched on a schedule — no arbitrary URLs.</li>
          <li>No ads, no tracking, no recommendation engine, no telemetry.</li>
          <li>Your preferences follow you between browsers because they are stored server-side.</li>
        </ul>

        {status === 'error' && error ? (
          <div className="landing__error">
            <ErrorState error={error} onRetry={reload} what="your session" />
          </div>
        ) : null}

        <p className="landing__cta">
          {/* A full navigation, not a fetch: the server needs to set the OAuth
              state cookie and redirect to GitHub. */}
          <a className="button button--primary button--large" href={GITHUB_SIGN_IN_PATH}>
            <GitHubIcon />
            Sign in with GitHub
          </a>
        </p>

        <p className="landing__note">
          Sign-in is restricted to GitHub accounts the operator has allow-listed. Nothing is shared with
          GitHub beyond the standard OAuth exchange, and the access token never reaches your browser.
        </p>
      </main>
    </div>
  );
}
