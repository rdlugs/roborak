# roborak website

The documentation site at [roborak.pages.dev](https://roborak.pages.dev): a
landing page plus ten documentation pages, built with
[Vite](https://vite.dev/), [React Router](https://reactrouter.com/) and
[Tailwind CSS](https://tailwindcss.com/).

## Running it

```bash
npm ci
npm run dev          # dev server
npm run typecheck
npm run build        # bundle to dist/
npm run serve        # serve the build, exactly as Cloudflare Pages will
```

Node 20 or newer (`.nvmrc`). Fonts are self-hosted but not committed: they are
copied out of `node_modules` into `public/fonts/` by `scripts/copy-fonts.mjs`,
which runs automatically before `dev` and `build`.

## How it is laid out

```
index.html           the document shell; paints the background before the bundle lands
src/
  main.tsx           mounts the router
  routes.tsx         the route table, written out by hand
  layouts/root.tsx   scroll restoration around every route
  layouts/docs.tsx   sidebar, mobile drawer, prev/next
  pages/home.tsx     landing page
  pages/docs/*.tsx   one file per documentation page
  components/        the design system: prose, code blocks, callouts, tables
  content/           page data that is really a table (command flags)
tailwind.config.js   the neon palette, defined once
```

Pages are written as a flat list of the components in `src/components/`, which is
what keeps ten hand-written pages looking like one site. There is deliberately no
MDX pipeline: the component set is what an MDX component map would have to be
anyway, and a build-time transformer is a lot of machinery to save some angle
brackets.

Routes are listed one by one in `src/routes.tsx` rather than generated from a
catch-all. At this size a concrete list is the more legible thing — the component
behind any URL is greppable from one file.

## Deployment

Cloudflare Pages, from this repository, on push to `main`:

| Setting | Value |
|---|---|
| Root directory | `docs` |
| Build command | `npm ci && npm run build` |
| Output directory | `dist` |
| Environment | `NODE_VERSION=20` |

Both paths the build uses are relative to the root directory, not to the
repository: with a root directory of `docs`, the output directory is `dist`, and
`docs/dist` would be looked for at `docs/docs/dist`.

`public/_headers` sets the CSP and cache headers, and `public/_redirects` rewrites
every path to `index.html` so deep links reach the router instead of a 404. Pages
reads both from the build root. Pull requests get preview deployments
automatically.

Note that this is a single-page app: every URL serves the same `index.html`, and
titles, descriptions and body copy are filled in by JavaScript. Crawlers that
render pages see the real content; link-preview bots that do not will fall back to
the shell. Moving to prerendered HTML per route later means adopting React
Router's framework mode and listing the routes to prerender — the components do
not change.

## Editing content

Every page is derived from something in the repository above this directory —
`README.md`, `CONTRIBUTING.md`, the Typer `help=` strings, and
`src/roborak/config_template.yaml`. See "Working on the website" in
[`../CONTRIBUTING.md`](../CONTRIBUTING.md) for which page tracks which source.
