#!/bin/sh
# Decode cloud qcow2 to RAW, then let native vmctl create its own qcow2 format.
set -eu
PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin
export PATH
umask 077
root=/home/vmm-ci
source=$root/ubuntu-cloud.qcow2
target=$root/base/build.qcow2
[ -f "$source" ] || { echo 'Missing verified cloud image' >&2; exit 1; }
[ ! -e "$target" ] || { echo 'Existing build disk preserved' >&2; exit 1; }
stage=$(mktemp -d "$root/base/convert.XXXXXXXXXX")
cleanup() {
    rm -f "$stage/normalized.raw" "$stage/expanded.qcow2"
    rmdir "$stage"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
# RAW has no compression, refcounts or format extensions for vmctl to interpret.
qemu-img convert -f qcow2 -O raw "$source" "$stage/normalized.raw"
qemu-img compare -f qcow2 -F raw "$source" "$stage/normalized.raw"
vmctl create -i "raw:$stage/normalized.raw" -s 30G "$stage/expanded.qcow2"
qemu-img check -f qcow2 "$stage/expanded.qcow2"
qemu-img compare -f raw -F qcow2 "$stage/normalized.raw" "$stage/expanded.qcow2"
[ ! -e "$target" ] || { echo 'Target appeared during conversion; preserved' >&2; exit 1; }
mv "$stage/expanded.qcow2" "$target"
