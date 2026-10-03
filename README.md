# OpenBackup

Web-based, image-level backup and restore for VMware vSphere, storing backups
on NFS. You run it entirely from the browser, with authenticated users and
roles.

- **Backups.** Snapshot-based, with Changed Block Tracking incrementals.
  Application-consistent quiescing goes through VMware Tools.
- **Disk access.** Disks are read through nbdkit's `vddk` plugin, backed by
  [OpenVDDK](https://github.com/jimmyma-zhanwei/openvddk), so you don't need
  Broadcom's VDDK. VMware's own VDDK is a drop-in alternative.
- **Repositories.** Backups go to NFS or local repositories. Data is
  deduplicated, compressed, and optionally AES-256-GCM encrypted.
- **Restores.** You can restore as a new VM, over the original VM, or export
  disks as VMDK, QCOW2 or raw images.
- **Scheduling and maintenance.** Jobs run on cron schedules, with retention
  by count and/or age. Verification and garbage collection run from the UI.
- **Web interface.** Live task progress and logs, local users with
  admin / operator / viewer roles, and an audit log.

## Architecture

```
Browser ──HTTPS──> openbackup-api      FastAPI; serves the React UI and /api
                         │
                    SQLite (WAL)        /var/lib/openbackup — config, users, tasks
                         │
                   openbackup-worker    job queue + scheduler
                     ├─ pyvmomi         snapshots, CBT, VM config
                     ├─ nbdkit vddk     per-disk NBD over a private unix socket
                     └─ repository      NFS mount (hard) under /mnt/openbackup
```

The API and the worker share only the database. Tasks against the same
repository run one at a time. Tasks against different repositories run in
parallel, up to `OPENBACKUP_WORKER_CONCURRENCY`.

## Install (RHEL / Rocky / Alma 9)

```bash
git clone <this repo> /opt/openbackup
/opt/openbackup/deploy/install.sh
/opt/openbackup/deploy/build-openvddk.sh
/opt/openbackup/.venv/bin/openbackup user create --admin admin
```

Open `https://<server>:8443/` and sign in. The first admin must choose a new
password at first sign-in.

`install.sh` does the following:

- installs the dependencies (python3.12, nbdkit + vddk plugin, nfs-utils,
  qemu-img, Node.js for the UI build);
- creates `/etc/openbackup/` with a secret key and a self-signed TLS
  certificate (replace `tls.crt` / `tls.key` with your own);
- enables `openbackup-api` and `openbackup-worker`;
- opens port 8443 in firewalld.

Configuration lives in `/etc/openbackup/openbackup.env`. See
[deploy/openbackup.env.example](deploy/openbackup.env.example).

### vCenter account

Use a dedicated account. It needs privileges to:

- browse inventory;
- create and remove snapshots;
- enable CBT (Virtual machine → Change configuration → Toggle disk change
  tracking);
- read disks: Virtual machine → Provisioning → Allow disk access / Allow
  read-only disk access.

Restores additionally need these on the target folder, resource pool,
datastore and network:

- create VMs;
- add disks;
- power on/off.

When you add a vCenter, OpenBackup shows the certificate fingerprint for you
to confirm, and then pins it.

### NFS repositories

Give the export `no_root_squash` (or an equivalent) for this server.
OpenBackup mounts it with `nfsvers=4.2,hard` and refuses soft mounts, because
a soft mount turns a timeout into silently missing data.

A repository is self-contained. Detaching one leaves the data in place, and
you can import it again on a rebuilt server. An encrypted repository also
needs its passphrase.

## How backups work

1. Clean up any OpenBackup snapshot left behind by a crashed run, and enable
   CBT if it is off.
2. Take a snapshot (quiesced if the job says so) and capture the VM
   configuration.
3. For each disk, ask CBT what changed since the previous point. If there is
   no usable change ID, the disk was resized, CBT rejects the query, or a
   periodic active full is due, read every allocated block instead. A
   needless full read costs time; a wrong incremental costs data.
4. Read only those 1 MiB blocks over NBD. Store each block once (SHA-256, or
   HMAC-SHA-256 when encrypted), zstd-compressed, in ~128 MB pack files.
5. Write the block map and manifest last, so a point exists completely or not
   at all. Then remove the snapshot, always.

Every restore point is a complete block map, so it never depends on a
chain. Deleting any point never breaks another one. Retention deletes points,
and mark-and-sweep GC reclaims unreferenced data. The local chunk index can
be rebuilt from pack-file trailers.

## Security

- **Passwords and sign-in.**
  - Passwords are hashed with argon2id, minimum length 12.
  - An account locks after 5 failed sign-ins.
  - Each source address is also throttled.
  - Unknown users and wrong passwords produce identical responses.
- **Sessions.**
  - Sessions are server-side, in `HttpOnly; Secure; SameSite=Strict` cookies.
  - They expire after 8 h idle or 7 days absolute.
  - A CSRF token is required on every mutating request.
  - Changing or resetting a password signs out the account's other sessions.
- **Roles.** The API enforces them on every endpoint; hiding things in the UI
  is only a convenience.
  - **viewer:** reads everything.
  - **operator:** also runs and stops jobs, verifies, restores as a new VM or
    exports.
  - **admin:** also configures the system, manages users, restores over a
    production VM and deletes backups.
- **Stored secrets.**
  - vCenter passwords and repository passphrases are Fernet-encrypted under
    `/etc/openbackup/secret.key`.
  - The vCenter password reaches nbdkit through an inherited pipe, never on a
    command line.
- **Exports** are written only inside the repository, never to a path chosen
  by the caller.
- **Audit log.** Every sign-in, failed sign-in and change is logged with the
  user and source address.
- **Response headers.** Strict CSP, `X-Frame-Options: DENY` and HSTS.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                 # needs nbdkit; NFS tests also need root + an export
cd frontend && npm install && npm run dev   # proxies /api to https://127.0.0.1:8443
```

The tests exercise the full path without a vCenter. A fake vSphere
(`tests/fake_vsphere.py`) serves file-backed disks through a real nbdkit, with
simulated snapshots and CBT. Backups and restores are checked byte for byte.
Set `OPENBACKUP_TEST_NFS=server:/export` to run the NFS tests against a
specific export.

## Status and limitations

- **Untested against a real vCenter.** The vSphere and OpenVDDK path hasn't
  run against a real vCenter yet. Everything below the `VSphereSource`
  interface is tested.
- **No file-level restore yet.** You can't browse files inside a restore
  point.
- **No migrations.** The database schema is created directly; there's no
  migration tooling yet.
- **Single server.** There are no backup proxies, and no HotAdd or SAN
  transport configuration.
