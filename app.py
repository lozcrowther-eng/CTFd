"""
Vercel entrypoint -- deliberately NOT wsgi.py (CTFd's own entrypoint). wsgi.py calls
gevent.monkey.patch_all() at import time, which exists for CTFd's gunicorn+gevent worker
model (many concurrent connections handled cooperatively in one long-lived process).
Vercel's native Python runtime never runs gunicorn/gevent workers at all -- it handles its
own concurrency -- so globally monkey-patching stdlib socket/ssl/threading here would be an
untested interaction with Vercel's own runtime internals for zero benefit. `app.py` is
Vercel's first-priority auto-detected entrypoint name, so this takes precedence over wsgi.py
without needing to touch CTFd's own upstream file.
"""
import sys
import types

# distutils was removed in Python 3.12. CTFd's own core files are patched directly (see
# CTFd/config.py, CTFd/__init__.py, CTFd/utils/updates/__init__.py) but several of CTFd's
# own pinned THIRD-PARTY dependencies still import distutils at module load time too --
# confirmed: flask_marshmallow==0.10.1 imports distutils.version.LooseVersion. Patching every
# old pinned package's installed files individually doesn't scale and won't catch ones we
# haven't hit yet; registering shim modules in sys.modules before anything imports them,
# backed by `packaging` (already a real dependency here), covers the whole tree in one place.
_Version = __import__("packaging.version", fromlist=["Version"]).Version


def _strtobool(value):
    value = str(value).lower()
    if value in ("y", "yes", "t", "true", "on", "1"):
        return 1
    elif value in ("n", "no", "f", "false", "off", "0"):
        return 0
    raise ValueError(f"invalid truth value {value!r}")


class _DistutilsVersionShim:
    """Covers both the comparison behavior AND the `.version` tuple attribute old callers
    reach for directly (confirmed: marshmallow==2.20.2 does
    `tuple(LooseVersion(__version__).version)` at import time) -- a bare alias to
    packaging.version.Version breaks on that attribute access, since Version uses `.release`
    instead."""

    def __init__(self, vstring):
        self.vstring = str(vstring)
        self._parsed = _Version(self.vstring)
        self.version = self._parsed.release

    def __str__(self):
        return self.vstring

    def __repr__(self):
        return f"{type(self).__name__}({self.vstring!r})"

    def _cmp_value(self, other):
        return other._parsed if isinstance(other, _DistutilsVersionShim) else _Version(str(other))

    def __eq__(self, other):
        return self._parsed == self._cmp_value(other)

    def __lt__(self, other):
        return self._parsed < self._cmp_value(other)

    def __le__(self, other):
        return self._parsed <= self._cmp_value(other)

    def __gt__(self, other):
        return self._parsed > self._cmp_value(other)

    def __ge__(self, other):
        return self._parsed >= self._cmp_value(other)

    def __hash__(self):
        return hash(self._parsed)


_distutils = types.ModuleType("distutils")
_distutils_version = types.ModuleType("distutils.version")
_distutils_version.StrictVersion = _DistutilsVersionShim
_distutils_version.LooseVersion = _DistutilsVersionShim
_distutils_util = types.ModuleType("distutils.util")
_distutils_util.strtobool = _strtobool
_distutils.version = _distutils_version
_distutils.util = _distutils_util
sys.modules.setdefault("distutils", _distutils)
sys.modules.setdefault("distutils.version", _distutils_version)
sys.modules.setdefault("distutils.util", _distutils_util)

from CTFd import create_app

app = create_app()
