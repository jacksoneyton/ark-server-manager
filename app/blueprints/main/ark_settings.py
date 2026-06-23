"""
ark_settings.py
---------------
Flask route for the ARK: Survival Evolved server settings dashboard.

URL: /ark-settings?server_id=<id>

The page is scoped to a specific GameServer record so the same web UI
works correctly when multiple ARK instances are installed on the same host.
All field definitions live in SETTINGS_SCHEMA (ark_ini_manager.py); the
route only handles HTTP concerns and delegates read/write/validation to
ArkIniManager.
"""

import os

from flask_login import login_required, current_user
from flask import (
    render_template,
    request,
    flash,
    url_for,
    redirect,
    current_app,
)

from app.utils import audit_log_event, log_wrap, validation_errors
from app.models import GameServer
from app.forms.views import ArkSettingsForm, ValidateID
from app.config.config_manager import ConfigManager
from app.services.ark_ini_manager import ArkIniManager, ark_cfg_dir, SETTINGS_SCHEMA

from . import main_bp


@main_bp.route("/ark-settings", methods=["GET", "POST"])
@login_required
def ark_settings():
    config = ConfigManager()
    form   = ArkSettingsForm()

    # ------------------------------------------------------------------ GET
    if request.method == "GET":
        id_form = ValidateID(request.args)
        if not id_form.validate():
            validation_errors(id_form)
            return redirect(url_for("main.home"))

        server_id = request.args.get("server_id")
        server    = GameServer.query.filter_by(id=server_id).first()

        if not current_user.has_access("controls", server_id):
            flash("Your user does not have access to this server.", category="error")
            return redirect(url_for("main.home"))

        if not _is_ark_server(server):
            flash(
                f"'{server.install_name}' does not appear to be an ARK server "
                "(LinuxServer config directory not found).",
                category="error",
            )
            return redirect(url_for("main.controls", server_id=server_id))

        ark_ini        = ArkIniManager(server.install_path)
        settings       = ark_ini.read_all_settings()
        server_running = ark_ini.is_server_running()
        current_app.logger.info(log_wrap("ark_settings GET", settings))

        return render_template(
            "ark_settings.html",
            user=current_user,
            _config=config,
            form=form,
            settings=settings,
            schema=SETTINGS_SCHEMA,
            server_id=server_id,
            server_name=server.install_name,
            server_running=server_running,
        )

    # ----------------------------------------------------------------- POST
    if not form.validate_on_submit():
        flash("Invalid form submission (CSRF check failed).", category="error")
        return redirect(url_for("main.home"))

    server_id = request.form.get("server_id", "").strip()
    server    = GameServer.query.filter_by(id=server_id).first()

    if not server:
        flash("Invalid server ID.", category="error")
        return redirect(url_for("main.home"))

    if not current_user.has_access("controls", server_id):
        flash("Your user does not have access to this server.", category="error")
        return redirect(url_for("main.home"))

    if not _is_ark_server(server):
        flash("Target server is not a recognised ARK instance.", category="error")
        return redirect(url_for("main.controls", server_id=server_id))

    ark_ini        = ArkIniManager(server.install_path)
    server_running = ark_ini.is_server_running()

    if server_running:
        current_app.logger.warning(
            "ARK settings saved while ShooterGameServer is running for '%s'.",
            server.install_name,
        )

    # ---- Schema-driven validation ----
    errors = ark_ini.validate_form(request.form)
    if errors:
        for err in errors:
            flash(err, category="error")
        return redirect(url_for("main.ark_settings", server_id=server_id))

    # ---- Backup before any write ----
    try:
        gus_bak = ark_ini.backup(ark_ini.GAME_USER_SETTINGS)
        gi_bak  = ark_ini.backup(ark_ini.GAME_INI)
    except OSError as exc:
        flash(f"Backup failed — aborting save: {exc}", category="error")
        return redirect(url_for("main.ark_settings", server_id=server_id))

    # ---- Build and write updates ----
    gus_updates, gi_updates = ark_ini.build_updates(request.form)

    try:
        ark_ini.set_values(ark_ini.GAME_USER_SETTINGS, gus_updates)
        ark_ini.set_values(ark_ini.GAME_INI, gi_updates)
    except OSError as exc:
        flash(f"Error writing settings: {exc}", category="error")
        return redirect(url_for("main.ark_settings", server_id=server_id))

    bak_names = ", ".join(filter(None, [
        os.path.basename(gus_bak) if gus_bak else None,
        os.path.basename(gi_bak)  if gi_bak  else None,
    ]))
    audit_log_event(
        current_user.id,
        f"User '{current_user.username}' updated ARK settings for "
        f"'{server.install_name}'. Backups: {bak_names or 'none'}",
    )

    flash(
        "ARK settings saved!"
        + (f" Backups: {bak_names}" if bak_names else "")
    )
    return redirect(url_for("main.ark_settings", server_id=server_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_ark_server(server: GameServer) -> bool:
    """True if this GameServer has an ARK LinuxServer config directory."""
    return os.path.isdir(ark_cfg_dir(server.install_path))
