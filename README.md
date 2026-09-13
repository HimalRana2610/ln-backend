# ln-backend

FastAPI service behind LectureNote AI. Owns the database, authentication and all
business rules; [`ln-web`](../ln-web) and [`ln-app`](../ln-app) are pure clients.

Setup and how to run: **[../SETUP.md](../SETUP.md)**.
Deploying it free: **[../DEPLOYMENT.md](../DEPLOYMENT.md)**.

## Stack

| Concern | Choice | Why |
| --- | --- | --- |
| Framework | FastAPI + Uvicorn | Async, typed, generates the OpenAPI schema clients consume |
| ORM | SQLAlchemy 2.0 async + asyncpg | Typed `Mapped[]` models, real async driver |
| Migrations | Alembic | Versioned schema; autogenerate diffs models against the DB |
| Validation | Pydantic v2 | One layer for parsing, validation and OpenAPI |
| Passwords | argon2-cffi | Memory-hard; no 72-byte truncation like bcrypt |
| Tokens | PyJWT | Short access token, rotating revocable refresh token |
| Storage | S3-compatible via boto3 | MinIO locally, Cloudflare R2 or S3 in production |
| Tests | pytest + httpx | Against real Postgres, not a SQLite stand-in |
| Lint / types | ruff + mypy (strict) | |

## Layout

```
app/
├── main.py             App factory, middleware, exception handlers
├── core/
│   ├── config.py       Env-driven settings — the only place env vars are read
│   ├── security.py     Password hashing, JWT encode/decode
│   └── exceptions.py   Domain errors and their HTTP rendering
├── db/
│   ├── base.py         DeclarativeBase, UUID/timestamp mixins, naming convention
│   └── session.py      Async engine, session factory, get_db dependency
├── models/             SQLAlchemy tables
├── schemas/            Pydantic request/response bodies
├── services/           Business logic — no FastAPI imports
└── api/
    ├── deps.py         Shared dependencies (current user, db, client info)
    └── v1/             Versioned routers
```

## The two rules that keep this clean

**Services never import FastAPI; routes never contain business logic.** A
service raises an `AppError` subclass and knows nothing about status codes; only
`app/main.py` maps those to HTTP. That is what makes the same logic reusable
from a worker or a CLI later.

**Routes never call `commit()`.** `get_db` commits once if the request succeeds
and rolls back if it raises, so a half-applied request is impossible.

This is the direct answer to the old backend, which was one 2,696-line `app.py`
with database calls, Google OAuth, Gemini prompts and HTTP handling interleaved.

## Commands

Activate the venv first (`.\.venv\Scripts\Activate.ps1`).

| Command | Purpose |
| --- | --- |
| `uvicorn app.main:app --reload --host 0.0.0.0` | Dev server, reachable from your phone |
| `pytest` | Test suite (needs `docker compose up -d db`) |
| `pytest --cov=app` | With coverage |
| `ruff check . --fix` | Lint and auto-fix |
| `ruff format .` | Format |
| `mypy app` | Strict type check |
| `alembic revision --autogenerate -m "..."` | New migration from model changes |
| `alembic upgrade head` | Apply migrations |
| `alembic downgrade -1` | Roll back one |

## Environment

Copy `.env.example` to `.env`. Full descriptions are in that file.

| Variable | Required | Notes |
| --- | --- | --- |
| `ENVIRONMENT` | yes | `local` \| `test` \| `staging` \| `production`. Hides `/docs` in production |
| `SECRET_KEY` | yes | Min 32 chars. Rotating it invalidates every issued token |
| `DATABASE_URL` | yes | `postgresql://user:pass@host:5432/db` |
| `ACCESS_TOKEN_TTL_MINUTES` | no | Default 15 |
| `REFRESH_TOKEN_TTL_DAYS` | no | Default 30 |
| `CORS_ORIGINS` | no | Comma-separated; must list the web app's origin |
| `S3_ENDPOINT_URL` | no | Omit for real AWS S3; set for MinIO or R2 |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | no | Required once uploads land |
| `S3_BUCKET` | no | Default `lecture-note` |
| `GEMINI_API_KEY` | for notes | From https://aistudio.google.com/apikey. Without it every generation fails with a clear message |
| `GEMINI_API_KEY_BACKUP` | no | Takes over when the primary key is rate limited |
| `GEMINI_MODEL` | no | Default `gemini-3.7-flash`. Google closes old models to **new** API keys — a retired model still lists but 404s on the first call, which looks like a broken key. Change this, not the code |
| `SUPADATA_API_KEY` | for YouTube | Transcript fetching only; audio, PDF and text need only the Gemini key |
| `DB_SERVERLESS` | no | Default false. Set **true** only on a serverless host — swaps to `NullPool` and disables prepared-statement caching, both required behind a transaction pooler. See [../DEPLOYMENT.md](../DEPLOYMENT.md) |

## API

Everything is under `/api/v1`. Interactive docs at `/docs` while running.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | – | Liveness; touches nothing |
| GET | `/health/ready` | – | Readiness; verifies the database |
| POST | `/auth/register` | – | Create an account, returns the profile |
| POST | `/auth/login` | – | Exchange credentials for a token pair |
| POST | `/auth/refresh` | – | Rotate a refresh token |
| POST | `/auth/logout` | Bearer | Revoke one session, or all when body is `{}` |
| GET | `/users/me` | Bearer | Current profile |
| PATCH | `/users/me` | Bearer | Update name / institute |
| GET | `/classrooms` | Bearer | Classrooms you are a member of |
| POST | `/classrooms` | Bearer | Create one; you become the owner |
| POST | `/classrooms/join` | Bearer | Join by 6-character code |
| GET | `/classrooms/theme-colors` | – | Card gradients both clients offer |
| GET | `/classrooms/{id}` | Bearer | One classroom |
| PATCH | `/classrooms/{id}` | Bearer | Edit (owner or teacher) |
| DELETE | `/classrooms/{id}` | Bearer | Delete (owner only) |
| POST | `/classrooms/{id}/leave` | Bearer | Leave (owner cannot) |
| GET | `/classrooms/{id}/members` | Bearer | Member list |
| PATCH | `/classrooms/{id}/members/{user_id}` | Bearer | Change role (owner only) |
| DELETE | `/classrooms/{id}/members/{user_id}` | Bearer | Remove (owner or teacher) |
| POST | `/uploads/presign` | Bearer | URL to PUT a file straight to storage |
| POST | `/classrooms/{id}/notes` | Bearer | Queue generation — returns **202**, `status: pending` |
| GET | `/classrooms/{id}/notes` | Bearer | Notes in a class; `?date=` filters. Omits the Markdown body |
| GET | `/notes/{id}` | Bearer | One note, with its Markdown |
| PATCH | `/notes/{id}` | Bearer | Edit (author or teacher) |
| DELETE | `/notes/{id}` | Bearer | Delete (author or teacher) |

A non-member gets **404, not 403**, on any classroom route — a stranger must not
be able to confirm that a classroom id exists.

Errors always use one envelope:

```json
{ "error": { "code": "conflict", "message": "An account with that email already exists" } }
```

Validation failures add a `details` array of `{field, message}`.

## How auth works

1. `POST /auth/login` returns a **15-minute access token** and a **30-day
   refresh token**.
2. The access token is a stateless JWT sent as `Authorization: Bearer`.
3. The refresh token's `jti` is stored in `refresh_tokens`, so it can be revoked.
4. `POST /auth/refresh` issues a new pair and marks the old row revoked, with
   `replaced_by_jti` pointing at its successor.
5. **Reuse detection** — presenting an already-rotated token means it was
   replayed, so every session for that user is revoked, not just that request.

Point 5 is why the mobile client collapses concurrent refreshes onto a single
request; see [`ln-app`](../ln-app/README.md).

## Testing

Tests run against real Postgres in a per-test transaction that is rolled back,
so they are isolated and order-independent. A separate `..._test` database is
created and dropped per run.

```bash
docker compose up -d db     # required
pytest
```

Postgres rather than SQLite is deliberate: UUID columns, `gen_random_uuid()` and
server-side defaults all behave differently on SQLite, which is exactly where a
passing test would lie to you.

## Adding a feature

1. Model in `app/models/`, exported from `app/models/__init__.py`.
2. `alembic revision --autogenerate -m "..."`, then **read the generated file**
   before committing — autogenerate is a starting point, not an oracle.
3. Schemas in `app/schemas/`.
4. Logic in `app/services/`, raising `AppError` subclasses.
5. Routes in `app/api/v1/routes/`, registered in `app/api/v1/router.py`.
6. Tests in `tests/`.

## Note generation

Generation takes minutes, so it never happens inside a request.
`POST .../notes` returns **202** with `status: pending`; the client polls until
`ready` or `failed`.

Who does the work depends on `NOTES_INLINE_WORKER`:

| Deployment | Setting | Runs generation |
| --- | --- | --- |
| Local, Docker, Render | `true` (default) | A FastAPI background task |
| Vercel / serverless | `false` | `python -m app.worker`, running elsewhere |

A serverless function is killed the moment it responds, so a background task
there never finishes. The worker is a polling loop over the same table; claims
use `SELECT … FOR UPDATE SKIP LOCKED` so several can run at once, and a note
left in `processing` past `NOTES_STALE_AFTER_MINUTES` is reclaimed.

`GEMINI_API_KEY` must be set, or every note fails with a clear message.

The model name is `GEMINI_MODEL`, not a constant, because Google closes older
models to new API keys on a rolling basis. A retired model still appears in
`models.list()` but returns 404 on the first real call — which reads like an
invalid key, so it is worth recognising.

Generated Markdown is constrained by the prompt to a strict Mermaid subset and
to Unicode rather than LaTeX, because neither client renders maths and an
invalid diagram otherwise displays as an error box. `ln-web` validates every
diagram before rendering and downgrades invalid ones to code blocks.

## Status

Auth, classrooms and notes are complete: **91 tests** passing in ~6s, ruff
clean, mypy strict clean.

Materials and assignments are next — see
[../docs/PROGRESS.md](../docs/PROGRESS.md).