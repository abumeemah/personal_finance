from flask import Blueprint, request, session, redirect, url_for, render_template, flash, current_app, jsonify
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect, CSRFError
from wtforms import StringField, DecimalField, SelectField, BooleanField, IntegerField, DateField
from wtforms.validators import DataRequired, NumberRange, Optional, ValidationError
from flask_login import current_user
from mailersend_email import send_email, EMAIL_CONFIG
from datetime import datetime, date, timedelta
from translations import trans
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from utils import get_all_recent_activities, requires_role, is_admin, get_mongo_db, limiter, log_tool_usage, check_ficore_credit_balance
from session_utils import create_anonymous_session
from decimal import Decimal, InvalidOperation
import re

bill_bp = Blueprint('bill', __name__, template_folder='templates/', url_prefix='/bill')

csrf = CSRFProtect()

def custom_login_required(f):
    """Custom login decorator that allows both authenticated users and anonymous sessions."""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.is_authenticated or session.get('is_anonymous', False):
            return f(*args, **kwargs)
        return redirect(url_for('users.login', next=request.url))
    return decorated_function

class BillFormProcessor:
    """Handles proper form data validation and type conversion for bill management."""
    
    @staticmethod
    def clean_currency_input(value):
        """Clean and convert currency input to Decimal."""
        if not value:
            return None
        if isinstance(value, str):
            cleaned = re.sub(r'[^\d.]', '', value.strip())
            parts = cleaned.split('.')
            if len(parts) > 2:
                cleaned = parts[0] + '.' + ''.join(parts[1:])
            if not cleaned or cleaned == '.':
                return None
        else:
            cleaned = str(value)
        try:
            decimal_value = Decimal(cleaned)
            if decimal_value < 0:
                raise ValueError(trans('bill_amount_positive', default="Amount must be positive"))
            if decimal_value > 10000000000:
                raise ValueError(trans('bill_amount_max', default="Input cannot exceed 10 billion"))
            return decimal_value
        except (InvalidOperation, ValueError) as e:
            raise ValueError(trans('bill_amount_invalid', default=f"Invalid amount format: {e}"))

    @staticmethod
    def clean_integer_input(value, min_val=None, max_val=None):
        """Clean and convert integer input (like reminder days)."""
        if not value:
            return None
        if isinstance(value, str):
            cleaned = re.sub(r'[^\d]', '', value.strip())
            if not cleaned:
                return None
        else:
            cleaned = str(value)
        try:
            int_value = int(cleaned)
            if min_val is not None and int_value < min_val:
                raise ValueError(trans('bill_reminder_days_min', default=f"Value must be at least {min_val}"))
            if max_val is not None and int_value > max_val:
                raise ValueError(trans('bill_reminder_days_max', default=f"Value must be at most {max_val}"))
            return int_value
        except ValueError as e:
            raise ValueError(trans('bill_reminder_days_invalid', default=f"Invalid integer format: {e}"))

    @staticmethod
    def validate_date_input(value):
        """Validate and convert date input."""
        if not value:
            return None
        if isinstance(value, str):
            try:
                parsed_date = datetime.strptime(value, '%Y-%m-%d').date()
            except ValueError:
                raise ValueError(trans('bill_due_date_invalid', default="Invalid date format. Use YYYY-MM-DD"))
        elif isinstance(value, datetime):
            parsed_date = value.date()
        elif isinstance(value, date):
            parsed_date = value
        else:
            raise ValueError(trans('bill_due_date_invalid_type', default="Invalid date type"))
        if parsed_date < date.today():
            raise ValueError(trans('bill_due_date_future_validation', default="Due date must be today or in the future"))
        return parsed_date

    @staticmethod
    def process_bill_form_data(form_data):
        """Process and validate all bill form data."""
        cleaned_data = {}
        errors = []
        required_fields = ['bill_name', 'amount', 'due_date', 'frequency', 'category', 'status']
        
        for field in required_fields:
            if field not in form_data or not form_data[field]:
                errors.append(trans(f'bill_{field}_required', default=f"{field.replace('_', ' ').title()} is required"))
        
        if errors:
            raise ValueError("; ".join(errors))
        
        try:
            cleaned_data['bill_name'] = form_data['bill_name'].strip()
            if not cleaned_data['bill_name']:
                errors.append(trans('bill_bill_name_required', default="Bill name cannot be empty"))
            
            cleaned_data['amount'] = BillFormProcessor.clean_currency_input(form_data['amount'])
            if cleaned_data['amount'] is None:
                errors.append(trans('bill_amount_required', default="Valid amount is required"))
            
            cleaned_data['due_date'] = BillFormProcessor.validate_date_input(form_data['due_date'])
            if cleaned_data['due_date'] is None:
                errors.append(trans('bill_due_date_required', default="Valid due date is required"))
            
            valid_frequencies = ['one-time', 'weekly', 'monthly', 'quarterly']
            frequency = form_data['frequency'].strip().lower()
            if frequency not in valid_frequencies:
                errors.append(trans('bill_frequency_invalid', default=f"Frequency must be one of: {', '.join(valid_frequencies)}"))
            cleaned_data['frequency'] = frequency
            
            valid_categories = ['utilities', 'rent', 'data_internet', 'ajo_esusu_adashe', 'food', 'transport',
                               'clothing', 'education', 'healthcare', 'entertainment', 'airtime', 'school_fees',
                               'savings_investments', 'other']
            category = form_data['category'].strip().lower()
            if category not in valid_categories:
                errors.append(trans('bill_category_invalid', default=f"Category must be one of: {', '.join(valid_categories)}"))
            cleaned_data['category'] = category
            
            valid_statuses = ['unpaid', 'paid', 'pending', 'overdue']
            status = form_data['status'].strip().lower()
            if status not in valid_statuses:
                errors.append(trans('bill_status_invalid', default=f"Status must be one of: {', '.join(valid_statuses)}"))
            cleaned_data['status'] = status
            
            cleaned_data['send_email'] = bool(form_data.get('send_email', False))
            
            reminder_days = form_data.get('reminder_days')
            if cleaned_data['send_email'] and reminder_days:
                cleaned_data['reminder_days'] = BillFormProcessor.clean_integer_input(
                    reminder_days, min_val=1, max_val=30
                )
            elif cleaned_data['send_email']:
                cleaned_data['reminder_days'] = 7
            else:
                cleaned_data['reminder_days'] = None
            
        except ValueError as e:
            errors.append(str(e))
        
        if errors:
            raise ValueError("; ".join(errors))
        
        return cleaned_data

def format_currency(value):
    """Format a numeric value with comma separation, no currency symbol."""
    try:
        if isinstance(value, str):
            cleaned_value = BillFormProcessor.clean_currency_input(value)
            numeric_value = float(cleaned_value)
        else:
            numeric_value = float(value)
        formatted = f"{numeric_value:,.2f}"
        current_app.logger.debug(f"Formatted value: input={value}, output={formatted}", extra={'session_id': session.get('sid', 'unknown')})
        return formatted
    except (ValueError, TypeError, InvalidOperation) as e:
        current_app.logger.warning(f"Format Error: input={value}, error={str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        return "0.00"

def calculate_next_due_date(due_date, frequency):
    """Calculate the next due date based on frequency."""
    if frequency == 'weekly':
        return due_date + timedelta(days=7)
    elif frequency == 'monthly':
        return due_date + timedelta(days=30)
    elif frequency == 'quarterly':
        return due_date + timedelta(days=90)
    return due_date

def deduct_ficore_credits(db, user_id, amount, action, bill_id=None):
    """Deduct Ficore Credits from user balance and log the transaction."""
    try:
        user = db.users.find_one({'_id': user_id})
        if not user:
            current_app.logger.error(f"User {user_id} not found for credit deduction", extra={'session_id': session.get('sid', 'unknown')})
            return False
        current_balance = user.get('ficore_credit_balance', 0)
        if current_balance < amount:
            current_app.logger.warning(f"Insufficient credits for user {user_id}: required {amount}, available {current_balance}", extra={'session_id': session.get('sid', 'unknown')})
            return False
        result = db.users.update_one(
            {'_id': user_id},
            {'$inc': {'ficore_credit_balance': -amount}}
        )
        if result.modified_count == 0:
            current_app.logger.error(f"Failed to deduct {amount} credits for user {user_id}", extra={'session_id': session.get('sid', 'unknown')})
            return False
        transaction = {
            '_id': ObjectId(),
            'user_id': user_id,
            'action': action,
            'amount': -amount,
            'bill_id': str(bill_id) if bill_id else None,
            'timestamp': datetime.utcnow(),
            'session_id': session.get('sid', 'unknown'),
            'status': 'completed'
        }
        db.ficore_credit_transactions.insert_one(transaction)
        current_app.logger.info(f"Deducted {amount} Ficore Credits for {action} by user {user_id}", extra={'session_id': session.get('sid', 'unknown')})
        return True
    except Exception as e:
        current_app.logger.error(f"Error deducting {amount} Ficore Credits for {action} by user {user_id}: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        return False

class BillForm(FlaskForm):
    bill_name = StringField(
        trans('bill_bill_name', default='Bill Name'),
        validators=[DataRequired(message=trans('bill_bill_name_required', default='Bill name is required'))]
    )
    amount = DecimalField(
        trans('bill_amount', default='Amount'),
        filters=[lambda x: BillFormProcessor.clean_currency_input(x) if x else None],
        validators=[
            DataRequired(message=trans('bill_amount_required', default='Valid amount is required')),
            NumberRange(min=0, max=10000000000, message=trans('bill_amount_max', default='Input cannot exceed 10 billion'))
        ]
    )
    due_date = DateField(
        trans('bill_due_date', default='Due Date'),
        validators=[
            DataRequired(message=trans('bill_due_date_required', default='Valid due date is required')),
            lambda form, field: BillFormProcessor.validate_date_input(field.data) or None
        ]
    )
    frequency = SelectField(
        trans('bill_frequency', default='Frequency'),
        choices=[
            ('one-time', trans('bill_frequency_one_time', default='One-Time')),
            ('weekly', trans('bill_frequency_weekly', default='Weekly')),
            ('monthly', trans('bill_frequency_monthly', default='Monthly')),
            ('quarterly', trans('bill_frequency_quarterly', default='Quarterly'))
        ],
        default='one-time',
        validators=[DataRequired(message=trans('bill_frequency_required', default='Frequency is required'))]
    )
    category = SelectField(
        trans('general_category', default='Category'),
        choices=[
            ('utilities', trans('bill_category_utilities', default='Utilities')),
            ('rent', trans('bill_category_rent', default='Rent')),
            ('data_internet', trans('bill_category_data_internet', default='Data/Internet')),
            ('ajo_esusu_adashe', trans('bill_category_ajo_esusu_adashe', default='Ajo/Esusu/Adashe')),
            ('food', trans('bill_category_food', default='Food')),
            ('transport', trans('bill_category_transport', default='Transport')),
            ('clothing', trans('bill_category_clothing', default='Clothing')),
            ('education', trans('bill_category_education', default='Education')),
            ('healthcare', trans('bill_category_healthcare', default='Healthcare')),
            ('entertainment', trans('bill_category_entertainment', default='Entertainment')),
            ('airtime', trans('bill_category_airtime', default='Airtime')),
            ('school_fees', trans('bill_category_school_fees', default='School Fees')),
            ('savings_investments', trans('bill_category_savings_investments', default='Savings/Investments')),
            ('other', trans('general_other', default='Other'))
        ],
        default='utilities',
        validators=[DataRequired(message=trans('bill_category_required', default='Category is required'))]
    )
    status = SelectField(
        trans('bill_status', default='Status'),
        choices=[
            ('unpaid', trans('bill_status_unpaid', default='Unpaid')),
            ('paid', trans('bill_status_paid', default='Paid')),
            ('pending', trans('bill_status_pending', default='Pending')),
            ('overdue', trans('bill_status_overdue', default='Overdue'))
        ],
        default='unpaid',
        validators=[DataRequired(message=trans('bill_status_required', default='Status is required'))]
    )
    send_email = BooleanField(
        trans('general_send_email', default='Send Email Reminders'),
        default=False
    )
    reminder_days = IntegerField(
        trans('bill_reminder_days', default='Reminder Days'),
        default=7,
        validators=[
            Optional(),
            NumberRange(min=1, max=30, message=trans('bill_reminder_days_invalid_range', default='Number of days must be between 1 and 30'))
        ]
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lang = session.get('lang', 'en')
        self.bill_name.label.text = trans('bill_bill_name', lang) or 'Bill Name'
        self.amount.label.text = trans('bill_amount', lang) or 'Amount'
        self.due_date.label.text = trans('bill_due_date', lang) or 'Due Date'
        self.frequency.label.text = trans('bill_frequency', lang) or 'Frequency'
        self.category.label.text = trans('general_category', lang) or 'Category'
        self.status.label.text = trans('bill_status', lang) or 'Status'
        self.send_email.label.text = trans('general_send_email', lang) or 'Send Email Reminders'
        self.reminder_days.label.text = trans('bill_reminder_days', lang) or 'Reminder Days'

    def validate(self, extra_validators=None):
        """Custom validation for decimal and integer fields."""
        if not super().validate(extra_validators):
            return False
        if self.send_email.data and not current_user.is_authenticated:
            self.send_email.errors.append(trans('bill_email_required', lang=session.get('lang', 'en')) or 'Email notifications require an authenticated user')
            return False
        if self.due_date.data and self.due_date.data < date.today():
            self.due_date.errors.append(trans('bill_due_date_future_validation', lang=session.get('lang', 'en')) or 'Due date must be today or in the future')
            return False
        return True

class EditBillForm(FlaskForm):
    amount = DecimalField(
        trans('bill_amount', default='Amount'),
        filters=[lambda x: BillFormProcessor.clean_currency_input(x) if x else None],
        validators=[
            DataRequired(message=trans('bill_amount_required', default='Valid amount is required')),
            NumberRange(min=0, max=10000000000, message=trans('bill_amount_max', default='Input cannot exceed 10 billion'))
        ]
    )
    frequency = SelectField(
        trans('bill_frequency', default='Frequency'),
        choices=[
            ('one-time', trans('bill_frequency_one_time', default='One-Time')),
            ('weekly', trans('bill_frequency_weekly', default='Weekly')),
            ('monthly', trans('bill_frequency_monthly', default='Monthly')),
            ('quarterly', trans('bill_frequency_quarterly', default='Quarterly'))
        ],
        default='one-time',
        validators=[DataRequired(message=trans('bill_frequency_required', default='Frequency is required'))]
    )
    category = SelectField(
        trans('general_category', default='Category'),
        choices=[
            ('utilities', trans('bill_category_utilities', default='Utilities')),
            ('rent', trans('bill_category_rent', default='Rent')),
            ('data_internet', trans('bill_category_data_internet', default='Data/Internet')),
            ('ajo_esusu_adashe', trans('bill_category_ajo_esusu_adashe', default='Ajo/Esusu/Adashe')),
            ('food', trans('bill_category_food', default='Food')),
            ('transport', trans('bill_category_transport', default='Transport')),
            ('clothing', trans('bill_category_clothing', default='Clothing')),
            ('education', trans('bill_category_education', default='Education')),
            ('healthcare', trans('bill_category_healthcare', default='Healthcare')),
            ('entertainment', trans('bill_category_entertainment', default='Entertainment')),
            ('airtime', trans('bill_category_airtime', default='Airtime')),
            ('school_fees', trans('bill_category_school_fees', default='School Fees')),
            ('savings_investments', trans('bill_category_savings_investments', default='Savings/Investments')),
            ('other', trans('general_other', default='Other'))
        ],
        default='utilities',
        validators=[DataRequired(message=trans('bill_category_required', default='Category is required'))]
    )
    status = SelectField(
        trans('bill_status', default='Status'),
        choices=[
            ('unpaid', trans('bill_status_unpaid', default='Unpaid')),
            ('paid', trans('bill_status_paid', default='Paid')),
            ('pending', trans('bill_status_pending', default='Pending')),
            ('overdue', trans('bill_status_overdue', default='Overdue'))
        ],
        default='unpaid',
        validators=[DataRequired(message=trans('bill_status_required', default='Status is required'))]
    )
    send_email = BooleanField(
        trans('general_send_email', default='Send Email Reminders'),
        default=False
    )
    reminder_days = IntegerField(
        trans('bill_reminder_days', default='Reminder Days'),
        default=7,
        validators=[
            Optional(),
            NumberRange(min=1, max=30, message=trans('bill_reminder_days_invalid_range', default='Number of days must be between 1 and 30'))
        ]
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lang = session.get('lang', 'en')
        self.amount.label.text = trans('bill_amount', lang) or 'Amount'
        self.frequency.label.text = trans('bill_frequency', lang) or 'Frequency'
        self.category.label.text = trans('general_category', lang) or 'Category'
        self.status.label.text = trans('bill_status', lang) or 'Status'
        self.send_email.label.text = trans('general_send_email', lang) or 'Send Email Reminders'
        self.reminder_days.label.text = trans('bill_reminder_days', lang) or 'Reminder Days'

    def validate(self, extra_validators=None):
        """Custom validation for decimal and integer fields."""
        if not super().validate(extra_validators):
            return False
        if self.send_email.data and not current_user.is_authenticated:
            self.send_email.errors.append(trans('bill_email_required', lang=session.get('lang', 'en')) or 'Email notifications require an authenticated user')
            return False
        return True

@bill_bp.route('/main', methods=['GET', 'POST'])
@custom_login_required
@requires_role(['personal', 'admin'])
@limiter.limit("10 per minute")
def main():
    """Main bill management interface with tabbed layout."""
    if 'sid' not in session:
        create_anonymous_session()
        session['is_anonymous'] = True
        current_app.logger.debug(f"New anonymous session created with sid: {session['sid']}", extra={'session_id': session.get('sid', 'unknown')})
    session.permanent = True
    session.modified = True
    form = BillForm()
    db = get_mongo_db()
    valid_tabs = ['add-bill', 'manage-bills', 'dashboard']
    active_tab = request.args.get('tab', 'add-bill')
    if active_tab not in valid_tabs:
        active_tab = 'add-bill'

    try:
        log_tool_usage(
            tool_name='bill',
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'unknown'),
            action='main_view'
        )
    except Exception as e:
        current_app.logger.error(f"Failed to log tool usage: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('bill_log_error', default='Error logging bill activity. Please try again.'), 'warning')

    try:
        activities = get_all_recent_activities(
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'unknown') if not current_user.is_authenticated else None,
            limit=10
        )
        current_app.logger.debug(f"Fetched {len(activities)} recent activities for {'user ' + str(current_user.id) if current_user.is_authenticated else 'session ' + session.get('sid', 'unknown')}", extra={'session_id': session.get('sid', 'unknown')})
    except Exception as e:
        current_app.logger.error(f"Failed to fetch recent activities: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('bill_activities_load_error', default='Error loading recent activities.'), 'warning')
        activities = []

    tips = [
        trans('bill_tip_pay_early', default='Pay bills early to avoid penalties.'),
        trans('bill_tip_energy_efficient', default='Use energy-efficient appliances to reduce utility bills.'),
        trans('bill_tip_plan_monthly', default='Plan monthly expenses to manage cash flow.'),
        trans('bill_tip_ajo_reminders', default='Set reminders for ajo contributions.'),
        trans('bill_tip_data_topup', default='Schedule data top-ups to avoid service interruptions.')
    ]
    insights = []

    try:
        filter_kwargs = {} if is_admin() else {'user_id': current_user.id} if current_user.is_authenticated else {'session_id': session['sid']}
        bills_collection = db.bills
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'add_bill':
                if current_user.is_authenticated and not is_admin():
                    if not check_ficore_credit_balance(required_amount=1, user_id=current_user.id):
                        current_app.logger.warning(f"Insufficient Ficore Credits for adding bill by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_insufficient_credits', default='Insufficient Ficore Credits to add a bill. Please purchase more credits.'), 'danger')
                        return redirect(url_for('dashboard.index'))
                try:
                    form_data = {
                        'bill_name': request.form.get('bill_name', ''),
                        'amount': request.form.get('amount', ''),
                        'due_date': request.form.get('due_date', ''),
                        'frequency': request.form.get('frequency', ''),
                        'category': request.form.get('category', ''),
                        'status': request.form.get('status', ''),
                        'send_email': request.form.get('send_email', False),
                        'reminder_days': request.form.get('reminder_days', '')
                    }
                    cleaned_data = BillFormProcessor.process_bill_form_data(form_data)
                    if form.validate_on_submit():
                        log_tool_usage(
                            tool_name='bill',
                            db=db,
                            user_id=current_user.id if current_user.is_authenticated else None,
                            session_id=session.get('sid', 'unknown'),
                            action='add_bill'
                        )
                        bill_id = ObjectId()
                        bill_data = {
                            '_id': bill_id,
                            'user_id': current_user.id if current_user.is_authenticated else None,
                            'session_id': session['sid'] if not current_user.is_authenticated else None,
                            'user_email': current_user.email if current_user.is_authenticated else '',
                            'first_name': current_user.get_first_name() if current_user.is_authenticated else '',
                            'bill_name': cleaned_data['bill_name'],
                            'amount': float(cleaned_data['amount']),
                            'due_date': cleaned_data['due_date'].isoformat(),
                            'frequency': cleaned_data['frequency'],
                            'category': cleaned_data['category'],
                            'status': cleaned_data['status'],
                            'send_email': cleaned_data['send_email'],
                            'reminder_days': cleaned_data['reminder_days'],
                            'created_at': datetime.utcnow()
                        }
                        bills_collection.insert_one(bill_data)
                        if current_user.is_authenticated and not is_admin():
                            if not deduct_ficore_credits(db, current_user.id, 1, 'add_bill', bill_id):
                                bills_collection.delete_one({'_id': bill_id})
                                current_app.logger.error(f"Failed to deduct Ficore Credit for adding bill {bill_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                                flash(trans('bill_credit_deduction_failed', default='Failed to deduct Ficore Credit for adding bill.'), 'danger')
                                return redirect(url_for('bill.main', tab='add-bill'))
                        current_app.logger.info(f"Bill {bill_id} added successfully for user {bill_data['user_email']}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_added_success', default='Bill added successfully!'), 'success')
                        if cleaned_data['send_email'] and bill_data['user_email']:
                            try:
                                config = EMAIL_CONFIG['bill_reminder']
                                subject = trans(config['subject_key'], default='Your Bill Reminder')
                                send_email(
                                    app=current_app,
                                    logger=current_app.logger,
                                    to_email=bill_data['user_email'],
                                    subject=subject,
                                    template_name=config['template'],
                                    data={
                                        'first_name': bill_data['first_name'],
                                        'bills': [{
                                            'bill_name': bill_data['bill_name'],
                                            'amount': format_currency(bill_data['amount']),
                                            'due_date': bill_data['due_date'],
                                            'category': bill_data['category'],
                                            'status': bill_data['status']
                                        }],
                                        'cta_url': url_for('bill.main', _external=True),
                                        'unsubscribe_url': url_for('bill.unsubscribe', _external=True)
                                    },
                                    lang=session.get('lang', 'en')
                                )
                                current_app.logger.info(f"Email sent to {bill_data['user_email']}", extra={'session_id': session.get('sid', 'unknown')})
                            except Exception as e:
                                current_app.logger.error(f"Failed to send email: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                                flash(trans('general_email_send_failed', default='Failed to send email.'), 'warning')
                        if cleaned_data['amount'] > 100000:
                            insights.append(trans('bill_insight_large_amount', default='Large bill amount detected. Consider reviewing for accuracy or splitting payments.'))
                        return redirect(url_for('bill.main', tab='dashboard'))
                    else:
                        for field, errors in form.errors.items():
                            for error in errors:
                                flash(trans(error, default=error), 'danger')
                        return redirect(url_for('bill.main', tab='add-bill'))
                except ValueError as e:
                    current_app.logger.error(f"Form validation error: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                    flash(str(e), 'danger')
                    return redirect(url_for('bill.main', tab='add-bill'))
                except DuplicateKeyError:
                    current_app.logger.error(f"Duplicate bill error for session {session['sid']}", extra={'session_id': session.get('sid', 'unknown')})
                    flash(trans('bill_duplicate_error', default='A bill with this name already exists.'), 'danger')
                    return redirect(url_for('bill.main', tab='add-bill'))
                except Exception as e:
                    current_app.logger.error(f"Failed to save bill to MongoDB: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                    flash(trans('bill_storage_error', default='Error saving bill.'), 'danger')
                    return redirect(url_for('bill.main', tab='add-bill'))
            elif action in ['update_bill', 'delete_bill', 'toggle_status']:
                bill_id = request.form.get('bill_id')
                bill = bills_collection.find_one({'_id': ObjectId(bill_id), **filter_kwargs})
                if not bill:
                    current_app.logger.warning(f"Bill {bill_id} not found for update/delete/toggle", extra={'session_id': session.get('sid', 'unknown')})
                    flash(trans('bill_not_found', default='Bill not found.'), 'danger')
                    return redirect(url_for('bill.main', tab='manage-bills'))
                if current_user.is_authenticated and not is_admin():
                    if not check_ficore_credit_balance(required_amount=1, user_id=current_user.id):
                        current_app.logger.warning(f"Insufficient Ficore Credits for {action} on bill {bill_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_insufficient_credits', default='Insufficient Ficore Credits to perform this action. Please purchase more credits.'), 'danger')
                        return redirect(url_for('dashboard.index'))
                if action == 'update_bill':
                    try:
                        form_data = {
                            'bill_name': bill['bill_name'],
                            'amount': request.form.get('amount', ''),
                            'due_date': bill['due_date'],
                            'frequency': request.form.get('frequency', ''),
                            'category': request.form.get('category', ''),
                            'status': request.form.get('status', ''),
                            'send_email': request.form.get('send_email', False),
                            'reminder_days': request.form.get('reminder_days', '')
                        }
                        cleaned_data = BillFormProcessor.process_bill_form_data(form_data)
                        edit_form = EditBillForm(formdata=request.form)
                        if edit_form.validate():
                            update_data = {
                                'amount': float(cleaned_data['amount']),
                                'frequency': cleaned_data['frequency'],
                                'category': cleaned_data['category'],
                                'status': cleaned_data['status'],
                                'send_email': cleaned_data['send_email'],
                                'reminder_days': cleaned_data['reminder_days'],
                                'updated_at': datetime.utcnow()
                            }
                            bills_collection.update_one({'_id': ObjectId(bill_id), **filter_kwargs}, {'$set': update_data})
                            if current_user.is_authenticated and not is_admin():
                                if not deduct_ficore_credits(db, current_user.id, 1, 'update_bill', bill_id):
                                    current_app.logger.error(f"Failed to deduct Ficore Credit for updating bill {bill_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                                    flash(trans('bill_credit_deduction_failed', default='Failed to deduct Ficore Credit for updating bill.'), 'danger')
                                    return redirect(url_for('bill.main', tab='manage-bills'))
                            current_app.logger.info(f"Bill {bill_id} updated successfully", extra={'session_id': session.get('sid', 'unknown')})
                            flash(trans('bill_updated_success', default='Bill updated successfully!'), 'success')
                            if cleaned_data['amount'] > 100000:
                                insights.append(trans('bill_insight_large_amount', default='Large bill amount detected. Consider reviewing for accuracy or splitting payments.'))
                        else:
                            for field, errors in edit_form.errors.items():
                                for error in errors:
                                    flash(trans(error, default=error), 'danger')
                            return redirect(url_for('bill.main', tab='manage-bills'))
                    except ValueError as e:
                        current_app.logger.error(f"Form validation error: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(str(e), 'danger')
                        return redirect(url_for('bill.main', tab='manage-bills'))
                    except Exception as e:
                        current_app.logger.error(f"Failed to update bill {bill_id}: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_update_failed', default='Failed to update bill.'), 'danger')
                    return redirect(url_for('bill.main', tab='manage-bills'))
                elif action == 'delete_bill':
                    try:
                        log_tool_usage(
                            tool_name='bill',
                            db=db,
                            user_id=current_user.id if current_user.is_authenticated else None,
                            session_id=session.get('sid', 'unknown'),
                            action='delete_bill'
                        )
                        bills_collection.delete_one({'_id': ObjectId(bill_id), **filter_kwargs})
                        if current_user.is_authenticated and not is_admin():
                            if not deduct_ficore_credits(db, current_user.id, 1, 'delete_bill', bill_id):
                                current_app.logger.error(f"Failed to deduct Ficore Credit for deleting bill {bill_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                                flash(trans('bill_credit_deduction_failed', default='Failed to deduct Ficore Credit for deleting bill.'), 'danger')
                                return redirect(url_for('bill.main', tab='manage-bills'))
                        current_app.logger.info(f"Bill {bill_id} deleted successfully", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_deleted_success', default='Bill deleted successfully!'), 'success')
                    except Exception as e:
                        current_app.logger.error(f"Failed to delete bill {bill_id}: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_delete_failed', default='Failed to delete bill.'), 'danger')
                    return redirect(url_for('bill.main', tab='manage-bills'))
                elif action == 'toggle_status':
                    new_status = 'paid' if bill['status'] == 'unpaid' else 'unpaid'
                    try:
                        log_tool_usage(
                            tool_name='bill',
                            db=db,
                            user_id=current_user.id if current_user.is_authenticated else None,
                            session_id=session.get('sid', 'unknown'),
                            action='toggle_bill_status'
                        )
                        bills_collection.update_one({'_id': ObjectId(bill_id), **filter_kwargs}, {'$set': {'status': new_status, 'updated_at': datetime.utcnow()}})
                        if current_user.is_authenticated and not is_admin():
                            if not deduct_ficore_credits(db, current_user.id, 1, 'toggle_bill_status', bill_id):
                                current_app.logger.error(f"Failed to deduct Ficore Credit for toggling status of bill {bill_id} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                                flash(trans('bill_credit_deduction_failed', default='Failed to deduct Ficore Credit for toggling bill status.'), 'danger')
                                return redirect(url_for('bill.main', tab='manage-bills'))
                        if new_status == 'paid' and bill['frequency'] != 'one-time':
                            try:
                                due_date = bill['due_date']
                                if isinstance(due_date, str):
                                    due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
                                new_due_date = calculate_next_due_date(due_date, bill['frequency'])
                                new_bill = bill.copy()
                                new_bill['_id'] = ObjectId()
                                new_bill['due_date'] = new_due_date.isoformat()
                                new_bill['status'] = 'unpaid'
                                new_bill['created_at'] = datetime.utcnow()
                                bills_collection.insert_one(new_bill)
                                if current_user.is_authenticated and not is_admin():
                                    if not deduct_ficore_credits(db, current_user.id, 1, 'add_recurring_bill', new_bill['_id']):
                                        bills_collection.delete_one({'_id': new_bill['_id']})
                                        current_app.logger.error(f"Failed to deduct Ficore Credit for adding recurring bill {new_bill['_id']} by user {current_user.id}", extra={'session_id': session.get('sid', 'unknown')})
                                        flash(trans('bill_credit_deduction_failed', default='Failed to deduct Ficore Credit for adding recurring bill.'), 'danger')
                                        return redirect(url_for('bill.main', tab='manage-bills'))
                                current_app.logger.info(f"Recurring bill {new_bill['_id']} created for {bill['bill_name']}", extra={'session_id': session.get('sid', 'unknown')})
                                flash(trans('bill_new_recurring_bill_success', default='New recurring bill created for {bill_name}.').format(bill_name=bill['bill_name']), 'success')
                            except Exception as e:
                                current_app.logger.error(f"Error creating recurring bill: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                                flash(trans('bill_recurring_failed', default='Failed to create recurring bill.'), 'warning')
                        current_app.logger.info(f"Bill {bill_id} status toggled to {new_status}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_status_toggled_success', default='Bill status toggled successfully!'), 'success')
                    except Exception as e:
                        current_app.logger.error(f"Failed to toggle bill status {bill_id}: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                        flash(trans('bill_status_toggle_failed', default='Failed to toggle bill status.'), 'danger')
                    return redirect(url_for('bill.main', tab='manage-bills'))

        bills = bills_collection.find(filter_kwargs).sort('created_at', -1).limit(100)
        bills_data = []
        edit_forms = {}
        paid_count = unpaid_count = overdue_count = pending_count = 0
        total_paid = total_unpaid = total_overdue = total_bills = 0.0
        categories = {}
        due_today = []
        due_week = []
        due_month = []
        upcoming_bills = []
        today = date.today()
        for bill in bills:
            bill_id = str(bill['_id'])
            try:
                due_date = bill['due_date']
                if isinstance(due_date, str):
                    due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
                elif not isinstance(due_date, date):
                    current_app.logger.warning(f"Invalid due_date for bill {bill_id}: {bill.get('due_date')}", extra={'session_id': session.get('sid', 'unknown')})
                    due_date = today
            except (ValueError, TypeError) as e:
                current_app.logger.warning(f"Invalid due_date for bill {bill_id}: {bill.get('due_date')}, error: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                due_date = today
            bill_data = {
                'id': bill_id,
                'bill_name': bill.get('bill_name', ''),
                'amount': format_currency(float(bill.get('amount', 0.0))),
                'amount_raw': float(bill.get('amount', 0.0)),
                'due_date': due_date,
                'due_date_formatted': due_date.strftime('%Y-%m-%d'),
                'frequency': bill.get('frequency', 'one-time'),
                'category': bill.get('category', 'other'),
                'status': bill.get('status', 'unpaid'),
                'send_email': bill.get('send_email', False),
                'reminder_days': bill.get('reminder_days', None),
                'created_at': bill.get('created_at', datetime.utcnow()).strftime('%Y-%m-%d')
            }
            edit_form = EditBillForm(data={
                'amount': bill_data['amount_raw'],
                'frequency': bill_data['frequency'],
                'category': bill_data['category'],
                'status': bill_data['status'],
                'send_email': bill_data['send_email'],
                'reminder_days': bill_data['reminder_days']
            })
            bills_data.append((bill_id, bill_data, edit_form))
            edit_forms[bill_id] = edit_form
            try:
                bill_amount = float(bill_data['amount_raw'])
                total_bills += bill_amount
                cat = bill_data['category']
                categories[cat] = categories.get(cat, 0) + bill_amount
                if bill_data['status'] == 'paid':
                    paid_count += 1
                    total_paid += bill_amount
                elif bill_data['status'] == 'unpaid':
                    unpaid_count += 1
                    total_unpaid += bill_amount
                elif bill_data['status'] == 'overdue':
                    overdue_count += 1
                    total_overdue += bill_amount
                elif bill_data['status'] == 'pending':
                    pending_count += 1
                bill_due_date = bill_data['due_date']
                if bill_due_date == today:
                    due_today.append((bill_id, bill_data, edit_form))
                if today <= bill_due_date <= (today + timedelta(days=7)):
                    due_week.append((bill_id, bill_data, edit_form))
                if today <= bill_due_date <= (today + timedelta(days=30)):
                    due_month.append((bill_id, bill_data, edit_form))
                if today < bill_due_date:
                    upcoming_bills.append((bill_id, bill_data, edit_form))
                # Add insights based on bill data
                if bill_due_date <= today and bill_data['status'] not in ['paid', 'pending']:
                    insights.append(trans('bill_insight_overdue', default=f"Bill '{bill_data['bill_name']}' is overdue. Consider paying it soon."))
                elif bill_due_date <= (today + timedelta(days=7)) and bill_data['status'] == 'unpaid':
                    insights.append(trans('bill_insight_due_soon', default=f"Bill '{bill_data['bill_name']}' is due soon. Plan your payment."))
            except (ValueError, TypeError) as e:
                current_app.logger.warning(f"Invalid amount for bill {bill_id}: {bill.get('amount')}, error: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
                continue
        categories = {trans(f'bill_category_{k}', default=k.replace('_', ' ').title()): v for k, v in categories.items() if v > 0}
        if total_overdue > total_bills * 0.3:
            insights.append(trans('bill_insight_high_overdue', default='Overdue bills exceed 30% of total bills. Prioritize clearing overdue amounts.'))
        return render_template(
            'bill_main.html',
            form=form,
            bills_data=bills_data,
            edit_forms=edit_forms,
            paid_count=paid_count,
            unpaid_count=unpaid_count,
            overdue_count=overdue_count,
            pending_count=pending_count,
            total_paid=format_currency(total_paid),
            total_unpaid=format_currency(total_unpaid),
            total_overdue=format_currency(total_overdue),
            total_bills=format_currency(total_bills),
            categories=categories,
            due_today=due_today,
            due_week=due_week,
            due_month=due_month,
            upcoming_bills=upcoming_bills,
            tips=tips,
            insights=insights,
            activities=activities,
            tool_title=trans('bill_title', default='Bill Manager'),
            active_tab=active_tab
        )
    except Exception as e:
        current_app.logger.error(f"Error in bill.main: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('bill_dashboard_load_error', default='Error loading bill dashboard.'), 'danger')
        return render_template(
            'bill_main.html',
            form=form,
            bills_data=[],
            edit_forms={},
            paid_count=0,
            unpaid_count=0,
            overdue_count=0,
            pending_count=0,
            total_paid=format_currency(0.0),
            total_unpaid=format_currency(0.0),
            total_overdue=format_currency(0.0),
            total_bills=format_currency(0.0),
            categories={},
            due_today=[],
            due_week=[],
            due_month=[],
            upcoming_bills=[],
            tips=tips,
            insights=[],
            activities=activities,
            tool_title=trans('bill_title', default='Bill Manager'),
            active_tab=active_tab
        ), 500

@bill_bp.route('/summary')
@custom_login_required
@requires_role(['personal', 'admin'])
@limiter.limit("5 per minute")
def summary():
    """Return summary of upcoming bills for the current user."""
    db = get_mongo_db()
    try:
        log_tool_usage(
            tool_name='bill',
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'unknown'),
            action='summary_view'
        )
        filter_kwargs = {} if is_admin() else {'user_id': current_user.id} if current_user.is_authenticated else {'session_id': session['sid']}
        bills_collection = db.bills
        today = date.today()
        pipeline = [
            {'$match': {**filter_kwargs, 'status': {'$ne': 'paid'}, 'due_date': {'$gte': today.isoformat()}}},
            {'$group': {'_id': None, 'totalUpcomingBills': {'$sum': '$amount'}}}
        ]
        result = list(bills_collection.aggregate(pipeline))
        total_upcoming_bills = result[0]['totalUpcomingBills'] if result else 0.0
        current_app.logger.info(f"Fetched bill summary for {'user ' + str(current_user.id) if current_user.is_authenticated else 'session ' + session.get('sid', 'unknown')}: {total_upcoming_bills}", extra={'session_id': session.get('sid', 'unknown')})
        return jsonify({'totalUpcomingBills': format_currency(total_upcoming_bills)})
    except Exception as e:
        current_app.logger.error(f"Error in bill.summary: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        return jsonify({'totalUpcomingBills': format_currency(0.0)}), 500

@bill_bp.route('/unsubscribe', methods=['GET', 'POST'])
@custom_login_required
@requires_role(['personal', 'admin'])
@limiter.limit("5 per minute")
def unsubscribe():
    """Unsubscribe user from bill email notifications."""
    db = get_mongo_db()
    if 'sid' not in session:
        create_anonymous_session()
        session['is_anonymous'] = True
        current_app.logger.debug(f"New anonymous session created with sid: {session['sid']}", extra={'session_id': session.get('sid', 'unknown')})
    session.permanent = True
    session.modified = True
    try:
        log_tool_usage(
            tool_name='bill',
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'unknown'),
            action='unsubscribe'
        )
        filter_kwargs = {'user_email': current_user.email if current_user.is_authenticated else ''}
        if current_user.is_authenticated and not is_admin():
            filter_kwargs['user_id'] = current_user.id
        bills_collection = db.bills
        result = bills_collection.update_many(filter_kwargs, {'$set': {'send_email': False}})
        if result.modified_count > 0:
            current_app.logger.info(f"Successfully unsubscribed email {current_user.email if current_user.is_authenticated else 'anonymous'}", extra={'session_id': session.get('sid', 'unknown')})
            flash(trans('bill_unsubscribe_success', default='Successfully unsubscribed from bill emails.'), 'success')
        else:
            current_app.logger.warning(f"No records updated for email {current_user.email if current_user.is_authenticated else 'anonymous'} during unsubscribe", extra={'session_id': session.get('sid', 'unknown')})
            flash(trans('bill_unsubscribe_failed', default='No matching email found or already unsubscribed.'), 'danger')
        return redirect(url_for('bill.main', tab='manage-bills'))
    except Exception as e:
        current_app.logger.error(f"Error in bill.unsubscribe: {str(e)}", extra={'session_id': session.get('sid', 'unknown')})
        flash(trans('bill_unsubscribe_error', default='Error processing unsubscribe request.'), 'danger')
        return redirect(url_for('bill.main', tab='manage-bills'))

@bill_bp.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Handle CSRF errors with user-friendly message."""
    current_app.logger.error(f"CSRF error on {request.path}: {e.description}", extra={'session_id': session.get('sid', 'unknown')})
    flash(trans('bill_csrf_error', default='Form submission failed due to a missing security token. Please refresh and try again.'), 'danger')
    return redirect(url_for('bill.main', tab='add-bill')), 403
