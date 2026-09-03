// Registers jest-dom's matchers (toBeInTheDocument, toHaveTextContent, …) with
// Vitest's `expect` and wires their TypeScript augmentation. Referenced by
// vite.config's `test.setupFiles`, so every test file gets them for free.
import '@testing-library/jest-dom/vitest'
