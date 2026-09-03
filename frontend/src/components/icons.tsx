/**
 * Inline SVG rather than sprite files or an icon package: no extra
 * requests, no `img-src`/`data:` dependency, and `currentColor` keeps
 * every icon correct in both themes for free.
 */
interface IconProps {
  className?: string;
}

function base(className?: string) {
  return {
    className: className ? `icon ${className}` : 'icon',
    viewBox: '0 0 16 16',
    width: 16,
    height: 16,
    fill: 'none' as const,
    stroke: 'currentColor',
    strokeWidth: 1.5,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    focusable: 'false' as const,
  };
}

export function BookmarkIcon({ filled = false, className }: IconProps & { filled?: boolean }) {
  return (
    <svg {...base(className)} fill={filled ? 'currentColor' : 'none'}>
      <path d="M4 2.5h8v11l-4-3-4 3z" />
    </svg>
  );
}

export function CheckIcon({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="m3 8.5 3.5 3.5L13 4" />
    </svg>
  );
}

export function FilterIcon({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M2 3.5h12L9.5 9v4.5l-3-1.5V9z" />
    </svg>
  );
}

export function GitHubIcon({ className }: IconProps) {
  return (
    <svg
      className={className ? `icon ${className}` : 'icon'}
      viewBox="0 0 16 16"
      width={18}
      height={18}
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M8 0C3.58 0 0 3.58 0 8a8 8 0 0 0 5.47 7.59c.4.07.55-.17.55-.38v-1.33c-2.23.49-2.7-1.07-2.7-1.07-.36-.93-.89-1.18-.89-1.18-.73-.5.05-.49.05-.49.81.06 1.24.83 1.24.83.72 1.23 1.88.87 2.34.67.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 4 0c1.53-1.03 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.28.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48v2.2c0 .21.15.46.55.38A8 8 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

/** Points down when the region it controls is open, and the CSS rotates it
 *  from the button's own `aria-expanded` rather than from a second prop. */
export function ChevronIcon({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="m3.5 6 4.5 4.5L12.5 6" />
    </svg>
  );
}

export function CrossIcon({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="m4 4 8 8M12 4l-8 8" />
    </svg>
  );
}

export function SearchIcon({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <circle cx="7" cy="7" r="4.5" />
      <path d="m10.5 10.5 3 3" />
    </svg>
  );
}
