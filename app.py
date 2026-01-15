import os
import io
import csv
import atexit
import secrets
import smtplib
import logging
import traceback
import threading
import mysql.connector
from flask_cors import CORS
from datetime import datetime
from mysql.connector import Error
from email.mime.text import MIMEText
from contextlib import contextmanager
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, session, request, jsonify, Response, make_response, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app, supports_credentials=True)

app.secret_key = secrets.token_hex(32)

logging.basicConfig(level=logging.INFO)

@app.errorhandler(Exception)
def handle_exception(e):
    logging.error(f"Unhandled exception: {str(e)}")
    logging.error(traceback.format_exc())
    return jsonify({
        'error': 'Internal Server Error',
        'details': str(e)
    }), 500

app.config['MYSQL_HOST'] = ''
app.config['MYSQL_USER'] = ''
app.config['MYSQL_PASSWORD'] = ''
app.config['MYSQL_DB'] = ''

EMAIL_ADDRESS = 'system@example.com'
EMAIL_PASSWORD = 'system'
SMTP_SERVER = 'mail.example.com'
SMTP_PORT = 587

DEPARTMENT_EMAIL_MAPPING = { 
    'HI': ['hi@example.com']
}

HR_NOTIFICATION_EMAIL = ['hr@example.com', 'hr2@example.com']
OPERATION_MANAGER_EMAIL = ['mgr@example.com']

def get_conn():
    try:
        conn = mysql.connector.connect(
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            charset='utf8mb4',
            use_unicode=True
        )
        if conn.is_connected():
            return conn
        else:
            logging.error("Failed to connect to MySQL")
            return None
    except Error as e:
        logging.error(f"MySQL connection error: {e}")
        return None

def daily_maintenance():
    conn = get_conn()
    if not conn:
        logging.error("Failed to connect to MySQL")
        return
    
    cur = conn.cursor()
    
    # Automatically checks in overdue items
    cur.execute("""
        UPDATE checkout 
        SET checkin_time=NOW(), 
            status='IN',
            session_token = NULL
        WHERE status='OUT' 
        AND DATE(checkout_time) < CURDATE()
    """)
    checkin_count = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    logging.info(
        f"Daily maintenance completed: {checkin_count} auto check-ins"
    )

def cleanup_session_tokens():
    conn = get_conn()
    if not conn:
        logging.error("Failed to connect to MySQL")
        return

    cur = conn.cursor()

    # Clean up session tokens older than 12 hours
    cur.execute("""
        UPDATE checkout 
        SET session_token = NULL 
        WHERE session_token IS NOT NULL 
        AND status = 'IN'
        AND checkin_time IS NOT NULL
        AND checkin_time < NOW() - INTERVAL 15 MINUTE
    """)
    cleanup_count = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    logging.info(f"Session cleanup: {cleanup_count} tokens cleared")

def cleanup_pending_checkouts():
    conn = get_conn()
    if not conn:
        logging.error("Failed to connect to MySQL")
        return

    cur = conn.cursor()

    # Delete old pending checkout requests (> 20 minutes)
    cur.execute("""
        DELETE FROM checkout
        WHERE status = 'PENDING'
        AND created_at < NOW() - INTERVAL 20 MINUTE
    """)
    pending_cleanup = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    logging.info(f"Pending checkout cleanup: {pending_cleanup} records removed")
  
scheduler = BackgroundScheduler()

# Every 1 minute → pending cleanup
scheduler.add_job(
    cleanup_pending_checkouts,
    trigger="interval",
    minutes=1,
    id="cleanup_pending_checkouts",
    replace_existing=True
)

# Every 5 minutes → session token cleanup
scheduler.add_job(
    cleanup_session_tokens,
    trigger="interval",
    minutes=5,
    id="cleanup_session_tokens",
    replace_existing=True
)

# Once per day → daily maintenance
scheduler.add_job(
    daily_maintenance,
    trigger="cron",
    hour=20,
    minute=0,
    id="daily_maintenance",
    replace_existing=True
)

scheduler.start()
atexit.register(lambda: scheduler.shutdown())

@app.route('/')
def home():
    return redirect(url_for('dashboard_page'))

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/checkout-form')
def checkout_form():
    return render_template('checkout.html')

@app.route('/checkin-form')
def checkin_form():
    return render_template('checkin_manual.html')

@app.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')

@app.route('/scan-preregister', methods=['GET'])
def scan_preregister():
    return redirect(url_for('checkout_form'))

@app.route('/scan-confirm', methods=['GET'])
def scan_confirm():
    token = request.cookies.get('checkout_session')
    
    if token:
        conn = get_conn()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT Employee_no, status 
            FROM checkout 
            WHERE session_token=%s 
            AND status IN ('PENDING', 'OUT') 
            LIMIT 1
        """, (token,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            if row['status'] == 'PENDING':
                # Has pending registration - go to guardhouse confirmation
                return render_template('guardhouse_confirm.html')
            elif row['status'] == 'OUT':
                # Has active OUT session - go to auto check-in
                return render_template('checkin_auto.html')
    
    logging.warning(f"No session found. Token: {token}")
    # No active session - show error page
    return render_template('no_session.html')

@app.route('/employee/<employee_no>', methods=['GET'])
def get_employee(employee_no):
    conn = get_conn()
    if not conn:
        return jsonify({'error': 'DB connection failed'}), 500
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT Employee_no, Employee_name, Department FROM employee WHERE Employee_no=%s LIMIT 1", (employee_no,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return jsonify(row)
    return jsonify({'error': 'not found'}), 404

@app.route('/checkout', methods=['POST'])
def checkout():
    data = request.json
    required = ['Employee_no', 'Department', 'Location', 'Purpose']
    if not data or not all(k in data for k in required):
        return jsonify({'error': 'Missing fields'}), 400

    conn = get_conn()
    if not conn:
        return jsonify({'error': 'DB connection failed'}), 500
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT Employee_name FROM employee WHERE Employee_no=%s LIMIT 1", (data['Employee_no'],))
    emp_row = cur.fetchone()
    
    if not emp_row:
        cur.close()
        conn.close()
        return jsonify({'error': 'Employee not found'}), 404
    
    employee_name = emp_row['Employee_name']

    # Check active session (PENDING or OUT)
    cur.execute("SELECT ID, status FROM checkout WHERE Employee_no=%s AND status IN ('PENDING', 'OUT') LIMIT 1", (data['Employee_no'],))
    existing = cur.fetchone()

    if existing:
        cur.close()
        conn.close()
        if existing['status'] == 'PENDING':
            return jsonify({'error': 'You already have a pending checkout. Please scan at guardhouse to confirm.'}), 400
        else:
            return jsonify({'error': 'You already have an active checkout'}), 400

    session_token = secrets.token_hex(32)

    # Insert with status='PENDING', checkout_time=NULL
    cur.execute("""
        INSERT INTO checkout (Employee_no, Employee_name, Department, Location, Purpose, checkout_time, status, session_token) 
        VALUES (%s, %s, %s, %s, %s, NULL, 'PENDING', %s)
    """, (data['Employee_no'], employee_name, data['Department'], data['Location'], data['Purpose'], session_token))

    conn.commit()
    cur.close()
    conn.close()

    resp = make_response(jsonify({
        'success': True, 
        'message': 'Pre-registration successful. Please scan at guardhouse to complete checkout.',
        'session_token': session_token
    }))
    resp.set_cookie(
        'checkout_session',
        session_token,
        httponly=True,
        samesite='Lax',
        secure=False,
        max_age=86400,
        path='/'
    )
    return resp

def send_checkout_notification(employee_no, employee_name, department, location, purpose, checkout_time):
    try:
        primary_recipients = []
        
        if department:
            if department in DEPARTMENT_EMAIL_MAPPING:
                primary_recipients.extend(DEPARTMENT_EMAIL_MAPPING[department])
                logging.info(f"Using unified email for department {department}: {DEPARTMENT_EMAIL_MAPPING[department]}")
            else:
                logging.warning(f"Department {department} not found in email mapping")
        
        cc_recipients = []
        cc_recipients.extend(HR_NOTIFICATION_EMAIL)
        cc_recipients.extend(OPERATION_MANAGER_EMAIL)

        primary_recipients = list(set([email for email in primary_recipients if email]))
        cc_recipients = list(set([email for email in cc_recipients if email]))
        
        if not primary_recipients and not cc_recipients:
            logging.warning("No recipients found for checkout notification")
            return False
        
        logging.info(f"Sending checkout notification to {len(primary_recipients)} primary recipients and {len(cc_recipients)} CC recipients")
    
        subject = f"Check Out Notification"
        # subject = f"[TEST] Check Out Notification Email – Please Ignore"
        
        html_body = f"""
        <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; }}
                    .container {{ max-width: 600px; margin: 20px auto; padding: 20px; border: 1px solid #ddd; border-radius: 5px; }}
                    .header {{ background-color: #4CAF50; color: white; padding: 15px; border-radius: 5px 5px 0 0; }}
                    .content {{ padding: 20px; background-color: #f9f9f9; }}
                    .info-row {{ margin: 10px 0; padding: 8px; background-color: white; border-left: 3px solid #4CAF50; }}
                    .label {{ color: #333; }}
                    .value {{ font-weight: bold; color: #333; margin-left: 10px; }}
                    .footer {{ margin-top: 20px; padding: 10px; font-size: 12px; color: #999; text-align: center; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h2 style="margin: 0;">Employee Checkout Notification</h2>
                    </div>
                    <div class="content">
                        <p>An employee has checked out from the premise:</p>
                        
                        <div class="info-row">
                            <span class="label">Employee No:</span>
                            <span class="value">{employee_no}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Employee Name:</span>
                            <span class="value">{employee_name}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Department:</span>
                            <span class="value">{department}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Location:</span>
                            <span class="value">{location}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Purpose:</span>
                            <span class="value">{purpose}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Checkout Time:</span>
                            <span class="value">{checkout_time.strftime('%Y-%m-%d %H:%M:%S') if checkout_time else 'N/A'}</span>
                        </div>
                    </div>
                    <div class="footer">
                        <p>This is a system-generated email. Please do not reply.</p>
                    </div>
                </div>
            </body>
        </html>
        """
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = ', '.join(primary_recipients) if primary_recipients else EMAIL_ADDRESS
        msg['Cc'] = ', '.join(cc_recipients)
        msg['Subject'] = subject
        
        html_part = MIMEText(html_body, 'html')
        msg.attach(html_part)
        
        all_recipients = primary_recipients + cc_recipients
        
        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, all_recipients, msg.as_string())
        
        logging.info(f"Checkout notification sent successfully - To: {len(primary_recipients)}, CC: {len(cc_recipients)} for {employee_name}")
        return True
        
    except Exception as e:
        logging.error(f"Error sending checkout notification: {e}")
        logging.error(traceback.format_exc())
        return False

@app.route('/confirm-checkout', methods=['POST'])
def confirm_checkout():
    token = request.cookies.get('checkout_session')
    
    if not token:
        return jsonify({'error': 'No session found. Please pre-register from your workstation first.'}), 400
    
    conn = get_conn()
    if not conn:
        return jsonify({'error': 'DB connection failed'}), 500
    cur = conn.cursor(dictionary=True)
    
    cur.execute("""
        SELECT ID, Employee_no, Employee_name, Department, Location, Purpose 
        FROM checkout 
        WHERE session_token=%s AND status='PENDING' 
        LIMIT 1
    """, (token,))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'No pending checkout found or already confirmed'}), 404
    
    # Update checkout time and status
    cur.execute("""
        UPDATE checkout 
        SET checkout_time=NOW(), status='OUT' 
        WHERE ID=%s
    """, (row['ID'],))
    
    conn.commit()
    
    # Get the updated checkout time
    cur.execute("SELECT checkout_time FROM checkout WHERE ID=%s", (row['ID'],))
    checkout_time_row = cur.fetchone()
    checkout_time = checkout_time_row['checkout_time'] if checkout_time_row else None
    
    cur.close()
    conn.close()
    
    # Send email notification in background thread (non-blocking)
    email_thread = threading.Thread(
        target=send_checkout_notification,
        args=(
            row['Employee_no'],
            row['Employee_name'],
            row['Department'],
            row['Location'],
            row['Purpose'],
            checkout_time
        ),
        daemon=True  # Thread will close when main program exits
    )
    email_thread.start()
    
    # Return response immediately without waiting for email
    return jsonify({
        'success': True,
        'Employee_no': row['Employee_no'],
        'Employee_name': row['Employee_name'],
        'Department': row['Department'],
        'Location': row['Location'],
        'Purpose': row['Purpose']
    })

def send_checkin_notification(employee_no, employee_name, department, location, purpose, checkout_time, checkin_time, duration):
    try:
        primary_recipients = []
        
        if department:
            if department in DEPARTMENT_EMAIL_MAPPING:
                primary_recipients.extend(DEPARTMENT_EMAIL_MAPPING[department])
                logging.info(f"Using unified email for department {department}: {DEPARTMENT_EMAIL_MAPPING[department]}")
            else:
                logging.warning(f"Department {department} not found in email mapping")
        
        cc_recipients = []
        cc_recipients.extend(HR_NOTIFICATION_EMAIL)
        cc_recipients.extend(OPERATION_MANAGER_EMAIL)

        primary_recipients = list(set([email for email in primary_recipients if email]))
        cc_recipients = list(set([email for email in cc_recipients if email]))
        
        if not primary_recipients and not cc_recipients:
            logging.warning("No recipients found for checkin notification")
            return False
        
        logging.info(f"Sending checkin notification to {len(primary_recipients)} primary recipients and {len(cc_recipients)} CC recipients")
    
        subject = f"Check In Notification"
        # subject = f"[TEST] Check In Notification Email – Please Ignore"
        
        html_body = f"""
        <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; }}
                    .container {{ max-width: 600px; margin: 20px auto; padding: 20px; border: 1px solid #ddd; border-radius: 5px; }}
                    .header {{ background-color: #2196F3; color: white; padding: 15px; border-radius: 5px 5px 0 0; }}
                    .content {{ padding: 20px; background-color: #f9f9f9; }}
                    .info-row {{ margin: 10px 0; padding: 8px; background-color: white; border-left: 3px solid #2196F3; }}
                    .label {{ color: #333; }}
                    .value {{ font-weight: bold; color: #333; margin-left: 10px; }}
                    .duration-highlight {{ background-color: #fff3cd; padding: 12px; border-left: 3px solid #ffc107; margin: 15px 0; }}
                    .footer {{ margin-top: 20px; padding: 10px; font-size: 12px; color: #999; text-align: center; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h2 style="margin: 0;">Employee Check-in Notification</h2>
                    </div>
                    <div class="content">
                        <p>An employee has checked back into the premise:</p>
                        
                        <div class="info-row">
                            <span class="label">Employee No:</span>
                            <span class="value">{employee_no}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Employee Name:</span>
                            <span class="value">{employee_name}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Department:</span>
                            <span class="value">{department}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Location:</span>
                            <span class="value">{location}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Purpose:</span>
                            <span class="value">{purpose}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Checkout Time:</span>
                            <span class="value">{checkout_time.strftime('%Y-%m-%d %H:%M:%S') if checkout_time else 'N/A'}</span>
                        </div>
                        
                        <div class="info-row">
                            <span class="label">Check-in Time:</span>
                            <span class="value">{checkin_time.strftime('%Y-%m-%d %H:%M:%S') if checkin_time else 'N/A'}</span>
                        </div>
                        
                        <div class="duration-highlight">
                            <span class="label">Duration Away:</span>
                            <span class="value" style="font-size: 16px; color: #d68800;">{duration if duration else 'N/A'}</span>
                        </div>
                    </div>
                    <div class="footer">
                        <p>This is a system-generated email. Please do not reply.</p>
                    </div>
                </div>
            </body>
        </html>
        """
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = ', '.join(primary_recipients) if primary_recipients else EMAIL_ADDRESS
        msg['Cc'] = ', '.join(cc_recipients)
        msg['Subject'] = subject
        
        html_part = MIMEText(html_body, 'html')
        msg.attach(html_part)
        
        all_recipients = primary_recipients + cc_recipients
        
        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, all_recipients, msg.as_string())
        
        logging.info(f"Check-in notification sent successfully - To: {len(primary_recipients)}, CC: {len(cc_recipients)} for {employee_name}")
        return True
        
    except Exception as e:
        logging.error(f"Error sending checkin notification: {e}")
        logging.error(traceback.format_exc())
        return False

@app.route('/checkin/<employee_no>', methods=['PUT'])
def checkin(employee_no):
    conn = get_conn()
    if not conn:
        return jsonify({'error': 'DB connection failed'}), 500
    cur = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT ID, Employee_name, Department, Location, Purpose, checkout_time 
        FROM checkout WHERE Employee_no=%s AND status='OUT' LIMIT 1
    """, (employee_no,))

    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'No active checkout found or already checked-in'}), 403
    
    cur.execute("UPDATE checkout SET checkin_time=NOW(), status='IN' WHERE ID=%s", (row['ID'],))
    conn.commit()

    cur.execute(
        "SELECT checkout_time, checkin_time FROM checkout WHERE ID=%s",
        (row['ID'],)
    )
    times = cur.fetchone()
    
    duration = None
    if times and times['checkout_time'] and times['checkin_time']:
        seconds = int((times['checkin_time'] - times['checkout_time']).total_seconds())
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        duration = f"{hours}h {minutes}m"

    cur.close()
    conn.close()

    email_thread = threading.Thread(
        target=send_checkin_notification,
        args=(
            employee_no,
            row['Employee_name'],
            row['Department'],
            row['Location'],
            row['Purpose'],
            times['checkout_time'] if times else None,
            times['checkin_time'] if times else None,
            duration
        ),
        daemon=True
    )
    email_thread.start()

    resp = make_response(jsonify({'success': True, 'duration': duration}))
    resp.delete_cookie('checkout_session')
    return resp

@app.route('/session-status', methods=['GET'])
def session_status():
    token = request.cookies.get('checkout_session')
    if not token:
        return jsonify({'active': False})
    
    conn = get_conn()
    if not conn:
        return jsonify({'active': False})
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT Employee_no, status FROM checkout WHERE session_token=%s AND status IN ('PENDING', 'OUT') LIMIT 1", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row:
        return jsonify({
            'active': True, 
            'Employee_no': row['Employee_no'],
            'status': row['status']
        })
    return jsonify({'active': False})

@app.route('/checkout-status/<employee_no>', methods=['GET'])
def checkout_status(employee_no):
    conn = get_conn()
    if not conn:
        return jsonify({'error': 'DB connection failed'}), 500
    cur = conn.cursor(dictionary=True)
    
    cur.execute("""
        SELECT Employee_no, Employee_name, Department, Location, Purpose, checkout_time 
        FROM checkout 
        WHERE Employee_no=%s AND status='OUT' 
        LIMIT 1
    """, (employee_no,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    
    if row:
        return jsonify({
            'active': True,
            'Employee_no': row['Employee_no'],
            'Employee_name': row['Employee_name'],
            'Department': row['Department'],
            'Location': row['Location'],
            'Purpose': row['Purpose'],
            'checkout_time': row['checkout_time'].strftime('%Y-%m-%d %H:%M:%S') if row['checkout_time'] else None
        })
    return jsonify({'active': False})

@app.route('/checkout-history', methods=['GET'])
def checkout_history():
    try:
        conn = get_conn()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(dictionary=True)

        is_hr = session.get('hr_logged_in', False)
        
        if is_hr:
            cur.execute("""
                SELECT Employee_no, Employee_name, Department, Location, Purpose, checkout_time, checkin_time, status
                FROM checkout 
                WHERE status IN ('OUT', 'IN') 
                ORDER BY checkout_time DESC
            """)
        else:
            cur.execute("""
                SELECT Employee_no, Employee_name, Department, Location, Purpose, checkout_time
                FROM checkout 
                WHERE status='OUT' 
                ORDER BY checkout_time DESC
            """)

        rows = cur.fetchall()
        cur.close()
        conn.close()

        for row in rows:
            if row.get('checkout_time'):
                row['checkout_time'] = row['checkout_time'].strftime('%Y-%m-%d %H:%M:%S')
            if row.get('checkin_time'):
                row['checkin_time'] = row['checkin_time'].strftime('%Y-%m-%d %H:%M:%S')
        
        return jsonify(rows)
    
    except Exception as e:
        logging.error(f"checkout_history error: {str(e)}")
        logging.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/hr-login', methods=['POST'])
def hr_login():
    data = request.json
    if not data or 'username' not in data or 'password' not in data:
        return jsonify({'error': 'Missing credentials'}), 400
    
    conn = get_conn()
    if not conn:
        return jsonify({'error': 'DB connection failed'}), 500
    
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT username, password, department
        FROM auth_user
        WHERE username=%s
            AND password=%s
            AND department=%s
        LIMIT 1
    """, (data['username'], data['password'], 'HR'))
    
    user = cur.fetchone()
    cur.close()
    conn.close()
    
    if user:
        session['hr_logged_in'] = True
        session['username'] = user['username']
        session['password'] = user['password']
        session['department'] = user['department']
        return jsonify({'success': True})
    
    return jsonify({'error': 'Invalid credentials or insufficient permissions'}), 401

@app.route('/hr-history')
def hr_history():
    if not session.get('hr_logged_in'):
        return redirect(url_for('dashboard_page'))
    
    # Return HR history page here
    return render_template('hr_history.html')

@app.route('/export', methods=['GET'])
def export_csv():
    conn = get_conn()
    if not conn:
        return jsonify({'error': 'DB connection failed'}), 500
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT ID, Employee_no, Employee_name, Department, Location, Purpose, checkout_time, checkin_time, status 
        FROM checkout 
        ORDER BY checkout_time DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Employee_no', 'Employee_name', 'Department', 'Location', 'Purpose', 'checkout_time', 'checkin_time', 'status'])
    for r in rows:
        writer.writerow([
            r['ID'], r['Employee_no'], r['Employee_name'], r['Department'], r['Location'], r['Purpose'], r['checkout_time'], r['checkin_time'], r['status']
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment;filename=checkout_history.csv"}
    )

@app.route('/hr-logout')
def hr_logout():
    session.clear()
    return redirect(url_for('dashboard_page'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=, debug=True)
