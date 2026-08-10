import sqlite3

DB_PATH = "quiz.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def fetch_choices(conn, question_id):
    rows = conn.execute(
        "SELECT id, text, is_correct FROM choices "
        "WHERE question_id = ? ORDER BY id",
        (question_id,),
    ).fetchall()
    return [dict(r) for r in rows]


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
