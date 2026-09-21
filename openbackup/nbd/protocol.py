"""Wire constants and struct layouts for the NBD fixed-newstyle protocol.

Reference: https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md

We speak fixed-newstyle with simple replies only. Structured replies are not
negotiated: the only thing we would gain is NBD_CMD_BLOCK_STATUS, and we get
allocation information from VMware CBT (QueryChangedDiskAreas with changeId="*")
instead, which works identically across every VADP transport.
"""

from __future__ import annotations

import struct

# --- handshake -------------------------------------------------------------

NBD_MAGIC = 0x4E42444D41474943  # "NBDMAGIC"
NBD_IHAVEOPT = 0x49484156454F5054  # "IHAVEOPT"
NBD_OPT_REPLY_MAGIC = 0x0003E889045565A9

# Server handshake flags
NBD_FLAG_FIXED_NEWSTYLE = 1 << 0
NBD_FLAG_NO_ZEROES = 1 << 1

# Client handshake flags
NBD_FLAG_C_FIXED_NEWSTYLE = 1 << 0
NBD_FLAG_C_NO_ZEROES = 1 << 1

# --- options ---------------------------------------------------------------

NBD_OPT_EXPORT_NAME = 1
NBD_OPT_ABORT = 2
NBD_OPT_LIST = 3
NBD_OPT_STARTTLS = 5
NBD_OPT_INFO = 6
NBD_OPT_GO = 7
NBD_OPT_STRUCTURED_REPLY = 8

# Option replies
NBD_REP_ACK = 1
NBD_REP_SERVER = 2
NBD_REP_INFO = 3

NBD_REP_FLAG_ERROR = 1 << 31
NBD_REP_ERR_UNSUP = NBD_REP_FLAG_ERROR | 1
NBD_REP_ERR_POLICY = NBD_REP_FLAG_ERROR | 2
NBD_REP_ERR_INVALID = NBD_REP_FLAG_ERROR | 3
NBD_REP_ERR_PLATFORM = NBD_REP_FLAG_ERROR | 4
NBD_REP_ERR_TLS_REQD = NBD_REP_FLAG_ERROR | 5
NBD_REP_ERR_UNKNOWN = NBD_REP_FLAG_ERROR | 6
NBD_REP_ERR_SHUTDOWN = NBD_REP_FLAG_ERROR | 7
NBD_REP_ERR_BLOCK_SIZE_REQD = NBD_REP_FLAG_ERROR | 8

REP_ERR_NAMES = {
    NBD_REP_ERR_UNSUP: "option unsupported",
    NBD_REP_ERR_POLICY: "denied by server policy",
    NBD_REP_ERR_INVALID: "invalid option payload",
    NBD_REP_ERR_PLATFORM: "unsupported on this platform",
    NBD_REP_ERR_TLS_REQD: "server requires TLS",
    NBD_REP_ERR_UNKNOWN: "export not found",
    NBD_REP_ERR_SHUTDOWN: "server is shutting down",
    NBD_REP_ERR_BLOCK_SIZE_REQD: "server requires block size negotiation",
}

# NBD_REP_INFO payload types
NBD_INFO_EXPORT = 0
NBD_INFO_NAME = 1
NBD_INFO_DESCRIPTION = 2
NBD_INFO_BLOCK_SIZE = 3

# --- transmission flags (from NBD_INFO_EXPORT) -----------------------------

NBD_FLAG_HAS_FLAGS = 1 << 0
NBD_FLAG_READ_ONLY = 1 << 1
NBD_FLAG_SEND_FLUSH = 1 << 2
NBD_FLAG_SEND_FUA = 1 << 3
NBD_FLAG_ROTATIONAL = 1 << 4
NBD_FLAG_SEND_TRIM = 1 << 5
NBD_FLAG_SEND_WRITE_ZEROES = 1 << 6
NBD_FLAG_SEND_DF = 1 << 7
NBD_FLAG_CAN_MULTI_CONN = 1 << 8
NBD_FLAG_SEND_RESIZE = 1 << 9
NBD_FLAG_SEND_CACHE = 1 << 10
NBD_FLAG_SEND_FAST_ZERO = 1 << 11

# --- transmission phase ----------------------------------------------------

NBD_REQUEST_MAGIC = 0x25609513
NBD_SIMPLE_REPLY_MAGIC = 0x67446698

NBD_CMD_READ = 0
NBD_CMD_WRITE = 1
NBD_CMD_DISC = 2
NBD_CMD_FLUSH = 3
NBD_CMD_TRIM = 4
NBD_CMD_CACHE = 5
NBD_CMD_WRITE_ZEROES = 6
NBD_CMD_BLOCK_STATUS = 7

CMD_NAMES = {
    NBD_CMD_READ: "READ",
    NBD_CMD_WRITE: "WRITE",
    NBD_CMD_DISC: "DISC",
    NBD_CMD_FLUSH: "FLUSH",
    NBD_CMD_TRIM: "TRIM",
    NBD_CMD_CACHE: "CACHE",
    NBD_CMD_WRITE_ZEROES: "WRITE_ZEROES",
    NBD_CMD_BLOCK_STATUS: "BLOCK_STATUS",
}

# Command flags
NBD_CMD_FLAG_FUA = 1 << 0
NBD_CMD_FLAG_NO_HOLE = 1 << 1
NBD_CMD_FLAG_DF = 1 << 2
NBD_CMD_FLAG_REQ_ONE = 1 << 3
NBD_CMD_FLAG_FAST_ZERO = 1 << 4

# --- struct layouts (all big-endian) ---------------------------------------

S_HANDSHAKE = struct.Struct(">QQH")  # magic, ihaveopt, handshake flags
S_CLIENT_FLAGS = struct.Struct(">I")
S_OPTION = struct.Struct(">QII")  # ihaveopt, option, length
S_OPT_REPLY = struct.Struct(">QIII")  # magic, option, reply type, length
S_INFO_TYPE = struct.Struct(">H")
S_INFO_EXPORT = struct.Struct(">QH")  # size, transmission flags
S_INFO_BLOCK_SIZE = struct.Struct(">III")  # min, preferred, max
S_REQUEST = struct.Struct(">IHHQQI")  # magic, flags, type, cookie, offset, len
S_SIMPLE_REPLY = struct.Struct(">IIQ")  # magic, error, cookie

#: NBD servers cap a single request; nbdkit's limit is 64 MiB. Stay well under
#: it so we also work against qemu-nbd (32 MiB) and other servers.
MAX_REQUEST_SIZE = 32 * 1024 * 1024
