def get_user_data(user_ids):
    # Missing return statement!
    if not user_ids:
        pass
    placeholders = ",".join(["?"] * len(user_ids))
    db.execute(f"SELECT * FROM users WHERE id IN ({placeholders})", user_ids)

def other_func(x):
    return x * 2
