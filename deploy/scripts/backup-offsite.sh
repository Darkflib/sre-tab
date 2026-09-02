#!/bin/sh
#
# Copy the newest database dump off-host, and prove it arrived intact.
#
# Runs on the HOST, not in a container, and not on the schedule: it is pulled
# by `OnSuccess=` from sre-tab-backup.service, so it runs when and only when a
# backup has just succeeded. See deploy/systemd/sre-tab-backup-offsite.service
# and the drop-in beside it.
#
# Why the host and not a container. The backup itself runs inside the pinned
# postgres image with ReadOnly=true and DropCapability=all; that image has no
# ssh client and no S3 client, and putting one in it would mean maintaining a
# derived image of the database server in order to move a file. A second image
# would mean a second registry dependency for a job whose entire content is
# "read a file, send it, ask the far end what it got". curl, openssl, ssh, and
# the coreutils below are already on any host that can run this deployment.
#
# Installed to /etc/sre-tab/backup-offsite.sh; configuration arrives through
# EnvironmentFile=/etc/sre-tab/backup-offsite.env, which systemd reads as root
# before dropping to the unprivileged service user. Nothing secret is ever
# passed on a command line -- argv is readable by every process on the host,
# which is the same reason create-secrets.sh takes the OAuth secret on stdin.
#
# THE POINT OF THIS SCRIPT IS THE VERIFICATION, NOT THE COPY. An upload that
# exited zero is not evidence: scp reports on the transfer, and an S3 PUT
# reports on the request. Both transports below therefore end by asking the
# far end what it is holding and comparing that against the digest computed
# here, and neither treats a successful send as an answer.
#
# Run it by hand exactly as systemd does:
#
#     sudo systemctl start sre-tab-backup-offsite.service
#     journalctl -u sre-tab-backup-offsite.service -n 50
#
# or, with the environment file sourced into the shell, as the service user:
#
#     sudo -u sre-tab-offsite env $(sudo cat /etc/sre-tab/backup-offsite.env \
#         | grep -v '^#' | xargs) /etc/sre-tab/backup-offsite.sh

set -eu

# The dumps are 0600 and this script only ever reads them, but it also writes
# curl configuration files carrying a request signature. 077 for both.
umask 077

: "${BACKUP_DIR:=/srv/sre-tab/backups}"
: "${BACKUP_DATABASE:=sretab}"
: "${BACKUP_KEEP_DAYS:=14}"
: "${BACKUP_OFFSITE_TARGETS:=}"

# A dump that is not from last night means the pipeline is broken somewhere
# this script cannot see -- the timer disabled, the database container gone,
# the host's clock wrong -- and copying a stale dump while exiting zero is
# precisely the reassuring lie this feature exists to remove. 48 hours rather
# than 24 so a single missed night is not an alert. Set to 0 to disable, which
# is worth doing only when deliberately re-sending an old dump by hand.
: "${OFFSITE_MAX_DUMP_AGE_HOURS:=48}"

: "${OFFSITE_SSH_KEY:=/etc/sre-tab/backup-offsite.key}"
: "${OFFSITE_SSH_KNOWN_HOSTS:=/etc/sre-tab/backup-offsite.known_hosts}"
: "${OFFSITE_SSH_CONNECT_TIMEOUT:=30}"

: "${OFFSITE_S3_ENDPOINT:=}"
: "${OFFSITE_S3_REGION:=us-east-1}"
: "${OFFSITE_S3_ADDRESSING:=auto}"
: "${OFFSITE_S3_ACCESS_KEY_ID:=}"
: "${OFFSITE_S3_SECRET_ACCESS_KEY:=}"
: "${OFFSITE_S3_PRUNE:=false}"
: "${OFFSITE_TIMEOUT_SECONDS:=3600}"

log() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; }

# Scratch space. Under systemd this is RuntimeDirectory= -- 0700, owned by the
# service user, on tmpfs, and removed by systemd when the unit stops. The
# fallback is for a by-hand run outside systemd.
if [ -n "${RUNTIME_DIRECTORY:-}" ]; then
    work=${RUNTIME_DIRECTORY%%:*}
else
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT INT TERM
fi

# ---------------------------------------------------------------------------
# Pick the dump
# ---------------------------------------------------------------------------

if [ -z "$BACKUP_OFFSITE_TARGETS" ]; then
    # Reaching here means /etc/sre-tab/backup-offsite.env exists -- the unit
    # carries ConditionPathExists= on it, so an operator who has not asked for
    # off-host copies never runs this at all. Having written that file and
    # left it empty is a mistake, not a posture, and it is the exact shape of
    # a gate that reports success having done nothing.
    err "BACKUP_OFFSITE_TARGETS is empty in /etc/sre-tab/backup-offsite.env"
    err "       Set at least one ssh:// or s3:// target, or remove the file."
    exit 1
fi

if [ ! -d "$BACKUP_DIR" ]; then
    err "$BACKUP_DIR does not exist"
    exit 1
fi

# The permission problem is diagnosed here, before anything looks for a dump.
# /srv/sre-tab/backups is 0700 and owned by uid 999 on purpose -- see
# deploy/install.sh -- and this unit's user reads it only through a POSIX ACL
# that install.sh grants. `install -d -m 0700` resets the ACL mask to `---`
# and silently masks that grant out, so an installer re-run is how this breaks.
#
# Observed on a test host, which is why the check is here and not left to the
# `find` below: with the mask reset, find simply matches nothing and the run
# ends with "no dump in this directory". That is a true statement about what
# find could see and a completely misleading one about what is wrong, and it
# points an operator at the backup job when the fault is the reader. The same
# mistake cost time in restore.sh, where an EACCES was reported as a checksum
# mismatch.
if [ ! -r "$BACKUP_DIR" ] || [ ! -x "$BACKUP_DIR" ]; then
    err "cannot read $BACKUP_DIR as $(id -un)"
    err "       This is a permission problem, not a missing backup. The ACL"
    err "       that grants this user read access is re-applied by the"
    err "       installer, and reset by any chmod of the directory:"
    err "         getfacl $BACKUP_DIR"
    err "         deploy/install.sh"
    exit 1
fi

# Newest by NAME, not by mtime. backup.sh stamps every dump with a UTC
# timestamp in a fixed-width format, so lexicographic order is chronological
# order, and a name cannot be changed by a stray `touch`, a restore, or a
# filesystem copy that did not preserve times. Only the published naming
# scheme is considered, so an operator's own file parked in this directory is
# never picked up -- the same discipline backup.sh applies to retention.
dump=$(find "$BACKUP_DIR" -maxdepth 1 -type f \
    -name "$BACKUP_DATABASE-*.dump" 2>/dev/null | sort | tail -1)

if [ -z "$dump" ]; then
    err "no $BACKUP_DATABASE-*.dump in $BACKUP_DIR"
    exit 1
fi

name=$(basename "$dump")
sidecar="$dump.sha256"

if [ ! -f "$sidecar" ]; then
    # backup.sh writes the sidecar immediately after the atomic rename, so a
    # dump without one is either mid-flight or came from somewhere else.
    # Sending it would mean sending something the far end cannot check.
    err "no checksum sidecar for $name; refusing to send an unverifiable dump"
    exit 1
fi

if [ ! -r "$dump" ] || [ ! -r "$sidecar" ]; then
    # /srv/sre-tab/backups is 0700 and owned by uid 999, deliberately -- see
    # deploy/install.sh. This unit's user reads it through a POSIX ACL that
    # install.sh grants and re-grants on every run. `install -d -m 0700` sets
    # the ACL mask to --- and silently masks the grant out, so the way this
    # breaks is an installer re-run, and the way to fix it is another.
    err "cannot read $name as $(id -un)"
    err "       Re-run deploy/install.sh, then check:"
    err "         getfacl $BACKUP_DIR"
    exit 1
fi

bytes=$(wc -c < "$dump" | tr -d ' ')

# The digest every comparison below is made against. Computed here, once, from
# the local file -- not read out of the sidecar, so a sidecar that disagreed
# with its dump could not launder a corrupt copy through this script. The
# sidecar is sent as well, because the far end needs it to check on its own.
local_hex=$(sha256sum "$dump" | cut -d ' ' -f 1)
sidecar_hex=$(cut -d ' ' -f 1 < "$sidecar")

if [ "$local_hex" != "$sidecar_hex" ]; then
    err "$name does not match its own .sha256 sidecar"
    err "       local  $local_hex"
    err "       sidecar $sidecar_hex"
    exit 1
fi

if [ "$OFFSITE_MAX_DUMP_AGE_HOURS" -gt 0 ]; then
    dump_mtime=$(date -r "$dump" +%s)
    age_hours=$(( ( $(date +%s) - dump_mtime ) / 3600 ))
    if [ "$age_hours" -gt "$OFFSITE_MAX_DUMP_AGE_HOURS" ]; then
        err "the newest dump is ${age_hours}h old (limit ${OFFSITE_MAX_DUMP_AGE_HOURS}h)"
        err "       $name"
        err "       Something upstream has stopped producing backups. Copying"
        err "       this one and exiting zero would report a healthy pipeline."
        err "       Check: systemctl list-timers 'sre-tab-*'"
        exit 1
    fi
fi

log "off-host copy: $name ($bytes bytes, sha256 $local_hex)"

# ---------------------------------------------------------------------------
# ssh transport
# ---------------------------------------------------------------------------
#
# The far end runs deploy/scripts/backup-offsite-receive.sh as a FORCED
# COMMAND, so this key cannot obtain a shell there and the verbs below are the
# entire vocabulary it has. There is deliberately no verb that deletes: a
# machine holding a live database is exactly the machine an attacker reaches
# first, and a credential that can erase the off-host copies turns disaster
# recovery into a second thing to lose. Retention at the far end is the far
# end's own decision, taken after it has verified the new dump.
#
# Why a receiving script rather than a forced `rsync --server` or `scp -t`.
# Those move bytes and nothing else, and this feature is not about moving
# bytes -- it is about the far end asserting what it holds. Verification means
# executing something over there, and bolting a second, unrestricted key onto
# the account for "just run sha256sum" would hand back the arbitrary-command
# surface the forced command exists to remove. One script that can store,
# check, and report is a smaller attack surface than a file mover plus a way
# to run commands, and it is the piece that makes the append-only rule
# enforceable at all: `put` refuses to overwrite a name that already exists.

ssh_run() {
    # $1 remote verb line; stdin is the body for `put`.
    # -F none so a stray /etc/ssh/ssh_config cannot reintroduce an agent, a
    # different key, or a ProxyCommand. StrictHostKeyChecking=yes with an
    # operator-supplied known_hosts: trust-on-first-use would hand the dumps
    # to whoever answered on the night the file was empty.
    ssh -F none \
        -i "$ssh_key" \
        -o IdentitiesOnly=yes \
        -o IdentityAgent=none \
        -o BatchMode=yes \
        -o PasswordAuthentication=no \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$OFFSITE_SSH_KNOWN_HOSTS" \
        -o ClearAllForwardings=yes \
        -o ConnectTimeout="$OFFSITE_SSH_CONNECT_TIMEOUT" \
        -p "$ssh_port" \
        "$ssh_user@$ssh_host" "$1"
}

send_ssh() {
    # $1 = ssh://user@host[:port]/remote/dir
    ssh_rest=${1#ssh://}
    ssh_authority=${ssh_rest%%/*}
    ssh_path="/${ssh_rest#*/}"
    ssh_user=${ssh_authority%@*}
    ssh_hostport=${ssh_authority#*@}

    case "$ssh_authority" in
        *@*) : ;;
        *) err "ssh target needs a user: ssh://user@host/path"; return 1 ;;
    esac
    case "$ssh_rest" in
        */*) : ;;
        *) err "ssh target needs the far-end directory: ssh://user@host/path"; return 1 ;;
    esac

    case "$ssh_hostport" in
        *:*) ssh_host=${ssh_hostport%:*}; ssh_port=${ssh_hostport##*:} ;;
        *) ssh_host=$ssh_hostport; ssh_port=22 ;;
    esac

    ssh_key=$OFFSITE_SSH_KEY
    if [ ! -r "$ssh_key" ]; then
        err "cannot read the ssh key $ssh_key as $(id -un)"
        return 1
    fi
    if [ ! -r "$OFFSITE_SSH_KNOWN_HOSTS" ]; then
        err "cannot read $OFFSITE_SSH_KNOWN_HOSTS"
        err "       Pin the far end's host key before trusting it with dumps:"
        err "         ssh-keyscan -p $ssh_port $ssh_host > $OFFSITE_SSH_KNOWN_HOSTS"
        err "       and compare the fingerprint against the far host itself."
        return 1
    fi

    log "  ssh://$ssh_user@$ssh_hostport$ssh_path"

    # The directory travels with every verb and the far end compares it against
    # its own configured destination, refusing anything else. It is therefore
    # not an instruction -- the far end already knows where it puts things --
    # but an assertion, which means a target URL that names the wrong directory
    # fails loudly here instead of quietly writing somewhere unexpected.

    # Sidecar first, deliberately. `put` on the dump verifies the bytes it has
    # just received against the sidecar already on disk and refuses to publish
    # a dump that does not match, so the far end never holds an unchecked file
    # under a published name, and an interrupted transfer leaves only a
    # .partial for the far end to sweep. The other order would leave a
    # truncated dump published, and the append-only rule would then prevent a
    # re-run from ever repairing it.
    # Every remote call is checked for its own exit status before its output is
    # looked at. A pipeline would hide it: `ssh ... | sed` reports on sed, and
    # a refused forced command would read as an empty answer rather than as a
    # failure.
    if ! ssh_sidecar_stat=$(ssh_run "stat $ssh_path $name.sha256"); then
        err "  the far end refused 'stat'; check the forced command and the key"
        return 1
    fi
    if [ "$ssh_sidecar_stat" = absent ]; then
        ssh_run "put $ssh_path $name.sha256" < "$sidecar" >/dev/null || return 1
        log "    sent $name.sha256"
    else
        log "    $name.sha256 already present"
    fi

    ssh_stat=$(ssh_run "stat $ssh_path $name") || return 1
    if [ "$ssh_stat" = absent ]; then
        ssh_run "put $ssh_path $name" < "$dump" >/dev/null || return 1
        log "    sent $name"
    else
        # Idempotent re-run. Not trusted on the strength of being present:
        # the verify below re-reads it at the far end regardless.
        log "    $name already present at the far end ($ssh_stat)"
    fi

    # The assertion. `verify` re-reads the file from the far end's disk, runs
    # sha256sum -c against the sidecar there, and prints the digest it derived.
    # Comparing that value against the one computed here is the check; the exit
    # status of the transfer is not, and neither is the exit status of the
    # remote sha256sum on its own.
    if ! ssh_report=$(ssh_run "verify $ssh_path $name"); then
        err "  the far end failed to verify $name"
        printf '%s\n' "$ssh_report" >&2
        return 1
    fi
    ssh_verified=$(printf '%s\n' "$ssh_report" | sed -n 's/^verified //p')
    if [ -z "$ssh_verified" ]; then
        err "  the far end did not report a digest for $name"
        return 1
    fi
    if [ "$ssh_verified" != "$local_hex" ]; then
        err "  far-end digest does not match: $ssh_verified != $local_hex"
        return 1
    fi
    log "    VERIFIED at the far end: $ssh_verified"
    printf '%s\n' "$ssh_report" | sed -n 's/^pruned /    pruned /p'

    # Retention is the far end's, run by the far end after that verification.
    # Reported here so the sending host's journal shows what the receiving
    # host now holds, which is the thing an operator actually wants to know
    # and cannot otherwise see without logging in.
    ssh_list=$(ssh_run "list $ssh_path") || return 1
    printf '%s\n' "$ssh_list" | sed 's/^/    far end: /'
}

# ---------------------------------------------------------------------------
# S3 transport
# ---------------------------------------------------------------------------
#
# Signed here with curl and openssl rather than through the AWS CLI. That is
# not a preference for hand-rolling: on Debian 13 `apt-get install awscli`
# pulls 23 packages and 144MB, including a second Python and a cryptography
# stack, onto a host whose entire design is that everything runs in a
# container with all capabilities dropped. It also spools, and the unit sets
# PrivateTmp=true -- a private /tmp AND /var/tmp, discarded at exit with a
# zero status, which is the worst possible combination with a tool that writes
# there. Nothing below spools: the dump is streamed from the file by curl and
# the digests come from sha256sum.
#
# The failure mode of a signing mistake is the safe one. A wrong signature is
# refused by the object store, so the request fails loudly and this script
# exits non-zero; there is no path by which a signing bug produces a copy that
# is believed verified. The comparison itself -- the part that could fail
# towards a false pass -- is our own code either way, and it is the part the
# corruption tests in deploy/README.md exercise.
#
# The S3-compatible case is the one that matters here. Self-hosters reach for
# MinIO, Garage, Backblaze B2, Wasabi, and Hetzner far more often than for AWS
# proper, so OFFSITE_S3_ENDPOINT is a first-class setting rather than an
# escape hatch, and path-style addressing is the default whenever it is set.

s3_hmac_hex() {
    # $1 hex key; message on stdin; hex MAC on stdout. od rather than xxd:
    # xxd moved out of vim-common into its own package and is not installed by
    # default on Debian 13, whereas od is coreutils.
    openssl dgst -sha256 -mac HMAC -macopt "hexkey:$1" -binary \
        | od -An -v -tx1 | tr -d ' \n'
}

s3_sha256_hex() { sha256sum | cut -d ' ' -f 1; }

# SHA-256 of the empty string, which is the payload hash for every request
# below that has no body.
S3_EMPTY_HASH=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855

s3_sign() {
    # $1 method, $2 canonical URI, $3 canonical query, $4 payload hash,
    # $5 one extra signed header as "name:value", or "".
    #
    # The only extra headers this script signs are x-amz-checksum-sha256 and
    # x-amz-checksum-mode. Both sort between "host" and "x-amz-content-sha256",
    # so the canonical header block is assembled in a fixed order rather than
    # sorted. A third header would need real sorting, and this comment is
    # where that would be noticed.
    s3_method=$1
    s3_uri=$2
    s3_query=$3
    s3_payload=$4
    s3_extra=$5

    s3_amzdate=$(date -u +%Y%m%dT%H%M%SZ)
    s3_datestamp=$(printf '%s' "$s3_amzdate" | cut -c1-8)

    if [ -n "$s3_extra" ]; then
        s3_extra_name=${s3_extra%%:*}
        s3_extra_value=${s3_extra#*:}
        s3_headers=$(printf 'host:%s\n%s:%s\nx-amz-content-sha256:%s\nx-amz-date:%s' \
            "$s3_host" "$s3_extra_name" "$s3_extra_value" "$s3_payload" "$s3_amzdate")
        s3_signed="host;$s3_extra_name;x-amz-content-sha256;x-amz-date"
    else
        s3_headers=$(printf 'host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s' \
            "$s3_host" "$s3_payload" "$s3_amzdate")
        s3_signed="host;x-amz-content-sha256;x-amz-date"
    fi

    # CanonicalHeaders ends with a newline of its own, hence the blank line
    # before SignedHeaders. Getting this wrong is the classic SigV4 mistake
    # and it shows up as a refused request, never as a bad one.
    s3_canonical=$(printf '%s\n%s\n%s\n%s\n\n%s\n%s' \
        "$s3_method" "$s3_uri" "$s3_query" "$s3_headers" "$s3_signed" "$s3_payload")
    s3_canonical_hash=$(printf '%s' "$s3_canonical" | s3_sha256_hex)

    s3_scope="$s3_datestamp/$OFFSITE_S3_REGION/s3/aws4_request"
    s3_to_sign=$(printf 'AWS4-HMAC-SHA256\n%s\n%s\n%s' \
        "$s3_amzdate" "$s3_scope" "$s3_canonical_hash")

    # The signing key. openssl takes an HMAC key only on its command line, so
    # the derived per-day key is briefly visible in argv -- the secret itself
    # never is, because it is converted to hex here and only the derived keys
    # are passed on. This is the one place the argv discipline is not absolute,
    # and it is the reason the IAM policy in deploy/README.md is load-bearing
    # rather than belt-and-braces: a local unprivileged reader who catches the
    # key gets, at worst, the ability to write objects under one prefix of one
    # bucket for the rest of the UTC day, and with Object Lock on it cannot
    # touch what is already there.
    s3_key=$(printf '%s' "AWS4$OFFSITE_S3_SECRET_ACCESS_KEY" \
        | od -An -v -tx1 | tr -d ' \n')
    s3_key=$(printf '%s' "$s3_datestamp" | s3_hmac_hex "$s3_key")
    s3_key=$(printf '%s' "$OFFSITE_S3_REGION" | s3_hmac_hex "$s3_key")
    s3_key=$(printf '%s' s3 | s3_hmac_hex "$s3_key")
    s3_key=$(printf '%s' aws4_request | s3_hmac_hex "$s3_key")
    s3_signature=$(printf '%s' "$s3_to_sign" | s3_hmac_hex "$s3_key")

    # Everything sensitive goes into a 0600 config file in RuntimeDirectory=
    # rather than onto curl's command line.
    # The canonical URI and the request URI are the same string by
    # construction: path-style puts the bucket in the path and leaves the host
    # alone, virtual-hosted puts it in the host and leaves the path alone.
    s3_config="$work/curl.conf"
    {
        if [ -n "$s3_query" ]; then
            printf 'url = "%s%s?%s"\n' "$s3_base" "$s3_uri" "$s3_query"
        else
            printf 'url = "%s%s"\n' "$s3_base" "$s3_uri"
        fi
        # Host is set explicitly rather than left to curl to derive from the
        # URL: the signature covers the exact byte string, and a mismatch
        # between what was signed and what was sent is refused with a message
        # that does not say which of the two was wrong.
        printf 'header = "Host: %s"\n' "$s3_host"
        printf 'header = "x-amz-date: %s"\n' "$s3_amzdate"
        printf 'header = "x-amz-content-sha256: %s"\n' "$s3_payload"
        if [ -n "$s3_extra" ]; then
            printf 'header = "%s: %s"\n' "$s3_extra_name" "$s3_extra_value"
        fi
        printf 'header = "Authorization: AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"\n' \
            "$OFFSITE_S3_ACCESS_KEY_ID" "$s3_scope" "$s3_signed" "$s3_signature"
        # A signature is valid inside a 15-minute clock-skew window, so a
        # couple of retries for a dropped connection are free; a nightly job
        # that alerts on one lost packet trains its operator to ignore it.
        printf 'silent\nshow-error\nretry = 3\nretry-delay = 5\n'
        printf 'connect-timeout = 30\nmax-time = %s\n' "$OFFSITE_TIMEOUT_SECONDS"
    } > "$s3_config"
}

s3_header_value() {
    # $1 header name, from the dumped response headers. Case-insensitive, and
    # the last occurrence wins, so a 100-continue preamble cannot confuse it.
    tr -d '\r' < "$work/head" \
        | grep -i "^$1:" | tail -1 | sed "s/^[^:]*:[[:space:]]*//"
}

s3_fail_body() {
    # S3 error bodies are XML with a <Code> and a <Message>. Print both rather
    # than the whole document, which is mostly request ids.
    if [ -s "$work/body" ]; then
        sed -n 's|.*<Code>\([^<]*\)</Code>.*|  code: \1|p;s|.*<Message>\([^<]*\)</Message>.*|  message: \1|p' \
            "$work/body" >&2 || true
    fi
}

send_s3() {
    # $1 = s3://bucket/prefix
    s3_rest=${1#s3://}
    s3_bucket=${s3_rest%%/*}
    if [ "$s3_bucket" = "$s3_rest" ]; then
        s3_prefix=
    else
        s3_prefix=${s3_rest#*/}
        s3_prefix=${s3_prefix%/}
    fi

    if [ -z "$OFFSITE_S3_ACCESS_KEY_ID" ] || [ -z "$OFFSITE_S3_SECRET_ACCESS_KEY" ]; then
        err "OFFSITE_S3_ACCESS_KEY_ID / OFFSITE_S3_SECRET_ACCESS_KEY are not set"
        return 1
    fi

    # Refusing an awkward bucket or prefix rather than encoding it. Every key
    # this script writes is <prefix>/<dump name>, and backup.sh's naming scheme
    # is entirely within the RFC 3986 unreserved set, so with the prefix held
    # to the same set the canonical URI needs no percent-encoding at all. That
    # removes the single most common way to get a SigV4 canonical request
    # wrong, at the cost of telling an operator with a space in their prefix
    # to pick a different prefix.
    case "$s3_bucket$s3_prefix" in
        *[!A-Za-z0-9._/-]*)
            err "bucket/prefix may only contain A-Z a-z 0-9 . _ - /: $1"
            return 1 ;;
    esac

    # 5 GiB is the largest single PUT S3 accepts. Above it the API wants a
    # multipart upload, whose x-amz-checksum-sha256 is a checksum OF THE PART
    # CHECKSUMS and not the SHA-256 of the object -- so the comparison this
    # whole script exists to make would silently start comparing something
    # else. Refusing is the honest answer; the ssh transport has no such limit.
    if [ "$bytes" -gt 5368709120 ]; then
        err "$name is ${bytes} bytes, over the 5 GiB single-PUT limit"
        err "       A multipart upload's checksum is a checksum of part"
        err "       checksums, not of the object, so it could not be compared"
        err "       against the .sha256 sidecar. Use an ssh:// target."
        return 1
    fi

    if [ -n "$OFFSITE_S3_ENDPOINT" ]; then
        s3_scheme=${OFFSITE_S3_ENDPOINT%%://*}
        s3_endpoint_host=${OFFSITE_S3_ENDPOINT#*://}
        s3_endpoint_host=${s3_endpoint_host%/}
    else
        s3_scheme=https
        s3_endpoint_host="s3.$OFFSITE_S3_REGION.amazonaws.com"
    fi

    # Path-style whenever an endpoint is configured, because that is what every
    # self-hosted implementation serves out of the box: MinIO and Garage need a
    # wildcard DNS entry and a matching certificate before they will answer to
    # bucket.host at all. AWS is addressed virtual-hosted, which is what it
    # prefers and what its deprecation notice pushed towards.
    case "$OFFSITE_S3_ADDRESSING" in
        auto) if [ -n "$OFFSITE_S3_ENDPOINT" ]; then s3_style=path; else s3_style=virtual; fi ;;
        path|virtual) s3_style=$OFFSITE_S3_ADDRESSING ;;
        *) err "OFFSITE_S3_ADDRESSING must be auto, path, or virtual"; return 1 ;;
    esac

    if [ "$s3_style" = path ]; then
        s3_host=$s3_endpoint_host
        s3_base="$s3_scheme://$s3_endpoint_host"
        s3_uri_prefix="/$s3_bucket"
    else
        s3_host="$s3_bucket.$s3_endpoint_host"
        s3_base="$s3_scheme://$s3_host"
        s3_uri_prefix=""
    fi

    if [ -n "$s3_prefix" ]; then
        s3_key_dump="$s3_prefix/$name"
    else
        s3_key_dump="$name"
    fi
    s3_key_sidecar="$s3_key_dump.sha256"

    log "  s3://$s3_bucket/${s3_prefix:+$s3_prefix/} ($s3_style-style, $s3_base)"

    s3_put "$s3_key_sidecar" "$sidecar" || return 1
    s3_put "$s3_key_dump" "$dump" || return 1

    # The assertion, and it is deliberately a read rather than a memory of the
    # write. HEAD with x-amz-checksum-mode: ENABLED asks the store for the
    # SHA-256 it has recorded against the stored object, which is a different
    # question from "did the PUT return 200".
    #
    # Not the ETag. For a single-part PUT the ETag happens to be the MD5 of
    # the body, but for a multipart upload it is an MD5 of concatenated part
    # MD5s with a part count appended -- so an ETag comparison is a comparison
    # that works until the dump gets big and then quietly stops meaning
    # anything. x-amz-checksum-sha256 with ChecksumType FULL_OBJECT is the
    # SHA-256 of the object's bytes, which is the same number the sidecar
    # holds and the same number restore.sh will check.
    s3_verify "$s3_key_dump" || return 1

    if [ "$OFFSITE_S3_PRUNE" = true ]; then
        s3_prune
    else
        log "    retention: left to the bucket (OFFSITE_S3_PRUNE=false)"
    fi
}

s3_put() {
    # $1 object key, $2 local file.
    s3_put_key=$1
    s3_put_file=$2
    s3_put_hex=$(sha256sum "$s3_put_file" | cut -d ' ' -f 1)
    s3_put_b64=$(openssl dgst -sha256 -binary "$s3_put_file" | openssl base64 -A)

    s3_head "$s3_put_key" && s3_put_present=$? || s3_put_present=$?
    case "$s3_put_present" in
        0)
            s3_put_stored=$(s3_header_value x-amz-checksum-sha256)
            if [ "$s3_put_stored" = "$s3_put_b64" ]; then
                # Idempotent re-run: the object is already there and the store
                # agrees about its bytes, so there is nothing to send. Not the
                # same as trusting it -- the dump is verified again below.
                log "    $s3_put_key already stored and matching"
                return 0
            fi
            # Present and different. Not overwritten: with versioning and
            # Object Lock on, as deploy/README.md recommends, the store would
            # refuse anyway, and without them an overwrite is how a good copy
            # is lost.
            err "  $s3_put_key exists at the far end with a different checksum"
            err "         stored $s3_put_stored"
            err "         local  $s3_put_b64"
            return 1 ;;
        1) : ;;                 # genuinely absent, carry on and send it
        *) return 1 ;;          # the store could not be asked; s3_head said why
    esac

    # x-amz-checksum-sha256 on the way in is not decoration: the store
    # recomputes it over the bytes it received and rejects the PUT with
    # BadDigest if they disagree, so a truncated or flipped body never becomes
    # an object. That is the first of the two independent checks; the HEAD
    # afterwards is the second, and it is the one that survives bit-rot in the
    # store after the write.
    s3_sign PUT "$s3_uri_prefix/$s3_put_key" "" "$s3_put_hex" \
        "x-amz-checksum-sha256:$s3_put_b64"
    s3_put_status=$(curl --config "$s3_config" \
        --request PUT --upload-file "$s3_put_file" \
        --output "$work/body" --write-out '%{http_code}') || s3_put_status=000
    if [ "$s3_put_status" != 200 ]; then
        err "  PUT $s3_put_key returned HTTP $s3_put_status"
        s3_fail_body
        return 1
    fi
    log "    sent $s3_put_key"
}

s3_head() {
    # $1 object key. 0 when the object exists, 1 when the store says 404, and
    # 2 when the store could not be asked at all. Three answers rather than
    # two because "absent" and "I could not tell" must not lead to the same
    # decision: the first means send it, the second means stop.
    s3_sign HEAD "$s3_uri_prefix/$1" "" "$S3_EMPTY_HASH" \
        "x-amz-checksum-mode:ENABLED"
    s3_head_status=$(curl --config "$s3_config" --head \
        --output "$work/head" --write-out '%{http_code}') || s3_head_status=000
    case "$s3_head_status" in
        200) return 0 ;;
        404) return 1 ;;
        *)
            err "  HEAD $1 returned HTTP $s3_head_status"
            return 2 ;;
    esac
}

s3_verify() {
    if ! s3_head "$1"; then
        err "  $1 is not readable back after a successful PUT"
        return 1
    fi
    s3_v_stored=$(s3_header_value x-amz-checksum-sha256)
    s3_v_length=$(s3_header_value content-length)
    s3_v_type=$(s3_header_value x-amz-checksum-type)
    s3_v_expect=$(openssl dgst -sha256 -binary "$dump" | openssl base64 -A)

    if [ -z "$s3_v_stored" ]; then
        # Some S3-compatible stores do not implement checksum retrieval. A
        # missing answer is not a passing one: this script would otherwise be
        # asserting on the absence of a header.
        err "  the store returned no x-amz-checksum-sha256 for $1"
        err "         Without it there is nothing to compare, and an ETag is"
        err "         not a SHA-256. Use an ssh:// target for this store."
        return 1
    fi
    if [ -n "$s3_v_type" ] && [ "$s3_v_type" != FULL_OBJECT ]; then
        err "  $1 has ChecksumType $s3_v_type, not FULL_OBJECT"
        err "         A composite checksum is a checksum of part checksums."
        return 1
    fi
    if [ "$s3_v_stored" != "$s3_v_expect" ]; then
        err "  far-end checksum does not match for $1"
        err "         stored $s3_v_stored"
        err "         local  $s3_v_expect"
        return 1
    fi
    if [ "$s3_v_length" != "$bytes" ]; then
        err "  far-end length does not match for $1: $s3_v_length != $bytes"
        return 1
    fi
    log "    VERIFIED by the object store: $s3_v_stored ($s3_v_length bytes)"
}

s3_prune() {
    # Only ever objects under this prefix whose names match backup.sh's
    # published scheme, so nothing else in the bucket is at risk -- the same
    # rule backup.sh applies locally, and the reason its retention pass can be
    # pointed at a directory an operator also uses.
    s3_prune_query="list-type=2&max-keys=1000"
    if [ -n "$s3_prefix" ]; then
        s3_prune_query="$s3_prune_query&prefix=$(printf '%s' "$s3_prefix/" | sed 's|/|%2F|g')"
    fi
    s3_sign GET "$s3_uri_prefix/" "$s3_prune_query" "$S3_EMPTY_HASH" ""
    s3_prune_status=$(curl --config "$s3_config" \
        --output "$work/list" --write-out '%{http_code}') || s3_prune_status=000
    if [ "$s3_prune_status" != 200 ]; then
        err "  listing $s3_bucket returned HTTP $s3_prune_status"
        return 1
    fi

    # One page. A 14-day window over nightly dumps is 28 objects, so 1000 keys
    # is three decades of headroom -- but a shared prefix could still truncate
    # the listing, and a retention pass that quietly considered only part of
    # the bucket is the kind of half-working gate this project keeps finding.
    # So it says so rather than implementing continuation for a case that
    # should not arise: the warning is the signal that the prefix is not what
    # this was designed for.
    if grep -q '<IsTruncated>true</IsTruncated>' "$work/list" 2>/dev/null; then
        log "    warning: the listing was truncated at 1000 keys; retention"
        log "             considered only the first page of this prefix"
    fi

    # Collected to a file and then read back, rather than piped: a `while` on
    # the right of a pipe runs in a subshell, and the count below would come
    # back zero however many objects it had removed.
    sed 's|<Contents>|\n<Contents>|g' "$work/list" \
        | sed -n 's|.*<Key>\([^<]*\)</Key>.*<LastModified>\([^<]*\)</LastModified>.*|\1 \2|p' \
        > "$work/objects"

    s3_prune_cutoff=$(( $(date +%s) - BACKUP_KEEP_DAYS * 86400 ))
    s3_pruned=0
    s3_kept=0
    # `|| [ -n "$s3_old_key" ]` is not defensive noise, it is a bug this had.
    # S3 returns the listing as one line with no trailing newline, and GNU sed
    # preserves that, so the last record leaves `read` returning non-zero with
    # the variables perfectly well set -- and the loop body never runs for it.
    # The effect was that the newest object in the listing was silently
    # exempt from retention, on every run, which nothing but a count would
    # have shown: the pass reported success having considered n-1 objects.
    while read -r s3_old_key s3_old_when || [ -n "$s3_old_key" ]; do
        case "${s3_old_key##*/}" in
            "$BACKUP_DATABASE"-*.dump|"$BACKUP_DATABASE"-*.dump.sha256) : ;;
            *) continue ;;
        esac
        s3_kept=$((s3_kept + 1))
        s3_old_epoch=$(date -u -d "$s3_old_when" +%s 2>/dev/null || echo 0)
        [ "$s3_old_epoch" -gt 0 ] || continue
        [ "$s3_old_epoch" -lt "$s3_prune_cutoff" ] || continue
        s3_sign DELETE "$s3_uri_prefix/$s3_old_key" "" "$S3_EMPTY_HASH" ""
        s3_del_status=$(curl --config "$s3_config" --request DELETE \
            --output "$work/body" --write-out '%{http_code}') || s3_del_status=000
        case "$s3_del_status" in
            204|200)
                log "    pruned $s3_old_key"
                s3_pruned=$((s3_pruned + 1))
                s3_kept=$((s3_kept - 1)) ;;
            *)
                # Not fatal, and deliberately so: with Object Lock on, a
                # refused delete is the configuration working. The dump has
                # already been verified by this point, which is the part that
                # must not fail quietly.
                log "    could not prune $s3_old_key (HTTP $s3_del_status)" ;;
        esac
    done < "$work/objects"

    # Said out loud even when it is zero. A retention pass that prints nothing
    # looks exactly like one that did not run.
    log "    retention: $s3_pruned removed, $s3_kept kept (window ${BACKUP_KEEP_DAYS}d)"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
#
# One space-separated list of URLs, and the scheme picks the transport. A
# separate mode flag beside a separate address would let an operator configure
# an ssh mode with an s3 address; this way that configuration cannot be
# written down. Two targets of different schemes is the belt-and-braces case
# and needs no extra syntax.
#
# Every target is attempted even after one fails, and the exit status is the
# summary. Stopping at the first failure would mean an unreachable ssh host
# silently cancelling tonight's S3 copy, which is the opposite of what a
# second target is for.

failures=0
attempted=0

for target in $BACKUP_OFFSITE_TARGETS; do
    attempted=$((attempted + 1))
    case "$target" in
        ssh://*) send_ssh "$target" || failures=$((failures + 1)) ;;
        s3://*) send_s3 "$target" || failures=$((failures + 1)) ;;
        *)
            err "unknown transport in target: $target"
            err "       Expected ssh://user@host/path or s3://bucket/prefix"
            failures=$((failures + 1))
            ;;
    esac
done

if [ "$failures" -ne 0 ]; then
    err "$failures of $attempted off-host targets failed for $name"
    exit 1
fi

log "off-host copy verified at $attempted target(s): $name"
