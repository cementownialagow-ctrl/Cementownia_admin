"""Operacyjna obsługa zakładu: awizacje i kolejka załadunkowa.

Moduł celowo nie steruje stanem magazynowym ani nie odczytuje wagi. Zachowuje
etapy zakładowe, aby później można było bezpiecznie dołączyć urządzenie wagi.
"""
import math
import secrets
import time

from flask import Blueprint, abort, redirect, render_template_string, request, session, url_for

bp = Blueprint("dispatch", __name__, url_prefix="/dispatch")
D = {}

STAGES = [
    ("planned", "Zaplanowana"), ("loading", "Załadunek"),
    ("ready_to_leave", "Gotowy do wyjazdu"), ("departed", "Wyjechał"),
    ("cancelled", "Anulowana"), ("problem", "Problem"),
]
STAGE_LABEL = dict(STAGES)
TRANSPORT_STAGE_LABEL = {
    "assigned": "Przypisany", "issued": "Towar wydany", "in_transit": "W dostawie",
    "closed": "Na miejscu", "delivered": "WZ podpisane", "returned": "Wrócił na bazę", "problem": "Problem",
}

# Uproszczony obieg: dyspozytor wystawia wyjazd, kierowca obsługuje dopiero dostawę.
STAGES = [("waiting", "Oczekuje"), ("departed", "Wyjechał"),
          ("cancelled", "Anulowana"), ("problem", "Problem")]
STAGE_LABEL = dict(STAGES)
TRANSPORT_STAGE_LABEL.update({"assigned": "Oczekuje", "issued": "Oczekuje", "in_transit": "Wyjechał"})

def _now(): return D["now_iso"]()
def _actor(): return session.get("display_name") or session.get("username") or "pracownik"
def _cloud_id(): return int(time.time() * 1000) * 1000 + secrets.randbelow(1000)
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
            order_plan = c.execute("SELECT delivery_date, delivery_time FROM orders WHERE id=?", (order_id,)).fetchone()
            # Termin z zamówienia jest źródłem prawdy. Dyspozytor nie wpisuje
            # go drugi raz podczas przydzielania transportu.
            planned_date = ((order_plan["delivery_date"] if order_plan else "") or request.form.get("planned_date") or day).strip()
            planned_delivery_time = ((order_plan["delivery_time"] if order_plan else "") or request.form.get("time_to") or "").strip()
            wz_id = int(request.form.get("wz_id") or 0)
            driver_id = int(request.form.get("driver_id") or 0)
            vehicle_id = int(request.form.get("vehicle_id") or 0)
            transport_id = int(request.form.get("transport_id") or 0)
            if not wz_id:
                return 'Wybierz wydany dokument WZ dla tego kursu.', 400
            # Jedna awizacja z wybranym WZ, kierowcą i autem od razu tworzy kurs.
            # Dzięki temu kierowca widzi go natychmiast w swoim panelu.
            if not transport_id and wz_id and driver_id and vehicle_id:
                try:
                    transport_qty=float((request.form.get("transport_qty") or "").replace(",", "."))
                except ValueError:
                    transport_qty=0
                if transport_qty <= 0 or transport_qty > 8:
                    return 'Podaj ilość tego kursu od 0,01 do 8 m³.', 400
                remaining_rows=c.execute('''SELECT wi.*,COALESCE((SELECT SUM(ti.qty) FROM transport_items ti
                    JOIN transports t ON t.id=ti.transport_id WHERE ti.wz_item_id=wi.id AND t.deleted_at IS NULL),0) assigned
                    FROM wz_items wi WHERE wi.wz_id=? ORDER BY wi.id''',(wz_id,)).fetchall()
                remaining_total=sum(max(0.0,float((x['qty_issued'] if x['qty_issued'] is not None else x['qty_planned']) or 0)-float(x['assigned'] or 0)) for x in remaining_rows)
                if transport_qty > remaining_total + 0.00001:
                    return f'Pozostało tylko {remaining_total:g} m³ do przydzielenia.', 400
                if remaining_total > 0:
                    year = now[:4]
                    number = c.execute("SELECT COUNT(*) FROM transports WHERE transport_no LIKE ?", (f"TR/{year}/%",)).fetchone()[0] + 1
                    transport_no = f"TR/{year}/{number:05d}"
                    transport_id = _cloud_id()
                    c.execute("""INSERT INTO transports(id,transport_no,wz_id,driver_id,vehicle_id,destination,status,created_by,updated_by,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,'assigned',?,?,?,?)""", (transport_id, transport_no, wz_id, driver_id, vehicle_id, request.form.get("destination", "").strip(), _actor(), _actor(), now, now))
                    left=transport_qty
                    for item in remaining_rows:
                        if left <= 0.00001: break
                        issued=float((item['qty_issued'] if item['qty_issued'] is not None else item['qty_planned']) or 0)
                        qty=min(max(0.0, issued-float(item['assigned'] or 0)),left)
                        if qty > 0.00001:
                            c.execute("INSERT INTO transport_items(id,transport_id,wz_item_id,qty,created_at) VALUES(?,?,?,?,?)", (_cloud_id(), transport_id, item["id"], qty, now))
                            left-=qty
            position = c.execute("SELECT COALESCE(MAX(queue_position),0)+1 FROM dispatch_appointments WHERE planned_date=?", (planned_date,)).fetchone()[0]
            c.execute("""INSERT INTO dispatch_appointments(appointment_no,order_id,wz_id,transport_id,driver_id,vehicle_id,loading_bay_id,planned_date,time_from,time_to,shift,queue_position,status,notes,created_by,updated_by,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'waiting',?,?,?,?,?)""", (_number(c), order_id, wz_id or None, transport_id or None, driver_id or None, vehicle_id or None, request.form.get("loading_bay_id") or None, planned_date, request.form.get("time_from") or None, planned_delivery_time or None, request.form.get("shift") or None, position, request.form.get("notes", "").strip(), _actor(), _actor(), now, now))
            c.commit()
            if transport_id:
                D['sync_local_rows_to_supabase']('transports','id',[transport_id])
                item_ids=[x['id'] for x in c.execute('SELECT id FROM transport_items WHERE transport_id=?',(transport_id,)).fetchall()]
                D['sync_local_rows_to_supabase']('transport_items','id',item_ids)
        # Przypomnienie o rozdzieleniu betonu na gruszki pojawia się zaraz po
        # dodaniu pierwszej awizacji. Jedna gruszka może zabrać maks. 8 m³.
        total_row = c.execute("SELECT COALESCE(SUM(qty), 0) AS total_m3 FROM order_items WHERE order_id=?", (order_id,)).fetchone()
        total_m3 = float(total_row["total_m3"] or 0)
        required_trips = max(1, math.ceil(total_m3 / 8))
        if required_trips > 1:
            notice = f"Zamówienie ma {total_m3:g} m³. Wymaga co najmniej {required_trips} podjazdów po maksymalnie 8 m³. Pierwszy kurs został dodany; pozostałą ilość rozdziel w dokumencie WZ, tworząc następny transport."
            return redirect(url_for("dispatch.appointments", day=day, capacity_notice=notice))
        return redirect(url_for("dispatch.appointments", day=day))
    with D["conn"]() as c:
        rows = c.execute("""SELECT a.*,o.order_no,o.customer_name,v.registration_no,d.name driver_name,b.code bay_code,t.status transport_status,
          (SELECT group_concat(sku || ' · ' || qty_planned, ', ') FROM wz_items WHERE wz_id=a.wz_id) items
          FROM dispatch_appointments a JOIN orders o ON o.id=a.order_id
          LEFT JOIN transports t ON t.id=a.transport_id
          LEFT JOIN vehicles v ON v.id=a.vehicle_id LEFT JOIN drivers d ON d.id=a.driver_id LEFT JOIN loading_bays b ON b.id=a.loading_bay_id
          WHERE a.planned_date=?
            AND COALESCE(t.status,'assigned') NOT IN ('returned','closed')
            AND COALESCE(a.status,'planned') NOT IN ('departed','cancelled')
          ORDER BY a.queue_position,a.time_from,a.id""", (day,)).fetchall()
        rows = [dict(x) for x in rows]
        for x in rows:
            x['display_stage'] = TRANSPORT_STAGE_LABEL.get(x.get('transport_status')) or STAGE_LABEL.get(x['status'], x['status'])
        # W formularzu pokazujemy tylko zamówienia, które nie trafiły jeszcze
        # do harmonogramu ani nie mają przypisanego aktywnego transportu.
        orders = c.execute("""SELECT o.id,o.order_no,o.customer_name,COALESCE(o.delivery_date,'') delivery_date,COALESCE(o.delivery_time,'') delivery_time,
            COALESCE((SELECT SUM(oi.qty) FROM order_items oi WHERE oi.order_id=o.id),0) AS total_m3,
            MAX(1, CAST((COALESCE((SELECT SUM(oi.qty) FROM order_items oi WHERE oi.order_id=o.id),0)+7.999999)/8 AS INTEGER)) AS required_trips
          FROM orders o
          WHERE lower(COALESCE(o.status,'')) NOT IN ('cancelled','issued','invoiced','completed')
            AND NOT EXISTS (
              SELECT 1 FROM dispatch_appointments a
              WHERE a.order_id=o.id AND a.status NOT IN ('departed','cancelled')
            )
            AND NOT EXISTS (
              SELECT 1 FROM wz_documents w
              JOIN transports t ON t.wz_id=w.id AND t.deleted_at IS NULL
              WHERE w.order_id=o.id AND w.deleted_at IS NULL
                AND t.status NOT IN ('returned','closed')
            )
          ORDER BY o.id DESC LIMIT 300""").fetchall()
        wzs = c.execute("""SELECT w.id,w.wz_no,o.order_no FROM wz_documents w
          JOIN orders o ON o.id=w.order_id
          WHERE w.deleted_at IS NULL AND w.status NOT IN ('ready_invoice','invoiced','returned','completed')
            AND NOT EXISTS (
              SELECT 1 FROM dispatch_appointments a
              WHERE a.wz_id=w.id AND a.status NOT IN ('departed','cancelled')
            )
            AND COALESCE((SELECT SUM(wi.qty_issued) FROM wz_items wi WHERE wi.wz_id=w.id),0) >
                COALESCE((SELECT SUM(ti.qty) FROM transport_items ti JOIN transports t ON t.id=ti.transport_id
                  WHERE t.wz_id=w.id AND t.deleted_at IS NULL),0)
          ORDER BY w.id DESC LIMIT 300""").fetchall()
        transports = c.execute("SELECT id,transport_no FROM transports WHERE deleted_at IS NULL AND status NOT IN ('returned','closed') ORDER BY id DESC LIMIT 300").fetchall()
        drivers = c.execute("SELECT id,name FROM drivers WHERE active=1 AND deleted_at IS NULL ORDER BY name").fetchall()
        vehicles = c.execute("SELECT id,registration_no FROM vehicles WHERE active=1 AND deleted_at IS NULL ORDER BY registration_no").fetchall()
        bays = c.execute("SELECT * FROM loading_bays WHERE active=1 ORDER BY code").fetchall()
    dispatch_tpl = TPL.replace('<label>Transport</label>', '<label>Ilość na ten transport [m³]</label><input type="number" name="transport_qty" min="0.01" max="8" step="0.01" value="8" required><label>Transport</label>')
    return render_template_string(dispatch_tpl, rows=rows, orders=orders, wzs=wzs, transports=transports, drivers=drivers, vehicles=vehicles, bays=bays, day=day, labels=STAGE_LABEL, capacity_notice=request.args.get("capacity_notice", ""), title="Wydaj transport", base_url=D["BASE_URL"], db_path=D["DB_PATH"])

@bp.post("/appointments/<int:appointment_id>/status")
def appointment_status(appointment_id):
    target = request.form.get("status", "")
    reason = request.form.get("reason", "").strip()
    with D["conn"]() as c:
        row = c.execute("SELECT * FROM dispatch_appointments WHERE id=?", (appointment_id,)).fetchone()
        if not row: abort(404)
        # Pracownicy zakładu mogą skorygować przebieg awizacji lub pominąć etap.
        # Ograniczenie kolejnych kliknięć dotyczy wyłącznie panelu kierowcy.
        if target not in STAGE_LABEL: return "Wybierz prawidłowy etap.", 400
        if target in {"problem", "cancelled"} and not reason: return "Podaj powód problemu lub anulowania.", 400
        now = _now()
        c.execute("UPDATE dispatch_appointments SET status=?,problem_reason=?,updated_by=?,updated_at=? WHERE id=?", (target, reason or None, _actor(), now, appointment_id))
        if target == 'departed' and row['transport_id']:
            # Wyjazd jest zatwierdzany przez dyspozytora, nie przez kierowcę.
            c.execute("UPDATE transports SET status='in_transit',departed_at=?,updated_by=?,updated_at=? WHERE id=? AND status IN ('assigned','issued')", (now, _actor(), now, row['transport_id']))
            c.execute("UPDATE wz_documents SET status='in_transport' WHERE id=? AND status='issued'", (row['wz_id'],))
        c.execute("INSERT INTO appointment_status_history(appointment_id,old_status,new_status,reason,actor,created_at) VALUES(?,?,?,?,?,?)", (appointment_id,row["status"],target,reason,_actor(),now))
        transport_id=row['transport_id']; wz_id=row['wz_id']
    # Najpierw zapisujemy sam etap awizacji. Dzięki temu po odświeżeniu
    # kierowca i panel główny widzą dokładnie ten sam stan.
    D['sync_local_rows_to_supabase']('dispatch_appointments','id',[appointment_id])
    if target == 'departed' and transport_id:
        D['sync_local_rows_to_supabase']('transports','id',[transport_id])
        if wz_id: D['sync_local_rows_to_supabase']('wz_documents','id',[wz_id])
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
          LEFT JOIN transports t ON t.id=a.transport_id
          WHERE a.planned_date=? AND a.status NOT IN ('departed','cancelled')
            AND COALESCE(t.status,'assigned') NOT IN ('returned','closed')
          ORDER BY a.queue_position,a.time_from""",(day,)).fetchall()
    groups={"waiting":[],"weighing":[],"loading":[],"ready":[]}
    for row in rows:
        key="waiting" if row["status"] in {"planned","waiting","gate_entered","waiting_for_loading"} else "weighing" if row["status"] in {"first_weighing","second_weighing"} else "loading" if row["status"]=="loading" else "ready"
        groups[key].append(row)
    return render_template_string(QUEUE_TPL, groups=groups, day=day, labels=STAGE_LABEL, title="Kolejka załadunkowa", base_url=D["BASE_URL"], db_path=D["DB_PATH"])

TPL = '''{% extends "base.html" %}{% block content %}
<div class="flex"><h1>Wydaj transport</h1><a class="btn right" href="{{url_for('dispatch.queue',day=day)}}">Ekran kolejki</a></div>
<div class="card"><form method="get" class="flex"><label>Data <input type="date" name="day" value="{{day}}"></label><button class="btn primary">Pokaż dzień</button></form></div>
<div class="card"><h2>Dodaj awizację</h2>{% if capacity_notice %}<div class="notice warn"><b>Podział transportu:</b> {{capacity_notice}}</div>{% endif %}<form method="post" class="grid3"><input type="hidden" name="planned_date" value="{{day}}"><div><label>Zamówienie</label><select name="order_id" required><option value="">Wybierz</option>{% for x in orders %}<option value="{{x.id}}">{{x.order_no}} · {{x.customer_name}} · {{'%.2f'|format(x.total_m3)|replace('.', ',')}} m³ · min. {{x.required_trips}} podjazd(y)</option>{% endfor %}</select></div><div><label>Planowany wyjazd</label><input name="time_from" type="time"></div><div><label>Planowana dostawa</label><input name="time_to" type="time"></div><div><label>Zmiana</label><input name="shift" placeholder="np. I"></div><div><label>WZ (opcjonalnie)</label><select name="wz_id"><option value="">—</option>{% for x in wzs %}<option value="{{x.id}}">{{x.wz_no}} · {{x.order_no}}</option>{% endfor %}</select></div><div><label>Transport</label><select name="transport_id"><option value="">—</option>{% for x in transports %}<option value="{{x.id}}">{{x.transport_no}}</option>{% endfor %}</select></div><div><label>Stanowisko</label><select name="loading_bay_id"><option value="">—</option>{% for x in bays %}<option value="{{x.id}}">{{x.code}}</option>{% endfor %}</select></div><div><label>Kierowca</label><select name="driver_id"><option value="">—</option>{% for x in drivers %}<option value="{{x.id}}">{{x.name}}</option>{% endfor %}</select></div><div><label>Auto</label><select name="vehicle_id"><option value="">—</option>{% for x in vehicles %}<option value="{{x.id}}">{{x.registration_no}}</option>{% endfor %}</select></div><div style="align-self:end"><button class="btn primary">Dodaj do harmonogramu</button></div></form></div>
<div class="card"><table><thead><tr><th>Wyjazd / dostawa</th><th>Awizacja / klient</th><th>Auto / kierowca</th><th>Stanowisko</th><th>Etap transportu</th><th>Kolejka</th><th>Zmień etap zakładowy</th></tr></thead><tbody>{% for x in rows %}<tr><td><b>Wyjazd: {{x.time_from or '—'}}</b><br><b>Dostawa: {{x.time_to or '—'}}</b><br><span class="muted">{{x.shift or ''}}</span></td><td><b>{{x.appointment_no}}</b><br>{{x.customer_name}}<br><span class="muted">{{x.items or 'Pozycje WZ po utworzeniu'}}</span></td><td>{{x.registration_no or '—'}}<br>{{x.driver_name or '—'}}</td><td>{{x.bay_code or '—'}}</td><td><span class="badge">{{x.display_stage}}</span></td><td><form method="post" action="{{url_for('dispatch.appointment_move',appointment_id=x.id)}}"><input type="hidden" name="day" value="{{day}}"><button name="direction" value="up" class="btn">↑</button><button name="direction" value="down" class="btn">↓</button></form></td><td><form method="post" action="{{url_for('dispatch.appointment_status',appointment_id=x.id)}}"><input type="hidden" name="day" value="{{day}}"><select name="status"><option value="">Ustaw etap</option>{% for value,label in labels.items() %}<option value="{{value}}">{{label}}</option>{% endfor %}</select><input name="reason" placeholder="Powód tylko dla problemu/anulowania"><button class="btn primary">Zapisz</button></form></td></tr>{% else %}<tr><td colspan="7">Brak awizacji na ten dzień.</td></tr>{% endfor %}</tbody></table></div>{% endblock %}'''
QUEUE_TPL = '''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Kolejka załadunkowa</h1><a class="btn right" href="{{url_for('dispatch.appointments',day=day)}}">Awizacje</a></div><div class="card"><form method="get" class="flex"><input type="date" name="day" value="{{day}}"><button class="btn primary">Pokaż</button></form></div><div class="grid3">{% for key,title in [('waiting','Oczekują'),('weighing','Ważenie'),('loading','Załadunek'),('ready','Gotowe do wyjazdu')] %}<section class="card"><h2>{{title}}</h2>{% for x in groups[key] %}<div style="border-bottom:1px solid #edf0f5;padding:12px 0"><b>{{x.registration_no or 'Auto nieprzypisane'}}</b><br>{{x.driver_name or 'Kierowca nieprzypisany'}} · {{x.customer_name}}<br><span class="muted">{{x.time_from or 'bez godziny'}} · stanowisko {{x.bay_code or '—'}} · {{labels[x.status]}}</span></div>{% else %}<div class="muted">Brak pojazdów.</div>{% endfor %}</section>{% endfor %}</div>{% endblock %}'''
