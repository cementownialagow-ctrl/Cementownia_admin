import ast
import sqlite3
from pathlib import Path


def load_inventory_functions():
    tree = ast.parse(Path("beton_logistics_module.py").read_text(encoding="utf-8"))
    wanted = {"issue_recipe_materials", "reverse_recipe_materials"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"cloud_id": iter(range(1000, 2000)).__next__}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "inventory-functions", "exec"), namespace)
    return namespace


def test_issue_and_exact_reversal():
    fn = load_inventory_functions()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,sku TEXT);
      CREATE TABLE wz_items(id INTEGER PRIMARY KEY,wz_id INTEGER,product_id INTEGER,qty_planned REAL,qty_issued REAL);
      CREATE TABLE raw_materials(id INTEGER PRIMARY KEY,name TEXT,unit TEXT);
      CREATE TABLE product_recipes(id INTEGER PRIMARY KEY,product_id INTEGER,material_id INTEGER,qty_per_unit REAL);
      CREATE TABLE raw_material_stock(material_id INTEGER PRIMARY KEY,qty REAL);
      CREATE TABLE raw_material_movements(id INTEGER PRIMARY KEY,material_id INTEGER,wz_id INTEGER,qty_delta REAL,movement_type TEXT,note TEXT,reversed_movement_id INTEGER,created_by TEXT,created_at TEXT);
    """)
    c.execute("INSERT INTO products VALUES(1,'Beton B25','B25')")
    c.execute("INSERT INTO wz_items VALUES(1,10,1,2,NULL)")
    c.executemany("INSERT INTO raw_materials VALUES(?,?,?)", [(1, "cement B25", "kg"), (2, "piasek", "kg")])
    c.executemany("INSERT INTO product_recipes VALUES(?,?,?,?,?)".replace("?,?,?,?,?", "?,?,?,?"), [(1, 1, 1, 300), (2, 1, 2, 700)])
    c.executemany("INSERT INTO raw_material_stock VALUES(?,?)", [(1, 1000), (2, 2000)])

    fn["issue_recipe_materials"](c, 10, "tester", "2026-08-11T10:00:00")
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=1").fetchone()[0] == 400
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=2").fetchone()[0] == 600

    # Zmiana receptury po wydaniu nie może zmienić ilości zwracanej przy cofnięciu.
    c.execute("UPDATE product_recipes SET qty_per_unit=999 WHERE material_id=1")
    assert fn["reverse_recipe_materials"](c, 10, "tester", "2026-08-11T11:00:00") == 2
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=1").fetchone()[0] == 1000
    assert c.execute("SELECT qty FROM raw_material_stock WHERE material_id=2").fetchone()[0] == 2000
    assert fn["reverse_recipe_materials"](c, 10, "tester", "2026-08-11T12:00:00") == 0


if __name__ == "__main__":
    test_issue_and_exact_reversal()
    print("OK: recipe issue and reversal")
