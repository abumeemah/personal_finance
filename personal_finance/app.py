import os
import sys
import logging
import uuid
from datetime import datetime, timedelta
from flask import (
    Flask, jsonify, request, render_template, redirect, url_for, flash,
    make_response, session, abort, current_app
)
from flask_session import Session
from flask_cors import CORS
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv
from functools import wraps
from pymongo import MongoClient
import certifi
from flask_login import LoginManager, login_required, current_user, UserMixin, logout_user
from flask_wtf.csrf import CSRFProtect, CSRFError
from jinja2.exceptions import TemplateNotFound
import utils
from mailersend_email import init_email_config
from scheduler_setup import init_scheduler
from models import create_user, get_user_by_email, initialize_app_data
from credits.routes import credits_bp
from dashboard.routes import dashboard_bp
from users.routes import users_bp
from reports.routes import reports_bp
from settings.routes import settings_bp
from general.routes import general_bp
from admin.routes import admin_bp
from bill import bill_bp
from budget import budget_bp
import summaries_bp
from shopping import shopping_bp

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s [session: %(session_id)s, role: %(user_role)s, ip: %(ip_address)s]',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger('ficore_app')

# Initialize Flask app and extensions
app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app, resources={r"/api/*": {"origins": "*"}})
app.config.from_mapping(
    SECRET_KEY=os.getenv('SECRET_KEY'),
    SERVER_NAME=os.getenv('SERVER_NAME', 'ficore-africa.onrender.com'),
    MONGO_URI=os.getenv('MONGO_URI'),
    SESSION_TYPE='mongodb',
    SESSION_PERMANENT=False,
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=5),
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('FLASK_ENV', 'development') == 'production',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_NAME='ficore_session',
    SUPPORTED_LANGUAGES=['en', 'ha']
)

# Validate critical configuration
for key in ['SECRET_KEY', 'MONGO_URI', 'ADMIN_PASSWORD']:
    if not app.config.get(key):
        logger.error(f'{key} environment variable is not set')
        raise ValueError(f'{key} must be set')

# Initialize MongoDB
client = MongoClient(
    app.config['MONGO_URI'],
    serverSelectionTimeoutMS=5000,
    tls=True,
    tlsCAFile=certifi.where(),
    maxPoolSize=50,
    minPoolSize=5
)
app.extensions = {'mongo': client}
client.admin.command('ping')
logger.info('MongoDB client initialized')

# Initialize extensions
Session(app)
CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'users.login'

# Session decorator
def ensure_session_id(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'sid' not in session:
            if not current_user.is_authenticated:
                utils.create_anonymous_session()
                logger.info(f'New anonymous session created: {session["sid"]}')
            else:
                session['sid'] = str(uuid.uuid4())
                session['is_anonymous'] = False
                logger.info(f'New session for user {current_user.id}: {session["sid"]}')
        return f(*args, **kwargs)
    return decorated_function

# User class
class User(UserMixin):
    def __init__(self, id, email, display_name=None, role='personal'):
        self.id = id
        self.email = email
        self.display_name = display_name or id
        self.role = role

    @property
    def is_active(self):
        user = app.extensions['mongo']['ficodb'].users.find_one({'_id': self.id})
        return user.get('is_active', True) if user else False

    def get_id(self):
        return str(self.id)

@login_manager.user_loader
def load_user(user_id):
    user = app.extensions['mongo']['ficodb'].users.find_one({'_id': user_id})
    if not user:
        return None
    return User(
        id=user['_id'],
        email=user['email'],
        display_name=user.get('display_name', user['_id']),
        role=user.get('role', 'personal')
    )

# App setup
def create_app():
    # Setup session
    app.config.update(
        SESSION_MONGODB=app.extensions['mongo'],
        SESSION_MONGODB_DB='ficodb',
        SESSION_MONGODB_COLLECT='sessions'
    )
    app.extensions['mongo']['ficodb'].sessions.create_index("created_at", expireAfterSeconds=300)
    
    # Initialize data
    with app.app_context():
        initialize_app_data(app)
        initialize_tax_data(app.extensions['mongo']['ficodb'], utils.trans)
        
        # Create indexes
        db = app.extensions['mongo']['ficodb']
        for collection, indexes in [
            ('bills', [[('user_id', 1), ('due_date', 1)], [('session_id', 1), ('due_date', 1)], [('created_at', -1)], [('due_date', 1)], [('status', 1)]]),
            ('budgets', [[('user_id', 1), ('created_at', -1)], [('session_id', 1), ('created_at', -1)], [('created_at', -1)]]),
            ('bill_reminders', [[('user_id', 1), ('sent_at', -1)], [('notification_id', 1)]]),
            ('records', [[('user_id', 1), ('type', 1), ('created_at', -1)]]),
            ('cashflows', [[('user_id', 1), ('type', 1), ('created_at', -1)]])
        ]:
            for index in indexes:
                db[collection].create_index(index)
        
        # Setup admin user
        admin_email = os.getenv('ADMIN_EMAIL', 'ficoreafrica@gmail.com')
        admin_password = os.getenv('ADMIN_PASSWORD')
        admin_username = os.getenv('ADMIN_USERNAME', 'admin')
        if not get_user_by_email(db, admin_email):
            create_user(db, {
                '_id': admin_username.lower(),
                'username': admin_username.lower(),
                'email': admin_email.lower(),
                'password': admin_password,
                'is_admin': True,
                'role': 'admin',
                'created_at': datetime.utcnow(),
                'lang': 'en',
                'setup_complete': True,
                'display_name': admin_username
            })
        else:
            db.users.update_one(
                {'_id': admin_username.lower()},
                {'$set': {'password_hash': generate_password_hash(admin_password)}}
            )

    # Register blueprints
    app.register_blueprint(users_bp, url_prefix='/users')
    app.register_blueprint(credits_bp, url_prefix='/credits')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(reports_bp, url_prefix='/reports')
    app.register_blueprint(settings_bp, url_prefix='/settings')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(bill_bp, url_prefix='/bill')
    app.register_blueprint(budget_bp, url_prefix='/budget')
    app.register_blueprint(summaries_bp, url_prefix='/summaries')
    app.register_blueprint(shopping_bp, url_prefix='/shopping')
    app.register_blueprint(general_bp, url_prefix='/general')

    # Template filters and context processors
    app.jinja_env.globals.update(
        trans=utils.trans,
        is_admin=utils.is_admin,
        FACEBOOK_URL=app.config.get('FACEBOOK_URL', 'https://facebook.com/ficoreafrica'),
        TWITTER_URL=app.config.get('TWITTER_URL', 'https://x.com/ficoreafrica'),
        LINKEDIN_URL=app.config.get('LINKEDIN_URL', 'https://linkedin.com/company/ficoreafrica')
    )

    @app.template_filter('format_number')
    def format_number(value):
        try:
            return f'{float(value):,.2f}' if isinstance(value, (int, float)) else str(value)
        except (ValueError, TypeError):
            return str(value)

    @app.template_filter('format_datetime')
    def format_datetime(value):
        format_str = '%B %d, %Y, %I:%M %p' if session.get('lang', 'en') == 'en' else '%d %B %Y, %I:%M %p'
        try:
            if isinstance(value, datetime):
                return value.strftime(format_str)
            elif isinstance(value, str):
                return datetime.strptime(value, '%Y-%m-%d').strftime(format_str)
            return str(value)
        except Exception:
            return str(value)

    @app.context_processor
    def inject_globals():
        lang = session.get('lang', 'en')
        return {
            'trans': utils.trans,
            'current_lang': lang,
            'available_languages': [
                {'code': code, 'name': utils.trans(f'lang_{code}', lang=lang, default=code.capitalize())}
                for code in app.config['SUPPORTED_LANGUAGES']
            ]
        }

    # Routes
    @app.route('/')
    @ensure_session_id
    def index():
        if current_user.is_authenticated:
            if current_user.role == 'admin':
                return redirect(url_for('dashboard.index'))
            return redirect(url_for('bill_bp.home'))
        return redirect(url_for('general_bp.landing'))

    @app.route('/change-language', methods=['POST'])
    def change_language():
        data = request.get_json()
        new_lang = data.get('language', 'en')
        if new_lang in app.config['SUPPORTED_LANGUAGES']:
            session['lang'] = new_lang
            if current_user.is_authenticated:
                app.extensions['mongo']['ficodb'].users.update_one(
                    {'_id': current_user.id},
                    {'$set': {'language': new_lang}}
                )
            return jsonify({'success': True, 'message': utils.trans('lang_change_success', lang=new_lang)})
        return jsonify({'success': False, 'message': utils.trans('lang_invalid')}), 400

    @app.route('/health')
    def health():
        try:
            app.extensions['mongo'].admin.command('ping')
            return jsonify({'status': 'healthy'}), 200
        except Exception as e:
            return jsonify({'status': 'unhealthy', 'details': str(e)}), 500

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        return render_template(
            'error/403.html',
            error=utils.trans('csrf_error'),
            title=utils.trans('csrf_error', lang=session.get('lang', 'en'))
        ), 400

    @app.errorhandler(404)
    def page_not_found(e):
        return render_template(
            'general/404.html',
            error=str(e),
            title=utils.trans('not_found', lang=session.get('lang', 'en'))
        ), 404

    return app

app = create_app()

if __name__ == '__main__':
    logger.info('Starting Flask application')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
