# -*- coding: utf-8 -*-
"""``python -m stoker_rawreplay`` — run the PISTON raw-replay engine."""

from __future__ import absolute_import

import sys

from .engine import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
