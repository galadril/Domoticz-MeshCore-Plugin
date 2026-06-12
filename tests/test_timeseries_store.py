"""Tests for the SQLite time-series analytics store (schema v4).

Covers:
  - Migration v3 -> v4 creates all five tables and both indices.
  - Idempotent on a fresh DB (full ladder 0->1->2->3->4).
  - _ts_ingest("rx", ...) inserts one row with the correct types.
  - _ts_packets_add upserts minute + hour rows correctly.
  - _ts_packets_add across a minute boundary (two separate minute rows).
  - _ts_packets_add across an hour boundary (two separate hour rows).
  - _q_rssi_snr returns one series per node, averaged into buckets, bounded.
  - _q_packets sums rx+tx per bucket.
  - _q_packets_hourly returns rows in time order.
  - _q_msg_per_channel reads from messages table, excludes chan='P'.
  - _q_hop_histogram groups by hops, excludes rows outside the range.
  - _q_top_relays orders by count DESC, respects limit.
  - _ts_prune removes rows older than each table's cutoff.
  - Dirty-flag invalidation: ingest marks panels dirty; query clears them.
"""
import _bootstrap  # noqa: F401
import os
import sqlite3
import tempfile
import time
import unittest

import plugin


# ── helpers ───────────────────────────────────────────────────────────────────

def _open_db(path):
    p = plugin.BasePlugin()
    p._msg_store_open(path)
    return p, p._msgdb


def _close(p, path):
    try:
        if p._msgdb is not None:
            p._msgdb.close()
            p._msgdb = None
    except Exception:
        pass
    for suf in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suf)
        except Exception:
            pass


def _make_v3_db(path):
    """Create a DB that looks exactly like a v3 schema (no ts_* tables)."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chan      TEXT    NOT NULL,
            sender    TEXT    NOT NULL,
            epoch     TEXT    NOT NULL,
            bad       INTEGER NOT NULL DEFAULT 0,
            body      TEXT    NOT NULL,
            hops      INTEGER,
            snr       REAL,
            rssi      INTEGER,
            path      TEXT,
            ack       INTEGER,
            direction TEXT    NOT NULL DEFAULT 'in',
            recv_ts   TEXT    NOT NULL,
            peer_key  TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS elevation_cache (
            lat_q     INTEGER NOT NULL,
            lon_q     INTEGER NOT NULL,
            elev_m    REAL    NOT NULL,
            last_used INTEGER NOT NULL,
            PRIMARY KEY (lat_q, lon_q)
        )
    """)
    con.execute(
        "INSERT OR REPLACE INTO preferences VALUES ('db_version', '3')"
    )
    con.commit()
    con.close()


# ── Migration tests ───────────────────────────────────────────────────────────

class TestMigrationV3ToV4(unittest.TestCase):
    """Schema migration v3 -> v4 creates all five ts_* tables and indices."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        _close(getattr(self, "_p", plugin.BasePlugin()), self.path)

    def _open(self):
        _make_v3_db(self.path)
        self._p, db = _open_db(self.path)
        return db

    def _tables(self, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}

    def _indices(self, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        return {r[0] for r in rows}

    def test_ts_radio_table_created(self):
        db = self._open()
        self.assertIn("ts_radio", self._tables(db))

    def test_ts_packets_hourly_table_created(self):
        db = self._open()
        self.assertIn("ts_packets_hourly", self._tables(db))

    def test_ts_packets_min_table_created(self):
        db = self._open()
        self.assertIn("ts_packets_min", self._tables(db))

    def test_ts_relay_keys_table_created(self):
        db = self._open()
        self.assertIn("ts_relay_keys", self._tables(db))

    def test_ts_hops_table_created(self):
        db = self._open()
        self.assertIn("ts_hops", self._tables(db))

    def test_ix_ts_radio_ts_created(self):
        db = self._open()
        self.assertIn("ix_ts_radio_ts", self._indices(db))

    def test_ix_ts_radio_node_ts_created(self):
        db = self._open()
        self.assertIn("ix_ts_radio_node_ts", self._indices(db))

    def test_ix_ts_hops_ts_created(self):
        db = self._open()
        self.assertIn("ix_ts_hops_ts", self._indices(db))

    def test_db_version_is_4(self):
        _make_v3_db(self.path)
        self._p, _ = _open_db(self.path)
        self.assertEqual(self._p._pref_get("db_version"), "4")

    def test_idempotent_on_fresh_db(self):
        """A completely fresh DB (0->4) should have all ts_* tables without error."""
        self._p, db = _open_db(self.path)
        tables = self._tables(db)
        for tbl in ("ts_radio", "ts_packets_hourly", "ts_packets_min",
                    "ts_relay_keys", "ts_hops"):
            self.assertIn(tbl, tables, f"{tbl} missing on fresh DB")
        self.assertEqual(self._p._pref_get("db_version"), "4")


# ── _ts_ingest tests ──────────────────────────────────────────────────────────

class TestTsIngest(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)

    def tearDown(self):
        _close(self.p, self.path)

    def test_inserts_one_row(self):
        self.p._ts_ingest("rx", node_key="aabbcc112233",
                          rssi=-80, snr=7.5, noise=-110, path_len=2)
        with self.p._msgdb_lock:
            rows = self.db.execute("SELECT * FROM ts_radio").fetchall()
        self.assertEqual(len(rows), 1)

    def test_row_fields_and_types(self):
        before = int(time.time())
        self.p._ts_ingest("rx", node_key="aabbcc112233",
                          rssi=-80, snr=7.5, noise=-110, path_len=2)
        with self.p._msgdb_lock:
            row = self.db.execute(
                "SELECT ts, node_key, rssi, snr, noise, path_len, src"
                " FROM ts_radio"
            ).fetchone()
        self.assertGreaterEqual(row[0], before)
        self.assertEqual(row[1], "aabbcc112233")
        self.assertEqual(row[2], -80)
        self.assertAlmostEqual(row[3], 7.5)
        self.assertEqual(row[4], -110)
        self.assertEqual(row[5], 2)
        self.assertEqual(row[6], "rx")

    def test_null_fields_allowed(self):
        self.p._ts_ingest("adv", node_key="aabbcc112233", snr=5.0)
        with self.p._msgdb_lock:
            row = self.db.execute(
                "SELECT rssi, noise, path_len FROM ts_radio"
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])

    def test_marks_dirty_panels(self):
        self.p._ts_dirty_panels.clear()
        self.p._ts_ingest("rx", node_key="self", rssi=-70, snr=8.0)
        self.assertIn("rssi", self.p._ts_dirty_panels)
        self.assertIn("snr",  self.p._ts_dirty_panels)
        self.assertIn("noise", self.p._ts_dirty_panels)

    def test_never_raises_on_closed_db(self):
        self.p._msgdb = None
        # Should not raise
        self.p._ts_ingest("rx", node_key="self", rssi=-70)


# ── _ts_packets_add tests ─────────────────────────────────────────────────────

class TestTsPacketsAdd(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)

    def tearDown(self):
        _close(self.p, self.path)

    def _min_rows(self):
        with self.p._msgdb_lock:
            return self.db.execute(
                "SELECT ts, rx_count, tx_count FROM ts_packets_min ORDER BY ts"
            ).fetchall()

    def _hour_rows(self):
        with self.p._msgdb_lock:
            return self.db.execute(
                "SELECT hour_ts, rx_count, tx_count,"
                " flood_rx, flood_tx, direct_rx, direct_tx"
                " FROM ts_packets_hourly ORDER BY hour_ts"
            ).fetchall()

    def test_inserts_minute_row(self):
        now = 1_700_000_000
        self.p._ts_packets_add(now, 10, 5)
        rows = self._min_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], now - (now % 60))
        self.assertEqual(rows[0][1], 10)
        self.assertEqual(rows[0][2], 5)

    def test_upserts_same_minute(self):
        now = 1_700_000_000
        self.p._ts_packets_add(now, 10, 5)
        self.p._ts_packets_add(now + 30, 3, 2)
        rows = self._min_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 13)
        self.assertEqual(rows[0][2], 7)

    def test_separate_rows_across_minute_boundary(self):
        t1 = 1_700_000_000
        t2 = t1 + 70  # next minute
        self.p._ts_packets_add(t1, 10, 5)
        self.p._ts_packets_add(t2, 4, 2)
        rows = self._min_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 10)
        self.assertEqual(rows[1][1], 4)

    def test_inserts_hour_row(self):
        now = 1_700_000_000
        self.p._ts_packets_add(now, 10, 5, flood_drx=2, flood_dtx=1, direct_drx=3, direct_dtx=2)
        rows = self._hour_rows()
        self.assertEqual(len(rows), 1)
        hr = rows[0]
        self.assertEqual(hr[0], now - (now % 3600))
        self.assertEqual(hr[1], 10)
        self.assertEqual(hr[2], 5)
        self.assertEqual(hr[3], 2)   # flood_rx
        self.assertEqual(hr[4], 1)   # flood_tx
        self.assertEqual(hr[5], 3)   # direct_rx
        self.assertEqual(hr[6], 2)   # direct_tx

    def test_upserts_same_hour(self):
        t1 = 1_700_000_000
        t2 = t1 + 300
        self.p._ts_packets_add(t1, 10, 5)
        self.p._ts_packets_add(t2, 4, 2)
        rows = self._hour_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 14)
        self.assertEqual(rows[0][2], 7)

    def test_separate_rows_across_hour_boundary(self):
        t1 = 1_700_000_000
        t2 = t1 + 3700
        self.p._ts_packets_add(t1, 10, 5)
        self.p._ts_packets_add(t2, 4, 2)
        rows = self._hour_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 10)
        self.assertEqual(rows[1][1], 4)

    def test_marks_packets_and_hourly_dirty(self):
        self.p._ts_dirty_panels.clear()
        self.p._ts_packets_add(int(time.time()), 1, 1)
        self.assertIn("packets", self.p._ts_dirty_panels)
        self.assertIn("hourly",  self.p._ts_dirty_panels)


# ── _q_rssi_snr tests ─────────────────────────────────────────────────────────

class TestQRssiSnr(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)
        # Seed ts_radio with two nodes, three rows each.
        # node A: rssi -70, -72, -68  -> avg -70
        # node B: rssi -90, -92       -> avg -91
        now = 1_700_010_000
        with self.p._msgdb_lock:
            for rssi in (-70, -72, -68):
                self.db.execute(
                    "INSERT INTO ts_radio (ts, node_key, rssi, snr, noise, path_len, src)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (now, "nodeA", rssi, 5.0, None, None, "rx"),
                )
            for rssi in (-90, -92):
                self.db.execute(
                    "INSERT INTO ts_radio (ts, node_key, rssi, snr, noise, path_len, src)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (now, "nodeB", rssi, 3.0, None, None, "rx"),
                )
            # Out-of-range row (should not appear)
            self.db.execute(
                "INSERT INTO ts_radio (ts, node_key, rssi, snr, noise, path_len, src)"
                " VALUES (?,?,?,?,?,?,?)",
                (now - 100000, "nodeA", -50, 10.0, None, None, "rx"),
            )
            self.db.commit()
        self.now = now

    def tearDown(self):
        _close(self.p, self.path)

    def test_returns_series_per_node(self):
        result = self.p._q_rssi_snr("rssi", self.now - 60, self.now + 60, 120, ())
        nodes = {s["node"] for s in result["series"]}
        self.assertIn("nodeA", nodes)
        self.assertIn("nodeB", nodes)

    def test_averages_into_bucket(self):
        result = self.p._q_rssi_snr("rssi", self.now - 60, self.now + 60, 120, ())
        nodeA = next(s for s in result["series"] if s["node"] == "nodeA")
        # All three rows fall in the same 120-s bucket; avg(-70,-72,-68) = -70
        self.assertEqual(len(nodeA["data"]), 1)
        self.assertAlmostEqual(nodeA["data"][0][1], -70.0)

    def test_excludes_out_of_range_rows(self):
        result = self.p._q_rssi_snr("rssi", self.now - 60, self.now + 60, 120, ())
        nodeA = next(s for s in result["series"] if s["node"] == "nodeA")
        # Only the 3 in-range rows should contribute, not the -50 out-of-range
        self.assertAlmostEqual(nodeA["data"][0][1], -70.0)

    def test_node_filter(self):
        result = self.p._q_rssi_snr("rssi", self.now - 60, self.now + 60, 120, ("nodeA",))
        nodes = {s["node"] for s in result["series"]}
        self.assertIn("nodeA", nodes)
        self.assertNotIn("nodeB", nodes)

    def test_empty_when_no_data(self):
        result = self.p._q_rssi_snr("rssi", 0, 1, 60, ())
        self.assertEqual(result["series"], [])


# ── _q_packets tests ──────────────────────────────────────────────────────────

class TestQPackets(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)
        self.base = 1_700_010_000
        # Two minute rows in range, one out-of-range
        with self.p._msgdb_lock:
            self.db.execute(
                "INSERT INTO ts_packets_min (ts, rx_count, tx_count) VALUES (?,?,?)",
                (self.base,       10, 5),
            )
            self.db.execute(
                "INSERT INTO ts_packets_min (ts, rx_count, tx_count) VALUES (?,?,?)",
                (self.base + 60,  8, 3),
            )
            self.db.execute(
                "INSERT INTO ts_packets_min (ts, rx_count, tx_count) VALUES (?,?,?)",
                (self.base - 200, 99, 99),   # out-of-range
            )
            self.db.commit()

    def tearDown(self):
        _close(self.p, self.path)

    def test_returns_rx_and_tx_series(self):
        result = self.p._q_packets("packets", self.base, self.base + 120, 60, ())
        names = {s["name"] for s in result["series"]}
        self.assertIn("rx", names)
        self.assertIn("tx", names)

    def test_sums_per_bucket(self):
        result = self.p._q_packets("packets", self.base, self.base + 120, 60, ())
        rx = next(s for s in result["series"] if s["name"] == "rx")
        # bucket0 = base, rx=10; bucket1 = base+60, rx=8
        self.assertEqual(len(rx["data"]), 2)
        vals = {d[0]: d[1] for d in rx["data"]}
        self.assertEqual(vals[self.base], 10)
        self.assertEqual(vals[self.base + 60], 8)

    def test_excludes_out_of_range(self):
        result = self.p._q_packets("packets", self.base, self.base + 120, 60, ())
        rx = next(s for s in result["series"] if s["name"] == "rx")
        buckets = [d[0] for d in rx["data"]]
        self.assertNotIn(self.base - 200, buckets)


# ── _q_packets_hourly tests ───────────────────────────────────────────────────

class TestQPacketsHourly(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)
        self.h0 = 1_700_010_000 - (1_700_010_000 % 3600)
        with self.p._msgdb_lock:
            for i in range(3):
                self.db.execute(
                    "INSERT INTO ts_packets_hourly"
                    " (hour_ts, rx_count, tx_count, flood_rx, flood_tx, direct_rx, direct_tx)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (self.h0 + i * 3600, 10 + i, 5 + i, 1, 1, 2, 2),
                )
            self.db.commit()

    def tearDown(self):
        _close(self.p, self.path)

    def test_returns_rows_in_time_order(self):
        result = self.p._q_packets_hourly(
            "hourly", self.h0, self.h0 + 3 * 3600, 3600, ()
        )
        rows = result["rows"]
        self.assertEqual(len(rows), 3)
        self.assertLessEqual(rows[0]["ts"], rows[1]["ts"])
        self.assertLessEqual(rows[1]["ts"], rows[2]["ts"])

    def test_row_shape(self):
        result = self.p._q_packets_hourly(
            "hourly", self.h0, self.h0 + 3600, 3600, ()
        )
        row = result["rows"][0]
        for key in ("ts", "rx", "tx", "flood_rx", "flood_tx", "direct_rx", "direct_tx"):
            self.assertIn(key, row)


# ── _q_msg_per_channel tests ──────────────────────────────────────────────────

class TestQMsgPerChannel(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)
        epoch_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(1_700_010_000))
        recv_str  = epoch_str
        with self.p._msgdb_lock:
            for chan in ("General", "General", "Alerts", "P", "P"):
                self.db.execute(
                    "INSERT INTO messages"
                    " (chan, sender, epoch, bad, body, direction, recv_ts)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (chan, "Alice", epoch_str, 0, "hi", "in", recv_str),
                )
            self.db.commit()

    def tearDown(self):
        _close(self.p, self.path)

    def test_excludes_private_messages(self):
        t_from = 1_700_009_000
        t_to   = 1_700_011_000
        result = self.p._q_msg_per_channel("channels", t_from, t_to, 3600, ())
        chans = {r["chan"] for r in result["rows"]}
        self.assertNotIn("P", chans)

    def test_counts_public_channels(self):
        t_from = 1_700_009_000
        t_to   = 1_700_011_000
        result = self.p._q_msg_per_channel("channels", t_from, t_to, 3600, ())
        rows = {r["chan"]: r["count"] for r in result["rows"]}
        self.assertEqual(rows.get("General"), 2)
        self.assertEqual(rows.get("Alerts"), 1)

    def test_ordered_by_count_desc(self):
        t_from = 1_700_009_000
        t_to   = 1_700_011_000
        result = self.p._q_msg_per_channel("channels", t_from, t_to, 3600, ())
        counts = [r["count"] for r in result["rows"]]
        self.assertEqual(counts, sorted(counts, reverse=True))


# ── _q_hop_histogram tests ────────────────────────────────────────────────────

class TestQHopHistogram(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)
        base = 1_700_010_000
        with self.p._msgdb_lock:
            for ts, hops in [
                (base,     1), (base,     1), (base,     2),
                (base,     3), (base - 999999, 9),  # out-of-range
            ]:
                self.db.execute(
                    "INSERT INTO ts_hops (ts, hops) VALUES (?,?)", (ts, hops)
                )
            self.db.commit()
        self.base = base

    def tearDown(self):
        _close(self.p, self.path)

    def test_groups_by_hops(self):
        result = self.p._q_hop_histogram(
            "hops", self.base - 60, self.base + 60, 120, ()
        )
        rows = {r["hops"]: r["count"] for r in result["rows"]}
        self.assertEqual(rows.get(1), 2)
        self.assertEqual(rows.get(2), 1)
        self.assertEqual(rows.get(3), 1)

    def test_excludes_out_of_range(self):
        result = self.p._q_hop_histogram(
            "hops", self.base - 60, self.base + 60, 120, ()
        )
        rows = {r["hops"]: r["count"] for r in result["rows"]}
        self.assertNotIn(9, rows)

    def test_sorted_by_hops(self):
        result = self.p._q_hop_histogram(
            "hops", self.base - 60, self.base + 60, 120, ()
        )
        hop_vals = [r["hops"] for r in result["rows"]]
        self.assertEqual(hop_vals, sorted(hop_vals))


# ── _q_top_relays tests ───────────────────────────────────────────────────────

class TestQTopRelays(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)
        now = int(time.time())
        with self.p._msgdb_lock:
            entries = [("aa", "NodeA", 500, now),
                       ("bb", "NodeB", 200, now),
                       ("cc", None,    50,  now),
                       ("dd", "NodeD", 10,  now)]
            for hk, name, cnt, ls in entries:
                self.db.execute(
                    "INSERT INTO ts_relay_keys (hex_key, name, last_seen, count)"
                    " VALUES (?,?,?,?)",
                    (hk, name, ls, cnt),
                )
            self.db.commit()

    def tearDown(self):
        _close(self.p, self.path)

    def test_ordered_by_count_desc(self):
        result = self.p._q_top_relays("relays", 0, int(time.time()) + 1, 3600, ())
        counts = [r["count"] for r in result["rows"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_respects_limit(self):
        result = self.p._q_top_relays("relays", 0, int(time.time()) + 1, 3600, (2,))
        self.assertEqual(len(result["rows"]), 2)

    def test_default_limit_20(self):
        # Insert 25 rows
        now = int(time.time())
        with self.p._msgdb_lock:
            for i in range(25):
                hk = f"{i:02x}"
                self.db.execute(
                    "INSERT OR IGNORE INTO ts_relay_keys (hex_key, name, last_seen, count)"
                    " VALUES (?,?,?,?)",
                    (hk, None, now, i),
                )
            self.db.commit()
        result = self.p._q_top_relays("relays", 0, now + 1, 3600, ())
        self.assertLessEqual(len(result["rows"]), 20)

    def test_row_shape(self):
        result = self.p._q_top_relays("relays", 0, int(time.time()) + 1, 3600, ())
        row = result["rows"][0]
        for key in ("hex", "name", "count", "last_seen"):
            self.assertIn(key, row)


# ── _ts_prune tests ───────────────────────────────────────────────────────────

class TestTsPrune(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)

    def tearDown(self):
        _close(self.p, self.path)

    def _count(self, tbl):
        with self.p._msgdb_lock:
            return self.db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]

    def test_prune_removes_old_ts_radio(self):
        now = int(time.time())
        old = now - 15 * 86400  # 15 days ago — older than 14-day cutoff
        new = now - 1
        with self.p._msgdb_lock:
            self.db.execute(
                "INSERT INTO ts_radio (ts, node_key, src) VALUES (?,?,?)",
                (old, "self", "rx")
            )
            self.db.execute(
                "INSERT INTO ts_radio (ts, node_key, src) VALUES (?,?,?)",
                (new, "self", "rx")
            )
            self.db.commit()
        self.p._ts_prune()
        self.assertEqual(self._count("ts_radio"), 1)

    def test_prune_keeps_recent_ts_radio(self):
        now = int(time.time())
        recent = now - 86400  # 1 day ago — within 14-day window
        with self.p._msgdb_lock:
            self.db.execute(
                "INSERT INTO ts_radio (ts, node_key, src) VALUES (?,?,?)",
                (recent, "self", "rx")
            )
            self.db.commit()
        self.p._ts_prune()
        self.assertEqual(self._count("ts_radio"), 1)

    def test_prune_removes_old_ts_packets_min(self):
        now = int(time.time())
        old = (now - 50 * 3600)   # 50 h ago — older than 48 h cutoff
        old -= (old % 60)
        new = now - 60
        new -= (new % 60)
        with self.p._msgdb_lock:
            self.db.execute(
                "INSERT OR REPLACE INTO ts_packets_min (ts, rx_count, tx_count) VALUES (?,?,?)",
                (old, 1, 1)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO ts_packets_min (ts, rx_count, tx_count) VALUES (?,?,?)",
                (new, 2, 2)
            )
            self.db.commit()
        self.p._ts_prune()
        self.assertEqual(self._count("ts_packets_min"), 1)

    def test_prune_removes_old_ts_hops(self):
        now = int(time.time())
        old = now - 15 * 86400
        new = now - 86400
        with self.p._msgdb_lock:
            self.db.execute("INSERT INTO ts_hops (ts, hops) VALUES (?,?)", (old, 2))
            self.db.execute("INSERT INTO ts_hops (ts, hops) VALUES (?,?)", (new, 3))
            self.db.commit()
        self.p._ts_prune()
        self.assertEqual(self._count("ts_hops"), 1)

    def test_prune_removes_old_low_count_relay_keys(self):
        now = int(time.time())
        old_ts = now - 31 * 86400
        with self.p._msgdb_lock:
            self.db.execute(
                "INSERT INTO ts_relay_keys (hex_key, name, last_seen, count)"
                " VALUES ('aa', NULL, ?, 5)",   # old AND low count -> deleted
                (old_ts,)
            )
            self.db.execute(
                "INSERT INTO ts_relay_keys (hex_key, name, last_seen, count)"
                " VALUES ('bb', NULL, ?, 200)",  # old BUT high count -> kept
                (old_ts,)
            )
            self.db.execute(
                "INSERT INTO ts_relay_keys (hex_key, name, last_seen, count)"
                " VALUES ('cc', NULL, ?, 5)",    # recent, low count -> kept
                (now,)
            )
            self.db.commit()
        self.p._ts_prune()
        with self.p._msgdb_lock:
            keys = {r[0] for r in self.db.execute(
                "SELECT hex_key FROM ts_relay_keys"
            ).fetchall()}
        self.assertNotIn("aa", keys)
        self.assertIn("bb", keys)
        self.assertIn("cc", keys)

    def test_prune_keeps_ts_packets_hourly_forever(self):
        # hourly table has no time-based prune; old rows survive
        now = int(time.time())
        old = (now - 365 * 86400)
        old -= (old % 3600)
        with self.p._msgdb_lock:
            self.db.execute(
                "INSERT OR REPLACE INTO ts_packets_hourly"
                " (hour_ts, rx_count, tx_count, flood_rx, flood_tx, direct_rx, direct_tx)"
                " VALUES (?,?,?,?,?,?,?)",
                (old, 5, 3, 0, 0, 0, 0)
            )
            self.db.commit()
        self.p._ts_prune()
        self.assertEqual(self._count("ts_packets_hourly"), 1)


# ── Dirty-flag / cache invalidation tests ────────────────────────────────────

class TestDirtyFlagInvalidation(unittest.TestCase):
    """Ingest marks panels dirty; a successful query clears the panel's flag."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)

    def tearDown(self):
        _close(self.p, self.path)

    def test_ingest_marks_rssi_dirty(self):
        self.p._ts_dirty_panels.clear()
        self.p._ts_ingest("rx", node_key="self", rssi=-70, snr=8.0)
        self.assertIn("rssi", self.p._ts_dirty_panels)

    def test_ingest_marks_snr_dirty(self):
        self.p._ts_dirty_panels.clear()
        self.p._ts_ingest("rx", node_key="self", rssi=-70, snr=8.0)
        self.assertIn("snr", self.p._ts_dirty_panels)

    def test_query_clears_panel_flag(self):
        self.p._ts_dirty_panels.add("rssi")
        now = int(time.time())
        self.p._handle_analytics_query("rssi", now - 3600, now, bucket_s=60)
        self.assertNotIn("rssi", self.p._ts_dirty_panels)

    def test_other_panels_remain_dirty_after_query(self):
        self.p._ts_dirty_panels.update({"rssi", "snr", "noise"})
        now = int(time.time())
        self.p._handle_analytics_query("rssi", now - 3600, now, bucket_s=60)
        self.assertIn("snr", self.p._ts_dirty_panels)
        self.assertIn("noise", self.p._ts_dirty_panels)


# ── _handle_analytics_query validation tests ─────────────────────────────────

class TestHandleAnalyticsQuery(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)

    def tearDown(self):
        _close(self.p, self.path)

    def test_unknown_panel_returns_error(self):
        now = int(time.time())
        result = self.p._handle_analytics_query("nonexistent", now - 3600, now)
        self.assertFalse(result["ok"])
        self.assertIn("unknown panel", result["error"])

    def test_range_over_30_days_rejected(self):
        now = int(time.time())
        result = self.p._handle_analytics_query("rssi", now - 31 * 86400, now)
        self.assertFalse(result["ok"])
        self.assertIn("range out of bounds", result["error"])

    def test_valid_query_returns_ok(self):
        now = int(time.time())
        result = self.p._handle_analytics_query("rssi", now - 3600, now, bucket_s=60)
        self.assertTrue(result["ok"])
        self.assertEqual(result["panel"], "rssi")
        self.assertIn("series", result)

    def test_default_bucket_chosen_when_none(self):
        now = int(time.time())
        result = self.p._handle_analytics_query("rssi", now - 3600, now, bucket_s=None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["bucket_s"], 60)   # 3h range -> 1 min bucket

    def test_nodes_tuple_is_accepted(self):
        now = int(time.time())
        result = self.p._handle_analytics_query(
            "rssi", now - 3600, now, bucket_s=60, nodes=["nodeA", "nodeB"]
        )
        self.assertTrue(result["ok"])


class TestRelayNameUpdateBehaviour(unittest.TestCase):
    """_ts_relay_observed: null-safe name update and counter advancement."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)

    def tearDown(self):
        _close(self.p, self.path)

    def _row(self, hex_key):
        with self.p._msgdb_lock:
            return self.db.execute(
                "SELECT name, last_seen, count FROM ts_relay_keys WHERE hex_key=?",
                (hex_key,),
            ).fetchone()

    def test_insert_with_none_name_creates_null_row(self):
        self.p._ts_relay_observed("aa", name=None)
        row = self._row("aa")
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])

    def test_insert_with_name_updates_null_to_name(self):
        self.p._ts_relay_observed("aa", name=None)
        self.p._ts_relay_observed("aa", name="Foo")
        row = self._row("aa")
        self.assertEqual(row[0], "Foo")

    def test_insert_none_does_not_blank_existing_name(self):
        self.p._ts_relay_observed("aa", name=None)
        self.p._ts_relay_observed("aa", name="Foo")
        self.p._ts_relay_observed("aa", name=None)
        row = self._row("aa")
        self.assertEqual(row[0], "Foo")

    def test_insert_new_name_overwrites_old_name(self):
        self.p._ts_relay_observed("aa", name=None)
        self.p._ts_relay_observed("aa", name="Foo")
        self.p._ts_relay_observed("aa", name=None)
        self.p._ts_relay_observed("aa", name="Bar")
        row = self._row("aa")
        self.assertEqual(row[0], "Bar")

    def test_count_advances_on_every_call(self):
        for _ in range(4):
            self.p._ts_relay_observed("aa", name=None)
        row = self._row("aa")
        self.assertEqual(row[2], 4)

    def test_last_seen_advances_on_every_call(self):
        self.p._ts_relay_observed("aa", name=None)
        first_ls = self._row("aa")[1]
        time.sleep(0.01)
        self.p._ts_relay_observed("aa", name=None)
        second_ls = self._row("aa")[1]
        self.assertGreaterEqual(second_ls, first_ls)


class TestPathTokenChars(unittest.TestCase):
    """_path_token_chars: hex-char hop-token width from device_info."""

    def setUp(self):
        self.p = plugin.BasePlugin()

    def test_default_is_one_byte(self):
        self.p._device_info = {}
        self.assertEqual(self.p._path_token_chars(), 2)

    def test_modes_map_to_widths(self):
        for mode, width in ((1, 2), (2, 4), (3, 6)):
            self.p._device_info = {"path_hash_mode": mode}
            self.assertEqual(self.p._path_token_chars(), width)

    def test_out_of_range_is_clamped(self):
        self.p._device_info = {"path_hash_mode": 7}
        self.assertEqual(self.p._path_token_chars(), 6)
        self.p._device_info = {"path_hash_mode": 0}
        self.assertEqual(self.p._path_token_chars(), 2)

    def test_garbage_falls_back_to_one_byte(self):
        self.p._device_info = {"path_hash_mode": "x"}
        self.assertEqual(self.p._path_token_chars(), 2)


class TestMultiByteRelayKeys(unittest.TestCase):
    """2-byte path-hash support: _ts_relay_observed, _resolve_relay_key,
    _q_top_relays width filter."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.p, self.db = _open_db(self.path)

    def tearDown(self):
        _close(self.p, self.path)

    def _count_rows(self):
        with self.p._msgdb_lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM ts_relay_keys").fetchone()[0]

    def test_observed_accepts_two_byte_key(self):
        self.p._ts_relay_observed("a1b2", name=None)
        self.assertEqual(self._count_rows(), 1)

    def test_observed_accepts_three_byte_key(self):
        self.p._ts_relay_observed("a1b2c3", name=None)
        self.assertEqual(self._count_rows(), 1)

    def test_observed_rejects_odd_and_oversize_keys(self):
        for bad in ("a", "abc", "a1b2c3d4", ""):
            self.p._ts_relay_observed(bad, name=None)
        self.assertEqual(self._count_rows(), 0)

    def test_resolve_two_byte_token(self):
        self.p._prefix_to_name = {"a1b2c3d4e5f6": "NodeA", "0102030405aa": "NodeB"}
        self.assertEqual(self.p._resolve_relay_key("a1b2"), "NodeA")

    def test_resolve_two_byte_token_ambiguous_returns_none(self):
        self.p._prefix_to_name = {"a1b2c3d4e5f6": "NodeA", "a1b2ffffffff": "NodeB"}
        self.assertIsNone(self.p._resolve_relay_key("a1b2"))

    def test_resolve_one_byte_token_still_works(self):
        self.p._prefix_to_name = {"a1b2c3d4e5f6": "NodeA", "0102030405aa": "NodeB"}
        self.assertEqual(self.p._resolve_relay_key("a1"), "NodeA")

    def test_top_relays_filters_by_current_token_width(self):
        now = int(time.time())
        with self.p._msgdb_lock:
            for hk, cnt in (("aa", 500), ("bb", 200), ("a1b2", 300), ("c3d4", 100)):
                self.db.execute(
                    "INSERT INTO ts_relay_keys (hex_key, name, last_seen, count)"
                    " VALUES (?,?,?,?)", (hk, None, now, cnt))
            self.db.commit()
        # Mode 1 (default): only the 2-char keys.
        self.p._device_info = {}
        rows = self.p._q_top_relays("relays", 0, now + 1, 3600, ())["rows"]
        self.assertEqual({r["hex"] for r in rows}, {"aa", "bb"})
        # Mode 2: only the 4-char keys.
        self.p._device_info = {"path_hash_mode": 2}
        rows = self.p._q_top_relays("relays", 0, now + 1, 3600, ())["rows"]
        self.assertEqual({r["hex"] for r in rows}, {"a1b2", "c3d4"})


if __name__ == "__main__":
    unittest.main()
