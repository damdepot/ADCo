def get_user_data(user_ids):
    # DB operation outside loop
    if not user_ids:
        return []
    placeholders = ",".join(["?"] * len(user_ids))
    results = db.execute(f"SELECT * FROM users WHERE id IN ({placeholders})", user_ids)
    return results

def other_func(x):
    return x * 2
