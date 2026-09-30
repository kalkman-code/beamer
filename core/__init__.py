"""What both apps run the same way: the wire protocol and its pairing, the receiver, the crossing
effects and their arithmetic, the settings both machines share, update checks and Wake-on-LAN.

One copy, imported by mac_app, win_app and anything later, from the repository root (the apps put
it on sys.path when run from source; both packagers bundle it). Everything platform-shaped is
injected at construction, so nothing in here may import an app module at the top level.
"""
