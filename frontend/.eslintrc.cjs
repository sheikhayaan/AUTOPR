/* ESLint for the AutoPR dashboard (ESLint 8, classic config).
 *
 * Non-type-checked TypeScript linting: fast, no `project` service needed. The
 * react-hooks plugin is the high-value one — it catches stale-closure and
 * missing-dependency bugs that TypeScript alone won't. `eslint-config-prettier`
 * turns off every stylistic rule so Prettier owns formatting and the two never
 * fight.
 */
module.exports = {
  root: true,
  env: { browser: true, es2022: true },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react-hooks'],
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
    'prettier',
  ],
  ignorePatterns: ['dist', 'node_modules', '*.cjs', '*.config.js', '*.config.ts'],
  rules: {
    // Allow intentional throwaways (`_`, `_evt`) without noise.
    '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
  },
}
