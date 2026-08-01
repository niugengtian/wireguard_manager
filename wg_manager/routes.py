from __future__ import annotations

import base64
import hmac
import io
import secrets
from functools import wraps

import qrcode
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException

from .db import get_db, transaction
from .security import (
    DUMMY_PASSWORD_HASH,
    clear_login_failures,
    opaque_source_hash,
    record_login_failure,
    throttle_status,
    verify_password,
)
from .services import (
    ARCHITECTURES,
    CLIENT_TYPES,
    DomainError,
    audit,
    bi,
    create_device,
    create_user,
    delete_device,
    reset_device,
    set_user_password,
    store_installer,
    update_user,
    verified_installer_path,
)


web = Blueprint("web", __name__)


@web.app_errorhandler(HTTPException)
def bilingual_http_error(error: HTTPException):
    defaults = {
        400: bi("请求无效", "Bad request"),
        403: bi("权限不足", "Permission denied"),
        404: bi("页面或对象不存在", "Page or object not found"),
        413: bi("上传内容过大", "Upload too large"),
        429: bi("请求过多，请稍后再试", "Too many requests; try later"),
    }
    description = str(error.description)
    if " / " not in description:
        description = defaults.get(error.code, bi("请求处理失败", "Request failed"))
    return render_template("error.html", status=error.code, description=description), error.code


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("web.login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "admin":
            audit(
                get_db(),
                action="rbac.denied",
                object_type=request.endpoint or "unknown",
                object_id=None,
                actor_user_id=g.user["id"],
                actor_kind="web",
                outcome="denied",
            )
            abort(403, bi("权限不足", "permission denied"))
        return view(*args, **kwargs)

    return wrapped


def csrf_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        supplied = request.form.get("_csrf", "")
        expected = session.get("csrf", "")
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            abort(400, bi("CSRF 令牌无效", "invalid CSRF token"))
        return view(*args, **kwargs)

    return wrapped


@web.app_context_processor
def template_context():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf"], "client_types": CLIENT_TYPES, "architectures": ARCHITECTURES}


@web.get("/")
def index():
    if g.user is None:
        return redirect(url_for("web.login"))
    if g.user["role"] == "admin":
        return redirect(url_for("web.admin_dashboard"))
    return redirect(url_for("web.devices"))


@web.get("/help")
def help_page():
    return render_template("help.html")


@web.route("/login", methods=("GET", "POST"))
def login():
    if g.user is not None:
        return redirect(url_for("web.index"))
    if request.method == "GET":
        return render_template("login.html")
    supplied = request.form.get("_csrf", "")
    expected = session.get("csrf", "")
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        abort(400, bi("CSRF 令牌无效", "invalid CSRF token"))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    source_hash = opaque_source_hash(
        current_app.config["SECRET_KEY"], username, request.remote_addr or "unknown"
    )
    connection = get_db()
    retry_after = throttle_status(
        connection,
        source_hash,
        limit=current_app.config["LOGIN_ATTEMPT_LIMIT"],
        window_seconds=current_app.config["LOGIN_WINDOW_SECONDS"],
    )
    if retry_after:
        audit(
            connection,
            action="auth.login",
            object_type="session",
            object_id=None,
            actor_user_id=None,
            actor_kind="web",
            outcome="denied",
            source_hash=source_hash,
            details={"reason": "rate_limited"},
        )
        response = make_response(
            render_template("login.html", error=bi("登录尝试过多，请稍后再试", "Too many attempts. Try later.")),
            429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    user = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    password_valid = verify_password(
        user["password_hash"] if user is not None else DUMMY_PASSWORD_HASH, password
    )
    valid = user is not None and bool(user["enabled"]) and password_valid
    if not valid:
        record_login_failure(
            connection,
            source_hash,
            limit=current_app.config["LOGIN_ATTEMPT_LIMIT"],
            window_seconds=current_app.config["LOGIN_WINDOW_SECONDS"],
        )
        audit(
            connection,
            action="auth.login",
            object_type="session",
            object_id=None,
            actor_user_id=None,
            actor_kind="web",
            outcome="denied",
            source_hash=source_hash,
            details={"reason": "invalid_credentials"},
        )
        return render_template("login.html", error=bi("用户名或密码错误", "Invalid credentials.")), 401
    clear_login_failures(connection, source_hash)
    session.clear()
    session["user_id"] = user["id"]
    session["session_version"] = user["session_version"]
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    audit(
        connection,
        action="auth.login",
        object_type="session",
        object_id=None,
        actor_user_id=user["id"],
        actor_kind="web",
        source_hash=source_hash,
    )
    return redirect(url_for("web.index"))


@web.post("/logout")
@login_required
@csrf_required
def logout():
    user_id = g.user["id"]
    audit(
        get_db(),
        action="auth.logout",
        object_type="session",
        object_id=None,
        actor_user_id=user_id,
        actor_kind="web",
    )
    session.clear()
    return redirect(url_for("web.login"))


@web.get("/devices")
@login_required
def devices():
    connection = get_db()
    rows = connection.execute(
        "SELECT * FROM devices WHERE user_id = ? ORDER BY created_at, id", (g.user["id"],)
    ).fetchall()
    installers = connection.execute(
        "SELECT * FROM installers ORDER BY platform, architecture, version"
    ).fetchall()
    return render_template("devices.html", devices=rows, installers=installers)


@web.post("/devices")
@login_required
@csrf_required
def create_device_route():
    try:
        device, configuration = create_device(
            get_db(),
            current_app.config,
            user_id=g.user["id"],
            name=request.form.get("name", ""),
            client_type=request.form.get("client_type", ""),
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
    except DomainError as error:
        flash(error.message, "error")
        rows = get_db().execute(
            "SELECT * FROM devices WHERE user_id = ? ORDER BY created_at, id", (g.user["id"],)
        ).fetchall()
        installers = get_db().execute(
            "SELECT * FROM installers ORDER BY platform, architecture, version"
        ).fetchall()
        return render_template("devices.html", devices=rows, installers=installers), error.status
    return _deliver_configuration(device, configuration, request.form.get("delivery", "download"))


@web.post("/devices/<device_id>/reset")
@login_required
@csrf_required
def reset_device_route(device_id: str):
    try:
        device, configuration = reset_device(
            get_db(),
            current_app.config,
            device_id=device_id,
            owner_user_id=g.user["id"],
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
    except DomainError as error:
        audit(
            get_db(),
            action="device.reset",
            object_type="device",
            object_id=None,
            actor_user_id=g.user["id"],
            actor_kind="web",
            outcome="denied",
            details={"reason": "not_owned_or_missing"},
        )
        abort(error.status, error.message)
    return _deliver_configuration(device, configuration, request.form.get("delivery", "download"))


@web.post("/devices/<device_id>/delete")
@login_required
@csrf_required
def delete_device_route(device_id: str):
    try:
        delete_device(
            get_db(),
            current_app.config,
            device_id=device_id,
            owner_user_id=g.user["id"],
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
    except DomainError as error:
        audit(
            get_db(),
            action="device.delete",
            object_type="device",
            object_id=None,
            actor_user_id=g.user["id"],
            actor_kind="web",
            outcome="denied",
            details={"reason": "not_owned_or_missing"},
        )
        abort(error.status, error.message)
    flash(bi("设备已删除，隧道 IP 可重新分配", "Device deleted; its IP is now available for reuse."), "success")
    return redirect(url_for("web.devices"))


@web.post("/account/password")
@login_required
@csrf_required
def change_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirmation = request.form.get("new_password_confirm", "")
    row = get_db().execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if not verify_password(row["password_hash"], current_password):
        flash(bi("当前密码不正确", "Current password is incorrect."), "error")
        return redirect(url_for("web.devices"))
    if new_password != confirmation:
        flash(bi("两次输入的新密码不一致", "New passwords do not match."), "error")
        return redirect(url_for("web.devices"))
    try:
        set_user_password(
            get_db(),
            user_id=g.user["id"],
            password=new_password,
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
    except DomainError as error:
        flash(error.message, "error")
        return redirect(url_for("web.devices"))
    session.clear()
    flash(bi("密码已修改，请重新登录", "Password changed. Sign in again."), "success")
    return redirect(url_for("web.login"))


@web.get("/installers/<installer_id>/download")
@login_required
def download_installer(installer_id: str):
    try:
        installer, path = verified_installer_path(get_db(), current_app.config, installer_id)
    except DomainError as error:
        abort(error.status, error.message)
    response = send_file(
        path,
        mimetype=installer["media_type"],
        as_attachment=True,
        download_name=installer["original_filename"],
        conditional=False,
        etag=installer["sha256"],
    )
    return _no_store(response)


@web.get("/admin")
@admin_required
def admin_dashboard():
    connection = get_db()
    users = connection.execute(
        """SELECT users.id, users.username, users.role, users.enabled, users.device_quota,
                  count(devices.id) AS device_count
           FROM users LEFT JOIN devices ON devices.user_id = users.id
           GROUP BY users.id ORDER BY users.username"""
    ).fetchall()
    devices = connection.execute(
        """SELECT devices.*, users.username FROM devices JOIN users ON users.id = devices.user_id
           ORDER BY users.username, devices.created_at"""
    ).fetchall()
    installers = connection.execute("SELECT * FROM installers ORDER BY created_at DESC").fetchall()
    events = connection.execute(
        "SELECT * FROM audit_events ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return render_template(
        "admin.html", users=users, devices=devices, installers=installers, events=events
    )


@web.post("/admin/users")
@admin_required
@csrf_required
def admin_create_user():
    try:
        create_user(
            get_db(),
            username=request.form.get("username", ""),
            password=request.form.get("password", ""),
            quota=int(request.form.get("quota", "0")),
            role="user",
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
        flash(bi("用户已创建", "User created."), "success")
    except (DomainError, ValueError) as error:
        flash(error.message if isinstance(error, DomainError) else bi("配额无效", "Invalid quota."), "error")
    return redirect(url_for("web.admin_dashboard"))


@web.post("/admin/users/<int:user_id>")
@admin_required
@csrf_required
def admin_update_user(user_id: int):
    try:
        update_user(
            get_db(),
            user_id=user_id,
            enabled=request.form.get("enabled") == "1",
            quota=int(request.form.get("quota", "0")),
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
        flash(bi("用户已更新", "User updated."), "success")
    except (DomainError, ValueError) as error:
        flash(error.message if isinstance(error, DomainError) else bi("配额无效", "Invalid quota."), "error")
    return redirect(url_for("web.admin_dashboard"))


@web.post("/admin/users/<int:user_id>/password")
@admin_required
@csrf_required
def admin_reset_user_password(user_id: int):
    password = request.form.get("password", "")
    if password != request.form.get("password_confirm", ""):
        flash(bi("两次输入的密码不一致", "Passwords do not match."), "error")
        return redirect(url_for("web.admin_dashboard"))
    try:
        set_user_password(
            get_db(),
            user_id=user_id,
            password=password,
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
        flash(bi("密码已重置，现有会话已撤销", "Password reset; existing sessions were revoked."), "success")
    except DomainError as error:
        flash(error.message, "error")
    return redirect(url_for("web.admin_dashboard"))


@web.post("/admin/devices/<device_id>/delete")
@admin_required
@csrf_required
def admin_delete_device(device_id: str):
    try:
        delete_device(
            get_db(),
            current_app.config,
            device_id=device_id,
            owner_user_id=None,
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
        flash(bi("设备已删除", "Device deleted."), "success")
    except DomainError as error:
        flash(error.message, "error")
    return redirect(url_for("web.admin_dashboard"))


@web.post("/admin/installers")
@admin_required
@csrf_required
def admin_upload_installer():
    uploaded = request.files.get("installer")
    if uploaded is None:
        flash(bi("必须选择安装包文件", "Installer file is required."), "error")
        return redirect(url_for("web.admin_dashboard"))
    try:
        store_installer(
            get_db(),
            current_app.config,
            stream=uploaded.stream,
            filename=uploaded.filename or "",
            platform=request.form.get("platform", ""),
            architecture=request.form.get("architecture", ""),
            version=request.form.get("version", ""),
            media_type=uploaded.mimetype,
            license_name=request.form.get("license_name", ""),
            license_source_url=request.form.get("license_source_url", ""),
            redistribution_confirmed=request.form.get("redistribution_confirmed") == "1",
            actor_user_id=g.user["id"],
            actor_kind="web",
        )
        flash(bi("安装包已保存并记录 SHA-256 校验信息", "Installer stored with verified SHA-256 metadata."), "success")
    except DomainError as error:
        flash(error.message, "error")
    return redirect(url_for("web.admin_dashboard"))


def _deliver_configuration(device: dict, configuration: str, delivery: str):
    if delivery == "qr":
        image = qrcode.make(configuration)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        response = make_response(
            render_template("qr.html", device=device, qr_data=f"data:image/png;base64,{encoded}")
        )
        return _no_store(response)
    if delivery != "download":
        abort(400, bi("交付方式无效", "invalid delivery type"))
    safe_name = "".join(character if character.isalnum() or character in "-_" else "-" for character in device["name"])
    response = make_response(configuration)
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{safe_name or "wireguard"}.conf"'
    return _no_store(response)


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
