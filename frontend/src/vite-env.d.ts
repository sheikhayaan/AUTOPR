/// <reference types="vite/client" />

// Typed access to the VITE_* build-time variables the client reads. Keeping
// them here means `import.meta.env.VITE_API_BASE` is `string | undefined`
// (not `any`), so the `?? '/api'` fallback is type-checked.
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  readonly VITE_AUTOPR_API_TOKEN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
