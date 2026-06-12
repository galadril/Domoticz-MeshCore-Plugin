"""chan_hash resolution — restore-on-load and lowercase normalization tests.

Verifies:
  (a) _load_rx_log restores _chan_hash_to_name from a persisted
      chan_hash_names dict, tolerating missing / old files.
  (b) Keys are lowercased on restore so mixed-case firmware hashes
      compare reliably with lowercased on-air hashes.
  (c) The _on_rx_log path lowercases chan_hash before counting so keys
      always match the lowercase-stored chan_hash_to_name.
  (d) _load_rx_log restores _chan_hash_counts (the "Hashes heard on air"
      table) from the persisted stats.chan_hash_counts dict, lowercasing
      and merging keys, skipping junk, and never double-counting live
      counters on a warm disable→enable.
"""
import _bootstrap  # noqa: F401  (sys.path side-effect)
import json
import os
import shutil
import tempfile
import unittest


def _plugin():
    import plugin
    return plugin._plugin


class TestLoadRxLogRestoresChanHash(unittest.TestCase):
    """(a)/(b) _load_rx_log must restore _chan_hash_to_name from persisted file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        p = _plugin()
        # Ensure packet_times is empty so _load_rx_log doesn't short-circuit.
        p._packet_times.clear()
        p._chan_hash_to_name = {}

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        p = _plugin()
        p._chan_hash_to_name = {}

    def _write_rx_log_file(self, payload):
        path = os.path.join(self.tmpdir, "meshcore_rx_log.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def _run_load(self, payload):
        p = _plugin()
        path = self._write_rx_log_file(payload)
        orig = p._rx_log_path
        p._rx_log_path = lambda: path
        p._packet_times.clear()
        p._chan_hash_to_name = {}
        try:
            p._load_rx_log()
        finally:
            p._rx_log_path = orig
        return dict(p._chan_hash_to_name)

    def test_restore_populates_chan_hash_to_name(self):
        """chan_hash_names present in file → _chan_hash_to_name populated."""
        result = self._run_load({
            "packet_times": [],
            "chan_hash_names": {"a3": "General", "7f": "Local"},
        })
        self.assertEqual(result.get("a3"), "General")
        self.assertEqual(result.get("7f"), "Local")

    def test_restore_lowercases_keys(self):
        """Mixed-case keys in the persisted file must be lowercased on restore."""
        result = self._run_load({
            "packet_times": [],
            "chan_hash_names": {"A3": "General", "7F": "Local"},
        })
        self.assertIn("a3", result, "Key A3 must be stored as a3")
        self.assertNotIn("A3", result, "Original case key must not survive")
        self.assertEqual(result["a3"], "General")

    def test_restore_missing_field_is_noop(self):
        """File present but no chan_hash_names key → _chan_hash_to_name stays empty."""
        result = self._run_load({"packet_times": []})
        self.assertEqual(result, {})

    def test_restore_missing_file_is_noop(self):
        """No file → _chan_hash_to_name stays empty, no exception."""
        p = _plugin()
        nonexistent = os.path.join(self.tmpdir, "does_not_exist.json")
        orig = p._rx_log_path
        p._rx_log_path = lambda: nonexistent
        p._packet_times.clear()
        p._chan_hash_to_name = {}
        try:
            p._load_rx_log()
        finally:
            p._rx_log_path = orig
        self.assertEqual(p._chan_hash_to_name, {})

    def test_restore_empty_dict_is_noop(self):
        """Empty chan_hash_names dict in file → _chan_hash_to_name stays empty."""
        result = self._run_load({"packet_times": [], "chan_hash_names": {}})
        self.assertEqual(result, {})

    def test_restore_skips_entries_with_empty_key_or_value(self):
        """Entries with empty key or empty value must be skipped."""
        result = self._run_load({
            "packet_times": [],
            "chan_hash_names": {"a3": "General", "": "Bad", "b1": ""},
        })
        self.assertIn("a3", result)
        self.assertNotIn("", result)
        self.assertNotIn("b1", result)


class TestLoadRxLogRestoresChanHashCounts(unittest.TestCase):
    """(d) _load_rx_log must restore _chan_hash_counts from stats.chan_hash_counts."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        p = _plugin()
        p._packet_times.clear()
        with p._rx_log_lock:
            p._chan_hash_counts.clear()
            p._chan_hash_to_name = {}

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        p = _plugin()
        p._packet_times.clear()
        with p._rx_log_lock:
            p._chan_hash_counts.clear()
            p._chan_hash_to_name = {}

    def _run_load(self, payload, preexisting_counts=None):
        p = _plugin()
        path = os.path.join(self.tmpdir, "meshcore_rx_log.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        orig = p._rx_log_path
        p._rx_log_path = lambda: path
        p._packet_times.clear()
        with p._rx_log_lock:
            p._chan_hash_counts.clear()
            if preexisting_counts:
                p._chan_hash_counts.update(preexisting_counts)
        try:
            p._load_rx_log()
        finally:
            p._rx_log_path = orig
        with p._rx_log_lock:
            return dict(p._chan_hash_counts)

    def test_restore_populates_counts(self):
        result = self._run_load({
            "packet_times": [],
            "stats": {"chan_hash_counts": {"a3": 42, "7f": 3}},
        })
        self.assertEqual(result.get("a3"), 42)
        self.assertEqual(result.get("7f"), 3)

    def test_restore_lowercases_and_merges_keys(self):
        """Mixed-case duplicates of the same hash must merge under lowercase."""
        result = self._run_load({
            "packet_times": [],
            "stats": {"chan_hash_counts": {"A3": 10, "a3": 5}},
        })
        self.assertNotIn("A3", result)
        self.assertEqual(result.get("a3"), 15)

    def test_restore_skips_junk_values(self):
        """Empty keys, non-numeric and non-positive counts must be skipped."""
        result = self._run_load({
            "packet_times": [],
            "stats": {"chan_hash_counts": {"a3": 7, "": 9, "b1": "x", "c2": 0}},
        })
        self.assertEqual(result, {"a3": 7})

    def test_restore_missing_stats_is_noop(self):
        result = self._run_load({"packet_times": []})
        self.assertEqual(result, {})

    def test_restore_does_not_double_count_live_counters(self):
        """Non-empty live counters (warm re-enable) must be left untouched."""
        result = self._run_load(
            {"packet_times": [], "stats": {"chan_hash_counts": {"a3": 42}}},
            preexisting_counts={"a3": 5},
        )
        self.assertEqual(result.get("a3"), 5)


def _make_rx_event(chan_hash, payload_typename="CHAN_DATA"):
    """Build a minimal fake rx-log event object matching what _on_rx_log expects."""
    class _Ev:
        pass
    ev = _Ev()
    ev.payload = {"payload_typename": payload_typename, "chan_hash": chan_hash}
    return ev


class TestChanHashLowercaseNormalization(unittest.TestCase):
    """(c) On-air chan_hash is lowercased before counting so keys match storage."""

    def setUp(self):
        p = _plugin()
        with p._rx_log_lock:
            p._rx_log.clear()
            p._chan_hash_counts.clear()
            p._chan_hash_to_name = {"a3": "General"}

    def tearDown(self):
        p = _plugin()
        with p._rx_log_lock:
            p._rx_log.clear()
            p._chan_hash_counts.clear()
            p._chan_hash_to_name = {}

    def test_uppercase_on_air_hash_counted_under_lowercase_key(self):
        """An uppercase chan_hash from the firmware must count under its
        lowercase form so it matches the stored _chan_hash_to_name key."""
        p = _plugin()
        p._on_rx_log(_make_rx_event("A3"))
        with p._rx_log_lock:
            counts = dict(p._chan_hash_counts)
        self.assertIn("a3", counts, "Lowercase key must be present in counts")
        self.assertNotIn("A3", counts, "Uppercase key must not appear in counts")
        self.assertEqual(counts["a3"], 1)

    def test_lowercase_on_air_hash_counted_correctly(self):
        """A lowercase chan_hash must work as before."""
        p = _plugin()
        p._on_rx_log(_make_rx_event("a3"))
        with p._rx_log_lock:
            counts = dict(p._chan_hash_counts)
        self.assertEqual(counts.get("a3"), 1)

    def test_uppercase_chan_hash_normalized_in_entry(self):
        """After _on_rx_log, the stored entry's chan_hash field must be lowercase."""
        p = _plugin()
        p._on_rx_log(_make_rx_event("A3"))
        with p._rx_log_lock:
            entry = list(p._rx_log)[-1]
        self.assertEqual(entry.get("chan_hash"), "a3")


if __name__ == "__main__":
    unittest.main()
