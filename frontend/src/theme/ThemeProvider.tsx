import { useEffect, type ReactNode } from 'react';

import type { Theme } from '../api/types';
import { applyTheme, rememberChoice, resolveTheme, subscribeToSystemTheme } from './theme';

interface ThemeProviderProps {
  /** The user's stored choice, or `system` until `GET /me` resolves. */
  choice: Theme;
  children: ReactNode;
}

/**
 * Applies the theme choice to the document. `public/theme-init.js` has
 * already painted the remembered choice; this keeps it in step once the
 * server profile arrives and follows the OS when the choice is `system`.
 */
export function ThemeProvider({ choice, children }: ThemeProviderProps) {
  useEffect(() => {
    applyTheme(resolveTheme(choice));
    rememberChoice(choice);
    if (choice !== 'system') return undefined;
    return subscribeToSystemTheme(() => {
      applyTheme(resolveTheme('system'));
    });
  }, [choice]);

  return children;
}
