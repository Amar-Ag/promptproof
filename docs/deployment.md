# Deployment

Two environments, both free: **Render** runs the application, **Neon** holds the database.
Why this shape rather than Fly, and what it costs architecturally, is
[ADR-0005](adr/0005-free-tier-deployment.md).

```
  merge to main ──▶ CI ──▶ deploy-dev ────────────────▶ promptproof-dev  ──▶ Neon `development`
                     │
                     └──▶ deploy-prod (manual + approval) ──▶ promptproof-prod ──▶ Neon `production`
```

One container per environment runs the API and the worker as separate processes, and the API
serves the built frontend from its own origin — so each environment is a single public URL
with no CORS.

## One-time setup

### 1. Database — already done

Neon project `rough-heart-88913963`, with a branch per environment:

| Branch | Used by |
|---|---|
| `production` | promptproof-prod |
| `development` | promptproof-dev |

Branches rather than two projects: separate data and separate credentials on one free project.
To re-read a connection string:

```bash
neon connection-string development --project-id rough-heart-88913963
```

Neon returns a libpq-style URL. The application needs the asyncpg form — replace
`postgresql://` with `postgresql+asyncpg://`, and swap `sslmode=require&channel_binding=require`
for `ssl=require`. asyncpg does not understand the libpq parameters and fails at connect time
with an unhelpful error if you leave them.

### 2. Create the Render services

Dashboard → **New** → **Blueprint** → pick this repository. Render reads
[`render.yaml`](../render.yaml) and creates both services on the free plan.

It will prompt for the two values marked `sync: false`:

| Service | `DATABASE_URL` | `GROQ_API_KEY` |
|---|---|---|
| `promptproof-dev` | Neon **development** branch, asyncpg form | your Groq key |
| `promptproof-prod` | Neon **production** branch, asyncpg form | your Groq key |

Using the wrong branch for an environment is the mistake to avoid here; nothing downstream
will catch it, because both URLs are valid.

### 3. Repository secrets and variables

**Settings → Secrets and variables → Actions.**

| Secret | Where to get it |
|---|---|
| `RENDER_API_KEY` | Render → Account Settings → API Keys |
| `RENDER_SERVICE_ID_DEV` | In the dev service's dashboard URL: `srv-...` |
| `RENDER_SERVICE_ID_PROD` | Same, for the production service |

| Variable | Value |
|---|---|
| `DEV_URL` | `https://promptproof-dev.onrender.com` |
| `PROD_URL` | `https://promptproof-prod.onrender.com` |

The URLs are variables, not secrets — they are public addresses, and keeping them visible means
the deploy logs can name what they smoke-checked.

### 4. The production approval gate

**Settings → Environments → New environment → `production`**, then add yourself under
**Required reviewers**.

Without this the production workflow still runs its CI check, but nobody is asked. The
environment is what makes the approval real rather than decorative, and it lives in repository
settings precisely so it cannot be removed by editing a workflow in a pull request.

Add a `development` environment too if you want the dev URL shown on deploy runs; it needs no
reviewers.

## Deploying

**Development** happens by itself: merge to `main`, CI runs, and a successful CI run triggers
`deploy-dev`. No human action.

**Production** is deliberate:

1. Actions → **Deploy (production)** → *Run workflow*
2. Give it the commit SHA to deploy
3. It refuses unless that SHA is an ancestor of `main` **and** CI concluded `success` on that
   exact commit
4. It then waits for a reviewer to approve
5. On approval it deploys, waits for the deploy to go live, and smoke-checks `/readyz`

Migrations run inside the container before it serves, so a release never takes traffic against
a schema it does not expect.

## What a free instance does

- **It sleeps after about fifteen minutes of inactivity.** The first request afterwards takes
  roughly a minute while the container wakes.
- **The worker sleeps with it.** A scan submitted to a sleeping service wakes it and then runs.
  A scan queued and then abandoned does not progress until something wakes the service.
- **A deploy restarts both processes**, so an in-flight scan is abandoned and recovered by
  lease expiry rather than continuing. `SCAN_LEASE_SECONDS` is raised to 600 in deployment for
  this reason.
- **`SCAN_CONCURRENCY` is 2**, not the local default of 4. Two model calls per probe against a
  free provider quota is how scans end up a third errored — see
  [#32](https://github.com/Amar-Ag/promptproof/issues/32).

None of this is suitable for production use by anyone else. It is a demo, hosted for nothing.

## Rolling back

[docs/runbooks/rollback.md](runbooks/rollback.md).
