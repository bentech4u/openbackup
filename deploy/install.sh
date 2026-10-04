#!/bin/bash
# Install or upgrade OpenBackup on RHEL / Rocky / Alma 9 from this checkout.
# Re-running is safe: existing configuration, keys and data are kept.
set -euo pipefail

APP=/opt/openbackup
ETC=/etc/openbackup
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
[ "$SRC" = "$APP" ] || { echo "expected the checkout at $APP (found $SRC)" >&2; exit 1; }

echo "==> System packages"
dnf -y install python3.12 nbdkit nbdkit-vddk-plugin nfs-utils qemu-img openssl \
    libguestfs libguestfs-xfs libguestfs-winsupport python3-libguestfs xfsprogs
if ! command -v node >/dev/null; then
    dnf -y module install nodejs:22/common
fi

echo "==> Python environment"
[ -x "$APP/.venv/bin/python" ] || python3.12 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -e "$APP"

echo "==> Web interface"
(cd "$APP/frontend" && npm ci --no-audit --no-fund && npm run build)

echo "==> Configuration"
install -d -m 0700 "$ETC" /var/lib/openbackup
install -d -m 0755 /mnt/openbackup
[ -f "$ETC/openbackup.env" ] || install -m 0600 "$APP/deploy/openbackup.env.example" "$ETC/openbackup.env"
if [ ! -f "$ETC/tls.crt" ]; then
    openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
        -keyout "$ETC/tls.key" -out "$ETC/tls.crt" \
        -subj "/CN=$(hostname -f)" -addext "subjectAltName=DNS:$(hostname -f),DNS:$(hostname -s)"
    chmod 0600 "$ETC/tls.key"
    echo "    Generated a self-signed certificate; replace $ETC/tls.crt/.key with your own."
fi
"$APP/.venv/bin/openbackup" init

echo "==> Services"
install -m 0644 "$APP"/deploy/systemd/openbackup-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable openbackup-api openbackup-worker
systemctl restart openbackup-api openbackup-worker
if systemctl is-active -q firewalld; then
    firewall-cmd -q --permanent --add-port=8443/tcp && firewall-cmd -q --reload
fi

if [ ! -e /opt/openvddk/lib64/libvixDiskLib.so.8 ]; then
    echo
    echo "NOTE: no VDDK library in /opt/openvddk. Backups need it; build it with:"
    echo "      $APP/deploy/build-openvddk.sh"
fi
if ! "$APP/.venv/bin/openbackup" user list | grep -q ' admin '; then
    echo
    echo "Create the first administrator:"
    echo "      $APP/.venv/bin/openbackup user create --admin <username>"
fi
echo
echo "OpenBackup is running at https://$(hostname -f):8443/"
