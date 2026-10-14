# Frontend

React app for AI Reporting (Vite + TypeScript + Tailwind + shadcn/ui).

## Prerequisites

- [Node.js](https://nodejs.org/) (LTS recommended)
- [pnpm](https://pnpm.io/installation)

## Install dependencies

From the `frontend/` directory:

```sh
pnpm install
```

## Start the app

```sh
pnpm dev
```

The dev server runs at [http://localhost:5173](http://localhost:5173).

In development, requests to `/api` are proxied to the backend at `http://localhost:8000`. Start the [backend](../backend/README.md) before using API features.

## Other commands

```sh
pnpm build      # type-check and production build
pnpm preview    # preview production build
pnpm lint       # ESLint
pnpm format     # Prettier
```

## Adding shadcn/ui components

```sh
pnpm dlx shadcn@latest add <component>
```

Components are generated in `src/components/ui/`.
