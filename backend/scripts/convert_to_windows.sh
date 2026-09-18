#!/bin/bash
#
# Ubuntu -> Windows conversion for DigitalOcean droplets (cloud-init user-data).
#
# Two methods, chosen per OS via the API (install_method):
#   qemu  - unattended install from a Windows ISO under QEMU. Requires KVM
#           acceleration (/dev/kvm). Without it a full install takes many hours
#           and is not viable, so this script aborts loudly instead of faking it.
#   image - stream a prebuilt gzipped raw Windows disk image straight onto the
#           droplet's disk with dd, then reboot. Works WITHOUT KVM. This is the
#           reliable path on DigitalOcean. Set image_url to your hosted image.
#
# Progress is reported to the backend via HTTP callbacks. The backend confirms
# "active" independently by polling RDP (3389), so this script never fakes success.
set -uo pipefail

CALLBACK_URL="{{CALLBACK_URL}}"
CALLBACK_TOKEN="{{CALLBACK_TOKEN}}"
SERVER_ID="{{SERVER_ID}}"
ISO_URL="{{ISO_URL}}"
VIRTIO_URL="{{VIRTIO_URL}}"
ADMIN_PASSWORD="{{ADMIN_PASSWORD}}"
AUTOUNATTEND_B64="{{AUTOUNATTEND_B64}}"
INSTALL_METHOD="{{INSTALL_METHOD}}"
IMAGE_URL="{{IMAGE_URL}}"

WORK=/root/win-convert
mkdir -p "$WORK"
exec > >(tee -a "$WORK/convert.log") 2>&1

report() {
  local stage="$1"; local progress="$2"; shift 2; local msg="$*"
  msg="${msg//\"/}"
  curl -s -m 20 -X POST "$CALLBACK_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"server_id\":\"$SERVER_ID\",\"token\":\"$CALLBACK_TOKEN\",\"stage\":\"$stage\",\"progress\":$progress,\"message\":\"$msg\"}" >/dev/null || true
}
fail() { report failed "${2:-0}" "$1"; exit 0; }

report boot 15 "Host booted. Installing tools."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y wget curl pv gzip file >/dev/null 2>&1 || true

DISK_NAME=$(lsblk -ndo NAME,TYPE | awk '$2=="disk"{print $1; exit}')
DISK="/dev/$DISK_NAME"

# ---------------- image method (no KVM required) ----------------
if [ "$INSTALL_METHOD" = "image" ]; then
  [ -n "$IMAGE_URL" ] || fail "install_method=image but no image_url configured." 20
  report download_image 30 "Streaming prebuilt Windows image to disk (this can take a while)."
  # dd raw sectors to the boot disk, then hard reboot into Windows.
  if wget -q -O- "$IMAGE_URL" | gunzip | dd of="$DISK" bs=64M conv=fsync 2>>"$WORK/convert.log"; then
    sync
    report finalizing 90 "Image written to disk. Rebooting into Windows."
    sleep 5
    echo 1 > /proc/sys/kernel/sysrq 2>/dev/null || true
    reboot -f
    exit 0
  else
    fail "Failed to write Windows image (check image_url is a gzipped raw disk image)." 30
  fi
fi

# ---------------- qemu method (needs KVM) ----------------
apt-get install -y qemu-utils qemu-system-x86 genisoimage ovmf dosfstools >/dev/null 2>&1

if [ ! -e /dev/kvm ]; then
  fail "No /dev/kvm on this DigitalOcean droplet - hardware virtualization is not available, so a QEMU Windows install is not viable. Use install_method=image with a prebuilt disk image instead." 20
fi

report download_iso 25 "Downloading Windows ISO (~5 GB)."
wget -q -O "$WORK/windows.iso" "$ISO_URL" || fail "Windows ISO download failed (check iso_url)." 25
ISO_SIZE=$(stat -c%s "$WORK/windows.iso" 2>/dev/null || echo 0)
if [ "$ISO_SIZE" -lt 2000000000 ]; then
  fail "Windows ISO looks invalid (only $ISO_SIZE bytes - the URL likely returned an error page)." 25
fi

report download_virtio 40 "Downloading VirtIO driver ISO."
wget -q -O "$WORK/virtio.iso" "$VIRTIO_URL" || fail "VirtIO ISO download failed (check virtio_url)." 40
VIRTIO_SIZE=$(stat -c%s "$WORK/virtio.iso" 2>/dev/null || echo 0)
[ "$VIRTIO_SIZE" -gt 50000000 ] || fail "VirtIO ISO looks invalid ($VIRTIO_SIZE bytes)." 40

report prepare 45 "Building unattended answer + driver disk."
mkdir -p "$WORK/answer"
echo "$AUTOUNATTEND_B64" | base64 -d > "$WORK/answer/autounattend.xml"
genisoimage -quiet -J -r -o "$WORK/answer.iso" "$WORK/answer"

# Build a minimal FAT floppy containing ONLY this OS's virtio drivers, so Setup
# injects valid boot-critical drivers (pointing at the whole virtio ISO makes
# Setup try wrong-OS drivers and fail with "boot-critical drivers" error).
DRV_VER="{{VIRTIO_DIR}}"
mkdir -p /mnt/virtio "$WORK/drv"
mount -o loop,ro "$WORK/virtio.iso" /mnt/virtio
if [ ! -d "/mnt/virtio/viostor/$DRV_VER" ]; then
  for alt in 2k25 2k22 2k19 2k16; do
    [ -d "/mnt/virtio/viostor/$alt" ] && DRV_VER="$alt" && break
  done
fi
for comp in viostor vioscsi NetKVM; do
  if [ -d "/mnt/virtio/$comp/$DRV_VER/amd64" ]; then
    mkdir -p "$WORK/drv/$comp"
    cp "/mnt/virtio/$comp/$DRV_VER/amd64/"* "$WORK/drv/$comp/" 2>/dev/null || true
  fi
done
umount /mnt/virtio
dd if=/dev/zero of="$WORK/drv.img" bs=1024 count=2880 status=none
mkfs.vfat "$WORK/drv.img" >/dev/null 2>&1
mkdir -p /mnt/drv
mount -o loop "$WORK/drv.img" /mnt/drv
cp -r "$WORK/drv/." /mnt/drv/ 2>/dev/null || true
sync
umount /mnt/drv

report install_windows 55 "Running unattended Windows installation (20-40 min)."
# Stop services that write to the boot disk before handing it to QEMU.
systemctl stop rsyslog cron unattended-upgrades 2>/dev/null || true
sync
# Size the guest to fit the host: ~65% of RAM (min 2048 MB) and all vCPUs.
MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024*0.65}' /proc/meminfo)
[ "$MEM_MB" -lt 2048 ] && MEM_MB=2048
CPUS=$(nproc)
START=$(date +%s)
qemu-system-x86_64 -enable-kvm -cpu host -m "${MEM_MB}" -smp "${CPUS}" \
  -drive file="$DISK",format=raw,if=virtio,cache=none \
  -fda "$WORK/drv.img" \
  -drive file="$WORK/windows.iso",media=cdrom,index=1 \
  -drive file="$WORK/virtio.iso",media=cdrom,index=2 \
  -drive file="$WORK/answer.iso",media=cdrom,index=3 \
  -boot d -vnc 0.0.0.0:1 -monitor telnet:127.0.0.1:4444,server,nowait \
  -no-reboot 2>"$WORK/qemu.err"
RC=$?
DUR=$(( $(date +%s) - START ))
# With -no-reboot, QEMU exits when Windows Setup performs its first reboot after
# the offline apply phase - that is SUCCESS. A genuine crash exits within seconds.
# So only fail on a fast exit; otherwise boot the host into the installed Windows,
# where specialize/OOBE run on the real DO hardware and RDP comes up.
if [ "$DUR" -lt 240 ]; then
  KVMDEV=$([ -e /dev/kvm ] && echo yes || echo no)
  ERR=$(tr '\n\r\"' '   ' < "$WORK/qemu.err" | tail -c 400)
  fail "QEMU exited after ${DUR}s (rc=$RC kvm=$KVMDEV) - install did not run. err=[$ERR]" 55
fi

sync
report finalizing 90 "Offline install done in ${DUR}s. Rebooting host into Windows to finish setup."
sleep 3
reboot -f
