import ast
import json
import sqlite3
from pathlib import Path


def load_inventory_functions():
    tree = ast.parse(Path("beton_logistics_module.py").read_text(encoding="utf-8"))
    wanted = {"snapshot_wz_technology", "issue_recipe_materials", "reverse_recipe_materials"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"cloud_id": iter(range(1000, 2000)).__next__,"json":json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "inventory-functions", "exec"), namespace)
    return namespace


def test_issue_and_exact_reversal():
    fn = load_inventory_functions()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,sku TEXT,model TEXT,unit TEXT);
      CREATE TABLE wz_items(id INTEGER PRIMARY KEY,wz_id INTEGER,product_id INTEGER,qty_planned REAL,qty_issued REAL);
      CREATE TABLE raw_materials(id INTEGER PRIMARY KEY,name TEXT,unit TEXT,code TEXT,material_type TEXT,manufacturer TEXT,trade_name TEXT,reference_document TEXT,technical_designation TEXT,description TEXT,cement_type TEXT,cement_designation TEXT,strength_class TEXT,aggregate_type TEXT,fraction TEXT,max_grain_size TEXT);
      CREATE TABLE product_recipes(id INTEGER PRIMARY KEY,product_id INTEGER,material_id INTEGER,qty_per_unit REAL);
      CREATE TABLE recipe_versions(id INTEGER PRIMARY KEY,product_id INTEGER,version_no INTEGER,recipe_no TEXT,name TEXT,valid_from TEXT,concrete_class TEXT,consistency TEXT,water_cement_ratio REAL,exposure_class TEXT,max_aggregate_size TEXT,chloride_class TEXT,characteristic_strength TEXT,reference_document TEXT,cement_type TEXT,admixtures TEXT,fibres TEXT,other_additions TEXT,technology_notes TEXT);
      CREATE TABLE recipe_version_items(id INTEGER PRIMARY KEY,recipe_version_id INTEGER,material_id INTEGER,qty_per_unit REAL,unit TEXT,material_snapshot_json TEXT);
      CREATE TABLE wz_technology_snapshots(id INTEGER PRIMARY KEY,wz_id INTEGER,wz_item_id INTEGER UNIQUE,product_id INTEGER,recipe_version_id INTEGER,snapshot_json TEXT,created_at TEXT);
      CREATE TABLE raw_material_stock(material_id INTEGER PRIMARY KEY,qty REAL);
      CREATE TABLE raw_material_movements(id INTEGER PRIMARY KEY,material_id INTEGER,wz_id INTEGER,qty_delta REAL,movement_type TEXT,note TEXT,reversed_movement_id INTEGER,created_by TEXT,created_at TEXT);
    """)
    c.execute("INSERT INTO products VALUES(1,'Beton B25','B25','B25','m3')")
    c.execute("INSERT INTO wz_items VALUES(1,10,1,2,NULL)")
    c.executemany("INSERT INTO raw_materials(id,name,unit,material_type,technical_designation) VALUES(?,?,?,?,?)", [(1, "cement B25", "kg","cement","CEM II/A-S 42,5"), (2, "piasek", "kg","piasek","")])
    c.executemany("INSERT INTO product_recipes VALUES(?,?,?,?,?)".replace("?,?,?,?,?", "?,?,?,?"), [(1, 1, 1, 300), (2, 1, 2, 700)])
    c.executemany("INSERT INTO raw_material_stock VALUES(?,?)", [(1, 1000), (2, 2000)])
    c.execute("INSERT INTO recipe_versions(id,product_id,version_no,recipe_no,name,valid_from,concrete_class,consistency,water_cement_ratio,exposure_class,max_aggregate_size,chloride_class,reference_document) VALUES(10,1,1,'B25-001','B25','2026-01-01','C20/25','S3',0.55,'XC2','16 mm','Cl 0,20','PN-EN 206')")
    c.executemany("INSERT INTO recipe_version_items(id,recipe_version_id,material_id,qty_per_unit,unit) VALUES(?,?,?,?,?)",[(11,10,1,300,'kg'),(12,10,2,700,'kg')])

    fn["issue_recipe_materials"](c, 10, "tester", "2026-08-11T10:00:00")
    old_snapshot=json.loads(c.execute("SELECT snapshot_json FROM wz_technology_snapshots WHERE wz_id=10").fetchone()[0])
    assert old_snapshot['consistency']=='S3' and old_snapshot['water_cement_ratio']==0.55
    assert old_snapshot['cement_type']=='CEM II/A-S 42,5'
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=1").fetchone()[0] == 400
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=2").fetchone()[0] == 600

    # Zmiana receptury po wydaniu nie może zmienić ilości zwracanej przy cofnięciu.
    c.execute("UPDATE product_recipes SET qty_per_unit=999 WHERE material_id=1")
    assert fn["reverse_recipe_materials"](c, 10, "tester", "2026-08-11T11:00:00") == 2
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=1").fetchone()[0] == 1000
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=2").fetchone()[0] == 2000
    assert fn["reverse_recipe_materials"](c, 10, "tester", "2026-08-11T12:00:00") == 0

    c.execute("INSERT INTO raw_materials(id,name,unit,material_type,technical_designation) VALUES(3,'cement nowy','kg','cement','CEM TEST 52,5')")
    c.execute("INSERT INTO raw_material_stock VALUES(3,1000)")
    c.execute("INSERT INTO recipe_versions(id,product_id,version_no,recipe_no,name,valid_from,concrete_class,consistency,water_cement_ratio,exposure_class,max_aggregate_size,chloride_class,reference_document) VALUES(20,1,2,'B25-002','B25','2026-08-12','C20/25','S4',0.50,'XC2','16 mm','Cl 0,20','PN-EN 206')")
    c.executemany("INSERT INTO recipe_version_items(id,recipe_version_id,material_id,qty_per_unit,unit) VALUES(?,?,?,?,?)",[(21,20,3,300,'kg'),(22,20,2,700,'kg')])
    c.execute("INSERT INTO wz_items VALUES(2,11,1,1,NULL)")
    fn["issue_recipe_materials"](c,11,"tester","2026-08-12T10:00:00")
    assert json.loads(c.execute("SELECT snapshot_json FROM wz_technology_snapshots WHERE wz_id=10").fetchone()[0])['consistency']=='S3'
    new_snapshot=json.loads(c.execute("SELECT snapshot_json FROM wz_technology_snapshots WHERE wz_id=11").fetchone()[0])
    assert new_snapshot['consistency']=='S4' and new_snapshot['water_cement_ratio']==0.50
    assert new_snapshot['cement_type']=='CEM TEST 52,5'
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=3").fetchone()[0] == 700

    # Snapshot roboczy musi zostać zastąpiony wersją obowiązującą przy wydaniu.
    c.execute("INSERT INTO wz_items VALUES(3,12,1,1,NULL)")
    fn["snapshot_wz_technology"](c,12,"tester","2026-08-12T11:00:00")
    c.execute("UPDATE recipe_versions SET consistency='S5' WHERE id=20")
    fn["issue_recipe_materials"](c,12,"tester","2026-08-12T12:00:00")
    issued_snapshot=json.loads(c.execute("SELECT snapshot_json FROM wz_technology_snapshots WHERE wz_id=12").fetchone()[0])
    assert issued_snapshot['consistency']=='S5'


if __name__ == "__main__":
    test_issue_and_exact_reversal()
    print("OK: recipe issue and reversal")
