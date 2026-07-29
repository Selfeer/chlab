"""The one exception every chlab failure inherits from.

Front ends catch :class:`ChlabError` and print it. That makes error handling total by
construction: a new exception type cannot be forgotten in a hand-maintained tuple and reach
the user as a traceback.
"""

from __future__ import annotations


class ChlabError(Exception):
    """Base class for every error chlab raises deliberately."""
