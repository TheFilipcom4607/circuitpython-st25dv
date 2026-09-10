## Releasing

`.github/workflows/release_gh.yml` builds the bundle zips and attaches them to
a published GitHub release, using Adafruit's `circuitpython-build-tools`. The
tag is the only source of truth for the version: `__version__` in `st25dv.py`
and `version` in `pyproject.toml` both read `0.0.0+auto.0` in a checkout, and
the build rewrites that literal to the tag.

To cut a release, push a plain semver tag — no `v` prefix, because `circup`
parses the tag as a version — then publish a GitHub release for it. The tag
alone does nothing; the workflow fires on the release being *published*:

```bash
git tag 1.0.0 && git push origin 1.0.0
```

The workflow then attaches six assets:

```
circuitpython-st25dv-py-1.0.0.zip           source, lib/st25dv.py
circuitpython-st25dv-9.x-mpy-1.0.0.zip      compiled for CircuitPython 9.x
circuitpython-st25dv-10.x-mpy-1.0.0.zip     compiled for CircuitPython 10.x
circuitpython-st25dv-examples-1.0.0.zip     examples/
circuitpython-st25dv-1.0.0.json             bundle metadata for circup
z-build_tools_version-*.ignore              which build-tools cut the release
```

A second job then extracts the compiled module from those zips and attaches it
as a bare `st25dv.mpy`, so the release page offers a file that can be dropped
into `lib/` without unzipping anything. It is extracted rather than compiled
again, so it is identical to what `circup` installs by construction. The 9.x
and 10.x builds are currently byte-identical, both being mpy v6, so one
unsuffixed file covers either; if they ever diverge the job attaches
`st25dv-9.x.mpy` and `st25dv-10.x.mpy` instead rather than shipping one build
under a name that claims both.

Those names are what `circup bundle-add TheFilipcom4607/circuitpython-st25dv`
expects, and they are derived from the repository name — so the repository has
to be called `circuitpython-st25dv`, matching `pyproject.toml` and the badges
above, and renaming it later breaks `circup` until the next release.

`requirements.txt` must exist at the repo root even though the driver has no
dependencies: Adafruit's `actions-ci/install.sh` runs `pip install -r
requirements.txt` with no existence check, and the build dies at that step
without it.

Two more things to know. GitHub runs release-triggered workflows from the copy
of the file on the default branch, so the workflow has to be on `main` before a
release will build anything. And a release build checks out the *tag*, not
`main`, so a fix to the build itself only takes effect in a new tag — re-running
a failed job against an old tag just rebuilds the old tree.
