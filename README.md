# Guidebook

A framework for building personal web applications. Guidebook is designed
for a single user accessing the app from one or a handful of trusted
browsers — not for multi-tenant or public-facing use. It ships with
strong security defaults: TLS and cookie-based authentication are enabled
out of the box, sessions are locked to a single browser by default, and
login links are one-time use with a short expiration window. Optional
mTLS client certificate authentication is available for an even stronger
auth posture.

Built with FastAPI, SQLite (SQLAlchemy), and Svelte. Forked from
[rigbook](https://github.com/EnigmaCurry/rigbook), with all
domain-specific code replaced by a generic Record model (title, content,
tags) as a starting point — replace it with your own data model and UI.

## Features

- **Personal by default**: TLS, single-session auth, one-time login links, and optional mTLS client certificates
- **Records**: Generic CRUD records (title, content, tags) — replace with your own data model
- **Scratchpad**: Ephemeral shared notepad synced in real time across all connected clients via SSE
- **Multi-database**: Multiple projects with separate SQLite databases and a database picker
- **Themes**: Dark, light, amber, green, blue, and custom theme builder
- **Auto-updates**: Self-updating from GitHub releases
- **SQL Query**: Built-in SQL query interface for advanced data access
- **Notifications**: In-app notification system with SSE real-time updates
- **Auto-backup**: Automatic database backups on a configurable schedule
- **Cross-platform**: Runs on Linux, macOS, and Windows
- **GitHub Actions releases**: Standalone PyInstaller binaries for Linux (amd64/arm64), macOS (universal .pkg), and Windows
- **Claude Code skills**: Built-in slash commands for developer workflow

## Quick Start

```bash
# Install dependencies
uv sync
cd frontend && npm install && cd ..

# Run
just run
```

Open http://localhost:4280 in your browser.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `GUIDEBOOK_DB` | Database name to open | `guidebook` |
| `GUIDEBOOK_PICKER` | Enable database picker mode | `false` |
| `GUIDEBOOK_NO_BROWSER` | Skip opening browser | `false` |
| `GUIDEBOOK_NO_SHUTDOWN` | Disable shutdown endpoint | `false` |
| `GUIDEBOOK_HOST` | Bind address | `127.0.0.1` |
| `GUIDEBOOK_PORT` | Port | `4280` |
| `GUIDEBOOK_BROWSER_URL` | Override browser URL | |
| `GUIDEBOOK_DISABLE_AUTH` | Disable authentication (allow unauthenticated access) | `false` |
| `GUIDEBOOK_AUTH_SLOTS` | Max concurrent sessions | `1` |
| `GUIDEBOOK_AUTH_TTL` | Session cookie TTL in seconds | `315360000` (10 years) |
| `GUIDEBOOK_ALLOW_TRANSFER` | Enable session transfer | `false` |
| `GUIDEBOOK_NO_TLS` | Disable TLS (serve plain HTTP) | `false` |

## Development

```bash
# Frontend dev server with HMR
just dev

# Backend (in another terminal)
uv run guidebook --no-browser

# Lint and format
just check
just fix

# Tests
just test
```

## Building

```bash
# Build standalone binary
just build
```

## License

MIT
