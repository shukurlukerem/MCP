MCP Integration for a Django Backend — Complete Guide
1. Core concept: two directions
"Adding MCP" means two different things. Decide which you need before reading on:
Direction
Django's role
Why
A) Consume MCP
Client / Host
Give your AI external tools (Gmail, Drive, Calendar, etc.)
B) Expose MCP
Server
Open your own Django data to other AI agents

Email, files, calendar → Direction A (Google client). Your own database → Direction B (your own server).
Key reality: MCP doesn't work alone. It serves an LLM (Claude, Gemini). The flow is:
LLM  ⇄  Django (orchestrator)  ⇄  MCP server(s)  ⇄  real system (Gmail/Drive/DB)
Django is the broker between the LLM and the MCP servers.
2. Important shortcut: the LLM's built-in MCP connector
Before writing anything by hand, know this — sometimes you don't need to write MCP client code at all:
Anthropic (Claude) MCP connector: Claude's Messages API connects to remote MCP servers directly. <cite index="37-1">This feature requires the anthropic-beta: mcp-client-2025-04-04 header and lets you connect to MCP servers without implementing a separate MCP client.</cite> You just pass an mcp_servers array in the request. Limitations: <cite index="37-1">only tool calls are currently supported, the server must be publicly exposed over HTTP (Streamable HTTP and SSE), local STDIO servers cannot be connected directly, and the connector is not supported on Amazon Bedrock or Google Vertex.</cite>
With this path, your Django's job is just: store tokens + pass MCP server URLs to the Claude API. Anthropic's infrastructure executes the tools.
If you want full control (e.g. you use Gemini, or you want tool execution to stay on your own server), build your own client with the MCP Python SDK (below).
3. Resources and links
Official MCP
Resource
Link
MCP official docs / spec
https://modelcontextprotocol.io
MCP Python SDK (client + server)
https://github.com/modelcontextprotocol/python-sdk
Install
pip install "mcp[cli]"

Google (email, files, calendar) — official managed servers
Resource
Link
Configure Google Workspace MCP servers
https://developers.google.com/workspace/guides/configure-mcp-servers
Gmail MCP server
https://developers.google.com/workspace/gmail/api/reference/mcp
Google Drive MCP server
https://developers.google.com/workspace/drive/api/guides/configure-mcp-server
Google-managed MCP servers (announcement)
https://cloud.google.com/blog/products/ai-machine-learning/google-managed-mcp-servers-are-available-for-everyone
Codelab (step-by-step)
https://codelabs.developers.google.com/google-workspace-mcp-gemini-cli
Google Cloud Console (project + OAuth)
https://console.cloud.google.com
Python OAuth libraries
google-auth, google-auth-oauthlib

Note: Google's official Workspace MCP servers are currently in Developer Preview — you may need to join the program.
Google — open-source (community) alternatives
All bundle Gmail + Drive + Calendar in one server; you self-host:
Repo
Link
taylorwilsdon/google_workspace_mcp (most complete)
https://github.com/taylorwilsdon/google_workspace_mcp
aaronsb/google-workspace-mcp
https://github.com/aaronsb/google-workspace-mcp

Expose your own database as an MCP server (Django)
Repo / Package
Link
django-mcp-server (exposes models as tools)
https://github.com/omarbenhamid/django-mcp-server
PyPI
https://pypi.org/project/django-mcp-server/
kitespark/django-mcp (ASGI mount)
https://github.com/kitespark/django-mcp
joshuadavidthomas/mcp-django (for development)
https://github.com/joshuadavidthomas/mcp-django

LLM side (orchestration)
Resource
Link
Anthropic MCP connector
https://docs.claude.com/en/docs/agents-and-tools/mcp-connector
Anthropic tool use
https://docs.claude.com/en/docs/build-with-claude/tool-use
Gemini function calling
https://ai.google.dev/gemini-api/docs/function-calling
Celery (background jobs)
https://docs.celeryq.dev

4. Step-by-step: Email, Files, Calendar (Google)
All three belong to Google Workspace, so the common setup happens once.
4.0 Common setup (once)
Create a project in Google Cloud Console: https://console.cloud.google.com
Enable the required APIs: Gmail API, Drive API, Calendar API.
Configure the OAuth consent screen and create an OAuth client (Web application type). Point the redirect URI to your Django callback.
Choose the necessary scopes (e.g. gmail.readonly, gmail.send, drive.file, calendar.events). Request only what you need.
Build a token storage mechanism in Django (see section 6, database structure). Each user consents with their own Google account; you store the access + refresh token.
4.1 Email (Gmail)
Finish the common setup (Gmail API enabled).
Connect to the Gmail MCP server endpoint (form: gmailmcp.googleapis.com) — see official docs.
Tools: search, read, create drafts, send email.
4.2 Files (Drive)
Common setup (Drive API enabled).
Connect to the Drive MCP server.
Tools: search files, get metadata, read content, create files.
4.3 Calendar
Common setup (Calendar API enabled).
Connect to the Calendar MCP server.
Tools: list events, create, update, delete, manage attendees.
Practical truth: you set up Google once, then the three services are just connections to three different endpoints. You don't build three separate systems.
5. Step-by-step: Your own database (Django MCP server)
This is the opposite direction — you expose your own Django as an MCP server.
pip install django-mcp-server
Add mcp_server to INSTALLED_APPS.
Open an MCP endpoint in your project (a URL under your domain, e.g. /mcp/).
Define which models/operations to expose — read/write permissions. Be careful: you're granting the AI database access.
Add authentication (mandatory). DRF token or OAuth2 is supported via DJANGO_MCP_AUTHENTICATION_CLASSES. <cite index="6-1">The MCP spec recommends an OAuth2 flow, so you can integrate django-oauth-toolkit with its DRF integration and use oauth2_provider.contrib.rest_framework.OAuth2Authentication.</cite>
Use ASGI deployment and configure server affinity at the load balancer. Why: <cite index="1-1">MCP sessions are persisted in RAM and can be lost on server restarts or when routing changes across load-balanced instances, so a client must always connect to the same Django node.</cite>
6. Database structure (proposed schema)
Think of these as Django models. Three core tables:
Table 1: GoogleCredential — per-user OAuth tokens
Field
Type
Notes
user
FK → User
Owner
google_account_email
CharField
Which Google account
access_token
TextField (encrypted)
Current access token
refresh_token
TextField (encrypted)
For refreshing
token_expiry
DateTimeField
Expiry
scopes
JSONField
Granted permissions
created_at / updated_at
DateTimeField
Audit

Store tokens encrypted (e.g. django-fernet-fields or a KMS). This is the most critical security point.
Table 2: MCPServer — registry of servers you connect to
Field
Type
Notes
name
CharField
E.g. "gmail", "drive", "internal-db"
transport
CharField (choices)
http / stdio
url
URLField
Endpoint for HTTP transport
auth_type
CharField
oauth / token / none
auth_ref
CharField
Which credential table it links to
enabled
BooleanField
Active/inactive
config
JSONField
Extra parameters

Table 3: AutomationRun / MCPToolLog — execution log
Field
Type
Notes
user
FK → User
Who started it
mcp_server
FK → MCPServer
Which server
tool_name
CharField
Tool invoked
input_payload
JSONField
Input
output_payload
JSONField
Result
status
CharField
success / error / pending
error_message
TextField
Error detail
started_at / finished_at
DateTimeField
Timing

This log matters for debugging, auditing, and tracking what the AI actually did.
7. Architecture recommendation (where to put it)
The MCP+LLM loop can be long and stateful → run it in a Celery worker (or a separate async service), not in a normal Django view.
Treat each MCP server as a separate HTTP service (not local stdio).
A backend should always pick remote HTTP transport.
User → Django view (accepts request, starts a Celery task)
                  ↓
          Celery worker → LLM API ⇄ MCP servers (Gmail / Drive / Calendar / own DB)
                  ↓
          Result written to DB → returned to user
8. Checklist
[ ] Google Cloud project + APIs enabled
[ ] OAuth consent + client configured
[ ] Token storage (encrypted) ready
[ ] LLM choice: Claude (MCP connector) vs Gemini (function calling)
[ ] MCP server registry (MCPServer table)
[ ] If exposing your own DB server: auth + ASGI + server affinity
[ ] Execution log (AutomationRun)
[ ] Orchestration in a Celery worker

Final notes
Təhlükəsizlik / Security: Never store tokens in plaintext. Always protect the MCP endpoint with authentication.
Prompt injection: MCP servers can fetch external content; be cautious granting the AI full write access to your database.
Başlanğıc / Starting point: Simplest path — Claude's MCP connector + Google's official managed servers.

