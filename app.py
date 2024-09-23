from flask import Flask, render_template, jsonify
import yaml
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from urllib.parse import urljoin
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# Initialize the Flask application
app = Flask(__name__)

# Global cache to store instance data
instance_data_cache = []

def load_config():
    """
    Load the configuration from the 'config.yaml' file.
    Ensures that 'shared_data_mode' is set, defaulting to False if not present.
    
    Returns:
        dict: Configuration dictionary containing shared_data_mode and instances.
    """
    with open('config.yaml', 'r') as file:
        config = yaml.safe_load(file)
    config['shared_data_mode'] = config.get('shared_data_mode', False)
    return config

def check_instance_status(instance_url):
    """
    Check the status of a Foundry instance by navigating to its URL using Selenium.
    Determines if the instance is offline, online, or active based on the current URL and page content.
    
    Args:
        instance_url (str): The URL of the Foundry instance to check.
    
    Returns:
        tuple: A tuple containing the status ('offline', 'online', 'active'), 
               active_world (dict or None), and background_url (str or None).
    """
    # Configure Selenium Chrome options for headless browsing
    options = Options()
    options.add_argument('--headless')  # Run browser in headless mode
    options.add_argument('--no-sandbox')  # Bypass OS security model
    options.add_argument('--disable-dev-shm-usage')  # Overcome limited resource problems
    options.add_argument('--disable-gpu')  # Disable GPU usage
    options.add_argument('window-size=1920x1080')  # Set window size
    options.add_argument('--ignore-certificate-errors')  # Ignore SSL certificate errors

    # Initialize the Chrome WebDriver with the specified options
    driver = webdriver.Chrome(options=options)

    # Default status and data
    status = "offline"
    active_world = None
    background_url = None

    try:
        # Navigate to the instance URL
        driver.get(instance_url)

        # Check if the current URL indicates an authentication page
        if "/auth" in driver.current_url:
            status = "online"
            # Execute JavaScript to retrieve the background URL from CSS variables
            background_url = driver.execute_script("""
                var background = getComputedStyle(document.body).getPropertyValue('--background-url').trim();
                background = background.replace(/^url\\(["']?/, '').replace(/["']?\\)$/, '');
                var link = document.createElement('a');
                link.href = background;
                return link.href;
            """)

        # Check if the current URL indicates an active world
        elif "/join" in driver.current_url:
            # Wait until the page title contains a specific condition (empty string here)
            WebDriverWait(driver, 10).until(EC.title_contains(""))
            world_name = driver.title

            if world_name:
                # Retrieve the background URL using JavaScript
                background_url = driver.execute_script("""
                    var background = getComputedStyle(document.body).getPropertyValue('--background-url').trim();
                    background = background.replace(/^url\\(["']?/, '').replace(/["']?\\)$/, '');
                    var link = document.createElement('a');
                    link.href = background;
                    return link.href;
                """)

                try:
                    # Wait until the player count elements are present on the page
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, '.current-players .count'))
                    )
                    # Extract current and maximum player counts
                    current_players = driver.find_elements(By.CSS_SELECTOR, '.current-players .count')[0].text
                    max_players = driver.find_elements(By.CSS_SELECTOR, '.current-players .count')[1].text
                    player_info = f"{current_players} / {max_players}"
                except (IndexError, TimeoutException):
                    # Handle cases where player information is unavailable
                    player_info = "Unknown / Unknown"

                if world_name and background_url:
                    # Populate the active_world dictionary with relevant information
                    active_world = {
                        'name': world_name,
                        'background': background_url,
                        'players': player_info
                    }
                    status = "active"
    except (TimeoutException, WebDriverException):
        # In case of any Selenium-related exceptions, mark the instance as offline
        status = "offline"
    finally:
        # Ensure the WebDriver is properly closed
        driver.quit()

    return status, active_world, background_url

def initialize_instance_data():
    """
    Initialize the instance data cache with default values from the configuration.
    Sets all instances to 'offline' initially with default background images.
    """
    global instance_data_cache
    config = load_config()
    instances = []

    for instance in config['instances']:
        instance_data = {
            'name': instance['name'],
            'url': instance['url'],
            'status': 'offline',  # Default status
            'active_world': None,  # No active world initially
            'background': '/static/images/background.jpg'  # Default background image
        }
        instances.append(instance_data)

    instance_data_cache = instances

def update_instance_statuses():
    """
    Update the status of all configured instances by checking each one's current state.
    Refreshes the global instance_data_cache with the latest information.
    """
    global instance_data_cache
    config = load_config()
    instances = []

    for instance in config['instances']:
        # Check the status of each instance
        status, active_world, background_url = check_instance_status(instance['url'])
        instance_data = {
            'name': instance['name'],
            'url': instance['url'],
            'status': status,
            'active_world': active_world,
            'background': background_url if background_url else '/static/images/background.jpg'
        }
        instances.append(instance_data)

    # Update the global cache with the latest instance data
    instance_data_cache = instances
    print("Instance statuses updated.")

@app.route('/api/instance-status')
def api_instance_status():
    """
    API endpoint to retrieve the current status of all instances.
    
    Returns:
        Response: JSON response containing the instance data cache.
    """
    return jsonify(instance_data_cache)

@app.route('/')
def home():
    """
    Home route that renders the main page with instance data and shared data mode.
    
    Returns:
        Response: Rendered HTML template for the homepage.
    """
    config = load_config()
    return render_template('index.html', instances=instance_data_cache, shared_data_mode=config['shared_data_mode'])

# Initialize the background scheduler
scheduler = BackgroundScheduler()
# Schedule the 'update_instance_statuses' function to run every 10 seconds
scheduler.add_job(func=update_instance_statuses, trigger="interval", seconds=10)
scheduler.start()

# Ensure that the scheduler shuts down gracefully when the application exits
atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    # Initialize instance data and perform an initial status update before starting the server
    initialize_instance_data()
    update_instance_statuses()
    # Run the Flask development server with debug mode enabled
    app.run(debug=True)
