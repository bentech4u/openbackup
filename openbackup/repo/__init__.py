"""Repository: the backup format and where it is stored.

* :mod:`codec`     -- compress, encrypt, frame and verify one chunk
* :mod:`packfile`  -- group chunks into pack files
* :mod:`blockmap`  -- a disk's chunk hashes, which is what a restore point is
* :mod:`backend`   -- repository storage, local or on a managed NFS mount
* :mod:`index`     -- local, rebuildable hash -> pack location cache
* :mod:`store`     -- the chunk store the rest of the system uses
"""
