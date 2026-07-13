# -*- coding: utf-8 -*-
"""``python -m stoker_metrics`` — run the Stoker metrics engine."""

from __future__ import absolute_import

import sys

from .engine import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
