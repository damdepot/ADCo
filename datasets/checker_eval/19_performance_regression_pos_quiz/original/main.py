import asyncio
import json

from db import (fetch_choices, fetch_questions, fetch_quiz,
                get_connection, record_answer)


async def handle_quiz_detail(quiz_id):
    conn = get_connection()
    try:
        quiz = fetch_quiz(conn, quiz_id)
        if quiz is None:
            return 404, {"error": "quiz not found"}
        questions = fetch_questions(conn, quiz_id)
        detail = []
        for q in questions:
            choices = fetch_choices(conn, q["id"])
            detail.append({
                "id": q["id"],
                "text": q["text"],
                "points": q["points"],
                "choices": [{"id": c["id"], "text": c["text"]}
                            for c in choices],
            })
        return 200, {"quiz": quiz, "questions": detail}
    finally:
        conn.close()


async def handle_submit(attempt_id, answers):
    conn = get_connection()
    try:
        total = 0
        for item in answers:
            total += record_answer(conn, attempt_id,
                                   item["question_id"], item["choice_id"])
        conn.commit()
        return 200, {"recorded": len(answers), "score": total}
    finally:
        conn.close()


async def main():
    status, body = await handle_quiz_detail(1)
    print(status, json.dumps(body)[:200])
    status, body = await handle_submit(1, [{"question_id": 1, "choice_id": 2}])
    print(status, json.dumps(body))


if __name__ == "__main__":
    asyncio.run(main())
