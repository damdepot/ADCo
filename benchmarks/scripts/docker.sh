#!/bin/bash

set -euo pipefail

op="${1:-}"
container="${2:-}"
service="${3:-}"

exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
compose_file="${exp_path}/docker-compose.yml"

# Prefer docker compose V2, fallback to docker-compose V1 (fixes ContainerConfig / merge_volume_bindings bug on compose V1→V2 migration)
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    COMPOSE="docker-compose"
fi

if [ "$op" == "Restart" ]; then  
    echo '-------------------<< Restarting docker production database >>-------------------'
    docker restart "${container}"

elif [ "$op" == "Down" ]; then  
    echo '-------------------<< Removing docker production database >>-------------------'
    docker stop "${container}" 2>/dev/null || true
    docker rm -v -f "${container}" 2>/dev/null || true
    # stale one-off init container causes 'ContainerConfig' / merge_volume_bindings error after compose V1->V2 upgrade
    docker rm -f adcoexp-db-init 2>/dev/null || true

elif [ "$op" == "Up" ]; then  
    echo '-------------------<< Creating docker production database >>-------------------'
    # pre-clean stale init left from previous V1 run (prevents ContainerConfig error)
    docker rm -f adcoexp-db-init 2>/dev/null || true
    $COMPOSE -p adco-experiments -f "${compose_file}" up -d "${service}"
    if [ "${service}" == "pgdb" ]; then
        $COMPOSE -p adco-experiments -f "${compose_file}" up db-init
    fi

elif [ "$op" == "WaitFor" ]; then
    echo '-------------------<< Waiting for docker production database to become ready >>-------------------'
    deadline=$(( SECONDS + 60 ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        # Empty output means the container has no healthcheck defined.
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${container}" 2>/dev/null || true)"
        if [ "${health}" == "healthy" ]; then
            echo "Container '${container}' is healthy."
            exit 0
        fi
        if [ -z "${health}" ]; then
            # No healthcheck: fall back to probing the Postgres server directly.
            if docker exec "${container}" pg_isready -U postgres >/dev/null 2>&1; then
                echo "Container '${container}' is accepting connections."
                exit 0
            fi
        fi
        sleep 2
    done
    echo "ERROR: timed out after 60s waiting for container '${container}' to become ready." >&2
    exit 1

else     
    echo "Invalid operation: $op. Supported operations are: Restart, Down, Up, WaitFor." >&2
    exit 2
fi
