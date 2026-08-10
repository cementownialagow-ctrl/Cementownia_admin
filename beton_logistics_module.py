from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template_string, request, session, url_for

bp=Blueprint('beton',__name__,url_prefix='/beton')
driver_api=Blueprint('driver_api',__name__,url_prefix='/api/driver')
D={}

def register_beton_logistics(app,deps):
    global D; D=deps
    with D['conn']() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS drivers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,phone TEXT,email TEXT,
          active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS vehicles(
          id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,brand TEXT,model TEXT,registration_no TEXT NOT NULL UNIQUE,
          trailer_no TEXT,year INTEGER,vin TEXT,current_mileage REAL NOT NULL DEFAULT 0,driver_id INTEGER REFERENCES drivers(id),
          active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS wz_documents(
          id INTEGER PRIMARY KEY AUTOINCREMENT,wz_no TEXT NOT NULL UNIQUE,order_id INTEGER NOT NULL REFERENCES orders(id),
          invoice_id INTEGER REFERENCES invoices(id),issue_location TEXT NOT NULL,warehouse_location TEXT NOT NULL,
          destination TEXT,status TEXT NOT NULL DEFAULT 'created',created_by TEXT NOT NULL,issued_by TEXT,
          ready_by TEXT,invoiced_by TEXT,created_at TEXT NOT NULL,issued_at TEXT,ready_at TEXT,invoiced_at TEXT,
          notes TEXT DEFAULT '',deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS wz_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,wz_id INTEGER NOT NULL REFERENCES wz_documents(id) ON DELETE CASCADE,
          order_item_id INTEGER NOT NULL REFERENCES order_items(id),product_id INTEGER NOT NULL REFERENCES products(id),
          sku TEXT NOT NULL,qty_planned REAL NOT NULL CHECK(qty_planned>0),qty_issued REAL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS transports(
          id INTEGER PRIMARY KEY AUTOINCREMENT,transport_no TEXT NOT NULL UNIQUE,invoice_id INTEGER REFERENCES invoices(id),wz_id INTEGER REFERENCES wz_documents(id),
          driver_id INTEGER NOT NULL REFERENCES drivers(id),vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),destination TEXT,
          status TEXT NOT NULL DEFAULT 'assigned',issued_at TEXT,departed_at TEXT,delivered_at TEXT,returned_at TEXT,
          receiver_name TEXT,driver_notes TEXT,created_by TEXT NOT NULL,updated_by TEXT NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS transport_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
          invoice_allocation_id INTEGER REFERENCES invoice_allocations(id),wz_item_id INTEGER REFERENCES wz_items(id),qty REAL NOT NULL CHECK(qty>0),created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS delivery_photos(
          id INTEGER PRIMARY KEY AUTOINCREMENT,transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
          storage_ref TEXT NOT NULL,photo_type TEXT NOT NULL DEFAULT 'delivery',caption TEXT,created_by TEXT NOT NULL,created_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS audit_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,
          entity_id INTEGER,details_json TEXT,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_transports_invoice ON transports(invoice_id);
        CREATE INDEX IF NOT EXISTS idx_wz_status ON wz_documents(status,created_at);
        CREATE INDEX IF NOT EXISTS idx_transports_driver_status ON transports(driver_id,status);
        CREATE INDEX IF NOT EXISTS idx_transport_items_transport ON transport_items(transport_id);
        ''')
        transport_cols={r[1]:r for r in c.execute('PRAGMA table_info(transports)')}
        if 'wz_id' not in transport_cols:c.execute('ALTER TABLE transports ADD COLUMN wz_id INTEGER REFERENCES wz_documents(id)')
        item_cols={r[1]:r for r in c.execute('PRAGMA table_info(transport_items)')}
        if 'wz_item_id' not in item_cols:c.execute('ALTER TABLE transport_items ADD COLUMN wz_item_id INTEGER REFERENCES wz_items(id)')
        # Starsza wersja wymagała faktury przed transportem. Migracja odwraca obieg na WZ -> transport -> FV.
        transport_cols={r[1]:r for r in c.execute('PRAGMA table_info(transports)')}
        if transport_cols.get('invoice_id') and int(transport_cols['invoice_id'][3] or 0)==1:
            c.execute('PRAGMA foreign_keys=OFF')
            c.executescript('''
            ALTER TABLE transport_items RENAME TO transport_items_invoice_first_backup;
            ALTER TABLE delivery_photos RENAME TO delivery_photos_invoice_first_backup;
            ALTER TABLE transports RENAME TO transports_invoice_first_backup;
            CREATE TABLE transports(
              id INTEGER PRIMARY KEY AUTOINCREMENT,transport_no TEXT NOT NULL UNIQUE,invoice_id INTEGER REFERENCES invoices(id),
              wz_id INTEGER REFERENCES wz_documents(id),driver_id INTEGER NOT NULL REFERENCES drivers(id),
              vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),destination TEXT,status TEXT NOT NULL DEFAULT 'assigned',
              issued_at TEXT,departed_at TEXT,delivered_at TEXT,returned_at TEXT,receiver_name TEXT,driver_notes TEXT,
              created_by TEXT NOT NULL,updated_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
            INSERT INTO transports(id,transport_no,invoice_id,wz_id,driver_id,vehicle_id,destination,status,issued_at,departed_at,delivered_at,returned_at,receiver_name,driver_notes,created_by,updated_by,created_at,updated_at,deleted_at)
              SELECT id,transport_no,invoice_id,wz_id,driver_id,vehicle_id,destination,status,issued_at,departed_at,delivered_at,returned_at,receiver_name,driver_notes,created_by,updated_by,created_at,updated_at,deleted_at FROM transports_invoice_first_backup;
            CREATE TABLE transport_items(
              id INTEGER PRIMARY KEY AUTOINCREMENT,transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
              invoice_allocation_id INTEGER REFERENCES invoice_allocations(id),wz_item_id INTEGER REFERENCES wz_items(id),
              qty REAL NOT NULL CHECK(qty>0),created_at TEXT NOT NULL);
            INSERT INTO transport_items(id,transport_id,invoice_allocation_id,wz_item_id,qty,created_at)
              SELECT id,transport_id,invoice_allocation_id,wz_item_id,qty,created_at FROM transport_items_invoice_first_backup;
            CREATE TABLE delivery_photos(
              id INTEGER PRIMARY KEY AUTOINCREMENT,transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
              storage_ref TEXT NOT NULL,photo_type TEXT NOT NULL DEFAULT 'delivery',caption TEXT,created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,deleted_at TEXT);
            INSERT INTO delivery_photos SELECT * FROM delivery_photos_invoice_first_backup;
            DROP TABLE transport_items_invoice_first_backup;
            DROP TABLE delivery_photos_invoice_first_backup;
            DROP TABLE transports_invoice_first_backup;
            CREATE INDEX IF NOT EXISTS idx_transports_invoice ON transports(invoice_id);
            CREATE INDEX IF NOT EXISTS idx_transports_wz ON transports(wz_id);
            CREATE INDEX IF NOT EXISTS idx_transports_driver_status ON transports(driver_id,status);
            CREATE INDEX IF NOT EXISTS idx_transport_items_transport ON transport_items(transport_id);
            ''')
            c.execute('PRAGMA foreign_keys=ON')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transports_wz ON transports(wz_id)')
    app.register_blueprint(bp)
    app.register_blueprint(driver_api)

def stamp(): return D['now_iso']()
def actor(): return session.get('display_name') or session.get('username') or 'kierowca'
def next_no(c):
    year=stamp()[:4]; n=c.execute("SELECT COUNT(*) FROM transports WHERE transport_no LIKE ?",(f'TR/{year}/%',)).fetchone()[0]+1
    return f'TR/{year}/{n:05d}'
def next_wz_no(c):
    year=stamp()[:4]; n=c.execute("SELECT COUNT(*) FROM wz_documents WHERE wz_no LIKE ?",(f'WZ/{year}/%',)).fetchone()[0]+1
    return f'WZ/{year}/{n:05d}'

@bp.get('/wz')
def wz_list():
    with D['conn']() as c:
        rows=c.execute('''SELECT w.*,o.customer_name,i.invoice_no,
          (SELECT t.transport_no FROM transports t WHERE t.wz_id=w.id AND t.deleted_at IS NULL ORDER BY t.id DESC LIMIT 1) transport_no
          FROM wz_documents w JOIN orders o ON o.id=w.order_id LEFT JOIN invoices i ON i.id=w.invoice_id
          WHERE w.deleted_at IS NULL ORDER BY w.id DESC''').fetchall()
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Dokumenty WZ</h1><a class="btn primary right" href="{{url_for('beton.wz_new')}}">+ Wystaw WZ</a></div><div class="card"><table><thead><tr><th>WZ</th><th>Klient</th><th>Miejsca</th><th>Status</th><th>Transport</th><th>Faktura</th></tr></thead><tbody>{% for x in rows %}<tr><td><a href="{{url_for('beton.wz_view',wz_id=x.id)}}"><b>{{x.wz_no}}</b></a><br><span class="muted">{{x.created_at}}</span></td><td>{{x.customer_name}}</td><td>{{x.issue_location}} → {{x.warehouse_location}}</td><td><span class="badge">{{x.status}}</span></td><td>{{x.transport_no or '—'}}</td><td>{{x.invoice_no or '—'}}</td></tr>{% else %}<tr><td colspan="6">Brak dokumentów WZ.</td></tr>{% endfor %}</tbody></table></div>{% endblock %}''',rows=rows,base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.route('/wz/new',methods=['GET','POST'])
def wz_new():
    order_id=int(request.values.get('order_id') or 0)
    with D['conn']() as c:
        orders=c.execute("SELECT id,order_no,customer_name,created_at FROM orders WHERE lower(status) NOT IN ('cancelled') ORDER BY id DESC LIMIT 300").fetchall()
        order=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone() if order_id else None
        items=c.execute('''SELECT oi.*,COALESCE(p.name,p.sku) product_name,COALESCE((SELECT SUM(wi.qty_planned) FROM wz_items wi JOIN wz_documents wd ON wd.id=wi.wz_id WHERE wi.order_item_id=oi.id AND wd.deleted_at IS NULL),0) wz_reserved FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=? ORDER BY oi.id''',(order_id,)).fetchall() if order else []
        if request.method=='POST':
            if not order:abort(400)
            s=stamp(); cur=c.execute('''INSERT INTO wz_documents(wz_no,order_id,issue_location,warehouse_location,destination,status,created_by,created_at,notes)
              VALUES(?,?,?,?,?,'created',?,?,?)''',(next_wz_no(c),order_id,request.form.get('issue_location','Miejscowość X').strip(),request.form.get('warehouse_location','Miejscowość Y').strip(),request.form.get('destination','').strip(),actor(),s,request.form.get('notes','').strip()))
            wz_id=cur.lastrowid; count=0
            for item in items:
                qty=float(request.form.get(f'qty_{item["id"]}') or 0)
                if qty>0 and qty<=float(item['qty'])-float(item['wz_reserved']):
                    c.execute('INSERT INTO wz_items(wz_id,order_item_id,product_id,sku,qty_planned,created_at) VALUES(?,?,?,?,?,?)',(wz_id,item['id'],item['product_id'],item['sku'],qty,s)); count+=1
            if not count:raise ValueError('WZ musi zawierać co najmniej jedną pozycję')
            return redirect(url_for('beton.wz_view',wz_id=wz_id))
    return render_template_string('''{% extends "base.html" %}{% block content %}<h1>Nowy dokument WZ</h1><div class="card"><form method="get"><label>Zamówienie klienta</label><select name="order_id" onchange="this.form.submit()"><option value="">Wybierz zamówienie</option>{% for x in orders %}<option value="{{x.id}}" {{'selected' if x.id==order_id}}>{{x.order_no}} · {{x.customer_name}}</option>{% endfor %}</select></form></div>{% if order %}<form method="post" class="card"><input type="hidden" name="order_id" value="{{order.id}}"><h2>{{order.order_no}} · {{order.customer_name}}</h2><div class="grid3"><div><label>Wystawiono w</label><input name="issue_location" value="Miejscowość X" required></div><div><label>Magazyn wydający</label><input name="warehouse_location" value="Miejscowość Y" required></div><div><label>Miejsce dostawy</label><input name="destination"></div></div><table><thead><tr><th>Materiał</th><th>Zamówiono</th><th>Już na WZ</th><th>Na nowym WZ</th></tr></thead><tbody>{% for x in items %}{% set available=x.qty-x.wz_reserved %}<tr><td>{{x.product_name}}<br><span class="muted">{{x.sku}}</span></td><td>{{x.qty}}</td><td>{{x.wz_reserved}}</td><td><input type="number" min="0" max="{{available}}" step="0.01" name="qty_{{x.id}}" value="{{available}}"></td></tr>{% endfor %}</tbody></table><label>Uwagi</label><textarea name="notes"></textarea><button class="btn primary">Wystaw WZ</button></form>{% endif %}{% endblock %}''',orders=orders,order=order,order_id=order_id,items=items,base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.get('/wz/<int:wz_id>')
def wz_view(wz_id):
    with D['conn']() as c:
        w=c.execute('''SELECT w.*,o.customer_name,o.order_no,i.invoice_no FROM wz_documents w JOIN orders o ON o.id=w.order_id LEFT JOIN invoices i ON i.id=w.invoice_id WHERE w.id=? AND w.deleted_at IS NULL''',(wz_id,)).fetchone()
        if not w:abort(404)
        items=c.execute('SELECT * FROM wz_items WHERE wz_id=? ORDER BY id',(wz_id,)).fetchall()
        transport=c.execute('SELECT * FROM transports WHERE wz_id=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1',(wz_id,)).fetchone()
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>{{w.wz_no}}</h1><span class="badge">{{w.status}}</span><a class="btn right" target="_blank" href="{{url_for('beton.wz_print',wz_id=w.id)}}">Drukuj WZ</a></div><div class="card"><div class="grid3"><div><span class="muted">Klient</span><br><b>{{w.customer_name}}</b></div><div><span class="muted">Wystawiono / magazyn</span><br>{{w.issue_location}} → {{w.warehouse_location}}</div><div><span class="muted">Dostawa</span><br>{{w.destination or '—'}}</div></div><div class="line"></div><table><thead><tr><th>Materiał</th><th>Plan</th><th>Wydano</th></tr></thead><tbody>{% for x in items %}<tr><td>{{x.sku}}</td><td>{{x.qty_planned}}</td><td>{{x.qty_issued if x.qty_issued is not none else '—'}}</td></tr>{% endfor %}</tbody></table><div class="flex" style="margin-top:16px">{% if w.status=='created' %}<form method="post" action="{{url_for('beton.wz_issue',wz_id=w.id)}}"><button class="btn primary">Potwierdź wydanie w {{w.warehouse_location}}</button></form>{% elif w.status=='issued' and not transport %}<a class="btn primary" href="{{url_for('beton.transport_new',wz_id=w.id)}}">Przypisz kierowcę i auto</a>{% elif w.status=='returned' %}<form method="post" action="{{url_for('beton.wz_ready',wz_id=w.id)}}"><button class="btn primary">Podpisane WZ — gotowe do faktury VAT</button></form>{% elif w.status=='ready_invoice' %}<a class="btn primary" href="{{url_for('order_invoice',order_id=w.order_id,wz_id=w.id)}}">Wystaw fakturę VAT</a>{% elif w.status=='invoiced' %}<span class="badge">Zafakturowano: {{w.invoice_no}}</span><a class="btn" href="{{url_for('invoice_download_admin',invoice_id=w.invoice_id)}}">Pobierz fakturę</a>{% endif %}{% if transport %}<a class="btn" href="{{url_for('beton.transport_view',transport_id=transport.id)}}">Transport {{transport.transport_no}}</a>{% endif %}</div></div><div class="card"><h2>Podpisy czynności</h2><table><tr><th>Wystawił WZ</th><td>{{w.created_by}} · {{w.created_at}}</td></tr><tr><th>Wydał towar</th><td>{{w.issued_by or '—'}} {{w.issued_at or ''}}</td></tr><tr><th>Gotowość do FV</th><td>{{w.ready_by or '—'}} {{w.ready_at or ''}}</td></tr><tr><th>Wystawił FV</th><td>{{w.invoiced_by or '—'}} {{w.invoiced_at or ''}}</td></tr></table></div>{% endblock %}''',w=w,items=items,transport=transport,base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.get('/wz/<int:wz_id>/print')
def wz_print(wz_id):
    with D['conn']() as c:
        w=c.execute('''SELECT w.*,o.customer_name,o.customer_address FROM wz_documents w JOIN orders o ON o.id=w.order_id WHERE w.id=? AND w.deleted_at IS NULL''',(wz_id,)).fetchone()
        if not w:abort(404)
        items=c.execute('SELECT sku,qty_planned,qty_issued FROM wz_items WHERE wz_id=? ORDER BY id',(wz_id,)).fetchall()
    return render_template_string('''<!doctype html><html lang="pl"><meta charset="utf-8"><title>{{w.wz_no}}</title><style>body{font:14px Arial,sans-serif;max-width:900px;margin:35px auto;color:#111}h1{margin-bottom:4px}table{border-collapse:collapse;width:100%;margin:22px 0}th,td{border:1px solid #333;padding:9px;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.sign{display:grid;grid-template-columns:1fr 1fr;gap:70px;margin-top:70px}.line{border-top:1px solid #111;padding-top:7px;text-align:center}@media print{button{display:none}}</style><button onclick="print()">Drukuj</button><h1>Wydanie zewnętrzne {{w.wz_no}}</h1><p>Data: {{w.created_at[:10]}} · Status: {{w.status}}</p><div class="grid"><div><b>Nabywca / odbiorca</b><br>{{w.customer_name}}<br>{{w.customer_address or ''}}</div><div><b>Wydanie</b><br>{{w.issue_location}} → {{w.warehouse_location}}<br>Dostawa: {{w.destination or '—'}}</div></div><table><thead><tr><th>Materiał</th><th>Ilość</th></tr></thead><tbody>{% for x in items %}<tr><td>{{x.sku}}</td><td>{{x.qty_issued if x.qty_issued is not none else x.qty_planned}}</td></tr>{% endfor %}</tbody></table><p>Uwagi: {{w.notes or '—'}}</p><div class="sign"><div class="line">Wydał: {{w.issued_by or ''}}</div><div class="line">Odebrał / podpis i pieczęć</div></div></html>''',w=w,items=items)

@bp.post('/wz/<int:wz_id>/issue')
def wz_issue(wz_id):
    s=stamp()
    with D['conn']() as c:
        w=c.execute("SELECT * FROM wz_documents WHERE id=? AND status='created'",(wz_id,)).fetchone()
        if not w:abort(409)
        c.execute('UPDATE wz_items SET qty_issued=qty_planned WHERE wz_id=?',(wz_id,))
        c.execute("UPDATE wz_documents SET status='issued',issued_by=?,issued_at=? WHERE id=?",(actor(),s,wz_id))
    return redirect(url_for('beton.wz_view',wz_id=wz_id))

@bp.post('/wz/<int:wz_id>/ready')
def wz_ready(wz_id):
    s=stamp()
    with D['conn']() as c:
        w=c.execute("SELECT * FROM wz_documents WHERE id=? AND status='returned'",(wz_id,)).fetchone()
        if not w:abort(409)
        c.execute("UPDATE wz_documents SET status='ready_invoice',ready_by=?,ready_at=? WHERE id=?",(actor(),s,wz_id))
    return redirect(url_for('beton.wz_view',wz_id=wz_id))

@bp.get('/drivers')
def drivers():
    with D['conn']() as c:
        ds=c.execute('SELECT * FROM drivers WHERE deleted_at IS NULL ORDER BY active DESC,name').fetchall()
        vs=c.execute('SELECT v.*,d.name driver_name FROM vehicles v LEFT JOIN drivers d ON d.id=v.driver_id WHERE v.deleted_at IS NULL ORDER BY v.active DESC,v.registration_no').fetchall()
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Kierowcy i pojazdy</h1></div><div class="row"><div class="card"><h2>Dodaj kierowcę</h2><form method="post" action="{{url_for('beton.driver_add')}}"><label>Imię i nazwisko</label><input name="name" required><label>Telefon</label><input name="phone"><label>E-mail / login</label><input name="email" type="email"><button class="btn primary" style="margin-top:12px">Dodaj kierowcę</button></form></div><div class="card"><h2>Dodaj pojazd</h2><form method="post" action="{{url_for('beton.vehicle_add')}}"><div class="row"><div><label>Numer rejestracyjny</label><input name="registration_no" required></div><div><label>Naczepa</label><input name="trailer_no"></div><div><label>Marka</label><input name="brand"></div><div><label>Model</label><input name="model"></div><div><label>Rok</label><input name="year" type="number"></div><div><label>VIN</label><input name="vin"></div><div><label>Przebieg</label><input name="current_mileage" type="number" value="0"></div><div><label>Domyślny kierowca</label><select name="driver_id"><option value="">—</option>{% for d in ds %}<option value="{{d.id}}">{{d.name}}</option>{% endfor %}</select></div></div><button class="btn primary" style="margin-top:12px">Dodaj pojazd</button></form></div></div><div class="card"><h2>Kierowcy</h2><table><thead><tr><th>Kierowca</th><th>Telefon</th><th>E-mail</th><th>Status</th></tr></thead><tbody>{% for x in ds %}<tr><td><b>{{x.name}}</b></td><td>{{x.phone or '-'}}</td><td>{{x.email or '-'}}</td><td><span class="badge">{{'Aktywny' if x.active else 'Nieaktywny'}}</span></td></tr>{% endfor %}</tbody></table></div><div class="card"><h2>Pojazdy</h2><table><thead><tr><th>Rejestracja</th><th>Marka / model</th><th>Naczepa</th><th>Kierowca</th><th>Przebieg</th></tr></thead><tbody>{% for x in vs %}<tr><td><b>{{x.registration_no}}</b></td><td>{{x.brand or ''}} {{x.model or ''}}</td><td>{{x.trailer_no or '-'}}</td><td>{{x.driver_name or '-'}}</td><td>{{x.current_mileage}}</td></tr>{% endfor %}</tbody></table></div>{% endblock %}''',ds=ds,vs=vs,title='Kierowcy i pojazdy',base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.post('/drivers/add')
def driver_add():
    s=stamp()
    with D['conn']() as c:c.execute('INSERT INTO drivers(name,phone,email,created_at,updated_at) VALUES(?,?,?,?,?)',(request.form['name'].strip(),request.form.get('phone','').strip(),request.form.get('email','').strip().lower(),s,s))
    return redirect(url_for('beton.drivers'))

@bp.post('/vehicles/add')
def vehicle_add():
    s=stamp()
    with D['conn']() as c:c.execute('INSERT INTO vehicles(name,brand,model,registration_no,trailer_no,year,vin,current_mileage,driver_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(request.form.get('name',''),request.form.get('brand',''),request.form.get('model',''),request.form['registration_no'].strip().upper(),request.form.get('trailer_no','').strip().upper(),request.form.get('year') or None,request.form.get('vin',''),request.form.get('current_mileage') or 0,request.form.get('driver_id') or None,s,s))
    return redirect(url_for('beton.drivers'))

@bp.get('/transports')
def transports():
    with D['conn']() as c:
        rows=c.execute('''SELECT t.*,w.wz_no,i.invoice_no,d.name driver_name,v.registration_no,o.customer_name
          FROM transports t JOIN wz_documents w ON w.id=t.wz_id JOIN orders o ON o.id=w.order_id LEFT JOIN invoices i ON i.id=w.invoice_id
          JOIN drivers d ON d.id=t.driver_id JOIN vehicles v ON v.id=t.vehicle_id
          WHERE t.deleted_at IS NULL ORDER BY t.id DESC''').fetchall()
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Transporty</h1><a class="btn primary right" href="{{url_for('beton.wz_list')}}">Wybierz wydane WZ</a></div><div class="card"><table><thead><tr><th>Transport</th><th>WZ</th><th>Klient</th><th>Kierowca / auto</th><th>Status</th><th>Faktura</th></tr></thead><tbody>{% for x in rows %}<tr><td><a href="{{url_for('beton.transport_view',transport_id=x.id)}}"><b>{{x.transport_no}}</b></a></td><td><a href="{{url_for('beton.wz_view',wz_id=x.wz_id)}}">{{x.wz_no}}</a></td><td>{{x.customer_name}}</td><td>{{x.driver_name}}<br>{{x.registration_no}}</td><td><span class="badge">{{x.status}}</span></td><td>{{x.invoice_no or '—'}}</td></tr>{% else %}<tr><td colspan="6">Brak transportów.</td></tr>{% endfor %}</tbody></table></div>{% endblock %}''',rows=rows,title='Transporty',base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.route('/transports/new',methods=['GET','POST'])
def transport_new():
    wz_id=int(request.values.get('wz_id') or 0)
    with D['conn']() as c:
        wz_rows=c.execute("""SELECT w.id,w.wz_no,o.customer_name FROM wz_documents w JOIN orders o ON o.id=w.order_id WHERE w.status='issued' AND w.deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM transports t WHERE t.wz_id=w.id AND t.deleted_at IS NULL) ORDER BY w.id DESC""").fetchall()
        ds=c.execute('SELECT * FROM drivers WHERE active=1 AND deleted_at IS NULL ORDER BY name').fetchall(); vs=c.execute('SELECT * FROM vehicles WHERE active=1 AND deleted_at IS NULL ORDER BY registration_no').fetchall()
        wz=c.execute("SELECT w.*,o.customer_name FROM wz_documents w JOIN orders o ON o.id=w.order_id WHERE w.id=? AND w.status='issued'",(wz_id,)).fetchone() if wz_id else None
        wz_items=c.execute('SELECT * FROM wz_items WHERE wz_id=? ORDER BY id',(wz_id,)).fetchall() if wz else []
        if request.method=='POST':
            if not wz:abort(400)
            if not ds or not vs:raise ValueError('Najpierw dodaj kierowcę i pojazd')
            s=stamp(); cur=c.execute("INSERT INTO transports(transport_no,wz_id,driver_id,vehicle_id,destination,status,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,'assigned',?,?,?,?)",(next_no(c),wz_id,request.form['driver_id'],request.form['vehicle_id'],request.form.get('destination','') or wz['destination'],actor(),actor(),s,s)); tid=cur.lastrowid
            for item in wz_items:c.execute('INSERT INTO transport_items(transport_id,wz_item_id,qty,created_at) VALUES(?,?,?,?)',(tid,item['id'],item['qty_issued'] or item['qty_planned'],s))
            c.execute('INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)',(actor(),'create','transport',tid,'{}',s))
            return redirect(url_for('beton.transport_view',transport_id=tid))
    return render_template_string('''{% extends "base.html" %}{% block content %}<h1>Transport z dokumentu WZ</h1><div class="card"><form method="get"><label>Wydane WZ</label><select name="wz_id" onchange="this.form.submit()"><option value="">Wybierz WZ</option>{% for x in wz_rows %}<option value="{{x.id}}" {{'selected' if wz_id==x.id}}>{{x.wz_no}} · {{x.customer_name}}</option>{% endfor %}</select></form></div>{% if wz %}<form method="post" class="card"><input type="hidden" name="wz_id" value="{{wz.id}}"><h2>{{wz.wz_no}} · {{wz.customer_name}}</h2><div class="row"><div><label>Kierowca</label><select name="driver_id" required>{% for x in ds %}<option value="{{x.id}}">{{x.name}}</option>{% endfor %}</select></div><div><label>Pojazd</label><select name="vehicle_id" required>{% for x in vs %}<option value="{{x.id}}">{{x.registration_no}}</option>{% endfor %}</select></div></div><label>Miejsce dostawy</label><input name="destination" value="{{wz.destination or ''}}"><table><thead><tr><th>Materiał</th><th>Ilość wydana</th></tr></thead><tbody>{% for x in wz_items %}<tr><td>{{x.sku}}</td><td>{{x.qty_issued or x.qty_planned}}</td></tr>{% endfor %}</tbody></table><button class="btn primary">Utwórz i przypisz transport</button></form>{% endif %}{% endblock %}''',wz_rows=wz_rows,wz_id=wz_id,wz=wz,wz_items=wz_items,ds=ds,vs=vs,base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.get('/transports/<int:transport_id>')
def transport_view(transport_id):
    with D['conn']() as c:
        x=c.execute('''SELECT t.*,w.wz_no,w.invoice_id,i.invoice_no,d.name driver_name,v.registration_no,o.customer_name FROM transports t JOIN wz_documents w ON w.id=t.wz_id LEFT JOIN invoices i ON i.id=w.invoice_id JOIN orders o ON o.id=w.order_id JOIN drivers d ON d.id=t.driver_id JOIN vehicles v ON v.id=t.vehicle_id WHERE t.id=?''',(transport_id,)).fetchone()
        if not x:abort(404)
        items=c.execute('SELECT ti.qty,w.sku FROM transport_items ti JOIN wz_items w ON w.id=ti.wz_item_id WHERE ti.transport_id=?',(transport_id,)).fetchall()
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>{{x.transport_no}}</h1><span class="badge">{{x.status}}</span><a class="btn right" href="{{url_for('beton.wz_view',wz_id=x.wz_id)}}">{{x.wz_no}}</a>{% if x.invoice_id %}<a class="btn" href="{{url_for('invoice_download_admin',invoice_id=x.invoice_id)}}">Pobierz fakturę</a>{% endif %}</div><div class="card"><div class="grid3"><div><span class="muted">Klient</span><br><b>{{x.customer_name}}</b></div><div><span class="muted">Kierowca</span><br><b>{{x.driver_name}}</b></div><div><span class="muted">Pojazd</span><br><b>{{x.registration_no}}</b></div></div><div class="line"></div><table><thead><tr><th>Materiał / SKU</th><th>Ilość</th></tr></thead><tbody>{% for i in items %}<tr><td>{{i.sku}}</td><td><b>{{i.qty}}</b></td></tr>{% endfor %}</tbody></table></div>{% endblock %}''',x=x,items=items,title=x['transport_no'],base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@driver_api.get('/transports')
def driver_transports_api():
    email=(g.client_user.get('email') or '').strip().lower()
    with D['conn']() as c:
        rows=c.execute('''SELECT t.id,t.transport_no,t.wz_id,w.wz_no,w.invoice_id,t.destination,t.status,t.issued_at,t.departed_at,t.delivered_at,t.returned_at,t.receiver_name,t.driver_notes,i.invoice_no,o.customer_name,v.registration_no FROM transports t JOIN drivers d ON d.id=t.driver_id JOIN wz_documents w ON w.id=t.wz_id JOIN orders o ON o.id=w.order_id LEFT JOIN invoices i ON i.id=w.invoice_id JOIN vehicles v ON v.id=t.vehicle_id WHERE lower(d.email)=? AND d.active=1 AND d.deleted_at IS NULL AND t.deleted_at IS NULL ORDER BY t.id DESC''',(email,)).fetchall()
        result=[]
        for r in rows:
            x=dict(r); x['items']=[dict(z) for z in c.execute('SELECT w.sku,ti.qty FROM transport_items ti JOIN wz_items w ON w.id=ti.wz_item_id WHERE ti.transport_id=?',(r['id'],))]; result.append(x)
    return jsonify(ok=True,transports=result)

@driver_api.post('/transports/<int:transport_id>/status')
def driver_transport_status_api(transport_id):
    email=(g.client_user.get('email') or '').strip().lower(); data=request.get_json(silent=True) or {}; status=str(data.get('status',''))
    allowed={'issued','in_transit','delivered','returned','problem'}
    if status not in allowed:return jsonify(ok=False,error='Niedozwolony status'),400
    field={'issued':'issued_at','in_transit':'departed_at','delivered':'delivered_at','returned':'returned_at'}.get(status)
    with D['conn']() as c:
        row=c.execute('SELECT t.id,t.status,t.wz_id FROM transports t JOIN drivers d ON d.id=t.driver_id WHERE t.id=? AND lower(d.email)=? AND d.active=1 AND t.deleted_at IS NULL',(transport_id,email)).fetchone()
        if not row:return jsonify(ok=False,error='Brak dostępu'),403
        transitions={'assigned':{'issued','problem'},'issued':{'in_transit','problem'},'in_transit':{'delivered','problem'},'delivered':{'returned','problem'},'problem':{'issued','in_transit','delivered','returned'}}
        if status not in transitions.get(row['status'],set()):return jsonify(ok=False,error='Nieprawidłowa kolejność statusów'),409
        sql='UPDATE transports SET status=?,driver_notes=?,receiver_name=?,updated_by=?,updated_at=?'+(f',{field}=?' if field else '')+' WHERE id=?'; values=[status,str(data.get('notes',''))[:2000],str(data.get('receiver_name',''))[:200],email,stamp()]
        if field:values.append(stamp())
        values.append(transport_id); c.execute(sql,values)
        if status=='returned':c.execute("UPDATE wz_documents SET status='returned' WHERE id=? AND status IN ('issued','in_transport')",(row['wz_id'],))
        elif status in {'issued','in_transit','delivered'}:c.execute("UPDATE wz_documents SET status='in_transport' WHERE id=? AND status='issued'",(row['wz_id'],))
        c.execute('INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)',(email,'status:'+status,'transport',transport_id,'{}',stamp()))
    return jsonify(ok=True,status=status)

@driver_api.get('/transports/<int:transport_id>/invoice')
def driver_invoice_api(transport_id):
    email=(g.client_user.get('email') or '').strip().lower()
    with D['conn']() as c: row=c.execute('SELECT w.invoice_id FROM transports t JOIN wz_documents w ON w.id=t.wz_id JOIN drivers d ON d.id=t.driver_id WHERE t.id=? AND lower(d.email)=? AND d.active=1 AND t.deleted_at IS NULL',(transport_id,email)).fetchone()
    if not row or not row['invoice_id']:abort(404)
    return current_app.view_functions['invoice_download_admin'](row['invoice_id'])
