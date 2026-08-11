import re
import os
import io
import json
import secrets
import time
import unicodedata
from datetime import datetime

from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template_string, request, send_file, session, url_for
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

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
        CREATE TABLE IF NOT EXISTS driver_accounts(
          driver_id INTEGER PRIMARY KEY REFERENCES drivers(id) ON DELETE CASCADE,
          username TEXT NOT NULL UNIQUE,auth_user_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
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
        CREATE TABLE IF NOT EXISTS wz_technology_snapshots(
          id INTEGER PRIMARY KEY,wz_id INTEGER NOT NULL REFERENCES wz_documents(id) ON DELETE CASCADE,
          wz_item_id INTEGER NOT NULL REFERENCES wz_items(id) ON DELETE CASCADE,product_id INTEGER NOT NULL REFERENCES products(id),
          recipe_version_id INTEGER,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(wz_item_id));
        CREATE TABLE IF NOT EXISTS transports(
          id INTEGER PRIMARY KEY AUTOINCREMENT,transport_no TEXT NOT NULL UNIQUE,invoice_id INTEGER REFERENCES invoices(id),wz_id INTEGER REFERENCES wz_documents(id),
          driver_id INTEGER NOT NULL REFERENCES drivers(id),vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),destination TEXT,
          status TEXT NOT NULL DEFAULT 'assigned',issued_at TEXT,departed_at TEXT,delivered_at TEXT,returned_at TEXT,
          receiver_name TEXT,driver_notes TEXT,created_by TEXT NOT NULL,updated_by TEXT NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS transport_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
          invoice_allocation_id INTEGER REFERENCES invoice_allocations(id),wz_item_id INTEGER REFERENCES wz_items(id),qty REAL NOT NULL CHECK(qty>0),created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS transport_delivery_adjustments(
          id INTEGER PRIMARY KEY,transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
          water_added INTEGER NOT NULL DEFAULT 0,water_qty REAL,water_unit TEXT DEFAULT 'l',event_at TEXT,
          added_fibres TEXT,added_chemicals TEXT,other_additions TEXT,notes TEXT,responsible_person TEXT,
          created_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
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
def cloud_id(): return int(time.time() * 1000) * 1000 + secrets.randbelow(1000)
def next_no(c):
    year=stamp()[:4]; n=c.execute("SELECT COUNT(*) FROM transports WHERE transport_no LIKE ?",(f'TR/{year}/%',)).fetchone()[0]+1
    return f'TR/{year}/{n:05d}'
def next_wz_no(c):
    year=stamp()[:4]; n=c.execute("SELECT COUNT(*) FROM wz_documents WHERE wz_no LIKE ?",(f'WZ/{year}/%',)).fetchone()[0]+1
    return f'WZ/{year}/{n:05d}'

def snapshot_wz_technology(c, wz_id, created_by, created_at):
    """Freeze recipe and material technology for every WZ line exactly once."""
    for wz_item in c.execute('SELECT * FROM wz_items WHERE wz_id=? ORDER BY id',(wz_id,)).fetchall():
        if c.execute('SELECT 1 FROM wz_technology_snapshots WHERE wz_item_id=?',(wz_item['id'],)).fetchone(): continue
        version=c.execute('''SELECT * FROM recipe_versions WHERE product_id=? AND (valid_from IS NULL OR valid_from='' OR valid_from<=?) ORDER BY version_no DESC,id DESC LIMIT 1''',(wz_item['product_id'],created_at[:10])).fetchone()
        if version:
            material_rows=c.execute('''SELECT rvi.qty_per_unit,rvi.unit,rvi.material_snapshot_json,rm.* FROM recipe_version_items rvi JOIN raw_materials rm ON rm.id=rvi.material_id WHERE rvi.recipe_version_id=? ORDER BY rm.name''',(version['id'],)).fetchall()
        else:
            material_rows=c.execute('''SELECT pr.qty_per_unit,rm.unit,NULL material_snapshot_json,rm.* FROM product_recipes pr JOIN raw_materials rm ON rm.id=pr.material_id WHERE pr.product_id=? ORDER BY rm.name''',(wz_item['product_id'],)).fetchall()
        materials=[]
        for row in material_rows:
            material=dict(row); frozen={}
            if material.get('material_snapshot_json'):
                try: frozen=json.loads(material['material_snapshot_json'])
                except Exception: frozen={}
            for key in ('id','name','code','material_type','unit','manufacturer','trade_name','reference_document','technical_designation','description','cement_type','cement_designation','strength_class','aggregate_type','fraction','max_grain_size'):
                frozen.setdefault(key,material.get(key))
            frozen['qty_per_unit']=float(material['qty_per_unit']); materials.append(frozen)
        version_data=dict(version) if version else {}
        product=c.execute('SELECT id,sku,name,model,unit FROM products WHERE id=?',(wz_item['product_id'],)).fetchone()
        cement=next((m for m in materials if (m.get('material_type') or '').lower()=='cement'),{})
        snapshot={'recipe_version_id':version_data.get('id'),'recipe_no':version_data.get('recipe_no') or '','recipe_name':version_data.get('name') or 'Receptura produktu','version_no':version_data.get('version_no') or 1,'valid_from':version_data.get('valid_from'),'concrete_class':version_data.get('concrete_class'),'consistency':version_data.get('consistency'),'water_cement_ratio':version_data.get('water_cement_ratio'),'exposure_class':version_data.get('exposure_class'),'max_aggregate_size':version_data.get('max_aggregate_size'),'chloride_class':version_data.get('chloride_class'),'characteristic_strength':version_data.get('characteristic_strength'),'reference_document':version_data.get('reference_document'),'cement_type':version_data.get('cement_type') or cement.get('technical_designation') or cement.get('cement_designation'),'admixtures':version_data.get('admixtures'),'fibres':version_data.get('fibres'),'other_additions':version_data.get('other_additions'),'technology_notes':version_data.get('technology_notes'),'product':dict(product) if product else {},'materials':materials,'qty':float(wz_item['qty_issued'] if wz_item['qty_issued'] is not None else wz_item['qty_planned'])}
        c.execute('INSERT INTO wz_technology_snapshots(id,wz_id,wz_item_id,product_id,recipe_version_id,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?)',(cloud_id(),wz_id,wz_item['id'],wz_item['product_id'],version_data.get('id'),json.dumps(snapshot,ensure_ascii=False),created_at))

def issue_recipe_materials(c, wz_id, issued_by, issued_at):
    """Book one idempotent raw-material issue for a WZ."""
    snapshot_wz_technology(c,wz_id,issued_by,issued_at)
    if c.execute("SELECT 1 FROM raw_material_movements WHERE wz_id=? AND movement_type='wz_issue' LIMIT 1",(wz_id,)).fetchone():
        raise ValueError('Materiały dla tego WZ zostały już rozchodowane.')
    totals={}
    for row in c.execute('SELECT snapshot_json FROM wz_technology_snapshots WHERE wz_id=?',(wz_id,)).fetchall():
        data=json.loads(row['snapshot_json']); product_qty=float(data.get('qty') or 0)
        if not data.get('materials'):
            raise ValueError(f"Produkt {(data.get('product') or {}).get('name') or (data.get('product') or {}).get('sku')} nie ma receptury.")
        for material in data['materials']:
            material_id=int(material['id'])
            entry=totals.setdefault(material_id,{'name':material.get('name') or str(material_id),'unit':material.get('unit') or 'kg','qty':0.0})
            entry['qty']+=float(material.get('qty_per_unit') or 0)*product_qty
    shortages=[]
    for material_id,entry in totals.items():
        stock=c.execute('SELECT COALESCE(qty,0) qty FROM raw_material_stock WHERE material_id=?',(material_id,)).fetchone()
        entry['stock']=float(stock['qty'] if stock else 0)
        if entry['stock']+1e-9<entry['qty']: shortages.append(f"{entry['name']}: potrzeba {entry['qty']:.4f} {entry['unit']}, stan {entry['stock']:.4f} {entry['unit']}")
    if shortages: raise ValueError('Brak materiału na magazynie: '+'; '.join(shortages))
    for material_id,entry in totals.items():
        c.execute('UPDATE raw_material_stock SET qty=qty-? WHERE material_id=?',(entry['qty'],material_id))
        c.execute("INSERT INTO raw_material_movements(id,material_id,wz_id,qty_delta,movement_type,note,created_by,created_at) VALUES(?,?,?,?,'wz_issue','Rozchód według wersji receptury zapisanej w snapshot WZ',?,?)",(cloud_id(),material_id,wz_id,-entry['qty'],issued_by,issued_at))
    return
    already=c.execute("SELECT 1 FROM raw_material_movements WHERE wz_id=? AND movement_type='wz_issue' LIMIT 1",(wz_id,)).fetchone()
    if already:
        raise ValueError('Materiały dla tego WZ zostały już rozchodowane.')
    missing=c.execute('''SELECT COALESCE(p.name,p.sku) product
      FROM wz_items wi JOIN products p ON p.id=wi.product_id
      WHERE wi.wz_id=? AND NOT EXISTS(SELECT 1 FROM product_recipes pr WHERE pr.product_id=wi.product_id)
      LIMIT 1''',(wz_id,)).fetchone()
    if missing:
        raise ValueError(f"Produkt {missing['product']} nie ma receptury. Uzupełnij ją w Ustawienia → Produkty.")
    requirements=c.execute('''SELECT rm.id material_id,rm.name,rm.unit,
      SUM(pr.qty_per_unit*COALESCE(wi.qty_issued,wi.qty_planned)) required_qty,
      COALESCE(s.qty,0) stock_qty
      FROM wz_items wi JOIN product_recipes pr ON pr.product_id=wi.product_id
      JOIN raw_materials rm ON rm.id=pr.material_id
      LEFT JOIN raw_material_stock s ON s.material_id=rm.id
      WHERE wi.wz_id=? GROUP BY rm.id,rm.name,rm.unit,s.qty ORDER BY rm.name''',(wz_id,)).fetchall()
    shortages=[f"{x['name']}: potrzeba {x['required_qty']:.4f} {x['unit']}, stan {x['stock_qty']:.4f} {x['unit']}" for x in requirements if float(x['stock_qty'])+1e-9<float(x['required_qty'])]
    if shortages:
        raise ValueError('Brak materiału na magazynie: '+'; '.join(shortages))
    for item in requirements:
        qty=float(item['required_qty'])
        c.execute('UPDATE raw_material_stock SET qty=qty-? WHERE material_id=?',(qty,item['material_id']))
        c.execute('''INSERT INTO raw_material_movements(id,material_id,wz_id,qty_delta,movement_type,note,created_by,created_at)
          VALUES(?,?,?,?,'wz_issue','Rozchód według receptury',?,?)''',(cloud_id(),item['material_id'],wz_id,-qty,issued_by,issued_at))

def reverse_recipe_materials(c, wz_id, reversed_by, reversed_at):
    """Reverse the exact stored WZ movements, independent of later recipe edits."""
    movements=c.execute('''SELECT m.* FROM raw_material_movements m
      WHERE m.wz_id=? AND m.movement_type='wz_issue'
      AND NOT EXISTS(SELECT 1 FROM raw_material_movements r WHERE r.reversed_movement_id=m.id)
      ORDER BY m.id''',(wz_id,)).fetchall()
    for movement in movements:
        qty=-float(movement['qty_delta'])
        c.execute('INSERT OR IGNORE INTO raw_material_stock(material_id,qty) VALUES(?,0)',(movement['material_id'],))
        c.execute('UPDATE raw_material_stock SET qty=qty+? WHERE material_id=?',(qty,movement['material_id']))
        c.execute('''INSERT INTO raw_material_movements(id,material_id,wz_id,qty_delta,movement_type,note,reversed_movement_id,created_by,created_at)
          VALUES(?,?,?,?,?,'Cofnięcie wydania WZ',?,?,?)''',(cloud_id(),movement['material_id'],wz_id,qty,'wz_reversal',movement['id'],reversed_by,reversed_at))
    return len(movements)


def create_wz_from_order(c, order_id, destination='', issue_location='Beton Łagów', warehouse_location='Magazyn główny', created_by=None):
    """Create the initial WZ together with a newly entered concrete order."""
    order=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    if not order:
        raise ValueError('Nie znaleziono zamówienia do wystawienia WZ.')
    items=c.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order_id,)).fetchall()
    if not items:
        raise ValueError('Nie można wystawić WZ bez pozycji zamówienia.')
    created_at=stamp()
    wz_id=cloud_id()
    final_destination=(destination or order['customer_address'] or '').strip()
    c.execute('''INSERT INTO wz_documents(id,wz_no,order_id,issue_location,warehouse_location,destination,status,created_by,created_at,notes)
        VALUES(?,?,?,?,?,?,'created',?,?,?)''',(
            wz_id,next_wz_no(c),order_id,issue_location,warehouse_location,final_destination,
            created_by or actor(),created_at,''
        ))
    wz_item_ids=[]
    for item in items:
        qty=float(item['qty'] or 0)
        if qty<=0:
            continue
        wz_item_id=cloud_id()
        c.execute('''INSERT INTO wz_items(id,wz_id,order_item_id,product_id,sku,qty_planned,created_at)
            VALUES(?,?,?,?,?,?,?)''',(wz_item_id,wz_id,item['id'],item['product_id'],item['sku'],qty,created_at))
        wz_item_ids.append(wz_item_id)
    if not wz_item_ids:
        raise ValueError('WZ musi zawierać co najmniej jedną pozycję.')
    return wz_id,wz_item_ids

def driver_auth_email(username):
    value=unicodedata.normalize('NFD',(username or '').strip().lower())
    value=''.join(ch for ch in value if unicodedata.category(ch)!='Mn')
    value=re.sub(r'[^a-z0-9._-]+','',value)
    if not value:
        raise ValueError('Login kierowcy może zawierać litery, cyfry, kropkę, myślnik i podkreślenie.')
    # Supabase Auth odrzuca końcówkę .local (HTTP 422). To jest techniczny
    # identyfikator, bez wysyłki e-maili, ale z poprawną domeną publiczną.
    return f'{value}@kierowca.betonlagow.app'

def save_row_to_supabase(table, row, conflict='id'):
    """Critical operational records are saved centrally before the response."""
    if not D['supabase_enabled']():
        raise RuntimeError('Brak połączenia z Supabase. Nie można zapisać danych bezpiecznie w chmurze.')
    D['supabase_request'](f'/rest/v1/{table}', method='POST', params={'on_conflict': conflict}, payload=[dict(row)], prefer='resolution=merge-duplicates,return=minimal')

def existing_auth_user_id(email):
    """Find an account left behind by an interrupted earlier creation attempt."""
    result=D['supabase_request']('/auth/v1/admin/users',method='GET',params={'page':1,'per_page':1000}) or {}
    users=result.get('users',[]) if isinstance(result,dict) else []
    wanted=(email or '').strip().lower()
    for item in users:
        if (item.get('email') or '').strip().lower()==wanted and item.get('id'):
            return str(item['id'])
    return ''

def provision_driver_account(driver_id, username, password, update=False):
    if not D['supabase_enabled']():
        raise RuntimeError('Nie można nadać hasła: brakuje SUPABASE_URL lub SUPABASE_SERVICE_ROLE_KEY na Render.')
    username=(username or '').strip()
    if len(username)<3 or len(password or '')<12:
        raise ValueError('Login musi mieć min. 3 znaki, a hasło min. 12 znaków.')
    with D['conn']() as c:
        driver=c.execute('SELECT * FROM drivers WHERE id=? AND deleted_at IS NULL',(driver_id,)).fetchone()
        account=c.execute('SELECT * FROM driver_accounts WHERE driver_id=?',(driver_id,)).fetchone()
    if not driver:
        raise ValueError('Nie znaleziono kierowcy.')
    email=driver_auth_email(username)
    if account:
        auth_user_id=account['auth_user_id']
    else:
        # Save the driver in Supabase before creating the protected profile relation.
        D['supabase_request']('/rest/v1/drivers',method='POST',payload=[dict(driver)],prefer='resolution=merge-duplicates,return=minimal')
        try:
            auth=D['supabase_request']('/auth/v1/admin/users',method='POST',payload={'email':email,'password':password,'email_confirm':True,'user_metadata':{'driver_id':driver_id,'username':username}})
            auth_user_id=(auth or {}).get('id')
        except RuntimeError:
            auth_user_id=existing_auth_user_id(email)
        if not auth_user_id:
            raise RuntimeError('Nie udało się odnaleźć ani utworzyć konta kierowcy w Supabase.')
    D['supabase_request'](f"/auth/v1/admin/users/{auth_user_id}",method='PUT',payload={'password':password,'email_confirm':True,'user_metadata':{'driver_id':driver_id,'username':username}})
    D['supabase_request']('/rest/v1/driver_profiles',method='POST',params={'on_conflict':'driver_id'},payload=[{'user_id':auth_user_id,'driver_id':driver_id,'active':True}],prefer='resolution=merge-duplicates,return=minimal')
    with D['conn']() as c:
        c.execute('INSERT INTO driver_accounts(driver_id,username,auth_user_id,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(driver_id) DO UPDATE SET username=excluded.username,updated_at=excluded.updated_at',(driver_id,username,auth_user_id,stamp(),stamp()))
    return username

@driver_api.post('/login')
def driver_login_api():
    """The static Netlify portal talks only to Render; Supabase keys stay server-side."""
    data=request.get_json(silent=True) or {}
    username=(data.get('username') or '').strip()
    password=data.get('password') or ''
    if not username or not password:
        return jsonify(ok=False,code='DRIVER-INPUT',error='Wpisz login i hasło.'),400
    if not D['supabase_enabled']():
        current_app.logger.error('Logowanie kierowcy: brak konfiguracji Supabase na Render.')
        return jsonify(ok=False,code='DRIVER-CONFIG',error='Panel kierowcy nie jest jeszcze połączony z Supabase. Administrator: sprawdź SUPABASE_URL i SUPABASE_SERVICE_ROLE_KEY na Render.'),503
    try:
        email=driver_auth_email(username)
        auth=D['supabase_request'](
            '/auth/v1/token',
            method='POST',
            params={'grant_type':'password'},
            payload={'email':email,'password':password},
            # Pobranie sesji kierowcy nie może korzystać z tajnego klucza
            # serwisowego. Anon key zostaje wyłącznie na Render.
            use_anon_key=True,
        ) or {}
        access_token=auth.get('access_token')
        if not access_token:
            raise ValueError('Nieprawidłowy login lub hasło.')
        return jsonify(ok=True,access_token=access_token,expires_in=auth.get('expires_in') or 3600)
    except ValueError:
        return jsonify(ok=False,code='DRIVER-CREDENTIALS',error='Nieprawidłowy login lub hasło. Sprawdź też, czy konto kierowcy ma ustawione hasło w panelu administratora.'),401
    except RuntimeError as exc:
        # Nie przekazujemy odpowiedzi Supabase ani żadnych sekretów do telefonu,
        # ale operator dostaje rozróżnialny komunikat i kod do zgłoszenia.
        current_app.logger.warning('Logowanie kierowcy %s nie powiodło się: %s', username, exc)
        reason=str(exc)
        if 'Invalid login credentials' in reason or 'invalid login credentials' in reason:
            return jsonify(ok=False,code='DRIVER-CREDENTIALS',error='Nieprawidłowy login lub hasło. Sprawdź też, czy konto kierowcy ma ustawione hasło w panelu administratora.'),401
        return jsonify(ok=False,code='DRIVER-AUTH',error='Nie można zweryfikować konta kierowcy w Supabase. Administrator: sprawdź logi Render oraz zmienne SUPABASE_URL, SUPABASE_ANON_KEY i SUPABASE_SERVICE_ROLE_KEY.'),502
    except Exception as exc:
        current_app.logger.exception('Nieoczekiwany błąd logowania kierowcy %s: %s', username, exc)
        return jsonify(ok=False,code='DRIVER-ERROR',error='Wewnętrzny błąd logowania. Administrator: sprawdź logi Render (kod DRIVER-ERROR).'),500

@bp.get('/wz')
def wz_list():
    # Render nie zachowuje lokalnej SQLite po wdrożeniu. Dokumenty i zdjęcia
    # muszą być odświeżone z centralnego Supabase przed pokazaniem listy.
    try:
        D['pull_shared_tables_from_supabase'](force=True)
    except Exception:
        current_app.logger.exception('Nie udało się odświeżyć dokumentów WZ z Supabase')
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
        orders=c.execute("SELECT id,order_no,customer_name,customer_address,created_at FROM orders WHERE lower(status) NOT IN ('cancelled') ORDER BY id DESC LIMIT 300").fetchall()
        order=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone() if order_id else None
        items=c.execute('''SELECT oi.*,COALESCE(p.name,p.sku) product_name,COALESCE((SELECT SUM(wi.qty_planned) FROM wz_items wi JOIN wz_documents wd ON wd.id=wi.wz_id WHERE wi.order_item_id=oi.id AND wd.deleted_at IS NULL),0) wz_reserved FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=? ORDER BY oi.id''',(order_id,)).fetchall() if order else []
        if request.method=='POST':
            if not order:abort(400)
            s=stamp(); wz_id=cloud_id(); cur=c.execute('''INSERT INTO wz_documents(id,wz_no,order_id,issue_location,warehouse_location,destination,status,created_by,created_at,notes)
              VALUES(?,?,?,?,?,?,'created',?,?,?)''',(wz_id,next_wz_no(c),order_id,request.form.get('issue_location','Miejscowość X').strip(),request.form.get('warehouse_location','Miejscowość Y').strip(),request.form.get('destination','').strip() or (order['note'] or '').strip() or (order['customer_address'] or '').strip(),actor(),s,request.form.get('notes','').strip()))
            count=0
            for item in items:
                qty=float(request.form.get(f'qty_{item["id"]}') or 0)
                if qty>0 and qty<=float(item['qty'])-float(item['wz_reserved']):
                    c.execute('INSERT INTO wz_items(id,wz_id,order_item_id,product_id,sku,qty_planned,created_at) VALUES(?,?,?,?,?,?,?)',(cloud_id(),wz_id,item['id'],item['product_id'],item['sku'],qty,s)); count+=1
            if not count:raise ValueError('WZ musi zawierać co najmniej jedną pozycję')
            c.commit()
            D['sync_local_rows_to_supabase']('wz_documents','id',[wz_id])
            wz_item_ids=[x['id'] for x in c.execute('SELECT id FROM wz_items WHERE wz_id=?',(wz_id,)).fetchall()]
            D['sync_local_rows_to_supabase']('wz_items','id',wz_item_ids)
            return redirect(url_for('beton.wz_view',wz_id=wz_id))
    return render_template_string('''{% extends "base.html" %}{% block content %}
      <h1>Nowy dokument WZ</h1>
      <div class="card"><form method="get"><label>Zamówienie klienta</label><select name="order_id" onchange="this.form.submit()"><option value="">Wybierz zamówienie</option>{% for x in orders %}<option value="{{x.id}}" {{'selected' if x.id==order_id}}>{{x.order_no}} · {{x.customer_name}}</option>{% endfor %}</select></form></div>
      {% if order %}<form method="post" class="card"><input type="hidden" name="order_id" value="{{order.id}}"><h2>{{order.order_no}} · {{order.customer_name}}</h2>
        <div class="grid3"><div><label>Wystawiono w</label><input name="issue_location" value="Miejscowość X" required></div><div><label>Magazyn wydający</label><input name="warehouse_location" value="Miejscowość Y" required></div><div><label>Adres dostawy</label><input name="destination" value="{{order.customer_address or ''}}"></div></div>
        <p class="muted">Adres dostawy pobrano z zamówienia. Możesz go poprawić wyłącznie dla tego WZ.</p>
        <table><thead><tr><th>Produkt</th><th>Zamówiono [m³]</th><th>Już na WZ [m³]</th><th>Na nowym WZ [m³]</th></tr></thead><tbody>{% for x in items %}{% set available=x.qty-x.wz_reserved %}<tr><td>{{x.product_name}}<br><span class="muted">{{x.sku}}</span></td><td>{{x.qty}}</td><td>{{x.wz_reserved}}</td><td><input type="number" min="0" max="{{available}}" step="0.01" name="qty_{{x.id}}" value="{{available}}"></td></tr>{% endfor %}</tbody></table><label>Uwagi</label><textarea name="notes"></textarea><button class="btn primary">Wystaw WZ</button></form>{% endif %}
    {% endblock %}''',orders=orders,order=order,order_id=order_id,items=items,base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.get('/wz/<int:wz_id>')
def wz_view(wz_id):
    # W szczególności pobierz metadane zdjęć przesłanych z panelu kierowcy.
    try:
        D['pull_shared_tables_from_supabase'](force=True)
    except Exception:
        current_app.logger.exception('Nie udało się odświeżyć WZ z Supabase')
    with D['conn']() as c:
        w=c.execute('''SELECT w.*,o.customer_name,o.order_no,o.customer_address,o.note AS order_delivery_address,
            COALESCE(NULLIF(w.destination,''), o.customer_address) AS destination,
            i.invoice_no FROM wz_documents w JOIN orders o ON o.id=w.order_id
            LEFT JOIN invoices i ON i.id=w.invoice_id WHERE w.id=? AND w.deleted_at IS NULL''',(wz_id,)).fetchone()
        if not w:abort(404)
        w=dict(w)
        w['destination']=(w.get('destination') or w.get('order_delivery_address') or w.get('customer_address') or '').strip()
        items=c.execute('''SELECT wi.*, COALESCE(p.name, wi.sku) AS sku
            FROM wz_items wi LEFT JOIN products p ON p.id=wi.product_id
            WHERE wi.wz_id=? ORDER BY wi.id''',(wz_id,)).fetchall()
        transport=c.execute('SELECT * FROM transports WHERE wz_id=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1',(wz_id,)).fetchone()
        photos=c.execute('SELECT id,created_at FROM delivery_photos WHERE transport_id=? AND deleted_at IS NULL ORDER BY created_at DESC',(transport['id'],)).fetchall() if transport else []
    return render_template_string('''{% extends "base.html" %}{% block content %}
      <div class="flex"><h1>{{w.wz_no}}</h1><span class="badge">{{w.status}}</span><a class="btn right" target="_blank" href="{{url_for('beton.wz_print',wz_id=w.id)}}">Drukuj WZ</a><form method="post" action="{{url_for('beton.wz_delete',wz_id=w.id)}}" onsubmit="return confirm('Usunąć WZ {{w.wz_no}}? Dokument i przypisany transport znikną z bieżącej listy.');"><button class="btn danger" type="submit">Usuń WZ</button></form></div>
      <div class="card">
        <div class="grid3">
          <div><span class="muted">Zamawiający</span><br><b>{{w.customer_name}}</b></div>
          <div><span class="muted">Wystawiono / magazyn</span><br>{{w.issue_location}} → {{w.warehouse_location}}</div>
          <div><span class="muted">Adres dostawy</span><br><b>{{w.destination or '—'}}</b></div>
        </div>
        <div class="line"></div>
        <table><thead><tr><th>Produkt</th><th>Plan [m³]</th><th>Wydano [m³]</th></tr></thead><tbody>
          {% for x in items %}<tr><td>{{x.sku}}</td><td>{{x.qty_planned}}</td><td>{{x.qty_issued if x.qty_issued is not none else '—'}}</td></tr>{% endfor %}
        </tbody></table>
        <div class="flex" style="margin-top:16px">{% if w.status=='created' %}<form method="post" action="{{url_for('beton.wz_issue',wz_id=w.id)}}"><button class="btn primary">Potwierdź wydanie w {{w.warehouse_location}}</button></form>{% elif w.status in ['issued','in_transport'] %}<a class="btn primary" href="{{url_for('beton.transport_new',wz_id=w.id)}}">Przydziel transport(y) — maks. 8 m³</a>{% endif %}{% if w.status=='issued' and not transport %}<form method="post" action="{{url_for('beton.wz_revert',wz_id=w.id)}}" onsubmit="return confirm('Cofnąć wydanie WZ i zwrócić materiały na magazyn?')"><button class="btn danger">Cofnij WZ</button></form>{% endif %}{% if w.status in ['issued','in_transport','returned'] %}<form method="post" action="{{url_for('beton.wz_ready',wz_id=w.id)}}"><button class="btn primary">Podpisane WZ → wystaw fakturę VAT</button></form>{% elif w.status=='ready_invoice' %}<a class="btn primary" href="{{url_for('order_invoice',order_id=w.order_id,wz_id=w.id)}}">Wystaw fakturę VAT</a>{% elif w.status=='invoiced' %}<span class="badge">Zafakturowano: {{w.invoice_no}}</span><a class="btn" href="{{url_for('invoice_download_admin',invoice_id=w.invoice_id)}}">Pobierz fakturę</a>{% endif %}{% if transport %}<a class="btn" href="{{url_for('beton.transport_view',transport_id=transport.id)}}">Ostatni transport {{transport.transport_no}}</a>{% endif %}</div>
      </div>
      <div class="card"><h2>Podpisy czynności</h2><table><tr><th>Wystawił WZ</th><td>{{w.created_by}} · {{w.created_at}}</td></tr><tr><th>Wydał towar</th><td>{{w.issued_by or '—'}} {{w.issued_at or ''}}</td></tr><tr><th>Gotowość do FV</th><td>{{w.ready_by or '—'}} {{w.ready_at or ''}}</td></tr><tr><th>Wystawił FV</th><td>{{w.invoiced_by or '—'}} {{w.invoiced_at or ''}}</td></tr></table></div>
      <div class="card"><h2>Zdjęcia podpisanego WZ</h2>{% for p in photos %}<div class="flex" style="margin:8px 0"><span>{{p.created_at}}</span><a class="btn" href="{{url_for('beton.photo_download',photo_id=p.id)}}">Pobierz zdjęcie</a></div>{% else %}<span class="muted">Brak zdjęć.</span>{% endfor %}</div>
      {% if w.status in ['issued','in_transport','returned'] %}<div class="card"><h2>Fakturowanie</h2><p class="muted">Po otrzymaniu podpisanego WZ kliknij poniżej. Status zostanie zapisany, a następnie otworzy się wystawienie faktury VAT.</p><form method="post" action="{{url_for('beton.wz_ready',wz_id=w.id)}}"><button class="btn primary">Podpisane WZ → wystaw fakturę VAT</button></form></div>{% endif %}
    {% endblock %}''',w=w,items=items,transport=transport,photos=photos,base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.get('/photos/<int:photo_id>/download')
def photo_download(photo_id):
    with D['conn']() as c:
        photo=c.execute('SELECT storage_ref FROM delivery_photos WHERE id=? AND deleted_at IS NULL',(photo_id,)).fetchone()
    if not photo: abort(404)
    try:
        raw, filename=D['supabase_storage_download_bytes'](photo['storage_ref'])
        return send_file(io.BytesIO(raw), as_attachment=True, download_name=filename)
    except Exception:
        current_app.logger.exception('Nie udało się pobrać zdjęcia podpisanego WZ')
        return 'Nie udało się pobrać zdjęcia z Supabase.', 502


@bp.post('/wz/<int:wz_id>/delete')
def wz_delete(wz_id):
    """Delete only an unfulfilled WZ; completed logistics require a formal correction."""
    s = stamp()
    with D['conn']() as c:
        wz = c.execute('SELECT * FROM wz_documents WHERE id=? AND deleted_at IS NULL', (wz_id,)).fetchone()
        if not wz:
            abort(404)
        if wz['invoice_id']:
            return 'Najpierw usuń powiązaną fakturę VAT, a następnie WZ.', 409
        if wz['status'] in {'in_transport','ready_invoice','returned','invoiced'}:
            return 'Nie można usunąć WZ po rozpoczęciu dostawy, podpisaniu dokumentu lub fakturowaniu. Wymagana jest formalna korekta.', 409
        progressed = c.execute('''SELECT transport_no,status FROM transports
            WHERE wz_id=? AND deleted_at IS NULL AND status NOT IN ('assigned')
            ORDER BY id LIMIT 1''',(wz_id,)).fetchone()
        if progressed:
            return f"Nie można usunąć WZ: transport {progressed['transport_no']} rozpoczął realizację ({progressed['status']}).", 409
        reverse_recipe_materials(c, wz_id, actor(), s)
        material_ids = [x['material_id'] for x in c.execute('SELECT DISTINCT material_id FROM raw_material_movements WHERE wz_id=?',(wz_id,)).fetchall()]
        movement_ids = [x['id'] for x in c.execute('SELECT id FROM raw_material_movements WHERE wz_id=?',(wz_id,)).fetchall()]
        transport_ids = [int(row['id']) for row in c.execute(
            'SELECT id FROM transports WHERE wz_id=? AND deleted_at IS NULL', (wz_id,)
        ).fetchall()]
        c.execute('''UPDATE transports SET deleted_at=?, updated_at=?, updated_by=?
                     WHERE wz_id=? AND deleted_at IS NULL''', (s, s, actor(), wz_id))
        c.execute('UPDATE wz_documents SET deleted_at=? WHERE id=?', (s, wz_id))
        c.execute('''INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at)
                     VALUES(?,?,?,?,?,?)''', (actor(), 'delete', 'wz_document', wz_id, '{"mode":"soft_delete"}', s))
        c.commit()
    # Synchronizacja od razu, aby kierowca nie widział anulowanego kursu.
    D['sync_local_rows_to_supabase']('wz_documents', 'id', [wz_id])
    if transport_ids:
        D['sync_local_rows_to_supabase']('transports', 'id', transport_ids)
    D['sync_local_rows_to_supabase']('raw_material_stock', 'material_id', material_ids)
    D['sync_local_rows_to_supabase']('raw_material_movements', 'id', movement_ids)
    return redirect(url_for('beton.wz_list'))

def build_wz_form_pdf(w,items,courses,technology,company):
    """Jednostronicowy WZ w układzie formularza betoniarni."""
    font='Helvetica'; bold='Helvetica-Bold'
    for path in (r'C:\Windows\Fonts\arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(path):
            try:
                if 'WZForm' not in pdfmetrics.getRegisteredFontNames(): pdfmetrics.registerFont(TTFont('WZForm',path))
                font=bold='WZForm'; break
            except Exception: pass
    out=io.BytesIO(); p=canvas.Canvas(out,pagesize=A4); W,H=A4
    left=10*mm; right=W-10*mm; width=right-left; top=H-10*mm
    p.setLineWidth(.6)
    def rect(x,y,wid,hei): p.rect(x,y,wid,hei,stroke=1,fill=0)
    def text(x,y,value,size=8,strong=False):
        p.setFont(bold if strong else font,size); p.drawString(x,y,str(value or ''))
    def centered(x,y,wid,value,size=8,strong=False):
        p.setFont(bold if strong else font,size); p.drawCentredString(x+wid/2,y,str(value or ''))
    def wrapped(x,y,value,max_chars=65,size=7,leading=3.6*mm,max_lines=3):
        words=str(value or '').split(); lines=[]; current=''
        for word in words:
            candidate=(current+' '+word).strip()
            if len(candidate)>max_chars and current: lines.append(current); current=word
            else: current=candidate
        if current: lines.append(current)
        for idx,line_value in enumerate(lines[:max_lines]): text(x,y-idx*leading,line_value,size)
    def labelled(x,y,label,value,size=8):
        text(x,y,label,6.5); text(x,y-4*mm,value,size,True)
    company_name=company.get('company_name') or 'Betoniarnia Łagów'
    issued=str(w.get('issued_at') or w.get('created_at') or '')
    date_value=issued[:10]; time_value=issued[11:16]
    first_course=dict(courses[0]) if courses else {}
    tech=technology[0] if technology else {}
    delivery_method='ODBIÓR WŁASNY' if str(w.get('delivery_method') or '').lower()=='pickup' else 'DOSTAWA'
    total_qty=sum(float(i['qty_issued'] if i['qty_issued'] is not None else i['qty_planned']) for i in items)
    product=', '.join(str(i['sku']) for i in items)
    # Nagłówek
    header_h=25*mm; header_y=top-header_h; rect(left,header_y,width,header_h)
    text(left+4*mm,top-7*mm,company_name,13,True)
    wrapped(left+4*mm,top-12*mm,company.get('address') or '',60,7,max_lines=2)
    text(left+4*mm,header_y+5*mm,'Tel: '+str(company.get('phone') or '')+'    e-mail: '+str(company.get('email') or ''),7)
    centered(left+width*.55,top-8*mm,width*.45,'WYDANIE ZEWNĘTRZNE',13,True)
    centered(left+width*.55,top-15*mm,width*.45,w.get('wz_no'),12,True)
    centered(left+width*.55,header_y+4*mm,width*.45,'Dokument dostawy mieszanki betonowej',7)
    # Dane zlecenia i odbiorcy
    info_h=50*mm; info_y=header_y-info_h; half=width/2
    rect(left,info_y,half,info_h); rect(left+half,info_y,half,info_h)
    labelled(left+4*mm,header_y-7*mm,'Godzina:',time_value)
    labelled(left+29*mm,header_y-7*mm,'Data:',date_value)
    labelled(left+4*mm,header_y-19*mm,'Zlecenie nr:',w.get('order_no'))
    labelled(left+4*mm,header_y-31*mm,'Ilość m³:',f'{total_qty:g}')
    labelled(left+29*mm,header_y-31*mm,'Klasa betonu:',tech.get('concrete_class') or product)
    labelled(left+4*mm,header_y-43*mm,'Nr receptury:',tech.get('recipe_no') or '')
    text(left+half+4*mm,header_y-7*mm,'Odbiorca:',6.5); text(left+half+25*mm,header_y-7*mm,w.get('customer_name'),9,True)
    text(left+half+4*mm,header_y-15*mm,'Adres:',6.5); wrapped(left+half+25*mm,header_y-15*mm,w.get('customer_address'),40,8,max_lines=2)
    text(left+half+4*mm,header_y-28*mm,'Plac budowy:',6.5); wrapped(left+half+25*mm,header_y-28*mm,w.get('destination'),40,8,max_lines=2)
    text(left+half+4*mm,header_y-40*mm,'NIP:',6.5); text(left+half+25*mm,header_y-40*mm,w.get('customer_nip'),8)
    text(left+half+4*mm,header_y-47*mm,'e-mail:',6.5); text(left+half+25*mm,header_y-47*mm,w.get('customer_email'),7)
    # Dyspozycja / kierowca / odbiorca
    people_h=35*mm; people_y=info_y-people_h; rect(left,people_y,width,people_h)
    text(left+4*mm,info_y-7*mm,'Kierowca:',7); text(left+28*mm,info_y-7*mm,first_course.get('driver_name'),10,True)
    text(left+4*mm,info_y-16*mm,'Nr rejestracyjny:',7); text(left+28*mm,info_y-16*mm,first_course.get('registration_no'),10,True)
    text(left+4*mm,info_y-25*mm,'Transport:',7); text(left+28*mm,info_y-25*mm,first_course.get('transport_no'),9,True)
    text(left+half,info_y-8*mm,'Towar zgodnie z dyspozycją',10,True)
    text(left+half,info_y-19*mm,'Na odpowiedzialność kierowcy:',8)
    p.line(left+half+48*mm,info_y-20*mm,right-5*mm,info_y-20*mm)
    text(left+half,info_y-29*mm,delivery_method,12,True)
    # Specyfikacja techniczna
    tech_h=49*mm; tech_y=people_y-tech_h; rect(left,tech_y,width,tech_h)
    centered(left,people_y-6*mm,width,'Specyfikacja techniczna wskazana przez zamawiającego',10,True)
    col=width/4
    fields=[('Cement',tech.get('cement_type')),('Dokument odniesienia',tech.get('reference_document')),('Klasa ekspozycji',tech.get('exposure_class')),('Rodzaj kruszywa',tech.get('max_aggregate_size')),('Konsystencja',tech.get('consistency')),('W/S',tech.get('water_cement_ratio')),('Wytrzymałość',tech.get('characteristic_strength')),('Klasa chlorków',tech.get('chloride_class'))]
    for idx,(label,value) in enumerate(fields):
        row=idx//4; column=idx%4; x=left+column*col; y=people_y-12*mm-row*17*mm
        if column: p.line(x,tech_y,x,people_y-8*mm)
        labelled(x+3*mm,y,label+':',value or '—',8)
    # Informacje o dodatkach i uwagi
    notes_h=35*mm; notes_y=tech_y-notes_h; rect(left,notes_y,width,notes_h)
    text(left+3*mm,tech_y-6*mm,'Domieszki / chemia:',7,True); wrapped(left+38*mm,tech_y-6*mm,tech.get('admixtures') or '—',75,7,max_lines=2)
    text(left+3*mm,tech_y-15*mm,'Włókna:',7,True); wrapped(left+38*mm,tech_y-15*mm,tech.get('fibres') or '—',75,7,max_lines=2)
    text(left+3*mm,tech_y-24*mm,'Inne dodatki:',7,True); wrapped(left+38*mm,tech_y-24*mm,tech.get('other_additions') or w.get('notes') or '—',75,7,max_lines=2)
    # Ostrzeżenie i podpisy
    warning_h=22*mm; warning_y=notes_y-warning_h; rect(left,warning_y,width,warning_h)
    wrapped(left+3*mm,notes_y-5*mm,'Dodanie wody lub innych składników na żądanie odbiorcy może zmienić właściwości mieszanki. Zdarzenie musi być odnotowane na dokumencie dostawy.',125,6.5,max_lines=3)
    sig_y=warning_y-25*mm
    labels=('Operator betoniarni','Kierowca','Odbiorca / czytelny podpis')
    for idx,label in enumerate(labels):
        x=left+idx*(width/3)+4*mm; p.line(x,sig_y+8*mm,x+width/3-8*mm,sig_y+8*mm); centered(x,sig_y+3*mm,width/3-8*mm,label,6.5)
    p.showPage(); p.save(); out.seek(0); return out

@bp.get('/wz/<int:wz_id>/print')
def wz_print(wz_id):
    with D['conn']() as c:
        w=c.execute('''SELECT w.*,o.order_no,o.customer_name,o.customer_address,o.customer_phone,o.customer_email,o.delivery_method,o.note AS order_delivery_address,
            COALESCE(c.nip,'') customer_nip,
            COALESCE(NULLIF(w.destination,''), o.customer_address) AS destination
            FROM wz_documents w JOIN orders o ON o.id=w.order_id LEFT JOIN customers c ON c.id=o.customer_id
            WHERE w.id=? AND w.deleted_at IS NULL''',(wz_id,)).fetchone()
        if not w:abort(404)
        w=dict(w)
        w['destination']=(w.get('destination') or w.get('order_delivery_address') or w.get('customer_address') or '').strip()
        items=c.execute('''SELECT COALESCE(p.name, wi.sku) AS sku, wi.qty_planned, wi.qty_issued
            FROM wz_items wi LEFT JOIN products p ON p.id=wi.product_id
            WHERE wi.wz_id=? ORDER BY wi.id''',(wz_id,)).fetchall()
        courses=c.execute('''SELECT t.transport_no,t.created_at,d.name driver_name,v.registration_no,
              COALESCE((SELECT SUM(ti.qty) FROM transport_items ti WHERE ti.transport_id=t.id),0) qty
            FROM transports t JOIN drivers d ON d.id=t.driver_id JOIN vehicles v ON v.id=t.vehicle_id
            WHERE t.wz_id=? AND t.deleted_at IS NULL ORDER BY t.id''',(wz_id,)).fetchall()
        technology=[]
        for snapshot_row in c.execute('SELECT snapshot_json FROM wz_technology_snapshots WHERE wz_id=? ORDER BY id',(wz_id,)).fetchall():
            try: technology.append(json.loads(snapshot_row['snapshot_json']))
            except Exception: pass
        company=c.execute('SELECT * FROM company_profile WHERE id=1').fetchone()
        company=dict(company) if company else {}
    pdf_buffer=build_wz_form_pdf(w,items,courses,technology,company)
    filename=re.sub(r'[^A-Za-z0-9_.-]+','_',w['wz_no'])+'.pdf'
    return send_file(pdf_buffer,mimetype='application/pdf',as_attachment=False,download_name=filename)
    font_name='Helvetica'
    for font_path in (r'C:\Windows\Fonts\arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(font_path):
            try:
                if 'WZRegular' not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont('WZRegular',font_path))
                font_name='WZRegular'
                break
            except Exception:
                pass
    out=io.BytesIO(); pdf=canvas.Canvas(out,pagesize=A4)
    width,height=A4; y=height-22*mm
    def line(text,size=10,gap=6):
        nonlocal y
        if y<22*mm:
            pdf.showPage(); pdf.setFont(font_name,10); y=height-22*mm
        pdf.setFont(font_name,size); pdf.drawString(18*mm,y,str(text or '—')[:115]); y-=gap*mm
    line(f"Wydanie zewnętrzne {w['wz_no']}",16,9)
    line(f"Data: {str(w['created_at'])[:10]}    Status: {w['status']}")
    line(f"Numer zlecenia: {w['order_no']}")
    line(f"Odbiorca: {w['customer_name']}")
    line(f"Adres odbiorcy: {w['customer_address'] or '—'}")
    line(f"Adres dostawy: {w['destination'] or '—'}")
    line(f"Miejsce wydania: {w['issue_location']} → {w['warehouse_location']}",10,9)
    line("Pozycje dokumentu",12,7)
    for item in items:
        qty=item['qty_issued'] if item['qty_issued'] is not None else item['qty_planned']
        line(f"{item['sku']}    {qty} m³")
    y-=3*mm; line("Kursy składające się na wydanie",12,7)
    for tech in technology:
        y-=3*mm; line("SPECYFIKACJA TECHNICZNA",12,7)
        for label,key in (("Klasa betonu","concrete_class"),("Nr receptury","recipe_no"),("Wersja","version_no"),("Cement","cement_type"),("Konsystencja","consistency"),("W/S","water_cement_ratio"),("Klasa ekspozycji","exposure_class"),("Maks. wymiar kruszywa","max_aggregate_size"),("Klasa chlorkĂłw","chloride_class"),("Wytrzymałość","characteristic_strength"),("Dokument odniesienia","reference_document"),("Domieszki","admixtures"),("Włókna","fibres"),("Inne dodatki","other_additions")):
            if tech.get(key) not in (None,''): line(f"{label}: {tech[key]}")
    if courses:
        for course in courses:
            line(f"{course['transport_no']}    {course['driver_name']} / {course['registration_no']}    {course['qty']} m³")
    else:
        line("Brak przydzielonych kursów.")
    y-=15*mm; line("________________________________________",10,5)
    line("Podpis i pieczęć odbiorcy",9)
    line("Operator betoniarni: ____________________",9)
    line("Kierowca: _______________________________",9)
    line("Odbiorca: _______________________________",9)
    pdf.save(); out.seek(0)
    filename=re.sub(r'[^A-Za-z0-9_.-]+','_',w['wz_no'])+'.pdf'
    return send_file(out,mimetype='application/pdf',as_attachment=False,download_name=filename)
    return render_template_string('''<!doctype html><html lang="pl"><meta charset="utf-8"><title>{{w.wz_no}}</title><style>body{font:14px Arial,sans-serif;max-width:900px;margin:35px auto;color:#111}table{border-collapse:collapse;width:100%;margin:22px 0}th,td{border:1px solid #333;padding:9px;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.sign{border-top:1px solid #111;padding-top:7px;text-align:center;margin-top:70px;width:40%}@media print{button{display:none}}</style><button onclick="print()">Drukuj</button><h1>Wydanie zewnętrzne {{w.wz_no}}</h1><p>WZ zbiorcza — obejmuje wszystkie kursy dla tego zamówienia.</p><div class="grid"><div><b>Odbiorca</b><br>{{w.customer_name}}<br>{{w.customer_address or ''}}</div><div><b>Adres dostawy</b><br>{{w.destination or '—'}}<br><br><b>Miejsce wydania</b><br>{{w.issue_location}} → {{w.warehouse_location}}</div></div><table><thead><tr><th>Produkt</th><th>Łączna ilość [m³]</th></tr></thead><tbody>{% for x in items %}<tr><td>{{x.sku}}</td><td>{{x.qty_issued if x.qty_issued is not none else x.qty_planned}}</td></tr>{% endfor %}</tbody></table><h2>Kursy składające się na wydanie</h2><table><thead><tr><th>Transport / WZ kursu</th><th>Kierowca / auto</th><th>Ilość [m³]</th><th>Data utworzenia</th></tr></thead><tbody>{% for x in courses %}<tr><td>{{x.transport_no}}</td><td>{{x.driver_name}} · {{x.registration_no}}</td><td>{{x.qty}}</td><td>{{x.created_at[:16]}}</td></tr>{% else %}<tr><td colspan="4">Brak przydzielonych kursów.</td></tr>{% endfor %}</tbody></table><div class="sign">Odbiorca — podpis i pieczęć na WZ zbiorczej</div></html>''',w=w,items=items,courses=courses)
    return render_template_string('''<!doctype html><html lang="pl"><meta charset="utf-8"><title>{{w.wz_no}}</title><style>body{font:14px Arial,sans-serif;max-width:900px;margin:35px auto;color:#111}h1{margin-bottom:4px}table{border-collapse:collapse;width:100%;margin:22px 0}th,td{border:1px solid #333;padding:9px;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.sign{display:grid;grid-template-columns:1fr 1fr;gap:70px;margin-top:70px}.line{border-top:1px solid #111;padding-top:7px;text-align:center}@media print{button{display:none}}</style><button onclick="print()">Drukuj</button><h1>Wydanie zewnętrzne {{w.wz_no}}</h1><p>Data: {{w.created_at[:10]}} · Status: {{w.status}}</p><div class="grid"><div><b>Zamawiający / odbiorca</b><br>{{w.customer_name}}<br>{{w.customer_address or ''}}</div><div><b>Miejsce wydania</b><br>{{w.issue_location}} → {{w.warehouse_location}}<br><br><b>Adres dostawy</b><br>{{w.destination or '—'}}</div></div><table><thead><tr><th>Produkt</th><th>Ilość [m³]</th></tr></thead><tbody>{% for x in items %}<tr><td>{{x.sku}}</td><td>{{x.qty_issued if x.qty_issued is not none else x.qty_planned}}</td></tr>{% endfor %}</tbody></table><p>Uwagi: {{w.notes or '—'}}</p><div class="sign"><div class="line">Wydał: {{w.issued_by or ''}}</div><div class="line">Odebrał / podpis i pieczęć</div></div></html>''',w=w,items=items)

@bp.post('/wz/<int:wz_id>/issue')
def wz_issue(wz_id):
    s=stamp()
    with D['conn']() as c:
        w=c.execute("SELECT * FROM wz_documents WHERE id=? AND status='created'",(wz_id,)).fetchone()
        if not w:abort(409)
        try:
            c.execute('UPDATE wz_items SET qty_issued=qty_planned WHERE wz_id=?',(wz_id,))
            issue_recipe_materials(c,wz_id,actor(),s)
            c.execute("UPDATE wz_documents SET status='issued',issued_by=?,issued_at=? WHERE id=?",(actor(),s,wz_id))
        except ValueError as exc:
            c.rollback()
            return str(exc),409
    D['sync_local_rows_to_supabase']('wz_documents','id',[wz_id])
    with D['conn']() as c:
        item_ids=[x['id'] for x in c.execute('SELECT id FROM wz_items WHERE wz_id=?',(wz_id,)).fetchall()]
        snapshot_ids=[x['id'] for x in c.execute('SELECT id FROM wz_technology_snapshots WHERE wz_id=?',(wz_id,)).fetchall()]
        material_ids=[x['material_id'] for x in c.execute("SELECT DISTINCT material_id FROM raw_material_movements WHERE wz_id=? AND movement_type='wz_issue'",(wz_id,)).fetchall()]
        movement_ids=[x['id'] for x in c.execute("SELECT id FROM raw_material_movements WHERE wz_id=?",(wz_id,)).fetchall()]
    D['sync_local_rows_to_supabase']('wz_items','id',item_ids)
    D['sync_local_rows_to_supabase']('wz_technology_snapshots','id',snapshot_ids)
    D['sync_local_rows_to_supabase']('raw_material_stock','material_id',material_ids)
    D['sync_local_rows_to_supabase']('raw_material_movements','id',movement_ids)
    return redirect(url_for('beton.wz_view',wz_id=wz_id))

@bp.post('/wz/<int:wz_id>/revert')
def wz_revert(wz_id):
    s=stamp()
    with D['conn']() as c:
        w=c.execute("SELECT * FROM wz_documents WHERE id=? AND status='issued' AND deleted_at IS NULL",(wz_id,)).fetchone()
        if not w:abort(409)
        if w['invoice_id'] or c.execute('SELECT 1 FROM transports WHERE wz_id=? AND deleted_at IS NULL LIMIT 1',(wz_id,)).fetchone():
            return 'Nie można cofnąć WZ powiązanego z transportem lub fakturą.',409
        reverse_recipe_materials(c,wz_id,actor(),s)
        c.execute('UPDATE wz_items SET qty_issued=NULL WHERE wz_id=?',(wz_id,))
        c.execute("UPDATE wz_documents SET status='created',issued_by=NULL,issued_at=NULL WHERE id=?",(wz_id,))
        c.execute('INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)',(actor(),'revert_issue','wz_document',wz_id,'{}',s))
    D['sync_local_rows_to_supabase']('wz_documents','id',[wz_id])
    with D['conn']() as c:
        item_ids=[x['id'] for x in c.execute('SELECT id FROM wz_items WHERE wz_id=?',(wz_id,)).fetchall()]
        material_ids=[x['material_id'] for x in c.execute('SELECT DISTINCT material_id FROM raw_material_movements WHERE wz_id=?',(wz_id,)).fetchall()]
        movement_ids=[x['id'] for x in c.execute('SELECT id FROM raw_material_movements WHERE wz_id=?',(wz_id,)).fetchall()]
    D['sync_local_rows_to_supabase']('wz_items','id',item_ids)
    D['sync_local_rows_to_supabase']('raw_material_stock','material_id',material_ids)
    D['sync_local_rows_to_supabase']('raw_material_movements','id',movement_ids)
    return redirect(url_for('beton.wz_view',wz_id=wz_id))

@bp.post('/wz/<int:wz_id>/ready')
def wz_ready(wz_id):
    s=stamp()
    with D['conn']() as c:
        w=c.execute("SELECT * FROM wz_documents WHERE id=? AND status IN ('issued','in_transport','returned')",(wz_id,)).fetchone()
        if not w:abort(409)
        c.execute("UPDATE wz_documents SET status='ready_invoice',ready_by=?,ready_at=? WHERE id=?",(actor(),s,wz_id))
    D['sync_local_rows_to_supabase']('wz_documents','id',[wz_id])
    return redirect(url_for('order_invoice',order_id=w['order_id'],wz_id=wz_id))

@bp.get('/drivers')
def drivers():
    # Render does not retain SQLite after a deploy. Restore the operational
    # lists from Supabase before this page is rendered.
    try:
        D['pull_shared_tables_from_supabase'](force=True)
    except Exception:
        pass
    with D['conn']() as c:
        ds=c.execute('''SELECT d.*,a.username,a.auth_user_id FROM drivers d LEFT JOIN driver_accounts a ON a.driver_id=d.id WHERE d.deleted_at IS NULL ORDER BY d.active DESC,d.name''').fetchall()
        vs=c.execute('SELECT v.*,d.name driver_name FROM vehicles v LEFT JOIN drivers d ON d.id=v.driver_id WHERE v.deleted_at IS NULL ORDER BY v.active DESC,v.registration_no').fetchall()
    return render_template_string('''{% extends "base.html" %}{% block content %}
      <h1>Kierowcy i pojazdy</h1>
      {% if request.args.get('error') %}<div class="notice">{{request.args.get('error')}}</div>{% endif %}
      {% if request.args.get('ok') %}<div class="notice">{{request.args.get('ok')}}</div>{% endif %}
      <div class="row"><div class="card"><h2>Dodaj kierowcę i konto</h2><form method="post" action="{{url_for('beton.driver_add')}}"><label>Imię i nazwisko</label><input name="name" required><label>Telefon</label><input name="phone"><label>Login do panelu kierowcy</label><input name="username" placeholder="np. Kicia" required><label>Hasło kierowcy (min. 12 znaków)</label><input type="password" name="password" required><button class="btn primary" style="margin-top:12px">Dodaj kierowcę i ustaw hasło</button></form></div><div class="card"><h2>Dodaj pojazd</h2><form method="post" action="{{url_for('beton.vehicle_add')}}"><label>Numer rejestracyjny</label><input name="registration_no" required><label>Naczepa</label><input name="trailer_no"><label>Marka / model</label><input name="brand"><input name="model"><label>Domyślny kierowca</label><select name="driver_id"><option value="">—</option>{% for d in ds %}<option value="{{d.id}}">{{d.name}}</option>{% endfor %}</select><button class="btn primary" style="margin-top:12px">Dodaj pojazd</button></form></div></div>
      <div class="card"><h2>Kierowcy</h2><table><thead><tr><th>Kierowca</th><th>Login</th><th>Telefon</th><th>Status</th><th>Hasło i konto</th></tr></thead><tbody>{% for x in ds %}<tr><td><b>{{x.name}}</b></td><td>{{x.username or 'Brak konta'}}</td><td>{{x.phone or '-'}}</td><td><span class="badge">{{'Aktywny' if x.active else 'Nieaktywny'}}</span></td><td>{% if x.auth_user_id %}<form method="post" action="{{url_for('beton.driver_password',driver_id=x.id)}}"><input name="username" value="{{x.username or ''}}" placeholder="login" required><input name="password" type="password" placeholder="nowe hasło (min. 12)" required><button class="btn">Zmień hasło</button></form><form method="post" action="{{url_for('beton.driver_account_delete',driver_id=x.id)}}" style="margin-top:8px"><button class="btn" style="color:#b42318">Usuń konto</button></form>{% else %}<span class="muted">Brak konta — możesz utworzyć je przy dodawaniu kierowcy.</span>{% endif %}</td></tr>{% else %}<tr><td colspan="5">Brak kierowców.</td></tr>{% endfor %}</tbody></table></div>
      <div class="card"><h2>Pojazdy</h2><table><thead><tr><th>Rejestracja</th><th>Marka / model</th><th>Naczepa</th><th>Kierowca</th></tr></thead><tbody>{% for x in vs %}<tr><td><b>{{x.registration_no}}</b></td><td>{{x.brand or ''}} {{x.model or ''}}</td><td>{{x.trailer_no or '-'}}</td><td>{{x.driver_name or '-'}}</td></tr>{% else %}<tr><td colspan="4">Brak pojazdów.</td></tr>{% endfor %}</tbody></table></div>
    {% endblock %}''',ds=ds,vs=vs,title='Kierowcy i pojazdy',base_url=D['BASE_URL'],db_path=D['DB_PATH'])
    with D['conn']() as c:
        ds=c.execute('SELECT * FROM drivers WHERE deleted_at IS NULL ORDER BY active DESC,name').fetchall()
        vs=c.execute('SELECT v.*,d.name driver_name FROM vehicles v LEFT JOIN drivers d ON d.id=v.driver_id WHERE v.deleted_at IS NULL ORDER BY v.active DESC,v.registration_no').fetchall()
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Kierowcy i pojazdy</h1></div><div class="row"><div class="card"><h2>Dodaj kierowcę</h2><form method="post" action="{{url_for('beton.driver_add')}}"><label>Imię i nazwisko</label><input name="name" required><label>Telefon</label><input name="phone"><label>E-mail / login</label><input name="email" type="email"><button class="btn primary" style="margin-top:12px">Dodaj kierowcę</button></form></div><div class="card"><h2>Dodaj pojazd</h2><form method="post" action="{{url_for('beton.vehicle_add')}}"><div class="row"><div><label>Numer rejestracyjny</label><input name="registration_no" required></div><div><label>Naczepa</label><input name="trailer_no"></div><div><label>Marka</label><input name="brand"></div><div><label>Model</label><input name="model"></div><div><label>Rok</label><input name="year" type="number"></div><div><label>VIN</label><input name="vin"></div><div><label>Przebieg</label><input name="current_mileage" type="number" value="0"></div><div><label>Domyślny kierowca</label><select name="driver_id"><option value="">—</option>{% for d in ds %}<option value="{{d.id}}">{{d.name}}</option>{% endfor %}</select></div></div><button class="btn primary" style="margin-top:12px">Dodaj pojazd</button></form></div></div><div class="card"><h2>Kierowcy</h2><table><thead><tr><th>Kierowca</th><th>Telefon</th><th>E-mail</th><th>Status</th></tr></thead><tbody>{% for x in ds %}<tr><td><b>{{x.name}}</b></td><td>{{x.phone or '-'}}</td><td>{{x.email or '-'}}</td><td><span class="badge">{{'Aktywny' if x.active else 'Nieaktywny'}}</span></td></tr>{% endfor %}</tbody></table></div><div class="card"><h2>Pojazdy</h2><table><thead><tr><th>Rejestracja</th><th>Marka / model</th><th>Naczepa</th><th>Kierowca</th><th>Przebieg</th></tr></thead><tbody>{% for x in vs %}<tr><td><b>{{x.registration_no}}</b></td><td>{{x.brand or ''}} {{x.model or ''}}</td><td>{{x.trailer_no or '-'}}</td><td>{{x.driver_name or '-'}}</td><td>{{x.current_mileage}}</td></tr>{% endfor %}</tbody></table></div>{% endblock %}''',ds=ds,vs=vs,title='Kierowcy i pojazdy',base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.post('/drivers/add')
def driver_add():
    s=stamp()
    try:
        username=request.form.get('username','').strip()
        password=request.form.get('password','')
        driver_id=cloud_id()
        driver_row={'id':driver_id,'name':request.form['name'].strip(),'phone':request.form.get('phone','').strip(),'email':driver_auth_email(username),'active':1,'created_at':s,'updated_at':s,'deleted_at':None}
        # Supabase first: a driver is never reported as saved if the central
        # record failed. This protects against data disappearing on redeploy.
        save_row_to_supabase('drivers', driver_row)
        with D['conn']() as c:
            c.execute('INSERT INTO drivers(id,name,phone,email,active,created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?)',(driver_id,driver_row['name'],driver_row['phone'],driver_row['email'],1,s,s,None))
            driver=c.execute('SELECT * FROM drivers WHERE id=?',(driver_id,)).fetchone()
        provision_driver_account(driver_id,username,password)
        with D['conn']() as c:
            account=c.execute('SELECT * FROM driver_accounts WHERE driver_id=?',(driver_id,)).fetchone()
        save_row_to_supabase('driver_accounts', account, 'driver_id')
        return redirect(url_for('beton.drivers',ok=f'Konto kierowcy {username} zostało utworzone.'))
    except Exception as exc:
        return redirect(url_for('beton.drivers',error=str(exc)))

@bp.post('/drivers/<int:driver_id>/password')
def driver_password(driver_id):
    try:
        username=request.form.get('username','').strip()
        provision_driver_account(driver_id,username,request.form.get('password',''),update=True)
        return redirect(url_for('beton.drivers',ok=f'Hasło dla {username} zostało zmienione.'))
    except Exception as exc:
        return redirect(url_for('beton.drivers',error=str(exc)))

@bp.post('/drivers/<int:driver_id>/account/delete')
def driver_account_delete(driver_id):
    """Remove only the driver's login; the driver and operational history stay."""
    try:
        with D['conn']() as c:
            account=c.execute('SELECT * FROM driver_accounts WHERE driver_id=?',(driver_id,)).fetchone()
            driver=c.execute('SELECT name FROM drivers WHERE id=?',(driver_id,)).fetchone()
        if not account:
            raise ValueError('Ten kierowca nie ma konta do usunięcia.')
        auth_user_id=str(account['auth_user_id'] or '')
        if not auth_user_id:
            raise ValueError('Brakuje identyfikatora konta kierowcy.')
        # First remove the central account. Local SQLite may disappear after a
        # deploy, so it must never be treated as the authority for deletion.
        try:
            D['supabase_request']('/rest/v1/driver_profiles',method='DELETE',params={'driver_id':f'eq.{driver_id}'})
            D['supabase_request']('/rest/v1/driver_accounts',method='DELETE',params={'driver_id':f'eq.{driver_id}'})
            D['supabase_request'](f'/auth/v1/admin/users/{auth_user_id}',method='DELETE')
        except RuntimeError as exc:
            if 'HTTP 404' not in str(exc):
                raise
        with D['conn']() as c:
            c.execute('DELETE FROM driver_accounts WHERE driver_id=?',(driver_id,))
            c.execute('INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)',(actor(),'delete_account','driver',driver_id,'{}',stamp()))
        name=(driver['name'] if driver else 'kierowcy')
        return redirect(url_for('beton.drivers',ok=f'Konto kierowcy {name} zostało usunięte. Kierowca pozostaje na liście.'))
    except Exception as exc:
        return redirect(url_for('beton.drivers',error=f'Nie usunięto konta: {exc}'))

@bp.post('/vehicles/add')
def vehicle_add():
    s=stamp()
    try:
        vehicle_id=cloud_id()
        vehicle_row={'id':vehicle_id,'name':request.form.get('name',''),'brand':request.form.get('brand',''),'model':request.form.get('model',''),'registration_no':request.form['registration_no'].strip().upper(),'trailer_no':request.form.get('trailer_no','').strip().upper(),'year':request.form.get('year') or None,'vin':request.form.get('vin',''),'current_mileage':request.form.get('current_mileage') or 0,'driver_id':request.form.get('driver_id') or None,'active':1,'created_at':s,'updated_at':s,'deleted_at':None}
        # As above, do not leave a successful-looking local-only vehicle.
        save_row_to_supabase('vehicles', vehicle_row)
        with D['conn']() as c:
            c.execute('INSERT INTO vehicles(id,name,brand,model,registration_no,trailer_no,year,vin,current_mileage,driver_id,active,created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(vehicle_id,vehicle_row['name'],vehicle_row['brand'],vehicle_row['model'],vehicle_row['registration_no'],vehicle_row['trailer_no'],vehicle_row['year'],vehicle_row['vin'],vehicle_row['current_mileage'],vehicle_row['driver_id'],1,s,s,None))
        return redirect(url_for('beton.drivers',ok='Pojazd został zapisany w Supabase.'))
    except Exception as exc:
        return redirect(url_for('beton.drivers',error=str(exc)))

@bp.get('/transports')
def transports():
    with D['conn']() as c:
        rows=c.execute('''SELECT t.*,w.wz_no,i.invoice_no,d.name driver_name,v.registration_no,o.customer_name
          FROM transports t JOIN wz_documents w ON w.id=t.wz_id JOIN orders o ON o.id=w.order_id LEFT JOIN invoices i ON i.id=w.invoice_id
          JOIN drivers d ON d.id=t.driver_id JOIN vehicles v ON v.id=t.vehicle_id
          WHERE t.deleted_at IS NULL ORDER BY t.id DESC''').fetchall()
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>Transporty</h1><a class="btn primary right" href="{{url_for('beton.wz_list')}}">Wybierz wydane WZ</a></div><div class="card"><table><thead><tr><th>Transport</th><th>WZ</th><th>Klient</th><th>Kierowca / auto</th><th>Status</th><th>Faktura</th></tr></thead><tbody>{% for x in rows %}<tr><td><a href="{{url_for('beton.transport_view',transport_id=x.id)}}"><b>{{x.transport_no}}</b></a></td><td><a href="{{url_for('beton.wz_view',wz_id=x.wz_id)}}">{{x.wz_no}}</a></td><td>{{x.customer_name}}</td><td>{{x.driver_name}}<br>{{x.registration_no}}</td><td><span class="badge">{{x.status}}</span></td><td>{{x.invoice_no or '—'}}</td></tr>{% else %}<tr><td colspan="6">Brak transportów.</td></tr>{% endfor %}</tbody></table></div>{% endblock %}''',rows=rows,title='Transporty',base_url=D['BASE_URL'],db_path=D['DB_PATH'])

TRANSPORT_NEW_TPL = '''{% extends "base.html" %}{% block content %}
<h1>Podziel WZ na transporty</h1>
<div class="card"><form method="get"><label>Wydane WZ</label><select name="wz_id" onchange="this.form.submit()"><option value="">Wybierz WZ</option>{% for x in wz_rows %}<option value="{{x.id}}" {{'selected' if wz_id==x.id}}>{{x.wz_no}} · {{x.customer_name}}</option>{% endfor %}</select></form></div>
{% if request.args.get('created') %}<div class="notice">Kurs został utworzony. <a href="{{url_for('beton.transport_course_print',transport_id=request.args.get('created_transport'))}}" target="_blank">Drukuj WZ tego kursu</a>. Pozostałą ilość możesz przydzielić do kolejnego transportu.</div>{% endif %}
{% if wz %}<form method="post" class="card"><input type="hidden" name="wz_id" value="{{wz.id}}"><h2>{{wz.wz_no}} · {{wz.customer_name}}</h2><div class="row"><div><label>Kierowca</label><select name="driver_id" required>{% for x in ds %}<option value="{{x.id}}">{{x.name}}</option>{% endfor %}</select></div><div><label>Pojazd</label><select name="vehicle_id" required>{% for x in vs %}<option value="{{x.id}}">{{x.registration_no}}</option>{% endfor %}</select></div><div><label>Ilość na ten transport [m³]</label><input type="number" name="transport_qty" min="0.01" max="8" step="0.01" value="{{[remaining_total,8]|min}}" required></div></div><label>Adres dostawy</label><input name="destination" value="{{wz.destination or ''}}" required><p class="muted">Wpisz faktyczną ilość dla tej gruszki. Maksimum 8 m³; pozostała ilość zostanie na następne kursy.</p><table><thead><tr><th>Produkt</th><th>Na WZ [m³]</th><th>Przydzielono [m³]</th><th>Pozostało [m³]</th></tr></thead><tbody>{% for x in wz_items %}<tr><td>{{x.sku}}</td><td>{{x.qty_issued if x.qty_issued is not none else x.qty_planned}}</td><td>{{x.assigned_qty}}</td><td>{{x.remaining_qty}}</td></tr>{% endfor %}</tbody></table><p><b>Pozostało łącznie: {{remaining_total}} m³.</b></p>{% if allocation_plan %}<button class="btn primary">Utwórz transport i WZ kursu</button>{% else %}<span class="badge">Cała ilość została już przydzielona.</span>{% endif %}</form>{% endif %}{% endblock %}'''

@bp.route('/transports/new',methods=['GET','POST'])
def transport_new():
    wz_id=int(request.values.get('wz_id') or 0)
    with D['conn']() as c:
        wz_rows=c.execute("""SELECT w.id,w.wz_no,o.customer_name FROM wz_documents w JOIN orders o ON o.id=w.order_id WHERE w.status IN ('issued','in_transport') AND w.deleted_at IS NULL ORDER BY w.id DESC""").fetchall()
        ds=c.execute('SELECT * FROM drivers WHERE active=1 AND deleted_at IS NULL ORDER BY name').fetchall(); vs=c.execute('SELECT * FROM vehicles WHERE active=1 AND deleted_at IS NULL ORDER BY registration_no').fetchall()
        wz=c.execute("SELECT w.*,o.customer_name,o.note AS order_delivery_address,o.customer_address,o.delivery_date,o.delivery_time FROM wz_documents w JOIN orders o ON o.id=w.order_id WHERE w.id=? AND w.status IN ('issued','in_transport')",(wz_id,)).fetchone() if wz_id else None
        wz_items=[dict(row) for row in c.execute('''SELECT wi.*,COALESCE((SELECT SUM(ti.qty) FROM transport_items ti
              JOIN transports t ON t.id=ti.transport_id WHERE ti.wz_item_id=wi.id AND t.deleted_at IS NULL),0) AS assigned_qty
              FROM wz_items wi WHERE wi.wz_id=? ORDER BY wi.id''',(wz_id,)).fetchall()] if wz else []
        if wz:
            wz=dict(wz)
            wz['destination']=(wz.get('destination') or wz.get('order_delivery_address') or wz.get('customer_address') or '').strip()
        capacity=8.0
        allocation_plan=[]
        capacity_left=capacity
        for item in wz_items:
            issued=float(item['qty_issued'] if item['qty_issued'] is not None else item['qty_planned'] or 0)
            remaining=max(0.0,issued-float(item.get('assigned_qty') or 0))
            item['remaining_qty']=remaining
            if remaining>0 and capacity_left>0:
                qty=min(remaining,capacity_left)
                allocation_plan.append((item,qty))
                capacity_left-=qty
        allocation_by_item={int(item['id']):qty for item,qty in allocation_plan}
        remaining_total=sum(float(item['remaining_qty']) for item in wz_items)
        if request.method=='POST':
            if not wz:abort(400)
            if not ds or not vs:raise ValueError('Najpierw dodaj kierowcę i pojazd')
            if not allocation_plan:
                return 'Cała ilość z WZ jest już przydzielona do transportów.',409
            try:
                requested_qty=float((request.form.get('transport_qty') or '').replace(',','.'))
            except ValueError:
                requested_qty=0
            if requested_qty <= 0:
                return 'Podaj ilość betonu dla tego transportu w m³.', 400
            if requested_qty > capacity:
                return 'Jedna gruszka może zabrać maksymalnie 8 m³.', 400
            if requested_qty > remaining_total + 0.00001:
                return f'Pozostało tylko {remaining_total:g} m³ do przydzielenia.', 400
            planned_departure_time=request.form.get('planned_departure_time','').strip()
            if not planned_departure_time:
                return 'Podaj godzinę wyjazdu dla tego transportu.', 400
            planned_date=(wz.get('delivery_date') or '').strip()
            if not planned_date:
                return 'W zamówieniu brakuje terminu realizacji. Uzupełnij go przed przydzieleniem transportu.', 400
            allocation_plan=[]
            left=requested_qty
            for item in wz_items:
                if left <= 0.00001:
                    break
                qty=min(float(item['remaining_qty']), left)
                if qty > 0.00001:
                    allocation_plan.append((item,qty))
                    left-=qty
            destination=request.form.get('destination','').strip() or (wz['destination'] or '').strip() or (wz['order_delivery_address'] or '').strip() or (wz['customer_address'] or '').strip()
            s=stamp(); tid=cloud_id(); cur=c.execute("INSERT INTO transports(id,transport_no,wz_id,driver_id,vehicle_id,destination,status,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'assigned',?,?,?,?)",(tid,next_no(c),wz_id,request.form['driver_id'],request.form['vehicle_id'],destination,actor(),actor(),s,s));
            for item,qty in allocation_plan:c.execute('INSERT INTO transport_items(id,transport_id,wz_item_id,qty,created_at) VALUES(?,?,?,?,?)',(cloud_id(),tid,item['id'],qty,s))
            appointment_id=cloud_id()
            appointment_no=f"AW/{s[:4]}/{c.execute('SELECT COUNT(*) FROM dispatch_appointments WHERE appointment_no LIKE ?',(f'AW/{s[:4]}/%',)).fetchone()[0]+1:05d}"
            position=c.execute('SELECT COALESCE(MAX(queue_position),0)+1 FROM dispatch_appointments WHERE planned_date=?',(planned_date,)).fetchone()[0]
            c.execute('''INSERT INTO dispatch_appointments(id,appointment_no,order_id,wz_id,transport_id,driver_id,vehicle_id,planned_date,time_from,time_to,queue_position,status,notes,created_by,updated_by,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,'waiting',?,?,?,?,?)''',(appointment_id,appointment_no,wz['order_id'],wz_id,tid,request.form['driver_id'],request.form['vehicle_id'],planned_date,planned_departure_time,None,position,'',actor(),actor(),s,s))
            c.execute('INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)',(actor(),'create','transport',tid,'{}',s))
            c.commit()
            D['sync_local_rows_to_supabase']('transports','id',[tid])
            transport_item_ids=[x['id'] for x in c.execute('SELECT id FROM transport_items WHERE transport_id=?',(tid,)).fetchall()]
            D['sync_local_rows_to_supabase']('transport_items','id',transport_item_ids)
            D['sync_local_rows_to_supabase']('dispatch_appointments','id',[appointment_id])
            return redirect(url_for('beton.transport_new',wz_id=wz_id,created=1,created_transport=tid))
    # Nowy, prosty formularz zastępuje dawny automatyczny podział po 8 m³.
    transport_tpl=TRANSPORT_NEW_TPL.replace(
        '<label>Adres dostawy</label>',
        '<div class="row"><div><label>Data dostawy</label><input value="{{wz.delivery_date or \'Brak terminu w zamówieniu\'}}" readonly></div><div><label>Godzina wyjazdu</label><input type="time" name="planned_departure_time" value="{{wz.delivery_time or \'\'}}" required></div></div><label>Adres dostawy</label>',
        1,
    )
    return render_template_string(transport_tpl,wz_rows=wz_rows,wz_id=wz_id,wz=wz,wz_items=wz_items,allocation_plan=allocation_plan,allocation_by_item=allocation_by_item,remaining_total=remaining_total,capacity=capacity,capacity_left=capacity_left,ds=ds,vs=vs,base_url=D['BASE_URL'],db_path=D['DB_PATH'])
    return render_template_string('''{% extends "base.html" %}{% block content %}<h1>Przydziel transporty z dokumentu WZ</h1><div class="card"><form method="get"><label>Wydane WZ</label><select name="wz_id" onchange="this.form.submit()"><option value="">Wybierz WZ</option>{% for x in wz_rows %}<option value="{{x.id}}" {{'selected' if wz_id==x.id}}>{{x.wz_no}} · {{x.customer_name}}</option>{% endfor %}</select></form></div>{% if request.args.get('created') %}<div class="notice">Transport został przydzielony. Jeśli pozostała ilość, przydziel kolejny kurs.</div>{% endif %}{% if wz %}<form method="post" class="card"><input type="hidden" name="wz_id" value="{{wz.id}}"><h2>{{wz.wz_no}} · {{wz.customer_name}}</h2><div class="row"><div><label>Kierowca</label><select name="driver_id" required>{% for x in ds %}<option value="{{x.id}}">{{x.name}}</option>{% endfor %}</select></div><div><label>Pojazd</label><select name="vehicle_id" required>{% for x in vs %}<option value="{{x.id}}">{{x.registration_no}}</option>{% endfor %}</select></div></div><label>Adres dostawy</label><input name="destination" value="{{wz.destination or ''}}" required><p class="muted">Gruszka zabiera maksymalnie 8 m³. System sam przygotuje najbliższy kurs, a następnie pokaże pozostałą ilość do przydzielenia.</p><table><thead><tr><th>Produkt</th><th>WZ [m³]</th><th>Już przydzielono [m³]</th><th>Ten transport [m³]</th></tr></thead><tbody>{% for x in wz_items %}{% set planned=x.qty_issued if x.qty_issued is not none else x.qty_planned %}<tr><td>{{x.sku}}</td><td>{{planned}}</td><td>{{x.assigned_qty}}</td><td>{{allocation_by_item.get(x.id, '—')}}</td></tr>{% endfor %}</tbody></table><p><b>Pozostało do rozdzielenia: {{remaining_total}} m³.</b> Ten kurs: <b>{{capacity - capacity_left}} m³</b>.</p>{% if allocation_plan %}<button class="btn primary">Utwórz i przypisz transport (maks. 8 m³)</button>{% else %}<span class="badge">Cała ilość została już przydzielona do transportów.</span>{% endif %}</form>{% endif %}{% endblock %}''',wz_rows=wz_rows,wz_id=wz_id,wz=wz,wz_items=wz_items,allocation_plan=allocation_plan,allocation_by_item=allocation_by_item,remaining_total=remaining_total,capacity=capacity,capacity_left=capacity_left,ds=ds,vs=vs,base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.get('/transports/<int:transport_id>/wz-print')
def transport_course_print(transport_id):
    """Druk pojedynczego kursu; końcowa WZ pozostaje dokumentem zbiorczym."""
    with D['conn']() as c:
        transport=c.execute('''SELECT t.transport_no,t.created_at,t.destination,w.wz_no,w.issue_location,w.warehouse_location,
              o.customer_name,o.customer_address,d.name driver_name,v.registration_no
            FROM transports t JOIN wz_documents w ON w.id=t.wz_id JOIN orders o ON o.id=w.order_id
            JOIN drivers d ON d.id=t.driver_id JOIN vehicles v ON v.id=t.vehicle_id
            WHERE t.id=? AND t.deleted_at IS NULL''',(transport_id,)).fetchone()
        if not transport: abort(404)
        items=c.execute('''SELECT COALESCE(p.name,wi.sku) product,ti.qty
            FROM transport_items ti JOIN wz_items wi ON wi.id=ti.wz_item_id
            LEFT JOIN products p ON p.id=wi.product_id WHERE ti.transport_id=? ORDER BY ti.id''',(transport_id,)).fetchall()
        course_ids=[int(x['id']) for x in c.execute('SELECT id FROM transports WHERE wz_id=(SELECT wz_id FROM transports WHERE id=?) AND deleted_at IS NULL ORDER BY created_at,id',(transport_id,)).fetchall()]
        course_index=course_ids.index(int(transport_id))
        def course_suffix(index):
            value=index+1; result=''
            while value:
                value,remainder=divmod(value-1,26); result=chr(65+remainder)+result
            return result
        transport=dict(transport)
        transport['course_wz_no']=f"{transport['wz_no']}/{course_suffix(course_index)}"
        completed_ids=course_ids[:course_index+1]
        placeholders=','.join('?' for _ in completed_ids)
        cumulative=float(c.execute(f'SELECT COALESCE(SUM(qty),0) FROM transport_items WHERE transport_id IN ({placeholders})',completed_ids).fetchone()[0] or 0)
        full_items=c.execute('''SELECT COALESCE(p.name,wi.sku) product,COALESCE(wi.qty_issued,wi.qty_planned) qty
            FROM wz_items wi LEFT JOIN products p ON p.id=wi.product_id
            WHERE wi.wz_id=(SELECT wz_id FROM transports WHERE id=?) ORDER BY wi.id''',(transport_id,)).fetchall()
        full_total=sum(float(x['qty'] or 0) for x in full_items)
        transport['is_final_course']=cumulative+0.00001>=full_total
        adjustment=c.execute('SELECT * FROM transport_delivery_adjustments WHERE transport_id=? ORDER BY id DESC LIMIT 1',(transport_id,)).fetchone()
        technology=[]
        for row in c.execute('SELECT snapshot_json FROM wz_technology_snapshots WHERE wz_id=(SELECT wz_id FROM transports WHERE id=?) ORDER BY id',(transport_id,)).fetchall():
            try: technology.append(json.loads(row['snapshot_json']))
            except Exception: pass
        wz_data=c.execute('''SELECT w.*,o.order_no,o.customer_name,o.customer_address,o.customer_phone,o.customer_email,o.delivery_method,o.note AS order_delivery_address,COALESCE(cu.nip,'') customer_nip,COALESCE(NULLIF(t.destination,''),NULLIF(w.destination,''),o.customer_address) destination FROM transports t JOIN wz_documents w ON w.id=t.wz_id JOIN orders o ON o.id=w.order_id LEFT JOIN customers cu ON cu.id=o.customer_id WHERE t.id=?''',(transport_id,)).fetchone()
        wz_data=dict(wz_data)
        company=c.execute('SELECT * FROM company_profile WHERE id=1').fetchone(); company=dict(company) if company else {}
    course_tpl='''<!doctype html><html lang="pl"><meta charset="utf-8"><title>{{t.course_wz_no}}</title><style>body{font:14px Arial;max-width:900px;margin:35px auto;color:#111}table{border-collapse:collapse;width:100%;margin:22px 0}td,th{border:1px solid #222;padding:9px;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:25px}.sign{margin-top:60px;border-top:1px solid #111;padding-top:8px;width:40%;text-align:center}.full-wz{page-break-before:always;padding-top:20px}@media print{button{display:none}}</style><button onclick="print()">Drukuj</button><h1>WZ kursu {{t.course_wz_no}}</h1><p>Kurs: <b>{{t.transport_no}}</b> · dokument główny: <b>{{t.wz_no}}</b></p><div class="grid"><div><b>Odbiorca</b><br>{{t.customer_name}}<br>{{t.customer_address or ''}}<br><br><b>Adres dostawy</b><br>{{t.destination or '—'}}</div><div><b>Kierowca / auto</b><br>{{t.driver_name}} · {{t.registration_no}}<br><br><b>Miejsce wydania</b><br>{{t.issue_location}} → {{t.warehouse_location}}</div></div><table><thead><tr><th>Produkt</th><th>Ilość kursu [m³]</th></tr></thead><tbody>{% for i in items %}<tr><td>{{i.product}}</td><td>{{i.qty}}</td></tr>{% endfor %}</tbody></table><div class="sign">Podpis odbiorcy dla kursu</div>{% if t.is_final_course %}<section class="full-wz"><h1>Wydanie zewnętrzne {{t.wz_no}}</h1><p><b>Pełna WZ zbiorcza — dołączona do ostatniego kursu.</b></p><div class="grid"><div><b>Odbiorca</b><br>{{t.customer_name}}<br>{{t.customer_address or ''}}</div><div><b>Adres dostawy</b><br>{{t.destination or '—'}}<br><br><b>Miejsce wydania</b><br>{{t.issue_location}} → {{t.warehouse_location}}</div></div><table><thead><tr><th>Produkt</th><th>Łączna ilość [m³]</th></tr></thead><tbody>{% for i in full_items %}<tr><td>{{i.product}}</td><td>{{i.qty}}</td></tr>{% endfor %}</tbody></table><div class="sign">Podpis i pieczęć odbiorcy — pełna WZ</div></section>{% endif %}</html>'''
    extra='''{% for tech in technology %}<section><h2>SPECYFIKACJA TECHNICZNA</h2>{% for label,key in [('Klasa betonu','concrete_class'),('Nr receptury','recipe_no'),('Wersja','version_no'),('Cement','cement_type'),('Konsystencja','consistency'),('W/S','water_cement_ratio'),('Klasa ekspozycji','exposure_class'),('Maks. wymiar kruszywa','max_aggregate_size'),('Klasa chlorkĂłw','chloride_class'),('WytrzymaĹ‚oĹ›Ä‡','characteristic_strength'),('Dokument odniesienia','reference_document'),('Domieszki','admixtures'),('WĹ‚Ăłkna','fibres'),('Inne dodatki','other_additions')] %}{% if tech.get(key) not in [none,''] %}<div><b>{{label}}:</b> {{tech.get(key)}}</div>{% endif %}{% endfor %}</section>{% endfor %}{% if adjustment %}<section><h2>Dane konkretnej dostawy</h2>{% if adjustment.water_added %}<p><b>Dodano wodÄ™ na ĹĽÄ…danie odbiorcy:</b> {{adjustment.water_qty or 'â€”'}} {{adjustment.water_unit or 'l'}} · {{adjustment.event_at or ''}}</p>{% endif %}{% if adjustment.added_fibres %}<p><b>Dodano wĹ‚Ăłkna:</b> {{adjustment.added_fibres}}</p>{% endif %}{% if adjustment.added_chemicals %}<p><b>Dodano chemiÄ™:</b> {{adjustment.added_chemicals}}</p>{% endif %}{% if adjustment.other_additions %}<p><b>Inne dodatki:</b> {{adjustment.other_additions}}</p>{% endif %}{% if adjustment.notes %}<p><b>Uwagi:</b> {{adjustment.notes}}</p>{% endif %}<p><b>Osoba odpowiedzialna:</b> {{adjustment.responsible_person or adjustment.created_by}}</p></section>{% endif %}<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:40px;margin-top:70px"><div class="sign">Operator betoniarni</div><div class="sign">Kierowca</div><div class="sign">Odbiorca</div></div></html>'''
    if transport['is_final_course']:
        print_items=[{'sku':x['product'],'qty_planned':x['qty'],'qty_issued':x['qty']} for x in full_items]
    else:
        wz_data['wz_no']=transport['course_wz_no']
        print_items=[{'sku':x['product'],'qty_planned':x['qty'],'qty_issued':x['qty']} for x in items]
    pdf_buffer=build_wz_form_pdf(wz_data,print_items,[transport],technology,company)
    filename=re.sub(r'[^A-Za-z0-9_.-]+','_',wz_data['wz_no'])+'.pdf'
    return send_file(pdf_buffer,mimetype='application/pdf',as_attachment=False,download_name=filename)
    course_tpl=course_tpl.replace('</html>',extra)
    return render_template_string(course_tpl,t=transport,items=items,full_items=full_items,technology=technology,adjustment=adjustment)
    return render_template_string('''<!doctype html><html lang="pl"><meta charset="utf-8"><title>WZ kursu {{t.transport_no}}</title><style>body{font:14px Arial;max-width:900px;margin:35px auto}table{border-collapse:collapse;width:100%;margin:22px 0}td,th{border:1px solid #222;padding:9px;text-align:left}.grid{display:grid;grid-template-columns:1fr 1fr;gap:25px}.sign{margin-top:60px;border-top:1px solid #111;padding-top:8px;width:40%;text-align:center}@media print{button{display:none}}</style><button onclick="print()">Drukuj</button><h1>WZ cząstkowa / kurs {{t.transport_no}}</h1><p>Dokument do WZ zbiorczej: <b>{{t.wz_no}}</b></p><div class="grid"><div><b>Odbiorca</b><br>{{t.customer_name}}<br>{{t.customer_address or ''}}<br><br><b>Adres dostawy</b><br>{{t.destination or '—'}}</div><div><b>Kierowca / auto</b><br>{{t.driver_name}} · {{t.registration_no}}<br><br><b>Miejsce wydania</b><br>{{t.issue_location}} → {{t.warehouse_location}}</div></div><table><thead><tr><th>Produkt</th><th>Ilość [m³]</th></tr></thead><tbody>{% for i in items %}<tr><td>{{i.product}}</td><td>{{i.qty}}</td></tr>{% endfor %}</tbody></table><div class="sign">Podpis odbiorcy dla kursu</div></html>''',t=transport,items=items)

@bp.get('/transports/<int:transport_id>')
def transport_view(transport_id):
    with D['conn']() as c:
        x=c.execute('''SELECT t.*,w.wz_no,w.invoice_id,i.invoice_no,d.name driver_name,v.registration_no,o.customer_name FROM transports t JOIN wz_documents w ON w.id=t.wz_id LEFT JOIN invoices i ON i.id=w.invoice_id JOIN orders o ON o.id=w.order_id JOIN drivers d ON d.id=t.driver_id JOIN vehicles v ON v.id=t.vehicle_id WHERE t.id=?''',(transport_id,)).fetchone()
        if not x:abort(404)
        items=c.execute('''SELECT ti.qty, COALESCE(p.name, w.sku) AS sku
            FROM transport_items ti JOIN wz_items w ON w.id=ti.wz_item_id
            LEFT JOIN products p ON p.id=w.product_id WHERE ti.transport_id=?''',(transport_id,)).fetchall()
        adjustment=c.execute('SELECT * FROM transport_delivery_adjustments WHERE transport_id=? ORDER BY id DESC LIMIT 1',(transport_id,)).fetchone()
    detail_tpl='''{% extends "base.html" %}{% block content %}<div class="flex"><h1>{{x.transport_no}}</h1><span class="badge">{{x.status}}</span><a class="btn right" href="{{url_for('beton.wz_view',wz_id=x.wz_id)}}">{{x.wz_no}}</a><a class="btn" target="_blank" href="{{url_for('beton.transport_course_print',transport_id=x.id)}}">Drukuj WZ kursu</a></div><div class="card"><div class="grid3"><div><span class="muted">Klient</span><br><b>{{x.customer_name}}</b></div><div><span class="muted">Kierowca</span><br><b>{{x.driver_name}}</b></div><div><span class="muted">Pojazd</span><br><b>{{x.registration_no}}</b></div></div><table><tbody>{% for i in items %}<tr><td>{{i.sku}}</td><td><b>{{i.qty}} mÂł</b></td></tr>{% endfor %}</tbody></table></div><form method="post" action="{{url_for('beton.transport_adjustment_save',transport_id=x.id)}}" class="card"><h2>Dane konkretnej dostawy</h2><div class="grid3"><div><label><input type="checkbox" name="water_added" value="1" {{'checked' if adjustment and adjustment.water_added}}> Dodano wodÄ™ na ĹĽÄ…danie odbiorcy</label></div><div><label>IloĹ›Ä‡ wody</label><input type="number" step="0.01" name="water_qty" value="{{adjustment.water_qty if adjustment else ''}}"></div><div><label>Jednostka</label><select name="water_unit"><option>l</option><option>kg</option></select></div><div><label>Data i godzina</label><input type="datetime-local" name="event_at" value="{{(adjustment.event_at or '')[:16] if adjustment else ''}}"></div><div><label>Osoba odpowiedzialna</label><input name="responsible_person" value="{{adjustment.responsible_person or '' if adjustment else ''}}"></div><div><label>Dodano wĹ‚Ăłkna</label><input name="added_fibres" value="{{adjustment.added_fibres or '' if adjustment else ''}}"></div><div><label>Dodano chemiÄ™</label><input name="added_chemicals" value="{{adjustment.added_chemicals or '' if adjustment else ''}}"></div><div><label>Inne dodatki</label><input name="other_additions" value="{{adjustment.other_additions or '' if adjustment else ''}}"></div></div><label>Uwagi</label><textarea name="notes">{{adjustment.notes or '' if adjustment else ''}}</textarea><button class="btn primary">Zapisz dane dostawy</button></form>{% endblock %}'''
    return render_template_string(detail_tpl,x=x,items=items,adjustment=adjustment,title=x['transport_no'],base_url=D['BASE_URL'],db_path=D['DB_PATH'])
    return render_template_string('''{% extends "base.html" %}{% block content %}<div class="flex"><h1>{{x.transport_no}}</h1><span class="badge">{{x.status}}</span><a class="btn right" href="{{url_for('beton.wz_view',wz_id=x.wz_id)}}">{{x.wz_no}}</a>{% if x.invoice_id %}<a class="btn" href="{{url_for('invoice_download_admin',invoice_id=x.invoice_id)}}">Pobierz fakturę</a>{% endif %}</div><div class="card"><div class="grid3"><div><span class="muted">Klient</span><br><b>{{x.customer_name}}</b></div><div><span class="muted">Kierowca</span><br><b>{{x.driver_name}}</b></div><div><span class="muted">Pojazd</span><br><b>{{x.registration_no}}</b></div></div><div class="line"></div><table><thead><tr><th>Materiał / SKU</th><th>Ilość</th></tr></thead><tbody>{% for i in items %}<tr><td>{{i.sku}}</td><td><b>{{i.qty}}</b></td></tr>{% endfor %}</tbody></table></div>{% endblock %}''',x=x,items=items,title=x['transport_no'],base_url=D['BASE_URL'],db_path=D['DB_PATH'])

@bp.post('/transports/<int:transport_id>/delivery-data')
def transport_adjustment_save(transport_id):
    now=stamp(); adjustment_id=cloud_id()
    with D['conn']() as c:
        if not c.execute('SELECT 1 FROM transports WHERE id=? AND deleted_at IS NULL',(transport_id,)).fetchone(): abort(404)
        existing=c.execute('SELECT id FROM transport_delivery_adjustments WHERE transport_id=? ORDER BY id DESC LIMIT 1',(transport_id,)).fetchone()
        values=(1 if request.form.get('water_added') else 0,request.form.get('water_qty') or None,request.form.get('water_unit') or 'l',request.form.get('event_at') or now,request.form.get('added_fibres','').strip(),request.form.get('added_chemicals','').strip(),request.form.get('other_additions','').strip(),request.form.get('notes','').strip(),request.form.get('responsible_person','').strip(),actor(),now)
        if existing:
            adjustment_id=int(existing['id']); c.execute('''UPDATE transport_delivery_adjustments SET water_added=?,water_qty=?,water_unit=?,event_at=?,added_fibres=?,added_chemicals=?,other_additions=?,notes=?,responsible_person=?,created_by=?,updated_at=? WHERE id=?''',values+(adjustment_id,))
        else:
            c.execute('''INSERT INTO transport_delivery_adjustments(id,transport_id,water_added,water_qty,water_unit,event_at,added_fibres,added_chemicals,other_additions,notes,responsible_person,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(adjustment_id,transport_id)+values[:-1]+(now,now))
    D['sync_local_rows_to_supabase']('transport_delivery_adjustments','id',[adjustment_id])
    return redirect(url_for('beton.transport_view',transport_id=transport_id))

def current_driver_id():
    """Resolve the logged-in Supabase account to the internal driver record."""
    auth_user_id=str((getattr(g,'client_user',{}) or {}).get('id') or '').strip()
    if not auth_user_id:
        return None
    with D['conn']() as c:
        row=c.execute('''SELECT d.id FROM driver_accounts a
            JOIN drivers d ON d.id=a.driver_id
            WHERE a.auth_user_id=? AND d.active=1 AND d.deleted_at IS NULL''',(auth_user_id,)).fetchone()
    return int(row['id']) if row else None


@driver_api.get('/transports')
def driver_transports_api():
    try:
        D['pull_shared_tables_from_supabase'](force=True)
    except Exception:
        pass
    driver_id=current_driver_id()
    if not driver_id:
        return jsonify(ok=False,error='Konto kierowcy nie jest powiązane z kierowcą w panelu głównym.'),403
    with D['conn']() as c:
        rows=c.execute('''SELECT t.id,t.transport_no,t.wz_id,w.wz_no,w.invoice_id,t.destination,t.status,t.issued_at,t.departed_at,t.delivered_at,t.returned_at,t.receiver_name,t.driver_notes,i.invoice_no,o.customer_name,v.registration_no,
          EXISTS(SELECT 1 FROM delivery_photos dp WHERE dp.transport_id=t.id AND dp.deleted_at IS NULL) AS has_signed_wz_photo,
          COALESCE(NULLIF(t.destination,''),NULLIF(w.destination,''),NULLIF(o.note,''),o.customer_address) AS delivery_address,
          (SELECT a.status FROM dispatch_appointments a WHERE a.transport_id=t.id ORDER BY a.id DESC LIMIT 1) plant_status,
          (SELECT a.planned_date FROM dispatch_appointments a WHERE a.transport_id=t.id ORDER BY a.id DESC LIMIT 1) planned_date,
          (SELECT a.time_from FROM dispatch_appointments a WHERE a.transport_id=t.id ORDER BY a.id DESC LIMIT 1) planned_departure_time,
          (SELECT a.time_to FROM dispatch_appointments a WHERE a.transport_id=t.id ORDER BY a.id DESC LIMIT 1) planned_delivery_time,
          (SELECT b.code FROM dispatch_appointments a LEFT JOIN loading_bays b ON b.id=a.loading_bay_id WHERE a.transport_id=t.id ORDER BY a.id DESC LIMIT 1) loading_bay
          FROM transports t JOIN drivers d ON d.id=t.driver_id JOIN wz_documents w ON w.id=t.wz_id JOIN orders o ON o.id=w.order_id LEFT JOIN invoices i ON i.id=w.invoice_id JOIN vehicles v ON v.id=t.vehicle_id WHERE t.driver_id=? AND d.active=1 AND d.deleted_at IS NULL AND t.deleted_at IS NULL ORDER BY t.id DESC''',(driver_id,)).fetchall()
        result=[]
        for r in rows:
            x=dict(r); x['destination']=(x.get('destination') or x.get('delivery_address') or '').strip(); x['items']=[dict(z) for z in c.execute('''SELECT COALESCE(p.name, w.sku) AS sku, ti.qty
                FROM transport_items ti JOIN wz_items w ON w.id=ti.wz_item_id
                LEFT JOIN products p ON p.id=w.product_id WHERE ti.transport_id=?''',(r['id'],))]; result.append(x)
    return jsonify(ok=True,transports=result)

@driver_api.post('/transports/<int:transport_id>/status')
def driver_transport_status_api(transport_id):
    driver_id=current_driver_id(); email=(g.client_user.get('email') or '').strip().lower(); data=request.get_json(silent=True) or {}; status=str(data.get('status',''))
    appointment_id_to_sync=None
    if not driver_id:return jsonify(ok=False,error='Konto kierowcy nie jest powiązane z kierowcą w panelu głównym.'),403
    allowed={'closed','delivered','returned','problem'}
    if status not in allowed:return jsonify(ok=False,error='Niedozwolony status'),400
    field={'issued':'issued_at','in_transit':'departed_at','delivered':'delivered_at','returned':'returned_at'}.get(status)
    with D['conn']() as c:
        row=c.execute('''SELECT t.id,t.status,t.wz_id,t.issued_at,t.departed_at,t.delivered_at,t.returned_at,t.updated_at
          FROM transports t JOIN drivers d ON d.id=t.driver_id
          WHERE t.id=? AND t.driver_id=? AND d.active=1 AND t.deleted_at IS NULL''',(transport_id,driver_id)).fetchone()
        if not row:return jsonify(ok=False,error='Brak dostępu'),403
        transitions={'in_transit':{'closed','problem'},'closed':{'delivered','problem'},'delivered':{'returned','problem'},'problem':{'closed','delivered','returned'}}
        if status not in transitions.get(row['status'],set()):return jsonify(ok=False,error='Nieprawidłowa kolejność statusów'),409
        # Tylko kierowca ma obowiązkową przerwę między kolejnymi etapami.
        # Pracownik, dyspozytor i administrator zmieniają etap w panelu głównym bez tej blokady.
        previous=[row[key] for key in ('issued_at','departed_at','delivered_at','returned_at') if row[key]]
        if row['status']=='closed' and row['updated_at']:
            previous.append(row['updated_at'])
        if previous and status != 'problem':
            try:
                last_action=max(previous)
                elapsed=(datetime.strptime(stamp(), '%Y-%m-%d %H:%M:%S')-datetime.strptime(last_action, '%Y-%m-%d %H:%M:%S')).total_seconds()
                if 0 <= elapsed < 300:
                    return jsonify(ok=False,error='Zbyt szybko kliknięto kolejny etap. Potwierdzenie nie jest teraz możliwe.'),429
            except (TypeError, ValueError):
                pass
        appointment=c.execute("SELECT id,status FROM dispatch_appointments WHERE transport_id=? ORDER BY id DESC LIMIT 1",(transport_id,)).fetchone()
        if status in {'issued','in_transit'} and appointment and appointment['status']!='ready_to_leave':
            return jsonify(ok=False,error='Dyspozytor musi najpierw oznaczyć transport jako gotowy do wyjazdu.'),409
        sql='UPDATE transports SET status=?,driver_notes=?,receiver_name=?,updated_by=?,updated_at=?'+(f',{field}=?' if field else '')+' WHERE id=?'; values=[status,str(data.get('notes',''))[:2000],str(data.get('receiver_name',''))[:200],email,stamp()]
        if field:values.append(stamp())
        values.append(transport_id); c.execute(sql,values)
        # Podpisane WZ zamyka część dostawczą i od razu daje księgowości
        # możliwość wystawienia faktury. Powrót auta na bazę pozostaje
        # osobnym etapem logistycznym, ale nie blokuje fakturowania.
        if status=='delivered':
            c.execute("UPDATE wz_documents SET status='ready_invoice',ready_by=?,ready_at=? WHERE id=? AND status IN ('issued','in_transport')",(email,stamp(),row['wz_id']))
        elif status=='returned':
            c.execute("UPDATE wz_documents SET status='returned' WHERE id=? AND status IN ('issued','in_transport')",(row['wz_id'],))
        elif status in {'issued','in_transit','closed'}:
            c.execute("UPDATE wz_documents SET status='in_transport' WHERE id=? AND status='issued'",(row['wz_id'],))
        if status=='in_transit' and appointment:
            c.execute("UPDATE dispatch_appointments SET status='departed',updated_by=?,updated_at=? WHERE id=?",(email,stamp(),appointment['id']))
            c.execute("INSERT INTO appointment_status_history(appointment_id,old_status,new_status,reason,actor,created_at) VALUES(?,?,?,?,?,?)",(appointment['id'],'ready_to_leave','departed','Potwierdzenie wyjazdu przez kierowcę',email,stamp()))
            appointment_id_to_sync=appointment['id']
        c.execute('INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)',(email,'status:'+status,'transport',transport_id,'{}',stamp()))
    D['sync_local_rows_to_supabase']('transports','id',[transport_id])
    D['sync_local_rows_to_supabase']('wz_documents','id',[row['wz_id']])
    if appointment_id_to_sync:
        D['sync_local_rows_to_supabase']('dispatch_appointments','id',[appointment_id_to_sync])
    return jsonify(ok=True,status=status)

@driver_api.get('/transports/<int:transport_id>/invoice')
def driver_invoice_api(transport_id):
    # Faktury są dokumentami księgowymi dostępnymi wyłącznie w panelu głównym.
    return jsonify(ok=False,error='Faktury nie są dostępne w panelu kierowcy.'),403

@driver_api.post('/transports/<int:transport_id>/photos')
def driver_delivery_photo_api(transport_id):
    driver_id=current_driver_id()
    if not driver_id:return jsonify(ok=False,error='Konto kierowcy nie jest powiązane z kierowcą w panelu głównym.'),403
    photo=request.files.get('photo')
    if not photo or not photo.filename:
        return jsonify(ok=False,error='Wybierz zdjęcie.'),400
    if photo.mimetype not in {'image/jpeg','image/png','image/webp'}:
        return jsonify(ok=False,error='Dozwolone są zdjęcia JPG, PNG lub WEBP.'),400
    raw=photo.read()
    if not raw or len(raw)>10*1024*1024:
        return jsonify(ok=False,error='Zdjęcie jest puste lub większe niż 10 MB.'),400
    with D['conn']() as c:
        row=c.execute('SELECT t.id,t.status FROM transports t JOIN drivers d ON d.id=t.driver_id WHERE t.id=? AND t.driver_id=? AND d.active=1 AND t.deleted_at IS NULL',(transport_id,driver_id)).fetchone()
    if not row:
        return jsonify(ok=False,error='Brak dostępu do transportu.'),403
    if row['status'] != 'delivered':
        return jsonify(ok=False,error='Zdjęcie podpisanego WZ można dodać po potwierdzeniu etapu „WZ podpisane”.'),409
    ext={'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[photo.mimetype]
    object_path=f"{g.client_user['id']}/{transport_id}/{int(time.time()*1000)}-{os.urandom(4).hex()}.{ext}"
    try:
        storage_ref=D['supabase_storage_upload_bytes'](raw,object_path,bucket='delivery-photos',content_type=photo.mimetype)
        photo_id=cloud_id()
        with D['conn']() as c:
            c.execute('INSERT INTO delivery_photos(id,transport_id,storage_ref,photo_type,caption,created_by,created_at) VALUES(?,?,?,?,?,?,?)',(photo_id,transport_id,storage_ref,'signed_wz','Podpisane WZ',g.client_user['id'],stamp()))
        D['sync_local_rows_to_supabase']('delivery_photos','id',[photo_id])
        return jsonify(ok=True)
    except Exception:
        current_app.logger.exception('Nie udało się przesłać zdjęcia dostawy')
        return jsonify(ok=False,error='Nie udało się zapisać zdjęcia. Spróbuj ponownie.'),502
