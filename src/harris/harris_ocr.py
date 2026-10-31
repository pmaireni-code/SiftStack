import os, sys, glob, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ["OMP_THREAD_LIMIT"] = "1"
src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)


def ocr(pdf):
    name = os.path.basename(pdf)[:-4]
    dst = os.path.join(out, name + ".txt")
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return name
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", "300", "-gray", "-png", pdf, os.path.join(td, "p")], check=True)
        text = "".join(
            subprocess.run(["tesseract", png, "stdout"], capture_output=True, text=True).stdout
            for png in sorted(glob.glob(os.path.join(td, "p-*.png")))
        )
    open(dst, "w").write(text)
    return name


with ThreadPoolExecutor(4) as ex:
    for n in ex.map(ocr, sorted(glob.glob(os.path.join(src, "*.pdf")))):
        print(n, flush=True)
