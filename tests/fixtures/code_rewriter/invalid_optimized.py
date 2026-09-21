def get_user_data(user_ids):
    # Missing colon
    if not user_ids
        return []
    return db.execute("SELECT * FROM users", user_ids)

def other_func(x):
    return x * 2
