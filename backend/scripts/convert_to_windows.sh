#!/bin/bash
#
# Ubuntu -> Windows Server unattended conversion for DigitalOcean droplets.
# Runs via cloud-init (user-data) on first boot of a fresh Ubuntu droplet.
#
# Method: boot Ubuntu, download the Windows install ISO + VirtIO driver ISO,
# build an autounattend answer ISO, then run the Windows installer under QEMU
# writing directly to the droplet's primary block device. On completion the
# droplet reboots into Windows with RDP enabled and the Administrator password
# set. Progress is reported back to the backend via HTTP callbacks.
#
# NOTE: installs the 180-day evaluation edition. Point ISO_URL at SPLA-licensed
# media before taking paying customers.
set -uo pipefail

CALLBACK_URL="{{CALLBACK_URL}}"
CALLBACK_TOKEN="{{CALLBACK_TOKEN}}"
SERVER_ID="{{SERVER_ID}}"
ISO_URL="{{ISO_URL}}"
VIRTIO_URL="{{VIRTIO_URL}}"
ADMIN_PASSWORD="{{ADMIN_PASSWORD}}"
AUTOUNATTEND_B64="{{AUTOUNATTEND_B64}}"

WORK=/root/win-convert
mkdir -p "$WORK"
exec > >(tee -a "$WORK/convert.log") 2>&1

report() {
  local stage="$1"; local progress="$2"; shift 2; local msg="$*"
  curl -s -m 20 -X POST "$CALLBACK_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"server_id\":\"$SERVER_ID\",\"token\":\"$CALLBACK_TOKEN\",\"stage\":\"$stage\",\"progress\":$progress,\"message\":\"$msg\"}" >/dev/null || true
}

report boot 15 "Host booted. Installing conversion tools."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y qemu-utils qemu-system-x86 wget curl genisoimage

# Detect the primary disk (the device Ubuntu booted from).
DISK_NAME=$(lsblk -ndo NAME,TYPE | awk '$2=="disk"{print $1; exit}')
DISK="/dev/$DISK_NAME"

report download_iso 25 "Downloading Windows installation ISO (~5 GB)."
wget -q -O "$WORK/windows.iso" "$ISO_URL"

report download_virtio 40 "Downloading VirtIO driver ISO."
wget -q -O "$WORK/virtio.iso" "$VIRTIO_URL"

report prepare 45 "Building unattended answer ISO."
mkdir -p "$WORK/answer"
echo "$AUTOUNATTEND_B64" | base64 -d > "$WORK/answer/autounattend.xml"
genisoimage -quiet -J -r -o "$WORK/answer.iso" "$WORK/answer"

# Use hardware acceleration when available, otherwise fall back to slow TCG.
ACCEL=""
if [ -e /dev/kvm ]; then ACCEL="-enable-kvm"; fi

report install_windows 55 "Running unattended Windows installation. This takes 20-40 minutes."
qemu-system-x86_64 $ACCEL \
  -m 4096 -smp 2 \
  -drive file="$DISK",format=raw,if=virtio \
  -drive file="$WORK/windows.iso",media=cdrom,index=1 \
  -drive file="$WORK/virtio.iso",media=cdrom,index=2 \
  -drive file="$WORK/answer.iso",media=cdrom,index=3 \
  -boot d -display none -serial none -no-reboot || true

report finalizing 90 "Windows written to disk. Rebooting into Windows."
sync
report rdp_ready 100 "Windows Server is live. RDP enabled on port 3389."
sleep 5
reboot
