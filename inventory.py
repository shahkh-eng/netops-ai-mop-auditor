"""
inventory.py
------------
Centralized router inventory and CMDB mapping for the 3-router GNS3 lab.
Separates Out-of-Band (OOB) management access from data-plane addressing.
"""

import os

# Default to Telnet for Dynamips IOS 12.4 lab speed; set NET_USE_TELNET=false for SSHv2.
USE_TELNET = os.getenv("NET_USE_TELNET", "true").lower() == "true"

USERNAME = os.getenv("NET_USER", "admin")
PASSWORD = os.getenv("NET_PASS", "cisco123")
SECRET = os.getenv("NET_SECRET", "cisco123")

ROUTERS = {
    "R1": {
        "role": "HQ Edge (AS 65001)",
        "hostname": "172.20.40.211",
        "username": USERNAME,
        "password": PASSWORD,
        "secret": SECRET,
        "driver": "ios",
        "port": 23 if USE_TELNET else 22,
        "netmiko_device_type": "cisco_ios_telnet" if USE_TELNET else "cisco_ios",
    },
    "R2": {
        "role": "Core ASBR (AS 65002)",
        "hostname": "172.20.40.212",
        "username": USERNAME,
        "password": PASSWORD,
        "secret": SECRET,
        "driver": "ios",
        "port": 23 if USE_TELNET else 22,
        "netmiko_device_type": "cisco_ios_telnet" if USE_TELNET else "cisco_ios",
    },
    "R3": {
        "role": "DC Edge (AS 65002)",
        "hostname": "172.20.40.213",
        "username": USERNAME,
        "password": PASSWORD,
        "secret": SECRET,
        "driver": "ios",
        "port": 23 if USE_TELNET else 22,
        "netmiko_device_type": "cisco_ios_telnet" if USE_TELNET else "cisco_ios",
    },
}

# Lightweight CMDB map used to enrich BGP/OSPF/interface audit outputs
IP_TO_DEVICE = {
    "1.1.1.1": ("R1", "Loopback0"),
    "192.168.10.1": ("R1", "Loopback1"),
    "10.12.0.1": ("R1", "FastEthernet0/0"),
    "2.2.2.2": ("R2", "Loopback0"),
    "10.12.0.2": ("R2", "FastEthernet0/0"),
    "10.23.0.1": ("R2", "FastEthernet2/0"),
    "3.3.3.3": ("R3", "Loopback0"),
    "172.16.30.1": ("R3", "Loopback1"),
    "10.23.0.2": ("R3", "FastEthernet0/0"),
}