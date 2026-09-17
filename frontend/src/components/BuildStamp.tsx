import { BUILD, type BuildInfo } from '../lib/build';

/**
 * The version and commit, for the footer. The commit links to its page when
 * the build knows where its source lives; `noreferrer` so following it does
 * not tell the code host which instance the reader came from.
 */
export function BuildStamp({ info = BUILD }: { info?: BuildInfo }) {
  let commit;
  if (info.commit && info.shortCommit) {
    commit = info.commitUrl ? (
      <a href={info.commitUrl} title={info.commit} rel="noopener noreferrer">
        {info.shortCommit}
      </a>
    ) : (
      <span title={info.commit}>{info.shortCommit}</span>
    );
  } else {
    commit = <span>development build</span>;
  }

  return (
    <p className="build-stamp">
      sre-tab {info.version} · {commit}
    </p>
  );
}
