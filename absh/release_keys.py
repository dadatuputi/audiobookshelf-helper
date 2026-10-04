"""The keys `absh update` trusts to sign a release.

`absh update` installs a release only if its SHA256SUMS is signed by one of
these. The list is read from the copy being updated, before anything is
replaced, so a release can only ever be vouched for by a key its predecessor
already trusted - never by one it brings with it.

The private half is the RELEASE_SIGNING_KEY Actions secret, and the release
workflow signs every build with it; the "Pin release-signing key" workflow
adds its public half here. That was the maintainer's choice, for a release
with no manual steps, and it has a cost worth stating: anyone who can run
workflows in the repository can read the secret and sign. A key held off
GitHub (`python3 tools/sign_release.py keygen`) closes that, and can be pinned
beside this one.

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
