from flask import Flask, request, jsonify, render_template_string
import requests
import yaml
import re

app = Flask(__name__)

DOCKER_HOST = "http://192.168.192.136:2375"  # Change to your Docker API endpoint

last_deploy_logs = []  # store logs/errors to show on UI

def parse_duration(duration):
    units = {
        'ns': 1,
        'us': 1_000,
        'ms': 1_000_000,
        's': 1_000_000_000,
        'm': 60 * 1_000_000_000,
        'h': 3600 * 1_000_000_000,
    }
    if isinstance(duration, int):
        return duration * 1_000_000_000
    match = re.match(r'^(\d+)([a-z]+)?$', duration.strip())
    if not match:
        raise ValueError(f"Invalid duration: {duration}")
    value, unit = match.groups()
    unit = unit or 's'
    return int(value) * units.get(unit, 1)

def deploy_compose(compose_text):
    global last_deploy_logs
    last_deploy_logs = []
    try:
        compose = yaml.safe_load(compose_text)
    except Exception as e:
        last_deploy_logs.append(f"YAML parse error: {e}")
        return False, "Invalid YAML file"

    # 1. Create networks
    networks = compose.get("networks", {})
    for name, config in networks.items():
        if config is None:
            config = {}
        payload = {
            "Name": name,
            "Driver": config.get("driver", "bridge"),
            "CheckDuplicate": True
        }
        r = requests.post(f"{DOCKER_HOST}/networks/create", json=payload)
        last_deploy_logs.append(f"Network '{name}': {r.status_code} {r.text}")

    # 2. Pull images and create containers
    services = compose.get("services", {})
    for name, svc in services.items():
        image = svc.get("image")
        if not image:
            last_deploy_logs.append(f"Service {name} missing image, skipping")
            continue

        # Pull image
        r = requests.post(f"{DOCKER_HOST}/images/create", params={"fromImage": image})
        last_deploy_logs.append(f"Pull image '{image}': {r.status_code}")

        # Ports
        port_bindings = {}
        exposed_ports = {}
        for port in svc.get("ports", []):
            try:
                host, container = port.split(":")
                port_proto = container + "/tcp"
                port_bindings[port_proto] = [{"HostPort": host}]
                exposed_ports[port_proto] = {}
            except Exception as e:
                last_deploy_logs.append(f"Port parse error for service {name} port '{port}': {e}")

        # Healthcheck
        healthcheck = svc.get("healthcheck", {})
        hc_config = {}
        if healthcheck:
            try:
                hc_config = {
                    "Test": healthcheck.get("test", ["CMD-SHELL", "exit 1"]),
                    "Interval": parse_duration(healthcheck.get("interval", "30s")),
                    "Timeout": parse_duration(healthcheck.get("timeout", "30s")),
                    "Retries": healthcheck.get("retries", 3),
                    "StartPeriod": parse_duration(healthcheck.get("start_period", "0s"))
                }
            except Exception as e:
                last_deploy_logs.append(f"Healthcheck parse error for service {name}: {e}")

        # Networks config
        network_names = []
        if "networks" in svc:
            if isinstance(svc["networks"], dict):
                network_names = list(svc["networks"].keys())
            elif isinstance(svc["networks"], list):
                network_names = svc["networks"]

        endpoint_config = {net: {} for net in network_names}

        container_config = {
            "Image": image,
            "ExposedPorts": exposed_ports,
            "Healthcheck": hc_config,
            "HostConfig": {
                "PortBindings": port_bindings,
            },
            "NetworkingConfig": {
                "EndpointsConfig": endpoint_config
            }
        }

        container_name = svc.get("container_name", name)
        r = requests.post(f"{DOCKER_HOST}/containers/create", params={"name": container_name}, json=container_config)
        if r.ok:
            container_id = r.json().get("Id", "")[:12]
            last_deploy_logs.append(f"Created container '{container_name}': ID {container_id}")
            r_start = requests.post(f"{DOCKER_HOST}/containers/{container_id}/start")
            last_deploy_logs.append(f"Started container '{container_name}': {r_start.status_code}")
        else:
            last_deploy_logs.append(f"Failed to create container '{container_name}': {r.status_code} {r.text}")

    return True, "Deployment finished"

@app.route("/", methods=["GET"])
def index():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Docker Compose Remote Deploy</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        textarea { width: 100%; height: 250px; font-family: monospace; }
        pre { background: #f0f0f0; padding: 10px; overflow-x: auto; max-height: 200px; }
        .section { margin-bottom: 30px; }
        button { padding: 10px 20px; font-size: 16px; }
        .status-ok { color: green; }
        .status-error { color: red; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ccc; padding: 6px; text-align: left; }
        th { background: #eee; }
    </style>
</head>
<body>
    <h1>Docker Compose Remote Deployment</h1>

    <div class="section">
        <h2>Upload / Paste Docker Compose YAML</h2>
        <textarea id="composeText" placeholder="Paste your docker-compose YAML here..."></textarea><br>
        <button onclick="deployCompose()">Deploy Compose</button>
        <div id="deployResult"></div>
    </div>

    <div class="section">
        <h2>Last Deployment Logs</h2>
        <pre id="logs"></pre>
    </div>

    <div class="section">
        <h2>Docker Status (auto refresh every 10s)</h2>
        <button onclick="refreshStatus()">Refresh Now</button>

        <h3>Containers</h3>
        <div id="containers"></div>

        <h3>Images</h3>
        <div id="images"></div>

        <h3>Networks</h3>
        <div id="networks"></div>

        <h3>Volumes</h3>
        <div id="volumes"></div>
    </div>

<script>
async function deployCompose() {
    const text = document.getElementById('composeText').value;
    if (!text.trim()) {
        alert("Please paste docker-compose YAML");
        return;
    }
    document.getElementById('deployResult').innerText = "Deploying...";
    const res = await fetch('/deploy', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({compose: text})
    });
    const data = await res.json();
    document.getElementById('deployResult').innerText = data.message;
    fetchLogs();
    refreshStatus();
}

async function fetchLogs() {
    const res = await fetch('/logs');
    const data = await res.json();
    document.getElementById('logs').innerText = data.logs.join("\\n");
}

async function refreshStatus() {
    // Containers
    let res = await fetch('/api/containers');
    let data = await res.json();
    document.getElementById('containers').innerHTML = renderTable(data, ["Id", "Names", "Image", "State", "Status"]);

    // Images
    res = await fetch('/api/images');
    data = await res.json();
    document.getElementById('images').innerHTML = renderTable(data, ["Id", "RepoTags", "Size"]);

    // Networks
    res = await fetch('/api/networks');
    data = await res.json();
    document.getElementById('networks').innerHTML = renderTable(data, ["Id", "Name", "Driver"]);

    // Volumes
    res = await fetch('/api/volumes');
    data = await res.json();
    if (data.volumes) {
        document.getElementById('volumes').innerHTML = renderTable(data.volumes, ["Name", "Driver", "Mountpoint"]);
    } else {
        document.getElementById('volumes').innerHTML = "No volumes";
    }
}

function renderTable(data, columns) {
    if (!data || data.length === 0) return "No data";
    let html = "<table><tr>";
    for (const col of columns) {
        html += `<th>${col}</th>`;
    }
    html += "</tr>";

    for (const row of data) {
        html += "<tr>";
        for (const col of columns) {
            let val = row[col];
            if (Array.isArray(val)) val = val.join(", ");
            html += `<td>${val !== undefined ? val : ""}</td>`;
        }
        html += "</tr>";
    }
    html += "</table>";
    return html;
}

// Auto refresh every 10 seconds
setInterval(() => {
    refreshStatus();
    fetchLogs();
}, 10000);

window.onload = () => {
    refreshStatus();
    fetchLogs();
};
</script>

</body>
</html>
""")

@app.route("/deploy", methods=["POST"])
def deploy():
    data = request.json
    compose_text = data.get("compose")
    if not compose_text:
        return jsonify({"success": False, "message": "No compose file provided"}), 400

    success, msg = deploy_compose(compose_text)
    return jsonify({"success": success, "message": msg})

@app.route("/logs")
def logs():
    global last_deploy_logs
    return jsonify({"logs": last_deploy_logs})

@app.route("/api/containers")
def api_containers():
    r = requests.get(f"{DOCKER_HOST}/containers/json?all=1")
    if r.ok:
        return jsonify(r.json())
    return jsonify([])

@app.route("/api/images")
def api_images():
    r = requests.get(f"{DOCKER_HOST}/images/json")
    if r.ok:
        return jsonify(r.json())
    return jsonify([])

@app.route("/api/networks")
def api_networks():
    r = requests.get(f"{DOCKER_HOST}/networks")
    if r.ok:
        return jsonify(r.json())
    return jsonify([])

@app.route("/api/volumes")
def api_volumes():
    r = requests.get(f"{DOCKER_HOST}/volumes")
    if r.ok:
        return jsonify(r.json())
    return jsonify({"volumes": []})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
