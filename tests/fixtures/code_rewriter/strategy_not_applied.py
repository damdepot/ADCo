def get_user_data(user_ids):
    results = []
    for uid in user_ids:
        # Still in loop
        user = db.execute("SELECT * FROM users WHERE id = ?", uid)
        results.append(user)
    return results

def other_func(x):
    return x * 2
