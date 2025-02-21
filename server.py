import sys
import logging
from logging import StreamHandler
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from waitress import serve
from functools import wraps
import os
import time
from datetime import datetime, timezone, timedelta  # Import timedelta for expiration calculation
import secrets
import re
import config
from modules import helper

# -----------------------------------------------------------------------------
# Production-level Logging Configuration (Output to container's stdout)
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

stream_handler = StreamHandler(sys.stdout)
formatter = logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# -----------------------------------------------------------------------------
# Ensure the /data folder exists (for local testing)
# -----------------------------------------------------------------------------
if not os.path.exists('/data'):
    os.makedirs('/data')
    logger.info("Created /data folder.")

# -----------------------------------------------------------------------------
# Check if the system is a Raspberry Pi and setup GPIO if needed.
# -----------------------------------------------------------------------------
isPi = helper.is_raspberry_pi()

if isPi:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(config.pin, GPIO.OUT)
    GPIO.output(config.pin, GPIO.LOW)
    logger.info("Running on Raspberry Pi. GPIO initialized.")
    

# -----------------------------------------------------------------------------
# Initialize two separate Flask applications
# -----------------------------------------------------------------------------
app = Flask('main_app')
admin_app = Flask('admin_app', template_folder='templates')
app.secret_key = config.secret_key
admin_app.secret_key = config.secret_key

# -----------------------------------------------------------------------------
# Initialize the database
# -----------------------------------------------------------------------------
db = SQLAlchemy()

# -----------------------------------------------------------------------------
# Configure both apps
# -----------------------------------------------------------------------------
for application in (app, admin_app):
    application.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////data/users.db'
    application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(application)

    application.logger.addHandler(stream_handler)

# -----------------------------------------------------------------------------
# Before-request logging for both apps
# -----------------------------------------------------------------------------
@app.before_request
def log_request_info():
    logger.info("Request from %s: %s %s", request.remote_addr, request.method, request.url)

@admin_app.before_request
def log_admin_request_info():
    logger.info("Admin request from %s: %s %s", request.remote_addr, request.method, request.url)

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
def generate_urlsafe_key():
    """Generate a 32-byte URL-safe key for API key generation."""
    return secrets.token_urlsafe(32)

def get_client_ip():
    """Retrieve the client IP, even behind a reverse proxy."""
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

# -----------------------------------------------------------------------------
# Global limiter for the main app
# -----------------------------------------------------------------------------
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "75 per hour"]
)

# -----------------------------------------------------------------------------
# User Model: Stores name, API key, and an expiration date if needed.
# -----------------------------------------------------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    api_key_expiration = db.Column(db.DateTime(timezone=True), nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    admin = db.Column(db.Boolean, default=False)
    api_key = db.Column(db.String(64), unique=True, nullable=False, default=generate_urlsafe_key)

with app.app_context():
    db.create_all()
    logger.info("Database initialized and tables created.")

# -----------------------------------------------------------------------------
# Decorator for endpoints requiring an API key for authentication.
# -----------------------------------------------------------------------------
def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            logger.warning("Missing API key in request from %s", get_client_ip())
            return jsonify({'message': 'API key is missing!'}), 401

        api_key = auth_header.split(" ")[1]
        user = User.query.filter_by(api_key=api_key).first()

        if not user or not user.enabled:
            logger.warning("Invalid API key attempt from %s", get_client_ip())
            return jsonify({'message': 'Invalid API key or user not enabled!'}), 401

        expiration = user.api_key_expiration
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) > expiration:
            logger.warning("Expired API key for user %s from %s", user.username, get_client_ip())
            return jsonify({'message': 'API key expired!'}), 401

        return f(user, *args, **kwargs)
    return decorated

# -----------------------------------------------------------------------------
# Protected Endpoint: Trigger (requires valid API key)
# -----------------------------------------------------------------------------
@app.route('/api/v1/trigger', methods=['POST'])
@limiter.limit(f"{config.trigger_limit} per minute")
@api_key_required
def trigger(user, isPi=isPi):
    logger.info("Trigger endpoint accessed by user %s from %s", user.username, get_client_ip())
    if isPi:
        logger.info("Is Pi True")
        GPIO.output(config.pin, GPIO.HIGH)
        time.sleep(config.click_delay)
        GPIO.output(config.pin, GPIO.LOW)

    return jsonify({'message': f"{config.friendly_name} triggered by {user.username}"}), 200

# -----------------------------------------------------------------------------
# Protected Endpoint: Custom Trigger (requires valid API key)
# -----------------------------------------------------------------------------
@app.route('/api/v1/customtrigger', methods=['POST'])
@limiter.limit(f"{config.trigger_limit} per minute")
@api_key_required
def customtrigger(user, isPi=isPi):
    # Use config defaults but override if query parameters are provided
    pin = config.pin
    click_delay = config.click_delay

    # Validate and override 'pin' if provided in URL parameters
    if 'pin' in request.args:
        try:
            pin = int(request.args.get('pin'))
        except (ValueError, TypeError):
            return jsonify({"error": "Parameter 'pin' must be an integer."}), 400

    # Validate and override 'click_delay' if provided in URL parameters
    if 'click_delay' in request.args:
        try:
            click_delay = float(request.args.get('click_delay'))
        except (ValueError, TypeError):
            return jsonify({"error": "Parameter 'click_delay' must be a number."}), 400

    logger.info("Custom Trigger endpoint accessed by user %s from %s", user.username, get_client_ip())

    if isPi:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
        GPIO.output(pin, GPIO.HIGH)
        time.sleep(click_delay)
        GPIO.output(pin, GPIO.LOW)

    return jsonify({'message': f"{config.friendly_name} triggered by {user.username}"}), 200

# -----------------------------------------------------------------------------
# Admin Endpoints for Managing Users
# -----------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged_in' not in session:
            logger.warning("Unauthorized admin access attempt from %s", get_client_ip())
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

@admin_app.route('/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == config.admin_password:
            session['admin_logged_in'] = True
            logger.info("Admin logged in successfully from %s", get_client_ip())
            return redirect(url_for('admin_dashboard'))
        else:
            logger.warning("Failed admin login attempt from %s", get_client_ip())
            return render_template('admin_login.html', error="Invalid password")
    return render_template('admin_login.html')

@admin_app.route('/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    logger.info("Admin logged out from %s", get_client_ip())
    return redirect(url_for('admin_login'))

@admin_app.route('/')
@login_required
def admin_dashboard():
    users = User.query.all()
    logger.info("Admin dashboard accessed from %s", get_client_ip())
    return render_template('admin_dashboard.html', users=users)


@admin_app.route('/api/v1/user/add', methods=['POST'])
@login_required
def add_user():
    data = request.get_json() or {}
    name = data.get('name')

    if not name:
        logger.warning("Add user failed: missing name from %s", get_client_ip())
        return jsonify({'message': 'Name is required!'}), 400

    if User.query.filter_by(username=name).first():
        logger.warning("Add user failed: user %s already exists from %s", name, get_client_ip())
        return jsonify({'message': 'User with that name already exists!'}), 400

    expiration_date = datetime.now(timezone.utc) + timedelta(days=config.api_key_expiration_days)
    new_user = User(username=name, api_key_expiration=expiration_date, enabled=True)
    db.session.add(new_user)
    db.session.commit()
    logger.info("User %s added by admin from %s", name, get_client_ip())

    return jsonify({
        'message': 'User added successfully',
        'api_key': new_user.api_key,
        'user': {
            'id': new_user.id,
            'username': new_user.username,
            'api_key_expiration': new_user.api_key_expiration.strftime('%Y-%m-%d %H:%M:%S'),
            'enabled': new_user.enabled
        }
    }), 201

@admin_app.route('/api/v1/user/enable/<int:user_id>', methods=['POST'])
@login_required
def admin_enable_user(user_id):
    user = User.query.get_or_404(user_id)
    user.enabled = True
    db.session.commit()
    logger.info("Enabled user %s by admin from %s", user.username, get_client_ip())
    return redirect(url_for('admin_dashboard'))

@admin_app.route('/api/v1/user/regenerate/<int:user_id>', methods=['POST'])
@login_required
def admin_regenerate_api_key(user_id):
    user = User.query.get_or_404(user_id)
    
    new_api_key = generate_urlsafe_key()
    user.api_key = new_api_key
    
    user.api_key_expiration = datetime.now(timezone.utc) + timedelta(days=config.api_key_expiration_days)
    
    db.session.commit()
    
    logger.info("Regenerated API key for user %s by admin from %s", user.username, get_client_ip())
    
    return jsonify({
        'message': 'API token regenerated successfully',
        'api_key': new_api_key,
        'user': {
            'id': user.id,
            'username': user.username,
            'api_key_expiration': user.api_key_expiration.strftime('%Y-%m-%d %H:%M:%S')
        }
    }), 200

@admin_app.route('/api/v1/user/disable/<int:user_id>', methods=['POST'])
@login_required
def admin_disable_user(user_id):
    user = User.query.get_or_404(user_id)
    user.enabled = False
    db.session.commit()
    logger.info("Disabled user %s by admin from %s", user.username, get_client_ip())
    return redirect(url_for('admin_dashboard'))

@admin_app.route('/api/v1/user/delete/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    logger.info("Deleted user %s by admin from %s", user.username, get_client_ip())
    return redirect(url_for('admin_dashboard'))

# -----------------------------------------------------------------------------
# Error Handlers for graceful error logging
# -----------------------------------------------------------------------------
@app.errorhandler(404)
def not_found_error(error):
    logger.warning("404 Not Found: %s", request.url)
    return jsonify({'message': 'Not found'}), 404

@admin_app.errorhandler(404)
def admin_not_found_error(error):
    logger.warning("Admin 404 Not Found: %s", request.url)
    return jsonify({'message': 'Not found'}), 404

@app.errorhandler(Exception)
def internal_error(error):
    logger.error("Unhandled Exception: %s", error, exc_info=True)
    return jsonify({'message': 'Internal server error'}), 500

@admin_app.errorhandler(Exception)
def admin_internal_error(error):
    logger.error("Unhandled Exception in admin app: %s", error, exc_info=True)
    return jsonify({'message': 'Internal server error'}), 500

# -----------------------------------------------------------------------------
# Run both the main and admin apps concurrently using multiprocessing.
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    from multiprocessing import Process

    def run_app():
        logger.info("Starting main app on port 5151")
        serve(app, host="0.0.0.0", port=5151)

    def run_admin_app():
        logger.info("Starting admin app on port 5252")
        serve(admin_app, host="0.0.0.0", port=5252)

    p1 = Process(target=run_app)
    p2 = Process(target=run_admin_app)
    p1.start()
    p2.start()
    p1.join()
    p2.join()