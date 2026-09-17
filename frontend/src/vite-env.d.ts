/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Must match `CSRF_COOKIE_NAME` in the server's settings. */
  readonly VITE_CSRF_COOKIE_NAME?: string;
  /** Must match `CSRF_HEADER_NAME` in the server's settings. */
  readonly VITE_CSRF_HEADER_NAME?: string;
  /** The commit this bundle was built from; set by the Containerfile. */
  readonly VITE_SRE_TAB_COMMIT?: string;
  /** The repository that commit lives in, as an https URL. */
  readonly VITE_SRE_TAB_SOURCE_URL?: string;
}

/** `package.json`'s version, substituted by `define` in vite.config.ts. */
declare const __APP_VERSION__: string;

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
