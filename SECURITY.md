# Security policy

## Reporting a vulnerability
Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository, with steps to reproduce and the affected version or commit.

## Security model

**Federated sign-in** (optional, `CPSIM_OIDC_ISSUER`)
- Access tokens are verified locally against the identity provider's JWKS (algorithm allow-list, issuer, audience, expiry), so a
  token minted for another app or signed by another key is refused. Only the roles of THIS app are honoured, and they are mapped to
  permissions in `core.py` (`admin`, `operator`, `viewer`); an authenticated person with no role here can do nothing.
- Browser sign-in is authorization code + PKCE with state and nonce; the session is server-side and carries the person's roles from
  the verified ID token. Sign-out also ends the single sign-on session. Role changes reach an existing session at its next sign-in.
- Service tokens carry scopes (`cpsim.read`, `cpsim.metrics`) and nothing more. A browser session, of any role, cannot scrape `/metrics`.
- Outbound, the simulator may mint tokens with its own `service` client (`CPSIM_SERVICE_CLIENT_SECRET`), one per callee and only
  with that callee's scope; static tokens win when set. The service secret is read from the environment or a file, never stored.

**Credentials**
- First run: every protected route is denied. The server prints a one-time setup code to its log; whoever presents it in
  the browser sets the admin password (stored only as a salted scrypt hash, 12-character minimum). The code lives in memory
  and changes on every restart until setup is complete.
- Browser sessions are random cookies (`HttpOnly`, `SameSite=Strict` for the local login, `Lax` after single sign-on, `Secure` over HTTPS),
  stored in Postgres as SHA-256 only, 12 hours long, ended by sign-out or a password change. Browser writes also need a custom CSRF header.
- Automation may use `CPSIM_API_KEY` (full access) or API tokens created under Settings (scope read, metrics or full; stored only as a hash,
  shown once, revocable). `/metrics` accepts only `CPSIM_METRICS_TOKEN` (or a full credential); a
  metrics token can read nothing else and a browser session cannot scrape.
- With no credential configured the app denies everything; `CPSIM_ALLOW_ANONYMOUS=true` is an explicit demo-only opt-out.
- Failed credentials are rate limited (10 per minute per client address, in memory, per process).

**Secrets are never stored in a city.** A city holds only the *name* of the environment variable that carries its CityPulse
ingest token (validated as an upper-case variable name, so pasting a token there is refused). Cities, companies, workloads and the credentials
above (password hash, session and token hashes) are the only things persisted, in Postgres, which is required. The file store of the
first release is retired (`cpsim import-files` loads it once).

**Untrusted input.** Every city is validated: names, numbers (finite, bounded), counts (a group at most 5,000, a city 10,000),
sensor types and protocols from allow-lists, unknown fields rejected. The CityPulse URL must be `https` (plain `http` only for
this machine), carry no credentials, query or fragment, and must not be a link-local address (cloud metadata). Requests are
capped at 512 KB.

**Outbound requests.** The only outbound traffic is to the city's CityPulse URL (the ingest token goes there and nowhere else;
redirects are never followed) and, if configured, to PagerLens (one token, same rules).

**Industrial protocol endpoints.** They bind to loopback by default. Binding to all interfaces is a per-city opt-in, because
Modbus, EtherNet/IP, EtherCAT, PROFINET and PROFIBUS have no authentication: anyone who can reach a port can read the
simulated values. They are read-only (writes are refused or ignored), bounded in message size, and survive malformed input
(tested). Do not expose them to untrusted networks.

**Container.** Non-root, no secrets baked in, strict Content-Security-Policy (no inline scripts or styles), `X-Frame-Options:
DENY`, `nosniff`, `no-referrer`; the interface builds DOM nodes with `textContent`.

**Companies (TrustDesk).** A company stores only the NAME of an environment variable (validated, and it must contain `TOKEN`, so it
cannot point at a database URL or an API key). The TrustDesk client token is sent only to the company's URL (https, or http for this
machine; no credentials, no link-local addresses; redirects are never followed). Generated people are invented and use `example.com`
addresses. A run is bounded (20,000 requests, 2,000 employees, 200 requests per second, 16 connections).

**Workloads.** A workload stores only the NAME of the environment variable that carries its credential (validated: it must contain `TOKEN`, `KEY` or `SECRET`, and may not
be one of the simulator's own `CPSIM_*` variables or a database variable), so pasting a key there is refused. The credential is sent only to the workload's URL (https, or http for
this machine; plain http to other hosts only with the explicit `CPSIM_ALLOW_HTTP_TARGETS`; no credentials in the URL, no link-local addresses; redirects are never followed) and is never logged, printed,
stored or put in a status, error or metric. Targets are chosen by the operator, so there is no further SSRF restriction beyond a city's; the headless command prints one line when a target is
not on this machine. Bounds: batch at most 500, interval at least 0.2 s, `CPSIM_MAX_RATE` for one workload and for all together, the identity probe at most 20 calls per tick and one tick a second. The
identity probe sends the service client secret only to a token endpoint on the provider's own origin. Every generated identity is fictional (`example.com`/`example.org`, documentation IP ranges, test cards in
the reserved `999999` range). Residual risk: someone with the *operator* role can make the simulator send any non-denylisted environment secret of its process to a URL they choose; give the sandbox only
the credentials the workloads need, narrow and revocable. Running (`sim:run`), editing (`sim:edit`) and seeing (`sim:read`) workloads follow the same roles as cities and companies.

**Known limits.** Rate limiting is per process. A leaked ingest token lets an attacker push readings to CityPulse
(bounded by its validation) until revoked; use one token per simulator.

**Masking durability.** The read-only database role's REVOKEs on the secret tables are re-asserted every 5 minutes (grants after startup, from `agentpg db-users create` or default privileges, would otherwise reopen them); gauge `masking_drift_tables` and `/readyz` `masking` show drift. A new grant can be readable for up to one interval.

**Saved credentials.** Credentials may be saved in the app (Settings > Credentials) instead of the environment. They are encrypted with the kit's
vault, whose data key is wrapped under the platform secret `CPSIM_ENCRYPTION_KEY` and the admin password; the database alone cannot decrypt
them. Plaintext exists only in process memory; no endpoint returns it and the interface never shows it. Names must contain `TOKEN`, `KEY` or
`SECRET` and may not be the simulator's own or the database's; only `settings:manage` may list, save or delete them. The environment wins
over a saved value, so an operator can always override from outside. Residual risk: an administrator can point a workload at a URL they
control and have the simulator send that workload's credential there; keep the role for people you trust with the credentials.
