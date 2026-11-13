"""Public keys only. The compiled build embeds the issuer's exported key set."""

"""Trusted public keys embedded in the customer application.

Private signing keys stay outside this repository and are never bundled into
the customer build.
"""

PUBLIC_KEYS: dict[str, bytes] = {
    "b626d5b2ffc5c2b7": bytes.fromhex(
        "f7f814e4ee2cd485fb3db4bf7bb7a654ff60b075fd67f009419e8a2db6adb8dc"
    ),
}
