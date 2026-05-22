"""Tests for auth.verify_password constant-time behavior + bcrypt edge cases."""

import os
import sys
import time

import pytest

# auth.py refuses to import without this — set before any auth import
os.environ.setdefault("DEVFLEET_JWT_SECRET", "test-secret-not-for-production")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import auth


# ── DUMMY_HASH sanity ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_dummy_hash_is_a_real_bcrypt_hash():
    """DUMMY_HASH must be parseable bcrypt so the dummy verify call actually
    runs the bcrypt work factor — otherwise the timing-attack mitigation
    silently no-ops."""
    assert auth.DUMMY_HASH.startswith("$2b$") or auth.DUMMY_HASH.startswith("$2a$")
    # Round-trip: the precomputed hash should not validate any real password
    assert auth.verify_password("wrong", auth.DUMMY_HASH) is False


# ── verify_password — happy path ─────────────────────────────────────────────


@pytest.mark.unit
def test_verify_password_correct_returns_true():
    h = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", h) is True


@pytest.mark.unit
def test_verify_password_wrong_returns_false():
    h = auth.hash_password("hunter2")
    assert auth.verify_password("wrongpass", h) is False


# ── verify_password — None / empty hash (the A2 fix) ─────────────────────────


@pytest.mark.unit
def test_verify_password_none_hash_returns_false():
    """When user lookup fails (hashed=None), verify must return False
    rather than raising — caller should not need a separate code path."""
    assert auth.verify_password("anything", None) is False


@pytest.mark.unit
def test_verify_password_empty_hash_returns_false():
    """Empty string treated same as None — defensive against partial DB rows."""
    assert auth.verify_password("anything", "") is False


@pytest.mark.unit
def test_verify_password_none_hash_runs_bcrypt():
    """Constant-time check: verify(None) must take roughly the same time as
    verify(real_hash). If it short-circuits, login becomes a timing oracle
    for which emails are registered.

    We use a generous tolerance because pytest startup, CI noise, and
    bcrypt cost variance all jitter the wall-clock. The point is that
    the None path is in the same order of magnitude as the real path,
    not microseconds vs hundreds of milliseconds (the pre-fix gap).
    """
    real_hash = auth.hash_password("hunter2")

    # Warm-up — first bcrypt call after import is often slower
    auth.verify_password("warmup", real_hash)
    auth.verify_password("warmup", None)

    # Take several samples and use the median to avoid GC outliers
    def _time(hashed):
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            auth.verify_password("attempt", hashed)
            samples.append(time.perf_counter() - t0)
        samples.sort()
        return samples[len(samples) // 2]

    t_real = _time(real_hash)
    t_none = _time(None)

    # None path must take at least 25% of the real bcrypt time. If it returned
    # in a few microseconds, that's the pre-fix bug.
    assert t_none > 0.25 * t_real, (
        f"verify(None) too fast ({t_none*1000:.2f}ms) vs verify(real) "
        f"({t_real*1000:.2f}ms) — A2 timing fix regressed"
    )


# ── hash_password length cap ─────────────────────────────────────────────────


@pytest.mark.unit
def test_hash_password_rejects_over_72_bytes():
    """bcrypt silently truncates passwords > 72 bytes — we raise instead
    so the user knows their long password isn't being used in full."""
    long_pw = "x" * 73
    with pytest.raises(ValueError, match="72 bytes"):
        auth.hash_password(long_pw)


@pytest.mark.unit
def test_hash_password_accepts_72_bytes():
    """Exactly 72 bytes is the limit, not the cutoff."""
    pw = "x" * 72
    h = auth.hash_password(pw)
    assert auth.verify_password(pw, h) is True


@pytest.mark.unit
def test_hash_password_counts_utf8_bytes_not_chars():
    """Emoji are 4 bytes in UTF-8 — an 18-char emoji password is 72 bytes
    (exactly at the limit), 19 chars is over."""
    pw_at_limit = "🔒" * 18  # 18 * 4 = 72 bytes
    auth.hash_password(pw_at_limit)  # should not raise

    pw_over_limit = "🔒" * 19  # 76 bytes
    with pytest.raises(ValueError, match="72 bytes"):
        auth.hash_password(pw_over_limit)
