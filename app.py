from flask import Flask, request, jsonify, render_template
import requests
import yaml
import re

app = Flask(__name__, template_folder='templates')

DOCKER_HOST = "http://192.168.192.136:2375"

DOCKER_HUB_API = "https://hub.docker.com/v2 "
last_deploy_logs = [] 

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
    return render_template("index.html")

@app.route("/api/registry/image/<image>/tags")
def registry_tags(image):
    """Get tags for official Docker images like 'nginx', 'redis', etc."""
    url = f"https://hub.docker.com/v2/repositories/library/{image}/tags/"  # Fixed URL
    res = requests.get(url)

    if res.status_code != 200:
        return jsonify({"error": "Failed to fetch tags", "status": res.status_code, "body": res.text})

    try:
        return jsonify(res.json())
    except requests.exceptions.JSONDecodeError:
        return jsonify({"error": "Invalid JSON response from Docker Hub", "body": res.text})


@app.route("/api/registry/image/<namespace>/<image>/tags")
def registry_user_image_tags(namespace, image):
    """Get tags for user/public Docker images like 'bitnami/nginx'"""
    page = request.args.get("page", 1)
    url = f"https://hub.docker.com/v2/repositories/{namespace}/{image}/tags/?page={page}"

    res = requests.get(url)
    if res.status_code != 200:
        return jsonify({"error": "Failed to fetch tags", "status": res.status_code, "body": res.text})

    try:
        return jsonify(res.json())
    except requests.exceptions.JSONDecodeError:
        return jsonify({"error": "Invalid JSON response from Docker Hub", "body": res.text})


@app.route("/api/registry/image/<image>")
def registry_image_info(image):
    url = f"https://hub.docker.com/v2/repositories/library/{image}"
    res = requests.get(url)

    if res.status_code != 200:
        return jsonify({"error": "Failed to fetch image info", "status": res.status_code, "body": res.text})

    try:
        return jsonify(res.json())
    except requests.exceptions.JSONDecodeError:
        return jsonify({"error": "Invalid JSON response from Docker Hub", "body": res.text})



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

# === Container Management Routes ===

@app.route("/api/containers/<container_id>/start", methods=["POST"])
def start_container(container_id):
    r = requests.post(f"{DOCKER_HOST}/containers/{container_id}/start")
    return jsonify({"status": "ok" if r.ok else "error", "response": r.text})


@app.route("/api/containers/<container_id>/stop", methods=["POST"])
def stop_container(container_id):
    r = requests.post(f"{DOCKER_HOST}/containers/{container_id}/stop", timeout=10)
    return jsonify({"status": "ok" if r.ok else "error", "response": r.text})


@app.route("/api/containers/<container_id>/restart", methods=["POST"])
def restart_container(container_id):
    r = requests.post(f"{DOCKER_HOST}/containers/{container_id}/restart", timeout=10)
    return jsonify({"status": "ok" if r.ok else "error", "response": r.text})


@app.route("/api/containers/<container_id>/delete", methods=["DELETE"])
def delete_container(container_id):
    r = requests.delete(f"{DOCKER_HOST}/containers/{container_id}?force=true")
    return jsonify({"status": "ok" if r.ok else "error", "response": r.text})


@app.route("/api/containers/<container_id>/logs", methods=["GET"])
def container_logs(container_id):
    params = {
        "stdout": 1,
        "stderr": 1,
        "tail": 100,
        "timestamps": False
    }
    r = requests.get(f"{DOCKER_HOST}/containers/{container_id}/logs", params=params)
    return jsonify({"logs": r.text.splitlines()})


@app.route("/api/containers/<container_id>/inspect", methods=["GET"])
def container_inspect(container_id):
    r = requests.get(f"{DOCKER_HOST}/containers/{container_id}/json")
    return jsonify(r.json() if r.ok else {"error": r.text})


# === Image Management Routes ===

@app.route("/api/images/<image_id>/remove", methods=["DELETE"])
def remove_image(image_id):
    r = requests.delete(f"{DOCKER_HOST}/images/{image_id}?force=true")
    return jsonify({"status": "ok" if r.ok else "error", "response": r.text})


@app.route("/api/images/<image_id>/inspect", methods=["GET"])
def inspect_image(image_id):
    r = requests.get(f"{DOCKER_HOST}/images/{image_id}/json")
    return jsonify(r.json() if r.ok else {"error": r.text})


@app.route("/api/images/pull", methods=["POST"])
def pull_image():
    data = request.json
    image = data.get("image")
    if not image:
        return jsonify({"success": False, "message": "No image name provided"}), 400

    r = requests.post(f"{DOCKER_HOST}/images/create", params={"fromImage": image})
    return jsonify({"success": r.ok, "message": f"Status {r.status_code}: {r.text}"})


@app.route("/api/images/prune", methods=["POST"])
def prune_images():
    r = requests.post(f"{DOCKER_HOST}/images/prune")
    return jsonify({"success": r.ok, "message": f"Pruned {r.json().get('SpaceReclaimed', 0)} bytes"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
