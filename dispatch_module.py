"""Operacyjna obsługa zakładu: awizacje i kolejka załadunkowa.

Moduł celowo nie steruje stanem magazynowym ani nie odczytuje wagi. Zachowuje
etapy zakładowe, aby później można było bezpiecznie dołączyć urządzenie wagi.
"""
from flask import Blueprint, abort, redirect, render_template_string, request, session, url_for

bp = Blueprint("dispatch", __name__, url_prefix="/dispatch")
D = {}

STAGES = [
    ("planned", "Zaplanowana"), ("waiting", "Oczekuje"),
    ("gate_entered", "Wjazd na zakład"), ("first_weighing", "Pierwsza waga"),
    ("waiting_for_loading", "Kolejka do załadunku"), ("loading", "Załadunek"),
    ("second_weighing", "Waga końcowa"), ("ready_to_leave", "Gotowy do wyjazdu"),
    ("departed", "Wyjechał"), ("cancelled", "Anulowana"), ("problem", "Problem"),
]
STAGE_LABEL = dict(STAGES)
NEXT = {
    "planned": {"waiting", "cancelled", "problem"},
    "waiting": {"gate_entered", "cancelled", "problem"},
    "gate_entered": {"first_weighing", "problem"},
    "first_weighing": {"waiting_for_loading", "problem"},
    "waiting_for_loading": {"loading", "problem"},
    "loading": {"second_weighing", "problem"},
    "second_weighing": {"ready_to_leave", "problem"},
    "ready_to_leave": {"departed", "problem"},
    "problem": {"waiting", "gate_entered", "first_weighing", "waiting_for_loading", "loading", "second_weighing", "ready_to_leave", "cancelled"},
}

def _now(): return D["now_iso"]()
def _actor(): return session.get("display_name") or session.get("username") or "pracownik"
def _number(c):
    year = _now()[:4]
    n = c.execute("SELECT COUNT(*) FROM dispatch_appointments WHERE appointment_no LIKE ?", (f"AW/{year}/%",)).fetchone()[0] + 1
    return f"AW/{year}/{n:05d}"

def register_dispatch(app, deps):
    global D; D = deps
    with D["conn"]() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS loading_bays(
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dispatch_appointments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, appointment_no TEXT NOT NULL UNIQUE,
          order_id INTEGER NOT NULL REFERENCES orders(id), wz_id INTEGER REFERENCES wz_documents(id),
          transport_id INTEGER REFERENCES transports(id), driver_id INTEGER REFERENCES drivers(id),
          vehicle_id INTEGER REFERENCES vehicles(id), loading_bay_id INTEGER REFERENCES loading_bays(id),
          planned_date TEXT NOT NULL, time_from TEXT, time_to TEXT, shift TEXT, queue_position INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'planned', notes TEXT DEFAULT '', problem_reason TEXT,
          created_by TEXT NOT NULL, updated_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS appointment_status_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT, appointment_id INTEGER NOT NULL REFERENCES dispatch_appointments(id) ON DELETE CASCADE,
          old_status TEXT, new_status TEXT NOT NULL, reason TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_dispatch_day_status ON dispatch_appointments(planned_date,status,queue_position);
        CREATE INDEX IF NOT EXISTS idx_dispatch_transport ON dispatch_appointments(transport_id);
        """)
        if not c.execute("SELECT 1 FROM loading_bays LIMIT 1").fetchone():
            now = _now()
            c.execute("INSERT INTO loading_bays(code,name,created_at,updated_at) VALUES(?,?,?,?)", ("S1", "Stanowisko 1", now, now))
    app.register_blueprint(bp)

@bp.route("/appointments", methods=["GET", "POST"])
def appointments():
    day = request.values.get("day") or _now()[:10]
    if request.method == "POST":
        order_id = int(request.form.get("order_id") or 0)
        if not order_id: abort(400)
        with D["conn"]() as c:
            now = _now()
            position = c.execute("SELECT COALESCE(MAX(queue_position),0)+1 FROM dispatch_appointments WHERE planned_date=?", (request.form.get("planned_date") or day,)).fetchone()[0]
            c.execute("""INSERT INTO dispatch_appointments(appointment_no,order_id,wz_id,transport_id,driver_id,vehicle_id,loading_bay_id,planned_date,time_from,time_to,shift,queue_position,status,notes,created_by,updated_by,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'planned',?,?,?,?,?)""", (_number(c), order_id, request.form.get("wz_id") or None, request.form.get("transport_id") or None, request.form.get("driver_id") or None, request.form.get("vehicle_id") or None, request.form.get("loading_bay_id") or None, request.form.get("planned_date") or day, request.form.get("time_from") or None, request.form.get("time_to") or None, request.form.get("shift") or None, position, request.form.get("notes", "").strip(), _actor(), _actor(), now, now))
        return redirect(url_for("dispatch.appointments", day=day))
    with D["conn"]() as c:
        rows = c.execute("""SELECT a.*,o.order_no,o.customer_name,v.registration_no,d.name driver_name,b.code bay_code,
          (SELECT group_concat(sku || ' · ' || qty_planned, ', ') FROM wz_items WHERE wz_id=a.wz_id) items
          FROM dispatch_appointments a JOIN orders o ON o.id=a.order_id
          LEFT JOIN vehicles v ON v.id=a.vehicle_id LEFT JOIN drivers d ON d.id=a.driver_id LEFT JOIN loading_bays b ON b.id=a.loading_bay_id
          WHERE a.planned_date=? ORDER BY a.queue_position,a.time_from,a.id""", (day,)).fetchall()
        orders = c.execute("SELECT id,order_no,customer_name FROM orders WHERE lower(status) <> 'cancelled' ORDER BY id DESC LIMIT 300").fetchall()
        wzs = c.execute("SELECT w.id,w.wz_no,o.order_no FROM wz_documents w JOIN orders o ON o.id=w.order_id WHERE w.deleted_at IS NULL ORDER BY w.id DESC LIMIT 300").fetchall()
        transports = c.execute("SELECT id,transport_no FROM transports WHERE deleted_at IS NULL AND status NOT IN ('returned','closed') ORDER BY id DESC LIMIT 300").fetchall()
        drivers = c.execute("SELECT id,name FROM drivers WHERE active=1 AND deleted_at IS NULL ORDER BY name").fetchall()
        vehicles = c.execute("SELECT id,registration_no FROM vehicles WHERE active=1 AND deleted_at IS NULL ORDER BY registration_no").fetchall()
        bays = c.execute("SELECT * FROM loading_bays WHERE active=1 ORDER BY code").fetchall()
    return render_template_string(TPL, rows=rows, orders=orders, wzs=wzs, transports=transports, drivers=drivers, vehicles=vehicles, bays=bays, day=day, labels=STAGE_LABEL, title="Awizacje", base_url=D["BASE_URL"], db_path=D["DB_PATH"])

@bp.post("/appointments/<int:appointment_id>/status")
def appointment_status(appointment_id):
    target = request.form.get("status", "")
    reason = request.form.get("reason", "").strip()
    with D["conn"]() as c:
        row = c.execute("SELECT * FROM dispatch_appointments WHERE id=?", (appointment_id,)).fetchone()
        if not row: abort(404)
        if target not in NEXT.get(row["status"], set()): return "Nieprawidłowa kolejność etapu.", 409
        if target in {"problem", "cancelled"} and not reason: return "Podaj powód problemu lub anulowania.", 400
        now = _now()
        c.execute("UPDATE dispatch_appointments SET status=?,problem_reason=?,updated_by=?,updated_at=? WHERE id=?", (target, reason or None, _actor(), now, appointment_id))
        c.execute("INSERT INTO appointment_status_history(appointment_id,old_status,new_status,reason,actor,created_at) VALUES(?,?,?,?,?,?)", (appointment_id,row["status"],target,reason,_actor(),now))
    return redirect(url_for("dispatch.appointments", day=request.form.get("day") or _now()[:10]))

@bp.post("/appointments/<int:appointment_id>/move")
def appointment_move(appointment_id):
    direction = request.form.get("direction")
    with D["conn"]() as c:
        row=c.execute("SELECT * FROM dispatch_appointments WHERE id=?", (appointment_id,)).fetchone()
        if not row: abort(404)
        sign = -1 if direction == "up" else 1
        other=c.execute("SELECT * FROM dispatch_appointments WHERE planned_date=? AND id<>? AND queue_position {} ? ORDER BY queue_position {} LIMIT 1".format("<" if sign < 0 else ">", "DESC" if sign < 0 else "ASC"), (row["planned_date"], appointment_id, row["queue_position"])).fetchone()
        if other:
            c.execute("UPDATE dispatch_appointments SET queue_position=?,updated_by=?,updated_at=? WHERE id=?", (other["queue_position"],_actor(),_now(),appointment_id))
            c.execute("UPDATE dispatch_appointments SET queue_position=?,updated_by=?,updated_at=? WHERE id=?", (row["queue_position"],_actor(),_now(),other["id"]))
    return redirect(url_for("dispatch.appointments", day=request.form.get("day") or _now()[:10]))

@bp.get("/queue")
def queue():
    day=request.args.get("day") or _now()[:10]
    with D["conn"]() as c:
        rows=c.execute("""SELECT a.*,o.customer_name,v.registration_no,d.name driver_name,b.code bay_code FROM dispatch_appointments a
          JOIN orders o ON o.id=a.order_id LEFT JOIN vehicles v ON v.id=a.vehicle_id LEFT JOIN drivers d ON d.id=a.driver_id LEFT JOIN loading_bays b ON b.id=a.loading_bay_id
          WHERE a.planned_date=? AND a.status NOT IN ('departed','cancelled') ORDER BY a.queue_position,a.time_from""",(day,)).fetchall()
    groups={"waiting":[],"weighing":[],"loading":[],"ready":[]}
    for row in rows:
        key="waiting" if row["status"] in {"planned","waiting","gate_entered","waiting_for_loading"} else "weighing" if row["status"] in {"first_weighing","second_weighing"} else "loading" if row["status"]=="loading" else "ready"
        groups[key].append(row)
    return render_template_string(QUEUE_TPL, groups=groups, day=day, labels=STAGE_LABEL, title="Kolejka załadunkowa", base_url=D["BASE_URL"], db_path=D["DB_PATH"])

TPL = '''{% extends "base.html" %}{% block content %}
<div class="flex"><h1>Awizacje zakładowe</h1><a class="btn right" href="{{url_for('dispatch.queue',day=day)}}">Ekran kolejki</a></div>
<div class="card"><form method="get" class="flex"><label>Data <input type="date" name="day" value="{{day}}"></label><button class="btn primary">Pokaż dzień</button></form></div>
<div class="card"><h2>Dodaj awizację</h2><form method="post" class="grid3"><input type="hidden" name="planned_date" value="{{day}}"><div><label>Zamówienie</label><select name="order_id" required><option value="">Wybierz</option>{% for x in orders %}<option value="{{x.id}}">{{x.order_no}} · {{x.customer_name}}</option>{% endfor %}</select></div><div><label>Godzina</label><input name="time_from" type="time"></div><div><label>Zmiana</label><input name="shift" placeholder="np. I"></div><div><label>WZ (opcjonalnie)</label><select name="wz_id"><option value="">—</option>{% for x in wzs %}<option value="{{x.id}}">{{x.wz_no}} · {{x.order_no}}</option>{% endfor %}</select></div><div><label>Transport</label><select name="transport_id"><option value="">—</option>{% for x in transports %}<option value="{{x.id}}">{{x.transport_no}}</option>{% endfor %}</select></div><div><label>Stanowisko</label><select name="loading_bay_id"><option value="">—</option>{% for x in bays %}<option value="{{x.id}}">{{x.code}}</option>{% endfor %}</select></div><div><label>Kierowca</label><select name="driver_id"><option value="">—</option>{% for x in drivers %}<option value="{{x.id}}">{{x.name}}</option>{% endfor %}</select></div><div><label>Auto</label><select name="vehicle_id"><option value="">—</option>{% for x in vehicles %}<option value="{{x.id}}">{{x.registration_no}}</option>{% endfor %}</select></div><div style="align-self:end"><button class="btn primary">Dodaj do harmonogramu</button></div></form></div>
<div class="card"><table><thead><tr><th>Godzina</th><th>Awizacja / klient</th><th>Auto / kierowca</th><th>Stanowisko</th><th>Etap</th><th>Kolejka</th><th>Zmień etap</th></tr></thead><tbody>{% for x in rows %}<tr><td>{{x.time_from or '—'}}<br><span class="muted">{{x.shift or ''}}</span></td><td><b>{{x.appointment_no}}</b><br>{{x.customer_name}}<br><span class="muted">{{x.items or 'Pozycje WZ po utworzeniu'}}</span></td><td>{{x.registration_no or '—'}}<br>{{x.driver_name or '—'}}</td><td>{{x.bay_code or '—'}}</td><td><span class="badge">{{labels[x.status]}}</span></td><td><form method="post" action="{{url_for('dispatch.appointment_move',appointment_id=x.id)}}"><input type="hidden" name="day" value="{{day}}"><button name="direction" value="up" class="btn">↑</button><button name="direction" value="down" class="btn">↓</button></form></td><td><form method="post" action="{{url_for('dispatch.appointment_status',appointment_id=x.id)}}"><input type="hidden" name="day" value="{{day}}"><select name="status"><option value="">Wybierz następny etap</option>{% for value,label in labels.items() %}<option value="{{value}}">{{label}}</option>{% endfor %}</select><input name="reason" placeholder="Powód tylko dla problemu/anulowania"><button class="btn primary">Zapisz</button></form></td></tr>{% else %}<tr><td colspan="7">Brak awizacji na ten dzień.</td></tr>{% endfor %}</tbody></table></div>{% endblock %}'''
QUEUE_TPL = '''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Kolejka załadunkowa</h1><a class="btn right" href="{{url_for('dispatch.appointments',day=day)}}">Awizacje</a></div><div class="card"><form method="get" class="flex"><input type="date" name="day" value="{{day}}"><button class="btn primary">Pokaż</button></form></div><div class="grid3">{% for key,title in [('waiting','Oczekują'),('weighing','Ważenie'),('loading','Załadunek'),('ready','Gotowe do wyjazdu')] %}<section class="card"><h2>{{title}}</h2>{% for x in groups[key] %}<div style="border-bottom:1px solid #edf0f5;padding:12px 0"><b>{{x.registration_no or 'Auto nieprzypisane'}}</b><br>{{x.driver_name or 'Kierowca nieprzypisany'}} · {{x.customer_name}}<br><span class="muted">{{x.time_from or 'bez godziny'}} · stanowisko {{x.bay_code or '—'}} · {{labels[x.status]}}</span></div>{% else %}<div class="muted">Brak pojazdów.</div>{% endfor %}</section>{% endfor %}</div>{% endblock %}'''
