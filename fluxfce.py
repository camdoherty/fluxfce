#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
fluxfce v0.1: Automatic XFCE Theming Tool (Python/atd Edition)

Switches XFCE GTK theme, background color/gradient, and screen temperature
based on calculated sunrise/sunset times using the system 'atd' scheduler.
Includes preset management and respects manual overrides.

Core Scheduling Concept:
- A daily systemd timer triggers 'fluxfce schedule-jobs'.
- 'schedule-jobs' calculates the next ~24hrs of sunrise/sunset times.
- It clears old 'at' jobs and schedules new 'at' jobs to run
  'fluxfce internal-apply --mode <day|night>' precisely at the
  calculated sunrise/sunset times.
- A systemd service runs 'fluxfce run-login-check' shortly after login
  to apply the correct theme immediately.
- Manual commands ('apply', 'force-*', 'toggle') apply settings AND clear
  pending 'at' jobs to prevent reverts.
- 'enable' schedules jobs; 'disable' clears them.

Usage Examples:
  # Initial setup (interactive)
  fluxfce install

  # Apply a saved preset (disables scheduled automatic changes)
  fluxfce apply my_preset

  # Re-enable automatic scheduling
  fluxfce enable

  # Disable automatic scheduling
  fluxfce disable

  # Show status (config, calculated times, scheduled jobs)
  fluxfce status

  # --- Other Commands ---
  # fluxfce save <name>
  # fluxfce list-presets
  # fluxfce delete-preset <name>
  # fluxfce force-day | force-night | toggle
  # fluxfce config [--get KEY | --set KEY=VALUE]
  # fluxfce uninstall
"""

import tempfile
import argparse
import configparser
import logging
import math
import os
import pathlib
import re
import subprocess
import sys
import time
import shutil # Add this import at the top of the file
from datetime import date, datetime, timedelta, timezone
from typing import List, Tuple, Optional, Dict, Any
# zoneinfo is standard library in Python 3.9+
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    print("Error: 'zoneinfo' module not found. Requires Python 3.9+.", file=sys.stderr)
    sys.exit(1)


# --- Constants ---
APP_NAME = "fluxfce"
CONFIG_DIR = pathlib.Path.home() / ".config" / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.ini"
PRESETS_FILE = CONFIG_DIR / "presets.ini"
STATE_FILE = CONFIG_DIR / "state" # Tracks last *auto* applied state ('day'/'night')
SYSTEMD_USER_DIR = pathlib.Path.home() / ".config" / "systemd" / "user"

# Systemd Unit Names
LOGIN_SERVICE_NAME = f"{APP_NAME}-login.service"
SCHEDULER_SERVICE_NAME = f"{APP_NAME}-scheduler.service"
SCHEDULER_TIMER_NAME = f"{APP_NAME}-scheduler.timer"

# File Paths
LOGIN_SERVICE_FILE = SYSTEMD_USER_DIR / LOGIN_SERVICE_NAME
SCHEDULER_SERVICE_FILE = SYSTEMD_USER_DIR / SCHEDULER_SERVICE_NAME
SCHEDULER_TIMER_FILE = SYSTEMD_USER_DIR / SCHEDULER_TIMER_NAME

# XFCE Constants
XFCONF_CHANNEL = "xfce4-desktop"
XFCONF_THEME_CHANNEL = "xsettings"
XFCONF_THEME_PROPERTY = "/Net/ThemeName"

# atd Job Identification Tag (used in commands scheduled with 'at')
AT_JOB_TAG = f"# {APP_NAME}_marker"

# Default configuration values
DEFAULT_CONFIG = {
    'Location': {
        'LATITUDE': "43.65N",    # Toronto Latitude (Example)
        'LONGITUDE': "79.38W",   # Toronto Longitude (Example)
        'TIMEZONE': "America/Toronto", # IANA Timezone Name
    },
    'Themes': {
        'LIGHT_THEME': "Adwaita",
        'DARK_THEME': "Adwaita-dark",
    },
    'BackgroundDay': {
        'BG_HEX1': "ADD8E6",
        'BG_HEX2': "87CEEB",
        'BG_DIR': "v",
    },
    'ScreenDay': {
        'XSCT_TEMP': "6500",
        'XSCT_BRIGHT': "1.0",
    },
    'BackgroundNight': {
        'BG_HEX1': "1E1E2E",
        'BG_HEX2': "000000",
        'BG_DIR': "v",
    },
    'ScreenNight': {
        'XSCT_TEMP': "4500",
        'XSCT_BRIGHT': "0.85",
    }
    # No 'General'/'AUTO_ENABLED' - managed by presence/absence of 'at' jobs
}

# Logging setup
log = logging.getLogger(APP_NAME)


# --- Helper Functions ---

def setup_logging(verbose: bool):
    """Configures logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s') # Simpler format
    # Suppress overly verbose configparser debug logs if not needed
    if not verbose:
        logging.getLogger("configparser").setLevel(logging.WARNING)


def run_command(cmd_list: List[str], check: bool = False, capture: bool = True, input_str: Optional[str] = None) -> Tuple[int, str, str]:
    """Runs an external command and returns status, stdout, stderr."""
    log.debug(f"Running command: {' '.join(cmd_list)}")
    stdin_pipe = subprocess.PIPE if input_str is not None else None
    stdout_pipe = subprocess.PIPE if capture else None
    stderr_pipe = subprocess.PIPE if capture else None
    try:
        process = subprocess.run(
            cmd_list,
            check=check,
            input=input_str,
            stdout=stdout_pipe,
            stderr=stderr_pipe,
            text=True,
            encoding='utf-8'
        )
        stdout = process.stdout.strip() if process.stdout else ""
        stderr = process.stderr.strip() if process.stderr else ""
        log.debug(f"Command finished with code {process.returncode}")
        if stdout and capture: log.debug(f"stdout: {stdout}") # Only log if captured
        if stderr and capture: log.debug(f"stderr: {stderr}") # Only log if captured
        return process.returncode, stdout, stderr
    except FileNotFoundError:
        log.error(f"Command not found: {cmd_list[0]}")
        return -1, "", f"Command not found: {cmd_list[0]}"
    except subprocess.CalledProcessError as e:
        # This is expected if check=True and command fails
        stdout = e.stdout.strip() if e.stdout else ""
        stderr = e.stderr.strip() if e.stderr else ""
        log.warning(f"Command failed with exit code {e.returncode}: {' '.join(cmd_list)}")
        if stdout and capture: log.warning(f"stdout: {stdout}")
        if stderr and capture: log.warning(f"stderr: {stderr}")
        return e.returncode, stdout, stderr
    except Exception as e:
        log.exception(f"An unexpected error occurred running command: {' '.join(cmd_list)} - {e}")
        return -2, "", str(e)

def check_dependencies(deps: List[str]) -> bool:
    """Checks if required external commands exist in PATH using shutil.which."""
    missing = []
    for dep in deps:
        if shutil.which(dep) is None: # shutil.which returns None if not found
            missing.append(dep)
    if missing:
        log.error(f"Missing required command(s): {', '.join(missing)}")
        log.error("Please install them using your package manager (e.g., apt, dnf).")
        return False
    log.debug(f"All dependencies checked successfully: {', '.join(deps)}")
    return True

def check_atd_service() -> bool:
    """Checks if the 'atd' service appears to be running."""
    # Check system service first, then user service as fallback (less common)
    log.debug("Checking if atd service is active...")
    code_sys, _, err_sys = run_command(['systemctl', 'is-active', 'atd.service'], capture=True)
    if code_sys == 0:
        log.info("System 'atd' service is active.")
        return True

    log.warning(f"System 'atd' service not active (or check failed: {err_sys}). Checking user service...")
    code_user, _, err_user = run_command(['systemctl', '--user', 'is-active', 'atd.service'], capture=True)
    if code_user == 0:
        log.info("User 'atd' service is active.")
        return True

    log.error("The 'atd' service does not appear to be active.")
    log.error("Please install and enable 'atd' (e.g., 'sudo apt install at && sudo systemctl enable --now atd')")
    return False

def latlon_str_to_float(coord_str: str) -> Optional[float]:
    """Converts Lat/Lon string (e.g., '43.65N', '79.38W') to float degrees."""
    coord_str = coord_str.strip().upper()
    match = re.match(r'^(\d+(\.\d+)?)([NSEW])$', coord_str)
    if not match:
        log.error(f"Invalid coordinate format: '{coord_str}'. Use format like '43.65N' or '79.38W'.")
        return None
    value = float(match.group(1))
    direction = match.group(3)
    if direction in ('S', 'W'):
        value = -value
    # Basic range check
    if direction in ('N', 'S') and not (-90 <= value <= 90):
         log.error(f"Latitude out of range (-90 to 90): {value}")
         return None
    if direction in ('E', 'W') and not (-180 <= value <= 180):
         log.error(f"Longitude out of range (-180 to 180): {value}")
         return None
    return value

def hex_to_rgba_doubles(hex_color: str) -> Optional[List[float]]:
    """Converts a 6-digit hex color string (#RRGGBB or RRGGBB) to RGBA doubles [R, G, B, A] (0.0-1.0)."""
    hex_color = hex_color.lstrip('#')
    if not re.match(r'^[0-9a-fA-F]{6}$', hex_color):
        log.error(f"Invalid hex color format: '{hex_color}'")
        return None
    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return [r, g, b, 1.0] # R, G, B, Alpha
    except ValueError:
        log.error(f"Could not convert hex to int: '{hex_color}'")
        return None


# --- Sun Calculation (Integrated NOAA Algorithm) ---

def _noaa_sunrise_sunset(
    *, lat: float, lon: float, target_date: date
) -> tuple[float, float]:
    """
    Internal NOAA algorithm. Returns (sunrise_utc_min, sunset_utc_min).
    Based on NOAA Javascript: www.esrl.noaa.gov/gmd/grad/solcalc/calcdetails.html
    """
    # Validate latitude range
    if not (-90 <= lat <= 90):
        raise ValueError("Latitude must be between -90 and 90 degrees.")
    # Validate longitude range
    if not (-180 <= lon <= 180):
         raise ValueError("Longitude must be between -180 and 180 degrees.")

    n = target_date.timetuple().tm_yday # Day of year
    longitude = lon # Use input directly

    # Eq of Time and Declination (approximation)
    gamma = (2 * math.pi / 365) * (n - 1 + (12 - (longitude / 15)) / 24) # Fractional year
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma) \
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = 0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) \
           - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma) \
           - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)

    # Hour Angle
    lat_rad = math.radians(lat)
    cos_zenith = math.cos(math.radians(90.833)) # Zenith for sunrise/sunset
    cos_h_arg = (cos_zenith - math.sin(lat_rad) * math.sin(decl)) / (math.cos(lat_rad) * math.cos(decl))

    # Check for polar day/night
    if cos_h_arg > 1.0:  # Sun never rises
        return -999, -999 # Indicate error/no rise
    if cos_h_arg < -1.0: # Sun never sets
        return -998, -998 # Indicate error/no set

    ha_rad = math.acos(cos_h_arg)
    ha_minutes = 4 * math.degrees(ha_rad) # Convert hour angle to minutes

    # Solar noon (minutes from UTC midnight)
    solar_noon = 720 - 4 * longitude - eqtime # 720 = noon in minutes

    sunrise_utc = solar_noon - ha_minutes
    sunset_utc = solar_noon + ha_minutes

    return sunrise_utc, sunset_utc


def get_sun_times(
    lat: float, lon: float, target_date: date, tz_name: str
) -> Optional[Dict[str, datetime]]:
    """
    Return sunrise & sunset as timezone-aware datetimes for the given date,
    coordinate, and timezone. Returns None on calculation error (e.g., polar night).
    """
    try:
        tz_info = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        log.error(f"Invalid or unknown IANA Timezone Name: '{tz_name}'")
        return None

    try:
        sunrise_min, sunset_min = _noaa_sunrise_sunset(
            lat=lat, lon=lon, target_date=target_date
        )
    except ValueError as e: # Catch errors from calculation (e.g. invalid lat/lon)
        log.error(f"Sun time calculation error: {e}")
        return None

    # Handle polar day/night indicators
    if sunrise_min == -999:
        log.warning(f"Sun never rises on {target_date} at {lat}, {lon}. Cannot schedule.")
        return None # Or return a specific indicator? None is simpler for now.
    if sunrise_min == -998:
        log.warning(f"Sun never sets on {target_date} at {lat}, {lon}. Cannot schedule both events.")
        # Could potentially schedule only one event if needed, but complicates logic.
        return None

    # Convert minutes from UTC midnight to datetime objects
    utc_midnight = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    sunrise_utc_dt = utc_midnight + timedelta(minutes=sunrise_min)
    sunset_utc_dt = utc_midnight + timedelta(minutes=sunset_min)

    # Convert to the target local timezone
    sunrise_local = sunrise_utc_dt.astimezone(tz_info)
    sunset_local = sunset_utc_dt.astimezone(tz_info)

    return {"sunrise": sunrise_local, "sunset": sunset_local}


# --- Configuration Manager ---
# (Identical to previous version - handles config.ini, presets.ini, state file)
class ConfigManager:
    """Handles reading/writing config, presets, and state."""

    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def _load_ini(self, file_path: pathlib.Path) -> configparser.ConfigParser:
        """Loads an INI file."""
        parser = configparser.ConfigParser()
        if file_path.exists():
            try:
                # Handle potential empty file
                if file_path.stat().st_size > 0:
                    parser.read(file_path, encoding='utf-8')
                    log.debug(f"Loaded config from {file_path}")
                else:
                     log.warning(f"Config file {file_path} is empty.")
            except configparser.Error as e:
                 log.warning(f"Could not parse config file {file_path}: {e}")
        return parser

    def _save_ini(self, parser: configparser.ConfigParser, file_path: pathlib.Path):
        """Saves an INI file."""
        try:
            with file_path.open('w', encoding='utf-8') as f:
                parser.write(f)
            log.info(f"Saved configuration to {file_path}")
        except IOError as e:
            log.error(f"Failed to write configuration to {file_path}: {e}")

    def load_config(self) -> configparser.ConfigParser:
        """Loads the main config.ini, applying defaults if sections/keys are missing."""
        parser = self._load_ini(CONFIG_FILE)
        made_changes = False
        for section, defaults in DEFAULT_CONFIG.items():
            if not parser.has_section(section):
                parser.add_section(section)
                made_changes = True
                log.debug(f"Added missing section [{section}] to config")
            for key, value in defaults.items():
                if not parser.has_option(section, key):
                    parser.set(section, key, value)
                    made_changes = True
                    log.debug(f"Added missing key '{key}' = '{value}' to section [{section}]")

        # Only save back if changes were made and the file exists or was loaded
        # This prevents creating a default file just by loading if it wasn't there.
        if made_changes and parser.sections(): # Check if parser isn't empty
             if CONFIG_FILE.exists() or len(parser.sections()) > len(DEFAULT_CONFIG): # Avoid saving *only* defaults if file was initially missing
                 log.info("Applying default values to config file.")
                 self._save_ini(parser, CONFIG_FILE)
        return parser

    def save_config(self, config: configparser.ConfigParser):
        """Saves the main config.ini."""
        self._save_ini(config, CONFIG_FILE)

    def load_presets(self) -> configparser.ConfigParser:
        """Loads presets.ini."""
        return self._load_ini(PRESETS_FILE)

    def save_presets(self, presets: configparser.ConfigParser):
        """Saves presets.ini."""
        self._save_ini(presets, PRESETS_FILE)

    def get_setting(self, config: configparser.ConfigParser, section: str, key: str, default: Optional[str] = None) -> Optional[str]:
        """Gets a setting value."""
        return config.get(section, key, fallback=default)

    # get_bool_setting removed as AUTO_ENABLED is gone

    def set_setting(self, config: configparser.ConfigParser, section: str, key: str, value: str):
        """Sets a setting value."""
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, value)

    def read_state(self) -> Optional[str]:
        """Reads the last known auto-applied state ('day' or 'night')."""
        if STATE_FILE.exists():
            try:
                state = STATE_FILE.read_text(encoding='utf-8').strip()
                if state in ('day', 'night'):
                    log.debug(f"Read state: {state}")
                    return state
                else:
                    log.warning(f"Invalid content in state file {STATE_FILE}. Ignoring.")
                    # --- CORRECTED BLOCK ---
                    try:
                        # Try to remove the invalid state file
                        STATE_FILE.unlink()
                        log.debug(f"Removed invalid state file: {STATE_FILE}")
                    except OSError as e:
                        # Log if removal fails (e.g., permissions) but continue
                        log.warning(f"Could not remove invalid state file {STATE_FILE}: {e}")
                    # --- END CORRECTION ---
                    return None # Return None as state is invalid/unknown
            except IOError as e:
                log.warning(f"Could not read state file {STATE_FILE}: {e}")
                return None
        log.debug("State file not found.")
        return None

    def write_state(self, state: str):
        """Writes the current auto-applied state ('day' or 'night')."""
        if state not in ('day', 'night'):
            log.error(f"Attempted to write invalid state: {state}")
            return
        try:
            STATE_FILE.write_text(state, encoding='utf-8')
            log.info(f"State successfully written: {state}")
        except IOError as e:
            log.error(f"Failed to write state file {STATE_FILE}: {e}")

    def get_preset_settings(self, presets: configparser.ConfigParser, name: str) -> Optional[Dict[str, str]]:
        """Gets settings for a specific preset."""
        section_name = f"Preset_{name}"
        if presets.has_section(section_name):
            return dict(presets.items(section_name))
        return None

    def save_preset_settings(self, presets: configparser.ConfigParser, name: str, settings: Dict[str, str]):
        """Saves settings for a specific preset."""
        section_name = f"Preset_{name}"
        if not presets.has_section(section_name):
            presets.add_section(section_name)
        for key, value in settings.items():
            # Ensure value is string and handle potential None
            presets.set(section_name, key, str(value) if value is not None else '')

    def delete_preset(self, presets: configparser.ConfigParser, name: str) -> bool:
        """Deletes a preset section."""
        section_name = f"Preset_{name}"
        if presets.has_section(section_name):
            presets.remove_section(section_name)
            return True
        return False

    def list_preset_names(self, presets: configparser.ConfigParser) -> List[str]:
        """Lists names of saved presets."""
        names = []
        prefix = "Preset_"
        for section in presets.sections():
            if section.startswith(prefix):
                names.append(section[len(prefix):])
        return names


# --- XFCE Interaction Handler ---
# (Largely unchanged from previous version)
class XfceHandler:
    """Handles interactions with XFCE settings via xfconf-query and xsct."""

    def find_desktop_paths(self) -> List[str]:
        """Finds relevant XFCE desktop property base paths for background."""
        cmd = ['xfconf-query', '-c', XFCONF_CHANNEL, '-l']
        code, stdout, stderr = run_command(cmd)
        if code != 0:
            log.error(f"Failed to list xfconf properties: {stderr}")
            return []

        paths = set()
        # Prioritize monitor + workspace combo paths
        # Example: /backdrop/screen0/monitorDP-1/workspace0/last-image
        prop_pattern = re.compile(r'(/backdrop/screen\d+/[\w-]+/workspace\d+)/last-image$')
        for line in stdout.splitlines():
            match = prop_pattern.match(line.strip())
            if match:
                paths.add(match.group(1))

        if paths:
            sorted_paths = sorted(list(paths))
            log.info(f"Found {len(sorted_paths)} workspace background paths: {sorted_paths}")
            return sorted_paths
        
        # Fallback to monitor level only if no workspace paths found (less common)
        monitor_paths = set()
        monitor_pattern = re.compile(r'(/backdrop/screen\d+/[\w-]+)/last-image$')
        for line in stdout.splitlines():
             match = monitor_pattern.match(line.strip())
             if match and match.group(1) not in [p.rsplit('/', 1)[0] for p in paths]: # Avoid double-adding parent
                 monitor_paths.add(match.group(1))

        if monitor_paths:
             sorted_paths = sorted(list(monitor_paths))
             log.info(f"Found {len(sorted_paths)} monitor background paths (fallback): {sorted_paths}")
             return sorted_paths

        # Last resort default
        default_path = "/backdrop/screen0/monitorHDMI-0/workspace0" # Adjust if needed
        cmd_check = ['xfconf-query', '-c', XFCONF_CHANNEL, '-p', f"{default_path}/last-image"]
        code, _, _ = run_command(cmd_check, capture=False)
        if code == 0:
             log.warning(f"Could not detect specific paths, using default: {default_path}")
             return [default_path]
        else:
             log.error("Could not find ANY background property paths.")
             return []


    def get_gtk_theme(self) -> Optional[str]:
        """Gets the current GTK theme name."""
        cmd = ['xfconf-query', '-c', XFCONF_THEME_CHANNEL, '-p', XFCONF_THEME_PROPERTY]
        code, stdout, stderr = run_command(cmd)
        if code != 0:
            log.error(f"Failed to query GTK theme: {stderr}")
            return None
        return stdout

    def set_gtk_theme(self, theme_name: str) -> bool:
        """Sets the GTK theme."""
        log.info(f"Setting GTK theme to: {theme_name}")
        cmd = ['xfconf-query', '-c', XFCONF_THEME_CHANNEL, '-p', XFCONF_THEME_PROPERTY, '-s', theme_name]
        code, _, stderr = run_command(cmd)
        if code != 0:
            log.error(f"Failed to set GTK theme: {stderr}")
            return False
        return True

    def get_background_settings(self) -> Optional[Dict[str, Any]]:
        """Gets background settings (style, colors) from the first found path."""
        paths = self.find_desktop_paths()
        if not paths:
            log.error("Cannot get background settings: no paths found.")
            return None

        base_path = paths[0] # Use the first path for consistency when getting
        log.debug(f"Getting background settings from primary path: {base_path}")

        settings = {'path': base_path, 'hex1': None, 'hex2': None, 'dir': None}
        props_to_get = {
            "image-style": None,
            "color-style": None,
            "rgba1": None, # Will store raw output
            "rgba2": None, # Will store raw output
        }
        prop_values: Dict[str, Optional[str]] = {} # Store processed values

        # Query essential style properties first
        essential_props_ok = True
        for prop in ["image-style", "color-style"]:
             prop_path = f"{base_path}/{prop}"
             code_check, _, _ = run_command(['xfconf-query', '-c', XFCONF_CHANNEL, '-p', prop_path], capture=False)
             if code_check != 0:
                 log.warning(f"Property {prop_path} does not exist or is not readable.")
                 prop_values[prop] = None
                 essential_props_ok = False
                 continue
             code, stdout, stderr = run_command(['xfconf-query', '-c', XFCONF_CHANNEL, '-p', prop_path])
             if code == 0:
                 prop_values[prop] = stdout.strip()
             else:
                 log.warning(f"Could not query background property '{prop}' from {base_path}: {stderr} (code: {code})")
                 prop_values[prop] = None
                 essential_props_ok = False

        # Check if background is set to 'Color' before proceeding
        image_style = prop_values.get("image-style")
        if image_style != '1':
             log.info(f"Background image-style is not 'Color' (value: {image_style}). Cannot get color settings.")
             return None # Not in color mode
        if not essential_props_ok or prop_values.get("color-style") is None:
             log.error(f"Could not retrieve essential color-style property from {base_path}.")
             return None

        color_style = prop_values.get("color-style")

        # --- NEW RGBA Parsing Logic ---
        def parse_rgba_output(prop_name: str) -> Optional[List[float]]:
            """Parses multi-line xfconf-query output for rgba arrays."""
            prop_path = f"{base_path}/{prop_name}"
            code, stdout, stderr = run_command(['xfconf-query', '-c', XFCONF_CHANNEL, '-p', prop_path])
            if code != 0:
                log.warning(f"Could not query {prop_name} from {base_path}: {stderr} (code: {code})")
                return None
            
            # Find numeric values (floats or ints) in the output lines
            float_values = []
            for line in stdout.splitlines():
                 try:
                     # Match lines containing only a float/int number
                     match = re.fullmatch(r'\s*(-?\d+(\.\d+)?)\s*', line)
                     if match:
                         float_values.append(float(match.group(1)))
                 except ValueError:
                     continue # Ignore lines that are not pure numbers
            
            if len(float_values) == 4:
                 log.debug(f"Parsed {prop_name} values: {float_values}")
                 return float_values # Should be [r, g, b, a] as floats 0.0-1.0
            else:
                 log.warning(f"Could not parse 4 float values from {prop_name} output ({len(float_values)} found). Output:\n{stdout}")
                 return None

        def floats_to_hex(rgba_floats: Optional[List[float]]) -> Optional[str]:
             """Converts list of [r,g,b,a] floats (0.0-1.0) to Hex."""
             if not rgba_floats or len(rgba_floats) != 4:
                 return None
             try:
                 r = int(rgba_floats[0] * 255 + 0.5)
                 g = int(rgba_floats[1] * 255 + 0.5)
                 b = int(rgba_floats[2] * 255 + 0.5)
                 r = max(0, min(255, r))
                 g = max(0, min(255, g))
                 b = max(0, min(255, b))
                 return f"{r:02X}{g:02X}{b:02X}"
             except (ValueError, TypeError, IndexError):
                 log.error(f"Error converting float list {rgba_floats} to hex.")
                 return None
        # --- End NEW RGBA Parsing Logic ---

        # Get RGBA values using the new parser
        rgba1_floats = parse_rgba_output("rgba1")
        settings['hex1'] = floats_to_hex(rgba1_floats)

        if not settings['hex1']:
            log.error("Failed to parse or convert primary color (rgba1).")
            return None # Primary color is essential

        rgba2_floats = None
        # Determine direction and get second color if needed
        if color_style == '0': # Solid
            settings['dir'] = 's'
        elif color_style in ('1', '2'): # Horizontal or Vertical gradient
            settings['dir'] = 'h' if color_style == '1' else 'v'
            rgba2_floats = parse_rgba_output("rgba2")
            settings['hex2'] = floats_to_hex(rgba2_floats)
            if not settings['hex2'] and rgba2_floats is not None: # Log if parsing worked but hex conversion failed
                log.warning("Failed to convert secondary gradient color (rgba2) to hex.")
            elif rgba2_floats is None: # Log if parsing failed
                 log.warning("Could not parse secondary gradient color (rgba2).")
        else:
            log.warning(f"Unknown color-style: {color_style}")
            return None

        # Ensure hex2 is None if not applicable
        if settings['dir'] == 's':
            settings['hex2'] = None

        log.info(f"Retrieved background: Dir={settings['dir']}, Hex1={settings['hex1']}, Hex2={settings.get('hex2', 'N/A')}")
        return settings

    def set_background(self, hex1: str, hex2: Optional[str], direction: str) -> bool:
        """Sets the background to solid or gradient color across all detected paths."""
        log.info(f"Setting background: Dir={direction}, Hex1={hex1}, Hex2={hex2}")
        paths = self.find_desktop_paths()
        if not paths:
            log.error("Cannot set background: no paths found.")
            return False

        rgba1_list = hex_to_rgba_doubles(hex1)
        if not rgba1_list: return False

        rgba2_list = None
        if direction in ('h', 'v'):
            if not hex2:
                log.error("Gradient direction specified but Hex2 is missing.")
                return False
            rgba2_list = hex_to_rgba_doubles(hex2)
            if not rgba2_list: return False

        if direction == 's': color_style = '0'
        elif direction == 'h': color_style = '1'
        elif direction == 'v': color_style = '2'
        else:
            log.error(f"Invalid background direction: {direction}")
            return False
        image_style = '1' # Color mode

        overall_success = True
        for base_path in paths:
            log.debug(f"Applying background settings to path: {base_path}")
            path_success = True

            # Explicitly set types using --create (-n) flag which handles type creation/update
            style_cmds = [
                ['xfconf-query', '-c', XFCONF_CHANNEL, '-p', f"{base_path}/image-style", '-n', '-t', 'int', '-s', image_style],
                ['xfconf-query', '-c', XFCONF_CHANNEL, '-p', f"{base_path}/color-style", '-n', '-t', 'int', '-s', color_style],
            ]
            # Command for rgba1
            rgba1_cmd = ['xfconf-query', '-c', XFCONF_CHANNEL, '-p', f"{base_path}/rgba1", '-n']
            for val in rgba1_list: rgba1_cmd.extend(['-t', 'double', '-s', f"{val:.6f}"])

            # Command for rgba2 or reset
            if rgba2_list:
                rgba2_cmd = ['xfconf-query', '-c', XFCONF_CHANNEL, '-p', f"{base_path}/rgba2", '-n']
                for val in rgba2_list: rgba2_cmd.extend(['-t', 'double', '-s', f"{val:.6f}"])
            else:
                rgba2_cmd = ['xfconf-query', '-c', XFCONF_CHANNEL, '-p', f"{base_path}/rgba2", '-r'] # Reset if solid

            # Command to reset last-image
            reset_img_cmd = ['xfconf-query', '-c', XFCONF_CHANNEL, '-p', f"{base_path}/last-image", '-n', '-t', 'string', '-s', '']

            # Execute commands
            all_cmds = style_cmds + [rgba1_cmd, rgba2_cmd, reset_img_cmd]
            for cmd in all_cmds:
                # Run with check=False and log manually for better warnings
                code, _, stderr = run_command(cmd, check=False)
                if code != 0:
                    # Don't treat reset failures as critical errors
                    is_reset_cmd = '-r' in cmd or '/last-image' in cmd[4]
                    log_level = logging.WARNING if is_reset_cmd else logging.ERROR
                    log.log(log_level, f"Failed command for {base_path}: {' '.join(cmd)} - {stderr}")
                    if not is_reset_cmd: # Only mark failure for non-reset commands
                         path_success = False


            if not path_success:
                overall_success = False
                # Don't bail early, attempt to set for all paths

        # Reload desktop once after trying all paths
        self.reload_xfdesktop()

        if overall_success: log.info("Background settings applied successfully.")
        else: log.warning("Background settings failed for one or more properties/paths.")
        return overall_success


    def get_screen_settings(self) -> Optional[Dict[str, Any]]:
        """Gets screen temperature and brightness via xsct."""
        code, stdout, stderr = run_command(['xsct'])
        if code != 0:
            # ... (existing error handling for non-zero exit code remains the same) ...
            if "Unknown" in stderr or "usage" in stderr.lower() or "failed" in stderr.lower():
                 log.info("xsct appears off or failed to query. Assuming default screen settings.")
                 return {'temperature': None, 'brightness': None} # Indicate off/default
            else:
                 log.error(f"xsct command failed unexpectedly (code {code}): {stderr}")
                 return None # Indicate actual error

        # --- ADJUSTED REGEX ---
        # Match pattern like: Screen #: temperature ~ <temp_digits> <brightness_float>
        # Capture temp digits and brightness float
        match = re.search(r'temperature\s+~\s+(\d+)\s+([\d.]+)', stdout)

        if match:
            try:
                temp = int(match.group(1))
                brightness = float(match.group(2))
                log.info(f"Retrieved screen settings: Temp={temp}, Brightness={brightness:.6f}") # Log with more precision if needed
                return {'temperature': temp, 'brightness': brightness}
            except (ValueError, IndexError):
                 log.error(f"Could not parse values from xsct output match: {stdout}")
                 return None # Parsing failed despite regex match
        else:
             # If output exists but doesn't match, assume off/default
             log.info(f"Could not parse temp/brightness pattern from xsct output: '{stdout}'. Assuming default.")
             return {'temperature': None, 'brightness': None}

        # Example output: "temperature ~ 6500K, brightness ~ 1.0"
        temp_match = re.search(r'temperature\s+~\s+(\d+)K', stdout)
        bright_match = re.search(r'brightness\s+~\s+([\d.]+)', stdout)
        if temp_match and bright_match:
            try:
                temp = int(temp_match.group(1))
                brightness = float(bright_match.group(1))
                log.info(f"Retrieved screen settings: Temp={temp}, Brightness={brightness}")
                return {'temperature': temp, 'brightness': brightness}
            except ValueError:
                 log.error(f"Could not parse xsct output: {stdout}")
                 return None
        else:
             # If output exists but doesn't match, assume off/default
             log.info("Could not parse temp/brightness from xsct output. Assuming default.")
             return {'temperature': None, 'brightness': None}


    def set_screen_temp(self, temp: Optional[int], brightness: Optional[float]) -> bool:
        """Sets screen temperature/brightness using xsct."""
        if temp is not None and brightness is not None:
            # Validate ranges roughly
            if not (1000 <= temp <= 10000): log.warning(f"Unusual temperature value: {temp}K")
            if not (0.1 <= brightness <= 2.0): log.warning(f"Unusual brightness value: {brightness}")

            log.info(f"Setting screen: Temp={temp}, Brightness={brightness:.2f}")
            cmd = ['xsct', str(temp), f"{brightness:.2f}"]
        else:
            log.info("Resetting screen temperature/brightness (xsct -x)")
            cmd = ['xsct', '-x']

        code, _, stderr = run_command(cmd)
        if code != 0:
            log.error(f"Failed to set screen temperature/brightness: {stderr}")
            return False
        return True

    def reload_xfdesktop(self):
        """Reloads the xfdesktop process."""
        log.debug("Reloading xfdesktop...")
        try:
             # Run in background, ignore output/errors
             subprocess.Popen(['xfdesktop', '--reload'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             time.sleep(0.5) # Brief pause
        except FileNotFoundError:
             log.warning("xfdesktop command not found, skipping reload.")
        except Exception as e:
             log.warning(f"Exception trying to reload xfdesktop: {e}")


# --- atd Scheduler Interaction ---

class AtdScheduler:
    """Handles scheduling and clearing of theme transition jobs via 'at'. """

    def __init__(self, python_exe: str, script_path: str):
        self.python_exe = python_exe
        self.script_path = str(pathlib.Path(script_path).resolve()) # Ensure absolute path

    def _get_pending_jobs(self) -> List[Tuple[str, str, str]]:
        """Gets list of pending 'at' jobs, filtering for fluxfce. Returns [(job_id, time_str, command_snippet)]."""
        code, stdout, stderr = run_command(['atq'])
        jobs = []
        if code != 0:
            # Not necessarily an error, queue might be empty
            if "queue is empty" in stderr.lower():
                 log.debug("atq: Queue is empty.")
            else:
                 log.warning(f"atq command failed (code {code}): {stderr}")
            return jobs

        # Parse 'atq' output (format varies, often: JobID Date Time Queue User)
        # We mainly need the Job ID. We look for our marker in the job's command later.
        job_id_pattern = re.compile(r'^(\d+)\s+.*') # Simple pattern to grab leading job ID
        for line in stdout.splitlines():
            match = job_id_pattern.match(line.strip())
            if match:
                job_id = match.group(1)
                # Check if this job contains our command
                code_show, stdout_show, _ = run_command(['at', '-c', job_id])
                if code_show == 0 and AT_JOB_TAG in stdout_show:
                     # Try to extract time for logging/status purposes (best effort)
                     time_match = re.search(r'^\d+\s+([\w\s\d:-]+)\s+[a-z]\s+\w+', line.strip())
                     time_str = time_match.group(1) if time_match else "Unknown Time"
                     # Extract command snippet for logging
                     cmd_match = re.search(r'internal-apply --mode (\w+)', stdout_show)
                     cmd_snippet = cmd_match.group(0) if cmd_match else "internal-apply ..."
                     jobs.append((job_id, time_str, cmd_snippet))
                     log.debug(f"Found relevant pending job: ID={job_id}, Time={time_str}, Cmd={cmd_snippet}")

        return jobs

    def clear_scheduled_transitions(self):
        """Removes all pending 'at' jobs created by this script."""
        log.info("Clearing previously scheduled fluxfce transitions...")
        jobs_to_clear = self._get_pending_jobs()
        if not jobs_to_clear:
            log.info("No relevant 'at' jobs found to clear.")
            return

        cleared_count = 0
        for job_id, time_str, cmd_snippet in jobs_to_clear:
            code, _, stderr = run_command(['atrm', job_id])
            if code == 0:
                log.info(f"Removed scheduled job {job_id} ({cmd_snippet} at {time_str})")
                cleared_count += 1
            else:
                log.warning(f"Failed to remove 'at' job {job_id}: {stderr}")
        log.info(f"Finished clearing jobs ({cleared_count} removed).")

    # Inside AtdScheduler.schedule_transitions:
    def schedule_transitions(self, lat: float, lon: float, tz_name: str) -> bool:
        log.info(f"Calculating and scheduling transitions for {lat}, {lon} (TZ: {tz_name})...")
        self.clear_scheduled_transitions()

        today = date.today()
        tomorrow = today + timedelta(days=1)
        try:
            tz_info = ZoneInfo(tz_name)
            now_local = datetime.now(tz_info)
        except ZoneInfoNotFoundError:
             log.error(f"Invalid Timezone '{tz_name}' during scheduling.")
             return False

        # 1. Collect all potential future events in the next ~48h
        potential_events: Dict[datetime, str] = {}
        for target_date in [today, tomorrow]:
            sun_times = get_sun_times(lat, lon, target_date, tz_name)
            if sun_times:
                if sun_times['sunrise'] > now_local:
                    potential_events[sun_times['sunrise']] = 'day'
                if sun_times['sunset'] > now_local:
                    potential_events[sun_times['sunset']] = 'night'
            else:
                log.warning(f"Could not calculate sun times for {target_date}. Skipping scheduling for this date.")

        if not potential_events:
            log.warning("No future sunrise/sunset events found to schedule in the next ~48 hours.")
            return False

        # 2. Determine the *very next* sunrise and sunset from the potential events
        next_sunrise_event: Optional[datetime] = None
        next_sunset_event: Optional[datetime] = None
        for event_time, mode in sorted(potential_events.items()):
            if mode == 'day' and next_sunrise_event is None:
                next_sunrise_event = event_time
            if mode == 'night' and next_sunset_event is None:
                next_sunset_event = event_time
            if next_sunrise_event and next_sunset_event:
                break # Found the first of each

        # 3. Create the final dictionary of jobs to schedule (at most one sunrise, one sunset)
        final_events_to_schedule: Dict[datetime, str] = {}
        if next_sunrise_event:
            final_events_to_schedule[next_sunrise_event] = 'day'
        if next_sunset_event:
            final_events_to_schedule[next_sunset_event] = 'night'

        # 4. Check if we actually have anything to schedule
        if not final_events_to_schedule:
             # This case should be rare if potential_events was not empty, but handles edge cases
             log.warning("Logical error or no suitable future events found after filtering. Cannot schedule.")
             return False

        # 5. Proceed with scheduling the selected events
        log.debug(f"Final events selected for scheduling: {final_events_to_schedule}")
        journal_tag = SCHEDULER_SERVICE_NAME
        scheduled_count = 0

        for event_time, mode in sorted(final_events_to_schedule.items()):
            at_time_str = event_time.strftime('%H:%M %Y-%m-%d')
            import shlex
            safe_python_exe = shlex.quote(self.python_exe)
            safe_script_path = shlex.quote(self.script_path)

            systemd_cat_command_list = [
                'systemd-cat',
                '-t', journal_tag,
                '--level-prefix=false',
                safe_python_exe,
                safe_script_path,
                'internal-apply',
                '--mode', mode
            ]
            command_to_pipe_to_at = ' '.join(systemd_cat_command_list) + f" {AT_JOB_TAG}"

            code, stdout, stderr = run_command(['at', at_time_str], input_str=command_to_pipe_to_at)

            if code == 0:
                log.info(f"Scheduled '{mode}' transition for {event_time.isoformat()} via 'at' (logging via systemd-cat).")
                if stderr: log.debug(f"'at' command output: {stderr}")
                scheduled_count += 1
            else:
                log.error(f"Failed to schedule '{mode}' transition for {at_time_str} using systemd-cat wrapper: {stderr}")
                log.debug(f"Failed command passed to 'at': {command_to_pipe_to_at}")

        log.info(f"Scheduling complete ({scheduled_count} jobs scheduled).")
        return scheduled_count > 0

    def list_scheduled_transitions(self) -> List[str]:
        """Returns a list of strings describing pending fluxfce jobs."""
        jobs = self._get_pending_jobs()
        return [f"Job {job_id}: {cmd_snippet} at {time_str}" for job_id, time_str, cmd_snippet in jobs]

# --- Systemd Manager ---
class SystemdManager:
    """Handles creation, installation, and removal of systemd user units for atd scheduling."""

    # Service to run the daily scheduler
    SCHEDULER_SERVICE_TEMPLATE = """\
[Unit]
Description={app_name} - Daily Job Scheduler
After=timers.target

[Service]
Type=oneshot
ExecStart={python_executable} "{script_path}" schedule-jobs
StandardError=journal

[Install]
WantedBy=default.target
"""

    # Timer to trigger the daily scheduler
    SCHEDULER_TIMER_TEMPLATE = """\
[Unit]
Description={app_name} - Trigger daily calculation of sunrise/sunset jobs
Requires={scheduler_service_name}

[Timer]
Unit={scheduler_service_name}
OnCalendar=daily
AccuracySec=1h
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
"""

    # Service to run theme check on login
    LOGIN_SERVICE_TEMPLATE = """\
[Unit]
Description={app_name} - Apply theme on login
After=graphical-session.target
Requires=graphical-session.target

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 20
ExecStart={python_executable} "{script_path}" run-login-check
StandardError=journal

[Install]
WantedBy=graphical-session.target
"""

    def _run_systemctl(self, args: List[str], check_errors: bool = True) -> bool:
        """Runs a systemctl --user command."""
        code, _, stderr = run_command(['systemctl', '--user'] + args)
        success = (code == 0)
        if not success and check_errors:
            log.error(f"systemctl --user {' '.join(args)} failed (code {code}): {stderr}")
        return success

    def check_user_instance(self) -> bool:
        """Checks if the systemd user instance appears active enough."""
        code, stdout, stderr = run_command(['systemctl', '--user', 'is-system-running'])
        if code == 0:
            log.info(f"Systemd user instance status: {stdout or 'running'}")
            return True
        elif code == 1:
            log.warning(f"Systemd user instance status: {stdout or 'degraded'} (code 1). Proceeding cautiously.")
            return True # Accept 'degraded'
        else:
            log.error(f"Systemd user instance is not running or degraded (code: {code}): {stdout} {stderr}")
            log.error("Systemd setup cannot proceed.")
            return False


    def install_units(self, script_path: str) -> bool:
        """Creates and enables the systemd user units for scheduler and login."""
        if not self.check_user_instance(): return False

        python_executable = sys.executable
        script_abs_path = str(pathlib.Path(script_path).resolve())

        units = {
            SCHEDULER_SERVICE_FILE: self.SCHEDULER_SERVICE_TEMPLATE.format(
                app_name=APP_NAME,
                python_executable=python_executable,
                script_path=script_abs_path
            ),
            SCHEDULER_TIMER_FILE: self.SCHEDULER_TIMER_TEMPLATE.format(
                app_name=APP_NAME,
                scheduler_service_name=SCHEDULER_SERVICE_NAME,
            ),
            LOGIN_SERVICE_FILE: self.LOGIN_SERVICE_TEMPLATE.format(
                app_name=APP_NAME,
                python_executable=python_executable,
                script_path=script_abs_path
            ),
        }

        try:
            SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
            for file_path, content in units.items():
                file_path.write_text(content, encoding='utf-8')
                log.info(f"Created systemd unit file: {file_path}")
        except IOError as e:
            log.error(f"Failed to write systemd unit files: {e}")
            return False

        if not self._run_systemctl(['daemon-reload']): return False
        # Enable scheduler timer and login service
        if not self._run_systemctl(['enable', '--now', SCHEDULER_TIMER_NAME]): return False
        if not self._run_systemctl(['enable', LOGIN_SERVICE_NAME]): return False # Enable only, runs on session target

        log.info("Systemd units installed and enabled successfully.")
        log.info(f"- Daily Scheduler Status: systemctl --user status {SCHEDULER_TIMER_NAME}")
        log.info(f"- Login Service Status: systemctl --user status {LOGIN_SERVICE_NAME}")
        return True


    def remove_units(self) -> bool:
        """Stops, disables, and removes the systemd user units."""
        log.info("Removing systemd units...")
        units_to_manage = [SCHEDULER_TIMER_NAME, SCHEDULER_SERVICE_NAME, LOGIN_SERVICE_NAME]
        files_to_remove = [SCHEDULER_TIMER_FILE, SCHEDULER_SERVICE_FILE, LOGIN_SERVICE_FILE]
        units_exist = any(f.exists() for f in files_to_remove)

        # Stop and disable units first
        self._run_systemctl(['disable', '--now', SCHEDULER_TIMER_NAME], check_errors=False)
        self._run_systemctl(['disable', LOGIN_SERVICE_NAME], check_errors=False) # Disable only

        removed_files = False
        for f in files_to_remove:
            if f.exists():
                try:
                    f.unlink()
                    log.info(f"Removed {f}")
                    removed_files = True
                except IOError as e:
                    log.warning(f"Failed to remove {f}: {e}")

        if removed_files or units_exist: # Reload if we removed something or think we should have
            if not self._run_systemctl(['daemon-reload']):
                 log.warning("Daemon-reload failed. Manual reload might be needed.")
            # Reset failed state just in case
            self._run_systemctl(['reset-failed'] + units_to_manage, check_errors=False)

        log.info("Systemd unit removal process finished.")
        return True # Report success even if some steps had warnings


# --- Command Handlers ---

def apply_settings(
    xfce_handler: XfceHandler,
    theme: Optional[str],
    bg_hex1: Optional[str],
    bg_hex2: Optional[str],
    bg_dir: Optional[str],
    xsct_temp: Optional[int],
    xsct_bright: Optional[float]
    ) -> Tuple[bool, bool, bool]:
    """Applies theme, background, and screen settings. Returns success status for each."""
    theme_ok = True
    bg_ok = True
    screen_ok = True

    if theme:
        theme_ok = xfce_handler.set_gtk_theme(theme)
    else:
        log.error("No theme provided to apply_settings.")
        theme_ok = False # Theme is essential

    if bg_hex1 and bg_dir:
        bg_ok = xfce_handler.set_background(bg_hex1, bg_hex2, bg_dir)
    else:
        log.info("Skipping background setting (missing hex1 or dir).")
        bg_ok = True # Not essential, don't mark as failure

    screen_ok = xfce_handler.set_screen_temp(xsct_temp, xsct_bright)

    return theme_ok, bg_ok, screen_ok


def _get_settings_for_mode(mode: str, config: configparser.ConfigParser) -> Dict[str, Any]:
    """Helper to get theme/bg/screen settings dict for 'day' or 'night'."""
    settings = {'theme': None, 'bg_hex1': None, 'bg_hex2': None, 'bg_dir': None, 'xsct_temp': None, 'xsct_bright': None}
    screen_section = None # Determine section first

    if mode == 'day':
        settings['theme'] = config.get('Themes', 'LIGHT_THEME', fallback=None)
        bg_section = 'BackgroundDay'
        screen_section = 'ScreenDay' # <<< Use ScreenDay section
    elif mode == 'night':
        settings['theme'] = config.get('Themes', 'DARK_THEME', fallback=None)
        bg_section = 'BackgroundNight'
        screen_section = 'ScreenNight' # <<< Use ScreenNight section
    else:
        log.error(f"Invalid mode specified: {mode}")
        return settings # Return empty settings

    # Load Background settings
    settings['bg_hex1'] = config.get(bg_section, 'BG_HEX1', fallback=None)
    settings['bg_hex2'] = config.get(bg_section, 'BG_HEX2', fallback=None)
    settings['bg_dir'] = config.get(bg_section, 'BG_DIR', fallback=None)

    # Load Screen settings from the determined section
    if screen_section:
         try:
             temp_str = config.get(screen_section, 'XSCT_TEMP', fallback=None)
             bright_str = config.get(screen_section, 'XSCT_BRIGHT', fallback=None)

             # Check if settings signify reset (both empty)
             if temp_str == '' and bright_str == '':
                  log.debug(f"Mode '{mode}' screen settings indicate reset (empty values in config).")
                  settings['xsct_temp'] = None # Explicitly set to None for apply_settings
                  settings['xsct_bright'] = None
             elif temp_str is not None and bright_str is not None:
                  # Attempt to parse if not explicitly reset
                  settings['xsct_temp'] = int(temp_str)
                  settings['xsct_bright'] = float(bright_str)
             # else: leave as None if one is missing or not explicitly reset

         except (ValueError, TypeError) as e:
              log.warning(f"Could not parse screen settings from config section [{screen_section}]: {e}. Screen settings will be None.")
              settings['xsct_temp'] = None
              settings['xsct_bright'] = None

    return settings
      
def handle_internal_apply(args: argparse.Namespace, cfg_mgr: ConfigManager, xfce_handler: XfceHandler) -> bool: # Added return type hint
    """(Internal) Applies settings for a given mode, updates state file. Returns True on success."""
    mode = args.mode
    log.info(f"--- Applying Settings for Mode: {mode.upper()} ---")
    config = cfg_mgr.load_config()
    settings = _get_settings_for_mode(mode, config)

    if not settings.get('theme'):
        log.error(f"Theme not configured for mode '{mode}'. Cannot apply.")
        # sys.exit(1) # REMOVED
        return False # Indicate failure

    theme_ok, bg_ok, screen_ok = apply_settings(
        xfce_handler,
        settings['theme'],
        settings['bg_hex1'], settings['bg_hex2'], settings['bg_dir'],
        settings['xsct_temp'], settings['xsct_bright']
    )

    if theme_ok:
        cfg_mgr.write_state(mode) # Update state file only on success
        if not bg_ok: log.warning("Background setting failed during apply.")
        if not screen_ok: log.warning("Screen temperature setting failed during apply.")
        log.info(f"--- Successfully Applied {mode.upper()} Mode ---")
        # sys.exit(0) # REMOVED
        return True # Indicate success
    else:
        log.error(f"Critical failure: Could not set GTK theme for {mode.upper()} mode.")
        log.info(f"--- Failed to Apply {mode.upper()} Mode ---")
        # sys.exit(1) # REMOVED
        return False # Indicate failure

def handle_schedule_jobs(args: argparse.Namespace, cfg_mgr: ConfigManager, atd_scheduler: AtdScheduler) -> bool:
    """(Internal) Calculates and schedules next transitions via 'at'. Returns True on success, False on failure."""
    log.info("--- Running Daily Transition Scheduler ---")
    config = cfg_mgr.load_config()
    lat_str = cfg_mgr.get_setting(config, 'Location', 'LATITUDE')
    lon_str = cfg_mgr.get_setting(config, 'Location', 'LONGITUDE')
    tz_name = cfg_mgr.get_setting(config, 'Location', 'TIMEZONE')

    lat = latlon_str_to_float(lat_str) if lat_str else None
    lon = latlon_str_to_float(lon_str) if lon_str else None

    if lat is None or lon is None or not tz_name:
        log.error("Latitude, Longitude, or Timezone not configured correctly. Cannot schedule jobs.")
        return False # Return False on config error

    # Dependencies checked by caller (handle_enable_disable) or implicitly by atd_scheduler
    # if not check_dependencies(['at', 'atrm']):
    #     return False # Return False if deps missing

    scheduled_ok = atd_scheduler.schedule_transitions(lat, lon, tz_name)
    if not scheduled_ok:
        # schedule_transitions already logs warnings/errors
        log.warning("Scheduler did not schedule any new transitions (or failed).")
        # Treat inability to schedule as a failure for 'enable' command's perspective
        return False

    log.info("--- Daily Transition Scheduler Finished ---")
    return True # Return True on success


def handle_run_login_check(args: argparse.Namespace, cfg_mgr: ConfigManager, xfce_handler: XfceHandler):
    """(Internal) Checks current time and applies appropriate theme on login."""
    log.info("--- Running Login Theme Check ---")
    config = cfg_mgr.load_config()
    lat_str = cfg_mgr.get_setting(config, 'Location', 'LATITUDE')
    lon_str = cfg_mgr.get_setting(config, 'Location', 'LONGITUDE')
    tz_name = cfg_mgr.get_setting(config, 'Location', 'TIMEZONE')

    lat = latlon_str_to_float(lat_str) if lat_str else None
    lon = latlon_str_to_float(lon_str) if lon_str else None

    if lat is None or lon is None or not tz_name:
        log.error("Latitude, Longitude, or Timezone not configured. Cannot determine login state.")
        sys.exit(1)

    # Determine current state based on sun times for today
    today = date.today()
    now_local = datetime.now(ZoneInfo(tz_name))
    sun_times = get_sun_times(lat, lon, today, tz_name)

    current_mode = 'night' # Default assumption
    if sun_times:
        if sun_times['sunrise'] <= now_local < sun_times['sunset']:
            current_mode = 'day'
        else:
            current_mode = 'night'
        log.info(f"Login check: Current time {now_local.isoformat()} is during '{current_mode}'.")
    else:
        log.warning(f"Could not calculate sun times for {today}. Assuming 'night' for login check.")

    # Apply the determined mode (reuse internal apply handler)
    apply_args = argparse.Namespace(mode=current_mode)
    success = handle_internal_apply(apply_args, cfg_mgr, xfce_handler) # Get return value

    log.info("--- Login Theme Check Finished ---")
    sys.exit(0 if success else 1) # Exit with appropriate code


def handle_install(args: argparse.Namespace, cfg_mgr: ConfigManager, sysd_mgr: SystemdManager, atd_scheduler: AtdScheduler):
    print("--- fluxfce Installation ---")
    # ... (dependency checks) ...

    config = cfg_mgr.load_config()
    config_changed = False # Flag to track if we need to save

    # --- Timezone Validation Loop ---
    while True:
        current_tz = cfg_mgr.get_setting(config, 'Location', 'TIMEZONE')
        if current_tz and current_tz != DEFAULT_CONFIG['Location']['TIMEZONE']:
            print(f"\nCurrent Timezone detected: {current_tz}")
            change_tz = input("Change timezone? [y/N]: ").strip().lower()
            if change_tz != 'y':
                tz_name = current_tz
                break # Keep current valid timezone

        # Prompt if missing, default, or user wants to change
        try:
            local_tz_guess = str(datetime.now().astimezone().tzinfo)
            if ZoneInfo(local_tz_guess): default_prompt_tz = local_tz_guess
            else: default_prompt_tz = DEFAULT_CONFIG['Location']['TIMEZONE']
        except Exception: default_prompt_tz = DEFAULT_CONFIG['Location']['TIMEZONE']

        print(f"\nEnter IANA Timezone Name (e.g., America/New_York, Europe/London).")
        tz_name_input = input(f"Timezone [{default_prompt_tz}]: ").strip() or default_prompt_tz

        try:
            ZoneInfo(tz_name_input) # Validate
            tz_name = tz_name_input
            cfg_mgr.set_setting(config, 'Location', 'TIMEZONE', tz_name)
            config_changed = True
            break # Valid timezone entered
        except ZoneInfoNotFoundError:
             print(f"\nERROR: Invalid timezone '{tz_name_input}'. Please use a valid IANA name.", file=sys.stderr)
             print("See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones", file=sys.stderr)
        except Exception as e:
             print(f"\nERROR: Unexpected error validating timezone '{tz_name_input}': {e}", file=sys.stderr)
        # Loop continues if validation failed

    # --- Lat/Lon and Theme Validation (only if config doesn't exist) ---
    config_exists = CONFIG_FILE.exists()
    if not config_exists:
        print(f"\nConfiguration file not found at {CONFIG_FILE}. Creating...")
        # --- Latitude Loop ---
        while True:
            lat_str_input = input(f"Enter Latitude (e.g., {DEFAULT_CONFIG['Location']['LATITUDE']}): ").strip() or DEFAULT_CONFIG['Location']['LATITUDE']
            if latlon_str_to_float(lat_str_input) is not None:
                lat_str = lat_str_input
                cfg_mgr.set_setting(config, 'Location', 'LATITUDE', lat_str)
                config_changed = True
                break
            else:
                print("Invalid Latitude format or range. Please try again.", file=sys.stderr)
        # --- Longitude Loop ---
        while True:
            lon_str_input = input(f"Enter Longitude (e.g., {DEFAULT_CONFIG['Location']['LONGITUDE']}): ").strip() or DEFAULT_CONFIG['Location']['LONGITUDE']
            if latlon_str_to_float(lon_str_input) is not None:
                lon_str = lon_str_input
                cfg_mgr.set_setting(config, 'Location', 'LONGITUDE', lon_str)
                config_changed = True
                break
            else:
                print("Invalid Longitude format or range. Please try again.", file=sys.stderr)
        # --- Theme Loop (Basic Non-Empty Check) ---
        while True:
             light_theme_input = input(f"Enter Light Theme name [{DEFAULT_CONFIG['Themes']['LIGHT_THEME']}]: ").strip() or DEFAULT_CONFIG['Themes']['LIGHT_THEME']
             if light_theme_input:
                 light_theme = light_theme_input
                 cfg_mgr.set_setting(config, 'Themes', 'LIGHT_THEME', light_theme)
                 config_changed = True
                 break
             else:
                 print("Theme name cannot be empty.", file=sys.stderr)
        while True:
             dark_theme_input = input(f"Enter Dark Theme name [{DEFAULT_CONFIG['Themes']['DARK_THEME']}]: ").strip() or DEFAULT_CONFIG['Themes']['DARK_THEME']
             if dark_theme_input:
                 dark_theme = dark_theme_input
                 cfg_mgr.set_setting(config, 'Themes', 'DARK_THEME', dark_theme)
                 config_changed = True
                 break
             else:
                 print("Theme name cannot be empty.", file=sys.stderr)

    else:
        print(f"\nConfiguration file found at {CONFIG_FILE}.")

    # Save config if any changes were made (new file or timezone update)
    if config_changed:
        cfg_mgr.save_config(config)
        # Create empty presets file if it doesn't exist (important after first config save)
        if not PRESETS_FILE.exists():
             cfg_mgr.save_presets(cfg_mgr.load_presets())

    # --- Systemd and Initial Apply Section ---
    print("\nSetting up systemd user units...")
    script_path = os.path.realpath(sys.argv[0])
    if sysd_mgr.install_units(script_path):
        print("\nSystemd units installed.")
        print("Scheduling initial sunrise/sunset transitions...")
        # Pass args explicitly to handle_schedule_jobs if needed, though it reads from config
        schedule_success = handle_schedule_jobs(args, cfg_mgr, atd_scheduler)
        if not schedule_success:
             print("\nWARNING: Failed to schedule initial jobs. Automatic switching might not work until the next daily schedule run.", file=sys.stderr)
             # Decide if this should be a fatal error for install? Maybe not.

        # --- Start: Determine and Apply Initial Theme ---
        print("\nApplying initial theme based on current time and configured location...")
        config = cfg_mgr.load_config() # Reload potentially changed config
        lat_str = cfg_mgr.get_setting(config, 'Location', 'LATITUDE')
        lon_str = cfg_mgr.get_setting(config, 'Location', 'LONGITUDE')
        tz_name = cfg_mgr.get_setting(config, 'Location', 'TIMEZONE')
        lat = latlon_str_to_float(lat_str) if lat_str else None
        lon = latlon_str_to_float(lon_str) if lon_str else None

        initial_mode = "night" # Default assumption

        if lat is not None and lon is not None and tz_name:
            try:
                tz_info = ZoneInfo(tz_name)
                now_local = datetime.now(tz_info)
                sun_times = get_sun_times(lat, lon, date.today(), tz_name)
                log.debug(f"Attempting to determine initial mode for {tz_name} at {now_local.isoformat()}")

                if sun_times:
                    log.debug(f"Calculated sun times: Rise={sun_times['sunrise'].isoformat()}, Set={sun_times['sunset'].isoformat()}")
                    if sun_times['sunrise'] <= now_local < sun_times['sunset']:
                        initial_mode = 'day'
                    # else: initial_mode remains 'night'
                    print(f"  Timezone: {tz_name}")
                    print(f"  Current Time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
                    print(f"  Today's Sunrise: {sun_times['sunrise'].strftime('%H:%M:%S')}")
                    print(f"  Today's Sunset:  {sun_times['sunset'].strftime('%H:%M:%S')}")
                else:
                    log.warning(f"Could not calculate sun times for {date.today()}. Defaulting to night.")
                    print(f"  Could not calculate sun times for {tz_name} today. Defaulting to night.")

                print(f"  Determined Initial Mode: {initial_mode.upper()}")

            except ZoneInfoNotFoundError:
                 log.error(f"Invalid timezone '{tz_name}' during initial mode check. Defaulting to night.")
                 print(f"  Error: Invalid timezone '{tz_name}'. Defaulting to night.")
            except Exception as e:
                 log.error(f"Error determining initial mode: {e}. Defaulting to night.")
                 print(f"  Error calculating times. Defaulting to night.")
        else:
            log.warning("Location/Timezone not fully configured. Defaulting initial mode to night.")
            print("  Location/Timezone not fully configured. Defaulting initial mode to night.")

        # Apply the determined mode (runs regardless of whether location was configured)
        print(f"Applying {initial_mode.upper()} mode settings...")
        apply_args = argparse.Namespace(mode=initial_mode)
        xfce_handler = XfceHandler()
        apply_success = handle_internal_apply(apply_args, cfg_mgr, xfce_handler)

        if not apply_success:
            print("\nWarning: Applying the initial theme failed. Check logs ('fluxfce log') for details.", file=sys.stderr)
            # Installation continues even if initial apply fails

        # --- End: Determine and Apply Initial Theme ---


        # --- Start: Installation Complete Messages and Instructions ---
        print("\n--- Installation Complete ---")
        print("FluxFCE core components are installed and automatic scheduling is active.")

        print("\n--- Making 'fluxfce' command available ---")
        script_full_path = pathlib.Path(script_path).resolve()
        user_bin_dir = pathlib.Path.home() / ".local" / "bin"
        system_bin_dir = pathlib.Path("/usr/local/bin")

        print(f"The script is currently located at: {script_full_path}")
        print("To run 'fluxfce' easily from your terminal, you need to:")
        print(" 1. Make the script executable.")
        print(" 2. Place it or a symbolic link to it in a directory listed in your system's $PATH.")

        print(f"\nRecommendation: User Installation (usually no 'sudo' needed)")
        print(f"  1. Ensure '{user_bin_dir}' exists and is in your PATH:")
        print(f"     $ mkdir -p \"{user_bin_dir}\"")
        print(f"     # Check if PATH includes it: echo $PATH")
        print(f"     # If not, add to ~/.bashrc or ~/.zshrc: export PATH=\"{user_bin_dir}:$PATH\"")
        print(f"     # Then reload your shell: source ~/.bashrc (or restart terminal)")
        print(f"  2. Choose ONE method:")
        print(f"     a) Move the script:")
        print(f"        $ mv \"{script_full_path}\" \"{user_bin_dir / 'fluxfce'}\"")
        print(f"        $ chmod +x \"{user_bin_dir / 'fluxfce'}\"")
        print(f"     b) Create a symbolic link (keeps script in current location):")
        print(f"        $ ln -s \"{script_full_path}\" \"{user_bin_dir / 'fluxfce'}\"")
        print(f"        $ chmod +x \"{script_full_path}\" # Make original executable")
        # Link itself doesn't need chmod +x if the target is executable

        print(f"\nAlternative: System-Wide Installation (requires 'sudo')")
        print(f"  (Installs for all users, place in {system_bin_dir})")
        print(f"  1. Choose ONE method:")
        print(f"     a) Move the script:")
        print(f"        $ sudo mv \"{script_full_path}\" \"{system_bin_dir / 'fluxfce'}\"")
        print(f"        $ sudo chmod +x \"{system_bin_dir / 'fluxfce'}\"")
        print(f"     b) Create a symbolic link:")
        print(f"        $ sudo ln -s \"{script_full_path}\" \"{system_bin_dir / 'fluxfce'}\"")
        print(f"        $ sudo chmod +x \"{script_full_path}\" # Make original executable")

        print("\nAfter setup, you should be able to run commands like 'fluxfce status'.")
        # --- End: Installation Complete Messages and Instructions ---

    else: # This corresponds to the 'if sysd_mgr.install_units(script_path):' check
        print("\n--- Installation Failed ---", file=sys.stderr)
        print("Systemd unit setup failed. Please check the log messages above.", file=sys.stderr)
        sys.exit(1)

def handle_uninstall(args: argparse.Namespace, cfg_mgr: ConfigManager, sysd_mgr: SystemdManager, atd_scheduler: AtdScheduler):
    """Handles the 'uninstall' command: remove systemd, clear at jobs, remove config."""
    print("--- fluxfce Uninstallation ---")
    # Clear scheduled 'at' jobs first
    atd_scheduler.clear_scheduled_transitions()
    # Remove systemd units
    sysd_mgr.remove_units()

    confirm = input(f"\nDo you want to remove the configuration directory ({CONFIG_DIR})? [y/N]: ").strip().lower()
    if confirm == 'y':
        try:
            import shutil
            if CONFIG_DIR.exists():
                 shutil.rmtree(CONFIG_DIR)
                 print(f"Removed configuration directory: {CONFIG_DIR}")
            else:
                 print(f"Configuration directory not found: {CONFIG_DIR}")
        except OSError as e:
            print(f"Error removing configuration directory {CONFIG_DIR}: {e}", file=sys.stderr)
    else:
        print("Configuration directory kept.")

    print("\n--- Uninstallation Complete ---")
      
def handle_manual_override_command(args: argparse.Namespace, cfg_mgr: ConfigManager, xfce_handler: XfceHandler, atd_scheduler: AtdScheduler, mode: str):
    """Common logic for commands that manually set state and disable schedule."""
    log.info(f"--- Manually Applying Mode: {mode.upper()} ---")
    apply_args = argparse.Namespace(mode=mode)

    # Call internal apply and check success
    success = handle_internal_apply(apply_args, cfg_mgr, xfce_handler)

    if success:
        # Clear scheduled jobs ONLY after successful manual application
        log.info("Clearing scheduled transitions after manual override.")
        atd_scheduler.clear_scheduled_transitions()
        # --- USER MESSAGE ---
        print(f"\n{mode.capitalize()} mode applied. Automatic scheduling disabled.")
        print("Run 'fluxfce enable' to re-enable automatic transitions.")
        sys.exit(0) # Exit successfully
    else:
        # handle_internal_apply already logged the error
        print(f"\nFailed to apply {mode.capitalize()} mode fully. Automatic scheduling may not have been disabled.", file=sys.stderr)
        # Optional: Should we still clear schedule on partial failure? Maybe not.
        # atd_scheduler.clear_scheduled_transitions()
        sys.exit(1) # Exit with error

def handle_log(args: argparse.Namespace):
    """Handles the 'log' command: Displays recent journal entries."""
    log.info("--- Displaying fluxfce Logs ---")
    if not check_dependencies(['journalctl']):
        sys.exit(1)

    # Base command targeting the relevant units/tags
    # Include the systemd-cat tag used in schedule_transitions
    journal_tag = SCHEDULER_SERVICE_NAME # Or APP_NAME if changed above
    cmd = [
        'journalctl',
        '--user', # Look in the user journal
        '-u', SCHEDULER_SERVICE_NAME, # Logs from schedule-jobs
        '-u', LOGIN_SERVICE_NAME,     # Logs from run-login-check
        '-t', journal_tag,            # Logs from internal-apply via systemd-cat
        '--no-pager' # Direct output
    ]

    # Add optional arguments
    if args.lines:
        cmd.extend(['-n', str(args.lines)])
    else:
        cmd.extend(['-n', '50']) # Default to 50 lines

    if args.follow:
        cmd.append('-f')
    else:
        # Show newest first only if not following
        cmd.append('--reverse')

    print(f"\nRunning: {' '.join(cmd)}\n") # Show the user the command being run

    # Execute journalctl, letting it print directly to terminal
    # Use subprocess.call or run without capture
    try:
        # We don't capture output, just run it. Check=False allows non-zero exit if logs are empty?
        # No, journalctl usually exits 0 even if no matches. We don't need check=True.
        return_code = subprocess.call(cmd)
        if return_code != 0:
             log.warning(f"journalctl command exited with code {return_code}.")
    except KeyboardInterrupt:
         print("\nLog following stopped.")
         sys.exit(0)
    except Exception as e:
         log.error(f"Failed to execute journalctl: {e}")
         sys.exit(1)

def handle_save_preset(args: argparse.Namespace, cfg_mgr: ConfigManager, xfce_handler: XfceHandler):
    """Handles the 'save' command: save current settings to presets.ini."""
    preset_name = args.name
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', preset_name): # Start with letter/num
        log.error(f"Invalid preset name: '{preset_name}'. Use letters, numbers, underscore, hyphen (starting with letter/number).")
        sys.exit(1)

    log.info(f"--- Saving Current Settings as Preset '{preset_name}' ---")
    if not check_dependencies(['xfconf-query', 'xsct']): sys.exit(1)

    theme = xfce_handler.get_gtk_theme()
    bg_settings = xfce_handler.get_background_settings()
    screen_settings = xfce_handler.get_screen_settings()

    if not theme:
        log.error("Could not retrieve current GTK theme. Cannot save preset.")
        sys.exit(1)

    preset_data: Dict[str, Optional[str]] = {'theme': theme} # Ensure type checker knows value can be None

    if bg_settings and bg_settings.get('hex1') and bg_settings.get('dir'):
         preset_data['bg_hex1'] = bg_settings['hex1']
         preset_data['bg_dir'] = bg_settings['dir']
         preset_data['bg_hex2'] = bg_settings.get('hex2') # Can be None
         log.info(f"Read Background: Dir={preset_data['bg_dir']}, Hex1={preset_data['bg_hex1']}, Hex2={preset_data.get('bg_hex2', 'N/A')}")
    else: log.warning("Could not read valid background color settings. Background won't be saved.")

    if screen_settings:
        # Handle case where xsct is off/default (both are None)
        if screen_settings.get('temperature') is None and screen_settings.get('brightness') is None:
             preset_data['xsct_temp'] = '' # Empty string signifies reset
             preset_data['xsct_bright'] = ''
             log.info("Read Screen: Off / Default values (will be saved as reset).")
        elif screen_settings.get('temperature') is not None and screen_settings.get('brightness') is not None:
            preset_data['xsct_temp'] = str(screen_settings['temperature'])
            preset_data['xsct_bright'] = f"{screen_settings['brightness']:.2f}"
            log.info(f"Read Screen: Temp={preset_data['xsct_temp']}, Brightness={preset_data['xsct_bright']}")
        else: # Inconsistent state from xsct?
             log.warning("Inconsistent screen settings read from xsct. Screen settings won't be saved.")
    else: log.warning("Could not read screen settings (xsct query failed?). Screen settings won't be saved.")


    presets = cfg_mgr.load_presets()
    section_name = f"Preset_{preset_name}"
    if presets.has_section(section_name):
         confirm = input(f"Preset '{preset_name}' already exists. Overwrite? [y/N]: ").strip().lower()
         if confirm != 'y':
             print("Preset not saved.")
             sys.exit(0)

    cfg_mgr.save_preset_settings(presets, preset_name, preset_data)
    cfg_mgr.save_presets(presets)
    log.info(f"--- Preset '{preset_name}' Saved Successfully ---")


def handle_apply_preset(args: argparse.Namespace, cfg_mgr: ConfigManager, xfce_handler: XfceHandler, atd_scheduler: AtdScheduler):
    """Handles the 'apply' command: load preset, apply settings, clear schedule."""
    preset_name = args.name
    log.info(f"--- Applying Preset '{preset_name}' ---")
    if not check_dependencies(['xfconf-query', 'xsct']): sys.exit(1)

    presets = cfg_mgr.load_presets()
    preset_data = cfg_mgr.get_preset_settings(presets, preset_name)

    if not preset_data:
        log.error(f"Preset '{preset_name}' not found in {PRESETS_FILE}.")
        sys.exit(1)

    theme = preset_data.get('theme')
    bg_hex1 = preset_data.get('bg_hex1')
    bg_hex2 = preset_data.get('bg_hex2')
    bg_dir = preset_data.get('bg_dir')
    temp_str = preset_data.get('xsct_temp')
    bright_str = preset_data.get('xsct_bright')

    if not theme:
        log.error(f"Preset '{preset_name}' is missing the essential 'theme' setting.")
        sys.exit(1)

    xsct_temp: Optional[int] = None
    xsct_bright: Optional[float] = None
    try:
        if temp_str == '' and bright_str == '':
             log.info("Preset specifies resetting screen settings.")
        elif temp_str and bright_str:
            xsct_temp = int(temp_str)
            xsct_bright = float(bright_str)
        elif temp_str or bright_str:
            log.warning("Preset has inconsistent screen settings (temp/bright). Resetting screen.")
    except (ValueError, TypeError) as e:
        log.warning(f"Could not parse screen settings from preset '{preset_name}': {e}. Resetting screen.")

    # Apply settings
    theme_ok, bg_ok, screen_ok = apply_settings(
        xfce_handler, theme, bg_hex1, bg_hex2, bg_dir, xsct_temp, xsct_bright
    )

    # Clear schedule after applying preset
    log.info("Clearing scheduled transitions after applying preset...")
    atd_scheduler.clear_scheduled_transitions()

    if theme_ok and bg_ok and screen_ok:
        log.info(f"--- Preset '{preset_name}' Applied Successfully ---")
        # --- MODIFIED USER MESSAGE ---
        print(f"\nPreset '{preset_name}' applied. Automatic scheduling disabled.")
        print("Run 'fluxfce enable' to re-enable.")
        sys.exit(0)
    else:
        # ... (existing error handling) ...
        # Optional: Add message here too? Might be confusing if apply failed.
        print(f"\nPreset application failed. Automatic scheduling was also disabled.")
        sys.exit(1)

def handle_set_background(args: argparse.Namespace, xfce_handler: XfceHandler, atd_scheduler: AtdScheduler):
    """Handles the 'set background' command: applies specified background and clears schedule."""
    log.info("--- Setting Background Manually ---")
    if not check_dependencies(['xfconf-query']): # xfdesktop implicitly checked by reload
        sys.exit(1)

    # --- Validate Inputs ---
    direction = args.dir
    hex1 = args.hex1
    hex2 = args.hex2
    hex2_to_pass: Optional[str] = None

    # Validate Hex1 format
    if hex_to_rgba_doubles(hex1) is None:
        log.error(f"Invalid format for --hex1: '{hex1}'. Use 6-digit hex (e.g., FF0000).")
        sys.exit(1)

    # Validate Hex2 and requirement based on direction
    if direction in ['h', 'v']:
        if hex2 is None:
            log.error(f"Missing required argument --hex2 for gradient direction '{direction}'.")
            sys.exit(1)
        if hex_to_rgba_doubles(hex2) is None:
             log.error(f"Invalid format for --hex2: '{hex2}'. Use 6-digit hex.")
             sys.exit(1)
        hex2_to_pass = hex2 # Use the provided hex2
    elif direction == 's':
        if hex2 is not None:
             log.warning(f"Ignoring --hex2='{hex2}' because --dir='s' (solid) was specified.")
        hex2_to_pass = None # Ensure None is passed for solid

    # --- Apply Background ---
    success = xfce_handler.set_background(hex1, hex2_to_pass, direction)

    if success:
        log.info("Clearing scheduled transitions after manual background set.")
        atd_scheduler.clear_scheduled_transitions()
        print("\nBackground set successfully. Automatic scheduling disabled.")
        print("Run 'fluxfce enable' to re-enable automatic transitions.")
        sys.exit(0)
    else:
        log.error("Failed to set background.")
        print("\nFailed to set background. Automatic scheduling *not* disabled.", file=sys.stderr)
        sys.exit(1)

def handle_set_theme(args: argparse.Namespace, xfce_handler: XfceHandler, atd_scheduler: AtdScheduler):
    """Handles the 'set theme' command: applies specified GTK theme and clears schedule."""
    theme_name = args.theme_name
    log.info(f"--- Setting GTK Theme Manually to: {theme_name} ---")

    if not check_dependencies(['xfconf-query']):
        sys.exit(1)

    # Basic validation: Ensure theme name is not empty
    if not theme_name:
         log.error("Theme name cannot be empty.")
         print("\nError: Theme name cannot be empty.", file=sys.stderr)
         sys.exit(1)
    # Note: We don't rigorously validate if the theme *exists* here,
    # as xfconf-query will handle applying it (or failing gracefully).
    # Adding validation would require complex theme path checking.

    # --- Apply Theme ---
    success = xfce_handler.set_gtk_theme(theme_name)

    if success:
        log.info("Clearing scheduled transitions after manual theme set.")
        atd_scheduler.clear_scheduled_transitions()
        print(f"\nTheme set to '{theme_name}'. Automatic scheduling disabled.")
        print("Run 'fluxfce enable' to re-enable automatic transitions.")
        sys.exit(0)
    else:
        log.error(f"Failed to set theme to '{theme_name}'.")
        # Let set_gtk_theme handle specific error logging
        print(f"\nFailed to set theme. Automatic scheduling *not* disabled.", file=sys.stderr)
        sys.exit(1)

def handle_set_default(args: argparse.Namespace, cfg_mgr: ConfigManager, xfce_handler: XfceHandler):
    """Handles the 'set-default' command: saves current settings to config.ini for day/night."""
    mode = args.mode
    log.info(f"--- Saving Current Settings as Default for Mode: {mode.upper()} ---")
    if not check_dependencies(['xfconf-query', 'xsct']): sys.exit(1)

    # 1. Get Current Settings
    theme = xfce_handler.get_gtk_theme()
    bg_settings = xfce_handler.get_background_settings()
    screen_settings = xfce_handler.get_screen_settings()

    if not theme:
        log.error("Could not retrieve current GTK theme. Cannot save defaults.")
        sys.exit(1)

    config = cfg_mgr.load_config()
    config_changed = False

    # 2. Update Theme Setting
    theme_key = 'LIGHT_THEME' if mode == 'day' else 'DARK_THEME'
    current_theme_setting = cfg_mgr.get_setting(config, 'Themes', theme_key)
    if current_theme_setting != theme:
         log.info(f"Updating [{mode.capitalize()} Theme] from '{current_theme_setting}' to '{theme}'")
         cfg_mgr.set_setting(config, 'Themes', theme_key, theme)
         config_changed = True
    else:
         log.info(f"Current theme '{theme}' already matches {mode.capitalize()} Theme setting.")

    # 3. Update Background Settings
    bg_section = 'BackgroundDay' if mode == 'day' else 'BackgroundNight'
    if bg_settings and bg_settings.get('hex1') and bg_settings.get('dir'):
         log.info(f"Read Background: Dir={bg_settings['dir']}, Hex1={bg_settings['hex1']}, Hex2={bg_settings.get('hex2', 'N/A')}")
         # Compare and set each background key
         for key, config_key in [('dir', 'BG_DIR'), ('hex1', 'BG_HEX1'), ('hex2', 'BG_HEX2')]:
             new_value = bg_settings.get(key)
             current_value = cfg_mgr.get_setting(config, bg_section, config_key)
             # Handle None vs empty string comparison if necessary, treat them as same "not set" state for hex2
             new_value_str = str(new_value) if new_value is not None else ''
             current_value_str = str(current_value) if current_value is not None else ''

             if new_value_str != current_value_str:
                  log.info(f"Updating [{bg_section} {config_key}] from '{current_value_str}' to '{new_value_str}'")
                  cfg_mgr.set_setting(config, bg_section, config_key, new_value_str)
                  config_changed = True
             else:
                  log.debug(f"Current {config_key} '{current_value_str}' already matches {bg_section} setting.")
    else:
        log.warning("Could not read valid background settings. Defaults for background not updated.")

    # 4. Update Screen Settings (ONLY for Night mode)
    if mode == 'night':
        screen_section = 'ScreenDay' if mode == 'day' else 'ScreenNight' # Determine target section
        temp_to_save: Optional[str] = None
        bright_to_save: Optional[str] = None

        if screen_settings:
            if screen_settings.get('temperature') is None and screen_settings.get('brightness') is None:
                temp_to_save = '' # Save empty string for reset
                bright_to_save = ''
                log.info(f"Read Screen: Off / Default values. Saving as reset ('') for {mode} default.")
            elif screen_settings.get('temperature') is not None and screen_settings.get('brightness') is not None:
                temp_to_save = str(screen_settings['temperature'])
                bright_to_save = f"{screen_settings['brightness']:.2f}"
                log.info(f"Read Screen: Temp={temp_to_save}, Brightness={bright_to_save}")
            else:
                log.warning(f"Inconsistent screen settings read from xsct. {mode.capitalize()} screen defaults not updated.")
        else:
            log.warning(f"Could not read screen settings (xsct query failed?). {mode.capitalize()} screen defaults not updated.")

        # Compare and set screen settings if successfully read
        if temp_to_save is not None and bright_to_save is not None:
            current_temp = cfg_mgr.get_setting(config, screen_section, 'XSCT_TEMP')
            current_bright = cfg_mgr.get_setting(config, screen_section, 'XSCT_BRIGHT')
            if current_temp != temp_to_save:
                log.info(f"Updating [{screen_section} XSCT_TEMP] from '{current_temp}' to '{temp_to_save}'")
                cfg_mgr.set_setting(config, screen_section, 'XSCT_TEMP', temp_to_save)
                config_changed = True
            if current_bright != bright_to_save:
                log.info(f"Updating [{screen_section} XSCT_BRIGHT] from '{current_bright}' to '{bright_to_save}'")
                cfg_mgr.set_setting(config, screen_section, 'XSCT_BRIGHT', bright_to_save)
                config_changed = True
    else: # Day mode
         log.info("Screen settings are not saved for Day mode (day mode resets xsct).")

    # 5. Save Config if Changed
    if config_changed:
        cfg_mgr.save_config(config)
        print(f"\nCurrent settings saved as default for {mode.upper()} mode in {CONFIG_FILE}.")
        print("Run 'fluxfce enable' to ensure the schedule uses the updated settings for future transitions.")
    else:
        print(f"\nCurrent settings already match the configured defaults for {mode.upper()} mode. No changes made.")

    log.info(f"--- Set Default {mode.upper()} Finished ---")


def handle_list_presets(args: argparse.Namespace, cfg_mgr: ConfigManager):
    """Handles the 'list-presets' command."""
    presets = cfg_mgr.load_presets()
    names = cfg_mgr.list_preset_names(presets)
    if names:
        print("Available presets:")
        for name in sorted(names):
            print(f"  - {name}")
    else:
        print("No presets found.")


def handle_delete_preset(args: argparse.Namespace, cfg_mgr: ConfigManager):
    """Handles the 'delete-preset' command."""
    preset_name = args.name
    presets = cfg_mgr.load_presets()
    if cfg_mgr.delete_preset(presets, preset_name):
        cfg_mgr.save_presets(presets)
        print(f"Preset '{preset_name}' deleted.")
    else:
        print(f"Preset '{preset_name}' not found.")
        sys.exit(1)


def handle_enable_disable(args: argparse.Namespace, cfg_mgr: ConfigManager, atd_scheduler: AtdScheduler, enable: bool):
    """Handles 'enable' and 'disable' commands by scheduling or clearing jobs."""
    action = "Enabling" if enable else "Disabling"
    log.info(f"--- {action} Automatic Scheduling ---")

    if enable:
        if not check_dependencies(['at', 'atrm']): sys.exit(1)
        # Schedule jobs based on current config
        if handle_schedule_jobs(args, cfg_mgr, atd_scheduler):
             print("Automatic theme scheduling enabled.")
             # Maybe run login check immediately?
             # print("Applying current theme...")
             # handle_run_login_check(args, cfg_mgr, XfceHandler())
        else:
             print("Failed to schedule jobs. Automatic scheduling may not be fully enabled.", file=sys.stderr)
             sys.exit(1)
    else:
        if not check_dependencies(['atq', 'atrm']): sys.exit(1) # Need atq for listing in clear
        atd_scheduler.clear_scheduled_transitions()
        print("Automatic theme scheduling disabled ('at' jobs cleared).")


def handle_status(args: argparse.Namespace, cfg_mgr: ConfigManager, atd_scheduler: AtdScheduler):
    """Handles the 'status' command."""
    print("--- fluxfce Status ---")
    config = cfg_mgr.load_config()

    # Configured Settings
    print("\n[Configuration]")
    lat_str = cfg_mgr.get_setting(config, 'Location', 'LATITUDE', 'Not Set')
    lon_str = cfg_mgr.get_setting(config, 'Location', 'LONGITUDE', 'Not Set')
    tz_name = cfg_mgr.get_setting(config, 'Location', 'TIMEZONE', 'Not Set')
    light = cfg_mgr.get_setting(config, 'Themes', 'LIGHT_THEME', 'Not Set')
    dark = cfg_mgr.get_setting(config, 'Themes', 'DARK_THEME', 'Not Set')
    print(f"  Location:      {lat_str}, {lon_str}")
    print(f"  Timezone:      {tz_name}")
    print(f"  Light Theme:   {light}")
    print(f"  Dark Theme:    {dark}")

    # Current State
    print("\n[State]")
    last_state = cfg_mgr.read_state()
    print(f"  Last Auto-Applied: {last_state or 'Unknown'}")

    # Calculated Sun Times (for today)
    print("\n[Calculated Sun Times (Today)]")
    lat = latlon_str_to_float(lat_str)
    lon = latlon_str_to_float(lon_str)
    if lat is not None and lon is not None and tz_name and tz_name != 'Not Set':
        today = date.today()
        sun_times = get_sun_times(lat, lon, today, tz_name)
        if sun_times:
            print(f"  Sunrise:       {sun_times['sunrise'].isoformat()}")
            print(f"  Sunset:        {sun_times['sunset'].isoformat()}")
            # Indicate current period based on calculation
            now_local = datetime.now(ZoneInfo(tz_name))
            if sun_times['sunrise'] <= now_local < sun_times['sunset']:
                 print("  Current Period:  Daytime")
            else:
                 print("  Current Period:  Nighttime")
        else:
            print("  Could not calculate sun times (check config/location/polar?).")
    else:
        print("  Cannot calculate sun times (Latitude/Longitude/Timezone missing or invalid in config).")

    # Scheduled Jobs ('at' queue)
    print("\n[Scheduled Transitions ('at' jobs)]")
    if check_dependencies(['atq']):
        pending_jobs = atd_scheduler.list_scheduled_transitions()
        if pending_jobs:
            for job_desc in pending_jobs:
                print(f"  - {job_desc}")
        else:
            print("  No automatic transitions currently scheduled (Automatic mode likely disabled).")
            print("  Run 'fluxfce enable' to schedule transitions.")
    else: print("  Cannot check schedule ('atq' command not found).")

    # Systemd Status
    print("\n[Systemd Units]")
    if check_dependencies(['systemctl']):
        for unit_name, unit_desc in [
            (SCHEDULER_TIMER_NAME, "Daily Scheduler Timer"),
            (SCHEDULER_SERVICE_NAME, "Daily Scheduler Service (triggered by timer)"), # Clarify description
            (LOGIN_SERVICE_NAME, "Login Service")
        ]:
            # Check if loaded first (distinguishes missing from disabled/inactive)
            # Use 'show' which exits 0 if unit exists, regardless of state
            load_check_cmd = ['systemctl', '--user', 'show', '--property=LoadState', '--value', unit_name]
            load_code, load_state_output, _ = run_command(load_check_cmd, capture=True)

            if load_code != 0 or load_state_output != 'loaded':
                 final_status = "not found / not loaded"
            else:
                 # Unit is loaded, now check enabled/active state
                 is_enabled_code, _, _ = run_command(['systemctl', '--user', 'is-enabled', unit_name], capture=True)
                 # is-enabled: 0=enabled, 1=disabled/static. >1=error
                 enabled_status = "enabled" if is_enabled_code == 0 else ("disabled" if is_enabled_code == 1 else "error checking enabled")

                 is_active_code, _, _ = run_command(['systemctl', '--user', 'is-active', unit_name], capture=True)
                 # is-active: 0=active, non-zero=inactive
                 active_status = "active" if is_active_code == 0 else "inactive"

                 # --- Interpret status based on unit type ---
                 if unit_name.endswith(".timer"):
                     # For timers: Active means waiting. Enabled is key.
                     final_status = f"{enabled_status}, {active_status} (waiting)" if active_status == "active" else f"{enabled_status}, {active_status}"
                 elif unit_name == SCHEDULER_SERVICE_NAME:
                      # For scheduler service: It's triggered by timer. 'is-enabled' is often 'disabled' or 'static'. Focus on 'LoadState' and recent activity.
                      # We already know it's loaded. Show active status.
                      final_status = f"loaded, {active_status} (triggered by timer)"
                 elif unit_name == LOGIN_SERVICE_NAME:
                      # For login service: Enabled is key. Active is usually inactive after run.
                      final_status = f"{enabled_status}, {active_status} (runs on login)"
                 else: # Fallback for unexpected unit types
                      final_status = f"{enabled_status}, {active_status}"

            print(f"  {unit_desc} ({unit_name}): {final_status}")

            # Optional: More detail if verbose and not found/error
            if args.verbose and (load_code != 0 or load_state_output != 'loaded'):
                 code, stdout, stderr = run_command(['systemctl', '--user', 'status', unit_name], capture=True)
                 if stdout: print(f"    Detail: {stdout}")
                 if stderr: print(f"    Detail: {stderr}")

    else: print("  Cannot check systemd status (systemctl not found).")

def handle_config(args: argparse.Namespace, cfg_mgr: ConfigManager):
    """Handles the 'config' command for getting/setting values."""
    config = cfg_mgr.load_config()

    if args.get:
        key_to_get = args.get
        found_val = None
        found_section = None
        for section in config.sections():
            if config.has_option(section, key_to_get):
                found_val = config.get(section, key_to_get)
                found_section = section
                break
        if found_val is not None:
             print(f"[{found_section}] {key_to_get} = {found_val}")
        else:
            print(f"Error: Key '{key_to_get}' not found in configuration.", file=sys.stderr)
            sys.exit(1)

    elif args.set:
        set_arg = args.set
        if '=' not in set_arg:
            print("Error: Use format --set KEY=VALUE", file=sys.stderr)
            sys.exit(1)
        key_to_set, value = set_arg.split('=', 1)
        key_to_set = key_to_set.strip()
        value = value.strip()

        found_section = None
        # Find which default section the key belongs to
        for section, defaults in DEFAULT_CONFIG.items():
             if key_to_set in defaults:
                  found_section = section
                  break

        if not found_section:
            # Fallback: Maybe it's a custom key already present? Less safe. Check existing.
            for section in config.sections():
                  if config.has_option(section, key_to_set):
                        found_section = section
                        log.warning(f"Setting key '{key_to_set}' which is not in default config structure (found in section [{section}]).")
                        break

        if found_section:
             # Special handling/validation for certain keys
             if key_to_set in ["LATITUDE", "LONGITUDE"]:
                 if latlon_str_to_float(value) is None:
                     print(f"Error: Invalid format for {key_to_set}. Use e.g., '43.65N' or '79.38W'.", file=sys.stderr)
                     sys.exit(1)
             elif key_to_set == "TIMEZONE":
                 try: ZoneInfo(value)
                 except Exception:
                      print(f"Error: Invalid IANA Timezone '{value}'.", file=sys.stderr)
                      sys.exit(1)

             print(f"Setting [{found_section}] {key_to_set} = {value}")
             cfg_mgr.set_setting(config, found_section, key_to_set, value)
             cfg_mgr.save_config(config)
             # Inform user if schedule needs update
             if key_to_set in ["LATITUDE", "LONGITUDE", "TIMEZONE"]:
                  print("Location changed. Run 'fluxfce enable' to update the schedule.")
        else:
            print(f"Error: Key '{key_to_set}' not found in default configuration structure.", file=sys.stderr)
            print("Cannot determine which section it belongs to.", file=sys.stderr)
            sys.exit(1)
    else:
         # Default: print current config
         print(f"# Current configuration ({CONFIG_FILE}):")
         config.write(sys.stdout)
         print(f"\n# Presets file: {PRESETS_FILE}")
         print(f"# State file: {STATE_FILE}")


# --- Main Execution ---

def main():
    # Use module docstring as epilog for examples
    parser = argparse.ArgumentParser(
        description="fluxfce: Manage XFCE appearance via atd scheduling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('Usage Examples:')[1] # Extract examples section
    )
    parser.add_argument('-v', '--verbose', action='store_true', help="Enable detailed logging output.")

    subparsers = parser.add_subparsers(dest='command', title='Commands', required=True)

    # --- Define Commands ---
    subparsers.add_parser('install', help='Interactive setup, install systemd units, schedule jobs.')
    subparsers.add_parser('uninstall', help='Clear schedule, remove systemd units & config.')
    subparsers.add_parser('enable', help='Enable automatic scheduling (schedules transitions).')
    subparsers.add_parser('disable', help='Disable automatic scheduling (clears scheduled transitions).')
    subparsers.add_parser('status', help='Show config, calculated times, and scheduled jobs.')

    parser_log = subparsers.add_parser('log', help='Show recent fluxfce logs from the systemd journal.')
    parser_log.add_argument('-n', '--lines', type=int, help='Number of lines to show (default: 50).')
    parser_log.add_argument('-f', '--follow', action='store_true', help='Follow the log output.')

    parser_save = subparsers.add_parser('save', help='Save current desktop settings as a named preset.')
    parser_save.add_argument('name', help='Name for the preset (e.g., "work_mode").')

    parser_apply = subparsers.add_parser('apply', help='Apply preset (disables automatic scheduling).')
    parser_apply.add_argument('name', help='Name of the preset to apply.')

    subparsers.add_parser('list-presets', help='List saved preset names.')
    parser_delete = subparsers.add_parser('delete-preset', help='Delete a saved preset.')
    parser_delete.add_argument('name', help='Name of the preset to delete.')

    subparsers.add_parser('force-day', help='Apply Day Mode settings now (disables automatic scheduling).')
    subparsers.add_parser('force-night', help='Apply Night Mode settings now (disables automatic scheduling).')
    subparsers.add_parser('toggle', help='Apply opposite of last auto-mode (disables automatic scheduling).')

    parser_set_default = subparsers.add_parser('set-default', help='Save current settings as the new default for Day or Night mode.')
    parser_set_default.add_argument('--mode', choices=['day', 'night'], required=True, help='Specify whether to save as the Day or Night default.')

    parser_config = subparsers.add_parser('config', help='View or modify configuration settings.')
    config_group = parser_config.add_mutually_exclusive_group()
    config_group.add_argument('--get', metavar='KEY', help='Get the value of a specific configuration key.')
    config_group.add_argument('--set', metavar='KEY=VALUE', help='Set the value of a specific configuration key.')

    parser_set = subparsers.add_parser('set', help='Immediately set specific desktop components (disables auto schedule).')
    set_subparsers = parser_set.add_subparsers(dest='set_component', title='Components to set', required=True)

    parser_set_bg = set_subparsers.add_parser('background', help='Set background color (solid or gradient).')
    parser_set_bg.add_argument('--dir', choices=['s', 'h', 'v'], required=True,
                               help='Direction: s=solid, h=horizontal gradient, v=vertical gradient.')
    parser_set_bg.add_argument('--hex1', required=True,
                               help='Primary Hex color (e.g., FF0000).')
    parser_set_bg.add_argument('--hex2',
                               help='Secondary Hex color for gradients (e.g., 00FF00). Required if --dir is h or v.')
    
    # --- Add placeholders for future 'set theme', 'set screen' if desired ---
    parser_set_theme = set_subparsers.add_parser('theme', help='Set GTK theme.')
    parser_set_theme.add_argument('theme_name', help='Name of the GTK theme.')
    # parser_set_screen = set_subparsers.add_parser('screen', help='Set screen temperature/brightness.')
    # screen_group = parser_set_screen.add_mutually_exclusive_group(required=True)
    # screen_group.add_argument('--reset', action='store_true', help='Reset screen settings (xsct -x).')
    # screen_group.add_argument('--values', nargs=2, metavar=('TEMP', 'BRIGHT'), help='Set specific temperature (e.g., 3500) and brightness (e.g., 0.8).')

    # --- Internal Commands (not shown in help) ---
    parser_internal = subparsers.add_parser('internal-apply', help=argparse.SUPPRESS)
    parser_internal.add_argument('--mode', choices=['day', 'night'], required=True)

    subparsers.add_parser('schedule-jobs', help=argparse.SUPPRESS)
    subparsers.add_parser('run-login-check', help=argparse.SUPPRESS)

    args = parser.parse_args()

    # --- Setup ---
    setup_logging(args.verbose)
    cfg_mgr = ConfigManager()
    xfce_handler = XfceHandler()
    # Pass script path needed by scheduler for 'at' commands
    atd_scheduler = AtdScheduler(python_exe=sys.executable, script_path=sys.argv[0])
    sysd_mgr = SystemdManager()

    # --- Dispatch Command ---
    try:
        if args.command == 'install':
            handle_install(args, cfg_mgr, sysd_mgr, atd_scheduler)
        elif args.command == 'uninstall':
            handle_uninstall(args, cfg_mgr, sysd_mgr, atd_scheduler)
        elif args.command == 'enable':
            handle_enable_disable(args, cfg_mgr, atd_scheduler, enable=True)
        elif args.command == 'disable':
            handle_enable_disable(args, cfg_mgr, atd_scheduler, enable=False)
        elif args.command == 'status':
            handle_status(args, cfg_mgr, atd_scheduler)
        elif args.command == 'log':
            handle_log(args)
        elif args.command == 'save':
            handle_save_preset(args, cfg_mgr, xfce_handler)
        elif args.command == 'apply':
             handle_apply_preset(args, cfg_mgr, xfce_handler, atd_scheduler)
        elif args.command == 'list-presets':
             handle_list_presets(args, cfg_mgr)
        elif args.command == 'delete-preset':
             handle_delete_preset(args, cfg_mgr)
        elif args.command == 'set-default':
             handle_set_default(args, cfg_mgr, xfce_handler)
        elif args.command == 'force-day':
             handle_manual_override_command(args, cfg_mgr, xfce_handler, atd_scheduler, 'day')
        elif args.command == 'force-night':
             handle_manual_override_command(args, cfg_mgr, xfce_handler, atd_scheduler, 'night')
        elif args.command == 'toggle':
             last_state = cfg_mgr.read_state()
             mode_to_apply = 'day' if last_state == 'night' else 'night'
             handle_manual_override_command(args, cfg_mgr, xfce_handler, atd_scheduler, mode_to_apply)

        elif args.command == 'set': # New top-level command
             if args.set_component == 'background':
                handle_set_background(args, xfce_handler, atd_scheduler)
             elif args.set_component == 'theme':
                 handle_set_theme(args, xfce_handler, atd_scheduler) # Future
            # elif args.set_component == 'screen':
            #     handle_set_screen(args, xfce_handler, atd_scheduler) # Future
            # else:
            #     log.error(f"Unknown component for set command: {args.set_component}")
            #     parser.print_help()
            #     sys.exit(1)        

        elif args.command == 'config':
             handle_config(args, cfg_mgr)
        # --- Internal commands ---
        elif args.command == 'internal-apply':
             handle_internal_apply(args, cfg_mgr, xfce_handler) # Exits internally
        elif args.command == 'schedule-jobs':
             success = handle_schedule_jobs(args, cfg_mgr, atd_scheduler)
             if not success:
                 sys.exit(1) # Exit code 1 if scheduling failed
             # Implicit sys.exit(0) if success
        elif args.command == 'run-login-check':
             handle_run_login_check(args, cfg_mgr, xfce_handler) # Exits internally
        else:
            log.error(f"Unknown command: {args.command}") # Should be caught by argparse
            parser.print_help()
            sys.exit(1)

    except Exception as e:
        # Catch-all for unexpected errors during command execution
        log.exception(f"An unexpected error occurred during command '{args.command}': {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()