# Security

Beamer sends your keystrokes, pointer and clipboard between machines, so the link between them
matters. This is what it protects, what it does not, and how to report a problem.

## What the link protects

- **Every frame is encrypted and authenticated.** Beamer 1.5.0 uses wire version 6. Each connection
  exchanges fresh X25519 public keys and random nonce prefixes. The two directions get separate
  keys, derived with HKDF-SHA256 from the shared token and the X25519 result. PyNaCl provides
  X25519 and ChaCha20-Poly1305 through libsodium; HKDF-SHA256 uses Python's standard library.
  Frames are sealed with ChaCha20-Poly1305.
- **A later token leak does not decrypt a recorded 1.5.0 connection.** The connection key also
  depends on each side's fresh X25519 secret, which is discarded after key setup. A token is still
  sensitive: someone who gets it can act as that paired machine while they have it.
- **The token is never sent.** It is used locally to identify the pair and derive connection keys.
- **Frames cannot be replayed or reordered within a connection.** Each frame carries a counter the
  receiver requires to advance by one. A repeated, dropped or reordered frame ends the connection.
  A recorded connection also fails when sent again because a new connection has fresh keys.
- **Pairing uses a short-lived six-digit code.** The machines run CPace, a password-authenticated
  key exchange, to agree a token. The code is not sent over the network, and a recorded exchange
  cannot be checked against a list of possible codes offline. A code is accepted once and expires
  after a minute. The exchange is written out in [PAIRING.md](PAIRING.md).
- **Traffic stays on your network.** Paired machines talk directly. There is no account, relay
  server or telemetry. The update check makes an ordinary HTTPS request to GitHub's public releases
  API once a day; it sends no Beamer data, though GitHub sees the address the request comes from.
  Check for updates on Overview turns it off. Windows firewall rules apply to Private networks
  only.
- **Unauthenticated connections are limited.** A peer has five seconds and a 4KB first frame to
  authenticate, and Beamer holds only a small number of unauthenticated connections at once.

## What it does not protect against

- **Someone on your network can see that Beamer is running.** Machines announce their name and
  connection port on the local network. Anyone on that network can try to reach the pairing
  service while a code is on screen.
- **Someone on your network can interrupt pairing.** A failed attempt can use up the displayed
  code, so you may need to show a new one. The attempt does not reveal the code or let the person
  join the pair.
- **Pairing does not prove a machine's network address.** The address comes from local discovery,
  which another machine on the network can imitate. That can stop a connection or point Beamer at
  the wrong address, but the encrypted link still requires the paired token.
- **A machine with the token is trusted.** It can type and click in applications that accept
  synthetic input on the paired machine. On Windows, Beamer runs elevated so it can reach elevated
  windows; it cannot control the secure desktop. Treat the token like a password.
- **Malware on either machine.** Software that can read your keyboard or Beamer's local settings
  can already act as you or the paired machine.
- **Traffic analysis.** Encryption hides the contents, not when machines talk or the size and timing
  of frames. The protocol version and a stable key identifier for the pair are sent in the clear,
  so a network observer can recognise connections between the same pair of machines.
- **Old Beamer versions cannot connect to 1.5.0.** Version 1.5.0 uses wire version 6; Beamer 1.4.x
  uses version 5. Update both ends and pair the machines again.

## Reporting a vulnerability

Please report privately, not in a public issue. On
[github.com/kalkman-code/beamer](https://github.com/kalkman-code/beamer), open the Security tab and
choose Report a vulnerability. Say what you found, how to reproduce it, and which version and
platform. Beamer is made by one person, so there is no fixed response time.
