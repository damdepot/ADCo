class Repository:
    def get_product(self, product_ids):
        cursor.execute("SELECT * FROM products WHERE id = ANY(%s)", (product_ids,))
        return cursor.fetchall()

    def get_category(self, category_ids):
        cursor.execute("SELECT * FROM categories WHERE id = ANY(%s)", (category_ids,))
        return cursor.fetchall()

    def get_supplier(self, supplier_ids):
        cursor.execute("SELECT * FROM suppliers WHERE id = ANY(%s)", (supplier_ids,))
        return cursor.fetchall()

    def authenticate(self, user_id):
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cursor.fetchone()
