# Roomie Interview Scheduler

A minimal web app for 8 roommates to share and view interview schedules so you can coordinate quiet time and avoid clashes.

## Quick Start

Prereqs: Python 3.9+ installed.

1. Create a virtualenv and install deps:

   ```bash
   cd roomie-interview-scheduler
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Run the server:

   ```bash
   python app.py
   ```

3. Open the app:

- http://127.0.0.1:5000/

## Features

- Shared calendar view of all interviews
- Color-coded by roommate
- Add/update/delete interviews
- Optional conflict detection on create
- Lightweight SQLite storage (file `data.db`)

## Customize Roommates

On first run, the app seeds these roommates:

```
Vatsal, Ganesh, Jenil, Shibin, Jeevan,
Sarwesh, Tushar, Rajeev, Vineet, Prakhar, Srinidhi
```

If you already ran the app before this change and want these names:

- Easiest (resets data): stop the app and delete `data.db`, then start again.
- Keep existing events: use Settings → Roommates to rename current entries, and use the API to add any missing names (`POST /api/roommates`).

## API (brief)

- `GET /api/roommates` — list roommates
- `POST /api/roommates` — add roommate `{name, color}`
- `PUT /api/roommates/<id>` — update `{name?, color?}`
- `GET /api/events?start=ISO&end=ISO` — list events
- `POST /api/events` — create event `{roommate_id, title, start, end, location?, notes?, rejectOnConflict?}`
- `PUT /api/events/<id>` — update event
- `DELETE /api/events/<id>` — delete event

Times are ISO-8601 strings, local or UTC. The UI uses your local time.

## Deploy

Option A — Render (recommended, persistent storage):

- Push this folder to a GitHub repo.
- In Render, create “New +” → “Web Service” → “Build from a Git repository”.
- Select the repo. Render reads `render.yaml` and sets:
  - Build: `pip install -r requirements.txt`
  - Start: `gunicorn -w 2 -k gthread -b 0.0.0.0:$PORT 'app:create_app()'`
  - Disk: mounts at `/data`, `DB_PATH=/data/data.db` so events persist.
- Wait for deploy, then open the Render URL.

Option B — Quick share via ngrok (ephemeral):

- Install ngrok and sign in (free tier is fine).
- Run locally (`python app.py`), then in another terminal: `ngrok http 5000`.
- Share the `https://...ngrok.io` URL. Data lives in local `data.db`.
