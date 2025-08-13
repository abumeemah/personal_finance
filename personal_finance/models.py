from datetime import datetime, timedelta
from pymongo import ASCENDING, DESCENDING
import os
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError, DuplicateKeyError, OperationFailure, WriteError
from werkzeug.security import generate_password_hash
from bson import ObjectId
import logging
from translations import trans
from utils import get_mongo_db, logger
from functools import lru_cache
import traceback
import time
import uuid

# Configure logger for the application
logger = logging.getLogger('ficore_app')
logger.setLevel(logging.INFO)

def get_db():
    """
    Get MongoDB database connection using the global client from utils.py.
    
    Returns:
        Database object
    """
    try:
        db = get_mongo_db()
        logger.info(f"Successfully connected to MongoDB database: {db.name}", extra={'session_id': 'no-session-id'})
        return db
    except Exception as e:
        logger.error(f"Error connecting to database: {str(e)}", exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def initialize_app_data(app):
    """
    Initialize MongoDB collections and indexes.
    
    Args:
        app: Flask application instance
    """
    max_retries = 3
    retry_delay = 1
    
    with app.app_context():
        for attempt in range(max_retries):
            try:
                db = get_db()
                db.command('ping')
                logger.info(f"Attempt {attempt + 1}/{max_retries} - {trans('general_database_connection_established', default='MongoDB connection established')}", 
                           extra={'session_id': 'no-session-id'})
                break
            except Exception as e:
                logger.error(f"Failed to initialize database (attempt {attempt + 1}/{max_retries}): {str(e)}", 
                            exc_info=True, extra={'session_id': 'no-session-id'})
                if attempt == max_retries - 1:
                    raise RuntimeError(trans('general_database_connection_failed', default='MongoDB connection failed after max retries'))
                time.sleep(retry_delay)
        
        try:
            db_instance = get_db()
            logger.info(f"MongoDB database: {db_instance.name}", extra={'session_id': 'no-session-id'})
            collections = db_instance.list_collection_names()
            
            # Define collection schemas
            collection_schemas = {
                'users': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['_id', 'email', 'password_hash', 'role'],
                            'properties': {
                                '_id': {'bsonType': 'string'},
                                'email': {'bsonType': 'string', 'pattern': r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'},
                                'password_hash': {'bsonType': 'string'},
                                'role': {'enum': ['personal', 'trader', 'agent', 'admin']},
                                'coin_balance': {'bsonType': 'int', 'minimum': 0},
                                'ficore_credit_balance': {'bsonType': 'int', 'minimum': 0},
                                'language': {'enum': ['en', 'ha']},
                                'created_at': {'bsonType': 'date'},
                                'display_name': {'bsonType': ['string', 'null']},
                                'is_admin': {'bsonType': 'bool'},
                                'setup_complete': {'bsonType': 'bool'},
                                'reset_token': {'bsonType': ['string', 'null']},
                                'reset_token_expiry': {'bsonType': ['date', 'null']},
                                'otp': {'bsonType': ['string', 'null']},
                                'otp_expiry': {'bsonType': ['date', 'null']},
                                'business_details': {
                                    'bsonType': ['object', 'null'],
                                    'properties': {
                                        'name': {'bsonType': 'string'},
                                        'address': {'bsonType': 'string'},
                                        'industry': {'bsonType': 'string'},
                                        'products_services': {'bsonType': 'string'},
                                        'phone_number': {'bsonType': 'string'}
                                    }
                                },
                                'personal_details': {
                                    'bsonType': ['object', 'null'],
                                    'properties': {
                                        'first_name': {'bsonType': 'string'},
                                        'last_name': {'bsonType': 'string'},
                                        'phone_number': {'bsonType': 'string'},
                                        'address': {'bsonType': 'string'}
                                    }
                                },
                                'agent_details': {
                                    'bsonType': ['object', 'null'],
                                    'properties': {
                                        'agent_name': {'bsonType': 'string'},
                                        'agent_id': {'bsonType': 'string'},
                                        'area': {'bsonType': 'string'},
                                        'role': {'bsonType': 'string'},
                                        'email': {'bsonType': 'string'},
                                        'phone': {'bsonType': 'string'}
                                    }
                                }
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('email', ASCENDING)], 'unique': True},
                        {'key': [('reset_token', ASCENDING)], 'sparse': True},
                        {'key': [('role', ASCENDING)]}
                    ]
                },
                'records': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'type', 'name', 'amount_owed'],
                            'properties': {
                                'user_id': {'bsonType': 'string'},
                                'type': {'enum': ['debtor', 'creditor']},
                                'name': {'bsonType': 'string'},
                                'contact': {'bsonType': ['string', 'null']},
                                'amount_owed': {'bsonType': 'number', 'minimum': 0},
                                'description': {'bsonType': ['string', 'null']},
                                'reminder_count': {'bsonType': 'int', 'minimum': 0},
                                'created_at': {'bsonType': 'date'},
                                'updated_at': {'bsonType': ['date', 'null']}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING), ('type', ASCENDING)]},
                        {'key': [('created_at', DESCENDING)]}
                    ]
                },
                'cashflows': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'type', 'party_name', 'amount'],
                            'properties': {
                                'user_id': {'bsonType': 'string'},
                                'type': {'enum': ['receipt', 'payment']},
                                'party_name': {'bsonType': 'string'},
                                'amount': {'bsonType': 'number', 'minimum': 0},
                                'method': {'bsonType': ['string', 'null']},
                                'category': {'bsonType': ['string', 'null']},
                                'created_at': {'bsonType': 'date'},
                                'updated_at': {'bsonType': ['date', 'null']}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING), ('type', ASCENDING)]},
                        {'key': [('created_at', DESCENDING)]}
                    ]
                },
                'ficore_credit_transactions': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'amount', 'type', 'date'],
                            'properties': {
                                'user_id': {'bsonType': 'string'},
                                'amount': {'bsonType': ['int', 'double']},
                                'type': {'enum': ['add', 'spend', 'purchase', 'admin_credit', 'create_shopping_list', 'initial_credit']},
                                'ref': {'bsonType': ['string', 'null']},
                                'date': {'bsonType': 'date'},
                                'facilitated_by_agent': {'bsonType': ['string', 'null']},
                                'payment_method': {'bsonType': ['string', 'null']},
                                'cash_amount': {'bsonType': ['number', 'null']},
                                'notes': {'bsonType': ['string', 'null']}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING)]},
                        {'key': [('date', DESCENDING)]}
                    ]
                },
                'credit_requests': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'amount', 'payment_method', 'status', 'created_at'],
                            'properties': {
                                'user_id': {'bsonType': 'string'},
                                'amount': {'bsonType': 'int', 'minimum': 1},
                                'payment_method': {'enum': ['card', 'cash', 'bank']},
                                'receipt_file_id': {'bsonType': ['objectId', 'null']},
                                'status': {'enum': ['pending', 'approved', 'denied']},
                                'created_at': {'bsonType': 'date'},
                                'updated_at': {'bsonType': ['date', 'null']},
                                'admin_id': {'bsonType': ['string', 'null']}
                            }
                        }
                    },
                    'indexes': [
                        [{'key': [('user_id', ASCENDING)]},
                         {'key': [('status', ASCENDING)]},
                         {'key': [('created_at', DESCENDING)]}]
                    ]
                },
                'audit_logs': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['admin_id', 'action', 'timestamp'],
                            'properties': {
                                'admin_id': {'bsonType': 'string'},
                                'action': {'bsonType': 'string'},
                                'details': {'bsonType': ['object', 'null']},
                                'timestamp': {'bsonType': 'date'}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('admin_id', ASCENDING)]},
                        {'key': [('timestamp', DESCENDING)]}
                    ]
                },
                'agents': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['_id', 'status', 'created_at'],
                            'properties': {
                                '_id': {'bsonType': 'string', 'pattern': r'^[A-Z0-9]{8}$'},
                                'status': {'enum': ['active', 'inactive']},
                                'created_at': {'bsonType': 'date'},
                                'updated_at': {'bsonType': ['date', 'null']}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('status', ASCENDING)]}
                    ]
                },
                'shopping_items': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'session_id', 'list_id', 'name', 'quantity', 'price', 'category', 'status', 'created_at', 'updated_at', 'unit'],
                            'properties': {
                                'user_id': {'bsonType': ['string', 'null']},
                                'session_id': {'bsonType': 'string'},
                                'list_id': {'bsonType': 'string'},
                                'name': {'bsonType': 'string'},
                                'quantity': {'bsonType': 'int', 'minimum': 1},
                                'price': {'bsonType': 'double', 'minimum': 0},
                                'category': {'enum': ['fruits', 'vegetables', 'dairy', 'meat', 'grains', 'beverages', 'household', 'other']},
                                'status': {'enum': ['to_buy', 'bought']},
                                'created_at': {'bsonType': 'date'},
                                'updated_at': {'bsonType': 'date'},
                                'store': {'bsonType': ['string', 'null']},
                                'frequency': {'bsonType': 'int', 'minimum': 1},
                                'unit': {'bsonType': 'string', 'enum': ['piece', 'kg', 'liter', 'pack', 'unit', 'other']}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING), ('list_id', ASCENDING)]},
                        {'key': [('created_at', DESCENDING)]}
                    ]
                },
                'shopping_lists': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['name', 'session_id', 'budget', 'created_at', 'updated_at', 'total_spent', 'status', 'items'],
                            'properties': {
                                'name': {'bsonType': 'string'},
                                'user_id': {'bsonType': ['string', 'null']},
                                'session_id': {'bsonType': 'string'},
                                'budget': {'bsonType': 'double', 'minimum': 0},
                                'created_at': {'bsonType': 'date'},
                                'updated_at': {'bsonType': 'date'},
                                'collaborators': {
                                    'bsonType': 'array',
                                    'items': {'bsonType': 'string'}
                                },
                                'total_spent': {'bsonType': 'double', 'minimum': 0},
                                'status': {'enum': ['active', 'saved']},
                                'items': {
                                    'bsonType': 'array',
                                    'items': {
                                        'bsonType': 'object',
                                        'required': ['name', 'quantity', 'price', 'category', 'status', 'created_at', 'updated_at'],
                                        'properties': {
                                            'name': {'bsonType': 'string'},
                                            'quantity': {'bsonType': 'int', 'minimum': 1},
                                            'price': {'bsonType': 'double', 'minimum': 0},
                                            'category': {'enum': ['fruits', 'vegetables', 'dairy', 'meat', 'grains', 'beverages', 'household', 'other']},
                                            'status': {'enum': ['to_buy', 'bought']},
                                            'created_at': {'bsonType': 'date'},
                                            'updated_at': {'bsonType': 'date'},
                                            'store': {'bsonType': ['string', 'null']},
                                            'frequency': {'bsonType': 'int', 'minimum': 1},
                                            'unit': {'bsonType': 'string', 'enum': ['piece', 'kg', 'liter', 'pack', 'unit', 'other']}
                                        }
                                    }
                                }
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING), ('status', ASCENDING), ('updated_at', DESCENDING)]},
                        {'key': [('session_id', ASCENDING), ('status', ASCENDING), ('updated_at', DESCENDING)]}
                    ]
                },
                'feedback': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['tool_name', 'rating', 'timestamp'],
                            'properties': {
                                'user_id': {'bsonType': ['string', 'null']},
                                'session_id': {'bsonType': ['string', 'null']},
                                'tool_name': {'bsonType': 'string'},
                                'rating': {'bsonType': 'int', 'minimum': 1, 'maximum': 5},
                                'comment': {'bsonType': ['string', 'null']},
                                'timestamp': {'bsonType': 'date'}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING)], 'sparse': True},
                        {'key': [('session_id', ASCENDING)]},
                        {'key': [('timestamp', DESCENDING)]}
                    ]
                },
                'tool_usage': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['tool_name', 'timestamp'],
                            'properties': {
                                'tool_name': {'bsonType': 'string'},
                                'user_id': {'bsonType': ['string', 'null']},
                                'session_id': {'bsonType': ['string', 'null']},
                                'action': {'bsonType': ['string', 'null']},
                                'timestamp': {'bsonType': 'date'}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('tool_name', ASCENDING)]},
                        {'key': [('timestamp', DESCENDING)]}
                    ]
                },
                'budgets': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'income', 'fixed_expenses', 'variable_expenses', 'created_at'],
                            'properties': {
                                'user_id': {'bsonType': 'string'},
                                'session_id': {'bsonType': ['string', 'null']},
                                'income': {'bsonType': 'number', 'minimum': 0},
                                'fixed_expenses': {'bsonType': 'number', 'minimum': 0},
                                'variable_expenses': {'bsonType': 'number', 'minimum': 0},
                                'savings_goal': {'bsonType': 'number', 'minimum': 0},
                                'surplus_deficit': {'bsonType': 'number'},
                                'housing': {'bsonType': 'number', 'minimum': 0},
                                'food': {'bsonType': 'number', 'minimum': 0},
                                'transport': {'bsonType': 'number', 'minimum': 0},
                                'dependents': {'bsonType': 'number', 'minimum': 0},
                                'miscellaneous': {'bsonType': 'number', 'minimum': 0},
                                'others': {'bsonType': 'number', 'minimum': 0},
                                'created_at': {'bsonType': 'date'}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING), ('created_at', DESCENDING)]},
                        {'key': [('session_id', ASCENDING), ('created_at', DESCENDING)]}
                    ]
                },
                'bills': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'bill_name', 'amount', 'due_date', 'status'],
                            'properties': {
                                'user_id': {'bsonType': 'string'},
                                'session_id': {'bsonType': ['string', 'null']},
                                'bill_name': {'bsonType': 'string'},
                                'amount': {'bsonType': 'number', 'minimum': 0},
                                'due_date': {'bsonType': 'date'},
                                'frequency': {'bsonType': ['string', 'null']},
                                'category': {'bsonType': ['string', 'null']},
                                'status': {'enum': ['pending', 'paid', 'overdue']},
                                'send_notifications': {'bsonType': 'bool'},
                                'send_email': {'bsonType': 'bool'},
                                'send_sms': {'bsonType': 'bool'},
                                'send_whatsapp': {'bsonType': 'bool'},
                                'reminder_days': {'bsonType': ['int', 'null']},
                                'user_email': {'bsonType': ['string', 'null']},
                                'user_phone': {'bsonType': ['string', 'null']},
                                'first_name': {'bsonType': ['string', 'null']}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING), ('due_date', ASCENDING)]},
                        {'key': [('session_id', ASCENDING), ('due_date', ASCENDING)]},
                        {'key': [('status', ASCENDING)]}
                    ]
                },
                'bill_reminders': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'notification_id', 'type', 'message', 'sent_at'],
                            'properties': {
                                'user_id': {'bsonType': 'string'},
                                'session_id': {'bsonType': ['string', 'null']},
                                'notification_id': {'bsonType': 'string'},
                                'type': {'enum': ['email', 'sms', 'whatsapp']},
                                'message': {'bsonType': 'string'},
                                'sent_at': {'bsonType': 'date'},
                                'read_status': {'bsonType': 'bool'}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING), ('sent_at', DESCENDING)]},
                        {'key': [('session_id', ASCENDING), ('sent_at', DESCENDING)]}
                    ]
                },
                'sessions': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['_id', 'data', 'expiration'],
                            'properties': {
                                '_id': {'bsonType': 'string'},
                                'data': {'bsonType': 'object'},
                                'expiration': {'bsonType': 'date'}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('expiration', ASCENDING)], 'expireAfterSeconds': 0}
                    ]
                },
                'system_config': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['_id', 'value'],
                            'properties': {
                                '_id': {'bsonType': 'string'},
                                'value': {'bsonType': ['bool', 'string', 'int', 'double', 'date', 'object', 'array']}
                            }
                        }
                    },
                    'indexes': []                      
                },
                'temp_passwords': {
                    'validator': {
                        '$jsonSchema': {
                            'bsonType': 'object',
                            'required': ['user_id', 'temp_password', 'created_at'],
                            'properties': {
                                '_id': {'bsonType': 'objectId'},
                                'user_id': {'bsonType': 'string'},
                                'temp_password': {'bsonType': 'string'},
                                'created_at': {'bsonType': 'date'},
                                'expires_at': {'bsonType': ['date', 'null']}
                            }
                        }
                    },
                    'indexes': [
                        {'key': [('user_id', ASCENDING)], 'unique': True},
                        {'key': [('expires_at', ASCENDING)], 'expireAfterSeconds': 604800}  # 7 days
                    ]
                }
            }
                
            # Initialize collections and indexes
            for collection_name, config in collection_schemas.items():
                if collection_name == 'credit_requests' and collection_name in collections:
                    try:
                        db_instance.command('collMod', collection_name, validator=config.get('validator', {}))
                        logger.info(f"Updated validator for collection: {collection_name}", 
                                    extra={'session_id': 'no-session-id'})
                    except Exception as e:
                        logger.error(f"Failed to update validator for collection {collection_name}: {str(e)}", 
                                    exc_info=True, extra={'session_id': 'no-session-id'})
                        raise
                elif collection_name not in collections:
                    try:
                        db_instance.create_collection(collection_name, validator=config.get('validator', {}))
                        logger.info(f"{trans('general_collection_created', default='Created collection')}: {collection_name}", 
                                   extra={'session_id': 'no-session-id'})
                    except Exception as e:
                        logger.error(f"Failed to create collection {collection_name}: {str(e)}", 
                                    exc_info=True, extra={'session_id': 'no-session-id'})
                        raise
                
                existing_indexes = db_instance[collection_name].index_information()
                for index in config.get('indexes', []):
                    if isinstance(index, list):
                        for idx in index:
                            keys = idx['key']
                            options = {k: v for k, v in idx.items() if k != 'key'}
                            index_key_tuple = tuple(keys)
                            index_name = '_'.join(f"{k}_{v if isinstance(v, int) else str(v).replace(' ', '_')}" for k, v in keys)
                            index_exists = False
                            for existing_index_name, existing_index_info in existing_indexes.items():
                                if tuple(existing_index_info['key']) == index_key_tuple:
                                    existing_options = {k: v for k, v in existing_index_info.items() if k not in ['key', 'v', 'ns']}
                                    if existing_options == options:
                                        logger.info(f"{trans('general_index_exists', default='Index already exists on')} {collection_name}: {keys} with options {options}", 
                                                   extra={'session_id': 'no-session-id'})
                                        index_exists = True
                                    else:
                                        if existing_index_name == '_id_':
                                            logger.info(f"Skipping drop of _id index on {collection_name}", 
                                                       extra={'session_id': 'no-session-id'})
                                            continue
                                        try:
                                            db_instance[collection_name].drop_index(existing_index_name)
                                            logger.info(f"Dropped conflicting index {existing_index_name} on {collection_name}", 
                                                       extra={'session_id': 'no-session-id'})
                                        except Exception as e:
                                            logger.error(f"Failed to drop index {existing_index_name} on {collection_name}: {str(e)}", 
                                                        exc_info=True, extra={'session_id': 'no-session-id'})
                                            raise
                                    break
                            if not index_exists:
                                try:
                                    db_instance[collection_name].create_index(keys, name=index_name, **options)
                                    logger.info(f"{trans('general_index_created', default='Created index on')} {collection_name}: {keys} with options {options}", 
                                               extra={'session_id': 'no-session-id'})
                                except Exception as e:
                                    if 'IndexKeySpecsConflict' in str(e):
                                        logger.info(f"Attempting to resolve index conflict for {collection_name}: {index_name}", 
                                                   extra={'session_id': 'no-session-id'})
                                        if index_name != '_id_':
                                            db_instance[collection_name].drop_index(index_name)
                                            db_instance[collection_name].create_index(keys, name=index_name, **options)
                                            logger.info(f"Recreated index on {collection_name}: {keys} with options {options}", 
                                                       extra={'session_id': 'no-session-id'})
                                        else:
                                            logger.info(f"Skipping recreation of _id index on {collection_name}", 
                                                       extra={'session_id': 'no-session-id'})
                                    else:
                                        logger.error(f"Failed to create index on {collection_name}: {str(e)}", 
                                                    exc_info=True, extra={'session_id': 'no-session-id'})
                                        raise
                    else:
                        keys = index['key']
                        options = {k: v for k, v in index.items() if k != 'key'}
                        index_key_tuple = tuple(keys)
                        index_name = '_'.join(f"{k}_{v if isinstance(v, int) else str(v).replace(' ', '_')}" for k, v in keys)
                        index_exists = False
                        for existing_index_name, existing_index_info in existing_indexes.items():
                            if tuple(existing_index_info['key']) == index_key_tuple:
                                existing_options = {k: v for k, v in existing_index_info.items() if k not in ['key', 'v', 'ns']}
                                if existing_options == options:
                                    logger.info(f"{trans('general_index_exists', default='Index already exists on')} {collection_name}: {keys} with options {options}", 
                                               extra={'session_id': 'no-session-id'})
                                    index_exists = True
                                else:
                                    if existing_index_name == '_id_':
                                        logger.info(f"Skipping drop of _id index on {collection_name}", 
                                                   extra={'session_id': 'no-session-id'})
                                        continue
                                    try:
                                        db_instance[collection_name].drop_index(existing_index_name)
                                        logger.info(f"Dropped conflicting index {existing_index_name} on {collection_name}", 
                                                   extra={'session_id': 'no-session-id'})
                                    except Exception as e:
                                        logger.error(f"Failed to drop index {existing_index_name} on {collection_name}: {str(e)}", 
                                                    exc_info=True, extra={'session_id': 'no-session-id'})
                                        raise
                                break
                        if not index_exists:
                            try:
                                db_instance[collection_name].create_index(keys, name=index_name, **options)
                                logger.info(f"{trans('general_index_created', default='Created index on')} {collection_name}: {keys} with options {options}", 
                                           extra={'session_id': 'no-session-id'})
                            except Exception as e:
                                if 'IndexKeySpecsConflict' in str(e):
                                    logger.info(f"Attempting to resolve index conflict for {collection_name}: {index_name}", 
                                               extra={'session_id': 'no-session-id'})
                                    if index_name != '_id_':
                                        db_instance[collection_name].drop_index(index_name)
                                        db_instance[collection_name].create_index(keys, name=index_name, **options)
                                        logger.info(f"Recreated index on {collection_name}: {keys} with options {options}", 
                                                   extra={'session_id': 'no-session-id'})
                                    else:
                                        logger.info(f"Skipping recreation of _id index on {collection_name}", 
                                                   extra={'session_id': 'no-session-id'})
                                else:
                                    logger.error(f"Failed to create index on {collection_name}: {str(e)}", 
                                                exc_info=True, extra={'session_id': 'no-session-id'})
                                    raise
            
            # Fix existing user documents in a single pass, only if not already applied
            if 'users' in collections:
                try:
                    # Check if user fixes have already been applied
                    fix_flag = db_instance.system_config.find_one({'_id': 'user_fixes_applied'})
                    if fix_flag and fix_flag.get('value') is True:
                        logger.info("User fixes already applied, skipping.", extra={'session_id': 'no-session-id'})
                    else:
                        # Query users needing either a missing password_hash or a double ficore_credit_balance
                        users_to_fix = db_instance.users.find({
                            '$or': [
                                {'password_hash': {'$exists': False}},
                                {'ficore_credit_balance': {'$type': 'double'}}
                            ]
                        })
                        for user in users_to_fix:
                            updates = {}
                            # Fix missing password_hash, log, and store temporary password for admin
                            if 'password_hash' not in user:
                                temp_password = str(uuid.uuid4())
                                updates['password_hash'] = generate_password_hash(temp_password)
                                logger.info(
                                    f"Added password_hash for user {user['_id']}. Temporary password: {temp_password} (for admin use only)",
                                    extra={'session_id': 'no-session-id'}
                                )
                                # Store temporary password in temp_passwords collection
                                try:
                                    db_instance.temp_passwords.update_one(
                                        {'user_id': str(user['_id'])},
                                        {
                                            '$set': {
                                                'temp_password': temp_password,
                                                'created_at': datetime.utcnow(),
                                                'expires_at': datetime.utcnow() + timedelta(days=7)
                                            },
                                            '$setOnInsert': {
                                                '_id': ObjectId(),
                                                'user_id': str(user['_id'])
                                            }
                                        },
                                        upsert=True
                                    )
                                    logger.info(
                                        f"Stored temporary password for user {user['_id']} in temp_passwords collection",
                                        extra={'session_id': 'no-session-id'}
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to store temporary password for user {user['_id']}: {str(e)}",
                                        exc_info=True, extra={'session_id': 'no-session-id'}
                                    )
                                    raise
                            # Convert ficore_credit_balance to int if it's a double
                            if user.get('ficore_credit_balance', None) is not None and isinstance(user['ficore_credit_balance'], float):
                                updates['ficore_credit_balance'] = int(round(float(user['ficore_credit_balance']), 2))
                                logger.info(
                                    f"Converted ficore_credit_balance to int for user {user['_id']}",
                                    extra={'session_id': 'no-session-id'}
                                )
                            # Apply updates if any are needed
                            if updates:
                                db_instance.users.update_one(
                                    {'_id': user['_id']},
                                    {'$set': updates}
                                )
                        
                        # Mark fixes as applied in system_config
                        try:
                            db_instance.system_config.update_one(
                                {'_id': 'user_fixes_applied'},
                                {'$set': {'value': True}},
                                upsert=True
                            )
                            logger.info("Marked user fixes as applied in system_config", extra={'session_id': 'no-session-id'})
                        except Exception as e:
                            logger.error(f"Failed to mark user fixes as applied: {str(e)}", 
                                        exc_info=True, extra={'session_id': 'no-session-id'})
                            raise
                
                    # Optionally, convert ficore_credit_balance to int if decimals are not allowed
                    # Uncomment the following block if you want integer credits
                    """
                    users_to_fix = db_instance.users.find({'ficore_credit_balance': {'$type': 'double'}})
                    for user in users_to_fix:
                        db_instance.users.update_one(
                            {'_id': user['_id']},
                            {'$set': {'ficore_credit_balance': int(user['ficore_credit_balance'])}}
                        )
                        logger.info(f"Converted ficore_credit_balance to int for user {user['_id']}", 
                                   extra={'session_id': 'no-session-id'})
                    """
                except Exception as e:
                    logger.error(f"Failed to fix user documents: {str(e)}", 
                                exc_info=True, extra={'session_id': 'no-session-id'})
                    raise
            
            # Initialize agents
            agents_collection = db_instance.agents
            if agents_collection.count_documents({}) == 0:
                try:
                    agents_collection.insert_many([
                        {
                            '_id': 'AG123456',
                            'status': 'active',
                            'created_at': datetime.utcnow(),
                            'updated_at': datetime.utcnow()
                        }
                    ])
                    logger.info(trans('general_agents_initialized', default='Initialized agents in MongoDB'), 
                               extra={'session_id': 'no-session-id'})
                except Exception as e:
                    logger.error(f"Failed to insert sample agents: {str(e)}", exc_info=True, extra={'session_id': 'no-session-id'})
                    raise
            
        except Exception as e:
            logger.error(f"{trans('general_database_initialization_failed', default='Failed to initialize database')}: {str(e)}", 
                        exc_info=True, extra={'session_id': 'no-session-id'})
            raise

# Rest of the original code remains unchanged
class User:
    def __init__(self, id, email, display_name=None, role='personal', username=None, is_admin=False, setup_complete=False, coin_balance=0, ficore_credit_balance=0, language='en', dark_mode=False):
        self.id = id
        self.email = email
        self.username = username or display_name or email.split('@')[0]
        self.role = role
        self.display_name = display_name or self.username
        self.is_admin = is_admin
        self.setup_complete = setup_complete
        self.coin_balance = coin_balance
        self.ficore_credit_balance = ficore_credit_balance
        self.language = language
        self.dark_mode = dark_mode

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    def get(self, key, default=None):
        return getattr(self, key, default)

def create_user(db, user_data):
    try:
        user_id = user_data.get('username', user_data['email'].split('@')[0]).lower()
        if 'password' not in user_data:
            user_data['password'] = str(uuid.uuid4())  # Generate a random temporary password
        user_data['password_hash'] = generate_password_hash(user_data['password'])
        
        # Ensure ficore_credit_balance is an integer
        ficore_credit_balance = int(user_data.get('ficore_credit_balance', 10))
        
        user_doc = {
            '_id': user_id,
            'email': user_data['email'].lower(),
            'password_hash': user_data['password_hash'],
            'role': user_data.get('role', 'personal'),
            'display_name': user_data.get('display_name', user_id),
            'is_admin': user_data.get('is_admin', False),
            'setup_complete': user_data.get('setup_complete', False),
            'coin_balance': int(user_data.get('coin_balance', 10)),  # Ensure coin_balance is also int
            'ficore_credit_balance': ficore_credit_balance,
            'language': user_data.get('lang', 'en'),
            'dark_mode': user_data.get('dark_mode', False),
            'created_at': user_data.get('created_at', datetime.utcnow()),
            'business_details': user_data.get('business_details'),
            'personal_details': user_data.get('personal_details'),
            'agent_details': user_data.get('agent_details')
        }
        
        with db.client.start_session() as session:
            with session.start_transaction():
                db.users.insert_one(user_doc, session=session)
                transaction = {
                    'user_id': user_id,
                    'amount': ficore_credit_balance,  # Use integer for consistency
                    'type': 'initial_credit',
                    'date': datetime.utcnow(),
                    'notes': f'Initial {ficore_credit_balance} Ficore Credits granted upon registration'
                }
                db.ficore_credit_transactions.insert_one(transaction, session=session)
        
        logger.info(f"Created user with ID: {user_id} with {ficore_credit_balance} Ficore Credits", 
                   extra={'session_id': 'no-session-id'})
        get_user.cache_clear()
        get_user_by_email.cache_clear()
        return User(
            id=user_doc['_id'],
            email=user_doc['email'],
            username=user_doc['_id'],
            role=user_doc['role'],
            display_name=user_doc['display_name'],
            is_admin=user_doc['is_admin'],
            setup_complete=user_doc['setup_complete'],
            coin_balance=user_doc['coin_balance'],
            ficore_credit_balance=user_doc['ficore_credit_balance'],
            language=user_doc['language'],
            dark_mode=user_doc['dark_mode']
        )
    except WriteError as e:
        if e.code == 121:  # Document validation error
            logger.error(f"Document validation failed: {e.details['errmsg']}", 
                        exc_info=True, extra={'session_id': 'no-session-id'})
            raise ValueError(f"Document validation failed: {e.details['errmsg']}")
        elif e.code == 11000:  # Duplicate key error
            logger.error(f"Duplicate key error for user {user_id}: {str(e)}", 
                        exc_info=True, extra={'session_id': 'no-session-id'})
            raise ValueError("User with this email or username already exists")
        logger.error(f"Unexpected MongoDB error creating user {user_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise
    except Exception as e:
        logger.error(f"Error creating user {user_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

@lru_cache(maxsize=128)
def get_user_by_email(db, email):
    """
    Retrieve a user by email from the users collection.
    
    Args:
        db: MongoDB database instance
        email: Email address of the user
    
    Returns:
        User: User object or None if not found
    """
    try:
        logger.debug(f"Calling get_user_by_email for email: {email}, stack: {''.join(traceback.format_stack()[-5:])}", 
                    extra={'session_id': 'no-session-id'})
        user_doc = db.users.find_one({'email': email.lower()})
        if user_doc:
            return User(
                id=user_doc['_id'],
                email=user_doc['email'],
                username=user_doc['_id'],
                role=user_doc.get('role', 'personal'),
                display_name=user_doc.get('display_name'),
                is_admin=user_doc.get('is_admin', False),
                setup_complete=user_doc.get('setup_complete', False),
                coin_balance=user_doc.get('coin_balance', 0),
                ficore_credit_balance=user_doc.get('ficore_credit_balance', 0),
                language=user_doc.get('language', 'en'),
                dark_mode=user_doc.get('dark_mode', False)
            )
        return None
    except Exception as e:
        logger.error(f"{trans('general_user_fetch_error', default='Error getting user by email')} {email}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

@lru_cache(maxsize=128)
def get_user(db, user_id):
    """
    Retrieve a user by ID from the users collection.
    
    Args:
        db: MongoDB database instance
        user_id: ID of the user
    
    Returns:
        User: User object or None if not found
    """
    try:
        logger.debug(f"Calling get_user for user_id: {user_id}, stack: {''.join(traceback.format_stack()[-5:])}", 
                    extra={'session_id': 'no-session-id'})
        user_doc = db.users.find_one({'_id': user_id})
        if user_doc:
            return User(
                id=user_doc['_id'],
                email=user_doc['email'],
                username=user_doc['_id'],
                role=user_doc.get('role', 'personal'),
                display_name=user_doc.get('display_name'),
                is_admin=user_doc.get('is_admin', False),
                setup_complete=user_doc.get('setup_complete', False),
                coin_balance=user_doc.get('coin_balance', 0),
                ficore_credit_balance=user_doc.get('ficore_credit_balance', 0),
                language=user_doc.get('language', 'en'),
                dark_mode=user_doc.get('dark_mode', False)
            )
        return None
    except Exception as e:
        logger.error(f"{trans('general_user_fetch_error', default='Error getting user by ID')} {user_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def create_credit_request(db, request_data):
    """
    Create a new credit request in the credit_requests collection.
    
    Args:
        db: MongoDB database instance
        request_data: Dictionary containing credit request information
    
    Returns:
        str: ID of the created credit request
    """
    try:
        required_fields = ['user_id', 'amount', 'payment_method', 'status', 'created_at']
        if not all(field in request_data for field in required_fields):
            raise ValueError(trans('credits_missing_request_fields', default='Missing required credit request fields'))
        result = db.credit_requests.insert_one(request_data)
        logger.info(f"{trans('credits_request_created', default='Created credit request with ID')}: {result.inserted_id}", 
                   extra={'session_id': request_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('credits_request_creation_error', default='Error creating credit request')}: {str(e)}", 
                    exc_info=True, extra={'session_id': request_data.get('session_id', 'no-session-id')})
        raise

def update_credit_request(db, request_id, update_data):
    """
    Update a credit request in the credit_requests collection.
    
    Args:
        db: MongoDB database instance
        request_id: The ID of the credit request to update
        update_data: Dictionary containing fields to update (e.g., status, admin_id)
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        update_data['updated_at'] = datetime.utcnow()
        result = db.credit_requests.update_one(
            {'_id': ObjectId(request_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('credits_request_updated', default='Updated credit request with ID')}: {request_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('credits_request_no_change', default='No changes made to credit request with ID')}: {request_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('credits_request_update_error', default='Error updating credit request with ID')} {request_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def get_credit_requests(db, filter_kwargs):
    """
    Retrieve credit request records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of credit request records
    """
    try:
        return list(db.credit_requests.find(filter_kwargs).sort('created_at', DESCENDING))
    except Exception as e:
        logger.error(f"{trans('credits_requests_fetch_error', default='Error getting credit requests')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def to_dict_credit_request(record):
    """Convert credit request record to dictionary."""
    if not record:
        return {'user_id': None, 'amount': None, 'status': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'amount': record.get('amount', 0),
        'payment_method': record.get('payment_method', ''),
        'receipt_file_id': str(record.get('receipt_file_id', '')) if record.get('receipt_file_id') else None,
        'status': record.get('status', ''),
        'created_at': record.get('created_at'),
        'updated_at': record.get('updated_at'),
        'admin_id': record.get('admin_id')
    }

def get_agent(db, agent_id):
    """
    Retrieve an agent by ID from the agents collection.
    
    Args:
        db: MongoDB database instance
        agent_id: The agent ID to retrieve
    
    Returns:
        dict: Agent document or None if not found
    """
    try:
        agent_doc = db.agents.find_one({'_id': agent_id.upper()})
        if agent_doc:
            return {
                '_id': agent_doc['_id'],
                'status': agent_doc['status'],
                'created_at': agent_doc['created_at'],
                'updated_at': agent_doc.get('updated_at')
            }
        return None
    except Exception as e:
        logger.error(f"{trans('agents_fetch_error', default='Error getting agent by ID')} {agent_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def update_agent(db, agent_id, status):
    """
    Update an agent's status in the agents collection.
    
    Args:
        db: MongoDB database instance
        agent_id: The agent ID to update
        status: The new status ('active' or 'inactive')
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        result = db.agents.update_one(
            {'_id': agent_id.upper()},
            {'$set': {'status': status, 'updated_at': datetime.utcnow()}}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('agents_status_updated', default='Updated agent status for ID')}: {agent_id} to {status}", 
                       extra={'session_id': 'no-session-id'})
            return True
        return False
    except Exception as e:
        logger.error(f"{trans('agents_update_error', default='Error updating agent status for ID')} {agent_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def get_budgets(db, filter_kwargs):
    """
    Retrieve budget records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of budget records
    """
    try:
        return list(db.budgets.find(filter_kwargs).sort('created_at', DESCENDING))
    except Exception as e:
        logger.error(f"{trans('general_budgets_fetch_error', default='Error getting budgets')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def get_bills(db, filter_kwargs):
    """
    Retrieve bill records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of bill records
    """
    try:
        return list(db.bills.find(filter_kwargs).sort('due_date', ASCENDING))
    except Exception as e:
        logger.error(f"{trans('general_bills_fetch_error', default='Error getting bills')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def create_feedback(db, feedback_data):
    """
    Create a new feedback record in the feedback collection.
    
    Args:
        db: MongoDB database instance
        feedback_data: Dictionary containing feedback information
    """
    try:
        required_fields = ['tool_name', 'rating', 'timestamp']
        if not all(field in feedback_data for field in required_fields):
            raise ValueError(trans('general_missing_feedback_fields', default='Missing required feedback fields'))
        if 'user_id' not in feedback_data or feedback_data['user_id'] is None:
            feedback_data['user_id'] = 'anonymous'
        logger.debug(f"Feedback data before insertion: {feedback_data}",
                    extra={'session_id': feedback_data.get('session_id', 'no-session-id')})
        db.feedback.insert_one(feedback_data)
        logger.info(f"{trans('general_feedback_created', default='Created feedback record for tool')}: {feedback_data.get('tool_name')}", 
                   extra={'session_id': feedback_data.get('session_id', 'no-session-id')})
    except Exception as e:
        logger.error(f"{trans('general_feedback_creation_error', default='Error creating feedback')}: {str(e)}", 
                    exc_info=True, extra={'session_id': feedback_data.get('session_id', 'no-session-id')})
        raise

def log_tool_usage(db, tool_name, user_id=None, session_id=None, action=None):
    """
    Log tool usage in the tool_usage collection.
    
    Args:
        db: MongoDB database instance
        tool_name: Name of the tool used
        user_id: ID of the user (optional)
        session_id: Session ID (optional)
        action: Action performed (optional)
    """
    try:
        usage_data = {
            'tool_name': tool_name,
            'user_id': user_id,
            'session_id': session_id,
            'action': action,
            'timestamp': datetime.utcnow()
        }
        db.tool_usage.insert_one(usage_data)
        logger.info(f"{trans('general_tool_usage_logged', default='Logged tool usage')}: {tool_name} - {action}", 
                   extra={'session_id': session_id or 'no-session-id'})
    except Exception as e:
        logger.error(f"{trans('general_tool_usage_log_error', default='Error logging tool usage')}: {str(e)}", 
                    exc_info=True, extra={'session_id': session_id or 'no-session-id'})
        raise

def create_budget(db, budget_data):
    """
    Create a new budget record in the budgets collection.
    
    Args:
        db: MongoDB database instance
        budget_data: Dictionary containing budget information
    
    Returns:
        str: ID of the created budget record
    """
    try:
        required_fields = ['user_id', 'income', 'fixed_expenses', 'variable_expenses', 'created_at']
        if not all(field in budget_data for field in required_fields):
            raise ValueError(trans('general_missing_budget_fields', default='Missing required budget fields'))
        result = db.budgets.insert_one(budget_data)
        logger.info(f"{trans('general_budget_created', default='Created budget record with ID')}: {result.inserted_id}", 
                   extra={'session_id': budget_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_budget_creation_error', default='Error creating budget record')}: {str(e)}", 
                    exc_info=True, extra={'session_id': budget_data.get('session_id', 'no-session-id')})
        raise

def create_bill(db, bill_data):
    """
    Create a new bill record in the bills collection.
    
    Args:
        db: MongoDB database instance
        bill_data: Dictionary containing bill information
    
    Returns:
        str: ID of the created bill record
    """
    try:
        required_fields = ['user_id', 'bill_name', 'amount', 'due_date', 'status']
        if not all(field in bill_data for field in required_fields):
            raise ValueError(trans('general_missing_bill_fields', default='Missing required bill fields'))
        result = db.bills.insert_one(bill_data)
        logger.info(f"{trans('general_bill_created', default='Created bill record with ID')}: {result.inserted_id}", 
                   extra={'session_id': bill_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_bill_creation_error', default='Error creating bill record')}: {str(e)}", 
                    exc_info=True, extra={'session_id': bill_data.get('session_id', 'no-session-id')})
        raise

def create_bill_reminder(db, reminder_data):
    """
    Create a new bill reminder in the bill_reminders collection.
    
    Args:
        db: MongoDB database instance
        reminder_data: Dictionary containing bill reminder information
    
    Returns:
        str: ID of the created bill reminder
    """
    try:
        required_fields = ['user_id', 'notification_id', 'type', 'message', 'sent_at']
        if not all(field in reminder_data for field in required_fields):
            raise ValueError(trans('general_missing_bill_reminder_fields', default='Missing required bill reminder fields'))
        result = db.bill_reminders.insert_one(reminder_data)
        logger.info(f"{trans('general_bill_reminder_created', default='Created bill reminder with ID')}: {result.inserted_id}", 
                   extra={'session_id': reminder_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_bill_reminder_creation_error', default='Error creating bill reminder')}: {str(e)}", 
                    exc_info=True, extra={'session_id': reminder_data.get('session_id', 'no-session-id')})
        raise

def get_records(db, filter_kwargs):
    """
    Retrieve records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of records
    """
    try:
        return list(db.records.find(filter_kwargs).sort('created_at', DESCENDING))
    except Exception as e:
        logger.error(f"{trans('general_records_fetch_error', default='Error getting records')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def create_record(db, record_data):
    """
    Create a new record in the records collection.
    
    Args:
        db: MongoDB database instance
        record_data: Dictionary containing record information
    
    Returns:
        str: ID of the created record
    """
    try:
        required_fields = ['user_id', 'type', 'name', 'amount_owed']
        if not all(field in record_data for field in required_fields):
            raise ValueError(trans('general_missing_record_fields', default='Missing required record fields'))
        result = db.records.insert_one(record_data)
        logger.info(f"{trans('general_record_created', default='Created record with ID')}: {result.inserted_id}", 
                   extra={'session_id': record_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_record_creation_error', default='Error creating record')}: {str(e)}", 
                    exc_info=True, extra={'session_id': record_data.get('session_id', 'no-session-id')})
        raise

def get_cashflows(db, filter_kwargs):
    """
    Retrieve cashflow records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of cashflow records
    """
    try:
        return list(db.cashflows.find(filter_kwargs).sort('created_at', DESCENDING))
    except Exception as e:
        logger.error(f"{trans('general_cashflows_fetch_error', default='Error getting cashflows')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def create_cashflow(db, cashflow_data):
    """
    Create a new cashflow record in the cashflows collection.
    
    Args:
        db: MongoDB database instance
        cashflow_data: Dictionary containing cashflow information
    
    Returns:
        str: ID of the created cashflow record
    """
    try:
        required_fields = ['user_id', 'type', 'party_name', 'amount']
        if not all(field in cashflow_data for field in required_fields):
            raise ValueError(trans('general_missing_cashflow_fields', default='Missing required cashflow fields'))
        result = db.cashflows.insert_one(cashflow_data)
        logger.info(f"{trans('general_cashflow_created', default='Created cashflow record with ID')}: {result.inserted_id}", 
                   extra={'session_id': cashflow_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_cashflow_creation_error', default='Error creating cashflow record')}: {str(e)}", 
                    exc_info=True, extra={'session_id': cashflow_data.get('session_id', 'no-session-id')})
        raise

def get_ficore_credit_transactions(db, filter_kwargs):
    """
    Retrieve ficore credit transaction records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of ficore credit transaction records
    """
    try:
        return list(db.ficore_credit_transactions.find(filter_kwargs).sort('date', DESCENDING))
    except Exception as e:
        logger.error(f"{trans('credits_transactions_fetch_error', default='Error getting ficore credit transactions')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def create_ficore_credit_transaction(db, transaction_data):
    """
    Create a new ficore credit transaction in the ficore_credit_transactions collection.
    
    Args:
        db: MongoDB database instance
        transaction_data: Dictionary containing transaction information
    
    Returns:
        str: ID of the created transaction
    """
    try:
        required_fields = ['user_id', 'amount', 'type', 'date']
        if not all(field in transaction_data for field in required_fields):
            raise ValueError(trans('credits_missing_transaction_fields', default='Missing required ficore credit transaction fields'))
        result = db.ficore_credit_transactions.insert_one(transaction_data)
        logger.info(f"{trans('credits_transaction_created', default='Created ficore credit transaction with ID')}: {result.inserted_id}", 
                   extra={'session_id': transaction_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('credits_transaction_creation_error', default='Error creating ficore credit transaction')}: {str(e)}", 
                    exc_info=True, extra={'session_id': transaction_data.get('session_id', 'no-session-id')})
        raise

def get_audit_logs(db, filter_kwargs):
    """
    Retrieve audit log records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of audit log records
    """
    try:
        return list(db.audit_logs.find(filter_kwargs).sort('timestamp', DESCENDING))
    except Exception as e:
        logger.error(f"{trans('general_audit_logs_fetch_error', default='Error getting audit logs')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def create_audit_log(db, audit_data):
    """
    Create a new audit log in the audit_logs collection.
    
    Args:
        db: MongoDB database instance
        audit_data: Dictionary containing audit log information
    
    Returns:
        str: ID of the created audit log
    """
    try:
        required_fields = ['admin_id', 'action', 'timestamp']
        if not all(field in audit_data for field in required_fields):
            raise ValueError(trans('general_missing_audit_fields', default='Missing required audit log fields'))
        result = db.audit_logs.insert_one(audit_data)
        logger.info(f"{trans('general_audit_log_created', default='Created audit log with ID')}: {result.inserted_id}", 
                   extra={'session_id': audit_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_audit_log_creation_error', default='Error creating audit log')}: {str(e)}", 
                    exc_info=True, extra={'session_id': audit_data.get('session_id', 'no-session-id')})
        raise

def update_user(db, user_id, update_data):
    """
    Update a user in the users collection.
    
    Args:
        db: MongoDB database instance
        user_id: The ID of the user to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        if 'password' in update_data:
            update_data['password_hash'] = generate_password_hash(update_data.pop('password'))
        result = db.users.update_one(
            {'_id': user_id},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_user_updated', default='Updated user with ID')}: {user_id}", 
                       extra={'session_id': 'no-session-id'})
            get_user.cache_clear()
            get_user_by_email.cache_clear()
            return True
        logger.info(f"{trans('general_user_no_change', default='No changes made to user with ID')}: {user_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_user_update_error', default='Error updating user with ID')} {user_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def to_dict_record(record):
    """Convert record to dictionary."""
    if not record:
        return {'name': None, 'amount_owed': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'type': record.get('type', ''),
        'name': record.get('name', ''),
        'contact': record.get('contact', ''),
        'amount_owed': record.get('amount_owed', 0),
        'description': record.get('description', ''),
        'reminder_count': record.get('reminder_count', 0),
        'created_at': record.get('created_at'),
        'updated_at': record.get('updated_at')
    }

def to_dict_cashflow(record):
    """Convert cashflow record to dictionary."""
    if not record:
        return {'party_name': None, 'amount': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'type': record.get('type', ''),
        'party_name': record.get('party_name', ''),
        'amount': record.get('amount', 0),
        'method': record.get('method', ''),
        'category': record.get('category', ''),
        'created_at': record.get('created_at'),
        'updated_at': record.get('updated_at')
    }

def to_dict_ficore_credit_transaction(record):
    """Convert ficore credit transaction record to dictionary."""
    if not record:
        return {'amount': None, 'type': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'amount': record.get('amount', 0),
        'type': record.get('type', ''),
        'ref': record.get('ref', ''),
        'date': record.get('date'),
        'facilitated_by_agent': record.get('facilitated_by_agent', ''),
        'payment_method': record.get('payment_method', ''),
        'cash_amount': record.get('cash_amount', 0),
        'notes': record.get('notes', '')
    }

def to_dict_audit_log(record):
    """Convert audit log record to dictionary."""
    if not record:
        return {'action': None, 'timestamp': None}
    return {
        'id': str(record.get('_id', '')),
        'admin_id': record.get('admin_id', ''),
        'action': record.get('action', ''),
        'details': record.get('details', {}),
        'timestamp': record.get('timestamp')
    }

def to_dict_user(user):
    """Convert user object to dictionary."""
    if not user:
        return {'id': None, 'email': None}
    return {
        'id': user.id,
        'email': user.email,
        'username': user.username,
        'role': user.role,
        'display_name': user.display_name,
        'is_admin': user.is_admin,
        'setup_complete': user.setup_complete,
        'coin_balance': user.coin_balance,
        'ficore_credit_balance': user.ficore_credit_balance,
        'language': user.language,
        'dark_mode': user.dark_mode
    }

def to_dict_budget(record):
    """Convert budget record to dictionary."""
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
        'created_at': record.get('created_at')
    }

def to_dict_bill(record):
    """Convert bill record to dictionary."""
    if not record:
        return {'bill_name': None, 'amount': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'bill_name': record.get('bill_name', ''),
        'amount': record.get('amount', 0),
        'due_date': record.get('due_date'),
        'frequency': record.get('frequency', ''),
        'category': record.get('category', ''),
        'status': record.get('status', ''),
        'send_notifications': record.get('send_notifications', False),
        'send_email': record.get('send_email', False),
        'send_sms': record.get('send_sms', False),
        'send_whatsapp': record.get('send_whatsapp', False),
        'reminder_days': record.get('reminder_days', None),
        'user_email': record.get('user_email', ''),
        'user_phone': record.get('user_phone', ''),
        'first_name': record.get('first_name', '')
    }

def to_dict_bill_reminder(record):
    """Convert bill reminder record to dictionary."""
    if not record:
        return {'notification_id': None, 'type': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'notification_id': record.get('notification_id', ''),
        'type': record.get('type', ''),
        'message': record.get('message', ''),
        'sent_at': record.get('sent_at'),
        'read_status': record.get('read_status', False)
    }

def create_shopping_item(db, item_data):
    """
    Create a new shopping item in the shopping_items collection.
    
    Args:
        db: MongoDB database instance
        item_data: Dictionary containing shopping item information
    
    Returns:
        str: ID of the created shopping item
    """
    try:
        required_fields = ['user_id', 'list_id', 'name', 'quantity', 'price', 'category', 'status', 'created_at', 'updated_at']
        if not all(field in item_data for field in required_fields):
            raise ValueError(trans('general_missing_shopping_item_fields', default='Missing required shopping item fields'))
        item_data['unit'] = item_data.get('unit', 'piece')
        result = db.shopping_items.insert_one(item_data)
        logger.info(f"{trans('general_shopping_item_created', default='Created shopping item with ID')}: {result.inserted_id}", 
                   extra={'session_id': item_data.get('session_id', 'no-session-id')})
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_shopping_item_creation_error', default='Error creating shopping item')}: {str(e)}", 
                    exc_info=True, extra={'session_id': item_data.get('session_id', 'no-session-id')})
        raise

def get_shopping_items(db, filter_kwargs):
    """
    Retrieve shopping item records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of shopping item records
    """
    try:
        return list(db.shopping_items.find(filter_kwargs).sort('created_at', DESCENDING))
    except Exception as e:
        logger.error(f"{trans('general_shopping_items_fetch_error', default='Error getting shopping items')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def to_dict_shopping_item(record):
    """Convert shopping item record to dictionary."""
    if not record:
        return {'name': None, 'quantity': None}
    return {
        'id': str(record.get('_id', '')),
        'user_id': record.get('user_id', ''),
        'list_id': record.get('list_id', ''),
        'name': record.get('name', ''),
        'quantity': record.get('quantity', 0),
        'price': record.get('price', 0.0),
        'category': record.get('category', ''),
        'status': record.get('status', ''),
        'created_at': record.get('created_at'),
        'updated_at': record.get('updated_at'),
        'store': record.get('store', ''),
        'frequency': record.get('frequency', 1)
    }

def update_shopping_item(db, item_id, update_data):
    """
    Update a shopping item in the shopping_items collection.
    
    Args:
        db: MongoDB database instance
        item_id: The ID of the shopping item to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        update_data['updated_at'] = datetime.utcnow()
        result = db.shopping_items.update_one(
            {'_id': ObjectId(item_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_shopping_item_updated', default='Updated shopping item with ID')}: {item_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('general_shopping_item_no_change', default='No changes made to shopping item with ID')}: {item_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_shopping_item_update_error', default='Error updating shopping item with ID')} {item_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def update_record(db, record_id, update_data):
    """
    Update a record in the records collection.
    
    Args:
        db: MongoDB database instance
        record_id: The ID of the record to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        update_data['updated_at'] = datetime.utcnow()
        result = db.records.update_one(
            {'_id': ObjectId(record_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_record_updated', default='Updated record with ID')}: {record_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('general_record_no_change', default='No changes made to record with ID')}: {record_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_record_update_error', default='Error updating record with ID')} {record_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def update_cashflow(db, cashflow_id, update_data):
    """
    Update a cashflow record in the cashflows collection.
    
    Args:
        db: MongoDB database instance
        cashflow_id: The ID of the cashflow record to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        update_data['updated_at'] = datetime.utcnow()
        result = db.cashflows.update_one(
            {'_id': ObjectId(cashflow_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_cashflow_updated', default='Updated cashflow record with ID')}: {cashflow_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('general_cashflow_no_change', default='No changes made to cashflow record with ID')}: {cashflow_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_cashflow_update_error', default='Error updating cashflow record with ID')} {cashflow_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def update_ficore_credit_transaction(db, transaction_id, update_data):
    """
    Update a ficore credit transaction in the ficore_credit_transactions collection.
    
    Args:
        db: MongoDB database instance
        transaction_id: The ID of the transaction to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        result = db.ficore_credit_transactions.update_one(
            {'_id': ObjectId(transaction_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('credits_transaction_updated', default='Updated ficore credit transaction with ID')}: {transaction_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('credits_transaction_no_change', default='No changes made to ficore credit transaction with ID')}: {transaction_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('credits_transaction_update_error', default='Error updating ficore credit transaction with ID')} {transaction_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def update_budget(db, budget_id, update_data):
    """
    Update a budget record in the budgets collection.
    
    Args:
        db: MongoDB database instance
        budget_id: The ID of the budget record to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        result = db.budgets.update_one(
            {'_id': ObjectId(budget_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_budget_updated', default='Updated budget record with ID')}: {budget_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('general_budget_no_change', default='No changes made to budget record with ID')}: {budget_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_budget_update_error', default='Error updating budget record with ID')} {budget_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def update_bill(db, bill_id, update_data):
    """
    Update a bill record in the bills collection.
    
    Args:
        db: MongoDB database instance
        bill_id: The ID of the bill record to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        result = db.bills.update_one(
            {'_id': ObjectId(bill_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_bill_updated', default='Updated bill record with ID')}: {bill_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('general_bill_no_change', default='No changes made to bill record with ID')}: {bill_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_bill_update_error', default='Error updating bill record with ID')} {bill_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def update_bill_reminder(db, reminder_id, update_data):
    """
    Update a bill reminder in the bill_reminders collection.
    
    Args:
        db: MongoDB database instance
        reminder_id: The ID of the bill reminder to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        result = db.bill_reminders.update_one(
            {'_id': ObjectId(reminder_id)},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_bill_reminder_updated', default='Updated bill reminder with ID')}: {reminder_id}", 
                       extra={'session_id': 'no-session-id'})
            return True
        logger.info(f"{trans('general_bill_reminder_no_change', default='No changes made to bill reminder with ID')}: {reminder_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_bill_reminder_update_error', default='Error updating bill reminder with ID')} {reminder_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def create_shopping_list(db, list_data):
    """
    Create a new shopping list in the shopping_lists collection.
    
    Args:
        db: MongoDB database instance
        list_data: Dictionary containing shopping list information
    
    Returns:
        str: ID of the created shopping list
    """
    try:
        required_fields = ['name', 'session_id', 'budget', 'created_at', 'updated_at', 'total_spent', 'status']
        if not all(field in list_data for field in required_fields):
            raise ValueError(trans('general_missing_shopping_list_fields', default='Missing required shopping list fields'))
        list_data['_id'] = str(uuid.uuid4())
        list_data['items'] = list_data.get('items', [])
        result = db.shopping_lists.insert_one(list_data)
        logger.info(f"{trans('general_shopping_list_created', default='Created shopping list with ID')}: {result.inserted_id}", 
                   extra={'session_id': list_data.get('session_id', 'no-session-id')})
        get_shopping_lists.cache_clear()
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"{trans('general_shopping_list_creation_error', default='Error creating shopping list')}: {str(e)}", 
                    exc_info=True, extra={'session_id': list_data.get('session_id', 'no-session-id')})
        raise

@lru_cache(maxsize=128)
def normalize_shopping_list(record):
    """
    Normalize a shopping list record to ensure consistent structure.
    
    Args:
        record: Raw shopping list document from MongoDB
    
    Returns:
        dict: Normalized shopping list dictionary
    """
    return {
        'id': str(record.get('_id', '')),
        'name': record.get('name', ''),
        'user_id': record.get('user_id', None),
        'session_id': record.get('session_id', ''),
        'budget': float(record.get('budget', 0.0)),
        'created_at': record.get('created_at', datetime.utcnow()),
        'updated_at': record.get('updated_at', datetime.utcnow()),
        'collaborators': record.get('collaborators', []),
        'total_spent': float(record.get('total_spent', 0.0)),
        'status': record.get('status', 'active'),
        'items': record.get('items', []) if isinstance(record.get('items'), list) else []
    }

@lru_cache(maxsize=128)
def get_shopping_lists(db, filter_kwargs):
    """
    Retrieve shopping list records based on filter criteria.
    
    Args:
        db: MongoDB database instance
        filter_kwargs: Dictionary of filter criteria
    
    Returns:
        list: List of normalized shopping list records
    """
    try:
        return [normalize_shopping_list(record) for record in db.shopping_lists.find(filter_kwargs).sort('updated_at', DESCENDING)]
    except Exception as e:
        logger.error(f"{trans('general_shopping_lists_fetch_error', default='Error getting shopping lists')}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise
        

def update_shopping_list(db, list_id, update_data):
    """
    Update a shopping list in the shopping_lists collection.
    
    Args:
        db: MongoDB database instance
        list_id: The ID of the shopping list to update
        update_data: Dictionary containing fields to update
    
    Returns:
        bool: True if updated, False if not found or no changes made
    """
    try:
        update_data['updated_at'] = datetime.utcnow()
        result = db.shopping_lists.update_one(
            {'_id': list_id},
            {'$set': update_data}
        )
        if result.modified_count > 0:
            logger.info(f"{trans('general_shopping_list_updated', default='Updated shopping list with ID')}: {list_id}", 
                       extra={'session_id': 'no-session-id'})
            get_shopping_lists.cache_clear()
            return True
        logger.info(f"{trans('general_shopping_list_no_change', default='No changes made to shopping list with ID')}: {list_id}", 
                   extra={'session_id': 'no-session-id'})
        return False
    except Exception as e:
        logger.error(f"{trans('general_shopping_list_update_error', default='Error updating shopping list with ID')} {list_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def to_dict_shopping_list(record):
    """Convert shopping list record to dictionary."""
    if not record:
        return {'name': None, 'budget': None}
    return {
        'id': str(record.get('_id', '')),
        'name': record.get('name', ''),
        'user_id': record.get('user_id', ''),
        'session_id': record.get('session_id', ''),
        'budget': record.get('budget', 0.0),
        'created_at': record.get('created_at'),
        'updated_at': record.get('updated_at'),
        'collaborators': record.get('collaborators', []),
        'total_spent': record.get('total_spent', 0.0),
        'status': record.get('status', '')
    }

def delete_shopping_list(db, list_id, user_id=None, email=None):
    """
    Delete a shopping list and its associated items from the shopping_lists and shopping_items collections.
    
    Args:
        db: MongoDB database instance
        list_id: The ID of the shopping list to delete
        user_id: Optional user ID for ownership check
        email: Optional email for ownership check
    
    Returns:
        bool: True if deleted, False if not found or no changes made
    """
    try:
        filter_criteria = {'_id': list_id}
        if user_id and email:
            filter_criteria.update({'user_id': user_id, 'email': email.lower()})
        
        with db.client.start_session() as session:
            with session.start_transaction():
                # Delete the shopping list
                list_result = db.shopping_lists.delete_one(filter_criteria)
                if list_result.deleted_count == 0:
                    logger.info(f"No shopping list found with ID {list_id} for deletion", 
                               extra={'session_id': 'no-session-id'})
                    return False
                
                # Delete associated items
                items_result = db.shopping_items.delete_many({'list_id': list_id})
                logger.info(f"Deleted shopping list ID {list_id} and {items_result.deleted_count} associated items", 
                           extra={'session_id': 'no-session-id'})
                
                get_shopping_lists.cache_clear()
                return True
    except Exception as e:
        logger.error(f"Error deleting shopping list ID {list_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        raise

def deduct_ficore_credits(db, user_id, email, amount, action, ref=None):
    try:
        if amount <= 0:
            raise ValueError(f"Invalid deduction amount: {amount}")
        amount = round(float(amount), 2)
        with db.client.start_session() as session:
            with session.start_transaction():
                user = db.users.find_one({'_id': user_id, 'email': email.lower()}, session=session)
                if not user:
                    logger.error(f"User {user_id}, email: {email} not found for credit deduction", 
                                extra={'session_id': 'no-session-id'})
                    return False
                
                current_balance = user.get('ficore_credit_balance', 0)
                if current_balance < amount:
                    logger.warning(f"Insufficient credits for user {user_id}: required {amount}, available {current_balance}", 
                                  extra={'session_id': 'no-session-id'})
                    return False
                
                new_balance = round(current_balance - amount, 2)  # Keep as double, or use int(current_balance - amount) for integers
                result = db.users.update_one(
                    {'_id': user_id, 'email': email.lower()},
                    {'$set': {'ficore_credit_balance': new_balance}},  # Use $set to enforce type
                    session=session
                )
                if result.modified_count == 0:
                    raise ValueError("Failed to update user balance")
                
                transaction = {
                    'user_id': user_id,
                    'email': email.lower(),
                    'amount': -amount,
                    'type': action,
                    'ref': ref,
                    'date': datetime.utcnow()
                }
                db.ficore_credit_transactions.insert_one(transaction, session=session)
                
                get_user.cache_clear()
                get_user_by_email.cache_clear()
                logger.info(f"Deducted {amount} Ficore Credits for {action} by user {user_id}", 
                           extra={'session_id': 'no-session-id'})
                return True
    except Exception as e:
        logger.error(f"Error deducting {amount} Ficore Credits for {action} by user {user_id}: {str(e)}", 
                    exc_info=True, extra={'session_id': 'no-session-id'})
        return False

def create_shopping_items_bulk(db, items_data):
    """
    Create multiple shopping items in the shopping_items collection in a single operation.
    
    Args:
        db: MongoDB database instance
        items_data: List of dictionaries containing shopping item information
    
    Returns:
        list: List of IDs of the created shopping items
    """
    try:
        required_fields = ['user_id', 'list_id', 'name', 'quantity', 'price', 'category', 'status', 'created_at', 'updated_at']
        for item in items_data:
            if not all(field in item for field in required_fields):
                raise ValueError(f"Missing required fields in item: {item}")
        
        result = db.shopping_items.insert_many(items_data)
        logger.info(f"Created {len(result.inserted_ids)} shopping items", 
                   extra={'session_id': items_data[0].get('session_id', 'no-session-id')})
        return [str(id) for id in result.inserted_ids]
    except Exception as e:
        logger.error(f"Error creating bulk shopping items: {str(e)}", 
                    exc_info=True, extra={'session_id': items_data[0].get('session_id', 'no-session-id') if items_data else 'no-session-id'})
        raise
