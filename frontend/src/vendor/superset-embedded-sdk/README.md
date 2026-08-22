# Vendored: @superset-ui/embedded-sdk

Source: `apache/superset`, path `superset-embedded-sdk/src`,
commit **5ce52e531dcb60f7c995ac7f4b74ff219b4b51ca**.

Unmodified except for this file.

## Why this is vendored rather than installed from npm

The published SDK does not expose `setDataMask`, which is what lets the host
page force an in-place chart re-query without remounting the iframe (the
remount is what causes the visible flash).

At the time of vendoring:

- `@superset-ui/embedded-sdk@0.4.0`, the latest published version, has no
  `setDataMask` in its types or its bundle.
- Superset 4.1.1 and 5.0.0 register only `getActiveTabs` and `getScrollSize`
  on the embedded switchboard.
- The above commit registers `setDataMask`, `getDataMask`, and
  `getChartStates` on **both** sides.

`docker-compose.yml` pins the Superset image to the matching commit tag
(`apache/superset:5ce52e5`). **The two pins must move together** — the host
SDK and the embedded page speak the same switchboard protocol, and this
feature exists only in unreleased builds of both.

The version mismatch is at least loud rather than silent: master's
`setDataMask` is sent with `port.get` rather than `port.emit`, so an embedded
page that predates it replies with an error instead of dropping the message.

## When to delete this

As soon as a published `@superset-ui/embedded-sdk` exposes `setDataMask` and a
tagged Superset release implements it. Then:

1. `npm install @superset-ui/embedded-sdk@<version>`
2. Change the import in `src/panels/DashboardPanel.tsx` back to the package.
3. Repoint the compose image at the tagged release.
4. Delete this directory.
