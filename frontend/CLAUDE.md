# Frontend — Agent Guide

## Commands
- `pnpm dev` — dev server (port 5173)
- `pnpm build` — type-check + build
- `pnpm lint` — ESLint
- `pnpm format` — Prettier

## Adding shadcn/ui components
```sh
pnpm dlx shadcn@latest add <component>
```
Components land in `src/components/ui/` — never edit them directly.

## Structure
- `src/components/ui/` — shadcn/ui primitives (auto-generated, do not edit)
- `src/components/` — shared app components
- `src/pages/` — page-level components
- `src/hooks/` — custom React hooks
- `src/lib/utils.ts` — `cn()` helper for class merging

## Conventions
- Path alias `@/` → `src/`
- Use `cn()` from `@/lib/utils` for conditional classnames
- All API calls use `/api` prefix — Vite proxies to backend in dev
- Components: PascalCase filenames. Hooks: camelCase prefixed with `use`
- Theme: dark mode supported via `ThemeProvider` in `src/components/theme-provider.tsx`
