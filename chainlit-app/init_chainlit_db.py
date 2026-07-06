import sqlite3

def ensure_column(cursor, table_name, column_name, column_definition):
    cursor.execute(f'PRAGMA table_info("{table_name}")')
    existing_columns = {row[1] for row in cursor.fetchall()}
    if column_name not in existing_columns:
        cursor.execute(
            f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_definition}'
        )
        print(f'➕ เพิ่ม column {table_name}.{column_name}')

def migrate_chainlit_schema(cursor):
    ensure_column(cursor, "steps", "defaultOpen", "INTEGER DEFAULT 0")
    ensure_column(cursor, "steps", "autoCollapse", "INTEGER DEFAULT 0")
    ensure_column(cursor, "elements", "props", "TEXT DEFAULT '{}'")

def init_db():
    print("⏳ กำลังสร้างตารางประวัติแชทให้ Chainlit...")
    
    # เชื่อมต่อไปที่ไฟล์ SQLite ตรงๆ
    conn = sqlite3.connect('chainlit_history.db')
    cursor = conn.cursor()
    
    # 1. Table: users
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        "id" TEXT PRIMARY KEY,
        "identifier" TEXT NOT NULL UNIQUE,
        "metadata" TEXT NOT NULL,
        "createdAt" TEXT
    )''')

    # 2. Table: threads
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS threads (
        "id" TEXT PRIMARY KEY,
        "createdAt" TEXT,
        "name" TEXT,
        "userId" TEXT,
        "userIdentifier" TEXT,
        "tags" TEXT,
        "metadata" TEXT,
        FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
    )''')

    # 3. Table: steps
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS steps (
        "id" TEXT PRIMARY KEY,
        "name" TEXT NOT NULL,
        "type" TEXT NOT NULL,
        "threadId" TEXT NOT NULL,
        "parentId" TEXT,
        "disableFeedback" INTEGER NOT NULL DEFAULT 0,
        "streaming" INTEGER NOT NULL DEFAULT 0,
        "waitForAnswer" INTEGER,
        "isError" INTEGER,
        "metadata" TEXT,
        "tags" TEXT,
        "input" TEXT,
        "output" TEXT,
        "createdAt" TEXT,
        "start" TEXT,
        "end" TEXT,
        "generation" TEXT,
        "showInput" TEXT,
        "language" TEXT,
        "indent" INTEGER
    )''')

    # 4. Table: elements
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS elements (
        "id" TEXT PRIMARY KEY,
        "threadId" TEXT,
        "type" TEXT,
        "url" TEXT,
        "chainlitKey" TEXT,
        "name" TEXT NOT NULL,
        "display" TEXT,
        "objectKey" TEXT,
        "size" TEXT,
        "page" INTEGER,
        "language" TEXT,
        "forId" TEXT,
        "mime" TEXT
    )''')

    # 5. Table: feedbacks
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS feedbacks (
        "id" TEXT PRIMARY KEY,
        "forId" TEXT NOT NULL,
        "threadId" TEXT NOT NULL,
        "value" INTEGER NOT NULL,
        "comment" TEXT
    )''')

    migrate_chainlit_schema(cursor)

    conn.commit()
    conn.close()
    
    print("✅ สร้างตารางสำเร็จแล้ว! (โครงสร้างฐานข้อมูลพร้อมใช้งาน)")

if __name__ == "__main__":
    init_db()
