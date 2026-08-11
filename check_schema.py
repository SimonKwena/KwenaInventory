import sqlite3
conn = sqlite3.connect('db.sqlite3')
c = conn.cursor()
c.execute('SELECT sql FROM sqlite_master WHERE name="inventory_userprofile"')
print(c.fetchone()[0])
conn.close()
