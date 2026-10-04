# OpenBackup

Web-based backup and restore for **VMware vSphere VMs** and **OpenShift
namespaces**, storing backups on NFS. You run it entirely from the browser,
with authenticated users and roles.

- **vSphere VMs.** Snapshot-based image backups with Changed Block Tracking
  incrementals. Disk data is read straight from the NFS datastore, so
  vCenter and ESXi only receive API calls and never carry backup data.
- **OpenShift.** Namespaces with all their objects, persistent-volume data
  (vSphere CSI volumes), OpenShift Virtualization VMs, and collection of
  etcd backups. Restore into the same cluster or another one (for example a
  DR cluster).
- **Repositories.** NFS or local. Data is deduplicated, zstd-compressed, and
  optionally AES-256-GCM encrypted.
- **Restores.**
  - Whole VM, as a new VM or over the original (quick rollback via CBT).
  - Individual files: browse a restore point, download files, or put them
    back into the running VM, overwriting or renaming.
  - Disk export as VMDK, QCOW2 or raw.
  - OpenShift namespaces, with storage class mapping and route host rewrite.
- **Scheduling and maintenance.** Cron schedules, retention by count and/or
  age, verification and garbage collection from the UI.
- **Web interface.** Live task progress and logs, local users with
  admin / operator / viewer roles, and an audit log.

## Architecture

```
Browser ──HTTPS──> openbackup-api        FastAPI; serves the React UI and /api
                         │
                    SQLite (WAL)          /var/lib/openbackup: config, users, tasks
                         │
                   openbackup-worker      job queue + scheduler
                     ├─ pyvmomi           vCenter API: snapshots, CBT, VM config
                     ├─ direct NFS        VM disks read from the datastore export
                     ├─ Kubernetes API    OpenShift objects, mover pods
                     ├─ libguestfs        file-level browse and restore
                     └─ repository        NFS mount (hard) under /mnt/openbackup
```

The API and the worker share only the database. Tasks against the same
repository run one at a time. Tasks against different repositories run in
parallel, up to `OPENBACKUP_WORKER_CONCURRENCY`.

## Install (RHEL / Rocky / Alma 9)

```bash
git clone https://github.com/bentech4u/openbackup.git /opt/openbackup
/opt/openbackup/deploy/install.sh
/opt/openbackup/.venv/bin/openbackup user create --admin admin
```

Open `https://<server>:8443/` and sign in. The first admin must choose a new
password at first sign-in.

`install.sh` does the following:

- installs the dependencies (python3.12, nfs-utils, libguestfs, qemu-img,
  nbdkit, Node.js for the UI build);
- creates `/etc/openbackup/` with a secret key and a self-signed TLS
  certificate (replace `tls.crt` / `tls.key` with your own);
- creates the database (migrations also run automatically whenever the
  services start);
- enables `openbackup-api` and `openbackup-worker`;
- opens port 8443 in firewalld.

Configuration lives in `/etc/openbackup/openbackup.env`. See
[deploy/openbackup.env.example](deploy/openbackup.env.example).

To reset a forgotten password:

```bash
/opt/openbackup/.venv/bin/openbackup user reset-password admin
```

### NFS repositories

Give the export `no_root_squash` (or an equivalent) for this server.
OpenBackup mounts it with `nfsvers=4.2,hard` and refuses soft mounts, because
a soft mount turns a timeout into silently missing data.

A repository is self-contained. Detaching one leaves the data in place, and
you can import it again on a rebuilt server. An encrypted repository also
needs its passphrase, which cannot be recovered if lost.

## VMware vSphere

### vCenter account

Use a dedicated account. It needs privileges to:

- browse inventory;
- create and remove snapshots;
- enable CBT (Virtual machine → Change configuration → Toggle disk change
  tracking).

Restores additionally need these on the target folder, resource pool,
datastore and network:

- create VMs;
- add disks;
- power on/off.

File restore into a running VM also needs guest operations (Virtual machine →
Guest operations), and a guest account entered at restore time.

When you add a vCenter, OpenBackup shows the certificate fingerprint for you
to confirm, and then pins it.

### Direct NFS datastore access

On the **vCenters** page, give each NFS datastore its export
(`server:/path`). OpenBackup then reads disks straight from the NAS:

- **Backups** mount the datastore **read-only** and open files `O_RDONLY`.
  Only the frozen base disk behind OpenBackup's snapshot is read. A VM that
  already has snapshots of its own is refused, because its base disk is stale.
- **Restores** mount it read-write on a separate mountpoint, only for the
  duration of the task. Only the disk files of the VM being restored are
  written, after checking that the VM is powered off, has no snapshots, and
  the file is one of its own base disks at the expected size. Files are never
  created, truncated or removed by OpenBackup; vCenter creates them.

A dedicated storage network for the NAS is recommended. NFS v3 is the default
for datastores (NFS v4 delegations held by this server can block ESXi from
consolidating snapshots).

### How backups work

1. Clean up any OpenBackup snapshot left behind by a crashed run, and enable
   CBT if it is off.
2. Take a snapshot (quiesced if the job says so) and capture the VM
   configuration.
3. For each disk, ask CBT what changed since the previous point. If there is
   no usable change ID, the disk was resized, CBT rejects the query, or a
   periodic active full is due, read every allocated block instead. A
   needless full read costs time; a wrong incremental costs data.
4. Read only those 1 MiB blocks from the datastore. Store each block once
   (SHA-256, or HMAC-SHA-256 when encrypted), zstd-compressed, in ~128 MB
   pack files.
5. Write the block map and manifest last, so a point exists completely or not
   at all. Then remove the snapshot, always (retrying consolidation if ESXi
   needs it).

Every restore point is a complete block map, so it never depends on a
chain. Deleting any point never breaks another one. Retention deletes points,
and mark-and-sweep GC reclaims unreferenced data. The local chunk index can
be rebuilt from pack-file trailers.

### Restoring VMs

- **As a new VM.** vCenter creates the VM from the backed-up configuration
  (firmware, controllers, disks, NICs with network mapping). OpenBackup then
  writes the disk data over direct NFS, skipping empty blocks so thin disks
  stay thin. The target can be any registered vCenter.
- **Over the original (admin only).** The VM is powered off (hard), then CBT
  names the blocks changed since the backup and only those are written back.
  When CBT cannot answer, every block is compared instead. CBT is reset
  afterwards, so the VM's next backup is a full one.
- **Files.** Browse a restore point in the UI (libguestfs, read-only, in a
  helper appliance). Download files, or upload them into the running VM
  through VMware Tools, choosing to overwrite or rename.
- **Export.** Write disks as VMDK, QCOW2 or raw images inside the repository.

## OpenShift

### Adding a cluster

On the **Clusters** page, add the API URL and confirm its CA certificate,
which is then pinned. Then either:

- **Set up with admin credentials:** kubeadmin with its password, or a
  cluster-admin token. OpenBackup uses them once to create its own namespace
  and service accounts, then works only with those accounts' tokens. The
  admin credentials are never stored, and a session token obtained from a
  password is revoked as soon as setup is done.
- **Use existing tokens:** apply the manifests yourself and paste the tokens.
  See [deploy/openshift/README.md](deploy/openshift/README.md).

The accounts:

- **openbackup-backup:** `cluster-reader`, plus read access to monitoring
  rules and KubeVirt freeze/unfreeze. Reading Secrets is a separate grant,
  given only if you allow it (**Update permissions** on the Clusters page).
- **openbackup-restore:** namespace `admin` plus creating namespaces and
  running mover pods. Only needed on clusters you restore into. Its token can
  be stored, or pasted for a single restore and not kept.

### What a namespace backup contains

- **Objects:** everything namespaced and listable, cleaned of server-assigned
  state. Objects that controllers recreate (owned Pods, ReplicaSets, Events,
  Endpoints...) are skipped.
- **Secrets:** only when the job allows it, and only into an **encrypted**
  repository. Service account tokens and other generated Secrets are never
  included.
- **Volume data:** vSphere CSI volumes (First Class Disks) are snapshotted
  through the vCenter API and read over direct NFS, like VM disks. Volumes of
  other storage drivers keep their definitions only, with a warning.
- **OpenShift Virtualization VMs:** running VMs with the guest agent are
  frozen around their volume snapshots, for consistent disks.

### Restoring a namespace

Choose the **target cluster** (the original or another one, such as DR) and
the target namespace name. Options:

- map storage classes to ones that exist on the target cluster;
- rewrite route hosts (for example `apps.prod.example.com` →
  `apps.dr.example.com`);
- keep the original UID range, so restored files still match `runAsUser`;
- restore Secrets or not (for example when the DR cluster has its own);
- restore volume data or not.

Objects are applied in dependency order, with VMs last. Volume data is filled
by a short-lived **mover pod** in the target namespace that pulls it from
OpenBackup over HTTPS with a single-use token: a tar stream for filesystem
volumes (works with any storage class, restricted SCC), a raw stream for
block volumes. The cluster's nodes must reach OpenBackup:

- `OPENBACKUP_PUBLIC_URL`: the URL mover pods fetch from; its name must
  match the TLS certificate.
- `OPENBACKUP_PUBLIC_ADDRESS`: the IP that name is pinned to inside the pod,
  when cluster DNS cannot resolve it.

### etcd

An **etcd collection** job reads the sets that OpenShift's
`cluster-backup.sh` writes (for example copied to a NAS folder), and stores
each new set with retention, encryption and dedup. It needs no access to the
cluster. Restoring etcd is deliberately manual: download the set and follow
Red Hat's `cluster-restore.sh` procedure, summarised on the restore page.

Do not back up OpenShift node VMs as vSphere images; Red Hat does not support
restoring a cluster from them.

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
    namespace, and exports.
  - **admin:** also configures the system, manages users, restores over a
    production VM and deletes backups.
- **Stored secrets.** vCenter passwords, cluster tokens and repository
  passphrases are Fernet-encrypted under `/etc/openbackup/secret.key`, and
  never returned by the API.
- **Certificates.** vCenter, ESXi and cluster certificates are pinned after
  you confirm them.
- **Exports** are written only inside the repository, never to a path chosen
  by the caller.
- **Audit log.** Every sign-in, failed sign-in and change is logged with the
  user and source address.
- **Response headers.** Strict CSP, `X-Frame-Options: DENY` and HSTS.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                 # libguestfs tests skip without libguestfs
cd frontend && npm install && npm run dev   # proxies /api to https://127.0.0.1:8443
```

The tests exercise the full path without a vCenter or cluster. A fake vSphere
(`tests/fake_vsphere.py`) serves file-backed disks with simulated snapshots
and CBT, and a fake Kubernetes API covers namespace capture and restore.
Backups and restores are checked byte for byte. Set
`OPENBACKUP_TEST_NFS=server:/export` to run the NFS tests against a specific
export.

Schema changes go through Alembic migrations in `openbackup/db/migrations/`
(`openbackup db upgrade`).

## Status and limitations

- **Early software.** It is developed and used in a homelab (vSphere 8,
  OpenShift 4 on vSphere, Synology NFS). Test restores before relying on it.
- **Direct NFS only for data.** VM disks are read and restored through NFS
  datastores. VMFS/vSAN datastores and VDDK-style transports (NBD, HotAdd,
  SAN) are not usable for now; the bundled OpenVDDK path does not work
  against vCenter.
- **CBT on OpenShift volumes.** vSphere cannot enable CBT on a volume that
  is attached to a node, so such volumes are read in full every time
  (deduplication keeps the stored size small).
- **Single server.** No backup proxies; one task per repository at a time.
- **Restoring over a VM** powers it off without a guest shutdown.
