from flask import Blueprint, session, request, render_template, redirect, url_for, flash, jsonify, current_app, Response
from flask_login import login_required, current_user
from translations import trans
import utils
from bson import ObjectId
from datetime import datetime, date
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.units import inch
from io import BytesIO, StringIO
from flask_wtf import FlaskForm
from wtforms import DateField, StringField, SubmitField, SelectField
from wtforms.validators import Optional
import csv
import logging
from helpers.branding_helpers import draw_ficore_pdf_header, ficore_csv_header

logger = logging.getLogger(__name__)

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')

class ReportForm(FlaskForm):
    start_date = DateField(trans('reports_start_date', default='Start Date'), validators=[Optional()])
    end_date = DateField(trans('reports_end_date', default='End Date'), validators=[Optional()])
    submit = SubmitField(trans('reports_generate_report', default='Generate Report'))

class CustomerReportForm(FlaskForm):
    role = SelectField('User Role', choices=[('', 'All'), ('personal', 'Personal'), ('trader', 'Trader'), ('agent', 'Agent'), ('admin', 'Admin')], validators=[Optional()])
    format = SelectField('Format', choices=[('html', 'HTML'), ('pdf', 'PDF'), ('csv', 'CSV')], default='html')
    submit = SubmitField('Generate Report')

class DebtorsCreditorsReportForm(FlaskForm):
    start_date = DateField(trans('reports_start_date', default='Start Date'), validators=[Optional()])
    end_date = DateField(trans('reports_end_date', default='End Date'), validators=[Optional()])
    record_type = SelectField('Record Type', choices=[('', 'All'), ('debtor', 'Debtor'), ('creditor', 'Creditor')], validators=[Optional()])
    submit = SubmitField(trans('reports_generate_report', default='Generate Report'))

class TaxObligationsReportForm(FlaskForm):
    start_date = DateField(trans('reports_start_date', default='Start Date'), validators=[Optional()])
    end_date = DateField(trans('reports_end_date', default='End Date'), validators=[Optional()])
    status = SelectField('Status', choices=[('', 'All'), ('pending', 'Pending'), ('paid', 'Paid'), ('overdue', 'Overdue')], validators=[Optional()])
    submit = SubmitField(trans('reports_generate_report', default='Generate Report'))

class BudgetPerformanceReportForm(FlaskForm):
    start_date = DateField(trans('reports_start_date', default='Start Date'), validators=[Optional()])
    end_date = DateField(trans('reports_end_date', default='End Date'), validators=[Optional()])
    submit = SubmitField(trans('reports_generate_report', default='Generate Report'))

class ShoppingReportForm(FlaskForm):
    start_date = DateField(trans('reports_start_date', default='Start Date'), validators=[Optional()])
    end_date = DateField(trans('reports_end_date', default='End Date'), validators=[Optional()])
    format = SelectField('Format', choices=[('html', 'HTML'), ('pdf', 'PDF'), ('csv', 'CSV')], default='html')
    submit = SubmitField(trans('reports_generate_report', default='Generate Report'))

def to_dict_budget(record):
    if not record:
        return {'surplus_deficit': None, 'savings_goal': None}
    return {
        'id': str(record.get('_id', '')),
        'income': record.get('income', 0),
        'fixed_expenses': record.get('fixed_expenses', 0),
        'variable_expenses': record.get('variable_expenses', 0),
        'savings_goal': record.get('savings_goal', 0),
        'surplus_deficit': record.get('surplus_deficit', 0),
        'housing': record.get('housing', 0),
        'food': record.get('food', 0),
        'transport': record.get('transport', 0),
        'dependents': record.get('dependents', 0),
        'miscellaneous': record.get('miscellaneous', 0),
        'others': record.get('others', 0),
        'created_at': utils.format_date(record.get('created_at'), format_type='iso')
    }

def to_dict_bill(record):
    if not record:
        return {'amount': None, 'status': None}
    return {
        'id': str(record.get('_id', '')),
        'bill_name': record.get('bill_name', ''),
        'amount': record.get('amount', 0),
        'due_date': utils.format_date(record.get('due_date'), format_type='iso'),
        'frequency': record.get('frequency', ''),
        'category': record.get('category', ''),
        'status': record.get('status', ''),
        'send_email': record.get('send_email', False),
        'reminder_days': record.get('reminder_days'),
        'user_email': record.get('user_email', ''),
        'first_name': record.get('first_name', '')
    }

def to_dict_tax_reminder(record):
    if not record:
        return {'tax_type': None, 'amount': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'tax_type': record.get('tax_type', ''),
        'due_date': utils.format_date(record.get('due_date'), format_type='iso'),
        'amount': record.get('amount', 0),
        'status': record.get('status', ''),
        'created_at': utils.format_date(record.get('created_at'), format_type='iso'),
        'notification_id': record.get('notification_id'),
        'sent_at': utils.format_date(record.get('sent_at'), format_type='iso') if record.get('sent_at') else None,
        'payment_location_id': record.get('payment_location_id')
    }

def to_dict_record(record):
    if not record:
        return {'name': None, 'amount_owed': None}
    try:
        created_at = utils.format_date(record.get('created_at'), format_type='iso') if record.get('created_at') else None
        updated_at = utils.format_date(record.get('updated_at'), format_type='iso') if record.get('updated_at') else None
    except Exception as e:
        logger.error(f"Error formatting dates in to_dict_record: {str(e)}", exc_info=True)
        created_at = None
        updated_at = None
    return {
        'id': str(record.get('_id', '')),
        'user_id': str(record.get('user_id', '')),
        'type': record.get('type', ''),
        'name': record.get('name', ''),
        'contact': record.get('contact', ''),
        'amount_owed': record.get('amount_owed', 0),
        'description': record.get('description', ''),
        'reminder_count': record.get('reminder_count', 0),
        'created_at': created_at,
        'updated_at': updated_at
    }

def to_dict_cashflow(record):
    if not record:
        return {'party_name': None, 'amount': None}
    result = {
        'id': str(record.get('_id', '')),
        'user_id': str(record.get('user_id', '')),
        'type': record.get('type', ''),
        'party_name': record.get('party_name', ''),
        'amount': record.get('amount', 0),
        'method': record.get('method', ''),
        'created_at': utils.format_date(record.get('created_at'), format_type='iso'),
        'updated_at': utils.format_date(record.get('updated_at'), format_type='iso') if record.get('updated_at') else None
    }
    for key, value in result.items():
        if isinstance(value, date) and not isinstance(value, datetime):
            result[key] = datetime.combine(value, datetime.min.time())
    return result

def to_dict_shopping_list(record):
    if not record:
        return {'name': None, 'budget': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': str(record.get('user_id', '')),
        'name': record.get('name', ''),
        'budget': record.get('budget', 0),
        'total_spent': record.get('total_spent', 0),
        'created_at': utils.format_date(record.get('created_at'), format_type='iso'),
        'updated_at': utils.format_date(record.get('updated_at'), format_type='iso') if record.get('updated_at') else None,
        'collaborators': record.get('collaborators', [])
    }

def to_dict_shopping_item(record):
    if not record:
        return {'name': None, 'price': None}
    return {
        'id': str(record.get('_id', '')),
        'list_id': str(record.get('list_id', '')),
        'name': str(record.get('name', '')),
        'quantity': record.get('quantity', 0),
        'price': record.get('price', 0),
        'status': record.get('status', ''),
        'category': record.get('category', ''),
        'store': record.get('store', ''),
        'created_at': utils.format_date(record.get('created_at'), format_type='iso'),
        'updated_at': utils.format_date(record.get('updated_at'), format_type='iso') if record.get('updated_at') else None
    }

def to_dict_shopping_suggestion(record):
    if not record:
        return {'name': None, 'price': None}
    return {
        'id': str(record.get('_id', '')),
        'list_id': str(record.get('list_id', '')),
        'user_id': str(record.get('user_id', '')),
        'name': str(record.get('name', '')),
        'quantity': record.get('quantity', 0),
        'price': record.get('price', 0),
        'category': record.get('category', ''),
        'status': record.get('status', ''),
        'created_at': utils.format_date(record.get('created_at'), format_type='iso'),
        'updated_at': utils.format_date(record.get('updated_at'), format_type='iso') if record.get('updated_at') else None
    }

@reports_bp.route('/')
@login_required
@utils.requires_role(['personal', 'trader'])
def index():
    """Display report selection page."""
    try:
        return render_template(
            'reports/index.html',
            title=utils.trans('reports_index', default='Reports', lang=session.get('lang', 'en'))
        )
    except Exception as e:
        logger.error(f"Error loading reports index for user {current_user.id}: {str(e)}", exc_info=True)
        flash(trans('reports_load_error', default='An error occurred'), 'danger')
        return redirect(url_for('dashboard.index'))

@reports_bp.route('/profit_loss', methods=['GET', 'POST'])
@login_required
@utils.requires_role('trader')
def profit_loss():
    """Generate profit/loss report with filters."""
    form = ReportForm()
    if not utils.is_admin() and not utils.check_ficore_credit_balance(1):
        flash(trans('debtors_insufficient_credits', default='Insufficient credits to generate a report. Request more credits.'), 'danger')
        return redirect(url_for('credits.request_credits'))
    cashflows = []
    query = {} if utils.is_admin() else {'user_id': str(current_user.id)}
    if form.validate_on_submit():
        try:
            db = utils.get_mongo_db()
            if form.start_date.data:
                start_datetime = datetime.combine(form.start_date.data, datetime.min.time())
                query['created_at'] = {'$gte': start_datetime}
            if form.end_date.data:
                end_datetime = datetime.combine(form.end_date.data, datetime.max.time())
                query['created_at'] = query.get('created_at', {}) | {'$lte': end_datetime}
            cashflows = [to_dict_cashflow(cf) for cf in db.cashflows.find(query).sort('created_at', -1)]
            output_format = request.form.get('format', 'html')
            if output_format == 'pdf':
                return generate_profit_loss_pdf(cashflows)
            elif output_format == 'csv':
                return generate_profit_loss_csv(cashflows)
            if not utils.is_admin():
                user_query = utils.get_user_query(str(current_user.id))
                db.users.update_one(
                    user_query,
                    {'$inc': {'ficore_credit_balance': -1}}
                )
                db.ficore_ficore_credit_transactions.insert_one({
                    'user_id': str(current_user.id),
                    'amount': -1,
                    'type': 'spend',
                    'date': datetime.utcnow(),
                    'ref': 'Profit/Loss report generation (Ficore Credits)'
                })
        except Exception as e:
            logger.error(f"Error generating profit/loss report for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    else:
        try:
            db = utils.get_mongo_db()
            cashflows = [to_dict_cashflow(cf) for cf in db.cashflows.find(query).sort('created_at', -1)]
        except Exception as e:
            logger.error(f"Error fetching cashflows for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    return render_template(
        'reports/profit_loss.html',
        form=form,
        cashflows=cashflows,
        title=utils.trans('reports_profit_loss', default='Profit/Loss Report', lang=session.get('lang', 'en'))
    )

@reports_bp.route('/debtors_creditors', methods=['GET', 'POST'])
@login_required
@utils.requires_role('trader')
def debtors_creditors():
    """Generate debtors/creditors report with filters."""
    form = DebtorsCreditorsReportForm()
    if not utils.is_admin() and not utils.check_ficore_credit_balance(1):
        flash(trans('debtors_insufficient_credits', default='Insufficient credits to generate a report. Request more credits.'), 'danger')
        return redirect(url_for('credits.request_credits'))
    records = []
    query = {} if utils.is_admin() else {'user_id': str(current_user.id)}
    if form.validate_on_submit():
        try:
            db = utils.get_mongo_db()
            if form.start_date.data:
                start_datetime = datetime.combine(form.start_date.data, datetime.min.time())
                query['created_at'] = {'$gte': start_datetime}
            if form.end_date.data:
                end_datetime = datetime.combine(form.end_date.data, datetime.max.time())
                query['created_at'] = query.get('created_at', {}) | {'$lte': end_datetime}
            if form.record_type.data:
                query['type'] = form.record_type.data
            records = [to_dict_record(r) for r in db.records.find(query).sort('created_at', -1)]
            output_format = request.form.get('format', 'html')
            if output_format == 'pdf':
                return generate_debtors_creditors_pdf(records)
            elif output_format == 'csv':
                return generate_debtors_creditors_csv(records)
            if not utils.is_admin():
                user_query = utils.get_user_query(str(current_user.id))
                db.users.update_one(
                    user_query,
                    {'$inc': {'ficore_credit_balance': -1}}
                )
                db.ficore_ficore_credit_transactions.insert_one({
                    'user_id': str(current_user.id),
                    'amount': -1,
                    'type': 'spend',
                    'date': datetime.utcnow(),
                    'ref': 'Debtors/Creditors report generation (Ficore Credits)'
                })
        except Exception as e:
            logger.error(f"Error generating debtors/creditors report for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    else:
        try:
            db = utils.get_mongo_db()
            records = [to_dict_record(r) for r in db.records.find(query).sort('created_at', -1)]
        except Exception as e:
            logger.error(f"Error fetching records for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    return render_template(
        'reports/debtors_creditors.html',
        form=form,
        records=records,
        title=utils.trans('reports_debtors_creditors', default='Debtors/Creditors Report', lang=session.get('lang', 'en'))
    )

@reports_bp.route('/tax_obligations', methods=['GET', 'POST'])
@login_required
@utils.requires_role('trader')
def tax_obligations():
    """Generate tax obligations report with filters."""
    form = TaxObligationsReportForm()
    if not utils.is_admin() and not utils.check_ficore_credit_balance(1):
        flash(trans('debtors_insufficient_credits', default='Insufficient credits to generate a report. Request more credits.'), 'danger')
        return redirect(url_for('credits.request_credits'))
    tax_reminders = []
    query = {} if utils.is_admin() else {'user_id': str(current_user.id)}
    if form.validate_on_submit():
        try:
            db = utils.get_mongo_db()
            if form.start_date.data:
                start_datetime = datetime.combine(form.start_date.data, datetime.min.time())
                query['due_date'] = {'$gte': start_datetime}
            if form.end_date.data:
                end_datetime = datetime.combine(form.end_date.data, datetime.max.time())
                query['due_date'] = query.get('due_date', {}) | {'$lte': end_datetime}
            if form.status.data:
                query['status'] = form.status.data
            tax_reminders = [to_dict_tax_reminder(tr) for tr in db.tax_reminders.find(query).sort('due_date', 1)]
            output_format = request.form.get('format', 'html')
            if output_format == 'pdf':
                return generate_tax_obligations_pdf(tax_reminders)
            elif output_format == 'csv':
                return generate_tax_obligations_csv(tax_reminders)
            if not utils.is_admin():
                user_query = utils.get_user_query(str(current_user.id))
                db.users.update_one(
                    user_query,
                    {'$inc': {'ficore_credit_balance': -1}}
                )
                db.ficore_ficore_credit_transactions.insert_one({
                    'user_id': str(current_user.id),
                    'amount': -1,
                    'type': 'spend',
                    'date': datetime.utcnow(),
                    'ref': 'Tax Obligations report generation (Ficore Credits)'
                })
        except Exception as e:
            logger.error(f"Error generating tax obligations report for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    else:
        try:
            db = utils.get_mongo_db()
            tax_reminders = [to_dict_tax_reminder(tr) for tr in db.tax_reminders.find(query).sort('due_date', 1)]
        except Exception as e:
            logger.error(f"Error fetching tax reminders for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    return render_template(
        'reports/tax_obligations.html',
        form=form,
        tax_reminders=tax_reminders,
        title=utils.trans('reports_tax_obligations', default='Tax Obligations Report', lang=session.get('lang', 'en'))
    )

@reports_bp.route('/budget_performance', methods=['GET', 'POST'])
@login_required
@utils.requires_role('personal')
def budget_performance():
    """Generate budget performance report with filters."""
    form = BudgetPerformanceReportForm()
    if not utils.is_admin() and not utils.check_ficore_credit_balance(1):
        flash(trans('debtors_insufficient_credits', default='Insufficient credits to generate a report. Request more credits.'), 'danger')
        return redirect(url_for('credits.request_credits'))
    budget_data = []
    query = {} if utils.is_admin() else {'user_id': str(current_user.id)}
    if form.validate_on_submit():
        try:
            db = utils.get_mongo_db()
            budget_query = query.copy()
            cashflow_query = query.copy()
            if form.start_date.data:
                start_datetime = datetime.combine(form.start_date.data, datetime.min.time())
                budget_query['created_at'] = {'$gte': start_datetime}
                cashflow_query['created_at'] = {'$gte': start_datetime}
            if form.end_date.data:
                end_datetime = datetime.combine(form.end_date.data, datetime.max.time())
                budget_query['created_at'] = budget_query.get('created_at', {}) | {'$lte': end_datetime}
                cashflow_query['created_at'] = cashflow_query.get('created_at', {}) | {'$lte': end_datetime}
            budgets = list(db.budgets.find(budget_query).sort('created_at', -1))
            cashflows = [to_dict_cashflow(cf) for cf in db.cashflows.find(cashflow_query).sort('created_at', -1)]
            for budget in budgets:
                budget_dict = to_dict_budget(budget)
                actual_income = sum(cf['amount'] for cf in cashflows if cf['type'] == 'receipt')
                actual_expenses = sum(cf['amount'] for cf in cashflows if cf['type'] == 'payment')
                budget_dict['actual_income'] = actual_income
                budget_dict['actual_expenses'] = actual_expenses
                budget_dict['income_variance'] = actual_income - budget_dict['income']
                budget_dict['expense_variance'] = actual_expenses - (budget_dict['fixed_expenses'] + budget_dict['variable_expenses'])
                budget_data.append(budget_dict)
            output_format = request.form.get('format', 'html')
            if output_format == 'pdf':
                return generate_budget_performance_pdf(budget_data)
            elif output_format == 'csv':
                return generate_budget_performance_csv(budget_data)
            if not utils.is_admin():
                user_query = utils.get_user_query(str(current_user.id))
                db.users.update_one(
                    user_query,
                    {'$inc': {'ficore_credit_balance': -1}}
                )
                db.ficore_ficore_credit_transactions.insert_one({
                    'user_id': str(current_user.id),
                    'amount': -1,
                    'type': 'spend',
                    'date': datetime.utcnow(),
                    'ref': 'Budget Performance report generation (Ficore Credits)'
                })
        except Exception as e:
            logger.error(f"Error generating budget performance report for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    else:
        try:
            db = utils.get_mongo_db()
            budgets = list(db.budgets.find(query).sort('created_at', -1))
            cashflows = [to_dict_cashflow(cf) for cf in db.cashflows.find(query).sort('created_at', -1)]
            for budget in budgets:
                budget_dict = to_dict_budget(budget)
                actual_income = sum(cf['amount'] for cf in cashflows if cf['type'] == 'receipt')
                actual_expenses = sum(cf['amount'] for cf in cashflows if cf['type'] == 'payment')
                budget_dict['actual_income'] = actual_income
                budget_dict['actual_expenses'] = actual_expenses
                budget_dict['income_variance'] = actual_income - budget_dict['income']
                budget_dict['expense_variance'] = actual_expenses - (budget_dict['fixed_expenses'] + budget_dict['variable_expenses'])
                budget_data.append(budget_dict)
        except Exception as e:
            logger.error(f"Error fetching budget data for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    return render_template(
        'reports/budget_performance.html',
        form=form,
        budget_data=budget_data,
        title=utils.trans('reports_budget_performance', default='Budget Performance Report', lang=session.get('lang', 'en'))
    )

@reports_bp.route('/shopping', methods=['GET', 'POST'])
@login_required
@utils.requires_role('personal')
def shopping_report():
    """Generate shopping report with filters for personal users."""
    form = ShoppingReportForm()
    if not utils.is_admin() and not utils.check_ficore_credit_balance(1):
        flash(trans('debtors_insufficient_credits', default='Insufficient credits to generate a report. Request more credits.'), 'danger')
        return redirect(url_for('credits.request_credits'))
    shopping_data = {'lists': [], 'items': [], 'suggestions': []}
    query = {} if utils.is_admin() else {'user_id': str(current_user.id)}
    if form.validate_on_submit():
        try:
            db = utils.get_mongo_db()
            list_query = query.copy()
            item_query = query.copy()
            suggestion_query = query.copy()
            if form.start_date.data:
                start_datetime = datetime.combine(form.start_date.data, datetime.min.time())
                list_query['created_at'] = {'$gte': start_datetime}
                item_query['created_at'] = {'$gte': start_datetime}
                suggestion_query['created_at'] = {'$gte': start_datetime}
            if form.end_date.data:
                end_datetime = datetime.combine(form.end_date.data, datetime.max.time())
                list_query['created_at'] = list_query.get('created_at', {}) | {'$lte': end_datetime}
                item_query['created_at'] = item_query.get('created_at', {}) | {'$lte': end_datetime}
                suggestion_query['created_at'] = suggestion_query.get('created_at', {}) | {'$lte': end_datetime}
            lists = [to_dict_shopping_list(lst) for lst in db.shopping_lists.find(list_query).sort('created_at', -1)]
            items = [to_dict_shopping_item(item) for item in db.shopping_items.find(item_query).sort('created_at', -1)]
            suggestions = [to_dict_shopping_suggestion(sug) for sug in db.shopping_suggestions.find(suggestion_query).sort('created_at', -1)]
            shopping_data = {'lists': lists, 'items': items, 'suggestions': suggestions}
            output_format = form.format.data
            if output_format == 'pdf':
                return generate_shopping_report_pdf(shopping_data)
            elif output_format == 'csv':
                return generate_shopping_report_csv(shopping_data)
            if not utils.is_admin():
                user_query = utils.get_user_query(str(current_user.id))
                db.users.update_one(
                    user_query,
                    {'$inc': {'ficore_credit_balance': -1}}
                )
                db.ficore_ficore_credit_transactions.insert_one({
                    'user_id': str(current_user.id),
                    'amount': -1,
                    'type': 'spend',
                    'date': datetime.utcnow(),
                    'ref': 'Shopping Report generation (Ficore Credits)'
                })
        except Exception as e:
            logger.error(f"Error generating shopping report for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    else:
        try:
            db = utils.get_mongo_db()
            lists = [to_dict_shopping_list(lst) for lst in db.shopping_lists.find(query).sort('created_at', -1)]
            items = [to_dict_shopping_item(item) for item in db.shopping_items.find(query).sort('created_at', -1)]
            suggestions = [to_dict_shopping_suggestion(sug) for sug in db.shopping_suggestions.find(query).sort('created_at', -1)]
            shopping_data = {'lists': lists, 'items': items, 'suggestions': suggestions}
        except Exception as e:
            logger.error(f"Error fetching shopping data for user {current_user.id}: {str(e)}", exc_info=True)
            flash(trans('reports_generation_error', default='An error occurred'), 'danger')
    return render_template(
        'reports/shopping.html',
        form=form,
        shopping_data=shopping_data,
        title=utils.trans('reports_shopping', default='Shopping Report', lang=session.get('lang', 'en'))
    )

@reports_bp.route('/admin/customer-reports', methods=['GET', 'POST'])
@login_required
@utils.requires_role('admin')
def customer_reports():
    """Generate customer reports for admin."""
    form = CustomerReportForm()
    if form.validate_on_submit():
        role = form.role.data if form.role.data else None
        report_format = form.format.data
        try:
            db = utils.get_mongo_db()
            pipeline = [
                {'$match': {'role': role}} if role else {},
                {'$lookup': {
                    'from': 'budgets',
                    'let': {'user_id': '$_id'},
                    'pipeline': [
                        {'$match': {'$expr': {'$eq': ['$user_id', '$$user_id']}}},
                        {'$sort': {'created_at': -1}},
                        {'$limit': 1}
                    ],
                    'as': 'latest_budget'
                }},
                {'$lookup': {
                    'from': 'bills',
                    'let': {'user_id': '$_id'},
                    'pipeline': [
                        {'$match': {'$expr': {'$eq': ['$user_id', '$$user_id']}}},
                        {'$group': {
                            '_id': '$status',
                            'count': {'$sum': 1}
                        }}
                    ],
                    'as': 'bill_status_counts'
                }},
                {'$lookup': {
                    'from': 'learning_materials',
                    'let': {'user_id': '$_id'},
                    'pipeline': [
                        {'$match': {'$expr': {'$eq': ['$user_id', '$$user_id']}}},
                        {'$group': {
                            '_id': None,
                            'total_lessons_completed': {'$sum': {'$size': '$lessons_completed'}}
                        }}
                    ],
                    'as': 'learning_progress'
                }},
                {'$lookup': {
                    'from': 'tax_reminders',
                    'let': {'user_id': '$_id'},
                    'pipeline': [
                        {'$match': {'$expr': {'$eq': ['$user_id', '$$user_id']}, 'due_date': {'$gte': datetime.utcnow()}}},
                        {'$sort': {'due_date': 1}},
                        {'$limit': 1}
                    ],
                    'as': 'next_tax_reminder'
                }},
            ]
            users = list(db.users.aggregate(pipeline))
            report_data = []
            for user in users:
                budget = to_dict_budget(user['latest_budget'][0] if user['latest_budget'] else None)
                bill_counts = {status['_id']: status['count'] for status in user['bill_status_counts']} if user['bill_status_counts'] else {'pending': 0, 'paid': 0, 'overdue': 0}
                learning_progress = user['learning_progress'][0]['total_lessons_completed'] if user['learning_progress'] else 0
                tax_reminder = to_dict_tax_reminder(user['next_tax_reminder'][0] if user['next_tax_reminder'] else None)
                data = {
                    'username': user['_id'],
                    'email': user.get('email', ''),
                    'role': user.get('role', ''),
                    'ficore_credit_balance': user.get('ficore_credit_balance', 0),
                    'language': user.get('language', 'en'),
                    'budget_income': budget['income'] if budget['income'] is not None else '-',
                    'budget_fixed_expenses': budget['fixed_expenses'] if budget['fixed_expenses'] is not None else '-',
                    'budget_variable_expenses': budget['variable_expenses'] if budget['variable_expenses'] is not None else '-',
                    'budget_surplus_deficit': budget['surplus_deficit'] if budget['surplus_deficit'] is not None else '-',
                    'pending_bills': bill_counts.get('pending', 0),
                    'paid_bills': bill_counts.get('paid', 0),
                    'overdue_bills': bill_counts.get('overdue', 0),
                    'lessons_completed': learning_progress,
                    'next_tax_due_date': utils.format_date(tax_reminder['due_date']) if tax_reminder['due_date'] else '-',
                    'next_tax_amount': tax_reminder['amount'] if tax_reminder['amount'] is not None else '-'
                }
                report_data.append(data)
            if report_format == 'html':
                return render_template('reports/customer_reports.html', report_data=report_data, title='Facore Credits')
            elif report_format == 'pdf':
                return generate_customer_report_pdf(report_data)
            elif report_format == 'csv':
                return generate_customer_report_csv(report_data)
        except Exception as e:
            logger.error(f"Error generating customer report: {str(e)}", exc_info=True)
            flash('An error occurred while generating the report', 'danger')
    return render_template('reports/customer_reports_form.html', form=form, title='Generate Customer Report')

def generate_profit_loss_pdf(cashflows):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    # Page setup
    header_height = 0.7
    extra_space = 0.2
    row_height = 0.3
    bottom_margin = 0.5
    max_y = 10.5
    title_y = max_y - header_height - extra_space
    page_height = (max_y - bottom_margin) * inch
    rows_per_page = int((page_height - (title_y - 0.6) * inch) / (row_height * inch))

    def draw_table_headers(y):
        p.setFillColor(colors.black)
        p.drawString(1 * inch, y * inch, trans('general_date', default='Date'))
        p.drawString(2.5 * inch, y * inch, trans('general_party_name', default='Party Name'))
        p.drawString(4 * inch, y * inch, trans('general_type', default='Type'))
        p.drawString(5 * inch, y * inch, trans('general_amount', default='Amount'))
        return y - row_height

    # Initialize first page
    draw_ficore_pdf_header(p, current_user, y_start=max_y)
    p.setFont("Helvetica", 12)
    p.drawString(1 * inch, title_y * inch, trans('reports_profit_loss_report', default='Profit/Loss Report'))
    p.drawString(1 * inch, (title_y - 0.3) * inch, f"{trans('reports_generated_on', default='Generated on')}: {utils.format_date(datetime.utcnow())}")
    y = title_y - 0.6
    y = draw_table_headers(y)

    total_income = 0
    total_expense = 0
    row_count = 0

    for t in cashflows:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            y = draw_table_headers(y)
            row_count = 0

        p.drawString(1 * inch, y * inch, utils.format_date(t['created_at']))
        p.drawString(2.5 * inch, y * inch, t['party_name'])
        p.drawString(4 * inch, y * inch, trans(t['type'], default=t['type']))
        p.drawString(5 * inch, y * inch, utils.format_currency(t['amount']))
        if t['type'] == 'receipt':
            total_income += t['amount']
        else:
            total_expense += t['amount']
        y -= row_height
        row_count += 1

    # Draw totals on the same page if there's space
    if row_count + 3 <= rows_per_page:
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_income', default='Total Income')}: {utils.format_currency(total_income)}")
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_expense', default='Total Expense')}: {utils.format_currency(total_expense)}")
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_net_profit', default='Net Profit')}: {utils.format_currency(total_income - total_expense)}")
    else:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_income', default='Total Income')}: {utils.format_currency(total_income)}")
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_expense', default='Total Expense')}: {utils.format_currency(total_expense)}")
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_net_profit', default='Net Profit')}: {utils.format_currency(total_income - total_expense)}")

    p.save()
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=profit_loss.pdf'})

def generate_profit_loss_csv(cashflows):
    output = []
    output.extend(ficore_csv_header(current_user))
    output.append([trans('general_date', default='Date'), trans('general_party_name', default='Party Name'), trans('general_type', default='Type'), trans('general_amount', default='Amount')])
    total_income = 0
    total_expense = 0
    for t in cashflows:
        output.append([utils.format_date(t['created_at']), t['party_name'], trans(t['type'], default=t['type']), utils.format_currency(t['amount'])])
        if t['type'] == 'receipt':
            total_income += t['amount']
        else:
            total_expense += t['amount']
    output.append(['', '', '', f"{trans('reports_total_income', default='Total Income')}: {utils.format_currency(total_income)}"])
    output.append(['', '', '', f"{trans('reports_total_expense', default='Total Expense')}: {utils.format_currency(total_expense)}"])
    output.append(['', '', '', f"{trans('reports_net_profit', default='Net Profit')}: {utils.format_currency(total_income - total_expense)}"])
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerows(output)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=profit_loss.csv'})

def generate_debtors_creditors_pdf(records):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    # Page setup
    header_height = 0.7
    extra_space = 0.2
    row_height = 0.3
    bottom_margin = 0.5
    max_y = 10.5
    title_y = max_y - header_height - extra_space
    page_height = (max_y - bottom_margin) * inch
    rows_per_page = int((page_height - (title_y - 0.6) * inch) / (row_height * inch))

    def draw_table_headers(y):
        p.setFillColor(colors.black)
        p.drawString(1 * inch, y * inch, trans('general_date', default='Date'))
        p.drawString(2.5 * inch, y * inch, trans('general_name', default='Name'))
        p.drawString(4 * inch, y * inch, trans('general_type', default='Type'))
        p.drawString(5 * inch, y * inch, trans('general_amount_owed', default='Amount Owed'))
        p.drawString(6.5 * inch, y * inch, trans('general_description', default='Description'))
        return y - row_height

    # Initialize first page
    draw_ficore_pdf_header(p, current_user, y_start=max_y)
    p.setFont("Helvetica", 12)
    p.drawString(1 * inch, title_y * inch, trans('reports_debtors_creditors_report', default='Debtors/Creditors Report'))
    p.drawString(1 * inch, (title_y - 0.3) * inch, f"{trans('reports_generated_on', default='Generated on')}: {utils.format_date(datetime.utcnow())}")
    y = title_y - 0.6
    y = draw_table_headers(y)

    total_debtors = 0
    total_creditors = 0
    row_count = 0

    for r in records:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            y = draw_table_headers(y)
            row_count = 0

        p.drawString(1 * inch, y * inch, utils.format_date(r['created_at']))
        p.drawString(2.5 * inch, y * inch, r['name'])
        p.drawString(4 * inch, y * inch, trans(r['type'], default=r['type']))
        p.drawString(5 * inch, y * inch, utils.format_currency(r['amount_owed']))
        p.drawString(6.5 * inch, y * inch, r.get('description', '')[:20])
        if r['type'] == 'debtor':
            total_debtors += r['amount_owed']
        else:
            total_creditors += r['amount_owed']
        y -= row_height
        row_count += 1

    if row_count + 2 <= rows_per_page:
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_debtors', default='Total Debtors')}: {utils.format_currency(total_debtors)}")
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_creditors', default='Total Creditors')}: {utils.format_currency(total_creditors)}")
    else:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_debtors', default='Total Debtors')}: {utils.format_currency(total_debtors)}")
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_creditors', default='Total Creditors')}: {utils.format_currency(total_creditors)}")

    p.save()
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=debtors_creditors.pdf'})

def generate_debtors_creditors_csv(records):
    output = []
    output.extend(ficore_csv_header(current_user))
    output.append([trans('general_date', default='Date'), trans('general_name', default='Name'), trans('general_type', default='Type'), trans('general_amount_owed', default='Amount Owed'), trans('general_description', default='Description')])
    total_debtors = 0
    total_creditors = 0
    for r in records:
        output.append([utils.format_date(r['created_at']), r['name'], trans(r['type'], default=r['type']), utils.format_currency(r['amount_owed']), r.get('description', '')])
        if r['type'] == 'debtor':
            total_debtors += r['amount_owed']
        else:
            total_creditors += r['amount_owed']
    output.append(['', '', '', f"{trans('reports_total_debtors', default='Total Debtors')}: {utils.format_currency(total_debtors)}", ''])
    output.append(['', '', '', f"{trans('reports_total_creditors', default='Total Creditors')}: {utils.format_currency(total_creditors)}", ''])
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerows(output)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=debtors_creditors.csv'})

def generate_tax_obligations_pdf(tax_reminders):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    # Page setup
    header_height = 0.7
    extra_space = 0.2
    row_height = 0.3
    bottom_margin = 0.5
    max_y = 10.5
    title_y = max_y - header_height - extra_space
    page_height = (max_y - bottom_margin) * inch
    rows_per_page = int((page_height - (title_y - 0.6) * inch) / (row_height * inch))

    def draw_table_headers(y):
        p.setFillColor(colors.black)
        p.drawString(1 * inch, y * inch, trans('general_due_date', default='Due Date'))
        p.drawString(2.5 * inch, y * inch, trans('general_tax_type', default='Tax Type'))
        p.drawString(4 * inch, y * inch, trans('general_amount', default='Amount'))
        p.drawString(5 * inch, y * inch, trans('general_status', default='Status'))
        return y - row_height

    # Initialize first page
    draw_ficore_pdf_header(p, current_user, y_start=max_y)
    p.setFont("Helvetica", 12)
    p.drawString(1 * inch, title_y * inch, trans('reports_tax_obligations_report', default='Tax Obligations Report'))
    p.drawString(1 * inch, (title_y - 0.3) * inch, f"{trans('reports_generated_on', default='Generated on')}: {utils.format_date(datetime.utcnow())}")
    y = title_y - 0.6
    y = draw_table_headers(y)

    total_amount = 0
    row_count = 0

    for tr in tax_reminders:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            y = draw_table_headers(y)
            row_count = 0

        p.drawString(1 * inch, y * inch, utils.format_date(tr['due_date']))
        p.drawString(2.5 * inch, y * inch, tr['tax_type'])
        p.drawString(4 * inch, y * inch, utils.format_currency(tr['amount']))
        p.drawString(5 * inch, y * inch, trans(tr['status'], default=tr['status']))
        total_amount += tr['amount']
        y -= row_height
        row_count += 1

    if row_count + 1 <= rows_per_page:
        y -= row_height
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_tax_amount', default='Total Tax Amount')}: {utils.format_currency(total_amount)}")
    else:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        p.drawString(1 * inch, y * inch, f"{trans('reports_total_tax_amount', default='Total Tax Amount')}: {utils.format_currency(total_amount)}")

    p.save()
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=tax_obligations.pdf'})

def generate_tax_obligations_csv(tax_reminders):
    output = []
    output.extend(ficore_csv_header(current_user))
    output.append([trans('general_due_date', default='Due Date'), trans('general_tax_type', default='Tax Type'), trans('general_amount', default='Amount'), trans('general_status', default='Status')])
    total_amount = 0
    for tr in tax_reminders:
        output.append([utils.format_date(tr['due_date']), tr['tax_type'], utils.format_currency(tr['amount']), trans(tr['status'], default=tr['status'])])
        total_amount += tr['amount']
    output.append(['', '', f"{trans('reports_total_tax_amount', default='Total Tax Amount')}: {utils.format_currency(total_amount)}", ''])
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerows(output)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=tax_obligations.csv'})

def generate_budget_performance_pdf(budget_data):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    # Page setup
    header_height = 0.7
    extra_space = 0.2
    row_height = 0.3
    bottom_margin = 0.5
    max_y = 10.5
    title_y = max_y - header_height - extra_space
    page_height = (max_y - bottom_margin) * inch
    rows_per_page = int((page_height - (title_y - 0.6) * inch) / (row_height * inch))

    def draw_table_headers(y):
        p.setFillColor(colors.black)
        headers = [
            trans('general_date', default='Date'),
            trans('general_income', default='Income'),
            trans('general_actual_income', default='Actual Income'),
            trans('general_income_variance', default='Income Variance'),
            trans('general_fixed_expenses', default='Fixed Expenses'),
            trans('general_variable_expenses', default='Variable Expenses'),
            trans('general_actual_expenses', default='Actual Expenses'),
            trans('general_expense_variance', default='Expense Variance')
        ]
        x_positions = [1 * inch + i * 0.9 * inch for i in range(len(headers))]
        for header, x in zip(headers, x_positions):
            p.drawString(x, y * inch, header)
        return y - row_height, x_positions

    # Initialize first page
    draw_ficore_pdf_header(p, current_user, y_start=max_y)
    p.setFont("Helvetica", 10)
    p.drawString(1 * inch, title_y * inch, trans('reports_budget_performance_report', default='Budget Performance Report'))
    p.drawString(1 * inch, (title_y - 0.3) * inch, f"{trans('reports_generated_on', default='Generated on')}: {utils.format_date(datetime.utcnow())}")
    y = title_y - 0.6
    y, x_positions = draw_table_headers(y)

    row_count = 0
    for bd in budget_data:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            y, x_positions = draw_table_headers(y)
            row_count = 0

        values = [
            utils.format_date(bd['created_at']),
            utils.format_currency(bd['income']),
            utils.format_currency(bd['actual_income']),
            utils.format_currency(bd['income_variance']),
            utils.format_currency(bd['fixed_expenses']),
            utils.format_currency(bd['variable_expenses']),
            utils.format_currency(bd['actual_expenses']),
            utils.format_currency(bd['expense_variance'])
        ]
        for value, x in zip(values, x_positions):
            p.drawString(x, y * inch, value)
        y -= row_height
        row_count += 1

    p.save()
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=budget_performance.pdf'})

def generate_budget_performance_csv(budget_data):
    output = []
    output.extend(ficore_csv_header(current_user))
    output.append([
        trans('general_date', default='Date'),
        trans('general_income', default='Income'),
        trans('general_actual_income', default='Actual Income'),
        trans('general_income_variance', default='Income Variance'),
        trans('general_fixed_expenses', default='Fixed Expenses'),
        trans('general_variable_expenses', default='Variable Expenses'),
        trans('general_actual_expenses', default='Actual Expenses'),
        trans('general_expense_variance', default='Expense Variance')
    ])
    for bd in budget_data:
        output.append([
            utils.format_date(bd['created_at']),
            utils.format_currency(bd['income']),
            utils.format_currency(bd['actual_income']),
            utils.format_currency(bd['income_variance']),
            utils.format_currency(bd['fixed_expenses']),
            utils.format_currency(bd['variable_expenses']),
            utils.format_currency(bd['actual_expenses']),
            utils.format_currency(bd['expense_variance'])
        ])
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerows(output)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=budget_performance.csv'})

def generate_shopping_report_pdf(shopping_data):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    # Page setup
    header_height = 0.7
    extra_space = 0.2
    row_height = 0.3
    section_space = 0.5
    bottom_margin = 0.5
    max_y = 10.5
    title_y = max_y - header_height - extra_space
    page_height = (max_y - bottom_margin) * inch
    rows_per_page = int((page_height - (title_y - 0.6) * inch) / (row_height * inch))

    def draw_list_headers(y):
        p.setFont("Helvetica", 10)
        headers = [
            trans('general_date', default='Date'),
            trans('shopping_list_name', default='List Name'),
            trans('shopping_budget', default='Budget'),
            trans('shopping_total_spent', default='Total Spent'),
            trans('shopping_collaborators', default='Collaborators')
        ]
        x_positions = [1 * inch, 2 * inch, 3.5 * inch, 4.5 * inch, 5.5 * inch]
        for header, x in zip(headers, x_positions):
            p.drawString(x, y * inch, header)
        return y - row_height, x_positions

    def draw_item_headers(y):
        p.setFont("Helvetica", 10)
        headers = [
            trans('general_date', default='Date'),
            trans('shopping_item_name', default='Item Name'),
            trans('shopping_quantity', default='Quantity'),
            trans('shopping_price', default='Price'),
            trans('shopping_status', default='Status'),
            trans('shopping_category', default='Category'),
            trans('shopping_store', default='Store')
        ]
        x_positions = [1 * inch, 2 * inch, 3 * inch, 3.5 * inch, 4 * inch, 4.8 * inch, 5.5 * inch]
        for header, x in zip(headers, x_positions):
            p.drawString(x, y * inch, header)
        return y - row_height, x_positions

    def draw_suggestion_headers(y):
        p.setFont("Helvetica", 10)
        headers = [
            trans('general_date', default='Date'),
            trans('shopping_item_name', default='Item Name'),
            trans('shopping_quantity', default='Quantity'),
            trans('shopping_price', default='Price'),
            trans('shopping_status', default='Status'),
            trans('shopping_category', default='Category')
        ]
        x_positions = [1 * inch, 2 * inch, 3 * inch, 3.5 * inch, 4 * inch, 4.8 * inch]
        for header, x in zip(headers, x_positions):
            p.drawString(x, y * inch, header)
        return y - row_height, x_positions

    # Initialize first page
    draw_ficore_pdf_header(p, current_user, y_start=max_y)
    p.setFont("Helvetica", 12)
    p.drawString(1 * inch, title_y * inch, trans('reports_shopping_report', default='Shopping Report'))
    p.drawString(1 * inch, (title_y - 0.3) * inch, f"{trans('reports_generated_on', default='Generated on')}: {utils.format_date(datetime.utcnow())}")
    y = title_y - 0.6

    # Shopping Lists Section
    p.setFont("Helvetica-Bold", 12)
    p.drawString(1 * inch, y * inch, trans('shopping_lists', default='Shopping Lists'))
    y -= row_height
    y, x_positions = draw_list_headers(y)
    total_budget = 0
    total_spent = 0
    row_count = 0

    for lst in shopping_data['lists']:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            p.setFont("Helvetica-Bold", 12)
            p.drawString(1 * inch, y * inch, trans('shopping_lists', default='Shopping Lists'))
            y -= row_height
            y, x_positions = draw_list_headers(y)
            row_count = 0

        p.drawString(x_positions[0], y * inch, utils.format_date(lst['created_at']))
        p.drawString(x_positions[1], y * inch, lst['name'][:20])
        p.drawString(x_positions[2], y * inch, utils.format_currency(lst['budget']))
        p.drawString(x_positions[3], y * inch, utils.format_currency(lst['total_spent']))
        p.drawString(x_positions[4], y * inch, ', '.join(lst['collaborators'])[:20])
        total_budget += lst['budget']
        total_spent += lst['total_spent']
        y -= row_height
        row_count += 1

    if row_count + 2 <= rows_per_page:
        y -= row_height
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_budget', default='Total Budget')}: {utils.format_currency(total_budget)}")
        y -= row_height
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_spent', default='Total Spent')}: {utils.format_currency(total_spent)}")
    else:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_budget', default='Total Budget')}: {utils.format_currency(total_budget)}")
        y -= row_height
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_spent', default='Total Spent')}: {utils.format_currency(total_spent)}")
    y -= section_space

    # Shopping Items Section
    if row_count + 3 >= rows_per_page:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        row_count = 0
    p.setFont("Helvetica-Bold", 12)
    p.drawString(1 * inch, y * inch, trans('shopping_items', default='Shopping Items'))
    y -= row_height
    y, x_positions = draw_item_headers(y)
    row_count += 2

    total_price = 0
    for item in shopping_data['items']:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            p.setFont("Helvetica-Bold", 12)
            p.drawString(1 * inch, y * inch, trans('shopping_items', default='Shopping Items'))
            y -= row_height
            y, x_positions = draw_item_headers(y)
            row_count = 0

        p.drawString(x_positions[0], y * inch, utils.format_date(item['created_at']))
        p.drawString(x_positions[1], y * inch, item['name'][:20])
        p.drawString(x_positions[2], y * inch, str(item['quantity']))
        p.drawString(x_positions[3], y * inch, utils.format_currency(item['price']))
        p.drawString(x_positions[4], y * inch, trans(item['status'], default=item['status']))
        p.drawString(x_positions[5], y * inch, item['category'][:15])
        p.drawString(x_positions[6], y * inch, item['store'][:15])
        total_price += item['price'] * item['quantity']
        y -= row_height
        row_count += 1

    if row_count + 1 <= rows_per_page:
        y -= row_height
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_price', default='Total Price')}: {utils.format_currency(total_price)}")
    else:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_price', default='Total Price')}: {utils.format_currency(total_price)}")
    y -= section_space

    # Suggestions Section
    if row_count + 3 >= rows_per_page:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        row_count = 0
    p.setFont("Helvetica-Bold", 12)
    p.drawString(1 * inch, y * inch, trans('shopping_suggestions', default='Suggestions'))
    y -= row_height
    y, x_positions = draw_suggestion_headers(y)
    row_count += 2

    total_suggestion_price = 0
    for sug in shopping_data['suggestions']:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            p.setFont("Helvetica-Bold", 12)
            p.drawString(1 * inch, y * inch, trans('shopping_suggestions', default='Suggestions'))
            y -= row_height
            y, x_positions = draw_suggestion_headers(y)
            row_count = 0

        p.drawString(x_positions[0], y * inch, utils.format_date(sug['created_at']))
        p.drawString(x_positions[1], y * inch, sug['name'][:20])
        p.drawString(x_positions[2], y * inch, str(sug['quantity']))
        p.drawString(x_positions[3], y * inch, utils.format_currency(sug['price']))
        p.drawString(x_positions[4], y * inch, trans(sug['status'], default=sug['status']))
        p.drawString(x_positions[5], y * inch, sug['category'][:15])
        total_suggestion_price += sug['price'] * sug['quantity']
        y -= row_height
        row_count += 1

    if row_count + 1 <= rows_per_page:
        y -= row_height
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_suggestion_price', default='Total Suggestion Price')}: {utils.format_currency(total_suggestion_price)}")
    else:
        p.showPage()
        draw_ficore_pdf_header(p, current_user, y_start=max_y)
        y = title_y - 0.6
        p.drawString(x_positions[0], y * inch, f"{trans('shopping_total_suggestion_price', default='Total Suggestion Price')}: {utils.format_currency(total_suggestion_price)}")

    p.save()
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=shopping_report.pdf'})

def generate_shopping_report_csv(shopping_data):
    output = []
    output.extend(ficore_csv_header(current_user))
    
    # Shopping Lists Section
    output.append([trans('shopping_lists', default='Shopping Lists')])
    output.append([
        trans('general_date', default='Date'),
        trans('shopping_list_name', default='List Name'),
        trans('shopping_budget', default='Budget'),
        trans('shopping_total_spent', default='Total Spent'),
        trans('shopping_collaborators', default='Collaborators')
    ])
    total_budget = 0
    total_spent = 0
    for lst in shopping_data['lists']:
        output.append([
            utils.format_date(lst['created_at']),
            lst['name'],
            utils.format_currency(lst['budget']),
            utils.format_currency(lst['total_spent']),
            ', '.join(lst['collaborators'])
        ])
        total_budget += lst['budget']
        total_spent += lst['total_spent']
    output.append(['', '', f"{trans('shopping_total_budget', default='Total Budget')}: {utils.format_currency(total_budget)}", f"{trans('shopping_total_spent', default='Total Spent')}: {utils.format_currency(total_spent)}", ''])
    output.append([])

    # Shopping Items Section
    output.append([trans('shopping_items', default='Shopping Items')])
    output.append([
        trans('general_date', default='Date'),
        trans('shopping_item_name', default='Item Name'),
        trans('shopping_quantity', default='Quantity'),
        trans('shopping_price', default='Price'),
        trans('shopping_status', default='Status'),
        trans('shopping_category', default='Category'),
        trans('shopping_store', default='Store')
    ])
    total_price = 0
    for item in shopping_data['items']:
        output.append([
            utils.format_date(item['created_at']),
            item['name'],
            item['quantity'],
            utils.format_currency(item['price']),
            trans(item['status'], default=item['status']),
            item['category'],
            item['store']
        ])
        total_price += item['price'] * item['quantity']
    output.append(['', '', '', f"{trans('shopping_total_price', default='Total Price')}: {utils.format_currency(total_price)}", '', '', ''])
    output.append([])

    # Suggestions Section
    output.append([trans('shopping_suggestions', default='Suggestions')])
    output.append([
        trans('general_date', default='Date'),
        trans('shopping_item_name', default='Item Name'),
        trans('shopping_quantity', default='Quantity'),
        trans('shopping_price', default='Price'),
        trans('shopping_status', default='Status'),
        trans('shopping_category', default='Category')
    ])
    total_suggestion_price = 0
    for sug in shopping_data['suggestions']:
        output.append([
            utils.format_date(sug['created_at']),
            sug['name'],
            sug['quantity'],
            utils.format_currency(sug['price']),
            trans(sug['status'], default=sug['status']),
            sug['category']
        ])
        total_suggestion_price += sug['price'] * sug['quantity']
    output.append(['', '', '', f"{trans('shopping_total_suggestion_price', default='Total Suggestion Price')}: {utils.format_currency(total_suggestion_price)}", '', ''])

    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerows(output)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=shopping_report.csv'})

def generate_customer_report_pdf(report_data):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    # Page setup
    header_height = 0.7
    extra_space = 0.2
    row_height = 0.2
    bottom_margin = 0.5
    max_y = 10.5
    title_y = max_y - header_height - extra_space
    page_height = (max_y - bottom_margin) * inch
    rows_per_page = int((page_height - (title_y - 0.6) * inch) / (row_height * inch))

    def draw_table_headers(y):
        p.setFillColor(colors.black)
        headers = [
            'Username', 'Email', 'Role', 'Credits', 'Lang',
            'Income', 'Fixed Exp', 'Var Exp', 'Surplus',
            'Pending Bills', 'Paid Bills', 'Overdue Bills',
            'Lessons', 'Tax Due', 'Tax Amt'
        ]
        x_positions = [0.5 * inch + i * 0.3 * inch for i in range(len(headers))]
        for header, x in zip(headers, x_positions):
            p.drawString(x, y * inch, header)
        return y - row_height, x_positions

    # Initialize first page
    draw_ficore_pdf_header(p, current_user, y_start=max_y)
    p.setFont("Helvetica", 8)
    p.drawString(0.5 * inch, title_y * inch, trans('reports_customer_report', default='Customer Report'))
    p.drawString(0.5 * inch, (title_y - 0.3) * inch, f"{trans('reports_generated_on', default='Generated on')}: {utils.format_date(datetime.utcnow())}")
    y = title_y - 0.6
    y, x_positions = draw_table_headers(y)

    row_count = 0
    for data in report_data:
        if row_count >= rows_per_page:
            p.showPage()
            draw_ficore_pdf_header(p, current_user, y_start=max_y)
            y = title_y - 0.6
            y, x_positions = draw_table_headers(y)
            row_count = 0

        values = [
            data['username'], data['email'], data['role'], str(data['ficore_credit_balance']), data['language'],
            str(data['budget_income']), str(data['budget_fixed_expenses']), str(data['budget_variable_expenses']), str(data['budget_surplus_deficit']),
            str(data['pending_bills']), str(data['paid_bills']), str(data['overdue_bills']),
            str(data['lessons_completed']), data['next_tax_due_date'], str(data['next_tax_amount'])
        ]
        for value, x in zip(values, x_positions):
            p.drawString(x, y * inch, str(value)[:15])
        y -= row_height
        row_count += 1

    p.save()
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=customer_report.pdf'})

def generate_customer_report_csv(report_data):
    output = []
    output.extend(ficore_csv_header(current_user))
    headers = [
        'Username', 'Email', 'Role', 'Ficore Credit Balance', 'Language',
        'Budget Income', 'Budget Fixed Expenses', 'Budget Variable Expenses', 'Budget Surplus/Deficit',
        'Pending Bills', 'Paid Bills', 'Overdue Bills',
        'Lessons Completed', 'Next Tax Due Date', 'Next Tax Amount'
    ]
    output.append(headers)
    for data in report_data:
        row = [
            data['username'], data['email'], data['role'], data['ficore_credit_balance'], data['language'],
            data['budget_income'], data['budget_fixed_expenses'], data['budget_variable_expenses'], data['budget_surplus_deficit'],
            data['pending_bills'], data['paid_bills'], data['overdue_bills'],
            data['lessons_completed'], data['next_tax_due_date'], data['next_tax_amount']
        ]
        output.append(row)
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerows(output)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=customer_report.csv'})
