document.addEventListener('DOMContentLoaded', function() {
    // Determine if shared_data_mode is enabled based on data attribute from the main script
    const sharedDataMode = document.getElementById("main-script").dataset.sharedDataMode === 'true';
    
    /**
     * Fetch the current statuses of all instances from the API.
     */
    function fetchInstanceStatuses() {
        fetch('/api/instance-status')
            .then(response => response.json())
            .then(instances => {
                updateActiveWorlds(instances);
                updateInstanceStatuses(instances);
            })
            .catch(error => console.error('Error fetching instance statuses:', error));
    }

    /**
     * Update the Active Worlds section with current active worlds.
     * @param {Array} instances - List of instance objects with their statuses.
     */
    function updateActiveWorlds(instances) {
        const worldsGallery = document.getElementById('worlds-gallery');
        worldsGallery.innerHTML = ''; // Clear existing content

        instances.forEach(instance => {
            if (instance.status === 'active' && instance.active_world) {
                // Create world card element
                const worldCard = document.createElement('div');
                worldCard.className = 'world-card';
                worldCard.style.backgroundImage = `url(${instance.active_world.background})`;
                worldCard.style.backgroundSize = 'cover';
                worldCard.style.backgroundPosition = 'center';

                // World name
                const worldName = document.createElement('h3');
                worldName.textContent = instance.active_world.name;
                worldCard.appendChild(worldName);

                // Player information
                const playerInfo = document.createElement('p');
                playerInfo.textContent = `Players: ${instance.active_world.players}`;
                worldCard.appendChild(playerInfo);

                // Hosting instance information
                const instanceInfo = document.createElement('p');
                instanceInfo.textContent = `Hosted on: ${instance.name}`;
                worldCard.appendChild(instanceInfo);

                // Redirect to instance URL on click
                worldCard.addEventListener('click', function() {
                    window.location.href = instance.url;
                });

                worldsGallery.appendChild(worldCard);
            }
        });

        // If shared_data_mode is enabled, add the "Activate World" button
        if (sharedDataMode) {
            const hasOnlineInstance = instances.some(instance => instance.status === 'online');

            if (hasOnlineInstance) {
                // Create "Activate World" card
                const addNewCard = document.createElement('div');
                addNewCard.className = 'world-card add-new';

                const addNewTitle = document.createElement('h3');
                addNewTitle.textContent = 'Activate World';
                addNewCard.appendChild(addNewTitle);

                const icon = document.createElement('h2');
                icon.textContent = '+';
                addNewCard.appendChild(icon);

                // Redirect to an available instance on click
                addNewCard.addEventListener('click', function() {
                    const availableInstance = instances.find(inst => inst.status === 'online');
                    if (availableInstance) {
                        window.location.href = availableInstance.url;
                    } else {
                        alert('No available instances to activate a world.');
                    }
                });

                worldsGallery.appendChild(addNewCard);
            }
        }
    }

    /**
     * Fetch the background URL from a specific instance.
     * @param {string} instanceUrl - The URL of the instance to fetch from.
     * @returns {string|null} - The background URL or null if not found.
     */
    async function fetchBackgroundFromInstance(instanceUrl) {
        try {
            const response = await fetch(instanceUrl, { mode: 'no-cors' });
            const htmlText = await response.text();

            const parser = new DOMParser();
            const doc = parser.parseFromString(htmlText, 'text/html');

            const bodyStyle = window.getComputedStyle(doc.body);
            const backgroundUrl = bodyStyle.getPropertyValue('--background-url').slice(4, -1);

            return backgroundUrl || null;
        } catch (error) {
            console.error(`Error fetching background from ${instanceUrl}:`, error);
            return null;
        }
    }

    /**
     * Update the Instances section with current instance statuses.
     * @param {Array} instances - List of instance objects with their statuses.
     */
    function updateInstanceStatuses(instances) {
        const instanceList = document.getElementById('instance-list');

        // Get names of currently displayed instances
        const existingInstances = [...instanceList.children].map(child => child.getAttribute('data-instance-name'));

        instances.forEach(instance => {
            if (!existingInstances.includes(instance.name)) {
                // Create new instance card
                const instanceCard = document.createElement('div');
                instanceCard.className = 'instance-card';
                instanceCard.setAttribute('data-instance-name', instance.name);

                const instanceInfoContainer = document.createElement('div');
                instanceInfoContainer.className = 'instance-info-container';

                const instanceHeader = document.createElement('h3');

                // Status indicator
                const statusIndicator = document.createElement('span');
                statusIndicator.className = `status-indicator ${instance.status}`;
                statusIndicator.style.display = 'inline-block';
                statusIndicator.style.marginRight = '5px';

                instanceHeader.appendChild(statusIndicator);
                instanceHeader.appendChild(document.createTextNode(instance.name));
                instanceInfoContainer.appendChild(instanceHeader);

                // Instance URL
                const instanceUrlParagraph = document.createElement('p');
                instanceUrlParagraph.textContent = instance.url;
                instanceUrlParagraph.classList.add('instance-url');
                instanceInfoContainer.appendChild(instanceUrlParagraph);

                instanceCard.appendChild(instanceInfoContainer);

                // Set background image if available
                if (instance.background) {
                    instanceCard.style.backgroundImage = `url(${instance.background})`;
                    instanceCard.style.backgroundSize = 'cover';
                    instanceCard.style.backgroundPosition = 'center';
                }

                // Redirect to instance URL on click
                instanceCard.addEventListener('click', function() {
                    window.location.href = instance.url;
                });

                instanceList.appendChild(instanceCard);
            } else {
                // Update existing instance card
                const existingCard = document.querySelector(`[data-instance-name="${instance.name}"]`);
                if (existingCard) {
                    const statusIndicator = existingCard.querySelector('.status-indicator');
                    statusIndicator.className = `status-indicator ${instance.status}`;

                    if (instance.background) {
                        existingCard.style.backgroundImage = `url(${instance.background})`;
                    }
                }
            }
        });

        // Remove instance cards that no longer exist
        const newInstanceNames = instances.map(inst => inst.name);
        [...instanceList.children].forEach(child => {
            if (!newInstanceNames.includes(child.getAttribute('data-instance-name'))) {
                instanceList.removeChild(child);
            }
        });
    }

    // Initial fetch of instance statuses
    fetchInstanceStatuses();

    // Periodically update instance statuses every 5 seconds
    setInterval(fetchInstanceStatuses, 5000);
});
