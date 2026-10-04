#!/usr/bin/python3
"""Build a small guest disk image for the file-level restore tests.

Run with the system Python (libguestfs bindings). Layout, like a typical
Linux VM: MBR, partition 1 ext4 mounted at /, partition 2 an LVM volume
group with an XFS logical volume mounted at /data.
"""
import sys

import guestfs

path = sys.argv[1]
g = guestfs.GuestFS(python_return_dict=True)
g.set_backend("direct")
g.disk_create(path, "raw", 256 * 1024 * 1024)
g.add_drive_opts(path, format="raw")
g.launch()
g.part_init("/dev/sda", "mbr")
g.part_add("/dev/sda", "p", 2048, 262143)
g.part_add("/dev/sda", "p", 262144, -2048)
g.mkfs("ext4", "/dev/sda1", label="root")
g.pvcreate("/dev/sda2")
g.vgcreate("vgdata", ["/dev/sda2"])
g.lvcreate("lvdata", "vgdata", 100)
g.mkfs("xfs", "/dev/vgdata/lvdata", label="data")
g.mount("/dev/sda1", "/")
for d in ("/etc", "/home/alice/docs", "/data"):
    g.mkdir_p(d)
g.write("/etc/fstab", "LABEL=root / ext4 defaults 0 1\n"
                      "/dev/vgdata/lvdata /data xfs defaults 0 0\n")
g.write("/etc/os-release", 'NAME="Test Linux"\nID=testlinux\nVERSION_ID="1"\n')
g.write("/etc/hostname", "flr-test\n")
g.write("/home/alice/docs/report.txt", "quarterly numbers\n")
g.write("/home/alice/docs/notes.md", "# notes\n" * 1000)
g.chmod(0o600, "/home/alice/docs/report.txt")
g.chown(1000, 1000, "/home/alice/docs/report.txt")
g.mount("/dev/vgdata/lvdata", "/data")
g.write("/data/blob.bin", bytes(range(256)) * 4096)
g.mkdir_p("/data/sub/deeper")
g.write("/data/sub/deeper/leaf.txt", "leaf\n")
g.umount_all()
g.shutdown()
g.close()
