/**
 * Which build is running, from what the image build baked in.
 *
 * The Containerfile passes CI's commit and repository URL to `vite build` as
 * `VITE_SRE_TAB_COMMIT` and `VITE_SRE_TAB_SOURCE_URL`, and Vite writes them
 * into the bundle as literals. A build that was given neither — `npm run
 * dev`, or a `podman build` with no arguments — has no commit to report and
 * says so, rather than presenting a guess as a fact.
 *
 * Both values are checked before use even though they come from our own
 * build: the commit is rendered as a link, and a link's target is exactly
 * the kind of value this frontend does not take on trust.
 */

export interface BuildEnv {
  readonly VITE_SRE_TAB_COMMIT?: string;
  readonly VITE_SRE_TAB_SOURCE_URL?: string;
}

export interface BuildInfo {
  /** `package.json`'s version, held to `pyproject.toml`'s by tests/test_version_parity.py. */
  version: string;
  /** The full commit, or `null` when the build was not told one. */
  commit: string | null;
  /** The first seven characters, as `git log --oneline` prints them. */
  shortCommit: string | null;
  /** The commit's page in its repository, or `null` if either half is unusable. */
  commitUrl: string | null;
}

const FULL_COMMIT = /^[0-9a-f]{40}$/;

export function buildInfo(env: BuildEnv, version: string): BuildInfo {
  const raw = (env.VITE_SRE_TAB_COMMIT || '').trim().toLowerCase();
  const commit = FULL_COMMIT.test(raw) ? raw : null;
  return {
    version,
    commit,
    shortCommit: commit ? commit.slice(0, 7) : null,
    commitUrl: commit ? commitPage(env.VITE_SRE_TAB_SOURCE_URL, commit) : null,
  };
}

function commitPage(source: string | undefined, commit: string): string | null {
  if (!source) {
    return null;
  }
  let url: URL;
  try {
    url = new URL(source);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
    return null;
  }
  return `${url.origin}${url.pathname.replace(/\/+$/, '')}/commit/${commit}`;
}

export const BUILD: BuildInfo = buildInfo(import.meta.env, __APP_VERSION__);
