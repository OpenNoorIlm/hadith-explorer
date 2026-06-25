"""
main.py  –  Hadith Explorer API  (SQLite / FTS5 edition)
─────────────────────────────────────────────────────────
Run migration first:
    python migrate_to_sqlite.py

Then start the server:
    uvicorn main:app --reload
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import sqlite3
import os
from contextlib import contextmanager
from threading import local

# ── Config ────────────────────────────────────────────────
BOOKS = ["bukhari", "muslim", "malik", "nasai", "abudawud", "tirmidhi", "ibnmajah"]

DB_PATHS = ["hadiths.db", "books/hadiths.db"]

def find_db() -> str:
    for p in DB_PATHS:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "hadiths.db not found. Run: python migrate_to_sqlite.py"
    )

DB_PATH = find_db()

# ── Per-thread SQLite connections (thread-local pool) ─────
_local = local()

def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA query_only=ON")   # read-only safety
        _local.conn = conn
    return _local.conn

@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
    except Exception:
        raise

# ── FastAPI app ───────────────────────────────────────────
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Root ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("static/index.html")

# ── /books ────────────────────────────────────────────────
@app.get("/books")
async def get_books():
    return {"books": BOOKS}

# ── /langs ────────────────────────────────────────────────
@app.get("/langs")
async def get_langs():
    return {"languages": ["english", "urdu", "arabic"]}

# ── /topics/{usefilter}/{book} ────────────────────────────
@app.get("/topics/{usefilter}/{book}")
async def get_topics(usefilter: bool, book: str):
    with db() as conn:
        if usefilter:
            if book not in BOOKS:
                raise HTTPException(404, f"Book '{book}' not found.")
            rows = conn.execute(
                "SELECT DISTINCT topic FROM hadiths WHERE book=? ORDER BY topic",
                (book,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT topic FROM hadiths ORDER BY topic"
            ).fetchall()

    return {"topics": [r["topic"] for r in rows]}

# ── /hadiths/{lang}/{topic}/{usefilter}/{book} ────────────
@app.get("/hadiths/{lang}/{topic}/{usefilter}/{book}")
async def get_hadiths(lang: str, topic: str, usefilter: bool, book: str):
    if usefilter and book not in BOOKS:
        raise HTTPException(404, f"Book '{book}' not found.")

    if lang not in ("english", "urdu", "arabic"):
        lang = "english"

    with db() as conn:
        if usefilter:
            rows = conn.execute(
                f"""
                SELECT h.id, h.book, h.hadith_num, h.topic,
                       h.{lang} AS text, h.english, h.arabic, h.power
                FROM   hadiths h
                WHERE  h.book=? AND h.topic=?
                """,
                (book, topic)
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT h.id, h.book, h.hadith_num, h.topic,
                       h.{lang} AS text, h.english, h.arabic, h.power
                FROM   hadiths h
                WHERE  h.topic=?
                """,
                (topic,)
            ).fetchall()

        # Collect IDs to fetch grades in one query
        ids = [r["id"] for r in rows]
        grades_map: dict[int, dict] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            grade_rows = conn.execute(
                f"SELECT hadith_id, grader, grade, power FROM grades WHERE hadith_id IN ({placeholders})",
                ids
            ).fetchall()
            for gr in grade_rows:
                hid = gr["hadith_id"]
                if hid not in grades_map:
                    grades_map[hid] = {}
                grades_map[hid][gr["grader"]] = {
                    "grade": gr["grade"],
                    "power": gr["power"],
                }

    # Merge rows: same hadith_num from multiple books → accumulate power
    hadiths: dict = {}
    for r in rows:
        num = r["hadith_num"]
        bk  = r["book"]
        pw  = r["power"]

        if num not in hadiths:
            hadiths[num] = {
                "power":  pw,
                "grades": grades_map.get(r["id"], {}),
                "books":  [bk],
                "text":   r["text"] or r["english"] or "",
                "arabic": r["arabic"] or "",
            }
        else:
            hadiths[num]["power"] += pw
            if bk not in hadiths[num]["books"]:
                hadiths[num]["books"].append(bk)
            # merge grades
            for grader, info in grades_map.get(r["id"], {}).items():
                if grader in hadiths[num]["grades"]:
                    hadiths[num]["grades"][grader]["power"] += info["power"]
                else:
                    hadiths[num]["grades"][grader] = info

    return {"hadiths": hadiths}

# ── /search/{lang}/{query}  (FTS5) ────────────────────────
@app.get("/search/{lang}/{query}")
async def search_hadiths(lang: str, query: str):
    q = query.strip()
    if len(q) < 2:
        return {"hadiths": {}}

    if lang not in ("english", "urdu", "arabic"):
        lang = "english"

    # Build FTS5 query: wrap each token in quotes for exact phrase matching,
    # fall back to prefix match if that fails.
    fts_query = " ".join(f'"{w}"' for w in q.split()) or q

    with db() as conn:
        try:
            rows = conn.execute(
                f"""
                SELECT h.id, h.book, h.hadith_num, h.topic,
                       h.{lang} AS text, h.english, h.arabic, h.power
                FROM   hadiths_fts f
                JOIN   hadiths h ON h.id = f.rowid
                WHERE  hadiths_fts MATCH ?
                ORDER BY rank
                LIMIT  500
                """,
                (fts_query,)
            ).fetchall()
        except sqlite3.OperationalError:
            # If FTS query syntax fails, fall back to LIKE
            rows = conn.execute(
                f"""
                SELECT h.id, h.book, h.hadith_num, h.topic,
                       h.{lang} AS text, h.english, h.arabic, h.power
                FROM   hadiths h
                WHERE  h.{lang} LIKE ?
                LIMIT  500
                """,
                (f"%{q}%",)
            ).fetchall()

        ids = [r["id"] for r in rows]
        grades_map: dict[int, dict] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            grade_rows = conn.execute(
                f"SELECT hadith_id, grader, grade, power FROM grades WHERE hadith_id IN ({placeholders})",
                ids
            ).fetchall()
            for gr in grade_rows:
                hid = gr["hadith_id"]
                if hid not in grades_map:
                    grades_map[hid] = {}
                grades_map[hid][gr["grader"]] = {
                    "grade": gr["grade"],
                    "power": gr["power"],
                }

    results: dict = {}
    for r in rows:
        num = r["hadith_num"]
        bk  = r["book"]
        pw  = r["power"]

        if num not in results:
            results[num] = {
                "power":  pw,
                "grades": grades_map.get(r["id"], {}),
                "books":  [bk],
                "text":   r["text"] or r["english"] or "",
                "arabic": r["arabic"] or "",
                "topic":  r["topic"],
                "book":   bk,
            }
        else:
            results[num]["power"] += pw
            if bk not in results[num]["books"]:
                results[num]["books"].append(bk)
            for grader, info in grades_map.get(r["id"], {}).items():
                if grader in results[num]["grades"]:
                    results[num]["grades"][grader]["power"] += info["power"]
                else:
                    results[num]["grades"][grader] = info

    return {"hadiths": results}

# ── Static files ──────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")