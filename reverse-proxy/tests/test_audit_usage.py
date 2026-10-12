"""AUDIT - admin/usage.py. Reproductions for audit findings only."""
import builtins
import threading
import time
import unittest

from admin import usage


class MemoryStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}

    def save(self):
        pass


class DimensionCapTest(unittest.TestCase):
    """BUG-U1 check (usage.py:66-68 and 88-90) - the per-bucket dimension caps
    do hold under a flood of distinct values. These PASS; recorded so the cap
    is marked audited rather than assumed."""

    def test_by_status_stays_bounded(self):
        store = MemoryStore()
        for i in range(usage.DIMENSION_KEEP * 3):
            usage.record(store, now=1791732600.0, status=1000 + i)
        bucket = store.data["usage_stats"]["daily"]["2026-10-11"]
        self.assertLessEqual(len(bucket["by_status"]), usage.DIMENSION_KEEP)

    def test_by_model_stays_bounded(self):
        store = MemoryStore()
        for i in range(usage.DIMENSION_KEEP * 3):
            usage.record(store, now=1791732600.0, status=200, model="m%d" % i)
        bucket = store.data["usage_stats"]["daily"]["2026-10-11"]
        self.assertLessEqual(len(bucket["by_model"]), usage.DIMENSION_KEEP)


class RecordStatusTypeTest(unittest.TestCase):
    """BUG-U1 (usage.py:110): record() does `int(status)` with no guard, so a
    status that is not a number raises TypeError out of the accounting path.

    log_request() -> log_task() -> usage.record() is called from
    PoolMiddleware.__call__'s `finally:` block (pool.py:1334-1343), which runs
    on the way out of the ASGI app. An exception raised there propagates out of
    the middleware after the response body has already been sent.

    status is typed `int` and the pool always seeds it with 200, so this needs a
    non-int status (a string code, or a caller that forgets the argument) - it
    is a robustness gap on a path where logging must never break the response,
    not a live crash today.
    """

    def test_non_numeric_status_does_not_raise(self):
        store = MemoryStore()
        try:
            usage.record(store, now=1791732600.0, status=None)
        except TypeError as exc:
            self.fail("record() raised TypeError instead of recording/ignoring: %s" % exc)


class PruneConcurrencyTest(unittest.TestCase):
    """BUG-U2 (usage.py:95-100): _prune() is a check-then-act over a dict that
    concurrent record() calls mutate, and nothing serialises it.

    _prune computes `sorted(buckets)[: len(buckets) - keep]` and then deletes
    those keys one at a time. Two threads that both enter the loop delete the
    SAME keys; the loser raises KeyError.

    record() documents "caller holds the lock", and PoolMiddleware does hold it -
    but pool.tick() -> flush_usage() and the admin read paths all touch the same
    nested dicts, and snapshot() only takes store.lock for the shallow `dict()`
    copy, reading nested buckets after releasing it (usage.py:157-166).

    Reproduced for real under 6 concurrent writers in ~3s:
        KeyError: '2026-10-16T11'  raised from usage.py:100
    This test pins it deterministically by parking one thread inside the delete
    loop (via a traced dict) while a second thread completes the same prune.
    """

    def test_concurrent_prune_does_not_raise(self):
        del_calls = []
        gate = threading.Event()
        parked = threading.Event()

        # 钩 pop 而不是 __delitem__：修好之后 _prune 用的是 pop(key, None)，
        # 它才是那条竞态真正会走的路径。钩 __delitem__ 会变成「钩一个再也不被
        # 调用的方法」—— 测试会红，但红得毫无意义（它没在测任何东西）。
        class TracedDict(dict):
            def pop(self, key, *default):
                del_calls.append(key)
                if len(del_calls) == 1:
                    parked.set()
                    gate.wait(timeout=5)
                return dict.pop(self, key, *default)

        buckets = TracedDict({"a": 1, "b": 1, "c": 1, "d": 1, "e": 1, "f": 1})
        errors = []

        def worker(tag):
            try:
                usage._prune(buckets, 3)
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: %s: %s" % (tag, type(exc).__name__, exc))

        first = threading.Thread(target=worker, args=("T1",))
        first.start()
        self.assertTrue(parked.wait(timeout=5), "prune never reached the delete loop")
        second = threading.Thread(target=worker, args=("T2",))
        second.start()
        second.join()
        gate.set()
        first.join()
        self.assertEqual(errors, [], "concurrent _prune() raised: %s" % errors)

    def test_record_under_concurrency_does_not_raise(self):
        """Same bug through the public entry point, no patching."""
        store = MemoryStore()
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            while not stop.is_set():
                try:
                    # 200 distinct hours => the hourly bucket is permanently
                    # over HOURLY_KEEP, so every call runs _prune.
                    usage.record(store, now=1791732600.0 + (i % 200) * 3600, status=200)
                except Exception as exc:  # noqa: BLE001
                    errors.append("%s: %s" % (type(exc).__name__, exc))
                    return
                i += 1

        threads = [threading.Thread(target=writer) for _ in range(6)]
        for t in threads:
            t.start()
        deadline = time.time() + 20
        while time.time() < deadline and not errors:
            time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], "record() raised under concurrency: %s" % errors)


class PruneOrderingTest(unittest.TestCase):
    """BUG-U3 check (usage.py:95-100): key ordering. PASSES."""

    def test_prune_keeps_the_chronologically_newest(self):
        store = MemoryStore()
        base = 1791732600.0
        for i in range(usage.DAILY_KEEP + 5):
            usage.record(store, now=base + i * 86400, status=200)
        daily = store.data["usage_stats"]["daily"]
        self.assertEqual(len(daily), usage.DAILY_KEEP)
        expected = sorted(usage.bucket_keys(base + i * 86400)[0]
                          for i in range(5, usage.DAILY_KEEP + 5))
        self.assertEqual(sorted(daily), expected)

    def test_bucket_key_width_is_constant(self):
        """The zero-padded, fixed-width guarantee the sort-based prune needs."""
        for now in (1791732600.0, 1791732600.0 + 86400 * 400, 946684800.0):
            day, hour = usage.bucket_keys(now)
            self.assertEqual(len(day), 10)
            self.assertEqual(len(hour), 13)


class SnapshotTotalsTest(unittest.TestCase):
    """Contract check (usage.py:168-180): totals sum the RETURNED rows. PASSES."""

    def test_totals_match_the_returned_rows(self):
        store = MemoryStore()
        base = 1791732600.0
        for i in range(10):
            usage.record(store, now=base + i * 86400, status=200, tokens=5)
        snap = usage.snapshot(store, days=3)
        self.assertEqual(snap["totals"]["daily"]["calls"],
                         sum(r["calls"] for r in snap["daily"]))
        self.assertEqual(snap["totals"]["daily"]["tokens"],
                         sum(r["tokens"] for r in snap["daily"]))
        self.assertEqual(len(snap["daily"]), 3)


if __name__ == "__main__":
    unittest.main()
