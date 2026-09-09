# Vendored front-end assets

Committed so the app runs with **no build step**. Regenerate `tailadmin.css` with
`scripts/build-css.sh` after editing `../index.html`, `../app.js` or `tailadmin.src.css`.

| file | source | license |
|---|---|---|
| `tailadmin.src.css` | TailAdmin free v2.3.0 — `src/css/style.css`, theme + component utilities only (third-party integration CSS removed), plus `@source` directives and an `[x-cloak]` rule. <https://github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template> | MIT |
| `tailadmin.css` | built from `tailadmin.src.css` with Tailwind CSS v4 (`@tailwindcss/cli`), minified | MIT (Tailwind CSS + TailAdmin) |
| `alpine.min.js` | Alpine.js v3.14.9 — `dist/cdn.min.js`. <https://github.com/alpinejs/alpine> | MIT |

The Outfit web font is pulled from Google Fonts by `tailadmin.css` (`@import`), same as
the upstream template.
