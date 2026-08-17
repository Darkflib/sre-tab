const relative = new Intl.RelativeTimeFormat('en-GB', { numeric: 'auto' });
const absolute = new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short' });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 365 * 24 * 60 * 60 * 1000],
  ['month', 30 * 24 * 60 * 60 * 1000],
  ['week', 7 * 24 * 60 * 60 * 1000],
  ['day', 24 * 60 * 60 * 1000],
  ['hour', 60 * 60 * 1000],
  ['minute', 60 * 1000],
];

export function formatRelative(iso: string, now: number = Date.now()): string {
  const timestamp = Date.parse(iso);
  if (Number.isNaN(timestamp)) return '';
  const delta = timestamp - now;
  for (const [unit, span] of UNITS) {
    if (Math.abs(delta) >= span) return relative.format(Math.round(delta / span), unit);
  }
  return 'just now';
}

export function formatAbsolute(iso: string): string {
  const timestamp = Date.parse(iso);
  if (Number.isNaN(timestamp)) return '';
  return absolute.format(timestamp);
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}
