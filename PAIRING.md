# Pairing

How two machines running Beamer agree a token from the six-digit code, exactly enough to write
a second implementation. The code is `core/pairing.py`, which both apps
import; `core/tests/pairing_vectors.json` holds test vectors for every step. What
pairing protects and what it does not is in [SECURITY.md](SECURITY.md).

Pairing is CPace, a password-authenticated key exchange, with the code as the password:
[draft-irtf-cfrg-cpace-21](https://datatracker.ietf.org/doc/draft-irtf-cfrg-cpace/21/), "the
draft" below. Nothing either machine sends can be tested against a list of codes, so whoever takes part
without the code gets at most one guess against each machine, and the guesses are limited by a
person pressing a button.

## The protocol

Suite: CPACE-X25519-SHA512, initiator and responder roles, with the explicit key confirmation
of the draft's section 10.4. The requester is the initiator (A); the host, which shows the
code, is the responder (B).

### Encodings

- **Datagrams.** One JSON object each, UTF-8, on UDP 24821, with `"beamy": 1` added to the
  fields shown below. At most 1024 bytes as encoded; a longer one is dropped unread. The
  messages in the vectors file are shown before `beamy` is added.
- **`b64`.** URL-safe base64 without padding, and only that spelling: a receiver decodes a
  field, encodes it again, and refuses the message if the text differs. Empty bytes are `""`.
- **`lv_cat(a, b, ...)`.** The draft's length-value concatenation: each string with its length
  in LEB128 before it (one byte for lengths under 128). `lv_cat` of one string still carries
  that string's length.
- **Hex in the vectors** is bytes in wire order. The two `elligator2` fields are the exception:
  integers printed big-endian, as RFC 9380 prints them.
- **`pair_id`** is 32 lowercase hex characters (16 random bytes) the host makes with each code
  and sends in its beacon. Everywhere it is used as those 32 ASCII characters, never decoded.
  A receiver ignores one that is empty, longer than 64 characters or not ASCII.
- **Names** in `pair_start` and `pair_answer` are at most 48 Unicode code points. A beacon's
  name is not held to that: it is only shown, cut to 64 for the list, and proves nothing. That is not UTF-16 units (Kotlin's and Java's
  `length`), not grapheme clusters (Swift's `count`) and not bytes. The sender cuts a longer
  name on a code point boundary before sending; a receiver refuses a longer one, and one that
  is not valid Unicode (a lone surrogate). Both sides hash the UTF-8 of the name exactly as the
  JSON carried it, with no normalisation. The `name_cut` vector pins the count.
- **Identities** are 0 to 32 bytes, and empty today. A later version feeds each machine's
  16-byte id and changes nothing else; the second exchange in the vectors is that shape. The
  exchange proves an identity was sent unaltered, not that it is the one wanted: a side that
  receives one must check it against what it expects, as the draft's section 10.1.1 requires.
  With empty identities there is nothing to check.

### Inputs

| CPace input | Value |
|---|---|
| PRS | the code, six ASCII digits |
| sid | `lv_cat(pair_id as its 32 ASCII characters, nonce)`, 50 bytes |
| CI | `b"beamer-pair-v2"` |
| ADa | `lv_cat(b"requester", requester_id, requester_name as UTF-8)` |
| ADb | `lv_cat(b"host", host_id, host_name as UTF-8)` |

- `nonce` is 16 random bytes from the requester, new for every attempt. With the host's
  `pair_id` the sid has fresh randomness from both ends, which is what the draft recommends.
- The host builds ADa from the `id` and `name` in `pair_start`; the requester builds ADb from
  the `id` and `name` in `pair_answer`. Each side builds its own from what it sent.
- The roles are the two fixed strings. They come first in ADa and ADb, so the two can never be
  equal, and a tag cannot be reflected at the side that made it.

### Computation

```
generator_string = lv_cat(b"CPace255", PRS, zero_bytes(z), CI, sid)
                   z = 128 - 1 - len(prepend_len(PRS)) - len(prepend_len(b"CPace255")) = 111 for a six-digit code
h        = first 32 bytes of SHA-512(generator_string)
r        = h as a little-endian integer, with bit 255 cleared
g        = elligator2(r) as 32 little-endian bytes
ya, yb   = 32 random bytes each, new for every exchange
Ya       = X25519(ya, g)            Yb = X25519(yb, g)
K        = X25519(ya, Yb)           = X25519(yb, Ya); refuse if K is 32 zero bytes (the host checks this before it computes g)
ISK      = SHA-512(lv_cat(b"CPace255_ISK", sid, K) || lv_cat(Ya, ADa) || lv_cat(Yb, ADb))
mac_key  = SHA-512(b"CPaceMac" || sid || ISK)
Ta       = HMAC-SHA-512(mac_key, lv_cat(Ya, ADa))
Tb       = HMAC-SHA-512(mac_key, lv_cat(Yb, ADb))
Td       = HMAC-SHA-512(mac_key, lv_cat(b"beamer-pair-done"))
token    = b64(HKDF-SHA-256(ikm = ISK, salt = sid, info = b"beamer-pair-token-v2", 32 bytes))
```

- **`elligator2(r)`**, with p = 2^255 - 19 and A = 486662, all arithmetic mod p (r may be p or
  more, and is reduced): `v = -A / (1 + 2 r^2)`; `e = (v^3 + A v^2 + v)^((p - 1) / 2)`; the
  result is `v` if `e` is 1, else `-v - A`. This is RFC 9380 section 6.7.1 with Z = 2, and the
  draft's appendix A.5. Only the u coordinate is used.
- **`X25519`** is RFC 7748's: the scalar is clamped and bit 255 of the incoming u is ignored.
  A platform's own X25519 does both. The draft's low-order vectors depend on the mask.
- **K all zero** is how a low-order share shows. Test the output. A library that throws on
  such a share instead of returning zeros (Node does, CryptoKit does for some) has made the
  same finding: treat the throw as a refusal.
- **The transcript** is the draft's `transcript_ir`: the initiator's part first, always.
- **`mac_key`** is used as the 64-byte HMAC key as it stands. The tags are the full 64 bytes.
- **HKDF** is RFC 5869 with the sid as the extract step's salt and the ASCII label as info.
  The token is the 43 characters of `b64` over its 32 bytes.

Everything down to `Tb` is the draft. `Td` and `token` are Beamer's. `Td` is the host's receipt
that it accepted the confirmation and holds the same token; it is sent before the host writes
its settings, so it does not prove that write succeeded. `token` is what both apps save and
what `protocol.py` derives each connection's keys from, unchanged.

### Messages

```
beacon        host, every 2 s   {type:"beacon", name, port, pairing:2, pair: pair_id while a code is up}
pair_start    requester         {type, pair, v:2, nonce: b64, share: b64(Ya), name, id: b64}
pair_answer   host              {type, pair, ok:true, share: b64(Yb), tag: b64(Tb), name, id: b64}
pair_confirm  requester         {type, pair, tag: b64(Ta)}
pair_done     host              {type, pair, ok:true, tag: b64(Td)}
```

A failure is `{type, pair, ok:false, error}` in place of `pair_answer` or `pair_done`:

| `error` | When |
|---|---|
| `not_pairing` | no code is up, the code has expired, or `pair` is not the current pairing id |
| `version` | `v` in `pair_start` is not 2; the code is dropped |
| `refused` | everything else: a malformed field (a nonce that is not 16 bytes, a share that is not 32, a name or identity over its limit), a share that gives a zero K, a second different `pair_start`, a tag that does not check out |

Errors are not authenticated: anyone on the LAN can already stop a pairing.

### Rules

These are what hold an attacker to one guess against each side, so a second implementation
keeps every one.

1. **A host answers one `pair_start` per code.** The first one binds the code. The same
   datagram again gets the same answer (UDP retransmits). A different one gets `refused`, and
   the exchange under way is left alone. Every answer would be another guess for its sender.
2. **A requester judges one `pair_answer` per share.** It takes the first `pair_answer` from
   the host's address that carries its `pair` and decides on it. It never waits for a better
   one: each answer it judged would be a guess for whoever is answering. A new attempt means a
   new nonce and a new scalar. The source address only sorts the replies; it proves nothing,
   and nothing may rest on it.
3. **A requester does not send a code again once an `ok:true` answer to it has failed.** Whoever
   sent that answer has had a guess at the code. The real host has by then bound the code to
   that attempt or dropped it, so sending it again cannot pair; it would only buy a second
   guess. For two minutes, longer than any code lives, the requester reports `refused` for
   that code without sending anything. It is the code that is remembered and not the pairing
   id, because a beacon with a new pairing id costs an attacker nothing. An `ok:false` answer
   tests no guess and is not remembered: an honest host sends one for a stale pairing id.
4. **The requester checks `Tb` before it sends `Ta`.** It refuses an `ok:true` answer when
   `ok` is anything but JSON `true`, a field is not the one spelling of `b64`, the share is
   not 32 bytes, the identity is over 32 bytes, the name is over 48 code points or not valid
   Unicode, K is zero, or `Tb` does not check out. It then sends `pair_confirm` with `tag: ""`,
   once, and reports `refused`. The host answers that with `pair_done`, `ok:false`, `refused`,
   drops the code, and its window says a wrong code was entered.
5. **The host stores the token only after `Ta` checks out.** A wrong tag ends the code.
6. **The requester stores the token only after `Td` checks out**, and records the host's name
   from `pair_answer`, which the exchange proved, not the one in the beacon.
7. **A share that gives an all-zero `K` is refused.** These are the low-order points; the
   draft's appendix B.1.10 lists them and the vectors file repeats them.
8. **No fallback.** A beacon without `pairing: 2` is never paired with; the requester says the
   two versions differ before it sends anything. A host answers a 1.4.2 `pair_request` with a
   refusal and drops its code, unless an exchange is already under way, which it leaves alone.
9. **Tags are compared in constant time**, and scalars are never reused.
10. **A code lasts 60 seconds**, bound or not.

### On the socket

- The host sends at most one refusal to an address every half second, as it already did for
  `find`. A requester that misses one asks again after its own 1.5 second timeout. A datagram
  whose `pair` is not shaped like a pairing id gets no reply at all, so a reply is never much
  larger than what drew it.
- The host sends a newly made `pair_answer` 50 milliseconds after the `pair_start` arrived,
  so the time the generator took cannot be read off the reply; making the answer takes about a
  thirtieth of that. Everything that can refuse a `pair_start` is checked before the code is
  used, so a refusal, which goes back at once, carries none of that time either. A retransmit
  is answered from memory, at once.
- The requester's list of hosts holds 64. When it is full, the one heard from longest ago makes
  way for a new one, except a host the user asked for by address, which always stays.
- The requester sends each datagram up to three times, 1.5 seconds apart, until the reply
  comes. It keeps only replies from the host's address that carry its pairing id, at most 64
  of them, and only while an attempt is under way.
- A datagram that does not parse, a nest of brackets included, is dropped, and nothing one
  datagram does ends the loop that read it.

### What it does not do

- It does not stop a denial. A junk `pair_start` uses up the code.
- It does not make the two machines' guesses one. Someone on the path can spend a guess against
  the host, as a requester, and another against the requester, as a host: two chances in a
  million for a code, not one. No exchange between two parties can do better than a guess a
  side.
- If every `pair_done` is lost, the host has stored the token and the requester has not. The
  requester reports no answer and the user pairs again, as with a lost reply in 1.4.2.
- The host's address, port and wake address are still taken from the beacon, which nothing
  authenticates. Something on the path can point the Mac at the wrong address; it cannot read
  or forge the link, whose keys come from the token.
- The map to the curve runs in Python integers, which are not constant time, where the draft
  asks for a constant-time map. PyNaCl exposes no Montgomery Elligator map; libsodium's
  `crypto_core_ed25519_from_uniform` clears the cofactor, which would give a different
  generator from the draft's and lose its vectors. The input is a hash of a code that is used
  once and lives a minute, and the host's answer leaves at a fixed time whatever the map took.
  The host computes it once per code and the requester once per attempt; on an Apple M1 it takes about 290 µs with 5.5 µs of spread, noise included, so there
  is one sample of a few microseconds' signal to time across a network.

## Versions

- This exchange is pairing version 2, in Beamer 1.4.3 and later. Version 1, in 1.4.2 and
  earlier, proved the code with an HMAC keyed from it and could be attacked offline; it is not
  spoken any more, in either direction.
- The link's wire protocol is not changed by it. Machines paired by an earlier Beamer stay
  paired, and the two can be updated one at a time; both need 1.4.3 to pair afresh.

## The vectors

`mac_app/tests/pairing_vectors.json`, with an identical copy in `win_app/tests`:

- `cpace_draft`: the draft's own X25519 vector, so the core is proved against the IETF's
  numbers before anything of Beamer's is added;
- `elligator2`: field elements and the u coordinates they map to, from RFC 9380 J.4.2;
- `low_order_shares`: the shares that must be refused, and five that must not be;
- `exchanges`: two complete exchanges with every intermediate value and all four messages, one
  with empty identities and one with 16-byte identities and a name outside ASCII and the BMP;
- `wrong_code`: an answer made under another code, which the requester must refuse and abort;
- `name_cut`: a 60 code point name and the 48 it is cut to.

No vector covers retransmission, expiry, or rules 2 and 3; `mac_app/tests/test_pairing_cpace.py`
and the loopback tests in `mac_app/tests/test_pairing.py` do.
