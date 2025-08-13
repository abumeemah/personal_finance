from flask import Blueprint, request, session, redirect, url_for, render_template, flash, current_app, jsonify
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect, CSRFError
from wtforms import FloatField, IntegerField, SubmitField
from wtforms.validators import DataRequired, NumberRange, ValidationError
from flask_login import current_user, login_required
from utils import get_all_recent_activities, requires_role, is_admin, get_mongo_db, limiter, check_ficore_credit_balance
from datetime import datetime
import re
from translations import trans
from bson import ObjectId
from models import log_tool_usage
from session_utils import create_anonymous_session
import uuid

budget_bp = Blueprint(
    'budget',
    __name__,
    template_folder='templates/personal/BUDGET',
    url_prefix='/budget'
)

csrf = CSRFProtect()

def clean_currency(value):
    """Transform input into a float, returning None for invalid cases."""
    if not value or value == '0':
        return 0.0
    if isinstance(value, str):
        value = re.sub(r'[^\d.]', '', value.strip())
        parts = value.split('.')
        if len(parts) > 2:
            value = parts[0] + '.' + ''.join(parts[1:])
        if not value or value == '.':
            return 0.0
        try:
            float_value = float(value)
            if float_value > 1e308:
                return None  # Exceeds float limit
            return float_value
        except ValueError:
            return None
    return float(value) if value else 0.0

def strip_commas(value):
    """Filter to remove commas and return a float."""
    return clean_currency(value)

def format_currency(value):
    """Format a numeric value with comma separation, no currency symbol."""
    try:
        numeric_value = float(value)
        formatted = f"{numeric_value:,.2f}"
        return formatted
    except (ValueError, TypeError):
        return "0.00"

def custom_login_required(f):
    """Custom login decorator that allows both authenticated users and anonymous sessions."""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.is_authenticated or session.get('is_anonymous', False):
            return f(*args, **kwargs)
        return redirect(url_for('users.login', next=request.url))
    return decorated_function

def deduct_ficore_credits(db, user_id, amount, action, budget_id=None):
    """Deduct Ficore Credits from user balance and log the transaction."""
    try:
        user = db.users.find_one({'_id': user_id})
        if not user:
            return False
        current_balance = user.get('ficore_credit_balance', 0)
        if current_balance < amount:
            return False
        result = db.users.update_one(
            {'_id': user_id},
            {'$inc': {'ficore_credit_balance': -amount}}
        )
        if result.modified_count == 0:
            return False
        transaction = {
            '_id': ObjectId(),
            'user_id': user_id,
            'action': action,
            'amount': -amount,
            'budget_id': str(budget_id) if budget_id else None,
            'timestamp': datetime.utcnow(),
            'session_id': session.get('sid', 'unknown'),
            'status': 'completed'
        }
        db.ficore_credit_transactions.insert_one(transaction)
        return True
    except Exception:
        return False

class CommaSeparatedIntegerField(IntegerField):
    def process_formdata(self, valuelist):
        if valuelist:
            try:
                cleaned_value = clean_currency(valuelist[0])
                self.data = int(cleaned_value) if cleaned_value is not None else None
            except (ValueError, TypeError):
                self.data = None
                raise ValidationError(trans('budget_dependents_invalid', default='Not a valid integer'))

class BudgetForm(FlaskForm):
    income = FloatField(
        trans('budget_monthly_income', default='Monthly Income'),
        filters=[strip_commas],
        validators=[
            DataRequired(message=trans('budget_income_required', default='Income is required')),
            NumberRange(min=0, max=10000000000, message=trans('budget_income_max', default='Income must be between 0 and 10 billion'))
        ]
    )
    housing = FloatField(
        trans('budget_housing_rent', default='Housing/Rent'),
        filters=[strip_commas],
        validators=[
            DataRequired(message=trans('budget_housing_required', default='Housing cost is required')),
            NumberRange(min=0, max=10000000000, message=trans('budget_amount_max', default='Amount must be between 0 and 10 billion'))
        ]
    )
    food = FloatField(
        trans('budget_food', default='Food'),
        filters=[strip_commas],
        validators=[
            DataRequired(message=trans('budget_food_required', default='Food cost is required')),
            NumberRange(min=0, max=10000000000, message=trans('budget_amount_max', default='Amount must be between 0 and 10 billion'))
        ]
    )
    transport = FloatField(
        trans('budget_transport', default='Transport'),
        filters=[strip_commas],
        validators=[
            DataRequired(message=trans('budget_transport_required', default='Transport cost is required')),
            NumberRange(min=0, max=10000000000, message=trans('budget_amount_max', default='Amount must be between 0 and 10 billion'))
        ]
    )
    dependents = CommaSeparatedIntegerField(
        trans('budget_dependents_support', default='Dependents Support'),
        validators=[
            DataRequired(message=trans('budget_dependents_required', default='Number of dependents is required')),
            NumberRange(min=0, max=100, message=trans('budget_dependents_max', default='Number of dependents cannot exceed 100'))
        ]
    )
    miscellaneous = FloatField(
        trans('budget_miscellaneous', default='Miscellaneous'),
        filters=[strip_commas],
        validators=[
            DataRequired(message=trans('budget_miscellaneous_required', default='Miscellaneous cost is required')),
            NumberRange(min=0, max=10000000000, message=trans('budget_amount_max', default='Amount must be between 0 and 10 billion'))
        ]
    )
    others = FloatField(
        trans('budget_others', default='Others'),
        filters=[strip_commas],
        validators=[
            DataRequired(message=trans('budget_others_required', default='Other expenses are required')),
            NumberRange(min=0, max=10000000000, message=trans('budget_amount_max', default='Amount must be between 0 and 10 billion'))
        ]
    )
    savings_goal = FloatField(
        trans('budget_savings_goal', default='Savings Goal'),
        filters=[strip_commas],
        validators=[
            DataRequired(message=trans('budget_savings_goal_required', default='Savings goal is required')),
            NumberRange(min=0, max=10000000000, message=trans('budget_amount_max', default='Amount must be between 0 and 10 billion'))
        ]
    )
    submit = SubmitField(trans('budget_submit', default='Submit'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lang = session.get('lang', 'en')
        self.income.label.text = trans('budget_monthly_income', lang) or 'Monthly Income'
        self.housing.label.text = trans('budget_housing_rent', lang) or 'Housing/Rent'
        self.food.label.text = trans('budget_food', lang) or 'Food'
        self.transport.label.text = trans('budget_transport', lang) or 'Transport'
        self.dependents.label.text = trans('budget_dependents_support', lang) or 'Dependents Support'
        self.miscellaneous.label.text = trans('budget_miscellaneous', lang) or 'Miscellaneous'
        self.others.label.text = trans('budget_others', lang) or 'Others'
        self.savings_goal.label.text = trans('budget_savings_goal', lang) or 'Savings Goal'
        self.submit.label.text = trans('budget_submit', lang) or 'Submit'

    def validate(self, extra_validators=None):
        return super().validate(extra_validators)

@budget_bp.route('/main', methods=['GET', 'POST'])
@custom_login_required
@requires_role(['personal', 'admin'])
@limiter.limit("10 per minute")
def main():
    if 'sid' not in session:
        create_anonymous_session()
        session['is_anonymous'] = True
        current_app.logger.debug(f"New anonymous session created with sid: {session['sid']}", extra={'session_id': session['sid']})
    session.permanent = True
    session.modified = True
    form = BudgetForm()
    db = get_mongo_db()

    valid_tabs = ['create-budget', 'dashboard']
    active_tab = request.args.get('tab', 'create-budget')
    if active_tab not in valid_tabs:
        active_tab = 'create-budget'

    try:
        log_tool_usage(
            tool_name='budget',
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'unknown'),
            action='main_view'
        )
    except Exception as e:
        current_app.logger.error(f"Failed to log tool usage: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('budget_log_error', default='Error logging budget activity. Please try again.'), 'warning')

    try:
        activities = get_all_recent_activities(
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'unknown') if not current_user.is_authenticated else None,
        )
        current_app.logger.debug(f"Fetched {len(activities)} recent activities for {'user ' + str(current_user.id) if current_user.is_authenticated else 'session ' + session.get('sid', 'unknown')}", extra={'session_id': session.get('sid', 'unknown')})
    except Exception as e:
        current_app.logger.error(f"Failed to fetch recent activities: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('budget_activities_load_error', default='Error loading recent activities.'), 'warning')
        activities = []

    try:
        filter_criteria = {} if is_admin() else {'user_id': current_user.id} if current_user.is_authenticated else {'session_id': session['sid']}
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'create_budget' and form.validate_on_submit():
                if current_user.is_authenticated and not is_admin():
                    if not check_ficore_credit_balance(required_amount=1, user_id=current_user.id):
                        current_app.logger.warning(f"Insufficient Ficore Credits for creating budget by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('budget_insufficient_credits', default='Insufficient Ficore Credits to create a budget. Please purchase more credits.'), 'danger')
                        return redirect(url_for('dashboard.index'))
                try:
                    log_tool_usage(
                        tool_name='budget',
                        db=db,
                        user_id=current_user.id if current_user.is_authenticated else None,
                        session_id=session.get('sid', 'unknown'),
                        action='create_budget'
                    )
                except Exception as e:
                    current_app.logger.error(f"Failed to log budget creation: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                    flash(trans('budget_log_error', default='Error logging budget creation. Continuing with submission.'), 'warning')

                income = form.income.data
                expenses = sum([
                    form.housing.data,
                    form.food.data,
                    form.transport.data,
                    float(form.dependents.data),
                    form.miscellaneous.data,
                    form.others.data
                ])
                savings_goal = form.savings_goal.data
                surplus_deficit = income - expenses
                budget_id = ObjectId()
                budget_data = {
                    '_id': budget_id,
                    'user_id': current_user.id if current_user.is_authenticated else None,
                    'session_id': session['sid'],
                    'user_email': current_user.email if current_user.is_authenticated else '',
                    'income': income,
                    'fixed_expenses': expenses,
                    'variable_expenses': 0.0,
                    'savings_goal': savings_goal,
                    'surplus_deficit': surplus_deficit,
                    'housing': form.housing.data,
                    'food': form.food.data,
                    'transport': form.transport.data,
                    'dependents': form.dependents.data,
                    'miscellaneous': form.miscellaneous.data,
                    'others': form.others.data,
                    'created_at': datetime.utcnow()
                }
                current_app.logger.debug(f"Saving budget data: {budget_data}", extra={'session_id': session['sid']})
                try:
                    db.budgets.insert_one(budget_data)
                    if current_user.is_authenticated and not is_admin():
                        if not deduct_ficore_credits(db, current_user.id, 1, 'create_budget', budget_id):
                            db.budgets.delete_one({'_id': budget_id})  # Rollback on failure
                            current_app.logger.error(f"Failed to deduct Ficore Credit for creating budget {budget_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                            flash(trans('budget_credit_deduction_failed', default='Failed to deduct Ficore Credit for creating budget.'), 'danger')
                            return redirect(url_for('budget.main', tab='create-budget'))
                    current_app.logger.info(f"Budget {budget_id} saved successfully to MongoDB for session {session['sid']}", extra={'session_id': session['sid']})
                    flash(trans("budget_completed_success", default='Budget created successfully!'), "success")
                    return redirect(url_for('budget.main', tab='dashboard'))
                except Exception as e:
                    current_app.logger.error(f"Failed to save budget {budget_id} to MongoDB for session {session['sid']}: {str(e)}", extra={'session_id': session['sid']})
                    flash(trans("budget_storage_error", default='Error saving budget.'), "danger")
                    return render_template(
                        'budget_main.html',
                        form=form,
                        budgets={},
                        latest_budget={
                            'id': None,
                            'user_id': None,
                            'session_id': session.get('sid', 'unknown'),
                            'user_email': current_user.email if current_user.is_authenticated else '',
                            'income': format_currency(0.0),
                            'income_raw': 0.0,
                            'fixed_expenses': format_currency(0.0),
                            'fixed_expenses_raw': 0.0,
                            'variable_expenses': format_currency(0.0),
                            'variable_expenses_raw': 0.0,
                            'savings_goal': format_currency(0.0),
                            'savings_goal_raw': 0.0,
                            'surplus_deficit': 0.0,
                            'surplus_deficit_formatted': format_currency(0.0),
                            'housing': format_currency(0.0),
                            'housing_raw': 0.0,
                            'food': format_currency(0.0),
                            'food_raw': 0.0,
                            'transport': format_currency(0.0),
                            'transport_raw': 0.0,
                            'dependents': format_currency(0.0),
                            'dependents_raw': 0,
                            'miscellaneous': format_currency(0.0),
                            'miscellaneous_raw': 0.0,
                            'others': format_currency(0.0),
                            'others_raw': 0.0,
                            'created_at': 'N/A'
                        },
                        categories={},
                        tips=[],
                        insights=[],
                        activities=activities,
                        tool_title=trans('budget_title', default='Budget Planner'),
                        active_tab=active_tab
                    )
            elif action == 'delete':
                budget_id = request.form.get('budget_id')
                budget = db.budgets.find_one({'_id': ObjectId(budget_id), **filter_criteria})
                if not budget:
                    current_app.logger.warning(f"Budget {budget_id} not found for deletion", extra={'session_id': session.get('sid', 'unknown')})
                    flash(trans("budget_not_found", default='Budget not found.'), "danger")
                    return redirect(url_for('budget.main', tab='dashboard'))
                if current_user.is_authenticated and not is_admin():
                    if not check_ficore_credit_balance(required_amount=1, user_id=current_user.id):
                        current_app.logger.warning(f"Insufficient Ficore Credits for deleting budget {budget_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('budget_insufficient_credits', default='Insufficient Ficore Credits to delete a budget. Please purchase more credits.'), 'danger')
                        return redirect(url_for('dashboard.index'))
                try:
                    log_tool_usage(
                        tool_name='budget',
                        db=db,
                        user_id=current_user.id if current_user.is_authenticated else None,
                        session_id=session.get('sid', 'unknown'),
                        action='delete_budget'
                    )
                    result = db.budgets.delete_one({'_id': ObjectId(budget_id), **filter_criteria})
                    if result.deleted_count > 0:
                        if current_user.is_authenticated and not is_admin():
                            if not deduct_ficore_credits(db, current_user.id, 1, 'delete_budget', budget_id):
                                current_app.logger.error(f"Failed to deduct Ficore Credit for deleting budget {budget_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                                flash(trans('budget_credit_deduction_failed', default='Failed to deduct Ficore Credit for deleting budget.'), 'danger')
                                return redirect(url_for('budget.main', tab='dashboard'))
                        current_app.logger.info(f"Deleted budget ID {budget_id} for session {session['sid']}", extra={'session_id': session['sid']})
                        flash(trans("budget_deleted_success", default='Budget deleted successfully!'), "success")
                    else:
                        current_app.logger.warning(f"Budget ID {budget_id} not found for session {session['sid']}", extra={'session_id': session['sid']})
                        flash(trans("budget_not_found", default='Budget not found.'), "danger")
                except Exception as e:
                    current_app.logger.error(f"Failed to delete budget ID {budget_id} for session {session['sid']}: {str(e)}", extra={'session_id': session['sid']})
                    flash(trans("budget_delete_failed", default='Error deleting budget.'), "danger")
                return redirect(url_for('budget.main', tab='dashboard'))

        budgets = list(db.budgets.find(filter_criteria).sort('created_at', -1).limit(10))
        current_app.logger.info(f"Read {len(budgets)} records from MongoDB budgets collection [session: {session['sid']}]", extra={'session_id': session['sid']})
        budgets_dict = {}
        latest_budget = None
        for budget in budgets:
            budget_data = {
                'id': str(budget['_id']),
                'user_id': budget.get('user_id'),
                'session_id': budget.get('session_id'),
                'user_email': budget.get('user_email', current_user.email if current_user.is_authenticated else ''),
                'income': format_currency(budget.get('income', 0.0)),
                'income_raw': float(budget.get('income', 0.0)),
                'fixed_expenses': format_currency(budget.get('fixed_expenses', 0.0)),
                'fixed_expenses_raw': float(budget.get('fixed_expenses', 0.0)),
                'variable_expenses': format_currency(budget.get('variable_expenses', 0.0)),
                'variable_expenses_raw': float(budget.get('variable_expenses', 0.0)),
                'savings_goal': format_currency(budget.get('savings_goal', 0.0)),
                'savings_goal_raw': float(budget.get('savings_goal', 0.0)),
                'surplus_deficit': float(budget.get('surplus_deficit', 0.0)),
                'surplus_deficit_formatted': format_currency(budget.get('surplus_deficit', 0.0)),
                'housing': format_currency(budget.get('housing', 0.0)),
                'housing_raw': float(budget.get('housing', 0.0)),
                'food': format_currency(budget.get('food', 0.0)),
                'food_raw': float(budget.get('food', 0.0)),
                'transport': format_currency(budget.get('transport', 0.0)),
                'transport_raw': float(budget.get('transport', 0.0)),
                'dependents': str(budget.get('dependents', 0)),
                'dependents_raw': int(budget.get('dependents', 0)),
                'miscellaneous': format_currency(budget.get('miscellaneous', 0.0)),
                'miscellaneous_raw': float(budget.get('miscellaneous', 0.0)),
                'others': format_currency(budget.get('others', 0.0)),
                'others_raw': float(budget.get('others', 0.0)),
                'created_at': budget.get('created_at').strftime('%Y-%m-%d') if budget.get('created_at') else 'N/A'
            }
            budgets_dict[budget_data['id']] = budget_data
            if not latest_budget or (budget.get('created_at') and (latest_budget['created_at'] == 'N/A' or budget.get('created_at') > datetime.strptime(latest_budget['created_at'], '%Y-%m-%d'))):
                latest_budget = budget_data
        if not latest_budget:
            latest_budget = {
                'id': None,
                'user_id': None,
                'session_id': session.get('sid', 'unknown'),
                'user_email': current_user.email if current_user.is_authenticated else '',
                'income': format_currency(0.0),
                'income_raw': 0.0,
                'fixed_expenses': format_currency(0.0),
                'fixed_expenses_raw': 0.0,
                'variable_expenses': format_currency(0.0),
                'variable_expenses_raw': 0.0,
                'savings_goal': format_currency(0.0),
                'savings_goal_raw': 0.0,
                'surplus_deficit': 0.0,
                'surplus_deficit_formatted': format_currency(0.0),
                'housing': format_currency(0.0),
                'housing_raw': 0.0,
                'food': format_currency(0.0),
                'food_raw': 0.0,
                'transport': format_currency(0.0),
                'transport_raw': 0.0,
                'dependents': str(0),
                'dependents_raw': 0,
                'miscellaneous': format_currency(0.0),
                'miscellaneous_raw': 0.0,
                'others': format_currency(0.0),
                'others_raw': 0.0,
                'created_at': 'N/A'
            }
        categories = {
            trans('budget_housing_rent', default='Housing/Rent'): latest_budget.get('housing_raw', 0.0),
            trans('budget_food', default='Food'): latest_budget.get('food_raw', 0.0),
            trans('budget_transport', default='Transport'): latest_budget.get('transport_raw', 0.0),
            trans('budget_dependents_support', default='Dependents Support'): latest_budget.get('dependents_raw', 0),
            trans('budget_miscellaneous', default='Miscellaneous'): latest_budget.get('miscellaneous_raw', 0.0),
            trans('budget_others', default='Others'): latest_budget.get('others_raw', 0.0)
        }
        categories = {k: v for k, v in categories.items() if v > 0}
        tips = [
            trans("budget_tip_track_expenses", default='Track your expenses daily to stay within budget.'),
            trans("budget_tip_ajo_savings", default='Contribute to ajo savings for financial discipline.'),
            trans("budget_tip_data_subscriptions", default='Optimize data subscriptions to reduce costs.'),
            trans("budget_tip_plan_dependents", default='Plan for dependents’ expenses in advance.')
        ]
        insights = []
        try:
            income_float = float(latest_budget.get('income_raw', 0.0))
            surplus_deficit_float = float(latest_budget.get('surplus_deficit', 0.0))
            savings_goal_float = float(latest_budget.get('savings_goal_raw', 0.0))
            if income_float > 0:
                if surplus_deficit_float < 0:
                    insights.append(trans("budget_insight_budget_deficit", default='Your expenses exceed your income. Consider reducing costs.'))
                elif surplus_deficit_float > 0:
                    insights.append(trans("budget_insight_budget_surplus", default='You have a surplus. Consider increasing savings.'))
                if savings_goal_float == 0:
                    insights.append(trans("budget_insight_set_savings_goal", default='Set a savings goal to build financial security.'))
                if income_float > 0 and latest_budget.get('housing_raw', 0.0) / income_float > 0.4:
                    insights.append(trans("budget_insight_high_housing", default='Housing costs exceed 40% of income. Consider cost-saving measures.'))
        except (ValueError, TypeError) as e:
            current_app.logger.warning(f"Error parsing budget amounts for insights: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        current_app.logger.debug(f"Latest budget: {latest_budget}", extra={'session_id': session.get('sid', 'unknown')})
        current_app.logger.debug(f"Categories: {categories}", extra={'session_id': session.get('sid', 'unknown')})
        return render_template(
            'budget_main.html',
            form=form,
            budgets=budgets_dict,
            latest_budget=latest_budget,
            categories=categories,
            tips=tips,
            insights=insights,
            activities=activities,
            tool_title=trans('budget_title', default='Budget Planner'),
            active_tab=active_tab
        )
    except Exception as e:
        current_app.logger.exception(f"Unexpected error in budget.main active_tab: {active_tab}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('budget_dashboard_load_error', default='Error loading budget dashboard.'), 'danger')
        return render_template(
            'budget_main.html',
            form=form,
            budgets={},
            latest_budget={
                'id': None,
                'user_id': None,
                'session_id': session.get('sid', 'unknown'),
                'user_email': current_user.email if current_user.is_authenticated else '',
                'income': format_currency(0.0),
                'income_raw': 0.0,
                'fixed_expenses': format_currency(0.0),
                'fixed_expenses_raw': 0.0,
                'variable_expenses': format_currency(0.0),
                'variable_expenses_raw': 0.0,
                'savings_goal': format_currency(0.0),
                'savings_goal_raw': 0.0,
                'surplus_deficit': 0.0,
                'surplus_deficit_formatted': format_currency(0.0),
                'housing': format_currency(0.0),
                'housing_raw': 0.0,
                'food': format_currency(0.0),
                'food_raw': 0.0,
                'transport': format_currency(0.0),
                'transport_raw': 0.0,
                'dependents': str(0),
                'dependents_raw': 0,
                'miscellaneous': format_currency(0.0),
                'miscellaneous_raw': 0.0,
                'others': format_currency(0.0),
                'others_raw': 0.0,
                'created_at': 'N/A'
            },
            categories={},
            tips=[],
            insights=[],
            activities=activities,
            tool_title=trans('budget_title', default='Budget Planner'),
            active_tab=active_tab
        ), 500

@budget_bp.route('/summary')
@login_required
@requires_role(['personal', 'admin'])
@limiter.limit("5 per minute")
def summary():
    db = get_mongo_db()
    try:
        log_tool_usage(
            tool_name='budget',
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'unknown'),
            action='summary_view'
        )
        filter_criteria = {} if is_admin() else {'user_id': current_user.id}
        latest_budget = db.budgets.find_one(filter_criteria, sort=[('created_at', -1)])
        if not latest_budget:
            current_app.logger.info(f"No budget found for user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
            return jsonify({
                'totalBudget': format_currency(0.0),
                'user_email': current_user.email if current_user.is_authenticated else ''
            })
        total_budget = float(latest_budget.get('income', 0.0))
        current_app.logger.info(f"Fetched budget summary for user {current_user.id}: {total_budget}", extra={'session_id': session.get('sid', 'unknown')})
        return jsonify({
            'totalBudget': format_currency(total_budget),
            'user_email': latest_budget.get('user_email', current_user.email if current_user.is_authenticated else '')
        })
    except Exception as e:
        current_app.logger.error(f"Error in budget.summary: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        return jsonify({
            'totalBudget': format_currency(0.0),
            'user_email': current_user.email if current_user.is_authenticated else ''
        }), 500

@budget_bp.errorhandler(CSRFError)
def handle_csrf_error(e):
    current_app.logger.error(f"CSRF error on {request.path}: {e.description}", extra={'session_id': session.get('sid', 'unknown')})
    flash(trans('budget_csrf_error', default='Form submission failed due to a missing security token. Please refresh and try again.'), 'danger')
    return redirect(request.url), 403
