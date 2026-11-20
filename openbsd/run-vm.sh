#!/bin/sh
# Manual registration by SSH, or private cloud-config on stdin in --auto mode.
set -eu
PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin
export PATH
umask 077

case "${1:-}" in
    github|forgejo) pool=$1 ;;
    *) echo 'Usage: run-vm.sh github|forgejo' >&2; exit 2 ;;
esac
automatic=false
if [ "$#" -eq 3 ] && [ "$2" = --auto ] && [ "$pool" = github ]; then
    session=$3
    case "$session" in *[!a-f0-9]*) echo 'Invalid session' >&2; exit 2 ;; esac
    [ "${#session}" -eq 32 ] || exit 2
    automatic=true
elif [ "$#" -ne 1 ]; then
    echo 'Usage: run-vm.sh github|forgejo [--auto SESSION]' >&2; exit 2
fi
[ "$(id -u)" -eq 0 ] || { echo 'Run as root on OpenBSD' >&2; exit 1; }
[ "$(uname -s)" = OpenBSD ] || { echo 'OpenBSD required' >&2; exit 1; }
root=/home/vmm-ci
[ ! -e "$root/build.lock" ] || { echo 'Image build in progress; finish sealing first' >&2; exit 1; }
base=$root/base/$pool.qcow2
disk=$root/runtime/job.qcow2
config=$root/job.vm.conf
lock=$root/runtime/lock
seed_source=$root/job-seed
seed=$lock/seed
[ -f "$base" ] || { echo "Missing base: $base" >&2; exit 1; }
[ -f "$config" ] || { echo "Missing runtime configuration: $config" >&2; exit 1; }
[ -f "$seed_source/user-data" ] && [ -f "$seed_source/network-config" ] || {
    echo 'Missing per-job cloud-init seed; run cloud-build provisioning first' >&2; exit 1;
}
[ ! -e "$disk" ] || { echo 'Existing job disk; recover manually first' >&2; exit 1; }
if "$automatic"; then
    [ -f "$lock/session" ] && [ "$(cat "$lock/session")" = "$session" ] || {
        echo 'Controller does not own this capacity lock' >&2; exit 1;
    }
    printf '%s\n' "$$" > "$lock/manager_pid"
else
    mkdir "$lock" || { echo 'Another session or stale lock exists' >&2; exit 1; }
fi

running() {
    if vmctl status -r > "$lock/status" 2> "$lock/status.err"; then
        :
    else
        status_rc=$?
        # OpenBSD returns 1 for an empty VM list. Accept only its exact header,
        # with no stderr; connection failures and malformed replies stay errors.
        [ "$status_rc" -eq 1 ] && [ ! -s "$lock/status.err" ] || return 2
        awk 'NR == 1 && NF == 9 && $1 == "ID" && $2 == "PID" &&
            $3 == "VCPUS" && $4 == "MAXMEM" && $5 == "CURMEM" &&
            $6 == "TTY" && $7 == "OWNER" && $8 == "STATE" && $9 == "NAME" {
            valid=1 } END { exit !(NR == 1 && valid) }' "$lock/status" || return 2
    fi
    awk '$NF == "ci-runner" { found=1 } END { exit !found }' "$lock/status"
}
cleanup() {
    trap - EXIT HUP INT TERM
    if running; then
        [ -f "$lock/launching" ] || {
            echo 'Running VM was not launched by this session; preserving it' >&2; exit 1;
        }
        vmctl stop -fw ci-runner || {
            echo 'STOP FAILED: retaining disk and lock for recovery' >&2
            exit 1
        }
    else
        rc=$?
        if [ "$rc" -ne 1 ]; then
            echo 'Cannot confirm VM state; retaining disk and lock' >&2
            exit 1
        fi
    fi
    # Drop stopped runtime definitions before deleting a referenced disk.
    vmctl reload || { echo 'Reload failed; retaining disk and lock' >&2; exit 1; }
    rm -f "$disk" "$root/runtime/job-seed.iso" "$lock/status" "$lock/status.err"
    rm -f "$seed/user-data" "$seed/network-config" "$seed/meta-data"
    if [ -d "$seed" ]; then rmdir "$seed"; fi
    rm -f "$lock/session" "$lock/manager_pid" "$lock/launching"
    rmdir "$lock"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
if running; then
    echo 'ci-runner is already running; refusing to replace it' >&2
    # Do not stop a VM owned by a previous/manual session.
    trap - EXIT HUP INT TERM
    rm -f "$lock/status" "$lock/status.err"
    if ! "$automatic"; then rmdir "$lock"; fi
    exit 1
else
    rc=$?
    [ "$rc" -eq 1 ] || exit 1
fi
mkdir "$seed"
cp "$seed_source/network-config" "$seed/network-config"
if "$automatic"; then
    cat > "$seed/user-data"
else
    cp "$seed_source/user-data" "$seed/user-data"
fi
vmctl create -b "$base" "$disk"
printf 'instance-id: ci-job-%s-%s\nlocal-hostname: ci-job\n' "$(date +%s)" "$$" > "$seed/meta-data"
rm -f "$root/runtime/job-seed.iso"
xorriso -as mkisofs -o "$root/runtime/job-seed.iso" -V cidata -J -r "$seed"
# The persistent host config contains no CI disk paths.
# Reload removes prior stopped dynamic definitions before loading this job.
vmctl reload
vmd -n -f "$config"
vmctl load "$config"
touch "$lock/launching"
vmctl start ci-runner
if "$automatic"; then
    echo 'Automatic JIT VM started; this session expires after 60 minutes.'
else
    echo 'VM ready. In another terminal: doas vmctl console ci-runner'
    echo 'Register ONE ephemeral runner; this session expires after 60 minutes.'
fi
deadline=$(( $(date +%s) + 3600 ))
while running; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo 'Host deadline reached; destroying this test environment' >&2
        exit 124
    fi
    sleep 10
done
# cleanup independently verifies state and will retain the disk on query failure.
