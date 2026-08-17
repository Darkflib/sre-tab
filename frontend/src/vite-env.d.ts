/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Must match `CSRF_COOKIE_NAME` in the server's settings. */
  readonly VITE_CSRF_COOKIE_NAME?: string;
  /** Must match `CSRF_HEADER_NAME` in the server's settings. */
  readonly VITE_CSRF_HEADER_NAME?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
