"""Hardware test package marker.

Keeps pytest module names in this directory package-qualified
(``hardware.test_*``) so a same-named file under ``tests/unit/`` cannot
collide with a hardware module during collection.
"""
