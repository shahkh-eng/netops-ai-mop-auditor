"""
scenarios.py
------------
Defines realistic Network Maintenance Activities (MOPs) that follow the exact same
Pre-Check -> Engineer Change -> Post-Check -> AI Audit lifecycle.

Each simulated scenario provides:
- authorized_scope: What the engineer is approved to modify (used by Llama 3.2 to catch scope creep).
- clean_change: Exact intended change (should pass Post-Check & get AI SIGN-OFF).
- broken_change: Realistic human error/misconfiguration (should be caught by Post-Check & REJECTED).
- rollback_commands: Safe inverse commands to restore baseline if a simulated change fails.
"""

MOP_SCENARIOS = {
    "MOP-101": {
        "title": "MOP-101: Core Link Description & Interface Hygiene (R2 Fa2/0)",
        "target_routers": ["R2"],
        "authorized_scope": (
            "Authorized ONLY to update interface description on R2 FastEthernet2/0. "
            "Must NOT alter IP MTU, ICMP unreachables, OSPF area, or BGP peering."
        ),
        "clean_change": {
            "router": "R2",
            "summary": "Updated FastEthernet2/0 description to '[MAINT-MOP101] Core-to-R3-f0/0'.",
            "commands": [
                "interface FastEthernet2/0",
                "description Core-to-R3-f0/0 [MAINT-MOP101]",
            ],
            "rollback": [
                "interface FastEthernet2/0",
                "description Core-to-R3-f0/0",
            ],
        },
        "broken_change": {
            "router": "R2",
            "summary": "Simulated Mistake: Engineer updated description AND accidentally applied ip mtu 1400 + no ip unreachables.",
            "commands": [
                "interface FastEthernet2/0",
                "description Core-to-R3-f0/0 [MAINT-MOP101]",
                "ip mtu 1400",
                "no ip unreachables",
                "ip ospf mtu-ignore",
            ],
            "rollback": [
                "interface FastEthernet2/0",
                "description Core-to-R3-f0/0",
                "no ip mtu",
                "ip unreachables",
                "no ip ospf mtu-ignore",
            ],
        },
    },
    "MOP-102": {
        "title": "MOP-102: Onboard New DC App Subnet 172.16.40.0/24 (R3)",
        "target_routers": ["R3"],
        "authorized_scope": (
            "Authorized ONLY to add Loopback2 (172.16.40.1/24) on R3 and advertise 172.16.40.0/24 in BGP AS 65002. "
            "Existing DC prefix 172.16.30.0/24 and all BGP/OSPF peers must remain intact."
        ),
        "clean_change": {
            "router": "R3",
            "summary": "Created Loopback2 (172.16.40.1/24) and advertised 172.16.40.0/24 in BGP.",
            "commands": [
                "interface Loopback2",
                "description DC-App-Cluster-2",
                "ip address 172.16.40.1 255.255.255.0",
                "router bgp 65002",
                "network 172.16.40.0 mask 255.255.255.0",
            ],
            "rollback": [
                "no interface Loopback2",
                "router bgp 65002",
                "no network 172.16.40.0 mask 255.255.255.0",
            ],
        },
        "broken_change": {
            "router": "R3",
            "summary": "Simulated Mistake: Engineer advertised Loopback2 BUT accidentally removed existing 172.16.30.0/24 network.",
            "commands": [
                "interface Loopback2",
                "description DC-App-Cluster-2",
                "ip address 172.16.40.1 255.255.255.0",
                "router bgp 65002",
                "no network 172.16.30.0 mask 255.255.255.0",
                "network 172.16.40.0 mask 255.255.255.0",
            ],
            "rollback": [
                "no interface Loopback2",
                "router bgp 65002",
                "no network 172.16.40.0 mask 255.255.255.0",
                "network 172.16.30.0 mask 255.255.255.0",
            ],
        },
    },
    "MOP-103": {
        "title": "MOP-103: Core iBGP Peer Policy Tuning (R2 BGP AS 65002)",
        "target_routers": ["R2"],
        "authorized_scope": (
            "Authorized ONLY to add neighbor description on R2 for iBGP peer 3.3.3.3. "
            "Must NOT remove next-hop-self, update-source Loopback0, or drop eBGP peering."
        ),
        "clean_change": {
            "router": "R2",
            "summary": "Added neighbor description 'iBGP-Core-Peer-R3' for 3.3.3.3 on R2.",
            "commands": [
                "router bgp 65002",
                "neighbor 3.3.3.3 description iBGP-Core-Peer-R3",
            ],
            "rollback": [
                "router bgp 65002",
                "no neighbor 3.3.3.3 description",
            ],
        },
        "broken_change": {
            "router": "R2",
            "summary": "Simulated Mistake: Engineer added description AND accidentally removed 'neighbor 3.3.3.3 next-hop-self'.",
            "commands": [
                "router bgp 65002",
                "neighbor 3.3.3.3 description iBGP-Core-Peer-R3",
                "no neighbor 3.3.3.3 next-hop-self",
            ],
            "rollback": [
                "router bgp 65002",
                "no neighbor 3.3.3.3 description",
                "neighbor 3.3.3.3 next-hop-self",
            ],
        },
    },
    "MOP-104": {
        "title": "MOP-104: Custom Live GNS3 Activity (Manual CLI by Engineer)",
        "target_routers": ["R1", "R2", "R3"],
        "authorized_scope": (
            "Engineer will apply manual changes directly inside the GNS3 router console "
            "and verify zero unintended regressions across BGP, OSPF, and end-to-end MTU."
        ),
        "clean_change": None,
        "broken_change": None,
    },
}