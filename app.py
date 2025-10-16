import os
import sqlite3
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional, Any, Iterable
from flask import Flask, jsonify, request, send_from_directory, g


APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Allow overriding DB path for cloud deploys with persistent disks (SQLite fallback)
DB_PATH = os.environ.get('DB_PATH', os.path.join(APP_DIR, 'data.db'))
# Prefer Postgres if DATABASE_URL is provided (e.g., Supabase)
DATABASE_URL = os.environ.get('DATABASE_URL')
DB_MODE = 'pg' if DATABASE_URL and DATABASE_URL.startswith(('postgres://', 'postgresql://')) else 'sqlite'


def _ensure_db_path_and_migrate():
    """Ensure target DB directory exists and migrate legacy DB if needed.

    If DB_PATH points outside the app dir (e.g., /data/data.db) and that file
    doesn't exist yet, but an existing legacy DB exists at APP_DIR/data.db,
    copy it so existing local data is preserved when first attaching a disk.
    """
    try:
        target_dir = os.path.dirname(DB_PATH)
        if target_dir and not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)
        legacy = os.path.join(APP_DIR, 'data.db')
        if not os.path.exists(DB_PATH) and os.path.exists(legacy) and os.path.abspath(legacy) != os.path.abspath(DB_PATH):
            import shutil
            shutil.copy2(legacy, DB_PATH)
    except Exception as e:
        # Non-fatal; DB will be created fresh if needed
        pass


def _add_ssl_and_ipv4_to_url(url: str) -> tuple[str, Optional[str]]:
    """Ensure sslmode=require in connstring and resolve IPv4 hostaddr.

    Returns (possibly modified url, ipv4_hostaddr or None).
    """
    parts = urlsplit(url)
    # Ensure sslmode=require
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.setdefault('sslmode', 'require')
    new_query = urlencode(q)
    new_url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    # Resolve IPv4 address (best-effort)
    try:
        host = parts.hostname
        port = parts.port or 5432
        ipv4 = None
        if host:
            infos = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            if infos:
                ipv4 = infos[0][4][0]
        return new_url, ipv4
    except Exception:
        return new_url, None


def _set_url_port(url: str, new_port: int) -> str:
    parts = urlsplit(url)
    username = parts.username or ''
    password = parts.password or ''
    hostname = parts.hostname or ''
    # Recompose credentials safely
    auth = ''
    if username:
        auth = username
        if password:
            auth += f":{password}"
        auth += '@'
    netloc = f"{auth}{hostname}:{new_port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _pg_connect(dsn: str, row_factory=None):
    import psycopg
    from psycopg.rows import dict_row
    url, ipv4 = _add_ssl_and_ipv4_to_url(dsn)
    kwargs = {}
    if row_factory:
        kwargs['row_factory'] = row_factory
    else:
        kwargs['row_factory'] = dict_row
    # First attempt: as-is, prefer IPv4 if available
    try:
        if ipv4:
            return psycopg.connect(url, hostaddr=ipv4, **kwargs)
        return psycopg.connect(url, **kwargs)
    except Exception as e:
        msg = repr(e)
        # Fallback: if Supabase on 5432, try pooled port 6543
        try:
            parts = urlsplit(url)
            host = parts.hostname or ''
            port = parts.port or 5432
            if host.endswith('.supabase.co') and port != 6543:
                url2 = _set_url_port(url, 6543)
                url2, ipv4b = _add_ssl_and_ipv4_to_url(url2)
                if ipv4b:
                    return psycopg.connect(url2, hostaddr=ipv4b, **kwargs)
                return psycopg.connect(url2, **kwargs)
        except Exception:
            pass
        raise


def create_app():
    app = Flask(__name__, static_folder='static', template_folder='templates')

    # Make sure DB dir exists and migrate any legacy file on first start (SQLite only)
    if DB_MODE == 'sqlite':
        _ensure_db_path_and_migrate()

    # DB helpers (works for SQLite and Postgres/psycopg)
    def _get_conn():
        if DB_MODE == 'sqlite':
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            return conn
        else:
            # Lazy import via helper and fallback to pooler/IPv4
            return _pg_connect(DATABASE_URL)

    def _adapt_sql(sql: str) -> str:
        # Our SQL is written with SQLite-style '?' placeholders.
        # For Postgres (psycopg), convert to '%s'.
        if DB_MODE == 'pg':
            # Replace only placeholder tokens. Since our SQL does not contain literal '?', a global replace is fine.
            return sql.replace('?', '%s')
        return sql

    def db_execute(sql: str, params: Iterable[Any] = ()):  # returns cursor
        sql2 = _adapt_sql(sql)
        if DB_MODE == 'sqlite':
            cur = g.db.execute(sql2, tuple(params))
            return cur
        else:
            cur = g.db.cursor()
            cur.execute(sql2, tuple(params))
            return cur

    def db_query_all(sql: str, params: Iterable[Any] = ()):  # returns list of rows (dict-like)
        cur = db_execute(sql, params)
        return cur.fetchall()

    def db_query_one(sql: str, params: Iterable[Any] = ()):  # returns single row or None
        cur = db_execute(sql, params)
        return cur.fetchone()

    @app.before_request
    def before_request():
        g.db = _get_conn()

    @app.teardown_request
    def teardown_request(exception):
        db = getattr(g, 'db', None)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    with app.app_context():
        init_db()

    @app.route('/')
    def index():
        return send_from_directory(app.static_folder, 'index.html')

    @app.route('/api/roommates', methods=['GET'])
    def list_roommates():
        rows = db_query_all('SELECT id, name, color FROM roommates ORDER BY id')
        return jsonify([dict(r) for r in rows])

    @app.route('/api/roommates', methods=['POST'])
    def add_roommate():
        data = request.get_json(force=True)
        name = (data.get('name') or '').strip()
        color = (data.get('color') or '#3778C2').strip()
        if not name:
            return jsonify({'error': 'name is required'}), 400
        try:
            cur = db_execute('INSERT INTO roommates(name, color) VALUES (?, ?)', (name, color))
            # lastrowid for Postgres: RETURNING is cleaner; fallback by re-query
            rid = None
            if DB_MODE == 'sqlite':
                rid = cur.lastrowid
            else:
                # Postgres: get id via name (unique)
                g.db.commit()
                row2 = db_query_one('SELECT id FROM roommates WHERE name=?', (name,))
                rid = row2['id']
            g.db.commit()
            row = db_query_one('SELECT id, name, color FROM roommates WHERE id=?', (rid,))
            return jsonify(dict(row)), 201
        except Exception as e:
            # Unique violation handling for both backends
            msg = repr(e).lower()
            if 'unique' in msg or 'duplicate key' in msg or '23505' in msg:
                return jsonify({'error': 'Roommate name must be unique'}), 409
            raise

    @app.route('/api/roommates/<int:rid>', methods=['PUT'])
    def update_roommate(rid):
        data = request.get_json(force=True)
        name = data.get('name')
        color = data.get('color')
        row = db_query_one('SELECT id FROM roommates WHERE id=?', (rid,))
        if not row:
            return jsonify({'error': 'not found'}), 404
        if name is not None:
            name = name.strip()
            if not name:
                return jsonify({'error': 'name cannot be empty'}), 400
            try:
                db_execute('UPDATE roommates SET name=? WHERE id=?', (name, rid))
                g.db.commit()
            except Exception as e:
                msg = repr(e).lower()
                if 'unique' in msg or 'duplicate key' in msg or '23505' in msg:
                    return jsonify({'error': 'Roommate name must be unique'}), 409
                raise
        if color is not None:
            color = color.strip()
            if not color:
                return jsonify({'error': 'color cannot be empty'}), 400
            db_execute('UPDATE roommates SET color=? WHERE id=?', (color, rid))
            g.db.commit()
        row = db_query_one('SELECT id, name, color FROM roommates WHERE id=?', (rid,))
        return jsonify(dict(row))

    def parse_iso(ts: str):
        # Accepts RFC3339/ISO-8601 with or without timezone.
        # Returns a normalized string; timezone inputs are normalized to UTC 'Z'.
        if not isinstance(ts, str) or not ts.strip():
            raise ValueError('invalid timestamp')
        s = ts.strip().replace(' ', 'T')
        s = s[:-1] + '+00:00' if s.endswith('Z') else s
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # Fallback to a few strict patterns
            fmts = [
                '%Y-%m-%dT%H:%M',
                '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%dT%H:%M:%S.%f',
                '%Y-%m-%dT%H:%M%z',
                '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%S.%f%z',
            ]
            for fmt in fmts:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError('invalid ISO datetime')
        # Normalize: timezone-aware -> UTC Z; naive -> keep as local naive string with seconds
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
            return dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        return dt.replace(microsecond=0).isoformat()

    def event_row_to_dict(row):
        return {
            'id': row['id'],
            'title': row['title'],
            'start': row['start'],
            'end': row['end'],
            'location': row['location'],
            'notes': row['notes'],
            'roommate': {
                'id': row['roommate_id'],
                'name': row['roommate_name'],
                'color': row['roommate_color'],
            },
        }

    def query_events(start: Optional[str], end: Optional[str]):
        base = (
            'SELECT e.id, e.title, e.start, e."end" as "end", e.location, e.notes, '
            'e.roommate_id, r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id'
        )
        args = []
        where = []
        if start:
            where.append('e."end" > ?')
            args.append(start)
        if end:
            where.append('e.start < ?')
            args.append(end)
        sql = base + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY e.start'
        rows = db_query_all(sql, args)
        return [event_row_to_dict(r) for r in rows]

    def find_conflicts(start: str, end: str, exclude_event_id: Optional[int] = None):
        args = [start, end]
        sql = (
            'SELECT e.id, e.title, e.start, e."end" as "end", e.location, e.notes, '
            'e.roommate_id, r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id '
            'WHERE e.start < ? AND e."end" > ?'
        )
        if exclude_event_id is not None:
            sql += ' AND e.id != ?'
            args.append(exclude_event_id)
        rows = db_query_all(sql, args)
        return [event_row_to_dict(r) for r in rows]

    @app.route('/api/events', methods=['GET'])
    def list_events():
        start = request.args.get('start')
        end = request.args.get('end')
        try:
            start = parse_iso(start) if start else None
            end = parse_iso(end) if end else None
        except ValueError:
            return jsonify({'error': 'invalid start or end ISO datetime'}), 400
        return jsonify(query_events(start, end))

    @app.route('/api/events', methods=['POST'])
    def create_event():
        data = request.get_json(force=True)
        roommate_id = data.get('roommate_id')
        title = (data.get('title') or 'Interview').strip()
        start = data.get('start')
        end = data.get('end')
        location = (data.get('location') or '').strip()
        notes = (data.get('notes') or '').strip()
        reject_on_conflict = bool(data.get('rejectOnConflict'))

        if not roommate_id:
            return jsonify({'error': 'roommate_id is required'}), 400
        rm = db_query_one('SELECT id FROM roommates WHERE id=?', (roommate_id,))
        if not rm:
            return jsonify({'error': 'invalid roommate_id'}), 400
        try:
            start = parse_iso(start)
            end = parse_iso(end)
        except ValueError:
            return jsonify({'error': 'invalid start or end ISO datetime'}), 400
        if start >= end:
            return jsonify({'error': 'end must be after start'}), 400

        conflicts = find_conflicts(end, start)  # Note we pass (new_end, new_start)
        if conflicts and reject_on_conflict:
            return jsonify({'error': 'conflict', 'conflicts': conflicts}), 409

        if DB_MODE == 'pg':
            cur = db_execute(
                'INSERT INTO events(roommate_id, title, start, "end", location, notes) VALUES (?, ?, ?, ?, ?, ?) RETURNING id',
                (roommate_id, title, start, end, location, notes)
            )
            eid = cur.fetchone()['id']
            g.db.commit()
        else:
            cur = db_execute(
                'INSERT INTO events(roommate_id, title, start, "end", location, notes) VALUES (?, ?, ?, ?, ?, ?)',
                (roommate_id, title, start, end, location, notes)
            )
            g.db.commit()
            eid = cur.lastrowid
        row = db_query_one(
            'SELECT e.id, e.title, e.start, e."end" as "end", e.location, e.notes, e.roommate_id, '
            'r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id WHERE e.id=?', (eid,)
        )
        return jsonify({'event': event_row_to_dict(row), 'conflicts': conflicts}), 201

    @app.route('/api/events/<int:eid>', methods=['PUT'])
    def update_event(eid):
        row = db_query_one('SELECT id FROM events WHERE id=?', (eid,))
        if not row:
            return jsonify({'error': 'not found'}), 404
        data = request.get_json(force=True)
        # Allow partial update
        fields = []
        args = []
        if 'roommate_id' in data:
            roommate_id = data['roommate_id']
            rm = g.db.execute('SELECT id FROM roommates WHERE id=?', (roommate_id,)).fetchone()
            if not rm:
                return jsonify({'error': 'invalid roommate_id'}), 400
            fields.append('roommate_id=?')
            args.append(roommate_id)
        if 'title' in data:
            fields.append('title=?')
            args.append((data.get('title') or 'Interview').strip())
        if 'start' in data:
            try:
                args.append(parse_iso(data['start']))
            except ValueError:
                return jsonify({'error': 'invalid start'}), 400
            fields.append('start=?')
        if 'end' in data:
            try:
                args.append(parse_iso(data['end']))
            except ValueError:
                return jsonify({'error': 'invalid end'}), 400
            fields.append('end=?')
        if 'location' in data:
            fields.append('location=?')
            args.append((data.get('location') or '').strip())
        if 'notes' in data:
            fields.append('notes=?')
            args.append((data.get('notes') or '').strip())

        if not fields:
            return jsonify({'error': 'no fields to update'}), 400

        # Fetch existing to validate time order if both present after update
        existing = db_query_one('SELECT start, "end" as "end" FROM events WHERE id=?', (eid,))
        new_start = None
        new_end = None
        for i, f in enumerate(fields):
            if f == 'start=?':
                new_start = args[i]
            elif f == 'end=?':
                new_end = args[i]
        if new_start is None:
            new_start = existing['start']
        if new_end is None:
            new_end = existing['end']
        if new_start >= new_end:
            return jsonify({'error': 'end must be after start'}), 400

        # Quote reserved column name "end" in update fields
        fields_q = [f.replace('end=?', '"end"=?') for f in fields]
        db_execute(f'UPDATE events SET {", ".join(fields_q)} WHERE id=?', (*args, eid))
        g.db.commit()
        # Conflicts after update (exclude this event)
        conflicts = find_conflicts(new_end, new_start, exclude_event_id=eid)
        row = db_query_one(
            'SELECT e.id, e.title, e.start, e."end" as "end", e.location, e.notes, e.roommate_id, '
            'r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id WHERE e.id=?', (eid,)
        )
        return jsonify({'event': event_row_to_dict(row), 'conflicts': conflicts})

    @app.route('/api/events/<int:eid>', methods=['DELETE'])
    def delete_event(eid):
        cur = db_execute('DELETE FROM events WHERE id=?', (eid,))
        g.db.commit()
        if cur.rowcount == 0:
            return jsonify({'error': 'not found'}), 404
        return jsonify({'ok': True})

    @app.route('/<path:path>')
    def static_proxy(path):
        # Serve other static assets
        return send_from_directory(app.static_folder, path)

    return app


def init_db():
    if DB_MODE == 'sqlite':
        with closing(sqlite3.connect(DB_PATH)) as db:
            db.executescript(
                '''
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS roommates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    color TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roommate_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    location TEXT,
                    notes TEXT,
                    FOREIGN KEY(roommate_id) REFERENCES roommates(id) ON DELETE CASCADE
                );
                '''
            )
            # Seed roommates if empty
            cur = db.execute('SELECT COUNT(*) as c FROM roommates')
            c = cur.fetchone()[0]
            if c == 0:
                defaults = [
                    ('Vatsal',  '#3778C2'),
                    ('Ganesh',  '#EF6C33'),
                    ('Jenil',   '#2BAF2B'),
                    ('Shibin',  '#8E44AD'),
                    ('Jeevan',  '#C0392B'),
                    ('Sarwesh', '#16A085'),
                    ('Tushar',  '#D35400'),
                    ('Rajeev',  '#7F8C8D'),
                    ('Vineet',  '#F1C40F'),
                    ('Prakhar', '#1ABC9C'),
                    ('Srinidhi','#9B59B6'),
                ]
                db.executemany('INSERT INTO roommates(name, color) VALUES (?,?)', defaults)
            db.commit()
    else:
        # Postgres init (uses same fallback logic)
        with closing(_pg_connect(DATABASE_URL)) as db:
            with db.cursor() as cur:
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS roommates (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        color TEXT NOT NULL
                    );
                ''')
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS events (
                        id SERIAL PRIMARY KEY,
                        roommate_id INTEGER NOT NULL REFERENCES roommates(id) ON DELETE CASCADE,
                        title TEXT NOT NULL,
                        start TEXT NOT NULL,
                        "end" TEXT NOT NULL,
                        location TEXT,
                        notes TEXT
                    );
                ''')
                # Seed if empty
                cur.execute('SELECT COUNT(*) FROM roommates')
                c = cur.fetchone()[0]
                if c == 0:
                    defaults = [
                        ('Vatsal',  '#3778C2'),
                        ('Ganesh',  '#EF6C33'),
                        ('Jenil',   '#2BAF2B'),
                        ('Shibin',  '#8E44AD'),
                        ('Jeevan',  '#C0392B'),
                        ('Sarwesh', '#16A085'),
                        ('Tushar',  '#D35400'),
                        ('Rajeev',  '#7F8C8D'),
                        ('Vineet',  '#F1C40F'),
                        ('Prakhar', '#1ABC9C'),
                        ('Srinidhi','#9B59B6'),
                    ]
                    cur.executemany('INSERT INTO roommates(name, color) VALUES (%s,%s)', defaults)
            db.commit()

# Expose a module-level WSGI variable for platforms expecting 'app:app'
app = create_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='127.0.0.1', port=port, debug=True)
