#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import pickle
import lzma
import sqlite3

from .utils import env


class SignatureDB:
    """Base class for signature DB using sqlite"""

    def __init__(self):
        self.db_file = os.path.join(env.exec_dir, self._db_name)
        self._conn = None
        self._cache = []

    def _get_conn(self):
        # there is a possibility that the _conn is copied with a process
        # and we would better have a fresh conn
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_file, timeout=60)
            self._conn.execute(self._db_structure)
            self._conn.commit()
        if self._cache:
            self._conn.executemany(self._write_query, self._cache)
            self._cache = []
            self._conn.commit()
        return self._conn

    def _write(self, record):
        self._cache.append(record)
        if len(self._cache) > 1000:
            # this will tricky the action to clear cache
            self._get_conn()

    conn = property(_get_conn)

    def commit(self):
        # getting conn will commit all cached records
        self._get_conn()

    def close(self):
        self.conn.close()


class StepSignatures(SignatureDB):
    """Step signature that stores runtime signatures of substeps"""

    _db_name = "step_signatures.db"
    _db_structure = """CREATE TABLE IF NOT EXISTS steps (
        step_id text PRIMARY KEY,
        signature BLOB
    )"""
    _write_query = "INSERT OR REPLACE INTO steps VALUES (?, ?)"

    def __init__(self):
        super(StepSignatures, self).__init__()

    def get(self, step_id: str):
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT signature FROM steps WHERE step_id=? ", (step_id,))
            res = cur.fetchone()
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to get step signature for step {step_id}: {e}")
            return None
        if res:
            try:
                return pickle.loads(lzma.decompress(res[0]))
            except Exception as e:
                env.logger.warning(f"Failed to load signature for step {step_id}: {e}")
                return None
        else:
            return None

    def set(self, step_id: str, signature: dict):
        try:
            self._write((step_id, lzma.compress(pickle.dumps(signature))))
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to set step signature for step {step_id}: {e}")

    def _num_records(self, cur):
        try:
            cur.execute("SELECT COUNT(rowid) FROM steps")
            return cur.fetchone()[0]
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to number of records for steps: {e}")
            return 0

    def remove_many(self, steps: list):
        try:
            cur = self.conn.cursor()
            cnt = self._num_records(cur)
            cur.executemany("DELETE FROM steps WHERE step_id=?", [(x,) for x in steps])
            self.conn.commit()
            return cnt - self._num_records(cur)
        except sqlite3.DatabaseError as e:
            env.logger.warning(
                f"Failed to remove signature for {len(steps)} substeps: {e}"
            )
            return 0

    def clear(self):
        try:
            self.conn.execute("DELETE FROM steps")
            self.conn.commit()
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to clear step signature database: {e}")


class WorkflowSignatures(SignatureDB):
    """Workflow signature to store runtime information for workflows"""

    _db_name = "workflow_signatures.db"
    _db_structure = """CREATE TABLE IF NOT EXISTS workflows (
            master_id text,
            entry_type text,
            id text,
            item text
    )"""
    _write_query = "INSERT INTO workflows VALUES (?, ?, ?, ?)"

    def __init__(self):
        super(WorkflowSignatures, self).__init__()

    def write(self, entry_type: str, id: str, item: str):
        try:
            self._write((env.config["master_id"], entry_type, id, item))
        except sqlite3.DatabaseError as e:
            env.logger.warning(
                f"Failed to write workflow signature of type {entry_type} and id {id}: {e}"
            )
            return None

    def records(self, workflow_id):
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT entry_type, id, item FROM workflows WHERE master_id = ?",
                (workflow_id,),
            )
            return cur.fetchall() if cur else []
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to get records of workflow {workflow_id}: {e}")
            return []

    def workflows(self):
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT DISTINCT master_id FROM workflows")
            return [x[0] for x in cur.fetchall()]
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to get workflows from signature database: {e}")
            return []

    def tasks(self):
        try:
            cur = self.conn.cursor()
            cur.execute('SELECT DISTINCT id FROM workflows WHERE entry_type = "task"')
            return [x[0] for x in cur.fetchall()]
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to get tasks from signature database: {e}")
            return []

    def files(self):
        """Listing files related to workflows related to current directory"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                'SELECT id, item FROM workflows WHERE entry_type = "tracked_files"'
            )
            return [(x[0], eval(x[1])) for x in cur.fetchall()]
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to get files from signature database: {e}")
            return []

    def placeholders(self, workflow_id=None):
        try:
            cur = self.conn.cursor()
            if workflow_id is None:
                cur.execute(
                    'SELECT item FROM workflows WHERE entry_type = "placeholder"'
                )
            else:
                cur.execute(
                    f'SELECT item FROM workflows WHERE entry_type = "placeholder" AND master_id = "{workflow_id}"'
                )
            return [x[0] for x in cur.fetchall()]
        except sqlite3.DatabaseError as e:
            env.logger.warning(
                f"Failed to get placeholders from signature database: {e}"
            )
            return []

    def clear(self):
        try:
            self.conn.execute(
                f"DELETE FROM workflows WHERE master_id = ?", (env.config["master_id"],)
            )
            self.conn.commit()
        except sqlite3.DatabaseError as e:
            env.logger.warning(f"Failed to clear workflow database: {e}")
            return []
