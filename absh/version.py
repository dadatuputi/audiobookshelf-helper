"""Which release this copy came from.

A checkout says "dev", because it is not a release and pretending otherwise
would have `absh update` comparing a made-up version against a real one. The
packaged copy carries the tag it was built from: tools/package.py overwrites
RELEASE on the way into the archive, and asserts the stamp took, the same way
it already asserts the extension manifest's version stamp.

Kept in its own module so stamping it is a whole-file rewrite rather than a
regex over code that matters.
"""

RELEASE = "dev"


def release():
    """The release string, or "dev" for a checkout."""
    return RELEASE


def is_release():
    return RELEASE != "dev"
