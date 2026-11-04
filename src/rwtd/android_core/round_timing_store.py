"""Durable round summaries, separate from rotating diagnostic logs."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path


def full_version(name):
    found = re.search(r'(?i)(?<![A-Za-z0-9])v(\d+(?:\.\d+)*)\b', name or '')
    return found[1] if found else None


def version_parts(value):
    text = str(value or '').strip().lstrip('vV')
    if not re.fullmatch(r'\d+(?:\.\d+)*', text):
        raise ValueError('Version must look like 1.6 or 1.6.2')
    return tuple(int(part) for part in text.split('.'))


def display_version(name):
    value = full_version(name)
    return '.'.join(value.split('.')[:2]) if value else None


def _compare(actual, bound):
    if len(bound)<=2:
        actual = actual[:len(bound)]
    length = max(len(actual),len(bound))
    left, right = actual+(0,)*(length-len(actual)), bound+(0,)*(length-len(bound))
    return (left>right)-(left<right)


def version_matches(actual, version=None, from_version=None, to_version=None):
    if not (version or from_version or to_version):
        return True
    if not actual:
        return False
    parts = version_parts(actual)
    if version:
        wanted = version_parts(version)
        return _compare(parts,wanted)==0
    return ((not from_version or _compare(parts,version_parts(from_version))>=0)
            and (not to_version or _compare(parts,version_parts(to_version))<=0))


class RoundTimingStore:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        connection.execute('''CREATE TABLE IF NOT EXISTS round_timings (
            record_id TEXT PRIMARY KEY, source TEXT NOT NULL,
            session TEXT NOT NULL, runtime_id TEXT NOT NULL, scope TEXT NOT NULL,
            canvas_id TEXT, canvas TEXT NOT NULL, canvas_version TEXT, revision TEXT,
            stage INTEGER, round INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
            seconds REAL NOT NULL CHECK(seconds >= 0), outcome TEXT NOT NULL,
            details_json TEXT NOT NULL
        )''')
        connection.execute('CREATE INDEX IF NOT EXISTS round_timing_stage_end ON round_timings(stage, end)')
        connection.commit()
        return connection

    def append(self, row):
        """One transaction per completed round; repeated publication is idempotent."""
        row = dict(row)
        source = row.get('source', 'runtime')
        key = (source, row['runtime_id'], row['round']) if source == 'runtime' else (
            source, row.get('canvas_id') or row['canvas'], row['round'], row['start'], row['end'])
        record_id = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
        values = (record_id, source, str(row['session']), row['runtime_id'], row.get('scope', 'farm'),
                  row.get('canvas_id'), row['canvas'], display_version(row['canvas']), row.get('revision'),
                  row.get('stage'), row['round'], row['start'], row['end'], row['seconds'],
                  row.get('outcome', 'UNKNOWN'), json.dumps(row.get('details', {}), ensure_ascii=False))
        connection = self._connect()
        try:
            with connection:
                # Historical log timings overlap live records but have less
                # precise boundaries. Never import them over a live interval.
                if source == 'log' and connection.execute(
                    '''SELECT 1 FROM round_timings WHERE source='runtime'
                       AND COALESCE(canvas_id,canvas)=? AND end>? AND start<? LIMIT 1''',
                    (row.get('canvas_id') or row['canvas'], row['start'], row['end'])).fetchone():
                    return False
                cursor = connection.execute('INSERT INTO round_timings VALUES (' +
                                            ','.join('?' for _ in values) +
                                            ') ON CONFLICT(record_id) DO NOTHING', values)
                return cursor.rowcount == 1
        finally:
            connection.close()

    def rows(self, stage, *, before=None, version=None, from_version=None, to_version=None,
             scope='farm', canvas_id=None):
        if scope not in {'farm','activity'}:
            raise ValueError('Invalid round timing scope')
        if not self.path.exists():
            return []
        connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            query = 'SELECT * FROM round_timings WHERE scope=?'
            params = [scope]
            if scope=='farm' and stage is not None:
                query += ' AND stage=?'
                params.append(stage)
            if canvas_id:
                query += ' AND COALESCE(canvas_id,canvas)=?'
                params.append(canvas_id)
            if before:
                query += ' AND end<=?'
                params.append(before)
            output = []
            for saved in connection.execute(query+' ORDER BY end,record_id', params):
                row = dict(saved)
                fine = full_version(row['canvas']) or (row['canvas_version'] or '').lstrip('vV') or None
                if not version_matches(fine,version,from_version,to_version):
                    continue
                row['canvas_fine_version'] = fine
                row['canvas_version'] = '.'.join(fine.split('.')[:2]) if fine else None
                output.append(row)
            return output
        finally:
            connection.close()
