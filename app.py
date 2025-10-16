import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional
from flask import Flask, jsonify, request, send_from_directory, g


APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Allow overriding DB path for cloud deploys with persistent disks
DB_PATH = os.environ.get('DB_PATH', os.path.join(APP_DIR, 'data.db'))


def create_app():
    app = Flask(__name__, static_folder='static', template_folder='templates')

    @app.before_request
    def before_request():
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row

    @app.teardown_request
    def teardown_request(exception):
        db = getattr(g, 'db', None)
        if db is not None:
            db.close()

    with app.app_context():
        init_db()

    @app.route('/')
    def index():
        return send_from_directory(app.static_folder, 'index.html')

    @app.route('/api/roommates', methods=['GET'])
    def list_roommates():
        rows = g.db.execute('SELECT id, name, color FROM roommates ORDER BY id').fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route('/api/roommates', methods=['POST'])
    def add_roommate():
        data = request.get_json(force=True)
        name = (data.get('name') or '').strip()
        color = (data.get('color') or '#3778C2').strip()
        if not name:
            return jsonify({'error': 'name is required'}), 400
        try:
            cur = g.db.execute('INSERT INTO roommates(name, color) VALUES (?, ?)', (name, color))
            g.db.commit()
            rid = cur.lastrowid
            row = g.db.execute('SELECT id, name, color FROM roommates WHERE id=?', (rid,)).fetchone()
            return jsonify(dict(row)), 201
        except sqlite3.IntegrityError as e:
            return jsonify({'error': 'Roommate name must be unique'}), 409

    @app.route('/api/roommates/<int:rid>', methods=['PUT'])
    def update_roommate(rid):
        data = request.get_json(force=True)
        name = data.get('name')
        color = data.get('color')
        row = g.db.execute('SELECT id FROM roommates WHERE id=?', (rid,)).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404
        if name is not None:
            name = name.strip()
            if not name:
                return jsonify({'error': 'name cannot be empty'}), 400
            try:
                g.db.execute('UPDATE roommates SET name=? WHERE id=?', (name, rid))
                g.db.commit()
            except sqlite3.IntegrityError:
                return jsonify({'error': 'Roommate name must be unique'}), 409
        if color is not None:
            color = color.strip()
            if not color:
                return jsonify({'error': 'color cannot be empty'}), 400
            g.db.execute('UPDATE roommates SET color=? WHERE id=?', (color, rid))
            g.db.commit()
        row = g.db.execute('SELECT id, name, color FROM roommates WHERE id=?', (rid,)).fetchone()
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
            'SELECT e.id, e.title, e.start, e.end, e.location, e.notes, '
            'e.roommate_id, r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id'
        )
        args = []
        where = []
        if start:
            where.append('julianday(replace(e.end, "T", " ")) > julianday(replace(?, "T", " "))')
            args.append(start)
        if end:
            where.append('julianday(replace(e.start, "T", " ")) < julianday(replace(?, "T", " "))')
            args.append(end)
        sql = base + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY e.start'
        rows = g.db.execute(sql, args).fetchall()
        return [event_row_to_dict(r) for r in rows]

    def find_conflicts(start: str, end: str, exclude_event_id: Optional[int] = None):
        args = [start, end]
        sql = (
            'SELECT e.id, e.title, e.start, e.end, e.location, e.notes, '
            'e.roommate_id, r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id '
            'WHERE julianday(replace(e.start, "T", " ")) < julianday(replace(?, "T", " ")) '
            'AND julianday(replace(e.end, "T", " ")) > julianday(replace(?, "T", " "))'
        )
        if exclude_event_id is not None:
            sql += ' AND e.id != ?'
            args.append(exclude_event_id)
        rows = g.db.execute(sql, args).fetchall()
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
        rm = g.db.execute('SELECT id FROM roommates WHERE id=?', (roommate_id,)).fetchone()
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

        cur = g.db.execute(
            'INSERT INTO events(roommate_id, title, start, end, location, notes) VALUES (?, ?, ?, ?, ?, ?)',
            (roommate_id, title, start, end, location, notes)
        )
        g.db.commit()
        eid = cur.lastrowid
        row = g.db.execute(
            'SELECT e.id, e.title, e.start, e.end, e.location, e.notes, e.roommate_id, '
            'r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id WHERE e.id=?', (eid,)
        ).fetchone()
        return jsonify({'event': event_row_to_dict(row), 'conflicts': conflicts}), 201

    @app.route('/api/events/<int:eid>', methods=['PUT'])
    def update_event(eid):
        row = g.db.execute('SELECT id FROM events WHERE id=?', (eid,)).fetchone()
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
        existing = g.db.execute('SELECT start, end FROM events WHERE id=?', (eid,)).fetchone()
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

        g.db.execute(f'UPDATE events SET {", ".join(fields)} WHERE id=?', (*args, eid))
        g.db.commit()
        # Conflicts after update (exclude this event)
        conflicts = find_conflicts(new_end, new_start, exclude_event_id=eid)
        row = g.db.execute(
            'SELECT e.id, e.title, e.start, e.end, e.location, e.notes, e.roommate_id, '
            'r.name as roommate_name, r.color as roommate_color '
            'FROM events e JOIN roommates r ON r.id = e.roommate_id WHERE e.id=?', (eid,)
        ).fetchone()
        return jsonify({'event': event_row_to_dict(row), 'conflicts': conflicts})

    @app.route('/api/events/<int:eid>', methods=['DELETE'])
    def delete_event(eid):
        cur = g.db.execute('DELETE FROM events WHERE id=?', (eid,))
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

# Expose a module-level WSGI variable for platforms expecting 'app:app'
app = create_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='127.0.0.1', port=port, debug=True)
