import time
import json
import datetime

from flask import Blueprint, request, Flask, render_template, url_for, redirect, flash

from CTFd.models import db, Solves
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.utils.decorators import authed_only, admins_only, during_ctf_time_only, ratelimit, require_verified_emails
from CTFd.utils.user import get_current_user
from CTFd.utils.modes import get_model

from .models import ContainerInfoModel, ContainerSettingsModel
from .container_manager import ContainerManager, ContainerException
from .container_challenge import ContainerChallenge

def settings_to_dict(settings):
    return {
        setting.key: setting.value for setting in settings
    }

containers_bp = Blueprint(
    'containers', __name__, template_folder='templates', static_folder='assets', url_prefix='/containers')

def register_app(app: Flask):
    container_settings = settings_to_dict(ContainerSettingsModel.query.all())
    global container_manager
    container_manager = ContainerManager(container_settings, app)
    app.register_blueprint(containers_bp)

@containers_bp.app_template_filter("format_time")
def format_time_filter(unix_seconds):
    # return time.ctime(unix_seconds)
    return datetime.datetime.fromtimestamp(unix_seconds, tz=datetime.datetime.now(
        datetime.timezone.utc).astimezone().tzinfo).isoformat()

def kill_container(container_id):
    container: ContainerInfoModel = ContainerInfoModel.query.filter_by(
        container_id=container_id).first()

    try:
        container_manager.kill_container(container_id)
    except ContainerException:
        return {"error": "Docker is not initialized. Please check your settings."}

    db.session.delete(container)

    db.session.commit()
    return {"success": "Container killed"}

def renew_container(chal_id, user_id):
    # Get the requested challenge
    challenge = ContainerChallenge.challenge_model.query.filter_by(
        id=chal_id).first()

    # Make sure the challenge exists and is a container challenge
    if challenge is None:
        return {"error": "Challenge not found"}, 400

    running_containers = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id, user_id=user_id)
    running_container = running_containers.first()

    if running_container is None:
        return {"error": "Container not found, try resetting the container."}

    try:
        running_container.expires = int(
            time.time() + container_manager.expiration_seconds)
        db.session.commit()
    except ContainerException:
        return {"error": "Database error occurred, please try again."}

    return {"success": "Container renewed", "expires": running_container.expires}

def create_container(chal_id, user_id):
    # Get the requested challenge
    challenge = ContainerChallenge.challenge_model.query.filter_by(
        id=chal_id).first()

    # Make sure the challenge exists and is a container challenge
    if challenge is None:
        return {"error": "Challenge not found"}, 400

    # Check for any existing containers for the user
    running_containers = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id, user_id=user_id)
    running_container = running_containers.first()

    # If a container is already running for the user, return it
    if running_container:
        # Check if Docker says the container is still running before returning it
        try:
            if container_manager.is_container_running(
                    running_container.container_id):
                return json.dumps({
                    "status": "already_running",
                    "hostname": challenge.connection_info,
                    "port": running_container.port,
                    "expires": running_container.expires
                })
            else:
                # Container is not running, it must have died or been killed,
                # remove it from the database and create a new one
                running_containers.delete()
                db.session.commit()
        except ContainerException as err:
            return {"error": str(err)}, 500

    running_containers_for_user = ContainerInfoModel.query.filter_by(user_id=user_id)
    running_container_for_user = running_containers_for_user.first()

    if running_container_for_user:
        challenge_of_running_container = ContainerChallenge.challenge_model.query.filter_by(id=running_container_for_user.challenge_id).first()
        return {"error": f"Stop other instance running ({challenge_of_running_container.name})"}, 400

    # TODO: Should insert before creating container, then update. That would avoid a TOCTOU issue

    # Run a new Docker container
    try:
        created_container = container_manager.create_container(
            challenge.image, challenge.port, challenge.command, challenge.volumes)
    except ContainerException as err:
        return {"error": str(err)}

    # Fetch the random port Docker assigned
    port = container_manager.get_container_port(created_container.id)

    # Port may be blank if the container failed to start
    if port is None:
        return json.dumps({
            "status": "error",
            "error": "Could not get port"
        })

    expires = int(time.time() + container_manager.expiration_seconds)

    # Insert the new container into the database
    new_container = ContainerInfoModel(
        container_id=created_container.id,
        challenge_id=challenge.id,
        user_id=user_id,
        port=port,
        timestamp=int(time.time()),
        expires=expires
    )
    db.session.add(new_container)
    db.session.commit()

    return json.dumps({
        "status": "created",
        "hostname": challenge.connection_info,
        "port": port,
        "expires": expires
    })

@containers_bp.route('/api/running', methods=['POST'])
@authed_only
@during_ctf_time_only
@require_verified_emails
# Rate limit to 100 requests per minute (be careful with this feature when event is on same network. Based on IP address, it will block all users on the same network.)
# @ratelimit(method="POST", limit=100, interval=60, key_prefix='rl_running_container_post')
def route_running_container():
    user = get_current_user()

    # Validate the request
    if request.json is None:
        return {"error": "Invalid request"}, 400

    if request.json.get("chal_id", None) is None:
        return {"error": "No chal_id specified"}, 400

    if user is None:
        return {"error": "User not found"}, 400

    try:
        challenge = ContainerChallenge.challenge_model.query.filter_by(
        id=request.json.get("chal_id")).first()

        # Make sure the challenge exists and is a container challenge
        if challenge is None:
            return {"error": "Challenge not found"}, 400

        # Check for any existing containers for the user
        running_containers = ContainerInfoModel.query.filter_by(
            challenge_id=challenge.id, user_id=user.team_id if user.team_id != None else user.id)
        running_container = running_containers.first()

        if running_container:
            return {"status": "already_running", "container_id": request.json.get("chal_id")}, 200

        else:
            return {"status": "stopped", "container_id": request.json.get("chal_id")}, 200

    except Exception as err:
        return {"error": str(err)}, 500

@containers_bp.route('/api/request', methods=['POST'])
@authed_only
@during_ctf_time_only
@require_verified_emails
# Rate limit to 100 requests per minute (be careful with this feature when event is on same network. Based on IP address, it will block all users on the same network.)
# @ratelimit(method="POST", limit=100, interval=60, key_prefix='rl_request_container_post')
def route_request_container():
    user = get_current_user()

    # Validate the request
    if request.json is None:
        return {"error": "Invalid request"}, 400

    if request.json.get("chal_id", None) is None:
        return {"error": "No chal_id specified"}, 400

    if user is None:
        return {"error": "User not found"}, 400

    try:
        return create_container(request.json.get("chal_id"), user.team_id if user.team_id != None else user.id)
    except ContainerException as err:
        return {"error": str(err)}, 500

@containers_bp.route('/api/renew', methods=['POST'])
@authed_only
@during_ctf_time_only
@require_verified_emails
# Rate limit to 100 requests per minute (be careful with this feature when event is on same network. Based on IP address, it will block all users on the same network.)
# @ratelimit(method="POST", limit=100, interval=60, key_prefix='rl_renew_container_post')
def route_renew_container():
    user = get_current_user()

    # Validate the request
    if request.json is None:
        return {"error": "Invalid request"}, 400

    if request.json.get("chal_id", None) is None:
        return {"error": "No chal_id specified"}, 400

    if user is None:
        return {"error": "User not found"}, 400

    try:
        return renew_container(request.json.get("chal_id"), user.team_id if user.team_id != None else user.id)
    except ContainerException as err:
        return {"error": str(err)}, 500

@containers_bp.route('/api/reset', methods=['POST'])
@authed_only
@during_ctf_time_only
@require_verified_emails
# Rate limit to 100 requests per minute (be careful with this feature when event is on same network. Based on IP address, it will block all users on the same network.)
# @ratelimit(method="POST", limit=100, interval=60, key_prefix='rl_restart_container_post')
def route_restart_container():
    user = get_current_user()

    # Validate the request
    if request.json is None:
        return {"error": "Invalid request"}, 400

    if request.json.get("chal_id", None) is None:
        return {"error": "No chal_id specified"}, 400

    if user is None:
        return {"error": "User not found"}, 400

    running_container: ContainerInfoModel = ContainerInfoModel.query.filter_by(
        challenge_id=request.json.get("chal_id"), user_id=user.team_id if user.team_id != None else user.id).first()

    if running_container:
        kill_container(running_container.container_id)

    return create_container(request.json.get("chal_id"), user.team_id if user.team_id != None else user.id)

@containers_bp.route('/api/stop', methods=['POST'])
@authed_only
@during_ctf_time_only
@require_verified_emails
# Rate limit to 100 requests per minute (be careful with this feature when event is on same network. Based on IP address, it will block all users on the same network.)
# @ratelimit(method="POST", limit=100, interval=60, key_prefix='rl_stop_container_post')
def route_stop_container():
    user = get_current_user()
    # Validate the request
    if request.json is None:
        return {"error": "Invalid request"}, 400

    if request.json.get("chal_id", None) is None:
        return {"error": "No chal_id specified"}, 400

    if user is None:
        return {"error": "User not found"}, 400

    running_container: ContainerInfoModel = ContainerInfoModel.query.filter_by(
        challenge_id=request.json.get("chal_id"), user_id=user.team_id if user.team_id != None else user.id).first()

    if running_container:
        return kill_container(running_container.container_id)

    return {"error": "No container found"}, 400

@containers_bp.route('/api/kill', methods=['POST'])
@admins_only
def route_kill_container():
    if request.json is None:
        return {"error": "Invalid request"}, 400

    if request.json.get("container_id", None) is None:
        return {"error": "No container_id specified"}, 400

    return kill_container(request.json.get("container_id"))

@containers_bp.route('/api/purge', methods=['POST'])
@admins_only
def route_purge_containers():
    containers: "list[ContainerInfoModel]" = ContainerInfoModel.query.all()
    for container in containers:
        try:
            kill_container(container.container_id)
        except ContainerException:
            pass
    return {"success": "Purged all containers"}, 200

@containers_bp.route('/api/images', methods=['GET'])
@admins_only
def route_get_images():
    try:
        images = container_manager.get_images()
    except ContainerException as err:
        return {"error": str(err)}

    return {"images": images}

@containers_bp.route('/api/settings/update', methods=['POST'])
@admins_only
def route_update_settings():
    if request.form.get("docker_base_url") is None:
        return {"error": "Invalid request"}, 400

    if request.form.get("docker_hostname") is None:
        return {"error": "Invalid request"}, 400

    if request.form.get("container_expiration") is None:
        return {"error": "Invalid request"}, 400

    if request.form.get("container_maxmemory") is None:
        return {"error": "Invalid request"}, 400

    if request.form.get("container_maxcpu") is None:
        return {"error": "Invalid request"}, 400

    docker_base_url = ContainerSettingsModel.query.filter_by(
        key="docker_base_url").first()

    docker_hostname = ContainerSettingsModel.query.filter_by(
        key="docker_hostname").first()

    container_expiration = ContainerSettingsModel.query.filter_by(
        key="container_expiration").first()

    container_maxmemory = ContainerSettingsModel.query.filter_by(
        key="container_maxmemory").first()

    container_maxcpu = ContainerSettingsModel.query.filter_by(
        key="container_maxcpu").first()

    # Create or update
    if docker_base_url is None:
        # Create
        docker_base_url = ContainerSettingsModel(
            key="docker_base_url", value=request.form.get("docker_base_url"))
        db.session.add(docker_base_url)
    else:
        # Update
        docker_base_url.value = request.form.get("docker_base_url")

    # Create or update
    if docker_hostname is None:
        # Create
        docker_hostname = ContainerSettingsModel(
            key="docker_hostname", value=request.form.get("docker_hostname"))
        db.session.add(docker_hostname)
    else:
        # Update
        docker_hostname.value = request.form.get("docker_hostname")

    # Create or update
    if container_expiration is None:
        # Create
        container_expiration = ContainerSettingsModel(
            key="container_expiration", value=request.form.get("container_expiration"))
        db.session.add(container_expiration)
    else:
        # Update
        container_expiration.value = request.form.get(
            "container_expiration")

    # Create or update
    if container_maxmemory is None:
        # Create
        container_maxmemory = ContainerSettingsModel(
            key="container_maxmemory", value=request.form.get("container_maxmemory"))
        db.session.add(container_maxmemory)
    else:
        # Update
        container_maxmemory.value = request.form.get("container_maxmemory")

    # Create or update
    if container_maxcpu is None:
        # Create
        container_maxcpu = ContainerSettingsModel(
            key="container_maxcpu", value=request.form.get("container_maxcpu"))
        db.session.add(container_maxcpu)
    else:
        # Update
        container_maxcpu.value = request.form.get("container_maxcpu")

    db.session.commit()

    container_manager.settings = settings_to_dict(
        ContainerSettingsModel.query.all())

    if container_manager.settings.get("docker_base_url") is not None:
        try:
            container_manager.initialize_connection(
                container_manager.settings, app)
        except ContainerException as err:
            flash(str(err), "error")
            return redirect(url_for(".route_containers_settings"))

    return redirect(url_for(".route_containers_dashboard"))

@containers_bp.route('/dashboard', methods=['GET'])
@admins_only
def route_containers_dashboard():
    running_containers = ContainerInfoModel.query.order_by(
        ContainerInfoModel.timestamp.desc()).all()

    connected = False
    try:
        connected = container_manager.is_connected()
    except ContainerException:
        pass

    for i, container in enumerate(running_containers):
        try:
            running_containers[i].is_running = container_manager.is_container_running(
                container.container_id)
        except ContainerException:
            running_containers[i].is_running = False

    return render_template('container_dashboard.html', containers=running_containers, connected=connected)

@containers_bp.route('/settings', methods=['GET'])
@admins_only
def route_containers_settings():
    running_containers = ContainerInfoModel.query.order_by(
        ContainerInfoModel.timestamp.desc()).all()
    return render_template('container_settings.html', settings=container_manager.settings)