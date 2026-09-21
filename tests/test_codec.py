"""Tests for chunk encoding: compression, encryption and verification."""

import os

import pytest

from openbackup.repo.codec import ChunkCodec, ChunkCorruption, chunk_hash

MIB = 1024 * 1024


@pytest.fixture
def codec():
    return ChunkCodec()


def test_roundtrip_preserves_contents(codec):
    data = os.urandom(64 * 1024)
    assert codec.decode(codec.encode(data), chunk_hash(data)) == data


def test_empty_block_roundtrips(codec):
    assert codec.decode(codec.encode(b""), chunk_hash(b"")) == b""


def test_identical_blocks_hash_identically():
    data = os.urandom(4096)
    assert chunk_hash(data) == chunk_hash(bytes(data))


def test_zero_blocks_collapse(codec):
    """Unallocated space in a thin disk is the most common block there is."""
    assert len(codec.encode(b"\0" * MIB)) < 1024


def test_incompressible_blocks_are_not_inflated(codec):
    """Already-compressed guest data must not pay a compression penalty."""
    data = os.urandom(MIB)
    assert len(codec.encode(data)) < len(data) + 64


def test_bitrot_is_detected_not_returned(codec):
    """Silently restoring altered blocks is worse than failing loudly."""
    data = os.urandom(8192)
    blob = bytearray(codec.encode(data))
    blob[-1] ^= 0xFF
    with pytest.raises(ChunkCorruption):
        codec.decode(bytes(blob), chunk_hash(data))


def test_truncated_blob_is_detected(codec):
    data = os.urandom(8192)
    blob = codec.encode(data)
    with pytest.raises(ChunkCorruption):
        codec.decode(blob[:8], chunk_hash(data))


def test_wrong_magic_is_detected(codec):
    with pytest.raises(ChunkCorruption, match="bad magic"):
        codec.decode(b"XXXX" + b"\0" * 32, chunk_hash(b"x"))


def test_blob_stored_under_the_wrong_hash_is_detected(codec):
    """Catches an index that points at the wrong place inside a pack."""
    blob = codec.encode(b"the real contents")
    with pytest.raises(ChunkCorruption, match="hashes to"):
        codec.decode(blob, chunk_hash(b"something else"))


def test_encrypted_roundtrip():
    key = os.urandom(32)
    codec = ChunkCodec(key=key)
    data = os.urandom(16 * 1024)
    blob = codec.encode(data)
    assert data not in blob
    assert codec.decode(blob, chunk_hash(data)) == data


def test_encrypted_data_is_unreadable_without_the_key():
    data = os.urandom(4096)
    blob = ChunkCodec(key=os.urandom(32)).encode(data)
    with pytest.raises(ChunkCorruption, match="no key"):
        ChunkCodec().decode(blob, chunk_hash(data))


def test_wrong_key_fails_authentication():
    data = os.urandom(4096)
    blob = ChunkCodec(key=os.urandom(32)).encode(data)
    with pytest.raises(ChunkCorruption, match="authentication"):
        ChunkCodec(key=os.urandom(32)).decode(blob, chunk_hash(data))


def test_plaintext_blob_in_an_encrypted_repo_is_rejected():
    """Otherwise an attacker could swap in unencrypted blocks of their choice."""
    data = os.urandom(4096)
    blob = ChunkCodec().encode(data)
    with pytest.raises(ChunkCorruption, match="unencrypted"):
        ChunkCodec(key=os.urandom(32)).decode(blob, chunk_hash(data))


def test_bad_key_length_is_rejected():
    with pytest.raises(ValueError, match="16, 24 or 32"):
        ChunkCodec(key=b"tooshort")
