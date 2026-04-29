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
- **Let's Encrypt**: Optional trusted TLS certificates via [ACME-DNS](https://github.com/joohoi/acme-dns) DNS-01 challenges, with automatic renewal
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

## Site-Specific Browser (SSB)

Guidebook includes an Electron-based site-specific browser for Linux. It
provides a frameless, locked-down window with no browser chrome or
shortcuts — a blank slate for the app.

### Install

```bash
# Install SSB dependencies
just ssb-deps

# Install default instance (bootstraps auth and launches SSB)
just ssb-install

# Install a named instance
just ssb-install foo
```

Each instance gets a unique loopback address (e.g. `127.172.189.26:4280`)
derived from its name, so multiple instances can run simultaneously on
the same port without conflict.

After install, the launcher appears in rofi (or any XDG-compliant app
launcher) as "Guidebook" or "Guidebook-Foo". The launcher starts the
server automatically if it isn't already running.

### Remote connections

Create a launcher that connects to a remote Guidebook server (no local
server management):

```bash
just ssb-connect myserver 10.0.0.5 4280
```

### Keyboard shortcuts

| Key | Action |
|---|---|
| `Alt+W` | Open a new window (same session) |

### Uninstall

```bash
just ssb-uninstall          # Remove default launcher
just ssb-uninstall foo      # Remove named launcher
```

### Configuration

The SSB defaults to 2x scaling. Override via `--scale` or environment:

```bash
GUIDEBOOK_SSB_SCALE=1.5 guidebook-ssb-foo
```

## License

MIT
