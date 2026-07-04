# Deployment

paid-plugin lives in two VPS users:
- **paid** (uid 1002) — jimmy's self-use PAID
- **paid-jelabs** (uid 1004) — JELabs pilot

Both are deployed by the `Deploy to VPS` GitHub Action workflow
(`.github/workflows/deploy.yml`).

## Triggers

1. **Push tag `v*`** — standard release path. Tags `v1.6.18`, `v1.7.0` etc.
   trigger automatic deploy to paid → paid-jelabs (sequential).
2. **Manual `workflow_dispatch`** — pick any ref + opt-out paid-jelabs.
   Useful for rollback, hotfix testing, or paid-only emergency push.

## Sequence

`paid → smoke OK → paid-jelabs`. If paid fails (smoke didn't see
`Gateway running with` in logs), paid-jelabs is NOT touched.

`concurrency: deploy-vps` ensures only one deploy runs at a time.

## What gets shipped

Tarball of the repo with these excluded:
- `.git/`, `.github/`, `.pytest_cache/`, `__pycache__/`, `*.pyc`
- `tests/` (dev only)
- `docs/v*_design.md` (design docs, not loaded at runtime)

Atomic-ish swap on VPS:
1. Extract to `~/.hermes/plugins/paid-v1.new/`
2. `mv paid-v1 paid-v1.bak`
3. `mv paid-v1.new paid-v1`
4. `systemctl --user restart hermes-gateway.service`

Rollback is `mv paid-v1.bak paid-v1 && systemctl --user restart`.

## Smoke check

After restart, the script waits up to 30 s for `is-active`, then looks for
`Gateway running with N platform(s)` in the last 100 log lines. If not
found, the job fails and paid-jelabs is skipped.

## Secrets needed (one-time setup)

In repo settings → Secrets and variables → Actions:

| Name | Value |
|---|---|
| `VPS_HOST` | `159.65.75.97` |
| `VPS_DEPLOY_KEY_PAID` | private key matching the paid user's authorized_keys |
| `VPS_DEPLOY_KEY_PAID_JELABS` | private key matching the paid-jelabs user's authorized_keys |

Public keys are already installed on the VPS (2026-05-21).

## First-time release v1.6.18

main HEAD content has accumulated v1.6.12–v1.6.18 fixes but only tag v1.6.11
exists. Closing the gap:

```
# in ~/Desktop/paid-plugin on main
bin/bump-version.sh 1.6.18
git add paid/_version.py CHANGELOG.md
git commit -m "chore(release): v1.6.18 — catch-up tag"
git push origin main
git tag -a v1.6.18 -m "v1.6.18"
git push origin v1.6.18  # triggers deploy
```
