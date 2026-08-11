"""Operacyjna obsługa zakładu: awizacje i kolejka załadunkowa.

Moduł celowo nie steruje stanem magazynowym ani nie odczytuje wagi. Zachowuje
etapy zakładowe, aby później można było bezpiecznie dołączyć urządzenie wagi.
"""
import math
import secrets
import time
from datetime import datetime

from flask import Blueprint, abort, current_app, redirect, render_template_string, request, session, url_for

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
def _next_sequence(c,key,table,column,prefix):
    c.execute('CREATE TABLE IF NOT EXISTS document_sequences(sequence_key TEXT PRIMARY KEY,value INTEGER NOT NULL)')
    current=c.execute(f"SELECT COALESCE(MAX(CAST(substr({column},?) AS INTEGER)),0) FROM {table} WHERE {column} LIKE ?",(len(prefix)+1,prefix+'%')).fetchone()[0]
    c.execute('INSERT INTO document_sequences(sequence_key,value) VALUES(?,?) ON CONFLICT(sequence_key) DO NOTHING',(key,int(current or 0)))
    return int(c.execute('UPDATE document_sequences SET value=value+1 WHERE sequence_key=? RETURNING value',(key,)).fetchone()[0])
def _number(c):
    year = _now()[:4]
    n = _next_sequence(c,f'appointment:{year}','dispatch_appointments','appointment_no',f'AW/{year}/')
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
    try:
        D['pull_shared_tables_from_supabase'](force=True)
    except Exception:
        current_app.logger.exception('Nie udało się odświeżyć zamówień przed planowaniem transportów')
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
            planned_departure_time = (request.form.get("time_from") or "").strip()
            allow_resource_conflict = request.form.get("allow_resource_conflict") == "1"
            conflict_reason = (request.form.get("conflict_reason") or "").strip()
            if allow_resource_conflict and not conflict_reason:
                return 'Podaj powód wyjątku od blokady kierowcy lub auta.', 400
            if transport_id:
                selected_transport=c.execute('''SELECT t.id,t.wz_id,t.driver_id,t.vehicle_id,w.order_id
                    FROM transports t JOIN wz_documents w ON w.id=t.wz_id
                    WHERE t.id=? AND t.deleted_at IS NULL AND t.status NOT IN ('returned','closed')''',(transport_id,)).fetchone()
                if not selected_transport:
                    return 'Wybrany transport nie jest już aktywny.',409
                if int(selected_transport['order_id']) != order_id:
                    return 'Wybrany transport należy do innego zamówienia.',400
                if wz_id and int(selected_transport['wz_id']) != wz_id:
                    return 'Wybrany transport należy do innego dokumentu WZ.',400
                if c.execute("SELECT 1 FROM dispatch_appointments WHERE transport_id=? AND status<>'cancelled' LIMIT 1",(transport_id,)).fetchone():
                    return 'Ten transport jest już dodany do harmonogramu.',409
                wz_id=int(selected_transport['wz_id'])
                driver_id=int(selected_transport['driver_id'] or driver_id or 0)
                vehicle_id=int(selected_transport['vehicle_id'] or vehicle_id or 0)
            if not wz_id:
                return 'Wybierz dokument WZ, z którego ma powstać transport.', 400
            if not planned_departure_time:
                return 'Podaj planowaną godzinę wyjazdu.', 400
            try:
                planned_departure = datetime.fromisoformat(f"{planned_date}T{planned_departure_time}")
            except ValueError:
                return 'Podaj prawidłową datę i godzinę wyjazdu.', 400
            if driver_id:
                driver_conflicts = c.execute("""SELECT a.planned_date,a.time_from,o.order_no
                    FROM dispatch_appointments a
                    JOIN orders o ON o.id=a.order_id
                    LEFT JOIN transports t ON t.id=a.transport_id
                    WHERE a.driver_id=? AND a.status<>'cancelled' AND COALESCE(a.time_from,'')<>''
                      AND COALESCE(t.status,'assigned') NOT IN ('returned','cancelled')""",
                    (driver_id,)).fetchall()
                for conflict in driver_conflicts:
                    try:
                        other_departure = datetime.fromisoformat(f"{conflict['planned_date']}T{conflict['time_from']}")
                    except (TypeError, ValueError):
                        continue
                    if abs((planned_departure - other_departure).total_seconds()) < 2 * 60 * 60 and not allow_resource_conflict:
                        return (f"Kierowca ma już kurs {conflict['order_no']} o {conflict['time_from']}. "
                                "Między planowanymi wyjazdami muszą być co najmniej 2 godziny.", 409)
            if vehicle_id:
                vehicle_conflicts = c.execute("""SELECT a.planned_date,a.time_from,o.order_no
                    FROM dispatch_appointments a
                    JOIN orders o ON o.id=a.order_id
                    LEFT JOIN transports t ON t.id=a.transport_id
                    WHERE a.vehicle_id=? AND a.status<>'cancelled' AND COALESCE(a.time_from,'')<>''
                      AND COALESCE(t.status,'assigned') NOT IN ('returned','cancelled')""",
                    (vehicle_id,)).fetchall()
                for conflict in vehicle_conflicts:
                    try:
                        other_departure = datetime.fromisoformat(f"{conflict['planned_date']}T{conflict['time_from']}")
                    except (TypeError, ValueError):
                        continue
                    if abs((planned_departure - other_departure).total_seconds()) < 2 * 60 * 60 and not allow_resource_conflict:
                        return (f"Auto ma już kurs {conflict['order_no']} o {conflict['time_from']}. "
                                "Między planowanymi wyjazdami tego samego auta muszą być co najmniej 2 godziny.", 409)
            if not transport_id and (not driver_id or not vehicle_id):
                return 'Aby utworzyć transport z WZ, wybierz kierowcę i auto.', 400
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
                    number = _next_sequence(c,f'transport:{year}','transports','transport_no',f'TR/{year}/')
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
            appointment_notes = request.form.get("notes", "").strip()
            if allow_resource_conflict:
                exception_note = f"WYJĄTEK OD BLOKADY 2H ({_actor()}): {conflict_reason}"
                appointment_notes = f"{appointment_notes}\n{exception_note}".strip()
            c.execute("""INSERT INTO dispatch_appointments(appointment_no,order_id,wz_id,transport_id,driver_id,vehicle_id,loading_bay_id,planned_date,time_from,time_to,shift,queue_position,status,notes,created_by,updated_by,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'waiting',?,?,?,?,?)""", (_number(c), order_id, wz_id or None, transport_id or None, driver_id or None, vehicle_id or None, request.form.get("loading_bay_id") or None, planned_date, request.form.get("time_from") or None, planned_delivery_time or None, request.form.get("shift") or None, position, appointment_notes, _actor(), _actor(), now, now))
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
        # Zamówienie pozostaje dostępne, dopóki ma co najmniej jedno własne WZ
        # bez przypisanego transportu. Nie ukrywamy całego zamówienia tylko
        # dlatego, że inne jego WZ zostało już przydzielone.
        orders = c.execute("""SELECT o.id,o.order_no,o.customer_name,COALESCE(o.delivery_date,'') delivery_date,COALESCE(o.delivery_time,'') delivery_time,
            COALESCE((SELECT SUM(oi.qty) FROM order_items oi WHERE oi.order_id=o.id),0) AS total_m3,
            MAX(1, CAST((COALESCE((SELECT SUM(oi.qty) FROM order_items oi WHERE oi.order_id=o.id),0)+7.999999)/8 AS INTEGER)) AS required_trips
          FROM orders o
          WHERE lower(COALESCE(o.status,'')) NOT IN ('cancelled','issued','invoiced','completed')
            AND EXISTS (
              SELECT 1 FROM wz_documents w WHERE w.order_id=o.id AND w.deleted_at IS NULL
                AND w.status NOT IN ('ready_invoice','invoiced','returned','completed')
                AND NOT EXISTS (SELECT 1 FROM transports assigned_transport
                  WHERE assigned_transport.wz_id=w.id AND assigned_transport.deleted_at IS NULL)
            )
          ORDER BY o.id DESC LIMIT 300""").fetchall()
        wzs = c.execute("""SELECT w.id,w.wz_no,w.order_id,o.order_no FROM wz_documents w
          JOIN orders o ON o.id=w.order_id
          WHERE w.deleted_at IS NULL AND w.status NOT IN ('ready_invoice','invoiced','returned','completed')
            AND NOT EXISTS (SELECT 1 FROM transports assigned_transport
              WHERE assigned_transport.wz_id=w.id AND assigned_transport.deleted_at IS NULL)
          ORDER BY w.id DESC LIMIT 300""").fetchall()
        transports = c.execute("""SELECT t.id,t.transport_no,t.wz_id,w.wz_no,o.order_no
          FROM transports t JOIN wz_documents w ON w.id=t.wz_id JOIN orders o ON o.id=w.order_id
          WHERE t.deleted_at IS NULL AND t.status NOT IN ('returned','closed')
            AND NOT EXISTS (SELECT 1 FROM dispatch_appointments a WHERE a.transport_id=t.id AND a.status<>'cancelled')
          ORDER BY t.id DESC LIMIT 300""").fetchall()
        drivers = c.execute("SELECT id,name FROM drivers WHERE active=1 AND deleted_at IS NULL ORDER BY name").fetchall()
        driver_busy = [dict(x) for x in c.execute("""SELECT a.driver_id,a.planned_date,a.time_from,o.order_no
          FROM dispatch_appointments a JOIN orders o ON o.id=a.order_id
          LEFT JOIN transports t ON t.id=a.transport_id
          WHERE a.driver_id IS NOT NULL AND a.status<>'cancelled' AND COALESCE(a.time_from,'')<>''
            AND COALESCE(t.status,'assigned') NOT IN ('returned','cancelled')""").fetchall()]
        vehicle_busy = [dict(x) for x in c.execute("""SELECT a.vehicle_id,a.planned_date,a.time_from,o.order_no
          FROM dispatch_appointments a JOIN orders o ON o.id=a.order_id
          LEFT JOIN transports t ON t.id=a.transport_id
          WHERE a.vehicle_id IS NOT NULL AND a.status<>'cancelled' AND COALESCE(a.time_from,'')<>''
            AND COALESCE(t.status,'assigned') NOT IN ('returned','cancelled')""").fetchall()]
        vehicles = c.execute("SELECT id,registration_no FROM vehicles WHERE active=1 AND deleted_at IS NULL ORDER BY registration_no").fetchall()
        bays = c.execute("SELECT * FROM loading_bays WHERE active=1 ORDER BY code").fetchall()
    dispatch_tpl = TPL.replace('<label>Istniejący transport (opcjonalnie)</label>', '<label>Ilość na nowy transport [m³]</label><input type="number" name="transport_qty" min="0.01" max="8" step="0.01" value="8" required><label>Istniejący transport (opcjonalnie)</label>')
    return render_template_string(dispatch_tpl, rows=rows, orders=orders, wzs=wzs, transports=transports, drivers=drivers, driver_busy=driver_busy, vehicles=vehicles, vehicle_busy=vehicle_busy, bays=bays, day=day, labels=STAGE_LABEL, capacity_notice=request.args.get("capacity_notice", ""), title="Wydaj transport", base_url=D["BASE_URL"], db_path=D["DB_PATH"])

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
<div class="card"><h2>Utwórz transport z WZ i dodaj awizację</h2>{% if capacity_notice %}<div class="notice warn"><b>Podział transportu:</b> {{capacity_notice}}</div>{% endif %}<form method="post" class="grid3"><input type="hidden" name="planned_date" value="{{day}}"><div><label>Zamówienie</label><select name="order_id" required><option value="">Wybierz</option>{% for x in orders %}<option value="{{x.id}}" data-delivery-date="{{x.delivery_date}}">{{x.order_no}} · {{x.customer_name}} · {{'%.2f'|format(x.total_m3)|replace('.', ',')}} m³ · min. {{x.required_trips}} podjazd(y)</option>{% endfor %}</select></div><div><label>Planowany wyjazd</label><input name="time_from" type="time" required></div><div><label>Planowana dostawa</label><input name="time_to" type="time"></div><div><label>Zmiana</label><input name="shift" placeholder="np. I"></div><div><label>WZ bez transportu</label><select name="wz_id" required><option value="">Najpierw wybierz zamówienie</option>{% for x in wzs %}<option value="{{x.id}}" data-order-id="{{x.order_id}}">{{x.wz_no}}</option>{% endfor %}</select></div><div><label>Istniejący transport (opcjonalnie)</label><select name="transport_id"><option value="">Utwórz nowy z wybranego WZ</option>{% for x in transports %}<option value="{{x.id}}">{{x.transport_no}}</option>{% endfor %}</select></div><div><label>Stanowisko</label><select name="loading_bay_id"><option value="">—</option>{% for x in bays %}<option value="{{x.id}}">{{x.code}}</option>{% endfor %}</select></div><div><label>Kierowca</label><select name="driver_id" required><option value="">Wybierz kierowcę</option>{% for x in drivers %}<option value="{{x.id}}">{{x.name}}</option>{% endfor %}</select><small class="muted" id="driver-availability-note">Wybierz godzinę, aby sprawdzić dostępność.</small></div><div><label>Auto</label><select name="vehicle_id" required><option value="">Wybierz auto</option>{% for x in vehicles %}<option value="{{x.id}}">{{x.registration_no}}</option>{% endfor %}</select></div><div style="align-self:end"><button class="btn primary">Utwórz transport i dodaj awizację</button></div></form></div>
<div class="card"><table><thead><tr><th>Wyjazd / dostawa</th><th>Awizacja / klient</th><th>Auto / kierowca</th><th>Stanowisko</th><th>Etap transportu</th><th>Kolejka</th><th>Zmień etap zakładowy</th></tr></thead><tbody>{% for x in rows %}<tr><td><b>Wyjazd: {{x.time_from or '—'}}</b><br><b>Dostawa: {{x.time_to or '—'}}</b><br><span class="muted">{{x.shift or ''}}</span></td><td><b>{{x.appointment_no}}</b><br>{{x.customer_name}}<br><span class="muted">{{x['items'] or 'Pozycje WZ po utworzeniu'}}</span></td><td>{{x.registration_no or '—'}}<br>{{x.driver_name or '—'}}</td><td>{{x.bay_code or '—'}}</td><td><span class="badge">{{x.display_stage}}</span></td><td><form method="post" action="{{url_for('dispatch.appointment_move',appointment_id=x.id)}}"><input type="hidden" name="day" value="{{day}}"><button name="direction" value="up" class="btn">↑</button><button name="direction" value="down" class="btn">↓</button></form></td><td><form method="post" action="{{url_for('dispatch.appointment_status',appointment_id=x.id)}}"><input type="hidden" name="day" value="{{day}}"><select name="status"><option value="">Ustaw etap</option>{% for value,label in labels.items() %}<option value="{{value}}">{{label}}</option>{% endfor %}</select><input name="reason" placeholder="Powód tylko dla problemu/anulowania"><button class="btn primary">Zapisz</button></form></td></tr>{% else %}<tr><td colspan="7">Brak awizacji na ten dzień.</td></tr>{% endfor %}</tbody></table></div>
<script>
(() => {
  const busy = {{ driver_busy|tojson }};
  const vehicleBusy = {{ vehicle_busy|tojson }};
  const order = document.querySelector('select[name="order_id"]');
  const wz = document.querySelector('select[name="wz_id"]');
  const departure = document.querySelector('input[name="time_from"]');
  const driver = document.querySelector('select[name="driver_id"]');
  const vehicle = document.querySelector('select[name="vehicle_id"]');
  const note = document.getElementById('driver-availability-note');
  if (!order || !wz || !departure || !driver || !vehicle) return;
  const form = order.closest('form');
  const exceptionBox = document.createElement('div');
  exceptionBox.innerHTML = '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="allow_resource_conflict" value="1" style="width:auto"> Wyjątek od blokady 2 godzin</label><input name="conflict_reason" placeholder="Obowiązkowy powód wyjątku" style="display:none">';
  form.insertBefore(exceptionBox, form.lastElementChild);
  const override = exceptionBox.querySelector('[name="allow_resource_conflict"]');
  const overrideReason = exceptionBox.querySelector('[name="conflict_reason"]');
  const vehicleNote = document.createElement('small');
  vehicleNote.className = 'muted';
  vehicleNote.textContent = 'Wybierz godzinę, aby sprawdzić dostępność.';
  vehicle.insertAdjacentElement('afterend', vehicleNote);
  const refreshWz = () => {
    const orderId = order.value;
    let available = 0;
    [...wz.options].forEach((item, index) => {
      if (!index) return;
      const visible = Boolean(orderId) && item.dataset.orderId === orderId;
      item.hidden = !visible;
      item.disabled = !visible;
      if (visible) available++;
    });
    if (wz.selectedOptions[0] && wz.selectedOptions[0].disabled) wz.value = '';
    if (!wz.value && available === 1) {
      const only = [...wz.options].find((item, index) => index && !item.disabled);
      if (only) wz.value = only.value;
    }
    wz.options[0].textContent = !orderId ? 'Najpierw wybierz zamówienie' :
      (available ? 'Wybierz WZ' : 'Brak wolnego WZ dla tego zamówienia');
  };
  const refresh = () => {
    const option = order.options[order.selectedIndex];
    const date = (option && option.dataset.deliveryDate) || '{{day}}';
    const value = departure.value;
    let blocked = 0;
    [...driver.options].forEach((item, index) => {
      if (!index) return;
      if (!item.dataset.label) item.dataset.label = item.textContent;
      const clashes = value && busy.filter(x => String(x.driver_id) === item.value &&
        Math.abs(new Date(`${date}T${value}`).getTime() - new Date(`${x.planned_date}T${x.time_from}`).getTime()) < 7200000);
      const hasClash = Boolean(clashes && clashes.length);
      item.disabled = hasClash && !override.checked;
      item.textContent = item.dataset.label + (hasClash ? ` — konflikt (${clashes[0].time_from})` : '');
      if (hasClash) blocked++;
    });
    if (driver.selectedOptions[0] && driver.selectedOptions[0].disabled) driver.value = '';
    note.textContent = value ? `Niedostępni kierowcy: ${blocked}. Obowiązuje odstęp minimum 2 godziny.` : 'Wybierz godzinę, aby sprawdzić dostępność.';
    let blockedVehicles = 0;
    [...vehicle.options].forEach((item, index) => {
      if (!index) return;
      if (!item.dataset.label) item.dataset.label = item.textContent;
      const clashes = value && vehicleBusy.filter(x => String(x.vehicle_id) === item.value &&
        Math.abs(new Date(`${date}T${value}`).getTime() - new Date(`${x.planned_date}T${x.time_from}`).getTime()) < 7200000);
      const hasClash = Boolean(clashes && clashes.length);
      item.disabled = hasClash && !override.checked;
      item.textContent = item.dataset.label + (hasClash ? ` — konflikt (${clashes[0].time_from})` : '');
      if (hasClash) blockedVehicles++;
    });
    if (vehicle.selectedOptions[0] && vehicle.selectedOptions[0].disabled) vehicle.value = '';
    vehicleNote.textContent = value ? `Niedostępne auta: ${blockedVehicles}. Obowiązuje odstęp minimum 2 godziny.` : 'Wybierz godzinę, aby sprawdzić dostępność.';
  };
  order.addEventListener('change', () => { refreshWz(); refresh(); });
  departure.addEventListener('change', refresh);
  override.addEventListener('change', () => {
    overrideReason.style.display = override.checked ? '' : 'none';
    overrideReason.required = override.checked;
    if (!override.checked) overrideReason.value = '';
    refresh();
  });
  refreshWz();
  refresh();
})();
</script>{% endblock %}'''
QUEUE_TPL = '''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Kolejka załadunkowa</h1><a class="btn right" href="{{url_for('dispatch.appointments',day=day)}}">Awizacje</a></div><div class="card"><form method="get" class="flex"><input type="date" name="day" value="{{day}}"><button class="btn primary">Pokaż</button></form></div><div class="grid3">{% for key,title in [('waiting','Oczekują'),('weighing','Ważenie'),('loading','Załadunek'),('ready','Gotowe do wyjazdu')] %}<section class="card"><h2>{{title}}</h2>{% for x in groups[key] %}<div style="border-bottom:1px solid #edf0f5;padding:12px 0"><b>{{x.registration_no or 'Auto nieprzypisane'}}</b><br>{{x.driver_name or 'Kierowca nieprzypisany'}} · {{x.customer_name}}<br><span class="muted">{{x.time_from or 'bez godziny'}} · stanowisko {{x.bay_code or '—'}} · {{labels[x.status]}}</span></div>{% else %}<div class="muted">Brak pojazdów.</div>{% endfor %}</section>{% endfor %}</div>{% endblock %}'''
