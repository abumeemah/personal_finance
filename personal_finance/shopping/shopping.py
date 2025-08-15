from flask import Blueprint, request, session, redirect, url_for, render_template, flash, current_app, jsonify, Response
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect, CSRFError
from wtforms import StringField, FloatField, IntegerField, SelectField, SubmitField
from wtforms.validators import DataRequired, NumberRange, ValidationError, Email
from flask_login import current_user, login_required
from datetime import datetime
from helpers.branding_helpers import draw_ficore_pdf_header
from bson import ObjectId
from pymongo import errors
from utils import get_mongo_db, requires_role, logger, clean_currency, check_ficore_credit_balance, is_admin, format_date, format_currency
from translations import trans
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import inch
from io import BytesIO
from contextlib import nullcontext
import uuid
from models import log_tool_usage, get_shopping_lists
from session_utils import create_anonymous_session
import json

shopping_bp = Blueprint(
    'shopping',
    __name__,
    template_folder='templates/',
    url_prefix='/shopping'
)

csrf = CSRFProtect()

def auto_categorize_item(item_name):
    item_name = item_name.lower().strip()
    categories = {
        'fruits': ['apple', 'banana', 'orange', 'mango', 'pineapple', 'berry', 'grape'],
        'vegetables': ['carrot', 'potato', 'tomato', 'onion', 'spinach', 'lettuce'],
        'dairy': ['milk', 'cheese', 'yogurt', 'butter', 'cream'],
        'meat': ['chicken', 'beef', 'pork', 'fish', 'egg'],
        'grains': ['rice', 'bread', 'pasta', 'flour', 'cereal'],
        'beverages': ['juice', 'soda', 'water', 'tea', 'coffee'],
        'household': ['detergent', 'soap', 'tissue', 'paper towel'],
        'other': []
    }
    for category, keywords in categories.items():
        if any(keyword in item_name for keyword in keywords):
            return category
    return 'other'

def deduct_ficore_credits(db, user_id, amount, action, item_id=None, mongo_session=None):
    try:
        amount = int(amount)  # Ensure amount is an integer (1 or 2)
        if amount not in [1, 2]:
            logger.error(f"Invalid deduction amount {amount} for user {user_id}, action: {action}",
                        extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
            return False
        user = db.users.find_one({'_id': user_id}, session=mongo_session)
        if not user:
            logger.error(f"User {user_id} not found for credit deduction, action: {action}",
                        extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
            return False
        current_balance = user.get('ficore_credit_balance', 0)
        if current_balance < amount:
            logger.warning(f"Insufficient credits for user {user_id}: required {amount}, available {current_balance}, action: {action}",
                         extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
            return False
        session_to_use = mongo_session if mongo_session else db.client.start_session()
        owns_session = not mongo_session
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with session_to_use.start_transaction() if not mongo_session else nullcontext():
                    result = db.users.update_one(
                        {'_id': user_id},
                        [{'$set': {'ficore_credit_balance': {'$toInt': {'$subtract': ['$ficore_credit_balance', amount]}}}}],
                        session=session_to_use
                    )
                    if result.modified_count == 0:
                        logger.error(f"Failed to deduct {amount} credits for user {user_id}, action: {action}: No documents modified",
                                    extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
                        db.ficore_credit_transactions.insert_one({
                            '_id': ObjectId(),
                            'user_id': user_id,
                            'action': action,
                            'amount': -amount,
                            'item_id': str(item_id) if item_id else None,
                            'timestamp': datetime.utcnow(),
                            'session_id': session.get('sid', 'no-session-id'),
                            'status': 'failed'
                        }, session=session_to_use)
                        raise ValueError(f"Failed to update user balance for {user_id}")
                    transaction = {
                        '_id': ObjectId(),
                        'user_id': user_id,
                        'action': action,
                        'amount': -amount,
                        'item_id': str(item_id) if item_id else None,
                        'timestamp': datetime.utcnow(),
                        'session_id': session.get('sid', 'no-session-id'),
                        'status': 'completed'
                    }
                    db.ficore_credit_transactions.insert_one(transaction, session=session_to_use)
                    db.audit_logs.insert_one({
                        'admin_id': 'system',
                        'action': f'deduct_ficore_credits_{action}',
                        'details': {'user_id': user_id, 'amount': amount, 'item_id': str(item_id) if item_id else None},
                        'timestamp': datetime.utcnow()
                    }, session=session_to_use)
                    if owns_session:
                        session_to_use.commit_transaction()
                    logger.info(f"Deducted {amount} Ficore Credits for {action} by user {user_id}",
                               extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
                    return True
            except errors.OperationFailure as e:
                if "TransientTransactionError" in e.details.get("errorLabels", []):
                    if attempt < max_retries - 1:
                        logger.warning(f"Retrying transaction for user {user_id}, action: {action}, attempt {attempt + 1}",
                                     extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
                        continue
                logger.error(f"Transaction aborted for user {user_id}, action: {action}: {str(e)}",
                            exc_info=True, extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
                if owns_session:
                    session_to_use.abort_transaction()
                return False
            except (ValueError, errors.PyMongoError) as e:
                logger.error(f"Transaction aborted for user {user_id}, action: {action}: {str(e)}",
                            exc_info=True, extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
                if owns_session:
                    session_to_use.abort_transaction()
                return False
            finally:
                if owns_session:
                    session_to_use.end_session()
    except Exception as e:
        logger.error(f"Unexpected error deducting {amount} Ficore Credits for {action} by user {user_id}: {str(e)}",
                    exc_info=True, extra={'session_id': session.get('sid', 'no-session-id'), 'user_id': user_id})
        return False

def custom_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.is_authenticated or session.get('is_anonymous', False):
            return f(*args, **kwargs)
        return redirect(url_for('users.login', next=request.url))
    return decorated_function

class ShoppingListForm(FlaskForm):
    name = StringField(
        trans('shopping_list_name', default='List Name'),
        validators=[DataRequired(message=trans('shopping_name_required', default='List name is required'))]
    )
    budget = FloatField(
        trans('shopping_budget', default='Budget'),
        filters=[clean_currency],
        validators=[
            DataRequired(message=trans('shopping_budget_required', default='Budget is required')),
            NumberRange(min=0.01, max=10000000000, message=trans('shopping_budget_max', default='Budget must be between 0.01 and 10 billion'))
        ]
    )
    submit = SubmitField(trans('shopping_submit', default='Create List'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lang = session.get('lang', 'en')
        self.name.label.text = trans('shopping_list_name', lang) or 'List Name'
        self.budget.label.text = trans('shopping_budget', lang) or 'Budget'
        self.submit.label.text = trans('shopping_submit', lang) or 'Create List'

    def validate_budget(self, budget):
        if budget.data is None or budget.data == '':
            raise ValidationError(trans('shopping_budget_required', default='Budget is required'))
        try:
            cleaned_value = str(budget.data).replace(',', '').replace(' ', '')
            budget.data = round(float(cleaned_value), 2)
            if budget.data < 0.01:
                raise ValidationError(trans('shopping_budget_min', default='Budget must be at least 0.01'))
            if budget.data > 10000000000:
                raise ValidationError(trans('shopping_budget_max', default='Budget must be between 0.01 and 10 billion'))
        except (ValueError, TypeError):
            logger.error(f"Budget validation failed: {budget.data}", extra={'session_id': session.get('sid', 'no-session-id')})
            raise ValidationError(trans('shopping_budget_invalid', default='Invalid budget format'))

class ShoppingItemsForm(FlaskForm):
    name = StringField(
        trans('shopping_item_name', default='Item Name'),
        validators=[DataRequired(message=trans('shopping_item_name_required', default='Item name is required'))]
    )
    quantity = IntegerField(
        trans('shopping_quantity', default='Quantity'),
        validators=[
            DataRequired(message=trans('shopping_quantity_required', default='Quantity is required')),
            NumberRange(min=1, max=1000, message=trans('shopping_quantity_range', default='Quantity must be between 1 and 1000'))
        ]
    )
    price = FloatField(
        trans('shopping_price', default='Price'),
        filters=[clean_currency],
        validators=[
            DataRequired(message=trans('shopping_price_required', default='Price is required')),
            NumberRange(min=0, max=1000000, message=trans('shopping_price_range', default='Price must be between 0 and 1 million'))
        ]
    )
    unit = SelectField(
        trans('shopping_unit', default='Unit'),
        choices=[
            ('piece', trans('shopping_unit_piece', default='Piece')),
            ('carton', trans('shopping_unit_carton', default='Carton')),
            ('kg', trans('shopping_unit_kg', default='Kilogram')),
            ('liter', trans('shopping_unit_liter', default='Liter')),
            ('pack', trans('shopping_unit_pack', default='Pack')),
            ('other', trans('shopping_unit_other', default='Other'))
        ],
        validators=[DataRequired(message=trans('shopping_unit_required', default='Unit is required'))]
    )
    store = StringField(
        trans('shopping_store', default='Store'),
        validators=[DataRequired(message=trans('shopping_store_required', default='Store is required'))]
    )
    category = SelectField(
        trans('shopping_category', default='Category'),
        choices=[
            ('fruits', trans('shopping_category_fruits', default='Fruits')),
            ('vegetables', trans('shopping_category_vegetables', default='Vegetables')),
            ('dairy', trans('shopping_category_dairy', default='Dairy')),
            ('meat', trans('shopping_category_meat', default='Meat')),
            ('grains', trans('shopping_category_grains', default='Grains')),
            ('beverages', trans('shopping_category_beverages', default='Beverages')),
            ('household', trans('shopping_category_household', default='Household')),
            ('other', trans('shopping_category_other', default='Other'))
        ]
    )
    status = SelectField(
        trans('shopping_status', default='Status'),
        choices=[
            ('to_buy', trans('shopping_status_to_buy', default='To Buy')),
            ('bought', trans('shopping_status_bought', default='Bought'))
        ]
    )
    frequency = IntegerField(
        trans('shopping_frequency', default='Frequency (days)'),
        validators=[
            DataRequired(message=trans('shopping_frequency_required', default='Frequency is required')),
            NumberRange(min=1, max=365, message=trans('shopping_frequency_range', default='Frequency must be between 1 and 365 days'))
        ]
    )
    submit = SubmitField(trans('shopping_item_submit', default='Add Item'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lang = session.get('lang', 'en')
        self.name.label.text = trans('shopping_item_name', lang) or 'Item Name'
        self.quantity.label.text = trans('shopping_quantity', lang) or 'Quantity'
        self.price.label.text = trans('shopping_price', lang) or 'Price'
        self.unit.label.text = trans('shopping_unit', lang) or 'Unit'
        self.store.label.text = trans('shopping_store', lang) or 'Store'
        self.category.label.text = trans('shopping_category', lang) or 'Category'
        self.status.label.text = trans('shopping_status', lang) or 'Status'
        self.frequency.label.text = trans('shopping_frequency', lang) or 'Frequency (days)'
        self.submit.label.text = trans('shopping_item_submit', lang) or 'Add Item'

class ShareListForm(FlaskForm):
    email = StringField(
        trans('shopping_collaborator_email', default='Collaborator Email'),
        validators=[
            DataRequired(message=trans('shopping_email_required', default='Email is required')),
            Email(message=trans('shopping_invalid_email', default='Invalid email address'))
        ]
    )
    submit = SubmitField(trans('shopping_share_submit', default='Share List'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lang = session.get('lang', 'en')
        self.email.label.text = trans('shopping_collaborator_email', lang) or 'Collaborator Email'
        self.submit.label.text = trans('shopping_share_submit', lang) or 'Share List'

@shopping_bp.route('/main', methods=['GET', 'POST'])
@custom_login_required
@requires_role(['personal', 'admin'])
def main():
    # Ensure session persistence for anonymous users
    if 'sid' not in session:
        create_anonymous_session()
        session['is_anonymous'] = True
        logger.debug(f"New session created with sid: {session['sid']}")
    session.permanent = True
    session.modified = True

    list_form = ShoppingListForm()
    item_form = ShoppingItemsForm()
    share_form = ShareListForm()
    items_form = ShoppingItemsForm()
    db = get_mongo_db()

    # Check credits before rendering create-list tab
    if request.args.get('tab', 'create-list') == 'create-list' and current_user.is_authenticated and not is_admin():
        if not check_ficore_credit_balance(required_amount=1, user_id=current_user.id):
            flash(trans('shopping_insufficient_credits', default='Insufficient credits to create a list. Please add credits.'), 'danger')
            return redirect(url_for('dashboard.index'))

    valid_tabs = ['create-list', 'add-items', 'view-lists', 'manage-list']
    active_tab = request.args.get('tab', 'create-list')
    if active_tab not in valid_tabs:
        active_tab = 'create-list'

    filter_criteria = {} if is_admin() else {'user_id': str(current_user.id)} if current_user.is_authenticated else {'session_id': session['sid']}
    lists = {str(lst['_id']): lst for lst in db.shopping_lists.find(filter_criteria).sort('created_at', -1)}
    
    # Preselect most recent list if none selected
    selected_list_id = request.args.get('list_id') or session.get('selected_list_id')
    if not selected_list_id and lists:
        selected_list_id = list(lists.keys())[0]
        session['selected_list_id'] = selected_list_id

    selected_list = lists.get(selected_list_id, {})
    items = []
    if selected_list_id:
        list_items = list(db.shopping_items.find({'list_id': selected_list_id}))
        items = [{
            'id': str(item['_id']),
            'name': item.get('name', ''),
            'quantity': int(item.get('quantity', 1)),
            'price_raw': float(item.get('price', 0.0)),
            'unit': item.get('unit', 'piece'),
            'category': item.get('category', 'other'),
            'status': item.get('status', 'to_buy'),
            'store': item.get('store', 'Unknown'),
            'frequency': int(item.get('frequency', 7))
        } for item in list_items]
        selected_list['items'] = items

    categories = {}
    if selected_list_id:
        categories = {
            trans('shopping_category_fruits', default='Fruits'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'fruits'),
            trans('shopping_category_vegetables', default='Vegetables'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'vegetables'),
            trans('shopping_category_dairy', default='Dairy'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'dairy'),
            trans('shopping_category_meat', default='Meat'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'meat'),
            trans('shopping_category_grains', default='Grains'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'grains'),
            trans('shopping_category_beverages', default='Beverages'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'beverages'),
            trans('shopping_category_household', default='Household'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'household'),
            trans('shopping_category_other', default='Other'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'other')
        }
        categories = {k: v for k, v in categories.items() if v > 0}

    try:
        log_tool_usage(
            tool_name='shopping',
            db=db,
            user_id=current_user.id if current_user.is_authenticated else None,
            session_id=session.get('sid', 'no-session'),
            action='main_view'
        )
    except Exception as e:
        flash(trans('shopping_log_error', default='Error logging activity.'), 'danger')

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create_list':
            logger.debug(f"Processing create_list action with form data: {request.form}", extra={'session_id': session.get('sid', 'no-session-id')})
            if list_form.validate_on_submit():
                if current_user.is_authenticated and not is_admin():
                    if not check_ficore_credit_balance(required_amount=1, user_id=current_user.id):
                        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                            return jsonify({
                                'success': False,
                                'error': trans('shopping_insufficient_credits', default='Insufficient credits to create a list.')
                            }), 403
                        flash(trans('shopping_insufficient_credits', default='Insufficient credits to create a list.'), 'danger')
                        return redirect(url_for('dashboard.index'))
                # Ensure session_id is always a string
                session_id = session.get('sid', str(uuid.uuid4()))
                if not session.get('sid'):
                    session['sid'] = session_id
                    logger.debug(f"Assigned new session_id: {session_id}")
                list_data = {
                    '_id': ObjectId(),
                    'name': list_form.name.data.strip(),
                    'user_id': str(current_user.id) if current_user.is_authenticated else None,
                    'session_id': session_id if not current_user.is_authenticated else None,
                    'budget': float(list_form.budget.data),
                    'created_at': datetime.utcnow(),
                    'updated_at': datetime.utcnow(),
                    'collaborators': [],
                    'items': [],
                    'total_spent': 0.0,
                    'status': 'active'
                }
                try:
                    with db.client.start_session() as mongo_session:
                        with mongo_session.start_transaction():
                            logger.debug(f"Inserting list_data: {list_data}", extra={'session_id': session_id})
                            db.shopping_lists.insert_one(list_data, session=mongo_session)
                            if current_user.is_authenticated and not is_admin():
                                if not deduct_ficore_credits(db, current_user.id, 1, 'create_shopping_list', str(list_data['_id']), mongo_session):
                                    flash(trans('shopping_credit_deduction_failed', default='Failed to deduct credits for list creation.'), 'danger')
                                    return redirect(url_for('shopping.main', tab='create-list'))
                    session['selected_list_id'] = str(list_data['_id'])
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return jsonify({
                            'success': True,
                            'redirect_url': url_for('shopping.main', tab='add-items', list_id=str(list_data['_id']))
                        })
                    flash(trans('shopping_list_created', default='Shopping list created successfully!'), 'success')
                    return redirect(url_for('shopping.main', tab='add-items', list_id=str(list_data['_id'])))
                except errors.WriteError as e:
                    logger.error(f"Failed to save list {list_data['_id']}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return jsonify({
                            'success': False,
                            'error': trans('shopping_list_error', default='Error saving list due to validation failure.')
                        }), 500
                    flash(trans('shopping_list_error', default='Error saving list due to validation failure.'), 'danger')
                    return redirect(url_for('shopping.main', tab='create-list'))
                except Exception as e:
                    logger.error(f"Unexpected error saving list {list_data['_id']}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return jsonify({
                            'success': False,
                            'error': trans('shopping_list_error', default=f'Error saving list: {str(e)}')
                        }), 500
                    flash(trans('shopping_list_error', default=f'Error saving list: {str(e)}'), 'danger')
                    return redirect(url_for('shopping.main', tab='create-list'))
            else:
                errors = {field: [trans(error, default=error) for error in field_errors] for field, field_errors in list_form.errors.items()}
                logger.debug(f"Form validation failed: {errors}", extra={'session_id': session.get('sid', 'no-session-id')})
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({
                        'success': False,
                        'error': trans('shopping_form_invalid', default='Invalid form data.'),
                        'errors': errors
                    }), 400
                for field, field_errors in list_form.errors.items():
                    for error in field_errors:
                        flash(f"{field.capitalize()}: {trans(error, default=error)}", 'danger')
                return render_template(
                    'shopping/create_list.html',
                    list_form=list_form,
                    item_form=item_form,
                    share_form=share_form,
                    lists=lists,
                    selected_list=selected_list,
                    selected_list_id=selected_list_id,
                    items=items,
                    categories=categories,
                    tips=[
                        trans('shopping_tip_plan_ahead', default='Plan your shopping list ahead to avoid impulse buys.'),
                        trans('shopping_tip_compare_prices', default='Compare prices across stores to save money.'),
                        trans('shopping_tip_bulk_buy', default='Buy non-perishable items in bulk to reduce costs.'),
                        trans('shopping_tip_check_sales', default='Check for sales or discounts before shopping.')
                    ],
                    insights=[],
                    tool_title=trans('shopping_title', default='Shopping List Planner'),
                    active_tab='create-list'
                )

        elif action == 'add_items':
            list_id = request.form.get('list_id')
            if not ObjectId.is_valid(list_id):
                flash(trans('shopping_invalid_list_id', default='Invalid list ID.'), 'danger')
                return redirect(url_for('shopping.main', tab='add-items'))
            shopping_list = db.shopping_lists.find_one({'_id': ObjectId(list_id), **filter_criteria})
            if not shopping_list:
                flash(trans('shopping_list_not_found', default='List not found.'), 'danger')
                return redirect(url_for('shopping.main', tab='add-items'))
            existing_items = db.shopping_items.find({'list_id': list_id}, {'name': 1})
            existing_names = {item['name'].lower() for item in existing_items}
            new_items = []
            for i in range(1, 6):
                new_name = request.form.get(f'new_item_name_{i}', '').strip()
                if new_name:
                    new_items.append({
                        'name': new_name,
                        'quantity': request.form.get(f'new_item_quantity_{i}', 1),
                        'price': request.form.get(f'new_item_price_{i}', '0'),
                        'unit': request.form.get(f'new_item_unit_{i}', 'piece'),
                        'category': request.form.get(f'new_item_category_{i}', auto_categorize_item(new_name)),
                        'status': request.form.get(f'new_item_status_{i}', 'to_buy'),
                        'store': request.form.get(f'new_item_store_{i}', 'Unknown'),
                        'frequency': request.form.get(f'new_item_frequency_{i}', 7)
                    })
            for item_data in new_items:
                if item_data['name'].lower() in existing_names:
                    flash(trans('shopping_duplicate_item_name', default='Item name already exists in this list.'), 'danger')
                    return redirect(url_for('shopping.main', tab='add-items', list_id=list_id))
            added = 0
            session_id = session.get('sid', str(uuid.uuid4()))
            if not session.get('sid'):
                session['sid'] = session_id
                logger.debug(f"Assigned new session_id: {session_id}")
            try:
                with db.client.start_session() as mongo_session:
                    with mongo_session.start_transaction():
                        for item_data in new_items:
                            try:
                                new_quantity = int(item_data['quantity'])
                                new_price = float(clean_currency(item_data['price']))
                                new_unit = item_data['unit']
                                new_category = item_data['category']
                                new_status = item_data['status']
                                new_store = item_data['store']
                                new_frequency = int(item_data['frequency'])
                                if new_quantity < 1 or new_quantity > 1000 or new_price < 0 or new_price > 1000000 or new_frequency < 1 or new_frequency > 365:
                                    raise ValueError('Invalid input range')
                                new_item_data = {
                                    '_id': ObjectId(),
                                    'list_id': list_id,
                                    'user_id': str(current_user.id) if current_user.is_authenticated else None,
                                    'session_id': session_id,
                                    'name': item_data['name'],
                                    'quantity': new_quantity,
                                    'price': new_price,
                                    'unit': new_unit,
                                    'category': new_category,
                                    'status': new_status,
                                    'store': new_store,
                                    'frequency': new_frequency,
                                    'created_at': datetime.utcnow(),
                                    'updated_at': datetime.utcnow()
                                }
                                logger.debug(f"Inserting item_data: {new_item_data}", extra={'session_id': session_id})
                                db.shopping_items.insert_one(new_item_data, session=mongo_session)
                                added += 1
                                existing_names.add(item_data['name'].lower())
                            except ValueError as e:
                                flash(trans('shopping_item_error', default='Error adding new item: ') + str(e), 'danger')
                        if added > 0:
                            items = list(db.shopping_items.find({'list_id': list_id}, session=mongo_session))
                            total_spent = sum(item['price'] * item['quantity'] for item in items)
                            db.shopping_lists.update_one(
                                {'_id': ObjectId(list_id)},
                                {'$set': {'total_spent': total_spent, 'updated_at': datetime.utcnow()}},
                                session=mongo_session
                            )
                            if current_user.is_authenticated and not is_admin():
                                if not deduct_ficore_credits(db, current_user.id, 1, 'add_shopping_items', list_id, mongo_session):
                                    flash(trans('shopping_credit_deduction_failed', default='Failed to deduct credits for adding items.'), 'danger')
                                    return redirect(url_for('shopping.main', tab='add-items', list_id=list_id))
                            get_shopping_lists.cache_clear()
                            flash(trans('shopping_items_added', default=f'{added} item(s) added successfully!'), 'success')
                            if total_spent > shopping_list['budget']:
                                flash(trans('shopping_over_budget', default='Warning: Total spent exceeds budget by ') + format_currency(total_spent - shopping_list['budget']) + '.', 'warning')
            except errors.WriteError as e:
                logger.error(f"Failed to save items for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                flash(trans('shopping_list_error', default='Error saving items due to validation failure.'), 'danger')
                return redirect(url_for('shopping.main', tab='add-items', list_id=list_id))
            except Exception as e:
                logger.error(f"Unexpected error saving items for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                flash(trans('shopping_list_error', default=f'Error saving items: {str(e)}'), 'danger')
                return redirect(url_for('shopping.main', tab='add-items', list_id=list_id))
            return redirect(url_for('shopping.main', tab='add-items', list_id=list_id))

        elif action == 'save_list':
            list_id = request.form.get('list_id')
            if not ObjectId.is_valid(list_id):
                flash(trans('shopping_invalid_list_id', default='Invalid list ID.'), 'danger')
                return redirect(url_for('shopping.main', tab='dashboard'))
            filter_criteria = {} if is_admin() else {'user_id': str(current_user.id)} if current_user.is_authenticated else {'session_id': session['sid']}
            shopping_list = db.shopping_lists.find_one({'_id': ObjectId(list_id), **filter_criteria})
            if not shopping_list:
                flash(trans('shopping_list_not_found', default='List not found.'), 'danger')
                return redirect(url_for('shopping.main', tab='dashboard'))
            existing_items = db.shopping_items.find({'list_id': list_id}, {'name': 1})
            existing_names = {item['name'].lower() for item in existing_items}
            new_items = []
            index = 0
            while f'items[{index}][name]' in request.form:
                new_name = request.form.get(f'items[{index}][name]', '').strip()
                if new_name:
                    new_items.append({
                        'name': new_name,
                        'quantity': request.form.get(f'items[{index}][quantity]', 1),
                        'price': request.form.get(f'items[{index}][price]', '0'),
                        'unit': request.form.get(f'items[{index}][unit]', 'piece'),
                        'category': request.form.get(f'items[{index}][category]', auto_categorize_item(new_name)),
                        'status': request.form.get(f'items[{index}][status]', 'to_buy'),
                        'store': request.form.get(f'items[{index}][store]', 'Unknown'),
                        'frequency': request.form.get(f'items[{index}][frequency]', 7)
                    })
                index += 1
            for item_data in new_items:
                if item_data['name'].lower() in existing_names:
                    flash(trans('shopping_duplicate_item_name', default='Item name already exists in this list.'), 'danger')
                    return redirect(url_for('shopping.main', tab='dashboard', list_id=list_id))
            added = 0
            session_id = session.get('sid', str(uuid.uuid4()))
            if not session.get('sid'):
                session['sid'] = session_id
                logger.debug(f"Assigned new session_id: {session_id}")
            try:
                with db.client.start_session() as mongo_session:
                    with mongo_session.start_transaction():
                        for item_data in new_items:
                            try:
                                new_quantity = int(item_data['quantity'])
                                new_price = float(clean_currency(item_data['price']))
                                new_unit = item_data['unit']
                                new_category = item_data['category']
                                new_status = item_data['status']
                                new_store = item_data['store']
                                new_frequency = int(item_data['frequency'])
                                if new_quantity < 1 or new_quantity > 1000 or new_price < 0 or new_price > 1000000 or new_frequency < 1 or new_frequency > 365:
                                    raise ValueError('Invalid input range')
                                new_item_data = {
                                    '_id': ObjectId(),
                                    'list_id': list_id,
                                    'user_id': str(current_user.id) if current_user.is_authenticated else None,
                                    'session_id': session_id,
                                    'name': item_data['name'],
                                    'quantity': new_quantity,
                                    'price': new_price,
                                    'unit': new_unit,
                                    'category': new_category,
                                    'status': new_status,
                                    'store': new_store,
                                    'frequency': new_frequency,
                                    'created_at': datetime.utcnow(),
                                    'updated_at': datetime.utcnow()
                                }
                                logger.debug(f"Inserting item_data: {new_item_data}", extra={'session_id': session_id})
                                db.shopping_items.insert_one(new_item_data, session=mongo_session)
                                added += 1
                                existing_names.add(item_data['name'].lower())
                            except ValueError as e:
                                flash(trans('shopping_item_error', default='Error adding item: ') + str(e), 'danger')
                        if added > 0:
                            items = list(db.shopping_items.find({'list_id': list_id}, session=mongo_session))
                            total_spent = sum(item['price'] * item['quantity'] for item in items)
                            db.shopping_lists.update_one(
                                {'_id': ObjectId(list_id)},
                                {'$set': {'total_spent': total_spent, 'updated_at': datetime.utcnow(), 'status': 'saved'}},
                                session=mongo_session
                            )
                            if current_user.is_authenticated and not is_admin():
                                if not deduct_ficore_credits(db, current_user.id, 1, 'save_shopping_list', list_id, mongo_session):
                                    flash(trans('shopping_credit_deduction_failed', default='Failed to deduct credits for saving list.'), 'danger')
                                    return redirect(url_for('shopping.main', tab='dashboard', list_id=list_id))
                            get_shopping_lists.cache_clear()
                            flash(trans('shopping_list_saved', default=f'{added} item(s) saved successfully!'), 'success')
                            if total_spent > shopping_list['budget']:
                                flash(trans('shopping_over_budget', default='Warning: Total spent exceeds budget by ') + format_currency(total_spent - shopping_list['budget']) + '.', 'warning')
            except errors.WriteError as e:
                logger.error(f"Failed to save list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                flash(trans('shopping_list_error', default='Error saving list due to validation failure.'), 'danger')
                return redirect(url_for('shopping.main', tab='dashboard', list_id=list_id))
            except Exception as e:
                logger.error(f"Unexpected error saving list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                flash(trans('shopping_list_error', default=f'Error saving list: {str(e)}'), 'danger')
                return redirect(url_for('shopping.main', tab='dashboard', list_id=list_id))
            return redirect(url_for('shopping.main', tab='dashboard', list_id=list_id))

        elif action == 'share_list' and share_form.validate_on_submit():
            list_id = request.form.get('list_id')
            if not ObjectId.is_valid(list_id):
                flash(trans('shopping_invalid_list_id', default='Invalid list ID.'), 'danger')
                return redirect(url_for('shopping.main', tab='view-lists'))
            shopping_list = db.shopping_lists.find_one({'_id': ObjectId(list_id), **filter_criteria})
            if not shopping_list:
                flash(trans('shopping_list_not_found', default='List not found.'), 'danger')
                return redirect(url_for('shopping.main', tab='view-lists'))
            collaborator = db.users.find_one({'email': share_form.email.data})
            if not collaborator:
                flash(trans('shopping_user_not_found', default='User with this email not found.'), 'danger')
                return redirect(url_for('shopping.main', tab='view-lists'))
            try:
                with db.client.start_session() as mongo_session:
                    with mongo_session.start_transaction():
                        db.shopping_lists.update_one(
                            {'_id': ObjectId(list_id)},
                            {'$addToSet': {'collaborators': share_form.email.data}, '$set': {'updated_at': datetime.utcnow()}},
                            session=mongo_session
                        )
                flash(trans('shopping_list_shared', default='List shared successfully!'), 'success')
            except errors.WriteError as e:
                logger.error(f"Error sharing list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session.get('sid', 'no-session-id')})
                flash(trans('shopping_share_error', default='Error sharing list due to validation failure.'), 'danger')
            except Exception as e:
                logger.error(f"Unexpected error sharing list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session.get('sid', 'no-session-id')})
                flash(trans('shopping_share_error', default='Error sharing list.'), 'danger')
            return redirect(url_for('shopping.main', tab='view-lists'))

        elif action == 'delete_list':
            list_id = request.form.get('list_id')
            if not ObjectId.is_valid(list_id):
                flash(trans('shopping_invalid_list_id', default='Invalid list ID.'), 'danger')
                return redirect(url_for('shopping.main', tab='view-lists'))
            shopping_list = db.shopping_lists.find_one({'_id': ObjectId(list_id), **filter_criteria})
            if not shopping_list:
                flash(trans('shopping_list_not_found', default='List not found.'), 'danger')
                return redirect(url_for('shopping.main', tab='view-lists'))
            if current_user.is_authenticated and not is_admin():
                if not check_ficore_credit_balance(required_amount=1, user_id=current_user.id):
                    flash(trans('shopping_insufficient_credits', default='Insufficient credits to delete list.'), 'danger')
                    return redirect(url_for('dashboard.index'))
            try:
                with db.client.start_session() as mongo_session:
                    with mongo_session.start_transaction():
                        db.shopping_items.delete_many({'list_id': list_id}, session=mongo_session)
                        db.shopping_lists.delete_one({'_id': ObjectId(list_id)}, session=mongo_session)
                        if current_user.is_authenticated and not is_admin():
                            if not deduct_ficore_credits(db, current_user.id, 1, 'delete_shopping_list', list_id, mongo_session):
                                flash(trans('shopping_credit_deduction_failed', default='Failed to deduct credits for deletion.'), 'danger')
                                return redirect(url_for('shopping.main', tab='view-lists'))
                if session.get('selected_list_id') == list_id:
                    session.pop('selected_list_id', None)
                flash(trans('shopping_list_deleted', default='List deleted successfully!'), 'success')
            except errors.WriteError as e:
                logger.error(f"Error deleting list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session.get('sid', 'no-session-id')})
                flash(trans('shopping_list_error', default='Error deleting list due to validation failure.'), 'danger')
            except Exception as e:
                logger.error(f"Unexpected error deleting list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session.get('sid', 'no-session-id')})
                flash(trans('shopping_list_error', default='Error deleting list.'), 'danger')
            return redirect(url_for('shopping.main', tab='view-lists'))

    lists_dict = {}
    for lst in lists.values():
        list_items = list(db.shopping_items.find({'list_id': str(lst['_id'])}))
        list_data = {
            'id': str(lst['_id']),
            'name': lst.get('name', ''),
            'budget_raw': float(lst.get('budget', 0.0)),
            'total_spent_raw': float(lst.get('total_spent', 0.0)),
            'status': lst.get('status', 'active'),
            'created_at': lst.get('created_at'),
            'collaborators': lst.get('collaborators', []),
            'items': [{
                'id': str(item['_id']),
                'name': item.get('name', ''),
                'quantity': int(item.get('quantity', 1)),
                'price_raw': float(item.get('price', 0.0)),
                'unit': item.get('unit', 'piece'),
                'category': item.get('category', 'other'),
                'status': item.get('status', 'to_buy'),
                'store': item.get('store', 'Unknown'),
                'frequency': int(item.get('frequency', 7))
            } for item in list_items]
        }
        lists_dict[list_data['id']] = list_data

    selected_list = lists_dict.get(selected_list_id, {'items': [], 'budget_raw': 0.0, 'total_spent_raw': 0.0})
    items = selected_list.get('items', [])
    insights = []
    if selected_list.get('budget_raw', 0.0) > 0:
        if selected_list['total_spent_raw'] > selected_list['budget_raw']:
            insights.append(trans('shopping_insight_over_budget', default='You are over budget. Consider removing non-essential items.'))
        elif selected_list['total_spent_raw'] < selected_list['budget_raw'] * 0.5:
            insights.append(trans('shopping_insight_under_budget', default='You are under budget. Consider allocating funds to savings.'))

    template_map = {
        'create-list': 'shopping/create_list.html',
        'add-items': 'shopping/add_items.html',
        'view-lists': 'shopping/view_lists.html',
        'manage-list': 'shopping/manage_list.html'
    }

    if active_tab == 'manage-list' and selected_list.get('name'):
        list_form.name.data = selected_list.get('name', '')
        list_form.budget.data = selected_list.get('budget_raw', 0.0)

    return render_template(
        template_map[active_tab],
        list_form=list_form,
        item_form=item_form,
        share_form=share_form,
        items_form=items_form,
        lists=lists_dict,
        selected_list=selected_list,
        selected_list_id=selected_list_id,
        items=items,
        categories=categories,
        tips=[
            trans('shopping_tip_plan_ahead', default='Plan your shopping list ahead to avoid impulse buys.'),
            trans('shopping_tip_compare_prices', default='Compare prices across stores to save money.'),
            trans('shopping_tip_bulk_buy', default='Buy non-perishable items in bulk to reduce costs.'),
            trans('shopping_tip_check_sales', default='Check for sales or discounts before shopping.')
        ],
        insights=insights,
        tool_title=trans('shopping_title', default='Shopping List Planner'),
        active_tab=active_tab
    )

@shopping_bp.route('/get_list_details', methods=['GET'])
@custom_login_required
@requires_role(['personal', 'admin'])
def get_list_details():
    db = get_mongo_db()
    list_id = request.args.get('list_id')
    tab = request.args.get('tab', 'manage-list')
    
    if not ObjectId.is_valid(list_id):
        return jsonify({'success': False, 'error': trans('shopping_invalid_list_id', default='Invalid list ID.')}), 400
    
    filter_criteria = {} if is_admin() else {'user_id': str(current_user.id)} if current_user.is_authenticated else {'session_id': session['sid']}
    shopping_list = db.shopping_lists.find_one({'_id': ObjectId(list_id), **filter_criteria})
    
    if not shopping_list:
        return jsonify({'success': False, 'error': trans('shopping_list_not_found', default='List not found.')}), 404
    
    list_items = list(db.shopping_items.find({'list_id': str(list_id)}))
    selected_list = {
        'id': str(shopping_list['_id']),
        'name': shopping_list.get('name', ''),
        'budget_raw': float(shopping_list.get('budget', 0.0)),
        'total_spent_raw': float(shopping_list.get('total_spent', 0.0)),
        'status': shopping_list.get('status', 'active'),
        'created_at': shopping_list.get('created_at'),
        'collaborators': shopping_list.get('collaborators', []),
        'items': [{
            'id': str(item['_id']),
            'name': item.get('name', ''),
            'quantity': int(item.get('quantity', 1)),
            'price_raw': float(item.get('price', 0.0)),
            'unit': item.get('unit', 'piece'),
            'category': item.get('category', 'other'),
            'status': item.get('status', 'to_buy'),
            'store': item.get('store', 'Unknown'),
            'frequency': int(item.get('frequency', 7))
        } for item in list_items]
    }
    
    try:
        html = render_template(
            'shopping/manage_list_details.html',
            list_form=ShoppingListForm(data={'name': selected_list['name'], 'budget': selected_list['budget_raw']}),
            item_form=ShoppingItemsForm(),
            selected_list=selected_list,
            selected_list_id=list_id,
            items=selected_list['items']
        )
        return jsonify({'success': True, 'html': html, 'items': selected_list['items']})
    except Exception as e:
        logger.error(f"Error rendering list details for {list_id}: {str(e)}")
        return jsonify({'success': False, 'error': trans('shopping_load_error', default='Failed to load list details.')}), 500

@shopping_bp.route('/lists/<list_id>/manage', methods=['GET', 'POST'])
@login_required
@requires_role(['personal', 'admin'])
def manage_list(list_id):
    db = get_mongo_db()
    filter_criteria = {'user_id': str(current_user.id)} if not is_admin() else {}
    try:
        if not ObjectId.is_valid(list_id):
            flash(trans('shopping_invalid_list_id', default='Invalid list ID.'), 'danger')
            return redirect(url_for('shopping.main', tab='manage-list'))
        shopping_list = db.shopping_lists.find_one({'_id': ObjectId(list_id), **filter_criteria})
        if not shopping_list:
            flash(trans('shopping_list_not_found', default='List not found.'), 'danger')
            return redirect(url_for('shopping.main', tab='manage-list'))

        if request.method == 'POST':
            action = request.form.get('action')
            session_id = session.get('sid', str(uuid.uuid4()))
            if not session.get('sid'):
                session['sid'] = session_id
                logger.debug(f"Assigned new session_id: {session_id}")
            if action == 'save_list_changes':
                new_name = request.form.get('list_name', shopping_list['name']).strip()
                new_budget_str = request.form.get('list_budget', str(shopping_list['budget']))
                try:
                    new_budget = float(clean_currency(new_budget_str))
                    if new_budget < 0 or new_budget > 10000000000:
                        raise ValueError
                except ValueError:
                    flash(trans('shopping_budget_invalid', default='Invalid budget value.'), 'danger')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                existing_items = db.shopping_items.find({'list_id': list_id}, {'name': 1})
                existing_names = {item['name'].lower() for item in existing_items}
                new_items = []
                for i in range(1, 6):
                    new_item_name = request.form.get(f'new_item_name_{i}', '').strip()
                    if new_item_name:
                        new_items.append({
                            'name': new_item_name,
                            'quantity': request.form.get(f'new_item_quantity_{i}', 1),
                            'price': request.form.get(f'new_item_price_{i}', '0'),
                            'unit': request.form.get(f'new_item_unit_{i}', 'piece'),
                            'category': request.form.get(f'new_item_category_{i}', auto_categorize_item(new_item_name)),
                            'status': request.form.get(f'new_item_status_{i}', 'to_buy'),
                            'store': request.form.get(f'new_item_store_{i}', 'Unknown'),
                            'frequency': request.form.get(f'new_item_frequency_{i}', 7)
                        })
                for item_data in new_items:
                    if item_data['name'].lower() in existing_names:
                        flash(trans('shopping_duplicate_item_name', default='Item name already exists in this list.'), 'danger')
                        return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                added = 0
                edited = 0
                deleted = 0
                try:
                    with db.client.start_session() as mongo_session:
                        with mongo_session.start_transaction():
                            new_status = 'saved' if request.form.get('save_list') else shopping_list.get('status', 'active')
                            if shopping_list['status'] == 'saved' and new_status == 'active':
                                flash(trans('shopping_invalid_status_transition', default='Cannot change saved list back to active.'), 'danger')
                                return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                            if request.form.get('item_id'):
                                item_id = request.form.get('item_id')
                                if ObjectId.is_valid(item_id):
                                    db.shopping_items.delete_one({'_id': ObjectId(item_id), 'list_id': list_id}, session=mongo_session)
                                    deleted += 1
                            if request.form.get('edit_item_id'):
                                item_id = request.form.get('edit_item_id')
                                if ObjectId.is_valid(item_id):
                                    new_item_data = {
                                        'name': request.form.get('edit_item_name', '').strip(),
                                        'quantity': int(request.form.get('edit_item_quantity', 1)),
                                        'price': float(clean_currency(request.form.get('edit_item_price', '0'))),
                                        'unit': request.form.get('edit_item_unit', 'piece'),
                                        'category': request.form.get('edit_item_category', 'other'),
                                        'status': request.form.get('edit_item_status', 'to_buy'),
                                        'store': request.form.get('edit_item_store', 'Unknown'),
                                        'frequency': int(request.form.get('edit_item_frequency', 7)),
                                        'updated_at': datetime.utcnow()
                                    }
                                    if new_item_data['name'].lower() in existing_names and new_item_data['name'].lower() != db.shopping_items.find_one({'_id': ObjectId(item_id), 'list_id': list_id}, {'name': 1})['name'].lower():
                                        flash(trans('shopping_duplicate_item_name', default='Item name already exists in this list.'), 'danger')
                                        return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                                    if new_item_data['quantity'] < 1 or new_item_data['quantity'] > 1000 or new_item_data['price'] < 0 or new_item_data['price'] > 1000000 or new_item_data['frequency'] < 1 or new_item_data['frequency'] > 365:
                                        flash(trans('shopping_item_error', default='Invalid input range for edited item.'), 'danger')
                                        return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                                    db.shopping_items.update_one(
                                        {'_id': ObjectId(item_id), 'list_id': list_id},
                                        {'$set': new_item_data},
                                        session=mongo_session
                                    )
                                    edited += 1
                            for item_data in new_items:
                                try:
                                    new_quantity = int(item_data['quantity'])
                                    new_price = float(clean_currency(item_data['price']))
                                    new_unit = item_data['unit']
                                    new_category = item_data['category']
                                    new_status = item_data['status']
                                    new_store = item_data['store']
                                    new_frequency = int(item_data['frequency'])
                                    if new_quantity < 1 or new_quantity > 1000 or new_price < 0 or new_price > 1000000 or new_frequency < 1 or new_frequency > 365:
                                        raise ValueError('Invalid input range')
                                    new_item_data = {
                                        '_id': ObjectId(),
                                        'list_id': list_id,
                                        'user_id': str(current_user.id) if current_user.is_authenticated else None,
                                        'session_id': session_id,
                                        'name': item_data['name'],
                                        'quantity': new_quantity,
                                        'price': new_price,
                                        'unit': new_unit,
                                        'category': new_category,
                                        'status': new_status,
                                        'store': new_store,
                                        'frequency': new_frequency,
                                        'created_at': datetime.utcnow(),
                                        'updated_at': datetime.utcnow()
                                    }
                                    logger.debug(f"Inserting item_data: {new_item_data}", extra={'session_id': session_id})
                                    db.shopping_items.insert_one(new_item_data, session=mongo_session)
                                    added += 1
                                    existing_names.add(item_data['name'].lower())
                                except ValueError as e:
                                    flash(trans('shopping_item_error', default='Error adding new item: ') + str(e), 'danger')
                            items = list(db.shopping_items.find({'list_id': list_id}, session=mongo_session))
                            total_spent = sum(item['price'] * item['quantity'] for item in items)
                            db.shopping_lists.update_one(
                                {'_id': ObjectId(list_id)},
                                {
                                    '$set': {
                                        'name': new_name,
                                        'budget': new_budget,
                                        'total_spent': total_spent,
                                        'updated_at': datetime.utcnow(),
                                        'status': new_status
                                    }
                                },
                                session=mongo_session
                            )
                            total_operations = added + edited + deleted
                            required_credits = 1 if total_operations > 0 else 0
                            if current_user.is_authenticated and not is_admin() and required_credits > 0:
                                if not check_ficore_credit_balance(required_amount=required_credits, user_id=current_user.id):
                                    flash(trans('shopping_insufficient_credits', default='Insufficient credits to save changes.'), 'danger')
                                    return redirect(url_for('dashboard.index'))
                                if not deduct_ficore_credits(db, current_user.id, required_credits, 'save_shopping_list_changes', list_id, mongo_session):
                                    flash(trans('shopping_credit_deduction_failed', default='Failed to deduct credits for changes.'), 'danger')
                                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                            get_shopping_lists.cache_clear()
                    flash(trans('shopping_changes_saved', default='Changes saved successfully!'), 'success')
                    if total_spent > new_budget and new_budget > 0:
                        flash(trans('shopping_over_budget', default='Warning: Total spent exceeds budget by ') + format_currency(total_spent - new_budget) + '.', 'warning')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                except errors.WriteError as e:
                    logger.error(f"Failed to save changes for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                    flash(trans('shopping_list_error', default='Error saving changes due to validation failure.'), 'danger')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                except Exception as e:
                    logger.error(f"Unexpected error saving changes for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                    flash(trans('shopping_list_error', default=f'Error saving changes: {str(e)}'), 'danger')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
            elif action == 'delete_item':
                item_id = request.form.get('item_id')
                if not ObjectId.is_valid(item_id):
                    flash(trans('shopping_invalid_item_id', default='Invalid item ID.'), 'danger')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                try:
                    with db.client.start_session() as mongo_session:
                        with mongo_session.start_transaction():
                            db.shopping_items.delete_one({'_id': ObjectId(item_id), 'list_id': list_id}, session=mongo_session)
                            items = list(db.shopping_items.find({'list_id': list_id}, session=mongo_session))
                            total_spent = sum(item['price'] * item['quantity'] for item in items)
                            db.shopping_lists.update_one(
                                {'_id': ObjectId(list_id)},
                                {'$set': {'total_spent': total_spent, 'updated_at': datetime.utcnow()}},
                                session=mongo_session
                            )
                            if current_user.is_authenticated and not is_admin():
                                if not deduct_ficore_credits(db, current_user.id, 1, 'delete_shopping_item', item_id, mongo_session):
                                    flash(trans('shopping_credit_deduction_failed', default='Failed to deduct credits for item deletion.'), 'danger')
                                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                            get_shopping_lists.cache_clear()
                    flash(trans('shopping_item_deleted', default='Item deleted successfully!'), 'success')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                except errors.WriteError as e:
                    logger.error(f"Error deleting item {item_id} for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                    flash(trans('shopping_list_error', default='Error deleting item due to validation failure.'), 'danger')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))
                except Exception as e:
                    logger.error(f"Unexpected error deleting item {item_id} for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
                    flash(trans('shopping_list_error', default=f'Error deleting item: {str(e)}'), 'danger')
                    return redirect(url_for('shopping.main', tab='manage-list', list_id=list_id))

        lists = list(db.shopping_items.find({'list_id': str(lst['_id'])}))
        lists_dict = {}
        for lst in lists:
            list_items = list(db.shopping_items.find({'list_id': str(lst['_id'])}))
            list_data = {
                'id': str(lst['_id']),
                'name': lst.get('name', ''),
                'budget_raw': float(lst.get('budget', 0.0)),
                'total_spent_raw': float(lst.get('total_spent', 0.0)),
                'status': lst.get('status', 'active'),
                'created_at': lst.get('created_at'),
                'collaborators': lst.get('collaborators', []),
                'items': [{
                    'id': str(item['_id']),
                    'name': item.get('name', ''),
                    'quantity': int(item.get('quantity', 1)),
                    'price_raw': float(item.get('price', 0.0)),
                    'unit': item.get('unit', 'piece'),
                    'category': item.get('category', 'other'),
                    'status': item.get('status', 'to_buy'),
                    'store': item.get('store', 'Unknown'),
                    'frequency': int(item.get('frequency', 7))
                } for item in list_items]
            }
            lists_dict[list_data['id']] = list_data
        selected_list = lists_dict.get(list_id, {'items': [], 'budget_raw': 0.0, 'total_spent_raw': 0.0})
        items = selected_list['items']
        categories = {
            trans('shopping_category_fruits', default='Fruits'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'fruits'),
            trans('shopping_category_vegetables', default='Vegetables'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'vegetables'),
            trans('shopping_category_dairy', default='Dairy'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'dairy'),
            trans('shopping_category_meat', default='Meat'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'meat'),
            trans('shopping_category_grains', default='Grains'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'grains'),
            trans('shopping_category_beverages', default='Beverages'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'beverages'),
            trans('shopping_category_household', default='Household'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'household'),
            trans('shopping_category_other', default='Other'): sum(item['price_raw'] * item['quantity'] for item in items if item['category'] == 'other')
        }
        categories = {k: v for k, v in categories.items() if v > 0}

        tips = [
            trans('shopping_tip_plan_ahead', default='Plan your shopping ahead to avoid impulse buys.'),
            trans('shopping_tip_compare_prices', default='Compare prices across stores to save money.'),
            trans('shopping_tip_bulk_buy', default='Buy non-perishables in bulk to reduce costs.'),
            trans('shopping_tip_check_sales', default='Check for sales or discounts before shopping.')
        ]
        insights = []
        if selected_list.get('budget_raw', 0.0) > 0:
            if selected_list['total_spent_raw'] > selected_list['budget_raw']:
                insights.append(trans('shopping_insight_over_budget', default='You are over budget. Consider removing non-essential items.'))
            elif selected_list['total_spent_raw'] < selected_list['budget_raw'] * 0.5:
                insights.append(trans('shopping_insight_under_budget', default='You are under budget. Consider allocating funds to savings.'))

        return render_template(
            'shopping/manage_list.html',
            list_form=ShoppingListForm(data={'name': selected_list['name'], 'budget': selected_list['budget_raw']}),
            item_form=ShoppingItemsForm(),
            share_form=ShareListForm(),
            lists=lists_dict,
            selected_list=selected_list,
            selected_list_id=list_id,
            items=items,
            categories=categories,
            tips=tips,
            insights=insights,
            tool_title=trans('shopping_title', default='Shopping List Planner'),
            active_tab='manage-list'
        )
    except Exception as e:
        logger.error(f"Error managing list {list_id}: {str(e)}")
        flash(trans('shopping_list_error', default='Error loading list.'), 'danger')
        return redirect(url_for('shopping.main', tab='manage-list'))

@shopping_bp.route('/lists/<list_id>/export_pdf', methods=['GET'])
@login_required
@requires_role(['personal', 'admin'])
def export_list_pdf(list_id):
    db = get_mongo_db()
    session_id = session.get('sid', str(uuid.uuid4()))
    if not session.get('sid'):
        session['sid'] = session_id
        logger.debug(f"Assigned new session_id: {session_id}")
    try:
        if not ObjectId.is_valid(list_id):
            flash(trans('shopping_invalid_list_id', default='Invalid list ID.'), 'danger')
            return redirect(url_for('shopping.main', tab='view-lists'))
        shopping_list = db.shopping_lists.find_one({'_id': ObjectId(list_id), 'user_id': str(current_user.id)})
        if not shopping_list:
            flash(trans('shopping_list_not_found', default='List not found.'), 'danger')
            return redirect(url_for('shopping.main', tab='view-lists'))
        if shopping_list.get('status') != 'saved':
            flash(trans('shopping_list_not_saved', default='List must be saved before exporting.'), 'danger')
            return redirect(url_for('shopping.main', tab='view-lists'))
        if current_user.is_authenticated and not is_admin():
            if not check_ficore_credit_balance(required_amount=2, user_id=current_user.id):
                flash(trans('shopping_insufficient_credits', default='Insufficient credits to export PDF.'), 'danger')
                return redirect(url_for('dashboard.index'))
        items = db.shopping_items.find({'list_id': str(list_id)}).sort('created_at', -1)
        shopping_data = {
            'lists': [{
                'name': shopping_list.get('name', ''),
                'budget': float(shopping_list.get('budget', 0)),
                'total_spent': float(shopping_list.get('total_spent', 0)),
                'collaborators': shopping_list.get('collaborators', []),
                'created_at': shopping_list.get('created_at')
            }],
            'items': [{
                'name': item.get('name', ''),
                'quantity': int(item.get('quantity', 1)),
                'price': float(item.get('price', 0)),
                'unit': item.get('unit', 'piece'),
                'category': item.get('category', 'other'),
                'status': item.get('status', 'to_buy'),
                'store': item.get('store', 'Unknown'),
                'created_at': item.get('created_at')
            } for item in items]
        }
        try:
            with db.client.start_session() as mongo_session:
                with mongo_session.start_transaction():
                    buffer = BytesIO()
                    p = canvas.Canvas(buffer, pagesize=A4)
                    header_height = 0.7
                    extra_space = 0.2
                    row_height = 0.3
                    bottom_margin = 0.5
                    max_y = 10.5
                    title_y = max_y - header_height - extra_space
                    page_height = (max_y - bottom_margin) * inch
                    rows_per_page = int((page_height - (title_y - 0.6) * inch) / (row_height * inch))
                    total_budget = float(shopping_data['lists'][0]['budget'])
                    total_spent = float(shopping_data['lists'][0]['total_spent'])
                    total_price = sum(float(item['price']) * item['quantity'] for item in shopping_data['items'])
                    def draw_list_headers(y):
                        p.setFillColor(colors.black)
                        p.drawString(1 * inch, y * inch, trans('general_date', default='Date'))
                        p.drawString(2 * inch, y * inch, trans('general_list_name', default='List Name'))
                        p.drawString(3.5 * inch, y * inch, trans('general_budget', default='Budget'))
                        p.drawString(4.5 * inch, y * inch, trans('general_total_spent', default='Total Spent'))
                        p.drawString(5.5 * inch, y * inch, trans('general_collaborators', default='Collaborators'))
                        return y - row_height
                    def draw_item_headers(y):
                        p.setFillColor(colors.black)
                        p.drawString(1 * inch, y * inch, trans('general_date', default='Date'))
                        p.drawString(2 * inch, y * inch, trans('general_item_name', default='Item Name'))
                        p.drawString(3 * inch, y * inch, trans('general_quantity', default='Quantity'))
                        p.drawString(3.8 * inch, y * inch, trans('general_price', default='Price'))
                        p.drawString(4.5 * inch, y * inch, trans('general_unit', default='Unit'))
                        p.drawString(5.2 * inch, y * inch, trans('general_status', default='Status'))
                        p.drawString(5.9 * inch, y * inch, trans('general_category', default='Category'))
                        p.drawString(6.6 * inch, y * inch, trans('general_store', default='Store'))
                        return y - row_height
                    draw_ficore_pdf_header(p, current_user, y_start=max_y)
                    p.setFont("Helvetica", 12)
                    p.drawString(1 * inch, title_y * inch, trans('shopping_list_report', default='Shopping List Report'))
                    p.drawString(1 * inch, (title_y - 0.3) * inch, f"{trans('reports_generated_on', default='Generated on')}: {format_date(datetime.utcnow())}")
                    y = title_y - 0.6
                    p.setFont("Helvetica", 10)
                    y = draw_list_headers(y)
                    row_count = 0
                    list_data = shopping_data['lists'][0]
                    p.drawString(1 * inch, y * inch, format_date(list_data['created_at']))
                    p.drawString(2 * inch, y * inch, list_data['name'])
                    p.drawString(3.5 * inch, y * inch, format_currency(list_data['budget']))
                    p.drawString(4.5 * inch, y * inch, format_currency(list_data['total_spent']))
                    p.drawString(5.5 * inch, y * inch, ', '.join(list_data['collaborators']) or 'None')
                    y -= row_height
                    row_count += 1
                    y -= 0.5
                    p.drawString(1 * inch, y * inch, trans('shopping_items', default='Items'))
                    y -= row_height
                    y = draw_item_headers(y)
                    for item in shopping_data['items']:
                        if row_count + 1 >= rows_per_page:
                            p.showPage()
                            draw_ficore_pdf_header(p, current_user, y_start=max_y)
                            y = title_y - 0.6
                            y = draw_item_headers(y)
                            row_count = 0
                        p.drawString(1 * inch, y * inch, format_date(item['created_at']))
                        p.drawString(2 * inch, y * inch, item['name'][:20])
                        p.drawString(3 * inch, y * inch, str(item['quantity']))
                        p.drawString(3.8 * inch, y * inch, format_currency(item['price']))
                        p.drawString(4.5 * inch, y * inch, trans(item['unit'], default=item['unit']))
                        p.drawString(5.2 * inch, y * inch, trans(item['status'], default=item['status']))
                        p.drawString(5.9 * inch, y * inch, trans(item['category'], default=item['category']))
                        p.drawString(6.6 * inch, y * inch, item['store'][:15])
                        y -= row_height
                        row_count += 1
                    if row_count + 3 <= rows_per_page:
                        y -= row_height
                        p.drawString(1 * inch, y * inch, f"{trans('reports_total_budget', default='Total Budget')}: {format_currency(total_budget)}")
                        y -= row_height
                        p.drawString(1 * inch, y * inch, f"{trans('reports_total_spent', default='Total Spent')}: {format_currency(total_spent)}")
                        y -= row_height
                        p.drawString(1 * inch, y * inch, f"{trans('reports_total_price', default='Total Price')}: {format_currency(total_price)}")
                    else:
                        p.showPage()
                        draw_ficore_pdf_header(p, current_user, y_start=max_y)
                        y = title_y - 0.6
                        p.drawString(1 * inch, y * inch, f"{trans('reports_total_budget', default='Total Budget')}: {format_currency(total_budget)}")
                        y -= row_height
                        p.drawString(1 * inch, y * inch, f"{trans('reports_total_spent', default='Total Spent')}: {format_currency(total_spent)}")
                        y -= row_height
                        p.drawString(1 * inch, y * inch, f"{trans('reports_total_price', default='Total Price')}: {format_currency(total_price)}")
                    p.save()
                    buffer.seek(0)
                    if current_user.is_authenticated and not is_admin():
                        if not deduct_ficore_credits(db, current_user.id, 2, 'export_shopping_list_pdf', list_id, mongo_session):
                            flash(trans('shopping_credit_deduction_failed', default='Failed to deduct credits for PDF export.'), 'danger')
                            return redirect(url_for('shopping.main', tab='view-lists'))
            return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': f'attachment;filename=shopping_list_{list_id}.pdf'})
        except errors.WriteError as e:
            logger.error(f"Error exporting PDF for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
            flash(trans('shopping_export_error', default='Error exporting to PDF due to validation failure.'), 'danger')
            return redirect(url_for('shopping.main', tab='view-lists'))
        except Exception as e:
            logger.error(f"Unexpected error exporting PDF for list {list_id}: {str(e)}", exc_info=True, extra={'session_id': session_id})
            flash(trans('shopping_export_error', default='Error exporting to PDF.'), 'danger')
            return redirect(url_for('shopping.main', tab='view-lists'))
    except Exception as e:
        logger.error(f"Error accessing list {list_id} for PDF export: {str(e)}", exc_info=True, extra={'session_id': session_id})
        flash(trans('shopping_list_error', default='Error loading list.'), 'danger')
        return redirect(url_for('shopping.main', tab='view-lists'))

@shopping_bp.errorhandler(CSRFError)
def handle_csrf_error(e):
    logger.error(f"CSRF error on {request.path}: {e.description}")
    flash(trans('shopping_csrf_error', default='Form submission failed. Please refresh and try again.'), 'danger')
    return redirect(url_for('shopping.main', tab='create-list')), 404

def init_app(app):
    try:
        csrf.init_app(app)
        db = get_mongo_db()
        db.shopping_lists.create_index([('user_id', 1), ('status', 1), ('updated_at
