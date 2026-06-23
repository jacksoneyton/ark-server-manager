"""
ark_ini_manager.py
------------------
Handles reading and writing ARK: Survival Evolved .ini configuration files
for GameUserSettings.ini and Game.ini without corrupting their structure.

Key design decisions:
  - Takes install_path from the GameServer DB record so it works for any
    number of ARK server instances on the same machine.
  - Regex-based line scanner instead of configparser: preserves duplicate keys
    (e.g. ConfigOverrideSupplyCrateItems), comments, and ARK's non-standard
    section headers like [/Script/ShooterGame.ShooterGameUserSettings].
  - SETTINGS_SCHEMA drives reading, writing, validation, and template rendering
    from a single source of truth.  Adding a new field = one dict entry.
  - set_values() creates missing sections/keys rather than failing silently.
  - backup() writes a timestamped .bak copy before every write.
  - is_server_running() uses psutil to detect the live ShooterGameServer process.
"""

import os
import re
import shutil
import psutil

from datetime import datetime


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

# LinuxGSM always places game files under serverfiles/ within install_path.
_CFG_RELATIVE = os.path.join(
    "serverfiles", "ShooterGame", "Saved", "Config", "LinuxServer"
)

# Executable name that appears in /proc when the server is live.
_ARK_PROC_NAME = "ShooterGameServer"


def ark_cfg_dir(install_path: str) -> str:
    """Return the LinuxServer config directory for a given install_path."""
    return os.path.join(install_path, _CFG_RELATIVE)


# ---------------------------------------------------------------------------
# Settings schema
# ---------------------------------------------------------------------------
# Each entry describes one INI key.  The template iterates this to render
# all fields; the manager iterates it to read and write values.
#
# Field keys:
#   key        (str)   INI key name; also the HTML form field name.
#   label      (str)   Human-readable label shown in the UI.
#   type       (str)   "float" | "int" | "bool" | "str"
#   default    (str)   Raw string default (ARK ini format).
#   section    (str)   INI section name, e.g. "ServerSettings".
#   file       (str)   "gus" = GameUserSettings.ini | "gi" = Game.ini
#   help       (str)   Short tooltip / description shown under the field.
#   min        (num)   Minimum valid value (float/int only).
#   max        (num)   Maximum valid value (float/int only).
#   step       (num)   Input step for number fields (float/int).
#   slider_max (num)   Upper end of the range slider (≤ max). Optional;
#                      defaults to max.  Useful when max is very large but
#                      90 % of users never go above 10.
#   input_type (str)   "password" overrides the HTML input type (str only).

SETTINGS_SCHEMA = [

    # ── Administration ───────────────────────────────────────────────────
    {
        "id": "administration",
        "label": "Administration",
        "icon": "bi-shield-lock",
        "fields": [
            {
                "key": "SessionName",
                "label": "Session Name",
                "type": "str",
                "default": "My ARK Server",
                "section": "SessionSettings",
                "file": "gus",
                "help": "Server name shown in the in-game browser.",
            },
            {
                "key": "ServerAdminPassword",
                "label": "Admin Password",
                "type": "str",
                "input_type": "password",
                "default": "",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Required for enablecheats / admincheat commands.",
            },
            {
                "key": "ServerPassword",
                "label": "Join Password",
                "type": "str",
                "input_type": "password",
                "default": "",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Leave blank for a public server.",
            },
            {
                "key": "MaxPlayers",
                "label": "Max Players",
                "type": "int",
                "default": "70",
                "section": "/Script/Engine.GameSession",
                "file": "gus",
                "help": "Maximum concurrent players.",
                "min": 1, "max": 255, "step": 1,
            },
            {
                "key": "RCONEnabled",
                "label": "Enable RCON",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Allow remote console connections.",
            },
            {
                "key": "RCONPort",
                "label": "RCON Port",
                "type": "int",
                "default": "27020",
                "section": "ServerSettings",
                "file": "gus",
                "help": "TCP port for RCON connections.",
                "min": 1024, "max": 65535, "step": 1,
            },
            {
                "key": "AutoSavePeriodMinutes",
                "label": "Auto-Save Interval (minutes)",
                "type": "float",
                "default": "15.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How often the world is saved automatically.",
                "min": 1.0, "max": 120.0, "step": 1.0, "slider_max": 60.0,
            },
        ],
    },

    # ── Environment ──────────────────────────────────────────────────────
    {
        "id": "environment",
        "label": "Environment",
        "icon": "bi-sun",
        "fields": [
            {
                "key": "DayCycleSpeedScale",
                "label": "Day/Night Cycle Speed",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Scales the entire day/night cycle. Higher = shorter days and nights.",
                "min": 0.001, "max": 100.0, "step": 0.001, "slider_max": 10.0,
            },
            {
                "key": "DayTimeSpeedScale",
                "label": "Daytime Speed",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Multiplier for daytime only. Higher = shorter days.",
                "min": 0.001, "max": 100.0, "step": 0.001, "slider_max": 10.0,
            },
            {
                "key": "NightTimeSpeedScale",
                "label": "Nighttime Speed",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Multiplier for nighttime only. Higher = shorter nights.",
                "min": 0.001, "max": 100.0, "step": 0.001, "slider_max": 10.0,
            },
            {
                "key": "OverrideOfficialDifficulty",
                "label": "Override Official Difficulty",
                "type": "float",
                "default": "5.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Sets max wild dino level. 5.0 = level 150 max. Requires DifficultyOffset=1.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "DifficultyOffset",
                "label": "Difficulty Offset",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "0.0–1.0. Set to 1.0 to use OverrideOfficialDifficulty.",
                "min": 0.0, "max": 1.0, "step": 0.01, "slider_max": 1.0,
            },
            {
                "key": "DinoCountMultiplier",
                "label": "Dino Count Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Scales the number of wild dinos spawned.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "HarvestAmountMultiplier",
                "label": "Harvest Amount Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Multiplies resources gained per harvest action.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "HarvestHealthMultiplier",
                "label": "Harvest Health Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Multiplies the HP of harvestable resources.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "ResourcesRespawnPeriodMultiplier",
                "label": "Resource Respawn Period",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Scales how long resources take to respawn. Lower = faster.",
                "min": 0.001, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "ItemStackSizeMultiplier",
                "label": "Item Stack Size Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Multiplies the max stack size of stackable items.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
        ],
    },

    # ── Players ──────────────────────────────────────────────────────────
    {
        "id": "players",
        "label": "Players",
        "icon": "bi-person-fill",
        "fields": [
            {
                "key": "XPMultiplier",
                "label": "XP Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Experience gained by players.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "PlayerDamageMultiplier",
                "label": "Player Damage Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage dealt by players.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "PlayerResistanceMultiplier",
                "label": "Player Resistance Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage received by players (lower = more resistant).",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "PlayerCharacterWaterDrainMultiplier",
                "label": "Water Drain Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly player water stat depletes.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "PlayerCharacterFoodDrainMultiplier",
                "label": "Food Drain Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly player food stat depletes.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "PlayerCharacterStaminaDrainMultiplier",
                "label": "Stamina Drain Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly player stamina depletes.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "PlayerCharacterHealthRecoveryMultiplier",
                "label": "Health Recovery Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly player health regenerates.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
        ],
    },

    # ── Dinos ────────────────────────────────────────────────────────────
    {
        "id": "dinos",
        "label": "Dinos",
        "icon": "bi-bug-fill",
        "fields": [
            {
                "key": "TamingSpeedMultiplier",
                "label": "Taming Speed Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly dinos are tamed.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "WildDinoDamageMultiplier",
                "label": "Wild Dino Damage Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage dealt by wild dinos.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "WildDinoResistanceMultiplier",
                "label": "Wild Dino Resistance Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage received by wild dinos (lower = tougher).",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "WildDinoCharacterFoodDrainMultiplier",
                "label": "Wild Dino Food Drain",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly wild dinos' food depletes (affects taming).",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "TamedDinoDamageMultiplier",
                "label": "Tamed Dino Damage Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage dealt by tamed dinos.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "TamedDinoResistanceMultiplier",
                "label": "Tamed Dino Resistance Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage received by tamed dinos (lower = tougher).",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "DinoCharacterFoodDrainMultiplier",
                "label": "Tamed Dino Food Drain",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly tamed dinos' food depletes.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "DinoCharacterStaminaDrainMultiplier",
                "label": "Tamed Dino Stamina Drain",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly tamed dinos' stamina depletes.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "DinoCharacterHealthRecoveryMultiplier",
                "label": "Tamed Dino Health Recovery",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How quickly tamed dinos' health regenerates.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "MaxTamedDinos",
                "label": "Max Tamed Dinos",
                "type": "float",
                "default": "5000.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Global cap on the total number of tamed dinos on the server.",
                "min": 0.0, "max": 50000.0, "step": 100.0, "slider_max": 10000.0,
            },
        ],
    },

    # ── Breeding ─────────────────────────────────────────────────────────
    {
        "id": "breeding",
        "label": "Breeding",
        "icon": "bi-heart-fill",
        "fields": [
            {
                "key": "MatingIntervalMultiplier",
                "label": "Mating Interval Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "Cooldown between matings. Lower = shorter cooldown.",
                "min": 0.001, "max": 100.0, "step": 0.01, "slider_max": 5.0,
            },
            {
                "key": "MatingSpeedMultiplier",
                "label": "Mating Speed Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "Speed of the mating progress bar.",
                "min": 0.001, "max": 100.0, "step": 0.01, "slider_max": 10.0,
            },
            {
                "key": "EggHatchSpeedMultiplier",
                "label": "Egg Hatch Speed Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "How quickly fertilised eggs incubate.",
                "min": 0.001, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "BabyMatureSpeedMultiplier",
                "label": "Baby Mature Speed Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "How quickly baby dinos grow to adult. Higher = faster.",
                "min": 0.001, "max": 1000.0, "step": 0.1, "slider_max": 50.0,
            },
            {
                "key": "BabyFoodConsumptionSpeedMultiplier",
                "label": "Baby Food Consumption Speed",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "How quickly baby dinos consume food.",
                "min": 0.001, "max": 100.0, "step": 0.01, "slider_max": 5.0,
            },
            {
                "key": "LayEggIntervalMultiplier",
                "label": "Lay Egg Interval Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "Cooldown between egg laying. Lower = more frequent.",
                "min": 0.001, "max": 100.0, "step": 0.01, "slider_max": 5.0,
            },
            {
                "key": "BabyCuddleIntervalMultiplier",
                "label": "Cuddle Interval Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "Time between imprint cuddle opportunities. Lower = more frequent.",
                "min": 0.001, "max": 100.0, "step": 0.01, "slider_max": 5.0,
            },
            {
                "key": "BabyCuddleGracePeriodMultiplier",
                "label": "Cuddle Grace Period Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "/Script/ShooterGame.ShooterGameMode",
                "file": "gi",
                "help": "Window of time to complete an imprint cuddle.",
                "min": 0.001, "max": 100.0, "step": 0.01, "slider_max": 5.0,
            },
            {
                "key": "BabyImprintingStatScaleMultiplier",
                "label": "Imprint Stat Scale Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Scales the stat bonus from imprinting.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "AllowAnyoneBabyImprintCuddle",
                "label": "Allow Anyone to Imprint Cuddle",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Any player can cuddle a baby, not just the original tamer.",
            },
        ],
    },

    # ── Structures ───────────────────────────────────────────────────────
    {
        "id": "structures",
        "label": "Structures",
        "icon": "bi-buildings",
        "fields": [
            {
                "key": "StructureDamageMultiplier",
                "label": "Structure Damage Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage dealt to structures.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "StructureResistanceMultiplier",
                "label": "Structure Resistance Multiplier",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Damage received by structures (lower = tougher).",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "DisableStructureDecayPvE",
                "label": "Disable Structure Decay (PvE)",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Turns off automatic structure decay timers in PvE mode.",
            },
            {
                "key": "PvEStructureDecayPeriodMultiplier",
                "label": "PvE Structure Decay Period",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Multiplies PvE structure decay timers. Higher = slower decay.",
                "min": 0.0, "max": 100.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "TheMaxStructuresInRange",
                "label": "Max Structures In Range",
                "type": "float",
                "default": "10500.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Max number of structures within the anti-mesh check radius.",
                "min": 0.0, "max": 50000.0, "step": 100.0, "slider_max": 25000.0,
            },
            {
                "key": "PerPlatformMaxStructuresMultiplier",
                "label": "Platform Saddle Structure Limit",
                "type": "float",
                "default": "1.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Multiplies the max structures allowed on platform saddles.",
                "min": 0.0, "max": 20.0, "step": 0.1, "slider_max": 10.0,
            },
            {
                "key": "StructurePickupTimeAfterPlacement",
                "label": "Structure Pickup Window (seconds)",
                "type": "float",
                "default": "30.000000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Seconds after placement during which a structure can be picked up.",
                "min": 0.0, "max": 3600.0, "step": 1.0, "slider_max": 300.0,
            },
            {
                "key": "StructurePickupHoldDuration",
                "label": "Structure Pickup Hold Duration (seconds)",
                "type": "float",
                "default": "0.500000",
                "section": "ServerSettings",
                "file": "gus",
                "help": "How long to hold the pickup key to collect a structure.",
                "min": 0.0, "max": 10.0, "step": 0.1, "slider_max": 5.0,
            },
            {
                "key": "AlwaysAllowStructurePickup",
                "label": "Always Allow Structure Pickup",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Structures can be picked up at any time (ignores placement timer).",
            },
            {
                "key": "AllowFlyerCarryPVE",
                "label": "Allow Flyer Carry (PvE)",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Allow tamed flyers to pick up creatures in PvE mode.",
            },
        ],
    },

    # ── Rules ────────────────────────────────────────────────────────────
    {
        "id": "rules",
        "label": "Rules",
        "icon": "bi-toggles",
        "fields": [
            {
                "key": "ServerPVE",
                "label": "Enable PvE Mode",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Prevents players from damaging each other's creatures and structures.",
            },
            {
                "key": "ServerHardcore",
                "label": "Hardcore Mode",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Player character is deleted permanently on death.",
            },
            {
                "key": "AllowThirdPersonPlayer",
                "label": "Allow Third-Person View",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Players can switch to third-person camera mode.",
            },
            {
                "key": "ShowMapPlayerLocation",
                "label": "Show Player Location on Map",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "GPS position is shown on the in-game map.",
            },
            {
                "key": "ServerCrosshair",
                "label": "Enable Crosshair",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Show the aim crosshair for players.",
            },
            {
                "key": "AllowHitMarkers",
                "label": "Allow Hit Markers",
                "type": "bool",
                "default": "True",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Show hit confirmation markers on screen.",
            },
            {
                "key": "ServerForceNoHUD",
                "label": "Force No HUD",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Removes all HUD elements for all players.",
            },
            {
                "key": "EnablePVPGamma",
                "label": "Allow Gamma in PvP",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Players can use gamma command on PvP servers.",
            },
            {
                "key": "GlobalVoiceChat",
                "label": "Global Voice Chat",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "All players hear each other regardless of distance.",
            },
            {
                "key": "ProximityChat",
                "label": "Proximity Text Chat",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Text chat is limited to nearby players.",
            },
            {
                "key": "AlwaysNotifyPlayerJoined",
                "label": "Announce Player Join",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Broadcasts a message when any player joins.",
            },
            {
                "key": "AlwaysNotifyPlayerLeft",
                "label": "Announce Player Leave",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Broadcasts a message when any player leaves.",
            },
            {
                "key": "AllowCaveBuildingPvE",
                "label": "Allow Cave Building (PvE)",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Allows structure placement inside caves in PvE mode.",
            },
            {
                "key": "PreventOfflinePvP",
                "label": "Prevent Offline Raiding",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Structures and dinos become invulnerable when the owner tribe is offline.",
            },
            {
                "key": "DisableDinoDecayPvE",
                "label": "Disable Dino Decay (PvE)",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Tamed dinos do not decay and auto-release in PvE.",
            },
            {
                "key": "AdminLogging",
                "label": "Enable Admin Logging",
                "type": "bool",
                "default": "False",
                "section": "ServerSettings",
                "file": "gus",
                "help": "Admin commands are logged to the tribe log of all players.",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Manager class
# ---------------------------------------------------------------------------

class ArkIniManager:
    """
    Read/write manager for a specific ARK server instance's .ini files.

    Args:
        install_path: GameServer.install_path from the database,
                      e.g. ``/home/arkserver``.
    """

    def __init__(self, install_path: str) -> None:
        cfg_base = ark_cfg_dir(install_path)
        self.GAME_USER_SETTINGS = os.path.join(cfg_base, "GameUserSettings.ini")
        self.GAME_INI           = os.path.join(cfg_base, "Game.ini")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def is_server_running(self) -> bool:
        """Return True if a ShooterGameServer process is currently running."""
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name    = proc.info.get("name") or ""
                cmdline = proc.info.get("cmdline") or []
                if _ARK_PROC_NAME in name or any(
                    _ARK_PROC_NAME in arg for arg in cmdline
                ):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def backup(self, filepath: str) -> str:
        """
        Create a timestamped backup of *filepath* in the same directory.
        Returns the backup path, or "" if the source file does not exist yet.
        """
        if not os.path.isfile(filepath):
            return ""
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{filepath}.bak.{ts}"
        shutil.copy2(filepath, bak)
        return bak

    def get_value(
        self, filepath: str, section: str, key: str, fallback: str = ""
    ) -> str:
        """Return the raw string value of *key* in *section* from *filepath*."""
        try:
            lines = self._read_lines(filepath)
        except (OSError, IOError):
            return fallback

        current_section: str | None = None
        for line in lines:
            stripped = line.strip()
            sec_match = _SECTION_RE.match(stripped)
            if sec_match:
                current_section = sec_match.group(1)
                continue
            if current_section == section:
                kv_match = _KV_RE.match(stripped)
                if kv_match and kv_match.group(1) == key:
                    return kv_match.group(2)
        return fallback

    def set_values(self, filepath: str, updates: dict) -> None:
        """
        Write *updates* into *filepath*, preserving all other content.
        ``updates`` maps ``(section, key)`` tuples to new string values.
        Missing keys are appended; missing sections are created at end of file.
        """
        try:
            lines = self._read_lines(filepath)
        except (OSError, IOError):
            lines = []

        sec_map: dict[str, dict[str, str]] = {}
        for (sec, key), val in updates.items():
            sec_map.setdefault(sec, {})[key] = val

        written: dict[tuple, bool] = {k: False for k in updates}
        result:  list[str]         = []
        current_section: str | None = None

        for line in lines:
            stripped  = line.strip()
            sec_match = _SECTION_RE.match(stripped)
            if sec_match:
                if current_section and current_section in sec_map:
                    self._flush_missing(result, current_section, sec_map, written)
                current_section = sec_match.group(1)
                result.append(line)
                continue

            if current_section and current_section in sec_map:
                kv_match = _KV_RE.match(stripped)
                if kv_match:
                    key = kv_match.group(1)
                    if key in sec_map[current_section]:
                        eol = "\r\n" if line.endswith("\r\n") else "\n"
                        result.append(
                            f"{key}={sec_map[current_section][key]}{eol}"
                        )
                        written[(current_section, key)] = True
                        continue

            result.append(line)

        if current_section and current_section in sec_map:
            self._flush_missing(result, current_section, sec_map, written)

        missing_by_sec: dict[str, dict[str, str]] = {}
        for (sec, key), done in written.items():
            if not done:
                missing_by_sec.setdefault(sec, {})[key] = updates[(sec, key)]

        for sec, kvs in missing_by_sec.items():
            if result and result[-1].strip():
                result.append("\n")
            result.append(f"[{sec}]\n")
            for key, val in kvs.items():
                result.append(f"{key}={val}\n")

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.writelines(result)

    # ------------------------------------------------------------------
    # Schema-driven convenience wrappers used by the Flask route
    # ------------------------------------------------------------------

    def read_all_settings(self) -> dict:
        """
        Return a flat dict of all settings defined in SETTINGS_SCHEMA.
        Boolean values are returned as Python bools; all others as strings.
        """
        result = {}
        for tab in SETTINGS_SCHEMA:
            for field in tab["fields"]:
                filepath = (
                    self.GAME_USER_SETTINGS if field["file"] == "gus"
                    else self.GAME_INI
                )
                raw = self.get_value(
                    filepath, field["section"], field["key"],
                    str(field["default"]),
                )
                if field["type"] == "bool":
                    result[field["key"]] = raw.strip().lower() == "true"
                else:
                    result[field["key"]] = raw
        return result

    def build_updates(self, form_data) -> tuple[dict, dict]:
        """
        Convert form POST data into two ``set_values`` update dicts.
        Returns ``(gus_updates, gi_updates)``.
        """
        gus_updates: dict = {}
        gi_updates:  dict = {}

        for tab in SETTINGS_SCHEMA:
            for field in tab["fields"]:
                key     = field["key"]
                default = str(field["default"])
                ftype   = field["type"]
                raw     = form_data.get(key, "")

                if ftype == "bool":
                    formatted = "True" if key in form_data else "False"
                elif ftype == "float":
                    try:
                        formatted = f"{float(raw or default):.6f}"
                    except (ValueError, TypeError):
                        formatted = f"{float(default):.6f}"
                elif ftype == "int":
                    try:
                        formatted = str(int(float(raw or default)))
                    except (ValueError, TypeError):
                        formatted = str(int(float(default)))
                else:  # str
                    formatted = (raw or default).strip()

                update_key = (field["section"], key)
                if field["file"] == "gus":
                    gus_updates[update_key] = formatted
                else:
                    gi_updates[update_key] = formatted

        return gus_updates, gi_updates

    def validate_form(self, form_data) -> list[str]:
        """
        Validate numeric form fields against schema min/max constraints.
        Returns a list of error strings (empty = all valid).
        """
        errors = []
        for tab in SETTINGS_SCHEMA:
            for field in tab["fields"]:
                if field["type"] not in ("float", "int"):
                    continue
                raw = form_data.get(field["key"], "")
                if not raw:
                    continue
                try:
                    val = float(raw)
                except ValueError:
                    errors.append(
                        f"'{field['label']}': not a valid number ({raw!r})"
                    )
                    continue
                if "min" in field and val < field["min"]:
                    errors.append(
                        f"'{field['label']}': {val} is below minimum {field['min']}"
                    )
                if "max" in field and val > field["max"]:
                    errors.append(
                        f"'{field['label']}': {val} exceeds maximum {field['max']}"
                    )
        return errors

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_lines(filepath: str) -> list[str]:
        with open(filepath, "r", encoding="utf-8") as fh:
            return fh.readlines()

    @staticmethod
    def _flush_missing(
        result: list[str],
        section: str,
        sec_map: dict,
        written: dict,
    ) -> None:
        for key, val in sec_map[section].items():
            if not written.get((section, key), True):
                result.append(f"{key}={val}\n")
                written[(section, key)] = True


# ---------------------------------------------------------------------------
# Module-level compiled regexes
# ---------------------------------------------------------------------------
_SECTION_RE = re.compile(r"^\[(.+)\]\s*$")
_KV_RE      = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)=(.*)")
