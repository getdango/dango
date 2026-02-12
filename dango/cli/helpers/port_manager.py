"""dango/cli/helpers/port_manager.py

Port checking utilities for CLI commands.
"""

import socket

import psutil


def check_port_in_use(port: int) -> bool:
    """
    Check if a port is already in use.

    Args:
        port: Port number to check

    Returns:
        True if port is in use, False otherwise
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Set SO_REUSEADDR to handle TIME_WAIT state
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def get_process_using_port(port: int) -> int | None:
    """
    Get PID of process using a specific port.

    Args:
        port: Port number

    Returns:
        PID of process using the port, or None
    """
    try:
        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status == "LISTEN":
                return conn.pid
    except (psutil.AccessDenied, AttributeError):
        pass

    return None
