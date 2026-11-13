# Security

## Reporting a problem

Please report vulnerabilities privately, not in a public issue: on GitHub, open the repository's **Security** tab and choose **Report a vulnerability**. Include what you found, how to reproduce it and the version (`tether --version`). Reports are answered as soon as possible; anything confirmed gets a fix and a coordinated disclosure.

Only the latest release is supported.

## What Tether does with your data

- **Everything stays on your machine.** The ledger is one SQLite file under `~/.tether` (or `TETHER_HOME`). Tether makes no network calls of its own. The two exceptions are ones you turn on: the shared sync folder you choose (for example iCloud Drive) and `tether eval`, which calls the Claude or NVIDIA API with the key you provide.
- **Secrets are refused.** Every write path (MCP tools, CLI, the live view and `tether apply`) rejects text that looks like an API key, token or password, so credentials do not end up in the ledger or in a synced folder.
- **Nothing is hard-deleted.** Archiving and dropping set flags; `tether export` makes a private (mode 0600) JSON backup.

## The live view

`tether ui` serves a page on `127.0.0.1` only. To keep other websites from using it:

- every request's `Host` header must be `127.0.0.1` or `localhost` (blocks DNS rebinding);
- every write needs a random per-run token that is placed in the page when it is served (blocks cross-site requests);
- the page is sent with a content security policy, `nosniff`, no referrer and `no-store` headers, and cannot be framed.

## Supply chain

- Dependencies are kept to the MCP SDK; exact versions are locked in `uv.lock`, and Dependabot proposes updates.
- Releases are built in CI from a tagged commit after the tests pass. The wheel, source archive and Desktop extension carry a signed build-provenance attestation. Verify a download with:

```bash
gh attestation verify tether_mcp-<version>-py3-none-any.whl --repo kelyonn/tether
```

- PyPI uploads use trusted publishing (no long-lived token).

## Known limits

- Anyone who can read your home folder can read the ledger. Tether does not encrypt it; use disk encryption.
- The shared sync folder is only as private as the service behind it.
- The secrets filter is pattern-based and will not catch every possible credential.
