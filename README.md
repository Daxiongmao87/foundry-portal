<p align="center"><img src="./static/images/foundry-portal-icon-512.png" style="height: 256px;"/><p>
<p align="center"><img src="./foundry_portal_logo.png" style="height: 96px;" alt="Foundry Portal"/></p>

**Foundry Portal** is a web frontend for multiple [Foundry Virtual Tabletop (VTT)](https://foundryvtt.com/) instances. It provides real-time status updates of each instance, displays active worlds, and (optionally) offers seamless world activation across shared data environments.

## Features

- **Real-Time Monitoring**: Keeps track of the status (`offline`, `online`, `active`) of each configured Foundry instance using Foundry's own HTTP endpoints, so no browser automation is required on the server.
- **Active Worlds Display**: Showcases currently active worlds with details like player counts and hosting instances.
- **Shared Data Mode**: Enables an "Activate World" button when instances share the same data folder, allowing users to activate worlds across any available instance.
- **Dynamic Updates**: Utilizes a background scheduler to refresh instance statuses at regular intervals without manual intervention.
- **Responsive Design**: Optimized for various screen sizes to ensure a consistent user experience across devices.

## Screenshot
<p align="center"><img src="./foundry-portal-screenshot.jpg" style="max-width:80%;"/><p>
  
## Prerequisites

Before setting up Foundry Portal, ensure you have the following installed on your system:

- **Python 3.8+**
  - `pip`
  - `venv` (optional, but recommended)


## Installation

1. **Clone the Repository**

   ```bash
   git clone https://github.com/daxiongmao87/foundry-portal.git
   cd foundry-portal
   ```

2. **Create a Virtual Environment (Optional but Recommended)**

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install Dependencies**

   Run:

   ```bash
   pip install -r requirements.txt
   ```

   The portal only needs Flask, APScheduler, PyYAML and Werkzeug. Instance status is read over HTTP, so no browser or WebDriver is installed.

## Running With Docker
1. **Clone the Repository**

   ```bash
   git clone https://github.com/daxiongmao87/foundry-portal.git
   cd foundry-portal
   ```

2. **Build the image**

   ```
   docker build -t foundry-portal .
   ```

3. **Start a Docker container**

   ```
   docker run -d -p 5000:5000 \
   -v /path/to/your/config.yaml:/app/config.yaml \
   --name foundry_portal_container foundry-portal
   ```
   Ensuring that you replace "/path/to/your/config.yaml" with a path on your host to a config file. You can use config.template.yaml from this repo as a starting point.


## Configuration

1. **Copy the Configuration Template**

   ```bash
   cp config.template.yaml config.yaml
   ```

2. **Edit `config.yaml`**

   Open `config.yaml` in your preferred text editor and configure the settings:

   ```yaml
   shared_data_mode: false  # Set to true if all instances share the same data folder

   instances:
     - name: "Foundry 1"  # Descriptive name for the first Foundry instance
       url: "https://url.to/your/foundry/instance/1"  # URL of the first Foundry instance
     - name: "Foundry 2"  # Descriptive name for the second Foundry instance
       url: "https://url.to/your/foundry/instance/2"  # URL of the second Foundry instance
   ```

   - **`shared_data_mode`**: 
     - **`true`**: Enables the "Activate World" button, allowing activation across shared data instances.
     - **`false`**: Disables the "Activate World" button; instances operate independently.

   - **`instances`**: 
     - List each Foundry instance with a unique `name` and accessible `url`.

## Running the Application

1. **Start the Server**

   Launch the Flask application by running:

   ```bash
   python app.py
   ```
   
   This will start the Flask development server on `http://127.0.0.1:5000`.
  
   **Optional:** To make the portal accessible from other devices on your network, run:

   ```bash
   python app.py --host=0.0.0.0
   ```

   **Caution:** Running the Flask development server with `--host=0.0.0.0` exposes it to your local network. **It is highly recommended that you do not use the development server in a production environment.** For production deployments, consider using a production-ready web server like Gunicorn or uWSGI.

2. **Access the Portal**

Open your web browser and navigate to `http://127.0.0.1:5000` (or replace `127.0.0.1` with your server's IP address if accessible externally) to view the Foundry Portal dashboard.

## Usage

- **Active Worlds**: View all currently active worlds across your Foundry instances. Click on a world card to navigate directly to the active world's session.
- **Instance Status**: Monitor the status of each configured Foundry instance. Status indicators:
  - **Active**: Instance is actively hosting a world.
  - **Online**: Instance is online but not actively hosting a world.
  - **Offline**: Instance is not reachable.
- **Activate World**: If `shared_data_mode` is enabled, use the "Activate World" button to redirect to any available online instance to activate a world.
- **Player Counts**: Active worlds show `Players: N connected`. Foundry's `/api/status` endpoint reports connected players only, not the configured slot limit, so no maximum is shown.

## Administration

The gear icon in the header opens the admin dialog.

- **First run**: with no admin password stored, the portal serves a setup form, and that form is reachable only from loopback (`http://127.0.0.1:5000`). Set `PORTAL_ALLOW_REMOTE_INIT=1` if you must bootstrap through a reverse proxy, and unset it afterwards.
- **Password recovery**: run `python3 reset_password.py` in the portal directory on the server and follow the prompt.
- **Viewer password**: an optional second password in the configuration dialog gates read-only access to the dashboard and `/api/instance-status`.
- **Session secret**: a random key is generated on first start and stored in `.secret_key` (mode `0600`) so sessions survive restarts. Set `PORTAL_SECRET_KEY`, or `PORTAL_SECRET_FILE` for another location, to manage it yourself.
- **Configuration writes**: the configuration dialog validates every instance name and URL and writes `config.yaml` atomically, so a rejected payload cannot leave a half-written file behind.

## Troubleshooting

- **All Instances Offline**:
  - The portal probes `<instance url>/api/status` and falls back to the `<instance url>/join` page. Both must be reachable from the machine running the portal; test with `curl -sS <instance url>/api/status`.
  - Foundry only serves `/api/status` once it is listening, so an instance that is starting up legitimately reports `offline` until it answers.

- **Configuration Errors**:
  - Ensure `config.yaml` adheres to proper YAML syntax.
  - Verify that all instance URLs are correct and accessible from the server running the portal.

- **Port Conflicts**:
  - If port `5000` is already in use, modify the `app.run()` line in `app.py` to use a different port:

    ```python
    app.run(debug=True, port=5001)
    ```

## Tests

The suite is stdlib `unittest` and needs no browser:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
```

Each file is also runnable on its own, for example `PYTHONPATH=. python3 tests/test_status.py`.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request with your enhancements or bug fixes.

## License

This project is licensed under the [MIT License](LICENSE.txt).

## Acknowledgements

- Developed by [Daxiongmao87](https://www.github.com/Daxiongmao87).
- Foundry Portal is not affiliated with Foundry Gaming LLC.
