import os
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import psycopg2
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get('DATABASE_URL')
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_secret_key')


def init_db():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()

        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT DEFAULT 'staff',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        c.execute('''CREATE TABLE IF NOT EXISTS user_activity (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            activity TEXT NOT NULL,
            activity_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')


        c.execute('''CREATE TABLE IF NOT EXISTS Category (
            id SERIAL PRIMARY KEY,
            category_name TEXT NOT NULL,
            created_at DATE DEFAULT CURRENT_DATE
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS Product (
            id SERIAL PRIMARY KEY,
            product_name TEXT NOT NULL,
            product_type TEXT NOT NULL,
            category_id INTEGER REFERENCES Category(id),
            stock_quantity INTEGER NOT NULL DEFAULT 0,
            stock_status TEXT DEFAULT 'in stock',
            status TEXT DEFAULT 'active',
            created_at DATE DEFAULT CURRENT_DATE
        )''')
        
        conn.commit()
        print("Database initialization schema check complete.")
    except Exception as e:
        print(f"Error initializing database: {str(e)}")
        if conn:
            conn.rollback()
    finally:
        if conn is not None:
            conn.close()

# Login required decorator
def login_required(f):
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash('Please log in to access this page', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def log_activity(username, activity):
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
            INSERT INTO user_activity (username, activity)
            VALUES (%s, %s)
        """, (username, activity))
        conn.commit()
    except Exception as e:
        print(f"Error logging activity: {e}")
    finally:
        if conn:
            conn.close()

def update_expiry_status():
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()

        # Expired: expiration_date < today
        c.execute("""
            UPDATE Purchase
            SET status = 'expired'
            WHERE expiration_date <= CURRENT_DATE
        """)

        # Near expiry: expires within 7 days
        c.execute("""
            UPDATE Purchase
            SET status = 'near expiry'
            WHERE expiration_date > CURRENT_DATE
              AND expiration_date <= CURRENT_DATE + INTERVAL '7 days'
              AND status != 'expired'
        """)

        # In stock: expires later
        c.execute("""
            UPDATE Purchase
            SET status = 'in stock'
            WHERE expiration_date > CURRENT_DATE + INTERVAL '7 days'
              AND status NOT IN ('expired', 'near expiry')
        """)

        conn.commit()
    except Exception as e:
        print("Error updating expiry status:", e)
    finally:
        if conn:
            conn.close()

# at the top, after your imports and database connection setup

def update_expiry_notifications():
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()

        c.execute("""
            UPDATE notification n
            SET ignored = TRUE
            FROM purchase pu
            WHERE n.ignored = FALSE
                AND n.product_id = pu.product_id
                AND n.batch_id = pu.batch_number
                AND (
                    -- If purchase is expired but notification is near-expiry → ignore near-expiry
                    (pu.status = 'expired' AND n.type = 'near-expiry')
                    -- If purchase is near-expiry but notification is expired → ignore expired
                    OR (pu.status = 'near expiry' AND n.type = 'expired')
                );
        """)

        # 👇 Add this line right after the execute
        print(f"Rows updated: {c.rowcount}")

        conn.commit()
    except Exception as e:
        print(f"Error updating expiry notifications: {e}")
    finally:
        if conn:
            conn.close()



# Routes
@app.route('/')
def login():
    session.clear()
    return render_template('index.html')

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form['username']
    password = request.form['password']

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
            SELECT id, username, password, full_name, role
            FROM users
            WHERE username = %s
        """, (username,))
        user = c.fetchone()

        if user and check_password_hash(user[2], password):
            session['logged_in'] = True
            session['user_id'] = user[0]
            session['username'] = user[1]
            session['name'] = user[3]
            session['role'] = user[4]

            log_activity(user[1], "Logged in")
            return redirect(url_for('dashboard'))
        else:
            # Pass the error to template
            return render_template('index.html', error="Invalid login, please try again.", username=username)

    except Exception as e:
        return render_template('index.html', error="Login error, please try again.", username=username)
    finally:
        if conn:
            conn.close()



@app.route('/logout')
def logout():
    if 'username' in session:
        log_activity(session['username'], "Logged out")
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    update_expiry_status()
    update_expiry_notifications()

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        
        # Get total stocks (sum of quantity of active products)
        c.execute("""
            SELECT SUM(stock_quantity) 
            FROM Product 
            WHERE status = 'active'
        """)
        total_stocks_quantity = c.fetchone()[0] or 0

        # Get total medicines and supplies (count of active products by type)
        c.execute("""
            SELECT 
                SUM(CASE WHEN product_type = 'medicine' THEN 1 ELSE 0 END) as medicines,
                SUM(CASE WHEN product_type = 'supply' THEN 1 ELSE 0 END) as supplies
            FROM Product 
            WHERE status = 'active'
        """)
        result = c.fetchone()
        total_medicines_count = result[0] or 0
        total_supplies_count = result[1] or 0

        # Get stock-ins for this week (medicines and supplies)
        c.execute("""
            SELECT 
                SUM(CASE WHEN pr.product_type = 'medicine' THEN p.purchase_quantity ELSE 0 END) as medicines,
                SUM(CASE WHEN pr.product_type = 'supply' THEN p.purchase_quantity ELSE 0 END) as supplies
            FROM Purchase p
            JOIN Product pr ON p.product_id = pr.id
            WHERE p.purchase_date >= CURRENT_DATE - INTERVAL '7 days'
        """)
        result_stockins = c.fetchone()
        stockins_medicines = result_stockins[0] or 0
        stockins_supplies = result_stockins[1] or 0

        # Get stock-outs for this week (medicines and supplies)
        c.execute("""
            SELECT 
                SUM(CASE WHEN pr.product_type = 'medicine' THEN o.order_quantity ELSE 0 END) as medicines,
                SUM(CASE WHEN pr.product_type = 'supply' THEN o.order_quantity ELSE 0 END) as supplies
            FROM "Order" o
            JOIN Product pr ON o.product_id = pr.id
            WHERE o.order_date >= CURRENT_DATE - INTERVAL '7 days'
        """)
        result_stockouts = c.fetchone()
        stockouts_medicines = result_stockouts[0] or 0
        stockouts_supplies = result_stockouts[1] or 0

        # Get total out of stock items
        c.execute("""
            SELECT COUNT(*) 
            FROM Product 
            WHERE stock_status = 'out of stock' AND status = 'active'
        """)
        total_out_of_stock = c.fetchone()[0] or 0

        # Get total orders
        c.execute("""
            SELECT COUNT(*) 
            FROM "Order"
        """)
        total_orders = c.fetchone()[0] or 0

        # Get total purchases with near-expiry status
        c.execute("""
            SELECT COUNT(*) 
            FROM Purchase 
            WHERE status = 'near expiry'
        """)
        total_expiring_soon = c.fetchone()[0] or 0

        # Get expiring soon items with details
        c.execute("""
            SELECT p.id as code, pr.product_name as name, p.expiration_date as expiration
            FROM Purchase p
            JOIN Product pr ON p.product_id = pr.id
            WHERE p.status = 'near expiry'
            ORDER BY p.expiration_date ASC
        """)
        expiring_soon = [{'code': row[0], 'name': row[1], 'expiration': row[2]} for row in c.fetchall()]

        return render_template('admin.html', 
                             total_stocks=total_stocks_quantity,
                             out_of_stocks=total_out_of_stock,
                             total_orders=total_orders,
                             expiring_soon=expiring_soon,
                             medicines=total_medicines_count,
                             supplies=total_supplies_count,
                             stockins_medicines=stockins_medicines,
                             stockins_supplies=stockins_supplies,
                             stockouts_medicines=stockouts_medicines,
                             stockouts_supplies=stockouts_supplies)
    except Exception as e:
        print(f"Error in dashboard route: {str(e)}")
        flash(f'Error loading dashboard: {str(e)}', 'error')
        return render_template('admin.html', 
                             total_stocks=0,
                             out_of_stocks=0,
                             total_orders=0,
                             expiring_soon=[],
                             medicines=0,
                             supplies=0,
                             stockins_medicines=0,
                             stockins_supplies=0,
                             stockouts_medicines=0,
                             stockouts_supplies=0)
    finally:
        if conn is not None:
            conn.close()

@app.route('/products')
@login_required
def products():
    update_expiry_status()
    update_expiry_notifications()

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
            SELECT p.id, p.product_name, p.product_type, p.stock_quantity, 
                   c.category_name, p.stock_status
            FROM Product p
            LEFT JOIN Category c ON p.category_id = c.id
            ORDER BY p.id DESC
        """)
        products = c.fetchall()
        c.execute("SELECT id, category_name FROM Category")
        categories = c.fetchall()
        c.execute("SELECT DISTINCT product_type FROM Product")
        product_types = [row[0] for row in c.fetchall()]
        return render_template('products.html', 
                             products=products,
                             categories=categories,
                             product_types=product_types)
    except Exception as e:
        print(f"Error in products route: {str(e)}")
        flash(f'Error loading products: {str(e)}', 'error')
        return render_template('products.html', products=[], categories=[], product_types=[])
    finally:
        if conn is not None:
            conn.close()

@app.route('/purchases')
@login_required
def purchases():
    update_expiry_status()
    update_expiry_notifications()

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
    SELECT 
        pu.id, 
        pr.product_name, 
        pu.batch_number, 
        pu.purchase_quantity, 
        pu.remaining_quantity, 
        pu.expiration_date, 
        pu.status, 
        pu.purchase_date,
        pu.supplier
    FROM Purchase pu
    LEFT JOIN Product pr ON pu.product_id = pr.id
    ORDER BY pu.purchase_date DESC
""")

        purchases = c.fetchall()
        c.execute("SELECT id, product_name FROM Product ORDER BY product_name ASC")
        products = c.fetchall()
        print("Purchases fetched:", purchases)
        return render_template('purchase.html', purchases=purchases, products=products)
    except Exception as e:
        print(f"Error in purchases route: {str(e)}")
        flash(f'Error loading purchases: {str(e)}', 'error')
        return render_template('purchase.html', purchases=[], products=[])
    finally:
        if conn is not None:
            conn.close()

@app.route('/orders')
@login_required
def orders():
    update_expiry_status()
    update_expiry_notifications()

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
    SELECT o.order_id, p.product_name, o.order_quantity, o.batch_number, o.order_date, o.customer
    FROM "Order" o
    LEFT JOIN Product p ON o.product_id = p.id
    ORDER BY o.order_date DESC
""")

        orders = c.fetchall()
        c.execute("SELECT id, product_name FROM Product ORDER BY product_name ASC")
        products = c.fetchall()
        print("Orders fetched:", orders)
        return render_template('orders.html', orders=orders, products=products)
    except Exception as e:
        print(f"Error in orders route: {str(e)}")
        flash(f'Error loading orders: {str(e)}', 'error')
        return render_template('orders.html', orders=[], products=[])
    finally:
        if conn is not None:
            conn.close()

@app.route('/notification')
@login_required
def notification():
    update_expiry_status()
    update_expiry_notifications()

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
            SELECT id, message, created_at, is_read, type
            FROM public.notification
            ORDER BY created_at DESC
        """)
        notifications = c.fetchall()
        print("Fetched notifications:", notifications)  # Debug print
        return render_template('notification.html', notifications=notifications)
    except Exception as e:
        print(f"Error in notification route: {str(e)}")
        flash(f'Error loading notifications: {str(e)}', 'error')
        return render_template('notification.html', notifications=[])
    finally:
        if conn is not None:
            conn.close()

@app.route('/add-product', methods=['POST'])
@login_required
def add_product():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        
        # Get form data
        product_name = request.form['product_name']
        product_type = request.form['product_type']
        category_id = request.form['category_id']
        
        # Insert new product
        c.execute("""
            INSERT INTO Product (product_name, product_type, category_id, 
                               stock_quantity)
            VALUES (%s, %s, %s, 0)
            RETURNING id
        """, (product_name, product_type, category_id))
        
        conn.commit()
        log_activity(session['username'], f"Added product '{product_name}'")

        flash('Product added successfully!', 'success')
        return redirect(url_for('products'))
        
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f'Error adding product: {str(e)}', 'error')
        return redirect(url_for('products'))
    finally:
        if conn is not None:
            conn.close()

@app.route('/edit-product/<int:product_id>', methods=['POST'])
@login_required
def edit_product(product_id):
    product_name = request.form['product_name']
    product_type = request.form['product_type']
    category_id = request.form['category_id']

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
            UPDATE Product
            SET product_name = %s, product_type = %s, category_id = %s
            WHERE id = %s
        """, (product_name, product_type, category_id, product_id))

        conn.commit()
        log_activity(session['username'], f"Edited product ID {product_id}")

        flash('Product updated successfully!', 'success')
        return jsonify({'success': True, 'message': 'Product updated successfully!'})
    except Exception as e:
        if conn:
            conn.rollback()
            return jsonify({'success': False, 'message': f'Error updating product: {str(e)}'})

    finally:
        if conn:
            conn.close()


@app.route('/delete-product/<int:product_id>', methods=['POST'])
@login_required
def delete_product(product_id):
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        # Either delete or mark inactive
        c.execute("DELETE FROM Product WHERE id = %s", (product_id,))
        conn.commit()
        log_activity(session['username'], f"Deleted product ID {product_id}")
        
        return jsonify({'success': True, 'message': 'Product deleted successfully!'})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'message': 'This product has associated purchases or orders. Please remove references before deleting.'})
    finally:
        if conn:
            conn.close()

@app.route('/add-purchase', methods=['POST'])
@login_required
def add_purchase():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()

        product_id = request.form['product_id']
        purchase_quantity = int(request.form['purchase_quantity'])
        expiration_date = request.form['expiration_date']
        supplier = request.form['supplier']

        c.execute("""
            INSERT INTO Purchase 
            (product_id, purchase_quantity, remaining_quantity, expiration_date, supplier)
            VALUES (%s, %s, %s, %s, %s)
        """, (product_id, purchase_quantity, purchase_quantity, expiration_date, supplier))

        conn.commit()
        log_activity(session['username'], f"Added stock-in: product_id {product_id}, qty {purchase_quantity}, expiration {expiration_date}")

        return jsonify({'success': True, 'message': 'Purchase added successfully!'})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'message': f'Error adding purchase: {str(e)}'})
    finally:
        if conn:
            conn.close()


@app.route('/edit-purchase/<int:purchase_id>', methods=['POST'])
@login_required
def edit_purchase(purchase_id):
    product_id = request.form['product_id']
    new_purchase_quantity = int(request.form['purchase_quantity'])
    expiration_date = request.form['expiration_date']

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()

        c.execute("SELECT batch_number FROM Purchase WHERE id = %s", (purchase_id,))
        batch_number = c.fetchone()[0]

        c.execute("""SELECT COALESCE(SUM(order_quantity), 0) 
                     FROM "Order" WHERE product_id=%s AND batch_number=%s""",
                  (product_id, batch_number))
        total_ordered_quantity = c.fetchone()[0]

        new_remaining_quantity = max(new_purchase_quantity - total_ordered_quantity, 0)

        c.execute("""UPDATE Purchase
                     SET product_id=%s, purchase_quantity=%s, remaining_quantity=%s, expiration_date=%s
                     WHERE id=%s""",
                  (product_id, new_purchase_quantity, new_remaining_quantity, expiration_date, purchase_id))
        conn.commit()
        log_activity(session['username'], f"Edited stock-in ID {purchase_id}")

        return jsonify({'success': True, 'message': "Purchase updated successfully!"})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'message': f'Error updating purchase: {str(e)}'})
    finally:
        if conn:
            conn.close()



@app.route('/delete-purchase/<int:purchase_id>', methods=['POST'])
@login_required
def delete_purchase(purchase_id):
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("SELECT product_id, batch_number FROM Purchase WHERE id = %s", (purchase_id,))
        result = c.fetchone()
        if not result:
            return jsonify({'success': False, 'message': 'Purchase not found.'})

        product_id, batch_number = result
        c.execute("""SELECT COUNT(*) FROM "Order" WHERE product_id=%s AND batch_number=%s""",
                  (product_id, batch_number))
        count = c.fetchone()[0]

        if count > 0:
            return jsonify({'success': False, 'message': "Couldn't delete purchase, being referenced with orders."})

        c.execute("DELETE FROM Purchase WHERE id = %s", (purchase_id,))
        conn.commit()
        log_activity(session['username'], f"Deleted stock-in ID {purchase_id}")

        return jsonify({'success': True, 'message': "Purchase deleted successfully!"})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'message': f'Error deleting purchase: {str(e)}'})
    finally:
        if conn:
            conn.close()


@app.route('/add-order', methods=['POST'])
@login_required
def add_order():
    data = request.get_json()  # <-- get JSON from request
    product_id = data.get('product_id')
    batch_number = data.get('batch_number')
    order_quantity = int(data.get('order_quantity', 0))
    customer = data.get('customer')

    if not product_id or not batch_number or order_quantity <= 0 or not customer:
        return jsonify({'success': False, 'message': 'Invalid input'}), 400

    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
            INSERT INTO "Order" (product_id, order_quantity, batch_number, customer)
            VALUES (%s, %s, %s, %s)
        """, (product_id, order_quantity, batch_number, customer))
        conn.commit()
        log_activity(session['username'], f"Added stock-out: product_id {product_id}, batch {batch_number}, qty {order_quantity}")

        return jsonify({'success': True, 'message': 'Order added successfully!'})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'message': f'Error adding order: {str(e)}'})
    finally:
        if conn:
            conn.close()



@app.route('/edit-order/<int:order_id>', methods=['POST'])
@login_required
def edit_order(order_id):
    data = request.get_json()
    product_id = data['product_id']
    batch_number = data['batch_number']
    new_quantity = int(data['order_quantity'])

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()

        # Get old order info
        c.execute('SELECT product_id, batch_number, order_quantity FROM "Order" WHERE order_id=%s', (order_id,))
        old_order = c.fetchone()
        if not old_order:
            return jsonify({'success': False, 'message': 'Order not found'})

        old_product_id, old_batch, old_quantity = old_order

        # 1️⃣ Update remaining quantity in old purchase
        c.execute("""UPDATE Purchase
                     SET remaining_quantity = remaining_quantity + %s
                     WHERE product_id=%s AND batch_number=%s""",
                  (old_quantity, old_product_id, old_batch))

        # 2️⃣ Update order
        customer = data.get('customer')
        c.execute("""
    UPDATE "Order"
    SET product_id=%s, batch_number=%s, order_quantity=%s, customer=%s
    WHERE order_id=%s
""", (product_id, batch_number, new_quantity, customer, order_id))


        # 3️⃣ Deduct from new purchase remaining_quantity
        c.execute("""UPDATE Purchase
                     SET remaining_quantity = remaining_quantity - %s
                     WHERE product_id=%s AND batch_number=%s""",
                  (new_quantity, product_id, batch_number))

        conn.commit()
        log_activity(session['username'], f"Edited stock-out ID {order_id}")

        return jsonify({'success': True, 'message': 'Order updated successfully'})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'message': f'Error editing order: {str(e)}'})
    finally:
        if conn:
            conn.close()


@app.route('/delete-order/<int:order_id>', methods=['POST'])
@login_required
def delete_order(order_id):
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        # Get order details
        c.execute('SELECT product_id, batch_number, order_quantity FROM "Order" WHERE order_id=%s', (order_id,))
        order = c.fetchone()
        if not order:
            return jsonify({'success': False, 'message': 'Order not found'})

        product_id, batch_number, quantity = order

        # Update purchase remaining quantity
        c.execute("""UPDATE Purchase
                     SET remaining_quantity = remaining_quantity + %s
                     WHERE product_id=%s AND batch_number=%s""",
                  (quantity, product_id, batch_number))

        # Delete order
        c.execute('DELETE FROM "Order" WHERE order_id=%s', (order_id,))
        conn.commit()

        log_activity(session['username'], f"Deleted stock-out ID {order_id}")
        return jsonify({'success': True, 'message': 'Order deleted successfully'})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'message': f'Error deleting order: {str(e)}'})
    finally:
        if conn:
            conn.close()


def get_notifications(limit=10):
    conn = psycopg2.connect(DATABASE_URL)

    c = conn.cursor()
    c.execute("SELECT id, message, created_at, is_read FROM Notification ORDER BY created_at DESC LIMIT %s", (limit,))
    notifications = c.fetchall()
    conn.close()
    return notifications

from flask import jsonify

# Return notifications as JSON
@app.route('/notification-json')
@login_required
def notification_json():
    update_expiry_status()
    update_expiry_notifications()
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        # Only return notifications that are not ignored
        c.execute("""
            SELECT id, message, created_at, is_read, ignored, type
            FROM notification
            ORDER BY created_at DESC
        """)
        notifications = c.fetchall()
        notif_list = []
        for n in notifications:
            notif_list.append({
                'id': n[0],
                'message': n[1],
                'created_at': n[2].isoformat(),
                'is_read': n[3],
                'ignored': n[4],
                'type': n[5]
            })
        return jsonify(notif_list)
    except Exception as e:
        print(f"Error fetching notifications: {str(e)}")
        return jsonify([])
    finally:
        if conn:
            conn.close()

@app.route('/touch-notification/<int:notif_id>', methods=['POST'])
@login_required
def touch_notification(notif_id):
    conn = psycopg2.connect(DATABASE_URL)

    c = conn.cursor()
    c.execute("""
        UPDATE notification
        SET last_notified = NOW()
        WHERE id = %s
    """, (notif_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# Mark a notification as ignored
@app.route('/ignore-notification/<int:notif_id>', methods=['POST'])
@login_required
def ignore_notification(notif_id):
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)

        c = conn.cursor()
        c.execute("""
            UPDATE notification
            SET ignored = TRUE
            WHERE id = %s
        """, (notif_id,))
        conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"Error ignoring notification: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        if conn:
            conn.close()

@app.route('/read-notification/<int:notif_id>', methods=['POST'])
@login_required
def read_notification(notif_id):
    conn = psycopg2.connect(DATABASE_URL)

    c = conn.cursor()
    c.execute("""
        UPDATE notification
        SET is_read = TRUE
        WHERE id = %s
    """, (notif_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    init_db()
    from waitress import serve
    serve(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
