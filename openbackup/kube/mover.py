"""Fill restored volumes inside OpenShift.

A short-lived "mover" pod in the target namespace mounts the new volume
claim and pulls the data from OpenBackup over HTTPS:

* Filesystem volumes get a tar of the backed-up filesystem (made by
  libguestfs, read-only), unpacked with `tar -x`. This works for any target
  storage class and runs under the restricted SCC.
* Block volumes (typical for VM disks) get the raw disk, written with `dd`.

The worker produces the stream into a private FIFO; the API process serves
it to exactly one request carrying the right single-use token, so the
stream never touches disk and cannot be fetched twice.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ..engine.context import TaskContext, check_cancel
from .client import KubeClient, KubeError

DEFAULT_IMAGESTREAM = ("openshift", "tools", "latest")
CHANNEL_TTL = 3600


class MoverError(Exception):
    pass


# ----------------------------------------------------------------- channels


def channel_dir(data_dir: Path) -> Path:
    d = data_dir / "movers"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


@dataclass
class Channel:
    id: str
    token: str
    fifo: Path


def open_channel(data_dir: Path) -> Channel:
    """A FIFO named by the hash of a fresh token: whoever holds the token can
    read the stream, once."""
    token = secrets.token_urlsafe(32)
    cid = hashlib.sha256(token.encode()).hexdigest()
    fifo = channel_dir(data_dir) / cid
    os.mkfifo(fifo, 0o600)
    return Channel(cid, token, fifo)


def claim_channel(data_dir: Path, cid: str, token: str) -> Path | None:
    """For the API: the FIFO if the token matches and the channel is fresh and
    unused, renamed so nobody can claim it again."""
    if len(cid) != 64 or not all(c in "0123456789abcdef" for c in cid):
        return None
    if not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), cid):
        return None
    fifo = channel_dir(data_dir) / cid
    try:
        if time.time() - fifo.stat().st_mtime > CHANNEL_TTL:
            return None
        claimed = fifo.with_name(cid + ".claimed")
        os.rename(fifo, claimed)  # atomic: a second request finds nothing
        return claimed
    except FileNotFoundError:
        return None


def close_channel(ch: Channel) -> None:
    for p in (ch.fifo, ch.fifo.with_name(ch.fifo.name + ".claimed")):
        with suppress(FileNotFoundError):
            p.unlink()


def release_writer(ch: Channel) -> None:
    """Unblock a producer still waiting for its reader (the pod never came):
    open and close the read end, so the producer's write fails."""
    for p in (ch.fifo, ch.fifo.with_name(ch.fifo.name + ".claimed")):
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        os.close(fd)


# --------------------------------------------------------------------- pods


def resolve_image(c: KubeClient, override: str | None) -> str:
    if override:
        return override
    ns, name, tag = DEFAULT_IMAGESTREAM
    try:
        ist = c.get(f"/apis/image.openshift.io/v1/namespaces/{ns}/imagestreamtags/{name}:{tag}")
        return ist["image"]["dockerImageReference"]
    except (KubeError, KeyError) as e:
        raise MoverError(f"Cannot find the {ns}/{name}:{tag} image on the target cluster "
                         f"({e}); set OPENBACKUP_MOVER_IMAGE to an image with curl and tar") \
            from None


def pod_spec(name: str, namespace: str, pvc: str, block: bool, image: str, url: str,
             token: str, ca_pem: str) -> dict:
    fetch = ('printf "%s" "$OB_CA" > /tmp/ca.pem && '
             'curl --fail --silent --show-error --cacert /tmp/ca.pem '
             '-H "Authorization: Bearer $OB_TOKEN" "$OB_URL"')
    if block:
        script = f"set -o pipefail; {fetch} | dd of=/dev/obdata bs=4M conv=fsync status=none"
    else:
        script = (f"set -o pipefail; {fetch} | tar -x -C /data --no-same-owner "
                  "--no-overwrite-dir --delay-directory-restore")
    container: dict = {
        "name": "mover", "image": image,
        "command": ["/bin/bash", "-c", script],
        "env": [{"name": "OB_URL", "value": url}, {"name": "OB_TOKEN", "value": token},
                {"name": "OB_CA", "value": ca_pem}],
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                      "limits": {"memory": "512Mi"}},
    }
    pod: dict = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "openbackup",
                                "openbackup.io/role": "restore-mover"}},
        "spec": {"restartPolicy": "Never", "containers": [container],
                 "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc}}]},
    }
    if block:
        # Raw device access needs root; the restore account may use the
        # privileged SCC for exactly this pod.
        pod["metadata"]["annotations"] = {"openshift.io/required-scc": "privileged"}
        container["securityContext"] = {"runAsUser": 0, "privileged": True}
        container["volumeDevices"] = [{"name": "data", "devicePath": "/dev/obdata"}]
    else:
        container["securityContext"] = {
            "allowPrivilegeEscalation": False, "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
        container["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
    return pod


def run_mover(c: KubeClient, namespace: str, pvc: str, block: bool, image: str, base_url: str,
              ca_pem: str, data_dir: Path, produce: Callable[[Path], None], ctx: TaskContext,
              start_timeout: float = 900, poll: float = 2.0) -> None:
    """Create the mover pod, feed it with ``produce(fifo)`` and wait for it."""
    ch = open_channel(data_dir)
    name = f"openbackup-restore-{pvc}"[:52] + "-" + secrets.token_hex(3)
    pod = pod_spec(name, namespace, pvc, block, image, f"{base_url}/api/mover/{ch.id}",
                   ch.token, ca_pem)
    pod_path = f"/api/v1/namespaces/{namespace}/pods/{name}"
    produced: dict = {}

    def producer() -> None:
        try:
            produce(ch.fifo)
        except BaseException as e:  # reported by the waiting thread
            produced["error"] = e

    try:
        c.create(f"/api/v1/namespaces/{namespace}/pods", pod)
        t = threading.Thread(target=producer, name=f"mover-{pvc}", daemon=True)
        t.start()
        started = time.monotonic()
        phase = ""
        while True:
            check_cancel(ctx)
            try:
                st = c.get(pod_path).get("status") or {}
            except KubeError as e:
                raise MoverError(f"lost track of mover pod {name}: {e}") from None
            phase = st.get("phase", "")
            if phase in ("Succeeded", "Failed"):
                break
            if phase == "Pending" and time.monotonic() - started > start_timeout:
                raise MoverError(f"mover pod {name} did not start within "
                                 f"{int(start_timeout)}s: {_reason(st)}")
            time.sleep(poll)
        if phase == "Failed":
            raise MoverError(f"mover pod for {pvc} failed: {_logs(c, pod_path)}")
        t.join(timeout=60)
        if "error" in produced:
            raise MoverError(f"producing data for {pvc} failed: {produced['error']}")
    finally:
        release_writer(ch)
        close_channel(ch)
        with suppress(KubeError):
            c.delete(pod_path)


def _reason(st: dict) -> str:
    for cs in st.get("containerStatuses") or []:
        w = (cs.get("state") or {}).get("waiting") or {}
        if w:
            return f"{w.get('reason')}: {w.get('message', '')}".strip()
    for cond in st.get("conditions") or []:
        if cond.get("status") == "False" and cond.get("message"):
            return cond["message"]
    return "still pending"


def _logs(c: KubeClient, pod_path: str) -> str:
    try:
        return c.get_text(f"{pod_path}/log", tailLines=20).strip()[-1500:] or "no output"
    except KubeError as e:
        return f"(logs unavailable: {e})"


def write_raw(fifo: Path, chunks) -> None:
    """Producer for block volumes: write the disk image into the FIFO."""
    try:
        with open(fifo, "wb") as f:
            for data in chunks:
                f.write(data)
    except BrokenPipeError:
        raise MoverError("the mover pod stopped reading") from None
    except OSError as e:
        if e.errno == errno.EPIPE:
            raise MoverError("the mover pod stopped reading") from None
        raise
