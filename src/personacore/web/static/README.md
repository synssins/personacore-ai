# Vendored front-end assets

Committed here rather than fetched at runtime. PersonaCore is a self-hosted
appliance that may run on a network with no internet access at all, and an admin
interface that only works when someone else's CDN is reachable is broken by
design (ADR-0020). A CDN is also a third party in the request path of the
interface used to administer the system.

| File | Version | Source | Licence |
|---|---|---|---|
| `htmx.min.js` | 2.0.10 | `https://unpkg.com/htmx.org@2.0.10/dist/htmx.min.js` | Zero-Clause BSD — see `htmx.LICENSE` |

**SHA-256 of `htmx.min.js` as vendored:**
`71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de` (51,238 bytes)

Upgrading is a deliberate commit: fetch the new release, record its version,
hash and licence here, and review it like any other dependency change.
