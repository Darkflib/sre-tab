import { NavLink, Outlet } from 'react-router-dom';

import { useSession } from '../session/useSession';

const NAV = [
  { to: '/feed', label: 'Feed' },
  { to: '/bookmarks', label: 'Bookmarks' },
  { to: '/settings', label: 'Settings' },
];

export function AppShell() {
  const { user } = useSession();

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="shell__header">
        <div className="shell__bar">
          <span className="brand">
            <span className="brand__mark" aria-hidden="true" />
            <span className="brand__name">News Dashboard</span>
          </span>

          <nav className="nav" aria-label="Primary">
            <ul className="nav__list">
              {NAV.map((entry) => (
                <li key={entry.to}>
                  <NavLink
                    to={entry.to}
                    className={({ isActive }) => (isActive ? 'nav__link nav__link--active' : 'nav__link')}
                  >
                    {entry.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>

          {user ? (
            <p className="shell__user">
              {user.avatar_url ? (
                <img
                  className="shell__avatar"
                  src={user.avatar_url}
                  alt=""
                  width={24}
                  height={24}
                  referrerPolicy="no-referrer"
                />
              ) : null}
              <span>{user.display_name || user.github_login}</span>
            </p>
          ) : null}
        </div>
      </header>

      <main id="main" className="shell__main" tabIndex={-1}>
        <Outlet />
      </main>

      <footer className="shell__footer">
        <p>Self-hosted. No analytics, no third-party requests beyond article images.</p>
      </footer>
    </div>
  );
}
