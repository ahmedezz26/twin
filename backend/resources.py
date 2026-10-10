import json
from pathlib import Path

from pypdf import PdfReader

# Next to this file, so loading works whatever folder the server is started from
DATA_DIR = Path(__file__).parent / "data"

# Read LinkedIn PDF
try:
    reader = PdfReader(DATA_DIR / "linkedin.pdf")
    linkedin = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            linkedin += text
except FileNotFoundError:
    linkedin = "LinkedIn profile not available"

# Read resume PDF
try:
    reader = PdfReader(DATA_DIR / "resume.pdf")
    resume = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            resume += text
except FileNotFoundError:
    resume = "resume is not available"

# Read other data files
with open(DATA_DIR / "summary.txt", "r", encoding="utf-8") as f:
    summary = f.read()

with open(DATA_DIR / "style.txt", "r", encoding="utf-8") as f:
    style = f.read()

with open(DATA_DIR / "facts.json", "r", encoding="utf-8") as f:
    facts = json.load(f)
