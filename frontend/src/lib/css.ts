import type { CSSProperties } from 'react';

/**
 * React writes the `style` prop through the CSSOM, not the `style`
 * attribute, so this stays legal under the app's `style-src 'self'` CSP.
 * The cast is only needed because `CSSProperties` has no index signature
 * for custom properties.
 */
export function cssVars(vars: Record<`--${string}`, string | number>): CSSProperties {
  return vars;
}
