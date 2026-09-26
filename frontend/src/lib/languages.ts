/**
 * Every language code the server's detector can emit — `LANGUAGE_CODES` in
 * `app/ingest/language.py`, which is fastText's `lid.176` label set.
 *
 * A copy, and held to the original rather than trusted:
 * `tests/test_frontend_languages.py` reads this block and fails if the two
 * differ. The server refuses a code outside its set, so a code only here
 * would be a choice the Settings page offers and the save rejects, and a
 * code only there would be a language nobody can pick.
 */
export const LANGUAGE_CODES: readonly string[] = [
  // BEGIN LANGUAGE_CODES
  'af', 'als', 'am', 'an', 'ar', 'arz', 'as', 'ast', 'av', 'az', 'azb', 'ba', 'bar', 'bcl', 'be',
  'bg', 'bh', 'bn', 'bo', 'bpy', 'br', 'bs', 'bxr', 'ca', 'cbk', 'ce', 'ceb', 'ckb', 'co', 'cs',
  'cv', 'cy', 'da', 'de', 'diq', 'dsb', 'dty', 'dv', 'el', 'eml', 'en', 'eo', 'es', 'et', 'eu',
  'fa', 'fi', 'fr', 'frr', 'fy', 'ga', 'gd', 'gl', 'gn', 'gom', 'gu', 'gv', 'he', 'hi', 'hif',
  'hr', 'hsb', 'ht', 'hu', 'hy', 'ia', 'id', 'ie', 'ilo', 'io', 'is', 'it', 'ja', 'jbo', 'jv',
  'ka', 'kk', 'km', 'kn', 'ko', 'krc', 'ku', 'kv', 'kw', 'ky', 'la', 'lb', 'lez', 'li', 'lmo',
  'lo', 'lrc', 'lt', 'lv', 'mai', 'mg', 'mhr', 'min', 'mk', 'ml', 'mn', 'mr', 'mrj', 'ms', 'mt',
  'mwl', 'my', 'myv', 'mzn', 'nah', 'nap', 'nds', 'ne', 'new', 'nl', 'nn', 'no', 'oc', 'or',
  'os', 'pa', 'pam', 'pfl', 'pl', 'pms', 'pnb', 'ps', 'pt', 'qu', 'rm', 'ro', 'ru', 'rue', 'sa',
  'sah', 'sc', 'scn', 'sco', 'sd', 'sh', 'si', 'sk', 'sl', 'so', 'sq', 'sr', 'su', 'sv', 'sw',
  'ta', 'te', 'tg', 'th', 'tk', 'tl', 'tr', 'tt', 'tyv', 'ug', 'uk', 'ur', 'uz', 'vec', 'vep',
  'vi', 'vls', 'vo', 'wa', 'war', 'wuu', 'xal', 'xmf', 'yi', 'yo', 'yue', 'zh',
  // END LANGUAGE_CODES
];

/**
 * Where fastText's code means something other than the ISO 639 code it
 * collides with. `lid.176` was trained on Wikipedia and takes Wikipedia's
 * codes, and `als` is Alemannic there and Tosk Albanian in ISO 639-3 — which
 * is what `Intl.DisplayNames` would call it.
 */
const NAME_OVERRIDES: Readonly<Record<string, string>> = {
  als: 'Alemannic',
};

/** A reader's name for *code*, in the browser's own language where it has
 *  one, falling back to the code itself for one `Intl` does not know. */
export function languageName(code: string, locale: string = navigator.language): string {
  const override = NAME_OVERRIDES[code];
  if (override) return override;
  try {
    const name = new Intl.DisplayNames([locale], { type: 'language', fallback: 'code' }).of(code);
    return name ?? code;
  } catch {
    return code;
  }
}

/** "English", "English and Portuguese", "English, Portuguese, and Spanish". */
export function describeLanguages(codes: readonly string[], locale?: string): string {
  const names = codes.map((code) => languageName(code, locale));
  if (names.length < 3) return names.join(' and ');
  return `${names.slice(0, -1).join(', ')}, and ${String(names.at(-1))}`;
}

/**
 * The code to offer first: the browser's own language where the detector
 * knows it, else English. `navigator.language` is `pt-BR`, not `pt`, and
 * the detector emits no regions.
 */
export function preferredLanguage(languages: readonly string[] = navigator.languages): string {
  for (const tag of languages) {
    const primary = tag.split('-')[0]?.toLowerCase();
    if (primary && LANGUAGE_CODES.includes(primary)) return primary;
  }
  return 'en';
}
