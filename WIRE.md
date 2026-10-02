# The link, wire version 6

How machines running Beamer 1.5.0 and later know each other, open an encrypted link, and hand
input from one to another, exactly enough to write a second implementation. Pairing, the
exchange that gives two machines their shared token, is in [PAIRING.md](PAIRING.md); this file
says what changes in it for 1.5.0 and otherwise leans on it. What the link protects and what it
does not is in [SECURITY.md](SECURITY.md).

Version 6 replaces version 5 (1.4.1 to 1.4.3) outright. Version 5 had two machines and two roles,
a Mac and a PC; version 6 has any number of machines, each with its own identity, and a platform
is something a machine says about itself rather than what it is. There is no fallback: a
machine on version 6 refuses a version 5 peer and says which machine to update.

Beamer 1.5.0 is built to it. The last sections list where it departs from version 5 and from its
first design, what has been decided, and the questions still open.

## Terms

- **Machine.** One install of Beamer: a desktop app, or later a phone app. Each has an id.
- **Peer.** A machine this one has paired with. Each peer has its own token.
- **Link.** One TCP connection between two peers. The machine that opens it is the
  **initiator**; the one that accepts it is the **responder**. Input only ever travels from
  initiator to responder, so between two desktops that may each drive the other there are two
  links, one each way, as in version 5. A phone only ever initiates.
- **Owner.** The machine whose input a responder is injecting now. A responder has at most one
  owner at a time, or none. The owner is always the initiator of the link the input arrives on.
- **Route.** A number the owner raises each time it moves its input, so that a late request
  cannot move input that has already moved.
- **Zone.** A stretch of a machine's own screen (an edge, part of one, a corner or the notch)
  that leads to one peer.

## Encodings

- **Ids.** A machine id is 16 bytes from the operating system's secure random source, made once
  per install. Sixteen zero bytes is never an id. In JSON and in the settings file it is
  PAIRING.md's `b64` (URL-safe base64 without padding, 22 characters), with the same rule:
  decode, encode again, and refuse the text if it differs. A machine id never travels in the
  clear on a link: the preamble carries the pair's key id instead (section 2). Pairing sends
  both machines' ids in the clear, once (section 6).
- **Tokens.** A token made by pairing is the `b64` of 32 bytes: 43 characters that decode and
  encode again to themselves. That spelling proves nothing about where a token came from: 43 `A`s
  spell 32 zero bytes, and a user could have typed them. So what links is an entry made by
  pairing on 1.5.0 or later (`from_1_4` false) whose token has that spelling. A token migrated
  from 1.4.x never links, whatever its spelling, and neither does one of any other shape, a
  **typed token** (sections 1 and 2).
- **Messages** are UTF-8 JSON objects of the shape `{"type": <string>, "data": <object>}`, with
  no byte order mark. Unknown types are ignored, and so are unknown keys inside `data`.
- **Not a message.** A frame's plaintext that is not UTF-8, not JSON, or JSON whose top level is
  not an object (`[]`, `"x"`, `1`) is not a message at all. It ends the link once the handshake
  is done (section 2, frames); as the first frame it is answered `invalid_hello` (section 2,
  step 7). Nothing below applies to it.
- **Malformed messages.** A message is malformed when it has no string `type` or no object
  `data`, holds `NaN` or an infinity, or has a known field whose value is outside what this
  document allows; section 10's shared values are the one exception, each left out on its own.
  A field that does not belong to a message, or to that kind of the message, is ignored like an
  unknown one. A sender never repeats a key; a receiver may refuse a message that does, or keep
  the last value (Swift's decoders keep the last, Android's `org.json` refuses), and neither may
  matter to what a correct sender sends. A malformed message is ignored as a whole, with three
  exceptions: a malformed first frame and a malformed `ack` end the link, and a malformed `focus`
  naming the receiver is answered with `refuse` (section 5). A receiver whose JSON parser
  refuses outright a text that breaks one of the rules above the parser cannot see past (a byte
  order mark, `NaN`, a lone surrogate) may end the link instead of ignoring the message; a
  correct sender never sends one, and the vectors record what the desktops do, which is to
  ignore it.
- **Numbers.** "Integer" means a JSON number with no fraction or exponent (`1`, never `1.0` or
  `1e0`), and never `true` or `false`. A parser that turns JSON into a platform number type must
  check the text, not the value: Swift's `JSONSerialization` gives `true` and `1.0` as numbers
  that cast to `1`. "Number" means any finite JSON number.
- **Strings.** Lengths are in Unicode code points unless a limit says bytes, and a string is
  malformed if it is not valid Unicode (a lone surrogate).
- **Names** are 1 to 48 code points, cut and checked exactly as PAIRING.md says.
- **Platforms** are `"macos"`, `"windows"`, `"linux"`, `"ios"` and `"android"`. Any other
  string of 1 to 16 characters from `a-z`, `0-9` and `_` is accepted and treated as `"linux"` for
  the key table; anything else is malformed.
- **Hex in the vectors** is bytes in wire order.

## 1. Identity and the peers list

### What each machine keeps

Each app keeps its settings in one JSON file:

| App | File |
|---|---|
| Mac | `~/Library/Application Support/Beamer/settings.json` |
| Windows | `%LOCALAPPDATA%\Beamer\settings.json` |
| Linux | `$XDG_CONFIG_HOME/Beamer/settings.json`, else `~/.config/Beamer/settings.json` |

Beside the machine's other settings, which keep the names 1.4.x gave them, it holds:

| Field | Meaning |
|---|---|
| `schema` | `6` |
| `machine_id` | this machine's id, `b64` |
| `name` | this machine's name as shown to peers; the computer's name when empty |
| `port` | the TCP port this machine accepts links on, 24820 unless the user chose another |
| `shortcut` | whether the switch shortcut is one of this machine's ways across |
| `peers` | a list of peer entries, below, at most 32 |
| `zones` | this machine's zones, section 8 |
| `migrated_token_sha256` | lower-case hex SHA-256 of the UTF-8 of the last 1.4.x `auth_token` migrated, or `""` |

A peer entry:

| Field | Type | Meaning |
|---|---|---|
| `id` | `b64` or `""` | the peer's machine id; `""` only for the entry migrated from 1.4.x |
| `name` | string | the peer's name, from pairing, then from every `hello` or `welcome` |
| `platform` | string | the peer's platform, from pairing, then from every `hello` or `welcome` |
| `token` | string | the shared token from pairing, never reused for another peer |
| `host` | string | the IPv4 address to open a link to the peer at; `""` for a phone |
| `port` | integer | the peer's own link port; 0 for a phone, which is never dialled |
| `hw` | string | the peer's hardware address for wake-on-LAN, `aa:bb:cc:dd:ee:ff`, or `""` |
| `send` | boolean | this machine's input may go to the peer |
| `allow_drive` | boolean | the peer may drive this machine |
| `side` | string | which edge of this machine faces the peer (`left`, `right`, `top`, `bottom`), or `""` |
| `side_set_at` | integer | unix seconds of the last change to `side` at either end, 0 when never changed |
| `side_by` | `b64` or `""` | the machine that made that change; `""` only with `side_set_at` 0 |
| `paired_with` | list of `b64` | the ids the peer last said it is paired with (the `paired` message) |
| `paired_at` | integer | unix seconds of the pairing, 0 for a migrated entry |
| `linked` | boolean | a version 6 link with this peer has authenticated at least once |
| `from_1_4` | boolean | this entry was migrated from a 1.4.x pairing, and never links |
| `way_back` | boolean, optional | what the peer last said in `arrangement`'s `way_back` (section 8): whether one of its zones leads here; absent until it says |

Rules:

- **Two entries never share an id**, and no entry has this machine's own id. Pairing refuses
  both (section 6), and a link that would make either true is refused (section 2).
- **Two entries never share a key id** (section 2: 16 bytes computed from the token, never
  stored). Pairing refuses a token whose key id is already an entry's (section 6), and the
  re-migration below skips one. With tokens from pairing a clash means the same token twice.
- **At most one entry has `id: ""`**, and it is the one with `from_1_4: true`. Zones for it
  name it by `""`.
- **An entry migrated from 1.4.x, or with a typed token,** is kept and shown, but never linked:
  it is not dialled, the responder never computes or matches its key id, and Overview asks the
  user to pair that machine again: "*name* was paired on Beamer 1.4. Pair the two again to link
  them on 1.5.0." A new pairing with that machine replaces it (section 6).
- **Removing a peer** deletes its entry, token included, closes its links at once, and ends its
  ownership if it holds it (section 4). The peer is told nothing; its next link's key id is
  unknown here, so it is closed with nothing sent, and the peer says this machine closed the
  connection (section 2).
- **Turning `allow_drive` off** ends that peer's ownership at once. Turning `send` off lets go of
  that peer if this machine's input is on it, and closes the link this machine opened to it.
- **Writes are atomic**: a new file beside the old, then a rename over it.
- The machine id never changes for the life of the settings file. A new file (the user deleted
  it, or a fresh install) is a new machine, which every peer must pair with again. A file copied
  to a second computer (a restored backup, a migration assistant) makes two machines with one
  id and the same tokens: their peers see one machine whose links keep replacing each other.
  Deleting `settings.json` on the copy makes it a new machine.

### The migration from 1.4.x

1.4.x keeps its settings in `config.json` in the same folder, shaped differently on each app.
Each app migrates its own when it starts and finds no `settings.json`:

1. Read `config.json`. If it is missing or does not parse, start with defaults and no peers.
2. Make the machine id.
3. Copy every field 1.5.0 still uses under the same name, then move the pairing fields into one
   peer entry as the tables below say. When `auth_token` is empty, there is no peer entry.
4. Set `migrated_token_sha256` from `auth_token` (`""` when it is empty).
5. Write `settings.json` atomically. Never write `config.json` again.

On the Mac:

| 1.4.x field | Becomes |
|---|---|
| `host` | `peers[0].host` |
| `port` | `port`, and `peers[0].port` |
| `auth_token` | `peers[0].token` |
| `pc_name` | `peers[0].name` |
| `mac_address` (the PC's, for wake-on-LAN) | `peers[0].hw` |
| `send_to_windows` | `peers[0].send` |
| `allow_windows_to_drive` | `peers[0].allow_drive` |
| `crossing.edge` | `peers[0].side` |
| `crossing.arrangement_set_at` | `peers[0].side_set_at` |
| `crossing.methods`, `crossing.edge_parts`, `crossing.corner` | `zones`, and `shortcut` (section 8) |
| `key_map` | kept as it is: a style name, or a map (section 7) |
| — | `peers[0]`: `platform` `"windows"`, `id` `""`, `from_1_4` true, `linked` false, `paired_at` 0 |

On Windows:

| 1.4.x field | Becomes |
|---|---|
| `mac_host` | `peers[0].host` |
| `port` | `port`, and `peers[0].port` |
| `auth_token` | `peers[0].token` |
| `paired_with` | `peers[0].name` |
| `mac_hardware_address` | `peers[0].hw` |
| `send_to_mac` | `peers[0].send` |
| `allow_mac_to_drive` | `peers[0].allow_drive` |
| `mac_return_edge` | `peers[0].side` |
| `arrangement_set_at` | `peers[0].side_set_at` |
| `crossing_methods`, `crossing_edge_parts`, `crossing_corner` | `zones`, and `shortcut` (section 8) |
| `modifier_style` | kept as it is (section 7) |
| `host` (this PC's own address, as shown) | dropped: the pairing sheet shows the live address |
| `mac_resistance_px` | dropped: the owner sends its resistance with every `focus` |
| — | `peers[0]`: `platform` `"macos"`, `id` `""`, `from_1_4` true, `linked` false, `paired_at` 0 |

On both, `peers[0].side_by` is this machine's own id when `side_set_at` is above 0, else `""`:
two migrated ends that disagree at the same second then settle by the larger id (section 8).

Every other field (the shortcut key and style, the Design page, pointer and scroll speed, the
keys that stay, appearance, update checks, Hide addresses, Same on both machines) keeps its
1.4.x name and value. A field 1.4.x did not have takes its default.

**Idempotent.** Once `settings.json` exists, starting again changes nothing in it (unless `config.json`'s
token has changed, below), and
`config.json` is byte for byte what 1.4.x left. A start that fails before step 5 leaves no
`settings.json`; the next start migrates again with a new machine id, which no peer has learnt.

**Downgrade.** Because `config.json` is never written, installing 1.4.x again finds its own
settings as they were at the upgrade. Changes made under 1.5.0 do not go back with it. Both
machines have to go back, because a 1.4.x machine speaks version 5, which 1.5.0 refuses.

**Pairing again under 1.4.x, then upgrading again.** On every start that finds `settings.json`,
if `config.json` holds a non-empty `auth_token` whose SHA-256 differs from
`migrated_token_sha256`, that one pair is migrated again: the `from_1_4` entry is replaced (or
added, if the user had removed it, in which case it gets zones from `config.json`'s methods as the first
migration made them, and is not added at all when the list already holds 32), with `id` `""`, `linked` false and `paired_with` empty; zones
that named the replaced entry name `""`; and `migrated_token_sha256` is updated. A token that
has not changed is never migrated twice, so a peer the user removed stays removed. A token whose
key id is another entry's already is not migrated, and `migrated_token_sha256` is still updated.
A typed token is never refused by that check. Either way the migrated entry never links.

**Why a migrated pairing never links.** 1.4.x kept one `auth_token` and nothing about where it
came from: pairing wrote it, and so did the token field on the Mac's Connection page and the
PC's settings, and the name pairing saved beside it stayed when a user typed a new token. A
typed token can be weak, and its key id (section 2), seen on any link, lets someone test guesses
against it offline. The spelling cannot tell the two apart, so the migration keeps the entry to
show the user which machine it was, and the user pairs the two again. The entry keeps `id: ""`,
since 1.4.x never had ids. 1.5.0's first two betas let a migrated token of pairing's spelling
link and learn its peer's id; such an entry keeps that id and does not link either.

## 2. The link

### Who connects to whom

Every desktop listens on its `port` for links from its peers. It keeps one link open to each
peer with `send` true, a `host` and a non-zero `port`, reconnecting every two seconds while it is
down, as version 5 did for its one peer. A phone listens for nothing; it opens a link to each
desktop it is paired with while its app is open, at once when the app comes to the front and
every two seconds while a link is down.

Version 5's Mac could reach its PC through an SSH tunnel on 127.0.0.1:24822 when macOS 27's
local network rules blocked it. 1.5.0 keeps it for the one peer whose address the tunnel was set
up for; a link that arrives from a loopback address never changes a saved `host`.

### The preamble

Each side's first bytes are a preamble, in the clear:

| Offset | Bytes | Field |
|---|---|---|
| 0 | 5 | `BEAMY`, ASCII |
| 5 | 1 | the version, `0x06` |
| 6 | 8 | the nonce prefix: random, new for every link |
| 14 | 16 | the pair's key id (below) |
| 30 | 32 | the sender's ephemeral X25519 share: new for every link |

62 bytes in all, the same layout both ways. The key id is in the clear so the responder can
choose the one token to try before anything is decrypted; it belongs to the pair, not to either
machine, so the responder's preamble carries the same key id as the initiator's. Neither
machine's id is in the clear: each travels inside the first frame (`hello` and `welcome`) and is
checked there.

```
key_id = HKDF-SHA-256(ikm = the token as saved, as text, salt = b"beamer-link-v6",
                      info = b"beamer-key-id", length = 16)
```

That is the first 16 bytes of HKDF's first block. Every entry's key id is computed from its token
when it is needed and never stored. A key id is an offline test of its token, harmless for 256
random bits and fatal for a typed word, and a token's spelling cannot tell the two apart, so a
key id is only ever sent or matched for an entry this machine made by pairing on 1.5.0 or later
(section 1). Every other entry is never used for a link at all.

The ephemeral share is `X25519(secret, 9)` (RFC 7748's base point), from 32 bytes of fresh
randomness the side draws for this link alone and forgets once the link's keys are made.

### The handshake

The responder:

1. Holds at most four connections in their handshakes, and at most one from any one address
   (the source IPv4 address, whatever the port). A new connection from an address that already
   has one replaces it; a new one from another address when four are held replaces the oldest.
   An authenticated link never counts against the four. The preamble must arrive within **1 second**
   and the whole handshake within **5 seconds** of the connection being accepted. Beside the
   four slots, at most **32** connections are being dealt with before they authenticate,
   counting those in a slot, those being answered and drained (step 2) and those whose slot
   was taken by a newer one but are not yet closed; a connection past the 32 is closed at
   once, with nothing read or sent.
2. Reads 6 bytes.
   - Not `BEAMY`: a Beamer from before version 4, which opened with a cleartext frame. It
     answers with the version 5 legacy frame, a 4-byte big-endian length then the JSON
     `{"type":"welcome","data":{"version":6,"error":"version_mismatch"}}`, and closes.
   - Any version below 6: reads the 8 bytes left of a 14-byte version 4 or 5 preamble, answers
     with the **short reply** (`BEAMY`, `0x06`, 8 random bytes: 14 bytes, no id), and closes.
     A version 5 Beamer reads that as a newer peer and says so in its own words. This machine's
     status names the peer whose `host` is the connection's address, if one is.
   - A version above 6: answers with the short reply and closes.

   Wherever it answers and closes, it frees the connection's handshake slot, shuts its sending
   side, then reads and discards what the peer sends for up to one second, so the answer is
   not lost to a reset. At most **8** connections drain at once; past that one is closed as
   soon as its answer is sent, without draining. A draining connection still counts against
   the 32 of step 1.
3. Reads the other 56 bytes, and looks up the entry made by pairing (section 1) whose token has
   this key id. None: it closes, sending nothing. There is never a second try with another
   token, and no entry is looked up by anything else.
4. Draws its own ephemeral secret and share, and computes the shared secret from the
   initiator's share (below). When the result is 32 zero bytes, or the platform's X25519 refuses
   the share, it closes, sending nothing.
5. Draws its own nonce prefix, drawing again while it equals the initiator's, and sends its
   preamble, with the same key id.
6. Makes the two keys (below) and reads the first frame, whose length field must be at least
   21 and at most **4096**; a larger claim is refused before the frame is read. A frame that
   fails its tag, or whose counter is not 1, closes the link with nothing sent back.
7. Requires that frame to be a valid `hello` (below). Any first frame whose tag checks but which
   is not a valid `hello` (a plaintext that is not JSON or not an object, another `type`, a
   malformed field) is answered with `welcome` carrying only `version` 6 and
   `error: "invalid_hello"`, and the link is closed. This is the one place a plaintext that is
   not a JSON object is answered rather than ending the link at once.
8. Checks the `id` in `hello`. When it is this machine's own, or is not the entry's, it answers
   `welcome` carrying only `version` 6 and `error: "wrong_id"`, and closes. Only a holder of the
   token can read that answer, so it tells nobody else anything. Otherwise it sets `linked`, and
   saves what changed.
9. Sends `welcome`, then its announcements (section 3).
10. Closes any earlier link it accepted from the same peer id; a link this machine opened to that
    peer is a separate link and stays. If the closed link's peer was the owner, the ownership
    ends first (section 4).

The initiator:

1. Connects, with a one-second timeout. From the moment the connection is up, the whole
   handshake must finish within **5 seconds**. It dials only an entry made by pairing (section 1).
2. Draws its ephemeral secret and share and sends its preamble, with the entry's key id.
3. Reads 6 bytes.
   - Not `BEAMY`: says the peer did not answer as a Beamer, and closes.
   - A version below 6: reads the 8 bytes left, closes, and says the peer runs an older Beamer,
     naming it: "Update Beamer on *name*: it is older than this one."
   - A version above 6: closes and says this machine is the older one.
   - The connection closes or resets before a byte arrives. A responder that does not know the
     key id closes like this, and so does a version 5 responder, after reading 14 of the 62
     bytes; on Windows and Linux that close can destroy the version 5 preamble in flight. So:
     when the entry has never linked on version 6 (`linked` false), "Update Beamer on *name* to
     1.5.0; if it is up to date, pair the two again"; otherwise "*name* closed the connection: it
     may have removed this machine, or be too busy to answer".
   - Nothing within the handshake's 5 seconds (the initiator has no separate deadline for the
     preamble): says the peer is probably running a Beamer from before version 4.
4. Reads the other 56 bytes and checks them: the prefix must differ from its own, and the key id
   must be the one it sent. It computes the shared secret from the responder's share, which must
   not give 32 zero bytes or be refused by the platform's X25519. On any failure it closes
   without sending anything and says a different Beamer answered at that address.
5. Makes the two keys, sends `hello`, and reads the `welcome`, whose length field is also held
   to 21 to 4096. The key id matched, so the peer holds this token: a failed tag here means
   something on the path altered the link ("*name* could not be authenticated"), and a closed
   connection says what a close in step 3 says, by `linked`.
6. Requires a valid `welcome`. One with `error: "invalid_hello"` means this app sent something
   the peer could not read; it says so and closes. One with `error: "wrong_id"` means the peer
   holds this pair's token under another machine's id: it closes, stops dialling that entry until
   its settings change or the app starts again, and says "*name* has this pairing under another
   machine: remove it on both and pair again." Any other `error` is a `welcome` that is not
   valid.
7. Checks the `id` in `welcome`: not this machine's own, and the entry's. On a failure it
   closes, sending nothing more, and says a different Beamer answered at that address. Otherwise
   it sets `linked`.
8. Sends its announcements.

The initiator sends `hello`, and its own id in it, before it has seen the responder's id. Both
are sealed under keys only a holder of the token can make, so only the paired machine, or one
that has its token, ever reads either id.

### The keys

```
shared         = X25519(this side's ephemeral secret, the other side's ephemeral share)
key(direction) = HKDF-SHA-256(ikm  = the token as saved, as text || shared,
                              salt = b"beamer-link-v6",
                              info = b"beamer-frames" || 0x06 || key_id || prefix_I || prefix_R
                                     || share_I || share_R || direction,
                              length = 32)
direction = 0x01 for frames the initiator seals, 0x02 for frames the responder seals
```

- `prefix_I` and `share_I` are the initiator's, `prefix_R` and `share_R` the responder's: the
  order is fixed by role, not by who is computing. `info` is always 111 bytes: 13 of label, the
  version byte, 16 of key id, 8 and 8 of prefix, 32 and 32 of share, and the direction byte.
- `ikm` is 75 bytes for a token made by pairing: the token's 43 ASCII characters of `b64`, never
  the 32 bytes they spell, then the 32 bytes of `shared`.
- HKDF is RFC 5869, and X25519 RFC 7748 (the scalar clamped, bit 255 of the incoming share
  ignored). A `shared` of 32 zero bytes ends the handshake, and so does a platform's X25519
  that throws on a low-order share (CryptoKit, Node), exactly as PAIRING.md treats `K`. The
  check is on the output: every low-order share gives zeros, and nothing else does.
- What the keys bind: the token, the version, both prefixes and both shares in role order, and
  the direction. So a recorded link cannot be played into a new one (fresh prefixes and shares
  change the keys), frames cannot be reflected back at their sender (the direction byte), and a
  link cannot be moved to another pair (the key id and the token). The machine ids are not in
  the keys: `hello` and `welcome` carry them sealed, and a frame cannot be altered without its tag
  failing.
- **Forward secrecy.** The shared secret never leaves either side, and both ephemeral secrets are
  forgotten once the keys are made, so a token that leaks later does not open a recording of a
  link made before. It still lets its holder open new links as either machine, until the pair
  is removed.

### Frames

As version 5:

```
frame  = length (4 bytes, big-endian) || counter (4 bytes, big-endian) || ciphertext || tag (16 bytes)
length = 4 + len(plaintext) + 16, at least 21 (the ciphertext is as long as the plaintext)
nonce  = the sealing side's prefix (8 bytes) || counter (the frame's 4 counter bytes, big-endian, as sent)
AEAD   = ChaCha20-Poly1305 (RFC 8439), no associated data
```

- Each side counts its own frames from 1. A receiver refuses any counter that is not one more
  than the last it accepted. The last frame a side may send carries 2^32 - 1; a sender that needs
  another closes the link and reconnects.
- The plaintext is one message. A frame may claim at most 16 MiB, except the first each way.
- **A frame begins** when its first length byte arrives, by a monotonic clock, and must arrive
  whole within **30 seconds** of that byte, whatever its size; one still arriving after that
  ends the link. Between its bytes the link's liveness still holds (section 3).
- Only a `clipboard` is ever larger than 64 KiB of plaintext, and a receiver parses such a frame
  only where it would take that clipboard: a responder from its owner, an initiator from a peer
  it let go of no more than 10 seconds before the frame began (section 5, the clipboard). Any
  other plaintext over 64 KiB is authenticated, so its counter is spent, and dropped unparsed;
  the link stays up.
- Anything that fails (the tag, the counter, a length out of range, a frame past its 30 seconds,
  a plaintext that is not a message: Encodings) ends the link.

### `hello` and `welcome`

`hello`, the initiator's first frame:

| Field | |
|---|---|
| `version` | the integer `6` |
| `id` | the initiator's machine id, `b64` |
| `name` | the initiator's name |
| `platform` | the initiator's platform |
| `app` | its app version, such as `"1.5.0"`: 1 to 32 printable ASCII characters (0x20 to 0x7E); shown, never compared |
| `caps` | capabilities, below: a list of at most 16 strings of 1 to 32 printable ASCII characters; one this machine does not know is ignored, and a repeat means the same as one |
| `port` | integer 0 to 65535: the port the initiator accepts links on, 0 when it accepts none |
| `hw` | optional: its hardware address on the interface this link leaves from, six two-digit lower-case hex groups joined by colons |

`welcome`, the responder's first frame, has `version`, `id` (the responder's machine id),
`name`, `platform`, `app`, `caps`, optionally `hw`, and:

| Field | |
|---|---|
| `accepts` | `true` when this initiator may drive the responder (`allow_drive`), else `false` |
| `error` | only in the `invalid_hello` and `wrong_id` answers, which have no other field but `version` |

Each end updates the peer entry from what the other sent: `name` and `platform` always, and `hw`
when it is present (an absent `hw` keeps the saved one). The responder also takes `port` from
`hello`, and `host` from the address the link came from when `port` is not 0 and that address
is neither loopback nor link-local. A change is saved.

Capabilities say what a machine does with a message it receives:

| Capability | Means |
|---|---|
| `clipboard` | it takes clipboard text |
| `clipboard_image` | it takes clipboard images |
| `gestures` | it acts on `gesture` |
| `media_keys` | it acts on the media key names (section 7) |
| `text` | it types `text` |
| `settings` | it keeps Same on all machines (`settings`) |

A sender does not send a message its peer's capabilities do not cover. A receiver that gets one
anyway drops it, and still acknowledges it when it carries a `seq`.

### What it does not do

- **The key id is in the clear.** It is the same on every link of one pair, so anyone on the
  path can recognise that pair's links on another network, and anyone who can reach a
  responder can learn whether a key id it has seen is one of its pairs, because it answers that
  key id and not others. Nothing in the clear links one machine's different pairings, and the
  machine ids are never in the clear. The key id is a test of its token for anyone who sees it,
  which is why only tokens made by pairing (256 random bits) are used.
- **Handshakes can be crowded out.** Anyone who can reach the port can keep the four slots
  busy, and one who has seen a pair's key id holds a slot for the whole 5 seconds and costs an
  X25519 each time. Replacing the oldest means a real peer's next try still gets in unless the
  flood comes from several addresses at once; something on the LAN that can do that can deny
  service in simpler ways. Two peers behind one address (a NAT) take turns, and the one
  replaced retries two seconds later.
- **A token holder is trusted.** Forward secrecy protects recordings from a token that leaks
  later; it does not stop the holder of a leaked token opening a new link as either machine, or
  sitting between the two on the path and reading what passes.
- **The address is not authenticated.** A peer's `host` comes from pairing, a beacon, a QR, or
  the source of an authenticated link. Something on the path can point a machine at the wrong
  address; it cannot read or forge a link, whose keys come from the token.

## 3. Messages

Initiator to responder:

| `type` | `data` | Carries `seq` |
|---|---|---|
| `hello` | section 2 | no |
| `ping` | `{}` | no |
| `focus` | section 5 | no |
| `keydown`, `keyup` | `key`, optional `us` (section 7) | yes |
| `mousemove` | `dx`, `dy` | yes |
| `mousedown`, `mouseup` | `button`: `left`, `right`, `middle`, `back` or `forward` | yes |
| `scroll` | `dy`, `dx`, `mode` | yes |
| `gesture` | `name`: `swipe_up`, `swipe_down`, `swipe_left`, `swipe_right`, `spread` or `pinch` | yes |
| `text` | `text` (section 10) | yes |

Responder to initiator:

| `type` | `data` |
|---|---|
| `welcome` | section 2 |
| `ack` | `seq`, `held_us`, optional `locked` (section 9) |
| `accept` | `route` (section 5) |
| `refuse` | `route`, `why` (section 5) |
| `accepts` | `accepts` (section 4) |
| `switch` | section 5 |

Either way:

| `type` | `data` |
|---|---|
| `clipboard` | optional `text`; optional `image` (standard base64 with padding, of a PNG) with `image_format: "png"` |
| `arrangement` | `edge`, `set_at`, `by` (section 8) |
| `settings` | section 10 |
| `paired` | `ids`: list of `b64`, at most 32 |

- **`seq`** is in `data`: an integer from 1 to 2^53 - 1 that each initiator counts from 1 on
  every new link (and from 1 again on a reconnect) and raises by one per input message. The responder acknowledges the
  highest `seq` it has dealt with, whether it injected the message, used it at an edge, or
  dropped it (section 4). No other message carries a `seq`.
- **`mousemove`**: `dx` and `dy` are integers from -65535 to 65535, relative motion in the
  sender's logical pixels (points on a Mac) after its own acceleration; positive `dy` is down.
  The sender keeps the fractions and sends whole pixels. A phone sends its own points after its
  acceleration curve.
- **`scroll`**: `dy` and `dx` are numbers from -65535 to 65535; `mode` is `"pixel"` for a
  trackpad's continuous delta in points, or `"line"` for whole wheel notches. Both are in the Mac's
  convention, as version 5 sends them: `dy` and `dx` are Core Graphics' scroll axes 1 and 2 as a
  Mac reports them, the user's scrolling direction already applied, and a Mac receiver posts them
  as they stand. A Windows receiver takes positive `dy` as its wheel's positive and positive `dx`
  as its horizontal wheel's positive; the `dx` mapping is still to be confirmed
  on hardware. Any other sender, a phone included, sends what a Mac would for the same
  movement. A sender always sends all three; a receiver takes a
  missing `dx` as 0 and a missing `mode` as `"line"`, as version 5 did.
- **Clipboard limits**: text at most 256 KiB as UTF-8. An image at most 8 MiB before base64,
  and a PNG a receiver can decode without the file deciding how much memory that takes, checked
  from its bytes before anything decodes it:
  - the PNG signature, then an `IHDR` chunk of 13 bytes, whose width and height are each 1 to
    16384 and whose product is at most 2^25 (33,554,432 pixels: an 8K screen is 33,177,600);
  - every chunk (4-byte big-endian length, type, data, CRC) inside the file, up to `IEND`; bytes
    after `IEND` are ignored, and CRCs are not checked;
  - a still with one header: no second `IHDR`, and none of APNG's `acTL`, `fcTL` or `fdAT`;
  - what a decoder inflates besides the pixels, the zlib streams of `iCCP`, `zTXt` and an `iTXt`
    whose compression flag is 1, at most 4 MiB inflated in all. Each stream must be one whole
    zlib stream ending where its chunk ends: one that is not zlib, is cut short or has bytes after
    its end counts as over, and so does an `iTXt` whose compression flag is neither 0 nor 1, or
    any of the three without the null bytes that end its keyword (and, in `iTXt`, its language
    and translated keyword, when it is compressed).

  A few megabytes of PNG can otherwise claim gigabytes once decoded. A sender never sends an
  image outside these limits, and a receiver drops one, without decoding it. Over any limit,
  that part is dropped and the rest of the message stands.
- **Liveness.** An initiator sends `ping` when it has sent nothing for a second; a responder's
  `ack` heartbeat does the same the other way (section 9). A side that receives no byte at all for
  2.5 seconds (a responder) or 2 seconds (an initiator) ends the link. Any byte counts, so a large
  clipboard frame arriving slowly keeps the link alive while it comes.
- **`paired`** lists the ids of every peer the sender has, less the receiver's. A machine sends
  it only to peers it may send input to (`send` true), since those are the ones whose Crossing
  page needs it: after the handshake and again whenever its list changes. The receiver stores it
  in `paired_with` (section 5, the Crossing page).
- **Announcements.** After the handshake each end sends `paired` when the rule above allows,
  `arrangement` when this pair has a side, and `settings` when the other's capabilities list
  `settings`.
- **Which link.** Between two desktops with a link each way, `arrangement`, `settings` and
  `paired` go on the link this machine opened when it is up, else on the other. When neither
  machine may send to the other there is no link, and nothing passes until one exists.

## 4. Input ownership

A responder has one owner or none. Only the owner's input is injected.

**Taking input.** An initiator asks for input with a `focus` whose `target` is the
responder's own id. The responder decides under one lock, in this order:

1. The `route` is not higher than the last this link had accepted: ignored, no answer. The
   owner has already moved on.
2. Fields other than `route` and `target` malformed: `refuse {why: "malformed"}`.
3. `allow_drive` false for this initiator: `refuse {why: "not_allowed"}`.
4. This machine's own input is on another machine: `refuse {why: "busy"}`. A machine never
   drives and is driven at once.
5. This machine sent this initiator home, or ended its ownership by force, less than 5 seconds
   ago: `refuse {why: "sent_home"}`.
6. Another peer is the owner: `refuse {why: "owned"}`.
7. A `stay` from an initiator that is not the owner: `refuse {why: "malformed"}`.
8. Otherwise it accepts, and the initiator is the owner. When it already was, the `focus` moves
   the pointer or re-arms the zones in place.

The answer, `accept {route}` or `refuse {route, why}`, is sent the moment the decision is made,
before the pointer is placed, an arrival plays or a lock screen is dealt with. A `focus` naming
this machine whose `route` or `target` is itself malformed cannot be answered with its route and
is ignored; the owner's wait for an answer (section 5) covers it.

There is no taking over. A second machine waits until the owner lets go. This differs from
version 5, where any new link replaced the old one.

**What a non-owner's messages do:**

| Message | From the owner | From anyone else |
|---|---|---|
| input (`seq` messages) | injected | dropped, acknowledged |
| `clipboard` | set on this machine | dropped |
| `focus` naming this machine | as above | as above |
| `focus` naming another machine | the owner lets go | ignored |
| `ping`, `arrangement`, `paired` | handled | handled |
| `settings` | handled if its caps list `settings` | the same |

**Ownership ends** when the owner sends a `focus` naming another target; when its link ends for
any reason or is replaced by another from the same peer; when the user removes the peer or turns
its `allow_drive` off; and by force, when this machine sent the owner home and it has not let go
within 1 second. Every time it ends, the responder:

1. releases every key and button it injected for that owner, before injecting anything else;
2. drops its armed zones;
3. on a let-go `focus` only, sends the owner its own clipboard when something other than a
   `clipboard` message changed it while that owner drove (section 5, the clipboard);
4. when it ended in any way but a let-go `focus` and the link is still up, sends
   `refuse {route, why}` on it (`not_allowed` when `allow_drive` was turned off, `sent_home` when
   it was ended by force), and from then on drops that link's input, still acknowledging it,
   until a `focus` of its is accepted again.

The owner, for its part, sends `keyup` and `mouseup` for everything it pressed and has not
released on that link before the `focus` that lets go, and swallows the physical releases that
follow. The other way round, a key or button pressed on the owner while its input was at home is
released at home: its release, and nothing else of it, stays there when it comes after the input
has left. The responder's release is the backstop for a link that dies or an owner that misbehaves.

**Per link.** Each responder link keeps its own `seq` count, its own ACK timer, its own last
accepted `route`, and its own record of keys and buttons held. Each initiator link keeps its own
`seq` count, its own unacknowledged sends and round-trip samples, and its own record of keys held
down on that peer. A machine's route counter is one.

**`accepts`**, responder to initiator, `{accepts: bool}`: sent whenever `allow_drive` for that
initiator changes, so an owner or a phone knows at once rather than at its next link.

**A machine that is driven does not drive.** When a desktop that is owned is asked to send its
own input elsewhere (its shortcut, its edge, a menu), it sends its owner home (section 5) and
waits up to 1 second for the owner to let go, ending the ownership by force if it does not; then
the move it was asked for goes ahead. A desktop whose own input is on another machine refuses to
be taken (`busy`), counting from the moment it sends a taking `focus` until that take is refused,
times out or is let go. Two desktops that try to take each other at the same moment are both refused
and both stay at home, and the next try works.

## 5. Routing

### `focus`, `accept`, `refuse` and `switch`

`focus`, owner to responder:

| Field | |
|---|---|
| `route` | integer, 1 to 2^53 - 1 |
| `target` | `b64`: where input is going. The responder's own id means here; any other id means the responder is being let go |
| `edge`, `offset` | where to land the pointer: `left`, `right`, `top` or `bottom`, and how far along that edge of the responder, any number, which the receiver clamps to 0.0 to 1.0 (never malformed for its range) |
| `resistance_px` | integer 0 to 1000: how far the owner's hand must push through a zone on the responder |
| `reach` | list of `b64`, at most 32, no repeats: the peers the owner can send input to now, less the owner and the responder |
| `stay` | `true`: re-arm in place; land nothing, play nothing |

Which fields each kind of `focus` has:

| Kind | `edge`, `offset` | `resistance_px` | `reach` | `stay` |
|---|---|---|---|---|
| take, by a zone | both | yes | yes | absent |
| take, by the shortcut or a menu | neither | yes | yes | absent |
| re-arm in place | neither | yes | yes | `true` |
| let go (`target` another machine) | neither | absent | absent | absent |

A take without `resistance_px` uses 120, and a phone, which has no hand at an edge, may leave it
out. A take or re-arm without `reach` has an empty one. A `reach` that names the owner or the
responder has those ignored. `edge` without `offset`, `offset` without `edge`, or `stay` with
anything but `true`, is malformed; on a re-arm `edge` and `offset` do not belong and are ignored.

`accept`, responder to the initiator whose taking `focus` it accepted: `route`.

`refuse`, responder to an initiator whose taking `focus` it did not accept: `route`, and `why`,
one of `"owned"`, `"not_allowed"`, `"busy"`, `"sent_home"` and `"malformed"`. A valid `route` is
all a `refuse` needs: one whose `why` is missing, not a string, or another string is not
malformed, and the initiator acts on it at once as a refusal it cannot name.

`switch`, responder to its owner only:

| Field | |
|---|---|
| `route` | the last `route` the responder accepted from this owner |
| `next` | `b64`: where input should go. The owner's own id means home |
| `edge`, `offset` | where to arrive on `next`, both or neither, as in `focus` (`offset` clamped, never malformed for its range) |

### The route

The owner keeps one route number for all its links, 0 when it starts. It raises it by one every
time it moves its input or re-arms in place, and every `focus` of that move carries the new
number: the `focus` letting go of the machine it leaves and the `focus` taking the machine it
goes to carry the same route. The owner remembers which machine its input is on (none, when at
home), the current route, and the `reach` it last sent there.

The owner acts on a `switch` only when it comes from the machine its input is on and carries the
current route. Anything else is ignored: it was sent before a move that has already happened, so
acting on it would move input a second time. One exception: a `switch` whose `next` is the owner,
from the machine its input is on, is honoured when its route is at least that of the take that
began the current visit, so a machine that sends its owner home while a re-arm is crossing it is
not left holding the input, and a send-home left over from an earlier visit still is not obeyed.
What the owner does with a `next` it cannot follow is below.

The responder ignores a `focus` whose `route` is not higher than the last it accepted on that
link (section 4). Routes on different links are not compared.

### Input comes home

One procedure, used everywhere below: the owner raises the route, sends the machine its input
was on `focus {route, target: owner}` if that link is still up, leaves its own pointer where it
was captured unless an `edge` and `offset` say where to land it, and swallows the releases of
keys it had sent there. A phone simply stops driving. It is used when:

- a `switch` names the owner;
- the owner's own shortcut, menu or edge brings it home;
- a `refuse` arrives for the current route from the machine its input is on, or a take from home
  has had no answer for 1 second;
- the link to that machine ends for any reason, a send that fails included, or its watchdog
  fires (no byte for 2 seconds).

Each says why, naming the machine: "*name* is being driven from another machine", "*name* does
not accept input from this one", "*name* is driving another machine", "*name* sent the input
back", "Lost the link to *name*".

### Moving input

**From the owner's own screen, from home, or by its own shortcut or menu.** A desktop's pointer
pushes through one of its zones (section 8) to peer P, or the shortcut or a menu picks P, whether
the input is at home or on another machine B. The owner raises the route. When the input is on
B, it first lets go of B: B's releases, then `focus {route, target: P}` to B. Then it sends P the
taking `focus`, then its clipboard, then input, without waiting: a refusal sends the input home,
not back to B, and whatever reached P in the meantime was dropped there. No answer within 1
second also sends it home. A phone picking a machine does the same.

**Onward from the machine being driven.** While owned, a responder watches its own pointer, as
version 5's return edge did. It arms each of its own zones whose peer is the owner or is in the
owner's latest `reach`, at the owner's `resistance_px`, and nothing else. When the pointer pushes
through a zone to peer N, it sends the owner `switch {route, next: N, edge, offset}`, with `edge`
and `offset` where that zone lands the pointer on N. It then disarms its zones and waits; input
that arrives in the meantime is injected. The zones cannot tell the owner's motion from the
machine's own mouse, so a local push through a zone sends the owner onward too, as in version 5.

**What the owner does with that `switch`**, input being on B:

- `next` is the owner: input comes home, landing at `edge` and `offset`.
- `next` is a peer N that was in the `reach` last sent to B, and the owner still has a live link
  to N and may send to it: the owner raises the route and sends N the taking `focus`. It holds
  its input, sending it nowhere, until N answers. B is not let go yet, so it keeps the input if
  N does not take it.
  - On `accept`, it sends B `focus {route, target: N}`, then N its clipboard and the held input.
  - On `refuse`, or no answer within 1 second, it raises the route again, sends B
    `focus {route, target: B, stay: true, resistance_px, reach}` with a fresh `reach` that leaves
    N out for the next 3 seconds, sends B the held input, and says N could not be reached.
  - Anything else that moves the input while it waits (a send-home from B, a lost link to B, the
    owner's own shortcut or menu) ends the wait and cancels its timer.
  - **A take given up closes N's link.** When the wait ends with no answer from N, by its 1
    second or by anything above, the owner closes the link it opened to N before it sends
    anything else, then goes on as that event says. N may have accepted already with its
    `accept` held up on the path; the close ends that ownership (section 4), so N is never left
    owned by an owner that drives elsewhere. N is out of `reach` until its link is up again, and
    the owner acts on nothing more that arrives on the closed link, an `accept` it had already
    read included. The one exception is the owner's own shortcut or menu picking N itself: that
    take follows the first on the same link, and its answer is the one awaited.
  - It ignores an `accept` whose route is not that of a take it sent. An `accept` for a take
    from home or by the shortcut that comes after its wait ended is answered with a let-go
    `focus` carrying the owner's current route and naming wherever its input is now (its own id
    when home); that take's own let-go has gone on the same link already, so this one changes
    nothing.
- `next` is anything else (outside that `reach`, or a link that has dropped since): the same as a
  refusal from N, without asking N.

**Keeping `reach` true.** When the set of peers the owner can send to changes while its input is
on a machine (a link comes up or drops, an `accepts` arrives, or a peer left out after a hand-over
that failed has had its 3 seconds), it raises the route and sends that machine a `stay` with the
new `reach`. While it waits on a hand-over it sends none; the `stay` or the take that ends the
wait carries the reach as it is then, and one more `stay` follows an `accept` if the reach for
the new machine changed during the wait.

**Sent home from the driven machine.** A responder's own user can always send the owner home:
its shortcut, a menu, a direction switch turned off, or Windows locking (as version 5). It
disarms its zones and sends `switch {route, next: the owner}` without `edge` or `offset`, and the
owner's input comes home. If the owner has not let go within 1 second, the ownership ends by
force (section 4).

### Chains need a direct link from the input to every machine

A to B to C works like this, with the keyboard on A: A drives B; B's pointer reaches B's zone for
C; B sends `switch {next: C}` to A; A takes C and lets go of B. A needs its own link to C. B never
relays A's input: it would sit in the path of every keystroke meant for C and would need a second
layer of encryption. So a chain is a full mesh of pairings from wherever the input is: three
desktops take three pairings, and a phone that drives all three takes three more.

Because each machine configures only its own zones, and each zone names only the peer beyond it,
no machine needs the whole layout. The owner follows `switch` and needs no model of the screens,
which is what lets a phone drive a chain.

### What the Crossing page may offer

A machine M can offer a zone to peer P when M is paired with P. It can check the rest from the
`paired` lists it holds: for each machine that may hold input while M is driven (M itself, and
every peer with `allow_drive` true), whether it is paired with P. The first design let M offer
a zone only when every one of them is. That is open question 3, built as its recommendation in
1.5.0: `reach` already stops a zone firing for an owner that could not follow it, so the zone is
offered, and the page names each machine that may drive M, has linked, and is not paired with P.
A zone never leads to a phone.

### Which machine the shortcut picks

The shortcut, a Send button and a menu's own item with no machine named send input to the machine
it was last on, while that one can take it (its link is up and it accepts input); else to the
first in the list of peers that can; else to the first this machine sends to at all, so the
refusal or the wake names it. Never to a phone, or to an entry with no id yet. A menu item that
names a machine sends input there, from home or straight from the machine it is on.

### The clipboard follows the input

- When an owner takes a machine, it sends its own clipboard after the taking `focus` (in a
  chain, after the `accept`) and before any input, unless that machine is known to hold it
  already: the clipboard went there or came from there, and has not changed here or come from
  another peer since. So something copied on B and brought home reaches C when C is taken next,
  and is never sent back to B.
- When a machine is let go by a `focus`, it sends its clipboard to the owner only when something
  other than a `clipboard` message changed it while that owner drove, so what the owner brought
  is never echoed back. It never sends one otherwise, so a paired device cannot
  collect a clipboard by taking and letting go of an idle machine.
- An initiator takes a clipboard only from a machine it has let go of, once per let-go, when its
  frame begins (its first length byte arrives, section 2) within 10 seconds of that let-go; any
  other is dropped. Like any frame it then has 30 seconds to arrive whole. A later move (a refusal
  from the next machine, say) does not cost the machine let go its one clipboard. It sets it on
  itself (a desktop) or keeps it (a phone), and sends it on to the machine its input is on now, if
  any. That is how something copied on B reaches C in a chain, through A, and it leaves it on A's
  clipboard, as it would if the input had come home.
- In a chain, B's clipboard reaches C a round trip after C was taken, so a paste in that moment
  gets C's older clipboard. Holding the input for it would add that round trip to every crossing
  in a chain; version 6 does not.
- Version 5 sent the clipboard before `focus`. Version 6 sends it after, because a responder takes
  a clipboard only from its owner; the input that follows it on the same link is still behind it.

### A phone

- A phone is an initiator only. A desktop's entry for a phone has `port` 0, `host` `""` and
  `send` false, and is never dialled; its `allow_drive` is the switch that lets the phone drive.
- A phone's entries for desktops have `send` true and `allow_drive` false. It learns from each
  `welcome`'s `accepts` which desktops it may drive, and leaves the others out of `reach`.
- A `switch` whose `next` is the phone means stop: the phone lets go with
  `focus {target: phone}` and drives nothing until the user picks a machine. `edge` is ignored.
- A phone may leave `resistance_px` out; the desktop's zones then push at 120.
- A phone that cannot hear beacons finds a desktop whose address changed by scanning its QR
  again (section 6).

## 6. Pairing in 1.5.0 (pairing version 3)

The exchange is PAIRING.md's, CPace with the code as the password, with these changes:

1. **Version and labels.** Beacons say `pairing: 3`, `pair_start` says `v: 3`, CI is
   `b"beamer-pair-v3"`, and the token's HKDF info is `b"beamer-pair-token-v3"`. The rest of the
   computation, the four messages, rules 1 to 10 and the encodings are PAIRING.md's.
2. **Identities.** Each side's identity is its 16-byte machine id: `requester_id` in ADa and
   `host_id` in ADb. A `pair_start` or `pair_answer` whose `id` is not exactly 16 bytes, or is
   all zero, is refused.
3. **What the exchange proves.** `pair_start` and `pair_answer` each gain `platform` and `port`
   (the sender's link port, 0 for a phone), and both go into the associated data:

   ```
   ADa = lv_cat(b"requester", requester_id, requester_name as UTF-8, requester_platform as ASCII, requester_port as 2 bytes big-endian)
   ADb = lv_cat(b"host",      host_id,      host_name as UTF-8,      host_platform as ASCII,      host_port as 2 bytes big-endian)
   ```

   So everything pairing saves about the other machine is proved, except its address.
4. **Checking the identity received**, as the CPace draft's section 10.1.1 requires:
   - The requester, once `Tb` checks out, refuses when the host's id is its own or belongs to any
     of its peers, except as the replacement below allows. It sends `pair_confirm` with `tag: ""` and `error: "known"`,
     once, stores nothing, and says the machine is already paired, naming the entry and when it
     was paired.
   - The host, once `Ta` checks out, refuses the same way: `pair_done` with `ok: false` and
     `error: "known"`, and the code is dropped. A `pair_confirm` carrying `error: "known"` is
     unauthenticated, so it cannot be told from a wrong code ended the same way; the host's window
     says "That machine may already be paired here, or the code was wrong."
   - An entry that has never linked (`linked` false), was made by pairing less than 10 minutes
     before, and has the same name and platform as the new pairing proves, is replaced by it
     rather than refused, so a pairing whose `pair_done` was lost can be done again at once.
     Any other match is `known`.
   - The entry migrated from 1.4.x (`from_1_4` true), which never links (section 1), is replaced
     by a pairing that proves its id, when an earlier beta linked it and so learnt one; and, when
     it has `id: ""`, by a pairing that completes with the machine at its saved `host` (the
     address `pair_start` came from, for the host; the address it reached, for the requester),
     or that proves its `name` and `platform`. It is replaced, never counted, under the rule in
     item 5. A pairing with the machine at its saved `host` that proves the entry's `platform`
     takes over its `side`, `side_set_at`, `side_by` and zones: every 1.4.x upgrader pairs again,
     since no 1.4.x token migrates, and is at the screen pairing that machine on purpose, so the
     arrangement should not have to be set twice. A match by name alone takes nothing, and the
     entry's zones go with it: pairing proves the code, not which machine that entry was, and
     anyone who saw the code can claim a name, which is safe only because the entry never links
     and nothing of it carries over.
   - To pair two linked machines afresh, the user removes each from the other's list first.
     Re-pairing never replaces a linked peer's token.
   - An id proves only that it arrived unaltered, not that the machine sending it owns it.
     Someone who sees a code can pair under another machine's id and name, which that machine's
     own pairing will then find `known`. The window naming the entry and its date is how the user
     tells.
5. **Storing.** The host writes the new peer entry before it sends `pair_done`, and sends
   `ok: false` with `error: "refused"` if the write fails, so `Td` now proves the host saved the
   token. The requester writes its entry after `Td` checks out. Each side first computes the new
   token's key id (section 2): when it is already one of its entries' key ids, the host sends
   `ok: false` with `error: "refused"` and the requester stores nothing and says pairing failed.
   With a fresh 256-bit token that never happens; the check keeps key ids unique whatever the
   entries hold. A clash the requester finds after `Td` leaves the host holding an entry that
   never links, which is the lost-`pair_done` case above: pairing again within 10 minutes
   replaces it.
6. **Beacons.** A beacon carries `platform`, and carries `id` (`b64`) only while a code is up, so
   a machine does not broadcast a stable id on every network it joins.
7. **The QR's secret.** With every code it shows, the host draws 16 random bytes, the code's
   **QR secret**, shown only inside the QR (below) and dropped with the code. A requester that
   read the code from a QR sends `qr: 1` in `pair_start`, and a requester that was typed the code
   sends no `qr`. The host takes the password from `pair_start` alone:

   | `pair_start` | PRS |
   |---|---|
   | no `qr` | the code, six ASCII digits (6 bytes, as PAIRING.md) |
   | `qr: 1` | the code's six ASCII digits followed by the QR secret's 16 raw bytes (22 bytes) |

   `qr` with any value but the integer `1` makes `pair_start` malformed. Nothing else changes: the
   generator's zero padding follows PRS's length by PAIRING.md's formula (`z` is 95 for the
   22-byte form), and `qr` is not in ADa, since a `qr` stripped or added on the path makes the
   two passwords differ and the exchange fail at `Ta` or `Tb`, which costs the code (rule 1) and
   nothing else.

   What it buys: an exchange that uses the QR secret gives someone on the path no useful guess,
   where the code alone gives one in a million a side. It does not close the typed path: while
   a code is up, anyone who guesses it may still try one `pair_start` without `qr`, with the same
   one in a million as before, and rule 1 then drops the code.

New errors:

| `error` | In | When |
|---|---|---|
| `known` | `pair_done`, `pair_confirm` | the other side's id is already one of this machine's peers' (bar the replacement in item 4), or this machine's own |
| `full` | `pair_answer` | the host reached 32 peers while this code was up; the code is dropped |

A host with 32 peers shows no code, and a requester with 32 does not start. That holds when one
of the 32 is an entry a new pairing would replace (a lost `pair_done`, or the one migrated from
1.4.x): the user removes a peer first. A known limit, left as it is. Pairing versions 2
and 3 never pair: a 1.5.0 requester shown a `pairing: 2` beacon says the other machine runs an
older Beamer before sending anything; a 1.5.0 host answers a `v: 2` `pair_start` with `version`
and drops its code; a 1.4.3 requester shown a `pairing: 3` beacon says the versions differ. This
protects a 1.4.3 machine from having its one pairing overwritten by a 1.5.0 requester that could
not then use it.

After pairing, each side's peer entry holds the other's `id`, `name`, `platform` and `port` as
the exchange proved them, the token, `paired_at`, `linked` false, `send` and `allow_drive` true
(for a phone, as section 5 says), `side` empty, and `host`: the requester saves the address it
reached the host at, and the host the address `pair_start` came from, unless the requester's
`port` is 0.

### Either end

Every desktop both announces and discovers. Each sends a beacon every 2 seconds to UDP 24821
while it runs, with `pair` and `id` only while a code is up, and each keeps the list of machines
it hears. Any desktop can show a code (and be the host) or enter one (and be the requester).

**Finding a peer whose address changed.** When the link to a peer with `send` true has been down
for 10 seconds, a desktop tries a link at the source address of a beacon carrying that peer's
exact name, at the peer's saved `port`, and saves the address only once that link authenticates.
It never tries a beacon with another name, nor one whose `id` is not the peer's; one at another
paired machine's saved address it tries after the rest, since two machines may have swapped
addresses. With several such beacons it tries them in turn. It tries at most one address per peer every 30 seconds,
and shows nothing when a try fails. A forged beacon can therefore cost one failed connection attempt every 30 seconds, never
a saved address and never a message that prompts the user to pair again.

### Pairing over TCP, from a code

Broadcast does not cross subnets or VPNs, and an iPhone needs an entitlement Apple grants on
application to send or receive it. So a host also accepts the exchange over TCP, on port 24821,
while a code is up and only then.

- **Framing.** Each message is a 4-byte big-endian length, 1 to 1024, then the same JSON object a
  datagram would carry, `beamy: 1` included. The desktops write it compact, with every character
  outside ASCII escaped as `\uXXXX`; a receiver accepts any spelling of the same JSON, since the
  exchange binds the decoded name and id, never these bytes.
- **Order.** The requester connects. The host sends its `beacon` at once (with `pair` and `id`);
  the requester checks it says `pairing: 3` and sends `pair_start` with that `pair`; the host
  answers `pair_answer`; the requester sends `pair_confirm`; the host sends `pair_done` and
  closes. A side that refuses sends its refusal first (`pair_answer` or `pair_done` with
  `ok: false`, or `pair_confirm` with `tag: ""`), then closes. Anything out of order, or a second
  `pair_start`, closes the connection with nothing sent.
- **Limits.** A connection has 10 seconds from being accepted to finish. The host holds at most
  four such connections at once and one per address, on the same terms as the link's handshakes.
  The requester's connect times out after 3 seconds.
- **One exchange per code, across both transports.** The UDP and TCP paths share the one pairing
  host, so rule 1 holds whichever way the first `pair_start` came. Rules 2 to 10 hold unchanged;
  the requester's memory of failed codes (rule 3) covers codes it sent either way.
- **The exchange under way is left alone.** Once a `pair_start` has bound the code, the host
  takes `pair_confirm` only from the address that `pair_start` came from (over TCP, only on its
  connection), and ignores any other without an answer; and the connection that bound the code
  is never the one a new connection replaces.
- **Checking and storing are one step.** A machine may host one pairing and request another at
  once. Each side makes its `known`, key id and 32-peer checks and writes its entry under one
  lock shared by both roles, so two pairings finishing together cannot pass 32 peers or leave two
  entries with one id. The host answers a count reached since `pair_start` with `pair_done`,
  `ok: false`, `refused`.
- **The fixed answer time** of 50 milliseconds applies as it does over UDP.

**The code on the screen.** While a code is up, each desktop's pairing sheet shows it as six
digits with this machine's address, and as a QR code of this text:

```
beamer://pair?host=<address>&port=<port>&code=<code>&k=<secret>
```

- `host` is a dotted IPv4 address, since the link is IPv4 only: the machine's address in a private
  range (10/8, 172.16/12, 192.168/16) when it has one, preferring the interface of its default
  route, and otherwise the default route's.
- `port` is 24821 in decimal. It is carried so a later version can move it. A number has one
  spelling, decimal digits with no sign and no leading zero, and so has each octet of `host`.
- `code` is the six digits.
- `k` is the code's QR secret as `b64`: exactly 22 characters, one spelling (Encodings).
- The four keys come in that order, lower case, each once, with nothing else. A reader refuses
  any other text, a QR without `k` included. The text is read as it is, with no percent-decoding.
  The pairing sheet never shows `k` as text.

A desktop can pair the same way by typing an address and a code: "Pair by address". That path
has no QR secret and sends no `qr`.

A phone that scans the QR of a desktop it is already paired with (the `id` in the host's TCP
beacon is a peer's) does not pair: it closes the pairing connection, tries its link at that
address, and saves the address once the link authenticates. That needs someone at the desktop to
show a code; finding a moved desktop without that (Bonjour, say) is left to the phone apps.

## 7. Keys

### Names

A key travels as `key`, a name, in `keydown` and `keyup`:

- **A character key** sends the character its layout types, as a string of exactly one code
  point, with Shift applied: `"A"` with Shift down, `"a"` without. While Command, Control or Alt
  (not Shift) is held, it sends the key's own unshifted character from its layout instead, so
  Option-V on a Mac is `"v"` and not `"√"`, and a German keyboard's Command-Z is `"z"`. Numpad
  keys send their character (`"1"`, `"+"`).
- **`us`** is sent beside a character: the unshifted, lower-case character the same key position
  types on a US layout, for a receiver whose layout cannot type `key`. A key with no US
  character (on a JIS keyboard, say) sends none.
- **Any other key** sends one of these names:

  | Group | Names |
  |---|---|
  | Modifiers | `shift`, `shift_r`, `ctrl`, `ctrl_r`, `alt`, `alt_r`, `cmd`, `cmd_r`, `caps_lock` |
  | Editing and movement | `backspace`, `tab`, `enter`, `esc`, `space`, `delete`, `insert`, `home`, `end`, `page_up`, `page_down`, `left`, `right`, `up`, `down` |
  | Function | `f1` to `f24` |
  | Windows keys | `menu`, `print_screen`, `pause`, `num_lock`, `scroll_lock`, `browser_back`, `browser_forward` |
  | Media (`media_keys`) | `volume_mute`, `volume_down`, `volume_up`, `media_next`, `media_prev`, `media_stop`, `media_play_pause` |

  The numpad's Enter is `enter`.
- **`keyup`** carries the same `key` and `us` as the `keydown` it releases, even if Shift changed
  in between. Auto-repeat is more `keydown`s with no `keyup` between them. `caps_lock` is sent as a
  `keydown` and a `keyup` together on each press.
- **On the receiver**, `ctrl` is Control, `alt` is Option or Alt, and `cmd` is Command on a Mac,
  the Windows key on Windows and Super on Linux. It types a character as given, pressing or
  releasing Shift around it as its own layout needs, and falls back to `us` when its layout has no
  key for the character. A name it cannot press is dropped.
- **A phone** has no physical keys to map. It shows a modifier row named for the platform it is
  driving and sends names directly: Command-C to a Mac is `keydown cmd`, `keydown "c"` with `us`
  `"c"`, `keyup "c"`, `keyup cmd`.

### Which physical key sends which name

The machines fall in two families: the Mac (`macos`, `ios`), and the PC (`windows`, `linux`,
`android`, and any unknown platform). Between two machines of the same family every key goes out
as itself. Between the two families, the sender applies its user's saved style:

| Physical key on the sender | Same family | Across, Semantic (the default) | Across, Positional |
|---|---|---|---|
| Mac Control | `ctrl` | `cmd` | `ctrl` |
| Mac right Control | `ctrl_r` | `cmd` | `ctrl` |
| Mac Command | `cmd` | `ctrl` | `cmd` |
| Mac right Command | `cmd_r` | `ctrl` | `cmd` |
| Mac Option | `alt` | `alt` | `alt` |
| Mac right Option | `alt_r` | `alt` | `alt` |
| PC Control | `ctrl` | `cmd` | `ctrl` |
| PC right Control | `ctrl_r` | `cmd_r` | `ctrl_r` |
| PC Windows or Super | `cmd` | `ctrl` | `cmd` |
| PC right Windows or Super | `cmd_r` | `ctrl_r` | `cmd_r` |
| PC Alt | `alt` | `alt` | `alt` |
| PC right Alt or AltGr | `alt_r` | `alt_r` | `alt_r` |

- Semantic makes the everyday shortcuts line up: Command-C on a Mac arrives as Control-C on a PC,
  and Control-C on a PC as Command-C on a Mac. Positional keeps each key in its place.
- The Mac's rows across families are 1.4.x's `DEFAULT_KEY_MAP` and `LEGACY_POSITIONAL_KEY_MAP`
  as they stand, sides folded. The PC's are Windows' capture names and 1.4.x's `POSITIONAL_SWAP`.
  Same family is new: the Mac as itself with its sides kept (the first design named the folded
  positional map), and Windows' capture swapped back, since the Windows receiver presses the Windows key for
  `cmd`.
- The saved style is the Mac's `key_map` (a style name, or a map a user wrote by hand, which
  applies across families only) and Windows' `modifier_style`. Both survive the migration. A
  Linux app keeps `modifier_style` as Windows does.
- A Linux sender reads keys by evdev code: `KEY_LEFTCTRL` and `KEY_RIGHTCTRL` are PC Control,
  `KEY_LEFTMETA` and `KEY_RIGHTMETA` Windows or Super, `KEY_LEFTALT` Alt and `KEY_RIGHTALT` right
  Alt or AltGr. A Linux receiver presses the same codes for the same names.

## 8. Zones

A zone is part of this machine's screen that leads to one peer:

```json
{"peer": "<b64>", "kind": "edge"}
{"peer": "<b64>", "kind": "part", "parts": ["start", "middle"]}
{"peer": "<b64>", "kind": "corner", "corner": "top_right", "edge": "right"}
{"peer": "<b64>", "kind": "notch"}
```

Any zone may also carry `"off": true`, which keeps it in the list and out of use. `peer` is `""`
only for the entry migrated from 1.4.x, which never links. A zone never names a phone.

- **Which edge a zone crosses.** An `edge` zone is the whole of its peer's `side`, and a `part`
  zone is some of its thirds (`start`, `middle` and `end`, top to bottom or left to right), so
  changing the side moves them. A `corner` zone names its corner and the edge it counts as
  crossing, one of the two that corner touches. A `notch` zone is the Mac's notch and crosses the
  top edge. These are 1.4.x's rules: its corner and notch could lead to the PC through an edge
  other than the one facing it, and a zone keeps that. The migration never changes where a
  pointer lands, so it writes what 1.4.x did: a Mac's corner zone crosses by the corner's own
  left or right edge whatever the side (the way home, which is not a zone, uses the side), and a
  PC's corner zone crosses the side (1.4.x built its corner push with the return edge), or the
  corner's own left or right edge while no side is set. An app with no control for a corner's
  edge writes the same again on every save.
- **Where the pointer lands.** At the peer's edge opposite the one crossed, at the fraction along
  it where the pointer left. A `corner` zone lands at the end of that edge nearest the corner: 0.0
  for a top or left end, 1.0 for a bottom or right one.
- **What they measure.** The thirds and corners are measured as 1.4.x measures them
  (`return_edge.py`): a third against the display the pointer is on, a corner in an 8-pixel box
  that wins over the edge it sits in.
- **Order and overlap.** Corners are checked first, then edges, then parts, then the notch, and the
  first that holds the pointer wins. Apart from a corner inside an edge, two zones in use never
  cover the same stretch: the settings refuse a list where they would, and an `arrangement` that
  would make two overlap is applied and turns the clashing zones for the sender `off`, with a
  notice naming both machines. A zone's stretch is its peer's `side` for an `edge` zone, the
  chosen thirds of it for a `part` zone, and the corner for a `corner` zone; the `notch` is a
  stretch of its own, which only another `notch` zone covers: it fires on a dwell and 1.4.x let
  it stand beside the edge, but a Mac has one notch, so it leads to one machine. 1.4.x let the
  edge and the thirds both be on, where the edge covered the thirds, so the migration and a
  save of those methods write the thirds `off`. Two machines may share one side by thirds.
- **Every machine starts with its whole edge.** Pairing gives the new machine an `edge` zone,
  and so does an `arrangement` for a machine that has no zone yet, and so does reading a
  settings file in which a paired desktop has none (pairing made no zones before 1.5.0-beta.5).
  An `edge` zone whose side another zone in use already covers is written `off`, so the file
  stays one the settings accept. A machine with a side and no way in, because another machine
  holds that side, is named on the Crossing page with the machine that holds it.
- **A peer with no zones** can still be reached by the shortcut and the menus.
- **The same list, both ways.** A machine's zones are where its own pointer crosses when its own
  hardware drives, and where a driven pointer is sent onward or home (section 5).
- **Never shared.** Zones and the direction switches belong to the machine that has them, and no
  message carries them. Only a pair's `side` is kept in step between the two, by `arrangement`.

**`arrangement`** says which of the sender's edges faces the receiver: `{edge, set_at, by}`.
`edge` is `left`, `right`, `top` or `bottom`; `set_at` an integer of unix seconds; `by` the
machine that made the change. The receiver sets its own `side` for the sender to the opposite
edge, and stores `set_at` and `by` as `side_set_at` and `side_by`, when the message is newer
than what it holds: a higher `set_at`, or the same `set_at` and the larger `by` compared as
bytes. It ignores one whose `set_at` is more than a day ahead of its own clock, or above
2^53 - 2. A change made here is stamped with the current time, and never lower than the stamp
held plus one. A side is never cleared by message. A machine with three peers keeps three sides,
each settled with that one peer.

`arrangement` may also carry **`way_back`**, a boolean: whether the sender has a zone in use, and
able to fire (an `edge` or `part` zone of a side that is set, a `corner` or a `notch`), that leads
to the receiver. The sender includes it every time it sends an `arrangement`: when the link comes
up, when it changes that machine's side or zones, and, once it has applied an `arrangement` from
that machine, in answer, so the machine that set the side learns at once that it was taken but
leads nowhere. The receiver keeps the last one it was told as `way_back` on the sender's entry,
whether or not the side in the same message was newer, and its Crossing page names the sender as
having no way back while it is `false`. An answer is sent only after an `arrangement` that changed
something here, so two machines never answer each other in turn. A version 6 machine from before
1.5.0-beta.5 ignores it, as it ignores any unknown key (Encodings).

**From 1.4.x's settings.** The migration turns the one pair's ways across into zones for its one
peer, one of each kind, with `off` set for each method that was not on: an `edge` zone; a `part`
zone with the saved thirds, `off` as well when the edge is on; a `corner` zone with the saved
corner, crossing the edge the rule above gives; and, on the Mac, a `notch` zone.
`shortcut` in the methods sets the machine's `shortcut`. So the one-machine Crossing page turns a
way across on and off by `off`, and finds the thirds and corner it had.

## 9. The round trip

Every `ack` says how long the responder held it:

| Field | |
|---|---|
| `seq` | the highest `seq` dealt with on this link, 0 before any |
| `held_us` | integer microseconds from dealing with that `seq` to sending this `ack`, 0 to 2^31 - 1; the sender clamps to that range |
| `locked` | `true` while Windows clears its lock screen and drops input; absent otherwise |

- An initiator ends the link on an `ack` that is malformed, whose `seq` goes backwards, or whose
  `seq` is higher than any it sent: the stream cannot be trusted.
- An `ack` whose `seq` is higher than the last one gives one sample:
  `round trip = (now - when that seq was sent) - held_us`, and 0 if that is below 0. An `ack` that
  repeats a `seq` gives none, and carries `held_us: 0`. The owner measures every link that
  carries its input, a phone included.
- **When the responder acknowledges**, timed from the last `ack` it sent on that link: input dealt
  with 15 ms or more after it is acknowledged at once; input dealt with sooner is acknowledged 15
  ms after it, in one `ack` for everything up to then; and with no input, an `ack` goes 0.4
  seconds after the last, as a heartbeat. 1.4.x held every `ack` 15 ms, so its figure read up to
  15 ms high; with `held_us` the figure is the network and the injection, whatever the timer.

**The jitter window's inputs.** Each initiator link keeps its samples, with the time each was
taken, for the last 30 seconds while it carries input. From them the app shows the median and
the spread and decides on the warning (Open questions, 4). It logs, every 60 seconds while input is
on that link, the count, the median, the 95th percentile, the largest and the mean `held_us`, so a
report can be read from the log.

## 10. Text and shared settings

**`text`** is a string the receiver types as it stands, for a phone's keyboard, where naming each
key would lose accents and emoji. The receiver advertises `text` only if it can type any Unicode
text.

- At most 4096 bytes as UTF-8. A longer one is dropped whole, and acknowledged.
- `\n` is typed as Return and `\t` as Tab. Every other character from U+0000 to U+001F, and U+007F
  to U+009F, is dropped.
- It is typed as characters, not keys, so the receiver's layout and any modifier the sender has
  down play no part. A sender does not send `text` while it holds a modifier on that link.
- It carries a `seq` and is acknowledged like any input.

**`settings`** is Same on all machines: 1.5.0's Same on both machines, extended from one peer to
every peer.

```json
{"type": "settings", "data": {
  "on": true, "set_at": 1790000000, "by": "<b64>",
  "crossing": {"resistance_px": 120, "block_while_dragging": true, "shortcut": true,
               "trigger_key": "alt_r", "trigger_style": "double_tap", "double_tap_ms": 300},
  "design": {"glow_style": "glow", "glow_colour": "signal", "effect_length": "normal",
             "shortcut_arrival": true, "shortcut_arrival_style": "match"}}}
```

- `on` is a boolean, `set_at` an integer of unix seconds, `by` the `b64` id of the machine that
  made the change, or `""`. A sender always sends all three. A receiver reads a missing `by` as
  `""`, and also reads a `by` spelt in standard base64 with padding, as the desktops do; a
  message missing `on` or `set_at`, or with any of the three of the wrong type or not an id, is
  malformed and ignored whole. `crossing` and `design` are objects, present only when `on` is
  true; one that is absent or is not an object brings no values, and the message stands.
- **Each value stands alone.** Unlike every other message, a value in `crossing` or `design`
  outside the table below, or one this receiver cannot hold (an effect it does not have), is
  left out and the rest still applied, and the message is not malformed for it; an unknown key
  is ignored. Whether a message is newer, and whether it is sent on, is decided by `set_at` and
  `by` alone: one with values left out still wins, takes its stamp, and is sent on as it came.
- The values:

  | Key | Allowed |
  |---|---|
  | `resistance_px` | integer 0 to 500 |
  | `block_while_dragging`, `shortcut`, `shortcut_arrival` | boolean |
  | `trigger_key` | a key name from section 7 that is not a character, a media key, `browser_back`, `browser_forward`, `backspace`, `tab`, `enter`, `esc`, `space` or `print_screen` |
  | `trigger_style` | `"double_tap"` or `"hold"` |
  | `double_tap_ms` | integer 50 to 2000 |
  | `glow_style` | `"glow"`, `"beam"`, or an effect id from `effects.py` |
  | `glow_colour` | `"signal"`, `"colourful"`, `"ocean"`, `"sunset"`, `"mono"`, or a colour pack id from `effects.py` |
  | `effect_length` | `"short"`, `"normal"` or `"long"` |
  | `shortcut_arrival_style` | `"match"`, `"locator"`, or an effect id from `effects.py` |

- Shared: the crossing's feel (resistance, the drag guard, the shortcut and its key) and the
  Design page. Never shared: zones, `side`, the direction switches, the Mac's notch look and
  haptics, and appearance. Version 5's `methods`, `edge_parts` and `corner` are zones now and
  leave the message, and with them goes 1.5.0's mirroring of the corner.
- **Newer wins**: a higher `set_at`, or the same `set_at` and the larger `by` as bytes. This
  replaces the Mac winning a tie, since there may be no Mac. A `set_at` more than a day ahead of
  the receiver's clock, or above 2^53 - 2, is ignored.
- **From and to whom.** A machine takes `settings` only from a peer whose capabilities list
  `settings`, and sends its state to every such peer after each handshake and on every change. One
  that takes a newer state sends it on, unchanged, to every other such peer it has a link to. An
  older or equal one is not sent on, so it stops. Turning it off travels the same way.
- A change made here is stamped with the current time, and never lower than the stamp held plus
  one, so a change on a machine whose clock is behind still wins.

## Versions

- **Wire version 6** is in Beamer 1.5.0 and later. It refuses versions 5 and below, and they refuse
  it; each side names the other machine to update.
- **Pairing version 3** is in 1.5.0 and later, and pairs only with itself.
- 1.4.x's settings are migrated once and kept, so going back to 1.4.x on both machines works.

## The vectors

The test vectors for version 6 and pairing version 3 are `core/tests/v6_vectors.json`, and
version 5's, kept byte for byte, `core/tests/v5_vectors.json`; the tests beside them replay each
one against the code and against the primitives written out by hand.

## Changes from version 5

- The preamble is 62 bytes and carries the pair's key id and an ephemeral X25519 share; the keys
  take the X25519 result beside the token and bind the key id, both shares and a direction byte
  in place of the prefixes' order, which gives forward secrecy; the salt is `beamer-link-v6`.
- `hello` and `welcome` carry each machine's id, checked there, and a `hello` naming the wrong id
  is answered `wrong_id`; a typed token, and any token migrated from 1.4.x, no longer links.
- The handshake is bounded as a whole (5 seconds, the preamble in 1) on both sides, where version
  5's initiator bounded each read at 2 seconds; one pending handshake per address.
- A responder keeps one link per peer and at most one owner, where version 5 kept one link and let
  the newest replace it. `focus` is answered with `accept` or `refuse`.
- `focus` and `switch` name machines by id, not `"mac"` and `"windows"`, and carry a route.
  `focus` no longer carries `return_edge`: the responder's own zones say where its edges lead,
  and the owner sends only its `resistance_px`.
- `hello` no longer carries `return_edge` and `resistance_px`; the arrangement travels only as
  `arrangement`, which names the sender's edge and a `by`, not `mac_edge`.
- `hello` and `welcome` carry the name, platform, app version and capabilities; the `settings: 1`
  flag became the `settings` capability.
- The clipboard follows `focus` instead of going before it, and goes back only when it changed
  under the owner that is leaving.
- `ack` carries `held_us`, and the responder acknowledges a lone event at once.
- Each initiator counts `seq` from 1 on every link; version 5's Mac carried it across reconnects.
- New messages: `accept`, `accepts`, `refuse`, `paired`, `text`.

## Changes from the first design

- **Taking input.** The first design let a `focus` in when it named "the current owner's own switch
  request by its route number". Version 6 has no such path: only a machine with no owner, or its
  own owner, can be taken, and a hand-over is the owner letting go.
- **Non-owners.** The first design dropped a non-owner's `focus`, `arrangement` and settings. Version 6
  answers its `focus` with `refuse`, so it can take its input home at once, and handles
  `arrangement` and `settings` from any peer, because they are between two paired machines rather
  than part of driving; dropping them would lose every change made while nobody drives.
- **The clipboard** is one per machine, not one per link; it follows the input through the owner.
- **Capabilities** gain `clipboard`, since a phone may take no clipboard at all.
- **A driven machine is not taken** (`busy`): the first design did not say what a machine whose own input
  is elsewhere does when taken.
- **Mac to Mac keys** keep their sides; the first design named 1.4.x's positional map, which folds them.
- **Pairing's associated data** gains platform and port, so the pairing code's AD construction
  changes too, not only the bytes fed in.
- **The id in the clear.** The first design put the sender's machine id in the preamble and both ids in
  the keys. Version 6 puts a per-pair key id there instead, with the ids sealed in `hello` and
  `welcome`, and adds forward secrecy (decided 1 and 2).
- **Pairing by QR** carries a second secret (decided 7).

## Decided

Decided on 30-09-2026, and 6 amended on 01-10-2026. The text above already follows these.

1. **A per-pair key id in the preamble, not the machine id.** The first 16 bytes of
   `HKDF-SHA-256(token, salt "beamer-link-v6", info "beamer-key-id")`; the machine ids travel in
   `hello` and `welcome` and are checked there; there is no empty-id lookup; key ids are unique
   across entries (section 2).
2. **Forward secrecy in version 6.** An ephemeral X25519 share in each preamble, the result in
   `ikm` and both shares in `info`; a zero result ends the handshake (section 2).
6. **No typed tokens, and no migrated ones.** Follows from 1: 1.5.0 has no token field, and a
   key id is an offline test of its token. The first decision let a migrated token of pairing's
   spelling link; since a typed token can have that spelling and 1.4.x kept no record of which
   it was, no migrated token links, and Overview asks the user to pair that machine again
   (sections 1 and 2).
7. **The QR's extra secret.** 16 random bytes per code in the QR as `k`; `qr: 1` in
   `pair_start` makes the password the code followed by those bytes (section 6).

## Open questions

Open. None touches the wire; the recommendation in each is the design 1.5.0 follows until it is
decided.

3. **The Crossing page and missing pairings.** The first design offered a zone to P only when
   every machine that may drive this one is paired with P. Adding a phone that is not paired with
   P would then take away a zone the desktops use. **Recommendation: offer the zone whenever this
   machine is paired with P, arm it only for owners whose `reach` includes P (section 5), and have
   the page name each machine that cannot follow it.**
4. **The jitter warning's threshold.** **Recommendation: warn when, over the last 30 seconds and at
   least 50 samples, the 95th percentile is 20 ms or more above the median**, and show the median
   and that gap in plain words.
5. **A peer that is already paired.** Pairing refuses a machine already in the list once it has
   linked, so to pair again after one side has removed the other, the user removes it on both.
   **Recommendation: keep the refusal**; the window says exactly which machine still has it.
