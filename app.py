import atexit
import json
import os
import re
import ssl
import urllib.request
from functools import wraps
from html.parser import HTMLParser

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

# Initialize the Flask application
app = Flask(__name__)
app.secret_key = os.urandom(24)  # Secret key for session management

# Global cache to store instance data
instance_data_cache = []
CONFIG_FILE = 'config.yaml'

def load_config():
    """
    Load the configuration from the 'config.yaml' file.
    Returns:
        dict: Configuration dictionary.
    """
    if not os.path.exists(CONFIG_FILE):
        print(f"DEBUG: Config file {CONFIG_FILE} NOT found.")
        return {}
    with open(CONFIG_FILE, 'r') as file:
        config = yaml.safe_load(file) or {}
        print(f"DEBUG: Loaded config. Keys: {config.keys()}")
        if 'admin_password_hash' in config:
            print("DEBUG: admin_password_hash is present.")
        else:
            print("DEBUG: admin_password_hash is MISSING.")
        return config

def save_config(config):
    """
    Save the configuration to 'config.yaml'.
    """
    with open(CONFIG_FILE, 'w') as file:
        yaml.dump(config, file)

class TitleParser(HTMLParser):
    """Extract the title and text content from a Foundry join page."""

    def __init__(self):
        super().__init__()
        self.title = None
        self.text_parts = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'title':
            self._in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == 'title':
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        self.text_parts.append(text)
        if self._in_title and self.title is None:
            self.title = text

    @property
    def text(self):
        return ' '.join(self.text_parts)


def fetch_url(url, timeout=10):
    """Fetch a URL while allowing Foundry instances with self-signed TLS."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        url,
        headers={'User-Agent': 'FoundryPortal/1.0'},
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=context,
        ) as response:
            return response.read().decode('utf-8')
    except Exception:
        return None


def parse_join_page(join_html):
    """Extract an active world's title and player count from a join page."""
    parser = TitleParser()
    try:
        parser.feed(join_html)
    except (TypeError, ValueError):
        return None, "Unknown / Unknown"

    player_info = "Unknown / Unknown"
    player_match = re.search(
        r"Current\s+Players\s*(\d+)\s*/\s*(\d+)",
        parser.text,
        flags=re.IGNORECASE,
    )
    if player_match:
        player_info = f"{player_match.group(1)} / {player_match.group(2)}"

    return parser.title, player_info


def check_instance_status(instance_url):
    """Check a Foundry instance through its built-in status HTTP API."""
    base_url = instance_url.rstrip('/')
    background_url = None

    api_response = fetch_url(base_url + '/api/status')
    try:
        data = json.loads(api_response) if api_response is not None else None
    except (json.JSONDecodeError, TypeError):
        data = None

    if not isinstance(data, dict):
        # Older Foundry releases do not provide /api/status. Their join page
        # still exposes the active world's title and player count.
        join_html = fetch_url(base_url + '/join')
        if join_html is not None:
            world_name, player_info = parse_join_page(join_html)
            if world_name:
                active_world = {
                    'name': world_name,
                    'background': '/static/images/background.jpg',
                    'players': player_info,
                }
                return "active", active_world, None
            return "online", None, None

        if fetch_url(base_url + '/auth') is not None:
            return "online", None, None
        return "offline", None, None

    raw_background = data.get('background', '')
    if isinstance(raw_background, str) and raw_background:
        if raw_background.startswith(('http://', 'https://')):
            background_url = raw_background
        else:
            background_url = base_url + '/' + raw_background.lstrip('/')

    if data.get('active') and data.get('world'):
        world_name = data['world']
        player_info = "Unknown / Unknown"
        join_html = fetch_url(base_url + '/join')
        if join_html:
            parser = TitleParser()
            try:
                parser.feed(join_html)
            except (TypeError, ValueError):
                pass
            if parser.title:
                world_name = parser.title

            player_match = re.search(
                r"Current\s+Players\s*(\d+)\s*/\s*(\d+)",
                parser.text,
                flags=re.IGNORECASE,
            )
            if player_match:
                player_info = f"{player_match.group(1)} / {player_match.group(2)}"

        active_world = {
            'name': world_name,
            'background': background_url or '/static/images/background.jpg',
            'players': player_info,
        }
        return "active", active_world, background_url

    return "online", None, background_url

def initialize_instance_data():
    global instance_data_cache
    config = load_config()
    instances = []

    if 'instances' in config:
        for instance in config['instances']:
            instance_data = {
                'name': instance['name'],
                'url': instance['url'],
                'status': 'offline',
                'active_world': None,
                'background': '/static/images/background.jpg'
            }
            instances.append(instance_data)

    instance_data_cache = instances

def update_instance_statuses():
    global instance_data_cache
    config = load_config()
    instances = []

    if 'instances' in config:
        for instance in config['instances']:
            status, active_world, background_url = check_instance_status(instance['url'])
            instance_data = {
                'name': instance['name'],
                'url': instance['url'],
                'status': status,
                'active_world': active_world,
                'background': background_url if background_url else '/static/images/background.jpg'
            }
            instances.append(instance_data)

    instance_data_cache = instances
    print("Instance statuses updated.")

# --- Authentication Decorators ---

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

def viewer_auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        config = load_config()
        # If viewer password is set and user is not logged in as viewer or admin
        if config.get('viewer_password_hash') and not (session.get('viewer_logged_in') or session.get('admin_logged_in')):
             # For API calls, return 401. For page loads, we might handle differently in frontend, 
             # but here we just check session.
             # Actually, for the main page, we pass a flag to the template.
             pass 
        return f(*args, **kwargs)
    return decorated_function

# --- Routes ---

@app.route('/api/instance-status')
def api_instance_status():
    return jsonify(instance_data_cache)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    password = data.get('password')
    role = data.get('role', 'admin') # 'admin' or 'viewer'
    config = load_config()

    if role == 'admin':
        if config.get('admin_password_hash') and check_password_hash(config['admin_password_hash'], password):
            session['admin_logged_in'] = True
            return jsonify({'success': True})
    elif role == 'viewer':
        if config.get('viewer_password_hash') and check_password_hash(config['viewer_password_hash'], password):
            session['viewer_logged_in'] = True
            return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'Invalid password'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/config', methods=['GET', 'POST'])
@admin_required
def handle_config():
    if request.method == 'GET':
        config = load_config()
        # Don't send hashes back
        safe_config = {
            'shared_data_mode': config.get('shared_data_mode', False),
            'instances': config.get('instances', []),
            'viewer_access_enabled': bool(config.get('viewer_password_hash'))
        }
        return jsonify(safe_config)
    
    if request.method == 'POST':
        new_data = request.json
        config = load_config()
        
        # Update fields
        if 'shared_data_mode' in new_data:
            config['shared_data_mode'] = new_data['shared_data_mode']
        if 'instances' in new_data:
            config['instances'] = new_data['instances']
        
        # Handle password updates
        if 'new_admin_password' in new_data and new_data['new_admin_password']:
            config['admin_password_hash'] = generate_password_hash(new_data['new_admin_password'])
            
        if 'new_viewer_password' in new_data:
            if new_data['new_viewer_password']:
                config['viewer_password_hash'] = generate_password_hash(new_data['new_viewer_password'])
            else:
                # If empty, disable viewer access (remove hash)
                config.pop('viewer_password_hash', None)

        save_config(config)
        # Trigger update immediately
        update_instance_statuses()
        return jsonify({'success': True})

@app.route('/api/init', methods=['POST'])
def init_config():
    """Endpoint for initial setup if no config exists."""
    if os.path.exists(CONFIG_FILE) and load_config().get('admin_password_hash'):
         return jsonify({'error': 'Already configured'}), 403
    
    data = request.json
    password = data.get('admin_password')
    if not password:
        return jsonify({'error': 'Password required'}), 400
        
    config = {
        'admin_password_hash': generate_password_hash(password),
        'shared_data_mode': False,
        'instances': []
    }
    save_config(config)
    return jsonify({'success': True})

@app.route('/')
def home():
    config = load_config()
    
    # Check if configured
    is_configured = bool(config.get('admin_password_hash'))
    print(f"DEBUG: home route - is_configured: {is_configured}")
    
    # Check viewer access
    viewer_locked = False
    if is_configured and config.get('viewer_password_hash'):
        if not (session.get('viewer_logged_in') or session.get('admin_logged_in')):
            viewer_locked = True
    
    print(f"DEBUG: home route - viewer_locked: {viewer_locked}")

    return render_template('index.html', 
                           instances=instance_data_cache, 
                           shared_data_mode=config.get('shared_data_mode', False),
                           is_configured=is_configured,
                           viewer_locked=viewer_locked,
                           is_admin=session.get('admin_logged_in', False))

# Initialize the background scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=update_instance_statuses, trigger="interval", seconds=30)
scheduler.start()

atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    initialize_instance_data()
    update_instance_statuses()
    app.run(host='0.0.0.0', port=5000)
