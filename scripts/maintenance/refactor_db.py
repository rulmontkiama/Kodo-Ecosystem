import re
import os

FILES = ["main_app.py", "views/modals.py", "views/stats_view.py", "database_manager.py"]

for fpath in FILES:
    if not os.path.exists(fpath): continue
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()

    # Inject imports for db_query, db_transaction if get_connection is there
    if "get_connection" in content and "db_transaction" not in content and "database_manager.py" not in fpath:
        content = content.replace("from database_manager import get_connection", "from database_manager import get_connection, db_transaction, db_query")

    # Replace try/finally get_connection blocks
    # We will just replace `conn = get_connection(); c = conn.cursor()` with `with db_transaction() as c:` or `with db_query() as c:` but indenting is hard.
    # Alternative: 
    # Just do a text replacement for `conn = get_connection(); c = conn.cursor()`
    # Actually, simpler: we replace the database_manager.get_connection definition to NOT be used directly if possible, or we just manually edit them.

print("Done")
