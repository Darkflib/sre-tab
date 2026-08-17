import { useMemo } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { AppShell } from './components/AppShell';
import { AuthenticatedArea } from './routes/AuthenticatedArea';
import { BookmarksPage } from './routes/BookmarksPage';
import { FeedPage } from './routes/FeedPage';
import { Landing } from './routes/Landing';
import { Onboarding } from './routes/Onboarding';
import { SettingsPage } from './routes/SettingsPage';
import { useSession } from './session/useSession';
import { ThemeProvider } from './theme/ThemeProvider';
import { readRememberedChoice } from './theme/theme';

export function App() {
  const { preferences } = useSession();
  // The server profile is authoritative; until it arrives, the remembered
  // choice is what `theme-init.js` already painted with.
  const remembered = useMemo(() => readRememberedChoice(), []);

  return (
    <ThemeProvider choice={preferences?.theme ?? remembered}>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route element={<AuthenticatedArea />}>
          <Route path="/onboarding" element={<Onboarding />} />
          <Route element={<AppShell />}>
            <Route path="/feed" element={<FeedPage />} />
            <Route path="/bookmarks" element={<BookmarksPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ThemeProvider>
  );
}
