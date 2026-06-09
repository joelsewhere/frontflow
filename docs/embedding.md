# Embedding forms

Render a frontflow form inside an iframe on another origin — a
marketing site, an internal portal, a department-specific tool.

## When to use it

Two scenarios drive iframe embedding:

1. **Public-facing surfaces.** The marketing team owns `company.com`;
   the contact form, newsletter signup, or waitlist runs on
   `forms.company.com`. They embed the form on `company.com/contact`
   instead of redirecting users away.
2. **Internal portals.** A larger internal tool owns the page
   surface; specific frontflow forms get dropped into iframes inside
   it.

If a form's natural home is just frontflow itself (admins fill it
out, or users link directly), don't bother with embedding.

## Quickstart

Mark the form embeddable:

```python
from frontflow import Button, displays, form, inputs, node

@form(
    form_id="contact",
    title="Get in touch",
    iframe_allowed_origins=[
        "https://company.com",
        "https://*.company.com",
    ],
)
def contact():

    @node
    def message():
        body = inputs.TextBlock(label="What's up?", required=True)
        return body, Button("Send")

    message()

contact()
```

Drop the iframe on the host page:

```html
<iframe
    src="https://forms.company.com/forms/contact/form"
    width="100%" height="400" frameborder="0"
></iframe>
```

That's it. The browser permits the embed; the form renders normally
inside the frame.

**The iframe `src` must point at `/forms/<form_id>/form`** — note
the trailing `/form`. The admin route `/forms/<form_id>` (no `/form`)
is the admin summary page, which is never iframable regardless of
the allowlist.

## Allowlist syntax

`iframe_allowed_origins` is a list of origin strings. Each entry
must include the scheme:

| Pattern | Matches |
|---|---|
| `https://company.com` | The exact origin `https://company.com` |
| `https://*.company.com` | Any subdomain at any depth — `www.company.com`, `blog.company.com`, `staging.docs.company.com`. **Does not** match the bare `company.com`; include it separately if you want both. |
| `https://company.com:8443` | An exact origin with a non-default port |
| `http://localhost:3000` | Useful for local dev. Strip in production. |
| `"*"` | **Any** origin. Disables the main security boundary; use only for genuinely public marketing forms. |

The list is emitted directly as a CSP `frame-ancestors` directive,
so the browser is the enforcement point — frontflow's job is just
to set the header correctly.

## Security model

The protection is a `Content-Security-Policy: frame-ancestors`
response header. Every form-render response carries one:

- **Form has `iframe_allowed_origins` set** *AND* is publicly
  visible → permissive header naming those origins
- **Otherwise** → `frame-ancestors 'none'`, plus the legacy
  `X-Frame-Options: DENY` for older browsers

This means:

- An attacker cannot embed a non-iframable form just by trying.
  The browser refuses the embed before any code on frontflow's side
  knows about the attempt.
- Visibility changes (admin marks a form `restricted` later) take
  effect immediately — no re-deploy needed.
- The legacy `X-Frame-Options` header is dropped on permissive
  responses, so an older browser doesn't strictly-deny while a
  modern browser permits.

## Visibility gate

Only **public** forms are actually iframable. A form marked
`unlisted` or `restricted` in the admin UI is served with
`frame-ancestors 'none'` even when its DSL declares
`iframe_allowed_origins`. A warning gets logged on each render so
operators can audit.

This is the v1 design. **Authenticated form embedding is not yet
supported.** A separate design is needed for token handoff, SSO,
or shared-domain cookies — see "Future" below.

## What "embedded mode" gives you today

When a form is rendered inside any iframe, the SPA sets
`<body data-embedded="true">`. The live-form route is already
chrome-free by design — no admin nav, no breadcrumbs — so today
this attribute is for forward-compatibility with future components
that may want to react to embedded state.

The detection is `window.self !== window.top`. The CSP header has
already enforced "you can't be in an iframe unless you're allowed,"
so the SPA can trust "in any iframe" as "allowed iframe."

## Admin awareness

Forms with `iframe_allowed_origins` set show an **Iframable** badge
on the forms listing page. Hover the badge to see the configured
origins. The form summary page surfaces the full allowlist in its
metadata.

## Try the bundled example

```bash
frontflow example install embeddable_signup --dest ./forms
frontflow serve ./forms
```

The form lives at `/forms/embeddable_signup/form`. The allowlist
in the example permits `localhost:3000` and `localhost:5173` so
you can iframe it from a local React or Vite dev server while
wiring things up.

## Future

The v1 surface is full-form public embedding. The longer arc:

- **Per-node embedding.** A multi-node form spans several
  departments; each department embeds its own node in their
  internal tool. Users hand off submissions across departments
  via the same submission handle. v1 doesn't ship this; the
  allowlist mechanism extends to per-node-override cleanly.
- **Authenticated form embedding.** Cross-origin auth handoff —
  parent page is logged in, want to share that with frontflow.
  Options on the table: short-lived token via URL, `postMessage`
  with origin check, shared-domain cookies, SSO redirect inside
  the iframe. Significant security design; not in v1.
- **`postMessage` events to the host page.** On submit success,
  the iframe could post a structured message to the parent page
  (so the host can show a confirmation, navigate, etc.). v1
  doesn't ship this; per-origin policy plugs into the same
  allowlist.

If your use case needs one of these, raise it — the v1 design is
intended to leave the doors open.

## Caveats

- **No iframe auto-resize.** The host page sets the iframe height
  (or wraps it in a fixed-height container). For variable-height
  forms (multi-step, conditional sections that change layout
  height), the host page either sizes generously or adds its own
  resize-via-`postMessage` glue. A built-in solution would land
  alongside the future `postMessage` work.
- **No iframe-specific theming.** The form uses its normal theme.
  The host page can wrap the iframe in styled framing; the iframe
  contents follow whatever the form's theme is set to.
- **Submissions are same-origin from the iframe.** No CORS gymnastics
  on the frontflow side — the form submits to its own origin like
  any other render.
- **Third-party cookies.** The iframe is on a different origin
  than the host page. If the form depends on cookies (auth,
  unlisted-form token), the user's browser may treat those as
  third-party and block them depending on privacy settings.
  Public-form embedding doesn't touch cookies; this is only a
  concern if you push beyond v1's "public only" gate.
