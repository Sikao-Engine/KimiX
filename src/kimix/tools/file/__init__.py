"""kimix file tools package.

The unused `Mkdir` / `Rm` demo classes that used to live here were removed in
the built-in tools review (FP-03): they were reachable from no agent manifest,
had no tests, no native-shim mirror and no documentation, so registering them
would have granted the model a file-deletion capability for no demonstrated
need. See reviews/tools/93-orphans-and-removal.md.
"""
