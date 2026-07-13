# Phase 2 Lite deployment

The production topology is defined in `docker-compose.prod.yml`. Only Django
and the OMS service publish host ports; PostgreSQL, Redis, FastAPI, and Flower
remain on the private Compose network.

1. Copy `.env.production.example` to `.env` and replace every placeholder.
2. Put an HTTPS reverse proxy or load balancer in front of Django port 8000.
3. During the first rollout, stop the old backend/worker/agent containers, then run:

   ```sh
   docker compose -f docker-compose.prod.yml build
   docker compose -f docker-compose.prod.yml up -d db redis
   docker compose -f docker-compose.prod.yml run --rm backend python manage.py migrate --noinput
   docker compose -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.prod.yml exec backend python manage.py check --deploy
   ```

The backend starts Gunicorn with 3 workers and 4 threads. `timer_worker`
consumes only the default `celery` queue with concurrency 2, while
`agent_job_worker` consumes only `agent` with concurrency 4. FastAPI runs with
2 Uvicorn workers and accepts `/ask` only with the shared internal token.

After an unclean worker shutdown, requeue jobs that have remained active for
more than five minutes:

```sh
docker compose -f docker-compose.prod.yml exec backend \
  python manage.py requeue_stale_agent_jobs --stale-minutes 5
```
