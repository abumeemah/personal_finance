import uuid
from datetime import datetime
from flask import session, has_request_context
import logging

logger = logging.getLogger(__name__)

def create_anonymous_session():
    """Create a guest session for anonymous access."""
    try:
        if not has_request_context():
            logger.warning("Attempted to create anonymous session outside request context")
            return
            
        session['sid'] = str(uuid.uuid4())
        session['is_anonymous'] = True
        session['last_activity'] = datetime.utcnow()  # Use last_activity for timeout tracking
        session.permanent = False  # Non-permanent session
        
        # Set default language if not already set
        if 'lang' not in session:
            session['lang'] = 'en'
            
        logger.info(f"Created anonymous session: {session['sid']}")
    except Exception as e:
        logger.error(f"Error creating anonymous session: {str(e)}", exc_info=True)

def get_session_id():
    """Get the current session ID, creating one if it doesn't exist."""
    try:
        if not has_request_context():
            return 'no-request-context'
            
        if 'sid' not in session:
            create_anonymous_session()
            
        return session.get('sid', 'no-session-id')
    except Exception as e:
        logger.error(f"Error getting session ID: {str(e)}")
        return 'session-error'

def is_anonymous_session():
    """Check if the current session is anonymous."""
    try:
        if not has_request_context():
            return False
            
        return session.get('is_anonymous', False)
    except Exception:
        return False

def clear_anonymous_session():
    """Clear anonymous session data while preserving language."""
    try:
        if not has_request_context():
            return
            
        lang = session.get('lang', 'en')
        session.clear()
        session['lang'] = lang
        session.permanent = False  # Non-permanent session
        session['last_activity'] = datetime.utcnow()  # Initialize for new session
        
        logger.info("Cleared anonymous session data")
    except Exception as e:
        logger.error(f"Error clearing anonymous session: {str(e)}")

def update_session_language(language):
    """Update the session language."""
    try:
        if not has_request_context():
            return False
            
        if language in ['en', 'ha']:
            session['lang'] = language
            session.permanent = False  # Non-permanent session
            session['last_activity'] = datetime.utcnow()  # Update activity
            logger.info(f"Updated session language to: {language}")
            return True
        else:
            logger.warning(f"Invalid language code: {language}")
            return False
    except Exception as e:
        logger.error(f"Error updating session language: {str(e)}")
        return False

def get_session_language():
    """Get the current session language."""
    try:
        if not has_request_context():
            return 'en'
            
        return session.get('lang', 'en')
    except Exception:
        return 'en'

def extend_session():
    """Extend the session by updating last_activity."""
    try:
        if not has_request_context():
            return
            
        session.permanent = False  # Ensure non-permanent
        session['last_activity'] = datetime.utcnow()  # Update activity
        session.modified = True
    except Exception as e:
        logger.error(f"Error extending session: {str(e)}")

def get_session_info():
    """Get session information for debugging."""
    try:
        if not has_request_context():
            return {'error': 'No request context'}
            
        return {
            'sid': session.get('sid'),
            'is_anonymous': session.get('is_anonymous', False),
            'lang': session.get('lang', 'en'),
            'last_activity': session.get('last_activity'),
            'permanent': session.permanent
        }
    except Exception as e:
        logger.error(f"Error getting session info: {str(e)}")
        return {'error': str(e)}
