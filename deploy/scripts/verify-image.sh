#!/bin/sh
#
# Verify that an application image was built and signed by this repository's
# CI workflow.
#
#   deploy/scripts/verify-image.sh                          # the pinned image
#   deploy/scripts/verify-image.sh ghcr.io/darkflib/sre-tab@sha256:...
#   deploy/scripts/verify-image.sh --require-attestations    # CI's mode
#
# Two independent questions are asked, and they are not the same question:
#
#   cosign verify           is there a signature over this digest, made by a
#                           Fulcio certificate whose subject is this
#                           repository's ci.yml on a ref it is allowed to
#                           publish from, and is it recorded in the Rekor
#                           transparency log?
#   gh attestation verify   is there SLSA build provenance, and an SBOM,
#                           issued by GitHub for this digest and this repo?
#
# Neither of them runs when a container starts. Podman cannot express this
# identity in its signature policy — containers-policy.json's `fulcio` block
# requires `subjectEmail`, and a GitHub Actions certificate carries a URI SAN
# instead — so the enforcement point is here, at promotion and in CI, rather
# than at admission. deploy/README.md says so in as many words; do not let a
# green run here be read as "the host checks this on every start", because it
# does not.
#
# Exit status is the whole point: non-zero means do not pin, do not deploy.

set -eu

IMAGE_REPO=ghcr.io/darkflib/sre-tab
SOURCE_REPO=Darkflib/sre-tab
# The signing identity: the signature must come from ci.yml in *this*
# repository, not from any workflow in any repository that happens to have
# pushed to this registry namespace.
#
# A regexp rather than a literal, because there is now more than one ref this
# workflow legitimately signs from. A keyless certificate's subject ends in
# the ref that produced it — `…/ci.yml@refs/heads/main` for a merge,
# `…/ci.yml@refs/tags/v1.1.0` for a release — so a verifier pinned to the
# branch string rejects every tagged release. This is the whole set and
# nothing else: main, or a vMAJOR.MINOR.PATCH tag with an optional
# pre-release suffix. A published build can come from no other ref, because
# the workflow's own `push:` filter and the version resolver in `publish`
# together allow no other.
#
# Anchored at both ends on purpose, and that is not a precaution taken on
# principle. cosign's CheckCertificatePolicy compiles the pattern with
# regexp.Compile and applies it with regex.MatchString(san) — read in
# pkg/cosign/verify.go, not assumed — and MatchString is unanchored, adding
# no `^` or `$` of its own. Without them this would accept any subject merely
# *containing* the string: `https://evil.example/https://github.com/…` and
# `…/ci.yml@refs/heads/main-x` both pass an unanchored version of this
# pattern and both fail the anchored one.
CERT_IDENTITY_RE="^https://github\.com/$SOURCE_REPO/\.github/workflows/ci\.yml@refs/(heads/main|tags/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?)$"
CERT_OIDC_ISSUER=https://token.actions.githubusercontent.com

require_attestations=false
image=

usage() {
    cat <<'EOF'
Usage: deploy/scripts/verify-image.sh [--require-attestations] [image]

  image   an image reference carrying a digest. Defaults to the reference
          pinned in deploy/quadlet/sre-tab.container.

  --require-attestations
          fail if the SLSA provenance and SBOM attestations cannot be
          checked, rather than skipping them when gh(1) is unavailable.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --require-attestations) require_attestations=true ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            usage >&2
            exit 2
            ;;
        *)
            if [ -n "$image" ]; then
                usage >&2
                exit 2
            fi
            image=$1
            ;;
    esac
    shift
done

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH='' cd -- "$script_dir/../.." && pwd)

if [ -z "$image" ]; then
    image=$(sed -n 's/^Image=\(ghcr\.io\/.*\)$/\1/p' \
        "$repo_root/deploy/quadlet/sre-tab.container" | head -1)
    if [ -z "$image" ]; then
        echo "error: no Image= line in deploy/quadlet/sre-tab.container" >&2
        exit 1
    fi
fi

case "$image" in
    *@sha256:*) ;;
    *)
        echo "error: refusing to verify a reference without a digest: $image" >&2
        echo "       a tag can be moved; a digest cannot." >&2
        exit 2
        ;;
esac

# cosign resolves name:tag@digest fine, but gh(1) and the printed output are
# clearer with the tag dropped, and the tag is decoration here in any case.
digest=${image##*@}
by_digest="$IMAGE_REPO@$digest"

if ! command -v cosign >/dev/null 2>&1; then
    echo "error: cosign is not installed." >&2
    echo "       https://docs.sigstore.dev/cosign/system_config/installation/" >&2
    exit 1
fi

echo "==> cosign verify $by_digest"
cosign verify \
    --certificate-identity-regexp "$CERT_IDENTITY_RE" \
    --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
    "$by_digest" >/dev/null
echo "    signature ok: $SOURCE_REPO ci.yml, on refs/heads/main or a version tag"

attest() {
    predicate=$1
    label=$2
    echo "==> gh attestation verify ($label)"
    gh attestation verify "oci://$by_digest" \
        --repo "$SOURCE_REPO" \
        --predicate-type "$predicate" >/dev/null
    echo "    $label ok"
}

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    attest https://slsa.dev/provenance/v1 "SLSA build provenance"
    # Unversioned on purpose. The attestation records
    # https://spdx.dev/Document/v2.3 and gh matches this prefix against it
    # (checked against a published image, not assumed), so an SPDX version
    # bump does not read as a supply-chain failure. The cost is that this
    # asserts "an SPDX document is attested" rather than a specific version.
    attest https://spdx.dev/Document "SPDX SBOM"
elif [ "$require_attestations" = true ]; then
    echo "error: --require-attestations, but gh(1) is missing or not logged in." >&2
    exit 1
else
    echo "==> skipping attestation checks: gh(1) missing or not logged in"
    echo "    the cosign signature above still verified; provenance and the"
    echo "    SBOM did not. Install gh and 'gh auth login' to check them."
fi

echo
echo "verified: $by_digest"
