const api = {
  async getRoommates() {
    const r = await fetch('/api/roommates');
    if (!r.ok) throw new Error('Failed to load roommates');
    return r.json();
  },
  async getEvents(startISO, endISO) {
    const url = new URL('/api/events', window.location.origin);
    if (startISO) url.searchParams.set('start', startISO);
    if (endISO) url.searchParams.set('end', endISO);
    const r = await fetch(url);
    if (!r.ok) throw new Error('Failed to load events');
    return r.json();
  },
  async createEvent(evt) {
    const r = await fetch('/api/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(evt),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw Object.assign(new Error(data.error || 'Failed to create'), { data, status: r.status });
    }
    return r.json();
  },
  async updateRoommate(id, patch) {
    const r = await fetch(`/api/roommates/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!r.ok) throw new Error('Failed to update roommate');
    return r.json();
  },
  async deleteEvent(id) {
    const r = await fetch(`/api/events/${id}`, { method: 'DELETE' });
    if (!r.ok) throw new Error('Failed to delete event');
    return r.json();
  }
};

function toLocalInputValue(date) {
  // date: Date
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}` +
         `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

let calendar;
let roommates = [];
let roommateFilters = new Set();

async function init() {
  roommates = await api.getRoommates();
  renderRoommateDropdown();
  renderFilters();
  renderRoommatesSettings();
  setupFormDefaults();
  initCalendar();
}

function renderRoommateDropdown() {
  const sel = document.getElementById('roommate');
  sel.innerHTML = '';
  roommates.forEach((r) => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = `${r.name}`;
    sel.appendChild(opt);
  });
}

function renderFilters() {
  const c = document.getElementById('filters');
  c.innerHTML = '';
  const label = document.createElement('label');
  label.textContent = 'Filter by roommate:';
  c.appendChild(label);
  const line = document.createElement('div');
  line.className = 'filter-row';
  roommates.forEach((r) => {
    const wrap = document.createElement('span');
    wrap.className = 'filter-chip';
    wrap.style.setProperty('--chip-color', r.color);
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.addEventListener('change', () => {
      if (cb.checked) roommateFilters.delete(r.id); else roommateFilters.add(r.id);
      calendar.refetchEvents();
    });
    const txt = document.createElement('span');
    txt.textContent = r.name;
    wrap.appendChild(cb);
    wrap.appendChild(txt);
    line.appendChild(wrap);
  });
  c.appendChild(line);
}

function renderRoommatesSettings() {
  const s = document.getElementById('roommates-settings');
  s.innerHTML = '';
  roommates.forEach((r) => {
    const row = document.createElement('div');
    row.className = 'settings-row';
    row.innerHTML = `
      <input type="text" value="${r.name}" data-id="${r.id}" class="name" />
      <input type="color" value="${r.color}" data-id="${r.id}" class="color" />
      <button class="save" data-id="${r.id}">Save</button>
    `;
    s.appendChild(row);
  });
  s.addEventListener('click', async (e) => {
    const btn = e.target.closest('button.save');
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const name = s.querySelector(`input.name[data-id="${id}"]`).value.trim();
    const color = s.querySelector(`input.color[data-id="${id}"]`).value.trim();
    await api.updateRoommate(id, { name, color });
    roommates = await api.getRoommates();
    renderRoommateDropdown();
    renderFilters();
    calendar.refetchEvents();
  });
}

function setupFormDefaults() {
  const now = new Date();
  now.setMinutes(now.getMinutes() + (15 - (now.getMinutes() % 15)) % 15);
  const end = new Date(now.getTime() + 60*60*1000);
  document.getElementById('start').value = toLocalInputValue(now);
  document.getElementById('end').value = toLocalInputValue(end);

  document.getElementById('event-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const status = document.getElementById('form-status');
    status.textContent = '';
    const roommate_id = Number(document.getElementById('roommate').value);
    const title = document.getElementById('title').value.trim() || 'Interview';
    const startLocal = document.getElementById('start').value; // YYYY-MM-DDTHH:MM (local)
    const endLocal = document.getElementById('end').value;
    // Convert to RFC3339 UTC so server and calendar compare reliably
    const start = new Date(startLocal).toISOString();
    const end = new Date(endLocal).toISOString();
    const location = document.getElementById('location').value.trim();
    const notes = document.getElementById('notes').value.trim();
    const rejectOnConflict = document.getElementById('rejectOnConflict').checked;
    try {
      const res = await api.createEvent({ roommate_id, title, start, end, location, notes, rejectOnConflict });
      calendar.refetchEvents();
      if (res.conflicts && res.conflicts.length) {
        status.textContent = `Warning: overlaps with ${res.conflicts.length} other interview(s).`;
      } else {
        status.textContent = 'Saved.';
      }
      document.getElementById('notes').value = '';
    } catch (err) {
      if (err.status === 409 && err.data && err.data.conflicts) {
        status.textContent = `Conflict: overlaps with ${err.data.conflicts.length} other interview(s).`;
      } else {
        status.textContent = err.message || 'Error';
      }
    }
  });
}

function initCalendar() {
  const calEl = document.getElementById('calendar');
  calendar = new FullCalendar.Calendar(calEl, {
    initialView: 'dayGridMonth',
    height: 'auto',
    slotMinTime: '06:00:00',
    slotMaxTime: '23:00:00',
    nowIndicator: true,
    headerToolbar: {
      left: 'prev,next today',
      center: 'title',
      right: 'dayGridMonth,timeGridWeek,timeGridDay'
    },
    events: async (info, success, failure) => {
      try {
        const evts = await api.getEvents(info.startStr, info.endStr);
        const data = evts
          .filter(e => !roommateFilters.has(e.roommate.id))
          .map(e => ({
            id: e.id,
            title: `${e.roommate.name}: ${e.title}`,
            start: e.start,
            end: e.end,
            backgroundColor: e.roommate.color,
            borderColor: e.roommate.color,
            extendedProps: { location: e.location, notes: e.notes, roommate: e.roommate }
          }));
        success(data);
      } catch (err) {
        failure(err);
      }
    },
    eventDidMount: (info) => {
      const { location, notes } = info.event.extendedProps;
      let tip = '';
      if (location) tip += `Location: ${location}\n`;
      if (notes) tip += `Notes: ${notes}`;
      if (tip) info.el.title = tip;
    },
    eventClick: async (info) => {
      if (!confirm('Delete this interview?')) return;
      try {
        await api.deleteEvent(info.event.id);
        calendar.refetchEvents();
      } catch (e) {
        alert('Failed to delete');
      }
    }
  });
  calendar.render();
}

init().catch((e) => {
  console.error(e);
  alert('Failed to initialize app');
});
