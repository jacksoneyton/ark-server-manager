import json
import os
import sys

from flask import Response, current_app
from flask_login import login_required, current_user
from flask_restful import Resource

from app.models import GameServer
from app.utils import audit_log_event, log_wrap
from app.services.ark_ini_manager import ark_cfg_dir

from . import api

# Import RCON client from bin/ alongside ark_autopause.
_BIN_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "bin")
sys.path.insert(0, os.path.abspath(_BIN_DIR))
from ark_rcon import ArkRcon, RconError  # noqa: E402

import configparser


def _rcon_credentials(install_path):
    """Read RCONEnabled/RCONPort/ServerAdminPassword from GameUserSettings.ini."""
    gus_path = os.path.join(ark_cfg_dir(install_path), "GameUserSettings.ini")
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(gus_path)
    except configparser.Error:
        return False, 27020, ""
    if not parser.has_section("ServerSettings"):
        return False, 27020, ""
    section = parser["ServerSettings"]
    enabled = section.get("RCONEnabled", "False").strip().lower() in ("true", "1", "yes")
    port = int(section.get("RCONPort", "27020"))
    password = section.get("ServerAdminPassword", "")
    return enabled, port, password


def _json_response(body, status):
    return Response(json.dumps(body, indent=4), status=status, mimetype="application/json")


class ArkWipeDinos(Resource):
    @login_required
    def post(self, server_id):
        server = GameServer.query.filter_by(id=server_id).first()
        if server is None:
            return _json_response({"error": "Server not found."}, 404)

        if not current_user.has_access("controls", server_id):
            return _json_response({"error": "Insufficient permissions."}, 403)

        enabled, port, password = _rcon_credentials(server.install_path)
        if not enabled or not password:
            return _json_response(
                {"error": "RCON is not enabled or has no admin password set. "
                          "Enable it on the ARK Settings page."}, 503
            )

        try:
            with ArkRcon("127.0.0.1", port, password) as rcon:
                rcon.command(
                    "ServerChat WARNING: Admin is wiping all wild dinos in 10 seconds..."
                )
                import time; time.sleep(10)
                rcon.command("DestroyWildDinos")
                rcon.command("ServerChat Wild dino wipe complete.")
        except RconError as exc:
            current_app.logger.warning("Wipe dinos RCON error: %s", exc)
            return _json_response({"error": f"RCON error: {exc}"}, 502)

        audit_log_event(
            current_user.id,
            f"User '{current_user.username}' wiped wild dinos on '{server.install_name}'"
        )
        current_app.logger.info(log_wrap("wipe_dinos", f"user={current_user.username} server={server.install_name}"))
        return _json_response({"message": "Wild dino wipe complete."}, 200)


api.add_resource(ArkWipeDinos, "/ark/wipe-dinos/<string:server_id>")
