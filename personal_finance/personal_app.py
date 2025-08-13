from flask import Blueprint, jsonify, current_app, redirect, url_for, flash, render_template, request, session, make_response
from flask_login import current_user, login_required
from utils import requires_role, is_admin, get_mongo_db, limiter, PERSONAL_TOOLS, PERSONAL_EXPLORE_FEATURES
import utils
from translations import trans
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG)

personal_bp = Blueprint('personal', __name__, url_prefix='/personal', template_folder='templates/personal')

# Register personal finance sub-blueprints
from personal.bill import bill_bp
from personal.budget import budget_bp
from personal.summaries import summaries_bp
from personal.shopping import shopping_bp

personal_bp.register_blueprint(bill_bp)
personal_bp.register_blueprint(budget_bp)
personal_bp.register_blueprint(summaries_bp)
personal_bp.register_blueprint(shopping_bp)

def init_app(app):
    """Initialize all personal finance sub-blueprints."""
    try:
        for blueprint in [bill_bp, budget_bp, summaries_bp, shopping_bp, food_order_bp]:
            if hasattr(blueprint, 'init_app'):
                blueprint.init_app(app)
                current_app.logger.info(f"Initialized {blueprint.name} blueprint", extra={'session_id': 'no-request-context'})
        current_app.logger.info("Personal finance blueprints initialized successfully", extra={'session_id': 'no-request-context'})
    except Exception as e:
        current_app.logger.error(f"Error initializing personal finance blueprints: {str(e)}", extra={'session_id': 'no-request-context'})
        raise

@personal_bp.route('/')
@login_required
@requires_role(['personal', 'admin'])
def index():
    """Render the personal finance dashboard."""
    try:
        current_app.logger.info(f"Accessing personal.index - User: {current_user.id}, Authenticated: {current_user.is_authenticated}, Session: {dict(session)}")
        
        # Define PERSONAL_TOOLS with dynamic URLs
        hardcoded_tools = [
            {
                "endpoint": "personal.shopping.main",
                "label": "Shopping",
                "label_key": "shopping_management",
                "description_key": "shopping_management_desc",
                "tooltip_key": "shopping_tooltip",
                "icon": "bi-cart",
                "url": url_for("personal.shopping.main", _external=True)
            },

        ]

        # Define PERSONAL_EXPLORE_FEATURES with dynamic URLs
        hardcoded_features = [
            {
                "endpoint": "personal.budget.main",
                "label": "Budget",
                "label_key": "budget_budget_planner",
                "description_key": "budget_budget_desc",
                "tooltip_key": "budget_tooltip",
                "icon": "bi-wallet",
                "url": url_for("personal.budget.main", _external=True)
            },
            {
                "endpoint": "personal.bill.main",
                "label": "Bills",
                "label_key": "bill_bill_planner",
                "description_key": "bill_bill_desc",
                "tooltip_key": "bill_tooltip",
                "icon": "bi-receipt",
                "url": url_for("personal.bill.main", _external=True)
            },
            {
                "endpoint": "personal.shopping.main",
                "label": "Shopping",
                "label_key": "shopping_management",
                "description_key": "shopping_management_desc",
                "tooltip_key": "shopping_tooltip",
                "icon": "bi-cart",
                "url": url_for("personal.shopping.main", _external=True)
            },
            {
                "endpoint": "credits.request_credits",
                "label": "Request Credits",
                "label_key": "credits_request",
                "description_key": "credits_request_desc",
                "tooltip_key": "credits_request_tooltip",
                "icon": "bi-coin",
                "url": url_for("credits.request_credits", _external=True)
            },
            {
                "endpoint": "credits.history",
                "label": "Credits History",
                "label_key": "credits_your_wallet",
                "description_key": "credits_your_wallet_desc",
                "tooltip_key": "credits_your_wallet_tooltip",
                "icon": "bi-coin",
                "url": url_for("credits.history", _external=True)
            },
            {
                "endpoint": "reports.index",
                "label": "Reports",
                "label_key": "personal_reports",
                "description_key": "personal_reports_desc",
                "tooltip_key": "personal_reports_tooltip",
                "icon": "bi-journal-minus",
                "url": url_for("reports.budget_performance", _external=True)
            },
        ]

        response = make_response(render_template(
            'personal/GENERAL/index.html',
            title=trans('general_welcome', lang=session.get('lang', 'en'), default='Welcome'),
            tools_for_template=hardcoded_tools,
            explore_features_for_template=hardcoded_features,
            is_admin=is_admin(),
            is_anonymous=False,
            is_public=False
        ))
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        current_app.logger.error(f"Error rendering personal index: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('general_error', default='An error occurred'), 'danger')
        response = make_response(render_template(
            'personal/GENERAL/error.html',
            error_message="Unable to load the personal finance dashboard due to an internal error.",
            title=trans('general_welcome', lang=session.get('lang', 'en'), default='Welcome'),
            is_admin=is_admin()
        ), 500)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response
