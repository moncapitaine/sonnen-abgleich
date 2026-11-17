#!/usr/bin/env bash
# Deploys or updates the app on a Docker Swarm behind caddy-docker-proxy. Run it on the machine that holds .env
# and ./secrets: the Docker CLI reaches the swarm through SWARM_MANAGER (an SSH host), and nothing secret is
# copied to a server as a file. Everything on a server is done through its Docker.
#
#   ./deploy.sh --check    # validates .env and the stack files and says what would be deployed; touches nothing
#   ./deploy.sh            # image, data folder, secrets, docker stack deploy, pinned to the node of the data
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY="$(cd .. && pwd)"

CHECK=false
case "${1:-}" in
  '') ;;
  --check) CHECK=true ;;
  *) echo "Usage: ./deploy.sh [--check]" >&2; exit 2 ;;
esac
fail() { echo "$*" >&2; exit 1; }

[ -f .env ] || fail "There is no .env here. Run ./setup.sh first, then fill in .env."
set -a && source .env && set +a
export STACK_NAME="${STACK_NAME:-sonnen}"

for name in APP_HOST IMAGE DATA_DIR; do
  [ -n "${!name:-}" ] || fail "${name} is not set in .env."
done
case "${DATA_DIR}" in /*) ;; *) fail "DATA_DIR must be an absolute path on the node that runs the service." ;; esac
# Without a password everybody who knows the address could read when the house is occupied.
[ -s secrets/app_password ] || fail "secrets/app_password is missing or empty. ./setup.sh makes it."

# Two Dockers. The manager's takes the stack. The one of the node the service runs on gets the data folder and
# the image, and the service is pinned to that node (below): anywhere else it would start with an empty database.
NODE="${DATA_NODE:-${SWARM_MANAGER:-}}"
on_swarm() { docker ${SWARM_MANAGER:+-H "ssh://${SWARM_MANAGER}"} "$@"; }
on_node() { docker ${NODE:+-H "ssh://${NODE}"} "$@"; }

# A secret of a swarm cannot be changed, so each is named after what is in it.
sha() { sha256sum "$@" 2>/dev/null || shasum -a 256 "$@"; }
fingerprint() { sha "$1" | cut -c1-12; }
export APP_PASSWORD_PRINT="$(fingerprint secrets/app_password)"
FILES=(-c stack.yml)
PORTAL=false
SONNEN_USERNAME_PRINT='' SONNEN_PASSWORD_PRINT=''
if [ -s secrets/sonnen_username ] && [ -s secrets/sonnen_password ]; then
  PORTAL=true
  export SONNEN_USERNAME_PRINT="$(fingerprint secrets/sonnen_username)"
  export SONNEN_PASSWORD_PRINT="$(fingerprint secrets/sonnen_password)"
  FILES+=(-c stack.portal.yml)
fi

# `build` as an image means: this checkout, built on the node that will run it and named after its content.
# Nothing is pushed to a registry, so the swarm must not look the image up there.
RESOLVE=always
BUILD=false
revision=''
if [ "${IMAGE}" = build ]; then
  revision="$(cd "${REPOSITORY}" && find app Dockerfile requirements.txt -type f ! -name '*.pyc' | LC_ALL=C sort | while read -r f; do sha "$f"; done | sha | cut -c1-12)"
  export IMAGE="sonnen-abgleich:${STACK_NAME}-${revision}"
  BUILD=true
  RESOLVE=never
fi

echo "Stack:    ${STACK_NAME} on ${SWARM_MANAGER:-the Docker of this shell}"
echo "App:      https://${APP_HOST}  (${IMAGE})"
echo "Runs on:  ${NODE:-the node of the Docker of this shell}, and only there; data in ${DATA_DIR}"
if ${PORTAL}; then echo "Portal:   daily download from my.sonnen.de at ${SYNC_TIME:-04:20}"
else echo "Portal:   no login in ./secrets - CSV import only (./setup.sh adds it)"; fi

if ${CHECK}; then
  # Reads the files as the deploy would, with every variable filled in. The swarm is not asked anything.
  NODE_ID=known-at-the-deploy docker stack config "${FILES[@]}" > /dev/null
  echo "The stack files are valid. Nothing was deployed."
  exit 0
fi

on_swarm network inspect "${PROXY_NETWORK:-caddy}" > /dev/null 2>&1 || fail "The network \"${PROXY_NETWORK:-caddy}\" of the edge proxy does not exist on this swarm."

# The service is pinned to the node whose Docker gets the folder and the image, by its ID.
NODE_ID="$(on_node info --format '{{.Swarm.NodeID}}')"
[ -n "${NODE_ID}" ] || fail "The Docker of ${NODE:-this shell} is not part of a swarm."
node_name="$(on_swarm node inspect "${NODE_ID}" --format '{{.Description.Hostname}}' 2> /dev/null)" \
  || fail "${NODE:-The Docker of this shell} is not a node of the swarm of ${SWARM_MANAGER:-the Docker of this shell}."
export NODE_ID
echo "Pinned to the node ${node_name} (${NODE_ID})."

if ${BUILD}; then
  echo "Building ${IMAGE} on ${node_name} ..."
  on_node build --quiet --build-arg "REVISION=${revision}" -t "${IMAGE}" "${REPOSITORY}"
fi

# The folder is made by the Docker that was just asked for its ID, and handed to the user the app runs as.
echo "Preparing ${DATA_DIR} on ${node_name} ..."
on_node run --rm --user 0 --entrypoint chown -v "${DATA_DIR}:/data" "${IMAGE}" 10001 /data

echo "Deploying ${STACK_NAME} ..."
on_swarm stack deploy --resolve-image "${RESOLVE}" --prune "${FILES[@]}" "${STACK_NAME}"

# The secrets this stack had before a file changed are removed. Only what is certainly one of them.
replaced="^${STACK_NAME}_(app_password|sonnen_username|sonnen_password)_[0-9a-f]{12}$"
on_swarm secret ls --filter "label=com.docker.stack.namespace=${STACK_NAME}" --format '{{.Name}}' | while read -r secret; do
  [[ "${secret}" =~ ${replaced} ]] || continue
  case "${secret}" in
    "${STACK_NAME}_app_password_${APP_PASSWORD_PRINT}" | "${STACK_NAME}_sonnen_username_${SONNEN_USERNAME_PRINT}" | "${STACK_NAME}_sonnen_password_${SONNEN_PASSWORD_PRINT}") ;;
    *) on_swarm secret rm "${secret}" > /dev/null 2>&1 && echo "Removed the old secret ${secret}" || true ;;
  esac
done

echo "Done. Status: docker stack services ${STACK_NAME}; log: docker service logs -f ${STACK_NAME}_app"
