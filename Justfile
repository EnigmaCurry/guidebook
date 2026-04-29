# Guidebook - Web Application Template
build_test_repo := "EnigmaCurry/guidebook-build-test"

# Show available recipes
@default:
    just --list

_check-uv:
    @command -v uv >/dev/null 2>&1 || { echo "Error: uv is not installed. Install it from https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }

_check-node:
    @command -v node >/dev/null 2>&1 || { echo "Error: node is not installed. Install it from https://nodejs.org/"; exit 1; }
    @command -v npm >/dev/null 2>&1 || { echo "Error: npm is not installed. It should come with node — reinstall from https://nodejs.org/"; exit 1; }

_check-curl:
    @command -v curl >/dev/null 2>&1 || { echo "Error: curl is not installed. Install it with your package manager (e.g. apt install curl)"; exit 1; }

# Install all dependencies
deps: _check-uv _check-node
    uv sync
    cd frontend && npm install

# Run the server (builds frontend first)
run *ARGS: _check-uv build-frontend
    uv run guidebook {{ ARGS }}

# Build the frontend (skips if sources unchanged)
build-frontend: _check-node
    @test -d frontend/node_modules || { echo "Error: frontend dependencies not installed. Run 'just deps' first."; exit 1; }
    @hash=$(find frontend/src frontend/index.html frontend/package.json frontend/vite.config.js -type f 2>/dev/null | sort | xargs cat | sha256sum | cut -d' ' -f1); \
    if [ -f .build-frontend.stamp ] && [ "$(cat .build-frontend.stamp)" = "$hash" ]; then \
        echo "frontend: up to date"; \
    else \
        cd frontend && npm run build && cd .. && echo "$hash" > .build-frontend.stamp; \
    fi

# Build a standalone binary with PyInstaller (skips if sources unchanged)
build-binary: _check-uv build-frontend
    @hash=$(find src/guidebook -type f -name '*.py' 2>/dev/null | sort | xargs cat | cat - guidebook.spec .build-frontend.stamp 2>/dev/null | sha256sum | cut -d' ' -f1); \
    if [ -f .build-binary.stamp ] && [ "$(cat .build-binary.stamp)" = "$hash" ]; then \
        echo "binary: up to date"; \
    else \
        uv sync --group build && uv run pyinstaller guidebook.spec && echo "$hash" > .build-binary.stamp; \
    fi

# Build everything (frontend + binary)
build: build-frontend build-binary

# Run tests
test: _check-uv
    uv run pytest

# Lint and format check
check: _check-uv
    uv run ruff check .
    uv run ruff format --check .

# Auto-fix lint and formatting
fix: _check-uv
    uv run ruff check --fix .
    uv run ruff format .

# Show current settings
config-show: _check-curl
    @curl -s http://localhost:4280/api/settings | python -m json.tool

# Reset local dev branch to match remote
reset-dev:
    git fetch origin
    git checkout dev
    git reset --hard origin/dev

# Print the next .devN version for test releases (e.g. 0.1.0.dev3)
next-dev-version:
    #!/usr/bin/env bash
    set -euo pipefail
    BASE=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
    EXISTING=$(gh release list --repo {{ build_test_repo }} --json tagName --limit 100 --jq '.[].tagName' 2>/dev/null || true)
    MAX=-1
    for tag in $EXISTING; do
        if [[ "$tag" =~ ^v${BASE}\.dev([0-9]+)$ ]]; then
            N=${BASH_REMATCH[1]}
            (( N > MAX )) && MAX=$N
        fi
    done
    echo "${BASE}.dev$(( MAX + 1 ))"

_check-docker:
    @command -v docker >/dev/null 2>&1 || { echo "Error: docker is not installed."; exit 1; }

# Build the Docker image from local source
docker-build: _check-docker
    docker compose build

# Build and install on the current Docker context
docker-install: docker-build
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose up -d
    endpoint=$(docker context inspect --format '{{"{{.Endpoints.docker.Host}}"}}')
    case "$endpoint" in
        unix://*) host=localhost ;;
        ssh://*)  host=$(echo "$endpoint" | sed 's|ssh://\([^@]*@\)\?\([^:/]*\).*|\2|') ;;
        tcp://*)  host=$(echo "$endpoint" | sed 's|tcp://\([^:/]*\).*|\1|') ;;
        *)        host=localhost ;;
    esac
    port=${GUIDEBOOK_PORT:-4280}
    echo "Open https://${host}:${port}"

# Reset authentication (interactive, restarts service after)
docker-reset-auth: _check-docker
    docker compose stop guidebook
    docker compose run --rm -it -e GUIDEBOOK_RESET_AUTH_ONLY=true guidebook .venv/bin/guidebook --reset-auth
    docker compose start guidebook

# Follow container logs
docker-logs: _check-docker
    docker compose logs -f

# Remove containers (preserves volumes)
docker-uninstall: _check-docker
    docker compose down

# Destroy containers and volumes (with confirmation)
docker-destroy: _check-docker
    #!/usr/bin/env bash
    set -euo pipefail
    read -rp "This will destroy all guidebook containers AND volumes. Are you sure? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        docker compose down -v
        echo "Destroyed."
    else
        echo "Cancelled."
    fi

# Install SSB (site-specific browser) dependencies
ssb-deps: _check-node
    cd ssb && npm install

# Run Guidebook in the SSB (e.g. just ssb --host 192.168.1.50 --port 8443)
ssb *ARGS: _check-node
    cd ssb && npx electron . {{ ARGS }}

# Build SSB AppImage
ssb-build: _check-node
    cd ssb && npx electron-builder --linux

# Derive a 127.x.x.x address from an instance name
_instance-host instance:
    #!/usr/bin/env bash
    instance="{{ instance }}"
    if [[ "$instance" == "default" ]]; then
        echo "127.0.0.1"
    else
        hash=$(echo -n "$instance" | md5sum | cut -c1-6)
        dec=$(( 16#$hash ))
        # Map to 127.0.0.2 – 127.255.255.254 (avoid .0.0.0, .0.0.1, .255.255.255)
        dec=$(( (dec % 16777213) + 2 ))
        echo "127.$(( (dec >> 16) & 255 )).$(( (dec >> 8) & 255 )).$(( dec & 255 ))"
    fi

# Install local dev SSB launcher (e.g. just ssb-install, just ssb-install foo)
ssb-install instance="default": ssb-deps build-frontend
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p ~/.local/bin ~/.local/share/applications
    instance="{{ instance }}"
    if [[ ! "$instance" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        echo "Error: instance name must contain only letters, digits, hyphens, and underscores" >&2
        exit 1
    fi
    host=$(just _instance-host "$instance")
    port=4280
    project_dir="$(pwd)"
    ssb_dir="$(pwd)/ssb"
    if [[ "$instance" == "default" ]]; then
        suffix=""
        name="Guidebook"
    else
        suffix="-${instance}"
        # Capitalize first letter of instance name
        pretty="$(echo "${instance:0:1}" | tr '[:lower:]' '[:upper:]')${instance:1}"
        name="Guidebook-${pretty}"
    fi
    launcher=~/.local/bin/guidebook-ssb${suffix}
    desktop=~/.local/share/applications/guidebook-ssb${suffix}.desktop
    # Generate launcher with paths baked in
    sed -e "s|__SSB_DIR__|${ssb_dir}|g" \
        -e "s|__PROJECT_DIR__|${project_dir}|g" \
        -e "s|__INSTANCE__|${instance}|g" \
        -e "s|__HOST__|${host}|g" \
        -e "s|__PORT__|${port}|g" \
        ssb/guidebook-ssb > "$launcher"
    chmod +x "$launcher"
    # Install desktop entry
    sed -e "s|Exec=guidebook-ssb|Exec=${launcher}|" \
        -e "s|Name=Guidebook|Name=${name}|" \
        ssb/guidebook-ssb.desktop > "$desktop"
    echo "Installed: $launcher (instance=${instance}, host=${host}:${port})"
    echo "Installed: $desktop"
    # Bootstrap auth and launch SSB on first install
    echo ""
    echo "Starting first-run auth setup..."
    GUIDEBOOK_HOST="$host" uv run guidebook --reset-auth --ssb --port "$port" --instance "$instance"

# Uninstall an SSB launcher (e.g. just ssb-uninstall, just ssb-uninstall foo)
ssb-uninstall instance="default":
    #!/usr/bin/env bash
    set -euo pipefail
    instance="{{ instance }}"
    if [[ "$instance" == "default" ]]; then
        suffix=""
    else
        suffix="-${instance}"
    fi
    launcher=~/.local/bin/guidebook-ssb${suffix}
    desktop=~/.local/share/applications/guidebook-ssb${suffix}.desktop
    removed=false
    for f in "$launcher" "$desktop"; do
        if [[ -f "$f" ]]; then
            rm "$f"
            echo "Removed: $f"
            removed=true
        fi
    done
    if [[ "$removed" == false ]]; then
        echo "Nothing to uninstall for instance '${instance}'"
    fi

# Create a remote SSB launcher (e.g. just ssb-connect myserver 10.0.0.5 4280)
ssb-connect name host port="4280" scale="2": ssb-deps
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p ~/.local/bin ~/.local/share/applications
    ssb_dir="$(pwd)/ssb"
    launcher=~/.local/bin/guidebook-ssb-{{ name }}
    cat > "$launcher" <<SCRIPT
    #!/usr/bin/env bash
    set -euo pipefail
    cd "${ssb_dir}"
    exec npx electron . --host {{ host }} --port {{ port }} --scale {{ scale }} "\$@"
    SCRIPT
    chmod +x "$launcher"
    cat > ~/.local/share/applications/guidebook-ssb-{{ name }}.desktop <<DESKTOP
    [Desktop Entry]
    Name=Guidebook ({{ name }})
    Comment=Guidebook on {{ host }}:{{ port }}
    Exec=${HOME}/.local/bin/guidebook-ssb-{{ name }}
    Icon=guidebook
    Type=Application
    Categories=Utility;
    StartupWMClass=guidebook-ssb
    DESKTOP
    echo "Installed: $launcher"
    echo "Installed: ~/.local/share/applications/guidebook-ssb-{{ name }}.desktop"

# Remove build artifacts and stamp files
clean:
    rm -rf dist/ build/
    rm -f .build-*.stamp
    @echo "Build artifacts cleaned."
