# MCP Automation Service

Production-ready FastAPI microservice that exposes your own data as an MCP server and orchestrates LLM+MCP loops via Celery workers and the OpenAI Responses API.

## Architecture

```
User → POST /automation/run → Celery worker → OpenAI Responses API (tools=mcp) ⇄ Google MCP servers
                                            ↓
                                  AutomationRun updated in PostgreSQL
```

- **FastAPI** — REST API + MCP SSE server at `/mcp/`
- **Celery + Redis** — LLM+MCP execution in background workers
- **PostgreSQL** — persistent state (GoogleCredential, MCPServer, AutomationRun)
- **OpenAI** — LLM orchestrator using remote MCP servers as tools (Responses API)
- **Google OAuth2** — per-user token storage (encrypted with Fernet)

## Prerequisites

- Docker + Docker Compose v2
- Google Cloud project (see Step 1 below)
- OpenAI API key

---

## Step 1 — Google Cloud Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or reuse an existing one)
3. Enable APIs: **Gmail API**, **Google Drive API**, **Google Calendar API**
4. Navigate to **APIs & Services → OAuth consent screen**
   - Set application type to **External**
   - Add scopes: `gmail.readonly`, `gmail.send`, `drive.file`, `calendar.events`
5. Navigate to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client IDs**
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:8000/auth/google/callback`
6. Copy the **Client ID** and **Client Secret**

---

## Step 2 — Local Configuration

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```bash
# Generate a secure secret key
SECRET_KEY=$(openssl rand -hex 32)

# Generate a Fernet key for token encryption
FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Paste your Google OAuth credentials
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret

# Paste your OpenAI API key
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1
```

---

## Step 3 — Start Services

```bash
docker compose up --build
```

This starts 4 services: `postgres`, `redis`, `api`, `worker`.

---

## Step 4 — Run Migrations

```bash
docker compose exec api alembic upgrade head
```

---

## Step 5 — Connect Google Account

Open in browser:

```
http://localhost:8000/auth/google/login
```

After granting access, you receive a JWT:

```json
{"access_token": "eyJ...", "token_type": "bearer"}
```

Use this token in all subsequent requests as `Authorization: Bearer <token>`.

---

## Step 6 — Register an MCP Server

Insert a server record directly into the DB (or build an admin endpoint):

```sql
INSERT INTO mcp_servers (name, transport, url, auth_type, enabled, config)
VALUES ('gmail', 'http', 'https://gmail.googleapis.com/mcp', 'oauth', true, '{}');
```

---

## Step 7 — Trigger an Automation Run

```bash
curl -X POST http://localhost:8000/automation/run \
  -H "Authorization: Bearer <your-jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "mcp_server_id": 1,
    "tool_name": "search_emails",
    "instructions": "Find all emails from last week about invoices and summarize them"
  }'
```

Response (HTTP 202):
```json
{"run_id": 1, "status": "pending"}
```

Poll for results:

```bash
curl http://localhost:8000/automation/run/1 \
  -H "Authorization: Bearer <your-jwt>"
```

---

## MCP Server (SSE)

Connect an MCP client to your own data:

```
GET http://localhost:8000/mcp/
Authorization: Bearer <your-jwt>
```

Available tools: `get_automation_runs`, `get_mcp_servers`, `get_run_detail`

---

## API Reference

Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | — | Health check |
| GET | `/auth/google/login` | — | Start Google OAuth flow |
| GET | `/auth/google/callback` | — | OAuth callback, returns JWT |
| POST | `/automation/run` | JWT | Trigger automation run |
| GET | `/automation/run/{id}` | JWT | Get run status |
| GET | `/automation/runs` | JWT | List recent runs |
| GET | `/mcp/` | JWT | MCP SSE stream |
| POST | `/mcp/messages/` | — | MCP message relay |

---

## Running Tests

Install deps locally:

```bash
pip install -r requirements.txt aiosqlite
pytest tests/ -v
```

---

## Security Notes

- Tokens are stored **Fernet-encrypted** in PostgreSQL — never in plaintext
- JWT validation happens **before** the MCP SSE stream opens
- Never commit `.env` — it contains secrets
- In production, use a secrets manager (Vault, AWS Secrets Manager) for `FERNET_KEY` and `SECRET_KEY`
- Configure session affinity at your load balancer — MCP sessions are RAM-persisted per API instance
