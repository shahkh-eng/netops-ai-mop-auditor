"""
guardrails.py
-------------
Zero-trust command boundary and Netmiko transport layer.
- Ensures the AI Agent can ONLY run safe read-only verification commands.
- Isolates engineer change-execution & rollback behind explicit application endpoints.
"""

import re
from netmiko import ConnectHandler
from inventory import PASSWORD, ROUTERS, SECRET, USERNAME

ALLOWED_READ_PREFIXES = ("show ", "ping ", "traceroute ")

# Regex-based blocklist avoids false positives on legitimate commands like "show running-config"
FORBIDDEN_READ_PATTERNS = (
    r"[;&\n\r]",
    r"\|\s*redirect\b",
    r"\|\s*tee\b",
    r"\bconfigure\b",
    r"^\s*conf\b",
    r"\breload\b",
    r"\berase\b",
    r"\bdelete\b",
    r"\bwrite\b",
    r"\bcopy\b",
    r"\bclear\b",
    r"\bdebug\b",
)


def build_netmiko_params(router_name: str) -> dict:
    """Construct validated Netmiko connection parameters for a target router."""
    router_key = router_name.strip().upper()
    if router_key not in ROUTERS:
        raise ValueError(f"Unknown router '{router_name}'. Valid routers: {list(ROUTERS.keys())}")

    target = ROUTERS[router_key]
    params = {
        "device_type": target["netmiko_device_type"],
        "host": target["hostname"],
        "port": target["port"],
        "username": USERNAME,
        "password": PASSWORD,
        "secret": SECRET,
        "conn_timeout": 15,
        "fast_cli": False,
        "global_delay_factor": 1.5,
    }
    # Support older RSA host key algorithms used by Cisco IOS 12.4 in GNS3
    if target["netmiko_device_type"] == "cisco_ios":
        params["disabled_algorithms"] = {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]}
    return params


def validate_readonly_command(command: str) -> str:
    """Strictly verify that a CLI string is read-only before sending it to a router."""
    cmd_clean = " ".join(command.strip().split())
    cmd_lower = cmd_clean.lower()

    if not cmd_lower.startswith(ALLOWED_READ_PREFIXES):
        raise PermissionError(
            f"GUARDRAIL BLOCKED: '{cmd_clean}'. Only {ALLOWED_READ_PREFIXES} commands permitted."
        )
    for pattern in FORBIDDEN_READ_PATTERNS:
        if re.search(pattern, cmd_lower):
            raise PermissionError(
                f"GUARDRAIL BLOCKED: '{cmd_clean}' matched restricted pattern '{pattern}'."
            )
    return cmd_clean


def run_readonly_cli(router_name: str, command: str, read_timeout: int = 25) -> str:
    """Execute a validated read-only CLI command on a router."""
    cmd_clean = validate_readonly_command(command)
    with ConnectHandler(**build_netmiko_params(router_name)) as conn:
        if not conn.check_enable_mode():
            conn.enable()
        return conn.send_command(cmd_clean, read_timeout=read_timeout)


def push_maintenance_commands(router_name: str, commands: list[str]) -> str:
    """
    Execute engineer-triggered MOP simulation or rollback commands.
    Note: Never exposed as an autonomous LLM tool—only invoked by explicit UI actions.
    """
    with ConnectHandler(**build_netmiko_params(router_name)) as conn:
        if not conn.check_enable_mode():
            conn.enable()
        return conn.send_config_set(commands)