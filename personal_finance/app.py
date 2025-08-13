# Import necessary modules
import os
import sys
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
import logging
import uuid
from datetime import datetime, date, timedelta
from flask import (
    Flask, jsonify, request, render_template, redirect, url_for, flash,
    make_response, has_request_context, g, send_from_directory, session, Response, current_app, abort
)
from flask_session import Session
from flask_cors import CORS
from werkzeug.security import generate_password_hash
from itsdangerous import URLSafeTimedSerializer
from dotenv import load_dotenv
from functools import wraps
from mailersend_email import init_email_config
from scheduler_setup import init_scheduler
from models import (
    create_user, get_user_by_email, get_user, get_budgets, get_bills,
    to_dict_budget, to_dict_bill, initialize_app_data
)
from tax_models import (
    initialize_tax_data, get_payment_locations, to_dict_payment_location
)
import utils
from session_utils import create_anonymous_session
from translations import register_translation, trans, get_translations, get_all_translations, get_module_translations
from flask_login import LoginManager, login_required, current_user, UserMixin, logout_user
from flask_wtf.csrf import CSRFError
from jinja2.exceptions import TemplateNotFound
import time
from pymongo import MongoClient
import certifi
from credits.routes import credits_bp
import re
from flask_mailman import Mail
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from flask_babel import Babel
from flask_compress import Compress
import requests
from business_finance import business

# Load environment variables
load_dotenv()

# Set up logging
root_logger = logging.getLogger('ficore_app')
root_logger.setLevel(logging.INFO)

class SessionFormatter(logging.Formatter):
    def format(self, record):
        record.session_id = getattr(record, 'session_id', 'no-session-id')
        record.user_role = getattr(record, 'user_role', 'anonymous')
        record.ip_address = getattr(record, 'ip_address', 'unknown')
        return super().format(record)

class SessionAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        kwargs['extra'] = kwargs.get('extra', {})
        session_id = 'no-session-id'
        user_role = 'anonymous'
        ip_address = 'unknown'
        try:
            if has_request_context():
                session_id = session.get('sid', 'no-session-id')
                user_role = current_user.role if current_user.is_authenticated else 'anonymous'
                ip_address = request.remote_addr
            else:
                session_id = f'non-request-{str(uuid.uuid4())[:8]}'
        except Exception as e:
            session_id = f'session-error-{str(uuid.uuid4())[:8]}'
            kwargs['extra']['session_error'] = str(e)
        kwargs['extra']['session_id'] = session_id
        kwargs['extra']['user_role'] = user_role
        kwargs['extra']['ip_address'] = ip_address
        return msg, kwargs

logger = SessionAdapter(root_logger, {})

# Initialize extensions
login_manager = utils.LoginManager()
flask_session = utils.Session()
csrf = utils.CSRFProtect()
babel = utils.Babel()
compress = utils.Compress()
limiter = utils.Limiter(key_func=get_remote_address, default_limits=['200 per day', '50 per hour'], storage_uri='memory://')

# Decorators
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            logger.warning("Unauthorized access attempt to admin route", extra={'ip_address': request.remote_addr})
            return redirect(url_for('users.login'))
        if not utils.is_admin():
            flash(utils.trans('general_no_permission', default='You do not have permission to access this page.'), 'danger')
            logger.warning(f"Non-admin user {current_user.id} attempted access", extra={'ip_address': request.remote_addr})
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def custom_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.is_authenticated or session.get('is_anonymous', False):
            return f(*args, **kwargs)
        logger.info("Redirecting unauthenticated user to login", extra={'ip_address': request.remote_addr})
        return redirect(url_for('users.login', next=request.url))
    return decorated_function

def ensure_session_id(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            if 'sid' not in session or not session.get('sid'):
                if not current_user.is_authenticated:
                    utils.create_anonymous_session()
                    logger.info(f'New anonymous session created: {session["sid"]}',
                                extra={'ip_address': request.remote_addr})
                else:
                    user_id = getattr(current_user, 'id', 'unknown-user')
                    session['sid'] = str(uuid.uuid4())
                    session['is_anonymous'] = False
                    session.modified = True
                    logger.info(f'New session ID generated for authenticated user {user_id}: {session["sid"]}',
                                extra={'ip_address': request.remote_addr})
            else:
                # Validate session in MongoDB
                session_id = session.get('sid')
                # Ensure current_app.extensions['mongo']['ficodb'] is correctly initialized
                mongo_session = current_app.extensions['mongo']['ficodb']['sessions'].find_one({'_id': session_id})
                if not mongo_session and current_user.is_authenticated:
                    logger.info(f'Invalid or expired session {session_id} for user {current_user.id}, logging out')
                    logout_user()
                    session.clear()
                    session['lang'] = session.get('lang', 'en')
                    utils.create_anonymous_session()
                    flash(utils.trans('session_timeout', default='Your session has timed out.'), 'warning')

                    # Clear client-side cookie
                    response = make_response(redirect(url_for('users.login')))
                    response.set_cookie(
                        current_app.config['SESSION_COOKIE_NAME'],
                        '',
                        expires=0,
                        httponly=True,
                        secure=current_app.config.get('SESSION_COOKIE_SECURE', True)
                    )
                    return response

        except Exception as e:
            logger.error(f'Session operation failed: {str(e)}', exc_info=True)
            session.clear()
            session['lang'] = session.get('lang', 'en')
            utils.create_anonymous_session()
            flash(utils.trans('session_error', default='An error occurred with your session. Please log in again.'), 'danger')
            # Clear client-side cookie on error
            response = make_response(redirect(url_for('users.login')))
            response.set_cookie(
                current_app.config['SESSION_COOKIE_NAME'],
                '',
                expires=0,
                httponly=True,
                secure=current_app.config.get('SESSION_COOKIE_SECURE', True)
            )
            return response
        return f(*args, **kwargs)
    return decorated_function

def is_safe_referrer(referrer, host):
    """Check if the referrer is safe (same host or None)."""
    if not referrer:
        return False
    try:
        from urllib.parse import urlparse
        parsed_referrer = urlparse(referrer)
        parsed_host = urlparse(f'http://{host}')
        return parsed_referrer.netloc == parsed_host.netloc
    except Exception as e:
        logger.error(f'Error checking referrer safety: {str(e)}')
        return False

def setup_logging(app):
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(SessionFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s [session: %(session_id)s, role: %(user_role)s, ip: %(ip_address)s]'))
    root_logger.handlers = []  # Clear existing handlers
    root_logger.addHandler(handler)
    
    flask_logger = logging.getLogger('flask')
    werkzeug_logger = logging.getLogger('werkzeug')
    pymongo_logger = logging.getLogger('pymongo')
    flask_logger.handlers = []
    werkzeug_logger.handlers = []
    pymongo_logger.handlers = []
    flask_logger.addHandler(handler)
    werkzeug_logger.addHandler(handler)
    pymongo_logger.addHandler(handler)
    flask_logger.setLevel(logging.INFO)
    werkzeug_logger.setLevel(logging.INFO)
    pymongo_logger.setLevel(logging.INFO)
    
    logger.info('Logging setup complete with StreamHandler for ficore_app, flask, werkzeug, and pymongo')

def check_mongodb_connection(app):
    try:
        client = app.extensions['mongo']
        client.admin.command('ping')
        logger.info('MongoDB connection verified with ping')
        return True
    except Exception as e:
        logger.error(f'MongoDB connection failed: {str(e)}', exc_info=True)
        return False

def setup_session(app):
    try:
        with app.app_context():
            max_retries = 3
            for attempt in range(max_retries):
                if check_mongodb_connection(app):
                    app.config['SESSION_TYPE'] = 'mongodb'
                    app.config['SESSION_MONGODB'] = app.extensions['mongo']
                    app.config['SESSION_MONGODB_DB'] = 'ficodb'
                    app.config['SESSION_MONGODB_COLLECT'] = 'sessions'
                    app.config['SESSION_PERMANENT'] = False
                    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=5)
                    app.config['SESSION_USE_SIGNER'] = True
                    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
                    app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_ENV', 'development') == 'production'
                    app.config['SESSION_COOKIE_HTTPONLY'] = True
                    app.config['SESSION_COOKIE_NAME'] = 'ficore_session'
                    utils.flask_session.init_app(app)

                    # Add TTL index for sessions collection
                    db = app.extensions['mongo']['ficodb']
                    db.sessions.create_index("created_at", expireAfterSeconds=300)  # 5 minute
                    logger.info(f'Session configured: type={app.config["SESSION_TYPE"]}, db={app.config["SESSION_MONGODB_DB"]}, collection={app.config["SESSION_MONGODB_COLLECT"]}')
                    return
                logger.warning(f'MongoDB connection attempt {attempt + 1} failed, retrying...')
                time.sleep(1)
            logger.error('MongoDB client is not available after retries, falling back to filesystem session')
            app.config['SESSION_TYPE'] = 'filesystem'
            utils.flask_session.init_app(app)
            logger.info('Session configured with filesystem fallback')
    except Exception as e:
        logger.error(f'Failed to configure session: {str(e)}', exc_info=True)
        app.config['SESSION_TYPE'] = 'filesystem'
        utils.flask_session.init_app(app)
        logger.info('Session configured with filesystem fallback due to error')

class User(UserMixin):
    def __init__(self, id, email, display_name=None, role='personal'):
        self.id = id
        self.email = email
        self.display_name = display_name or id
        self.role = role

    def get(self, key, default=None):
        try:
            with current_app.app_context():
                user = current_app.extensions['mongo']['ficodb'].users.find_one({'_id': self.id})
                return user.get(key, default) if user else default
        except Exception as e:
            logger.error(f'Error fetching user data for {self.id}: {str(e)}', exc_info=True)
            return default

    @property
    def is_active(self):
        try:
            with current_app.app_context():
                user = current_app.extensions['mongo']['ficodb'].users.find_one({'_id': self.id})
                return user.get('is_active', True) if user else False
        except Exception as e:
            logger.error(f'Error checking active status for user {self.id}: {str(e)}', exc_info=True)
            return False

    def get_id(self):
        return str(self.id)

    def get_first_name(self):
        try:
            with current_app.app_context():
                user = current_app.extensions['mongo']['ficodb'].users.find_one({'_id': self.id})
                if user and 'personal_details' in user:
                    return user['personal_details'].get('first_name', self.display_name)
                return self.display_name
        except Exception as e:
            logger.error(f'Error fetching first name for user {self.id}: {str(e)}', exc_info=True)
            return self.display_name

def create_app():
    app = Flask(__name__, template_folder='templates', static_folder='static')
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Load configuration
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
    if not app.config['SECRET_KEY']:
        logger.error('SECRET_KEY environment variable is not set')
        raise ValueError('SECRET_KEY must be set in environment variables')

    app.config['SERVER_NAME'] = os.getenv('SERVER_NAME', 'ficore-africa.onrender.com')
    app.config['APPLICATION_ROOT'] = os.getenv('APPLICATION_ROOT', '/')
    app.config['PREFERRED_URL_SCHEME'] = os.getenv('PREFERRED_URL_SCHEME', 'https')
    
    app.config['MONGO_URI'] = os.getenv('MONGO_URI')
    if not app.config['MONGO_URI']:
        logger.error('MONGO_URI environment variable is not set')
        raise ValueError('MONGO_URI must be set in environment variables')
    
    app.config['GOOGLE_CLIENT_ID'] = os.getenv('GOOGLE_CLIENT_ID')
    app.config['GOOGLE_CLIENT_SECRET'] = os.getenv('GOOGLE_CLIENT_SECRET')
    app.config['GOOGLE_API_KEY'] = os.getenv('GOOGLE_API_KEY')
    app.config['SMTP_SERVER'] = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    app.config['SMTP_PORT'] = int(os.getenv('SMTP_PORT', 587))
    app.config['SMTP_USERNAME'] = os.getenv('SMTP_USERNAME')
    app.config['SMTP_PASSWORD'] = os.getenv('SMTP_PASSWORD')
    app.config['SMS_API_URL'] = os.getenv('SMS_API_URL')
    app.config['SMS_API_KEY'] = os.getenv('SMS_API_KEY')
    app.config['WHATSAPP_API_URL'] = os.getenv('WHATSAPP_API_URL')
    app.config['WHATSAPP_API_KEY'] = os.getenv('WHATSAPP_API_KEY')
    app.config['BASE_URL'] = os.getenv('BASE_URL', 'http://localhost:5000')
    app.config['SETUP_KEY'] = os.getenv('SETUP_KEY')
    
    # Validate critical environment variables
    for key in ['SETUP_KEY', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'SMTP_USERNAME', 'SMTP_PASSWORD']:
        if not app.config.get(key):
            logger.warning(f'{key} environment variable not set; some features may be disabled')

    # Initialize MongoDB client with explicit TLS/SSL
    try:
        client = MongoClient(
            app.config['MONGO_URI'],
            serverSelectionTimeoutMS=5000,
            tls=True,
            tlsCAFile=certifi.where() if os.getenv('MONGO_CA_FILE') is None else os.getenv('MONGO_CA_FILE'),
            maxPoolSize=50,
            minPoolSize=5
        )
        app.extensions = getattr(app, 'extensions', {})
        app.extensions['mongo'] = client
        client.admin.command('ping')
        logger.info('MongoDB client initialized successfully')
        
        def shutdown_mongo_client():
            try:
                mongo = app.extensions.get('mongo')
                if mongo:
                    mongo.close()
                    logger.info('MongoDB client closed successfully')
            except Exception as e:
                logger.error(f'Error closing MongoDB client: {str(e)}', exc_info=True)
        
    except Exception as e:
        logger.error(f'MongoDB connection test failed: {str(e)}', exc_info=True)
        raise RuntimeError(f'Failed to connect to MongoDB: {str(e)}')
    
    # Initialize extensions
    setup_logging(app)
    utils.compress.init_app(app)
    utils.csrf.init_app(app)
    mail = Mail()
    mail.init_app(app)
    utils.limiter.init_app(app)
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    utils.babel.init_app(app)
    utils.login_manager.init_app(app)
    utils.login_manager.login_view = 'users.login'

    # User loader callback for Flask-Login
    @utils.login_manager.user_loader
    def load_user(user_id):
        try:
            with app.app_context():
                user = app.extensions['mongo']['ficodb'].users.find_one({'_id': user_id})
                if not user:
                    return None
                return User(
                    id=user['_id'],
                    email=user['email'],
                    display_name=user.get('display_name', user['_id']),
                    role=user.get('role', 'personal')
                )
        except Exception as e:
            logger.error(f"Error loading user {user_id}: {str(e)}", exc_info=True)
            return None

    # Initialize MongoDB, session, scheduler, and other components within app context
    try:
        with app.app_context():
            initialize_app_data(app)
            logger.info('Database initialized successfully')

            setup_session(app)

            scheduler = init_scheduler(app, app.extensions['mongo']['ficodb'])
            app.config['SCHEDULER'] = scheduler
            logger.info('Scheduler initialized successfully')
            
            def shutdown_scheduler():
                try:
                    if scheduler and getattr(scheduler, 'running', False):
                        scheduler.shutdown(wait=True)
                        logger.info('Scheduler shutdown successfully')
                except Exception as e:
                    logger.error(f'Error shutting down scheduler: {str(e)}', exc_info=True)

            personal_finance_collections = [
                'budgets', 'bills', 'bill_reminders'
            ]
            db = app.extensions['mongo']['ficodb']
            for collection_name in personal_finance_collections:
                if collection_name not in db.list_collection_names():
                    db.create_collection(collection_name)
                    logger.info(f'Created collection: {collection_name}')
            
            # Initialize tax-related collections
            try:
                initialize_tax_data(db, trans)
                logger.info('Tax-related data initialized successfully', extra={'session_id': 'no-session-id'})
            except Exception as e:
                logger.error(f'Failed to initialize tax-related data: {str(e)}', exc_info=True)
                raise
            
            try:
                db.bills.create_index([('user_id', 1), ('due_date', 1)])
                db.bills.create_index([('session_id', 1), ('due_date', 1)])
                db.bills.create_index([('created_at', -1)])
                db.bills.create_index([('due_date', 1)])
                db.bills.create_index([('status', 1)])
                db.budgets.create_index([('user_id', 1), ('created_at', -1)])
                db.budgets.create_index([('session_id', 1), ('created_at', -1)])
                db.budgets.create_index([('created_at', -1)])
                db.bill_reminders.create_index([('user_id', 1), ('sent_at', -1)])
                db.bill_reminders.create_index([('notification_id', 1)])
                db.records.create_index([('user_id', 1), ('type', 1), ('created_at', -1)])
                db.cashflows.create_index([('user_id', 1), ('type', 1), ('created_at', -1)])
                logger.info('Created indexes for collections')
            except Exception as e:
                logger.warning(f'Some indexes may already exist: {str(e)}')
            
            admin_email = os.getenv('ADMIN_EMAIL', 'ficoreafrica@gmail.com')
            admin_password = os.getenv('ADMIN_PASSWORD')
            if not admin_password:
                logger.error('ADMIN_PASSWORD environment variable is not set')
                raise ValueError('ADMIN_PASSWORD must be set in environment variables')
            admin_username = os.getenv('ADMIN_USERNAME', 'admin')
            admin_user = get_user_by_email(db, admin_email)
            if not admin_user:
                user_data = {
                    '_id': admin_username.lower(),
                    'username': admin_username.lower(),
                    'email': admin_email.lower(),
                    'password': admin_password,  # Pass plain password
                    'is_admin': True,
                    'role': 'admin',
                    'created_at': datetime.utcnow(),
                    'lang': 'en',
                    'setup_complete': True,
                    'display_name': admin_username
                }
                create_user(db, user_data)
                logger.info(f'Admin user created with email: {admin_email}')
            else:
                # Update password if admin exists to ensure correct hash
                db.users.update_one(
                    {'_id': admin_username.lower()},
                    {'$set': {'password_hash': generate_password_hash(admin_password)}}
                )
                logger.info(f'Admin user password updated for email: {admin_email}')

    except Exception as e:
        logger.error(f'Error in create_app initialization: {str(e)}', exc_info=True)
        raise

    # Register blueprints
    from users.routes import users_bp
    from agents.routes import agents_bp
    from creditors.routes import creditors_bp
    from dashboard.routes import dashboard_bp
    from debtors.routes import debtors_bp
    from payments.routes import payments_bp
    from receipts.routes import receipts_bp
    from reports.routes import reports_bp
    from settings.routes import settings_bp
    from personal import personal_bp
    from general.routes import general_bp
    from admin.routes import admin_bp
    from taxation.routes import taxation_bp
    from ai import ai_bp
    
    app.register_blueprint(users_bp, url_prefix='/users')
    logger.info('Registered users blueprint')
    app.register_blueprint(agents_bp, url_prefix='/agents')
    logger.info('Registered agents blueprint')
    app.register_blueprint(taxation_bp, url_prefix='/taxation')
    logger.info('Registered taxation blueprint')
    try:
        app.register_blueprint(credits_bp, url_prefix='/credits')
        logger.info('Registered credits blueprint and initialized limiter')
    except Exception as e:
        logger.warning(f'Could not import credits blueprint: {str(e)}')
    app.register_blueprint(creditors_bp, url_prefix='/creditors')
    logger.info('Registered creditors blueprint')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    logger.info('Registered dashboard blueprint')
    app.register_blueprint(debtors_bp, url_prefix='/debtors')
    logger.info('Registered debtors blueprint')
    app.register_blueprint(payments_bp, url_prefix='/payments')
    logger.info('Registered payments blueprint')
    app.register_blueprint(receipts_bp, url_prefix='/receipts')
    logger.info('Registered receipts blueprint')
    app.register_blueprint(reports_bp, url_prefix='/reports')
    logger.info('Registered reports blueprint')
    app.register_blueprint(settings_bp, url_prefix='/settings')
    logger.info('Registered settings blueprint')
    try:
        app.register_blueprint(admin_bp, url_prefix='/admin')
        logger.info('Registered admin blueprint')
    except Exception as e:
        logger.warning(f'Could not import admin blueprint: {str(e)}')
    app.register_blueprint(personal_bp)
    logger.info('Registered personal blueprint with url_prefix="/personal"')
    app.register_blueprint(general_bp, url_prefix='/general')
    logger.info('Registered general blueprint')
    app.register_blueprint(business, url_prefix='/business')
    logger.info('Registered business blueprint with url_prefix="/business"')
    app.register_blueprint(ai_bp)
    logger.info('Registered AI blueprint')

    utils.initialize_tools_with_urls(app)
    logger.info('Initialized tools and navigation with resolved URLs')

    app.jinja_env.globals.update(
        FACEBOOK_URL=app.config.get('FACEBOOK_URL', 'https://facebook.com/ficoreafrica'),
        TWITTER_URL=app.config.get('TWITTER_URL', 'https://x.com/ficoreafrica'),
        LINKEDIN_URL=app.config.get('LINKEDIN_URL', 'https://linkedin.com/company/ficoreafrica'),
        FEEDBACK_FORM_URL=app.config.get('FEEDBACK_FORM_URL', '#'),
        WAITLIST_FORM_URL=app.config.get('WAITLIST_FORM_URL', '#'),
        CONSULTANCY_FORM_URL=app.config.get('CONSULTANCY_FORM_URL', '#'),
        trans=trans,
        trans_function=utils.trans_function,
        get_translations=get_translations,
        is_admin=utils.is_admin
    )
    
    @app.template_filter('safe_nav')
    def safe_nav(value):
        try:
            if not isinstance(value, dict) or 'icon' not in value:
                logger.warning(f'Invalid navigation item: {value}')
                return {'icon': 'bi-question-circle', 'label': value.get('label', ''), 'url': value.get('url', '#')}
            if not value.get('icon', '').startswith('bi-'):
                logger.warning(f'Invalid icon in navigation item: {value.get("icon")}')
                value['icon'] = 'bi-question-circle'
            return value
        except Exception as e:
            logger.error(f'Navigation rendering error: {str(e)}', exc_info=True)
            return {'icon': 'bi-question-circle', 'label': str(value), 'url': '#'}

    @app.template_filter('format_number')
    def format_number(value):
        try:
            if isinstance(value, (int, float)):
                return f'{float(value):,.2f}'
            return str(value)
        except (ValueError, TypeError) as e:
            logger.warning(f'Error formatting number {value}: {str(e)}')
            return str(value)

    app.jinja_env.filters['format_currency'] = utils.format_currency

    @app.template_filter('format_datetime')
    def format_datetime(value):
        try:
            locale = session.get('lang', 'en')
            format_str = '%B %d, %Y, %I:%M %p' if locale == 'en' else '%d %B %Y, %I:%M %p'
            if isinstance(value, datetime):
                return value.strftime(format_str)
            elif isinstance(value, date):
                return value.strftime('%B %d, %Y' if locale == 'en' else '%d %B %Y')
            elif isinstance(value, str):
                parsed = datetime.strptime(value, '%Y-%m-%d')
                return parsed.strftime(format_str)
            return str(value)
        except Exception as e:
            logger.warning(f'Error formatting datetime {value}: {str(e)}')
            return str(value)
    
    @app.template_filter('format_date')
    def format_date(value):
        try:
            locale = session.get('lang', 'en')
            format_str = '%Y-%m-%d' if locale == 'en' else '%d-%m-%Y'
            if isinstance(value, datetime):
                return value.strftime(format_str)
            elif isinstance(value, date):
                return value.strftime(format_str)
            elif isinstance(value, str):
                parsed = datetime.strptime(value, '%Y-%m-%d').date()
                return parsed.strftime(format_str)
            return str(value)
        except Exception as e:
            logger.warning(f'Error formatting date {value}: {str(e)}')
            return str(value)
    
    @app.template_filter('trans')
    def trans_filter(key, **kwargs):
        lang = session.get('lang', 'en')
        translation = utils.trans(key, lang=lang, **kwargs)
        if translation == key:
            logger.warning(f'Missing translation for key="{key}" in lang="{lang}"')
            return key
        return translation
        
    @app.context_processor
    def inject_role_nav():
        tools_for_template = []
        explore_features_for_template = []
        bottom_nav_items = []
        try:
            with app.app_context():
                if current_user.is_authenticated:
                    if current_user.role == 'personal':
                        tools_for_template = utils.PERSONAL_TOOLS
                        explore_features_for_template = utils.PERSONAL_EXPLORE_FEATURES
                        bottom_nav_items = utils.PERSONAL_NAV
                    elif current_user.role == 'trader':
                        tools_for_template = utils.BUSINESS_TOOLS
                        explore_features_for_template = utils.BUSINESS_EXPLORE_FEATURES
                        bottom_nav_items = utils.BUSINESS_NAV
                    elif current_user.role == 'agent':
                        tools_for_template = utils.AGENT_TOOLS
                        explore_features_for_template = utils.AGENT_EXPLORE_FEATURES
                        bottom_nav_items = utils.AGENT_NAV
                    elif current_user.role == 'admin':
                        tools_for_template = utils.ALL_TOOLS
                        explore_features_for_template = utils.ADMIN_EXPLORE_FEATURES
                        bottom_nav_items = utils.ADMIN_NAV
                else:
                    explore_features_for_template = utils.get_explore_features()
                for nav_list in [tools_for_template, explore_features_for_template, bottom_nav_items]:
                    for item in nav_list:
                        if not isinstance(item, dict) or 'icon' not in item or not item['icon'].startswith('bi-'):
                            logger.warning(f'Invalid or missing icon in navigation item: {item}')
                            item['icon'] = 'bi-question-circle'
                logger.info('Navigation data injected for template rendering')
        except Exception as e:
            logger.error(f'Error in inject_role_nav: {str(e)}', exc_info=True)
        return dict(
            tools_for_template=tools_for_template,
            explore_features_for_template=explore_features_for_template,
            bottom_nav_items=bottom_nav_items,
            t=trans,
            lang=session.get('lang', 'en'),
            format_currency=utils.format_currency,
            format_date=utils.format_date
        )
    
    @app.context_processor
    def inject_globals():
        lang = session.get('lang', 'en')
        def context_trans(key, **kwargs):
            used_lang = kwargs.pop('lang', lang)
            return utils.trans(
                key,
                lang=used_lang,
                logger=g.get('logger', logger) if has_request_context() else logger,
                **kwargs
            )
        supported_languages = app.config.get('SUPPORTED_LANGUAGES', ['en', 'ha'])
        return {
            'google_client_id': app.config.get('GOOGLE_CLIENT_ID', ''),
            'trans': context_trans,
            'get_translations': get_translations,
            'current_year': datetime.now().year,
            'LINKEDIN_URL': app.config.get('LINKEDIN_URL', 'https://linkedin.com/company/ficoreafrica'),
            'TWITTER_URL': app.config.get('TWITTER_URL', 'https://x.com/ficoreafrica'),
            'FACEBOOK_URL': app.config.get('FACEBOOK_URL', 'https://facebook.com/ficoreafrica'),
            'FEEDBACK_FORM_URL': app.config.get('FEEDBACK_FORM_URL', '#'),
            'WAITLIST_FORM_URL': app.config.get('WAITLIST_FORM_URL', '#'),
            'CONSULTANCY_FORM_URL': app.config.get('CONSULTANCY_FORM_URL', '#'),
            'current_lang': lang,
            'current_user': current_user if has_request_context() else None,
            'available_languages': [
                {'code': code, 'name': utils.trans(f'lang_{code}', lang=lang, default=code.capitalize())}
                for code in supported_languages
            ],
            'dialogflow_agent_id': app.config.get('DIALOGFLOW_PROJECT_ID', 'ficoreassistant-kywl')
        }
    
    @app.after_request
    def add_security_headers(response):
        if not request.path.startswith('/api'):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://code.jquery.com https://cdnjs.cloudflare.com https://www.gstatic.com https://www.gstatic.com/dialogflow-console/;"
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com https://cdnjs.cloudflare.com;"
            "img-src 'self' data:;"
            "connect-src 'self' https://api.ficore.app;"
            "font-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com https://fonts.gstatic.com https://cdnjs.cloudflare.com;"
        )
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Strict-Transport-Security'] = 'max-age=3600; includeSubDomains'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        return response
    
    @app.route('/change-language', methods=['POST'])
    @utils.limiter.limit('10 per minute')
    def change_language():
        try:
            data = request.get_json()
            new_lang = data.get('language', 'en')
            supported_languages = app.config.get('SUPPORTED_LANGUAGES', ['en', 'ha'])
            if new_lang in supported_languages:
                session['lang'] = new_lang
                with app.app_context():
                    if current_user.is_authenticated:
                        try:
                            app.extensions['mongo']['ficodb'].users.update_one(
                                {'_id': current_user.id},
                                {'$set': {'language': new_lang}}
                            )
                        except Exception as e:
                            logger.warning(f'Could not update user language preference: {str(e)}')
                
                logger.info(f'Language changed to {new_lang}', 
                           extra={'session_id': session.get('sid', 'no-session-id'), 'ip_address': request.remote_addr})
                
                return jsonify({
                    'success': True, 
                    'message': utils.trans('lang_change_success', lang=new_lang)
                })
            else:
                logger.warning(f'Invalid language requested: {new_lang}')
                return jsonify({
                    'success': False, 
                    'message': utils.trans('lang_invalid')
                }), 400
        except Exception as e:
            logger.error(f'Error changing language: {str(e)}', 
                        extra={'session_id': session.get('sid', 'no-session-id'), 'ip_address': request.remote_addr})
            return jsonify({
                'success': False, 
                'message': utils.trans('error_general')
            }), 500
    
    @app.route('/', methods=['GET', 'HEAD'])
    @ensure_session_id
    def index():
        lang = session.get('lang', 'en')
        logger.info(f'Serving index page, authenticated: {current_user.is_authenticated}, user: {current_user.username if current_user.is_authenticated and hasattr(current_user, "username") else "None"}', 
                    extra={'ip_address': request.remote_addr})
        if request.method == 'HEAD':
            return '', 200
        if current_user.is_authenticated:
            if current_user.role == 'agent':
                return redirect(url_for('agents_bp.agent_portal'))
            elif current_user.role == 'trader':
                return redirect(url_for('business.home'))
            elif current_user.role == 'admin':
                try:
                    return redirect(url_for('dashboard.index'))
                except:
                    return redirect(url_for('general_bp.home'))
            elif current_user.role == 'personal':
                return redirect(url_for('personal.index'))
        return redirect(url_for('general_bp.landing'))
    
    @app.route('/general_dashboard')
    @ensure_session_id
    def general_dashboard():
        logger.info(f'Redirecting to unified dashboard for {"authenticated" if current_user.is_authenticated else "anonymous"}', 
                    extra={'ip_address': request.remote_addr})
        return redirect(url_for('dashboard_bp.index'))
    
    @app.route('/business-agent-home')
    @ensure_session_id
    def business_agent_home():
        lang = session.get('lang', 'en')
        logger.info(f'Serving business-agent home, authenticated: {current_user.is_authenticated}')
        if current_user.is_authenticated:
            if current_user.role == 'agent':
                return redirect(url_for('agents_bp.agent_portal'))
            elif current_user.role == 'trader':
                return redirect(url_for('business.home'))
            else:
                flash(utils.trans('no_permission'), 'error')
                return redirect(url_for('index'))
        try:
            logger.info('Serving public business-agent-home')
            return render_template(
                'general/home.html',
                is_public=True,
                title=utils.trans('business_home', lang=lang)
            )
        except TemplateNotFound as e:
            logger.error(f'Template not found: {str(e)}')
            return render_template(
                'personal/GENERAL/error.html',
                error=str(e),
                title=utils.trans('business_home', lang=lang)
            ), 404
    
    @app.route('/health')
    @utils.limiter.limit('10 per minute')
    def health():
        logger.info('Performing health check')
        status = {'status': 'healthy'}
        try:
            with app.app_context():
                app.extensions['mongo'].admin.command('ping')
            return jsonify(status), 200
        except Exception as e:
            logger.error(f'Health check failed: {str(e)}')
            status['status'] = 'unhealthy'
            status['details'] = str(e)
            return jsonify(status), 500
    
    @app.route('/api/translations/<lang>')
    @utils.limiter.limit('10 per minute')
    def get_translations_api(lang):
        try:
            supported_languages = app.config.get('SUPPORTED_LANGUAGES', ['en', 'ha'])
            if lang not in supported_languages:
                return jsonify({'error': utils.trans('invalid_language')}), 400
            
            all_translations = get_all_translations()
            result = {}
            for module_name, module_translations in all_translations.items():
                if lang in module_translations:
                    result.update(module_translations[lang])
            
            return jsonify({'translations': result})
        except Exception as e:
            logger.error(f'Translation API error: {str(e)}')
            return jsonify({'error': utils.trans('error')}), 500
    
    @app.route('/api/translate')
    @utils.limiter.limit('10 per minute')
    def api_translate():
        try:
            key = request.args.get('key')
            lang = request.args.get('lang', session.get('lang', 'en'))
            supported_languages = app.config.get('SUPPORTED_LANGUAGES', ['en', 'ha'])
            if not key:
                return jsonify({'error': utils.trans('missing_key')}), 400
            if lang not in supported_languages:
                return jsonify({'error': utils.trans('invalid_language')}), 400
            
            translation = utils.trans(key, lang=lang)
            return jsonify({'key': key, 'translation': translation, 'lang': lang})
        except Exception as e:
            logger.error(f'Translate API error: {str(e)}')
            return jsonify({'error': utils.trans('error')}), 500
    
    @app.route('/set_language/<lang>')
    @utils.limiter.limit('10 per minute')
    def set_language(lang):
        supported_languages = current_app.config.get('SUPPORTED_LANGUAGES', ['en', 'ha'])
        new_lang = lang if lang in supported_languages else 'en'
        
        try:
            session['lang'] = new_lang
            if current_user.is_authenticated:
                try:
                    current_app.extensions['mongo']['ficodb'].users.update_one(
                        {'_id': current_user.id},
                        {'$set': {'language': new_lang}}
                    )
                except Exception as e:
                    logger.warning(
                        f'Could not update user language for user {current_user.id}: {str(e)}',
                        extra={'session_id': session.get('sid', 'no-session-id'), 'ip_address': request.remote_addr}
                    )
                    flash(utils.trans('invalid_lang', default='Could not update language'), 'danger')
                    return redirect(url_for('index'))
                    
            logger.info(
                f'Set language to {new_lang} for user {current_user.id if current_user.is_authenticated else "anonymous"}',
                extra={'session_id': session.get('sid', 'no-session-id'), 'ip_address': request.remote_addr}
            )
            flash(utils.trans('lang_updated', default='Language updated successfully'), 'success')
            
            redirect_url = request.referrer if is_safe_referrer(request.referrer, request.host) else url_for('index')
            return redirect(redirect_url)
        except Exception as e:
            logger.error(
                f'Session error: {str(e)}',
                extra={'session_id': session.get('sid', 'no-session-id'), 'ip_address': request.remote_addr}
            )
            flash(utils.trans('invalid_lang', default='Could not update language'), 'danger')
            return redirect(url_for('index'))
    
    @app.route('/setup', methods=['GET'])
    @utils.limiter.limit('10 per minute')
    def setup_database_route():
        setup_key = request.args.get('key')
        lang = session.get('lang', 'en')
        if not app.config.get('SETUP_KEY') or setup_key != app.config['SETUP_KEY']:
            logger.warning(f'Invalid setup key: {setup_key}')
            try:
                return render_template(
                    'error/403.html',
                    content=utils.trans('access_denied'),
                    title=utils.trans('access_denied', lang=lang)
                ), 403
            except TemplateNotFound as e:
                logger.error(f'Template not found: {str(e)}')
                return render_template(
                    'personal/GENERAL/error.html',
                    content=utils.trans('access_denied'),
                    title=utils.trans('access_denied', lang=lang)
                ), 403
        try:
            with app.app_context():
                initialize_app_data(app)
            flash(utils.trans('db_setup_success'), 'success')
            logger.info('Database setup completed')
            return redirect(url_for('index'))
        except Exception as e:
            flash(utils.trans('db_setup_error'), 'danger')
            logger.error(f'DB setup error: {str(e)}')
            try:
                return render_template(
                    'personal/GENERAL/error.html',
                    content=utils.trans('server_error'),
                    title=utils.trans('error', lang=lang)
                ), 500
            except TemplateNotFound as e:
                logger.error(f'Template not found: {str(e)}')
                return render_template(
                    'personal/GENERAL/error.html',
                    content=utils.trans('server_error'),
                    title=utils.trans('error', lang=lang)
                ), 500
    
    @app.route('/static/<path:filename>')
    def static_files(filename):
        if '..' in filename or filename.startswith('/'):
            logger.warning(f'Invalid static path: {filename}')
            abort(404)
        try:
            response = send_from_directory('static', filename)
            if filename.endswith('.woff2'):
                response.headers['Content-Type'] = 'font/woff2'
                response.headers['Cache-Control'] = 'public, max-age=604800'
            else:
                response.headers['Cache-Control'] = 'public, max-age=3600'
            return response
        except FileNotFoundError:
            logger.error(f'Static file not found: {filename}')
            abort(404)
    
    @app.route('/static_personal/<path:filename>')
    def static_personal(filename):
        allowed_extensions = {'.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg'}
        allowed_mime_types = {
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.ico': 'image/x-icon',
            '.svg': 'image/svg+xml'
        }
        file_ext = os.path.splitext(filename)[1].lower()
        if '..' in filename or filename.startswith('/') or file_ext not in allowed_extensions:
            logger.warning(f'Invalid personal file path or ext: {filename}')
            abort(404)
        try:
            response = send_from_directory('static_personal', filename)
            response.headers['Content-Type'] = allowed_mime_types.get(file_ext, 'application/octet-stream')
            if file_ext in {'.css', '.js'}:
                response.headers['Cache-Control'] = 'public, max-age=3600'
            elif file_ext in {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg'}:
                response.headers['Cache-Control'] = 'public, max-age=604800'
            return response
        except FileNotFoundError:
            logger.error(f'File not found: {filename}')
            abort(404)
    
    @app.route('/favicon.ico')
    def favicon():
        try:
            return send_from_directory(app.static_folder, 'img/favicon.ico')
        except FileNotFoundError:
            logger.error('Favicon not found')
            abort(404)
    
    @app.route('/service-worker.js')
    def service_worker():
        try:
            return app.send_static_file('js/service-worker.js')
        except FileNotFoundError:
            logger.error('Service worker not found')
            abort(404)
    
    @app.route('/manifest.json')
    def manifest():
        manifest_data = {
            "name": "FiCore App",
            "short_name": "FiCore",
            "description": "Financial management for personal and business use",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#007bff",
            "icons": [
                {
                    "src": "/static/img/icon-192x192.png",
                    "sizes": "192x192",
                    "type": "image/png"
                },
                {
                    "src": "/static/img/icon-512x512.png",
                    "sizes": "512x512",
                    "type": "image/png"
                }
            ],
            "lang": session.get('lang', 'en'),
            "dir": "ltr",
            "orientation": "portrait",
            "scope": "/",
            "related_applications": [],
            "prefer_related_applications": False
        }
        return jsonify(manifest_data)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        logger.error(f'CSRF error: {str(e)}')
        try:
            return render_template(
                'error/403.html', 
                error=utils.trans('csrf_error'), 
                title=utils.trans('csrf_error', lang=session.get('lang', 'en'))
            ), 400
        except TemplateNotFound:
            return render_template(
                'personal/GENERAL/error.html', 
                error=utils.trans('csrf_error'), 
                title=utils.trans('csrf_error', lang=session.get('lang', 'en'))
            ), 400

    @app.errorhandler(404)
    def page_not_found(e):
        logger.error(f'Not found: {request.url}')
        try:
            return render_template(
                'personal/GENERAL/404.html', 
                error=str(e), 
                title=utils.trans('not_found', lang=session.get('lang', 'en'))
            ), 404
        except TemplateNotFound:
            return render_template(
                'personal/GENERAL/error.html', 
                error=str(e), 
                title=utils.trans('not_found', lang=session.get('lang', 'en'))
            ), 404

    @app.errorhandler(500)
    def internal_server_error(e):
        logger.error(f'Server error: {str(e)}')
        try:
            return render_template(
                'personal/GENERAL/error.html', 
                error=str(e), 
                title=utils.trans('server_error', lang=session.get('lang', 'en'))
            ), 500
        except TemplateNotFound:
            return render_template(
                'personal/GENERAL/error.html', 
                error=str(e), 
                title=utils.trans('server_error', lang=session.get('lang', 'en'))
            ), 500
    
    @app.before_request
    def check_session_timeout():
        # Skip session timeout check for static routes to avoid unnecessary processing
        if request.path.startswith('/static/') or request.path in ['/favicon.ico', '/manifest.json', '/service-worker.js']:
            return

        if current_user.is_authenticated and 'last_activity' in session:
            last_activity = session.get('last_activity')
            timeout_minutes = 5  # Adjust as needed

            # Convert last_activity to datetime if it's a string
            if isinstance(last_activity, str):
                try:
                    last_activity = datetime.fromisoformat(last_activity.replace(' ', 'T'))  # Handle space-separated datetime strings
                except ValueError:
                    logger.warning(f"Invalid last_activity format: {last_activity}, resetting to now")
                    last_activity = datetime.utcnow()
                    session['last_activity'] = last_activity

            # Check if session has timed out
            if (datetime.utcnow() - last_activity).total_seconds() > timeout_minutes * 60:
                user_id = current_user.id
                sid = session.get('sid', 'no-session-id')
                logger.info(f"Session timeout for user {user_id}, logging out")
                logout_user()
                # Delete MongoDB session
                if current_app.config.get('SESSION_TYPE') == 'mongodb':
                    try:
                        db = current_app.extensions['mongo']['ficodb']
                        db.sessions.delete_one({'_id': sid})
                        logger.info(f"Deleted MongoDB session for user {user_id}, SID: {sid}")
                    except Exception as e:
                        logger.error(f"Failed to delete MongoDB session for SID {sid}: {str(e)}")
                # Clear session and create new anonymous session
                session.clear()
                session['lang'] = session.get('lang', 'en')
                utils.create_anonymous_session()
                logger.info(f"New anonymous session created after timeout: {session['sid']}")
                flash(utils.trans('session_timeout', default='Your session has timed out.'), 'warning')
                response = make_response(redirect(url_for('users.login')))
                response.set_cookie(
                    current_app.config['SESSION_COOKIE_NAME'],
                    '',
                    expires=0,
                    httponly=True,
                    secure=current_app.config.get('SESSION_COOKIE_SECURE', True)
                )
                return response

        # Update last_activity for authenticated users or initialize for new sessions
        if current_user.is_authenticated:
            session['last_activity'] = datetime.utcnow()

    scheduler_shutdown_done = False
    mongo_client_closed = False

    @app.teardown_appcontext
    def cleanup_scheduler(exception):
        nonlocal scheduler_shutdown_done
        scheduler = app.config.get('SCHEDULER')
        if scheduler and scheduler.running and not scheduler_shutdown_done:
            try:
                scheduler.shutdown()
                logger.info('Scheduler shutdown')
                scheduler_shutdown_done = True
            except Exception as e:
                logger.error(f'Scheduler shutdown error: {str(e)}')

    return app

app = create_app()

if __name__ == '__main__':
    logger.info('Starting Flask application')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
