# Guidebook

A web application template with FastAPI, SQLAlchemy, and Svelte.
Forked from [rigbook](https://github.com/EnigmaCurry/rigbook), with all
domain-specific code stripped out and replaced by a generic Record model
(title, content, tags). The Record model and its views are intended as a
starting point — replace them with your own data model and UI to build
your application.

## Features

- **Records**: Generic CRUD records (title, content, tags) — replace with your own data model
- **Multi-database**: Multiple projects with separate SQLite databases and a database picker
- **Themes**: Dark, light, amber, green, blue, and custom theme builder
- **Auto-updates**: Self-updating from GitHub releases
- **SQL Query**: Built-in SQL query interface for advanced data access
- **Notifications**: In-app notification system with SSE real-time updates
- **Auto-backup**: Automatic database backups on a configurable schedule
- **Cross-platform**: Runs on Linux, macOS, and Windows
- **GitHub Actions releases**: Builds standalone PyInstaller binaries for Linux (amd64/arm64), macOS (universal .pkg), and Windows, and publishes them as GitHub release assets on tag push
- **Claude Code skills**: Built-in slash commands for developer workflow — `/master`, `/dev`, `/issue`, `/pr`, `/merge`, `/release`

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
