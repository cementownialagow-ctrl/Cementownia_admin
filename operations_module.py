import csv, io, os
from datetime import date, datetime, timedelta
from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, url_for

bp=Blueprint('ops',__name__)
DB=None; NOW=None

def register_operations(app,db_factory,now_factory):
    global DB,NOW; DB=db_factory; NOW=now_factory
    with DB() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS material_usage(id INTEGER PRIMARY KEY AUTOINCREMENT,usage_date TEXT NOT NULL,material_id INTEGER NOT NULL REFERENCES products(id),qty REAL NOT NULL CHECK(qty>0),unit TEXT NOT NULL,unit_price REAL NOT NULL CHECK(unit_price>=0),total_cost REAL NOT NULL,department_id INTEGER REFERENCES departments(id),location TEXT DEFAULT '',entered_by TEXT NOT NULL,notes TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS fuel_entries(id INTEGER PRIMARY KEY AUTOINCREMENT,vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),entry_date TEXT NOT NULL,mileage REAL NOT NULL CHECK(mileage>=0),liters REAL NOT NULL CHECK(liters>0),price_per_liter REAL NOT NULL CHECK(price_per_liter>=0),total_cost REAL NOT NULL,fuel_type TEXT NOT NULL,driver_id INTEGER REFERENCES drivers(id),document_no TEXT DEFAULT '',notes TEXT DEFAULT '',created_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS expense_categories(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,group_code TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vehicle_expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),expense_date TEXT NOT NULL,category_id INTEGER NOT NULL REFERENCES expense_categories(id),description TEXT NOT NULL,net_cost REAL NOT NULL CHECK(net_cost>=0),vat_rate REAL NOT NULL DEFAULT 23,gross_cost REAL NOT NULL,mileage REAL DEFAULT 0,vendor TEXT DEFAULT '',document_no TEXT DEFAULT '',notes TEXT DEFAULT '',created_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_usage_date ON material_usage(usage_date); CREATE INDEX IF NOT EXISTS idx_fuel_date_vehicle ON fuel_entries(entry_date,vehicle_id); CREATE INDEX IF NOT EXISTS idx_expense_date_vehicle ON vehicle_expenses(expense_date,vehicle_id);
        ''')
        c.executemany('INSERT OR IGNORE INTO departments(name) VALUES(?)',[('Betoniarnia',),('Transport',),('Warsztat',),('Biuro',)])
        c.executemany('INSERT OR IGNORE INTO expense_categories(name,group_code) VALUES(?,?)',[('Części','parts'),('Naprawy','repairs'),('Serwis','service'),('Opony','tires'),('Przeglądy','inspection'),('Ubezpieczenie','insurance'),('Inne','other')])
        cols={r[1] for r in c.execute('PRAGMA table_info(vehicles)')}
        for name,sql in [('name','TEXT DEFAULT ""'),('brand','TEXT DEFAULT ""'),('model','TEXT DEFAULT ""'),('year','INTEGER'),('vin','TEXT DEFAULT ""'),('current_mileage','REAL NOT NULL DEFAULT 0')]:
            if name not in cols:c.execute(f'ALTER TABLE vehicles ADD COLUMN {name} {sql}')
    template_dir=os.path.join(os.path.dirname(__file__),'templates')
    for name in ('operations.html','analytics.html'):
        with open(os.path.join(template_dir,name),encoding='utf-8') as fh: app.jinja_loader.mapping[name]=fh.read()
    app.register_blueprint(bp)

def period():
    today=date.today(); kind=request.args.get('period','month')
    if request.args.get('date_from') and request.args.get('date_to'): return request.args['date_from'],request.args['date_to'],kind
    if kind=='day': start=today
    elif kind=='week': start=today-timedelta(days=today.weekday())
    elif kind=='quarter': start=date(today.year,((today.month-1)//3)*3+1,1)
    elif kind=='year': start=date(today.year,1,1)
    else:start=date(today.year,today.month,1)
    return start.isoformat(),today.isoformat(),kind

@bp.route('/operations',methods=['GET','POST'])
def operations():
    with DB() as c:
        if request.method=='POST':
            typ=request.form['type']; stamp=NOW(); user=session.get('display_name') or session.get('username') or 'Pracownik'
            if typ=='material':
                q=float(request.form['qty'].replace(',','.')); p=float(request.form['unit_price'].replace(',','.'))
                c.execute('INSERT INTO material_usage(usage_date,material_id,qty,unit,unit_price,total_cost,department_id,location,entered_by,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(request.form['entry_date'],request.form['material_id'],q,request.form['unit'],p,round(q*p,2),request.form.get('department_id') or None,request.form.get('location',''),user,request.form.get('notes',''),stamp,stamp))
            elif typ=='fuel':
                liters=float(request.form['liters'].replace(',','.')); price=float(request.form['price'].replace(',','.'))
                c.execute('INSERT INTO fuel_entries(vehicle_id,entry_date,mileage,liters,price_per_liter,total_cost,fuel_type,driver_id,document_no,notes,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(request.form['vehicle_id'],request.form['entry_date'],request.form['mileage'],liters,price,round(liters*price,2),request.form['fuel_type'],request.form.get('driver_id') or None,request.form.get('document_no',''),request.form.get('notes',''),user,stamp,stamp))
            elif typ=='expense':
                net=float(request.form['net_cost'].replace(',','.')); vat=float(request.form['vat_rate'].replace(',','.'))
                c.execute('INSERT INTO vehicle_expenses(vehicle_id,expense_date,category_id,description,net_cost,vat_rate,gross_cost,mileage,vendor,document_no,notes,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(request.form['vehicle_id'],request.form['entry_date'],request.form['category_id'],request.form['description'],net,vat,round(net*(1+vat/100),2),request.form.get('mileage') or 0,request.form.get('vendor',''),request.form.get('document_no',''),request.form.get('notes',''),user,stamp,stamp))
            return redirect(url_for('ops.operations'))
        materials=c.execute('SELECT * FROM products ORDER BY name,sku').fetchall(); vehicles=c.execute('SELECT * FROM vehicles WHERE active=1 ORDER BY registration_no').fetchall(); drivers=c.execute('SELECT * FROM drivers WHERE active=1 ORDER BY name').fetchall(); departments=c.execute('SELECT * FROM departments WHERE active=1 ORDER BY name').fetchall(); categories=c.execute('SELECT * FROM expense_categories ORDER BY name').fetchall()
        recent=c.execute("""SELECT kind,event_date,label,amount,user FROM (SELECT 'Materiał' kind,u.usage_date event_date,COALESCE(m.name,m.sku) label,u.total_cost amount,u.entered_by user FROM material_usage u JOIN products m ON m.id=u.material_id WHERE u.deleted_at IS NULL UNION ALL SELECT 'Paliwo',f.entry_date,v.registration_no,f.total_cost,f.created_by FROM fuel_entries f JOIN vehicles v ON v.id=f.vehicle_id WHERE f.deleted_at IS NULL UNION ALL SELECT ec.name,e.expense_date,v.registration_no||' · '||e.description,e.gross_cost,e.created_by FROM vehicle_expenses e JOIN vehicles v ON v.id=e.vehicle_id JOIN expense_categories ec ON ec.id=e.category_id WHERE e.deleted_at IS NULL) ORDER BY event_date DESC LIMIT 30""").fetchall()
    return render_template('operations.html',materials=materials,vehicles=vehicles,drivers=drivers,departments=departments,categories=categories,recent=recent,today=date.today().isoformat())

@bp.get('/analytics')
def analytics():
    start,end,kind=period()
    with DB() as c:
        material=c.execute('SELECT COALESCE(SUM(total_cost),0) FROM material_usage WHERE deleted_at IS NULL AND usage_date BETWEEN ? AND ?',(start,end)).fetchone()[0]
        fuel=c.execute('SELECT COALESCE(SUM(total_cost),0) FROM fuel_entries WHERE deleted_at IS NULL AND entry_date BETWEEN ? AND ?',(start,end)).fetchone()[0]
        exp=c.execute("SELECT ec.group_code,COALESCE(SUM(e.gross_cost),0) value FROM vehicle_expenses e JOIN expense_categories ec ON ec.id=e.category_id WHERE e.deleted_at IS NULL AND e.expense_date BETWEEN ? AND ? GROUP BY ec.group_code",(start,end)).fetchall(); costs={r['group_code']:r['value'] for r in exp}
        monthly=c.execute("""SELECT substr(d,1,7) period,SUM(amount) amount FROM (SELECT usage_date d,total_cost amount FROM material_usage WHERE deleted_at IS NULL UNION ALL SELECT entry_date,total_cost FROM fuel_entries WHERE deleted_at IS NULL UNION ALL SELECT expense_date,gross_cost FROM vehicle_expenses WHERE deleted_at IS NULL) WHERE d BETWEEN ? AND ? GROUP BY substr(d,1,7) ORDER BY period""",(start,end)).fetchall()
        vehicles=c.execute("""SELECT v.registration_no,COALESCE(f.cost,0)+COALESCE(e.cost,0) cost FROM vehicles v LEFT JOIN (SELECT vehicle_id,SUM(total_cost) cost FROM fuel_entries WHERE deleted_at IS NULL AND entry_date BETWEEN ? AND ? GROUP BY vehicle_id) f ON f.vehicle_id=v.id LEFT JOIN (SELECT vehicle_id,SUM(gross_cost) cost FROM vehicle_expenses WHERE deleted_at IS NULL AND expense_date BETWEEN ? AND ? GROUP BY vehicle_id) e ON e.vehicle_id=v.id ORDER BY cost DESC""",(start,end,start,end)).fetchall()
        # Sales are based on issued WZ documents. This is available immediately,
        # even when an invoice is issued later by the accounting department.
        sales=c.execute("""
            SELECT COALESCE(SUM(i.total_net),0) net, COALESCE(SUM(i.total_gross),0) gross,
                   COUNT(i.id) invoices
            FROM invoices i
            WHERE substr(i.issue_date,1,10) BETWEEN ? AND ?
        """,(start,end)).fetchone()
        products=c.execute("""
            SELECT COALESCE(p.name, wi.sku) product, wi.sku,
                   ROUND(SUM(COALESCE(wi.qty_issued,wi.qty_planned)),2) qty
            FROM wz_items wi
            JOIN wz_documents w ON w.id=wi.wz_id
            LEFT JOIN products p ON p.id=wi.product_id
            WHERE w.deleted_at IS NULL AND w.issued_at IS NOT NULL
              AND substr(w.issued_at,1,10) BETWEEN ? AND ?
            GROUP BY wi.product_id, wi.sku, p.name
            ORDER BY qty DESC, product ASC
            LIMIT 12
        """,(start,end)).fetchall()
        sales_daily=c.execute("""
            SELECT substr(w.issued_at,1,10) day,
                   ROUND(SUM(COALESCE(wi.qty_issued,wi.qty_planned)),2) qty
            FROM wz_items wi JOIN wz_documents w ON w.id=wi.wz_id
            WHERE w.deleted_at IS NULL AND w.issued_at IS NOT NULL
              AND substr(w.issued_at,1,10) BETWEEN ? AND ?
            GROUP BY substr(w.issued_at,1,10) ORDER BY day
        """,(start,end)).fetchall()
        # Only completed transports are treated as completed courses in rankings.
        driver_ranking=c.execute("""
            SELECT d.name, COUNT(t.id) trips
            FROM transports t JOIN drivers d ON d.id=t.driver_id
            WHERE t.deleted_at IS NULL AND t.status='returned'
              AND substr(COALESCE(t.returned_at,t.updated_at,t.created_at),1,10) BETWEEN ? AND ?
            GROUP BY d.id,d.name ORDER BY trips DESC,d.name ASC
        """,(start,end)).fetchall()
        vehicle_stats=c.execute("""
            SELECT v.registration_no,
                   COALESCE(t.trips,0) trips,
                   COALESCE(f.fuel_cost,0) fuel_cost,
                   COALESCE(e.repair_cost,0) repair_cost,
                   COALESCE(f.fuel_cost,0)+COALESCE(e.repair_cost,0) total_cost
            FROM vehicles v
            LEFT JOIN (
              SELECT vehicle_id,COUNT(*) trips FROM transports
              WHERE deleted_at IS NULL AND status='returned'
                AND substr(COALESCE(returned_at,updated_at,created_at),1,10) BETWEEN ? AND ?
              GROUP BY vehicle_id
            ) t ON t.vehicle_id=v.id
            LEFT JOIN (
              SELECT vehicle_id,SUM(total_cost) fuel_cost FROM fuel_entries
              WHERE deleted_at IS NULL AND entry_date BETWEEN ? AND ? GROUP BY vehicle_id
            ) f ON f.vehicle_id=v.id
            LEFT JOIN (
              SELECT vehicle_id,SUM(gross_cost) repair_cost FROM vehicle_expenses
              WHERE deleted_at IS NULL AND expense_date BETWEEN ? AND ? GROUP BY vehicle_id
            ) e ON e.vehicle_id=v.id
            WHERE v.deleted_at IS NULL
            ORDER BY repair_cost DESC,total_cost DESC,v.registration_no ASC
        """,(start,end,start,end,start,end)).fetchall()
    total=material+fuel+sum(costs.values())
    sold_m3=sum(float(x['qty'] or 0) for x in products)
    total_trips=sum(int(x['trips'] or 0) for x in vehicle_stats)
    fleet_cost=sum(float(x['total_cost'] or 0) for x in vehicle_stats)
    avg_transport_cost=(fleet_cost/total_trips) if total_trips else 0
    return render_template('analytics.html',start=start,end=end,period=kind,material=material,fuel=fuel,costs=costs,total=total,monthly=monthly,vehicles=vehicles,sales=sales,products=products,sales_daily=sales_daily,sold_m3=sold_m3,driver_ranking=driver_ranking,vehicle_stats=vehicle_stats,total_trips=total_trips,fleet_cost=fleet_cost,avg_transport_cost=avg_transport_cost)

@bp.get('/analytics/export.csv')
def export_costs():
    start,end,_=period(); out=io.StringIO(); out.write('\ufeff'); w=csv.writer(out,delimiter=';'); w.writerow(['Data','Rodzaj','Opis','Koszt brutto'])
    with DB() as c:
        rows=c.execute("""SELECT d,kind,label,amount FROM (SELECT usage_date d,'Materiał' kind,COALESCE(m.name,m.sku) label,total_cost amount FROM material_usage u JOIN products m ON m.id=u.material_id WHERE u.deleted_at IS NULL UNION ALL SELECT entry_date,'Paliwo',v.registration_no,total_cost FROM fuel_entries f JOIN vehicles v ON v.id=f.vehicle_id WHERE f.deleted_at IS NULL UNION ALL SELECT expense_date,ec.name,v.registration_no||' · '||description,gross_cost FROM vehicle_expenses e JOIN vehicles v ON v.id=e.vehicle_id JOIN expense_categories ec ON ec.id=e.category_id WHERE e.deleted_at IS NULL) WHERE d BETWEEN ? AND ? ORDER BY d""",(start,end)).fetchall()
    for r in rows:w.writerow(r)
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename=koszty_{start}_{end}.csv'})
