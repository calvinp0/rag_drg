#!/usr/bin/env python3
"""Download the Molpro online manual and turn it into one PDF.

    python scripts/molpro_manual_pdf.py --out sources/molpro/2026/molpro_manual_web.pdf
    python scripts/molpro_manual_pdf.py --help

`rag-drg fetch --manuals` does the same for the Molpro source in rag_drg.yaml. The code is in
rag_drg/dokuwiki_pdf.py; run this with the Python that has rag-drg installed.
"""
import sys

from rag_drg.dokuwiki_pdf import main

if __name__ == "__main__":
    sys.exit(main())
