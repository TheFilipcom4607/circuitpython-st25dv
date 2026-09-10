"""Make ``st25dv`` importable on the desktop.

The driver imports CircuitPython-only modules. ``tests/stubs`` supplies just
enough of each to run every pure-logic path under CPython, so the parsing,
encoding and bounds-checking code is covered by CI. It does not replace
``test_st25dv.py``, which must still be run on a board: only real CircuitPython
catches the constructs (``0xFE in bytearray``, ``[::-1]``) that CPython accepts
and the firmware does not.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "stubs"))
sys.path.insert(0, os.path.dirname(_HERE))
