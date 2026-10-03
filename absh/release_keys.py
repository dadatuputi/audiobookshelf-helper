"""The keys `absh update` trusts to sign a release.

`absh update` installs a release only if its SHA256SUMS is signed by one of
these. The list is read from the copy being updated, before anything is
replaced, so a release can only ever be vouched for by a key its predecessor
already trusted - never by one it brings with it.

The private halves live with the maintainer, offline, and nowhere on GitHub:
not in the repository and not in an Actions secret, because a workflow can
read its secrets and a stolen secret would put us back where an unsigned
release leaves us. `python3 tools/sign_release.py keygen` makes a key and
prints the line that belongs here.

Rotating to a new key:

  1. Generate the new key and add its line here, beside the old one.
  2. Release that, signed with the old key. Copies that install it now trust
     both.
  3. Sign releases with both keys for a while (`sign --key OLD --key NEW`),
     so copies that skipped step 2 still verify against the old one.
  4. Remove the old line in a later release.

A copy that only knows a key that has since been dropped cannot verify
anything newer and says so; it has to be reinstalled by hand, which is the
same trust decision a first install makes. If a private key is ever exposed,
remove it here in a release signed by another key - and know that copies
which still trust it can be served anything signed with it, which no update
can fix for them.

An empty list is not "no checking": `absh update` refuses every release
until a key is here.
"""

KEYS = [
    # "ed25519:<base64 public key>",  # who holds it, when it was made
]
