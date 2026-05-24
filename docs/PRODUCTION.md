# Production runbook

Current production topology is split-host:

- **Hostinger VPS** runs n8n workflows.
- **dbhub local server** runs Flask, scheduler code, SQLite, LiteLLM/Ollama, and `cloudflared`.
- **Cloudflare** routes `https://sdr.tradecredit.agency` to dbhub.

## Routes

- `GET /` redirects to `/queue`.
- `GET /queue` is Daniel's Review UI.
- `GET /health` is the human health check after Cloudflare Access login.
- `GET /scheduler/ready` is the read-only n8n preflight. It verifies Bearer auth and local files/SQLite only.
- `POST /scheduler/run` is side-effectful and runs the scheduler. Do not use it for reachability checks.

## Cloudflare Access policy

Protect human UI routes for Daniel:

- `/`
- `/queue`
- `/health`

Bypass Access only for scheduler machine routes:

- `/scheduler/ready`
- `/scheduler/run`

Those scheduler routes must be protected by the Flask `SCHEDULER_AUTH_TOKEN`.

## Hostinger n8n validation sequence

1. Import `n8n/workflows/sdr-scheduler.json` and keep it inactive.
2. Set n8n variable `SCHEDULER_AUTH_TOKEN` to match dbhub's Flask `.env`.
3. Manually run only the `Scheduler preflight` node.
4. Confirm it returns HTTP 200 from `https://sdr.tradecredit.agency/scheduler/ready`.
5. Confirm a bad token returns 401.
6. Run the full scheduler manually only after Daniel approves side effects.
7. Activate weekday cron after the manual run succeeds.

