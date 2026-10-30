"""Real SQLite/GZIP regression tests with synthetic, isolated metadata only."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

_BOOT=tempfile.TemporaryDirectory(prefix="v16-audit-compression-import-")
with patch.dict(os.environ,{"LOCAL_AI_ROOT":_BOOT.name}):
    from app import audit

class AuditCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="v16-audit-gzip-")
        self.root=Path(self.temp.name)
        self.stack=ExitStack()
        self.stack.enter_context(patch.object(audit,"ROOT",self.root))
        self.stack.enter_context(patch.object(audit,"HOT_KEEP",4))
        self.stack.enter_context(patch.object(audit,"ARCHIVE_BATCH",8))
        self.now=time.time()
    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()
    def seed(self,count=35,stamp=None,compress=True):
        # The established 12-field integration fixture remains supported.
        with audit.LOCK,audit._db() as db:
            for i in range(count):
                t=(self.now-1000+i if stamp is None else stamp+i)
                row=(f"synthetic-{i:06d}",t,datetime.fromtimestamp(t,timezone.utc).isoformat(),
                     "model-"+str(i%3),"local","tool-"+str(i%2),"read","success" if i%4 else "error",
                     .125,"file#synthetic.md","","")
                db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",row)
        if compress:audit.cleanup()
    def all_ids(self,page_size=7,**filters):
        result=[];page=1
        while True:
            response=audit.listing(page=page,page_size=page_size,**filters)
            result.extend(x["id"] for x in response["items"])
            if page*page_size>=response["total"]:break
            page+=1
        return result
    def test_real_batch_compression_and_stable_pagination(self):
        self.seed(45)
        response=audit.listing()
        self.assertEqual(response["total"],45)
        self.assertEqual(response["compression"]["hot_records"],5) # odd tail kept, never one-record gzip
        self.assertGreater(response["compression"]["segments"],0)
        self.assertLess(response["compression"]["compressed_bytes"],response["compression"]["source_bytes"])
        expected=[f"synthetic-{i:06d}" for i in reversed(range(45))]
        self.assertEqual(self.all_ids(),expected)
        with audit._db() as db:
            sizes=[r[0] for r in db.execute("SELECT row_count FROM archives")]
            self.assertTrue(all(2<=size<=8 for size in sizes))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_index").fetchone()[0],45)
    def test_combined_filters_and_date_ranges(self):
        self.seed(80)
        start=self.now-1000+12;end=self.now-1000+65
        wanted=[f"synthetic-{i:06d}" for i in reversed(range(12,66)) if i%3==1 and i%2==0 and i%4==0]
        self.assertEqual(self.all_ids(model="model-1",tool="tool-0",status="error",
                         since=datetime.fromtimestamp(start,timezone.utc).isoformat(),
                         until=datetime.fromtimestamp(end,timezone.utc).isoformat()),wanted)
        self.assertEqual(audit.listing(model="nonexistent")["total"],0)
    def test_only_requested_page_segments_are_decompressed(self):
        self.seed(200)
        with patch.object(audit,"_decode",wraps=audit._decode) as decoder:
            page=audit.listing(page=10,page_size=3)
            self.assertEqual(len(page["items"]),3)
            self.assertLessEqual(decoder.call_count,2)
        with patch.object(audit,"_decode",side_effect=AssertionError("No matches must not decompress")):
            self.assertEqual(audit.listing(model="missing")["total"],0)
    def test_archived_call_id_survives_reopen_and_rejects_replay(self):
        self.seed(20)
        with audit._db() as db:
            self.assertIsNotNone(db.execute("SELECT archive_id FROM audit_index WHERE id='synthetic-000000'").fetchone()[0])
        with self.assertRaisesRegex(ValueError,"调用编号"):
            audit.start("memory_read",{},call_id="synthetic-000000")
        self.assertEqual(audit.listing()["total"],20)
    def test_running_calls_stay_hot_until_finished(self):
        ident=audit.start("memory_read",{},call_id="pending")
        self.seed(24)
        with audit._db() as db:
            self.assertIsNone(db.execute("SELECT archive_id FROM audit_index WHERE id=?",(ident,)).fetchone()[0])
        audit.finish(ident,"memory_read",{})
        self.assertEqual(audit.listing(status="running")["total"],0)
        self.assertEqual(audit.listing()["total"],25)
    def test_ttl_partial_block_boundary_inclusive(self):
        cutoff=self.now-audit.RETENTION_DAYS*86400
        # Initially create a valid mixed-time archive, then advance clock over half.
        with patch.object(audit.time,"time",return_value=self.now-100):
            self.seed(12,stamp=cutoff-4)
        with patch.object(audit.time,"time",return_value=self.now):
            audit.cleanup()
            ids=self.all_ids()
        self.assertEqual(set(ids),{f"synthetic-{i:06d}" for i in range(4,12)})
        with audit._db() as db:
            for segment in db.execute("SELECT * FROM archives"):
                self.assertTrue(all(row["timestamp"]>=cutoff for row in audit._decode(segment)))
    def test_ttl_whole_expired_blocks_without_full_decompression(self):
        before=self.now-400*86400
        with patch.object(audit.time,"time",return_value=before+100):
            self.seed(40,stamp=before)
        with patch.object(audit,"_decode",side_effect=AssertionError("Fully expired blocks need no inflate")):
            self.assertEqual(audit.listing()["total"],0)
    def test_corrupt_gzip_is_visible_and_records_retained(self):
        self.seed(20)
        with audit._db() as db:
            db.execute("UPDATE archives SET payload=? WHERE id=(SELECT MIN(id) FROM archives)",(b"broken gzip",))
        with self.assertRaises(audit.AuditCorruptionError):
            self.all_ids(page_size=100)
        with audit._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_index").fetchone()[0],20)
    def test_hash_or_index_mismatch_is_visible(self):
        self.seed(20)
        with audit._db() as db:db.execute("UPDATE archives SET sha256='wrong'")
        with self.assertRaises(audit.AuditCorruptionError):self.all_ids(page_size=100)
    def test_gzip_inflation_is_bounded(self):
        self.seed(20)
        malicious=gzip.compress(b"x"*(audit.MAX_SEGMENT_RAW_BYTES+100),mtime=0)
        with audit._db() as db:
            db.execute("UPDATE archives SET payload=?,raw_bytes=100",(malicious,))
        with self.assertRaises(audit.AuditCorruptionError):self.all_ids(page_size=100)
    def test_equal_timestamps_have_deterministic_pages(self):
        self.seed(20,compress=False)
        with audit._db() as db:db.execute("UPDATE events SET timestamp=?",(self.now-10,))
        audit.cleanup()
        ids=self.all_ids(page_size=3)
        self.assertEqual(ids,sorted(ids,reverse=True))
        self.assertEqual(len(ids),len(set(ids)))
    def test_small_forced_batch_and_hot_table_fixture_update(self):
        self.seed(10,compress=False)
        audit.cleanup()
        with audit._db() as db:
            self.assertEqual(db.execute("SELECT row_count FROM archives").fetchone()[0],6)
            db.execute("UPDATE events SET status='cancelled' WHERE id='synthetic-000009'")
        self.assertEqual(audit.listing(status="cancelled")["items"][0]["id"],"synthetic-000009")
    def test_concurrent_start_finish_and_archive(self):
        def work(i):
            ident=audit.start("memory_read",{},scope={"model":"parallel"},call_id=f"parallel-{i}")
            if i%5==0:audit.cleanup()
            audit.finish(ident,"memory_read",{},duration=.01)
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(work,range(64)))
        ids=self.all_ids(model="parallel",status="success")
        self.assertEqual(len(ids),64);self.assertEqual(len(set(ids)),64)
        self.assertGreater(audit.listing()["compression"]["archived_records"],0)
    def test_late_outcome_update_preserves_compressed_index(self):
        self.seed(25)
        audit.finish("synthetic-000000","synthetic",{},status="unknown",duration=2.5)
        response=audit.listing(status="unknown")
        self.assertEqual(response["total"],1)
        self.assertEqual(response["items"][0]["id"],"synthetic-000000")
        self.assertEqual(response["items"][0]["duration"],2.5)
        self.assertEqual(len(self.all_ids()),25)
        with self.assertRaises(ValueError) as rejected:
            audit.start("memory_read",{},call_id="synthetic-000000")
        self.assertIs(rejected.exception.executed,False)
    def test_privacy_rules_preserved_after_compression(self):
        with patch.object(audit,"HOT_KEEP",0),patch.object(audit,"ARCHIVE_BATCH",2):
            audit.record("not-a-tool",{"action":"PRIVATE_BODY","path":"/synthetic/private_name.PRIVATE_BODY"})
            audit.record("memory_read",{"text":"PRIVATE_BODY"})
        payload=json.dumps(audit.listing(),ensure_ascii=False)
        self.assertNotIn("PRIVATE_BODY",payload);self.assertNotIn("private_name",payload)
        with audit._db() as db:
            for segment in db.execute("SELECT * FROM archives"):
                raw=gzip.decompress(segment["payload"]).decode()
                self.assertNotIn("PRIVATE_BODY",raw)
    def test_capacity_keeps_database_and_actual_journal_below_bound(self):
        # Small real cap exercises eviction without creating a 1GB test artifact.
        with patch.object(audit,"MAX_BYTES",1024*1024),patch.object(audit,"HOT_KEEP",4),patch.object(audit,"ARCHIVE_BATCH",64):
            maximum=0
            rng=random.Random(17)
            for batch in range(45):
                with audit._db() as db:
                    for i in range(64):
                        ident=f"cap-{batch}-{i}"
                        marker=hashlib.sha256(str(rng.random()).encode()).hexdigest()
                        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ident,self.now-10000+batch*64+i,"synthetic-time","synthetic-model","local","memory_read","execute","success",.1,marker,"",""))
                    # Include an actual live rollback journal in the observation.
                    maximum=max(maximum,sum(p.stat().st_size for p in (self.root/"audit").glob("*") if p.is_file()))
                audit.cleanup()
                maximum=max(maximum,sum(p.stat().st_size for p in (self.root/"audit").glob("*") if p.is_file()))
            info=audit.listing()
            self.assertLess(maximum,audit.MAX_BYTES)
            self.assertLess(info["bytes"],audit.MAX_BYTES)
            self.assertLess(info["total"],45*64)
            self.assertEqual(info["items"][0]["id"],"cap-44-63")

if __name__=="__main__":unittest.main()
