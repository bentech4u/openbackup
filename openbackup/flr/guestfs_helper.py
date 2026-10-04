#!/usr/bin/python3
"""Filesystem access to a restore point, run as a separate process.

Executed with the system Python, which has RHEL's libguestfs bindings, so
it must not import the openbackup package (which needs a newer Python).
It speaks JSON lines on stdin/stdout:

    {"id": 1, "cmd": "open", "args": {...}}  ->  {"id": 1, "ok": true, "result": ...}

libguestfs parses the guest filesystems inside a small appliance VM, not in
this host's kernel, so a damaged or hostile backup cannot reach the host.
Every filesystem is mounted read-only.
"""

import json
import os
import stat
import sys

import guestfs

g = None
volumes = []


def _type(mode):
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


def _entry(name, st):
    return {
        "name": name,
        "type": _type(st["st_mode"]),
        "size": st["st_size"],
        "mode": stat.S_IMODE(st["st_mode"]),
        "uid": st["st_uid"],
        "gid": st["st_gid"],
        "mtime": st["st_mtime_sec"],
    }


def _check_path(path):
    if not isinstance(path, str) or not path.startswith("/v") or "\0" in path:
        raise ValueError("invalid path")
    parts = path.split("/")
    if ".." in parts:
        raise ValueError("invalid path")
    if parts[1] not in {v["id"] for v in volumes if v["mounted"]}:
        raise ValueError("unknown volume")
    return path


def cmd_open(drives):
    global g
    g = guestfs.GuestFS(python_return_dict=True)
    g.set_backend(os.environ.get("LIBGUESTFS_BACKEND", "direct"))
    g.set_memsize(int(os.environ.get("OPENBACKUP_FLR_MEMSIZE", "1024")))
    for d in drives:
        g.add_drive_opts(d["export"], format="raw", readonly=True, protocol="nbd",
                         server=["unix:" + d["socket"]])
    g.launch()

    guest_paths, os_info = {}, None
    for root in g.inspect_os():
        kind = g.inspect_get_type(root)
        os_info = {"type": kind, "distro": g.inspect_get_distro(root),
                   "name": g.inspect_get_product_name(root),
                   "hostname": g.inspect_get_hostname(root)}
        if kind == "windows":
            try:
                for letter, dev in g.inspect_get_drive_mappings(root).items():
                    guest_paths[dev] = letter.upper() + ":"
            except RuntimeError:
                pass
            guest_paths.setdefault(root, "C:")
        else:
            for mp, dev in g.inspect_get_mountpoints(root).items():
                guest_paths[dev] = mp

    fss = g.list_filesystems()
    order = sorted(fss, key=lambda dev: (guest_paths.get(dev) is None,
                                         len(guest_paths.get(dev) or ""), dev))
    for dev in order:
        fstype = fss[dev]
        if fstype in ("swap", "unknown", "") or fstype.startswith("crypto_"):
            continue
        vid = "v%d" % len(volumes)
        vol = {"id": vid, "device": dev, "fstype": fstype, "guest_path": guest_paths.get(dev),
               "label": "", "size": 0, "mounted": False, "error": ""}
        try:
            vol["label"] = g.vfs_label(dev)
        except RuntimeError:
            pass
        try:
            vol["size"] = g.blockdev_getsize64(dev)
        except RuntimeError:
            pass
        try:
            g.mkmountpoint("/" + vid)
            g.mount_ro(dev, "/" + vid)
            vol["mounted"] = True
        except RuntimeError as e:
            vol["error"] = str(e).splitlines()[0][:300]
        volumes.append(vol)
    return {"volumes": volumes, "os": os_info}


def cmd_ls(path):
    path = _check_path(path)
    names = sorted(g.ls(path))
    out = []
    for i in range(0, len(names), 500):
        batch = names[i:i + 500]
        for name, st in zip(batch, g.lstatnslist(path, batch)):
            out.append(_entry(name, st))
    return out


def cmd_stat(path):
    path = _check_path(path)
    return _entry(path.rsplit("/", 1)[-1], g.lstatns(path))


def cmd_download(path, dest):
    path = _check_path(path)
    st = g.lstatns(path)
    if not stat.S_ISREG(st["st_mode"]):
        raise ValueError("not a regular file")
    g.download(path, dest)
    return _entry(path.rsplit("/", 1)[-1], st)


def cmd_walk(path, limit=200000):
    """Every directory and file below ``path`` (symlinks are not followed)."""
    path = _check_path(path)
    out = []
    stack = [""]
    while stack:
        rel = stack.pop()
        full = path + rel
        for e in cmd_ls(full):
            e["path"] = rel + "/" + e["name"]
            out.append(e)
            if len(out) > limit:
                raise ValueError("more than %d entries; pick a smaller folder" % limit)
            if e["type"] == "dir":
                stack.append(e["path"])
    return out


COMMANDS = {"open": cmd_open, "ls": cmd_ls, "stat": cmd_stat, "download": cmd_download,
            "walk": cmd_walk}


def main():
    out = sys.stdout
    # libguestfs and the appliance may print; keep stdout for the protocol.
    sys.stdout = sys.stderr
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("cmd") == "quit":
            break
        try:
            result = COMMANDS[req["cmd"]](**req.get("args", {}))
            resp = {"id": req.get("id"), "ok": True, "result": result}
        except Exception as e:  # report every failure to the caller
            resp = {"id": req.get("id"), "ok": False, "error": str(e) or type(e).__name__}
        out.write(json.dumps(resp) + "\n")
        out.flush()
    if g is not None:
        try:
            g.shutdown()
            g.close()
        except RuntimeError:
            pass


if __name__ == "__main__":
    main()
