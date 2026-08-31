"""Supply-chain and infrastructure analysis.

The one thing this package exists to fix: ``ignore_paths`` excludes every lockfile
from the review, correctly -- a lockfile is generated data, and sending one to a
model spends thousands of tokens asking it to do a diff badly. But excluding it
also means a review cannot see an unexpected transitive package, a registry swap,
a checksum that quietly disappeared, or a manifest edit that never reached the
lock, and those are exactly the changes an attacker wants nobody looking at.

So the lockfile stays out of the prompt and a deterministic parser reads it
instead, producing a bounded semantic delta. Everything else here follows the
rules the rest of roborak already keeps: nothing is installed, nothing reaches the
network, and what could not be analysed says so rather than staying silent.
"""

from roborak.supply.analyzer import SCANNERS, analyse

__all__ = ["SCANNERS", "analyse"]
