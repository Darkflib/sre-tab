#!/bin/sh
#
# Promote a published build: resolve a commit to an immutable digest, verify
# that digest was signed by this repository's CI, and rewrite every
# application Quadlet to pin it.
#
#   deploy/scripts/promote.sh                 # whatever origin/main is at
#   deploy/scripts/promote.sh 1a2b3c4         # a specific commit
#   deploy/scripts/promote.sh sha-1a2b3c4...  # the published tag, verbatim
#
# This is the deliberate act that `Pull=newer` on a floating tag used to make
# accidental. Upgrading is now a commit: the digest lands in git, gets
# reviewed like anything else, and CI re-verifies it on every push. Restarting
# a unit no longer changes which build is running.
#
# It writes nothing until cosign has verified the signature, so a digest that
# is not ours cannot be pinned by a typo or by a registry that served
# something unexpected.
#
# Needs: curl, cosign, and git (only to resolve a commit). Run it from a
# checkout, commit the result, then follow the upgrade procedure in
# deploy/README.md.

set -eu

IMAGE_REPO=ghcr.io/darkflib/sre-tab
REGISTRY=ghcr.io
REPO_PATH=darkflib/sre-tab

# Every unit under deploy/quadlet that runs the application image. CI greps
# them all and fails unless they name one distinct reference, so a unit
# missing from this list is not a slow drift — it is the next promotion
# breaking the build, having left that unit pinned to the previous digest.
UNITS='sre-tab.container sre-tab-migrate.container sre-tab-assets.container
sre-tab-prune-sessions.container'

usage() {
    cat <<'EOF'
Usage: deploy/scripts/promote.sh [commit | sha-<commit> | ref@sha256:<digest>]

With no argument, promotes the build published for origin/main.

The published tag is sha-<full commit sha>, written by the publish job in
.github/workflows/ci.yml for every merge to main. A build for a commit that
never reached main was never published and cannot be promoted.
EOF
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
esac

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH='' cd -- "$script_dir/../.." && pwd)
cd "$repo_root"

for tool in curl cosign; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: $tool is required" >&2
        exit 1
    }
done

requested=${1:-}
tag=
digest=

case "$requested" in
    *@sha256:*)
        # A fully pinned reference, pasted from a CI run: take it as given.
        digest=${requested##*@}
        without_digest=${requested%@*}
        case "${without_digest##*/}" in
            *:*) tag=${without_digest##*:} ;;
            *) tag= ;;
        esac
        ;;
    sha-*)
        tag=$requested
        ;;
    "")
        commit=$(git rev-parse origin/main 2>/dev/null) || {
            echo "error: cannot resolve origin/main; pass a commit explicitly" >&2
            exit 1
        }
        tag="sha-$commit"
        ;;
    *)
        commit=$(git rev-parse "$requested" 2>/dev/null) || {
            echo "error: not a commit this checkout knows: $requested" >&2
            exit 1
        }
        tag="sha-$commit"
        ;;
esac

# Anonymous pull token. The package is public; GHCR still wants a bearer
# token for the manifest endpoint. GHCR_TOKEN overrides it if the package is
# ever made private.
registry_token() {
    if [ -n "${GHCR_TOKEN:-}" ]; then
        printf '%s' "$GHCR_TOKEN"
        return 0
    fi
    curl --fail --silent --show-error \
        "https://$REGISTRY/token?service=$REGISTRY&scope=repository:$REPO_PATH:pull" \
        | sed -n 's/.*"token":[[:space:]]*"\([^"]*\)".*/\1/p'
}

if [ -z "$digest" ]; then
    echo "==> resolving $IMAGE_REPO:$tag"
    token=$(registry_token)
    [ -n "$token" ] || {
        echo "error: could not obtain a registry token for $REPO_PATH" >&2
        exit 1
    }

    # --head, so the registry answers with the digest it would serve without
    # sending the manifest body. Every media type this project might ever
    # publish is offered; a registry that cannot satisfy the Accept header
    # returns 404 rather than the wrong manifest.
    digest=$(curl --fail --silent --show-error --head \
        --header "Authorization: Bearer $token" \
        --header 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
        "https://$REGISTRY/v2/$REPO_PATH/manifests/$tag" \
        | tr -d '\r' \
        | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p' \
        | head -1) || {
        echo "error: $IMAGE_REPO:$tag is not published" >&2
        exit 1
    }
fi

case "$digest" in
    sha256:[0-9a-f]*) ;;
    *)
        echo "error: no digest resolved for $IMAGE_REPO:$tag" >&2
        exit 1
        ;;
esac

if [ -n "$tag" ]; then
    reference="$IMAGE_REPO:$tag@$digest"
else
    reference="$IMAGE_REPO@$digest"
fi
echo "    $reference"
echo

# Verification before any file is touched. A digest that will not verify is a
# digest that never enters the repository.
"$script_dir/verify-image.sh" "$reference"
echo

changed=false
for unit in $UNITS; do
    path="deploy/quadlet/$unit"
    current=$(sed -n 's/^Image=\(ghcr\.io\/.*\)$/\1/p' "$path" | head -1)
    if [ "$current" = "$reference" ]; then
        echo "    $unit already pinned"
        continue
    fi
    tmp=$(mktemp "${TMPDIR:-/tmp}/promote.XXXXXX")
    sed "s|^Image=ghcr\.io/.*$|Image=$reference|" "$path" > "$tmp"
    cat "$tmp" > "$path"
    rm -f "$tmp"
    echo "    $unit updated"
    changed=true
done

echo
if [ "$changed" = false ]; then
    echo "Nothing to do: every unit already pins this digest."
    exit 0
fi

cat <<EOF
Pinned $reference

Next:

  git diff deploy/quadlet
  git commit -am 'deploy: promote $tag'

Then on the host, after pulling that commit:

  sudo deploy/install.sh
  sudo systemctl restart sre-tab-migrate.service sre-tab-assets.service \\
      sre-tab.service sre-tab-web.service

sre-tab-prune-sessions.service is timer-driven and not in that list on
purpose: it is not running, so there is nothing to restart. It picks up the
new digest by itself at its next elapse, once install.sh has staged the unit.

Take a backup first if the promoted build carries a migration.
EOF
