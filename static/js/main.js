/* Foundry Portal front-end.
 *
 * Statuses are rendered from /api/instance-status. Every card is a real link
 * so the dashboard works with a keyboard, and all text reaches the DOM through
 * textContent so a world title cannot inject markup.
 */
(function () {
    "use strict";

    var POLL_INTERVAL_MS = 10000;
    var STATUS_LABELS = {
        active: "Active",
        online: "Online",
        offline: "Offline",
        error: "Error"
    };

    var state = readPortalState();
    var sharedDataMode = state.sharedDataMode;
    var lastSignature = "";
    var lastFetchedAt = null;
    var fetchFailed = false;

    var elements = {
        worldsGallery: document.getElementById("worlds-gallery"),
        instanceList: document.getElementById("instance-list"),
        lastUpdated: document.getElementById("last-updated"),
        initModal: document.getElementById("init-modal"),
        loginModal: document.getElementById("login-modal"),
        configModal: document.getElementById("config-modal"),
        viewerLock: document.getElementById("viewer-lock"),
        adminBtn: document.getElementById("admin-btn"),
        instancesContainer: document.getElementById("instances-container")
    };

    function readPortalState() {
        var node = document.getElementById("portal-state");
        if (!node) {
            return { isConfigured: false, viewerLocked: false, isAdmin: false, sharedDataMode: false };
        }
        try {
            return JSON.parse(node.textContent);
        } catch (err) {
            console.error("Could not read portal state", err);
            return { isConfigured: false, viewerLocked: false, isAdmin: false, sharedDataMode: false };
        }
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = String(text);
        }
        return node;
    }

    function safeUrl(candidate, base) {
        if (!candidate) {
            return null;
        }
        try {
            var url = new URL(candidate, base || window.location.origin);
            if (url.protocol === "http:" || url.protocol === "https:") {
                return url.href;
            }
        } catch (err) {
            /* fall through */
        }
        return null;
    }

    function applyBackground(node, candidate, base) {
        var url = safeUrl(candidate, base);
        if (url) {
            node.style.backgroundImage = 'url("' + url.replace(/"/g, "%22") + '")';
        }
    }

    function joinUrl(instanceUrl) {
        var base = safeUrl(instanceUrl);
        if (!base) {
            return null;
        }
        return base.replace(/\/+$/, "") + "/join";
    }

    // --- Dashboard ---------------------------------------------------------

    function buildWorldCard(instance) {
        var world = instance.active_world;
        var card = el("a", "world-card");
        var target = joinUrl(instance.url);
        if (target) {
            card.href = target;
        } else {
            card.href = "#";
            card.setAttribute("aria-disabled", "true");
        }
        applyBackground(card, world.background || instance.background, instance.url);

        var info = el("div", "world-card-info");
        info.appendChild(el("h3", null, world.name || "Unnamed world"));
        if (world.players) {
            info_meta(info, "Players: " + world.players);
        }
        info_meta(info, "Hosted on: " + instance.name);
        card.appendChild(info);
        return card;
    }

    function info_meta(parent, text) {
        parent.appendChild(el("p", "world-card-meta", text));
    }

    function buildActivateCard(instances) {
        var available = instances.filter(function (instance) {
            return instance.status === "online" || instance.status === "active";
        })[0];
        if (!available) {
            return null;
        }
        var card = el("a", "world-card add-new");
        var target = joinUrl(available.url);
        card.href = target || "#";
        card.appendChild(el("h3", null, "Activate World"));
        card.appendChild(el("span", "add-new-icon", "+"));
        card.setAttribute("aria-label", "Activate a world on " + available.name);
        return card;
    }

    function buildInstanceCard(instance) {
        var status = STATUS_LABELS[instance.status] ? instance.status : "error";
        var card = el("a", "instance-card " + status);
        var target = safeUrl(instance.url);
        if (target) {
            card.href = target;
        } else {
            card.href = "#";
            card.setAttribute("aria-disabled", "true");
        }
        applyBackground(card, instance.background, instance.url);

        var info = el("div", "instance-info-container");
        var header = el("div", "instance-header");
        var dot = el("span", "status-indicator " + instance.status);
        var label = STATUS_LABELS[instance.status] || "Unknown";
        dot.setAttribute("title", label);
        header.appendChild(dot);
        header.appendChild(el("h3", null, instance.name));
        header.appendChild(el("span", "visually-hidden", "Status: " + label));
        info.appendChild(header);
        info.appendChild(el("p", "instance-url", instance.url));
        card.appendChild(info);
        return card;
    }

    function render(instances) {
        var gallery = elements.worldsGallery;
        var list = elements.instanceList;
        gallery.textContent = "";
        list.textContent = "";

        var activeWorlds = instances.filter(function (instance) {
            return instance.status === "active" && instance.active_world;
        });

        activeWorlds.forEach(function (instance) {
            gallery.appendChild(buildWorldCard(instance));
        });

        if (sharedDataMode) {
            var activate = buildActivateCard(instances);
            if (activate) {
                gallery.appendChild(activate);
            }
        }

        if (!gallery.childElementCount) {
            gallery.appendChild(el("p", "no-worlds", "No worlds are active right now."));
        }

        instances.forEach(function (instance) {
            list.appendChild(buildInstanceCard(instance));
        });
        if (!instances.length) {
            list.appendChild(el("p", "no-worlds", "No instances are configured."));
        }
    }

    function renderUpdated() {
        if (!elements.lastUpdated) {
            return;
        }
        if (fetchFailed || !lastFetchedAt) {
            elements.lastUpdated.textContent = "Status unavailable - retrying.";
            return;
        }
        var ageSeconds = Math.round((Date.now() - lastFetchedAt) / 1000);
        elements.lastUpdated.textContent = "Updated " + ageSeconds + "s ago";
    }

    function fetchStatus() {
        var request = new Request("/api/instance-status", { credentials: "same-origin" });
        fetch(request)
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Status request failed with HTTP " + response.status);
                }
                return response.json();
            })
            .then(function (instances) {
                fetchFailed = false;
                lastFetchedAt = Date.now();
                var signature = JSON.stringify(instances);
                if (signature !== lastSignature) {
                    lastSignature = signature;
                    render(instances);
                }
                renderUpdated();
            })
            .catch(function (err) {
                console.error("Error fetching instance statuses:", err);
                fetchFailed = true;
                renderUpdated();
            });
    }

    // --- Modals ------------------------------------------------------------

    function showModal(modal) {
        if (modal) {
            modal.hidden = false;
            var focusable = modal.querySelector("input, button");
            if (focusable) {
                focusable.focus();
            }
        }
    }

    function hideModal(modal) {
        if (modal) {
            modal.hidden = true;
        }
    }

    function postJson(url, body) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify(body)
        });
    }

    function wireModals() {
        Array.prototype.forEach.call(document.querySelectorAll(".close"), function (button) {
            button.addEventListener("click", function () {
                hideModal(button.closest(".modal"));
            });
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                Array.prototype.forEach.call(document.querySelectorAll(".modal, .overlay"), function (modal) {
                    if (modal.id !== "viewer-lock" && !modal.hidden) {
                        hideModal(modal);
                    }
                });
            }
        });

        var initForm = document.getElementById("init-form");
        if (initForm) {
            initForm.addEventListener("submit", function (event) {
                event.preventDefault();
                var password = document.getElementById("init-password").value;
                postJson("/api/init", { admin_password: password })
                    .then(function (response) {
                        if (response.ok) {
                            window.location.reload();
                        } else {
                            window.alert("Initialization failed. On a hosted install it must be run on the server itself.");
                        }
                    })
                    .catch(function (err) {
                        console.error(err);
                        window.alert("Error initializing");
                    });
            });
        }

        var loginForm = document.getElementById("login-form");
        if (loginForm) {
            loginForm.addEventListener("submit", function (event) {
                event.preventDefault();
                var password = document.getElementById("login-password").value;
                var role = document.getElementById("login-role").value;
                postJson("/api/login", { password: password, role: role })
                    .then(function (response) {
                        if (response.ok) {
                            hideModal(elements.loginModal);
                            state.isAdmin = true;
                            if (role === "admin") {
                                openConfigModal();
                            }
                        } else if (response.status === 429) {
                            window.alert("Too many attempts. Wait a few minutes and try again.");
                        } else {
                            window.alert("Invalid password");
                        }
                    })
                    .catch(function (err) {
                        console.error(err);
                        window.alert("Login error");
                    });
            });
        }

        var viewerForm = document.getElementById("viewer-form");
        if (viewerForm) {
            viewerForm.addEventListener("submit", function (event) {
                event.preventDefault();
                var password = document.getElementById("viewer-password").value;
                postJson("/api/login", { password: password, role: "viewer" })
                    .then(function (response) {
                        if (response.ok) {
                            window.location.reload();
                        } else {
                            window.alert("Invalid password");
                        }
                    })
                    .catch(function (err) {
                        console.error(err);
                        window.alert("Login error");
                    });
            });
        }

        var configForm = document.getElementById("config-form");
        if (configForm) {
            configForm.addEventListener("submit", function (event) {
                event.preventDefault();
                var payload = {
                    shared_data_mode: document.getElementById("shared-data-mode").checked,
                    instances: instancesFromDom(),
                    new_admin_password: document.getElementById("new-admin-password").value,
                    new_viewer_password: document.getElementById("new-viewer-password").value
                };
                postJson("/api/config", payload)
                    .then(function (response) {
                        if (response.ok) {
                            window.location.reload();
                        } else {
                            return response.json().then(function (body) {
                                window.alert(body.error || "Failed to save configuration");
                            });
                        }
                    })
                    .catch(function (err) {
                        console.error(err);
                        window.alert("Error saving configuration");
                    });
            });
        }

        var addInstanceBtn = document.getElementById("add-instance-btn");
        if (addInstanceBtn) {
            addInstanceBtn.addEventListener("click", function () {
                addInstanceRow();
            });
        }

        if (elements.adminBtn) {
            elements.adminBtn.addEventListener("click", function () {
                if (state.isAdmin) {
                    openConfigModal();
                } else {
                    showModal(elements.loginModal);
                }
            });
        }
    }

    function addInstanceRow(data) {
        var row = data || { name: "", url: "" };
        var container = elements.instancesContainer;
        var wrapper = el("div", "instance-row form-group");

        var name = document.createElement("input");
        name.type = "text";
        name.placeholder = "Name";
        name.className = "instance-name";
        name.value = row.name || "";
        name.setAttribute("aria-label", "Instance name");
        name.required = true;

        var url = document.createElement("input");
        url.type = "url";
        url.placeholder = "https://foundry.example.com";
        url.className = "instance-address";
        url.value = row.url || "";
        url.setAttribute("aria-label", "Instance URL");
        url.required = true;

        var remove = el("button", "btn-danger remove-instance", "\u00d7");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove instance");
        remove.addEventListener("click", function () {
            wrapper.remove();
        });

        wrapper.appendChild(name);
        wrapper.appendChild(url);
        wrapper.appendChild(remove);
        container.appendChild(wrapper);
    }

    function instancesFromDom() {
        return Array.prototype.map.call(
            document.querySelectorAll(".instance-row"),
            function (row) {
                return {
                    name: row.querySelector(".instance-name").value,
                    url: row.querySelector(".instance-address").value
                };
            }
        );
    }

    function openConfigModal() {
        fetch("/api/config", { credentials: "same-origin" })
            .then(function (response) {
                if (!response.ok) {
                    state.isAdmin = false;
                    showModal(elements.loginModal);
                    return null;
                }
                return response.json();
            })
            .then(function (config) {
                if (!config) {
                    return;
                }
                document.getElementById("shared-data-mode").checked = Boolean(config.shared_data_mode);
                elements.instancesContainer.textContent = "";
                (config.instances || []).forEach(function (instance) {
                    addInstanceRow(instance);
                });
                document.getElementById("new-admin-password").value = "";
                document.getElementById("new-viewer-password").value = "";
                showModal(elements.configModal);
            })
            .catch(function (err) {
                console.error(err);
            });
    }

    // --- Boot --------------------------------------------------------------

    function boot() {
        wireModals();

        if (!state.isConfigured) {
            showModal(elements.initModal);
            return;
        }
        if (state.viewerLocked) {
            showModal(elements.viewerLock);
            return;
        }

        fetchStatus();
        setInterval(fetchStatus, POLL_INTERVAL_MS);
        setInterval(renderUpdated, 1000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
