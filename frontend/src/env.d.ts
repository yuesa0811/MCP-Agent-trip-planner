/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_AMAP_WEB_JS_KEY: string
  readonly VITE_AMAP_SECURITY_JS_CODE?: string
}

interface Window {
  _AMapSecurityConfig?: { securityJsCode: string }
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
