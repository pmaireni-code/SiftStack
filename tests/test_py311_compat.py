"""Every Harris script must compile on Python 3.11 (2026-11-16: the weekly run got 3.11 and failed on an
f-string with nested double quotes, valid only from 3.12). Skipped when no python3.11 is installed."""
import glob, os, shutil, subprocess, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY311 = shutil.which("python3.11")


@unittest.skipUnless(PY311, "python3.11 not installed")
class Py311Compat(unittest.TestCase):
    def test_harris_compiles_on_311(self):
        files = sorted(glob.glob(os.path.join(ROOT, "src", "harris", "**", "*.py"), recursive=True))
        self.assertTrue(files)
        bad = []
        for f in files:
            r = subprocess.run([PY311, "-c", "import sys, ast; ast.parse(open(sys.argv[1]).read(), sys.argv[1])", f],
                               capture_output=True, text=True)
            if r.returncode:
                bad.append(f"{os.path.relpath(f, ROOT)}: {r.stderr.strip().splitlines()[-1]}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
