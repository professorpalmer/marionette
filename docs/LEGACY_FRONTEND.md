# Legacy Fallback Frontend

## What `harness/web/` is

`harness/web/` (`app.js`, `app.css`, `index.html`) is the LEGACY fallback
browser GUI for the harness. It is a minimal, no-build set of static files:
plain JavaScript, plain CSS, and a single HTML page with no bundler or
framework. It exists so the harness can present a usable UI directly in a
browser without any build step.

## How it is served

`harness/server.py` serves these files directly. The legacy GUI is reachable
at the `/app.js` route, where the server returns
`(_WEB / 'app.js').read_text()` (around line 2319 of `harness/server.py`).
Do not change this served behavior or these routes when working on the legacy
GUI comments or docs.

## What the shipping UI actually is

The shipping desktop application does NOT use `harness/web/`. The real,
shipping renderer lives in `webapp/src` (React/TypeScript). Its main
conversation view is `webapp/src/components/Conversation.tsx`. That is where
the product UI is built, tested, and shipped.

## When to touch the legacy GUI

Edit `harness/web/` only for browser-fallback fixes -- that is, when the
no-build browser fallback itself is broken or needs a small correction and
there is no other way to serve that path. This is the rare case.

## Where UI work belongs

Most UI work belongs in `webapp/src`, not in `harness/web/`.
