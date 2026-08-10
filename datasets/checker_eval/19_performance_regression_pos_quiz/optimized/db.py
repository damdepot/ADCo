# ADCO_OPTIMIZED: 19_performance_regression_pos_quiz
import sqlite3
import threading

DB_PATH = "quiz.db"
POOL_SIZE = 5


class ConnectionPool:
    def __init__(self, size=POOL_SIZE):
        self._size = size
        self._idle = []
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._idle:
                return self._idle.pop()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def put(self, conn):
        with self._lock:
            if len(self._idle) < self._size:
                self._idle.append(conn)
            else:
                conn.close()


pool = ConnectionPool()


def fetch_quiz(conn, quiz_id):
    row = conn.execute(
        "SELECT id, title, pass_score FROM quizzes WHERE id = ?",
        (quiz_id,),
    ).fetchone()
    return dict(row) if row else None


def fetch_questions(conn, quiz_id):
    rows = conn.execute(
        "SELECT id, text, points FROM questions "
        "WHERE quiz_id = ? ORDER BY position",
        (quiz_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_choices_batch(conn, question_ids):
    if not question_ids:
        return {}
    placeholders = ",".join("?" * len(question_ids))
    query = (
        "SELECT question_id, id, text, is_correct "
        "FROM choices WHERE question_id IN (" + placeholders + ") "
        "ORDER BY question_id, id"
    )
    rows = conn.execute(query, tuple(question_ids)).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["question_id"], []).append(dict(r))
    return grouped


def record_answer(conn, attempt_id, question_id, choice_id):
    row = conn.execute(
        "SELECT is_correct FROM choices WHERE id = ? AND question_id = ?",
        (choice_id, question_id),
    ).fetchone()
    score = int(row["is_correct"]) if row else 0
    conn.execute(
        "INSERT INTO answers (attempt_id, question_id, choice_id, score) "
        "VALUES (?, ?, ?, ?)",
        (attempt_id, question_id, choice_id, score),
    )
    return score
