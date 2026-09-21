class Repository:
    def get_product(self, product_ids):
        result = []
        for pid in product_ids:
            cursor.execute("SELECT * FROM products WHERE id = %s", (pid,))
            result.append(cursor.fetchone())
        return result

    def get_category(self, category_ids):
        result = []
        for cid in category_ids:
            cursor.execute("SELECT * FROM categories WHERE id = %s", (cid,))
            result.append(cursor.fetchone())
        return result

    def get_supplier(self, supplier_ids):
        result = []
        for sid in supplier_ids:
            cursor.execute("SELECT * FROM suppliers WHERE id = %s", (sid,))
            result.append(cursor.fetchone())
        return result

    def authenticate(self, user_id):
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cursor.fetchone()
